"""
Agent 2: Completeness Checker
Responsible for:
- Tracking and maintaining the user Instruction Ledger across conversation turns.
- Deterministic and semantic checking of required campaign slots.
- Checking claims ledger consistency (e.g. unverified claims, missing source URLs).
- Emitting structured CompletenessResult and questions (max 3 collection rounds).
- Executing diff_card_vs_ledger to verify review card accuracy before user presentation.
"""

import re
import uuid
from typing import Dict, Any, List, Optional
from app.core.orchestrator_v2_models import (
    InstructionEntry,
    ClaimEntry,
    MissingSlotInfo,
    CompletenessResult,
    ToolResult,
)
from app.core.storage import get_storage

REQUIRED_SLOTS_BY_TYPE: Dict[str, List[str]] = {
    "common": ["sender_name", "campaign_type", "intent_label", "tone_preference"],
    "event": ["event_name", "event_date_time", "location", "rsvp_or_info_link"],
    "donation_appeal": ["cause_purpose", "donation_link"],
    "donation_thank_you": ["gift_reference_or_amount"],
    "political_outreach": ["committee_or_candidate", "paid_for_by_committee", "subject", "source_url"],
    "political_fundraising": ["committee_or_candidate", "paid_for_by_committee", "donation_link"],
}

SLOT_PROMPTS: Dict[str, Dict[str, str]] = {
    "sender_name": {
        "why": "Recipients must know immediately which organization is contacting them (CTIA & 10DLC requirement).",
        "question": "What is the official sender or organization name to display in the message?",
    },
    "campaign_type": {
        "why": "Determines mandatory disclosures, compliance gates, and permissible templates.",
        "question": "Is this campaign an event, donation appeal, donation thank-you, political outreach, or political fundraising?",
    },
    "intent_label": {
        "why": "SMS regulations require a transparent and immediate intent statement.",
        "question": "What is the primary call-to-action or intent of this message?",
    },
    "tone_preference": {
        "why": "Ensures tone alignment across Urgent & Direct, Friendly & Casual, or Professional & Polite.",
        "question": "Which tone style do you prefer: Urgent & Direct, Friendly & Casual, or Professional & Polite?",
    },
    "event_name": {
        "why": "Event name is necessary for recipient identification of the event.",
        "question": "What is the official name of the event?",
    },
    "event_date_time": {
        "why": "Recipients require a clear date and time for event logistics.",
        "question": "When does the event take place (date and time)?",
    },
    "location": {
        "why": "Event address or virtual location is required for attendees.",
        "question": "Where is the event taking place (physical address or virtual meeting link)?",
    },
    "rsvp_or_info_link": {
        "why": "A verified RSVP or details URL is required for SMS event invitations.",
        "question": "What website link should recipients use to RSVP or view event details?",
    },
    "cause_purpose": {
        "why": "Non-profit donation appeals must clearly state the cause or mission supported.",
        "question": "What specific charitable cause or community program will these donations fund?",
    },
    "donation_link": {
        "why": "A verified donation link on your registered domain is required for fundraising appeals.",
        "question": "What is your official, secure donation webpage URL?",
    },
    "gift_reference_or_amount": {
        "why": "Donation acknowledgments must specify the gift reference, campaign, or verified amount.",
        "question": "What gift reference, recent contribution amount, or campaign are you acknowledging?",
    },
    "committee_or_candidate": {
        "why": "FEC and state campaign regulations require candidate or committee identification.",
        "question": "What is the official candidate or PAC/committee name sponsoring this message?",
    },
    "paid_for_by_committee": {
        "why": "Political SMS broadcasts legally require an explicit 'Paid for by [Committee]' disclaimer.",
        "question": "What is the exact legal 'Paid for by...' committee name for this political message?",
    },
    "subject": {
        "why": "Political outreach campaigns must have a clearly declared policy or campaign subject.",
        "question": "What is the specific topic, bill, or election issue for this outreach?",
    },
    "source_url": {
        "why": "Voting logistics and ballot information must cite an official government (.gov) source.",
        "question": "What official state or county election website (.gov) verifies this voting information?",
    },
}


class Agent2Checker:
    """Agent 2 - Completeness Checker service."""

    def __init__(self):
        self.storage = get_storage()

    def get_instruction_ledger(self, session_id: str) -> List[InstructionEntry]:
        """Retrieve instruction ledger for session."""
        session = self.storage.get_session(session_id)
        if not session or not session.get("data"):
            return []
        raw_ledger = session["data"].get("instruction_ledger", [])
        return [InstructionEntry(**item) if isinstance(item, dict) else item for item in raw_ledger]

    def save_instruction_ledger(self, session_id: str, ledger: List[InstructionEntry]) -> None:
        """Persist instruction ledger into session state."""
        session = self.storage.get_session(session_id)
        if session:
            data = session.get("data", {})
            data["instruction_ledger"] = [item.model_dump() for item in ledger]
            self.storage.update_session_data(session_id, data)

    def update_instruction_ledger(
        self,
        session_id: str,
        turn_index: int,
        user_message: str,
        current_ledger: Optional[List[InstructionEntry]] = None,
    ) -> List[InstructionEntry]:
        """
        Extract discrete instructions from the user message and update the ledger.
        Categorizes tone, length, inclusion/exclusion, disclaimer, style, etc.
        Supersedes contradicting older instructions when newer ones arrive.
        """
        ledger = current_ledger if current_ledger is not None else self.get_instruction_ledger(session_id)
        text = user_message.strip()
        if not text:
            return ledger

        new_entries: List[InstructionEntry] = []
        lower = text.lower()

        # Tone instructions
        if any(w in lower for w in ["urgent", "hurry", "asap", "deadline"]):
            new_entries.append(
                InstructionEntry(
                    instruction_id=f"inst_{uuid.uuid4().hex[:8]}",
                    turn_index=turn_index,
                    raw_instruction="Use an urgent and direct tone",
                    category="tone",
                    status="active",
                )
            )
        elif any(w in lower for w in ["friendly", "casual", "warm", "approachable"]):
            new_entries.append(
                InstructionEntry(
                    instruction_id=f"inst_{uuid.uuid4().hex[:8]}",
                    turn_index=turn_index,
                    raw_instruction="Use a friendly and casual tone",
                    category="tone",
                    status="active",
                )
            )
        elif any(w in lower for w in ["professional", "polite", "formal"]):
            new_entries.append(
                InstructionEntry(
                    instruction_id=f"inst_{uuid.uuid4().hex[:8]}",
                    turn_index=turn_index,
                    raw_instruction="Use a professional and polite tone",
                    category="tone",
                    status="active",
                )
            )

        # Length / Conciseness instructions
        if any(w in lower for w in ["short", "brief", "concise", "under 160", "1 segment"]):
            new_entries.append(
                InstructionEntry(
                    instruction_id=f"inst_{uuid.uuid4().hex[:8]}",
                    turn_index=turn_index,
                    raw_instruction="Keep message under 160 characters (1 SMS segment)",
                    category="length",
                    status="active",
                )
            )

        # Exclusion instructions
        no_match = re.search(r"(?:do not include|don\'t include|exclude|no)\s+([a-zA-Z0-9\s,\-\$\%]+)", lower)
        if no_match:
            new_entries.append(
                InstructionEntry(
                    instruction_id=f"inst_{uuid.uuid4().hex[:8]}",
                    turn_index=turn_index,
                    raw_instruction=f"Exclude: {no_match.group(1).strip()}",
                    category="exclude_topic",
                    status="active",
                )
            )

        # Generic capture if substantive directive
        if not new_entries and len(text) > 5 and not text.startswith("/"):
            new_entries.append(
                InstructionEntry(
                    instruction_id=f"inst_{uuid.uuid4().hex[:8]}",
                    turn_index=turn_index,
                    raw_instruction=text[:120],
                    category="other",
                    status="active",
                )
            )

        # Supersede older matching category instructions if new one arrives
        for new_item in new_entries:
            if new_item.category in ("tone", "length"):
                for existing in ledger:
                    if existing.category == new_item.category and existing.status == "active":
                        existing.status = "superseded"
            ledger.append(new_item)

        self.save_instruction_ledger(session_id, ledger)
        return ledger

    def check_required_slots(
        self,
        campaign_type: str,
        slots: Dict[str, Any],
        claims_ledger: Optional[List[ClaimEntry]] = None,
    ) -> CompletenessResult:
        """
        Verifies presence of mandatory slots for the campaign category.
        Also checks that numbers, dates, currency, and voting logistics are user-verified in Claims Ledger.
        """
        missing: List[MissingSlotInfo] = []
        ambiguities: List[str] = []

        # 1. Check Common Slots
        for slot in REQUIRED_SLOTS_BY_TYPE["common"]:
            val = slots.get(slot)
            if not val or (isinstance(val, str) and not val.strip()):
                info = SLOT_PROMPTS.get(slot, {"why": f"Mandatory slot {slot} missing.", "question": f"Please provide {slot}."})
                missing.append(MissingSlotInfo(field=slot, why=info["why"], suggested_question=info["question"]))

        # 2. Check Specific Campaign Slots
        specific_slots = REQUIRED_SLOTS_BY_TYPE.get(campaign_type, [])
        for slot in specific_slots:
            val = slots.get(slot)
            if not val or (isinstance(val, str) and not val.strip()):
                info = SLOT_PROMPTS.get(slot, {"why": f"Mandatory slot {slot} missing for {campaign_type}.", "question": f"Please provide {slot}."})
                missing.append(MissingSlotInfo(field=slot, why=info["why"], suggested_question=info["question"]))

        # 3. Check Claims Ledger verification
        claims = claims_ledger or []
        for claim in claims:
            if not claim.verified_by_user:
                ambiguities.append(f"Claim '{claim.raw_value}' ({claim.claim_type}) has not been verified by user.")
            if claim.claim_type == "voting_logistics":
                if not claim.source_url or not claim.source_url.lower().endswith(".gov") and ".gov/" not in claim.source_url.lower():
                    ambiguities.append(f"Voting logistics claim '{claim.raw_value}' requires an official .gov source URL.")

        is_complete = (len(missing) == 0 and len(ambiguities) == 0)
        return CompletenessResult(
            complete=is_complete,
            missing=missing,
            ambiguities=ambiguities,
            claims_ledger=claims,
        )

    def diff_card_vs_ledger(
        self,
        card_fields: Dict[str, Any],
        instruction_ledger: List[InstructionEntry],
        claims_ledger: List[ClaimEntry],
    ) -> ToolResult:
        """
        Compares the candidate Review Card contents against active instructions and verified claims.
        Fails if mandatory directives were dropped or altered.
        """
        missing_instructions: List[str] = []
        card_text_dump = " ".join(str(v) for v in card_fields.values()).lower()

        for inst in instruction_ledger:
            if inst.status != "active":
                continue
            if inst.category == "exclude_topic":
                forbidden = inst.raw_instruction.replace("Exclude:", "").strip().lower()
                if forbidden and forbidden in card_text_dump:
                    missing_instructions.append(f"Card still includes excluded topic: '{forbidden}'")
            elif inst.category == "tone":
                tone_val = card_fields.get("tone_preference", "").lower()
                if "urgent" in inst.raw_instruction.lower() and "urgent" not in tone_val:
                    missing_instructions.append("Card tone does not reflect user's Urgent & Direct directive")
                elif "friendly" in inst.raw_instruction.lower() and "friendly" not in tone_val:
                    missing_instructions.append("Card tone does not reflect user's Friendly & Casual directive")
                elif "professional" in inst.raw_instruction.lower() and "professional" not in tone_val:
                    missing_instructions.append("Card tone does not reflect user's Professional & Polite directive")

        # Check claims represented in card
        for claim in claims_ledger:
            if claim.verified_by_user and claim.raw_value.lower() not in card_text_dump:
                # If it's a critical amount or date, check presence
                if claim.claim_type in ("currency", "date", "voting_logistics"):
                    missing_instructions.append(f"Verified claim '{claim.raw_value}' missing from review card")

        if missing_instructions:
            return ToolResult(
                tool_id="diff_card_vs_ledger",
                category="policy_gate",
                passed=False,
                reason="Review card differs from instruction/claims ledger: " + "; ".join(missing_instructions),
                severity="block",
            )

        return ToolResult(
            tool_id="diff_card_vs_ledger",
            category="policy_gate",
            passed=True,
            reason="Review card fully matches all active user instructions and verified claims.",
            severity="warn",
        )
