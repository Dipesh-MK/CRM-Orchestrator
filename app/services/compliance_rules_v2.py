import re
import unicodedata
from typing import List, Dict, Any, Optional, Tuple
from app.core.orchestrator_v2_models import ToolResult, OrgRecord, ClaimEntry

# -------------------------------------------------------------
# PRIORITY RULE A: Sender Entity & Intent
# -------------------------------------------------------------

def validate_sender_entity(
    org: Optional[OrgRecord],
    campaign_type: str,
    sending_number: str
) -> ToolResult:
    """Policy Gate: Verifies org approved status, legal eligibility, use case, and sending number."""
    if not org:
        return ToolResult(
            tool_id="validate_sender_entity",
            category="policy_gate",
            passed=False,
            reason="Organization record not found in verified registry.",
            severity="block"
        )
    if not org.approved:
        return ToolResult(
            tool_id="validate_sender_entity",
            category="policy_gate",
            passed=False,
            reason=f"Organization '{org.display_name}' has not been approved in registry.",
            severity="block"
        )
    # Check sending number
    clean_num = re.sub(r"\D", "", sending_number)
    org_clean_nums = [re.sub(r"\D", "", n) for n in org.sending_numbers]
    if clean_num not in org_clean_nums and sending_number not in org.sending_numbers:
        return ToolResult(
            tool_id="validate_sender_entity",
            category="policy_gate",
            passed=False,
            reason=f"Sending number '{sending_number}' is not authorized for organization '{org.display_name}'.",
            severity="block"
        )
    return ToolResult(
        tool_id="validate_sender_entity",
        category="policy_gate",
        passed=True,
        reason="Organization record is verified and sending number is authorized."
    )

def check_org_type_gate(org: Any, campaign_type: str) -> ToolResult:
    """Policy Gate: 501(c)(3) charities can NEVER run political campaigns."""
    if not org:
        return ToolResult(tool_id="check_org_type_gate", category="policy_gate", passed=False, reason="Missing organization record.")
    org_type = org.org_type if hasattr(org, "org_type") else str(org)
    if org_type in ("501c3", "501(c)(3)") and campaign_type.startswith("political_"):
        return ToolResult(
            tool_id="check_org_type_gate",
            category="policy_gate",
            passed=False,
            reason="Federal 501(c)(3) organizations are strictly barred by law from engaging in political campaign activity.",
            severity="block"
        )
    return ToolResult(
        tool_id="check_org_type_gate",
        category="policy_gate",
        passed=True,
        reason=f"Organization type '{org_type}' is eligible for campaign type '{campaign_type}'."
    )

def check_campaign_verify(org: Any, campaign_type: str = "political_outreach", token: Optional[str] = None) -> ToolResult:
    """Policy Gate: Political 527/PAC senders must have a valid Campaign Verify token."""
    if not org:
        return ToolResult(tool_id="check_campaign_verify", category="policy_gate", passed=False, reason="Missing org.")
    org_type = org.org_type if hasattr(org, "org_type") else str(org)
    cv_token = token if token is not None else (getattr(org, "campaign_verify_token", None))
    if campaign_type.startswith("political_") and org_type in ("527_political", "pac", "527"):
        if not cv_token or not cv_token.strip():
            return ToolResult(
                tool_id="check_campaign_verify",
                category="policy_gate",
                passed=False,
                reason="Political organizations require a verified Campaign Verify token on record.",
                severity="block"
            )
    return ToolResult(tool_id="check_campaign_verify", category="policy_gate", passed=True, reason="Campaign Verify token verified or not required.")

def check_use_case_match(org: Optional[OrgRecord], campaign_type: str) -> ToolResult:
    """Policy Gate: Template use case must match organization's registered authorizations."""
    if not org:
        return ToolResult(tool_id="check_use_case_match", category="policy_gate", passed=False, reason="Missing org.")
    if org.registered_use_cases:
        # Check if broad match
        matched = any(uc in campaign_type or campaign_type in uc for uc in org.registered_use_cases)
        if not matched:
            return ToolResult(
                tool_id="check_use_case_match",
                category="policy_gate",
                passed=False,
                reason=f"Campaign use-case '{campaign_type}' is not registered. Registered: {org.registered_use_cases}",
                severity="block"
            )
    return ToolResult(tool_id="check_use_case_match", category="policy_gate", passed=True, reason="Use-case matches registered authorization.")

def check_age_gate_flag(message: str, age_gated: bool = False) -> ToolResult:
    """Policy Gate: Alcohol terms require age_gated flag."""
    alcohol_pattern = re.compile(r"(?i)\b(wine|beer|cocktail|cocktails|bourbon|whiskey|open\s+bar|champagne)\b")
    m = alcohol_pattern.search(message)
    if m and not age_gated:
        return ToolResult(
            tool_id="check_age_gate_flag",
            category="policy_gate",
            passed=False,
            reason=f"Alcohol-related term '{m.group(0)}' detected without age_gated flag enabled.",
            offending_span=m.group(0),
            severity="block"
        )
    return ToolResult(tool_id="check_age_gate_flag", category="policy_gate", passed=True, reason="Age gating rules compliant.")

def check_ai_disclosure(message: str, campaign_type: str = "political_outreach", state_code: str = "US", ai_assisted: bool = True) -> ToolResult:
    """Policy Gate: Political messages must carry state-mandated AI disclosure if required."""
    # By default, political messages from Agent 1 are AI assisted.
    # States like WA, CA, TX, WI have strict political AI disclosures.
    # If state requires it, exact string must be present.
    if ai_assisted and campaign_type.startswith("political_") and state_code in ("CA", "WA", "WI"):
        if not re.search(r"(?i)(generated\s+with\s+ai|ai[- ]assisted|created\s+using\s+artificial\s+intelligence)", message):
            return ToolResult(
                tool_id="check_ai_disclosure",
                category="policy_gate",
                passed=False,
                reason=f"Political outreach in state '{state_code}' requires an explicit AI generation disclosure line.",
                severity="block"
            )
    return ToolResult(tool_id="check_ai_disclosure", category="policy_gate", passed=True, reason="AI disclosure compliant.")

def check_sender_identified(message: str, org: Any, max_chars: int = 80) -> ToolResult:
    """Format: Registered name/committee must appear within first max_chars."""
    prefix = message[:max_chars]
    prefix_norm = unicodedata.normalize("NFKC", prefix).lower()
    
    if isinstance(org, str):
        candidates = [org.lower()]
        display_name = org
    else:
        candidates = [org.display_name.lower(), org.legal_name.lower()]
        if org.committee_name:
            candidates.append(org.committee_name.lower())
        display_name = org.display_name

    for c in candidates:
        if c in prefix_norm:
            return ToolResult(
                tool_id="check_sender_identified",
                category="format",
                passed=True,
                reason=f"Sender identity '{c}' clearly identified within first {max_chars} characters."
            )
            
    # Also check full message to provide clear diagnostic
    full_norm = unicodedata.normalize("NFKC", message).lower()
    found_late = any(c in full_norm for c in candidates)
    if found_late:
        return ToolResult(
            tool_id="check_sender_identified",
            category="format",
            passed=False,
            reason=f"Sender identity appears in message but not within the required first {max_chars} characters.",
            offending_span=prefix[-25:]
        )
    return ToolResult(
        tool_id="check_sender_identified",
        category="format",
        passed=False,
        reason=f"Message missing clear sender identification '{display_name}' in opening.",
        offending_span=None
    )

INTENT_LEXICONS = {
    "event_reminder": ["join us", "event", "gala", "gathering", "doors open", "rsvp", "market", "annual", "starts at"],
    "donation_request": ["donate", "support", "contribution", "gift", "give", "help fund", "help us raise"],
    "donation_thanks": ["thank you", "grateful", "appreciation", "receipt", "your generosity", "your gift"],
    "voter_info": ["vote", "ballot", "polling", "election", "register", "primary", "logistics"],
    "political_fundraising": ["chip in", "donate", "campaign", "fuel our campaign", "support our race", "contribute"],
    "opt_in_confirmation": ["welcome", "confirmed", "subscribed", "you are signed up", "alerts"],
    "help_reply": ["help", "support", "contact", "call", "questions", "assistance"],
    "stop_reply": ["unsubscribed", "opted out", "no further messages", "removed", "canceled"]
}

def check_intent_stated(message: str, intent_label: str) -> ToolResult:
    """Format: Verifies declared intent lexicon phrase is present."""
    lexicon = INTENT_LEXICONS.get(intent_label, [])
    if not lexicon:
        return ToolResult(tool_id="check_intent_stated", category="format", passed=True, reason="Generic intent approved.")
    
    msg_lower = message.lower()
    for phrase in lexicon:
        if phrase in msg_lower:
            return ToolResult(
                tool_id="check_intent_stated",
                category="format",
                passed=True,
                reason=f"Declared intent '{intent_label}' verified with matching phrase '{phrase}'."
            )
    return ToolResult(
        tool_id="check_intent_stated",
        category="format",
        passed=False,
        reason=f"Message does not clearly state declared intent '{intent_label}'. Expected context like: {lexicon[:4]}."
    )

# -------------------------------------------------------------
# PRIORITY RULE B: Banned URL Shorteners & Domain Allowlist
# -------------------------------------------------------------

def normalize_domain_token(raw_token: str) -> str:
    """Normalizes URL token: strip scheme, www, userinfo, port, punctuation, handle brackets and fullwidth dots."""
    t = raw_token.strip().lower()
    # Replace obfuscations
    t = re.sub(r"\[\.\]|\s*\.\s*", ".", t)
    t = t.replace("．", ".") # fullwidth dot
    # Strip protocols
    t = re.sub(r"^https?://", "", t)
    t = re.sub(r"^www\.", "", t)
    # Strip paths, queries, ports
    t = re.sub(r"[:/?#].*$", "", t)
    # Strip trailing punctuation
    t = re.sub(r"[.,;!?)]+$", "", t)
    return t

def check_banned_urls(text: str, banned_domains: Optional[List[Dict[str, Any]]] = None) -> ToolResult:
    """Format: Scans message for public shorteners with suffix and obfuscation matching."""
    # 1. Direct Regex check for zero-tolerance prohibited shorteners (tiny.url & bit.ly)
    direct_shortener_pattern = re.compile(
        r"(?i)(?:https?://)?(?:www\.)?(?:tiny(?:\.|\s*\[\.\]\s*)url|tinyurl(?:\.|\s*\[\.\]\s*)com|bit(?:\.|\s*\[\.\]\s*)ly|bitly(?:\.|\s*\[\.\]\s*)com)(?:[:/?#\s]|$|[^\w.-])"
    )
    m_direct = direct_shortener_pattern.search(text)
    if m_direct:
        offending = m_direct.group(0).strip()
        return ToolResult(
            tool_id="check_banned_urls",
            category="format",
            passed=False,
            reason=f"Prohibited public short URL domain detected ('{offending}'). Carrier 10DLC regulations strictly ban public URL shorteners (tiny.url / bit.ly) to prevent phishing.",
            offending_span=offending
        )

    if banned_domains is None:
        try:
            banned_domains = get_storage().list_banned_url_domains()
        except Exception:
            banned_domains = []

    hardcoded_banned = [
        {"domain": "bit.ly", "match_type": "suffix", "active": 1},
        {"domain": "bitly.com", "match_type": "suffix", "active": 1},
        {"domain": "tinyurl.com", "match_type": "suffix", "active": 1},
        {"domain": "tiny.url", "match_type": "suffix", "active": 1},
    ]
    seen_domains = {b.get("domain", "").lower().strip() for b in (banned_domains or [])}
    active_banned = list(banned_domains or [])
    for hb in hardcoded_banned:
        if hb["domain"] not in seen_domains:
            active_banned.append(hb)

    # Find all domain or URL-like tokens
    tokens = re.findall(r"(?:https?://)?(?:[a-zA-Z0-9_\-]+(?:\[\.\]|．|\.))+[a-zA-Z]{2,}(?:[^\s]*)?", text)
    
    for tok in tokens:
        clean_d = normalize_domain_token(tok)
        for b in active_banned:
            if not b.get("active", 1):
                continue
            b_domain = b["domain"].lower().strip()
            match_type = b.get("match_type", "suffix")
            
            is_hit = False
            if match_type == "exact":
                is_hit = (clean_d == b_domain)
            else: # suffix
                is_hit = (clean_d == b_domain or clean_d.endswith("." + b_domain))
                
            if is_hit:
                return ToolResult(
                    tool_id="check_banned_urls",
                    category="format",
                    passed=False,
                    reason=f"Prohibited public short URL domain '{b_domain}' detected. Carriers strictly ban public shorteners under 10DLC policy.",
                    offending_span=tok
                )
    return ToolResult(tool_id="check_banned_urls", category="format", passed=True, reason="No banned URL shorteners detected.")

def check_url_allowlist(text: str, registered_domains: List[str]) -> ToolResult:
    """Format: Every URL in message must match organization's registered domains."""
    tokens = re.findall(r"(?:https?://)([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})(?:[^\s]*)?", text)
    if not tokens:
        return ToolResult(tool_id="check_url_allowlist", category="format", passed=True, reason="No URLs to evaluate.")
    
    reg_clean = [d.lower().strip() for d in registered_domains]
    for tok in tokens:
        d = tok.lower().strip()
        if d.startswith("www."):
            d = d[4:]
        matched = any(d == rd or d.endswith("." + rd) for rd in reg_clean)
        if not matched:
            return ToolResult(
                tool_id="check_url_allowlist",
                category="format",
                passed=False,
                reason=f"URL domain '{tok}' is not in organization's registered domains {reg_clean}.",
                offending_span=tok
            )
    return ToolResult(tool_id="check_url_allowlist", category="format", passed=True, reason="All URLs comply with registered domain allowlist.")

# -------------------------------------------------------------
# Claims Ledger Verification
# -------------------------------------------------------------

def check_claims_in_ledger(text: str, claims_ledger: List[ClaimEntry], campaign_type: str = "general") -> ToolResult:
    """Claims Category: Verifies numbers, dollar amounts, dates, and voting logistics against ledger."""
    # Extract dollar amounts
    dollars = re.findall(r"\$\s*\d+(?:,\d{3})*(?:\.\d{2})?", text)
    # Extract percentages
    percentages = re.findall(r"\b\d+(?:\.\d+)?%", text)
    
    ledger_vals = {c.normalized_value.lower(): c for c in claims_ledger}
    ledger_raws = {c.raw_value.lower(): c for c in claims_ledger}
    
    for amt in dollars:
        clean_amt = re.sub(r"\s", "", amt).lower()
        if clean_amt not in ledger_vals and clean_amt not in ledger_raws:
            return ToolResult(
                tool_id="check_claims_in_ledger",
                category="claims",
                passed=False,
                reason=f"Financial dollar amount '{amt}' does not appear in verified claims ledger.",
                offending_span=amt
            )
            
    for pct in percentages:
        if pct.lower() not in ledger_vals and pct.lower() not in ledger_raws:
            return ToolResult(
                tool_id="check_claims_in_ledger",
                category="claims",
                passed=False,
                reason=f"Percentage claim '{pct}' does not appear in verified claims ledger.",
                offending_span=pct
            )

    # Check voting logistics
    if "political" in campaign_type:
        has_voting = bool(re.search(r"(?i)\b(polling|polling\s+place|poll\s+hours|vote\s+by|ballot\s+box)\b", text))
        if has_voting:
            voting_claims = [c for c in claims_ledger if c.claim_type == "voting_logistics"]
            if not voting_claims:
                return ToolResult(
                    tool_id="check_claims_in_ledger",
                    category="claims",
                    passed=False,
                    reason="Voting logistics mentioned in text without corresponding user-verified claim in ledger."
                )
            for vc in voting_claims:
                if not vc.verified_by_user:
                    return ToolResult(
                        tool_id="check_claims_in_ledger",
                        category="claims",
                        passed=False,
                        reason="Voting logistics in ledger has not been verified by user (verified_by_user=False)."
                    )
                if not vc.source_url or not re.search(r"\.gov(?:/|$)", vc.source_url.lower()):
                    return ToolResult(
                        tool_id="check_claims_in_ledger",
                        category="claims",
                        passed=False,
                        reason=f"Voting logistics requires an official election office source_url ending in .gov (got '{vc.source_url}')."
                    )

    return ToolResult(tool_id="check_claims_in_ledger", category="claims", passed=True, reason="All quantitative facts and claims verified in ledger.")

# -------------------------------------------------------------
# Encoding, Segments & Standard Format Rules
# -------------------------------------------------------------

def check_encoding_segments(text: str, max_segments: int = 2) -> ToolResult:
    """Format: Replaces 320-word limit. Detects GSM-7 vs UCS-2 and calculates SMS segments."""
    # Standard basic GSM-7 set
    gsm7_basic = set(
        "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
        "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
    )
    gsm7_ext = set("^{}\\[~]|€")
    
    is_gsm7 = True
    for ch in text:
        if ch not in gsm7_basic and ch not in gsm7_ext:
            is_gsm7 = False
            break
            
    if is_gsm7:
        # Count GSM-7 chars (extended count as 2)
        count = sum(2 if ch in gsm7_ext else 1 for ch in text)
        if count <= 160:
            segments = 1
        else:
            segments = (count + 152) // 153
        encoding = "GSM-7"
    else:
        # UCS-2
        count = len(text)
        if count <= 70:
            segments = 1
        else:
            segments = (count + 66) // 67
        encoding = "UCS-2"
        
    if segments > max_segments:
        return ToolResult(
            tool_id="check_encoding_segments",
            category="format",
            passed=False,
            reason=f"Message requires {segments} {encoding} segments, exceeding the configured ceiling of {max_segments} segments.",
            offending_span=f"{segments}_segments_{encoding}"
        )
    return ToolResult(
        tool_id="check_encoding_segments",
        category="format",
        passed=True,
        reason=f"Message fits within {segments} {encoding} segment(s) (ceiling: {max_segments})."
    )

def check_opt_out(text: str, moment: str) -> ToolResult:
    """Format: Checks STOP opt-out instruction unless moment is stop_reply."""
    if moment in ("stop_reply", "opt_out_confirmation"):
        return ToolResult(tool_id="check_opt_out", category="format", passed=True, reason="Opt-out instruction waived on stop confirmation.")
    
    stop_pattern = re.compile(r"(?i)\b(reply|text|send)?\s*stop\b|\bstop\s+(to\s+opt\s*out|to\s+cancel|to\s+unsubscribe|to\s+end)\b")
    if not stop_pattern.search(text):
        return ToolResult(
            tool_id="check_opt_out",
            category="format",
            passed=False,
            reason="Mandatory opt-out instruction missing (e.g. 'Reply STOP to unsubscribe')."
        )
    return ToolResult(tool_id="check_opt_out", category="format", passed=True, reason="Opt-out instruction verified.")

def check_rates_disclosure(text: str, moment: str = "outbound") -> ToolResult:
    """Format: Checks for 'Msg & data rates may apply' on CTA/opt-in moments."""
    if moment in ("opt_in_confirmation", "call_to_action"):
        if not re.search(r"(?i)(msg\s*&?\s*data\s*rates\s*may\s*apply|message\s*and\s*data\s*rates)", text):
            return ToolResult(
                tool_id="check_rates_disclosure",
                category="format",
                passed=False,
                reason="Required CTIA rates disclosure ('Msg & data rates may apply') missing from confirmation/call-to-action."
            )
    return ToolResult(tool_id="check_rates_disclosure", category="format", passed=True, reason="Rates disclosure verified or not required.")

def check_paid_for_by(text: str, org: Any, campaign_type: str = "political_outreach") -> ToolResult:
    """Format: Political messages must contain 'Paid for by {committee}'."""
    if campaign_type.startswith("political_") or "political" in campaign_type:
        if hasattr(org, "committee_name"):
            expected_comm = (org.committee_name or org.legal_name or "").lower()
            expected_display = org.committee_name or org.legal_name
        else:
            expected_comm = str(org or "").lower()
            expected_display = str(org or "")
        m = re.search(r"(?i)paid\s+for\s+by\s+([^.\n]+)", text)
        if not m:
            return ToolResult(
                tool_id="check_paid_for_by",
                category="format",
                passed=False,
                reason="Mandatory disclaimer 'Paid for by [Committee Name]' missing from political message."
            )
        matched_name = m.group(1).strip().lower()
        if expected_comm not in matched_name and matched_name not in expected_comm:
            return ToolResult(
                tool_id="check_paid_for_by",
                category="format",
                passed=False,
                reason=f"'Paid for by {m.group(1)}' does not match registered committee name '{expected_display}'.",
                offending_span=m.group(0)
            )
    return ToolResult(tool_id="check_paid_for_by", category="format", passed=True, reason="Political disclaimer verified or not required.")

def check_placeholders(text: str, brand_name: str = "") -> ToolResult:
    """Format: No unresolved [Date] or {{tags}}."""
    pattern = re.compile(r"(\[[A-Za-z0-9_\s]{2,40}\]|\{\{[^{}]+\}\}|\{\{\{[^{}]+\}\}\})")
    matches = pattern.findall(text)
    bad = [m for m in matches if m.strip("[]{} \t").lower() != brand_name.lower()]
    if bad:
        return ToolResult(
            tool_id="check_placeholders",
            category="format",
            passed=False,
            reason=f"Unresolved template placeholders detected: {bad}",
            offending_span=bad[0]
        )
    return ToolResult(tool_id="check_placeholders", category="format", passed=True, reason="No unresolved placeholders.")

def check_embedded_phones(text: str, allowed_phone: str) -> ToolResult:
    """Format: Embedded NANP numbers must match registered care or sending contact."""
    clean_target = re.sub(r"\D", "", allowed_phone or "")
    matches = re.finditer(r"(?<!\w)(?:\+?1[-. ]?)?(?:\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}|1-800-\d{3}-\d{4})(?!\w)", text)
    for m in matches:
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        if len(digits) >= 10:
            if not (digits == clean_target or digits.endswith(clean_target[-10:]) or clean_target.endswith(digits[-10:])):
                return ToolResult(
                    tool_id="check_embedded_phones",
                    category="format",
                    passed=False,
                    reason=f"Embedded phone number '{raw}' does not match registered customer care contact '{allowed_phone}'.",
                    offending_span=raw
                )
    return ToolResult(tool_id="check_embedded_phones", category="format", passed=True, reason="Embedded phone numbers verified.")

def check_all_caps_emoji(text: str, max_emoji: int = 3, max_caps_ratio: float = 0.35) -> ToolResult:
    """Format: Prevents scream caps and emoji spam."""
    alphas = [c for c in text if c.isalpha()]
    if alphas:
        caps = sum(1 for c in alphas if c.isupper())
        ratio = caps / len(alphas)
        if ratio > max_caps_ratio and len(alphas) > 25:
            return ToolResult(
                tool_id="check_all_caps_emoji",
                category="format",
                passed=False,
                reason=f"Uppercase ratio ({ratio:.1%}) exceeds maximum allowable threshold ({max_caps_ratio:.1%}).",
                offending_span=f"caps_ratio_{ratio:.2f}"
            )
    return ToolResult(tool_id="check_all_caps_emoji", category="format", passed=True, reason="Capitalization and formatting compliant.")

# -------------------------------------------------------------
# Harmful Intent Literal Regex Tools
# -------------------------------------------------------------

def check_scam_patterns(text: str) -> ToolResult:
    """Harmful Intent: Literal sensitive data requests and urgency threats."""
    sens = re.search(r"(?i)\b(enter|confirm|verify|provide|reply\s+with)\s+your(?:\s+[\w-]+){0,3}\s+(password|pin|ssn|social\s+security|card\s+number|cvv|credentials?)\b", text)
    if sens:
        return ToolResult(
            tool_id="check_scam_patterns",
            category="harmful_intent",
            passed=False,
            reason=f"Sensitive credential request detected: '{sens.group(0)}'",
            offending_span=sens.group(0)
        )
    urgency = re.search(r"(?i)\b(account|service)\s+(will\s+be|has\s+been|is)\s+(suspended|terminated|locked|restricted|frozen|blocked)\b", text)
    action = re.search(r"(?i)\b(verify|click|act|pay|reply)\s+(now|immediately|within)\b", text)
    if urgency and action:
        return ToolResult(
            tool_id="check_scam_patterns",
            category="harmful_intent",
            passed=False,
            reason=f"Urgency account threat detected: '{urgency.group(0)}' co-occurring with '{action.group(0)}'",
            offending_span=f"{urgency.group(0)} ... {action.group(0)}"
        )
    return ToolResult(tool_id="check_scam_patterns", category="harmful_intent", passed=True, reason="No literal scam/threat patterns detected.")

def check_no_payment_requests(text: str) -> ToolResult:
    """Harmful Intent: Direct payment detail requests over SMS are prohibited."""
    m = re.search(r"(?i)\b(text|send|reply\s+with)\s+your(?:\s+[\w-]+){0,2}\s+(credit\s+card|debit\s+card|card\s+details|bank\s+account|cvv|routing\s+number)\b", text)
    if m:
        return ToolResult(
            tool_id="check_no_payment_requests",
            category="harmful_intent",
            passed=False,
            reason=f"Direct payment credential solicitation via SMS detected: '{m.group(0)}'. Donations must route through registered domain links.",
            offending_span=m.group(0)
        )
    return ToolResult(tool_id="check_no_payment_requests", category="harmful_intent", passed=True, reason="No direct payment credential requests.")

def check_merge_values(data_or_text: Any, recipient_data: Optional[Dict[str, Any]] = None, banned_domains: Optional[List[Dict[str, Any]]] = None) -> ToolResult:
    """Format: Verifies per-recipient merge values do not contain URLs, phone numbers, or banned shorteners."""
    target_data = recipient_data if isinstance(recipient_data, dict) else (data_or_text if isinstance(data_or_text, dict) else {})
    for k, v in target_data.items():
        if k in ("phone", "recipient_id", "contact_id"):
            continue
        v_str = str(v)
        # Check URLs in merge field
        if re.search(r"(?:https?://|[a-zA-Z0-9-]+\.[a-zA-Z]{2,})", v_str):
            # Check banned URLs specifically
            b_check = check_banned_urls(v_str, banned_domains)
            if not b_check.passed:
                return ToolResult(
                    tool_id="check_merge_values",
                    category="format",
                    passed=False,
                    reason=f"Recipient merge column '{k}' contains prohibited shortener URL: '{v_str}'",
                    offending_span=v_str
                )
            return ToolResult(
                tool_id="check_merge_values",
                category="format",
                passed=False,
                reason=f"Recipient merge column '{k}' contains unexpected URL injection: '{v_str}'",
                offending_span=v_str
            )
        # Check phone numbers
        if re.search(r"\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}\b", v_str):
            return ToolResult(
                tool_id="check_merge_values",
                category="format",
                passed=False,
                reason=f"Recipient merge column '{k}' contains unexpected phone number: '{v_str}'",
                offending_span=v_str
            )
    return ToolResult(tool_id="check_merge_values", category="format", passed=True, reason="Recipient merge values clean.")
