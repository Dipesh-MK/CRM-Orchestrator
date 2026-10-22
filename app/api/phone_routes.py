import uuid
from typing import Dict, Any, Optional, List
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

from app.core.storage import get_storage, Storage
from app.providers.registry import get_provider_registry, ProviderRegistry
from app.providers.base import HandshakeResult

router = APIRouter(prefix="/api/phones", tags=["phones"])

class HandshakeRequest(BaseModel):
    provider_id: str
    endpoint_url: Optional[str] = None
    partial_config: Dict[str, Any] = Field(default_factory=dict)

class ConfigurePhoneRequest(BaseModel):
    phone: str
    provider_id: str
    endpoint_url: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    status: Optional[str] = "unverified"
    capabilities: Optional[Dict[str, bool]] = None

class SendTextMessageRequest(BaseModel):
    to_phone: str
    body: str
    media_urls: Optional[List[str]] = None

@router.get("")
def list_configured_phones(storage: Storage = Depends(get_storage)):
    """Lists all configured phone slots."""
    return storage.list_phone_configurations()

@router.get("/providers")
def list_available_providers(registry: ProviderRegistry = Depends(get_provider_registry)):
    """Lists all active messaging providers available for phone assignment."""
    return registry.list_providers()

@router.post("/handshake")
async def execute_provider_handshake(req: HandshakeRequest,
                                     registry: ProviderRegistry = Depends(get_provider_registry)):
    """
    Executes live handshake or capability discovery against the selected provider and endpoint.
    Guaranteed to fail gracefully without crashing on dummy/unreachable endpoints.
    """
    try:
        provider = registry.get_provider(req.provider_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    res = await provider.handshake(req.endpoint_url, req.partial_config)
    return res.model_dump()

@router.post("/configure")
async def save_phone_configuration(req: ConfigurePhoneRequest,
                                   storage: Storage = Depends(get_storage),
                                   registry: ProviderRegistry = Depends(get_provider_registry)):
    """
    Saves or updates phone-to-aggregator association.
    Accepts both verified and explicitly unverified configurations (e.g. against dummy endpoints).
    """
    phone = req.phone.strip()
    if not phone.startswith("+") or len(phone) < 8:
        raise HTTPException(status_code=400, detail="Phone number must be in E.164 format (+1234567890).")

    try:
        provider = registry.get_provider(req.provider_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    saved = storage.save_phone_configuration(
        phone=phone,
        provider_id=req.provider_id,
        endpoint_url=req.endpoint_url,
        config=req.config,
        status=req.status or "unverified",
        capabilities=req.capabilities
    )
    return {"status": "success", "phone_config": saved}

@router.post("/{phone}/retry-handshake")
async def retry_phone_handshake(phone: str,
                                storage: Storage = Depends(get_storage),
                                registry: ProviderRegistry = Depends(get_provider_registry)):
    """
    Retries handshake for a phone previously saved in 'unverified' state.
    """
    cfg = storage.get_phone_configuration(phone)
    if not cfg:
        raise HTTPException(status_code=404, detail=f"Phone '{phone}' is not configured.")

    provider = registry.get_provider(cfg["provider_id"])
    res = await provider.handshake(cfg.get("endpoint_url"), cfg.get("config", {}))
    new_status = "verified" if res.success else "unverified"
    
    updated = storage.save_phone_configuration(
        phone=phone,
        provider_id=cfg["provider_id"],
        endpoint_url=cfg.get("endpoint_url"),
        config=cfg.get("config", {}),
        status=new_status,
        capabilities=res.capabilities if res.success else cfg.get("capabilities")
    )
    return {
        "handshake_result": res.model_dump(),
        "phone_config": updated
    }

@router.delete("/{phone}")
def delete_phone_configuration(phone: str, storage: Storage = Depends(get_storage)):
    """Deletes a phone slot and its message history."""
    deleted = storage.delete_phone_configuration(phone)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Phone '{phone}' not found.")
    return {"status": "deleted", "phone": phone}

@router.get("/{phone}/messages")
def get_phone_conversation(phone: str, storage: Storage = Depends(get_storage)):
    """Retrieves conversation message history for a phone number."""
    msgs = storage.get_phone_messages(phone)
    return {"phone": phone, "count": len(msgs), "messages": msgs}

@router.post("/{phone}/send")
async def send_message_from_phone(phone: str,
                                  req: SendTextMessageRequest,
                                  storage: Storage = Depends(get_storage),
                                  registry: ProviderRegistry = Depends(get_provider_registry)):
    """
    Sends an outbound SMS/MMS from the configured phone number via its assigned aggregator.
    """
    provider, config_data = registry.get_provider_for_phone(phone, storage=storage)
    msg_id = f"msg_{uuid.uuid4().hex[:12]}"

    res = await provider.send_message(
        from_phone=phone,
        to_phone=req.to_phone,
        body=req.body,
        media_urls=req.media_urls,
        config=config_data
    )

    # Save to phone message history
    saved_msg = storage.save_phone_message(
        msg_id=msg_id,
        phone=phone,
        direction="outbound",
        body=req.body,
        media_urls=req.media_urls,
        status=res.status,
        provider_id=provider.provider_id,
        provider_message_id=res.provider_message_id,
        error=res.error_reason
    )

    return {
        "status": "success" if res.status in ("queued", "sent", "delivered") else "failed",
        "result": res.dict(),
        "message": saved_msg
    }
