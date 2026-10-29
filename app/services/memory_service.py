import json
import re
from typing import Dict, Any, List, Optional
from app.core.config import get_config
from app.core.storage import get_storage
from app.core.prompt_manager import get_prompt_manager
from app.core.llm_client import get_llm_client

class MemoryService:
    def __init__(self):
        self.config = get_config()
        self.storage = get_storage()
        self.prompts = get_prompt_manager()
        self.llm = get_llm_client()

    def get_memory_context(self, workspace_id: str, business_type: Optional[str] = None, business_name: Optional[str] = None) -> str:
        """Loads persistent memory entries formatted as text context for prompts, scoped by business/domain if provided."""
        entries = self.storage.get_workspace_memory(workspace_id)
        if not entries:
            return "No previous workspace memory recorded."
        
        relevant = []
        general = []
        for e in entries:
            cat = str(e.get("category", "general")).lower()
            key = str(e.get("key", "")).lower()
            val = str(e.get("value", "")).lower()

            # Direct match to active business type or active business name
            if business_type and (business_type.lower() in cat or business_type.lower() in key or business_type.lower() in val):
                relevant.append(e)
            elif business_name and (business_name.lower() in key or business_name.lower() in val):
                relevant.append(e)
            elif cat in ["policy", "compliance", "opt_out"]:
                # Only include universal policies that don't belong to a specific other brand/school/clinic
                if not any(k in key for k in ["school", "dental", "clinic", "hospital", "ngo", "foundation"]):
                    general.append(e)
            elif not business_type and not business_name:
                general.append(e)

        ordered = relevant + general
        lines = []
        for e in ordered[:self.config.memory.max_relevant_entries]:
            c = e.get("category", "general")
            k = e.get("key", "")
            v = e.get("value")
            lines.append(f"- [{c}] {k}: {v}")
        return "\n".join(lines) if lines else "No previous workspace memory recorded."

    async def extract_and_save_memory(self, session_id: str, workspace_id: str, campaign_data: Dict[str, Any]):
        """Runs LLM extraction to derive durable facts and preferences from the session."""
        existing_entries = self.storage.get_workspace_memory(workspace_id)
        
        system_prompt = self.prompts.load_prompt("memory_extractor")
        system_prompt = system_prompt.replace("{{campaign_data_json}}", json.dumps(campaign_data, indent=2))
        system_prompt = system_prompt.replace("{{existing_memory_json}}", json.dumps(existing_entries, indent=2))

        user_message = "Analyze the completed campaign and conversation. Extract new facts, preferred/rejected tones, and rules."

        llm_res = await self.llm.call_llm(
            session_id=session_id,
            purpose="memory_extract",
            system_prompt=system_prompt,
            user_message=user_message,
            step_id="memory_extract",
            json_mode=True,
            temperature=0.2
        )

        if llm_res.get("status") == "success":
            raw = llm_res.get("raw_response", "")
            try:
                from app.services.orchestrator_engine import parse_json_robust
                data = parse_json_robust(raw)
                if data:
                    entries = data.get("new_or_updated_memory_entries", [])
                    for entry in entries:
                        cat = entry.get("category", "business_fact")
                        k = entry.get("key", "info")
                        v = entry.get("value")
                        conf = float(entry.get("confidence", 1.0))
                        self.storage.write_memory(
                            workspace_id=workspace_id,
                            category=cat,
                            key=k,
                            value=v,
                            confidence=conf,
                            session_id=session_id
                        )
            except Exception as e:
                self.storage.log_error(session_id, "memory_extraction_parse_error", str(e), raw)

_memory_service: Optional[MemoryService] = None

def get_memory_service() -> MemoryService:
    global _memory_service
    if _memory_service is None:
        _memory_service = MemoryService()
    return _memory_service
