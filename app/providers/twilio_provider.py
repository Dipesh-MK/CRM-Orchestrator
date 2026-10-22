import os
import hmac
import hashlib
import base64
from typing import Dict, Any, Optional, List, Tuple
from urllib.parse import parse_qs, urlparse
import httpx
from datetime import datetime, timezone

from app.providers.base import (
    MessagingProvider,
    HandshakeResult,
    NormalizedSendResult,
    NormalizedInboundMessage,
    NormalizedDeliveryStatus
)

def compute_twilio_signature(auth_token: str, url: str, params: Dict[str, str]) -> str:
    """
    Computes Twilio signature (RFC 2104 HMAC-SHA1):
    Takes the full URL and sorted POST parameters, appends them to URL, and hashes with auth_token.
    """
    s = url
    if params:
        for key in sorted(params.keys()):
            s += f"{key}{params[key]}"
    mac = hmac.new(auth_token.encode("utf-8"), s.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode("utf-8")

class TwilioProvider(MessagingProvider):
    @property
    def provider_id(self) -> str:
        return "twilio"

    @property
    def display_name(self) -> str:
        return "Twilio"

    def validate_config(self, config: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        sid = config.get("account_sid") or config.get("AccountSid")
        token = config.get("auth_token") or config.get("AuthToken")
        if not sid:
            return False, "Twilio requires 'account_sid'"
        if not token:
            return False, "Twilio requires 'auth_token'"
        return True, None

    async def handshake(self, endpoint_url: Optional[str], partial_config: Dict[str, Any]) -> HandshakeResult:
        sid = partial_config.get("account_sid") or partial_config.get("AccountSid") or ""
        token = partial_config.get("auth_token") or partial_config.get("AuthToken") or ""
        base_url = (endpoint_url or partial_config.get("base_url") or "https://api.twilio.com").rstrip("/")

        if not sid or not token:
            return HandshakeResult(
                success=False,
                status="unverified",
                provider_name=self.display_name,
                message="Missing credentials: both Account SID and Auth Token are required for Twilio handshake."
            )

        fetch_url = f"{base_url}/2010-04-01/Accounts/{sid}.json"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(fetch_url, auth=(sid, token))
                if res.status_code == 200:
                    data = res.json()
                    status_name = data.get("status", "active")
                    friendly_name = data.get("friendly_name", sid)
                    return HandshakeResult(
                        success=True,
                        status="verified",
                        provider_name=self.display_name,
                        message=f"Twilio account '{friendly_name}' ({status_name}) verified successfully.",
                        capabilities={"sms": True, "mms": True, "delivery_receipts": True},
                        raw_response=data
                    )
                elif res.status_code == 401:
                    return HandshakeResult(
                        success=False,
                        status="unverified",
                        provider_name=self.display_name,
                        message="Twilio authentication failed: Account SID or Auth Token is invalid."
                    )
                elif res.status_code == 404:
                    return HandshakeResult(
                        success=False,
                        status="unverified",
                        provider_name=self.display_name,
                        message=f"Twilio Account SID '{sid}' not found at {base_url}."
                    )
                else:
                    return HandshakeResult(
                        success=False,
                        status="unverified",
                        provider_name=self.display_name,
                        message=f"Twilio handshake returned unexpected HTTP {res.status_code}: {res.text[:200]}"
                    )
        except httpx.TimeoutException:
            return HandshakeResult(
                success=False,
                status="unverified",
                provider_name=self.display_name,
                message=f"Connection timed out after 5.0s contacting Twilio endpoint at {base_url}."
            )
        except Exception as e:
            return HandshakeResult(
                success=False,
                status="unverified",
                provider_name=self.display_name,
                message=f"Failed to reach Twilio endpoint: {type(e).__name__}: {str(e)}"
            )

    async def send_message(self, from_phone: str, to_phone: str, body: str,
                           media_urls: Optional[List[str]] = None,
                           status_callback_url: Optional[str] = None,
                           config: Optional[Dict[str, Any]] = None) -> NormalizedSendResult:
        cfg = config or {}
        sid = cfg.get("account_sid") or os.getenv("TWILIO_ACCOUNT_SID", "")
        token = cfg.get("auth_token") or os.getenv("TWILIO_AUTH_TOKEN", "")
        base_url = (cfg.get("endpoint_url") or cfg.get("base_url") or "https://api.twilio.com").rstrip("/")

        if not sid or not token:
            return NormalizedSendResult(
                status="failed",
                recipient_phone=to_phone,
                error_reason="Missing Twilio credentials (account_sid or auth_token)"
            )

        url = f"{base_url}/2010-04-01/Accounts/{sid}/Messages.json"
        data: Dict[str, Any] = {
            "To": to_phone,
            "From": from_phone,
            "Body": body
        }
        if status_callback_url:
            data["StatusCallback"] = status_callback_url
        if media_urls:
            for i, m_url in enumerate(media_urls):
                data[f"MediaUrl[{i}]"] = m_url

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(url, data=data, auth=(sid, token))
                raw_json = res.json() if "application/json" in res.headers.get("content-type", "") else {"text": res.text}
                if res.status_code in (200, 201):
                    msg_sid = raw_json.get("sid")
                    status = raw_json.get("status", "queued")
                    return NormalizedSendResult(
                        status=status,
                        provider_message_id=msg_sid,
                        recipient_phone=to_phone,
                        raw_provider_response=raw_json
                    )
                else:
                    err_msg = raw_json.get("message") or f"HTTP {res.status_code}: {res.text}"
                    return NormalizedSendResult(
                        status="failed",
                        recipient_phone=to_phone,
                        error_reason=f"Twilio API error: {err_msg}",
                        raw_provider_response=raw_json
                    )
        except Exception as e:
            return NormalizedSendResult(
                status="failed",
                recipient_phone=to_phone,
                error_reason=f"Twilio transport exception: {type(e).__name__}: {str(e)}"
            )

    async def handle_inbound_webhook(self, request_headers: Dict[str, str],
                                     raw_body: bytes,
                                     query_params: Dict[str, str],
                                     config: Dict[str, Any]) -> NormalizedInboundMessage:
        # Parse application/x-www-form-urlencoded body
        body_text = raw_body.decode("utf-8", errors="replace")
        parsed = parse_qs(body_text)
        form_data = {k: v[0] if v else "" for k, v in parsed.items()}

        # Verify signature if configured
        auth_token = config.get("auth_token") or os.getenv("TWILIO_AUTH_TOKEN")
        expected_sig = request_headers.get("x-twilio-signature") or request_headers.get("X-Twilio-Signature")
        webhook_url = config.get("webhook_url") or request_headers.get("x-forwarded-proto", "http") + "://" + request_headers.get("host", "localhost") + "/api/webhooks/twilio/inbound"

        if auth_token and expected_sig and config.get("verify_signature", True):
            computed = compute_twilio_signature(auth_token, webhook_url, form_data)
            if not hmac.compare_digest(computed, expected_sig):
                raise ValueError("Twilio webhook signature verification failed.")

        msg_sid = form_data.get("MessageSid") or form_data.get("SmsSid") or f"tw_{int(datetime.now(timezone.utc).timestamp())}"
        from_phone = form_data.get("From", "")
        to_phone = form_data.get("To", "")
        text_body = form_data.get("Body", "")

        # Extract MediaUrls if any
        media_urls = []
        num_media = int(form_data.get("NumMedia", "0") or 0)
        for i in range(num_media):
            m = form_data.get(f"MediaUrl{i}")
            if m:
                media_urls.append(m)

        return NormalizedInboundMessage(
            message_id=msg_sid,
            from_phone=from_phone,
            to_phone=to_phone,
            body=text_body,
            media_urls=media_urls,
            received_at_iso=datetime.now(timezone.utc).isoformat(),
            raw_payload=form_data
        )

    async def parse_delivery_status(self, request_headers: Dict[str, str],
                                    raw_body: bytes,
                                    config: Dict[str, Any]) -> NormalizedDeliveryStatus:
        body_text = raw_body.decode("utf-8", errors="replace")
        parsed = parse_qs(body_text)
        form_data = {k: v[0] if v else "" for k, v in parsed.items()}

        msg_sid = form_data.get("MessageSid") or form_data.get("SmsSid") or ""
        to_phone = form_data.get("To") or ""
        raw_status = (form_data.get("MessageStatus") or form_data.get("SmsStatus") or "unknown").lower()

        status_map = {
            "queued": "queued",
            "sending": "queued",
            "sent": "sent",
            "delivered": "delivered",
            "undelivered": "undelivered",
            "failed": "failed"
        }
        normalized_status = status_map.get(raw_status, "unknown")

        return NormalizedDeliveryStatus(
            provider_message_id=msg_sid,
            recipient_phone=to_phone,
            status=normalized_status,
            error_code=form_data.get("ErrorCode"),
            error_message=form_data.get("ErrorMessage"),
            timestamp_iso=datetime.now(timezone.utc).isoformat(),
            raw_payload=form_data
        )
