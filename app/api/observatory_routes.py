"""
Agent Observatory API Routes
Provides endpoints for:
- Real-time TraceEvent streaming via SSE (/api/observatory/stream)
- Trace history querying and filtering (/api/observatory/traces)
- Component roster and health telemetry (/api/observatory/roster)
- Banned-URL manager CRUD (/api/observatory/banned_urls)
- Organization registry viewing and management (/api/observatory/orgs)
- Quarantine and Refusal queues (/api/observatory/quarantine)
- Component handoff statistics (/api/observatory/handoffs)
"""

import json
import asyncio
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Query, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.storage import get_storage
from app.core.orchestrator_v2_models import OrgRecord, TraceEvent
from app.services.supervisor import register_trace_subscriber, unregister_trace_subscriber, get_supervisor

router = APIRouter(prefix="/api/observatory", tags=["Agent Observatory"])
storage = get_storage()
supervisor = get_supervisor()


class BannedDomainRequest(BaseModel):
    domain: str
    match_type: str = "suffix"
    reason: str = "User added via Observatory"
    source: str = "manual"


class OrgRecordRequest(BaseModel):
    org_id: str
    display_name: str
    legal_name: str
    committee_name: Optional[str] = None
    org_type: str
    registered_domains: List[str] = []
    customer_care_contact: str
    sending_numbers: List[str] = []
    campaign_verify_token: Optional[str] = None
    registered_use_cases: List[str] = []
    approved: bool = True


@router.get("/stream")
async def observatory_stream(request: Request):
    """
    SSE stream pushing real-time TraceEvent records to Observatory UI.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    register_trace_subscriber(queue)

    async def event_generator():
        try:
            # Send initial connected message
            yield f"data: {json.dumps({'type': 'observatory.connected', 'message': 'Trace bus connected'})}\n\n"
            while True:
                # Check for client disconnect
                if await request.is_disconnected():
                    break
                try:
                    # Wait up to 15s for event or send heartbeat
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps({'type': 'trace_event', 'event': event})}\n\n"
                except asyncio.TimeoutError:
                    yield f": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            unregister_trace_subscriber(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/traces")
def get_traces(
    campaign_id: Optional[str] = None,
    run_id: Optional[str] = None,
    component_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    """Query traces from SQLite database with filtering."""
    traces = storage.list_trace_events(campaign_id=campaign_id, run_id=run_id, limit=limit)
    if component_id:
        traces = [t for t in traces if t.get("component_id") == component_id]
    if status:
        traces = [t for t in traces if t.get("status") == status]
    return {"traces": traces, "count": len(traces)}


@router.get("/runs")
def get_trace_runs(limit: int = Query(default=30, ge=1, le=100)):
    """Query recent execution runs/sessions from SQLite for n8n workflow playback."""
    runs = storage.list_trace_runs(limit=limit)
    return {"runs": runs, "count": len(runs)}


@router.get("/roster")
def get_component_roster():
    """
    Component roster with fixed status, assigned model/type, and telemetry.
    """
    roster = [
        {
            "id": "agent1",
            "name": "Agent 1: Drafter & Collector",
            "kind": "agent",
            "color": "#2563EB",
            "model": "gemini-3.6-flash (fallback: qwen2.5:7b-instruct)",
            "status": "online",
            "description": "Conversation, slot extraction, review card, and candidate drafting",
        },
        {
            "id": "agent2",
            "name": "Agent 2: Completeness Checker",
            "kind": "agent",
            "color": "#7C3AED",
            "model": "gemini-3.5-flash (fallback: qwen2.5:7b-instruct)",
            "status": "online",
            "description": "Instruction ledger, mandatory slots check, and card diff verification",
        },
        {
            "id": "guardrail",
            "name": "Agent 3: Guardrail Agent",
            "kind": "agent",
            "color": "#D97706",
            "model": "claude-sonnet-5-5 / gemini-3.1-pro (fail-closed)",
            "status": "online",
            "description": "Section 4.4 rubric evaluation, neutrality enforcement, and refusal generation",
        },
        {
            "id": "supervisor",
            "name": "Supervisor State Machine",
            "kind": "system",
            "color": "#64748B",
            "model": "deterministic state machine",
            "status": "online",
            "description": "Bus control, state transitions, retry limiter, and trace emission",
        },
        {
            "id": "compliance",
            "name": "Compliance Pipeline",
            "kind": "system",
            "color": "#0D9488",
            "model": "Rule Tools + Stage 2 Local Classifier",
            "status": "online",
            "description": "Deterministic CTIA/TCPA rule tools and harmful intent classification",
        },
        {
            "id": "audience",
            "name": "Audience Resolver",
            "kind": "system",
            "color": "#65A30D",
            "model": "deterministic system",
            "status": "online",
            "description": "E.164 normalization, suppression list filtering, and merge-value checking",
        },
        {
            "id": "dispatcher",
            "name": "Dispatcher & Rate Limiter",
            "kind": "system",
            "color": "#DB2777",
            "model": "50ms interval rate-governed dispatcher",
            "status": "online",
            "description": "Pre-flight verification and transmission to carrier aggregator",
        },
        {
            "id": "inbound",
            "name": "Inbound Handler",
            "kind": "system",
            "color": "#EA580C",
            "model": "deterministic rule handler",
            "status": "online",
            "description": "Inbound webhook processing for STOP/HELP opt-outs and DLR updates",
        },
    ]
    return {"roster": roster}


@router.get("/banned_urls")
def list_banned_urls():
    """List all registered banned URL domains."""
    return {"banned_domains": storage.list_banned_url_domains()}


@router.post("/banned_urls")
def add_banned_url(req: BannedDomainRequest):
    """Add a new domain to the banned URL domains list."""
    domain = req.domain.strip().lower()
    if not domain:
        raise HTTPException(status_code=400, detail="Domain cannot be empty")
    storage.add_banned_url_domain(
        domain=domain,
        match_type=req.match_type,
        reason=req.reason,
        source=req.source,
    )
    return {"status": "ok", "message": f"Domain '{domain}' added to banned list."}


@router.delete("/banned_urls/{domain}")
def delete_banned_url(domain: str):
    """Remove a domain from the banned URL domains list."""
    storage.delete_banned_url_domain(domain)
    return {"status": "ok", "message": f"Domain '{domain}' removed from banned list."}


@router.get("/orgs")
def list_orgs():
    """List organizations in the registry."""
    return {"organizations": storage.list_org_records()}


@router.post("/orgs")
def save_org(req: OrgRecordRequest):
    """Create or update organization in the registry."""
    record = OrgRecord(
        org_id=req.org_id,
        display_name=req.display_name,
        legal_name=req.legal_name,
        committee_name=req.committee_name,
        org_type=req.org_type,  # type: ignore
        registered_domains=req.registered_domains,
        customer_care_contact=req.customer_care_contact,
        sending_numbers=req.sending_numbers,
        campaign_verify_token=req.campaign_verify_token,
        registered_use_cases=req.registered_use_cases,
        approved=req.approved,
    )
    storage.upsert_org_record(record.model_dump())
    return {"status": "ok", "organization": record.model_dump()}


@router.get("/quarantine")
def list_quarantine():
    """List all quarantined and refused events."""
    traces = storage.list_trace_events(limit=500)
    quarantined = [
        t for t in traces
        if t.get("status") in ("quarantined", "blocked", "refused", "fail")
    ]
    return {"quarantined": quarantined, "count": len(quarantined)}


@router.get("/handoffs")
def get_handoff_stats():
    """Calculate handoff matrix from trace events."""
    traces = storage.list_trace_events(limit=500)
    handoffs = []
    for t in traces:
        if t.get("type") == "agent.handoff" and t.get("to_component"):
            handoffs.append({
                "from": t.get("component_id"),
                "to": t.get("to_component"),
                "ts": t.get("ts"),
                "run_id": t.get("run_id"),
            })
    return {"handoffs": handoffs, "count": len(handoffs)}
