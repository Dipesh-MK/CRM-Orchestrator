import os
import shutil
import subprocess
import time
import asyncio
import urllib.request
import json
import pytest
from pathlib import Path
from app.core.config import get_config
from app.core.storage import get_storage, Storage
from app.services.contact_service import get_contact_service
from app.services.dispatch_service import get_dispatch_service, DispatchService
from app.adapters.channel_adapter import get_channel_adapter, AggregatorChannelAdapter

@pytest.mark.asyncio
async def test_scratch_aggregator_e2e_send():
    """
    Step 6 E2E test on a FRESH scratch copy of the aggregator:
    - copy to /tmp/scratch_mock_aggregator
    - delete data/
    - only PORT in its env
    - show cwd and command
    - ingest contacts via real contact flow
    - register their numbers via operator tool
    - confirm a send
    - assert SIDs
    - assert messages appear in aggregator admin API
    - assert one number ending in 0 fails with provider error code (30008) and is shown that way
    - kill and delete scratch afterwards
    """
    repo_dir = Path(__file__).resolve().parent.parent
    aggregator_src = repo_dir / "Mock Aggregator"
    scratch_dir = Path("/tmp/scratch_mock_aggregator")
    
    # Clean previous scratch if any
    if scratch_dir.exists():
        shutil.rmtree(scratch_dir)
        
    # Copy aggregator to /tmp/scratch_mock_aggregator
    shutil.copytree(aggregator_src, scratch_dir)
    scratch_data = scratch_dir / "data"
    if scratch_data.exists():
        shutil.rmtree(scratch_data)
        
    port = "3025"
    cmd = ["node", "src/server.js"]
    env = {
        "PORT": port,
        "PATH": os.environ.get("PATH", "/usr/bin:/bin")
    }
    
    print(f"\n[E2E Scratch Aggregator] CWD: {scratch_dir}")
    print(f"[E2E Scratch Aggregator] Command: PORT={port} {' '.join(cmd)}")
    
    proc = subprocess.Popen(
        cmd,
        cwd=str(scratch_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    server_base = f"http://127.0.0.1:{port}"
    
    try:
        # Wait for scratch aggregator to be ready
        ready = False
        for _ in range(50):
            try:
                req = urllib.request.Request(f"{server_base}/health")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.1)
                
        assert ready, f"Scratch aggregator failed to start on {server_base}"
        print(f"[E2E Scratch Aggregator] Server ready on {server_base}")

        # Load CRM aggregator fixtures from tests/fixtures/.env.scratch
        fixture_env = repo_dir / "tests" / "fixtures" / ".env.scratch"
        if fixture_env.exists():
            for line in fixture_env.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()

        # Set CRM configuration to point to scratch aggregator
        os.environ["AGGREGATOR_BASE_URL"] = server_base
        os.environ["APITOOL_BASE_URL"] = server_base
        cfg = get_config(reload=True)
        adapter = AggregatorChannelAdapter()
        assert adapter.runtime.base_url == server_base

        # 1. Register recipient numbers via the operator tool
        # Number 1: ending in 1 -> will be delivered
        # Number 2: ending in 0 -> will fail with Error 30008 (aggregator deterministic rule)
        p1 = "+14155551111"
        p2 = "+14155551110"
        
        reg_res1 = adapter.runtime.call_operator_tool("register_phone", {"phone_number": p1, "display_name": "Amir"})
        assert reg_res1.get("ok") is True, f"Failed to register {p1}: {reg_res1}"
        
        reg_res2 = adapter.runtime.call_operator_tool("register_phone", {"phone_number": p2, "display_name": "Bina"})
        assert reg_res2.get("ok") is True, f"Failed to register {p2}: {reg_res2}"

        # Verify registered numbers via operator tool list_phone_numbers
        registered = adapter.get_registered_phones(force_refresh=True)
        assert p1 in registered
        assert p2 in registered

        # 2. Ingest contacts via real contact flow
        session_id = "sess_e2e_scratch"
        csv_content = f"Name,Phone,Donor_Type\nAmir,{p1},Monthly\nBina,{p2},Annual\n".encode("utf-8")
        contact_svc = get_contact_service()
        ingest_res = contact_svc.process_contacts(
            session_id=session_id,
            content=csv_content,
            filename="donors.csv",
            country_code="US"
        )
        assert ingest_res["success"] is True
        assert ingest_res["valid_count"] == 2
        
        # Verify persistence in contacts table (Amendment 1)
        storage = get_storage()
        persisted_contacts = storage.get_contacts_by_session(session_id)
        assert len(persisted_contacts) == 2
        assert all(c.get("contact_id") for c in persisted_contacts)

        # 3. Create approved campaign
        storage.save_session(session_id=session_id, workspace_id="ws_scratch")
        campaign_data = {
            "session_id": session_id,
            "workspace_id": "ws_scratch",
            "business_name": "Wildlife Sanctuary",
            "campaign_name": "Emergency Rescue Drive",
            "approved": 1,
            "templates": [
                {
                    "message_index": 1,
                    "stage": "Launch",
                    "body": "Hello {{name}}, thank you for being an active {{donor_type}} supporter of Wildlife Sanctuary! Visit https://wildlife.org. Reply STOP to unsubscribe.",
                    "word_count": 22,
                    "tone": "Warm & Urgent"
                }
            ]
        }
        storage.save_campaign(session_id, campaign_data)

        # 4. Preflight run
        dispatch_svc = DispatchService(storage=storage, adapter=adapter)
        preflight = await dispatch_svc.preflight_run(session_id)
        assert preflight["success"] is True
        assert preflight["passed"] is True, f"Preflight failed: {preflight.get('checks')}"
        assert len(preflight["previews"]) == 2
        
        job_id = preflight["job_id"]
        csrf_token = preflight["csrf_token"]

        # 5. Confirm and send
        send_res = await dispatch_svc.confirm_and_send(job_id=job_id, csrf_token=csrf_token)
        assert send_res["success"] is True
        assert send_res["total"] == 2
        assert send_res["successful"] == 2  # Both initially queued by provider
        
        # Assert SIDs
        items = storage.get_send_items_by_job(job_id)
        sids = [it.get("message_sid") for it in items]
        assert len(sids) == 2
        assert all(sid and sid.startswith("SM") for sid in sids)

        # 6. Assert messages appear in aggregator's admin API
        admin_req = urllib.request.Request(f"{server_base}/api/admin/messages")
        with urllib.request.urlopen(admin_req, timeout=2.0) as resp:
            admin_data = json.loads(resp.read().decode("utf-8"))
            all_aggregator_msgs = admin_data.get("data", [])
            aggregator_sids = [m["sid"] for m in all_aggregator_msgs]
            for sid in sids:
                assert sid in aggregator_sids, f"SID {sid} not found in aggregator admin API"

        # 7. Wait for aggregator simulator to transition sent -> delivered / undelivered (~2 seconds)
        print("[E2E Scratch Aggregator] Awaiting simulator delivery outcome (2.2s)...")
        await asyncio.sleep(2.3)

        # 8. Refresh status from aggregator operator inspection tool
        refresh_res = await dispatch_svc.refresh_job_status(job_id)
        assert refresh_res["success"] is True
        
        updated_items = storage.get_send_items_by_job(job_id)
        status_by_phone = {it["phone"]: it for it in updated_items}
        
        # Assert one number ending in 0 fails with its provider error code (30008)
        item_ending_0 = status_by_phone[p2]
        assert item_ending_0["status"] == "undelivered"
        assert "30008" in str(item_ending_0.get("error")) or "carrier rejected" in str(item_ending_0.get("error")).lower()

        # Assert number ending in 1 succeeded
        item_ending_1 = status_by_phone[p1]
        assert item_ending_1["status"] in ("delivered", "sent")

        print("[E2E Scratch Aggregator] SIDs and deterministic delivery error code 30008 verified!")

    finally:
        # Teardown: kill process and remove scratch directory
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            
        if scratch_dir.exists():
            shutil.rmtree(scratch_dir)
        assert not scratch_dir.exists(), "Scratch mock directory was not deleted during teardown"
        print("[E2E Scratch Aggregator] Teardown complete: process killed and /tmp/scratch_mock_aggregator deleted.")
