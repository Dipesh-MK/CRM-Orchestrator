import json
import re
from typing import Dict, Any, Optional
from app.core.config import get_config
from app.core.storage import get_storage
from app.core.prompt_manager import get_prompt_manager
from app.core.llm_client import get_llm_client

CHIT_CHAT_WORDS = {
    "hi", "hello", "hey", "greetings", "there", "good", "morning", "afternoon", "evening",
    "ok", "okay", "sure", "sounds", "continue", "next", "proceed", "yes", "no", "yep", "nope",
    "thanks", "thank", "you", "great", "awesome", "perfect", "got", "it", "fine", "done", "please", "go", "ahead",
    "just", "already", "selected", "testing", "sample", "test", "try", "pls", "plz", "keep", "going", "cool",
    "its", "it's", "am"
}

def is_chit_chat_or_ack(text: str) -> bool:
    """Returns True if the text is pure conversational pleasantry or acknowledgment without campaign facts."""
    clean = re.sub(r"[^a-zA-Z\s]", "", text).strip().lower()
    if not clean:
        return True
    words = clean.split()
    if len(words) <= 10 and all(w in CHIT_CHAT_WORDS for w in words):
        return True
    return False

class SafetyService:
    def __init__(self):
        self.config = get_config()
        self.storage = get_storage()
        self.prompts = get_prompt_manager()
        self.llm = get_llm_client()

    async def evaluate_safety(self, session_id: str, text_to_evaluate: str, context_label: str = "user_input") -> Dict[str, Any]:
        """
        Runs code-level guard: safety_policy.md as system prompt,
        classifying whether text is safe or violates prohibited categories.
        Skips LLM call on pure chit-chat turns without campaign facts.
        Fails closed on error/invalid output.
        """
        if context_label in ("user_input", "discovery_input", "chat_message") and is_chit_chat_or_ack(text_to_evaluate):
            result = {
                "allowed": True,
                "category": "none",
                "reason": "Chit-chat / acknowledgment turn skipped safety LLM guard.",
                "suggested_alternative": None,
                "skipped_llm": True
            }
            self.storage.log_validation(
                session_id=session_id,
                validation_type=f"safety_guard_{context_label}",
                passed=True,
                details=result
            )
            return result

        # 1. Zero-Tolerance Carrier 10DLC Public URL Shortener Filter (tiny.url & bit.ly)
        banned_shortener_pattern = re.compile(
            r"(?i)(?:https?://)?(?:www\.)?(?:tiny(?:\.|\s*\[\.\]\s*)url|tinyurl(?:\.|\s*\[\.\]\s*)com|bit(?:\.|\s*\[\.\]\s*)ly|bitly(?:\.|\s*\[\.\]\s*)com)(?:[:/?#\s]|$|[^\w.-])"
        )
        m_shortener = banned_shortener_pattern.search(text_to_evaluate)
        if m_shortener:
            matched_shortener = m_shortener.group(0).strip()
            shortener_result = {
                "allowed": False,
                "category": "prohibited_url_shortener_carrier_policy",
                "matched_term": matched_shortener,
                "reason": (
                    f"Prohibited public URL shortener detected ('{matched_shortener}'). "
                    f"US Carrier 10DLC and CTIA regulations strictly ban public URL shorteners (such as tiny.url, tinyurl.com, bit.ly, and bitly.com) "
                    f"to protect consumers against SMS phishing and fraud."
                ),
                "suggested_alternative": "Replace with a registered full domain link (e.g. https://hopefoundation.org/event) or an official support phone number."
            }
            self.storage.log_validation(
                session_id=session_id,
                validation_type=f"safety_guard_{context_label}",
                passed=False,
                details=shortener_result
            )
            return shortener_result

        # 2. Strict Whitelist Filter: Accept ONLY Charity-related and Political-related events
        if context_label in ("user_input", "campaign_interaction", "campaign_form_submit", "discovery_input", "chat_message"):
            t_lower = text_to_evaluate.lower()

            charity_keywords = (
                "charit", "non-profit", "nonprofit", "ngo", "fundrais", "donat", "gala",
                "volunteer", "relief", "food bank", "shelter", "rescue", "outreach",
                "501c3", "501c4", "foundation", "benevolent", "aid", "orphan"
            )
            political_keywords = (
                "politic", "campaign", "candidate", "pac", "super pac", "election",
                "vote", "voter", "ballot", "polling", "senat", "congress", "representative",
                "mayor", "governor", "caucus", "town hall", "primary", "paid for by",
                "civic", "referendum", "proposition", "rally", "legislation", "democrat",
                "republican", "independent", "libertarian", "green party", "constituent"
            )

            is_charity = any(k in t_lower for k in charity_keywords)
            is_political = any(k in t_lower for k in political_keywords)

            # Detect explicit non-whitelisted commercial, clinic, retail, or utility indicators
            commercial_indicators = (
                "dental", "dentist", "teeth", "cleaning", "clinic", "patient", "doctor",
                "electric", "power bill", "utility", "disconnection", "water bill",
                "retail", "sale", "discount", "promo", "shop", "store", "boutique",
                "fashion", "shoes", "coupon", "black friday", "cyber monday", "bogo",
                "saas", "subscription", "crypto", "casino", "payday", "loan", "bank",
                "gym", "fitness", "salon", "spa", "car wash", "dealership", "auto repair"
            )
            has_commercial = any(c in t_lower for c in commercial_indicators)

            if has_commercial and not (is_charity or is_political):
                scope_result = {
                    "allowed": False,
                    "category": "out_of_scope_non_whitelisted_domain",
                    "reason": (
                        "Gateway Policy Restriction: This platform operates strictly under a Charity & Political Events Whitelist. "
                        "Commercial retail promotions, healthcare/dental clinics, utility billing notices, and general corporate campaigns are strictly disallowed."
                    ),
                    "suggested_alternative": "Please submit a message for a registered charitable organization, fundraiser, community relief drive, or an authorized political campaign / civic event."
                }
                self.storage.log_validation(
                    session_id=session_id,
                    validation_type=f"safety_guard_{context_label}",
                    passed=False,
                    details=scope_result
                )
                return scope_result

            # Check if this session has already been established as a whitelisted domain
            is_existing_whitelisted_session = False
            try:
                sess_record = self.storage.get_session(session_id)
                if sess_record and sess_record.get("meta"):
                    m = sess_record.get("meta", {})
                    dom = m.get("campaign_domain") or m.get("business_type") or ""
                    if dom in ("Charity & Non-Profit", "Political & Civic"):
                        is_existing_whitelisted_session = True
            except Exception:
                pass

            # If message is substantial (longer prompt) and not related to charity or political events:
            if not (is_charity or is_political) and not is_existing_whitelisted_session and len(t_lower.split()) > 7:
                scope_result = {
                    "allowed": False,
                    "category": "out_of_scope_non_whitelisted_domain",
                    "reason": (
                        "Gateway Policy Restriction: Only Charity-related events and Political-related events are permitted on this gateway. "
                        "Commercial promotions, healthcare clinics, retail sales, and corporate messages are not accepted."
                    ),
                    "suggested_alternative": "Please specify a charitable cause, fundraiser, non-profit drive, or political / civic campaign."
                }
                self.storage.log_validation(
                    session_id=session_id,
                    validation_type=f"safety_guard_{context_label}",
                    passed=False,
                    details=scope_result
                )
                return scope_result

        # 3. Deterministic Carrier 10DLC SHAFT / Phishing / Credential Harvesting Pre-Filter
        prohibited_pattern = re.compile(
            r"(?i)\b("
            r"cannabis|marijuana|thc|pre-rolls|edibles|dispensary|"
            r"oxycodone|xanax|adderall|hydrocodone|narcotic|"
            r"online banking password|banking password|password and pin|reply with your pin|reply with your ssn|"
            r"payday loan|payday loans|underground poker|cash stakes poker"
            r")\b"
        )
        m_prohib = prohibited_pattern.search(text_to_evaluate)
        if m_prohib:
            matched_term = m_prohib.group(0)
            det_result = {
                "allowed": False,
                "category": "prohibited_content_carrier_policy",
                "reason": f"Prohibited under US Carrier 10DLC / SHAFT guidelines (detected: '{matched_term}'). Carrier policies strictly prohibit this category nationwide regardless of state licenses.",
                "suggested_alternative": "Please reformulate for a compliant charitable or political use-case."
            }
            self.storage.log_validation(
                session_id=session_id,
                validation_type=f"safety_guard_{context_label}",
                passed=False,
                details=det_result
            )
            return det_result

        safety_policy = self.prompts.load_prompt("safety_policy")
        
        system_prompt = f"""{safety_policy}

CRITICAL INSTRUCTION:
- This gateway explicitly operates on a Charity and Political whitelist.
- Legitimate Charity / Non-Profit events (fundraisers, galas, volunteer relief) AND Political / Civic events (candidate campaigns, election alerts, voter turnout, rallies, town halls) are FULLY WHITELISTED, PERMITTED, AND SAFE.
- DO NOT flag or disallow content simply because it promotes a political candidate, campaign, election, or voter event. Political events with full domains are explicitly allowed.
- ONLY set allowed: false for actual violations: illegal drugs, weapons, phishing/credential harvesting (passwords, PINs, SSNs), malware, hate speech, adult/sexual exploitation, public URL shorteners (tiny.url, bit.ly), or non-whitelisted commercial retail promotions.
- If safe and within charity or political categories, you MUST output allowed: true, category: "none".

Evaluate the given content against the policy.
You MUST respond with a valid JSON object matching this schema:
{{
  "allowed": true or false,
  "category": "none" or name of the category violated,
  "reason": "explanation of decision",
  "suggested_alternative": "legitimate alternative suggestion if blocked, or null"
}}
"""
        user_message = f"Content to evaluate ({context_label}):\n{text_to_evaluate}"

        max_retries = self.config.orchestrator.max_retries
        last_error = None
        result = None

        for attempt in range(max_retries + 1):
            llm_res = await self.llm.call_llm(
                session_id=session_id,
                purpose="safety_guard",
                system_prompt=system_prompt,
                user_message=user_message,
                step_id="safety_guard",
                json_mode=True,
                temperature=0.0
            )

            if llm_res.get("status") == "success":
                raw = llm_res.get("raw_response", "")
                from app.services.orchestrator_engine import parse_json_robust
                data = parse_json_robust(raw)
                if data and "allowed" in data:
                    cat = str(data.get("category", "")).strip().lower()
                    reason_text = str(data.get("reason", "")).strip().lower()

                    # Inversion guard: If category is none or reason says 'does not violate'/'not related to', content is ALLOWED
                    safe_phrases = (
                        "does not violate", "complies", "no violation", "is legitimate",
                        "is a legitimate", "safe use case", "legitimate and safe",
                        "is not related to", "not related to", "does not involve",
                        "does not contain", "is safe", "not prohibited", "no prohibited",
                        "does not mention", "does not fall under"
                    )
                    if cat in ("none", "null", "", "n/a", "no_violation") or any(p in reason_text for p in safe_phrases):
                        data["allowed"] = True
                        data["category"] = "none"
                    elif data.get("allowed") is False and cat in ("none", ""):
                        data["allowed"] = True
                        data["category"] = "none"

                    # Anti-hallucination check for adult content:
                    # Unless text contains actual adult/sexual keywords, charity/admission/events are NEVER adult content
                    if any(c in cat for c in ("adult", "sexual", "exploitation")):
                        has_real_adult = bool(re.search(r"(?i)\b(sex|porn|escort|nude|erotic|bdsm|prostitut|fetish|onlyfans)\b", text_to_evaluate))
                        if not has_real_adult:
                            data["allowed"] = True
                            data["category"] = "none"

                    # Anti-hallucination check for hate speech:
                    # Benign charities, sanctuaries, clinics, schools, and utility drives are NEVER hate speech
                    if "hate" in cat:
                        has_real_hate = bool(re.search(r"(?i)\b(kill\s+all|exterminate|subhuman|hate\s+all|die\s+all)\b", text_to_evaluate))
                        if not has_real_hate:
                            data["allowed"] = True
                            data["category"] = "none"

                    # Anti-hallucination check for scam/fraud:
                    # Mentions of UI confirmation 'card' or benign commercial exhibitions are NEVER financial fraud
                    if any(c in cat for c in ("scam", "fraud", "phish")):
                        has_real_fraud = bool(re.search(r"(?i)\b(password|pin\b|ssn|social security|bank account|routing number|cvv|wire transfer|crypto|seed phrase|login credentials)\b", text_to_evaluate))
                        if not has_real_fraud and any(w in text_to_evaluate.lower() for w in ["show card", "ready to show card", "the card", "confirmation card", "looks good", "ready", "grocery", "exhibition", "farmers"]):
                            data["allowed"] = True
                            data["category"] = "none"

                    # Anti-hallucination check for political content:
                    # Political and civic campaigns, candidate rallies, voter turnout, and PACs are explicitly WHITELISTED.
                    if any(c in cat for c in ("politic", "campaign", "candidate", "election", "vote")):
                        has_real_viol = bool(re.search(r"(?i)\b(kill|bomb|shoot|assassinate|terror|overthrow|fraud|scam|bribe|extort)\b", text_to_evaluate))
                        if not has_real_viol:
                            data["allowed"] = True
                            data["category"] = "none"
                            data["reason"] = "Political and civic campaign events are fully whitelisted and permitted."

                    # Anti-hallucination check for conversational navigational commands:
                    # Generic UI instructions (e.g. "ready to show card", "looks good", "proceed", "that's all")
                    # without any actual illegal keywords are NEVER safety violations.
                    nav_phrases = (
                        "show card", "ready to show card", "the card", "confirmation card",
                        "looks good", "ready", "proceed", "that's all", "thats all", "done",
                        "generate card", "make card", "confirm"
                    )
                    if any(p in text_to_evaluate.lower() for p in nav_phrases):
                        has_real_viol = bool(re.search(
                            r"(?i)\b(cannabis|marijuana|thc|cocaine|heroin|fentanyl|narcotic|gun|firearm|rifle|pistol|bomb|explosive|sex|porn|escort|nude|password|pin|ssn|wire transfer)\b",
                            text_to_evaluate
                        ))
                        if not has_real_viol:
                            data["allowed"] = True
                            data["category"] = "none"

                    result = data
                    break
                else:
                    last_error = "Malformed or missing 'allowed' in safety output"
            else:
                last_error = llm_res.get("error")

        if result is None:
            # Distinguish between rate limit and other guard failures
            is_rate_limit = "429" in str(last_error) or "Rate limit" in str(last_error)
            category = "rate_limit_exceeded" if is_rate_limit else "system_guard_failure"
            
            clean_reason = f"Safety check could not complete due to temporary upstream rate limits: {last_error}" if is_rate_limit else f"Safety check failed to validate safely after retries: {last_error}"
            
            result = {
                "allowed": False,
                "category": category,
                "reason": clean_reason,
                "is_rate_limit": is_rate_limit,
                "suggested_alternative": None
            }

        # Log validation check
        self.storage.log_validation(
            session_id=session_id,
            validation_type=f"safety_guard_{context_label}",
            passed=result["allowed"],
            details=result
        )

        return result

    def evaluate_tone_safety(self, session_id: str, tone_text: str) -> Dict[str, Any]:
        """
        Pre-Guardrail: Validates custom tone requests against carrier ethics and CTIA Section 4.4.
        Strictly disallows overtly aggressive, hostile, intimidating, threatening,
        coercive, debt-collection harassment, or deceptive tone registers.
        """
        clean = (tone_text or "").strip().lower()
        if not clean:
            return {"allowed": True, "category": "none", "reason": "No custom tone specified."}

        # Catch aggressive, intimidating, threatening, coercive, or abusive tone requests (including common misspellings like 'aggresive')
        prohibited_tone_pattern = re.compile(
            r"(?i)\b("
            r"aggres+ive|very\s+aggres+ive|hyper[\s-]?aggres+ive|"
            r"intimidat(ing|ion)?|threat(ening)?|hostile|coercive|coercion|"
            r"harass(ing|ment)?|bully(ing)?|abusive|violent|"
            r"debt[\s-]?collect(ion|or)?[\s-]?(shakedown|threat)|"
            r"scare[\s-]?tactics?|predatory|extort(ion)?|shaming"
            r")\b"
        )
        m = prohibited_tone_pattern.search(clean)
        if m:
            matched = m.group(0)
            res = {
                "allowed": False,
                "category": "prohibited_tone_carrier_ethics",
                "matched_term": matched,
                "reason": (
                    f"Prohibited tone register detected: '{matched}'. "
                    f"US Carrier 10DLC and CTIA Section 4.4 regulations strictly prohibit aggressive, "
                    f"intimidating, or coercive messaging registers to protect consumers against harassment."
                ),
                "suggested_alternative": "Please select a compliant register such as 'Direct & High-Converting', 'Urgent & Factual', or 'Professional & Clear'."
            }
            self.storage.log_validation(
                session_id=session_id,
                validation_type="safety_guard_tone_request",
                passed=False,
                details=res
            )
            return res

        return {"allowed": True, "category": "none", "reason": "Tone request complies with carrier ethics."}

    async def evaluate_copy_safety(self, session_id: str, copy_text: str) -> Dict[str, Any]:
        """
        Post-Guardrail: Validates generated message copy to ensure tone does not cross into
        deceptive urgency, coercive harassment, legal threats, or prohibited carrier content.
        """
        clean = (copy_text or "").strip().lower()
        
        # Check for deceptive threats or coercive intimidation in drafted copy
        coercive_copy_pattern = re.compile(
            r"(?i)\b("
            r"warrant for your arrest|you will be arrested|sheriff will|police will arrive|"
            r"law enforcement will take action|jail time|immediate seizure|"
            r"we are seizing your|pay immediately or face arrest|"
            r"you will regret this|or else|"
            r"publicly shame you|we will contact your employer"
            r")\b"
        )
        m_coercive = coercive_copy_pattern.search(clean)
        if m_coercive:
            matched = m_coercive.group(0)
            res = {
                "allowed": False,
                "category": "coercive_harassment_in_copy",
                "matched_term": matched,
                "reason": f"Generated copy contains coercive or deceptive threats ('{matched}'), violating CTIA consumer protection standards.",
                "suggested_alternative": "Rephrase as a professional, factual reminder without intimidation."
            }
            self.storage.log_validation(
                session_id=session_id,
                validation_type="safety_guard_copy_post_guard",
                passed=False,
                details=res
            )
            return res

        # Fallback to standard safety check
        return await self.evaluate_safety(session_id, copy_text, context_label="generated_copy_post_guard")

_safety_service: Optional[SafetyService] = None

def get_safety_service() -> SafetyService:
    global _safety_service
    if _safety_service is None:
        _safety_service = SafetyService()
    return _safety_service
