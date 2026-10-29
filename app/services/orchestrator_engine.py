import json
import re
import asyncio
from typing import Dict, Any, List, Optional
from app.core.config import get_config
from app.core.storage import get_storage
from app.core.prompt_manager import get_prompt_manager
from app.core.llm_client import get_llm_client
from app.services.compliance_service import check_template_compliance, count_words
from app.services.safety_service import get_safety_service

def parse_json_robust(raw: str) -> Optional[Dict[str, Any]]:
    """
    Robust JSON parser handling common LLM quirks:
    - Markdown code fences (```json ... ```)
    - Trailing commas before } or ]
    - Unclosed brackets/braces
    - Outermost JSON object extraction
    """
    if not raw or not isinstance(raw, str):
        return None
    clean = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    clean = re.sub(r"\s*```$", "", clean).strip()
    if not clean:
        return None

    # Attempt 1: Direct JSON parse
    try:
        res = json.loads(clean)
        if isinstance(res, dict):
            return res
    except Exception:
        pass

    # Attempt 2: Strip trailing commas before } or ]
    candidate = re.sub(r",\s*([\}\]])", r"\1", clean)
    try:
        res = json.loads(candidate)
        if isinstance(res, dict):
            return res
    except Exception:
        pass

    # Attempt 3: Close unclosed braces/brackets
    open_curly = candidate.count("{") - candidate.count("}")
    open_square = candidate.count("[") - candidate.count("]")
    candidate_balanced = candidate + ("]" * max(0, open_square)) + ("}" * max(0, open_curly))
    try:
        res = json.loads(candidate_balanced)
        if isinstance(res, dict):
            return res
    except Exception:
        pass

    # Attempt 4: Extract the outermost {...} substring
    first_brace = clean.find("{")
    last_brace = clean.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        snippet = clean[first_brace:last_brace+1]
        try:
            res = json.loads(snippet)
            if isinstance(res, dict):
                return res
        except Exception:
            snippet_commas = re.sub(r",\s*([\}\]])", r"\1", snippet)
            try:
                res = json.loads(snippet_commas)
                if isinstance(res, dict):
                    return res
            except Exception:
                pass

    return None

def generate_quiz_options_for_question(question: str, business_type: str = "") -> List[str]:
    """Generates tailored multiple-choice options for a discovery question, with Other as the final option."""
    q_lower = (question or "").lower()
    b_lower = (business_type or "").lower()

    if any(w in q_lower for w in ["business", "organization", "name", "company", "utility", "who is sending"]):
        if any(w in b_lower for w in ["electric", "power", "utilit", "energy"]):
            return [
                "Metro Power & Light",
                "State Electricity Board",
                "City Electric Services",
                "Other (Type custom answer)"
            ]
        elif any(w in b_lower for w in ["clinic", "dental", "health", "hospital"]):
            return [
                "Bright Smile Dental Clinic",
                "Community Health Center",
                "Metro Family Care",
                "Other (Type custom answer)"
            ]
        elif any(w in b_lower for w in ["retail", "boutique", "apparel", "store", "shop"]):
            return [
                "Urban Threads Boutique",
                "Corner General Store",
                "Metro Retailers",
                "Other (Type custom answer)"
            ]
        elif any(w in b_lower for w in ["ngo", "non-profit", "charity", "foundation"]):
            return [
                "Green Earth Foundation",
                "Community Relief Network",
                "Hope Outreach Project",
                "Other (Type custom answer)"
            ]
        else:
            return [
                "Metro City Services",
                "Prime Solutions Group",
                "Regional Service Provider",
                "Other (Type custom answer)"
            ]
    elif any(w in q_lower for w in ["goal", "what is this campaign for", "purpose", "why"]):
        if any(w in b_lower for w in ["electric", "power", "utilit", "energy"]) or "bill" in q_lower:
            return [
                "Electricity Bill Payment Reminder & Disconnection Warning",
                "Routine Monthly Bill Notification",
                "Scheduled Maintenance & Outage Alert",
                "Other (Type custom answer)"
            ]
        elif any(w in b_lower for w in ["clinic", "dental", "health"]):
            return [
                "6-Month Routine Cleaning Appointment Reminder",
                "Annual Health Checkup Notification",
                "Follow-up Treatment Scheduling",
                "Other (Type custom answer)"
            ]
        elif any(w in b_lower for w in ["retail", "boutique", "apparel"]):
            return [
                "Weekend Flash Sale Announcement (30% Off)",
                "New Seasonal Arrival Showcase",
                "VIP Member Exclusive Discount",
                "Other (Type custom answer)"
            ]
        else:
            return [
                "Bill Payment & Account Notification",
                "Promotional Discount & Special Offer",
                "Service Appointment Reminder",
                "Other (Type custom answer)"
            ]
    elif any(w in q_lower for w in ["link", "url", "website", "portal", "pay online", "payment link", "phone", "contact", "customer care"]):
        if any(w in b_lower for w in ["electric", "power", "utilit", "energy"]) or "bill" in q_lower:
            return [
                "https://pay.metropower.com/bill",
                "https://quickpay.energy.gov/settle",
                "1-800-555-0199 (Customer Support)",
                "Other (Type custom answer)"
            ]
        elif any(w in b_lower for w in ["ngo", "non-profit", "charity"]):
            return [
                "https://donate.greenearth.org/give",
                "https://give.ngo/cleanwater",
                "1-800-RELIEF-NOW",
                "Other (Type custom answer)"
            ]
        else:
            return [
                "https://secure.pay.link/portal",
                "https://service.link/account",
                "1-800-555-0199",
                "Other (Type custom answer)"
            ]
    elif any(w in q_lower for w in ["target audience", "who is", "who are", "audience", "target group", "demographic"]):
        if any(w in b_lower for w in ["electric", "power", "utilit", "energy"]) or "bill" in q_lower:
            return [
                "Residential customers with unpaid/overdue bills",
                "All registered electricity account holders",
                "Commercial & industrial utility clients",
                "Other (Type custom answer)"
            ]
        elif any(w in b_lower for w in ["clinic", "dental"]):
            return [
                "Existing patients due for 6-month checkup",
                "New patients registered this month",
                "All active clinic members",
                "Other (Type custom answer)"
            ]
        else:
            return [
                "All registered customers with overdue accounts",
                "Existing loyal subscribers & members",
                "General public & prospective clients",
                "Other (Type custom answer)"
            ]
    elif any(w in q_lower for w in ["action", "deadline", "offer", "discount", "cta", "when", "how"]):
        if any(w in b_lower for w in ["electric", "power", "utilit", "energy"]) or "bill" in q_lower:
            return [
                "Pay online today to prevent power disconnection",
                "Settle balance within 24 hours at nearest office",
                "Call support helpline to arrange payment plan",
                "Other (Type custom answer)"
            ]
        elif any(w in b_lower for w in ["retail", "boutique", "apparel"]):
            return [
                "Use code FLASH30 at checkout before midnight Sunday",
                "Visit store today for 30% off clearance racks",
                "Show this SMS at cash register for instant discount",
                "Other (Type custom answer)"
            ]
        else:
            return [
                "Take immediate action online before deadline",
                "Contact customer service within 24 hours",
                "Visit website to confirm your appointment",
                "Other (Type custom answer)"
            ]
    else:
        return [
            "Confirm standard campaign details",
            "Update with urgent priority",
            "Proceed with recommended settings",
            "Other (Type custom answer)"
        ]

def normalize_discovery_output(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalizes discovery output dictionary:
    - Hoists top-level fields (discovery_complete, assumptions_made, next_question_to_user, question_options, business_type)
      if the LLM accidentally nested them inside `slots`.
    - Enforces that all required campaign tags are filled before allowing discovery_complete to be true.
    - Generates quiz-style question_options with 'Other' as the final choice whenever a question is asked.
    """
    if not isinstance(data, dict):
        return {}

    slots = data.get("slots", {})
    if not isinstance(slots, dict):
        slots = {}
        data["slots"] = slots

    # Hoist any top-level keys that the model inadvertently placed inside slots
    hoist_keys = ["discovery_complete", "assumptions_made", "next_question_to_user", "question_options", "business_type"]
    for k in hoist_keys:
        if k in slots:
            val = slots.pop(k)
            if k not in data or data[k] is None or data[k] == "":
                data[k] = val

    def get_slot_val(key: str) -> Optional[str]:
        item = slots.get(key)
        if item is None:
            return None
        if isinstance(item, dict):
            v = item.get("value")
        else:
            v = str(item)
        if v is None:
            return None
        s = str(v).strip()
        if s.lower() in ("none", "null", "", "unspecified", "tbd", "unknown", "n/a"):
            return None
        if s.startswith("{{") and s.endswith("}}"):
            return None
        return s

    b_val = get_slot_val("business_name") or get_slot_val("organization_name") or get_slot_val("company_name")
    has_b = bool(b_val and b_val.lower() not in ("our business", "business", "company", "organization", "vegetable vendor", "our company"))

    g_val = get_slot_val("campaign_goal") or get_slot_val("goal") or get_slot_val("purpose")
    has_g = bool(g_val and g_val.lower() not in ("special offer", "campaign", "crm message"))

    a_val = get_slot_val("target_audience") or get_slot_val("audience") or get_slot_val("target_recipients")
    has_a = bool(a_val and a_val.lower() not in ("customers", "all", "everyone", "people"))

    c_val = get_slot_val("offer_or_cta") or get_slot_val("call_to_action") or get_slot_val("action_or_offer") or get_slot_val("action_deadline") or get_slot_val("cta")
    has_c = bool(c_val and c_val.lower() not in ("cta", "action"))

    l_val = get_slot_val("link_or_contact") or get_slot_val("payment_link") or get_slot_val("link") or get_slot_val("contact")
    has_l = bool(l_val and l_val.lower() not in ("link", "contact", "http://example.com", "https://example.com"))

    is_complete = data.get("discovery_complete")
    next_q = (data.get("next_question_to_user") or "").strip()

    assumptions = data.get("assumptions_made") or []
    has_assumptions = isinstance(assumptions, list) and len(assumptions) > 0

    # Rule: If essential business identity and core action/goal are missing or only placeholder scaffolds,
    # discovery CANNOT be complete. Chatbot must ask questions to be sure about empty tags!
    requires_link = any(w in str((data.get("business_type") or "") + " " + (g_val or "") + " " + (c_val or "") + " " + str(slots.get("campaign_name", {}).get("value") if isinstance(slots.get("campaign_name"), dict) else slots.get("campaign_name") or "")).lower() for w in ["bill", "electric", "pay", "due", "overdue", "donat", "appeal", "fund", "link", "portal"])
    if not (has_b and (has_c or has_g)):
        is_complete = False
    elif requires_link and not has_l:
        is_complete = False
    elif has_b and (has_c or has_g) and (not requires_link or has_l):
        is_complete = True

    if "business_type" not in data or not data["business_type"]:
        all_text = " ".join([
            str(b_val or ""),
            str(g_val or ""),
            str(c_val or ""),
            str(slots.get("campaign_name", {}).get("value") if isinstance(slots.get("campaign_name"), dict) else slots.get("campaign_name") or "")
        ]).lower()
        if any(w in all_text for w in ["boutique", "apparel", "clothing", "sale", "store", "shop", "discount", "retail", "jacket"]):
            data["business_type"] = "Retail"
        elif any(w in all_text for w in ["ngo", "foundation", "charity", "donation", "relief", "appeal", "water"]):
            data["business_type"] = "Non-Profit NGO"
        elif any(w in all_text for w in ["dental", "clinic", "hospital", "doctor", "appointment", "checkup"]):
            data["business_type"] = "Healthcare Clinic"
        elif any(w in all_text for w in ["electricity", "power", "utility", "bill", "energy"]):
            data["business_type"] = "Utility & Energy"
        else:
            data["business_type"] = "General Business"

    if is_complete is True:
        data["discovery_complete"] = True
        data["next_question_to_user"] = ""
        data["question_options"] = []
    else:
        data["discovery_complete"] = False
        if not next_q or next_q.strip().lower() in ("none", "null", ""):
            if not has_b:
                data["next_question_to_user"] = "What is the name of your business or organization sending this message?"
            elif not has_g:
                data["next_question_to_user"] = "What is the main goal or message for this campaign?"
            elif not has_a:
                data["next_question_to_user"] = "Who is the target audience for this campaign?"
            elif not has_c:
                data["next_question_to_user"] = "What is the specific call to action, offer, or payment deadline for recipients?"
            elif not has_l:
                data["next_question_to_user"] = "What payment link, portal URL, or contact phone should recipients use?"
            else:
                data["next_question_to_user"] = "Could you confirm your business name and main offer or deadline to proceed?"
            next_q = data["next_question_to_user"]

        raw_opts = data.get("question_options")
        if not raw_opts or not isinstance(raw_opts, list) or len(raw_opts) == 0:
            raw_opts = generate_quiz_options_for_question(next_q, data.get("business_type", ""))

        clean_opts = [str(opt).strip() for opt in raw_opts if "other" not in str(opt).lower() and str(opt).strip()]
        clean_opts.append("Other (Type custom answer)")
        data["question_options"] = clean_opts

    return data

def is_valid_url_value(val: Optional[str]) -> bool:
    """Verifies whether a string looks like a legitimate web URL or domain."""
    if not val:
        return False
    s = str(val).strip()
    if s.startswith("{{") and s.endswith("}}"):
        return False
    if any(s.lower().startswith(p) for p in ("http://", "https://", "www.")):
        return True
    if re.search(r'^[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}(?:/[^\s]*)?$', s):
        return True
    return False

def is_valid_phone_value(val: Optional[str]) -> bool:
    """Verifies whether a string contains a legitimate phone number with digits."""
    if not val:
        return False
    s = str(val).strip()
    if s.startswith("{{") and s.endswith("}}"):
        return False
    digits = re.findall(r'\d', s)
    return len(digits) >= 5

RECIPIENT_CONTACT_TAGS = {"customer_name", "name", "first_name", "phone", "masked_phone", "contact_name"}

TAG_HINTS = {
    "organization_name": ("Organization Name", "Metro Power & Energy"),
    "business_name": ("Organization Name", "Metro Power & Energy"),
    "company_name": ("Organization Name", "Metro Power & Energy"),
    "payment_link": ("Payment Link", "https://pay.metropower.com/bill"),
    "link": ("Payment or Website Link", "https://pay.metropower.com/bill"),
    "url": ("Payment or Website Link", "https://pay.metropower.com/bill"),
    "due_date": ("Due Date / Deadline", "5:00 PM Today, Oct 30"),
    "deadline": ("Payment Deadline", "5:00 PM Today"),
    "amount": ("Overdue Amount", "$120.00"),
    "bill_amount": ("Overdue Amount", "$142.50"),
    "contact_phone": ("Contact Phone", "1-800-555-0199"),
    "customer_care": ("Customer Care Number", "1-800-555-0199"),
    "customer_care_number": ("Customer Care Number", "1-800-555-0199"),
    "school_phone": ("Contact Phone", "Customer support hotline"),
    "account_number": ("Account Number", "ELEC-9842"),
}

def generate_interactive_choices_for_tag(tag: str, business_type: str = "") -> List[str]:
    """Generates targeted, actionable multiple-choice options for a specific missing tag."""
    t = tag.lower().strip()
    b = (business_type or "").lower()
    if t in ("payment_link", "link", "url", "cta_link", "website"):
        if any(k in b for k in ("util", "energy", "power")):
            return [
                "https://pay.metropower.com/bill",
                "https://quickpay.energy.gov/settle",
                "https://billpay.link/quick",
                "Other (Type custom answer)"
            ]
        elif any(k in b for k in ("ngo", "charity", "appeal")):
            return [
                "https://donate.greenearth.org/give",
                "https://give.ngo/cleanwater",
                "https://fundraise.link/relief",
                "Other (Type custom answer)"
            ]
        elif any(k in b for k in ("retail", "boutique", "shop")):
            return [
                "https://shop.urbanthreads.com/flash30",
                "https://deals.retailboutique.com/weekend",
                "https://store.link/sale",
                "Other (Type custom answer)"
            ]
        else:
            return [
                "https://secure.pay.link/portal",
                "https://quickpay.link/now",
                "https://service.link/account",
                "Other (Type custom answer)"
            ]
    elif t in ("customer_care_number", "customer_care", "contact_phone", "school_phone", "support_phone", "phone"):
        return [
            "1-800-555-0199 (Toll-Free Support)",
            "1-888-247-9000 (24/7 Helpline)",
            "1-800-POWER-HELP (Utility Dispatch)",
            "Other (Type custom answer)"
        ]
    elif t in ("due_date", "deadline"):
        return [
            "5:00 PM Today",
            "Within 48 Hours",
            "End of Business Day Tomorrow",
            "Other (Type custom answer)"
        ]
    elif t in ("organization_name", "business_name", "company_name"):
        return []
    elif t in ("amount", "bill_amount"):
        return [
            "Outstanding Balance",
            "$125.00",
            "$85.50",
            "Other (Type custom answer)"
        ]
    else:
        clean = tag.replace("_", " ").title()
        return [
            f"Standard {clean}",
            f"Priority {clean}",
            f"Verified {clean}",
            "Other (Type custom answer)"
        ]

def extract_missing_placeholders(template_body: str, slots: Dict[str, Any]) -> List[str]:
    """Finds all unique {{placeholder}} tags in the template that require user-provided values."""
    if not template_body:
        return []
    found_tags = list(dict.fromkeys(re.findall(r"\{\{([a-zA-Z0-9_]+)\}\}", template_body)))
    missing = []
    for tag in found_tags:
        t_low = tag.lower().strip()
        if t_low in RECIPIENT_CONTACT_TAGS:
            continue

        val = None
        if t_low in slots:
            item = slots[t_low]
            val = item.get("value") if isinstance(item, dict) else str(item)

        if not val or str(val).strip().lower() in ("none", "null", "", "unspecified", "tbd", "unknown", "n/a"):
            if t_low in ["organization_name", "business_name", "company_name"]:
                b = slots.get("business_name") or slots.get("organization_name")
                val = b.get("value") if isinstance(b, dict) else str(b) if b else None
            elif t_low in ["payment_link", "link", "url", "cta_link", "website"]:
                l = slots.get("link_or_contact") or slots.get("payment_link") or slots.get("link")
                raw_l = l.get("value") if isinstance(l, dict) else str(l) if l else None
                val = raw_l if is_valid_url_value(raw_l) else None
            elif t_low in ["contact_phone", "customer_care", "customer_care_number", "school_phone", "support_phone", "phone"]:
                p = slots.get("customer_care_number") or slots.get("contact_phone") or slots.get("customer_care") or slots.get("link_or_contact")
                raw_p = p.get("value") if isinstance(p, dict) else str(p) if p else None
                val = raw_p if is_valid_phone_value(raw_p) else None

        if val and (str(val).startswith("{{") and str(val).endswith("}}")):
            val = None

        # Strict type check for URL placeholders
        if t_low in ["payment_link", "link", "url", "cta_link", "website"] and val:
            if not is_valid_url_value(val):
                val = None

        # Strict type check for phone placeholders
        if t_low in ["contact_phone", "customer_care", "customer_care_number", "school_phone", "support_phone"] and val:
            if not is_valid_phone_value(val):
                val = None

        if not val or str(val).strip().lower() in ("none", "null", "", "unspecified", "tbd", "unknown", "n/a"):
            missing.append(tag)

    return missing

def generate_questions_ui(missing_tags: List[str]) -> str:
    """Generates the interactive Questions UI to collect missing placeholder values."""
    lines = [
        "### 🛠️ Required Information",
        "To finalize and send this automated message, please provide the following details:\n"
    ]
    for idx, tag in enumerate(missing_tags, 1):
        clean_name = tag.replace("_", " ").title()
        hint_label, hint_eg = TAG_HINTS.get(tag.lower(), (clean_name, "your value"))
        lines.append(f"{idx}. **{hint_label}** (e.g., {hint_eg})")

    lines.append("\nPlease reply with the values in order (e.g., \"1. Green Energy Co, 2. https://pay.link\") or fill them out one by one.")
    return "\n".join(lines)

def resolve_placeholders_from_reply(user_msg: str, missing_tags: List[str], slots: Dict[str, Any]) -> Tuple[Dict[str, str], List[str]]:
    """Extracts values from user message and maps them to the missing placeholder tags."""
    clean_msg = user_msg.strip()
    resolved: Dict[str, str] = {}

    # 1. Numbered parsing: e.g. "1. Green Energy Co, 2. https://pay.link" or "1: Acme \n 2: https://..."
    numbered_parts = re.split(r"(?:^|\n|\s+)(?:\d+[\.\:\)]|\#\d+)\s*", clean_msg)
    numbered_parts = [p.strip().rstrip(",;").strip() for p in numbered_parts if p.strip()]

    if len(numbered_parts) >= 1 and (len(numbered_parts) == len(missing_tags) or re.search(r"^\s*1[\.\:\)]", clean_msg)):
        for idx, tag in enumerate(missing_tags):
            if idx < len(numbered_parts):
                val = numbered_parts[idx].strip()
                if val:
                    resolved[tag] = val

    # 2. Labeled parsing: e.g. "Organization Name: Acme Corp", "Payment Link: https://..."
    if not resolved:
        for tag in missing_tags:
            tag_readable = tag.replace("_", " ")
            patterns = [
                rf"(?i){re.escape(tag)}\s*[:=\-]\s*([^,\n]+)",
                rf"(?i){re.escape(tag_readable)}\s*[:=\-]\s*([^,\n]+)"
            ]
            for pat in patterns:
                m = re.search(pat, clean_msg)
                if m:
                    resolved[tag] = m.group(1).strip()
                    break

    # 3. Comma-separated if count matches exactly
    if not resolved and "," in clean_msg and len(missing_tags) > 1:
        comma_parts = [p.strip() for p in clean_msg.split(",") if p.strip()]
        if len(comma_parts) == len(missing_tags):
            for idx, tag in enumerate(missing_tags):
                resolved[tag] = comma_parts[idx]

    # 4. Single value for single missing tag
    if not resolved and len(missing_tags) == 1:
        val = clean_msg
        if re.match(r"^1[\.\:\)]\s*", val):
            val = re.sub(r"^1[\.\:\)]\s*", "", val).strip()
        resolved[missing_tags[0]] = val

    # Update slots
    for tag, val in resolved.items():
        if tag in ["organization_name", "business_name", "company_name"]:
            slots["business_name"] = {"value": val, "required": True, "confidence": 1.0, "source": "user"}
        elif tag in ["payment_link", "link", "url", "cta_link"]:
            slots["link_or_contact"] = {"value": val, "required": True, "confidence": 1.0, "source": "user"}
            slots["payment_link"] = {"value": val, "required": True, "confidence": 1.0, "source": "user"}
        elif tag in ["contact_phone", "customer_care", "customer_care_number", "school_phone", "support_phone", "phone"]:
            slots["contact_phone"] = {"value": val, "required": True, "confidence": 1.0, "source": "user"}
            slots["customer_care_number"] = {"value": val, "required": True, "confidence": 1.0, "source": "user"}
        else:
            slots[tag] = {"value": val, "required": False, "confidence": 1.0, "source": "user"}

    remaining = [t for t in missing_tags if t not in resolved]
    return resolved, remaining

def fill_template_placeholders(template_body: str, resolved: Dict[str, str]) -> str:
    """Replaces resolved placeholder tags in the template body."""
    body = template_body
    for tag, val in resolved.items():
        body = body.replace(f"{{{{{tag}}}}}", str(val))
        body = body.replace(f"{{{{{tag.lower()}}}}}", str(val))
        if tag.lower() in ["organization_name", "business_name", "company_name"]:
            body = body.replace("{{organization_name}}", str(val))
            body = body.replace("{{business_name}}", str(val))
            body = body.replace("{{company_name}}", str(val))
        elif tag.lower() in ["payment_link", "link", "url", "cta_link", "website"]:
            body = body.replace("{{payment_link}}", str(val))
            body = body.replace("{{link}}", str(val))
            body = body.replace("{{url}}", str(val))
            body = body.replace("{{website}}", str(val))
        elif tag.lower() in ["contact_phone", "customer_care", "customer_care_number", "support_phone", "school_phone", "phone"]:
            body = body.replace("{{contact_phone}}", str(val))
            body = body.replace("{{customer_care}}", str(val))
            body = body.replace("{{customer_care_number}}", str(val))
            body = body.replace("{{support_phone}}", str(val))
            body = body.replace("{{school_phone}}", str(val))
            body = body.replace("{{phone}}", str(val))
    return body

def clean_organization_name(raw: str, fallback_domain: str = "General Business") -> str:
    """Extracts a clean concise business/organization name, filtering out conversational rambling or long prompts."""
    if not raw or not isinstance(raw, str):
        return ""
    text = raw.strip()
    words = text.split()

    conversational_markers = [
        "i run", "i am", "we are", "want to", "invite", "selling", "our shop", "my business",
        "crm", "need a message", "reminder", "send a", "everybody", "based on", "delight exhibition",
        "near 6th", "near 10th", "please", "call and", "enquire", "potential", "product", "so i want"
    ]
    if len(words) <= 4 and not any(m in text.lower() for m in conversational_markers):
        return text

    # Check for quotes like "Help the needy" or 'Metro Power'
    m_quote = re.search(r'["\']([^"\']{2,35})["\']', text)
    if m_quote and len(m_quote.group(1).split()) <= 4:
        return m_quote.group(1).strip()

    # Check for "called <Name>", "named <Name>", "known as <Name>"
    m_named = re.search(r'\b(?:called|named|known as)\s+([A-Za-z0-9\s&\'\-]{2,30})', text, re.IGNORECASE)
    if m_named:
        cand = m_named.group(1).strip().rstrip('.,;')
        if len(cand.split()) <= 4 and not any(m in cand.lower() for m in ["and", "to", "for", "with"]):
            return cand

    # If the user did not specify a concise organization name, return empty string so they can provide it cleanly
    return ""

class OrchestratorEngine:
    def __init__(self):
        self.config = get_config()
        self.storage = get_storage()
        self.prompts = get_prompt_manager()
        self.llm = get_llm_client()
        self.safety = get_safety_service()

    async def run_discovery_round(
        self,
        session_id: str,
        workspace_id: str,
        latest_user_message: str,
        conversation_history: List[Dict[str, str]],
        current_slots: Dict[str, Any],
        round_index: int
    ) -> Dict[str, Any]:
        """
        Executes one adaptive discovery step.
        Determines business type, extracts slots, evaluates confidence,
        generates the next 1-2 questions, or declares discovery complete.
        """
        # 1. First safety check on user input
        safety_eval = await self.safety.evaluate_safety(session_id, latest_user_message, "discovery_input")
        if not safety_eval.get("allowed", False):
            return {
                "blocked": True,
                "reason": safety_eval.get("reason"),
                "alternative": safety_eval.get("suggested_alternative"),
                "is_rate_limit": safety_eval.get("is_rate_limit", False)
            }

        # 2. Check memory with business/domain scoping if established
        from app.services.memory_service import get_memory_service
        b_name = current_slots.get("business_name", {}).get("value")
        b_type = current_slots.get("business_type", {}).get("value")
        memory_context = get_memory_service().get_memory_context(workspace_id, business_type=b_type, business_name=b_name)

        # 3. Format history (deduplicated and compacted)
        # Avoid duplicating latest_user_message if already in conversation_history
        history_prior = conversation_history[:-1] if conversation_history and conversation_history[-1].get("role") == "user" else conversation_history
        formatted_items = []
        for msg in history_prior:
            role = msg.get("role", "user").capitalize()
            content = str(msg.get("content", "")).strip()
            # Condense verbose markdown tone cards from assistant to preserve context budget
            if role.lower() == "assistant" and len(content) > 250 and ("Tone Exploration" in content or "Candidate" in content or "candidate" in content):
                content = "Requirements confirmed. Candidate tones proposed for selection."
            formatted_items.append(f"{role}: {content}")
        history_text = "\n".join(formatted_items) if formatted_items else "No previous conversation history."

        # 4. Render discovery prompt
        prompt = self.prompts.render("discovery", {
            "memory_context": memory_context,
            "slots_json": json.dumps(current_slots, indent=2),
            "conversation_history": history_text,
            "latest_user_message": latest_user_message
        })

        system_msg = self.prompts.load_prompt("system")
        max_retries = self.config.orchestrator.max_retries
        parsed = None

        for attempt in range(max_retries + 1):
            llm_res = await self.llm.call_llm(
                session_id=session_id,
                purpose="discovery",
                system_prompt=f"{system_msg}\n\n{prompt}",
                user_message="Extract required campaign slots from the conversation and latest user message. Formulate the next quiz question or declare complete.",
                step_id="discovery",
                json_mode=True
            )

            if llm_res.get("status") == "success":
                raw = llm_res.get("raw_response", "")
                data = parse_json_robust(raw)
                if data and ("slots" in data or "business_type" in data or "next_question_to_user" in data):
                    parsed = normalize_discovery_output(data)
                    break
                else:
                    self.storage.log_validation(
                        session_id=session_id,
                        validation_type="discovery_json_parse",
                        passed=False,
                        details={"error": "Malformed or non-dict discovery JSON output", "raw": raw, "attempt": attempt}
                    )

        if not parsed:
            # Fallback basic structure if model repeatedly failed schema
            parsed = {
                "business_type": "General Business",
                "slots": current_slots,
                "discovery_complete": round_index >= self.config.orchestrator.max_discovery_rounds,
                "assumptions_made": ["Assumed default CRM campaign configuration due to retry limits."],
                "next_question_to_user": "Could you confirm your business name and main campaign goal?"
            }
        else:
            parsed = normalize_discovery_output(parsed)

        # Check if max discovery rounds reached
        if round_index >= self.config.orchestrator.max_discovery_rounds:
            parsed["discovery_complete"] = True
            parsed["next_question_to_user"] = ""
            if "assumptions_made" not in parsed or not parsed["assumptions_made"]:
                parsed["assumptions_made"] = ["Standard campaign parameters assumed based on business type."]

        # Merge slots
        updated_slots = dict(current_slots)
        for k, v in parsed.get("slots", {}).items():
            updated_slots[k] = v

        # Direct extraction for explicit URL or phone replies to prevent LLM omissions
        clean_msg = latest_user_message.strip()
        if is_valid_url_value(clean_msg):
            url_slot = {"value": clean_msg, "required": True, "confidence": 1.0, "source": "user"}
            updated_slots["link_or_contact"] = url_slot
            updated_slots["payment_link"] = url_slot
            parsed["slots"] = updated_slots
            parsed = normalize_discovery_output(parsed)
        elif is_valid_phone_value(clean_msg):
            phone_slot = {"value": clean_msg, "required": True, "confidence": 1.0, "source": "user"}
            updated_slots["contact_phone"] = phone_slot
            updated_slots["customer_care_number"] = phone_slot
            parsed["slots"] = updated_slots
            parsed = normalize_discovery_output(parsed)

        parsed["slots"] = updated_slots
        return parsed

    async def explore_tones(
        self,
        session_id: str,
        workspace_id: str,
        business_type: str,
        slots: Dict[str, Any],
        user_tone_idea: str = ""
    ) -> Dict[str, Any]:
        """Proposes dynamic candidate tones with short real examples."""
        from app.services.memory_service import get_memory_service
        memory_context = get_memory_service().get_memory_context(workspace_id)

        business_name = slots.get("business_name", {}).get("value") or "Our Company"
        campaign_name = slots.get("campaign_name", {}).get("value") or "New Campaign"
        campaign_goal = slots.get("campaign_goal", {}).get("value") or "Special Offer"
        target_audience = slots.get("target_audience", {}).get("value") or "Valued Customers"

        prompt = self.prompts.render("tone_explorer", {
            "business_type": business_type,
            "business_name": business_name,
            "campaign_name": campaign_name,
            "campaign_goal": campaign_goal,
            "target_audience": target_audience,
            "user_tone_idea": user_tone_idea or "None specified",
            "memory_context": memory_context,
            "word_limit": str(self.config.orchestrator.word_limit),
            "optout_text": self.config.orchestrator.optout_text
        })

        system_msg = self.prompts.load_prompt("system")
        max_retries = self.config.orchestrator.max_retries
        parsed = None

        for attempt in range(max_retries + 1):
            llm_res = await self.llm.call_llm(
                session_id=session_id,
                purpose="tone_proposal",
                system_prompt=f"{system_msg}\n\n{prompt}",
                user_message="Propose suitable candidate tones with preview examples for this campaign.",
                step_id="tone_explorer",
                json_mode=True
            )

            if llm_res.get("status") == "success":
                raw = llm_res.get("raw_response", "")
                data = parse_json_robust(raw)
                if data and "candidate_tones" in data and len(data["candidate_tones"]) > 0:
                    parsed = data
                    break
                else:
                    self.storage.log_validation(
                        session_id=session_id,
                        validation_type="tone_explorer_json_parse",
                        passed=False,
                        details={"error": "Failed to parse candidate_tones", "raw": raw, "attempt": attempt}
                    )

        min_candidates = getattr(self.config.orchestrator, "min_tone_candidates", 3)
        raw_tones = parsed.get("candidate_tones", []) if parsed else []

        def build_multi_examples(t_obj: Dict[str, Any], tone_name: str) -> List[str]:
            raw_list = t_obj.get("example_messages")
            if isinstance(raw_list, list) and len(raw_list) >= 2:
                return [str(m).strip() for m in raw_list if str(m).strip()]
            
            single = (t_obj.get("example_message") or "").strip()
            if not single:
                single = f"Hello {{{{customer_name}}}}, important update from {business_name} regarding {campaign_goal}. {self.config.orchestrator.optout_text}"
            
            return [
                single,
                f"Follow-up from {business_name}: We wanted to check in regarding {campaign_goal}. Please review details at {{{{link}}}}. {self.config.orchestrator.optout_text}",
                f"Final reminder from {business_name}: Last notice regarding {campaign_goal}. Immediate action required. {self.config.orchestrator.optout_text}"
            ]

        cleaned_tones = []
        seen_names = set()
        for t in raw_tones:
            name = (t.get("name") or "").strip()
            desc = (t.get("description") or "").strip()
            if name and name.lower() not in seen_names:
                seen_names.add(name.lower())
                msgs = build_multi_examples(t, name)
                cleaned_tones.append({
                    "id": t.get("id") or f"tone_{len(cleaned_tones)+1}",
                    "name": name,
                    "description": desc or f"Campaign messaging tone: {name}",
                    "example_message": msgs[0],
                    "example_messages": msgs
                })

        # If fewer than min_candidates, run a lightweight repair prompt (up to 1 retry)
        if len(cleaned_tones) < min_candidates:
            needed = min_candidates - len(cleaned_tones)
            existing_names_str = ", ".join(f"'{t['name']}'" for t in cleaned_tones) if cleaned_tones else "none"
            repair_prompt = f"""You previously proposed {len(cleaned_tones)} tone(s): {existing_names_str}.
We need at least {min_candidates} candidate tones. Please provide {needed} more DISTINCT candidate tone(s) for business '{business_name}' ({business_type}).
Do NOT repeat existing tones.
Respond in strict JSON:
{{
  "additional_tones": [
    {{
      "id": "new_tone_id",
      "name": "Distinct Tone Name with Emoji",
      "description": "Short explanation of the tone",
      "example_message": "Message 1 preview...",
      "example_messages": [
        "Message 1 (Announcement) preview...",
        "Message 2 (Follow-up) preview...",
        "Message 3 (Final Call) preview..."
      ]
    }}
  ]
}}"""
            rep_res = await self.llm.call_llm(
                session_id=session_id,
                purpose="tone_proposal",
                system_prompt="You are a tone suggestion assistant. Output valid JSON.",
                user_message=repair_prompt,
                step_id="tone_repair",
                json_mode=True
            )
            if rep_res.get("status") == "success":
                rep_data = parse_json_robust(rep_res.get("raw_response", ""))
                if rep_data:
                    new_tones = rep_data.get("additional_tones") or rep_data.get("candidate_tones") or []
                    for t in new_tones:
                        name = (t.get("name") or "").strip()
                        desc = (t.get("description") or "").strip()
                        if name and name.lower() not in seen_names:
                            seen_names.add(name.lower())
                            msgs = build_multi_examples(t, name)
                            cleaned_tones.append({
                                "id": t.get("id") or f"tone_{len(cleaned_tones)+1}",
                                "name": name,
                                "description": desc or f"Campaign messaging tone: {name}",
                                "example_message": msgs[0],
                                "example_messages": msgs
                            })
                            if len(cleaned_tones) >= min_candidates:
                                break

        # Fallback complement if still under min_candidates
        if len(cleaned_tones) < min_candidates:
            fallback_options = [
                ("warm_friendly", "✨ Warm & Friendly", "Engaging, conversational tone focused on customer connection.", [
                    f"Hello {{{{customer_name}}}}, we hope this message finds you well! {business_name} wanted to let you know about {campaign_goal}. {self.config.orchestrator.optout_text}",
                    f"Hi {{{{customer_name}}}}, just a gentle follow-up from {business_name} regarding {campaign_goal}. We are here to help if you have questions! {self.config.orchestrator.optout_text}",
                    f"Hello {{{{customer_name}}}}, final friendly reminder from {business_name} regarding {campaign_goal}. Please let us know if we can assist you. {self.config.orchestrator.optout_text}"
                ]),
                ("clear_direct", "🎯 Clear & Direct", "Concise and action-driven message with clear next steps.", [
                    f"Notice from {business_name}: Please be informed regarding {campaign_goal}. Visit {{{{link}}}} to complete your action. {self.config.orchestrator.optout_text}",
                    f"Reminder from {business_name}: This is a follow-up regarding {campaign_goal}. Timely action is required. {self.config.orchestrator.optout_text}",
                    f"Final Notice from {business_name}: Urgent action required regarding {campaign_goal}. Immediate payment/response needed. {self.config.orchestrator.optout_text}"
                ]),
                ("formal_admin", "🏛️ Formal & Professional", "Professional, structured communication adhering to official policy standards.", [
                    f"Dear Parent/Guardian, this is an official notification from {business_name} concerning {campaign_goal}. {self.config.orchestrator.optout_text}",
                    f"Dear Parent/Guardian, this is a formal follow-up from {business_name} regarding {campaign_goal}. {self.config.orchestrator.optout_text}",
                    f"Dear Parent/Guardian, final administrative reminder from {business_name}: {campaign_goal} must be settled immediately to avoid restriction. {self.config.orchestrator.optout_text}"
                ])
            ]
            for f_id, f_name, f_desc, f_msgs in fallback_options:
                if f_name.lower() not in seen_names:
                    seen_names.add(f_name.lower())
                    cleaned_tones.append({
                        "id": f_id,
                        "name": f_name,
                        "description": f_desc,
                        "example_message": f_msgs[0],
                        "example_messages": f_msgs
                    })
                if len(cleaned_tones) >= min_candidates:
                    break

        return {
            "rationale": parsed.get("rationale", f"Adaptive tones selected for {business_type}") if parsed else f"Adaptive tones selected for {business_type}",
            "candidate_tones": cleaned_tones
        }

    async def generate_plan(
        self,
        session_id: str,
        business_type: str,
        slots: Dict[str, Any],
        selected_tone: str,
        contacts_count: int = 0
    ) -> Dict[str, Any]:
        """Generates the dynamic orchestrator pipeline steps based on the business use case."""
        business_name = slots.get("business_name", {}).get("value") or "Our Company"
        campaign_name = slots.get("campaign_name", {}).get("value") or "Campaign"
        target_audience = slots.get("target_audience", {}).get("value") or "Audience"
        messages_count = slots.get("messages_per_contact", {}).get("value") or 3

        prompt = self.prompts.render("planner", {
            "business_type": business_type,
            "business_name": business_name,
            "campaign_name": campaign_name,
            "target_audience": target_audience,
            "messages_count": str(messages_count),
            "selected_tone": selected_tone,
            "has_contacts": "true" if contacts_count > 0 else "false",
            "contact_count": str(contacts_count)
        })

        system_msg = self.prompts.load_prompt("system")
        max_retries = self.config.orchestrator.max_retries
        parsed = None

        for attempt in range(max_retries + 1):
            llm_res = await self.llm.call_llm(
                session_id=session_id,
                purpose="planner",
                system_prompt=f"{system_msg}\n\n{prompt}",
                user_message="Generate the custom orchestrator pipeline steps for this campaign.",
                step_id="planner",
                json_mode=True
            )

            if llm_res.get("status") == "success":
                raw = llm_res.get("raw_response", "")
                data = parse_json_robust(raw)
                if data and "steps" in data and len(data["steps"]) > 0:
                    parsed = data
                    break
                else:
                    self.storage.log_validation(
                        session_id=session_id,
                        validation_type="planner_json_parse",
                        passed=False,
                        details={"error": "Failed to parse planning blueprint", "raw": raw, "attempt": attempt}
                    )

        if not parsed:
            # Fallback domain-tailored pipeline
            parsed = {
                "plan_summary": f"Orchestrator pipeline configured for {business_type}",
                "steps": [
                    {
                        "step_id": "structure_sequence",
                        "title": "Sequence Structuring",
                        "icon": "📐",
                        "description": f"Planning {messages_count}-touchpoint cadence for {business_type}",
                        "sub_steps": [
                            "Mapping touchpoint milestones",
                            "Configuring recipient time intervals",
                            "Defining personalization placeholders"
                        ]
                    },
                    {
                        "step_id": "draft_templates",
                        "title": "Template Generation",
                        "icon": "✍️",
                        "description": f"Drafting templates in tone '{selected_tone}'",
                        "sub_steps": [
                            f"Drafting touchpoint 1 (Announcement)",
                            f"Drafting touchpoint 2 (Follow-up)",
                            f"Drafting touchpoint 3 (Final Call)",
                            "Verifying business identity & opt-out"
                        ]
                    },
                    {
                        "step_id": "compliance_audit",
                        "title": "Compliance & Safety Audit",
                        "icon": "🛡️",
                        "description": f"Strict {self.config.orchestrator.word_limit}-word compliance and safety verification",
                        "sub_steps": [
                            f"Word count ceiling enforcement (≤ {self.config.orchestrator.word_limit} words)",
                            "Mandatory opt-out statement verification",
                            "Safety policy classification"
                        ]
                    },
                    {
                        "step_id": "campaign_ready",
                        "title": "Campaign Package Ready",
                        "icon": "🚀",
                        "description": "Ready for approval and export",
                        "sub_steps": [
                            "Packaging approved message templates",
                            "Generating sample recipient preview",
                            "Awaiting final user sign-off"
                        ]
                    }
                ]
            }

        return parsed

    async def generate_and_validate_templates(
        self,
        session_id: str,
        business_type: str,
        slots: Dict[str, Any],
        selected_tone: str,
        contacts_sample: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """Generates sequence templates and validates against word count & compliance rules with auto-repair."""
        business_name = slots.get("business_name", {}).get("value") or "Our Business"
        campaign_name = slots.get("campaign_name", {}).get("value") or "Campaign"
        campaign_goal = slots.get("campaign_goal", {}).get("value") or "Special Offer"
        target_audience = slots.get("target_audience", {}).get("value") or "Customers"
        link_or_contact = slots.get("link_or_contact", {}).get("value") or ""
        messages_count = int(slots.get("messages_per_contact", {}).get("value") or 1)
        gap_between_messages = slots.get("gap_between_messages", {}).get("value") or "Immediate"
        language = slots.get("language", {}).get("value") or "English"
        must_include = slots.get("must_include", {}).get("value") or ""
        must_avoid = slots.get("must_avoid", {}).get("value") or ""

        word_limit = self.config.orchestrator.word_limit
        optout_text = self.config.orchestrator.optout_text

        prompt = self.prompts.render("template_writer", {
            "business_type": business_type,
            "business_name": business_name,
            "campaign_name": campaign_name,
            "campaign_goal": campaign_goal,
            "target_audience": target_audience,
            "link_or_contact": link_or_contact,
            "messages_count": str(messages_count),
            "gap_between_messages": str(gap_between_messages),
            "selected_tone": selected_tone,
            "must_include": must_include,
            "must_avoid": must_avoid,
            "language": language,
            "word_limit": str(word_limit),
            "optout_text": optout_text
        })

        system_msg = self.prompts.load_prompt("system")
        max_retries = self.config.orchestrator.max_retries
        parsed = None

        for attempt in range(max_retries + 1):
            llm_res = await self.llm.call_llm(
                session_id=session_id,
                purpose="template_write",
                system_prompt=f"{system_msg}\n\n{prompt}",
                user_message="Generate the compliant message templates for this campaign.",
                step_id="template_writer",
                json_mode=True
            )

            if llm_res.get("status") == "success":
                raw = llm_res.get("raw_response", "")
                data = parse_json_robust(raw)
                if data and "templates" in data and len(data["templates"]) > 0:
                    parsed = data
                    break
                else:
                    self.storage.log_validation(
                        session_id=session_id,
                        validation_type="template_writer_json_parse",
                        passed=False,
                        details={"error": "Failed to parse templates JSON", "raw": raw, "attempt": attempt}
                    )

        if not parsed:
            # Fallback structured templates
            parsed = {
                "sequence_summary": f"Standard sequence for {business_name}",
                "recommended_schedule": "Day 1 -> Day 3 -> Day 7",
                "templates": [
                    {
                        "message_index": 1,
                        "stage": "Announcement",
                        "tone": selected_tone,
                        "body": f"Hello {{{{customer_name}}}}, {business_name} is excited to announce {campaign_name}! {campaign_goal}. Visit {{{{link}}}} for details.\n\n{optout_text}",
                        "tags_used": ["customer_name", "business_name", "link"],
                        "estimated_word_count": 28
                    }
                ]
            }

        # Validate and repair templates in parallel using asyncio.gather
        async def process_single_template(tmpl: Dict[str, Any]) -> Dict[str, Any]:
            body = tmpl.get("body", "")
            comp = check_template_compliance(
                body=body,
                business_name=business_name,
                word_limit=word_limit,
                optout_text=optout_text,
                link_or_contact=link_or_contact
            )

            retries_done = 0
            while not comp["compliant"] and retries_done < max_retries:
                retries_done += 1
                repair_prompt = self.prompts.render("validator_repair", {
                    "violations_list": "\n".join(f"- {v}" for v in comp["violations"]),
                    "word_limit": str(word_limit),
                    "optout_text": optout_text,
                    "business_name": business_name,
                    "original_content": body
                })

                rep_res = await self.llm.call_llm(
                    session_id=session_id,
                    purpose="repair",
                    system_prompt=f"{system_msg}\n\n{repair_prompt}",
                    user_message="Repair this template to strictly satisfy all compliance rules.",
                    step_id="validator_repair",
                    json_mode=True
                )

                if rep_res.get("status") == "success":
                    rep_data = parse_json_robust(rep_res.get("raw_response", ""))
                    if rep_data:
                        repaired_body = rep_data.get("repaired_body")
                        if repaired_body:
                            body = repaired_body
                            comp = check_template_compliance(
                                body=body,
                                business_name=business_name,
                                word_limit=word_limit,
                                optout_text=optout_text,
                                link_or_contact=link_or_contact
                            )

            # Final safety check on the template body
            safety_res = await self.safety.evaluate_safety(session_id, body, f"template_{tmpl.get('message_index')}")
            is_safe = safety_res.get("allowed", True)

            # Record validation log
            self.storage.log_validation(
                session_id=session_id,
                validation_type=f"template_compliance_{tmpl.get('message_index')}",
                passed=comp["compliant"] and is_safe,
                details={
                    "message_index": tmpl.get("message_index"),
                    "word_count": comp["word_count"],
                    "word_limit": word_limit,
                    "violations": comp["violations"],
                    "safety": safety_res,
                    "retries": retries_done
                }
            )

            # Build preview with actual uploaded/pasted contact data
            previews = []
            has_real_contacts = contacts_sample is not None and len(contacts_sample) > 0
            if has_real_contacts:
                for sc in contacts_sample[:5]:
                    rendered_preview = body
                    c_name = sc.get("name", "Customer")
                    c_phone = sc.get("phone", sc.get("masked_phone", ""))

                    # Standard tags
                    rendered_preview = rendered_preview.replace("{{customer_name}}", c_name)
                    rendered_preview = rendered_preview.replace("{{name}}", c_name)
                    rendered_preview = rendered_preview.replace("{{donor_name}}", c_name)
                    rendered_preview = rendered_preview.replace("{{client_name}}", c_name)
                    rendered_preview = rendered_preview.replace("{{business_name}}", business_name)
                    rendered_preview = rendered_preview.replace("{{org_name}}", business_name)
                    rendered_preview = rendered_preview.replace("{{campaign_name}}", campaign_name)
                    rendered_preview = rendered_preview.replace("{{link}}", link_or_contact or "https://example.com")
                    rendered_preview = rendered_preview.replace("{{donation_link}}", link_or_contact or "https://example.com")
                    rendered_preview = rendered_preview.replace("{{phone}}", c_phone)

                    # Extra columns
                    extra_data = sc.get("extra", {})
                    for col_k, col_v in extra_data.items():
                        tag = "{{" + col_k.lower().strip().replace(" ", "_") + "}}"
                        rendered_preview = rendered_preview.replace(tag, str(col_v))
                        rendered_preview = rendered_preview.replace("{{" + col_k + "}}", str(col_v))

                    previews.append({
                        "contact_name": c_name,
                        "phone": sc.get("phone", ""),
                        "masked_phone": sc.get("masked_phone", ""),
                        "extra": extra_data,
                        "rendered_text": rendered_preview
                    })

            return {
                "message_index": tmpl.get("message_index", 1),
                "stage": tmpl.get("stage", "Message"),
                "tone": selected_tone,
                "body": body,
                "word_count": comp["word_count"],
                "word_limit": word_limit,
                "compliant": comp["compliant"] and is_safe,
                "violations": comp["violations"],
                "safety_status": "safe" if is_safe else "blocked",
                "retries": retries_done,
                "previews": previews
            }

        raw_templates = parsed.get("templates", [])
        if raw_templates:
            raw_templates = raw_templates[:1]
        if raw_templates:
            validated_templates = await asyncio.gather(*(process_single_template(t) for t in raw_templates))
        else:
            validated_templates = []
        all_compliant = all(t["compliant"] for t in validated_templates) if validated_templates else True

        return {
            "sequence_summary": parsed.get("sequence_summary", ""),
            "recommended_schedule": parsed.get("recommended_schedule", "Day 1 -> Day 3 -> Day 7"),
            "all_compliant": all_compliant,
            "templates": list(validated_templates)
        }

    async def revise_campaign_template(
        self,
        session_id: str,
        workspace_id: str,
        business_type: str,
        slots: Dict[str, Any],
        selected_tone: str,
        current_templates: List[Dict[str, Any]],
        user_feedback: str
    ) -> Dict[str, Any]:
        """Intelligently processes post-template conversational user feedback.
        Classifies intent (slot update, template rewrite, or tone change) without
        dumping meta-prompting words into must_include. Validates and auto-repairs revised template.
        """
        # Safety check on user feedback
        safety_eval = await self.safety.evaluate_safety(session_id, user_feedback, "revision_input")
        if not safety_eval.get("allowed", False):
            return {
                "blocked": True,
                "reason": safety_eval.get("reason"),
                "alternative": safety_eval.get("suggested_alternative")
            }

        word_limit = self.config.orchestrator.word_limit
        optout_text = self.config.orchestrator.optout_text
        current_body = current_templates[0].get("body", "") if current_templates else ""
        business_name = slots.get("business_name", {}).get("value") or "Our Business"

        revision_system = f"""You are the Campaign Revision Module of CampaignForge.
A user has already received a generated 1-message CRM campaign template and is providing feedback, edits, or changes in conversation.

Campaign Context:
- Business: {business_name} ({business_type})
- Tone: {selected_tone}
- Word Limit: {word_limit} words (STRICT CEILING)
- Mandatory Opt-Out: "{optout_text}"
- Current Slots:
{json.dumps(slots, indent=2)}

Current Template Body:
"{current_body}"

User's Feedback / Revision Request:
"{user_feedback}"

YOUR TASK:
1. Determine the user's intent:
   - "tone_change": The user wants a different messaging tone, style vibe, or wants to see new candidate tone options (e.g. "make it funny", "switch to formal", "try a different tone").
   - "slot_update": The user is correcting or updating specific campaign details (e.g. business name, phone number, link, deadline, offer percentage, target audience).
   - "template_edit": The user wants to change specific phrasing, wording, length, or emphasis in the template without changing the overarching tone (e.g. "make it more urgent", "remove the word discount", "mention that power cut happens at 5 PM").

2. If intent is "tone_change":
   - Set "intent": "tone_change".
   - Set "tone_idea": summarize the requested tone direction.

3. If intent is "slot_update":
   - Identify which slot(s) changed and provide the new values in "slot_updates".
   - CRITICAL: Do NOT put conversational instructions like "make it punchy" or "rewrite this" into "must_include"! Only put actual verbatim text that must appear in the message.
   - Provide the updated "revised_body" incorporating the new slot information.

4. If intent is "template_edit":
   - Directly edit and rewrite the message template into "revised_body".
   - STRICT RULES for revised_body:
     * Exactly 1 message.
     * Must be strictly under {word_limit} words.
     * Must end with the exact opt-out text: "{optout_text}"
     * Must mention {business_name} (or clean scaffold {{{{business_name}}}}).
     * Must preserve necessary personalization tags like {{{{customer_name}}}}.

5. Provide a 1-sentence friendly explanation of the changes made in "changes_summary".

OUTPUT FORMAT (Strict JSON):
```json
{{
  "intent": "template_edit",
  "slot_updates": {{}},
  "tone_idea": "",
  "revised_body": "Updated message body ending with {optout_text}",
  "changes_summary": "Adjusted the urgency and updated the deadline to 5 PM."
}}
```"""

        llm_res = await self.llm.call_llm(
            session_id=session_id,
            purpose="repair",
            system_prompt=revision_system,
            user_message=f"Process this revision request: '{user_feedback}'",
            step_id="template_revision",
            json_mode=True
        )

        parsed = None
        if llm_res.get("status") == "success":
            parsed = parse_json_robust(llm_res.get("raw_response", ""))

        if not parsed:
            # Fallback heuristic
            if "tone" in user_feedback.lower():
                return {"action": "tone_change", "tone_idea": user_feedback}
            tmpl_res = await self.generate_and_validate_templates(
                session_id=session_id,
                business_type=business_type,
                slots=slots,
                selected_tone=selected_tone,
                contacts_sample=None
            )
            return {
                "action": "template_updated",
                "templates": tmpl_res.get("templates", current_templates),
                "slots": slots,
                "changes_summary": f"Refreshed template addressing: '{user_feedback[:50]}'"
            }

        intent = parsed.get("intent", "template_edit")
        if intent == "tone_change" or ("tone" in user_feedback.lower() and not parsed.get("revised_body")):
            return {
                "action": "tone_change",
                "tone_idea": parsed.get("tone_idea") or user_feedback
            }

        # Apply slot updates if any
        slot_updates = parsed.get("slot_updates") or {}
        for k, v in slot_updates.items():
            if k in slots and isinstance(slots[k], dict):
                slots[k]["value"] = v
                slots[k]["source"] = "user"
            elif v:
                slots[k] = {"value": v, "required": False, "confidence": 1.0, "source": "user"}

        revised_body = (parsed.get("revised_body") or "").strip()
        if not revised_body:
            tmpl_res = await self.generate_and_validate_templates(
                session_id=session_id,
                business_type=business_type,
                slots=slots,
                selected_tone=selected_tone,
                contacts_sample=None
            )
            return {
                "action": "template_updated",
                "templates": tmpl_res.get("templates", current_templates),
                "slots": slots,
                "changes_summary": parsed.get("changes_summary") or "Updated template based on feedback."
            }

        # Validate and repair the revised body
        comp = check_template_compliance(
            body=revised_body,
            business_name=business_name,
            word_limit=word_limit,
            optout_text=optout_text,
            link_or_contact=slots.get("link_or_contact", {}).get("value") or ""
        )

        if not comp["compliant"]:
            repair_prompt = self.prompts.render("validator_repair", {
                "violations_list": "\n".join(f"- {v}" for v in comp["violations"]),
                "word_limit": str(word_limit),
                "optout_text": optout_text,
                "business_name": business_name,
                "original_content": revised_body
            })
            rep_res = await self.llm.call_llm(
                session_id=session_id,
                purpose="repair",
                system_prompt=f"{self.prompts.load_prompt('system')}\n\n{repair_prompt}",
                user_message="Repair this template to strictly satisfy all compliance rules.",
                step_id="validator_repair",
                json_mode=True
            )
            if rep_res.get("status") == "success":
                rep_data = parse_json_robust(rep_res.get("raw_response", ""))
                if rep_data and rep_data.get("repaired_body"):
                    revised_body = rep_data["repaired_body"]
                    comp = check_template_compliance(
                        body=revised_body,
                        business_name=business_name,
                        word_limit=word_limit,
                        optout_text=optout_text,
                        link_or_contact=slots.get("link_or_contact", {}).get("value") or ""
                    )

        # Safety evaluation
        safety_eval = await self.safety.evaluate_safety(session_id, revised_body, "template_revision")
        is_safe = safety_eval.get("allowed", True)

        tags_found = list(set(re.findall(r"\{\{([a-zA-Z0-9_]+)\}\}", revised_body)))
        updated_template = {
            "message_index": 1,
            "stage": current_templates[0].get("stage", "Announcement") if current_templates else "Announcement",
            "tone": selected_tone,
            "body": revised_body,
            "tags_used": tags_found,
            "word_count": comp["word_count"],
            "word_limit": word_limit,
            "compliant": comp["compliant"] and is_safe,
            "violations": comp["violations"],
            "safety_status": "safe" if is_safe else "blocked",
            "retries": 0,
            "previews": []
        }

        self.storage.log_validation(
            session_id=session_id,
            validation_type="template_compliance_revision",
            passed=comp["compliant"] and is_safe,
            details={
                "word_count": comp["word_count"],
                "violations": comp["violations"],
                "user_feedback": user_feedback
            }
        )

        return {
            "action": "template_updated",
            "templates": [updated_template],
            "slots": slots,
            "changes_summary": parsed.get("changes_summary") or "Template successfully revised according to your feedback."
        }

    async def generate_message_from_form(
        self,
        session_id: str,
        business_type: str,
        context: str,
        organization_name: str = "",
        link_or_contact: str = "",
        address_or_location: str = "",
        event_date: str = "",
        channel_format: str = "sms",
        tone: str = "Direct & Authoritative 📜",
        template_request: str = "",
        additional_info: Optional[List[str]] = None,
        skipped_fields: Optional[List[str]] = None,
        dynamic_fields: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Generates a tailored message directly from user-filled form details, strictly complying with format, tone, and exclusions."""
        skipped_fields = [str(s).lower().strip() for s in (skipped_fields or [])]
        additional_info = additional_info or []
        dynamic_fields = dynamic_fields or {}
        is_email = str(channel_format).lower() == "email"
        word_limit = self.config.orchestrator.word_limit
        optout_text = self.config.orchestrator.optout_text

        effective_org = "" if ("organization_name" in skipped_fields or not organization_name.strip()) else clean_organization_name(organization_name.strip(), business_type)
        effective_link = "" if ("link_or_contact" in skipped_fields or not link_or_contact.strip()) else link_or_contact.strip()

        effective_date = event_date.strip() if event_date and event_date.strip() else ""
        if not effective_date:
            d_val = dynamic_fields.get("date") or dynamic_fields.get("event_date")
            effective_date = (d_val.get("value") if isinstance(d_val, dict) else str(d_val)) if d_val else ""

        effective_address = address_or_location.strip() if address_or_location and address_or_location.strip() else ""
        if not effective_address:
            a_val = dynamic_fields.get("location") or dynamic_fields.get("address") or dynamic_fields.get("address_or_location")
            effective_address = (a_val.get("value") if isinstance(a_val, dict) else str(a_val)) if a_val else ""

        dyn_details_list = []
        for k, v in dynamic_fields.items():
            if k in ["location", "address", "date", "event_date"]:
                continue
            if isinstance(v, dict):
                lbl = v.get("label", k.replace("_", " ").title())
                val = v.get("value", "")
            else:
                lbl = k.replace("_", " ").title()
                val = str(v)
            if val and str(val).strip():
                dyn_details_list.append(f"- {lbl}: {val.strip()}")
        dyn_details_str = "\n".join(dyn_details_list) if dyn_details_list else "None"

        fmt_desc = "EMAIL (Subject Line + Email Body)" if is_email else "SMS / WhatsApp (Single Text Message)"
        system_msg = self.prompts.load_prompt("system")

        prompt = f"""You are the Template Writer Module of CampaignForge.
Generate a single high-converting, strictly compliant CRM message template (exactly 1 message).

CAMPAIGN CONFIGURATION:
- Domain / Business Type: {business_type or "General Business"}
- Campaign Context / Offer: {context}
- Message Format: {fmt_desc}
- Tone Required: {tone}
- Organization Name: {effective_org if effective_org else "[EXPLICITLY SKIPPED BY USER - DO NOT MENTION OR PLACEHOLD]"}
- Event Date & Timing: {effective_date if effective_date else "Not specified"}
- Physical Address / Location: {effective_address if effective_address else "Not specified"}
- Link / Contact: {effective_link if effective_link else "[EXPLICITLY SKIPPED BY USER - DO NOT MENTION OR PLACEHOLD]"}
- Specific User Request: {template_request if template_request else "None"}
- Specific Campaign Details / Requirements:
{dyn_details_str}
- Additional Notes / Details: {"; ".join(additional_info) if additional_info else "None"}
- Skipped Fields: {", ".join(skipped_fields) if skipped_fields else "None"}

CRITICAL COMPLIANCE & TONE RULES:
1. FORMAT:
   {"- You MUST format the output starting with 'Subject: <Impactful Subject Line>\\n\\n' followed by the body paragraphs." if is_email else "- Single concise text message."}
   - The message MUST end with the exact opt-out text: "{optout_text}"
2. TONE COMPLIANCE:
   - Adhere strictly to the requested tone: "{tone}".
   - If 'aggressive', 'urgent', or 'stern': use firm, direct, uncompromising language emphasizing immediate deadlines, late fees, or service cut-off. Do not make unlawful threats.
   - If 'polite', 'professional', or 'friendly': adopt that specific courteous or warm register.
3. SKIPPED FIELDS RULE (ZERO BROKEN PLACEHOLDERS):
   - If Organization Name is skipped: DO NOT use {{{{organization_name}}}} or fake names or [Company Name]. Write the copy naturally without mentioning a specific business name (e.g. 'Dear Customer, your electricity bill is overdue...').
   - If Link is skipped: DO NOT use {{{{link}}}} or fake links or [Link]. Direct recipient naturally without a URL (e.g. 'Please settle your bill through your customer portal or contact support.').
4. ZERO FABRICATED STATS OR DISCOUNTS:
   - Only include figures/discounts if explicitly stated in context or specific request.
5. ZERO UNRESOLVED SQUARE-BRACKET PLACEHOLDERS:
   - NEVER output literal unresolved brackets like '[Date]', '[Time]', '[Location]', '[Link]' in the message.
   - If a date is provided in configuration ({effective_date}), use that date. If no date is provided, write natural phrasing like 'this upcoming weekend' or 'our upcoming event' instead of leaving '[Date]'!
   - If an address is provided ({effective_address}), incorporate it naturally.
6. STRICT LENGTH: Under {word_limit} words total.

OUTPUT FORMAT (Strict JSON):
```json
{{
  "sequence_summary": "Single message campaign for {business_type}",
  "recommended_schedule": "Immediate",
  "templates": [
    {{
      "message_index": 1,
      "stage": "Announcement",
      "tone": "{tone}",
      "channel_format": "{'email' if is_email else 'sms'}",
      "body": "...",
      "tags_used": ["customer_name"],
      "estimated_word_count": 45
    }}
  ]
}}
```"""

        parsed = None
        for attempt in range(self.config.orchestrator.max_retries + 1):
            llm_res = await self.llm.call_llm(
                session_id=session_id,
                purpose="form_template_write",
                system_prompt=f"{system_msg}\n\n{prompt}",
                user_message="Generate the compliant message template matching these exact specifications.",
                step_id="template_writer",
                json_mode=True
            )
            if llm_res.get("status") == "success":
                data = parse_json_robust(llm_res.get("raw_response", ""))
                if data and "templates" in data and len(data["templates"]) > 0:
                    parsed = data
                    break

        if not parsed or not parsed.get("templates"):
            if is_email:
                sub = f"URGENT: Action Required Regarding Your Electricity Bill" if "electric" in (business_type+context).lower() else f"Important Notice: {context}"
                if effective_org:
                    sub = f"{effective_org}: {sub}"
                body_lines = [
                    f"Subject: {sub}\n",
                    f"Dear Customer,\n",
                    f"This is an important update regarding your {business_type.lower()} campaign."
                ]
                if dyn_details_list:
                    for d in dyn_details_list:
                        body_lines.append(d)
                if template_request:
                    body_lines.append(f"\n{template_request}")
                if additional_info:
                    for note in additional_info:
                        body_lines.append(f"- {note}")
                if effective_link:
                    body_lines.append(f"\nPlease access details or take action here: {effective_link}")
                else:
                    body_lines.append(f"\nPlease take action through your official portal or contact our support.")
                body_lines.append(f"\n\n{optout_text}")
                body_text = "\n".join(body_lines)
            else:
                prefix = f"{effective_org}: " if effective_org else ""
                urg = "URGENT: " if "aggressive" in tone.lower() or "urgent" in tone.lower() else ""
                cta = f" Details: {effective_link}." if effective_link else ""
                extra = f" {template_request}" if template_request else ""
                dyn_snippet = f" ({'; '.join([d.lstrip('- ') for d in dyn_details_list[:2]])})" if dyn_details_list else ""
                body_text = f"{urg}{prefix}{context}{dyn_snippet}.{extra}{cta} Reply STOP to unsubscribe."

            parsed = {
                "sequence_summary": f"Single message campaign for {business_type}",
                "recommended_schedule": "Immediate",
                "templates": [{
                    "message_index": 1,
                    "stage": "Announcement",
                    "tone": tone,
                    "channel_format": "email" if is_email else "sms",
                    "body": body_text,
                    "tags_used": ["customer_name"] if "{{customer_name}}" in body_text else [],
                    "estimated_word_count": len(body_text.split())
                }]
            }

        tmpl = parsed["templates"][0]
        body = tmpl.get("body", "").strip()

        # Clean up any raw square bracket placeholders like [Date], [Time], [Location], [Address]
        if effective_date:
            body = re.sub(r'\[Date\]|\[date\]', effective_date, body)
            body = re.sub(r'\[Time\]|\[time\]', effective_date, body)
        else:
            body = re.sub(r'on\s+\[Date\]', 'this upcoming weekend', body, flags=re.IGNORECASE)
            body = re.sub(r'by\s+\[Date\]', 'soon', body, flags=re.IGNORECASE)
            body = re.sub(r'\[Date\]|\[date\]', 'our upcoming event', body)
            body = re.sub(r'\[Time\]|\[time\]', '', body)

        if effective_address:
            body = re.sub(r'\[Location\]|\[Address\]|\[address\]|\[location\]', effective_address, body)
        else:
            body = re.sub(r'at\s+\[Location\]|at\s+\[Address\]', 'at our community location', body, flags=re.IGNORECASE)
            body = re.sub(r'\[Location\]|\[Address\]|\[address\]|\[location\]', '', body)

        # Enforce organization name if provided and not skipped
        if effective_org and effective_org.lower() not in body.lower():
            if is_email:
                if "Subject:" in body:
                    body = body.replace("Subject: ", f"Subject: {effective_org} - ", 1)
                else:
                    body = f"Subject: {effective_org} Notice\n\n{body}"
            else:
                body = f"{effective_org}: {body}"

        if effective_link and effective_link.lower() not in body.lower():
            clean_body = body.replace(optout_text, "").rstrip()
            if is_email:
                body = f"{clean_body}\n\nFor details or enquiries: {effective_link}"
            else:
                body = f"{clean_body} Info/Contact: {effective_link}"

        clean_optout = optout_text.strip()
        if not body.strip().endswith(clean_optout):
            body = f"{body.rstrip()}\n\n{clean_optout}"

        tmpl["body"] = body
        comp = check_template_compliance(
            body=body,
            business_name=effective_org,
            word_limit=word_limit,
            optout_text=optout_text,
            link_or_contact=effective_link
        )
        tmpl["word_count"] = comp["word_count"]
        tmpl["word_limit"] = word_limit
        tmpl["compliant"] = comp["compliant"]
        tmpl["violations"] = comp["violations"]

        safety_eval = await self.safety.evaluate_safety(session_id, body, "template_generation")
        is_safe = safety_eval.get("allowed", True)
        tmpl["safety_status"] = "safe" if is_safe else "blocked"
        if not is_safe:
            tmpl["compliant"] = False

        return {
            "templates": [tmpl],
            "recommended_schedule": parsed.get("recommended_schedule", "Immediate"),
            "compliant": tmpl["compliant"],
            "violations": tmpl["violations"]
        }

    async def generate_tone_candidate_previews(
        self,
        session_id: str,
        business_type: str,
        context: str,
        organization_name: str = "",
        link_or_contact: str = "",
        address_or_location: str = "",
        event_date: str = "",
        channel_format: str = "sms",
        candidate_tones: Optional[List[str]] = None,
        user_feedback: str = "",
        dynamic_fields: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Generates collapsible preview message templates across candidate tones or specified custom tones.
        Allows users to see full message copy in each tone, offer suggestions, and re-explore.
        """
        if user_feedback and user_feedback.strip():
            custom_tone = user_feedback.strip()
            target_tones = [
                f"{custom_tone.title()} ✨" if not any(c in custom_tone for c in ("✨", "🚨", "📜", "💼", "❤️", "🌟")) else custom_tone,
                "Direct & High-Converting 🎯",
                "Warm & Engaging 🌟"
            ]
        elif candidate_tones and len(candidate_tones) >= 2:
            target_tones = candidate_tones[:4]
        else:
            target_tones = [
                "Direct & Authoritative 📜",
                "Professional & Polite 💼",
                "Urgent & Action-Oriented 🚨"
            ]

        def get_tone_description(t_name: str) -> str:
            t_lower = t_name.lower()
            if "urgent" in t_lower or "aggressive" in t_lower or "final" in t_lower:
                return "Firm, high-priority language highlighting strict deadlines and immediate action."
            elif "heartfelt" in t_lower or "inspiring" in t_lower or "warm" in t_lower:
                return "Empathetic, community-driven appeal connecting personally with recipients."
            elif "fomo" in t_lower or "energy" in t_lower or "special" in t_lower or "exclusive" in t_lower:
                return "Exciting, fast-paced register designed to drive immediate conversions."
            elif "caring" in t_lower or "reassuring" in t_lower:
                return "Supportive, attentive tone reassuring recipients and providing clear guidance."
            else:
                return "Clear, trustworthy register delivering essential campaign updates without ambiguity."

        tasks = [
            self.generate_message_from_form(
                session_id=session_id,
                business_type=business_type,
                context=context,
                organization_name=organization_name,
                link_or_contact=link_or_contact,
                address_or_location=address_or_location,
                event_date=event_date,
                channel_format=channel_format,
                tone=t,
                dynamic_fields=dynamic_fields
            )
            for t in target_tones
        ]

        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        tone_cards = []
        for idx, (t_name, res) in enumerate(zip(target_tones, results_list), start=1):
            if isinstance(res, Exception) or not isinstance(res, dict) or not res.get("templates"):
                body_sample = f"{organization_name + ': ' if organization_name else ''}{context}. Contact: {link_or_contact or 'Support'}. Reply STOP to unsubscribe."
                tone_cards.append({
                    "id": f"tone_{idx}",
                    "name": t_name,
                    "description": get_tone_description(t_name),
                    "preview": body_sample,
                    "word_count": len(body_sample.split()),
                    "compliant": True
                })
            else:
                tmpl = res["templates"][0]
                tone_cards.append({
                    "id": f"tone_{idx}",
                    "name": t_name,
                    "description": get_tone_description(t_name),
                    "preview": tmpl["body"],
                    "word_count": tmpl["word_count"],
                    "compliant": tmpl["compliant"]
                })

        return tone_cards

    async def analyze_campaign_interaction(
        self,
        session_id: str,
        user_msg: str,
        domain: str,
        current_slots: Dict[str, Any],
        collected_details: Dict[str, Any],
        conversation_history: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Dynamically analyzes user input in the context of the conversation.
        Extracts structured slots (clean entity names, CTA, links/phones, locations, offers).
        Determines if sufficient details have been gathered or if an intelligent follow-up question is needed.
        """
        # 1. Safety check
        safety_eval = await self.safety.evaluate_safety(session_id, user_msg, "campaign_interaction")
        if not safety_eval.get("allowed", False):
            return {
                "blocked": True,
                "reason": safety_eval.get("reason"),
                "alternative": safety_eval.get("suggested_alternative"),
                "is_rate_limit": safety_eval.get("is_rate_limit", False)
            }

        msg_lower = user_msg.lower().strip()
        explicit_conclude = any(w in msg_lower for w in [
            "that's all", "thats all", "ready", "generate", "confirm", "show card",
            "generate card", "done", "no more", "proceed", "looks good", "enough",
            "generate message", "make message", "just create", "make card"
        ])

        system_prompt = (
            "You are the Conversational Campaign Intelligence Engine of CampaignForge.\n"
            "Your task is to analyze the user's latest input, extract structured CRM campaign slots, "
            "and determine whether to ask a clarifying follow-up question or present the final confirmation card.\n\n"
            "CRITICAL RULES:\n"
            "1. NEVER set organization_name to a rambling sentence or invent fake names. Extract a clean, concise name explicitly given by the user, or leave it empty string \"\" if unstated.\n"
            "2. EXTRACT ADDRESS & DATE: Actively extract physical addresses/locations (e.g. '6th cross 10th main near the Christmas tree') into address_or_location, and event dates/times into event_date.\n"
            "3. NO FAKE / SYNTHETIC OPTIONS FOR OPEN-ENDED QUESTIONS: For questions asking for Organization Name, Physical Address/Location, Event Date/Time, or Phone/URL/Link, the 'options' field MUST BE AN EMPTY ARRAY []! NEVER generate synthetic choices like ['Option A: Provide the address'] or fake organization names!\n"
            "4. NEVER RE-ASK EXISTING DETAILS: If address, date, or business name is already in Gathered Slots or user message, DO NOT ask for it again.\n"
            "5. If sufficient details are gathered (or user wants to proceed), set 'action': 'show_card'.\n"
            "6. OUTPUT STRICT JSON ONLY."
        )

        slots_summary = {
            "domain": domain,
            "existing_collected": collected_details,
            "slots": {k: v.get("value") for k, v in current_slots.items() if isinstance(v, dict)}
        }

        user_prompt = f"""Conversation Domain: {domain}
Current Gathered Slots: {json.dumps(slots_summary)}
Latest User Message: "{user_msg}"

Output JSON matching this exact structure:
{{
  "extracted": {{
    "organization_name": "clean concise name or empty string if unstated",
    "campaign_goal": "clean concise campaign goal/event name",
    "event_date": "event date/time or empty string",
    "call_to_action": "clean action or contact instruction",
    "link_or_contact": "phone number, URL, or empty string",
    "address_or_location": "physical address/location or empty string",
    "channel_format": "sms or email",
    "suggested_tone": "appropriate tone for domain and goal",
    "dynamic_fields": {{
      "event_name": "...",
      "key_highlights": "..."
    }}
  }},
  "action": "ask_question or show_card",
  "reasoning": "brief explanation",
  "followup": {{
    "acknowledgment": "brief warm acknowledgment of user's context",
    "question": "clear targeted follow-up question",
    "options": []
  }}
}}"""

        parsed = None
        for attempt in range(self.config.orchestrator.max_retries + 1):
            llm_res = await self.llm.call_llm(
                session_id=session_id,
                purpose="campaign_analysis",
                system_prompt=system_prompt,
                user_message=user_prompt,
                step_id="campaign_analyzer",
                json_mode=True
            )
            if llm_res.get("status") == "success":
                data = parse_json_robust(llm_res.get("raw_response", ""))
                if data and isinstance(data, dict) and "action" in data:
                    parsed = data
                    break

        # Fallback heuristic if LLM unavailable or malformed
        if not parsed:
            parsed = self._fallback_campaign_analysis(user_msg, domain, current_slots, collected_details, explicit_conclude)

        # Enforce clean organization_name and empty options for open-ended questions
        ext = parsed.get("extracted", {})
        raw_org = ext.get("organization_name", "")
        if raw_org:
            ext["organization_name"] = clean_organization_name(raw_org, domain)

        followup = parsed.get("followup", {})
        if followup and followup.get("question"):
            q_lower = followup["question"].lower()
            if any(k in q_lower for k in [
                "organization", "business name", "company name", "clinic name", "store name",
                "address", "location", "where", "date", "when", "timing", "time",
                "phone", "number", "url", "link", "website", "contact", "portal"
            ]):
                followup["options"] = []

        # If user explicitly said enough/ready, always show card
        if explicit_conclude:
            parsed["action"] = "show_card"

        return parsed

    def _fallback_campaign_analysis(
        self,
        user_msg: str,
        domain: str,
        current_slots: Dict[str, Any],
        collected_details: Dict[str, Any],
        explicit_conclude: bool
    ) -> Dict[str, Any]:
        """Rule-based fallback when LLM is unavailable."""
        msg = user_msg.strip()
        msg_lower = msg.lower()

        # Extract phone / url
        phone = ""
        m_phone = re.search(r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", msg)
        if m_phone:
            phone = m_phone.group(0).strip()
        elif re.search(r"\b\d{5}\s+\d{5}\b", msg):
            phone = re.search(r"\b\d{5}\s+\d{5}\b", msg).group(0).strip()

        url = ""
        m_url = re.search(r"https?://[^\s]+", msg)
        if m_url:
            url = m_url.group(0).strip()

        link_or_contact = url or phone or collected_details.get("link_or_contact", "")

        # Extract address
        address = ""
        m_addr = re.search(r"(?:address|located|location|near|at)\s*[:=\-]?\s*([^,\n]+(?:cross|main|street|road|st|ave|blvd|lane)[^,\n]*)", msg, re.IGNORECASE)
        if m_addr:
            address = m_addr.group(1).strip()

        # Clean organization name
        org_name = collected_details.get("organization_name") or clean_organization_name(msg, domain)
        channel = "email" if "email" in msg_lower else (collected_details.get("channel_format") or "sms")

        extracted = {
            "organization_name": org_name,
            "campaign_goal": collected_details.get("campaign_goal") or msg[:100],
            "call_to_action": collected_details.get("call_to_action") or "Contact us for details",
            "link_or_contact": link_or_contact,
            "address_or_location": address,
            "channel_format": channel,
            "suggested_tone": "Professional & Polite 💼",
            "dynamic_fields": {}
        }

        if explicit_conclude or (org_name and link_or_contact):
            return {
                "extracted": extracted,
                "action": "show_card",
                "reasoning": "Sufficient details present or user requested conclusion."
            }

        if not link_or_contact:
            return {
                "extracted": extracted,
                "action": "ask_question",
                "reasoning": "Need contact details or website link.",
                "followup": {
                    "acknowledgment": "Got it! That helps clarify your campaign.",
                    "question": "What website link, contact phone number, or store location should recipients use?",
                    "options": [
                        "1-800-555-0199 (Customer Support)",
                        "https://service.link/portal",
                        "Store Location / Address",
                        "Other (Type custom answer)"
                    ]
                }
            }

        return {
            "extracted": extracted,
            "action": "show_card",
            "reasoning": "Sufficient details gathered."
        }

def extract_form_context(user_msg: str, existing_slots: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Extracts initial form parameters from user message or existing slots."""
    existing_slots = existing_slots or {}
    msg_lower = user_msg.lower()

    channel_format = "email" if any(w in msg_lower for w in ["email", "e-mail", "mail format", "in email"]) else "sms"

    tone = "Direct & Authoritative 📜"
    if any(w in msg_lower for w in ["aggressive", "harsh", "stern", "forceful"]):
        tone = "Aggressive & Urgent 🚨"
    elif any(w in msg_lower for w in ["urgent", "rush", "immediate", "asap"]):
        tone = "Aggressive & Urgent 🚨"
    elif any(w in msg_lower for w in ["friendly", "warm", "casual", "cheerful"]):
        tone = "Friendly & Casual 🌟"
    elif any(w in msg_lower for w in ["polite", "professional", "formal", "courteous"]):
        tone = "Professional & Polite 💼"

    b_type = "General Business"
    if any(w in msg_lower for w in ["electric", "power", "utility", "energy", "bill", "current", "disconnection"]):
        b_type = "Utility & Energy"
    elif any(w in msg_lower for w in ["clinic", "dental", "doctor", "health", "hospital", "patient", "appointment"]):
        b_type = "Healthcare & Clinic"
    elif any(w in msg_lower for w in ["school", "course", "fee", "tuition", "student", "class", "admission"]):
        b_type = "Education & Tuition"
    elif any(w in msg_lower for w in ["sale", "discount", "store", "retail", "cloth", "shoe", "fashion"]):
        b_type = "Retail & E-Commerce"
    elif any(w in msg_lower for w in ["saas", "software", "subscription", "trial", "renewal"]):
        b_type = "SaaS & Subscription"

    org_name = ""
    if existing_slots.get("business_name", {}).get("value"):
        org_name = str(existing_slots["business_name"]["value"])
    else:
        m_for = re.search(r"\b(?:for|from|at)\s+([A-Z][A-Za-z0-9\s&]{2,25})\b", user_msg)
        if m_for:
            org_name = m_for.group(1).strip()
        elif "electricity company" in msg_lower:
            org_name = "Electricity Company"
        elif "power board" in msg_lower:
            org_name = "Metro Power Board"

    link_val = ""
    if existing_slots.get("link_or_contact", {}).get("value"):
        link_val = str(existing_slots["link_or_contact"]["value"])
    else:
        m_url = re.search(r"https?://[^\s]+", user_msg)
        if m_url:
            link_val = m_url.group(0).strip()
        else:
            m_phone = re.search(r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", user_msg)
            if m_phone:
                link_val = m_phone.group(0).strip()

    template_request = ""
    m_disc = re.search(r"(\d+%\s*(?:off|discount)[^,.]*)", user_msg, re.IGNORECASE)
    if m_disc:
        template_request = m_disc.group(1).strip()

    return {
        "context": user_msg,
        "business_type": b_type,
        "organization_name": org_name,
        "link_or_contact": link_val,
        "channel_format": channel_format,
        "tone": tone,
        "template_request": template_request,
        "additional_info": []
    }

_orchestrator_engine: Optional[OrchestratorEngine] = None

def get_orchestrator_engine() -> OrchestratorEngine:
    global _orchestrator_engine
    if _orchestrator_engine is None:
        _orchestrator_engine = OrchestratorEngine()
    return _orchestrator_engine
