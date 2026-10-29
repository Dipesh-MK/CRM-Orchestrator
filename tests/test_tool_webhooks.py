import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.core.storage import get_storage

@pytest.mark.asyncio
async def test_tool_webhooks_crud_and_test():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Create a webhook
        res = await client.post("/api/tools/webhooks", json={
            "name": "Zapier CRM",
            "url": "http://localhost:3000/health",
            "enabled": True
        })
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        wh = data["webhook"]
        wh_id = wh["id"]
        assert wh["name"] == "Zapier CRM"

        # 2. List webhooks
        res = await client.get("/api/tools/webhooks")
        assert res.status_code == 200
        wh_list = res.json()["webhooks"]
        assert any(w["id"] == wh_id for w in wh_list)

        # 3. Test connection probe
        res = await client.post(f"/api/tools/webhooks/{wh_id}/test")
        assert res.status_code == 200
        test_data = res.json()
        assert "status" in test_data
        assert "message" in test_data

        # 4. Delete webhook
        res = await client.delete(f"/api/tools/webhooks/{wh_id}")
        assert res.status_code == 200
        assert res.json()["status"] == "deleted"

        # Verify gone
        res = await client.get("/api/tools/webhooks")
        assert not any(w["id"] == wh_id for w in res.json()["webhooks"])

@pytest.mark.asyncio
async def test_chat_sessions_api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        storage = get_storage()
        test_session_id = "session_test_chat_history_1"
        storage.save_session(test_session_id, "default_workspace", campaign_name="Test Campaign")
        storage.log_user_action(test_session_id, "user_message", {"message": "Hello campaign"})

        # List sessions
        res = await client.get("/api/chat/sessions")
        assert res.status_code == 200
        sessions = res.json()["sessions"]
        assert any(s["id"] == test_session_id for s in sessions)

        # Get session details
        res = await client.get(f"/api/chat/sessions/{test_session_id}")
        assert res.status_code == 200
        sess_data = res.json()
        assert sess_data["session_id"] == test_session_id
        assert len(sess_data["actions"]) >= 1

        # Delete session
        res = await client.delete(f"/api/chat/sessions/{test_session_id}")
        assert res.status_code == 200
        assert res.json()["status"] == "deleted"

        # Verify deletion
        res = await client.get("/api/chat/sessions")
        assert not any(s["id"] == test_session_id for s in res.json()["sessions"])

@pytest.mark.asyncio
async def test_chat_session_title_editing_and_auto_naming():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        storage = get_storage()
        sid = "session_test_rename_456"
        # 1. Save session with auto title
        storage.save_session(sid, "default_workspace", meta={"auto_title": "Dental Clinic Sale"})
        
        # 2. Check list_sessions provides display_title
        res = await client.get("/api/chat/sessions")
        assert res.status_code == 200
        sess_list = res.json()["sessions"]
        matched = next((s for s in sess_list if s["id"] == sid), None)
        assert matched is not None
        assert matched["display_title"] == "Dental Clinic Sale"
        
        # 3. Rename via PATCH /api/chat/sessions/{session_id}
        res = await client.patch(f"/api/chat/sessions/{sid}", json={"title": "VIP Summer Dental Special"})
        assert res.status_code == 200
        assert res.json()["title"] == "VIP Summer Dental Special"
        
        # 4. Verify list_sessions reflects custom edited title
        res = await client.get("/api/chat/sessions")
        sess_list = res.json()["sessions"]
        matched = next((s for s in sess_list if s["id"] == sid), None)
        assert matched["display_title"] == "VIP Summer Dental Special"
        
        # 5. Clean up
        await client.delete(f"/api/chat/sessions/{sid}")

@pytest.mark.asyncio
async def test_tool_webhooks_handshake():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Invalid scheme
        res = await client.post("/api/tools/webhooks/handshake", json={"url": "ftp://example.com"})
        assert res.status_code == 400

        # Handshake probe against local mock endpoint or health
        res = await client.post("/api/tools/webhooks/handshake", json={
            "url": "http://127.0.0.1:8000/api/health",
            "headers": {"X-Custom-Test": "HandshakeCheck"}
        })
        assert res.status_code == 200
        data = res.json()
        assert "status" in data
        assert "latency_ms" in data
        assert "message" in data

