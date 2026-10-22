from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List, Tuple
from pydantic import BaseModel, Field
import re

ALLOWED_FIELD_TYPES = {"string", "password", "number", "url", "boolean", "text"}
FIELD_KEY_REGEX = re.compile(r"^[a-zA-Z0-9_-]+$")
MAX_DISCOVERED_FIELDS = 20

class DynamicConfigField(BaseModel):
    key: str
    label: str
    type: str = "string"
    required: bool = False
    secret: bool = False
    default: Optional[Any] = None
    description: Optional[str] = None

class HandshakeResult(BaseModel):
    success: bool
    status: str  # "verified" | "unverified"
    provider_name: str
    message: str
    capabilities: Dict[str, bool] = Field(default_factory=lambda: {"sms": True, "mms": False, "delivery_receipts": True})
    dynamic_form_schema: Optional[List[Dict[str, Any]]] = None
    raw_response: Optional[Dict[str, Any]] = None

class NormalizedSendResult(BaseModel):
    status: str  # "queued" | "sent" | "delivered" | "failed"
    provider_message_id: Optional[str] = None
    recipient_phone: str
    error_reason: Optional[str] = None
    raw_provider_response: Optional[Dict[str, Any]] = None

class NormalizedInboundMessage(BaseModel):
    message_id: str
    from_phone: str
    to_phone: str
    body: str
    media_urls: List[str] = Field(default_factory=list)
    received_at_iso: str
    raw_payload: Dict[str, Any] = Field(default_factory=dict)

class NormalizedDeliveryStatus(BaseModel):
    provider_message_id: str
    recipient_phone: str
    status: str  # "queued" | "sent" | "delivered" | "undelivered" | "failed"
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    timestamp_iso: str
    raw_payload: Dict[str, Any] = Field(default_factory=dict)

def sanitize_discovery_schema(payload: Any) -> Tuple[bool, List[Dict[str, Any]], Dict[str, bool]]:
    """
    Sanitizes untrusted discovery response from {endpoint}/capabilities:
    - Verifies JSON structure
    - Whitelists field types
    - Validates field keys
    - Caps number of fields at MAX_DISCOVERED_FIELDS
    - Extracts capabilities map
    """
    if not isinstance(payload, dict):
        return False, [], {}

    raw_fields = payload.get("config_fields")
    if not isinstance(raw_fields, list):
        return False, [], {}

    sanitized_fields: List[Dict[str, Any]] = []
    for item in raw_fields[:MAX_DISCOVERED_FIELDS]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        if not key or not FIELD_KEY_REGEX.match(key):
            continue

        raw_type = str(item.get("type", "string")).lower().strip()
        field_type = raw_type if raw_type in ALLOWED_FIELD_TYPES else "string"
        label = str(item.get("label", key))[:80]
        required = bool(item.get("required", False))
        secret = bool(item.get("secret", False))

        sanitized_fields.append({
            "key": key,
            "label": label,
            "type": field_type,
            "required": required,
            "secret": secret
        })

    capabilities = {}
    raw_caps = payload.get("capabilities")
    if isinstance(raw_caps, dict):
        for k, v in raw_caps.items():
            if isinstance(k, str) and len(k) < 40 and isinstance(v, bool):
                capabilities[k] = v

    if not capabilities:
        capabilities = {"sms": True, "mms": False, "delivery_receipts": True}

    return True, sanitized_fields, capabilities

class MessagingProvider(ABC):
    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Unique slug identifier, e.g. 'twilio', 'infobip', 'mock'."""
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable display name, e.g. 'Twilio', 'Infobip', 'Mock Aggregator'."""
        pass

    @abstractmethod
    async def handshake(self, endpoint_url: Optional[str], partial_config: Dict[str, Any]) -> HandshakeResult:
        """
        Validates configuration or queries dynamic capability schema.
        Must fail gracefully without raising uncaught exceptions on bad/dummy URLs.
        """
        pass

    @abstractmethod
    async def send_message(self, from_phone: str, to_phone: str, body: str,
                           media_urls: Optional[List[str]] = None,
                           status_callback_url: Optional[str] = None,
                           config: Optional[Dict[str, Any]] = None) -> NormalizedSendResult:
        """Sends an outbound SMS/MMS message."""
        pass

    @abstractmethod
    async def handle_inbound_webhook(self, request_headers: Dict[str, str],
                                     raw_body: bytes,
                                     query_params: Dict[str, str],
                                     config: Dict[str, Any]) -> NormalizedInboundMessage:
        """Validates provider signature and extracts normalized inbound SMS."""
        pass

    @abstractmethod
    async def parse_delivery_status(self, request_headers: Dict[str, str],
                                    raw_body: bytes,
                                    config: Dict[str, Any]) -> NormalizedDeliveryStatus:
        """Parses delivery receipt webhook payload."""
        pass

    @abstractmethod
    def validate_config(self, config: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Validates that all necessary configuration keys are provided."""
        pass
