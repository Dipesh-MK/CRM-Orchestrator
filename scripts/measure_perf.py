#!/usr/bin/env python3
"""
scripts/measure_perf.py
Measures performance of the real chat flow for the scripted NGO conversation.
"""
import os
import sys
import time
import json
import uuid
import httpx
from pathlib import Path
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import get_config
from app.core.storage import get_storage
from app.api.chat_routes import _session_states

def run_ngo_flow(output_path: str):
    config = get_config(reload=True)
    storage = get_storage()
    session_id = f"perf_{uuid.uuid4().hex[:8]}"

    client = TestClient(app)
    t0 = time.time()

    # 1. First turn: User initiates NGO campaign
    user_msg = "We are Green Earth Foundation launching our Clean Water for Villages emergency flood relief donation appeal."
    r1 = client.post("/api/chat/send", json={"message": user_msg, "session_id": session_id})
    assert r1.status_code == 200, f"Turn 1 failed: {r1.status_code} {r1.text}"

    state = _session_states.get(session_id, {})
    candidate_tones = state.get("candidate_tones", [])
    num_tones = len(candidate_tones)

    # 2. Select first tone if available, or fallback
    if candidate_tones:
        selected_tone = candidate_tones[0]
        t_id = selected_tone.get("id", "tone_1")
        t_name = selected_tone.get("name", "Warm & Friendly")
    else:
        t_id = "default"
        t_name = "Warm & Friendly"

    r2 = client.post("/api/chat/select_tone", json={"session_id": session_id, "tone_id": t_id, "tone_name": t_name})
    assert r2.status_code == 200, f"Tone select failed: {r2.status_code} {r2.text}"

    # 3. Approve campaign
    r3 = client.post("/api/campaign/approve", json={"session_id": session_id})
    assert r3.status_code == 200, f"Approve failed: {r3.status_code} {r3.text}"

    total_wall_clock_ms = int((time.time() - t0) * 1000)

    # Query LLM calls from storage
    with storage.get_connection() as conn:
        rows = conn.cursor().execute(
            "SELECT purpose, latency_ms FROM llm_calls WHERE session_id = ? ORDER BY id ASC",
            (session_id,)
        ).fetchall()

    per_purpose = {}
    for row in rows:
        purpose = row["purpose"]
        lat = row["latency_ms"] or 0
        if purpose not in per_purpose:
            per_purpose[purpose] = {"calls": 0, "total_latency_ms": 0, "latencies": []}
        per_purpose[purpose]["calls"] += 1
        per_purpose[purpose]["total_latency_ms"] += lat
        per_purpose[purpose]["latencies"].append(lat)

    per_purpose_summary = {}
    for purpose, data in per_purpose.items():
        avg = int(data["total_latency_ms"] / data["calls"]) if data["calls"] > 0 else 0
        per_purpose_summary[purpose] = {
            "calls": data["calls"],
            "average_latency_ms": avg,
            "total_latency_ms": data["total_latency_ms"]
        }

    result = {
        "session_id": session_id,
        "total_wall_clock_ms": total_wall_clock_ms,
        "total_wall_clock_seconds": round(total_wall_clock_ms / 1000, 2),
        "tone_candidates_count": num_tones,
        "per_purpose": per_purpose_summary,
        "raw_calls_count": len(rows)
    }

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else ".apitool/evidence/perf_baseline.json"
    run_ngo_flow(out)
