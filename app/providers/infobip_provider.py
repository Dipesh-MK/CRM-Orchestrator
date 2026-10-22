import os
import json
from typing import Dict, Any, Optional, List, Tuple
from urllib.parse import urlparse
import httpx
from datetime import datetime, timezone

from app.providers.base import (
    MessagingProvider,
    HandshakeResult,
    NormalizedSendResult,
    NormalizedInboundMessage,
    NormalizedDeliveryStatus
)

class InfobipProvider(MessagingProvider):
    @property
    def provider_id(self) -> str:
        return "infobip"

    @property
    def display_name(self) -> str:
        return "Infobip"

    def validate_config(self, config: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        key = config.get("api_key") or config.get("apiKey")
        url = config.get("base_url") or config.get("endpoint_url")
        if not key:
            return False, "Infobip requires 'api_key'"
        if not url:
            return False, "Infobip requires 'base_url' or 'endpoint_url'"
        return True, None

    async def handshake(self, endpoint_url: Optional[str], partial_config: Dict[str, Any]) -> HandshakeResult:
        api_key = partial_config.get("api_key") or partial_config.get("apiKey") or ""
        base_url = (endpoint_url or partial_config.get("base_url") or partial_config.get("endpoint_url") or "").rstrip("/")

        if not base_url:
            return HandshakeResult(
                success=False,
                status="unverified",
                provider_name=self.display_name,
                message="Missing base URL: Infobip requires a dedicated customer base URL (e.g. https://xxxxx.api.infobip.com)."
            )

        if not api_key:
            return HandshakeResult(
                success=False,
                status="unverified",
                provider_name=self.display_name,
                message="Missing credentials: API key is required for Infobip handshake."
            )

        check_url = f"{base_url}/account/1/balance"
        headers = {
            "Authorization": f"App {api_key}",
            "Accept": "application/json"
        }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(check_url, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    balance = data.get("balance", "N/A")
                    currency = data.get("currency", "")
                    return HandshakeResult(
                        success=True,
                        status="verified",
                        provider_name=self.display_name,
                        message=f"Infobip account verified successfully. Available balance: {balance} {currency}".strip(),
                        capabilities={"sms": True, "mms": True, "delivery_receipts": True},
                        raw_response=data
                    )
                elif res.status_code in (401, 403):
                    return HandshakeResult(
                        success=False,
                        status="unverified",
                        provider_name=self.display_name,
                        message="Infobip authentication failed: Invalid API key or unauthorized."
                    )
                else:
                    return HandshakeResult(
                        success=False,
                        status="unverified",
                        provider_name=self.display_name,
                        message=f"Infobip handshake returned HTTP {res.status_code}: {res.text[:200]}"
                    )
        except httpx.TimeoutException:
            return HandshakeResult(
                success=False,
                status="unverified",
                provider_name=self.display_name,
                message=f"Connection timed out after 5.0s contacting Infobip endpoint at {base_url}."
            )
        except Exception as e:
            return HandshakeResult(
                success=False,
                status="unverified",
                provider_name=self.display_name,
                message=f"Failed to reach Infobip endpoint: {type(e).__name__}: {str(e)}"
            )

    async def send_message(self, from_phone: str, to_phone: str, body: str,
                           media_urls: Optional[List[str]] = None,
                           status_callback_url: Optional[str] = None,
                           config: Optional[Dict[str, Any]] = None) -> NormalizedSendResult:
        cfg = config or {}
        api_key = cfg.get("api_key") or os.getenv("INFOBIP_API_KEY", "")
        base_url = (cfg.get("endpoint_url") or cfg.get("base_url") or os.getenv("INFOBIP_BASE_URL", "")).rstrip("/")

        if not api_key or not base_url:
            return NormalizedSendResult(
                status="failed",
                recipient_phone=to_phone,
                error_reason="Missing Infobip configuration: api_key and base_url are required."
            )

        send_url = f"{base_url}/sms/2/text/advanced"
        headers = {
            "Authorization": f"App {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        msg_payload: Dict[str, Any] = {
            "destinations": [{"to": to_phone}],
            "from": from_phone,
            "text": body
        }
        if status_callback_url:
            msg_payload["notifyUrl"] = status_callback_url

        payload = {"messages": [msg_payload]}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(send_url, json=payload, headers=headers)
                raw_json = res.json() if "application/json" in res.headers.get("content-type", "") else {"text": res.text}
                
                if res.status_code in (200, 201):
                    messages = raw_json.get("messages", [])
                    if messages:
                        first = messages[0]
                        msg_id = first.get("messageId")
                        status_obj = first.get("status", {})
                        group_name = status_obj.get("groupName", "PENDING").upper()
                        status = "queued" if group_name in ("PENDING", "ACCEPTED") else "sent"
                        return NormalizedSendResult(
                            status=status,
                            provider_message_id=msg_id,
                            recipient_phone=to_phone,
                            raw_provider_response=raw_json
                        )
                    return NormalizedSendResult(
                        status="queued",
                        recipient_phone=to_phone,
                        raw_provider_response=raw_json
                    )
                else:
                    err_text = raw_json.get("requestError", {}).get("serviceException", {}).get("text") or res.text
                    return NormalizedSendResult(
                        status="failed",
                        recipient_phone=to_phone,
                        error_reason=f"Infobip API error (HTTP {res.status_code}): {err_text}",
                        raw_provider_response=raw_json
                    )
        except Exception as e:
            return NormalizedSendResult(
                status="failed",
                recipient_phone=to_phone,
                error_reason=f"Infobip transport exception: {type(e).__name__}: {str(e)}"
            )

    async def handle_inbound_webhook(self, request_headers: Dict[str, str],
                                     raw_body: bytes,
                                     query_params: Dict[str, str],
                                     config: Dict[str, Any]) -> NormalizedInboundMessage:
        # Check authorization token header if secret configured
        expected_secret = config.get("webhook_secret")
        if expected_secret:
            auth_header = request_headers.get("authorization") or request_headers.get("Authorization") or ""
            api_key_header = request_headers.get("x-api-key") or request_headers.get("X-API-Key") or ""
            if expected_secret not in auth_header and expected_secret != api_key_header:
                raise ValueError("Infobip webhook authorization verification failed.")

        try:
            data = json.loads(raw_body.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"Invalid JSON payload from Infobip: {e}")

        # Infobip sends {"results": [{"messageId": "...", "from": "...", "to": "...", "text": "...", ...}]}
        results = data.get("results") if isinstance(data, dict) else None
        item = results[0] if (isinstance(results, list) and results) else (data if isinstance(data, dict) else {})

        msg_id = item.get("messageId") or f"ib_{int(datetime.now(timezone.utc).timestamp())}"
        from_phone = item.get("from") or ""
        to_phone = item.get("to") or ""
        text_body = item.get("cleanText") or item.get("text") or ""

        return NormalizedInboundMessage(
            message_id=msg_id,
            from_phone=from_phone,
            to_phone=to_phone,
            body=text_body,
            media_urls=[],
            received_at_iso=item.get("receivedAt") or datetime.now(timezone.utc).isoformat(),
            raw_payload=data
        )

    async def parse_delivery_status(self, request_headers: Dict[str, str],
                                    raw_body: bytes,
                                    config: Dict[str, Any]) -> NormalizedDeliveryStatus:
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"Invalid DLR JSON payload: {e}")

        results = data.get("results") if isinstance(data, dict) else None
        item = results[0] if (isinstance(results, list) and results) else (data if isinstance(data, dict) else {})

        msg_id = item.get("messageId") or ""
        to_phone = item.get("to") or ""
        status_info = item.get("status") or {}
        group_name = (status_info.get("groupName") or "").upper()
        error_info = item.get("error") or {}

        status_map = {
            "DELIVERED": "delivered",
            "PENDING": "queued",
            "UNDELIVERABLE": "undelivered",
            "REJECTED": "failed",
            "EXPIRED": "failed"
        }
        normalized_status = status_map.get(group_name, "unknown")

        return NormalizedDeliveryStatus(
            provider_message_id=msg_id,
            recipient_phone=to_phone,
            status=normalized_status,
            error_code=str(error_info.get("id") or error_info.get("groupId") or ""),
            error_message=error_info.get("description") or status_info.get("description"),
            timestamp_iso=item.get("doneAt") or datetime.now(timezone.utc).isoformat(),
            raw_payload=data
        )
