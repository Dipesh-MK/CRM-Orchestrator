import os
import sqlite3
import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
from app.core.config import get_config
from app.core.storage import get_storage, Storage
from app.services.dispatch_service import get_dispatch_service, render_template, DispatchService
from app.adapters.channel_adapter import get_channel_adapter, AggregatorChannelAdapter, FORBIDDEN_AGENT_WRITE_TOOLS

@pytest.fixture(autouse=True)
def setup_dispatch_test_env(tmp_path):
    test_db = tmp_path / "test_dispatch.db"
    test_logs = tmp_path / "logs"
    test_logs.mkdir(parents=True, exist_ok=True)
    os.environ["DATABASE_PATH"] = str(test_db)
    os.environ["LOGS_DIR"] = str(test_logs)
    os.environ["DISPATCH_ENABLED"] = "true"
    os.environ["DISPATCH_REQUIRE_REGISTERED_PHONE"] = "true"
    os.environ["DISPATCH_ALLOW_NON_LOOPBACK"] = "false"
    
    # Reload config and storage
    get_config(reload=True)
    storage = Storage(db_path=str(test_db), logs_dir=str(test_logs))
    
    # Pre-seed suppression list file
    supp_file = tmp_path / "suppression.txt"
    supp_file.write_text("+14155559999\n+14155558888\n")
    os.environ["SUPPRESSION_FILE"] = str(supp_file)
    
    yield {"db": test_db, "logs": test_logs, "storage": storage}

class DummyMockAdapter:
    def __init__(self, registered_phones=None, fail_registry=False):
        self.registered_phones = registered_phones or ["+14155550001", "+14155550002"]
        self.fail_registry = fail_registry
        self.runtime = type("Runtime", (), {"base_url": "http://localhost:3000"})()
        self.sent_calls = []

    def get_registered_phones(self, force_refresh=False):
        if self.fail_registry:
            raise RuntimeError("Aggregator phone registry unreachable")
        return list(self.registered_phones)

    def stage_send(self, to, body, session_id, status_callback=None, job_id="", item_id=""):
        self.sent_calls.append({"to": to, "body": body, "session_id": session_id})
        return {"ok": True, "status": "success", "sid": f"SM_MOCK_{len(self.sent_calls)}"}

    def execute_send(self, *args, **kwargs):
        return {"ok": True, "status": "success", "sid": "SM_MOCK_EXEC"}

    def read_operator_status(self, sid, session_id="system_status", job_id="", item_id=""):
        return {"sid": sid, "status": "delivered"}

def create_approved_campaign(storage, session_id="sess_test_1", template_text="Hello {{name}}, welcome! STOP to unsubscribe."):
    storage.save_session(session_id=session_id, workspace_id="test_ws")
    campaign_dict = {
        "session_id": session_id,
        "workspace_id": "test_ws",
        "business_name": "Test Org",
        "campaign_name": "Summer Drive",
        "approved": 1,
        "templates": [
            {
                "message_index": 1,
                "stage": "Launch",
                "body": template_text,
                "word_count": len(template_text.split()),
                "tone": "Warm & Professional"
            }
        ]
    }
    storage.save_campaign(session_id, campaign_dict)

# ================= 1. PRE-FLIGHT TESTS =================

def test_preflight_unresolved_tag_fails(setup_dispatch_test_env):
    storage = setup_dispatch_test_env["storage"]
    session_id = "sess_unresolved"
    create_approved_campaign(storage, session_id, template_text="Hello {{missing_tag}}! Reply STOP to unsubscribe.")
    
    # Ingest contact with no missing_tag
    storage.save_contacts(session_id, [
        {"contact_id": "c1", "session_id": session_id, "name": "Alice", "phone": "+14155550001", "extra": {}}
    ])
    
    adapter = DummyMockAdapter()
    service = DispatchService(storage=storage, adapter=adapter)
    import asyncio
    report = asyncio.run(service.preflight_run(session_id))
    assert report["success"] is True
    assert report["passed"] is False
    assert report["checks"]["tags_resolved"] is False

def test_preflight_post_personalization_word_limit_fails(setup_dispatch_test_env):
    storage = setup_dispatch_test_env["storage"]
    session_id = "sess_word_limit"
    # Long text in extra
    long_note = "word " * 350
    create_approved_campaign(storage, session_id, template_text="Hello {{name}}, {{note}}. Reply STOP to unsubscribe.")
    
    storage.save_contacts(session_id, [
        {"contact_id": "c1", "session_id": session_id, "name": "Bob", "phone": "+14155550001", "extra": {"note": long_note}}
    ])
    
    adapter = DummyMockAdapter()
    service = DispatchService(storage=storage, adapter=adapter)
    import asyncio
    report = asyncio.run(service.preflight_run(session_id))
    assert report["passed"] is False
    assert report["checks"]["under_word_limit"] is False

def test_preflight_suppression_fails(setup_dispatch_test_env):
    storage = setup_dispatch_test_env["storage"]
    session_id = "sess_suppressed"
    create_approved_campaign(storage, session_id)
    
    # Phone in suppression list (+14155559999)
    storage.save_contacts(session_id, [
        {"contact_id": "c1", "session_id": session_id, "name": "Charlie", "phone": "+14155559999", "extra": {}}
    ])
    
    adapter = DummyMockAdapter(registered_phones=["+14155559999"])
    service = DispatchService(storage=storage, adapter=adapter)
    import asyncio
    report = asyncio.run(service.preflight_run(session_id))
    assert report["passed"] is False
    assert report["checks"]["no_suppressed_numbers"] is False

def test_preflight_run_cap_fails(setup_dispatch_test_env):
    storage = setup_dispatch_test_env["storage"]
    session_id = "sess_runcap"
    create_approved_campaign(storage, session_id)
    
    cfg = get_config()
    cfg.dispatch.max_messages_per_run = 2
    
    # Add 3 contacts
    storage.save_contacts(session_id, [
        {"contact_id": f"c{i}", "session_id": session_id, "name": f"User {i}", "phone": f"+1415555000{i}", "extra": {}}
        for i in range(1, 4)
    ])
    
    adapter = DummyMockAdapter(registered_phones=[f"+1415555000{i}" for i in range(1, 4)])
    service = DispatchService(storage=storage, config=cfg, adapter=adapter)
    import asyncio
    report = asyncio.run(service.preflight_run(session_id))
    assert report["passed"] is False
    assert report["checks"]["run_cap_ok"] is False

def test_preflight_unregistered_number_blocked(setup_dispatch_test_env):
    storage = setup_dispatch_test_env["storage"]
    session_id = "sess_unreg"
    create_approved_campaign(storage, session_id)
    
    storage.save_contacts(session_id, [
        {"contact_id": "c1", "session_id": session_id, "name": "David", "phone": "+14155557777", "extra": {}}
    ])
    
    # Adapter only has +14155550001
    adapter = DummyMockAdapter(registered_phones=["+14155550001"])
    service = DispatchService(storage=storage, adapter=adapter)
    import asyncio
    report = asyncio.run(service.preflight_run(session_id))
    assert report["passed"] is False
    assert report["checks"]["registered_in_aggregator"] is False
    assert "UNREGISTERED_PHONE_NUMBER" in report["unregistered_policy_message"] or "register" in report["unregistered_policy_message"]

def test_preflight_registry_unreachable_fails_closed(setup_dispatch_test_env):
    storage = setup_dispatch_test_env["storage"]
    session_id = "sess_unreachable"
    create_approved_campaign(storage, session_id)
    
    storage.save_contacts(session_id, [
        {"contact_id": "c1", "session_id": session_id, "name": "Emma", "phone": "+14155550001", "extra": {}}
    ])
    
    # Adapter throws error on phone registry fetch
    adapter = DummyMockAdapter(fail_registry=True)
    service = DispatchService(storage=storage, adapter=adapter)
    import asyncio
    report = asyncio.run(service.preflight_run(session_id))
    # Fails closed
    assert report["passed"] is False
    assert report["checks"]["registered_in_aggregator"] is False

# ================= 2. SECURITY TESTS =================

def test_security_foreign_origin_refused():
    client = TestClient(app)
    res = client.post(
        "/api/dispatch/send",
        headers={"Host": "localhost:8000", "Origin": "http://malicious-site.com"},
        json={"job_id": "job_123", "csrf_token": "token123"}
    )
    assert res.status_code == 403
    assert "Foreign Origin" in res.json().get("detail", "")

def test_security_missing_origin_refused():
    client = TestClient(app)
    res = client.post(
        "/api/dispatch/send",
        headers={"Host": "localhost:8000"},
        json={"job_id": "job_123", "csrf_token": "token123"}
    )
    assert res.status_code == 403
    assert "Missing Origin" in res.json().get("detail", "")

def test_security_spoofed_host_refused():
    client = TestClient(app)
    res = client.post(
        "/api/dispatch/send",
        headers={"Host": "attacker.com", "Origin": "http://localhost:8000"},
        json={"job_id": "job_123", "csrf_token": "token123"}
    )
    assert res.status_code == 403
    assert "Host" in res.json().get("detail", "")

def test_security_missing_or_wrong_csrf_refused(setup_dispatch_test_env):
    storage = setup_dispatch_test_env["storage"]
    session_id = "sess_csrf_test"
    create_approved_campaign(storage, session_id)
    storage.save_contacts(session_id, [{"contact_id": "c1", "session_id": session_id, "name": "Frank", "phone": "+14155550001", "extra": {}}])
    
    adapter = DummyMockAdapter()
    service = DispatchService(storage=storage, adapter=adapter)
    import asyncio
    pre = asyncio.run(service.preflight_run(session_id))
    job_id = pre["job_id"]
    real_csrf = pre["csrf_token"]
    
    # 1. Wrong CSRF
    res_wrong = asyncio.run(service.confirm_and_send(job_id=job_id, csrf_token="wrong_token_1234567890"))
    assert res_wrong["success"] is False
    assert "CSRF" in res_wrong["error"]

    # 2. Correct CSRF -> succeeds
    res_correct = asyncio.run(service.confirm_and_send(job_id=job_id, csrf_token=real_csrf))
    assert res_correct["success"] is True

def test_security_non_loopback_provider_url_refused():
    cfg = get_config()
    cfg.dispatch.allow_non_loopback = False
    
    # An adapter pointing to external address must raise ValueError
    with pytest.raises(ValueError) as exc:
        adapter = AggregatorChannelAdapter()
        adapter.runtime = type("DummyRuntime", (), {"base_url": "http://api.external-cloud.com"})()
        adapter._verify_loopback()
    assert "Non-loopback provider URL" in str(exc.value)

def test_security_llm_tool_path_produces_zero_requests():
    """Verify that FORBIDDEN_AGENT_WRITE_TOOLS excludes all write and operator tools."""
    adapter = get_channel_adapter()
    agent_tools = adapter.get_agent_tools()
    tool_names = [t["name"] for t in agent_tools]
    
    # Ensure write tools are never exposed to LLM
    assert "send_message" not in tool_names
    assert "execute_approved" not in tool_names
    assert "list_phone_numbers" not in tool_names
    assert "get_admin_message_status" not in tool_names
    for forbidden in FORBIDDEN_AGENT_WRITE_TOOLS:
        assert forbidden not in tool_names

def test_security_forged_contact_id_blocked(setup_dispatch_test_env):
    """Contacts not ingested through the real flow cannot be added to send job."""
    storage = setup_dispatch_test_env["storage"]
    session_id = "sess_forge"
    create_approved_campaign(storage, session_id)
    
    # We do NOT save contact to contacts table
    service = DispatchService(storage=storage)
    import asyncio
    report = asyncio.run(service.preflight_run(session_id))
    assert report["success"] is False
    assert "No contacts found in contacts table" in report["error"]

# ================= 3. AT-MOST-ONCE TESTS =================

def test_at_most_once_crash_recovery_unknown(setup_dispatch_test_env):
    """Items found in 'sending' at startup become 'unknown' and are never resent."""
    db_path = setup_dispatch_test_env["db"]
    logs_path = setup_dispatch_test_env["logs"]
    storage = setup_dispatch_test_env["storage"]
    
    # Create send_job and send_item in 'sending'
    job_id = "job_crash_1"
    item_id = "item_crash_1"
    storage.create_send_job(job_id=job_id, session_id="sess_crash", total_items=1, csrf_token="token")
    storage.create_send_items([{
        "item_id": item_id,
        "job_id": job_id,
        "contact_id": "c1",
        "phone": "+14155550001",
        "status": "sending",
        "message_sid": None,
        "error": None
    }])
    
    # Simulate restart: instantiate new Storage object
    restarted_storage = Storage(db_path=str(db_path), logs_dir=str(logs_path))
    items = restarted_storage.get_send_items_by_job(job_id)
    assert len(items) == 1
    assert items[0]["status"] == "unknown"

def test_at_most_once_restart_never_resends(setup_dispatch_test_env):
    """Attempting to resend a non-pending job fails closed."""
    storage = setup_dispatch_test_env["storage"]
    job_id = "job_interrupted"
    storage.create_send_job(job_id=job_id, session_id="sess_1", total_items=1, csrf_token="tok123")
    storage.update_send_job_status(job_id, "interrupted")
    
    adapter = DummyMockAdapter()
    service = DispatchService(storage=storage, adapter=adapter)
    import asyncio
    res = asyncio.run(service.confirm_and_send(job_id=job_id, csrf_token="tok123"))
    assert res["success"] is False
    assert "cannot be sent" in res["error"]
    assert len(adapter.sent_calls) == 0

def test_single_message_sending_and_resend_lockout(setup_dispatch_test_env):
    """Verifies that only 1 message template is staged and once sent, resend is strictly refused."""
    storage = setup_dispatch_test_env["storage"]
    session_id = "sess_single_send_lockout"
    
    # Create campaign with 3 templates to verify clamp to 1
    create_approved_campaign(storage, session_id)
    campaign = storage.get_campaign(session_id)
    campaign["templates"] = [
        {"message_index": 1, "body": "Msg 1: {{customer_name}}, bill reminder. STOP to optout", "stage": "Touch 1"},
        {"message_index": 2, "body": "Msg 2: {{customer_name}}, outage warning. STOP to optout", "stage": "Touch 2"},
        {"message_index": 3, "body": "Msg 3: {{customer_name}}, power cut notice. STOP to optout", "stage": "Touch 3"}
    ]
    storage.save_campaign(session_id, campaign)
    storage.save_contacts(session_id, [
        {"contact_id": "c1", "session_id": session_id, "name": "Alice", "phone": "+14155550001", "extra": {}},
        {"contact_id": "c2", "session_id": session_id, "name": "Bob", "phone": "+14155550002", "extra": {}}
    ])
    
    adapter = DummyMockAdapter()
    service = DispatchService(storage=storage, adapter=adapter)
    import asyncio
    
    # 1. Preflight verifies only 1 message template per contact (total = 2 messages, not 6)
    pre = asyncio.run(service.preflight_run(session_id))
    assert pre["success"] is True
    assert pre["total_messages"] == 2
    assert pre["sequence_count"] == 1
    
    # 2. Confirm and send
    job_id = pre["job_id"]
    csrf_token = pre["csrf_token"]
    res = asyncio.run(service.confirm_and_send(job_id=job_id, csrf_token=csrf_token))
    assert res["success"] is True
    assert res["successful"] == 2
    assert len(adapter.sent_calls) == 2
    
    # 3. Attempting to resend the same job must be refused
    res_repeat = asyncio.run(service.confirm_and_send(job_id=job_id, csrf_token=csrf_token))
    assert res_repeat["success"] is False
    assert "already been dispatched" in res_repeat["error"]
    
    # 4. Attempting to preflight a new job for the already dispatched campaign must be refused
    pre_repeat = asyncio.run(service.preflight_run(session_id))
    assert pre_repeat["success"] is False
    assert "already been dispatched and cannot be resent" in pre_repeat["error"]

