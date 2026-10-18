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
    "thanks", "thank", "you", "great", "awesome", "perfect", "got", "it", "fine", "done", "please", "go", "ahead"
}

def is_chit_chat_or_ack(text: str) -> bool:
    """Returns True if the text is pure conversational pleasantry or acknowledgment without campaign facts."""
    clean = re.sub(r"[^a-zA-Z\s]", "", text).strip().lower()
    if not clean:
        return True
    words = clean.split()
    if len(words) <= 5 and all(w in CHIT_CHAT_WORDS for w in words):
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
        if context_label in ("user_input", "discovery_input") and is_chit_chat_or_ack(text_to_evaluate):
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
        safety_policy = self.prompts.load_prompt("safety_policy")
        
        system_prompt = f"""{safety_policy}

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
                try:
                    # Clean markdown fence if present
                    clean = re.sub(r"^```json\s*", "", raw.strip())
                    clean = re.sub(r"\s*```$", "", clean)
                    data = json.loads(clean)
                    if "allowed" in data:
                        result = data
                        break
                except Exception as e:
                    last_error = str(e)
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

_safety_service: Optional[SafetyService] = None

def get_safety_service() -> SafetyService:
    global _safety_service
    if _safety_service is None:
        _safety_service = SafetyService()
    return _safety_service
