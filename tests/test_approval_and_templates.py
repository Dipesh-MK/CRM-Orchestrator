import pytest
import hmac
import json
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from app.main import app
from app.api.chat_routes import get_or_create_state, select_tone, ToneSelectRequest
from app.core.storage import get_storage
from app.core.config import get_config

client = TestClient(app)

@pytest.mark.asyncio
async def test_3_message_campaign_returns_3_templates_in_sse():
    """Verify that a 3-message campaign returns all 3 template steps in the SSE payload."""
    session_id = "test_sse_3msg_suite"
    state = get_or_create_state(session_id)
    state["business_type"] = "Specialty Coffee Roaster"
    state["slots"] = {
        "business_name": {"value": "BeanCraft"},
        "campaign_name": {"value": "Holiday Blend Launch"},
        "messages_per_contact": {"value": 3}
    }

    mock_templates = [
        {"message_index": 1, "stage": "Announcement", "body": "Try BeanCraft Holiday Blend! Reply STOP to unsubscribe.", "word_count": 9, "word_limit": 320, "compliant": True, "tone": "Warm & Friendly"},
        {"message_index": 2, "stage": "Reminder", "body": "BeanCraft Holiday Blend is roasting fresh today! Reply STOP to unsubscribe.", "word_count": 11, "word_limit": 320, "compliant": True, "tone": "Warm & Friendly"},
        {"message_index": 3, "stage": "Final Call", "body": "Last batch of BeanCraft Holiday Blend this season! Reply STOP to unsubscribe.", "word_count": 11, "word_limit": 320, "compliant": True, "tone": "Warm & Friendly"}
    ]

    req = ToneSelectRequest(session_id=session_id, tone_id="tone_warm", tone_name="Warm & Friendly")

    with patch("app.api.chat_routes.engine.generate_plan", new=AsyncMock(return_value={"steps": []})), \
         patch("app.api.chat_routes.engine.generate_and_validate_templates", new=AsyncMock(return_value={"templates": mock_templates, "recommended_schedule": "Day 1 -> Day 3 -> Day 5"})):

        response = await select_tone(req)
        templates_ready_event = None

        async for chunk in response.body_iterator:
            for line in chunk.split("\n\n"):
                line = line.strip()
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    if ev.get("type") == "templates_ready":
                        templates_ready_event = ev

        assert templates_ready_event is not None
        assert len(templates_ready_event["templates"]) == 3
        indices = [t["message_index"] for t in templates_ready_event["templates"]]
        assert indices == [1, 2, 3]
        assert "csrf_token" in templates_ready_event
        assert len(templates_ready_event["csrf_token"]) >= 16

def test_approve_campaign_success_and_storage_marked():
    """Verify that approving a campaign with valid CSRF, Host, and Origin sets approved=1 and returns 200."""
    session_id = "test_approve_success_sess"
    storage = get_storage()
    state = get_or_create_state(session_id)
    state["slots"]["business_name"] = {"value": "BeanCraft"}
    state["slots"]["campaign_name"] = {"value": "Holiday Blend"}
    state["selected_tone"] = "Warm & Friendly"
    state["templates"] = [
        {"message_index": 1, "stage": "Launch", "body": "Sample body Reply STOP to unsubscribe.", "word_count": 7, "word_limit": 320, "compliant": True}
    ]
    csrf_token = state["csrf_token"]

    resp = client.post(
        "/api/campaign/approve",
        json={"session_id": session_id, "csrf_token": csrf_token},
        headers={
            "Host": "localhost:8000",
            "Origin": "http://localhost:8000",
            "X-CSRF-Token": csrf_token
        }
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["approved"] is True

    # Check persistence in database
    saved = storage.get_campaign(session_id)
    assert saved is not None
    assert saved["approved"] == 1
    assert state["approved"] is True

def test_approve_campaign_wrong_or_missing_csrf_refused():
    """Verify that missing or invalid CSRF tokens are rejected with HTTP 403."""
    session_id = "test_approve_csrf_sess"
    state = get_or_create_state(session_id)
    valid_csrf = state["csrf_token"]

    # 1. Missing CSRF
    resp_missing = client.post(
        "/api/campaign/approve",
        json={"session_id": session_id},
        headers={"Host": "localhost:8000", "Origin": "http://localhost:8000"}
    )
    assert resp_missing.status_code == 403
    assert "CSRF" in resp_missing.text

    # 2. Wrong CSRF
    resp_wrong = client.post(
        "/api/campaign/approve",
        json={"session_id": session_id, "csrf_token": "wrong_csrf_token_1234"},
        headers={"Host": "localhost:8000", "Origin": "http://localhost:8000", "X-CSRF-Token": "wrong_csrf_token_1234"}
    )
    assert resp_wrong.status_code == 403
    assert "CSRF" in resp_wrong.text

def test_approve_campaign_foreign_origin_and_host_refused():
    """Verify that foreign Origins and spoofed Hosts are rejected with HTTP 403."""
    session_id = "test_approve_security_sess"
    state = get_or_create_state(session_id)
    csrf_token = state["csrf_token"]

    # 1. Foreign Origin
    resp_origin = client.post(
        "/api/campaign/approve",
        json={"session_id": session_id, "csrf_token": csrf_token},
        headers={"Host": "localhost:8000", "Origin": "http://malicious-site.com", "X-CSRF-Token": csrf_token}
    )
    assert resp_origin.status_code == 403
    assert "Foreign Origin" in resp_origin.text

    # 2. Spoofed Host
    resp_host = client.post(
        "/api/campaign/approve",
        json={"session_id": session_id, "csrf_token": csrf_token},
        headers={"Host": "evil-domain.com", "Origin": "http://localhost:8000", "X-CSRF-Token": csrf_token}
    )
    assert resp_host.status_code == 403
    assert "Host" in resp_host.text

def test_approve_campaign_both_loopback_hosts_accepted():
    """Verify that both localhost:8000 and 127.0.0.1:8000 are accepted."""
    session_id_1 = "test_loopback_lh"
    state1 = get_or_create_state(session_id_1)
    csrf1 = state1["csrf_token"]

    resp1 = client.post(
        "/api/campaign/approve",
        json={"session_id": session_id_1, "csrf_token": csrf1},
        headers={"Host": "localhost:8000", "Origin": "http://localhost:8000", "X-CSRF-Token": csrf1}
    )
    assert resp1.status_code == 200

    session_id_2 = "test_loopback_ip"
    state2 = get_or_create_state(session_id_2)
    csrf2 = state2["csrf_token"]

    resp2 = client.post(
        "/api/campaign/approve",
        json={"session_id": session_id_2, "csrf_token": csrf2},
        headers={"Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:8000", "X-CSRF-Token": csrf2}
    )
    assert resp2.status_code == 200
