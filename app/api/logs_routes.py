import json
import sqlite3
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel
from app.core.storage import get_storage
from app.core.config import get_config

router = APIRouter(prefix="/api", tags=["Logging, Export, & Memory"])
storage = get_storage()
config = get_config()

class MemoryCreateUpdateRequest(BaseModel):
    workspace_id: str = "default_workspace"
    category: str
    key: str
    value: Any
    confidence: float = 1.0

# 1. Sessions
@router.get("/sessions")
def list_sessions(limit: int = 50):
    return storage.list_sessions(limit=limit)

@router.get("/sessions/{session_id}/timeline")
def get_session_timeline(session_id: str):
    with storage.get_connection() as conn:
        events = conn.cursor().execute(
            "SELECT * FROM pipeline_events WHERE session_id = ? ORDER BY step_order ASC, id ASC",
            (session_id,)
        ).fetchall()
        calls = conn.cursor().execute(
            "SELECT * FROM llm_calls WHERE session_id = ? ORDER BY timestamp ASC",
            (session_id,)
        ).fetchall()
        actions = conn.cursor().execute(
            "SELECT * FROM user_actions WHERE session_id = ? ORDER BY timestamp ASC",
            (session_id,)
        ).fetchall()
        validations = conn.cursor().execute(
            "SELECT * FROM validations WHERE session_id = ? ORDER BY timestamp ASC",
            (session_id,)
        ).fetchall()

    timeline = []
    for e in events:
        d = dict(e)
        d["data"] = json.loads(d["data_json"]) if d["data_json"] else {}
        timeline.append({"type": "pipeline_event", "record": d, "timestamp": d["timestamp"]})
    for c in calls:
        timeline.append({"type": "llm_call", "record": dict(c), "timestamp": c["timestamp"]})
    for a in actions:
        d = dict(a)
        d["payload"] = json.loads(d["payload_json"]) if d["payload_json"] else {}
        timeline.append({"type": "user_action", "record": d, "timestamp": d["timestamp"]})
    for v in validations:
        d = dict(v)
        d["details"] = json.loads(d["details_json"]) if d["details_json"] else {}
        timeline.append({"type": "validation", "record": d, "timestamp": d["timestamp"]})

    timeline.sort(key=lambda x: x["timestamp"] or "")
    return {"session_id": session_id, "timeline": timeline}

# 2. LLM Call Logs
@router.get("/logs/llm_calls")
def get_llm_calls(
    session_id: Optional[str] = Query(None),
    purpose: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = 100
):
    query = "SELECT * FROM llm_calls WHERE 1=1"
    params: List[Any] = []
    if session_id:
        query += " AND session_id = ?"
        params.append(session_id)
    if purpose:
        query += " AND purpose = ?"
        params.append(purpose)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    with storage.get_connection() as conn:
        rows = conn.cursor().execute(query, params).fetchall()
        return [dict(r) for r in rows]

# 3. Pipeline Events Logs
@router.get("/logs/events")
def get_events(session_id: Optional[str] = Query(None), limit: int = 100):
    query = "SELECT * FROM pipeline_events WHERE 1=1"
    params: List[Any] = []
    if session_id:
        query += " AND session_id = ?"
        params.append(session_id)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    with storage.get_connection() as conn:
        rows = conn.cursor().execute(query, params).fetchall()
        res = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data_json"]) if d["data_json"] else {}
            res.append(d)
        return res

# 4. Actions Logs
@router.get("/logs/actions")
def get_actions(session_id: Optional[str] = Query(None), limit: int = 100):
    query = "SELECT * FROM user_actions WHERE 1=1"
    params: List[Any] = []
    if session_id:
        query += " AND session_id = ?"
        params.append(session_id)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    with storage.get_connection() as conn:
        rows = conn.cursor().execute(query, params).fetchall()
        res = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload_json"]) if d["payload_json"] else {}
            res.append(d)
        return res

# 5. Validations Logs
@router.get("/logs/validations")
def get_validations(session_id: Optional[str] = Query(None), limit: int = 100):
    query = "SELECT * FROM validations WHERE 1=1"
    params: List[Any] = []
    if session_id:
        query += " AND session_id = ?"
        params.append(session_id)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    with storage.get_connection() as conn:
        rows = conn.cursor().execute(query, params).fetchall()
        res = []
        for r in rows:
            d = dict(r)
            d["details"] = json.loads(d["details_json"]) if d["details_json"] else {}
            res.append(d)
        return res

# 6. Memory Ops Logs
@router.get("/logs/memory_ops")
def get_memory_ops(session_id: Optional[str] = Query(None), limit: int = 100):
    query = "SELECT * FROM memory_ops WHERE 1=1"
    params: List[Any] = []
    if session_id:
        query += " AND session_id = ?"
        params.append(session_id)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    with storage.get_connection() as conn:
        rows = conn.cursor().execute(query, params).fetchall()
        res = []
        for r in rows:
            d = dict(r)
            d["value"] = json.loads(d["value_json"]) if d["value_json"] else None
            res.append(d)
        return res

# 7. Errors Logs
@router.get("/logs/errors")
def get_errors(session_id: Optional[str] = Query(None), limit: int = 100):
    query = "SELECT * FROM errors WHERE 1=1"
    params: List[Any] = []
    if session_id:
        query += " AND session_id = ?"
        params.append(session_id)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    with storage.get_connection() as conn:
        rows = conn.cursor().execute(query, params).fetchall()
        return [dict(r) for r in rows]

# 8. Memory Management API
@router.get("/memory")
def get_memory(workspace_id: str = "default_workspace", category: Optional[str] = Query(None)):
    return storage.get_workspace_memory(workspace_id=workspace_id, category=category)

@router.put("/memory")
def update_memory(req: MemoryCreateUpdateRequest):
    storage.write_memory(
        workspace_id=req.workspace_id,
        category=req.category,
        key=req.key,
        value=req.value,
        confidence=req.confidence,
        session_id="api_manual_update"
    )
    return {"status": "success", "message": f"Memory updated for {req.category}:{req.key}"}

@router.delete("/memory")
def delete_memory(workspace_id: str = "default_workspace", category: str = Query(...), key: str = Query(...)):
    success = storage.delete_workspace_memory(workspace_id=workspace_id, category=category, key=key, session_id="api_manual_delete")
    if not success:
        raise HTTPException(status_code=404, detail="Memory entry not found")
    return {"status": "success", "message": f"Memory deleted: {category}:{key}"}

# 9. Campaign Export
@router.get("/export/campaign/{session_id}")
def export_campaign(session_id: str, format: Optional[str] = "txt"):
    from fastapi.responses import Response
    campaign = storage.get_campaign(session_id)
    if not campaign:
        raise HTTPException(status_code=404, detail=f"Campaign not found for session {session_id}")
    
    if format == "json":
        return campaign

    # Format human-readable campaign document
    b_name = campaign.get("business_name") or "Organization"
    c_name = campaign.get("campaign_name") or "Campaign Sequence"
    b_type = campaign.get("business_type") or "General"
    tone_used = campaign.get("selected_tone") or "Standard"
    slots = campaign.get("slots") or {}
    templates = campaign.get("templates") or []
    cfg = campaign.get("config") or {}

    lines = []
    lines.append("=" * 60)
    lines.append(f"CAMPAIGN DISPATCH SPECIFICATION: {c_name.upper()}")
    lines.append("=" * 60)
    lines.append(f"Organization / Business: {b_name}")
    lines.append(f"Domain / Category:       {b_type}")
    lines.append(f"Tone Used:               {tone_used}")
    lines.append(f"Word Limit:              {cfg.get('word_limit', 320)} words per message")
    lines.append(f"Mandatory Opt-Out:       \"{cfg.get('optout_text', 'Reply STOP to unsubscribe.')}\"")
    
    # Campaign parameters / slots
    lines.append("\n" + "-" * 60)
    lines.append("CAMPAIGN PARAMETERS & DETAILS:")
    lines.append("-" * 60)
    for k, v in slots.items():
        val = v.get("value") if isinstance(v, dict) else v
        if val is not None:
            lines.append(f"• {k.replace('_', ' ').title()}: {val}")

    # Messages
    lines.append("\n" + "=" * 60)
    lines.append(f"MESSAGE SEQUENCE ({len(templates)} Messages)")
    lines.append("=" * 60)

    for tmpl in templates:
        idx = tmpl.get("message_index", 1)
        stage = tmpl.get("stage", "Notification")
        w_count = tmpl.get("word_count", 0)
        body = tmpl.get("body", "").strip()

        lines.append(f"\n[ MESSAGE {idx}: {stage.upper()} ]")
        lines.append(f"Tone:       {tone_used}")
        lines.append(f"Word Count: {w_count} / {cfg.get('word_limit', 320)} words")
        lines.append("-" * 40)
        lines.append(body)
        lines.append("-" * 40)

    # Footer instructions
    lines.append("\n" + "=" * 60)
    lines.append("DISPATCH INSTRUCTIONS:")
    lines.append("• Placeholders in {{double_braces}} will be dynamically populated from your contact list.")
    lines.append("• Send each message in accordance with your configured cadence schedule.")
    lines.append("=" * 60)

    export_text = "\n".join(lines)
    filename = f"{b_name.replace(' ', '_')}_{c_name.replace(' ', '_')}_campaign.txt"
    return Response(
        content=export_text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

# 10. Run Log Export
@router.get("/export/run_log/{session_id}")
def export_run_log(session_id: str):
    session = storage.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found for id {session_id}")
    
    with storage.get_connection() as conn:
        calls = [dict(r) for r in conn.cursor().execute("SELECT * FROM llm_calls WHERE session_id = ? ORDER BY timestamp ASC", (session_id,)).fetchall()]
        events = [dict(r) for r in conn.cursor().execute("SELECT * FROM pipeline_events WHERE session_id = ? ORDER BY id ASC", (session_id,)).fetchall()]
        actions = [dict(r) for r in conn.cursor().execute("SELECT * FROM user_actions WHERE session_id = ? ORDER BY id ASC", (session_id,)).fetchall()]
        validations = [dict(r) for r in conn.cursor().execute("SELECT * FROM validations WHERE session_id = ? ORDER BY id ASC", (session_id,)).fetchall()]
        memory_ops = [dict(r) for r in conn.cursor().execute("SELECT * FROM memory_ops WHERE session_id = ? ORDER BY id ASC", (session_id,)).fetchall()]
        errors = [dict(r) for r in conn.cursor().execute("SELECT * FROM errors WHERE session_id = ? ORDER BY id ASC", (session_id,)).fetchall()]

    return {
        "session": session,
        "llm_calls": calls,
        "pipeline_events": events,
        "user_actions": actions,
        "validations": validations,
        "memory_ops": memory_ops,
        "errors": errors
    }

# 11. Channel Calls Logs (Step 5)
@router.get("/logs/channel_calls")
def get_channel_calls_endpoint(
    session_id: Optional[str] = Query(None),
    tool_name: Optional[str] = Query(None),
    status_code: Optional[int] = Query(None),
    limit: int = 100
):
    return storage.get_channel_calls(session_id=session_id, tool_name=tool_name, status_code=status_code, limit=limit)

# 12. Send Log Export (Step 5)
@router.get("/export/send_log")
def export_send_log(session_id: Optional[str] = Query(None)):
    calls = storage.get_channel_calls(session_id=session_id, limit=500)
    lines = [json.dumps(c) for c in calls]
    content = "\n".join(lines)
    return Response(
        content=content,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="channel_calls.jsonl"'}
    )
