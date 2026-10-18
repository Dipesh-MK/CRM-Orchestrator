import time
import json
import httpx
from fastapi.testclient import TestClient
from app.main import app
from app.api.chat_routes import get_or_create_state

print(f"[{time.strftime('%X')}] Starting TestClient...")
t0 = time.time()
client = TestClient(app, raise_server_exceptions=False)
print(f"[{time.strftime('%X')}] TestClient started in {time.time() - t0:.2f}s")

session_id = "reproduce_sess_001"
state = get_or_create_state(session_id)
state["workspace_id"] = "default_workspace"
state["slots"]["business_name"] = {"value": "Acme Animal Rescue"}
state["slots"]["campaign_name"] = {"value": "Winter Drive"}
state["selected_tone"] = "Urgent & Direct"
state["templates"] = [
    {
        "message_index": 1,
        "stage": "Launch",
        "body": "Hello {{name}}, help us save animals this winter! Reply STOP to unsubscribe.",
        "word_count": 12,
        "word_limit": 320,
        "compliant": True
    }
]

print(f"[{time.strftime('%X')}] Sending POST /api/campaign/approve...")
t1 = time.time()
resp = client.post(
    "/api/campaign/approve",
    json={"session_id": session_id},
    headers={"Origin": "http://localhost:8000", "Host": "localhost:8000"}
)
print(f"[{time.strftime('%X')}] Finished in {time.time() - t1:.2f}s")
print("STATUS CODE:", resp.status_code)
print("RESPONSE BODY:", resp.text)
