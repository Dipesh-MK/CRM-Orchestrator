import hmac
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Request, Header
from pydantic import BaseModel
from app.core.config import get_config
from app.services.dispatch_service import get_dispatch_service

router = APIRouter(prefix="/api/dispatch", tags=["dispatch"])

class PreflightRequest(BaseModel):
    session_id: str
    template_step: int = 1

class SendRequest(BaseModel):
    job_id: str
    csrf_token: str

def verify_loopback_headers(request: Request, host: Optional[str], origin: Optional[str], cfg: Any):
    """
    Host and Origin must be verified against config allowed lists.
    A spoofed Host header or a missing/foreign Origin is refused with HTTP 403.
    Exact match of Host (including port and [::1]) and full Origin (scheme + host + port).
    """
    allowed_hosts = set(cfg.dispatch.allowed_hosts)
    allowed_origins = set(o.rstrip("/") for o in cfg.dispatch.allowed_origins)

    # 1. Host check (exact match against allowed_hosts including port and IPv6 [::1])
    host_val = host or request.headers.get("host", "")
    if not host_val or host_val not in allowed_hosts:
        raise HTTPException(
            status_code=403,
            detail=f"Dispatch refused: Invalid or spoofed Host '{host_val}' rejected by security policy"
        )

    # 2. Origin check (Required on POST; exact match of full origin against allowed_origins)
    origin_val = origin or request.headers.get("origin", "")
    if not origin_val:
        raise HTTPException(
            status_code=403,
            detail="Dispatch refused: Missing Origin header on dispatch POST request"
        )

    clean_origin = origin_val.rstrip("/")
    if clean_origin not in allowed_origins:
        raise HTTPException(
            status_code=403,
            detail=f"Dispatch refused: Foreign Origin '{origin_val}' rejected by loopback policy"
        )

@router.post("/preflight")
async def preflight_endpoint(req: PreflightRequest):
    """Runs pre-flight validation, generates CSRF token, and returns preview."""
    service = get_dispatch_service()
    res = await service.preflight_run(req.session_id, req.template_step)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    return res

@router.post("/send")
async def send_endpoint(
    req: SendRequest,
    request: Request,
    host: Optional[str] = Header(None),
    origin: Optional[str] = Header(None),
    x_csrf_token: Optional[str] = Header(None)
):
    """
    Executes confirm and send with exact Host/Origin checks and CSRF verification.
    """
    cfg = get_config()
    verify_loopback_headers(request, host, origin, cfg)

    csrf = x_csrf_token or req.csrf_token or request.headers.get("x-csrf-token", "")
    if not csrf:
        raise HTTPException(status_code=403, detail="Dispatch refused: Missing CSRF token")

    service = get_dispatch_service()
    res = await service.confirm_and_send(job_id=req.job_id, csrf_token=csrf)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    return res

@router.get("/jobs/{job_id}")
async def get_job_endpoint(job_id: str):
    """Returns status and item list for a send job."""
    service = get_dispatch_service()
    job = service.storage.get_send_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Send job not found")
    items = service.storage.get_send_items_by_job(job_id)
    return {"job": job, "items": items}

@router.post("/jobs/{job_id}/refresh")
async def refresh_job_endpoint(job_id: str):
    """Refreshes status from aggregator operator inspection tool."""
    service = get_dispatch_service()
    res = await service.refresh_job_status(job_id)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    return res
