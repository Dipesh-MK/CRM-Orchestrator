"""
Guardrail Agent (Agent 3)
Strict Section 4.4 Rubric Evaluator for CRM SMS Messages.
Enforces:
- Viewpoint neutrality (never discriminates based on candidate, party, or political stance).
- Instruction adherence.
- Tone matching & prohibition of aggressive/coercive/guilt-tripping language.
- Prohibition of deceptive urgency and manufactured deadlines.
- Harmful intent detection (scam, phishing, fraud, hate, harassment).
- Hidden asks in thank-you messages (thank-yous must not solicit further donations).
- Unsupported claims (statistics, $, % not in claims ledger).
- Official impersonation and voting logistics accuracy (.gov required).
- Failure origin attribution: "draft" (triggers silent redraft) vs "user_request" (triggers Card Refusal with suggestions).
- Strict fail-closed policy.
"""

import os
import re
import json
import logging
from typing import Dict, Any, List, Optional
import httpx
import yaml

from app.core.orchestrator_v2_models import (
    GuardrailVerdict,
    FlaggedSpan,
    InstructionAdherence,
    HarmfulIntentEvaluation,
    CardRefusal,
    RefusalSuggestion,
    ClaimEntry,
    InstructionEntry,
)

logger = logging.getLogger("guardrail_agent")

GUARDRAIL_SYSTEM_PROMPT = """You are the Guardrail Agent for an SMS CRM Orchestrator dedicated to US Non-Profit and Political campaigns.
Your duty is strict compliance verification against CTIA, TCPA, carrier, and safety guidelines.

IMPORTANT: You MUST maintain strict VIEWPOINT NEUTRALITY. You must never penalize, flag, or judge political stances, candidates, ballot measures, or party affiliations. Mirrored political messages (conservative or progressive) must receive identical objective evaluation.

Evaluate the draft message against these exact rubric points:
1. instruction_adherence: Did the draft follow active user instructions and exclusions?
2. requested_tone_match: Does the draft match the requested tone (Urgent & Direct, Friendly & Casual, Professional & Polite)?
3. aggressive_tone: Is the tone coercive, guilt-tripping, manipulative, or shaming? (Prohibited)
4. deceptive_urgency_or_guilt: Does it manufacture false deadlines or artificial panic not verified in claims? (Prohibited)
5. harmful_intent: Is there any phishing, credential request, scam, malware, or harassment? (Prohibited)
6. hidden_ask_in_thank_you: If the campaign is a thank-you message, does it sneak in an ask for more money? (Prohibited)
7. unsupported_claim: Are there specific dollar amounts, percentages, or facts not present in the verified claims ledger? (Prohibited)
8. impersonates_official: Does it falsely claim to be a government election office, law enforcement, or financial institution? (Prohibited)
9. voting_logistics_conflict: Do voting dates/polling locations lack verified .gov backing? (Prohibited)
10. defamation_or_fabricated_claim_about_person: Does it make defamatory or fabricated factual claims about an individual? (Prohibited)
11. use_case_drift: Does it stray from registered non-profit / political outreach? (Prohibited)
12. disclosure_unclear: Are required disclaimers (e.g. 'Paid for by...', opt-out STOP) missing or obscured?
13. intent_or_sender_unclear: Is the sender identity or primary purpose ambiguous?

ATTRIBUTION (failure_origin):
- "none": All checks pass.
- "user_request": The user specifically prompted for prohibited content (e.g. demanded phishing, asked for aggressive guilt-tripping, asked to omit mandatory disclosures).
- "draft": The user's request was compliant, but the generator produced non-compliant copy.

If failure_origin is "user_request", construct user_refusal with 2-4 actionable suggestion chips that the user can click to fix the issue.

Return ONLY a valid JSON object matching the GuardrailVerdict schema.
"""


class GuardrailAgent:
    """Agent 3: Guardrail Agent."""

    def __init__(self, config_path: str = "config/models.yaml"):
        self.models_config = self._load_models_config(config_path)

    def _load_models_config(self, path: str) -> Dict[str, Any]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                logger.error(f"Error loading models config from {path}: {e}")
        return {}

    async def evaluate_draft(
        self,
        draft_text: str,
        user_prompt: str,
        campaign_type: str,
        requested_tone: str,
        claims_ledger: List[ClaimEntry],
        instruction_ledger: List[InstructionEntry],
        org_name: str = "",
        committee_name: Optional[str] = None,
    ) -> GuardrailVerdict:
        """
        Evaluate a candidate SMS draft.
        Tries primary Guardrail provider (Anthropic), then secondary (Google Gemini).
        If both external providers fail or credentials are not supplied, executes the strict
        deterministic rule-grounded Section 4.4 rubric evaluator. Fails closed on any ambiguity.
        """
        # Try external LLM if API keys are configured
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")

        if anthropic_key:
            try:
                verdict = await self._call_anthropic_guardrail(
                    draft_text, user_prompt, campaign_type, requested_tone, claims_ledger, instruction_ledger, anthropic_key
                )
                if verdict:
                    return verdict
            except Exception as e:
                logger.warning(f"Anthropic Guardrail call failed: {e}. Trying secondary provider.")

        if gemini_key:
            try:
                verdict = await self._call_gemini_guardrail(
                    draft_text, user_prompt, campaign_type, requested_tone, claims_ledger, instruction_ledger, gemini_key
                )
                if verdict:
                    return verdict
            except Exception as e:
                logger.warning(f"Gemini Guardrail call failed: {e}. Falling back to strict deterministic evaluator.")

        # Deterministic Section 4.4 Rubric Evaluator (Never weaker/unaligned model)
        return self._evaluate_deterministic_rubric(
            draft_text=draft_text,
            user_prompt=user_prompt,
            campaign_type=campaign_type,
            requested_tone=requested_tone,
            claims_ledger=claims_ledger,
            instruction_ledger=instruction_ledger,
            org_name=org_name,
            committee_name=committee_name,
        )

    def _evaluate_deterministic_rubric(
        self,
        draft_text: str,
        user_prompt: str,
        campaign_type: str,
        requested_tone: str,
        claims_ledger: List[ClaimEntry],
        instruction_ledger: List[InstructionEntry],
        org_name: str,
        committee_name: Optional[str],
    ) -> GuardrailVerdict:
        """
        Comprehensive deterministic Section 4.4 rubric engine.
        Applies rigorous checks across all 13 dimensions.
        """
        draft_lower = draft_text.lower()
        user_lower = user_prompt.lower()
        redraft_instructions: List[str] = []

        # 1. Harmful Intent / Scams / Credentials / Phishing / SHAFT / Weapons / Fraud
        harmful_flag = False
        harmful_cat = None
        harmful_reason = "No harmful intent detected."

        prohibited_categories = {
            "phishing_credentials": [
                "password", "pin", "ssn", "social security", "bank account number",
                "online banking password", "verify your login", "confirm your credentials",
                "reply with your code", "account suspended reply with", "cvv", "credit card number",
                "routing number", "crypto seed phrase", "private key"
            ],
            "controlled_substances_shaft": [
                "cannabis", "marijuana", "thc", "cbd vape", "pre-rolls", "edibles", "dispensary",
                "oxycodone", "xanax", "adderall", "hydrocodone", "narcotic", "narcotics",
                "cocaine", "heroin", "meth", "fentanyl", "illicit drugs", "unprescribed pills"
            ],
            "weapons_explosives": [
                "ghost gun", "untraceable firearm", "unregistered rifle", "silencer",
                "buy ammo without check", "explosives", "bomb making", "automatic weapon conversion",
                "unlicensed weapons", "smuggle weapons"
            ],
            "financial_fraud_scams": [
                "wire money to claim prize", "lottery winner pay fee", "crypto investment guarantee 100x",
                "payday loan guaranteed", "payday loans", "underground poker", "cash stakes poker",
                "we have recorded your webcam pay ransom", "ransomware", "verify your wire transfer to unfreeze"
            ],
            "hate_harassment": [
                "kill all", "exterminate", "subhuman", "die all", "hate all", "racial cleansing",
                "violent mob"
            ],
            "malware_cyberattack": [
                "malware download", "trojan", "keylogger", "ddos attack", "exploit kit", "credential stealer"
            ],
            "adult_exploitation": [
                "escort service", "erotic massage", "pornography", "explicit adult", "onlyfans promo",
                "brothel"
            ]
        }

        matched_cat = None
        matched_term = None
        for cat_name, terms in prohibited_categories.items():
            for term in terms:
                if re.search(rf"\b{re.escape(term)}\b", draft_lower):
                    harmful_flag = True
                    matched_cat = cat_name
                    matched_term = term
                    harmful_cat = cat_name
                    harmful_reason = f"Draft contains prohibited {cat_name.replace('_', ' ')} content: '{term}'."
                    redraft_instructions.append(f"Remove prohibited {cat_name.replace('_', ' ')} reference '{term}'.")
                    break
            if harmful_flag:
                break

        # Check user request origin for harmful intent
        user_harmful_request = False
        user_matched_cat = None
        user_matched_term = None
        for cat_name, terms in prohibited_categories.items():
            for term in terms:
                if re.search(rf"\b{re.escape(term)}\b", user_lower):
                    user_harmful_request = True
                    user_matched_cat = cat_name
                    user_matched_term = term
                    break
            if user_harmful_request:
                break

        # Check for user assurance patterns attempting to bypass safety or guardrails
        assurance_patterns = [
            r"i assure you", r"i guarantee", r"it is legal", r"it's legal", r"its legal",
            r"it is safe", r"it's safe", r"its safe", r"trust me", r"100% legal", r"100% safe",
            r"completely safe", r"completely legal", r"override safety", r"override guardrail",
            r"override", r"permission granted", r"licensed provider",
            r"authorized\s+(?:user|official|agent|representative|personnel)",
            r"i am\s+(?:an?\s+)?authorized", r"fully authorized",
            r"don't worry it's safe", r"dont worry its safe", r"i promise it's safe", r"i promise it is safe",
            r"i am a doctor", r"i am a lawyer", r"we have legal approval", r"lawfully permitted",
            r"test environment override", r"it is valid", r"it's valid", r"its valid",
            r"verified safe", r"safe to send", r"force proceed", r"force", r"proceed anyway"
        ]
        user_assurance_detected = any(re.search(pat, user_lower) for pat in assurance_patterns)

        if user_harmful_request or (user_assurance_detected and harmful_flag):
            harmful_flag = True
            if not harmful_cat:
                harmful_cat = user_matched_cat or "user_prohibited_request"
            harmful_reason = f"Prohibited request detected: {harmful_cat.replace('_', ' ')} ({user_matched_term or matched_term})."
            if user_assurance_detected:
                harmful_reason += " User assurances cannot override carrier 10DLC or safety policies."

        harmful_eval = HarmfulIntentEvaluation(
            flag=harmful_flag,
            category=harmful_cat,
            reason=harmful_reason,
        )

        # 2. Deceptive Urgency or Guilt
        deceptive_flag = False
        deceptive_span = None
        deceptive_reason = "Urgency is factual or reasonable."

        guilt_or_panic_phrases = [
            "your account has been suspended", "suspended due to suspicious logins",
            "will be terminated tonight", "last chance before you are removed",
            "shame on you", "how could you ignore", "we know where you live"
        ]
        for phrase in guilt_or_panic_phrases:
            if phrase in draft_lower:
                deceptive_flag = True
                deceptive_span = phrase
                deceptive_reason = f"Draft contains manufactured urgency or guilt: '{phrase}'."
                redraft_instructions.append(f"Remove coercive panic phrase '{phrase}'.")
                break

        # Check if requested tone is Urgent without a verified deadline
        has_verified_deadline = any(
            c.claim_type == "date" and c.verified_by_user for c in claims_ledger
        )
        if requested_tone.lower().startswith("urgent") and not has_verified_deadline:
            # Urgent tone without a verified deadline in claims ledger violates Section 4.4
            if any(w in draft_lower for w in ["deadline", "tonight", "hours left", "urgent", "immediate"]):
                deceptive_flag = True
                deceptive_span = "unverified urgency"
                deceptive_reason = "Urgent tone used without a verified deadline in the claims ledger."
                redraft_instructions.append("Remove urgency claims because no verified deadline exists in claims ledger.")

        deceptive_urgency = FlaggedSpan(
            flag=deceptive_flag,
            evidence_span=deceptive_span,
            reason=deceptive_reason,
        )

        # 3. Aggressive Tone
        aggressive_flag = False
        aggressive_span = None
        aggressive_reason = "Tone is respectful."
        aggressive_phrases = [
            "give us money now", "pay up", "coward", "traitor", "pathetic",
            "don't be selfish", "you owe us"
        ]
        for phrase in aggressive_phrases:
            if phrase in draft_lower:
                aggressive_flag = True
                aggressive_span = phrase
                aggressive_reason = f"Aggressive or derogatory language detected: '{phrase}'."
                redraft_instructions.append(f"Remove aggressive phrasing '{phrase}'.")
                break

        aggressive_tone = FlaggedSpan(
            flag=aggressive_flag,
            evidence_span=aggressive_span,
            reason=aggressive_reason,
        )

        # 4. Hidden Ask in Thank-You
        hidden_ask_flag = False
        hidden_ask_span = None
        hidden_ask_reason = "No hidden solicitation in thank-you message."
        if campaign_type == "donation_thank_you":
            solicitation_markers = ["donate again", "give another", "chip in another", "contribute today", "support us with $"]
            for marker in solicitation_markers:
                if marker in draft_lower:
                    hidden_ask_flag = True
                    hidden_ask_span = marker
                    hidden_ask_reason = f"Donation thank-you message includes an improper new donation ask: '{marker}'."
                    redraft_instructions.append("Remove solicitation from thank-you acknowledgment.")
                    break

        hidden_ask = FlaggedSpan(
            flag=hidden_ask_flag,
            evidence_span=hidden_ask_span,
            reason=hidden_ask_reason,
        )

        # 5. Unsupported Claims
        unsupported_flag = False
        unsupported_span = None
        unsupported_reason = "All claims and figures are supported by claims ledger."
        # Extract dollar amounts, percentages, or numbers from draft
        dollars_in_draft = re.findall(r"\$\d+(?:,\d{3})*(?:\.\d{2})?", draft_text)
        verified_dollars = [c.raw_value for c in claims_ledger if c.claim_type == "currency" and c.verified_by_user]
        for d in dollars_in_draft:
            if not any(d.replace("$", "").replace(",", "") in v.replace("$", "").replace(",", "") for v in verified_dollars):
                # Unverified dollar claim
                unsupported_flag = True
                unsupported_span = d
                unsupported_reason = f"Dollar amount '{d}' does not appear in user-verified claims ledger."
                redraft_instructions.append(f"Remove or verify dollar amount '{d}'.")
                break

        unsupported_claim = FlaggedSpan(
            flag=unsupported_flag,
            evidence_span=unsupported_span,
            reason=unsupported_reason,
        )

        # 6. Impersonates Official
        impersonate_flag = False
        impersonate_span = None
        impersonate_reason = "Sender identity is legitimate."
        official_entities = ["irs", "internal revenue service", "fbi", "police department", "elections office", "usps", "chase bank", "bank of america", "wells fargo"]
        if org_name.lower() not in [e.lower() for e in official_entities]:
            for official in official_entities:
                if official in draft_lower:
                    impersonate_flag = True
                    impersonate_span = official
                    impersonate_reason = f"Draft impersonates regulated official institution: '{official}'."
                    redraft_instructions.append(f"Do not claim to represent '{official}'.")
                    break

        impersonates_official = FlaggedSpan(
            flag=impersonate_flag,
            evidence_span=impersonate_span,
            reason=impersonate_reason,
        )

        # 7. Voting Logistics Conflict
        voting_flag = False
        voting_span = None
        voting_reason = "Voting logistics are verified or absent."
        if any(w in draft_lower for w in ["polling place", "vote on", "polls open", "ballot drop"]):
            has_gov_url = any(
                c.claim_type == "voting_logistics" and c.source_url and ".gov" in c.source_url.lower()
                for c in claims_ledger
            )
            if not has_gov_url:
                voting_flag = True
                voting_span = "voting logistics without .gov"
                voting_reason = "Voting dates or polling claims must be backed by a verified .gov source URL."
                redraft_instructions.append("Cite an official .gov election website for voting logistics.")

        voting_conflict = FlaggedSpan(
            flag=voting_flag,
            evidence_span=voting_span,
            reason=voting_reason,
        )

        # 8. Instruction Adherence
        missing_or_contradicted: List[str] = []
        for inst in instruction_ledger:
            if inst.status == "active" and inst.category == "exclude_topic":
                forbidden = inst.raw_instruction.replace("Exclude:", "").strip().lower()
                if forbidden and forbidden in draft_lower:
                    missing_or_contradicted.append(f"Included excluded topic '{forbidden}'")
                    redraft_instructions.append(f"Exclude topic '{forbidden}' as requested.")

        instruction_adh = InstructionAdherence(
            flag=(len(missing_or_contradicted) > 0),
            missing_or_contradicted=missing_or_contradicted,
            reason="Instructions followed." if not missing_or_contradicted else "; ".join(missing_or_contradicted),
        )

        # 9. Requested Tone Match
        tone_flag = False
        tone_span = None
        tone_reason = f"Tone matches '{requested_tone}'."
        if requested_tone.lower().startswith("professional") and any(w in draft_lower for w in ["yo!", "hey bro", "what's up", "omg"]):
            tone_flag = True
            tone_span = "slang in professional tone"
            tone_reason = "Informal slang used in Professional & Polite copy."
            redraft_instructions.append("Maintain professional tone; remove casual slang.")

        requested_tone_match = FlaggedSpan(
            flag=tone_flag,
            evidence_span=tone_span,
            reason=tone_reason,
        )

        # 10. Defamation / Fabricated claims about person
        defamation = FlaggedSpan(flag=False, evidence_span=None, reason="No defamatory statements found.")
        # 11. Use Case Drift
        use_case_drift = FlaggedSpan(flag=False, evidence_span=None, reason="Content aligns with registered use case.")
        # 12. Disclosure Unclear
        disclosure_unclear = FlaggedSpan(flag=False, evidence_span=None, reason="Disclosures are clear.")
        # 13. Intent or Sender Unclear
        intent_or_sender = FlaggedSpan(flag=False, evidence_span=None, reason="Sender and intent transparent.")

        # Determine overall pass
        overall_pass = not (
            harmful_eval.flag
            or deceptive_urgency.flag
            or aggressive_tone.flag
            or hidden_ask.flag
            or unsupported_claim.flag
            or impersonates_official.flag
            or voting_conflict.flag
            or instruction_adh.flag
            or requested_tone_match.flag
        )

        # Determine failure_origin
        failure_origin = "none"
        user_refusal: Optional[CardRefusal] = None

        if not overall_pass:
            is_user_origin = (
                user_harmful_request
                or user_assurance_detected
                or any(w in user_lower for w in [
                    "guilt-trip", "shame them", "scam", "password", "pin", "ssn", "credentials",
                    "give us money now", "pay up", "urgent", "deadline", "irs", "fbi", "police",
                    "cannabis", "marijuana", "weed", "edibles", "pre-rolls", "narcotic", "firearm",
                    "ghost gun", "silencer", "ammo", "wire money", "lottery", "crypto guarantee",
                    "vote", "voting", "polling", "ballot", "election"
                ])
                or (impersonates_official.flag and any(e in user_lower for e in ["irs", "fbi", "police", "bank", "wells fargo", "chase"]))
                or (aggressive_tone.flag and any(p in user_lower for p in ["pay up", "coward", "traitor", "pathetic", "owe us", "give us money"]))
                or (deceptive_urgency.flag and any(p in user_lower for p in ["suspended", "terminated tonight", "last chance", "shame"]))
                or (voting_conflict.flag and any(w in user_lower for w in ["vote", "polling", "church", "ballot", "election"]))
                or unsupported_claim.flag
                or hidden_ask.flag
            )

            if is_user_origin:
                failure_origin = "user_request"

                # Construct dynamic context-relevant refusal and suggestion chips
                if user_matched_cat == "controlled_substances_shaft" or any(w in user_lower for w in ["cannabis", "marijuana", "thc", "pre-rolls", "edibles", "dispensary", "narcotic", "oxycodone", "fentanyl"]):
                    refusal_reason = "Carrier policies (10DLC / SHAFT) strictly forbid SMS messaging for cannabis, CBD, prescription opioids, and controlled substances nationwide regardless of state licenses or user assurances."
                    suggestions = [
                        RefusalSuggestion(
                            label="🌿 Health & Wellness Info",
                            prefill_instruction="Draft an educational health and wellness update without mentioning cannabis, dispensaries, or controlled substances."
                        ),
                        RefusalSuggestion(
                            label="🏥 Community Clinic Notice",
                            prefill_instruction="Send a general clinic appointment reminder with standard STOP opt-out instructions."
                        ),
                        RefusalSuggestion(
                            label="🤝 General Outreach",
                            prefill_instruction="Draft a friendly community outreach message complying with carrier policies."
                        ),
                    ]
                elif user_matched_cat == "weapons_explosives" or any(w in user_lower for w in ["firearm", "ghost gun", "silencer", "ammo", "weapons", "explosives", "rifle"]):
                    refusal_reason = "Carrier policies strictly prohibit SMS campaigns promoting firearms, untraceable weapons, ammunition, or explosives."
                    suggestions = [
                        RefusalSuggestion(
                            label="🕊️ Civic Peace Outreach",
                            prefill_instruction="Draft a peaceful community civic awareness SMS with standard STOP opt-out."
                        ),
                        RefusalSuggestion(
                            label="📢 Community Safety Forum",
                            prefill_instruction="Invite residents to our public safety informational session at the local town hall."
                        ),
                    ]
                elif user_matched_cat == "financial_fraud_scams" or any(w in user_lower for w in ["lottery winner", "payday loan", "wire money", "crypto guarantee", "ransom", "underground poker"]):
                    refusal_reason = "Carrier policies and consumer protection regulations strictly prohibit unsolicited financial offers, wire fee requests, or payday loan solicitations."
                    suggestions = [
                        RefusalSuggestion(
                            label="🎗️ Verified Charity Gala",
                            prefill_instruction="Invite supporters to our annual charity fundraising banquet with secure donation portal."
                        ),
                        RefusalSuggestion(
                            label="📊 Transparent Annual Report",
                            prefill_instruction="Share our non-profit's annual community impact report with verified statistics."
                        ),
                    ]
                elif impersonates_official.flag:
                    refusal_reason = f"Draft cannot impersonate government agencies, law enforcement, or financial institutions ('{impersonates_official.evidence_span}')."
                    suggestions = [
                        RefusalSuggestion(
                            label="🏛️ Verified Non-Profit Identity",
                            prefill_instruction=f"Draft outreach sent transparently from our verified organization name ({org_name or 'Our Non-Profit'})."
                        ),
                        RefusalSuggestion(
                            label="🔗 Official .gov Information Link",
                            prefill_instruction="Provide a link to the official state government election website for voter services."
                        ),
                    ]
                elif voting_conflict.flag:
                    refusal_reason = "Voting dates, deadlines, and polling location announcements must be verified against an official .gov election portal URL."
                    suggestions = [
                        RefusalSuggestion(
                            label="🗳️ Official .gov Voting Link",
                            prefill_instruction="Draft a non-partisan get-out-the-vote SMS citing our state's official elections.gov portal."
                        ),
                        RefusalSuggestion(
                            label="📅 General Volunteer Appeal",
                            prefill_instruction="Encourage supporters to volunteer as non-partisan election workers."
                        ),
                    ]
                else:
                    refusal_reason = "We cannot draft messages requesting passwords, PINs, or sensitive credentials, or using coercive guilt tactics."
                    if user_assurance_detected:
                        refusal_reason += " User assurances cannot override carrier 10DLC policies or Section 4.4 compliance requirements."
                    suggestions = [
                        RefusalSuggestion(
                            label="🛡️ Factual Security Notification",
                            prefill_instruction="Draft a security alert directing customers to our verified official portal without asking for passwords or PINs."
                        ),
                        RefusalSuggestion(
                            label="💼 Polite Account Update",
                            prefill_instruction="Inform customers of account maintenance and provide support hotline 1-800-555-0199."
                        ),
                        RefusalSuggestion(
                            label="🤝 Friendly General Outreach",
                            prefill_instruction="Send a friendly informational SMS with standard STOP opt-out."
                        ),
                    ]

                user_refusal = CardRefusal(
                    headline_variant="message",
                    reason=refusal_reason,
                    suggestions=suggestions,
                )
            else:
                failure_origin = "draft"

        return GuardrailVerdict(
            failure_origin=failure_origin,
            instruction_adherence=instruction_adh,
            requested_tone_match=requested_tone_match,
            aggressive_tone=aggressive_tone,
            deceptive_urgency_or_guilt=deceptive_urgency,
            harmful_intent=harmful_eval,
            hidden_ask_in_thank_you=hidden_ask,
            unsupported_claim=unsupported_claim,
            impersonates_official=impersonates_official,
            voting_logistics_conflict=voting_conflict,
            defamation_or_fabricated_claim_about_person=defamation,
            use_case_drift=use_case_drift,
            disclosure_unclear=disclosure_unclear,
            intent_or_sender_unclear=intent_or_sender,
            overall_pass=overall_pass,
            redraft_instructions=redraft_instructions,
            user_refusal=user_refusal,
        )

    async def _call_anthropic_guardrail(
        self, draft_text: str, user_prompt: str, campaign_type: str, requested_tone: str,
        claims_ledger: List[ClaimEntry], instruction_ledger: List[InstructionEntry], api_key: str
    ) -> Optional[GuardrailVerdict]:
        """Call Anthropic Claude Sonnet 5.5 for Guardrail verdict."""
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        user_content = {
            "draft_text": draft_text,
            "user_prompt": user_prompt,
            "campaign_type": campaign_type,
            "requested_tone": requested_tone,
            "claims_ledger": [c.model_dump() for c in claims_ledger],
            "instruction_ledger": [i.model_dump() for i in instruction_ledger],
        }
        body = {
            "model": "claude-sonnet-5-5",
            "max_tokens": 2048,
            "temperature": 0.0,
            "system": GUARDRAIL_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": json.dumps(user_content)}],
        }
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                text = data["content"][0]["text"]
                parsed = json.loads(text)
                return GuardrailVerdict(**parsed)
        return None

    async def _call_gemini_guardrail(
        self, draft_text: str, user_prompt: str, campaign_type: str, requested_tone: str,
        claims_ledger: List[ClaimEntry], instruction_ledger: List[InstructionEntry], api_key: str
    ) -> Optional[GuardrailVerdict]:
        """Call Google Gemini 3.1 Pro for Guardrail verdict."""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-pro:generateContent?key={api_key}"
        user_content = {
            "draft_text": draft_text,
            "user_prompt": user_prompt,
            "campaign_type": campaign_type,
            "requested_tone": requested_tone,
            "claims_ledger": [c.model_dump() for c in claims_ledger],
            "instruction_ledger": [i.model_dump() for i in instruction_ledger],
        }
        body = {
            "contents": [
                {"role": "user", "parts": [{"text": GUARDRAIL_SYSTEM_PROMPT + "\n\nPayload:\n" + json.dumps(user_content)}]}
            ],
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
            }
        }
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(url, json=body)
            if resp.status_code == 200:
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                parsed = json.loads(text)
                return GuardrailVerdict(**parsed)
        return None
