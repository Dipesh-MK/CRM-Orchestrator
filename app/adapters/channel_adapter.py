import os
import sys
import json
import time
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Callable
from urllib.parse import urlparse
from pathlib import Path
from app.core.config import get_config
from app.core.storage import get_storage

class ChannelAdapter(ABC):
    @abstractmethod
    def get_agent_tools(self) -> List[Dict[str, Any]]:
        """Returns tools exposed to LLM (strictly filtered by allowlist, no operator tools)."""
        pass

    @abstractmethod
    def stage_send(self, to: str, body: str, session_id: str, status_callback: Optional[str] = None) -> Dict[str, Any]:
        """Stages send_message via call_tool returning pending_action."""
        pass

    @abstractmethod
    def execute_send(self, action_id: str, to: str, body: str, verifier: Callable,
                     status_callback: Optional[str] = None, session_id: str = "",
                     run_id: str = "", item_id: str = "") -> Dict[str, Any]:
        """Executes approved action via execute_approved with injected verifier."""
        pass

    @abstractmethod
    def get_registered_phones(self, force_refresh: bool = False) -> List[str]:
        """Fetches registered virtual handsets from provider with TTL caching."""
        pass

    @abstractmethod
    def list_audit_messages(self, limit: int = 20) -> Dict[str, Any]:
        """Calls list_messages for audit trail."""
        pass

def load_forbidden_tools_from_contracts(contract_paths: Optional[List[Path]] = None) -> set[str]:
    """
    Contract-derived rule (Section B):
    A tool is forbidden for the agent if safety is 'write' or 'destructive',
    or approval is 'ui', in either contract.
    Fails closed: raises FileNotFoundError or ValueError if any contract is missing or invalid.
    """
    import yaml
    if contract_paths is None:
        cfg = get_config()
        if hasattr(cfg, "channel") and hasattr(cfg.channel, "contract_files") and cfg.channel.contract_files:
            contract_paths = [Path(p).resolve() for p in cfg.channel.contract_files]
        else:
            contract_paths = [
                Path(".apitool/tools.spec.yaml").resolve(),
                Path(".apitool/companion.spec.yaml").resolve()
            ]
    forbidden: set[str] = set()
    for p in contract_paths:
        p_path = Path(p).resolve()
        if not p_path.exists():
            raise FileNotFoundError(f"Contract file not found: {p_path}")
        try:
            content = p_path.read_text(encoding="utf-8")
            data = yaml.safe_load(content)
        except Exception as e:
            raise ValueError(f"Invalid YAML in contract file {p_path}: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"Contract file {p_path} must be a dictionary")
        for t in data.get("tools", []):
            safety = str(t.get("safety", "")).lower()
            approval = str(t.get("approval", "")).lower()
            if safety in ("write", "destructive") or approval == "ui":
                name = t.get("name")
                if name:
                    forbidden.add(name)
    return forbidden

# Dynamically derived from contracts; no hardcoded tool names
FORBIDDEN_AGENT_WRITE_TOOLS = load_forbidden_tools_from_contracts()

class AggregatorChannelAdapter(ChannelAdapter):
    """
    Channel adapter wrapping generated/aggregator/core.py.
    Provides strict loopback enforcement, operator tool access, credential redaction,
    and phone registry caching.
    """
    def __init__(self, tool_dir: Optional[str] = None, contract_paths: Optional[List[Path]] = None):
        cfg = get_config()
        self.tool_dir = Path(tool_dir or cfg.channel.tool_dir)
        self.config = cfg
        self.storage = get_storage()
        self.contract_paths = contract_paths or [
            Path(".apitool/tools.spec.yaml"),
            Path(".apitool/companion.spec.yaml")
        ]
        
        # Sync AGGREGATOR environment variables with safe defaults for runtime tool specs
        base_url_val = os.getenv("AGGREGATOR_BASE_URL") or (cfg.channel.base_url_env and os.getenv(cfg.channel.base_url_env)) or "http://localhost:3000"
        os.environ["APITOOL_BASE_URL"] = base_url_val
        os.environ["AGGREGATOR_BASE_URL"] = base_url_val
        if "AGGREGATOR_ACCOUNT_SID" not in os.environ:
            os.environ["AGGREGATOR_ACCOUNT_SID"] = os.getenv("AGGREGATOR_ACCOUNT_SID", "AC_IDACT_001")
        if "AGGREGATOR_AUTH_TOKEN" not in os.environ:
            os.environ["AGGREGATOR_AUTH_TOKEN"] = os.getenv("AGGREGATOR_AUTH_TOKEN", "mock_auth_token")

        # Load generated runtime ApiTools
        sys.path.insert(0, str(self.tool_dir.resolve()))
        from core import ApiTools
        self.runtime = ApiTools(root=str(self.tool_dir), config_file="tools.spec.yaml")
        
        # Security: Loopback enforcement
        self._verify_loopback()

        # Provider phone registry cache
        self._cached_phones: List[str] = []
        self._cache_timestamp: float = 0.0

    def _verify_loopback(self):
        allow_non_loopback = self.config.dispatch.allow_non_loopback
        base_url = self.runtime.base_url or ""
        parsed = urlparse(base_url)
        hostname = (parsed.hostname or "").lower()
        is_loopback = hostname in ("localhost", "127.0.0.1", "::1", "testserver")
        if not allow_non_loopback and not is_loopback:
            raise ValueError(f"Non-loopback provider URL '{base_url}' refused by dispatch security policy")

    def get_forbidden_agent_tools(self) -> set[str]:
        return load_forbidden_tools_from_contracts(self.contract_paths)

    def get_agent_tools(self) -> List[Dict[str, Any]]:
        """Returns tools matching channel.agent_tool_allowlist only. In Milestone 1, strictly NO write tools."""
        allowlist = set(self.config.channel.agent_tool_allowlist)
        forbidden = self.get_forbidden_agent_tools()
        safe_allowlist = allowlist - forbidden
        all_tools = self.runtime.list_tools()
        # Filter strictly by allowlist and exclude any write tool
        filtered = [t for t in all_tools if t["name"] in safe_allowlist and t["name"] not in forbidden]
        if "propose_dispatch" in allowlist:
            filtered.append({
                "name": "propose_dispatch",
                "description": "Generates a dry-run campaign dispatch plan for human review. Does NOT send any messages.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "campaign_id": {"type": "string"},
                        "template_step": {"type": "integer", "default": 1}
                    },
                    "required": ["campaign_id"]
                }
            })
        return filtered

    def get_registered_phones(self, force_refresh: bool = False) -> List[str]:
        """
        Amendment 3: Registered phones come from the operator tool list_phone_numbers,
        cached with dispatch.registry_cache_ttl_seconds, with a refresh endpoint.
        """
        now = time.time()
        ttl = self.config.dispatch.registry_cache_ttl_seconds
        if not force_refresh and self._cached_phones and (now - self._cache_timestamp < ttl):
            return list(self._cached_phones)

        # Call operator tool list_phone_numbers
        t0 = time.time()
        res = self.runtime.call_operator_tool("list_phone_numbers", {})
        latency_ms = int((time.time() - t0) * 1000)
        
        # Log to channel_calls (redacted)
        self.storage.log_channel_call(
            session_id="system_registry",
            tool_name="list_phone_numbers",
            operation="get_api_phone_numbers",
            request_url=f"{self.runtime.base_url}/api/phone/numbers",
            method="GET",
            redacted_request_body=None,
            status_code=res.get("status", 200),
            redacted_response_body=json.dumps({"count": len(res.get("data", {}).get("data", [])) if isinstance(res.get("data"), dict) else 0}),
            latency_ms=latency_ms
        )

        phones: List[str] = []
        if res.get("ok"):
            data = res.get("data", {})
            phone_list = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for p in phone_list:
                num = p.get("phone_number")
                if num:
                    phones.append(num)
        
        self._cached_phones = phones
        self._cache_timestamp = now
        return list(phones)

    def stage_send(self, to: str, body: str, session_id: str, status_callback: Optional[str] = None,
                   job_id: str = "", item_id: str = "") -> Dict[str, Any]:
        """Calls call_tool('send_message') to stage pending action."""
        args: Dict[str, Any] = {"To": to, "Body": body}
        cb = status_callback or self.config.dispatch.status_callback_url
        if cb:
            args["StatusCallback"] = cb
            
        t0 = time.time()
        res = self.runtime.call_tool("send_message", args)
        latency_ms = int((time.time() - t0) * 1000)
        
        redacted_args = {"To": to, "Body": body}
        self.storage.log_channel_call(
            session_id=session_id,
            job_id=job_id,
            item_id=item_id,
            tool_name="send_message",
            operation="post_2010_04_01_accounts_account_sid_messages_json",
            request_url=f"{self.runtime.base_url}/Messages.json (staging)",
            method="STAGE",
            redacted_request_body=json.dumps(redacted_args),
            status_code=200 if res.get("ok") else 400,
            redacted_response_body=json.dumps(res),
            latency_ms=latency_ms
        )
        return res

    def execute_send(self, action_id: str, to: str, body: str, verifier: Callable,
                     status_callback: Optional[str] = None, session_id: str = "",
                     run_id: str = "", item_id: str = "") -> Dict[str, Any]:
        """Calls execute_approved('send_message') with injected verifier."""
        args: Dict[str, Any] = {"To": to, "Body": body}
        cb = status_callback or self.config.dispatch.status_callback_url
        if cb:
            args["StatusCallback"] = cb

        t0 = time.time()
        res = self.runtime.execute_approved(action_id, "send_message", args, verifier=verifier)
        latency_ms = int((time.time() - t0) * 1000)

        redacted_req = {"To": to, "Body": body}
        redacted_resp = None
        if res.get("ok"):
            d = res.get("data", {})
            redacted_resp = json.dumps({"sid": d.get("sid"), "status": d.get("status")})
        else:
            redacted_resp = json.dumps(res.get("error", {}))

        self.storage.log_channel_call(
            session_id=session_id,
            tool_name="send_message",
            operation="post_2010_04_01_accounts_account_sid_messages_json",
            request_url=f"{self.runtime.base_url}/Messages.json",
            method="POST",
            redacted_request_body=json.dumps(redacted_req),
            status_code=res.get("status", 201 if res.get("ok") else 400),
            redacted_response_body=redacted_resp,
            latency_ms=latency_ms,
            job_id=run_id,
            item_id=item_id
        )
        return res

    def list_audit_messages(self, limit: int = 20) -> Dict[str, Any]:
        t0 = time.time()
        res = self.runtime.call_tool("list_messages", {"limit": limit})
        latency_ms = int((time.time() - t0) * 1000)
        
        self.storage.log_channel_call(
            session_id="system_audit",
            tool_name="list_messages",
            operation="get_api_admin_messages",
            request_url=f"{self.runtime.base_url}/api/admin/messages",
            method="GET",
            redacted_request_body=None,
            status_code=200 if res.get("ok") else 400,
            redacted_response_body=json.dumps({"count": len(res.get("data", {}).get("data", [])) if isinstance(res.get("data"), dict) else 0}),
            latency_ms=latency_ms
        )
        return res

    def read_operator_status(self, sid: str, session_id: str = "system_status",
                             job_id: str = "", item_id: str = "") -> Dict[str, Any]:
        """Calls operator tool get_admin_message_status to check live message status."""
        t0 = time.time()
        res = self.runtime.call_operator_tool("get_admin_message_status", {"sid": sid})
        latency_ms = int((time.time() - t0) * 1000)
        self.storage.log_channel_call(
            session_id=session_id,
            job_id=job_id,
            item_id=item_id,
            tool_name="get_admin_message_status",
            operation="get_api_admin_messages_sid",
            request_url=f"{self.runtime.base_url}/api/admin/messages/{sid}",
            method="GET",
            redacted_request_body=None,
            status_code=res.get("status", 200),
            redacted_response_body=json.dumps(res.get("data", {})),
            latency_ms=latency_ms
        )
        if res.get("ok"):
            d = res.get("data", {})
            return d.get("data", d) if isinstance(d, dict) else {}
        return {}

_channel_adapter: Optional[ChannelAdapter] = None

def get_channel_adapter(reload: bool = False) -> ChannelAdapter:
    global _channel_adapter
    if _channel_adapter is None or reload:
        _channel_adapter = AggregatorChannelAdapter()
    return _channel_adapter
