from datetime import datetime, timezone
from typing import List, Optional, Literal, Dict, Any
from pydantic import BaseModel, Field

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

class TemplateSchema(BaseModel):
    template_id: str
    moment: Literal["call_to_action", "opt_in_confirmation", "regular_send", "opt_out_confirmation"]
    requires_rates_disclosure: bool
    registered_domains: List[str] = Field(default_factory=list)
    brand_name: str
    customer_care_contact: str
    fallback_template_id: str

class Stage1CheckResult(BaseModel):
    check_id: str
    category: Literal["format", "harmful_intent"]
    passed: bool
    reason: str
    offending_span: Optional[str] = None

class Stage2Result(BaseModel):
    label: str
    confidence: float

class Stage3JudgeResult(BaseModel):
    harmful_intent_detected: bool
    intent_category: Literal["phishing", "fraud", "social_engineering", "none"] | str
    confidence: Literal["high", "medium", "low"]
    reason: str

class EvaluationRecord(BaseModel):
    timestamp: str = Field(default_factory=utc_now_iso)
    template_id: str
    campaign_id: str
    message_hash_or_id: str
    outcome: Literal["approved", "failed", "fallback_used", "quarantined"]
    failure_category: Optional[Literal["format", "harmful_intent"]] = None
    stage1_checks_run: List[Dict[str, Any]] = Field(default_factory=list)
    stage1_checks_failed: List[Dict[str, Any]] = Field(default_factory=list)
    stage2_invoked: bool = False
    stage2_result: Optional[Dict[str, Any]] = None
    stage3_invoked: bool = False
    stage3_result: Optional[Dict[str, Any]] = None
    retry_attempt_number: int = 1
    evaluator_version: str = "1.0.0"
    classifier_model_version: Optional[str] = None
    judge_model_version: Optional[str] = None
