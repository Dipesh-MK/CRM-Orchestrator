import os
import json
import pytest
from starlette.testclient import TestClient

from app.services.orchestrator_engine import parse_json_robust, normalize_discovery_output
from app.main import app

@pytest.fixture(autouse=True)
def setup_test_env():
    os.environ["DATABASE_PATH"] = "data/test_campaignforge.db"
    os.environ["LOGS_DIR"] = "logs/test_logs"
    yield

def test_parse_json_robust_malformed_unclosed_braces():
    # Exact raw response from real session attempt 0 (missing closing brace for slots)
    raw_attempt_0 = """```json
{
  "business_type": "Retail (Vegetable Vendor)",
  "slots": {
    "business_name": {
      "value": "Vegetable Vendor",
      "required": true,
      "confidence": 0.9,
      "source": "user"
    },
    "campaign_name": {
      "value": "Vegetable Vendor Clearance Sale",
      "required": true,
      "confidence": 0.9,
      "source": "user"
    },
    "offer_or_cta": {
      "value": "25% off on everything as clearance sale",
      "required": true,
      "confidence": 0.9,
      "source": "user"
    },
    "discovery_complete": true,
    "assumptions_made": [],
    "next_question_to_user": ""
}
```"""
    parsed = parse_json_robust(raw_attempt_0)
    assert parsed is not None
    assert "business_type" in parsed
    assert "slots" in parsed

def test_parse_json_robust_trailing_commas():
    raw_with_commas = """
    {
      "candidate_tones": [
        {"id": "tone_1", "name": "Urgent Clearance", "description": "Last chance",},
      ],
    }
    """
    parsed = parse_json_robust(raw_with_commas)
    assert parsed is not None
    assert len(parsed["candidate_tones"]) == 1
    assert parsed["candidate_tones"][0]["id"] == "tone_1"

def test_normalize_discovery_output_hoisting_from_slots():
    # Model mistakenly placed discovery_complete & next_question_to_user inside slots
    raw_data = {
        "business_type": "Retail",
        "slots": {
            "business_name": {"value": "Fresh Greens"},
            "offer_or_cta": {"value": "25% off storewide"},
            "discovery_complete": True,
            "assumptions_made": ["Assumed 1-week timeline"],
            "next_question_to_user": ""
        }
    }
    normalized = normalize_discovery_output(raw_data)
    assert normalized["discovery_complete"] is True
    assert normalized["next_question_to_user"] == ""
    assert "discovery_complete" not in normalized["slots"]
    assert "assumptions_made" not in normalized["slots"]
    assert len(normalized["assumptions_made"]) == 1

def test_normalize_discovery_output_incomplete_with_question_hoisted():
    raw_data = {
        "business_type": "Retail",
        "slots": {
            "offer_or_cta": {"value": "25% discount"},
            "discovery_complete": False,
            "next_question_to_user": "What is the name of your vegetable shop?"
        }
    }
    normalized = normalize_discovery_output(raw_data)
    assert normalized["discovery_complete"] is False
    assert normalized["next_question_to_user"] == "What is the name of your vegetable shop?"
    assert "next_question_to_user" not in normalized["slots"]

def test_normalize_discovery_output_guarantees_non_empty_question_when_incomplete():
    # If model returns incomplete without formulating a question
    raw_data = {
        "business_type": "Retail",
        "slots": {},
        "discovery_complete": False,
        "next_question_to_user": ""
    }
    normalized = normalize_discovery_output(raw_data)
    assert normalized["discovery_complete"] is False
    assert len(normalized["next_question_to_user"].strip()) > 0
    assert "business" in normalized["next_question_to_user"].lower()

def test_chat_send_never_yields_empty_text_chunk(monkeypatch):
    client = TestClient(app)

    # Mock engine.run_discovery_round returning incomplete discovery with empty question
    from app.services.orchestrator_engine import OrchestratorEngine
    async def mock_discovery(*args, **kwargs):
        return {
            "business_type": "Retail",
            "slots": {},
            "discovery_complete": False,
            "next_question_to_user": ""
        }

    monkeypatch.setattr(OrchestratorEngine, "run_discovery_round", mock_discovery)

    res = client.post("/api/chat/send", json={"message": "an sms for a vegetable vendor"})
    assert res.status_code == 200

    # Parse SSE events
    lines = res.text.split("\n\n")
    text_chunks = []
    for line in lines:
        if line.startswith("data: "):
            payload = json.loads(line[6:])
            if payload.get("type") == "text_chunk":
                text_chunks.append(payload.get("text", ""))

    assert len(text_chunks) > 0
    for chunk in text_chunks:
        assert chunk.strip() != "", "text_chunk must never be empty or whitespace"
