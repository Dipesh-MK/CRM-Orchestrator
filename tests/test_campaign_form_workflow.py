import pytest
import json
from unittest.mock import patch, AsyncMock
from app.services.orchestrator_engine import get_orchestrator_engine
from app.api.chat_routes import get_or_create_state, submit_campaign_form, CampaignFormSubmitRequest, chat_send, ChatMessageRequest

@pytest.mark.asyncio
async def test_generate_message_from_form_sms_and_email():
    engine = get_orchestrator_engine()

    # Test SMS format
    res_sms = await engine.generate_message_from_form(
        session_id="test_form_sms",
        business_type="Utility & Energy",
        context="Electricity bill reminder",
        organization_name="Metro Power",
        link_or_contact="https://pay.metropower.com",
        channel_format="sms",
        tone="Aggressive & Urgent 🚨",
        template_request="Penalty applies in 24 hours"
    )
    assert len(res_sms["templates"]) == 1
    sms_body = res_sms["templates"][0]["body"]
    assert "Reply STOP to unsubscribe." in sms_body
    assert "Metro Power" in sms_body or "{{organization_name}}" in sms_body or "{{business_name}}" in sms_body

    # Test Email format
    res_email = await engine.generate_message_from_form(
        session_id="test_form_email",
        business_type="Utility & Energy",
        context="Electricity bill reminder",
        organization_name="Metro Power",
        link_or_contact="https://pay.metropower.com",
        channel_format="email",
        tone="Aggressive & Urgent 🚨",
        template_request="Penalty applies in 24 hours"
    )
    assert len(res_email["templates"]) == 1
    email_body = res_email["templates"][0]["body"]
    assert "Subject:" in email_body
    assert "Reply STOP to unsubscribe." in email_body

@pytest.mark.asyncio
async def test_generate_message_from_form_skipped_fields_exclusion():
    engine = get_orchestrator_engine()

    # When user skips organization_name and link_or_contact
    res_skipped = await engine.generate_message_from_form(
        session_id="test_form_skipped",
        business_type="Utility & Energy",
        context="Electricity bill reminder",
        organization_name="",
        link_or_contact="",
        channel_format="sms",
        tone="Aggressive & Urgent 🚨",
        skipped_fields=["organization_name", "link_or_contact"]
    )
    assert len(res_skipped["templates"]) == 1
    body = res_skipped["templates"][0]["body"]
    # Ensure NO unresolved brackets or template variables remain for the skipped fields
    assert "{{organization_name}}" not in body
    assert "{{business_name}}" not in body
    assert "{{link}}" not in body
    assert "{{payment_link}}" not in body
    assert "[Company Name]" not in body
    assert "[Link]" not in body
    assert "Reply STOP to unsubscribe." in body

@pytest.mark.asyncio
async def test_submit_campaign_form_missing_warning_and_skip():
    session_id = "test_form_warn_and_skip"
    state = get_or_create_state(session_id)

    # 1. Missing fields without skip_confirmed should yield missing_fields_warning
    req_missing = CampaignFormSubmitRequest(
        session_id=session_id,
        context="Electricity bill reminder",
        business_type="Utility & Energy",
        organization_name="",
        link_or_contact="",
        skip_confirmed=False
    )
    resp = await submit_campaign_form(req_missing)
    events = []
    async for chunk in resp.body_iterator:
        text_chunk = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
        for line in text_chunk.split("\n\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

    warning_ev = next((e for e in events if e.get("type") == "missing_fields_warning"), None)
    assert warning_ev is not None
    assert "Organization Name" in warning_ev.get("text", "")

    # 2. Resubmitting with skip_confirmed=True should generate template without placeholders
    req_skip = CampaignFormSubmitRequest(
        session_id=session_id,
        context="Electricity bill reminder",
        business_type="Utility & Energy",
        organization_name="",
        link_or_contact="",
        skip_confirmed=True,
        skipped_fields=["organization_name", "link_or_contact"]
    )
    resp_skip = await submit_campaign_form(req_skip)
    events_skip = []
    async for chunk in resp_skip.body_iterator:
        text_chunk = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
        for line in text_chunk.split("\n\n"):
            if line.startswith("data: "):
                events_skip.append(json.loads(line[6:]))

    ready_ev = next((e for e in events_skip if e.get("type") == "templates_ready"), None)
    assert ready_ev is not None
    assert len(ready_ev["templates"]) == 1
    body = ready_ev["templates"][0]["body"]
    assert "{{organization_name}}" not in body
    assert "{{link}}" not in body

@pytest.mark.asyncio
async def test_greeting_does_not_show_form_card():
    session_id = "test_greeting_session"
    req = ChatMessageRequest(session_id=session_id, message="hi what can you do")
    resp = await chat_send(req)
    events = []
    async for chunk in resp.body_iterator:
        text_chunk = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
        for line in text_chunk.split("\n\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

    # Must NOT emit campaign_form
    assert not any(e.get("type") == "campaign_form" for e in events)

    # Must emit text_chunk with greeting and starter chips
    txt_ev = next((e for e in events if e.get("type") == "text_chunk"), None)
    assert txt_ev is not None
    assert "Welcome to CRMbot" in txt_ev.get("text", "")
    assert "question_options" in txt_ev
    assert any("Charity" in str(opt) for opt in txt_ev["question_options"])

@pytest.mark.asyncio
async def test_charity_back_and_forth_and_confirmation_card():
    session_id = "test_charity_flow"

    # Step 1: User provides context "a charity event"
    req1 = ChatMessageRequest(session_id=session_id, message="a charity event to protect wildlife")
    resp1 = await chat_send(req1)
    evs1 = []
    async for chunk in resp1.body_iterator:
        text_chunk = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
        for line in text_chunk.split("\n\n"):
            if line.startswith("data: "):
                evs1.append(json.loads(line[6:]))

    # Should ask Question 1 (admission/ticket model) and NOT emit campaign_form yet
    assert not any(e.get("type") == "campaign_form" for e in evs1)
    q1_ev = next((e for e in evs1 if e.get("type") == "text_chunk"), None)
    assert q1_ev is not None
    assert "Question 1" in q1_ev["text"]
    assert "Free Admission" in str(q1_ev["question_options"])

    # Step 2: User answers Question 1
    req2 = ChatMessageRequest(session_id=session_id, message="Free Admission (Donations Welcome)")
    resp2 = await chat_send(req2)
    evs2 = []
    async for chunk in resp2.body_iterator:
        text_chunk = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
        for line in text_chunk.split("\n\n"):
            if line.startswith("data: "):
                evs2.append(json.loads(line[6:]))

    # Should ask Question 2 (host and cause)
    assert not any(e.get("type") == "campaign_form" for e in evs2)
    q2_ev = next((e for e in evs2 if e.get("type") == "text_chunk"), None)
    assert q2_ev is not None
    assert "Question 2" in q2_ev["text"]

    # Step 3: User concludes ("that's all, generate the card now")
    req3 = ChatMessageRequest(session_id=session_id, message="Hope Wildlife Sanctuary, that's all ready for the card")
    resp3 = await chat_send(req3)
    evs3 = []
    async for chunk in resp3.body_iterator:
        text_chunk = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
        for line in text_chunk.split("\n\n"):
            if line.startswith("data: "):
                evs3.append(json.loads(line[6:]))

    # Should emit the campaign_form card with dynamic fields!
    card_ev = next((e for e in evs3 if e.get("type") == "campaign_form"), None)
    assert card_ev is not None
    form_data = card_ev["form_data"]
    assert form_data["business_type"] == "Charity & Non-Profit"
    assert "dynamic_fields" in form_data
    assert "Inspiring & Heartfelt ❤️" in form_data["recommended_tones"]

@pytest.mark.asyncio
async def test_submit_confirmation_card_with_dynamic_fields():
    session_id = "test_charity_card_submit"
    req = CampaignFormSubmitRequest(
        session_id=session_id,
        context="Charity wildlife rescue gala",
        business_type="Charity & Non-Profit",
        organization_name="Hope Wildlife Sanctuary",
        link_or_contact="https://give.hopecharity.org",
        channel_format="sms",
        tone="Inspiring & Heartfelt ❤️",
        dynamic_fields={
            "ticket_type": {"label": "🎟️ Admission / Cost", "value": "Free Admission (Donations Welcome)"},
            "volunteers_needed": {"label": "🤝 Needs & Volunteers", "value": "Volunteers Needed (Setup & Hospitality)"}
        }
    )
    resp = await submit_campaign_form(req)
    evs = []
    async for chunk in resp.body_iterator:
        text_chunk = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
        for line in text_chunk.split("\n\n"):
            if line.startswith("data: "):
                evs.append(json.loads(line[6:]))

    ready_ev = next((e for e in evs if e.get("type") == "templates_ready"), None)
    assert ready_ev is not None
    body = ready_ev["templates"][0]["body"]
    assert "Reply STOP to unsubscribe." in body
    assert "Hope Wildlife Sanctuary" in body or "{{organization_name}}" in body
    # Generated template message and follow-up question should be separate messages
    template_ev = next((e for e in evs if e.get("type") == "text_chunk" and "Message Generated Successfully" in e.get("text", "")), None)
    assert template_ev is not None
    assert "💡 Next Steps:" not in template_ev.get("text", "")

    txt_ev = next((e for e in evs if e.get("type") == "text_chunk" and "question_options" in e), None)
    assert txt_ev is not None
    assert "Next Steps" in txt_ev.get("text", "")
    assert any("Follow-Up" in opt for opt in txt_ev["question_options"])
    assert any("Contacts" in opt for opt in txt_ev["question_options"])

    # Verify dynamic memory persistence in workspace_memory and memory_ops
    from app.core.storage import get_storage
    storage = get_storage()
    memories = storage.get_workspace_memory("default_workspace")
    assert any(m["category"] == "campaign_profile" and m["key"] == "active_campaign" for m in memories)
    assert any(m["category"] == "dynamic_context" and m["key"] == "ticket_type" for m in memories)

    with storage.get_connection() as conn:
        ops = conn.cursor().execute("SELECT * FROM memory_ops WHERE session_id = ?", (session_id,)).fetchall()
        assert len(ops) >= 2

@pytest.mark.asyncio
async def test_followup_reminder_generation_and_chat_actions():
    session_id = "test_followup_flow"
    state = get_or_create_state(session_id)

    # 1. Initialize with an existing generated template
    req_init = CampaignFormSubmitRequest(
        session_id=session_id,
        context="Charity wildlife rescue gala",
        business_type="Charity & Non-Profit",
        organization_name="Hope Wildlife Sanctuary",
        link_or_contact="https://give.hopecharity.org",
        channel_format="sms",
        tone="Inspiring & Heartfelt ❤️",
        dynamic_fields={"ticket_type": "Free Admission"}
    )
    resp_init = await submit_campaign_form(req_init)
    async for _ in resp_init.body_iterator:
        pass

    assert len(state["templates"]) == 1

    # 2. User selects "📅 Create Follow-Up Reminder Template" -> triggers targeted follow-up Q&A loop
    req_followup = ChatMessageRequest(session_id=session_id, message="📅 Create Follow-Up Reminder Template")
    resp_followup = await chat_send(req_followup)
    evs_followup = []
    async for chunk in resp_followup.body_iterator:
        text_chunk = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
        for line in text_chunk.split("\n\n"):
            if line.startswith("data: "):
                evs_followup.append(json.loads(line[6:]))

    # Should ask Question 1 (reminder timing & deadline)
    q1_ev = next((e for e in evs_followup if e.get("type") == "text_chunk"), None)
    assert q1_ev is not None
    assert "Question 1" in q1_ev["text"]
    assert "24 Hours Before" in str(q1_ev.get("question_options", []))

    # 3. User answers Question 1
    req_q1 = ChatMessageRequest(session_id=session_id, message="24 Hours Before Event / Deadline")
    resp_q1 = await chat_send(req_q1)
    evs_q1 = []
    async for chunk in resp_q1.body_iterator:
        text_chunk = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
        for line in text_chunk.split("\n\n"):
            if line.startswith("data: "):
                evs_q1.append(json.loads(line[6:]))

    # Should ask Question 2 (urgency & action)
    q2_ev = next((e for e in evs_q1 if e.get("type") == "text_chunk"), None)
    assert q2_ev is not None
    assert "Question 2" in q2_ev["text"]

    # 4. User answers Question 2 -> Generates Tone Candidate Previews Card!
    req_q2 = ChatMessageRequest(session_id=session_id, message="Bring ticket and required documents")
    resp_q2 = await chat_send(req_q2)
    evs_q2 = []
    card_ev = None
    async for chunk in resp_q2.body_iterator:
        text_chunk = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
        for line in text_chunk.split("\n\n"):
            if line.startswith("data: "):
                try:
                    payload = json.loads(line[6:])
                    evs_q2.append(payload)
                    if payload.get("type") == "campaign_form":
                        card_ev = payload
                except Exception:
                    pass

    assert card_ev is not None
    f_data = card_ev["form_data"]
    assert "tone_candidates" in f_data
    assert len(f_data["tone_candidates"]) >= 2
    for tc in f_data["tone_candidates"]:
        assert "preview" in tc
        assert "name" in tc
        assert "word_count" in tc

    # 5. Test Tone Preview Regeneration Loop with custom suggestions
    from app.api.chat_routes import regenerate_tone_previews, TonePreviewRegenRequest
    regen_tones_res = await regenerate_tone_previews(TonePreviewRegenRequest(
        session_id=session_id,
        business_type=f_data["business_type"],
        context=f_data["context"],
        organization_name=f_data["organization_name"],
        link_or_contact=f_data["link_or_contact"],
        channel_format=f_data["channel_format"],
        user_feedback="Make it witty and mention VIP seating",
        dynamic_fields=f_data.get("dynamic_fields", {})
    ))
    assert regen_tones_res["status"] == "success"
    assert len(regen_tones_res["tone_candidates"]) >= 2
    assert "witty" in regen_tones_res["tone_candidates"][0]["name"].lower() or "witty" in regen_tones_res["tone_candidates"][0]["preview"].lower() or regen_tones_res["tone_candidates"][0]["compliant"] is True

    # 6. Confirm and generate the follow-up message
    selected_tc = regen_tones_res["tone_candidates"][0]
    submit_followup = await submit_campaign_form(CampaignFormSubmitRequest(
        session_id=session_id,
        context=f_data["context"],
        business_type=f_data["business_type"],
        organization_name=f_data["organization_name"],
        link_or_contact=f_data["link_or_contact"],
        channel_format=f_data["channel_format"],
        tone=selected_tc["name"],
        dynamic_fields=f_data.get("dynamic_fields", {})
    ))
    async for _ in submit_followup.body_iterator:
        pass

    assert len(state["templates"]) >= 1
    assert "Reply STOP to unsubscribe." in state["templates"][0]["body"]

@pytest.mark.asyncio
async def test_version_tracking_on_edit_and_restore():
    from app.api.chat_routes import edit_template, restore_template_version, TemplateEditRequest, TemplateRestoreVersionRequest
    session_id = "test_version_tracking_flow"
    state = get_or_create_state(session_id)

    # Initialize a template
    state["templates"] = [{
        "message_index": 1,
        "stage": "Announcement",
        "tone": "Direct & Urgent 🚨",
        "body": "Hi {{customer_name}}, your electricity bill is due. Please pay today. Reply STOP to unsubscribe.",
        "word_count": 15,
        "compliant": True,
        "violations": []
    }]

    # Edit template -> creates v2
    edit_req = TemplateEditRequest(
        session_id=session_id,
        message_index=1,
        updated_body="Hi {{customer_name}}, this is a revised notice. Your bill is overdue. Reply STOP to unsubscribe."
    )
    res_edit = await edit_template(edit_req)
    t = res_edit["template"]
    assert "versions" in t
    assert len(t["versions"]) == 2
    assert t["versions"][0]["version"] == 1
    assert t["versions"][1]["version"] == 2
    assert t["current_version"] == 2
    assert "revised notice" in t["body"]

    # Restore v1 -> creates v3 with v1 content
    restore_req = TemplateRestoreVersionRequest(
        session_id=session_id,
        message_index=1,
        version=1
    )
    res_restore = await restore_template_version(restore_req)
    t_restored = res_restore["template"]
    assert len(t_restored["versions"]) == 3
    assert t_restored["current_version"] == 3
    assert "your electricity bill is due. Please pay today" in t_restored["body"]
    assert "revised notice" not in t_restored["body"]

@pytest.mark.asyncio
async def test_context_grounded_regeneration_different_clinic_and_clauses():
    from app.api.chat_routes import regenerate_template, TemplateRegenerateRequest
    session_id = "test_regen_clinic_flow"
    state = get_or_create_state(session_id)
    state["business_type"] = "Healthcare & Clinics"
    state["campaign_domain"] = "Healthcare & Clinics"
    state["slots"] = {
        "business_name": {"value": "City Smiles Dental", "source": "discovery"},
        "campaign_goal": {"value": "Routine dental checkup and teeth cleaning reminder"}
    }
    state["templates"] = [{
        "message_index": 1,
        "stage": "Announcement",
        "tone": "Caring & Reassuring 🩺",
        "body": "Hi {{customer_name}}, City Smiles Dental reminds you to schedule your dental checkup. Reply STOP to unsubscribe.",
        "word_count": 16,
        "compliant": True,
        "violations": []
    }]

    # User asks to regenerate with different clinic name and an offer clause
    regen_req = TemplateRegenerateRequest(
        session_id=session_id,
        message_index=1,
        instructions="Change clinic name to LMAO Dental Care and add 20% discount offer"
    )
    res_regen = await regenerate_template(regen_req)
    assert res_regen["status"] == "success"
    tmpl = res_regen["template"]

    # Verify version tracking
    assert len(tmpl["versions"]) == 2
    assert tmpl["current_version"] == 2

    # Verify state and slots updated with new clinic name
    assert state["organization_name"] == "LMAO Dental Care"
    assert state["slots"]["business_name"]["value"] == "LMAO Dental Care"

    # Verify message does NOT address the clinic as recipient!
    body = tmpl["body"]
    assert not body.startswith("Hi LMAO Dental Care")
    assert not body.startswith("Dear LMAO Dental Care")
    assert "Reply STOP to unsubscribe." in body

@pytest.mark.asyncio
async def test_live_session_memory_endpoint():
    from app.api.chat_routes import get_live_session_memory
    session_id = "test_live_memory_flow"
    state = get_or_create_state(session_id)
    state["organization_name"] = "Apex Health"
    state["dynamic_fields"] = {"doctor": "Dr. Stone", "specialty": "Dentistry"}

    mem_resp = get_live_session_memory(session_id)
    assert mem_resp["session_id"] == session_id
    assert mem_resp["organization_name"] == "Apex Health"
    assert mem_resp["dynamic_fields"]["doctor"] == "Dr. Stone"
    assert "workspace_memory" in mem_resp
    assert "recent_ops" in mem_resp

@pytest.mark.asyncio
async def test_dynamic_context_grocery_exhibition_flow():
    import uuid
    from app.api.chat_routes import chat_send, ChatMessageRequest, submit_campaign_form, CampaignFormSubmitRequest, get_chat_session
    from app.services.orchestrator_engine import clean_organization_name
    
    session_id = f"test_dynamic_grocery_flow_{uuid.uuid4().hex[:8]}"
    state = get_or_create_state(session_id)
    state["csrf_token"] = "test_csrf_token"

    # 1. User provides initial context
    res1 = await chat_send(ChatMessageRequest(
        session_id=session_id,
        message="crm message for grocery exhibition"
    ))
    chunks1 = []
    async for chunk in res1.body_iterator:
        chunks1.append(chunk)

    # 2. User responds with rich narrative context instead of just a raw business name
    narrative_msg = (
        "i run a grocery shop and i want to invite everybody in the local area for a farmers delight "
        "exhibition with potential new upcoming locally sourced products direct from the fields with "
        "no fertilizers or chemicals. based on the response to the event i want to consider making it an official product. "
        "so i want to send out crm msgs"
    )
    res2 = await chat_send(ChatMessageRequest(
        session_id=session_id,
        message=narrative_msg
    ))
    chunks2 = []
    async for chunk in res2.body_iterator:
        chunks2.append(chunk)

    # Check that business_name slot is NOT the 40-word narrative
    org = state.get("slots", {}).get("business_name", {}).get("value", "")
    assert org != narrative_msg
    assert len(org.split()) <= 4

    # 3. User responds with CTA, phone number, and location address
    res3 = await chat_send(ChatMessageRequest(
        session_id=session_id,
        message="phone number: 88888 88888\naddress: grocery store near 6th cross, 10th main\ncall and enquire to taste fresh chemical-free produce"
    ))
    card_data = None
    async for chunk in res3.body_iterator:
        text_chunk = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        for line in text_chunk.split("\n\n"):
            if line.startswith("data: "):
                try:
                    payload = json.loads(line[6:])
                    if payload.get("form_data"):
                        card_data = payload["form_data"]
                except Exception:
                    pass

    # If card_data not in this turn, request conclusion explicitly
    if not card_data:
        res_done = await chat_send(ChatMessageRequest(
            session_id=session_id,
            message="Looks good, ready to show card"
        ))
        async for chunk in res_done.body_iterator:
            text_chunk = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
            for line in text_chunk.split("\n\n"):
                if line.startswith("data: "):
                    try:
                        payload = json.loads(line[6:])
                        if payload.get("form_data"):
                            card_data = payload["form_data"]
                    except Exception:
                        pass

    assert card_data is not None
    assert card_data["organization_name"] != narrative_msg
    assert card_data["organization_name"] in ["Grocery Shop", "Farmers Delight Grocers", "Local Grocery Store", "Our Business", ""] or len(card_data["organization_name"].split()) <= 4

    # 4. Verify conversation history and session retrieval preserve form_data
    sess_data = get_chat_session(session_id)
    history = sess_data.get("conversation_history", [])
    has_form_in_history = any(h.get("form_data") is not None for h in history if h.get("role") == "assistant")
    assert has_form_in_history or sess_data.get("state", {}).get("current_form_context") is not None

    # 5. Submit the confirmation card and verify generated message
    submit_req = CampaignFormSubmitRequest(
        session_id=session_id,
        context=card_data["context"],
        business_type=card_data["business_type"],
        organization_name=card_data["organization_name"],
        link_or_contact="88888 88888",
        channel_format="sms",
        tone=card_data["tone"],
        dynamic_fields=card_data.get("dynamic_fields", {})
    )
    res_submit = await submit_campaign_form(submit_req)
    tmpl_res = None
    async for chunk in res_submit.body_iterator:
        text_chunk = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        for line in text_chunk.split("\n\n"):
            if line.startswith("data: "):
                try:
                    payload = json.loads(line[6:])
                    if payload.get("type") == "templates_ready":
                        tmpl_res = payload["templates"]
                except Exception:
                    pass

    assert tmpl_res is not None and len(tmpl_res) == 1
    tmpl_body = tmpl_res[0]["body"]

    # Verify message does NOT start with the 40-word narrative prompt
    assert not tmpl_body.startswith(narrative_msg)
    assert "Reply STOP to unsubscribe." in tmpl_body
    assert tmpl_res[0]["compliant"] is True



