import os
import yaml
from pathlib import Path
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field

class LLMConfig(BaseModel):
    base_url: str = "http://localhost:11434/v1"
    model: str = "qwen2.5:7b-instruct"
    api_key_env: str = "LLM_API_KEY"
    api_key: str = "ollama"
    temperature: float = 0.3
    request_timeout_seconds: int = 60

class OrchestratorConfig(BaseModel):
    word_limit: int = 320
    max_retries: int = 3
    max_discovery_rounds: int = 5
    optout_text: str = "Reply STOP to unsubscribe."
    personalization_tag_format: str = "{{tag}}"
    default_workspace_id: str = "default_workspace"

class PathsConfig(BaseModel):
    database_path: str = "data/campaignforge.db"
    logs_dir: str = "logs"
    prompts_dir: str = "prompts"
    suppression_file: str = "data/suppression.json"

class MemoryConfig(BaseModel):
    enabled: bool = True
    max_relevant_entries: int = 10

class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

def load_config(config_path: str = "config/app.yaml") -> AppConfig:
    data: Dict[str, Any] = {}
    path = Path(config_path)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
            if loaded and isinstance(loaded, dict):
                data = loaded

    # Overlay environment variables
    llm_data = data.get("llm", {})
    if os.getenv("LLM_BASE_URL"):
        llm_data["base_url"] = os.getenv("LLM_BASE_URL")
    if os.getenv("LLM_MODEL"):
        llm_data["model"] = os.getenv("LLM_MODEL")
    api_key_env_var = llm_data.get("api_key_env", "LLM_API_KEY")
    if api_key_env_var and os.getenv(api_key_env_var):
        llm_data["api_key"] = os.getenv(api_key_env_var)
    elif os.getenv("LLM_API_KEY"):
        llm_data["api_key"] = os.getenv("LLM_API_KEY")
    else:
        llm_data["api_key"] = llm_data.get("api_key", "ollama")
    if os.getenv("LLM_TEMPERATURE"):
        try:
            llm_data["temperature"] = float(os.getenv("LLM_TEMPERATURE"))
        except ValueError:
            pass
    if os.getenv("LLM_TIMEOUT"):
        try:
            llm_data["request_timeout_seconds"] = int(os.getenv("LLM_TIMEOUT"))
        except ValueError:
            pass
    data["llm"] = llm_data

    orch_data = data.get("orchestrator", {})
    if os.getenv("WORD_LIMIT"):
        try:
            orch_data["word_limit"] = int(os.getenv("WORD_LIMIT"))
        except ValueError:
            pass
    if os.getenv("MAX_RETRIES"):
        try:
            orch_data["max_retries"] = int(os.getenv("MAX_RETRIES"))
        except ValueError:
            pass
    if os.getenv("MAX_DISCOVERY_ROUNDS"):
        try:
            orch_data["max_discovery_rounds"] = int(os.getenv("MAX_DISCOVERY_ROUNDS"))
        except ValueError:
            pass
    if os.getenv("OPTOUT_TEXT"):
        orch_data["optout_text"] = os.getenv("OPTOUT_TEXT")
    if os.getenv("DEFAULT_WORKSPACE_ID"):
        orch_data["default_workspace_id"] = os.getenv("DEFAULT_WORKSPACE_ID")
    data["orchestrator"] = orch_data

    paths_data = data.get("paths", {})
    if os.getenv("DATABASE_PATH"):
        paths_data["database_path"] = os.getenv("DATABASE_PATH")
    if os.getenv("LOGS_DIR"):
        paths_data["logs_dir"] = os.getenv("LOGS_DIR")
    if os.getenv("PROMPTS_DIR"):
        paths_data["prompts_dir"] = os.getenv("PROMPTS_DIR")
    if os.getenv("SUPPRESSION_FILE"):
        paths_data["suppression_file"] = os.getenv("SUPPRESSION_FILE")
    data["paths"] = paths_data

    return AppConfig(**data)

# Singleton getter
_config: Optional[AppConfig] = None

def get_config(reload: bool = False) -> AppConfig:
    global _config
    if _config is None or reload:
        _config = load_config()
    return _config
