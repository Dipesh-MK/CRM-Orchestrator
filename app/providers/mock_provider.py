import os
import json
from typing import Dict, Any, Optional, List, Tuple
import httpx
from datetime import datetime, timezone

from app.providers.base import (
    MessagingProvider,
    HandshakeResult,
    NormalizedSendResult,
    NormalizedInboundMessage,
    NormalizedDeliveryStatus,
    sanitize_discovery_schema
)

DEFAULT_FALLBACK_FIELDS = [
    {"key": "api_key", "label": "API Key / Auth Secret", "type": "string", "required": False, "secret": True},
    {"key": "sender_id", "label": "Sender ID", "type": "string", "required": False, "secret": False},
    {"key": "custom_header", "label": "Custom Header Name", "type": "string", "required": False, "secret": False}
]

class MockAggregatorProvider(MessagingProvider):
    @property
    def provider_id(self) -> str:
        return "mock"

    @property
    def display_name(self) -> str:
        return "Mock Aggregator"

    def validate_config(self, config: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        # Mock aggregator is flexible and can work with partial or dummy config
        return True, None

    async def handshake(self, endpoint_url: Optional[str], partial_config: Dict[str, Any]) -> HandshakeResult:
        base_url = (endpoint_url or partial_config.get("endpoint_url") or partial_config.get("base_url") or "").rstrip("/")

        if not base_url:
            return HandshakeResult(
                success=False,
                status="unverified",
                provider_name=self.display_name,
                message="No endpoint URL provided for Mock Aggregator. Provide a base URL to probe /capabilities.",
                dynamic_form_schema=DEFAULT_FALLBACK_FIELDS
            )

        capabilities_url = f"{base_url}/capabilities"

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(capabilities_url)
                if res.status_code == 200:
                    try:
                        raw_json = res.json()
                        valid, sanitized_fields, caps = sanitize_discovery_schema(raw_json)
                        if valid and sanitized_fields:
                            prov_name = raw_json.get("provider_name") or self.display_name
                            return HandshakeResult(
                                success=True,
                                status="verified",
                                provider_name=prov_name,
                                message=f"Successfully discovered capabilities schema from {capabilities_url}.",
                                capabilities=caps,
                                dynamic_form_schema=sanitized_fields,
                                raw_response=raw_json
                            )
                        else:
                            # 200 OK but payload does not conform to discovery contract
                            return HandshakeResult(
                                success=False,
                                status="unverified",
                                provider_name=self.display_name,
                                message=f"Endpoint {capabilities_url} returned non-conforming capabilities schema; falling back to manual config.",
                                dynamic_form_schema=DEFAULT_FALLBACK_FIELDS,
                                raw_response={"status": 200, "text": res.text[:200]}
                            )
                    except Exception:
                        return HandshakeResult(
                            success=False,
                            status="unverified",
                            provider_name=self.display_name,
                            message=f"Endpoint {capabilities_url} returned non-JSON response; falling back to manual config.",
                            dynamic_form_schema=DEFAULT_FALLBACK_FIELDS
                        )
                else:
                    return HandshakeResult(
                        success=False,
                        status="unverified",
                        provider_name=self.display_name,
                        message=f"Handshake failed with HTTP {res.status_code} at {capabilities_url}; falling back to manual config.",
                        dynamic_form_schema=DEFAULT_FALLBACK_FIELDS
                    )
        except httpx.TimeoutException:
            return HandshakeResult(
                success=False,
                status="unverified",
                provider_name=self.display_name,
                message=f"Handshake timed out after 5.0s contacting {capabilities_url}; falling back to manual config.",
                dynamic_form_schema=DEFAULT_FALLBACK_FIELDS
            )
        except Exception as e:
            # Graceful fallback for any connection failure (e.g. dummy link https://example.invalid)
            return HandshakeResult(
                success=False,
                status="unverified",
                provider_name=self.display_name,
                message=f"Unable to reach {capabilities_url} ({type(e).__name__}); falling back to manual config.",
                dynamic_form_schema=DEFAULT_FALLBACK_FIELDS
            )

    async def send_message(self, from_phone: str, to_phone: str, body: str,
                           media_urls: Optional[List[str]] = None,
                           status_callback_url: Optional[str] = None,
                           config: Optional[Dict[str, Any]] = None) -> NormalizedSendResult:
        cfg = config or {}
        base_url = (cfg.get("endpoint_url") or cfg.get("base_url") or os.getenv("AGGREGATOR_BASE_URL") or "http://localhost:3000").rstrip("/")

        # Check for placeholder / dummy links
        if "example.invalid" in base_url or "localhost" not in base_url and "127.0.0.1" not in base_url and not base_url.startswith("http"):
            return NormalizedSendResult(
                status="failed",
                recipient_phone=to_phone,
                error_reason=f"Cannot dispatch against unverified/dummy endpoint: {base_url}"
            )

        # The local teammate Mock Aggregator accepts Twilio-compatible messages endpoint
        # or a generic JSON send endpoint /api/phone/send
        account_sid = cfg.get("account_sid") or os.getenv("AGGREGATOR_ACCOUNT_SID", "AC_IDACT_001")
        auth_token = cfg.get("auth_token") or os.getenv("AGGREGATOR_AUTH_TOKEN", "mock_auth_token")

        url = f"{base_url}/2010-04-01/Accounts/{account_sid}/Messages.json"
        data = {
            "To": to_phone,
            "From": from_phone or cfg.get("sender_id", "+15551234560"),
            "Body": body
        }
        if status_callback_url:
            data["StatusCallback"] = status_callback_url

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(url, data=data, auth=(account_sid, auth_token))
                raw_json = res.json() if "application/json" in res.headers.get("content-type", "") else {"text": res.text}
                
                if res.status_code in (200, 201):
                    sid = raw_json.get("sid") or f"SM_{int(datetime.now(timezone.utc).timestamp()*1000)}"
                    status = raw_json.get("status", "queued")
                    return NormalizedSendResult(
                        status=status,
                        provider_message_id=sid,
                        recipient_phone=to_phone,
                        raw_provider_response=raw_json
                    )
                else:
                    return NormalizedSendResult(
                        status="failed",
                        recipient_phone=to_phone,
                        error_reason=f"Mock Aggregator HTTP {res.status_code}: {res.text[:200]}",
                        raw_provider_response=raw_json
                    )
        except Exception as e:
            return NormalizedSendResult(
                status="failed",
                recipient_phone=to_phone,
                error_reason=f"Mock Aggregator transport error: {type(e).__name__}: {str(e)}"
            )

    async def handle_inbound_webhook(self, request_headers: Dict[str, str],
                                     raw_body: bytes,
                                     query_params: Dict[str, str],
                                     config: Dict[str, Any]) -> NormalizedInboundMessage:
        # Accepts both JSON or Form-urlencoded
        body_text = raw_body.decode("utf-8", errors="replace")
        from_phone = ""
        to_phone = ""
        text_body = ""
        raw_payload = {}

        try:
            data = json.loads(body_text)
            raw_payload = data
            from_phone = data.get("from") or data.get("From") or ""
            to_phone = data.get("to") or data.get("To") or ""
            text_body = data.get("body") or data.get("text") or data.get("Body") or ""
            msg_id = data.get("message_id") or data.get("sid") or data.get("MessageSid") or f"mock_{int(datetime.now(timezone.utc).timestamp()*1000)}"
        except Exception:
            from urllib.parse import parse_qs
            parsed = parse_qs(body_text)
            raw_payload = {k: v[0] if v else "" for k, v in parsed.items()}
            from_phone = raw_payload.get("From", "")
            to_phone = raw_payload.get("To", "")
            text_body = raw_payload.get("Body", "")
            msg_id = raw_payload.get("MessageSid") or f"mock_{int(datetime.now(timezone.utc).timestamp()*1000)}"

        return NormalizedInboundMessage(
            message_id=msg_id,
            from_phone=from_phone,
            to_phone=to_phone,
            body=text_body,
            media_urls=[],
            received_at_iso=datetime.now(timezone.utc).isoformat(),
            raw_payload=raw_payload
        )

    async def parse_delivery_status(self, request_headers: Dict[str, str],
                                    raw_body: bytes,
                                    config: Dict[str, Any]) -> NormalizedDeliveryStatus:
        body_text = raw_body.decode("utf-8", errors="replace")
        raw_data = {}
        try:
            raw_data = json.loads(body_text)
        except Exception:
            from urllib.parse import parse_qs
            parsed = parse_qs(body_text)
            raw_data = {k: v[0] if v else "" for k, v in parsed.items()}

        sid = raw_data.get("MessageSid") or raw_data.get("sid") or raw_data.get("messageId") or ""
        to_p = raw_data.get("To") or raw_data.get("to") or ""
        raw_status = (raw_data.get("MessageStatus") or raw_data.get("status") or "delivered").lower()

        status_map = {
            "queued": "queued",
            "sending": "queued",
            "sent": "sent",
            "delivered": "delivered",
            "undelivered": "undelivered",
            "failed": "failed"
        }
        normalized_status = status_map.get(raw_status, "delivered")

        return NormalizedDeliveryStatus(
            provider_message_id=sid,
            recipient_phone=to_p,
            status=normalized_status,
            error_code=raw_data.get("ErrorCode"),
            error_message=raw_data.get("ErrorMessage"),
            timestamp_iso=datetime.now(timezone.utc).isoformat(),
            raw_payload=raw_data
        )
