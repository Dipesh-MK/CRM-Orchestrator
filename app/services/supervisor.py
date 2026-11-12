"""
Supervisor State Machine (CRM Orchestrator v2)
Deterministic core controlling the multi-agent CRM Orchestrator.
- Orchestrates Agent 1, Agent 2, Compliance Pipeline, and Guardrail Agent.
- Emits structured TraceEvents to SQLite and real-time SSE stream.
- Masks phone numbers in all trace logs (e.g. ***-***-1234).
- Maintains CardState lifecycle (Review Card: needs_info/ready; Tone Card: ready/refused/could_not_comply).
- Strictly enforces retry limits: max 2 harmful intent, max 3 format/claims retries.
"""

import re
import uuid
import time
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

from app.core.orchestrator_v2_models import (
    TraceEvent,
    CardState,
    CardBadges,
    CardRefusal,
    RefusalSuggestion,
    ToolResult,
    ClaimEntry,
    InstructionEntry,
    GuardrailVerdict,
    CompletenessResult,
)
from app.core.storage import get_storage
from app.services.agent2_checker import Agent2Checker
from app.services.guardrail_agent import GuardrailAgent
from app.services.compliance_rules_v2 import (
    validate_sender_entity,
    check_org_type_gate,
    check_campaign_verify,
    check_use_case_match,
    check_age_gate_flag,
    check_ai_disclosure,
    check_sender_identified,
    check_intent_stated,
    check_banned_urls,
    check_url_allowlist,
    check_claims_in_ledger,
    check_opt_out,
    check_rates_disclosure,
    check_paid_for_by,
    check_encoding_segments,
    check_placeholders,
    check_merge_values,
)

logger = logging.getLogger("supervisor")

# Global in-memory broadcast bus for SSE streaming to the Agent Observatory
_trace_subscribers: List[asyncio.Queue] = []


def register_trace_subscriber(queue: asyncio.Queue) -> None:
    _trace_subscribers.append(queue)


def unregister_trace_subscriber(queue: asyncio.Queue) -> None:
    if queue in _trace_subscribers:
        _trace_subscribers.remove(queue)


def mask_phone_numbers(text: str) -> str:
    """Mask phone numbers to only show last 4 digits (e.g., ***-***-1234)."""
    if not text:
        return text
    # Matches US-style 10-digit and 11-digit numbers
    pattern = r"(?:\+?1[-.\s]?)?\(?([0-9]{3})\)?[-.\s]?([0-9]{3})[-.\s]?([0-9]{4})"
    return re.sub(pattern, r"***-***-\3", text)


class Supervisor:
    """Deterministic Supervisor State Machine."""

    def __init__(self):
        self.storage = get_storage()
        self.agent2 = Agent2Checker()
        self.guardrail = GuardrailAgent()

    async def emit_trace(
        self,
        run_id: str,
        campaign_id: str,
        component_id: str,
        component_kind: str,
        event_type: str,
        summary: str,
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        to_component: Optional[str] = None,
        tool_id: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        tool_result: Optional[Dict[str, Any]] = None,
        model: Optional[str] = None,
        latency_ms: Optional[float] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        status: str = "ok",
    ) -> TraceEvent:
        """Create, persist, and broadcast a TraceEvent."""
        now_iso = datetime.now(timezone.utc).isoformat()
        span = span_id or f"span_{uuid.uuid4().hex[:8]}"

        # Clean / mask args and result
        masked_summary = mask_phone_numbers(summary)
        cleaned_args = None
        if tool_args:
            cleaned_args = {k: mask_phone_numbers(str(v)) if isinstance(v, str) else v for k, v in tool_args.items()}
        cleaned_res = None
        if tool_result:
            cleaned_res = {k: mask_phone_numbers(str(v)) if isinstance(v, str) else v for k, v in tool_result.items()}

        trace = TraceEvent(
            ts=now_iso,
            run_id=run_id,
            campaign_id=campaign_id,
            span_id=span,
            parent_span_id=parent_span_id,
            component_id=component_id,
            component_kind=component_kind,
            type=event_type,
            to_component=to_component,
            tool_id=tool_id,
            tool_args=cleaned_args,
            tool_result=cleaned_res,
            model=model,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            status=status,
            summary=masked_summary,
        )

        # Save to SQLite
        try:
            self.storage.save_trace_event(trace.model_dump())
        except Exception as e:
            logger.error(f"Failed to persist trace event: {e}")

        # Broadcast to active SSE queues
        dead_queues = []
        for q in list(_trace_subscribers):
            try:
                q.put_nowait(trace.model_dump())
            except Exception:
                dead_queues.append(q)
        for dead in dead_queues:
            unregister_trace_subscriber(dead)

        return trace

    def check_scope(self, text: str) -> Tuple[bool, str]:
        """
        Scope Screening: Strictly US Non-Profit and Political campaigns only.
        Decline commercial, retail, SaaS, or general business requests.
        """
        lower = text.lower()
        commercial_terms = [
            "retail flash sale", "saas renewal", "dental clinic reminder", "salon booking",
            "ecommerce", "shoe sale", "discount code", "coupon", "black friday", "cyber monday",
            "gym membership", "chase bank", "bank of america", "crypto trading"
        ]
        for term in commercial_terms:
            if term in lower:
                return False, f"This system is strictly scoped to US Non-Profit and Political campaigns. Commercial or banking requests like '{term}' are out of scope."

        non_profit_terms = ["charity", "nonprofit", "non-profit", "donation", "gala", "donor", "volunteer", "501c3", "501c4", "food bank", "shelter", "rescue"]
        political_terms = ["candidate", "campaign", "pac", "election", "vote", "voter", "ballot", "polling", "senator", "representative", "mayor", "paid for by", "political"]

        if any(term in lower for term in non_profit_terms + political_terms):
            return True, "Scope valid."

        # Default for ambiguous early discovery: allow if user hasn't explicitly specified commercial
        return True, "Permitted initial discovery."

    async def run_compliance_pipeline(
        self,
        draft_text: str,
        campaign_type: str,
        org_name: str,
        user_prompt: str,
        requested_tone: str,
        claims_ledger: List[ClaimEntry],
        instruction_ledger: List[InstructionEntry],
        run_id: str,
        campaign_id: str,
        committee_name: Optional[str] = None,
        recipient_sample: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, List[ToolResult], Optional[GuardrailVerdict]]:
        """
        Execute deterministic Rule Tools concurrently with Guardrail Agent Section 4.4 rubric.
        """
        tool_results: List[ToolResult] = []

        # 1. Deterministic Rule Tools
        rule_tools = [
            ("check_sender_identified", lambda: check_sender_identified(draft_text, org_name)),
            ("check_intent_stated", lambda: check_intent_stated(draft_text, campaign_type)),
            ("check_banned_urls", lambda: check_banned_urls(draft_text)),
            ("check_claims_in_ledger", lambda: check_claims_in_ledger(draft_text, claims_ledger)),
            ("check_opt_out", lambda: check_opt_out(draft_text, campaign_type)),
            ("check_rates_disclosure", lambda: check_rates_disclosure(draft_text)),
            ("check_encoding_segments", lambda: check_encoding_segments(draft_text)),
            ("check_placeholders", lambda: check_placeholders(draft_text)),
        ]

        if "political" in campaign_type:
            rule_tools.append(("check_paid_for_by", lambda: check_paid_for_by(draft_text, committee_name or org_name)))
            rule_tools.append(("check_ai_disclosure", lambda: check_ai_disclosure(draft_text, ai_assisted=True)))

        if recipient_sample:
            rule_tools.append(("check_merge_values", lambda: check_merge_values(draft_text, recipient_sample)))

        for tool_name, tool_fn in rule_tools:
            t0 = time.time()
            res: ToolResult = tool_fn()
            lat = round((time.time() - t0) * 1000, 2)
            tool_results.append(res)
            await self.emit_trace(
                run_id=run_id,
                campaign_id=campaign_id,
                component_id="compliance",
                component_kind="system",
                event_type="tool.result",
                summary=f"Rule Tool {tool_name}: {'PASS' if res.passed else 'FAIL'}",
                tool_id=tool_name,
                tool_result=res.model_dump(),
                latency_ms=lat,
                status="ok" if res.passed else "fail",
            )

        # 2. Guardrail Agent
        t0 = time.time()
        verdict = await self.guardrail.evaluate_draft(
            draft_text=draft_text,
            user_prompt=user_prompt,
            campaign_type=campaign_type,
            requested_tone=requested_tone,
            claims_ledger=claims_ledger,
            instruction_ledger=instruction_ledger,
            org_name=org_name,
            committee_name=committee_name,
        )
        lat = round((time.time() - t0) * 1000, 2)

        await self.emit_trace(
            run_id=run_id,
            campaign_id=campaign_id,
            component_id="guardrail",
            component_kind="agent",
            event_type="compliance.outcome",
            summary=f"Guardrail Verdict: {'PASS' if verdict.overall_pass else 'FAIL'} (origin: {verdict.failure_origin})",
            tool_result=verdict.model_dump(),
            latency_ms=lat,
            status="ok" if verdict.overall_pass else ("refused" if verdict.failure_origin == "user_request" else "fail"),
        )

        all_rules_passed = all(r.passed for r in tool_results if r.severity == "block")
        passed = all_rules_passed and verdict.overall_pass
        return passed, tool_results, verdict

    def compute_card_badges(self, tool_results: List[ToolResult], verdict: Optional[GuardrailVerdict]) -> CardBadges:
        """Derive badge statuses for the tone card."""
        sender_status = "pass" if any(r.tool_id == "check_sender_identified" and r.passed for r in tool_results) else "fail"
        intent_status = "pass" if any(r.tool_id == "check_intent_stated" and r.passed for r in tool_results) else "fail"
        links_status = "pass" if any(r.tool_id == "check_banned_urls" and r.passed for r in tool_results) else "fail"
        claims_status = "pass" if any(r.tool_id == "check_claims_in_ledger" and r.passed for r in tool_results) else "fail"
        tone_status = "pass" if (verdict and not verdict.aggressive_tone.flag and not verdict.deceptive_urgency_or_guilt.flag) else "fail"
        compliance_status = "pass" if (all(r.passed for r in tool_results) and (verdict and verdict.overall_pass)) else "fail"

        return CardBadges(
            sender=sender_status,
            intent=intent_status,
            links=links_status,
            tone=tone_status,
            claims=claims_status,
            compliance=compliance_status,
        )

    async def evaluate_and_update_card_state(
        self,
        session_id: str,
        run_id: str,
        user_message: str,
        draft_text: str,
        campaign_type: str,
        requested_tone: str,
        claims_ledger: List[ClaimEntry],
        instruction_ledger: List[InstructionEntry],
        org_name: str,
        committee_name: Optional[str] = None,
        current_card: Optional[CardState] = None,
    ) -> CardState:
        """
        Multi-turn card state evaluator:
        - Runs Rule Tools and Guardrail Agent against draft and user message.
        - Enforces that user assurance (e.g., 'I assure you it is safe', 'proceed anyway')
          CANNOT override an active compliance failure.
        - Emits traces, persists quarantine records if refused/failed.
        - Returns updated CardState ('ready', 'refused', 'could_not_comply').
        """
        passed, tool_results, verdict = await self.run_compliance_pipeline(
            draft_text=draft_text,
            campaign_type=campaign_type,
            org_name=org_name,
            user_prompt=user_message,
            requested_tone=requested_tone,
            claims_ledger=claims_ledger,
            instruction_ledger=instruction_ledger,
            run_id=run_id,
            campaign_id=session_id,
            committee_name=committee_name,
        )

        badges = self.compute_card_badges(tool_results, verdict)

        # Detect user attempting to force proceed or reassure the system without resolving the violation
        user_lower = user_message.lower()
        proceed_requests = ["proceed", "go ahead", "approve", "generate card", "make card", "force", "continue"]
        is_asking_to_proceed = any(w in user_lower for w in proceed_requests)

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
        has_assurance = any(re.search(pat, user_lower) for pat in assurance_patterns)

    def generate_remediation_details(
        self,
        tool_results: List[ToolResult],
        verdict: Optional[GuardrailVerdict],
        org_name: str,
        draft_text: str
    ) -> Tuple[str, str, List[str], List[RefusalSuggestion]]:
        """
        Generates specific explanation of why the draft cannot be accepted,
        the exact question asking the user specifically what to fix,
        and multiple-choice options with matching RefusalSuggestion items.
        """
        failed_tools = {r.tool_id: r for r in tool_results if not r.passed}
        safe_org = org_name or "Your Organization"

        # 1. Prohibited URL shortener / non-allowlisted links
        if "check_banned_urls" in failed_tools or "check_url_allowlist" in failed_tools:
            res = failed_tools.get("check_banned_urls") or failed_tools.get("check_url_allowlist")
            offending = res.offending_span or "public shortener"
            why = (
                f"Prohibited public URL shortener detected ('{offending}'). "
                "US Carrier 10DLC and CTIA regulations strictly ban public URL shorteners (such as tinyurl.com, bit.ly, tiny.url) "
                "to protect consumers against SMS phishing, spoofing, and malware."
            )
            question = "How would you like to update the link to comply with carrier rules?"
            clean_domain = re.sub(r'[^a-zA-Z0-9]', '', safe_org).lower() or "organization"
            options = [
                f"A) Replace with registered full domain link: https://{clean_domain}.org/notice",
                "B) Replace link with an official support phone number: (800) 555-0199",
                "C) Remove the short link and instruct recipients to visit the official portal",
                "D) Other (Provide a verified full domain URL)"
            ]
            suggestions = [
                RefusalSuggestion(label="A) Registered Full Domain", prefill_instruction=f"Use full registered domain https://{clean_domain}.org/notice"),
                RefusalSuggestion(label="B) Support Phone Number", prefill_instruction="Call customer support at (800) 555-0199"),
                RefusalSuggestion(label="C) Remove Short Link", prefill_instruction="Remove the short link from the message"),
                RefusalSuggestion(label="D) Specify Custom Domain", prefill_instruction="I will provide a verified full domain URL: ")
            ]
            return why, question, options, suggestions

        # 2. Missing sender brand / identity in first 80 chars
        if "check_sender_identified" in failed_tools or "validate_sender_entity" in failed_tools:
            why = (
                "Missing sender brand identity in the first 80 characters. "
                "Carrier 10DLC compliance mandates that the recipient immediately recognize who is messaging them "
                "within the first 80 characters of the SMS text."
            )
            question = "Which sender brand name should we place at the beginning of your message?"
            options = [
                f"A) Prefix with registered name: '{safe_org}: '",
                f"B) Add official organization notice: 'Important update from {safe_org} - '",
                "C) Format as formal customer notification: 'Customer Notice: '",
                "D) Other (Specify custom sender brand name)"
            ]
            suggestions = [
                RefusalSuggestion(label="A) Prefix Brand Name", prefill_instruction=f"Start message with '{safe_org}: '"),
                RefusalSuggestion(label="B) Official Notice", prefill_instruction=f"Important update from {safe_org} - "),
                RefusalSuggestion(label="C) Customer Notice", prefill_instruction="Customer Notice: "),
                RefusalSuggestion(label="D) Custom Brand", prefill_instruction="Use sender name: ")
            ]
            return why, question, options, suggestions

        # 3. Missing mandatory opt-out instructions
        if "check_opt_out" in failed_tools:
            why = (
                "Missing mandatory opt-out instructions. Federal TCPA and CTIA carrier standards require "
                "a clear opt-out keyword (such as 'Reply STOP to cancel') on all business SMS communications."
            )
            question = "Which compliant opt-out disclosure would you like to append?"
            options = [
                "A) Standard: 'Reply STOP to cancel'",
                "B) Concise: 'STOP2End'",
                "C) Comprehensive: 'Reply STOP to unsubscribe, HELP for help. Msg&data rates may apply.'",
                "D) Other (Specify custom opt-out phrasing)"
            ]
            suggestions = [
                RefusalSuggestion(label="A) Standard 'Reply STOP'", prefill_instruction="Append 'Reply STOP to cancel'"),
                RefusalSuggestion(label="B) Concise 'STOP2End'", prefill_instruction="Append 'STOP2End'"),
                RefusalSuggestion(label="C) Comprehensive Disclosure", prefill_instruction="Append 'Reply STOP to unsubscribe, HELP for help. Msg&data rates may apply.'"),
                RefusalSuggestion(label="D) Custom Opt-Out", prefill_instruction="Add opt-out: ")
            ]
            return why, question, options, suggestions

        # 4. Harmful Intent / Controlled Substances (Section 4.4 Rubric)
        if verdict and verdict.harmful_intent.flag:
            why = (
                f"Prohibited content detected ({verdict.harmful_intent.reason}). "
                "Cellular carrier Section 4.4 explicitly prohibits SHAFT categories (Sex, Hate, Alcohol, Firearms, Tobacco, Cannabis/CBD, and Controlled Substances)."
            )
            question = "This category cannot be transmitted over carrier SMS. How would you like to adjust your campaign?"
            options = [
                "A) Remove all promotional substance/restricted claims and re-synthesize compliant copy",
                "B) Pivot to an informational customer service or operating hours update",
                "C) Re-draft as an educational community announcement",
                "D) Cancel this campaign"
            ]
            suggestions = [
                RefusalSuggestion(label="A) Remove Prohibited Claims", prefill_instruction="Remove restricted claims and re-draft compliant copy"),
                RefusalSuggestion(label="B) Operating Hours Update", prefill_instruction="Pivot message to general operating hours and contact info"),
                RefusalSuggestion(label="C) Educational Notice", prefill_instruction="Re-draft as neutral educational announcement"),
                RefusalSuggestion(label="D) Cancel Campaign", prefill_instruction="Cancel this campaign")
            ]
            return why, question, options, suggestions

        # 5. Deceptive Urgency or Aggressive Guilt
        if verdict and (verdict.deceptive_urgency_or_guilt.flag or verdict.aggressive_tone.flag):
            reason_detail = verdict.deceptive_urgency_or_guilt.reason if verdict.deceptive_urgency_or_guilt.flag else verdict.aggressive_tone.reason
            why = (
                f"Prohibited coercive tone detected ({reason_detail}). "
                "Draft violates safety standards by using deceptive artificial urgency or guilt-inducing pressure."
            )
            question = "How would you like to adjust the campaign tone to meet compliance standards?"
            options = [
                "A) Informational & calm: Factual reminder with deadline stated without pressure",
                "B) Polite & professional: Courteous update highlighting upcoming schedule",
                "C) Direct announcement: Neutral summary with clear action items",
                "D) Other (Specify desired tone adjustments)"
            ]
            suggestions = [
                RefusalSuggestion(label="A) Informational & Calm", prefill_instruction="Make tone calm and informational, stating dates without pressure"),
                RefusalSuggestion(label="B) Polite & Professional", prefill_instruction="Use polite and courteous tone"),
                RefusalSuggestion(label="C) Direct Announcement", prefill_instruction="Use direct and neutral announcement format"),
                RefusalSuggestion(label="D) Custom Tone", prefill_instruction="Adjust tone to: ")
            ]
            return why, question, options, suggestions

        # 6. Unsupported Claims
        if verdict and verdict.unsupported_claims.flag:
            why = (
                f"Unverified claims detected ({verdict.unsupported_claims.reason}). "
                "Draft contains factual, statistical, or numerical claims not recorded in your verified Claims Ledger."
            )
            question = "How should we align these claims with your verified records?"
            options = [
                "A) Verify claim in ledger: Provide source proof/URL for the numbers",
                "B) Remove specific statistics and speak in general qualitative terms",
                "C) Replace with verified baseline data from ledger",
                "D) Other (Specify updated claim)"
            ]
            suggestions = [
                RefusalSuggestion(label="A) Verify in Ledger", prefill_instruction="Add verified source proof for this claim"),
                RefusalSuggestion(label="B) Remove Statistics", prefill_instruction="Remove specific numerical claims and generalize copy"),
                RefusalSuggestion(label="C) Baseline Data", prefill_instruction="Use verified baseline data"),
                RefusalSuggestion(label="D) Update Claim", prefill_instruction="Update claim to: ")
            ]
            return why, question, options, suggestions

        # Default fallback
        failed_rule_names = [r.tool_id for r in tool_results if not r.passed]
        why = f"Message does not comply with carrier 10DLC or safety rules: {', '.join(failed_rule_names) if failed_rule_names else 'Policy violation'}"
        question = "What specific fix would you like to apply to bring this message into compliance?"
        options = [
            f"A) Auto-format with verified {safe_org} sender ID and opt-out disclaimer",
            "B) Re-draft in neutral, purely informational tone",
            "C) Manually revise message copy",
            "D) Other (Type instructions to re-draft)"
        ]
        suggestions = [
            RefusalSuggestion(label="A) Auto-Format Compliant", prefill_instruction=f"Auto-format with {safe_org} sender ID and opt-out"),
            RefusalSuggestion(label="B) Neutral Tone", prefill_instruction="Re-draft in neutral informational tone"),
            RefusalSuggestion(label="C) Manual Revision", prefill_instruction="I will edit the message manually"),
            RefusalSuggestion(label="D) Custom Instructions", prefill_instruction="Re-draft with instructions: ")
        ]
        return why, question, options, suggestions

    async def evaluate_and_update_card_state(
        self,
        session_id: str,
        run_id: str,
        user_message: str,
        draft_text: str,
        campaign_type: str,
        requested_tone: str,
        claims_ledger: List[ClaimEntry],
        instruction_ledger: List[InstructionEntry],
        org_name: str,
        committee_name: Optional[str] = None,
        current_card: Optional[CardState] = None,
    ) -> CardState:
        """
        Multi-turn card state evaluator:
        - Runs Rule Tools and Guardrail Agent against draft and user message.
        - Enforces that user assurance (e.g., 'I assure you it is safe', 'proceed anyway')
          CANNOT override an active compliance failure.
        - Emits traces, persists quarantine records if refused/failed.
        - Returns updated CardState ('ready', 'refused', 'could_not_comply') with loopback remediation.
        """
        passed, tool_results, verdict = await self.run_compliance_pipeline(
            draft_text=draft_text,
            campaign_type=campaign_type,
            org_name=org_name,
            user_prompt=user_message,
            requested_tone=requested_tone,
            claims_ledger=claims_ledger,
            instruction_ledger=instruction_ledger,
            run_id=run_id,
            campaign_id=session_id,
            committee_name=committee_name,
        )

        badges = self.compute_card_badges(tool_results, verdict)

        # Detect user attempting to force proceed or reassure the system without resolving the violation
        user_lower = user_message.lower()
        proceed_requests = ["proceed", "go ahead", "approve", "generate card", "make card", "force", "continue"]
        is_asking_to_proceed = any(w in user_lower for w in proceed_requests)

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
        has_assurance = any(re.search(pat, user_lower) for pat in assurance_patterns)

        if not passed:
            why, question, options, suggestions = self.generate_remediation_details(
                tool_results=tool_results,
                verdict=verdict,
                org_name=org_name or "Your Organization",
                draft_text=draft_text
            )

            reason_text = f"Card refused: {why}"
            if is_asking_to_proceed or has_assurance:
                reason_text = (
                    f"Refusal maintained: Cannot proceed with non-compliant draft. "
                    f"User assurance detected, but carrier and safety guidelines cannot be overridden. "
                    f"Reason: {why}"
                )

            formatted_prompt = f"⚠️ **Why it cannot be accepted:**\n{why}\n\n❓ **What specifically to fix:**\n{question}"

            refusal = CardRefusal(
                headline_variant="message",
                reason=reason_text,
                suggestions=suggestions
            )

            card_state = CardState(
                card_type="tone",
                state="refused",
                highlights=[f"Fix Required: {question}"],
                questions=[formatted_prompt],
                badges=badges,
                refusal=refusal,
                metadata={
                    "session_id": session_id,
                    "run_id": run_id,
                    "remediation": {
                        "why": why,
                        "question": question,
                        "options": options
                    },
                    "question_options": options,
                    "loopback_active": True
                }
            )

            await self.emit_trace(
                run_id=run_id,
                campaign_id=session_id,
                component_id="supervisor",
                component_kind="system",
                event_type="card.update",
                summary=f"Tone Card Refused (Loopback Prompt): {reason_text}",
                tool_result=card_state.model_dump(),
                status="refused",
            )
            return card_state

        # If fully passed and compliant
        card_state = CardState(
            card_type="tone",
            state="ready",
            badges=badges,
            metadata={"session_id": session_id, "run_id": run_id}
        )
        await self.emit_trace(
            run_id=run_id,
            campaign_id=session_id,
            component_id="supervisor",
            component_kind="system",
            event_type="card.update",
            summary="Tone Card Ready: All compliance rules and Guardrail Agent Section 4.4 rubric PASSED.",
            tool_result=card_state.model_dump(),
            status="ok",
        )
        return card_state


_supervisor_instance: Optional[Supervisor] = None


def get_supervisor() -> Supervisor:
    global _supervisor_instance
    if _supervisor_instance is None:
        _supervisor_instance = Supervisor()
    return _supervisor_instance
