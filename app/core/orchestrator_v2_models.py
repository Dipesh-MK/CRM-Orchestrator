from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field

# -------------------------------------------------------------
# Uniform Compliance Tool Contract
# -------------------------------------------------------------
class ToolResult(BaseModel):
    tool_id: str
    category: Literal["format", "harmful_intent", "claims", "policy_gate"]
    passed: bool
    reason: str
    offending_span: Optional[str] = None
    severity: Literal["block", "warn"] = "block"

# -------------------------------------------------------------
# Claims Ledger & Instruction Ledger Models
# -------------------------------------------------------------
class ClaimEntry(BaseModel):
    claim_id: str
    claim_type: Literal["number", "date", "currency", "percentage", "url", "voting_logistics", "impact"]
    raw_value: str
    normalized_value: str
    source_description: str
    verified_by_user: bool = False
    source_url: Optional[str] = None

class InstructionEntry(BaseModel):
    instruction_id: str
    turn_index: int
    raw_instruction: str
    category: Literal["tone", "length", "include_topic", "exclude_topic", "disclaimer", "style", "other"]
    status: Literal["active", "superseded"] = "active"

# -------------------------------------------------------------
# Organization Record (org_registry)
# -------------------------------------------------------------
class OrgRecord(BaseModel):
    org_id: str
    display_name: str
    legal_name: str
    committee_name: Optional[str] = None
    org_type: Literal["501c3", "501c4", "501c5", "501c6", "527_political", "pac", "candidate_committee"]
    registered_domains: List[str] = Field(default_factory=list)
    customer_care_contact: str
    sending_numbers: List[str] = Field(default_factory=list)
    campaign_verify_token: Optional[str] = None
    registered_use_cases: List[str] = Field(default_factory=list)
    approved: bool = True

# -------------------------------------------------------------
# Card State Contract
# -------------------------------------------------------------
class RefusalSuggestion(BaseModel):
    label: str
    prefill_instruction: str

class CardRefusal(BaseModel):
    headline_variant: Literal["tone", "message"]
    reason: str
    suggestions: List[RefusalSuggestion] = Field(default_factory=list)

class CardBadges(BaseModel):
    sender: Literal["pass", "fail", "n/a"] = "n/a"
    intent: Literal["pass", "fail", "n/a"] = "n/a"
    links: Literal["pass", "fail", "n/a"] = "n/a"
    tone: Literal["pass", "fail", "n/a"] = "n/a"
    claims: Literal["pass", "fail", "n/a"] = "n/a"
    compliance: Literal["pass", "fail", "n/a"] = "n/a"

class CardState(BaseModel):
    card_type: Literal["review", "tone"]
    state: Literal["ready", "needs_info", "regenerating", "refused", "blocked_policy", "could_not_comply"]
    highlights: List[str] = Field(default_factory=list)
    questions: List[str] = Field(default_factory=list)
    badges: CardBadges = Field(default_factory=CardBadges)
    refusal: Optional[CardRefusal] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

# -------------------------------------------------------------
# Guardrail Agent Schema (Rubric Output)
# -------------------------------------------------------------
class FlaggedSpan(BaseModel):
    flag: bool
    evidence_span: Optional[str] = None
    reason: str

class InstructionAdherence(BaseModel):
    flag: bool
    missing_or_contradicted: List[str] = Field(default_factory=list)
    reason: str

class HarmfulIntentEvaluation(BaseModel):
    flag: bool
    category: Optional[str] = None
    reason: str

class GuardrailVerdict(BaseModel):
    failure_origin: Literal["none", "draft", "user_request"]
    instruction_adherence: InstructionAdherence
    requested_tone_match: FlaggedSpan
    aggressive_tone: FlaggedSpan
    deceptive_urgency_or_guilt: FlaggedSpan
    harmful_intent: HarmfulIntentEvaluation
    hidden_ask_in_thank_you: FlaggedSpan
    unsupported_claim: FlaggedSpan
    impersonates_official: FlaggedSpan
    voting_logistics_conflict: FlaggedSpan
    defamation_or_fabricated_claim_about_person: FlaggedSpan
    use_case_drift: FlaggedSpan
    disclosure_unclear: FlaggedSpan
    intent_or_sender_unclear: FlaggedSpan
    overall_pass: bool
    redraft_instructions: List[str] = Field(default_factory=list)
    user_refusal: Optional[CardRefusal] = None

# -------------------------------------------------------------
# Agent 2 Completeness Models
# -------------------------------------------------------------
class MissingSlotInfo(BaseModel):
    field: str
    why: str
    suggested_question: str

class CompletenessResult(BaseModel):
    complete: bool
    missing: List[MissingSlotInfo] = Field(default_factory=list)
    ambiguities: List[str] = Field(default_factory=list)
    instruction_ledger: List[InstructionEntry] = Field(default_factory=list)
    claims_ledger: List[ClaimEntry] = Field(default_factory=list)

# -------------------------------------------------------------
# Trace Event Schema
# -------------------------------------------------------------
class TraceEvent(BaseModel):
    ts: str
    run_id: str
    campaign_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    component_id: Literal[
        "agent1", "agent2", "guardrail", "supervisor",
        "compliance", "audience", "dispatcher", "inbound"
    ]
    component_kind: Literal["agent", "system"]
    type: Literal[
        "agent.start", "agent.thinking", "agent.output", "agent.end",
        "tool.call", "tool.result", "agent.handoff", "agent.message",
        "decision", "card.update", "compliance.outcome", "model.fallback", "error"
    ]
    to_component: Optional[str] = None
    tool_id: Optional[str] = None
    tool_args: Optional[Dict[str, Any]] = None
    tool_result: Optional[Dict[str, Any]] = None
    model: Optional[str] = None
    latency_ms: Optional[float] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    status: Literal["ok", "fail", "retry", "refused", "quarantined", "blocked"] = "ok"
    summary: str
