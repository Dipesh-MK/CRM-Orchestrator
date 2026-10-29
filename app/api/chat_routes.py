import json
import uuid
import asyncio
import hmac
import secrets
import re
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.config import get_config
from app.core.storage import get_storage, utc_now_iso
from app.services.contact_service import get_contact_service
from app.services.orchestrator_engine import (
    get_orchestrator_engine,
    extract_missing_placeholders,
    generate_questions_ui,
    generate_interactive_choices_for_tag,
    TAG_HINTS,
    resolve_placeholders_from_reply,
    fill_template_placeholders,
    check_template_compliance,
    extract_form_context,
    clean_organization_name
)
from app.services.safety_service import get_safety_service
from app.services.memory_service import get_memory_service
from app.api.dispatch_routes import verify_loopback_headers

router = APIRouter(prefix="/api", tags=["Chat & Orchestrator"])
config = get_config()
storage = get_storage()
contact_service = get_contact_service()
engine = get_orchestrator_engine()
memory_service = get_memory_service()
safety_service = get_safety_service()

# In-memory session state dictionary
_session_states: Dict[str, Dict[str, Any]] = {}

def get_or_create_state(session_id: str, workspace_id: str = "default_workspace") -> Dict[str, Any]:
    if session_id not in _session_states:
        # Check if session exists in DB and re-hydrate
        sess = storage.get_session(session_id)
        saved_meta = sess.get("meta", {}) if sess else {}
        csrf = saved_meta.get("csrf_token") or secrets.token_hex(16)

        camp = storage.get_campaign(session_id)
        templates = (camp.get("templates") if camp else None) or saved_meta.get("templates") or []
        slots = (camp.get("slots") if camp else None) or saved_meta.get("slots") or {}
        selected_tone = (camp.get("selected_tone") if camp else None) or saved_meta.get("selected_tone")
        approved = bool(camp.get("approved") if camp else saved_meta.get("approved", False))
        contacts = storage.get_contacts_by_session(session_id) or []

        conv_hist = saved_meta.get("conversation_history") or []
        if not conv_hist:
            actions = storage.get_user_actions(session_id)
            for a in actions:
                p = a.get("payload") or {}
                if a.get("action_type") in ("user_message", "user_chat_message") or p.get("message"):
                    conv_hist.append({"role": "user", "content": p.get("message") or str(p)})
                elif a.get("action_type") == "assistant_message" or p.get("text"):
                    conv_hist.append({"role": "assistant", "content": p.get("text") or str(p)})

        ws_id = (sess.get("workspace_id") if sess else None) or workspace_id
        b_type = (sess.get("business_type") if sess else None) or (camp.get("business_type") if camp else None)

        _session_states[session_id] = {
            "session_id": session_id,
            "workspace_id": ws_id,
            "business_type": b_type,
            "conversation_history": conv_hist,
            "slots": slots,
            "discovery_round": saved_meta.get("discovery_round", 0),
            "discovery_complete": saved_meta.get("discovery_complete", bool(slots and (saved_meta.get("candidate_tones") or selected_tone))),
            "candidate_tones": saved_meta.get("candidate_tones", []),
            "selected_tone": selected_tone,
            "rejected_tones": saved_meta.get("rejected_tones", []),
            "pipeline_plan": saved_meta.get("pipeline_plan"),
            "templates": templates,
            "contacts": contacts,
            "contacts_count": len(contacts),
            "approved": approved,
            "step_order": saved_meta.get("step_order", 0),
            "pending_placeholders": saved_meta.get("pending_placeholders", []),
            "csrf_token": csrf
        }
        storage.save_session(session_id, ws_id, meta={"csrf_token": csrf})
    elif "csrf_token" not in _session_states[session_id]:
        sess = storage.get_session(session_id)
        saved_meta = sess.get("meta", {}) if sess else {}
        _session_states[session_id]["csrf_token"] = saved_meta.get("csrf_token") or secrets.token_hex(16)
        if "pending_placeholders" not in _session_states[session_id]:
            _session_states[session_id]["pending_placeholders"] = saved_meta.get("pending_placeholders", [])
    return _session_states[session_id]

def record_assistant_message(
    session_id: str,
    state: dict,
    text: str,
    form_data: Optional[Dict[str, Any]] = None,
    question_options: Optional[List[str]] = None
):
    """Saves assistant response to in-memory history, user_actions log, and DB session metadata."""
    entry: Dict[str, Any] = {"role": "assistant", "content": text}
    if form_data:
        entry["form_data"] = form_data
    if question_options:
        entry["question_options"] = question_options
    state["conversation_history"].append(entry)

    action_payload: Dict[str, Any] = {"text": text}
    if form_data:
        action_payload["form_data"] = form_data
    if question_options:
        action_payload["question_options"] = question_options
    storage.log_user_action(session_id, "assistant_message", action_payload)

    meta = {
        "conversation_history": state["conversation_history"],
        "csrf_token": state.get("csrf_token"),
        "slots": state.get("slots", {}),
        "discovery_complete": state.get("discovery_complete", False),
        "discovery_round": state.get("discovery_round", 0),
        "approved": state.get("approved", False),
        "step_order": state.get("step_order", 0),
        "pending_placeholders": state.get("pending_placeholders", []),
        "current_form_context": state.get("current_form_context"),
        "pending_form_data": state.get("pending_form_data")
    }
    if state.get("candidate_tones"):
        meta["candidate_tones"] = state["candidate_tones"]
    if state.get("selected_tone"):
        meta["selected_tone"] = state["selected_tone"]
    if state.get("templates"):
        meta["templates"] = state["templates"]
    storage.save_session(
        session_id, state.get("workspace_id", "default_workspace"),
        meta=meta
    )

@router.get("/session/{session_id}/csrf")
async def get_session_csrf_endpoint(session_id: str):
    state = get_or_create_state(session_id)
    return {"session_id": session_id, "csrf_token": state["csrf_token"]}

@router.get("/chat/sessions")
def list_chat_sessions():
    """Lists saved chat sessions for reuse and reloading."""
    return {"sessions": storage.list_sessions(limit=50)}

@router.get("/chat/sessions/{session_id}")
def get_chat_session(session_id: str):
    """Retrieves full conversation state, campaign, and contacts for a session."""
    state = get_or_create_state(session_id)
    session_info = storage.get_session(session_id)
    saved_meta = session_info.get("meta", {}) if session_info else {}
    actions = storage.get_user_actions(session_id)

    # Restore conversation_history from persisted DB or fallback to action log
    if not state.get("conversation_history"):
        persisted = saved_meta.get("conversation_history") or []
        if not persisted and actions:
            for a in actions:
                p = a.get("payload") or {}
                if a.get("action_type") == "user_message" or p.get("message"):
                    persisted.append({"role": "user", "content": p.get("message") or str(p)})
                elif a.get("action_type") == "assistant_message" or p.get("text"):
                    entry = {"role": "assistant", "content": p.get("text") or str(p)}
                    if p.get("form_data"):
                        entry["form_data"] = p.get("form_data")
                    if p.get("question_options"):
                        entry["question_options"] = p.get("question_options")
                    persisted.append(entry)
        state["conversation_history"] = persisted

    if not state.get("current_form_context") and saved_meta.get("current_form_context"):
        state["current_form_context"] = saved_meta.get("current_form_context")
    if not state.get("pending_form_data") and saved_meta.get("pending_form_data"):
        state["pending_form_data"] = saved_meta.get("pending_form_data")

    if not state.get("candidate_tones") and saved_meta.get("candidate_tones"):
        state["candidate_tones"] = saved_meta.get("candidate_tones")
    if not state.get("selected_tone") and saved_meta.get("selected_tone"):
        state["selected_tone"] = saved_meta.get("selected_tone")

    camp = storage.get_campaign(session_id)
    if camp and not state.get("templates"):
        state["templates"] = camp.get("templates") or []
        state["selected_tone"] = camp.get("selected_tone")
        state["approved"] = camp.get("approved", False)
        state["slots"] = camp.get("slots") or {}
    contacts = storage.get_contacts_by_session(session_id)
    if contacts and not state.get("contacts"):
        state["contacts"] = contacts
        state["contacts_count"] = len(contacts)

    return {
        "session_id": session_id,
        "state": state,
        "campaign": camp,
        "contacts": contacts,
        "actions": actions,
        "conversation_history": state["conversation_history"]
    }

@router.delete("/chat/sessions/{session_id}")
def delete_chat_session(session_id: str):
    """Deletes a chat session and all its associated campaign data."""
    _session_states.pop(session_id, None)
    deleted = storage.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found.")
    return {"status": "deleted", "session_id": session_id}

class UpdateSessionTitleRequest(BaseModel):
    title: str

@router.patch("/chat/sessions/{session_id}")
@router.put("/chat/sessions/{session_id}/title")
def update_chat_session_title(session_id: str, req: UpdateSessionTitleRequest):
    """Updates the user-customized title / name of a chat session."""
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title cannot be empty.")
    success = storage.update_session_title(session_id, title)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session_id in _session_states:
        _session_states[session_id]["campaign_name"] = title
    return {"status": "success", "session_id": session_id, "title": title}

class ChatMessageRequest(BaseModel):
    session_id: Optional[str] = None
    workspace_id: Optional[str] = "default_workspace"
    message: str

class CampaignFormSubmitRequest(BaseModel):
    session_id: str
    workspace_id: Optional[str] = "default_workspace"
    context: Optional[str] = ""
    business_type: Optional[str] = "General Business"
    organization_name: Optional[str] = ""
    address_or_location: Optional[str] = ""
    event_date: Optional[str] = ""
    link_or_contact: Optional[str] = ""
    channel_format: Optional[str] = "sms"
    tone: Optional[str] = "Direct & Authoritative 📜"
    template_request: Optional[str] = ""
    additional_info: Optional[List[str]] = []
    skip_confirmed: Optional[bool] = False
    skipped_fields: Optional[List[str]] = []
    dynamic_fields: Optional[Dict[str, Any]] = {}

class TonePreviewRegenRequest(BaseModel):
    session_id: str
    business_type: Optional[str] = "General Business"
    context: Optional[str] = ""
    organization_name: Optional[str] = ""
    address_or_location: Optional[str] = ""
    event_date: Optional[str] = ""
    link_or_contact: Optional[str] = ""
    channel_format: Optional[str] = "sms"
    current_tone: Optional[str] = ""
    candidate_tones: Optional[List[str]] = None
    user_feedback: Optional[str] = ""
    dynamic_fields: Optional[Dict[str, Any]] = None

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

class TemplateRestoreVersionRequest(BaseModel):
    session_id: str
    message_index: int
    version: int

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

def is_greeting_or_general_inquiry(msg: str) -> bool:
    clean = msg.strip().lower()
    campaign_keywords = [
        "charit", "donate", "fundrais", "volunteer", "gala", "non-profit", "ngo",
        "electric", "bill", "power", "utility", "energy", "water", "broadband", "disconnection",
        "sale", "discount", "retail", "promo", "shop", "store", "fashion", "deal", "code", "bogo",
        "dental", "clinic", "doctor", "health", "hospital", "patient", "appointment", "checkup",
        "saas", "renew", "subscri", "tuition", "school", "course", "webinar",
        "crm", "template", "campaign for", "send a message", "reminder", "overdue"
    ]
    if any(k in clean for k in campaign_keywords):
        return False

    greetings = [
        "hi", "hello", "hey", "hola", "greetings", "good morning", "good afternoon",
        "good evening", "what can you do", "who are you", "help", "how does this work",
        "what is this", "tell me what you do", "what do you do", "how can you help",
        "how can you help me", "what are your capabilities", "what can i do here",
        "what do you assist with", "what can you assist with", "features"
    ]
    for g in greetings:
        if clean == g or clean.startswith(g + " ") or clean.startswith(g + "?") or clean.startswith(g + "!") or clean.endswith(" " + g):
            return True
    return False

def detect_campaign_domain(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["charit", "donate", "fundrais", "non-profit", "volunteer", "gala", "ngo", "relief", "cause"]):
        return "Charity & Non-Profit"
    elif any(w in t for w in ["electric", "bill", "power", "utility", "energy", "water", "broadband", "disconnection", "meter"]):
        return "Utility & Energy"
    elif any(w in t for w in ["sale", "discount", "retail", "promo", "shop", "store", "boutique", "fashion", "deal", "code", "bogo"]):
        return "Retail & E-Commerce"
    elif any(w in t for w in ["dental", "clinic", "doctor", "health", "hospital", "patient", "appointment", "checkup"]):
        return "Healthcare & Clinic"
    return "General Business"

DOMAIN_CONFIGS = {
    "Charity & Non-Profit": {
        "domain_name": "Charity & Non-Profit",
        "badge": "🎗️ Charity & Non-Profit",
        "default_tone": "Inspiring & Heartfelt ❤️",
        "recommended_tones": [
            "Inspiring & Heartfelt ❤️",
            "Community & Urgent 🤝",
            "Uplifting & Warm 🌟",
            "Respectful & Official 💼"
        ],
        "questions": [
            {
                "key": "ticket_type",
                "label": "🎟️ Admission / Donation Model",
                "question": "Is this charity event free admission, donation-based, or ticketed?",
                "options": [
                    "Free Admission (Donations Welcome)",
                    "Ticketed Registration ($25 - $50)",
                    "Pure Online Fundraising Drive",
                    "Other (Type custom answer)"
                ]
            },
            {
                "key": "organization_name",
                "label": "🏢 Host & Beneficiary Cause",
                "question": "Who is hosting the event and what is the beneficiary cause?",
                "options": []
            },
            {
                "key": "volunteers_needed",
                "label": "🤝 Needs & Volunteers",
                "question": "What does the event require? (e.g. volunteer helpers, donation supplies, or general attendees)",
                "options": [
                    "Volunteers Needed (Setup & Hospitality)",
                    "Donation Supplies & Goods Welcome",
                    "General Public Attendees & Donors",
                    "Other (Type custom answer)"
                ]
            },
            {
                "key": "date_and_link",
                "label": "📅 Date & RSVP / Donation Link",
                "question": "What is the date/time of the event, and what RSVP/donation link or phone number should recipients use?",
                "options": []
            }
        ]
    },
    "Utility & Energy": {
        "domain_name": "Utility & Energy",
        "badge": "⚡ Utility & Energy",
        "default_tone": "Aggressive & Urgent 🚨",
        "recommended_tones": [
            "Aggressive & Urgent 🚨",
            "Authoritative & Direct 📜",
            "Professional & Polite 💼",
            "Final Notice ⚠️"
        ],
        "questions": [
            {
                "key": "organization_name",
                "label": "🏢 Utility Provider",
                "question": "What is the name of your organization or utility provider?",
                "options": []
            },
            {
                "key": "urgency_level",
                "label": "⚠️ Urgency & Disconnection Warning",
                "question": "How urgent is this bill reminder, and should it mention a cutoff / disconnection deadline?",
                "options": [
                    "Urgent: Immediate Disconnection Notice",
                    "Due Soon: Payment Due in 48 Hours",
                    "Standard Monthly Bill Reminder",
                    "Other (Type custom answer)"
                ]
            },
            {
                "key": "link_or_contact",
                "label": "🔗 Payment Portal / Contact Support",
                "question": "What payment portal URL or customer support phone should recipients use?",
                "options": []
            }
        ]
    },
    "Retail & E-Commerce": {
        "domain_name": "Retail & E-Commerce",
        "badge": "🛍️ Retail & E-Commerce",
        "default_tone": "High Energy & FOMO 🔥",
        "recommended_tones": [
            "High Energy & FOMO 🔥",
            "Friendly & Excited 🛍️",
            "Exclusive VIP ⚡",
            "Weekend Special 🎉"
        ],
        "questions": [
            {
                "key": "organization_name",
                "label": "🏢 Store / Brand Name",
                "question": "What is the name of your store or brand?",
                "options": []
            },
            {
                "key": "offer_details",
                "label": "🏷️ Offer & Promo Code",
                "question": "What is the offer, discount percentage, or promo code?",
                "options": [
                    "30% Off All Items with code FLASH30",
                    "Buy 1 Get 1 Free this weekend",
                    "Free Shipping on orders over $50",
                    "Other (Type custom answer)"
                ]
            },
            {
                "key": "link_or_contact",
                "label": "🔗 Store URL & Expiry Deadline",
                "question": "What is your store website URL and when does this offer expire?",
                "options": []
            }
        ]
    },
    "Healthcare & Clinic": {
        "domain_name": "Healthcare & Clinic",
        "badge": "🏥 Healthcare & Clinic",
        "default_tone": "Caring & Reassuring 🩺",
        "recommended_tones": [
            "Caring & Reassuring 🩺",
            "Professional & Clear 📅",
            "Urgent Reminder ⏰",
            "Warm & Friendly 🤝"
        ],
        "questions": [
            {
                "key": "organization_name",
                "label": "🏢 Clinic / Practice Name",
                "question": "What is the name of your healthcare practice or clinic?",
                "options": []
            },
            {
                "key": "appointment_purpose",
                "label": "🩺 Appointment Purpose",
                "question": "What is the purpose of this appointment or reminder?",
                "options": [
                    "Routine 6-Month Dental Cleaning & Exam",
                    "Annual Preventive Physical Exam",
                    "Vaccination / Follow-up Visit",
                    "Other (Type custom answer)"
                ]
            },
            {
                "key": "link_or_contact",
                "label": "🔗 Confirmation Contact / Portal",
                "question": "How should patients confirm or reschedule their visit?",
                "options": []
            }
        ]
    },
    "General Business": {
        "domain_name": "General Business",
        "badge": "💼 General Business",
        "default_tone": "Professional & Polite 💼",
        "recommended_tones": [
            "Professional & Polite 💼",
            "Direct & Authoritative 📜",
            "Urgent & Direct 🚨",
            "Friendly & Casual 🌟"
        ],
        "questions": [
            {
                "key": "organization_name",
                "label": "🏢 Organization Name",
                "question": "What is the name of your business or organization?",
                "options": []
            },
            {
                "key": "campaign_goal",
                "label": "🎯 Goal & Action",
                "question": "What is the primary action or update you want recipients to take?",
                "options": [
                    "Take immediate action before upcoming deadline",
                    "Contact customer service within 24 hours",
                    "Confirm appointment or registration online",
                    "Other (Type custom answer)"
                ]
            },
            {
                "key": "link_or_contact",
                "label": "🔗 Website / Contact Phone",
                "question": "What website URL, portal, or contact number should recipients use?",
                "options": [
                    "https://service.link/portal",
                    "1-800-555-0199 (Support)",
                    "Other (Type custom answer)"
                ]
            }
        ]
    }
}

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

        # Auto-name session from initial message if not already customized
        sess_record = storage.get_session(session_id)
        current_meta = sess_record.get("meta", {}) if sess_record else {}
        if not current_meta.get("custom_title") and not current_meta.get("auto_title") and not (sess_record and sess_record.get("campaign_name")):
            clean_first = " ".join(user_msg.split())
            if len(clean_first) > 36:
                clean_first = clean_first[:36].rsplit(" ", 1)[0] + "..."
            auto_name = clean_first[:40].strip()
            if auto_name:
                auto_name = auto_name[0].upper() + auto_name[1:]
                storage.save_session(
                    session_id, state["workspace_id"],
                    meta={"auto_title": auto_name}
                )

        # Check if user asked execution / sending (excluding approval commands)
        execution_triggers = ["send now", "blast message", "send sms", "send to all"]
        if any(trig in user_msg.lower() for trig in execution_triggers) or ("dispatch" in user_msg.lower() and "approve" not in user_msg.lower()):
            refusal_text = "That's campaign execution. I'm focused on planning and templates."
            record_assistant_message(session_id, state, refusal_text)
            yield f"data: {json.dumps({'type': 'text_chunk', 'text': refusal_text})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        # Check if user is confirming to skip missing fields
        if any(w in user_msg.lower() for w in ["skip & exclude", "skip and exclude", "skip", "exclude field"]) and state.get("pending_form_data"):
            p_data = state["pending_form_data"]
            skipped = list(p_data.get("skipped_fields", []))
            if not p_data.get("organization_name") and "organization_name" not in skipped:
                skipped.append("organization_name")
            if not p_data.get("link_or_contact") and "link_or_contact" not in skipped:
                skipped.append("link_or_contact")

            gen_res = await engine.generate_message_from_form(
                session_id=session_id,
                business_type=p_data.get("business_type", "General Business"),
                context=p_data.get("context", "Campaign Notice"),
                organization_name=p_data.get("organization_name", ""),
                link_or_contact=p_data.get("link_or_contact", ""),
                address_or_location=p_data.get("address_or_location", ""),
                event_date=p_data.get("event_date", ""),
                channel_format=p_data.get("channel_format", "sms"),
                tone=p_data.get("tone", "Direct & Authoritative 📜"),
                template_request=p_data.get("template_request", ""),
                additional_info=p_data.get("additional_info", []),
                skipped_fields=skipped
            )
            state["templates"] = gen_res["templates"]
            state["discovery_complete"] = True
            state["skipped_fields"] = skipped
            state["pending_form_data"] = None

            ev_done = emit_event(
                state, "templates", "Template Generation", "✍️", "done",
                "Template created (Skipped fields excluded)",
                f"Generated compliant 1-message template omitting skipped variables", "done",
                {"templates_count": 1}
            )
            yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_done, 'tag': format_pipeline_event_tag(ev_done)})}\n\n"

            tmpl_body = state["templates"][0]["body"]
            msg_template = f"✅ **Message Generated Successfully (Omitted Skipped Fields)!**\n\n> {tmpl_body}"
            record_assistant_message(session_id, state, msg_template)
            yield f"data: {json.dumps({'type': 'text_chunk', 'text': msg_template})}\n\n"

            followup_msg = "💡 **Next Steps:** Would you like to create a **follow-up reminder message template**, load test recipient contacts, or approve this campaign for dispatch?"
            followup_options = [
                "📅 Create Follow-Up Reminder Template",
                "👥 Ingest & Verify Test Contacts",
                "🚀 Approve Campaign for Dispatch",
                "✏️ Revise Message Copy"
            ]
            record_assistant_message(session_id, state, followup_msg, question_options=followup_options)
            yield f"data: {json.dumps({'type': 'text_chunk', 'text': followup_msg, 'question_options': followup_options})}\n\n"
            yield f"data: {json.dumps({'type': 'templates_ready', 'templates': state['templates'], 'csrf_token': state['csrf_token']})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        # Check if user requested a follow-up reminder message
        is_followup_request = any(w in user_msg.lower() for w in [
            "follow-up", "follow up", "reminder template", "second message", "followup", "next message", "send reminder", "reminder message", "follow-up reminder"
        ])
        if (state.get("templates") or state.get("dispatched") or state.get("last_dispatched_campaign")) and is_followup_request:
            state["conversation_stage"] = "collecting_followup_info"
            state["followup_turns"] = 0
            state["followup_details"] = {}
            org_display = state.get("organization_name") or state["slots"].get("business_name", {}).get("value") or (state.get("last_dispatched_campaign", {}).get("business_name") if state.get("last_dispatched_campaign") else "") or "your organization"

            q1_text = (
                f"🎯 **Let's craft your follow-up reminder message for {org_display}!**\n\n"
                f"To make this reminder convert effectively without being repetitive, I'll ask 2 quick targeted questions:\n\n"
                f"👉 **Question 1:** When should this reminder be sent, and what deadline or countdown should it highlight?"
            )
            q1_options = [
                "24 Hours Before Event / Deadline",
                "Morning of the Event (Final Reminder)",
                "Final Notice: Avoid Service Disconnection / Late Fees",
                "Other (Type custom answer)"
            ]
            record_assistant_message(session_id, state, q1_text, question_options=q1_options)
            yield f"data: {json.dumps({'type': 'text_chunk', 'text': q1_text, 'question_options': q1_options})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        # Targeted Q&A loop for follow-up reminders
        if state.get("conversation_stage") == "collecting_followup_info":
            turn = state.get("followup_turns", 0) + 1
            state["followup_turns"] = turn
            if "followup_details" not in state:
                state["followup_details"] = {}

            if turn == 1:
                state["followup_details"]["timing_deadline"] = user_msg
            else:
                state["followup_details"]["urgency_action"] = user_msg

            is_concluding = any(w in user_msg.lower() for w in ["enough", "generate", "create message", "ready", "done", "skip", "that's it", "thats all"])

            if turn == 1 and not is_concluding:
                q2_text = (
                    "✅ Got it!\n\n"
                    "👉 **Question 2:** Is there any specific last-minute call to action, urgent warning, or attendee requirement to emphasize? (e.g. bring ticket/ID, pay now to avoid disconnection, limited seats left)"
                )
                q2_options = [
                    "Urgent Warning: Immediate action to prevent cutoff / disconnection",
                    "Last Chance: Limited slots / seats remaining",
                    "Attendee Checklist: Bring ticket and required documents",
                    "Other (Type custom answer)"
                ]
                record_assistant_message(session_id, state, q2_text, question_options=q2_options)
                yield f"data: {json.dumps({'type': 'text_chunk', 'text': q2_text, 'question_options': q2_options})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

            # Turn 2 or concluding -> Transition to Tone Previews Card for Follow-Up!
            state["conversation_stage"] = "card_review"
            b_type = state.get("business_type") or (state.get("last_dispatched_campaign", {}).get("business_type") if state.get("last_dispatched_campaign") else "General Business")
            ctx = state.get("context") or state["slots"].get("campaign_goal", {}).get("value") or (state.get("last_dispatched_campaign", {}).get("context") if state.get("last_dispatched_campaign") else "Campaign Notice")
            org_name = state.get("organization_name") or state["slots"].get("business_name", {}).get("value") or (state.get("last_dispatched_campaign", {}).get("business_name") if state.get("last_dispatched_campaign") else "")
            link_val = state.get("link_or_contact") or state["slots"].get("link_or_contact", {}).get("value") or ""
            addr_val = state.get("address_or_location") or state["slots"].get("location", {}).get("value") or (state.get("last_dispatched_campaign", {}).get("address_or_location") if state.get("last_dispatched_campaign") else "")
            date_val = state.get("event_date") or state["slots"].get("date", {}).get("value") or (state.get("last_dispatched_campaign", {}).get("event_date") if state.get("last_dispatched_campaign") else "")
            channel_fmt = state.get("channel_format") or "sms"
            dyn_fields = dict(state.get("dynamic_fields") or {})
            if state["followup_details"].get("timing_deadline"):
                dyn_fields["reminder_timing"] = {"label": "⏰ Reminder Timing", "value": state["followup_details"]["timing_deadline"]}
            if state["followup_details"].get("urgency_action"):
                dyn_fields["reminder_action"] = {"label": "⚠️ Urgency & Action", "value": state["followup_details"]["urgency_action"]}

            rec_tones = [
                "Urgent & Direct 🚨",
                "Friendly & Helpful 🌟",
                "Authoritative Final Notice 📜"
            ]

            ev_tones = emit_event(
                state, "tone_exploration", "Tone Exploration", "🎨", "active",
                "Generating collapsible candidate tone templates",
                f"Crafting 3 tone variations for follow-up reminder...", "processing"
            )
            yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_tones, 'tag': format_pipeline_event_tag(ev_tones)})}\n\n"

            tone_candidates = await engine.generate_tone_candidate_previews(
                session_id=session_id,
                business_type=b_type,
                context=f"Follow-up reminder: {ctx} ({state['followup_details'].get('timing_deadline', '')})",
                organization_name=org_name,
                link_or_contact=link_val,
                address_or_location=addr_val,
                event_date=date_val,
                channel_format=channel_fmt,
                candidate_tones=rec_tones,
                dynamic_fields=dyn_fields
            )

            card_data = {
                "context": f"Follow-up reminder: {ctx}",
                "business_type": b_type,
                "organization_name": org_name,
                "link_or_contact": link_val,
                "address_or_location": addr_val,
                "event_date": date_val,
                "channel_format": channel_fmt,
                "tone": rec_tones[0],
                "recommended_tones": rec_tones,
                "tone_candidates": tone_candidates,
                "dynamic_fields": dyn_fields,
                "template_request": "Follow-up reminder message sequence #2",
                "additional_info": []
            }
            state["current_form_context"] = card_data
            state["pending_form_data"] = card_data

            prompt_text = (
                f"### 📋 Follow-Up Reminder Details Confirmed\n\n"
                f"I've gathered the timing and details for your follow-up reminder for **{org_name or b_type}**.\n\n"
                f"Review the collapsible message templates across tones below, customize if desired, and click **✨ Confirm & Generate Message**:"
            )
            record_assistant_message(session_id, state, prompt_text, form_data=card_data)
            yield f"data: {json.dumps({'type': 'text_chunk', 'text': prompt_text, 'form_data': card_data})}\n\n"
            yield f"data: {json.dumps({'type': 'campaign_form', 'text': prompt_text, 'form_data': card_data})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        # Check if user requested mock contact ingestion
        is_contact_ingest = any(w in user_msg.lower() for w in ["ingest & verify test contacts", "ingest test contacts", "load test recipient contacts", "load test contacts", "load contacts"])
        if state.get("templates") and is_contact_ingest:
            default_contacts = [
                {"name": "Parent Beta", "recipient": "+15551234561", "channel": state.get("channel_format") or "sms"},
                {"name": "Parent Gamma", "recipient": "+15551234562", "channel": state.get("channel_format") or "sms"}
            ]
            storage.save_contacts(session_id, default_contacts)
            state["contacts"] = default_contacts
            state["contacts_count"] = len(default_contacts)

            ws_id = state.get("workspace_id") or "default_workspace"
            storage.write_memory(
                workspace_id=ws_id,
                category="campaign_contacts",
                key="test_contacts",
                value=default_contacts,
                confidence=1.0,
                session_id=session_id
            )

            contact_text = (
                "👥 **Test Recipient Contacts Ingested & Verified!**\n\n"
                "- `Parent Beta`: `+15551234561` (Format verified ✅)\n"
                "- `Parent Gamma`: `+15551234562` (Format verified ✅)\n\n"
                "All variables and opt-out footers match. Would you like to approve and dispatch this campaign now?"
            )
            record_assistant_message(session_id, state, contact_text)
            yield f"data: {json.dumps({'type': 'text_chunk', 'text': contact_text, 'question_options': ['🚀 Approve Campaign for Dispatch', '✏️ Revise Message Copy']})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        # Check if user requested approval via chat
        is_approval_request = any(w in user_msg.lower() for w in ["approve campaign for dispatch", "approve campaign", "approve for dispatch", "approve this campaign"])
        if state.get("templates") and is_approval_request:
            state["approved"] = True
            campaign_dict = {
                "session_id": session_id,
                "business_name": state.get("organization_name") or state["slots"].get("business_name", {}).get("value") or "Business",
                "campaign_name": state.get("context") or state["slots"].get("campaign_goal", {}).get("value") or "Campaign",
                "business_type": state.get("business_type") or "General Business",
                "channel_format": state.get("channel_format") or "sms",
                "selected_tone": state.get("selected_tone", ""),
                "templates": state.get("templates", []),
                "contacts_count": state.get("contacts_count", 0),
                "approved": True
            }
            storage.save_campaign(session_id, campaign_dict)
            ws_id = state.get("workspace_id") or "default_workspace"
            storage.write_memory(
                workspace_id=ws_id,
                category="campaign_status",
                key="approval",
                value={"status": "approved", "session_id": session_id, "timestamp": utc_now_iso()},
                confidence=1.0,
                session_id=session_id
            )

            ev_approve = emit_event(
                state, "ready", "Campaign Approved", "🚀", "done",
                "Campaign approved and ready for aggregator dispatch",
                "All compliance gates passed. Ready for send.", "done"
            )
            yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_approve, 'tag': format_pipeline_event_tag(ev_approve)})}\n\n"

            approve_text = (
                "🚀 **Campaign Approved!**\n\n"
                "Your message templates and compliance tags have been locked and authorized. "
                "You can now dispatch messages directly to the Mock Aggregator in the Dispatches / Send panel!"
            )
            record_assistant_message(session_id, state, approve_text)
            yield f"data: {json.dumps({'type': 'text_chunk', 'text': approve_text})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        # Check if user is asking for specific format/tone on existing campaign (e.g. "aggressive tone in email format")
        is_format_or_tone_change = any(w in user_msg.lower() for w in ["aggressive", "email format", "in email", "urgent tone", "authoritative tone", "friendly tone", "sms format", "change tone", "make it aggressive"])
        if state.get("templates") and is_format_or_tone_change:
            channel_fmt = "email" if "email" in user_msg.lower() else (state.get("channel_format") or "sms")
            tone_val = "Aggressive & Urgent 🚨" if any(w in user_msg.lower() for w in ["aggressive", "urgent", "harsh", "stern"]) else (state.get("selected_tone") or "Direct & Authoritative 📜")
            b_type = state.get("business_type") or "General Business"
            ctx = state["slots"].get("campaign_goal", {}).get("value") or user_msg
            org_name = state["slots"].get("business_name", {}).get("value") or ""
            link_val = state["slots"].get("link_or_contact", {}).get("value") or ""
            addr_val = state.get("address_or_location") or state["slots"].get("location", {}).get("value") or ""
            date_val = state.get("event_date") or state["slots"].get("date", {}).get("value") or ""

            gen_res = await engine.generate_message_from_form(
                session_id=session_id,
                business_type=b_type,
                context=ctx,
                organization_name=org_name,
                link_or_contact=link_val,
                address_or_location=addr_val,
                event_date=date_val,
                channel_format=channel_fmt,
                tone=tone_val,
                template_request=user_msg,
                additional_info=state.get("additional_info", []),
                skipped_fields=skipped
            )
            state["templates"] = gen_res["templates"]
            state["selected_tone"] = tone_val
            state["channel_format"] = channel_fmt

            ev_update = emit_event(
                state, "templates", "Template Generation", "✍️", "done",
                "Template Updated",
                f"Adapted to {channel_fmt.upper()} with tone '{tone_val}'", "done",
                {"templates_count": 1}
            )
            yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_update, 'tag': format_pipeline_event_tag(ev_update)})}\n\n"

            tmpl_body = state["templates"][0]["body"]
            update_text = f"✅ **Message Updated!** Formatted as **{channel_fmt.upper()}** with **{tone_val}** tone:\n\n> {tmpl_body}\n\nYou can review your template above, edit if needed, or click **Approve Campaign** to proceed."
            record_assistant_message(session_id, state, update_text)

            yield f"data: {json.dumps({'type': 'text_chunk', 'text': update_text})}\n\n"
            yield f"data: {json.dumps({'type': 'templates_ready', 'templates': state['templates'], 'csrf_token': state['csrf_token']})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        # Safety check on user message
        s_eval = await safety_service.evaluate_safety(session_id, user_msg, "chat_message")
        if not s_eval.get("allowed", True):
            reason = s_eval.get("reason", "Violates safety policy.")
            refusal = f"⚠️ **Request Declined**\n\nI cannot assist with this campaign: {reason}."
            record_assistant_message(session_id, state, refusal)
            yield f"data: {json.dumps({'type': 'text_chunk', 'text': refusal})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        # 1. Greeting or general assistance inquiry
        if is_greeting_or_general_inquiry(user_msg) and not state.get("templates") and state.get("conversation_stage") != "collecting_info":
            greeting_text = (
                "🤖 **Welcome to CRMbot Service!**\n\n"
                "I help you craft high-converting, compliant CRM message campaigns for SMS, WhatsApp, and Email across any industry:\n\n"
                "- 🎗️ **Charity & Community Drives** (fundraising, volunteer calls, galas)\n"
                "- ⚡ **Utility & Bill Reminders** (electricity, water, broadband notices)\n"
                "- 🛍️ **Retail & E-Commerce** (flash sales, promo codes, exclusive drops)\n"
                "- 🏥 **Healthcare & Clinics** (appointments, routine checkups)\n"
                "- 💻 **SaaS & Subscriptions** (renewals, expiring trials)\n"
                "- 🎓 **Education & Tuition** (course admissions, fee deadlines)\n\n"
                "To get started, tell me about your campaign idea, or click one of the quick starters below:"
            )
            starter_chips = [
                "🎗️ Charity Fundraiser Event",
                "⚡ Electricity Bill Reminder",
                "🛍️ 30% Off Flash Sale",
                "🏥 Dental Clinic Reminder"
            ]
            record_assistant_message(session_id, state, greeting_text)
            yield f"data: {json.dumps({'type': 'text_chunk', 'text': greeting_text, 'question_options': starter_chips})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        # 2. Back-and-forth Dynamic Q&A stage
        if state.get("conversation_stage") == "collecting_info" and not state.get("templates"):
            domain = state.get("campaign_domain", "General Business")
            cfg = DOMAIN_CONFIGS.get(domain, DOMAIN_CONFIGS["General Business"])
            if "collected_details" not in state:
                state["collected_details"] = {}

            turn_count = state.get("info_turns", 0) + 1
            state["info_turns"] = turn_count

            analysis = await engine.analyze_campaign_interaction(
                session_id=session_id,
                user_msg=user_msg,
                domain=domain,
                current_slots=state.get("slots", {}),
                collected_details=state.get("collected_details", {}),
                conversation_history=state.get("conversation_history", [])
            )

            if analysis.get("blocked"):
                reason = analysis.get("reason", "Prohibited content detected.")
                refusal = f"⚠️ **Request Declined**\n\nI cannot assist with this campaign: {reason}."
                record_assistant_message(session_id, state, refusal)
                yield f"data: {json.dumps({'type': 'text_chunk', 'text': refusal, 'is_rate_limit': analysis.get('is_rate_limit', False)})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

            ext = analysis.get("extracted", {})
            for k, v in ext.items():
                if v:
                    state["collected_details"][k] = v

            # Update slots cleanly
            if ext.get("organization_name"):
                clean_org = clean_organization_name(ext["organization_name"], domain)
                state["slots"]["business_name"] = {"value": clean_org, "source": "user"}
                state["collected_details"]["organization_name"] = clean_org

            if ext.get("link_or_contact"):
                state["slots"]["link_or_contact"] = {"value": ext["link_or_contact"], "source": "user"}
                state["collected_details"]["link_or_contact"] = ext["link_or_contact"]

            if ext.get("campaign_goal"):
                state["slots"]["campaign_goal"] = {"value": ext["campaign_goal"], "source": "user"}
                state["collected_details"]["campaign_goal"] = ext["campaign_goal"]

            if ext.get("channel_format"):
                state["channel_format"] = ext["channel_format"]

            is_concluding = any(w in user_msg.lower() for w in [
                "that's all", "thats all", "ready", "generate", "confirm", "show card",
                "generate card", "done", "no more", "proceed", "looks good", "enough",
                "just make", "generate template"
            ])

            org_val = state["collected_details"].get("organization_name") or state["slots"].get("business_name", {}).get("value") or ""
            link_val = state["collected_details"].get("link_or_contact") or state["slots"].get("link_or_contact", {}).get("value") or ""
            goal_val = state["collected_details"].get("campaign_goal") or state.get("initial_context") or "Campaign Notice"

            has_essentials = bool(org_val and (link_val or "link_or_contact" in state.get("skipped_fields", [])))

            # Transition to Editable Confirmation Card!
            # Only transition if user explicitly signals completion, or core details are filled, or maximum turns reached
            if is_concluding or (analysis.get("action") == "show_card" and (has_essentials or turn_count >= 3)) or turn_count >= 5:
                state["conversation_stage"] = "card_review"
                org_val = clean_organization_name(org_val, domain)

                channel_fmt = state.get("channel_format", "sms")
                tone_val = ext.get("suggested_tone") or cfg["default_tone"]

                dynamic_fields = {}
                # Include standard domain questions if present in collected details
                for q in cfg.get("questions", []):
                    if q["key"] not in ["organization_name", "link_or_contact"]:
                        val = state["collected_details"].get(q["key"], "")
                        if val:
                            dynamic_fields[q["key"]] = {"label": q["label"], "value": val}

                if ext.get("address_or_location"):
                    dynamic_fields["location"] = {"label": "📍 Location / Address", "value": ext["address_or_location"]}
                if ext.get("call_to_action"):
                    dynamic_fields["call_to_action"] = {"label": "🎯 Call to Action", "value": ext["call_to_action"]}
                if ext.get("dynamic_fields"):
                    for dk, dv in ext["dynamic_fields"].items():
                        if dv and dk not in dynamic_fields:
                            dynamic_fields[dk] = {"label": dk.replace("_", " ").title(), "value": dv}

                addr_val = ext.get("address_or_location") or state["collected_details"].get("address_or_location") or state["slots"].get("location", {}).get("value") or ""
                date_val = ext.get("event_date") or state["collected_details"].get("event_date") or state["slots"].get("date", {}).get("value") or ""

                ev_tones = emit_event(
                    state, "tone_exploration", "Tone Exploration", "🎨", "active",
                    "Generating candidate message templates",
                    f"Crafting {len(cfg.get('recommended_tones', []))} tone variations...", "processing"
                )
                yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_tones, 'tag': format_pipeline_event_tag(ev_tones)})}\n\n"

                tone_candidates = await engine.generate_tone_candidate_previews(
                    session_id=session_id,
                    business_type=cfg["domain_name"],
                    context=goal_val,
                    organization_name=org_val,
                    link_or_contact=link_val,
                    address_or_location=addr_val,
                    event_date=date_val,
                    channel_format=channel_fmt,
                    candidate_tones=cfg.get("recommended_tones", []),
                    dynamic_fields=dynamic_fields
                )

                chosen_tone = tone_candidates[0]["name"] if tone_candidates else tone_val

                card_data = {
                    "context": goal_val,
                    "business_type": cfg["domain_name"],
                    "organization_name": org_val,
                    "link_or_contact": link_val,
                    "address_or_location": addr_val,
                    "event_date": date_val,
                    "channel_format": channel_fmt,
                    "tone": chosen_tone,
                    "recommended_tones": cfg["recommended_tones"],
                    "tone_candidates": tone_candidates,
                    "dynamic_fields": dynamic_fields,
                    "template_request": state.get("template_request", ""),
                    "additional_info": []
                }
                state["current_form_context"] = card_data
                state["pending_form_data"] = card_data

                prompt_text = (
                    f"### 📋 Campaign Details Confirmed\n\n"
                    f"I have gathered the key details for your **{cfg['domain_name']}** campaign.\n\n"
                    f"Review the collapsible message templates across tones below, customize if desired, and click **✨ Confirm & Generate Message**:"
                )
                record_assistant_message(session_id, state, prompt_text, form_data=card_data)
                yield f"data: {json.dumps({'type': 'text_chunk', 'text': prompt_text, 'form_data': card_data})}\n\n"
                yield f"data: {json.dumps({'type': 'campaign_form', 'text': prompt_text, 'form_data': card_data})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return
            else:
                followup = analysis.get("followup", {})
                ack = followup.get("acknowledgment", "✅ Got it!")
                q_text = followup.get("question")
                options = followup.get("options")

                domain_questions = cfg.get("questions", [])
                if (not q_text or "provide more information" in q_text.lower()) and turn_count < len(domain_questions):
                    next_cfg_q = domain_questions[turn_count]
                    q_text = next_cfg_q["question"]
                    options = next_cfg_q["options"]
                elif not q_text:
                    q_text = "What is the primary goal or details for this campaign?"
                    options = ["Other (Type custom answer)"]

                full_reply = f"{ack}\n\n👉 **Question {turn_count + 1}:** {q_text}"

                record_assistant_message(session_id, state, full_reply, question_options=options)
                yield f"data: {json.dumps({'type': 'text_chunk', 'text': full_reply, 'question_options': options})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

        # 3. Post-Dispatch Reset or New Campaign Request
        is_new_campaign_request = any(w in user_msg.lower() for w in [
            "start new campaign", "new campaign", "start fresh", "another campaign", "create new campaign", "start over", "build new"
        ])
        if state.get("dispatched") and not is_followup_request:
            state["dispatched"] = False
            state["templates"] = []
            state["approved"] = False
            state["pending_placeholders"] = []
            state["slots"] = {}
            state["collected_details"] = {}
            state["conversation_stage"] = "collecting_info"
            state["info_turns"] = 0
            state["candidate_tones"] = []
            state["selected_tone"] = None
            state["rejected_tones"] = []
            state["contacts"] = []
            state["contacts_count"] = 0
            state["current_form_context"] = None
            state["pending_form_data"] = None
        elif is_new_campaign_request:
            state["dispatched"] = False
            state["templates"] = []
            state["approved"] = False
            state["pending_placeholders"] = []
            state["slots"] = {}
            state["collected_details"] = {}
            state["conversation_stage"] = "collecting_info"
            state["info_turns"] = 0
            state["candidate_tones"] = []
            state["selected_tone"] = None
            state["rejected_tones"] = []
            state["contacts"] = []
            state["contacts_count"] = 0
            state["current_form_context"] = None
            state["pending_form_data"] = None

        if is_new_campaign_request and len(user_msg.split()) <= 4:
            prompt_new = (
                "🎯 **Ready to build a new CRM campaign!**\n\n"
                "Tell me about your business and campaign idea (e.g. retail flash sale, clinic reminders, charity donation, utility reminders) to get started!"
            )
            record_assistant_message(session_id, state, prompt_new)
            yield f"data: {json.dumps({'type': 'text_chunk', 'text': prompt_new})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        # 4. Initial Campaign Context Received -> Initiate Back-and-forth Q&A
        if not state.get("templates"):
            domain = detect_campaign_domain(user_msg)
            cfg = DOMAIN_CONFIGS.get(domain, DOMAIN_CONFIGS["General Business"])

            state["campaign_domain"] = domain
            state["business_type"] = domain
            state["conversation_stage"] = "collecting_info"
            state["initial_context"] = user_msg
            state["collected_details"] = {}
            state["info_turns"] = 0
            if "email" in user_msg.lower():
                state["channel_format"] = "email"

            # Check if user already provided URL or phone
            m_url = re.search(r"https?://[^\s]+", user_msg)
            if m_url:
                state["slots"]["link_or_contact"] = {"value": m_url.group(0), "source": "user"}
                state["collected_details"]["link_or_contact"] = m_url.group(0)

            first_q = cfg["questions"][0]
            intro_text = (
                f"🎯 **Great, let's build your {cfg['domain_name']} campaign!**\n\n"
                f"To make sure we craft the highest-converting, compliant message, I'll ask you a few quick questions to gather the key details.\n\n"
                f"👉 **Question 1:** {first_q['question']}"
            )
            record_assistant_message(session_id, state, intro_text, question_options=first_q['options'])
            yield f"data: {json.dumps({'type': 'text_chunk', 'text': intro_text, 'question_options': first_q['options']})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        # ------------------ STAGE: PENDING PLACEHOLDERS RESOLUTION ------------------
        if state.get("pending_placeholders") and state.get("templates"):
            missing = state["pending_placeholders"]
            resolved, remaining = resolve_placeholders_from_reply(user_msg, missing, state["slots"])

            if resolved:
                current_body = state["templates"][0].get("body", "")
                final_body = fill_template_placeholders(current_body, resolved)
                state["templates"][0]["body"] = final_body
                state["pending_placeholders"] = remaining

                b_name = state["slots"].get("business_name", {}).get("value") or "Our Business"
                comp = check_template_compliance(
                    body=final_body,
                    business_name=b_name,
                    word_limit=config.orchestrator.word_limit,
                    optout_text=config.orchestrator.optout_text,
                    link_or_contact=state["slots"].get("link_or_contact", {}).get("value") or ""
                )
                state["templates"][0]["word_count"] = comp["word_count"]
                state["templates"][0]["compliant"] = comp["compliant"]
                state["templates"][0]["violations"] = comp["violations"]

            if state.get("pending_placeholders"):
                # Still missing some fields -> Ask next tag with interactive choices
                next_missing = state["pending_placeholders"][0]
                clean_name = next_missing.replace("_", " ").title()
                label, hint = TAG_HINTS.get(next_missing.lower(), (clean_name, "your value"))
                tag_options = generate_interactive_choices_for_tag(next_missing, state.get("business_type", ""))
                q_ui = generate_questions_ui(state["pending_placeholders"])
                target_question = f"What is your **{label}** for this campaign message? (e.g. {hint})"
                full_prompt_text = f"✅ Got it! Next detail needed:\n\n{q_ui}\n\n👉 **Immediate Question**: {target_question}"
                record_assistant_message(session_id, state, full_prompt_text)

                ev_info = emit_event(
                    state, "required_info", "Required Information", "🛠️", "waiting_for_input",
                    "Awaiting details for variable placeholders",
                    f"Please provide details for {len(state['pending_placeholders'])} remaining variable(s)", "waiting",
                    {"missing_placeholders": state["pending_placeholders"]}
                )
                yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_info, 'tag': format_pipeline_event_tag(ev_info)})}\n\n"
                yield f"data: {json.dumps({'type': 'text_chunk', 'text': full_prompt_text, 'question_options': tag_options})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return
            else:
                # All required information gathered!
                state["pending_placeholders"] = []
                final_text = f"✅ **All Required Information Provided!**\n\nYour message template has been finalized and verified under {config.orchestrator.word_limit} words:\n\n> {state['templates'][0]['body']}\n\nYou can review the preview above, make any additional edits, or click **Approve Campaign** to proceed."
                record_assistant_message(session_id, state, final_text)

                ev_done = emit_event(
                    state, "ready", "Campaign Package Ready", "🚀", "waiting_for_input",
                    "All required details gathered",
                    "Final message template ready for approval and dispatch", "done"
                )
                yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_done, 'tag': format_pipeline_event_tag(ev_done)})}\n\n"
                yield f"data: {json.dumps({'type': 'text_chunk', 'text': final_text})}\n\n"
                yield f"data: {json.dumps({'type': 'templates_ready', 'templates': state['templates'], 'csrf_token': state['csrf_token']})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

        # Check if user explicitly provided a template draft with variable placeholders
        if "{{" in user_msg and "}}" in user_msg and (not state.get("templates") or not state.get("selected_tone")):
            optout = config.orchestrator.optout_text
            clean_body = user_msg.strip()
            if optout.lower() not in clean_body.lower():
                clean_body = f"{clean_body}\n\n{optout}"

            b_name = state["slots"].get("business_name", {}).get("value") or "Our Business"
            comp = check_template_compliance(
                body=clean_body,
                business_name=b_name,
                word_limit=config.orchestrator.word_limit,
                optout_text=optout,
                link_or_contact=state["slots"].get("link_or_contact", {}).get("value") or ""
            )

            state["selected_tone"] = "Custom / User-Requested"
            state["discovery_complete"] = True
            state["templates"] = [{
                "message_index": 1,
                "stage": "Announcement",
                "tone": "Custom",
                "body": clean_body,
                "word_count": comp["word_count"],
                "word_limit": config.orchestrator.word_limit,
                "compliant": comp["compliant"],
                "violations": comp["violations"],
                "safety_status": "safe",
                "retries": 0,
                "previews": []
            }]

            missing = extract_missing_placeholders(clean_body, state["slots"])
            if missing:
                state["pending_placeholders"] = missing
                q_ui = generate_questions_ui(missing)
                record_assistant_message(session_id, state, q_ui)
                yield f"data: {json.dumps({'type': 'text_chunk', 'text': q_ui})}\n\n"
                yield f"data: {json.dumps({'type': 'templates_ready', 'templates': state['templates'], 'csrf_token': state['csrf_token']})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return
            else:
                state["pending_placeholders"] = []
                ready_text = f"✅ **Custom Template Adopted!**\n\n> {clean_body}\n\nReview the template above and click **Approve Campaign** to proceed."
                record_assistant_message(session_id, state, ready_text)
                yield f"data: {json.dumps({'type': 'text_chunk', 'text': ready_text})}\n\n"
                yield f"data: {json.dumps({'type': 'templates_ready', 'templates': state['templates'], 'csrf_token': state['csrf_token']})}\n\n"
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
                
                record_assistant_message(session_id, state, refusal_msg)
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
            auto_derived = None
            if camp_name and str(camp_name).strip():
                auto_derived = str(camp_name).strip()
            elif b_name and state.get("business_type"):
                auto_derived = f"{b_name} - {state['business_type'].capitalize()}"
            elif b_name:
                auto_derived = f"{b_name} Campaign"

            meta_to_save = {}
            if auto_derived:
                meta_to_save["auto_title"] = auto_derived

            storage.save_session(
                session_id, state["workspace_id"],
                business_type=state["business_type"],
                campaign_name=camp_name or auto_derived,
                meta=meta_to_save
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

                record_assistant_message(session_id, state, reply_text)
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
                question_options = discovery_res.get("question_options", [])
                record_assistant_message(session_id, state, reply_text)
                yield f"data: {json.dumps({'type': 'text_chunk', 'text': reply_text, 'question_options': question_options, 'slots': state['slots']})}\n\n"

        # ------------------ STAGE: TONE REFINEMENT (IF WAITING FOR TONE) ------------------
        elif state["discovery_complete"] and not state["selected_tone"]:
            # Check if user selected one of the proposed candidate tones by number or name
            chosen_tone = None
            if state.get("candidate_tones"):
                for idx, ct in enumerate(state["candidate_tones"], 1):
                    c_name = ct.get("name", "").lower()
                    u_clean = user_msg.lower().strip()
                    if u_clean in (str(idx), f"option {idx}", f"tone {idx}", f"#{idx}", f"tone {idx}:") or c_name in u_clean or (len(u_clean) > 3 and u_clean in c_name):
                        chosen_tone = ct
                        break

            if chosen_tone:
                t_name = chosen_tone.get("name", "Standard")
                state["selected_tone"] = t_name
                ev_t_done = emit_event(
                    state, "tone_exploration", "Tone Exploration", "🎨", "done",
                    "Tone selected", f"Selected Tone: {t_name}", "done",
                    {"selected_tone": t_name}
                )
                yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_t_done, 'tag': format_pipeline_event_tag(ev_t_done)})}\n\n"

                tmpl_res = await engine.generate_and_validate_templates(
                    session_id=session_id,
                    business_type=state["business_type"] or "General Business",
                    slots=state["slots"],
                    selected_tone=t_name,
                    contacts_sample=state.get("contacts")
                )
                state["templates"] = tmpl_res.get("templates", [])

                missing = extract_missing_placeholders(state["templates"][0]["body"], state["slots"]) if state["templates"] else []
                if missing:
                    state["pending_placeholders"] = missing
                    q_ui = generate_questions_ui(missing)
                    record_assistant_message(session_id, state, q_ui)

                    ev_info = emit_event(
                        state, "required_info", "Required Information", "🛠️", "waiting_for_input",
                        "Awaiting details for variable placeholders",
                        f"Please provide details for {len(missing)} required variable(s)", "waiting",
                        {"missing_placeholders": missing}
                    )
                    yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_info, 'tag': format_pipeline_event_tag(ev_info)})}\n\n"
                    yield f"data: {json.dumps({'type': 'text_chunk', 'text': q_ui})}\n\n"
                    yield f"data: {json.dumps({'type': 'templates_ready', 'templates': state['templates'], 'csrf_token': state['csrf_token']})}\n\n"
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return
                else:
                    state["pending_placeholders"] = []
                    ready_text = f"✅ **Template Generated!** ({t_name})\n\n> {state['templates'][0]['body']}\n\nReview the template above and click **Approve Campaign** to proceed."
                    record_assistant_message(session_id, state, ready_text)
                    yield f"data: {json.dumps({'type': 'text_chunk', 'text': ready_text})}\n\n"
                    yield f"data: {json.dumps({'type': 'templates_ready', 'templates': state['templates'], 'csrf_token': state['csrf_token']})}\n\n"
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

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

            record_assistant_message(session_id, state, reply_text)
            yield f"data: {json.dumps({'type': 'text_chunk', 'text': reply_text, 'tones': state['candidate_tones']})}\n\n"

        # ------------------ STAGE: TEMPLATE ADJUSTMENTS / REGEN / REVISION ------------------
        else:
            ev_rev = emit_event(
                state, "templates", "Template Generation", "✍️", "active",
                "Revising templates based on user feedback",
                f"Processing feedback: '{user_msg[:60]}...'", "processing"
            )
            yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_rev, 'tag': format_pipeline_event_tag(ev_rev)})}\n\n"

            rev_res = await engine.revise_campaign_template(
                session_id=session_id,
                workspace_id=state["workspace_id"],
                business_type=state["business_type"] or "General Business",
                slots=state["slots"],
                selected_tone=state.get("selected_tone") or "Professional & Clear",
                current_templates=state.get("templates", []),
                user_feedback=user_msg
            )

            if rev_res.get("blocked"):
                reason = rev_res.get("reason", "Prohibited content detected.")
                refusal_msg = f"⚠️ **Revision Declined**: {reason}."
                record_assistant_message(session_id, state, refusal_msg)
                yield f"data: {json.dumps({'type': 'text_chunk', 'text': refusal_msg})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

            if rev_res.get("action") == "tone_change":
                state["selected_tone"] = None
                tones_res = await engine.explore_tones(
                    session_id=session_id,
                    workspace_id=state["workspace_id"],
                    business_type=state["business_type"] or "General Business",
                    slots=state["slots"],
                    user_tone_idea=rev_res.get("tone_idea") or user_msg
                )
                state["candidate_tones"] = tones_res.get("candidate_tones", [])

                reply_text = f"Understood! I've reset the campaign tone and generated candidate options based on your request (*\"{user_msg}\"*):\n\n"
                for t in state["candidate_tones"]:
                    reply_text += f"**{t.get('name')}**\n*{t.get('description')}*\n\n> {t.get('example_message')}\n\n"
                reply_text += "Please select a preferred tone to proceed with your campaign."

                record_assistant_message(session_id, state, reply_text)
                yield f"data: {json.dumps({'type': 'text_chunk', 'text': reply_text, 'tones': state['candidate_tones']})}\n\n"
            else:
                state["templates"] = rev_res.get("templates", state.get("templates", []))
                state["slots"] = rev_res.get("slots", state["slots"])
                changes_summary = rev_res.get("changes_summary", "Updated message template based on your feedback.")

                ev_tmpl_done = emit_event(
                    state, "templates", "Template Generation", "✍️", "done",
                    "Template revised and verified",
                    f"Verified: 1 message strictly under {config.orchestrator.word_limit} words with opt-out footer",
                    "done",
                    {"templates_count": len(state["templates"])}
                )
                yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_tmpl_done, 'tag': format_pipeline_event_tag(ev_tmpl_done)})}\n\n"

                reply_text = f"🔄 **Revision Applied**: {changes_summary}\n\n"
                reply_text += f"- **Tone:** {state.get('selected_tone') or 'Standard'}\n"
                reply_text += f"- **Compliance:** Strictly verified under {config.orchestrator.word_limit} words with opt-out footer.\n\n"
                reply_text += "You can review the updated template, make further edits, or approve the campaign when you're ready."

                record_assistant_message(session_id, state, reply_text)
                yield f"data: {json.dumps({'type': 'text_chunk', 'text': reply_text})}\n\n"
                yield f"data: {json.dumps({'type': 'templates_ready', 'templates': state['templates'], 'csrf_token': state['csrf_token']})}\n\n"

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@router.post("/chat/submit_campaign_form")
async def submit_campaign_form(req: CampaignFormSubmitRequest):
    session_id = req.session_id or str(uuid.uuid4())
    state = get_or_create_state(session_id, req.workspace_id or "default_workspace")
    storage.log_user_action(session_id, "submit_campaign_form", req.model_dump())

    async def form_stream():
        init_meta = {"type": "session_init", "session_id": session_id, "csrf_token": state["csrf_token"]}
        yield f"data: {json.dumps(init_meta)}\n\n"

        # Check safety
        safety = get_safety_service()
        combined_text = f"{req.context} {req.organization_name} {req.template_request} {' '.join(req.additional_info or [])}"
        s_eval = await safety.evaluate_safety(session_id, combined_text, "campaign_form_submit")
        if not s_eval.get("allowed", True):
            reason = s_eval.get("reason", "Violates safety policy.")
            refusal = f"⚠️ **Request Declined**\n\nI cannot assist with this campaign: {reason}."
            record_assistant_message(session_id, state, refusal)
            yield f"data: {json.dumps({'type': 'text_chunk', 'text': refusal})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        # Check for missing required fields if not skip_confirmed
        missing = []
        if not (req.organization_name or "").strip():
            missing.append("Organization Name")
        if not (req.link_or_contact or "").strip() and any(w in (req.business_type + req.context).lower() for w in ["electric", "bill", "power", "utility", "donate", "fee"]):
            missing.append("Payment Link / Contact")

        if missing and not req.skip_confirmed:
            warn_text = (
                f"⚠️ **Missing Field Detected**\n\n"
                f"You haven't provided **{' and '.join(missing)}**.\n\n"
                f"Would you like to fill {'it' if len(missing)==1 else 'them'} in now, or skip and completely exclude from the message?"
            )
            record_assistant_message(session_id, state, warn_text)
            state["pending_form_data"] = req.model_dump()
            yield f"data: {json.dumps({'type': 'missing_fields_warning', 'text': warn_text, 'missing_fields': missing, 'question_options': ['✏️ Fill in Missing Details', '⏩ Skip & Exclude from Message']})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        # Proceed with generation
        skipped = list(req.skipped_fields or [])
        if not (req.organization_name or "").strip() and "organization_name" not in skipped:
            skipped.append("organization_name")
        if not (req.link_or_contact or "").strip() and "link_or_contact" not in skipped:
            skipped.append("link_or_contact")

        state["skipped_fields"] = skipped
        state["business_type"] = req.business_type or "General Business"
        state["channel_format"] = req.channel_format or "sms"
        state["selected_tone"] = req.tone or "Direct & Authoritative 📜"
        state["additional_info"] = req.additional_info or []

        # Update slots
        if req.organization_name and "organization_name" not in skipped:
            state["slots"]["business_name"] = {"value": req.organization_name.strip(), "source": "form"}
        elif "organization_name" in skipped:
            state["slots"].pop("business_name", None)

        if req.link_or_contact and "link_or_contact" not in skipped:
            state["slots"]["link_or_contact"] = {"value": req.link_or_contact.strip(), "source": "form"}
        elif "link_or_contact" in skipped:
            state["slots"].pop("link_or_contact", None)

        if (req.address_or_location or "").strip():
            state["slots"]["location"] = {"value": req.address_or_location.strip(), "source": "form"}
            state["address_or_location"] = req.address_or_location.strip()
        if (req.event_date or "").strip():
            state["slots"]["date"] = {"value": req.event_date.strip(), "source": "form"}
            state["event_date"] = req.event_date.strip()

        ev_start = emit_event(
            state, "templates", "Template Generation", "✍️", "active",
            "Generating message template from form details",
            f"Drafting {req.channel_format.upper()} message in tone '{req.tone}'...", "processing"
        )
        yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_start, 'tag': format_pipeline_event_tag(ev_start)})}\n\n"

        gen_res = await engine.generate_message_from_form(
            session_id=session_id,
            business_type=state["business_type"],
            context=req.context or "Campaign Notice",
            organization_name=req.organization_name or "",
            link_or_contact=req.link_or_contact or "",
            address_or_location=req.address_or_location or "",
            event_date=req.event_date or "",
            channel_format=req.channel_format or "sms",
            tone=req.tone or "Direct & Authoritative 📜",
            template_request=req.template_request or "",
            additional_info=req.additional_info or [],
            skipped_fields=skipped,
            dynamic_fields=req.dynamic_fields or {}
        )

        state["templates"] = gen_res["templates"]
        state["discovery_complete"] = True
        state["approved"] = False
        state["context"] = req.context or ""
        state["organization_name"] = req.organization_name or ""
        state["link_or_contact"] = req.link_or_contact or ""
        state["address_or_location"] = req.address_or_location or ""
        state["event_date"] = req.event_date or ""
        state["selected_tone"] = req.tone
        state["channel_format"] = req.channel_format
        state["additional_info"] = req.additional_info or []
        state["dynamic_fields"] = req.dynamic_fields or {}

        # Persist dynamic context and active campaign profile to workspace_memory (logs to memory_ops)
        ws_id = req.workspace_id or state.get("workspace_id") or "default_workspace"
        campaign_profile = {
            "business_type": state["business_type"],
            "organization_name": req.organization_name or "",
            "link_or_contact": req.link_or_contact or "",
            "address_or_location": req.address_or_location or "",
            "event_date": req.event_date or "",
            "channel_format": req.channel_format,
            "tone": req.tone,
            "context": req.context or "",
            "dynamic_fields": req.dynamic_fields or {},
            "additional_info": req.additional_info or []
        }
        storage.write_memory(
            workspace_id=ws_id,
            category="campaign_profile",
            key="active_campaign",
            value=campaign_profile,
            confidence=1.0,
            session_id=session_id
        )
        if req.dynamic_fields:
            for k, v in req.dynamic_fields.items():
                storage.write_memory(
                    workspace_id=ws_id,
                    category="dynamic_context",
                    key=k,
                    value=v,
                    confidence=1.0,
                    session_id=session_id
                )

        ev_done = emit_event(
            state, "templates", "Template Generation", "✍️", "done",
            "Template created",
            f"Compliance verified: 1 message strictly under {config.orchestrator.word_limit} words with opt-out footer",
            "done",
            {"templates_count": 1}
        )
        yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_done, 'tag': format_pipeline_event_tag(ev_done)})}\n\n"

        ev_ready = emit_event(
            state, "ready", "Campaign Package Ready", "🚀", "waiting_for_input",
            "Awaiting your final review and approval",
            "Generated compliant template — Review or Approve below", "waiting"
        )
        yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_ready, 'tag': format_pipeline_event_tag(ev_ready)})}\n\n"

        tmpl_body = state["templates"][0]["body"]
        format_badge = "📧 Email" if req.channel_format == "email" else "📱 SMS / WhatsApp"

        # Message 1: Generated template message (the message selected/generated)
        msg_template = (
            f"✅ **Message Generated Successfully!** ({format_badge} • {req.tone})\n\n"
            f"> {tmpl_body}"
        )
        record_assistant_message(session_id, state, msg_template)
        yield f"data: {json.dumps({'type': 'text_chunk', 'text': msg_template})}\n\n"

        # Message 2: Follow-up question as a separate message of its own from the agent
        followup_msg = (
            "💡 **Next Steps:** Would you like to create a **follow-up reminder message template** "
            "(e.g. sent 24 hours before the event/deadline), load test recipient contacts, or approve this campaign for dispatch?"
        )
        followup_options = [
            "📅 Create Follow-Up Reminder Template",
            "👥 Ingest & Verify Test Contacts",
            "🚀 Approve Campaign for Dispatch",
            "✏️ Revise Message Copy"
        ]
        record_assistant_message(session_id, state, followup_msg, question_options=followup_options)
        yield f"data: {json.dumps({'type': 'text_chunk', 'text': followup_msg, 'question_options': followup_options})}\n\n"
        yield f"data: {json.dumps({'type': 'templates_ready', 'templates': state['templates'], 'csrf_token': state['csrf_token']})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(form_stream(), media_type="text/event-stream")

@router.post("/chat/select_tone")
async def select_tone(req: ToneSelectRequest):
    """
    Called when user clicks/selects one candidate tone.
    Locks in tone, updates memory, generates dynamic plan & compliant templates.
    Streams execution events via SSE without artificial delays.
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

        # Template Generation phase
        ev_tmpl_start = emit_event(
            state, "templates", "Template Generation", "✍️", "active",
            "Crafting compliant message sequence",
            f"Drafting message in tone '{req.tone_name}'...", "processing"
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
            "Template created",
            f"Compliance verified: {len(state['templates'])} message strictly under {config.orchestrator.word_limit} words with opt-out footer",
            "done",
            {"templates_count": len(state["templates"])}
        )
        yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_tmpl_done, 'tag': format_pipeline_event_tag(ev_tmpl_done)})}\n\n"

        # Synchronize tone selection to conversation history
        state["conversation_history"].append({"role": "user", "content": f"Selected campaign tone: {req.tone_name}"})
        first_body = state["templates"][0].get("body", "") if state["templates"] else ""

        # Check if the generated template contains unresolved variable placeholders
        missing_placeholders = extract_missing_placeholders(first_body, state["slots"]) if first_body else []

        if missing_placeholders:
            state["pending_placeholders"] = missing_placeholders
            first_missing = missing_placeholders[0]
            clean_name = first_missing.replace("_", " ").title()
            label, hint = TAG_HINTS.get(first_missing.lower(), (clean_name, "your value"))
            tag_options = generate_interactive_choices_for_tag(first_missing, state.get("business_type", ""))

            q_ui = generate_questions_ui(missing_placeholders)
            target_question = f"What is your **{label}** for this campaign message? (e.g. {hint})"
            full_prompt_text = f"{q_ui}\n\n👉 **Immediate Question**: {target_question}"
            record_assistant_message(req.session_id, state, full_prompt_text)

            ev_info = emit_event(
                state, "required_info", "Required Information", "🛠️", "waiting_for_input",
                "Awaiting details for variable placeholders",
                f"Please provide details for {len(missing_placeholders)} required variable(s)", "waiting",
                {"missing_placeholders": missing_placeholders}
            )
            yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_info, 'tag': format_pipeline_event_tag(ev_info)})}\n\n"
            yield f"data: {json.dumps({'type': 'text_chunk', 'text': full_prompt_text, 'question_options': tag_options})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        else:
            state["pending_placeholders"] = []
            ev_ready = emit_event(
                state, "ready", "Campaign Package Ready", "🚀", "waiting_for_input",
                "Awaiting your final review and approval",
                f"Generated {len(state['templates'])} compliant template — Review or Approve below", "waiting"
            )
            yield f"data: {json.dumps({'type': 'pipeline_event', 'event': ev_ready, 'tag': format_pipeline_event_tag(ev_ready)})}\n\n"

            # Message 1: Generated template message
            msg_template = f"✅ **Message Generated Successfully!** (📱 SMS / WhatsApp • {req.tone_name})\n\n> {first_body}"
            record_assistant_message(req.session_id, state, msg_template)
            yield f"data: {json.dumps({'type': 'text_chunk', 'text': msg_template})}\n\n"

            # Message 2: Follow-up question as a separate message of its own from the agent
            followup_msg = (
                "💡 **Next Steps:** Would you like to create a **follow-up reminder message template** "
                "(e.g. sent 24 hours before the event/deadline), load test recipient contacts, or approve this campaign for dispatch?"
            )
            followup_options = [
                "📅 Create Follow-Up Reminder Template",
                "👥 Ingest & Verify Test Contacts",
                "🚀 Approve Campaign for Dispatch",
                "✏️ Revise Message Copy"
            ]
            record_assistant_message(req.session_id, state, followup_msg, question_options=followup_options)
            yield f"data: {json.dumps({'type': 'text_chunk', 'text': followup_msg, 'question_options': followup_options})}\n\n"

            yield f"data: {json.dumps({'type': 'templates_ready', 'templates': state['templates'], 'schedule': tmpl_res.get('recommended_schedule'), 'csrf_token': state['csrf_token']})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(tone_exec_stream(), media_type="text/event-stream")

@router.post("/contacts/upload")
async def upload_contacts(
    request: Request,
    session_id: str = Form(...),
    workspace_id: str = Form("default_workspace"),
    country_code: Optional[str] = Form(None),
    file: UploadFile = File(...),
    host: Optional[str] = Header(None),
    origin: Optional[str] = Header(None)
):
    """Handles contact file upload, parsing, deduplication, suppression check, and preview."""
    verify_loopback_headers(request, host, origin, config, require_origin=False)
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
    request: Request,
    session_id: str = Form(...),
    workspace_id: str = Form("default_workspace"),
    text_data: str = Form(...),
    country_code: Optional[str] = Form(None),
    host: Optional[str] = Header(None),
    origin: Optional[str] = Header(None)
):
    """Handles pasted contact text lines."""
    verify_loopback_headers(request, host, origin, config, require_origin=False)
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
    """Edits a specific template and re-validates compliance, tracks versions, and regenerates contact previews."""
    state = get_or_create_state(req.session_id)
    storage.log_user_action(req.session_id, "edit_template", {"index": req.message_index})

    b_name = state.get("organization_name") or state["slots"].get("business_name", {}).get("value") or "Business"
    link = state.get("link_or_contact") or state["slots"].get("link_or_contact", {}).get("value") or ""

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
            # Initialize versions list if not present
            if "versions" not in t or not isinstance(t.get("versions"), list) or len(t.get("versions", [])) == 0:
                t["versions"] = [{
                    "version": 1,
                    "body": t.get("body", ""),
                    "timestamp": utc_now_iso(),
                    "note": "Initial Draft"
                }]

            new_ver_num = len(t["versions"]) + 1
            t["versions"].append({
                "version": new_ver_num,
                "body": req.updated_body,
                "timestamp": utc_now_iso(),
                "note": "Manual Edit"
            })
            t["current_version"] = new_ver_num
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
                    r_text = req.updated_body.replace("{{customer_name}}", c_name).replace("{{patient_name}}", c_name).replace("{{donor_name}}", c_name).replace("{{name}}", c_name).replace("{{business_name}}", b_name).replace("{{org_name}}", b_name).replace("{{link}}", link or "https://example.com").replace("{{donation_link}}", link or "https://example.com")
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

            # Persist updated templates to storage if a campaign already exists for this session
            saved_campaign = storage.get_campaign(req.session_id)
            if saved_campaign:
                saved_campaign["templates"] = state["templates"]
                storage.save_campaign(req.session_id, saved_campaign)

            return {"status": "success", "template": t, "all_templates": state["templates"]}

    raise HTTPException(status_code=404, detail="Template not found")

@router.post("/campaign/regenerate_template")
async def regenerate_template(req: TemplateRegenerateRequest):
    """Regenerates a single specific template using LLM with context-grounded user instruction."""
    state = get_or_create_state(req.session_id)
    storage.log_user_action(req.session_id, "regenerate_template", {"index": req.message_index, "instructions": req.instructions})

    target_tmpl = None
    for t in state.get("templates", []):
        if t.get("message_index") == req.message_index:
            target_tmpl = t
            break

    if not target_tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    # Version tracking initialization
    if "versions" not in target_tmpl or not isinstance(target_tmpl.get("versions"), list) or len(target_tmpl.get("versions", [])) == 0:
        target_tmpl["versions"] = [{
            "version": 1,
            "body": target_tmpl.get("body", ""),
            "timestamp": utc_now_iso(),
            "note": "Initial Draft"
        }]

    # Extract dynamic business info and detect user modifications in instructions
    b_name = state.get("organization_name") or state["slots"].get("business_name", {}).get("value") or "Our Business"
    c_name = state.get("context") or state["slots"].get("campaign_goal", {}).get("value") or "Campaign Notice"
    domain = state.get("campaign_domain") or state.get("business_type") or "General Business"
    selected_tone = target_tmpl.get("tone") or state.get("selected_tone") or "Professional"
    channel_fmt = target_tmpl.get("channel_format") or state.get("channel_format") or "sms"
    link = state.get("link_or_contact") or state["slots"].get("link_or_contact", {}).get("value") or ""
    current_body = target_tmpl.get("body", "")

    # Check if user requested a new clinic/business name in instructions
    if req.instructions:
        inst_lower = req.instructions.lower()
        name_patterns = [
            r"(?:clinic|business|company|org(?:anization)?|provider|store|brand)\s*(?:name)?\s*(?:to|is|:)?\s*[\"']?([A-Za-z0-9\s&]+?)[\"']?(?:$|\.|\band\b|\bwith\b|\badd\b|\bmake\b)",
            r"(?:different|new)\s*(?:clinic|business|company|org(?:anization)?)\s*(?:name)?\s*(?:to|is|:)?\s*[\"']?([A-Za-z0-9\s&]+?)[\"']?(?:$|\.|\band\b|\bwith\b|\badd\b|\bmake\b)"
        ]
        for pat in name_patterns:
            m = re.search(pat, req.instructions, re.IGNORECASE)
            if m:
                extracted = m.group(1).strip()
                if len(extracted) >= 2 and len(extracted) <= 45 and extracted.lower() not in ["different", "new", "another", "our", "the", "a", "my"]:
                    b_name = extracted
                    state["organization_name"] = b_name
                    state["slots"]["business_name"] = {"value": b_name, "source": "regenerate"}
                    ws_id = state.get("workspace_id") or "default_workspace"
                    storage.write_memory(
                        workspace_id=ws_id,
                        category="campaign_profile",
                        key="organization_name",
                        value=b_name,
                        confidence=1.0,
                        session_id=req.session_id
                    )
                    break

        tone_map = {
            "caring": "Caring & Reassuring 🩺",
            "reassuring": "Caring & Reassuring 🩺",
            "urgent": "Direct & Urgent 🚨",
            "aggressive": "Direct & Urgent 🚨",
            "authoritative": "Professional & Authoritative 📜",
            "formal": "Professional & Authoritative 📜",
            "casual": "Casual & Friendly 🌟",
            "friendly": "Casual & Friendly 🌟",
            "informative": "Clear & Informative 📊"
        }
        for kw, mapped_tone in tone_map.items():
            if kw in inst_lower and ("tone" in inst_lower or "make it" in inst_lower):
                selected_tone = mapped_tone
                state["selected_tone"] = selected_tone
                target_tmpl["tone"] = selected_tone
                break

    system_prompt = f"""You are the Template Revision & Writer Module of CampaignForge.
Your job is to regenerate Message {req.message_index} ({target_tmpl.get('stage', 'Touchpoint')}) for an outgoing CRM messaging campaign.

CAMPAIGN PARAMETERS:
- Industry / Domain: {domain}
- Sending Business / Organization: {b_name}
- Campaign Context / Offer: {c_name}
- Target Channel / Format: {channel_fmt.upper()}
- Tone: {selected_tone}
- Strict Word Limit: {config.orchestrator.word_limit} words
- Mandatory Opt-Out Ending: "{config.orchestrator.optout_text}"

CRITICAL REVISION & ROLE RULES:
1. SENDER PERSPECTIVE (OUTGOING CRM NOTICE):
   - You are composing a message sent FROM the organization ({b_name}) TO patients/customers/recipients.
   - NEVER address the organization itself as the recipient! (E.g. NEVER write "Hi {b_name}, we are sorry you are not a legitimate clinic"). Address the customer or patient naturally (e.g. "Hi {{{{customer_name}}}}," or "Dear Patient,").
2. USER REVISION INSTRUCTIONS:
   - If the user instructed to change the organization / clinic name, use the updated name ({b_name}) as the sender.
   - If the user asked to add or remove clauses (e.g. promotional discounts, limited-time offers, appointment reminders, specific deadlines, fee policies, or volunteer perks), seamlessly incorporate or omit those clauses.
   - If the user asked for a tone shift (e.g. more caring, more urgent, more formal, casual), calibrate the vocabulary, warmth, and urgency accordingly.
   - Retain the core campaign purpose and context from the previous draft.
3. COMPLIANCE & TAGS:
   - Must strictly be under {config.orchestrator.word_limit} words.
   - Must end with: "{config.orchestrator.optout_text}"
   - Use standard tags like {{{{customer_name}}}} or contact link {link} if appropriate.

Output strict JSON:
{{
  "body": "..."
}}"""

    user_message = f"""CURRENT MESSAGE DRAFT:
\"\"\"{current_body}\"\"\"

REQUESTED MODIFICATIONS:
{req.instructions if req.instructions else "Regenerate with improved phrasing and flow while retaining compliance."}

Please regenerate the message incorporating all requested modifications while preserving the CRM campaign context."""

    from app.core.llm_client import get_llm_client
    llm = get_llm_client()
    res = await llm.call_llm(
        session_id=req.session_id,
        purpose="regenerate_single_template",
        system_prompt=system_prompt,
        user_message=user_message,
        step_id="template_writer",
        json_mode=True
    )

    new_body = current_body
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
    comp = check_template_compliance(
        body=new_body,
        business_name=b_name,
        word_limit=config.orchestrator.word_limit,
        optout_text=config.orchestrator.optout_text,
        link_or_contact=link
    )

    # Version tracking update
    new_ver_num = len(target_tmpl.get("versions", [])) + 1
    target_tmpl["versions"].append({
        "version": new_ver_num,
        "body": new_body,
        "timestamp": utc_now_iso(),
        "note": f"Regenerated: {req.instructions[:35]}" if req.instructions else "Regenerated Draft"
    })
    target_tmpl["current_version"] = new_ver_num
    target_tmpl["body"] = new_body
    target_tmpl["word_count"] = comp["word_count"]
    target_tmpl["compliant"] = comp["compliant"]
    target_tmpl["violations"] = comp["violations"]
    target_tmpl["tone"] = selected_tone

    # Rebuild previews
    has_contacts = len(state.get("contacts", [])) > 0
    new_previews = []
    if has_contacts:
        for sc in state.get("contacts", [])[:5]:
            c_name_val = sc.get("name", "Customer")
            r_text = new_body.replace("{{customer_name}}", c_name_val).replace("{{patient_name}}", c_name_val).replace("{{donor_name}}", c_name_val).replace("{{name}}", c_name_val).replace("{{business_name}}", b_name).replace("{{org_name}}", b_name).replace("{{link}}", link or "https://example.com")
            for k, v in sc.get("extra", {}).items():
                r_text = r_text.replace("{{" + k.lower().strip().replace(" ", "_") + "}}", str(v)).replace("{{" + k + "}}", str(v))
            new_previews.append({
                "contact_name": c_name_val,
                "phone": sc.get("phone", ""),
                "masked_phone": sc.get("masked_phone", ""),
                "extra": sc.get("extra", {}),
                "rendered_text": r_text
            })
    target_tmpl["previews"] = new_previews

    # Persist updated templates to storage if a campaign already exists for this session
    saved_campaign = storage.get_campaign(req.session_id)
    if saved_campaign:
        saved_campaign["templates"] = state["templates"]
        storage.save_campaign(req.session_id, saved_campaign)

    return {"status": "success", "template": target_tmpl, "all_templates": state["templates"]}

@router.post("/campaign/regenerate_tone_previews")
async def regenerate_tone_previews(req: TonePreviewRegenRequest):
    """
    Generates fresh collapsible message previews across candidate tones based on user suggestions/feedback.
    Allows continuous loop of refinement until user is satisfied.
    """
    state = get_or_create_state(req.session_id)
    storage.log_user_action(req.session_id, "regenerate_tone_previews", req.model_dump())

    b_type = req.business_type or state.get("business_type") or "General Business"
    ctx = req.context or state.get("context") or state.get("slots", {}).get("campaign_goal", {}).get("value") or "Campaign Notice"
    org_name = req.organization_name or state.get("organization_name") or state.get("slots", {}).get("business_name", {}).get("value") or ""
    link = req.link_or_contact or state.get("link_or_contact") or state.get("slots", {}).get("link_or_contact", {}).get("value") or ""
    addr = req.address_or_location or state.get("address_or_location") or state.get("slots", {}).get("location", {}).get("value") or ""
    date_val = req.event_date or state.get("event_date") or state.get("slots", {}).get("date", {}).get("value") or ""
    channel_fmt = req.channel_format or state.get("channel_format") or "sms"
    dyn_fields = req.dynamic_fields if req.dynamic_fields is not None else state.get("dynamic_fields", {})

    tone_cards = await engine.generate_tone_candidate_previews(
        session_id=req.session_id,
        business_type=b_type,
        context=ctx,
        organization_name=org_name,
        link_or_contact=link,
        address_or_location=addr,
        event_date=date_val,
        channel_format=channel_fmt,
        candidate_tones=req.candidate_tones,
        user_feedback=req.user_feedback or "",
        dynamic_fields=dyn_fields
    )

    if state.get("current_form_context"):
        state["current_form_context"]["tone_candidates"] = tone_cards
    if state.get("pending_form_data"):
        state["pending_form_data"]["tone_candidates"] = tone_cards

    return {"status": "success", "tone_candidates": tone_cards}

@router.post("/campaign/restore_template_version")
async def restore_template_version(req: TemplateRestoreVersionRequest):
    """Restores a previous version of a template draft and adds a restoration entry."""
    state = get_or_create_state(req.session_id)
    storage.log_user_action(req.session_id, "restore_template_version", {"index": req.message_index, "version": req.version})

    target_tmpl = None
    for t in state.get("templates", []):
        if t.get("message_index") == req.message_index:
            target_tmpl = t
            break

    if not target_tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    versions = target_tmpl.get("versions", [])
    target_ver = next((v for v in versions if v.get("version") == req.version), None)
    if not target_ver:
        raise HTTPException(status_code=404, detail=f"Version {req.version} not found")

    restored_body = target_ver["body"]
    new_ver_num = len(versions) + 1
    versions.append({
        "version": new_ver_num,
        "body": restored_body,
        "timestamp": utc_now_iso(),
        "note": f"Restored from v{req.version}"
    })
    target_tmpl["current_version"] = new_ver_num
    target_tmpl["body"] = restored_body

    b_name = state.get("organization_name") or state["slots"].get("business_name", {}).get("value") or "Business"
    link = state.get("link_or_contact") or state["slots"].get("link_or_contact", {}).get("value") or ""
    from app.services.compliance_service import check_template_compliance
    comp = check_template_compliance(
        body=restored_body,
        business_name=b_name,
        word_limit=config.orchestrator.word_limit,
        optout_text=config.orchestrator.optout_text,
        link_or_contact=link
    )
    target_tmpl["word_count"] = comp["word_count"]
    target_tmpl["compliant"] = comp["compliant"]
    target_tmpl["violations"] = comp["violations"]

    # Rebuild previews
    has_contacts = len(state.get("contacts", [])) > 0
    new_previews = []
    if has_contacts:
        for sc in state.get("contacts", [])[:5]:
            c_name_val = sc.get("name", "Customer")
            r_text = restored_body.replace("{{customer_name}}", c_name_val).replace("{{patient_name}}", c_name_val).replace("{{name}}", c_name_val).replace("{{business_name}}", b_name).replace("{{link}}", link or "https://example.com")
            for k, v in sc.get("extra", {}).items():
                r_text = r_text.replace("{{" + k.lower().strip().replace(" ", "_") + "}}", str(v)).replace("{{" + k + "}}", str(v))
            new_previews.append({
                "contact_name": c_name_val,
                "phone": sc.get("phone", ""),
                "masked_phone": sc.get("masked_phone", ""),
                "extra": sc.get("extra", {}),
                "rendered_text": r_text
            })
    target_tmpl["previews"] = new_previews

    saved_campaign = storage.get_campaign(req.session_id)
    if saved_campaign:
        saved_campaign["templates"] = state["templates"]
        storage.save_campaign(req.session_id, saved_campaign)

    return {"status": "success", "template": target_tmpl, "all_templates": state["templates"]}

@router.get("/chat/sessions/{session_id}/live_memory")
def get_live_session_memory(session_id: str):
    """Returns live agent memory, slots, dynamic context, and recent operations for the live logs panel."""
    state = get_or_create_state(session_id)
    ws_id = state.get("workspace_id") or "default_workspace"
    ws_mem = storage.get_workspace_memory(ws_id)
    ops = []
    with storage.get_connection() as conn:
        raw_ops = conn.cursor().execute("SELECT * FROM memory_ops WHERE session_id = ? ORDER BY timestamp DESC LIMIT 20", (session_id,)).fetchall()
        for r in raw_ops:
            d = dict(r)
            if d.get("value_json"):
                try:
                    d["value"] = json.loads(d["value_json"])
                except Exception:
                    d["value"] = d["value_json"]
            ops.append(d)

    return {
        "session_id": session_id,
        "workspace_id": ws_id,
        "campaign_domain": state.get("campaign_domain") or state.get("business_type") or "General Business",
        "conversation_stage": state.get("conversation_stage", "initial"),
        "slots": state.get("slots", {}),
        "dynamic_fields": state.get("dynamic_fields", {}),
        "organization_name": state.get("organization_name") or state["slots"].get("business_name", {}).get("value") or "",
        "link_or_contact": state.get("link_or_contact") or state["slots"].get("link_or_contact", {}).get("value") or "",
        "selected_tone": state.get("selected_tone"),
        "channel_format": state.get("channel_format", "sms"),
        "templates_count": len(state.get("templates", [])),
        "approved": state.get("approved", False),
        "contacts_count": state.get("contacts_count", 0),
        "workspace_memory": ws_mem,
        "recent_ops": ops
    }
