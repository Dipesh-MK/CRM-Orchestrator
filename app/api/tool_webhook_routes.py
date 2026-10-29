import uuid
import httpx
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, HTTPException, Depends, Request, Header
from pydantic import BaseModel, HttpUrl

from app.core.storage import Storage, get_storage, utc_now_iso
from app.api.dispatch_routes import verify_loopback_headers

router = APIRouter(prefix="/api/tools/webhooks", tags=["Tool Webhooks"])

class ToolWebhookCreateRequest(BaseModel):
    id: Optional[str] = None
    name: str
    url: str
    headers: Optional[Dict[str, str]] = None
    enabled: Optional[bool] = True

class TestWebhookResponse(BaseModel):
    status: str
    status_code: Optional[int] = None
    message: str

@router.get("")
def list_tool_webhooks(storage: Storage = Depends(get_storage)):
    """Lists all configured tool webhooks saved for future use."""
    return {"webhooks": storage.list_tool_webhooks()}

@router.post("")
async def create_or_update_tool_webhook(
    req: ToolWebhookCreateRequest,
    request: Request,
    host: Optional[str] = Header(None),
    origin: Optional[str] = Header(None),
    storage: Storage = Depends(get_storage)
):
    """Saves a tool webhook (persisted for future campaigns and use cases)."""
    verify_loopback_headers(request, host, origin, require_origin=False)
    url = req.url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Webhook URL must begin with http:// or https://")

    webhook_id = req.id or f"wh_{uuid.uuid4().hex[:8]}"
    saved = storage.save_tool_webhook(
        webhook_id=webhook_id,
        name=req.name.strip(),
        url=url,
        headers=req.headers or {},
        enabled=req.enabled if req.enabled is not None else True
    )
    return {"status": "success", "webhook": saved}

class HandshakeProbeRequest(BaseModel):
    url: str
    headers: Optional[Dict[str, str]] = None

@router.post("/handshake")
async def handshake_probe(
    req: HandshakeProbeRequest,
    request: Request,
    host: Optional[str] = Header(None),
    origin: Optional[str] = Header(None)
):
    """
    Performs live handshake and connection test against any target webhook or tool API.
    Measures latency and returns detailed HTTP response outcome.
    """
    verify_loopback_headers(request, host, origin, require_origin=False)
    import time
    url = req.url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")

    test_payload = {
        "event": "handshake_probe",
        "probe": True,
        "source": "CampaignForge CRM Orchestrator",
        "timestamp": utc_now_iso(),
        "message": "Connection handshake test probe from CampaignForge"
    }
    headers = {"Content-Type": "application/json", "User-Agent": "CampaignForge-Handshake/1.0"}
    if req.headers:
        headers.update(req.headers)

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            res = await client.post(url, json=test_payload, headers=headers)
            latency_ms = int((time.time() - t0) * 1000)
            if res.status_code < 500:
                return {
                    "status": "success",
                    "status_code": res.status_code,
                    "latency_ms": latency_ms,
                    "message": f"Connection Established (HTTP {res.status_code}, {latency_ms}ms)"
                }
            else:
                return {
                    "status": "failed",
                    "status_code": res.status_code,
                    "latency_ms": latency_ms,
                    "message": f"Connection Failed: Remote returned HTTP {res.status_code} ({latency_ms}ms)"
                }
    except httpx.TimeoutException:
        return {
            "status": "failed",
            "status_code": 408,
            "latency_ms": 6000,
            "message": "Connection Failed: Request timed out after 6 seconds"
        }
    except Exception as e:
        return {
            "status": "failed",
            "status_code": 0,
            "latency_ms": 0,
            "message": f"Connection Failed: {str(e)}"
        }

@router.post("/{webhook_id}/test")
async def test_tool_webhook(
    webhook_id: str,
    request: Request,
    host: Optional[str] = Header(None),
    origin: Optional[str] = Header(None),
    storage: Storage = Depends(get_storage)
):
    """
    Tests connectivity to the tool webhook URL.
    Returns success (Connection Established) or fail (Connection Failed) status.
    """
    verify_loopback_headers(request, host, origin, require_origin=False)
    import time
    wh = storage.get_tool_webhook(webhook_id)
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook not found.")

    test_payload = {
        "event": "connection_probe",
        "probe": True,
        "source": "CampaignForge CRM Orchestrator",
        "timestamp": utc_now_iso(),
        "message": "Connection test probe from CampaignForge"
    }
    headers = {"Content-Type": "application/json", "User-Agent": "CampaignForge-Probe/1.0"}
    if wh.get("headers"):
        headers.update(wh["headers"])

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            res = await client.post(wh["url"], json=test_payload, headers=headers)
            latency_ms = int((time.time() - t0) * 1000)
            if res.status_code < 500:
                storage.update_tool_webhook_status(webhook_id, "success", res.status_code)
                return {
                    "status": "success",
                    "status_code": res.status_code,
                    "latency_ms": latency_ms,
                    "message": f"Connection Established (HTTP {res.status_code}, {latency_ms}ms)"
                }
            else:
                storage.update_tool_webhook_status(webhook_id, "failed", res.status_code)
                return {
                    "status": "failed",
                    "status_code": res.status_code,
                    "latency_ms": latency_ms,
                    "message": f"Connection Failed: Server returned HTTP {res.status_code} ({latency_ms}ms)"
                }
    except httpx.TimeoutException:
        storage.update_tool_webhook_status(webhook_id, "failed", 408)
        return {
            "status": "failed",
            "status_code": 408,
            "latency_ms": 6000,
            "message": "Connection Failed: Request timed out after 6 seconds"
        }
    except Exception as e:
        storage.update_tool_webhook_status(webhook_id, "failed", 0)
        return {
            "status": "failed",
            "status_code": 0,
            "latency_ms": 0,
            "message": f"Connection Failed: {str(e)}"
        }

@router.delete("/{webhook_id}")
def delete_tool_webhook(
    webhook_id: str,
    request: Request,
    host: Optional[str] = Header(None),
    origin: Optional[str] = Header(None),
    storage: Storage = Depends(get_storage)
):
    """Deletes a tool webhook from persistent storage."""
    verify_loopback_headers(request, host, origin, require_origin=False)
    deleted = storage.delete_tool_webhook(webhook_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    return {"status": "deleted", "webhook_id": webhook_id}
