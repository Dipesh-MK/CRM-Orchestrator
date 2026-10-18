import uuid
import time
import json
import asyncio
import re
import httpx
from typing import Dict, Any, Optional, List
from app.core.config import get_config
from app.core.storage import get_storage, utc_now_iso

PURPOSE_MAX_TOKENS = {
    "safety_guard": 120,
    "tone_proposal": 500,
    "discovery": 450,
    "planner": 450,
    "template_write": 650,
    "repair": 300,
    "memory_extract": 300,
}

class LLMClient:
    def __init__(self):
        self.config = get_config()
        self.storage = get_storage()

    async def check_health(self) -> Dict[str, Any]:
        """Verify if Ollama/OpenAI server and target model are reachable."""
        base_url = self.config.llm.base_url.rstrip("/")
        # If OpenAI format /v1 is used, root or /models is standard
        tags_url = base_url.replace("/v1", "") + "/api/tags"
        models_url = f"{base_url}/models"
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Try ollama native tags first
            try:
                res = await client.get(tags_url)
                if res.status_code == 200:
                    models_data = res.json()
                    available_models = [m.get("name") for m in models_data.get("models", [])]
                    target = self.config.llm.model
                    is_available = any(target in m or m in target for m in available_models)
                    return {
                        "reachable": True,
                        "type": "ollama",
                        "available_models": available_models,
                        "target_model": target,
                        "target_model_available": is_available,
                        "message": "Ollama reachable" if is_available else f"Model {target} not found in Ollama models: {available_models}"
                    }
            except Exception:
                pass

            # Try OpenAI compatible /models endpoint
            try:
                headers = {}
                if self.config.llm.api_key:
                    headers["Authorization"] = f"Bearer {self.config.llm.api_key}"
                res = await client.get(models_url, headers=headers)
                if res.status_code == 200:
                    models_data = res.json()
                    available_models = [m.get("id") for m in models_data.get("data", [])]
                    target = self.config.llm.model
                    is_available = any(target in m or m in target for m in available_models)
                    return {
                        "reachable": True,
                        "type": "openai_compatible",
                        "available_models": available_models,
                        "target_model": target,
                        "target_model_available": is_available,
                        "message": "OpenAI-compatible API reachable"
                    }
            except Exception as e:
                return {
                    "reachable": False,
                    "target_model": self.config.llm.model,
                    "target_model_available": False,
                    "error": str(e),
                    "message": f"LLM server unreachable at {self.config.llm.base_url}. Please ensure Ollama or API server is running."
                }

            return {
                "reachable": False,
                "target_model": self.config.llm.model,
                "target_model_available": False,
                "message": f"Could not connect to {self.config.llm.base_url}"
            }

    async def call_llm(
        self,
        session_id: str,
        purpose: str,
        system_prompt: str,
        user_message: str,
        step_id: Optional[str] = None,
        json_mode: bool = True,
        temperature: Optional[float] = None
    ) -> Dict[str, Any]:
        """Execute a call to the LLM with timing, token tracking, logging, and error handling."""
        call_id = f"llm_{uuid.uuid4().hex[:12]}"
        temp = temperature if temperature is not None else self.config.llm.temperature
        base_url = self.config.llm.base_url.rstrip("/")
        endpoint = f"{base_url}/chat/completions"
        
        headers = {
            "Content-Type": "application/json"
        }
        if self.config.llm.api_key:
            headers["Authorization"] = f"Bearer {self.config.llm.api_key}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ]

        max_tokens = PURPOSE_MAX_TOKENS.get(purpose, 800)
        payload: Dict[str, Any] = {
            "model": self.config.llm.model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": max_tokens,
            "stream": False,
            "keep_alive": "15m",
            "options": {
                "num_predict": max_tokens,
                "keep_alive": "15m"
            }
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        start_time = time.time()
        raw_response = ""
        parsed_output = ""
        prompt_tokens = 0
        completion_tokens = 0
        status = "pending"
        error_msg = None

        # Retry loop for transient failures and HTTP 429 rate limits
        max_rate_limit_retries = 3
        total_attempts = max_rate_limit_retries + 1

        for attempt in range(total_attempts):
            start_time = time.time()
            try:
                async with httpx.AsyncClient(timeout=float(self.config.llm.request_timeout_seconds)) as client:
                    resp = await client.post(endpoint, json=payload, headers=headers)
                    latency_ms = int((time.time() - start_time) * 1000)

                    if resp.status_code == 200:
                        resp_json = resp.json()
                        choices = resp_json.get("choices", [])
                        if choices:
                            raw_response = choices[0].get("message", {}).get("content", "")
                            parsed_output = raw_response
                        
                        usage = resp_json.get("usage", {})
                        prompt_tokens = usage.get("prompt_tokens", 0)
                        completion_tokens = usage.get("completion_tokens", 0)

                        if prompt_tokens == 0:
                            prompt_tokens = (len(system_prompt) + len(user_message)) // 4
                        if completion_tokens == 0:
                            completion_tokens = len(raw_response) // 4

                        status = "success"
                        error_msg = None
                        break

                    elif resp.status_code == 429:
                        status = "rate_limited"
                        raw_response = resp.text
                        error_msg = f"HTTP 429 Rate Limit: {resp.text}"
                        
                        # Try parsing wait duration from header or body
                        wait_seconds = 2.0 * (attempt + 1)
                        try:
                            resp_err = resp.json().get("error", {})
                            msg = resp_err.get("message", "")
                            # Look for patterns like "try again in 28.619s" or "try again in 5s"
                            match = re.search(r"try again in ([0-9]+(?:\.[0-9]+)?)s", msg)
                            if match:
                                wait_seconds = min(float(match.group(1)) + 0.5, 35.0)
                        except Exception:
                            pass
                        
                        if attempt < total_attempts - 1:
                            print(f"[RateLimit] 429 encountered in {purpose}. Waiting {wait_seconds:.1f}s before retry {attempt + 1}/{max_rate_limit_retries}...")
                            await asyncio.sleep(wait_seconds)
                            continue
                        else:
                            break

                    else:
                        status = "error"
                        error_msg = f"HTTP {resp.status_code}: {resp.text}"
                        raw_response = resp.text
                        break

            except Exception as e:
                latency_ms = int((time.time() - start_time) * 1000)
                status = "error"
                error_msg = str(e)
                if attempt < total_attempts - 1:
                    await asyncio.sleep(1.5)
                    continue
                break

        # Log call record
        log_record = {
            "id": call_id,
            "session_id": session_id,
            "step_id": step_id or purpose,
            "purpose": purpose,
            "timestamp": utc_now_iso(),
            "model": self.config.llm.model,
            "base_url": self.config.llm.base_url,
            "temperature": temp,
            "full_prompt": json.dumps({"system": system_prompt, "messages": messages}),
            "full_raw_response": raw_response,
            "parsed_output": parsed_output,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_ms": latency_ms,
            "retries": 0,
            "status": status,
            "error": error_msg
        }
        self.storage.log_llm_call(log_record)

        return {
            "id": call_id,
            "status": status,
            "raw_response": raw_response,
            "error": error_msg,
            "latency_ms": latency_ms,
            "tokens": {"prompt": prompt_tokens, "completion": completion_tokens}
        }

_llm_client: Optional[LLMClient] = None

def get_llm_client() -> LLMClient:
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
