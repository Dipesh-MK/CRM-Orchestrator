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
    min_tone_candidates: int = 3
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

class ChannelConfig(BaseModel):
    adapter: str = "aggregator"
    tool_dir: str = "generated/aggregator"
    base_url_env: str = "APITOOL_BASE_URL"
    contract_files: list[str] = Field(default_factory=lambda: [".apitool/tools.spec.yaml", ".apitool/companion.spec.yaml"])
    agent_tool_allowlist: list[str] = Field(default_factory=list)

class DispatchConfig(BaseModel):
    enabled: bool = True
    require_user_confirm: bool = True
    max_messages_per_run: int = 50
    allow_non_loopback: bool = False
    allowed_hosts: list[str] = Field(default_factory=lambda: ["localhost", "localhost:8000", "127.0.0.1", "127.0.0.1:8000", "::1", "[::1]", "testserver", "test"])
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8000", "http://127.0.0.1:8000", "http://testserver", "http://test"])
    approval_ttl_seconds: int = 900
    registry_cache_ttl_seconds: int = 300
    require_registered_phone: bool = True
    send_interval_ms: int = 50
    status_callback_url: Optional[str] = None
    catch_up_policy: str = "all"

class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    channel: ChannelConfig = Field(default_factory=ChannelConfig)
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)

    @property
    def send(self) -> DispatchConfig:
        return self.dispatch

def load_env_defaults(env_path: str = ".env"):
    """Loads .env file and ensures default aggregator credentials are present in os.environ."""
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
    except Exception:
        pass

    path = Path(env_path)
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass

    defaults = {
        "APITOOL_BASE_URL": "http://localhost:3000",
        "AGGREGATOR_BASE_URL": "http://localhost:3000",
        "AGGREGATOR_ACCOUNT_SID": "AC_IDACT_001",
        "AGGREGATOR_AUTH_TOKEN": "idact_auth_token_secret_xyz789",
        "AGGREGATOR_FROM_NUMBER": "55512",
    }
    for k, v in defaults.items():
        if not os.environ.get(k):
            os.environ[k] = v

load_env_defaults()

def load_config(config_path: str = "config/app.yaml") -> AppConfig:
    load_env_defaults()
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

    channel_data = data.get("channel", {})
    if os.getenv("CHANNEL_ADAPTER"):
        channel_data["adapter"] = os.getenv("CHANNEL_ADAPTER")
    if os.getenv("CHANNEL_TOOL_DIR"):
        channel_data["tool_dir"] = os.getenv("CHANNEL_TOOL_DIR")
    if os.getenv("CHANNEL_BASE_URL_ENV"):
        channel_data["base_url_env"] = os.getenv("CHANNEL_BASE_URL_ENV")
    if os.getenv("CHANNEL_CONTRACT_FILES"):
        channel_data["contract_files"] = [f.strip() for f in os.getenv("CHANNEL_CONTRACT_FILES").split(",") if f.strip()]
    data["channel"] = channel_data

    dispatch_data = data.get("dispatch", {})
    if os.getenv("DISPATCH_ENABLED") is not None:
        dispatch_data["enabled"] = os.getenv("DISPATCH_ENABLED").lower() in ("1", "true", "yes")
    if os.getenv("DISPATCH_REQUIRE_USER_CONFIRM") is not None:
        dispatch_data["require_user_confirm"] = os.getenv("DISPATCH_REQUIRE_USER_CONFIRM").lower() in ("1", "true", "yes")
    if os.getenv("DISPATCH_ALLOW_NON_LOOPBACK") is not None:
        dispatch_data["allow_non_loopback"] = os.getenv("DISPATCH_ALLOW_NON_LOOPBACK").lower() in ("1", "true", "yes")
    if os.getenv("DISPATCH_MAX_MESSAGES_PER_RUN"):
        try:
            dispatch_data["max_messages_per_run"] = int(os.getenv("DISPATCH_MAX_MESSAGES_PER_RUN"))
        except ValueError:
            pass
    if os.getenv("DISPATCH_REQUIRE_REGISTERED_PHONE") is not None:
        dispatch_data["require_registered_phone"] = os.getenv("DISPATCH_REQUIRE_REGISTERED_PHONE").lower() in ("1", "true", "yes")
    if os.getenv("DISPATCH_APPROVAL_TTL_SECONDS"):
        try:
            dispatch_data["approval_ttl_seconds"] = int(os.getenv("DISPATCH_APPROVAL_TTL_SECONDS"))
        except ValueError:
            pass
    if os.getenv("DISPATCH_REGISTRY_CACHE_TTL_SECONDS"):
        try:
            dispatch_data["registry_cache_ttl_seconds"] = int(os.getenv("DISPATCH_REGISTRY_CACHE_TTL_SECONDS"))
        except ValueError:
            pass
    if os.getenv("DISPATCH_SEND_INTERVAL_MS"):
        try:
            dispatch_data["send_interval_ms"] = int(os.getenv("DISPATCH_SEND_INTERVAL_MS"))
        except ValueError:
            pass
    if os.getenv("DISPATCH_ALLOWED_HOSTS"):
        dispatch_data["allowed_hosts"] = [h.strip() for h in os.getenv("DISPATCH_ALLOWED_HOSTS").split(",") if h.strip()]
    if os.getenv("DISPATCH_ALLOWED_ORIGINS"):
        dispatch_data["allowed_origins"] = [o.strip() for o in os.getenv("DISPATCH_ALLOWED_ORIGINS").split(",") if o.strip()]
    if os.getenv("DISPATCH_STATUS_CALLBACK_URL"):
        dispatch_data["status_callback_url"] = os.getenv("DISPATCH_STATUS_CALLBACK_URL")
    data["dispatch"] = dispatch_data

    return AppConfig(**data)

# Singleton getter
_config: Optional[AppConfig] = None

def get_config(reload: bool = False) -> AppConfig:
    global _config
    if _config is None or reload:
        _config = load_config()
    return _config
