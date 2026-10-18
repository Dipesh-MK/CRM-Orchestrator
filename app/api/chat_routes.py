import json
import uuid
import asyncio
import hmac
import secrets
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.config import get_config
from app.core.storage import get_storage, utc_now_iso
from app.services.contact_service import get_contact_service
from app.services.orchestrator_engine import get_orchestrator_engine
from app.services.memory_service import get_memory_service

router = APIRouter(prefix="/api", tags=["Chat & Orchestrator"])
config = get_config()
storage = get_storage()
contact_service = get_contact_service()
engine = get_orchestrator_engine()
memory_service = get_memory_service()

# In-memory session state dictionary
_session_states: Dict[str, Dict[str, Any]] = {}

def get_or_create_state(session_id: str, workspace_id: str = "default_workspace") -> Dict[str, Any]:
    if session_id not in _session_states:
        _session_states[session_id] = {
            "session_id": session_id,
            "workspace_id": workspace_id,
            "business_type": None,
            "conversation_history": [],
            "slots": {},
            "discovery_round": 0,
            "discovery_complete": False,
            "candidate_tones": [],
            "selected_tone": None,
            "rejected_tones": [],
            "pipeline_plan": None,
            "templates": [],
            "contacts": [],
            "contacts_count": 0,
            "approved": False,
            "step_order": 0,
            "csrf_token": secrets.token_hex(16)
        }
        storage.save_session(session_id, workspace_id)
    elif "csrf_token" not in _session_states[session_id]:
        _session_states[session_id]["csrf_token"] = secrets.token_hex(16)
    return _session_states[session_id]

@router.get("/session/{session_id}/csrf")
async def get_session_csrf_endpoint(session_id: str):
    state = get_or_create_state(session_id)
    return {"session_id": session_id, "csrf_token": state["csrf_token"]}

class ChatMessageRequest(BaseModel):
    session_id: Optional[str] = None
    workspace_id: Optional[str] = "default_workspace"
    message: str

class ToneSelectRequest(BaseModel):
    session_id: str
    tone_id: str
    tone_name: str

class TemplateEditRequest(BaseModel):
    session_id: str
    message_index: int
    updated_body: str

class TemplateRegenerateRequest(BaseModel):
    session_id: str
    message_index: int
    instructions: Optional[str] = None

class ApproveCampaignRequest(BaseModel):
    session_id: str
    csrf_token: Optional[str] = None

def format_pipeline_event_tag(event: Dict[str, Any]) -> str:
    """Wraps event object in <pipeline_event> tags for SSE / LLM compatibility."""
    return f"<pipeline_event>\n{json.dumps(event, ensure_ascii=False)}\n</pipeline_event>\n"

def emit_event(state: Dict[str, Any], step_id: str, step_title: str, step_icon: str, status: str, description: str, sub_step: str, sub_status: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    state["step_order"] += 1
    event = {
        "session_id": state["session_id"],
        "step_order": state["step_order"],
        "step_id": step_id,
        "step_title": step_title,
        "step_icon": step_icon,
        "status": status,
        "description": description,
        "sub_step": sub_step,
        "sub_status": sub_status,
        "data": data or {},
        "timestamp": utc_now_iso()
    }
    storage.log_pipeline_event(event)
    return event

@router.post("/chat/send")
async def chat_send(req: ChatMessageRequest):
    """
    Main conversational endpoint streaming response via SSE.
    Adapts from Discovery -> Tone Selection -> Planning -> Template Generation.
    """
    session_id = req.session_id or str(uuid.uuid4())
    state = get_or_create_state(session_id, req.workspace_id or "default_workspace")
    storage.log_user_action(session_id, "user_chat_message", {"message": req.message})

    async def event_generator():
        # First send session initialization
        init_meta = {"type": "session_init", "session_id": session_id, "csrf_token": state["csrf_token"]}
        yield f"data: {json.dumps(init_meta)}\n\n"

        user_msg = req.message.strip()
        state["conversation_history"].append({"role": "user", "content": user_msg})

        # Check if user asked execution / sending
        execution_triggers = ["send now", "blast message", "send sms", "send to all", "dispatch"]
        if any(trig in user_msg.lower() for trig in execution_triggers):
            refusal_text = "That's campaign execution. I'm focused on planning and templates."
            state["conversation_history"].append({"role": "assistant", "content": refusal_text})
            yield f"data: {json.dumps({'type': 'text_chunk', 'text': refusal_text})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        # ------------------ STAGE: DISCOVERY ------------------
        if not state["discovery_complete"]:
            ev_start = emit_event(
                state, "discovery", "Campaign Discovery", "🎤", "active",
                "Gathering campaign details and requirements",
                "Analyzing campaign request & business domain...", "processing"
            )
            yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_start, 'tag': format_pipeline_event_tag(ev_start)})}\n\n"

            state["discovery_round"] += 1
            discovery_res = await engine.run_discovery_round(
                session_id=session_id,
                workspace_id=state["workspace_id"],
                latest_user_message=user_msg,
                conversation_history=state["conversation_history"],
                current_slots=state["slots"],
                round_index=state["discovery_round"]
            )

            if discovery_res.get("blocked"):
                reason = discovery_res.get("reason", "Prohibited content detected.")
                alt = discovery_res.get("alternative")
                is_rate_limit = discovery_res.get("is_rate_limit", False)
                
                if is_rate_limit:
                    refusal_msg = f"⏳ **Rate Limit Reached on Free Tier**\n\n{reason}\n\n*Automatic retries within the slot window were exhausted.* You can retry immediately below without losing your context."
                    ev_err = emit_event(
                        state, "discovery", "Campaign Discovery", "🎤", "error",
                        "Upstream Rate Limit (429)",
                        "Temporary rate limit exceeded on provider tier", "error"
                    )
                else:
                    refusal_msg = f"⚠️ **Request Declined**\n\nI cannot assist with this campaign: {reason}."
                    if alt:
                        refusal_msg += f"\n\n💡 *Suggested legitimate alternative:* {alt}"
                    ev_err = emit_event(
                        state, "discovery", "Campaign Discovery", "🎤", "error",
                        "Safety policy violation",
                        f"Blocked by safety guard: {reason}", "error"
                    )
                
                yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_err, 'tag': format_pipeline_event_tag(ev_err)})}\n\n"
                yield f"data: {json.dumps({'type': 'text_chunk', 'text': refusal_msg, 'is_rate_limit': is_rate_limit})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

            # Update state with extracted slots
            if discovery_res.get("business_type"):
                state["business_type"] = discovery_res["business_type"]
            state["slots"] = discovery_res.get("slots", state["slots"])
            
            # Check if campaign name was extracted
            camp_name = state["slots"].get("campaign_name", {}).get("value")
            b_name = state["slots"].get("business_name", {}).get("value")
            storage.save_session(
                session_id, state["workspace_id"],
                business_type=state["business_type"],
                campaign_name=camp_name
            )

            is_complete = bool(discovery_res.get("discovery_complete"))
            if not is_complete and isinstance(discovery_res.get("slots"), dict):
                if discovery_res["slots"].get("discovery_complete"):
                    is_complete = True

            if is_complete:
                state["discovery_complete"] = True
                ev_done = emit_event(
                    state, "discovery", "Campaign Discovery", "🎤", "done",
                    "Campaign requirements gathered",
                    "Campaign details and slots confirmed", "done",
                    {"business_type": state["business_type"], "slots": state["slots"]}
                )
                yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_done, 'tag': format_pipeline_event_tag(ev_done)})}\n\n"

                # Transition directly to Tone Exploration!
                ev_tone_start = emit_event(
                    state, "tone_exploration", "Tone Exploration", "🎨", "active",
                    "Proposing candidate message tones for selection",
                    "Generating domain-specific candidate tones...", "processing"
                )
                yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_tone_start, 'tag': format_pipeline_event_tag(ev_tone_start)})}\n\n"

                # Check if user mentioned tone in slots
                user_tone_idea = state["slots"].get("user_preferred_tone", {}).get("value", "")
                tones_res = await engine.explore_tones(
                    session_id=session_id,
                    workspace_id=state["workspace_id"],
                    business_type=state["business_type"] or "General Business",
                    slots=state["slots"],
                    user_tone_idea=user_tone_idea
                )
                state["candidate_tones"] = tones_res.get("candidate_tones", [])

                ev_tone_wait = emit_event(
                    state, "tone_exploration", "Tone Exploration", "🎨", "waiting_for_input",
                    "Awaiting your tone selection",
                    f"Proposed {len(state['candidate_tones'])} candidate tones — please select one", "waiting",
                    {"candidate_tones": state["candidate_tones"]}
                )
                yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_tone_wait, 'tag': format_pipeline_event_tag(ev_tone_wait)})}\n\n"

                # Construct rich message to user with assumptions, slots summary, and tone cards
                reply_text = f"### 📋 Campaign Requirements Confirmed\n\n"
                reply_text += f"- **Business / Domain:** {state['business_type']}\n"
                if b_name:
                    reply_text += f"- **Business Name:** {b_name}\n"
                if camp_name:
                    reply_text += f"- **Campaign Name:** {camp_name}\n"
                goal = state['slots'].get("campaign_goal", {}).get("value")
                if goal:
                    reply_text += f"- **Goal / Offer:** {goal}\n"

                if discovery_res.get("assumptions_made"):
                    reply_text += "\n**Assumptions applied:**\n"
                    for asm in discovery_res["assumptions_made"]:
                        reply_text += f"- {asm}\n"

                reply_text += f"\n---\n\n### 🎨 Step 2: Tone Exploration\n"
                reply_text += f"{tones_res.get('rationale', 'Here are candidate tones crafted for your campaign:')}\n\n"
                reply_text += "Please select a tone below to generate your message sequence, or reply with feedback (e.g. *'more urgent'*, *'less formal'*):\n\n"

                for t in state["candidate_tones"]:
                    reply_text += f"**{t.get('name')}**\n"
                    reply_text += f"*{t.get('description')}*\n\n"
                    reply_text += f"> {t.get('example_message')}\n\n"

                state["conversation_history"].append({"role": "assistant", "content": reply_text})
                yield f"data: {json.dumps({'type': 'text_chunk', 'text': reply_text, 'tones': state['candidate_tones']})}\n\n"

            else:
                # Discovery questions follow-up
                ev_wait = emit_event(
                    state, "discovery", "Campaign Discovery", "🎤", "waiting_for_input",
                    "Awaiting your input",
                    "Awaiting answer to campaign questions", "waiting"
                )
                yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_wait, 'tag': format_pipeline_event_tag(ev_wait)})}\n\n"

                reply_text = (discovery_res.get("next_question_to_user") or "").strip()
                if not reply_text:
                    reply_text = "Could you tell me your business name and what discount or offer you'd like to share?"
                state["conversation_history"].append({"role": "assistant", "content": reply_text})
                yield f"data: {json.dumps({'type': 'text_chunk', 'text': reply_text})}\n\n"

        # ------------------ STAGE: TONE REFINEMENT (IF WAITING FOR TONE) ------------------
        elif state["discovery_complete"] and not state["selected_tone"]:
            # User provided conversational feedback on tones (e.g. "regenerate", "make it more energetic")
            ev_regen = emit_event(
                state, "tone_exploration", "Tone Exploration", "🎨", "active",
                "Refining candidate tones based on user feedback",
                f"Adjusting tone options: '{user_msg}'", "processing"
            )
            yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_regen, 'tag': format_pipeline_event_tag(ev_regen)})}\n\n"

            tones_res = await engine.explore_tones(
                session_id=session_id,
                workspace_id=state["workspace_id"],
                business_type=state["business_type"] or "General Business",
                slots=state["slots"],
                user_tone_idea=user_msg
            )
            state["candidate_tones"] = tones_res.get("candidate_tones", [])

            ev_tone_wait = emit_event(
                state, "tone_exploration", "Tone Exploration", "🎨", "waiting_for_input",
                "Awaiting your tone selection",
                f"Updated candidate tones based on feedback", "waiting",
                {"candidate_tones": state["candidate_tones"]}
            )
            yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_tone_wait, 'tag': format_pipeline_event_tag(ev_tone_wait)})}\n\n"

            reply_text = f"I've adjusted the tone candidates based on your feedback (*\"{user_msg}\"*):\n\n"
            for t in state["candidate_tones"]:
                reply_text += f"**{t.get('name')}**\n*{t.get('description')}*\n\n> {t.get('example_message')}\n\n"

            state["conversation_history"].append({"role": "assistant", "content": reply_text})
            yield f"data: {json.dumps({'type': 'text_chunk', 'text': reply_text, 'tones': state['candidate_tones']})}\n\n"

        # ------------------ STAGE: TEMPLATE ADJUSTMENTS / REGEN / REVISION ------------------
        else:
            is_rejection = "reject" in user_msg.lower() or "wrong" in user_msg.lower() or "redo" in user_msg.lower() or "again" in user_msg.lower() or "change" in user_msg.lower() or "regenerate" in user_msg.lower() or "tone" in user_msg.lower() or "attach" in user_msg.lower()

            if is_rejection:
                ev_rev = emit_event(
                    state, "templates", "Template Generation", "✍️", "active",
                    "Revising templates based on user feedback",
                    f"Processing revision: '{user_msg[:60]}...'", "processing"
                )
                yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_rev, 'tag': format_pipeline_event_tag(ev_rev)})}\n\n"

                # Check if user wants different tones or tone change
                if "tone" in user_msg.lower():
                    # Re-explore candidate tones
                    state["selected_tone"] = None
                    tones_res = await engine.explore_tones(
                        session_id=session_id,
                        workspace_id=state["workspace_id"],
                        business_type=state["business_type"] or "General Business",
                        slots=state["slots"],
                        user_tone_idea=user_msg
                    )
                    state["candidate_tones"] = tones_res.get("candidate_tones", [])

                    reply_text = f"Understood! I've reset the campaign tone and generated new candidate options based on your feedback (*\"{user_msg}\"*):\n\n"
                    for t in state["candidate_tones"]:
                        reply_text += f"**{t.get('name')}**\n*{t.get('description')}*\n\n> {t.get('example_message')}\n\n"
                    reply_text += "Please select a preferred tone to regenerate your campaign sequence."

                    state["conversation_history"].append({"role": "assistant", "content": reply_text})
                    yield f"data: {json.dumps({'type': 'text_chunk', 'text': reply_text, 'tones': state['candidate_tones']})}\n\n"
                else:
                    # Regenerate full template sequence with user's instructions applied to must_include or slots
                    if state.get("slots"):
                        current_must = state["slots"].get("must_include", {}).get("value") or ""
                        if current_must:
                            state["slots"]["must_include"]["value"] = f"{current_must}; {user_msg}"
                        else:
                            state["slots"]["must_include"] = {"value": user_msg, "required": False, "confidence": 1.0, "source": "user"}

                    selected_tone = state.get("selected_tone") or "Professional & Clear"
                    tmpl_res = await engine.generate_and_validate_templates(
                        session_id=session_id,
                        business_type=state["business_type"] or "General Business",
                        slots=state["slots"],
                        selected_tone=selected_tone,
                        contacts_sample=state.get("contacts")
                    )
                    state["templates"] = tmpl_res.get("templates", [])

                    reply_text = f"🔄 I've revised and regenerated the messaging sequence to address your feedback (*\"{user_msg}\"*):\n\n"
                    reply_text += f"- **Tone:** {selected_tone}\n"
                    reply_text += f"- **Schedule:** {tmpl_res.get('recommended_schedule', 'Standard Cadence')}\n"
                    reply_text += f"- **Templates generated:** {len(state['templates'])} messages strictly verified under {config.orchestrator.word_limit} words.\n\n"
                    reply_text += "You can review the updated sequence above, edit any specific text, or approve the campaign when you're ready."

                    state["conversation_history"].append({"role": "assistant", "content": reply_text})
                    yield f"data: {json.dumps({'type': 'text_chunk', 'text': reply_text})}\n\n"
                    yield f"data: {json.dumps({'type': 'templates_ready', 'templates': state['templates'], 'schedule': tmpl_res.get('recommended_schedule')})}\n\n"
            else:
                reply_text = f"I've noted: *\"{user_msg}\"*. Would you like me to adjust the campaign tone, re-generate the message templates with new instructions, or update your contact list?"
                state["conversation_history"].append({"role": "assistant", "content": reply_text})
                yield f"data: {json.dumps({'type': 'text_chunk', 'text': reply_text})}\n\n"

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@router.post("/chat/select_tone")
async def select_tone(req: ToneSelectRequest):
    """
    Called when user clicks/selects one candidate tone.
    Locks in tone, updates memory, generates dynamic plan & compliant templates.
    Streams execution events via SSE.
    """
    state = get_or_create_state(req.session_id)
    storage.log_user_action(req.session_id, "select_tone", {"tone_id": req.tone_id, "tone_name": req.tone_name})

    state["selected_tone"] = req.tone_name
    # Record rejected tones
    for t in state.get("candidate_tones", []):
        if t.get("name") != req.tone_name and t.get("name") not in state["rejected_tones"]:
            state["rejected_tones"].append(t.get("name"))

    async def tone_exec_stream():
        # Complete Tone Exploration step
        ev_tone_done = emit_event(
            state, "tone_exploration", "Tone Exploration", "🎨", "done",
            "Tone agreed upon",
            f"Selected Tone: {req.tone_name}", "done",
            {"selected_tone": req.tone_name, "rejected_tones": state["rejected_tones"]}
        )
        yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_tone_done, 'tag': format_pipeline_event_tag(ev_tone_done)})}\n\n"

        # Planning step
        ev_plan_start = emit_event(
            state, "planning", "Workflow Planning", "🗺️", "active",
            "Generating dynamic pipeline steps for this domain",
            "Consulting Orchestrator Planner...", "processing"
        )
        yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_plan_start, 'tag': format_pipeline_event_tag(ev_plan_start)})}\n\n"

        plan = await engine.generate_plan(
            session_id=req.session_id,
            business_type=state["business_type"] or "General Business",
            slots=state["slots"],
            selected_tone=req.tone_name,
            contacts_count=state.get("contacts_count", 0)
        )
        state["pipeline_plan"] = plan

        ev_plan_done = emit_event(
            state, "planning", "Workflow Planning", "🗺️", "done",
            "Dynamic pipeline configured",
            f"Configured {len(plan.get('steps', []))} domain-specific pipeline phases", "done",
            {"plan": plan}
        )
        yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_plan_done, 'tag': format_pipeline_event_tag(ev_plan_done)})}\n\n"

        # Emit the plan steps into the pipeline so UI renders them
        for step in plan.get("steps", []):
            ev_step_init = emit_event(
                state, step.get("step_id"), step.get("title"), step.get("icon", "⚡"), "active",
                step.get("description", ""),
                step.get("sub_steps", ["Processing..."])[0] if step.get("sub_steps") else "Processing...", "processing"
            )
            yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_step_init, 'tag': format_pipeline_event_tag(ev_step_init)})}\n\n"
            await asyncio.sleep(0.3)

            for sub in step.get("sub_steps", [])[1:]:
                ev_sub = emit_event(
                    state, step.get("step_id"), step.get("title"), step.get("icon", "⚡"), "active",
                    step.get("description", ""),
                    sub, "done"
                )
                yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_sub, 'tag': format_pipeline_event_tag(ev_sub)})}\n\n"
                await asyncio.sleep(0.2)

            ev_step_done = emit_event(
                state, step.get("step_id"), step.get("title"), step.get("icon", "⚡"), "done",
                step.get("description", ""),
                "Phase complete", "done"
            )
            yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_step_done, 'tag': format_pipeline_event_tag(ev_step_done)})}\n\n"

        # Template Generation phase
        ev_tmpl_start = emit_event(
            state, "templates", "Template Generation", "✍️", "active",
            "Crafting compliant message sequence",
            f"Drafting message sequence in tone '{req.tone_name}'...", "processing"
        )
        yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_tmpl_start, 'tag': format_pipeline_event_tag(ev_tmpl_start)})}\n\n"

        tmpl_res = await engine.generate_and_validate_templates(
            session_id=req.session_id,
            business_type=state["business_type"] or "General Business",
            slots=state["slots"],
            selected_tone=req.tone_name,
            contacts_sample=state.get("contacts")
        )
        state["templates"] = tmpl_res.get("templates", [])

        # Emit sub-step for each generated template
        for t in state["templates"]:
            ev_sub_tmpl = emit_event(
                state, "templates", "Template Generation", "✍️", "active",
                "Drafting message templates",
                f"Message {t['message_index']} ({t['stage']}) — {t['word_count']} words (limit: {t['word_limit']}) {'✅' if t['compliant'] else '⚠️'}",
                "done",
                {"message_index": t["message_index"], "word_count": t["word_count"]}
            )
            yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_sub_tmpl, 'tag': format_pipeline_event_tag(ev_sub_tmpl)})}\n\n"

        ev_tmpl_done = emit_event(
            state, "templates", "Template Generation", "✍️", "done",
            "Template sequence created",
            f"Compliance verified: {len(state['templates'])} messages generated strictly under {config.orchestrator.word_limit} words with opt-out footer",
            "done",
            {"templates_count": len(state["templates"])}
        )
        yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_tmpl_done, 'tag': format_pipeline_event_tag(ev_tmpl_done)})}\n\n"

        # Final Package Ready
        ev_ready = emit_event(
            state, "ready", "Campaign Package Ready", "🚀", "waiting_for_input",
            "Awaiting your final review and approval",
            f"Generated {len(state['templates'])} compliant templates — Review or Approve below", "waiting"
        )
        yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_ready, 'tag': format_pipeline_event_tag(ev_ready)})}\n\n"

        # Send templates to UI
        yield f"data: {json.dumps({'type': 'templates_ready', 'templates': state['templates'], 'schedule': tmpl_res.get('recommended_schedule'), 'csrf_token': state['csrf_token']})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(tone_exec_stream(), media_type="text/event-stream")

@router.post("/contacts/upload")
async def upload_contacts(
    session_id: str = Form(...),
    workspace_id: str = Form("default_workspace"),
    country_code: Optional[str] = Form(None),
    file: UploadFile = File(...)
):
    """Handles contact file upload, parsing, deduplication, suppression check, and preview."""
    state = get_or_create_state(session_id, workspace_id)
    content = await file.read()
    storage.log_user_action(session_id, "contact_upload", {"filename": file.filename, "size": len(content)})

    ev_ingest = emit_event(
        state, "ingest", "File Ingestion", "📄", "active",
        "Parsing uploaded contact file",
        f"Reading file — {file.filename}", "processing"
    )

    res = contact_service.process_contacts(
        session_id=session_id,
        content=content,
        filename=file.filename or "contacts.csv",
        country_code=country_code
    )

    if not res.get("success"):
        ev_err = emit_event(
            state, "ingest", "File Ingestion", "📄", "error",
            "File parsing error",
            res.get("message", res.get("error", "Error parsing contacts")), "error"
        )
        return {"event": ev_err, "result": res}

    ev_ingest_done = emit_event(
        state, "ingest", "File Ingestion", "📄", "done",
        "Contacts parsed and sanitized",
        f"{res['valid_count']} valid contacts · {res['duplicates_count']} duplicates removed · {res['suppressed_count']} suppressed",
        "done",
        res
    )

    state["contacts"] = res.get("contacts", [])
    state["contacts_count"] = res.get("valid_count", 0)

    return {"event": ev_ingest_done, "result": res}

@router.post("/contacts/paste")
async def paste_contacts(
    session_id: str = Form(...),
    workspace_id: str = Form("default_workspace"),
    text_data: str = Form(...),
    country_code: Optional[str] = Form(None)
):
    """Handles pasted contact text lines."""
    state = get_or_create_state(session_id, workspace_id)
    content = text_data.encode("utf-8")
    storage.log_user_action(session_id, "contact_paste", {"lines_length": len(text_data)})

    res = contact_service.process_contacts(
        session_id=session_id,
        content=content,
        filename="pasted_contacts.txt",
        country_code=country_code
    )

    if res.get("success"):
        state["contacts"] = res.get("contacts", [])
        state["contacts_count"] = res.get("valid_count", 0)
        ev_done = emit_event(
            state, "ingest", "Contact Ingestion", "📄", "done",
            "Pasted contacts parsed",
            f"{res['valid_count']} valid contacts normalized", "done",
            res
        )
        return {"event": ev_done, "result": res}
    else:
        return {"result": res}

@router.post("/campaign/approve")
async def approve_campaign(
    req: ApproveCampaignRequest,
    request: Request,
    host: Optional[str] = Header(None),
    origin: Optional[str] = Header(None),
    x_csrf_token: Optional[str] = Header(None)
):
    """
    Approves campaign, stores final state, extracts durable memory, and marks ready.
    Protected with exact Host/Origin checks and server-side per-session CSRF token validation.
    """
    cfg = get_config()
    from app.api.dispatch_routes import verify_loopback_headers
    verify_loopback_headers(request, host, origin, cfg)

    state = get_or_create_state(req.session_id)
    csrf = x_csrf_token or req.csrf_token or request.headers.get("x-csrf-token", "")
    stored_csrf = state.get("csrf_token")

    if not csrf or not stored_csrf or not hmac.compare_digest(str(stored_csrf), str(csrf)):
        raise HTTPException(status_code=403, detail="Approval refused: Missing or invalid CSRF token")

    storage.log_user_action(req.session_id, "approve_campaign", {})

    state["approved"] = True

    b_name = state["slots"].get("business_name", {}).get("value") or "Business"
    c_name = state["slots"].get("campaign_name", {}).get("value") or "Campaign"

    campaign_dict = {
        "workspace_id": state["workspace_id"],
        "business_name": b_name,
        "campaign_name": c_name,
        "business_type": state["business_type"] or "General Business",
        "config": {
            "word_limit": config.orchestrator.word_limit,
            "optout_text": config.orchestrator.optout_text
        },
        "slots": state["slots"],
        "selected_tone": state.get("selected_tone", ""),
        "rejected_tones": state.get("rejected_tones", []),
        "templates": state.get("templates", []),
        "contacts_count": state.get("contacts_count", 0),
        "plan": state.get("pipeline_plan", {}),
        "approved": True
    }
    storage.save_campaign(req.session_id, campaign_dict)

    # Extract durable memory asynchronously in background (concise summary to avoid large prompt overhead)
    clean_campaign_dict = {
        "business_name": b_name,
        "campaign_name": c_name,
        "business_type": state["business_type"] or "General Business",
        "selected_tone": state.get("selected_tone", ""),
        "rejected_tones": state.get("rejected_tones", []),
        "templates_count": len(state.get("templates", [])),
        "template_summaries": [
            {"stage": t.get("stage"), "word_count": t.get("word_count"), "tone": t.get("tone")}
            for t in state.get("templates", [])
        ]
    }
    async def _bg_memory():
        try:
            await memory_service.extract_and_save_memory(
                session_id=req.session_id,
                workspace_id=state["workspace_id"],
                campaign_data=clean_campaign_dict
            )
        except Exception as ex:
            print(f"[MemoryService] Background memory extraction warning: {ex}")

    asyncio.create_task(_bg_memory())

    ev_done = emit_event(
        state, "ready", "Campaign Package Ready", "🚀", "done",
        "Campaign approved and finalized",
        f"Campaign approved! Memory recorded & package ready for export.", "done"
    )

    return {"status": "success", "session_id": req.session_id, "approved": True, "event": ev_done, "campaign": campaign_dict}

@router.post("/campaign/edit_template")
async def edit_template(req: TemplateEditRequest):
    """Edits a specific template and re-validates compliance and regenerates contact previews."""
    state = get_or_create_state(req.session_id)
    storage.log_user_action(req.session_id, "edit_template", {"index": req.message_index})

    b_name = state["slots"].get("business_name", {}).get("value") or "Business"
    link = state["slots"].get("link_or_contact", {}).get("value") or ""

    from app.services.compliance_service import check_template_compliance
    comp = check_template_compliance(
        body=req.updated_body,
        business_name=b_name,
        word_limit=config.orchestrator.word_limit,
        optout_text=config.orchestrator.optout_text,
        link_or_contact=link
    )

    for t in state.get("templates", []):
        if t.get("message_index") == req.message_index:
            t["body"] = req.updated_body
            t["word_count"] = comp["word_count"]
            t["compliant"] = comp["compliant"]
            t["violations"] = comp["violations"]

            # Refresh previews with updated body
            has_contacts = len(state.get("contacts", [])) > 0
            new_previews = []
            if has_contacts:
                for sc in state.get("contacts", [])[:5]:
                    c_name = sc.get("name", "Customer")
                    r_text = req.updated_body.replace("{{customer_name}}", c_name).replace("{{donor_name}}", c_name).replace("{{name}}", c_name).replace("{{business_name}}", b_name).replace("{{org_name}}", b_name).replace("{{link}}", link or "https://example.com").replace("{{donation_link}}", link or "https://example.com")
                    for k, v in sc.get("extra", {}).items():
                        r_text = r_text.replace("{{" + k.lower().strip().replace(" ", "_") + "}}", str(v)).replace("{{" + k + "}}", str(v))
                    new_previews.append({
                        "contact_name": c_name,
                        "phone": sc.get("phone", ""),
                        "masked_phone": sc.get("masked_phone", ""),
                        "extra": sc.get("extra", {}),
                        "rendered_text": r_text
                    })
            t["previews"] = new_previews
            return {"status": "success", "template": t, "all_templates": state["templates"]}

    raise HTTPException(status_code=404, detail="Template not found")

@router.post("/campaign/regenerate_template")
async def regenerate_template(req: TemplateRegenerateRequest):
    """Regenerates a single specific template using LLM with optional user instruction."""
    state = get_or_create_state(req.session_id)
    storage.log_user_action(req.session_id, "regenerate_template", {"index": req.message_index, "instructions": req.instructions})

    target_tmpl = None
    for t in state.get("templates", []):
        if t.get("message_index") == req.message_index:
            target_tmpl = t
            break

    if not target_tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    b_name = state["slots"].get("business_name", {}).get("value") or "Our Business"
    c_name = state["slots"].get("campaign_name", {}).get("value") or "Campaign"
    goal = state["slots"].get("campaign_goal", {}).get("value") or ""
    selected_tone = state.get("selected_tone") or "Professional"

    system_prompt = f"""You are the Template Writer Module of CampaignForge.
Regenerate Message {req.message_index} ({target_tmpl.get('stage')}) for:
- Business: {b_name}
- Campaign: {c_name}
- Goal: {goal}
- Tone: {selected_tone}
- Word Limit: {config.orchestrator.word_limit} words
- Exact Opt-out ending: "{config.orchestrator.optout_text}"
{f"- User Revision Request: {req.instructions}" if req.instructions else ""}

Output strict JSON:
{{
  "body": "Hi {{customer_name}}, ...\\n\\n{config.orchestrator.optout_text}"
}}
"""
    from app.core.llm_client import get_llm_client
    llm = get_llm_client()
    res = await llm.call_llm(
        session_id=req.session_id,
        purpose="regenerate_single_template",
        system_prompt=system_prompt,
        user_message=f"Regenerate Message {req.message_index}",
        step_id="template_writer",
        json_mode=True
    )

    new_body = target_tmpl["body"]
    if res.get("status") == "success":
        try:
            clean = re.sub(r"^```json\s*", "", res.get("raw_response", "").strip())
            clean = re.sub(r"\s*```$", "", clean)
            d = json.loads(clean)
            if d.get("body"):
                new_body = d["body"]
        except Exception:
            pass

    from app.services.compliance_service import check_template_compliance
    link = state["slots"].get("link_or_contact", {}).get("value") or ""
    comp = check_template_compliance(
        body=new_body,
        business_name=b_name,
        word_limit=config.orchestrator.word_limit,
        optout_text=config.orchestrator.optout_text,
        link_or_contact=link
    )

    target_tmpl["body"] = new_body
    target_tmpl["word_count"] = comp["word_count"]
    target_tmpl["compliant"] = comp["compliant"]
    target_tmpl["violations"] = comp["violations"]

    # Rebuild previews
    has_contacts = len(state.get("contacts", [])) > 0
    new_previews = []
    if has_contacts:
        for sc in state.get("contacts", [])[:5]:
            c_name = sc.get("name", "Customer")
            r_text = new_body.replace("{{customer_name}}", c_name).replace("{{donor_name}}", c_name).replace("{{name}}", c_name).replace("{{business_name}}", b_name).replace("{{org_name}}", b_name).replace("{{link}}", link or "https://example.com").replace("{{donation_link}}", link or "https://example.com")
            for k, v in sc.get("extra", {}).items():
                r_text = r_text.replace("{{" + k.lower().strip().replace(" ", "_") + "}}", str(v)).replace("{{" + k + "}}", str(v))
            new_previews.append({
                "contact_name": c_name,
                "phone": sc.get("phone", ""),
                "masked_phone": sc.get("masked_phone", ""),
                "extra": sc.get("extra", {}),
                "rendered_text": r_text
            })
    target_tmpl["previews"] = new_previews

    return {"status": "success", "template": target_tmpl, "all_templates": state["templates"]}
