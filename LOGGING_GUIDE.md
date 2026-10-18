# CampaignForge Orchestrator — Logging & Diagnostics Guide

CampaignForge Orchestrator follows a strict **"Log Everything"** philosophy. Every single LLM call (system prompt, messages, raw response, tokens, latency ms), pipeline lifecycle event, validation, memory read/write, user action, and system error is mirrored in SQLite (`data/campaignforge.db`) and append-only JSONL files in `/logs`.

---

## 1. Viewing Logs via the Web UI

1. Open the application at `http://localhost:8000`.
2. Click the **📋 Logs** tab in the top navigation bar.
3. Use the category dropdown to select what you want to inspect:
   - **LLM Calls**: Displays every call with purpose, model, status, token counts, and latency.
     - Click **🔍 View Full Prompt & Raw Response** to open an interactive modal displaying the exact un-truncated prompt and raw response received.
   - **Pipeline Events**: Shows all `<pipeline_event>` milestones.
   - **Compliance Validations**: Inspects word-count checks, opt-out verifications, and safety classifications.
   - **Workspace Memory**: Shows durable learned facts, preferred tones, and business rules.
   - **Memory Ops**: Audit trail of every read/write/delete.
   - **User Actions**: History of user clicks, uploads, and edits.
   - **Errors**: Exception traces if any occurred.
4. Click **📥 Download Run Log** to download the complete JSON run log for the active session.

---

## 2. Viewing Logs via FastAPI Swagger UI (`/docs`)

Visit `http://localhost:8000/docs` in your browser. All logging endpoints are interactive:

- **`GET /api/sessions`**: List all campaign orchestration sessions.
- **`GET /api/sessions/{session_id}/timeline`**: Full chronological event-by-event timeline for a session.
- **`GET /api/logs/llm_calls`**: Query LLM calls with optional filters:
  ```http
  GET /api/logs/llm_calls?session_id=session_xyz&purpose=discovery&status=success
  ```
- **`GET /api/logs/events`**: Query pipeline events.
- **`GET /api/logs/validations`**: Query compliance & safety checks.
- **`GET /api/logs/memory_ops`**: Query memory operations.
- **`GET /api/logs/errors`**: Query captured system errors.
- **`GET /api/memory`**: View active workspace persistent memory.
- **`GET /api/export/campaign/{session_id}`**: Export approved campaign package JSON.
- **`GET /api/export/run_log/{session_id}`**: Export complete run log JSON.

---

## 3. Viewing Logs via `curl`

### Retrieve LLM Calls for a Session
```bash
curl -s "http://localhost:8000/api/logs/llm_calls?session_id=session_xyz" | jq .
```

### Inspect Recent Validations (Word count & Safety)
```bash
curl -s "http://localhost:8000/api/logs/validations" | jq .
```

### View Workspace Memory
```bash
curl -s "http://localhost:8000/api/memory?workspace_id=default_workspace" | jq .
```

### Export Full Campaign Run Log
```bash
curl -s "http://localhost:8000/api/export/run_log/session_xyz" -o run_log_session_xyz.json
```

---

## 4. Viewing Logs via SQLite (`sqlite3`)

Connect directly to the database:
```bash
sqlite3 data/campaignforge.db
```

### Sample Queries

#### View all LLM calls with purpose, latency, and tokens:
```sql
SELECT id, purpose, model, latency_ms, prompt_tokens, completion_tokens, status, timestamp
FROM llm_calls
ORDER BY timestamp DESC;
```

#### Inspect the full prompt and raw response of the latest LLM call:
```sql
SELECT purpose, full_prompt, full_raw_response
FROM llm_calls
ORDER BY timestamp DESC
LIMIT 1;
```

#### Check all compliance validation results:
```sql
SELECT session_id, validation_type, passed, details_json, timestamp
FROM validations
ORDER BY timestamp DESC;
```

#### View all durable workspace memory entries:
```sql
SELECT workspace_id, category, key, value_json, confidence, updated_at
FROM workspace_memory;
```

---

## 5. Real-Time Tail on JSONL Files (`tail -f`)

All database entries are simultaneously mirrored to append-only JSONL files in `logs/`:

```bash
# Follow LLM calls live
tail -f logs/llm_calls.jsonl | jq .

# Follow pipeline events live
tail -f logs/events.jsonl | jq .

# Follow validations live (word count repairs, safety checks)
tail -f logs/validations.jsonl | jq .

# Follow memory operations live
tail -f logs/memory_ops.jsonl | jq .

# Follow errors live
tail -f logs/errors.jsonl | jq .
```
