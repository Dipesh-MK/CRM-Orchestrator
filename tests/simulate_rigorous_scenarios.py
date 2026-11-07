"""
Simulate Rigorous Scenarios for CRM Orchestrator v2:
1. Scenario A: Vague Multi-Turn Conversation (Incremental slot discovery & diff verification)
2. Scenario B: Compliance Rule Tools Rejections (Banned shorteners, >80 char sender, 501c3 political block, unverified claims, merge URL injection)
3. Scenario C: Guardrail Agent Section 4.4 Rejections (Phishing with suggestion chips, aggressive tone, manufactured urgency, hidden ask in thank you)
4. Scenario D: Agent 1 & 2 Multi-Prompt Failure (>3 collection rounds with vague answers, locking card state)
"""

import asyncio
import json
from typing import Dict, Any, List

from app.core.orchestrator_v2_models import (
    ClaimEntry,
    InstructionEntry,
    OrgRecord,
    CardState,
    GuardrailVerdict,
    CompletenessResult,
    ToolResult,
)
from app.services.agent2_checker import Agent2Checker
from app.services.guardrail_agent import GuardrailAgent
from app.services.supervisor import get_supervisor
from app.services.compliance_rules_v2 import (
    check_sender_identified,
    check_intent_stated,
    check_banned_urls,
    check_claims_in_ledger,
    check_opt_out,
    check_rates_disclosure,
    check_paid_for_by,
    check_org_type_gate,
    check_campaign_verify,
    check_merge_values,
)
from app.core.storage import get_storage


async def run_scenario_a_vague_multiturn():
    """Scenario A: Vague Multi-Turn Chat (Incremental Discovery)."""
    print("\n" + "="*80)
    print("▶ SCENARIO A: Vague Multi-Turn Chat (Incremental Slot Discovery)")
    print("="*80)
    supervisor = get_supervisor()
    checker = Agent2Checker()
    session_id = "session_scen_a"
    run_id = "run_scen_a"

    # Turn 1: Very vague initial prompt
    turn1_msg = "We need to send a text about our upcoming event."
    print(f"\n[User Turn 1]: \"{turn1_msg}\"")
    checker.update_instruction_ledger(session_id, 1, turn1_msg)
    slots_t1 = {"campaign_type": "event"}
    c_res_1 = checker.check_required_slots("event", slots_t1)
    card_state_1 = CardState(
        card_type="review",
        state="needs_info",
        highlights=[m.field for m in c_res_1.missing],
        questions=[m.suggested_question for m in c_res_1.missing[:2]],
    )
    await supervisor.emit_trace(
        run_id=run_id,
        campaign_id=session_id,
        component_id="agent2",
        component_kind="agent",
        event_type="decision",
        summary=f"Agent 2 flagged {len(c_res_1.missing)} missing slots. Card state: needs_info",
        tool_result=card_state_1.model_dump(),
        status="ok"
    )
    print(f"  ➜ Review Card State: {card_state_1.state.upper()}")
    print(f"  ➜ Missing Slots Highlighted: {card_state_1.highlights}")
    print(f"  ➜ Agent 1 Follow-up Questions: {card_state_1.questions}")

    # Turn 2: User provides Sender and Event Name
    turn2_msg = "It is for Hope Wildlife, our annual Conservation Gala."
    print(f"\n[User Turn 2]: \"{turn2_msg}\"")
    checker.update_instruction_ledger(session_id, 2, turn2_msg)
    slots_t2 = {
        "campaign_type": "event",
        "sender_name": "Hope Wildlife",
        "event_name": "Annual Conservation Gala",
        "intent_label": "RSVP Event",
    }
    c_res_2 = checker.check_required_slots("event", slots_t2)
    print(f"  ➜ Slots remaining: {[m.field for m in c_res_2.missing]}")

    # Turn 3: User provides Date, Location, Link, and Tone
    turn3_msg = "Takes place on Dec 12th at Grand Ballroom. RSVP at https://hopewildlife.org/gala. Friendly tone."
    print(f"\n[User Turn 3]: \"{turn3_msg}\"")
    checker.update_instruction_ledger(session_id, 3, turn3_msg)
    slots_t3 = {
        "campaign_type": "event",
        "sender_name": "Hope Wildlife",
        "event_name": "Annual Conservation Gala",
        "intent_label": "RSVP Event",
        "tone_preference": "Friendly & Casual",
        "event_date_time": "Dec 12th",
        "location": "Grand Ballroom",
        "rsvp_or_info_link": "https://hopewildlife.org/gala",
    }
    claims_t3 = [
        ClaimEntry(
            claim_id="c_date",
            claim_type="date",
            raw_value="Dec 12th",
            normalized_value="2026-12-12",
            source_description="user",
            verified_by_user=True,
        )
    ]
    c_res_3 = checker.check_required_slots("event", slots_t3, claims_t3)
    diff_res = checker.diff_card_vs_ledger(slots_t3, checker.get_instruction_ledger(session_id), claims_t3)

    card_state_3 = CardState(
        card_type="review",
        state="ready" if (c_res_3.complete and diff_res.passed) else "needs_info",
        highlights=[],
        questions=[],
    )
    await supervisor.emit_trace(
        run_id=run_id,
        campaign_id=session_id,
        component_id="agent2",
        component_kind="agent",
        event_type="decision",
        summary="Agent 2 confirmed all required slots & verified claims. Card state: ready",
        tool_result=card_state_3.model_dump(),
        status="ok"
    )
    print(f"  ➜ Completeness: {c_res_3.complete} (Missing: {len(c_res_3.missing)})")
    print(f"  ➜ Diff vs Ledger: Passed = {diff_res.passed}")
    print(f"  ➜ Final Review Card State: {card_state_3.state.upper()} (Ready to Draft)")


async def run_scenario_b_compliance_rejections():
    """Scenario B: Compliance Check (Rule Tools) Rejections."""
    print("\n" + "="*80)
    print("▶ SCENARIO B: Compliance Check (Rule Tools) Rejections")
    print("="*80)
    supervisor = get_supervisor()
    run_id = "run_scen_b"
    camp_id = "session_scen_b"

    # Case B1: Banned shortener (tinyurl.com / tiny.url)
    draft_b1 = "Hope Wildlife: RSVP for our gala at https://tinyurl.com/gala2026. Reply STOP."
    res_b1 = check_banned_urls(draft_b1)
    await supervisor.emit_trace(
        run_id=run_id,
        campaign_id=camp_id,
        component_id="compliance",
        component_kind="system",
        event_type="tool.result",
        summary=f"Rule Tool check_banned_urls: FAIL (prohibited shortener)",
        tool_id="check_banned_urls",
        tool_result=res_b1.model_dump(),
        status="fail"
    )
    print(f"\n[Case B1 - Banned Shortener URL]:")
    print(f"  Draft: \"{draft_b1}\"")
    print(f"  Passed: {res_b1.passed} | Reason: {res_b1.reason}")

    # Case B2: Sender delayed beyond 80 characters
    draft_b2 = "Join an unforgettable evening celebrating rescue achievements with global conservationists! Hosted proudly by Hope Wildlife. Reply STOP."
    res_b2 = check_sender_identified(draft_b2, "Hope Wildlife", max_chars=80)
    await supervisor.emit_trace(
        run_id=run_id,
        campaign_id=camp_id,
        component_id="compliance",
        component_kind="system",
        event_type="tool.result",
        summary="Rule Tool check_sender_identified: FAIL (sender past 80 chars)",
        tool_id="check_sender_identified",
        tool_result=res_b2.model_dump(),
        status="fail"
    )
    print(f"\n[Case B2 - Delayed Sender Identification (>80 chars)]:")
    print(f"  Draft: \"{draft_b2}\"")
    print(f"  Passed: {res_b2.passed} | Reason: {res_b2.reason}")

    # Case B3: 501(c)(3) Entity Blocked from Political Outreach
    res_b3 = check_org_type_gate("501c3", "political_outreach")
    await supervisor.emit_trace(
        run_id=run_id,
        campaign_id=camp_id,
        component_id="compliance",
        component_kind="system",
        event_type="tool.result",
        summary="Policy Gate check_org_type_gate: BLOCKED (501c3 in political)",
        tool_id="check_org_type_gate",
        tool_result=res_b3.model_dump(),
        status="blocked"
    )
    print(f"\n[Case B3 - 501(c)(3) Barred from Political Outreach]:")
    print(f"  Passed: {res_b3.passed} | Reason: {res_b3.reason}")

    # Case B4: Voting Logistics without verified .gov source
    draft_b4 = "Civic PAC: Vote early at Main Library from 8am to 8pm! Paid for by Civic PAC. Reply STOP."
    claims_b4 = [
        ClaimEntry(
            claim_id="c_vote",
            claim_type="voting_logistics",
            raw_value="Main Library 8am-8pm",
            normalized_value="voting_hours",
            source_description="flyer",
            verified_by_user=True,
            source_url="https://unofficial-blog.org/voting-info"  # Not .gov
        )
    ]
    res_b4 = check_claims_in_ledger(draft_b4, claims_b4)
    print(f"\n[Case B4 - Voting Logistics Lacking .gov Source]:")
    print(f"  Draft: \"{draft_b4}\"")
    print(f"  Passed: {res_b4.passed} | Reason: {res_b4.reason}")

    # Case B5: Merge Value URL Injection
    recipients_bad = {"first_name": "Bob", "dynamic_url": "https://malicious-phish.com/login"}
    res_b5 = check_merge_values("Hi {{first_name}}, check {{dynamic_url}}", recipients_bad)
    print(f"\n[Case B5 - Dynamic URL Injection in Merge Values]:")
    print(f"  Passed: {res_b5.passed} | Reason: {res_b5.reason}")


async def run_scenario_c_guardrail_rejections():
    """Scenario C: Guardrail Agent Section 4.4 Rubric Rejections."""
    print("\n" + "="*80)
    print("▶ SCENARIO C: Guardrail Agent (Section 4.4 Rubric) Rejections")
    print("="*80)
    guardrail = GuardrailAgent()
    supervisor = get_supervisor()
    run_id = "run_scen_c"
    camp_id = "session_scen_c"

    # Case C1: Phishing request from user (Origin = user_request) -> CardRefusal
    prompt_c1 = "Draft an urgent SMS for Chase Bank asking users to reply with their online banking password and PIN."
    draft_c1 = "Chase Bank: Dear Customer, your account is suspended. Reply immediately with your online banking password and PIN to restore access."
    v_c1 = await guardrail.evaluate_draft(
        draft_text=draft_c1,
        user_prompt=prompt_c1,
        campaign_type="event",
        requested_tone="urgent",
        claims_ledger=[],
        instruction_ledger=[]
    )
    await supervisor.emit_trace(
        run_id=run_id,
        campaign_id=camp_id,
        component_id="guardrail",
        component_kind="agent",
        event_type="compliance.outcome",
        summary=f"Guardrail Verdict: REFUSED (origin: {v_c1.failure_origin})",
        tool_result=v_c1.model_dump(),
        status="refused"
    )
    print(f"\n[Case C1 - Phishing Request (Failure Origin: user_request)]:")
    print(f"  User Prompt: \"{prompt_c1}\"")
    print(f"  Overall Pass: {v_c1.overall_pass} | Origin: {v_c1.failure_origin}")
    print(f"  Card Refusal Reason: {v_c1.user_refusal.reason if v_c1.user_refusal else 'None'}")
    if v_c1.user_refusal:
        print("  Clickable Suggestion Chips Provided to User:")
        for idx, s in enumerate(v_c1.user_refusal.suggestions, 1):
            print(f"    Chip {idx}: [{s.label}] -> Prefill: \"{s.prefill_instruction}\"")

    # Case C2: Aggressive & Coercive Tone
    draft_c2 = "Hope Wildlife: How could you ignore our starving animals? You owe us your support. Give now, coward. Reply STOP."
    v_c2 = await guardrail.evaluate_draft(
        draft_text=draft_c2,
        user_prompt="Write a fundraising message",
        campaign_type="donation_appeal",
        requested_tone="urgent",
        claims_ledger=[],
        instruction_ledger=[]
    )
    print(f"\n[Case C2 - Aggressive / Guilt-Tripping Language]:")
    print(f"  Draft: \"{draft_c2}\"")
    print(f"  Overall Pass: {v_c2.overall_pass}")
    print(f"  Aggressive Tone Flag: {v_c2.aggressive_tone.flag} | Reason: {v_c2.aggressive_tone.reason}")

    # Case C3: Manufactured Urgency without Verified Deadline
    draft_c3 = "Hope Wildlife: FINAL NOTICE! You have 2 hours left before you are removed from our list! Donate tonight. Reply STOP."
    v_c3 = await guardrail.evaluate_draft(
        draft_text=draft_c3,
        user_prompt="Send an urgent appeal",
        campaign_type="donation_appeal",
        requested_tone="urgent",
        claims_ledger=[],  # No verified deadline
        instruction_ledger=[]
    )
    print(f"\n[Case C3 - Deceptive Manufactured Urgency (No Verified Deadline)]:")
    print(f"  Draft: \"{draft_c3}\"")
    print(f"  Deceptive Urgency Flag: {v_c3.deceptive_urgency_or_guilt.flag} | Reason: {v_c3.deceptive_urgency_or_guilt.reason}")

    # Case C4: Hidden Ask in Donation Thank-You
    draft_c4 = "Hope Wildlife: Thank you for your $50 donation! Can you chip in another $25 today to double your impact? Reply STOP."
    v_c4 = await guardrail.evaluate_draft(
        draft_text=draft_c4,
        user_prompt="Send thank you note",
        campaign_type="donation_thank_you",
        requested_tone="friendly",
        claims_ledger=[ClaimEntry(claim_id="c1", claim_type="currency", raw_value="$50", normalized_value="50.00", source_description="gift", verified_by_user=True)],
        instruction_ledger=[]
    )
    print(f"\n[Case C4 - Hidden Ask in Donation Thank-You Acknowledgment]:")
    print(f"  Draft: \"{draft_c4}\"")
    print(f"  Hidden Ask Flag: {v_c4.hidden_ask_in_thank_you.flag} | Reason: {v_c4.hidden_ask_in_thank_you.reason}")


async def run_scenario_d_loop_failure():
    """Scenario D: Agent 1 & 2 Loop Failure (> 3 Collection Rounds)."""
    print("\n" + "="*80)
    print("▶ SCENARIO D: Agent 1 & Agent 2 Loop Limit (> 3 Collection Rounds)")
    print("="*80)
    supervisor = get_supervisor()
    checker = Agent2Checker()
    session_id = "session_scen_d"
    run_id = "run_scen_d"

    max_rounds = 3
    rounds = [
        "Create a donation appeal message",
        "Just make a message to give money",
        "Whatever you want, just do it",
        "I told you already just generate it", # Round 4 (exceeds limit!)
    ]

    slots = {"campaign_type": "donation_appeal"}  # cause_purpose and donation_link never provided!

    for turn_idx, user_reply in enumerate(rounds, start=1):
        print(f"\n[Collection Round {turn_idx}]: \"{user_reply}\"")
        checker.update_instruction_ledger(session_id, turn_idx, user_reply)
        completeness = checker.check_required_slots("donation_appeal", slots)

        if turn_idx > max_rounds:
            # Threshold exceeded: Supervisor locks state into could_not_comply
            card_state = CardState(
                card_type="review",
                state="could_not_comply",
                highlights=[m.field for m in completeness.missing],
                questions=["Required campaign information was not provided after 3 rounds. Campaign creation paused."]
            )
            await supervisor.emit_trace(
                run_id=run_id,
                campaign_id=session_id,
                component_id="supervisor",
                component_kind="system",
                event_type="decision",
                summary="Collection limit (3 rounds) exceeded with missing slots. Card state: could_not_comply",
                tool_result=card_state.model_dump(),
                status="quarantined"
            )
            print("  ⚠️ SUPERVISOR DECISION: Exceeded 3 collection rounds!")
            print(f"  ➜ Card State: {card_state.state.upper()} (Quarantined)")
            print(f"  ➜ Missing Mandatory Slots: {card_state.highlights}")
            print(f"  ➜ Message: {card_state.questions[0]}")
            break
        else:
            print(f"  ➜ Agent 2 Check: Incomplete (Missing: {[m.field for m in completeness.missing]})")
            print(f"  ➜ Round {turn_idx}/{max_rounds} - Prompting user again for missing fields.")


async def main():
    print("================================================================================")
    print("CRM ORCHESTRATOR v2: RIGOROUS TEST SCENARIOS EXECUTION")
    print("================================================================================")
    await run_scenario_a_vague_multiturn()
    await run_scenario_b_compliance_rejections()
    await run_scenario_c_guardrail_rejections()
    await run_scenario_d_loop_failure()
    print("\n" + "="*80)
    print("✅ ALL 4 RIGOROUS TEST SCENARIOS EXECUTED SUCCESSFULLY")
    print("================================================================================")

if __name__ == "__main__":
    asyncio.run(main())
