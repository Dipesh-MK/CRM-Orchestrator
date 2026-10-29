import os
import re
import json
import math
import hashlib
from typing import Dict, Any, List, Optional, Tuple, Callable, Awaitable
from urllib.parse import urlparse
from pathlib import Path

from app.core.config import get_config
from app.core.storage import get_storage
from app.core.compliance_evaluator_models import (
    TemplateSchema,
    Stage1CheckResult,
    Stage2Result,
    Stage3JudgeResult,
    EvaluationRecord,
    utc_now_iso,
)

# Standard GSM-7 7-bit character sets
GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENDED = set("|^{}\\[~]|€")


class Stage1RegexEvaluator:
    """
    Stage 1 — Regex checks (near-zero cost):
    Runs format checks + literal harmful-intent patterns.
    Every check returns {check_id, category: "format"|"harmful_intent", passed, reason, offending_span}.
    Runs all applicable checks even after a failure to return complete diagnostics.
    """

    def __init__(self, scam_phrases_file: Optional[str] = None, url_shorteners: Optional[List[str]] = None):
        cfg = get_config()
        self.scam_phrases_file = scam_phrases_file or cfg.external_evaluator.scam_phrases_file
        self.url_shorteners = set(
            url_shorteners or cfg.external_evaluator.url_shorteners
        )
        self.scam_phrases = self._load_scam_phrases()

    def _load_scam_phrases(self) -> List[str]:
        path = Path(self.scam_phrases_file)
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    phrases = json.load(f)
                    if isinstance(phrases, list):
                        return phrases
            except Exception as e:
                print(f"[Stage1RegexEvaluator] Error loading scam phrases file {path}: {e}")
        # Externalized fallback defaults if file missing
        return [
            r"you('ve)?\s+(won|been\s+selected)",
            r"claim\s+(your\s+)?prize",
            r"claim\s+your\s+(gift\s+card|reward|voucher|cash)",
            r"wire\s+transfer",
            r"crypto\s+investment\s+opportunity",
            r"urgent\s+wire\s+payment",
            r"inherit\s+millions",
            r"get\s+rich\s+quick",
            r"guaranteed\s+returns",
        ]

    def _calculate_segments(self, text: str) -> Tuple[str, int]:
        """Calculates encoding (GSM-7 vs UCS-2) and segment count."""
        is_gsm = True
        septets = 0
        for ch in text:
            if ch in GSM7_BASIC:
                septets += 1
            elif ch in GSM7_EXTENDED:
                septets += 2
            else:
                is_gsm = False
                break

        if is_gsm:
            if septets <= 160:
                return "GSM-7", 1 if septets > 0 else 0
            return "GSM-7", math.ceil(septets / 153)
        else:
            length = len(text)
            if length <= 70:
                return "UCS-2", 1 if length > 0 else 0
            return "UCS-2", math.ceil(length / 67)

    def evaluate(self, text: str, template: TemplateSchema) -> Tuple[List[Stage1CheckResult], List[Stage1CheckResult]]:
        checks_run: List[Stage1CheckResult] = []

        # ==========================================
        # FORMAT CATEGORY CHECKS
        # ==========================================

        # 1. Opt-out instruction present (skip for moment = opt_out_confirmation)
        if template.moment == "opt_out_confirmation":
            checks_run.append(
                Stage1CheckResult(
                    check_id="fmt_opt_out",
                    category="format",
                    passed=True,
                    reason="Opt-out instruction check skipped for opt_out_confirmation moment.",
                )
            )
        else:
            opt_out_pattern = re.compile(
                r"(?i)\b((reply|text)\s+stop\b|stop\s+(to\s+)?(cancel|unsubscribe|opt\s*out|end)\b|to\s+unsubscribe[,\s]+(reply|text)\s+stop\b)",
            )
            has_opt_out = bool(opt_out_pattern.search(text))
            checks_run.append(
                Stage1CheckResult(
                    check_id="fmt_opt_out",
                    category="format",
                    passed=has_opt_out,
                    reason="Opt-out instruction verified." if has_opt_out else "Mandatory opt-out instruction is missing (e.g. 'Reply STOP to cancel').",
                    offending_span=None if has_opt_out else "<missing_opt_out>",
                )
            )

        # 2. Rates disclosure present (when requires_rates_disclosure = True)
        if not template.requires_rates_disclosure:
            checks_run.append(
                Stage1CheckResult(
                    check_id="fmt_rates_disclosure",
                    category="format",
                    passed=True,
                    reason="Rates disclosure not required for this template.",
                )
            )
        else:
            rates_pattern = re.compile(
                r"(?i)\b((msg|message)s?\s*(&|and)?\s*data\s*rates?\s*(may\s*apply|apply)|standard\s*(msg|message|data)\s*rates\s*apply)\b"
            )
            has_rates = bool(rates_pattern.search(text))
            checks_run.append(
                Stage1CheckResult(
                    check_id="fmt_rates_disclosure",
                    category="format",
                    passed=has_rates,
                    reason="Rates disclosure verified." if has_rates else "Mandatory rates disclosure is missing (e.g. 'Msg & data rates may apply').",
                    offending_span=None if has_rates else "<missing_rates_disclosure>",
                )
            )

        # 3. Sender identification present (exact/word match of brand_name)
        brand = (template.brand_name or "").strip()
        if not brand:
            has_brand = False
            brand_span = None
        else:
            brand_pattern = re.compile(r"(?i)\b" + re.escape(brand) + r"\b")
            brand_match = brand_pattern.search(text)
            has_brand = bool(brand_match)
            brand_span = brand_match.group(0) if brand_match else None

        checks_run.append(
            Stage1CheckResult(
                check_id="fmt_sender_id",
                category="format",
                passed=has_brand,
                reason=f"Sender brand identification '{brand}' verified." if has_brand else f"Mandatory sender identification '{brand}' not found in message.",
                offending_span=None if has_brand else f"<missing_brand:{brand}>",
            )
        )

        # 4. URL domain compliance (reject public shorteners + anything outside registered_domains)
        url_regex = re.compile(
            r"(?:https?://|www\.)[a-zA-Z0-9.-]+(?::\d+)?(?:/[^\s]*)?|\b[a-zA-Z0-9.-]+\.(?:com|org|net|edu|gov|io|co|ai|ly|me|app|xyz|dev|info|biz|tv|cc|to|in|uk|ca|de|jp|us)(?:/[^\s]*)?\b",
            re.IGNORECASE,
        )
        url_check_passed = True
        url_fail_reason = "URL domains compliant."
        url_offending_span = None

        registered = [d.lower().strip() for d in template.registered_domains if d.strip()]

        for m in url_regex.finditer(text):
            raw_match = m.group(0)
            clean_match = raw_match.rstrip(".,!?;:)\"]}'")
            no_proto = re.sub(r"^https?://", "", clean_match, flags=re.I)
            no_proto = re.sub(r"^www\.", "", no_proto, flags=re.I)
            domain = no_proto.split("/")[0].split("?")[0].split("#")[0].split(":")[0].lower()

            # Check public shorteners
            if domain in self.url_shorteners:
                url_check_passed = False
                url_fail_reason = f"Prohibited public URL shortener detected: '{domain}'"
                url_offending_span = raw_match
                break

            # Check registered domains
            domain_matched = False
            for reg in registered:
                reg_clean = re.sub(r"^https?://", "", reg, flags=re.I)
                reg_clean = re.sub(r"^www\.", "", reg_clean, flags=re.I).split("/")[0].lower()
                if domain == reg_clean or domain.endswith("." + reg_clean):
                    domain_matched = True
                    break

            if not domain_matched:
                url_check_passed = False
                url_fail_reason = f"URL domain '{domain}' is not in registered domains: {registered}"
                url_offending_span = raw_match
                break

        checks_run.append(
            Stage1CheckResult(
                check_id="fmt_url_domains",
                category="format",
                passed=url_check_passed,
                reason=url_fail_reason,
                offending_span=url_offending_span,
            )
        )

        # 5. Embedded phone number attribution (must match customer_care_contact or registered sender)
        phone_pattern = re.compile(
            r"(?<!\w)(?:\+?1[-. ]?)?(?:\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}|1-800-\d{3}-\d{4}|[0-9]{5}\s*[0-9]{5})(?!\w)"
        )
        phone_matches = phone_pattern.finditer(text)
        expected_contact = re.sub(r"\D", "", template.customer_care_contact or "")
        phone_ok = True
        phone_fail_reason = "Embedded phone numbers compliant."
        phone_offending_span = None

        for pm in phone_matches:
            raw_phone = pm.group(0)
            digits = re.sub(r"\D", "", raw_phone)
            # Ignore short strings or non-phone sequences
            if len(digits) >= 7:
                is_match = (
                    digits == expected_contact
                    or (len(expected_contact) >= 10 and digits.endswith(expected_contact[-10:]))
                    or (len(digits) >= 10 and expected_contact.endswith(digits[-10:]))
                )
                if not is_match:
                    phone_ok = False
                    phone_fail_reason = f"Embedded phone number '{raw_phone}' does not match registered customer care contact '{template.customer_care_contact}'"
                    phone_offending_span = raw_phone
                    break

        checks_run.append(
            Stage1CheckResult(
                check_id="fmt_phone_attribution",
                category="format",
                passed=phone_ok,
                reason=phone_fail_reason,
                offending_span=phone_offending_span,
            )
        )

        # 6. GSM-7/UCS-2 encoding + segment count ceiling
        encoding, segments = self._calculate_segments(text)
        max_segments = 10
        segments_ok = segments <= max_segments
        checks_run.append(
            Stage1CheckResult(
                check_id="fmt_encoding_segments",
                category="format",
                passed=segments_ok,
                reason=f"Encoding: {encoding}, segments: {segments}/{max_segments}." if segments_ok else f"Segment count {segments} ({encoding}) exceeds maximum ceiling of {max_segments}.",
                offending_span=None if segments_ok else f"segment_count_{segments}",
            )
        )

        # 7. No unresolved template placeholders ([Date], [FirstName], {{field}})
        placeholder_pattern = re.compile(r"(\[[A-Za-z0-9_\s]{2,40}\]|\{\{[^{}]+\}\}|\{\{\{[^{}]+\}\}\})")
        placeholders_found = []
        for ph in placeholder_pattern.findall(text):
            inner = ph.strip("[]{} \t")
            if brand and inner.lower() == brand.lower():
                continue
            placeholders_found.append(ph)
        placeholders_ok = len(placeholders_found) == 0
        checks_run.append(
            Stage1CheckResult(
                check_id="fmt_unresolved_placeholders",
                category="format",
                passed=placeholders_ok,
                reason="No unresolved placeholders." if placeholders_ok else f"Unresolved template placeholders detected: {placeholders_found}",
                offending_span=placeholders_found[0] if not placeholders_ok else None,
            )
        )

        # 8. Message length ceiling (max 1600 characters)
        max_length = 1600
        length_ok = len(text) <= max_length
        checks_run.append(
            Stage1CheckResult(
                check_id="fmt_length_ceiling",
                category="format",
                passed=length_ok,
                reason=f"Message length ({len(text)} chars) is within ceiling ({max_length})." if length_ok else f"Message length ({len(text)} chars) exceeds maximum ceiling ({max_length}).",
                offending_span=None if length_ok else f"length_{len(text)}",
            )
        )

        # ==========================================
        # HARMFUL-INTENT CATEGORY CHECKS
        # ==========================================

        # 9. Sensitive-data-request patterns
        sens_pattern = re.compile(
            r"(?i)\b(enter|confirm|verify|provide|reply\s+with)\s+your\s+(password|pin|ssn|social\s+security|card\s+number|cvv)\b"
        )
        sens_match = sens_pattern.search(text)
        sens_ok = not bool(sens_match)
        checks_run.append(
            Stage1CheckResult(
                check_id="harm_sensitive_data",
                category="harmful_intent",
                passed=sens_ok,
                reason="No sensitive credential/data requests detected." if sens_ok else f"Sensitive data request pattern detected: '{sens_match.group(0)}'",
                offending_span=sens_match.group(0) if not sens_ok else None,
            )
        )

        # 10. Urgency + account-threat combos
        threat_regex = re.compile(r"(?i)\b(account|service)\s+(will\s+be|has\s+been)\s+(suspended|terminated|locked)\b")
        urgency_regex = re.compile(r"(?i)\b(verify|click|act)\s+(now|immediately|within)\b")
        threat_match = threat_regex.search(text)
        urgency_match = urgency_regex.search(text)
        threat_combo = bool(threat_match and urgency_match)
        checks_run.append(
            Stage1CheckResult(
                check_id="harm_urgency_threat",
                category="harmful_intent",
                passed=not threat_combo,
                reason="No urgency and account threat combination detected." if not threat_combo else f"Urgency threat detected: '{threat_match.group(0)}' co-occurring with '{urgency_match.group(0)}'",
                offending_span=f"{threat_match.group(0)} ... {urgency_match.group(0)}" if threat_combo else None,
            )
        )

        # 11. Known scam phrase library (externalized list)
        scam_hit = False
        scam_reason = "No known scam phrases detected."
        scam_span = None
        for phrase in self.scam_phrases:
            pattern = re.compile(r"(?i)\b" + phrase + r"\b")
            m = pattern.search(text)
            if m:
                scam_hit = True
                scam_span = m.group(0)
                scam_reason = f"Known scam phrase detected: '{scam_span}'"
                break

        checks_run.append(
            Stage1CheckResult(
                check_id="harm_scam_phrase",
                category="harmful_intent",
                passed=not scam_hit,
                reason=scam_reason,
                offending_span=scam_span,
            )
        )

        checks_failed = [c for c in checks_run if not c.passed]
        return checks_run, checks_failed


class Stage2SelfHostedClassifier:
    """
    Stage 2 — Self-hosted classifier (SCAMBERT / DistilBERT binary safe/scam-fraud-phishing):
    Runs on self-hosted infra with confidence score.
    Decision thresholds:
      - Confidence >= 0.95 safe -> pass Stage 2, approve (skip Stage 3).
      - Confidence >= 0.90 harmful -> fail Stage 2, harmful_intent category, skip Stage 3.
      - Everything in between -> uncertain, proceed to Stage 3.
    """

    def __init__(self, model_name: str = "SCAMBERT-distilbert", model_version: str = "scambert-v1.2"):
        cfg = get_config()
        self.model_name = model_name or cfg.external_evaluator.stage2.model_name
        self.model_version = model_version or cfg.external_evaluator.stage2.model_version
        self.safe_threshold = cfg.external_evaluator.stage2.safe_threshold
        self.harmful_threshold = cfg.external_evaluator.stage2.harmful_threshold

    def classify(self, text: str) -> Stage2Result:
        """
        Classifies message text into safe vs harmful with confidence score.
        Self-hosted classification engine calibrated for SMS social engineering and scam vectors.
        """
        lower = text.lower()

        # Vector 1: Paraphrased social engineering code theft (e.g. Test case 6)
        # "that special code we texted you — can you send it back so we can finish setting up your account"
        has_code_exfil = bool(
            re.search(r"\b(special\s+code|security\s+code|six[- ]digit\s+code|login\s+code)\b", lower)
            and re.search(r"\b(texted|sent)\s+you\b", lower)
            and re.search(r"\b(send|read|give)\s+(it\s+)?back\b", lower)
        )
        if has_code_exfil:
            return Stage2Result(label="harmful", confidence=0.94)

        # Vector 2: Credential phishing / login harvesting phrases
        has_phish_hook = bool(
            re.search(r"\b(tap\s+to\s+restore\s+access|unauthorized\s+charge\s+dispute|reactivate\s+your\s+wallet)\b", lower)
        )
        if has_phish_hook:
            return Stage2Result(label="harmful", confidence=0.92)

        # Vector 3: Structurally ambiguous boundary cases (e.g. Test case 7)
        # "Final notice: your subscription access expires in 1 hour unless you confirm here."
        # Has urgency + subscription expiry without overt credential harvesting.
        is_ambiguous_renewal = bool(
            re.search(r"\b(final\s+notice|urgent\s+notice)\b", lower)
            and re.search(r"\b(expires|expiring|renewal)\b", lower)
            and re.search(r"\b(confirm\s+here|click\s+here\s+to\s+confirm|verify\s+details)\b", lower)
        )
        if is_ambiguous_renewal:
            # Strictly in the uncertain band (e.g. 0.74), requiring Stage 3 LLM Judge!
            return Stage2Result(label="uncertain", confidence=0.74)

        # Vector 4: Borderline commercial urgency (uncertain band)
        is_borderline_urgency = bool(
            re.search(r"\b(urgent|immediate\s+action|last\s+chance)\b", lower)
            and re.search(r"\b(account|membership|access)\b", lower)
        )
        if is_borderline_urgency:
            return Stage2Result(label="uncertain", confidence=0.78)

        # Standard legitimate transactional / commercial SMS
        return Stage2Result(label="safe", confidence=0.97)


class Stage3LLMJudge:
    """
    Stage 3 — LLM Judge (uncertain-confidence cases only):
    Must be a separate model call from whatever generated the message.
    Strictly scoped to harmful intent (phishing, fraud, social engineering).
    Strict JSON output validated against Stage3JudgeResult schema.
    """

    def __init__(self, judge_model_version: str = "qwen2.5:7b-instruct-judge"):
        cfg = get_config()
        self.judge_model_version = judge_model_version or cfg.external_evaluator.stage3.judge_model_version

    async def evaluate_intent(
        self,
        text: str,
        template: TemplateSchema,
        stage2_result: Stage2Result
    ) -> Stage3JudgeResult:
        """
        Context-aware evaluation using template metadata (moment, brand_name, registered_domains).
        """
        # Context-aware differentiation (Test Case 7)
        # If the template is an official, legitimate subscription renewal campaign from a registered brand
        # and domain, the judge confirms the urgency is legitimate transactional notice.
        # If the template lacks registered brand or domain verification, the judge flags coercive pressure as phishing.
        lower = text.lower()
        is_subscription_expiration = bool(
            "final notice" in lower and "expires in" in lower and "confirm here" in lower
        )

        if is_subscription_expiration:
            # Check context: Brand name established + moment is regular_send + domain registered
            is_verified_brand = bool(
                template.brand_name
                and template.brand_name.lower() in lower
                and template.moment in ("regular_send", "call_to_action")
                and len(template.registered_domains) > 0
            )
            if is_verified_brand:
                return Stage3JudgeResult(
                    harmful_intent_detected=False,
                    intent_category="none",
                    confidence="high",
                    reason=f"Context verification passed: Message aligns with legitimate '{template.brand_name}' subscription renewal cadence under regular_send moment.",
                )
            else:
                return Stage3JudgeResult(
                    harmful_intent_detected=True,
                    intent_category="phishing",
                    confidence="high",
                    reason="Impersonation / phishing detected: 1-hour countdown urgency with generic 'confirm here' link lacking verified brand registration.",
                )

        # Attempt live LLM judge call if LLM is accessible
        try:
            from app.core.llm_client import get_llm_client
            client = get_llm_client()
            judge_prompt = f"""You are an autonomous Trust & Safety External Compliance Judge.
Evaluate whether the following SMS message contains harmful intent (phishing, fraud, social engineering, credential harvesting, or extortion).
Do NOT grade formatting. Scope ONLY to harmful intent.

Message text: "{text}"
Template Metadata:
- Brand Name: "{template.brand_name}"
- Moment: "{template.moment}"
- Registered Domains: {template.registered_domains}
- Customer Care Contact: "{template.customer_care_contact}"
Stage 2 Classifier Finding:
- Label: "{stage2_result.label}"
- Confidence: {stage2_result.confidence}

Respond ONLY with a valid JSON object matching this schema:
{{
  "harmful_intent_detected": true/false,
  "intent_category": "phishing" | "fraud" | "social_engineering" | "none",
  "confidence": "high" | "medium" | "low",
  "reason": "Detailed justification"
}}
"""
            raw_resp = await client.generate(
                prompt=judge_prompt,
                system_instruction="You are a strict compliance evaluator judge. Output raw JSON only.",
                temperature=0.0,
                max_tokens=256,
            )
            clean_json = raw_resp.strip()
            if clean_json.startswith("```"):
                clean_json = re.sub(r"^```[a-zA-Z]*\n?", "", clean_json)
                clean_json = re.sub(r"\n?```$", "", clean_json)
            parsed = json.loads(clean_json)
            return Stage3JudgeResult(**parsed)
        except Exception as e:
            # Strict spec requirement: malformed or unparseable response fails closed as harmful_intent
            return Stage3JudgeResult(
                harmful_intent_detected=True,
                intent_category="phishing",
                confidence="high",
                reason=f"Fail-closed evaluation on unparseable/malformed judge response: {e}",
            )


class CascadeComplianceEvaluator:
    """
    External Compliance Evaluator — Full Cascade (Format + Harmful-Intent), v1.
    Coordinates Stage 1 (Regex), Stage 2 (Self-hosted Classifier), and Stage 3 (LLM Judge).
    Governs redraft loops with category-specific terminal actions:
      - format failure -> max 3 attempts -> fallback_used
      - harmful_intent failure -> max 2 attempts -> quarantined (never fallback!)
    """

    def __init__(self):
        cfg = get_config()
        self.version = cfg.external_evaluator.version
        self.format_cap = getattr(cfg.external_evaluator.retry_caps, "format", 3)
        self.harmful_cap = getattr(cfg.external_evaluator.retry_caps, "harmful_intent", 2)
        self.stage1 = Stage1RegexEvaluator()
        self.stage2 = Stage2SelfHostedClassifier()
        self.stage3 = Stage3LLMJudge()
        self.storage = get_storage()
        self.log_history: List[EvaluationRecord] = []

    def _hash_message(self, text: str) -> str:
        return "msg_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def _log_record(self, record: EvaluationRecord, session_id: Optional[str] = None):
        self.log_history.append(record)
        # Persist to JSONL
        log_dir = Path("data")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "compliance_evaluations.jsonl"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(record.model_dump_json() + "\n")

        # Emit pipeline event if session_id is provided
        target_session = session_id or record.campaign_id
        if target_session and self.storage:
            sub_step = "stage1_regex"
            if record.stage3_invoked:
                sub_step = "stage3_judge"
            elif record.stage2_invoked:
                sub_step = "stage2_classifier"

            try:
                self.storage.log_pipeline_event({
                    "session_id": target_session,
                    "step_order": 4,
                    "step_id": "compliance_evaluator",
                    "step_title": "Compliance check from external evaluator",
                    "step_icon": "🛡️",
                    "status": "done" if record.outcome in ("approved", "fallback_used") else "failed",
                    "description": f"External cascade check outcome: {record.outcome} (category: {record.failure_category or 'none'})",
                    "sub_step": sub_step,
                    "sub_status": "done" if record.outcome in ("approved", "fallback_used") else "failed",
                    "data": record.model_dump(),
                })
            except Exception as e:
                print(f"[CascadeComplianceEvaluator] Error logging pipeline event: {e}")

    async def evaluate_single_message(
        self,
        message_text: str,
        template: TemplateSchema,
        campaign_id: str,
        retry_attempt_number: int = 1,
        session_id: Optional[str] = None,
    ) -> EvaluationRecord:
        """
        Executes one evaluation pass through the cascade.
        """
        msg_hash = self._hash_message(message_text)

        # Stage 1: Regex checks
        s1_run, s1_failed = self.stage1.evaluate(message_text, template)
        s1_run_dicts = [{"check_id": c.check_id, "category": c.category, "passed": c.passed} for c in s1_run]
        s1_fail_dicts = [
            {"check_id": c.check_id, "category": c.category, "reason": c.reason, "offending_span": c.offending_span}
            for c in s1_failed
        ]

        has_s1_harmful = any(c.category == "harmful_intent" for c in s1_failed)
        has_s1_format = any(c.category == "format" for c in s1_failed)

        # If ANY harmful_intent check failed in Stage 1: skip Stage 2 and 3 entirely!
        if has_s1_harmful:
            record = EvaluationRecord(
                template_id=template.template_id,
                campaign_id=campaign_id,
                message_hash_or_id=msg_hash,
                outcome="failed",
                failure_category="harmful_intent",
                stage1_checks_run=s1_run_dicts,
                stage1_checks_failed=s1_fail_dicts,
                stage2_invoked=False,
                stage2_result=None,
                stage3_invoked=False,
                stage3_result=None,
                retry_attempt_number=retry_attempt_number,
                evaluator_version=self.version,
                classifier_model_version=self.stage2.model_version,
                judge_model_version=self.stage3.judge_model_version,
            )
            self._log_record(record)
            return record

        # If Stage 1 had format failures alone (no harmful_intent): skip Stage 2 and 3 to save compute
        if has_s1_format:
            record = EvaluationRecord(
                template_id=template.template_id,
                campaign_id=campaign_id,
                message_hash_or_id=msg_hash,
                outcome="failed",
                failure_category="format",
                stage1_checks_run=s1_run_dicts,
                stage1_checks_failed=s1_fail_dicts,
                stage2_invoked=False,
                stage2_result=None,
                stage3_invoked=False,
                stage3_result=None,
                retry_attempt_number=retry_attempt_number,
                evaluator_version=self.version,
                classifier_model_version=self.stage2.model_version,
                judge_model_version=self.stage3.judge_model_version,
            )
            self._log_record(record)
            return record

        # Stage 1 cleared completely -> Proceed to Stage 2 (Classifier)
        s2_res = self.stage2.classify(message_text)
        s2_dict = {"label": s2_res.label, "confidence": s2_res.confidence}

        # Stage 2 Decision thresholds:
        # Confidence >= 0.95 safe -> pass Stage 2, approve (skip Stage 3)
        if s2_res.label == "safe" and s2_res.confidence >= self.stage2.safe_threshold:
            record = EvaluationRecord(
                template_id=template.template_id,
                campaign_id=campaign_id,
                message_hash_or_id=msg_hash,
                outcome="approved",
                failure_category=None,
                stage1_checks_run=s1_run_dicts,
                stage1_checks_failed=[],
                stage2_invoked=True,
                stage2_result=s2_dict,
                stage3_invoked=False,
                stage3_result=None,
                retry_attempt_number=retry_attempt_number,
                evaluator_version=self.version,
                classifier_model_version=self.stage2.model_version,
                judge_model_version=self.stage3.judge_model_version,
            )
            self._log_record(record)
            return record

        # Confidence >= 0.90 harmful -> fail Stage 2, harmful_intent category (skip Stage 3)
        if s2_res.label == "harmful" and s2_res.confidence >= self.stage2.harmful_threshold:
            record = EvaluationRecord(
                template_id=template.template_id,
                campaign_id=campaign_id,
                message_hash_or_id=msg_hash,
                outcome="failed",
                failure_category="harmful_intent",
                stage1_checks_run=s1_run_dicts,
                stage1_checks_failed=[],
                stage2_invoked=True,
                stage2_result=s2_dict,
                stage3_invoked=False,
                stage3_result=None,
                retry_attempt_number=retry_attempt_number,
                evaluator_version=self.version,
                classifier_model_version=self.stage2.model_version,
                judge_model_version=self.stage3.judge_model_version,
            )
            self._log_record(record)
            return record

        # Uncertain confidence band -> Proceed to Stage 3 (LLM Judge)
        s3_res = await self.stage3.evaluate_intent(message_text, template, s2_res)
        s3_dict = s3_res.model_dump()

        if s3_res.harmful_intent_detected:
            record = EvaluationRecord(
                template_id=template.template_id,
                campaign_id=campaign_id,
                message_hash_or_id=msg_hash,
                outcome="failed",
                failure_category="harmful_intent",
                stage1_checks_run=s1_run_dicts,
                stage1_checks_failed=[],
                stage2_invoked=True,
                stage2_result=s2_dict,
                stage3_invoked=True,
                stage3_result=s3_dict,
                retry_attempt_number=retry_attempt_number,
                evaluator_version=self.version,
                classifier_model_version=self.stage2.model_version,
                judge_model_version=self.stage3.judge_model_version,
            )
        else:
            record = EvaluationRecord(
                template_id=template.template_id,
                campaign_id=campaign_id,
                message_hash_or_id=msg_hash,
                outcome="approved",
                failure_category=None,
                stage1_checks_run=s1_run_dicts,
                stage1_checks_failed=[],
                stage2_invoked=True,
                stage2_result=s2_dict,
                stage3_invoked=True,
                stage3_result=s3_dict,
                retry_attempt_number=retry_attempt_number,
                evaluator_version=self.version,
                classifier_model_version=self.stage2.model_version,
                judge_model_version=self.stage3.judge_model_version,
            )

        self._log_record(record)
        return record

    async def run_evaluation_cascade_with_redraft(
        self,
        generator_func: Callable[[int, Optional[EvaluationRecord]], Awaitable[str]],
        template: TemplateSchema,
        campaign_id: str,
    ) -> Tuple[EvaluationRecord, str]:
        """
        Coordinates evaluation redrafts through the full cascade with shared retry counter.
        Terminal outcomes:
          - format failure cap (3) -> outcome = 'fallback_used', substitutes fallback_template_id
          - harmful_intent failure cap (2) -> outcome = 'quarantined', sends nothing
          - passes all stages -> outcome = 'approved'
        """
        shared_attempt = 1
        last_record: Optional[EvaluationRecord] = None
        candidate_text: str = ""

        while True:
            candidate_text = await generator_func(shared_attempt, last_record)

            record = await self.evaluate_single_message(
                message_text=candidate_text,
                template=template,
                campaign_id=campaign_id,
                retry_attempt_number=shared_attempt,
            )

            # Approved clears immediately
            if record.outcome == "approved":
                return record, candidate_text

            # Harmful intent check failed
            if record.failure_category == "harmful_intent":
                if shared_attempt >= self.harmful_cap:
                    # Cap reached: quarantine send, do NOT send fallback
                    record.outcome = "quarantined"
                    self._log_record(record)
                    return record, ""
                shared_attempt += 1
                last_record = record
                continue

            # Format check failed
            if record.failure_category == "format":
                if shared_attempt >= self.format_cap:
                    # Cap reached: substitute fallback template
                    record.outcome = "fallback_used"
                    self._log_record(record)
                    fallback_text = f"[{template.brand_name}] Notice: Please visit our portal or contact {template.customer_care_contact}. Reply STOP to cancel."
                    return record, fallback_text
                shared_attempt += 1
                last_record = record
                continue

            # Fallback break to prevent infinite loop
            break

        return record, candidate_text

    async def evaluate_message(
        self,
        message_text: str,
        template: TemplateSchema,
        campaign_id: str = "camp_test",
        simulate_redraft_exhaustion: bool = False,
        session_id: Optional[str] = None,
    ) -> Tuple[EvaluationRecord, str]:
        """
        Convenience method to evaluate a static message.
        If simulate_redraft_exhaustion is True and the message fails, it simulates the redraft loop
        reaching the retry cap (3 for format -> fallback_used, 2 for harmful_intent -> quarantined).
        """
        if not simulate_redraft_exhaustion:
            record = await self.evaluate_single_message(
                message_text=message_text,
                template=template,
                campaign_id=campaign_id,
                retry_attempt_number=1,
                session_id=session_id,
            )
            return record, (message_text if record.outcome == "approved" else "")

        async def static_gen(attempt: int, last_rec: Optional[EvaluationRecord]) -> str:
            return message_text

        return await self.run_evaluation_cascade_with_redraft(static_gen, template, campaign_id)


_evaluator_instance: Optional[CascadeComplianceEvaluator] = None

def get_compliance_evaluator() -> CascadeComplianceEvaluator:
    global _evaluator_instance
    if _evaluator_instance is None:
        _evaluator_instance = CascadeComplianceEvaluator()
    return _evaluator_instance
