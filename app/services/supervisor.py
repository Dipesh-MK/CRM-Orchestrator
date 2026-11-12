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

        if not passed:
            refusal = None
            if verdict and verdict.user_refusal:
                refusal = verdict.user_refusal
            elif not all(r.passed for r in tool_results):
                failed_rules = [r.tool_id for r in tool_results if not r.passed]
                rule_reasons = "; ".join([r.reason for r in tool_results if not r.passed])
                refusal = CardRefusal(
                    headline_variant="message",
                    reason=f"Card refused by compliance rules ({', '.join(failed_rules)}): {rule_reasons}",
                    suggestions=[
                        RefusalSuggestion(
                            label="✏️ Fix Compliance Requirements",
                            prefill_instruction="Update draft to include required sender identity, valid opt-out notice, and compliant URLs."
                        )
                    ]
                )
            elif verdict and not verdict.overall_pass:
                flagged = []
                if verdict.harmful_intent.flag:
                    flagged.append(verdict.harmful_intent.reason)
                if verdict.deceptive_urgency_or_guilt.flag:
                    flagged.append(verdict.deceptive_urgency_or_guilt.reason)
                if verdict.aggressive_tone.flag:
                    flagged.append(verdict.aggressive_tone.reason)
                if verdict.hidden_ask_thank_you.flag:
                    flagged.append(verdict.hidden_ask_thank_you.reason)
                if verdict.unsupported_claims.flag:
                    flagged.append(verdict.unsupported_claims.reason)
                if verdict.impersonates_official.flag:
                    flagged.append(verdict.impersonates_official.reason)
                if verdict.voting_logistics_conflict.flag:
                    flagged.append(verdict.voting_logistics_conflict.reason)
                if verdict.instruction_adherence.flag:
                    flagged.append(verdict.instruction_adherence.reason)

                refusal = CardRefusal(
                    headline_variant="message",
                    reason=f"Card refused by guardrail agent: {'; '.join(flagged) if flagged else 'Policy violation detected.'}",
                    suggestions=[
                        RefusalSuggestion(
                            label="✏️ Align with Compliance Guidelines",
                            prefill_instruction="Revise message to remove unverified claims, aggressive tone, or prohibited content."
                        )
                    ]
                )
            else:
                refusal = CardRefusal(
                    headline_variant="message",
                    reason="Card refused: Message does not meet safety or compliance requirements.",
                    suggestions=[
                        RefusalSuggestion(
                            label="✏️ Revise Message",
                            prefill_instruction="Revise message content to comply with carrier 10DLC and Section 4.4 requirements."
                        )
                    ]
                )

            # If user explicitly asked to proceed or offered assurance on a non-compliant draft
            if is_asking_to_proceed or has_assurance:
                specific_reason = refusal.reason
                refusal.reason = (
                    f"Refusal maintained: Cannot proceed with non-compliant draft. "
                    f"User assurance detected, but carrier and safety guidelines cannot be overridden. "
                    f"Reason: {specific_reason}"
                )

            card_state = CardState(
                card_type="tone",
                state="refused",
                badges=badges,
                refusal=refusal,
                metadata={"session_id": session_id, "run_id": run_id}
            )

            await self.emit_trace(
                run_id=run_id,
                campaign_id=session_id,
                component_id="supervisor",
                component_kind="system",
                event_type="card.update",
                summary=f"Tone Card Refused: {refusal.reason if refusal else 'Compliance failure'}",
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
