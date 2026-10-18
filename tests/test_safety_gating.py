import pytest
from app.services.safety_service import get_safety_service, is_chit_chat_or_ack

def test_is_chit_chat_detection():
    assert is_chit_chat_or_ack("hi") is True
    assert is_chit_chat_or_ack("Hello there") is True
    assert is_chit_chat_or_ack("ok") is True
    assert is_chit_chat_or_ack("sounds good") is True
    assert is_chit_chat_or_ack("thanks") is True
    assert is_chit_chat_or_ack("We are selling illegal prescription narcotics online") is False
    assert is_chit_chat_or_ack("Our company Green Earth is launching a donation drive") is False

@pytest.mark.asyncio
async def test_safety_skipped_on_chit_chat():
    safety = get_safety_service()
    res = await safety.evaluate_safety(
        session_id="test_safety_gating_sess",
        text_to_evaluate="Sounds good, continue",
        context_label="discovery_input"
    )
    assert res["allowed"] is True
    assert res.get("skipped_llm") is True

@pytest.mark.asyncio
async def test_safety_enforced_on_template():
    safety = get_safety_service()
    res = await safety.evaluate_safety(
        session_id="test_safety_gating_sess",
        text_to_evaluate="Hello! Please donate to Green Earth Foundation for flood relief. Reply STOP to unsubscribe.",
        context_label="template_1"
    )
    assert res["allowed"] is True
    # For templates, skipped_llm should be False because templates always go through policy evaluation
    assert res.get("skipped_llm", False) is False
