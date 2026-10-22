import pytest
import json
import httpx
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

from app.main import app
from app.core.storage import get_storage
from app.providers.base import (
    HandshakeResult,
    NormalizedSendResult,
    NormalizedInboundMessage,
    NormalizedDeliveryStatus,
    sanitize_discovery_schema
)
from app.providers.twilio_provider import TwilioProvider, compute_twilio_signature
from app.providers.infobip_provider import InfobipProvider
from app.providers.mock_provider import MockAggregatorProvider
from app.providers.registry import ProviderRegistry, get_provider_registry

client = TestClient(app)

# =============================================================================
# 1. TwilioProvider Tests
# =============================================================================

@pytest.mark.asyncio
async def test_twilio_handshake_valid_credentials():
    provider = TwilioProvider()
    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {"status": "active", "friendly_name": "Test Account"}

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        res = await provider.handshake(
            endpoint_url="https://api.twilio.com",
            partial_config={"account_sid": "AC_VALID_SID", "auth_token": "VALID_TOKEN"}
        )
        assert res.success is True
        assert res.status == "verified"
        assert "Test Account" in res.message
        assert res.capabilities["sms"] is True

@pytest.mark.asyncio
async def test_twilio_handshake_invalid_credentials():
    provider = TwilioProvider()
    mock_response = MagicMock(status_code=401, text="Unauthorized")

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        res = await provider.handshake(
            endpoint_url="https://api.twilio.com",
            partial_config={"account_sid": "AC_BAD_SID", "auth_token": "BAD_TOKEN"}
        )
        assert res.success is False
        assert res.status == "unverified"
        assert "Account SID or Auth Token is invalid" in res.message

@pytest.mark.asyncio
async def test_twilio_send_and_inbound_signature():
    provider = TwilioProvider()
    
    # Test send
    mock_post_resp = MagicMock(status_code=201)
    mock_post_resp.headers = {"content-type": "application/json"}
    mock_post_resp.json.return_value = {"sid": "SM_12345", "status": "queued"}

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_post_resp
        send_res = await provider.send_message(
            from_phone="+15551234560",
            to_phone="+15551234561",
            body="Hello from Twilio",
            config={"account_sid": "AC_1", "auth_token": "token_1"}
        )
        assert send_res.status == "queued"
        assert send_res.provider_message_id == "SM_12345"

    # Test inbound webhook parsing & signature verification
    auth_token = "secret_token_123"
    webhook_url = "http://testserver/api/webhooks/twilio/inbound"
    form_params = {"From": "+15551234561", "To": "+15551234560", "Body": "Inbound reply", "MessageSid": "SM_IN_99"}
    valid_sig = compute_twilio_signature(auth_token, webhook_url, form_params)

    from urllib.parse import urlencode
    raw_body = urlencode(form_params).encode("utf-8")

    # Valid signature
    inbound_res = await provider.handle_inbound_webhook(
        request_headers={"x-twilio-signature": valid_sig},
        raw_body=raw_body,
        query_params={},
        config={"auth_token": auth_token, "webhook_url": webhook_url, "verify_signature": True}
    )
    assert inbound_res.body == "Inbound reply"
    assert inbound_res.from_phone == "+15551234561"

    # Invalid signature -> raises ValueError
    with pytest.raises(ValueError, match="signature verification failed"):
        await provider.handle_inbound_webhook(
            request_headers={"x-twilio-signature": "bogus_signature"},
            raw_body=raw_body,
            query_params={},
            config={"auth_token": auth_token, "webhook_url": webhook_url, "verify_signature": True}
        )

# =============================================================================
# 2. InfobipProvider Tests
# =============================================================================

@pytest.mark.asyncio
async def test_infobip_handshake_balance_check():
    provider = InfobipProvider()
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {"balance": 150.75, "currency": "EUR"}

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        res = await provider.handshake(
            endpoint_url="https://test.api.infobip.com",
            partial_config={"api_key": "App test_key_123"}
        )
        assert res.success is True
        assert res.status == "verified"
        assert "150.75 EUR" in res.message

@pytest.mark.asyncio
async def test_infobip_handshake_auth_failure():
    provider = InfobipProvider()
    mock_resp = MagicMock(status_code=401, text="Unauthorized")

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        res = await provider.handshake(
            endpoint_url="https://test.api.infobip.com",
            partial_config={"api_key": "App bad_key"}
        )
        assert res.success is False
        assert res.status == "unverified"
        assert "Invalid API key" in res.message

@pytest.mark.asyncio
async def test_infobip_send_and_inbound():
    provider = InfobipProvider()
    mock_resp = MagicMock(status_code=200)
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.json.return_value = {
        "messages": [{"messageId": "ib_msg_100", "status": {"groupName": "PENDING"}}]
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        send_res = await provider.send_message(
            from_phone="+15551234560",
            to_phone="+15551234562",
            body="Infobip test notification",
            config={"api_key": "App test_key", "base_url": "https://test.api.infobip.com"}
        )
        assert send_res.status == "queued"
        assert send_res.provider_message_id == "ib_msg_100"

    # Test inbound webhook
    inbound_payload = {
        "results": [
            {
                "messageId": "ib_in_500",
                "from": "+15551234562",
                "to": "+15551234560",
                "cleanText": "YES confirm",
                "receivedAt": "2026-10-22T12:00:00Z"
            }
        ]
    }
    raw_bytes = json.dumps(inbound_payload).encode("utf-8")
    inbound_res = await provider.handle_inbound_webhook(
        request_headers={},
        raw_body=raw_bytes,
        query_params={},
        config={}
    )
    assert inbound_res.body == "YES confirm"
    assert inbound_res.from_phone == "+15551234562"
    assert inbound_res.message_id == "ib_in_500"

# =============================================================================
# 3. MockAggregatorProvider & Discovery Contract Tests (Section 2)
# =============================================================================

@pytest.mark.asyncio
async def test_mock_discovery_valid_schema():
    provider = MockAggregatorProvider()
    valid_capabilities = {
        "provider_name": "mock-aggregator",
        "auth": {"type": "api_key", "header": "X-API-Key"},
        "config_fields": [
            {"key": "api_key", "label": "API Key", "type": "string", "required": True, "secret": True},
            {"key": "sender_id", "label": "Sender ID", "type": "string", "required": True},
            {"key": "webhook_secret", "label": "Webhook Secret", "type": "string", "required": False, "secret": True}
        ],
        "capabilities": {"sms": True, "mms": True, "delivery_receipts": True}
    }
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = valid_capabilities

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        res = await provider.handshake(
            endpoint_url="http://localhost:3000",
            partial_config={}
        )
        assert res.success is True
        assert res.status == "verified"
        assert len(res.dynamic_form_schema) == 3
        assert res.dynamic_form_schema[0]["key"] == "api_key"
        assert res.capabilities["mms"] is True

@pytest.mark.asyncio
async def test_mock_discovery_dummy_unreachable_endpoint_graceful_fallback():
    """Explicit requirement Section 2: Dummy/unreachable URL -> handshake fails cleanly, falls back to manual form, no crash."""
    provider = MockAggregatorProvider()

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = httpx.ConnectError("Failed to establish connection")
        res = await provider.handshake(
            endpoint_url="https://example.invalid",
            partial_config={}
        )
        assert res.success is False
        assert res.status == "unverified"
        assert "falling back to manual config" in res.message
        assert res.dynamic_form_schema is not None
        assert len(res.dynamic_form_schema) >= 1

@pytest.mark.asyncio
async def test_mock_discovery_malformed_response_graceful_fallback():
    """Explicit requirement Section 2: Mock aggregator serving a malformed response -> treated as failed handshake (graceful fallback), not a crash."""
    provider = MockAggregatorProvider()
    mock_resp = MagicMock(status_code=200)
    # Returning invalid JSON or non-conforming dict
    mock_resp.json.return_value = {"random_key": "not_capabilities"}

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        res = await provider.handshake(
            endpoint_url="http://localhost:3000",
            partial_config={}
        )
        assert res.success is False
        assert res.status == "unverified"
        assert "non-conforming" in res.message
        assert res.dynamic_form_schema is not None

# =============================================================================
# 4. Aggregator Switching & Cross-Contamination Test (Section 1 & 5)
# =============================================================================

def test_aggregator_switching_end_to_end():
    """
    Explicit requirement: Reconfiguring a phone number's aggregator changes routing
    with zero cross-contamination of other numbers or state.
    """
    storage = get_storage()
    test_phone_a = "+15559000001"
    test_phone_b = "+15559000002"

    # Step 1: Configure phone A with Mock Aggregator
    res_a1 = client.post("/api/phones/configure", json={
        "phone": test_phone_a,
        "provider_id": "mock",
        "endpoint_url": "http://localhost:3000",
        "config": {"sender_id": "55512"},
        "status": "verified"
    })
    assert res_a1.status_code == 200

    # Step 2: Configure phone B with Twilio
    res_b1 = client.post("/api/phones/configure", json={
        "phone": test_phone_b,
        "provider_id": "twilio",
        "endpoint_url": "https://api.twilio.com",
        "config": {"account_sid": "AC_PHONE_B", "auth_token": "TOKEN_B"},
        "status": "verified"
    })
    assert res_b1.status_code == 200

    # Verify initial mappings
    cfg_a = storage.get_phone_configuration(test_phone_a)
    cfg_b = storage.get_phone_configuration(test_phone_b)
    assert cfg_a["provider_id"] == "mock"
    assert cfg_b["provider_id"] == "twilio"
    assert cfg_b["config"]["account_sid"] == "AC_PHONE_B"

    # Step 3: Switch phone A from Mock to Infobip
    res_a2 = client.post("/api/phones/configure", json={
        "phone": test_phone_a,
        "provider_id": "infobip",
        "endpoint_url": "https://infobip.domain.com",
        "config": {"api_key": "App KEY_A", "base_url": "https://infobip.domain.com"},
        "status": "verified"
    })
    assert res_a2.status_code == 200

    # Verify Phone A updated to Infobip, but Phone B remains untouched (no cross-contamination!)
    updated_cfg_a = storage.get_phone_configuration(test_phone_a)
    untouched_cfg_b = storage.get_phone_configuration(test_phone_b)
    assert updated_cfg_a["provider_id"] == "infobip"
    assert updated_cfg_a["config"]["api_key"] == "App KEY_A"
    assert untouched_cfg_b["provider_id"] == "twilio"
    assert untouched_cfg_b["config"]["account_sid"] == "AC_PHONE_B"

    # Clean up test numbers
    client.delete(f"/api/phones/{test_phone_a}")
    client.delete(f"/api/phones/{test_phone_b}")

# =============================================================================
# 5. Inbound Webhook Routing & Contact Attachment (Section 6)
# =============================================================================

def test_inbound_webhook_unrecognized_number_creates_contact():
    """
    Explicit requirement Section 6: Every inbound message, from either provider,
    must land in the correct CRM conversation thread / contact record,
    and a message from an unrecognized number still creates/attaches to the right record
    rather than getting silently dropped or misrouted.
    """
    storage = get_storage()
    unrecognized_phone = "+15557778899"
    receiving_phone = "+15551234560"

    # Ensure phone not present initially in contacts
    with storage.get_connection() as conn:
        conn.cursor().execute("DELETE FROM contacts WHERE phone = ?", (unrecognized_phone,))
        conn.commit()

    inbound_payload = {
        "From": unrecognized_phone,
        "To": receiving_phone,
        "Body": "Hello, I want to inquire about enrollment",
        "MessageSid": "SM_UNRECOGNIZED_01"
    }

    # Simulate inbound POST from provider webhook route
    res = client.post("/api/webhooks/mock/inbound", json=inbound_payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["contact_id"] is not None

    # Verify contact was created in CRM database
    with storage.get_connection() as conn:
        contact_row = conn.cursor().execute("SELECT * FROM contacts WHERE phone = ?", (unrecognized_phone,)).fetchone()
        assert contact_row is not None
        assert contact_row["phone"] == unrecognized_phone

    # Verify message was appended to receiving phone's thread
    msgs = storage.get_phone_messages(receiving_phone)
    matched = [m for m in msgs if m["body"] == "Hello, I want to inquire about enrollment"]
    assert len(matched) == 1
    assert matched[0]["direction"] == "inbound"

def test_delivery_status_callback_updates_database():
    storage = get_storage()
    sid = "SM_TEST_DLR_123"
    storage.save_phone_message(
        msg_id="msg_dlr_1",
        phone="+15551234561",
        direction="outbound",
        body="Testing DLR callback",
        status="queued",
        provider_id="mock",
        provider_message_id=sid
    )

    # Post delivery report callback
    callback_payload = {
        "MessageSid": sid,
        "MessageStatus": "delivered",
        "To": "+15551234561"
    }
    res = client.post("/api/webhooks/mock/status", json=callback_payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["delivery_status"] == "delivered"

    # Verify status in database
    msgs = storage.get_phone_messages("+15551234561")
    target_msg = [m for m in msgs if m["provider_message_id"] == sid][0]
    assert target_msg["status"] == "delivered"

# =============================================================================
# 6. Retry Handshake Flow (Section 2)
# =============================================================================

@pytest.mark.asyncio
async def test_retry_handshake_flow():
    storage = get_storage()
    test_phone = "+15559990001"

    # Save phone initially as unverified (dummy link)
    client.post("/api/phones/configure", json={
        "phone": test_phone,
        "provider_id": "mock",
        "endpoint_url": "https://dummy-link.invalid",
        "config": {"sender_id": "55512"},
        "status": "unverified"
    })

    cfg = storage.get_phone_configuration(test_phone)
    assert cfg["status"] == "unverified"

    # Now simulate successful retry handshake when endpoint comes online
    valid_caps = {
        "provider_name": "mock-aggregator",
        "config_fields": [{"key": "sender_id", "label": "Sender", "type": "string"}],
        "capabilities": {"sms": True}
    }
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = valid_caps

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        retry_res = client.post(f"/api/phones/{test_phone}/retry-handshake")
        assert retry_res.status_code == 200
        data = retry_res.json()
        assert data["handshake_result"]["success"] is True
        assert data["phone_config"]["status"] == "verified"

    client.delete(f"/api/phones/{test_phone}")
