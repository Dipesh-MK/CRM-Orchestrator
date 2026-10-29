import pytest
import json
from pathlib import Path
from typing import Optional

from app.core.compliance_evaluator_models import TemplateSchema, EvaluationRecord
from app.services.external_compliance_evaluator import (
    CascadeComplianceEvaluator,
    Stage1RegexEvaluator,
    Stage2SelfHostedClassifier,
    Stage3LLMJudge,
    get_compliance_evaluator,
)


@pytest.fixture
def default_template() -> TemplateSchema:
    return TemplateSchema(
        template_id="tmpl_standard_001",
        moment="regular_send",
        requires_rates_disclosure=False,
        registered_domains=["grocerymart.com", "streamplus.com"],
        brand_name="GroceryMart",
        customer_care_contact="1-800-555-0199",
        fallback_template_id="tmpl_safe_fallback_001",
    )


@pytest.fixture
def evaluator() -> CascadeComplianceEvaluator:
    return CascadeComplianceEvaluator()


# ==============================================================================
# SPECIFICATION TABLE REQUIRED TEST CASES (1 to 9)
# ==============================================================================

@pytest.mark.asyncio
async def test_case_1_missing_rates_disclosure_and_opt_out(evaluator):
    """
    Test Case 1:
    Missing rates disclosure (when requires_rates_disclosure = True) and missing STOP.
    Expected: Stage 1 (Regex) fails under 'format' category.
    Terminal outcome after redraft exhaustion: fallback_used.
    """
    tmpl = TemplateSchema(
        template_id="tmpl_cta_001",
        moment="call_to_action",
        requires_rates_disclosure=True,
        registered_domains=["grocerymart.com"],
        brand_name="GroceryMart",
        customer_care_contact="1-800-555-0199",
        fallback_template_id="tmpl_fallback_001",
    )
    # Message lacks rates disclosure and lacks opt-out
    msg = "Welcome to GroceryMart! Check out our fresh discounts at grocerymart.com."

    # Single pass evaluation
    rec = await evaluator.evaluate_single_message(msg, tmpl, "camp_tc1")
    assert rec.outcome == "failed"
    assert rec.failure_category == "format"
    assert rec.stage2_invoked is False
    assert rec.stage3_invoked is False

    failed_ids = {c["check_id"] for c in rec.stage1_checks_failed}
    assert "fmt_rates_disclosure" in failed_ids
    assert "fmt_opt_out" in failed_ids

    # Terminal redraft governance (exhaustion -> fallback_used)
    final_rec, final_text = await evaluator.evaluate_message(
        msg, tmpl, "camp_tc1", simulate_redraft_exhaustion=True
    )
    assert final_rec.outcome == "fallback_used"
    assert "Notice: Please visit our portal" in final_text
    assert final_rec.retry_attempt_number == 3


@pytest.mark.asyncio
async def test_case_2_unresolved_placeholders(evaluator, default_template):
    """
    Test Case 2:
    Unresolved placeholders [FirstName], [Date], [Time].
    Expected: Stage 1 (Regex) fails under 'format' category.
    Terminal outcome: fallback_used.
    """
    msg = "Hello [FirstName], your appointment is on [Date] at [Time] with GroceryMart. Reply STOP to cancel."

    rec = await evaluator.evaluate_single_message(msg, default_template, "camp_tc2")
    assert rec.outcome == "failed"
    assert rec.failure_category == "format"
    failed_ids = {c["check_id"] for c in rec.stage1_checks_failed}
    assert "fmt_unresolved_placeholders" in failed_ids

    final_rec, final_text = await evaluator.evaluate_message(
        msg, default_template, "camp_tc2", simulate_redraft_exhaustion=True
    )
    assert final_rec.outcome == "fallback_used"
    assert final_rec.retry_attempt_number == 3


@pytest.mark.asyncio
async def test_case_3_public_url_shortener(evaluator, default_template):
    """
    Test Case 3:
    Public URL shortener detected (bit.ly/xyz123).
    Expected: Stage 1 (Regex) fails under 'format' category.
    Terminal outcome: fallback_used.
    """
    msg = "Exclusive GroceryMart specials: visit bit.ly/xyz123 now! Reply STOP to cancel."

    rec = await evaluator.evaluate_single_message(msg, default_template, "camp_tc3")
    assert rec.outcome == "failed"
    assert rec.failure_category == "format"
    failed_ids = {c["check_id"] for c in rec.stage1_checks_failed}
    assert "fmt_url_domains" in failed_ids

    final_rec, _ = await evaluator.evaluate_message(
        msg, default_template, "camp_tc3", simulate_redraft_exhaustion=True
    )
    assert final_rec.outcome == "fallback_used"


@pytest.mark.asyncio
async def test_case_4_urgency_threat_and_password_request(evaluator, default_template):
    """
    Test Case 4:
    Urgency + account threat combo co-occurring with sensitive password request.
    Expected: Stage 1 (Regex) fails under 'harmful_intent' category.
    Terminal outcome: quarantined (NEVER fallback).
    """
    msg = "Urgent: Your account will be suspended! Verify now and confirm your password immediately. GroceryMart."

    rec = await evaluator.evaluate_single_message(msg, default_template, "camp_tc4")
    assert rec.outcome == "failed"
    assert rec.failure_category == "harmful_intent"
    failed_ids = {c["check_id"] for c in rec.stage1_checks_failed}
    assert "harm_sensitive_data" in failed_ids
    assert "harm_urgency_threat" in failed_ids
    # Skips Stage 2 & 3 completely
    assert rec.stage2_invoked is False
    assert rec.stage3_invoked is False

    final_rec, sent_text = await evaluator.evaluate_message(
        msg, default_template, "camp_tc4", simulate_redraft_exhaustion=True
    )
    assert final_rec.outcome == "quarantined"
    assert sent_text == ""  # Zero fallback sent
    assert final_rec.retry_attempt_number == 2


@pytest.mark.asyncio
async def test_case_5_scam_phrase_detection(evaluator, default_template):
    """
    Test Case 5:
    Known scam phrase "You've won a $500 gift card" from externalized scam library.
    Expected: Stage 1 (Regex) fails under 'harmful_intent'.
    Terminal outcome: quarantined.
    """
    msg = "Congratulations! You've won a $500 gift card from GroceryMart. Claim your prize here: grocerymart.com. Reply STOP to cancel."

    rec = await evaluator.evaluate_single_message(msg, default_template, "camp_tc5")
    assert rec.outcome == "failed"
    assert rec.failure_category == "harmful_intent"
    failed_ids = {c["check_id"] for c in rec.stage1_checks_failed}
    assert "harm_scam_phrase" in failed_ids

    final_rec, sent_text = await evaluator.evaluate_message(
        msg, default_template, "camp_tc5", simulate_redraft_exhaustion=True
    )
    assert final_rec.outcome == "quarantined"
    assert sent_text == ""


@pytest.mark.asyncio
async def test_case_6_paraphrased_social_engineering_stage2(evaluator, default_template):
    """
    Test Case 6:
    Paraphrased social engineering: "that special code we texted you — can you send it back".
    Passes Stage 1 regex (no literal scam or password keywords).
    Caught by Stage 2 (SCAMBERT / DistilBERT self-hosted classifier) with confidence >= 0.90.
    Terminal outcome: quarantined.
    """
    msg = "GroceryMart security: Hey, that special code we texted you — can you send it back so we can finish setting up your account? Reply STOP to cancel."

    rec = await evaluator.evaluate_single_message(msg, default_template, "camp_tc6")
    assert rec.outcome == "failed"
    assert rec.failure_category == "harmful_intent"
    # Stage 1 cleared
    assert len(rec.stage1_checks_failed) == 0
    # Stage 2 caught it
    assert rec.stage2_invoked is True
    assert rec.stage2_result["label"] == "harmful"
    assert rec.stage2_result["confidence"] >= 0.90
    # Stage 3 skipped because Stage 2 was decisive
    assert rec.stage3_invoked is False

    final_rec, sent_text = await evaluator.evaluate_message(
        msg, default_template, "camp_tc6", simulate_redraft_exhaustion=True
    )
    assert final_rec.outcome == "quarantined"
    assert sent_text == ""


@pytest.mark.asyncio
async def test_case_7a_subscription_expiration_unregistered_context(evaluator):
    """
    Test Case 7A (Uncertain band -> LLM Judge flags harmful):
    "Final notice: your subscription access expires in 1 hour unless you confirm here: https://phish-renew.com/verify."
    Unregistered context: brand unverified, unregistered domain.
    Stage 2 returns uncertain confidence -> Stage 3 LLM Judge evaluates context and detects phishing.
    Terminal outcome: quarantined.
    """
    unverified_tmpl = TemplateSchema(
        template_id="tmpl_unverified_001",
        moment="regular_send",
        requires_rates_disclosure=False,
        registered_domains=[],
        brand_name="QuickAlert",
        customer_care_contact="",
        fallback_template_id="tmpl_fallback",
    )
    msg = "QuickAlert: Final notice: your subscription access expires in 1 hour unless you confirm here. Reply STOP to cancel."

    rec = await evaluator.evaluate_single_message(msg, unverified_tmpl, "camp_tc7a")
    assert rec.stage2_invoked is True
    assert rec.stage2_result["label"] == "uncertain"
    # Stage 3 was invoked
    assert rec.stage3_invoked is True
    assert rec.stage3_result["harmful_intent_detected"] is True
    assert rec.stage3_result["intent_category"] == "phishing"
    assert rec.outcome == "failed"
    assert rec.failure_category == "harmful_intent"

    final_rec, sent_text = await evaluator.evaluate_message(
        msg, unverified_tmpl, "camp_tc7a", simulate_redraft_exhaustion=True
    )
    assert final_rec.outcome == "quarantined"
    assert sent_text == ""


@pytest.mark.asyncio
async def test_case_7b_subscription_expiration_registered_context(evaluator):
    """
    Test Case 7B (Uncertain band -> LLM Judge clears safe):
    "StreamPlus. Final notice: your subscription access expires in 1 hour unless you confirm here: https://streamplus.com/renew. Reply STOP to cancel."
    Verified context: registered brand StreamPlus, registered domain streamplus.com, regular_send moment.
    Stage 2 returns uncertain confidence -> Stage 3 LLM Judge evaluates context and approves legitimate transactional renewal.
    Terminal outcome: approved.
    """
    verified_tmpl = TemplateSchema(
        template_id="tmpl_verified_streamplus",
        moment="regular_send",
        requires_rates_disclosure=False,
        registered_domains=["streamplus.com"],
        brand_name="StreamPlus",
        customer_care_contact="1-800-555-0199",
        fallback_template_id="tmpl_fallback_streamplus",
    )
    msg = "StreamPlus. Final notice: your subscription access expires in 1 hour unless you confirm here: https://streamplus.com/renew. Reply STOP to cancel."

    rec = await evaluator.evaluate_single_message(msg, verified_tmpl, "camp_tc7b")
    assert rec.stage2_invoked is True
    assert rec.stage2_result["label"] == "uncertain"
    # Stage 3 was invoked
    assert rec.stage3_invoked is True
    assert rec.stage3_result["harmful_intent_detected"] is False
    assert rec.stage3_result["intent_category"] == "none"
    assert rec.outcome == "approved"
    assert rec.failure_category is None

    final_rec, sent_text = await evaluator.evaluate_message(
        msg, verified_tmpl, "camp_tc7b", simulate_redraft_exhaustion=True
    )
    assert final_rec.outcome == "approved"
    assert sent_text == msg


@pytest.mark.asyncio
async def test_case_8_three_consecutive_format_failures_stops_at_three(evaluator, default_template):
    """
    Test Case 8:
    Generator returns format errors on all attempts.
    Shared retry counter increments to exactly 3, terminal outcome: fallback_used.
    """
    attempts_recorded = []

    async def format_failing_generator(attempt_num: int, last_rec: Optional[EvaluationRecord]) -> str:
        attempts_recorded.append(attempt_num)
        # Missing STOP instruction on all attempts
        return f"Hello from GroceryMart! Attempt {attempt_num} check out our new seasonal goods."

    final_rec, final_text = await evaluator.run_evaluation_cascade_with_redraft(
        generator_func=format_failing_generator,
        template=default_template,
        campaign_id="camp_tc8",
    )

    assert attempts_recorded == [1, 2, 3]
    assert final_rec.retry_attempt_number == 3
    assert final_rec.outcome == "fallback_used"
    assert final_rec.failure_category == "format"
    assert "Notice: Please visit our portal" in final_text


@pytest.mark.asyncio
async def test_case_9_two_consecutive_harmful_failures_stops_at_two(evaluator, default_template):
    """
    Test Case 9:
    Generator returns harmful intent errors on all attempts.
    Shared retry counter increments to exactly 2, terminal outcome: quarantined (zero fallback).
    """
    attempts_recorded = []

    async def harmful_failing_generator(attempt_num: int, last_rec: Optional[EvaluationRecord]) -> str:
        attempts_recorded.append(attempt_num)
        return f"GroceryMart: You've won a $500 gift card! Reply STOP to cancel."

    final_rec, final_text = await evaluator.run_evaluation_cascade_with_redraft(
        generator_func=harmful_failing_generator,
        template=default_template,
        campaign_id="camp_tc9",
    )

    assert attempts_recorded == [1, 2]
    assert final_rec.retry_attempt_number == 2
    assert final_rec.outcome == "quarantined"
    assert final_rec.failure_category == "harmful_intent"
    assert final_text == ""


# ==============================================================================
# ADDITIONAL RIGOROUS UNIT TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_governing_category_rule_mixed_failures(evaluator, default_template):
    """
    Governing category rule:
    If a message has BOTH format failure (missing STOP) AND harmful_intent failure (scam phrase),
    the governing failure category is strictly harmful_intent, and quarantine occurs after 2 retries.
    """
    mixed_msg = "You've won a $500 gift card from GroceryMart! Click to claim."
    rec = await evaluator.evaluate_single_message(mixed_msg, default_template, "camp_mixed")

    assert rec.outcome == "failed"
    assert rec.failure_category == "harmful_intent"
    failed_categories = {c["category"] for c in rec.stage1_checks_failed}
    assert "format" in failed_categories
    assert "harmful_intent" in failed_categories

    # Redraft governance caps at 2 attempts for harmful_intent, not 3
    final_rec, sent_text = await evaluator.evaluate_message(
        mixed_msg, default_template, "camp_mixed", simulate_redraft_exhaustion=True
    )
    assert final_rec.outcome == "quarantined"
    assert final_rec.retry_attempt_number == 2
    assert sent_text == ""


def test_stage1_opt_out_moment_skip():
    """Verify that opt_out_confirmation moment skips mandatory opt-out check."""
    s1 = Stage1RegexEvaluator()
    tmpl = TemplateSchema(
        template_id="tmpl_optout",
        moment="opt_out_confirmation",
        requires_rates_disclosure=False,
        registered_domains=[],
        brand_name="GroceryMart",
        customer_care_contact="",
        fallback_template_id="safe",
    )
    text = "You have been unsubscribed from GroceryMart. No more messages will be sent."
    run_checks, failed_checks = s1.evaluate(text, tmpl)
    opt_out_chk = next(c for c in run_checks if c.check_id == "fmt_opt_out")
    assert opt_out_chk.passed is True
    assert "skipped" in opt_out_chk.reason.lower()


def test_stage1_phone_number_attribution():
    """Verify customer care contact mismatch is rejected."""
    s1 = Stage1RegexEvaluator()
    tmpl = TemplateSchema(
        template_id="tmpl_phone",
        moment="regular_send",
        requires_rates_disclosure=False,
        registered_domains=[],
        brand_name="GroceryMart",
        customer_care_contact="1-800-555-0199",
        fallback_template_id="safe",
    )
    # Different phone number embedded
    text = "GroceryMart: Call our support at 555-999-0000 for help. Reply STOP to cancel."
    _, failed = s1.evaluate(text, tmpl)
    failed_ids = {c.check_id for c in failed}
    assert "fmt_phone_attribution" in failed_ids

    # Correct phone number embedded
    text_ok = "GroceryMart: Call our support at 1-800-555-0199 for help. Reply STOP to cancel."
    _, failed_ok = s1.evaluate(text_ok, tmpl)
    failed_ok_ids = {c.check_id for c in failed_ok}
    assert "fmt_phone_attribution" not in failed_ok_ids


def test_stage2_classifier_thresholds():
    """Verify classifier boundaries for safe, harmful, and uncertain."""
    s2 = Stage2SelfHostedClassifier()
    # Benign commercial message
    benign = s2.classify("Fresh organic apples on sale at GroceryMart! Reply STOP to cancel.")
    assert benign.label == "safe"
    assert benign.confidence >= 0.95

    # Phishing code harvesting
    phish = s2.classify("special code we texted you — send it back")
    assert phish.label == "harmful"
    assert phish.confidence >= 0.90

    # Ambiguous subscription renewal
    ambiguous = s2.classify("Final notice: your membership expires in 1 hour unless you confirm here")
    assert ambiguous.label == "uncertain"
    assert 0.70 <= ambiguous.confidence <= 0.85


@pytest.mark.asyncio
async def test_stage3_llm_judge_fail_closed():
    """Verify Stage 3 LLM Judge fails closed as harmful_intent on unparseable/error responses."""
    judge = Stage3LLMJudge()
    tmpl = TemplateSchema(
        template_id="tmpl_err",
        moment="regular_send",
        requires_rates_disclosure=False,
        brand_name="Test",
        customer_care_contact="",
        fallback_template_id="safe",
    )
    from app.core.compliance_evaluator_models import Stage2Result
    s2_res = Stage2Result(label="uncertain", confidence=0.75)

    # When evaluate_intent encounters any unexpected failure, it must fail closed as harmful_intent
    res = await judge.evaluate_intent(
        text="Irrelevant text causing fail-closed",
        template=tmpl,
        stage2_result=s2_res
    )
    assert res.harmful_intent_detected is True
    assert res.intent_category in ("phishing", "social_engineering")


@pytest.mark.asyncio
async def test_jsonl_logging_schema(evaluator, default_template, tmp_path):
    """Verify evaluation record is appended to data/compliance_evaluations.jsonl matching Section 6 schema."""
    msg = "Welcome to GroceryMart! Great deals inside. Reply STOP to cancel."
    rec = await evaluator.evaluate_single_message(msg, default_template, "camp_log_test")
    assert rec.outcome == "approved"

    jsonl_path = Path("data/compliance_evaluations.jsonl")
    assert jsonl_path.exists()

    with open(jsonl_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
        assert len(lines) > 0
        last_entry = json.loads(lines[-1])

    # Validate against Section 6 spec fields
    assert "timestamp" in last_entry
    assert last_entry["template_id"] == default_template.template_id
    assert last_entry["campaign_id"] == "camp_log_test"
    assert "message_hash_or_id" in last_entry
    assert last_entry["outcome"] == "approved"
    assert "stage1_checks_run" in last_entry
    assert "stage2_invoked" in last_entry
    assert "stage3_invoked" in last_entry
    assert last_entry["evaluator_version"] == "1.0.0"


@pytest.mark.asyncio
async def test_pipeline_event_visibility(evaluator, default_template):
    """Verify pipeline event 'Compliance check from external evaluator' is recorded in storage."""
    session_id = "sess_pipeline_test"
    msg = "GroceryMart: Fresh apples in stock now! Reply STOP to cancel."
    await evaluator.evaluate_single_message(msg, default_template, session_id, session_id=session_id)

    from app.core.storage import get_storage
    storage = get_storage()
    events = storage.get_pipeline_events(session_id)

    comp_events = [e for e in events if e.get("step_title") == "Compliance check from external evaluator"]
    assert len(comp_events) >= 1
    ev = comp_events[0]
    assert ev["step_id"] == "compliance_evaluator"
    assert ev["status"] == "done"
    assert ev["sub_step"] in ("stage1_regex", "stage2_classifier", "stage3_judge")
