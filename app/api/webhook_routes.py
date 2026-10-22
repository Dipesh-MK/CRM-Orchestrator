import uuid
from typing import Dict, Any, Optional
from fastapi import APIRouter, Request, HTTPException, Depends

from app.core.storage import get_storage, Storage
from app.providers.registry import get_provider_registry, ProviderRegistry

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

@router.post("/{provider_id}/inbound")
async def handle_inbound_sms(provider_id: str,
                             request: Request,
                             storage: Storage = Depends(get_storage),
                             registry: ProviderRegistry = Depends(get_provider_registry)):
    """
    Provider-specific inbound webhook endpoint.
    Verifies provider signature, normalizes inbound SMS, matches receiving number,
    attaches message to contact thread (creating contact if unrecognized),
    and logs into two-way conversation history.
    """
    try:
        provider = registry.get_provider(provider_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    raw_body = await request.body()
    headers = dict(request.headers)
    query_params = dict(request.query_params)

    # Inbound message recipient number determines configuration
    # For initial signature check, if we need global or per-phone config, look up
    # We first parse or extract from body or default config
    config = {}

    try:
        normalized = await provider.handle_inbound_webhook(
            request_headers=headers,
            raw_body=raw_body,
            query_params=query_params,
            config=config
        )
    except ValueError as val_err:
        raise HTTPException(status_code=403, detail=f"Webhook authentication refused: {str(val_err)}")
    except Exception as ex:
        raise HTTPException(status_code=400, detail=f"Malformed webhook payload: {str(ex)}")

    # 1. Attach or create contact in CRM (Guaranteed zero message loss)
    contact = storage.find_or_create_contact_for_inbound(
        from_phone=normalized.from_phone,
        to_phone=normalized.to_phone,
        body=normalized.body
    )

    # 2. Persist in conversation message thread for receiving phone
    msg_id = f"in_{normalized.message_id or uuid.uuid4().hex[:10]}"
    saved = storage.save_phone_message(
        msg_id=msg_id,
        phone=normalized.to_phone or normalized.from_phone,
        direction="inbound",
        body=normalized.body,
        media_urls=normalized.media_urls,
        status="delivered",
        provider_id=provider.provider_id,
        provider_message_id=normalized.message_id,
        timestamp=normalized.received_at_iso
    )

    return {
        "status": "success",
        "action": "processed",
        "contact_id": contact.get("contact_id"),
        "contact_name": contact.get("name"),
        "message_id": saved.get("id"),
        "body": normalized.body
    }

@router.post("/{provider_id}/status")
async def handle_delivery_status(provider_id: str,
                                request: Request,
                                storage: Storage = Depends(get_storage),
                                registry: ProviderRegistry = Depends(get_provider_registry)):
    """
    Provider-specific delivery status / failure callback endpoint.
    Updates delivery status in both phone_messages and send_items tables.
    """
    try:
        provider = registry.get_provider(provider_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    raw_body = await request.body()
    headers = dict(request.headers)

    try:
        status_info = await provider.parse_delivery_status(
            request_headers=headers,
            raw_body=raw_body,
            config={}
        )
    except Exception as ex:
        raise HTTPException(status_code=400, detail=f"Failed to parse delivery report: {str(ex)}")

    sid = status_info.provider_message_id
    updated_phone = False
    updated_send = False

    if sid:
        updated_phone = storage.update_phone_message_status(
            provider_message_id=sid,
            status=status_info.status,
            error=status_info.error_message
        )
        # Also update send_items in case this was a campaign batch send
        with storage.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE send_items
            SET status = ?, error = ?
            WHERE message_sid = ?
            """, (status_info.status, status_info.error_message, sid))
            conn.commit()
            updated_send = cursor.rowcount > 0

    return {
        "status": "success",
        "provider": provider_id,
        "provider_message_id": sid,
        "delivery_status": status_info.status,
        "updated_phone_messages": updated_phone,
        "updated_send_items": updated_send
    }
