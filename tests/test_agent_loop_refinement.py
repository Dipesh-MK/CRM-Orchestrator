import pytest
import json
from unittest.mock import patch, AsyncMock
from app.api.chat_routes import get_or_create_state, select_tone, ToneSelectRequest, chat_send, ChatMessageRequest
from app.services.orchestrator_engine import get_orchestrator_engine
from app.services.memory_service import get_memory_service
from app.core.storage import get_storage

@pytest.mark.asyncio
async def test_select_tone_synchronizes_conversation_history():
    """Verify that select_tone records both user selection and assistant generated template in conversation_history."""
    session_id = "test_sess_tone_sync"
    state = get_or_create_state(session_id)
    state["conversation_history"] = [{"role": "user", "content": "Initial prompt"}]
    
    mock_templates = [{
        "message_index": 1,
        "stage": "Announcement",
        "body": "Hello Customer! Reply STOP to cancel.",
        "word_count": 6,
        "word_limit": 160,
        "compliant": True,
        "tone": "Warm & Friendly"
    }]

    req = ToneSelectRequest(session_id=session_id, tone_id="tone_1", tone_name="Warm & Friendly")

    with patch("app.api.chat_routes.engine.generate_plan", new=AsyncMock(return_value={"steps": []})), \
         patch("app.api.chat_routes.engine.generate_and_validate_templates", new=AsyncMock(return_value={"templates": mock_templates, "recommended_schedule": "Day 1"})):
        
        response = await select_tone(req)
        async for _ in response.body_iterator:
            pass

    history = state["conversation_history"]
    assert any("Selected campaign tone: Warm & Friendly" in h.get("content", "") for h in history)
    assert any("Hello Customer!" in h.get("content", "") for h in history)

@pytest.mark.asyncio
async def test_revise_campaign_template_does_not_pollute_must_include():
    """Verify that conversational revisions do not append meta-instructions into must_include."""
    session_id = "test_sess_revise_no_pollute"
    engine = get_orchestrator_engine()

    slots = {
        "business_name": {"value": "Metro Power"},
        "campaign_goal": {"value": "Electricity bill payment reminder"},
        "must_include": {"value": "Due date: Oct 30"}
    }
    current_templates = [{
        "message_index": 1,
        "stage": "Announcement",
        "body": "Metro Power notice: Due date: Oct 30. Reply STOP to cancel.",
        "word_count": 10,
        "word_limit": 160,
        "compliant": True
    }]

    mock_llm_response = {
        "intent": "template_edit",
        "slot_updates": {},
        "revised_body": "Metro Power urgent notice: Due date: Oct 30. Power disconnection warning. Reply STOP to cancel.",
        "changes_summary": "Added urgent disconnection warning."
    }

    async def mock_call(session_id, purpose, **kwargs):
        if purpose == "safety_guard":
            return {"status": "success", "raw_response": json.dumps({"allowed": True, "category": "none", "reason": "Safe content"})}
        return {"status": "success", "raw_response": json.dumps(mock_llm_response)}

    with patch.object(engine.llm, "call_llm", side_effect=mock_call):
        res = await engine.revise_campaign_template(
            session_id=session_id,
            workspace_id="default_workspace",
            business_type="Utility & Energy",
            slots=slots,
            selected_tone="Clear & Direct",
            current_templates=current_templates,
            user_feedback="Make it much more urgent and warn about power outage"
        )

    assert res["action"] == "template_updated"
    # must_include must NOT contain "Make it much more urgent"
    assert "make it much more urgent" not in slots["must_include"]["value"].lower()
    assert slots["must_include"]["value"] == "Due date: Oct 30"
    assert "urgent" in res["templates"][0]["body"].lower()

@pytest.mark.asyncio
async def test_revise_campaign_template_tone_change_intent():
    """Verify that asking for a friendlier tone is classified as tone_change intent."""
    session_id = "test_sess_revise_tone_intent"
    engine = get_orchestrator_engine()

    slots = {
        "business_name": {"value": "City Library"},
        "campaign_goal": {"value": "Book return reminder"}
    }
    current_templates = [{
        "message_index": 1,
        "stage": "Announcement",
        "body": "Return book now. Reply STOP to cancel.",
        "word_count": 6,
        "word_limit": 160,
        "compliant": True
    }]

    mock_llm_response = {
        "intent": "tone_change",
        "tone_idea": "Warm, polite, and encouraging",
        "slot_updates": {},
        "revised_body": "",
        "changes_summary": "Switching to a warmer and more polite tone."
    }

    async def mock_call(session_id, purpose, **kwargs):
        if purpose == "safety_guard":
            return {"status": "success", "raw_response": json.dumps({"allowed": True, "category": "none", "reason": "Safe content"})}
        return {"status": "success", "raw_response": json.dumps(mock_llm_response)}

    with patch.object(engine.llm, "call_llm", side_effect=mock_call):
        res = await engine.revise_campaign_template(
            session_id=session_id,
            workspace_id="default_workspace",
            business_type="Education & Public",
            slots=slots,
            selected_tone="Direct",
            current_templates=current_templates,
            user_feedback="Can we try a warmer and friendlier tone?"
        )

    assert res["action"] == "tone_change"
    assert "warm" in res["tone_idea"].lower()

def test_memory_context_scopes_by_business_domain():
    """Verify that get_memory_context prioritizes relevant business/domain entries."""
    mem_service = get_memory_service()
    storage = get_storage()
    workspace_id = "test_scope_ws"

    # Write two different domain entries
    storage.write_memory(workspace_id, category="retail", key="discount_rule", value="Clearance items get 20% off", session_id="s1")
    storage.write_memory(workspace_id, category="utility", key="overdue_notice", value="Disconnection notice requires 24h grace", session_id="s2")

    # When querying for utility
    ctx_utility = mem_service.get_memory_context(workspace_id, business_type="utility")
    lines = ctx_utility.split("\n")
    assert any("overdue_notice" in line for line in lines)
    assert "utility" in lines[0].lower() or "overdue_notice" in lines[0]

def test_extract_missing_placeholders_and_generate_questions_ui():
    """Verify that extract_missing_placeholders finds non-recipient placeholders and formats the Questions UI."""
    from app.services.orchestrator_engine import extract_missing_placeholders, generate_questions_ui

    template = "Dear {{customer_name}}, your electricity bill from {{organization_name}} is overdue. Please settle via {{payment_link}}. Reply STOP to unsubscribe."
    slots = {}

    missing = extract_missing_placeholders(template, slots)
    # customer_name is a contact tag and should NOT be considered missing from user input
    assert "customer_name" not in missing
    assert "organization_name" in missing
    assert "payment_link" in missing

    ui_text = generate_questions_ui(missing)
    assert "### 🛠️ Required Information" in ui_text
    assert "1. **Organization Name**" in ui_text
    assert "2. **Payment Link**" in ui_text
    assert 'Please reply with the values in order (e.g., "1.' in ui_text

@pytest.mark.asyncio
async def test_resolve_placeholders_end_to_end_in_chat():
    """Verify that answering the Questions UI resolves placeholders and finalizes template without looping."""
    from app.services.orchestrator_engine import resolve_placeholders_from_reply, fill_template_placeholders

    slots = {}
    missing = ["organization_name", "payment_link"]
    user_reply = "1. Green Energy Co, 2. https://pay.link"

    resolved, remaining = resolve_placeholders_from_reply(user_reply, missing, slots)
    assert resolved["organization_name"] == "Green Energy Co"
    assert resolved["payment_link"] == "https://pay.link"
    assert len(remaining) == 0

    original_body = "Dear {{customer_name}}, your electricity bill from {{organization_name}} is overdue. Please settle via {{payment_link}}. Reply STOP to unsubscribe."
    final_body = fill_template_placeholders(original_body, resolved)

    assert "Green Energy Co" in final_body
    assert "https://pay.link" in final_body
    assert "{{organization_name}}" not in final_body
    assert "{{payment_link}}" not in final_body
    assert "{{customer_name}}" in final_body  # preserved for per-contact preview

def test_strict_tag_validation_and_interactive_choices():
    """Verify that descriptive text like 'Quick payment link...' does not satisfy URL/phone tags, and interactive choices are generated."""
    from app.services.orchestrator_engine import (
        is_valid_url_value,
        is_valid_phone_value,
        extract_missing_placeholders,
        generate_interactive_choices_for_tag
    )
    from app.services.dispatch_service import render_template

    # 1. Descriptive text must NOT be valid URL or Phone
    desc_text = "Quick payment link and customer care number"
    assert is_valid_url_value(desc_text) is False
    assert is_valid_phone_value(desc_text) is False
    assert is_valid_url_value("https://pay.metropower.com") is True
    assert is_valid_phone_value("1-800-555-0199") is True

    # 2. Template with {{link}} and {{customer_care_number}} when slot contains descriptive text
    tmpl = "Hi {{customer_name}}! Pay at {{link}} or call {{customer_care_number}}. Reply STOP to unsubscribe."
    slots = {
        "link_or_contact": {"value": desc_text}
    }
    missing = extract_missing_placeholders(tmpl, slots)
    assert "link" in missing
    assert "customer_care_number" in missing

    # 3. Interactive choices generation
    link_opts = generate_interactive_choices_for_tag("link", "Utility & Energy")
    assert len(link_opts) >= 3
    assert link_opts[-1] == "Other (Type custom answer)"
    assert any("http" in opt for opt in link_opts[:-1])

    phone_opts = generate_interactive_choices_for_tag("customer_care_number", "Utility & Energy")
    assert len(phone_opts) >= 3
    assert phone_opts[-1] == "Other (Type custom answer)"

    # 4. render_template must NOT substitute descriptive text into {{link}}
    rendered, unres = render_template(tmpl, {"name": "John Doe", "phone": "+14155552671"}, campaign_context={"slots": slots})
    assert desc_text not in rendered
    assert "{{link}}" in rendered or "link" in unres
