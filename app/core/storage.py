import sqlite3
import json
import os
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from app.core.config import get_config

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

class Storage:
    def __init__(self, db_path: Optional[str] = None, logs_dir: Optional[str] = None):
        cfg = get_config()
        self.db_path = db_path or cfg.paths.database_path
        self.logs_dir = logs_dir or cfg.paths.logs_dir
        
        # Ensure directories exist
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.logs_dir).mkdir(parents=True, exist_ok=True)
        
        self.init_db()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA busy_timeout = 30000;")
        except Exception:
            pass
        return conn

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Sessions
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                workspace_id TEXT,
                business_type TEXT,
                campaign_name TEXT,
                status TEXT,
                created_at TEXT,
                updated_at TEXT,
                meta_json TEXT
            )
            """)

            # LLM Calls
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS llm_calls (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                step_id TEXT,
                purpose TEXT,
                timestamp TEXT,
                model TEXT,
                base_url TEXT,
                temperature REAL,
                full_prompt TEXT,
                full_raw_response TEXT,
                parsed_output TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                latency_ms INTEGER,
                retries INTEGER,
                status TEXT,
                error TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
            """)

            # Pipeline Events
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                step_order INTEGER,
                step_id TEXT,
                step_title TEXT,
                step_icon TEXT,
                status TEXT,
                description TEXT,
                sub_step TEXT,
                sub_status TEXT,
                data_json TEXT,
                timestamp TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
            """)

            # User Actions
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                action_type TEXT,
                payload_json TEXT,
                timestamp TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
            """)

            # Validations
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS validations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                validation_type TEXT,
                passed INTEGER,
                details_json TEXT,
                timestamp TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
            """)

            # Memory Operations
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS memory_ops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                workspace_id TEXT,
                operation TEXT,
                category TEXT,
                key TEXT,
                value_json TEXT,
                timestamp TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
            """)

            # Workspace Memory
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS workspace_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id TEXT,
                category TEXT,
                key TEXT,
                value_json TEXT,
                confidence REAL,
                created_at TEXT,
                updated_at TEXT,
                UNIQUE(workspace_id, category, key)
            )
            """)

            # Errors
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                error_type TEXT,
                message TEXT,
                traceback TEXT,
                timestamp TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
            """)

            # Campaigns (Final approved/saved state)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS campaigns (
                session_id TEXT PRIMARY KEY,
                workspace_id TEXT,
                business_name TEXT,
                campaign_name TEXT,
                business_type TEXT,
                config_json TEXT,
                slots_json TEXT,
                selected_tone TEXT,
                rejected_tones_json TEXT,
                templates_json TEXT,
                contacts_count INTEGER,
                plan_json TEXT,
                approved INTEGER,
                created_at TEXT,
                updated_at TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
            """)

            # Contacts Table (Amendment 1)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                contact_id TEXT PRIMARY KEY,
                session_id TEXT,
                name TEXT,
                phone TEXT,
                extra_json TEXT,
                created_at TEXT
            )
            """)

            # Send Jobs (Step 5 At-most-once)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS send_jobs (
                job_id TEXT PRIMARY KEY,
                session_id TEXT,
                status TEXT,
                total_items INTEGER DEFAULT 0,
                successful_items INTEGER DEFAULT 0,
                failed_items INTEGER DEFAULT 0,
                csrf_token TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """)

            # Send Items (Step 5 At-most-once)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS send_items (
                item_id TEXT PRIMARY KEY,
                job_id TEXT,
                contact_id TEXT,
                phone TEXT,
                status TEXT,
                message_sid TEXT,
                error TEXT,
                sent_at TEXT,
                FOREIGN KEY (job_id) REFERENCES send_jobs(job_id)
            )
            """)

            # Channel Calls (Telemetry logging)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS channel_calls (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                job_id TEXT,
                item_id TEXT,
                tool_name TEXT,
                adapter TEXT,
                operation TEXT,
                status_code INTEGER,
                duration_ms INTEGER,
                payload_redacted TEXT,
                response_redacted TEXT,
                error TEXT,
                created_at TEXT
            )
            """)

            # Phone Configurations (Multi-Aggregator per-phone routing)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS phone_configurations (
                phone TEXT PRIMARY KEY,
                provider_id TEXT NOT NULL,
                endpoint_url TEXT,
                config_json TEXT,
                status TEXT DEFAULT 'unverified',
                capabilities_json TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """)

            # Phone Messages (Two-way texting conversation thread and webhook ingestion)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS phone_messages (
                id TEXT PRIMARY KEY,
                phone TEXT NOT NULL,
                direction TEXT NOT NULL,
                body TEXT NOT NULL,
                media_urls_json TEXT,
                status TEXT DEFAULT 'queued',
                provider_id TEXT,
                provider_message_id TEXT,
                error TEXT,
                timestamp TEXT,
                FOREIGN KEY (phone) REFERENCES phone_configurations(phone)
            )
            """)

            # Tool Webhooks (Configurable custom webhooks for broadcasting outbound messages)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS tool_webhooks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                headers_json TEXT,
                enabled INTEGER DEFAULT 1,
                last_status TEXT DEFAULT 'untested',
                last_status_code INTEGER,
                last_tested_at TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """)

            # Ensure columns exist if table was already created
            cursor.execute("PRAGMA table_info(channel_calls)")
            existing_cols = [col[1] for col in cursor.fetchall()]
            for c in ["session_id", "job_id", "item_id", "tool_name", "adapter", "duration_ms", "payload_redacted", "response_redacted", "error", "created_at"]:
                if c not in existing_cols:
                    cursor.execute(f"ALTER TABLE channel_calls ADD COLUMN {c} TEXT")

            # Startup Recovery: mark any stranded 'sending' items as 'unknown'
            cursor.execute("UPDATE send_items SET status = 'unknown' WHERE status = 'sending'")
            cursor.execute("UPDATE send_jobs SET status = 'interrupted' WHERE status = 'sending'")

            conn.commit()

    def _append_jsonl(self, filename: str, data: Dict[str, Any]):
        file_path = Path(self.logs_dir) / filename
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    # Session methods
    def save_session(self, session_id: str, workspace_id: str, business_type: Optional[str] = None,
                     campaign_name: Optional[str] = None, status: str = "active", meta: Optional[Dict[str, Any]] = None):
        now = utc_now_iso()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            existing = cursor.execute("SELECT meta_json, campaign_name FROM sessions WHERE id = ?", (session_id,)).fetchone()
            merged_meta = {}
            if existing and existing[0]:
                try:
                    merged_meta = json.loads(existing[0])
                except Exception:
                    merged_meta = {}
            if meta:
                merged_meta.update(meta)
            
            # Preserve custom title if previously saved by user
            if merged_meta.get("custom_title") and not campaign_name:
                campaign_name = merged_meta["custom_title"]

            meta_str = json.dumps(merged_meta)
            cursor.execute("""
            INSERT INTO sessions (id, workspace_id, business_type, campaign_name, status, created_at, updated_at, meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                business_type=COALESCE(?, business_type),
                campaign_name=COALESCE(?, campaign_name),
                status=?,
                updated_at=?,
                meta_json=?
            """, (session_id, workspace_id, business_type, campaign_name, status, now, now, meta_str,
                  business_type, campaign_name, status, now, meta_str))
            conn.commit()

    def update_session_title(self, session_id: str, title: str) -> bool:
        clean_title = (title or "").strip()
        if not clean_title:
            return False
        with self.get_connection() as conn:
            cursor = conn.cursor()
            row = cursor.execute("SELECT meta_json FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not row:
                return False
            meta = json.loads(row[0]) if row[0] else {}
            meta["custom_title"] = clean_title
            now = utc_now_iso()
            cursor.execute("""
                UPDATE sessions
                SET campaign_name = ?, meta_json = ?, updated_at = ?
                WHERE id = ?
            """, (clean_title, json.dumps(meta), now, session_id))
            conn.commit()
            return True

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.cursor().execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row:
                d = dict(row)
                d["meta"] = json.loads(d["meta_json"]) if d["meta_json"] else {}
                return d
            return None

    def list_sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.cursor().execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                meta = json.loads(d["meta_json"]) if d["meta_json"] else {}
                d["meta"] = meta
                
                # Derive display_title
                custom = meta.get("custom_title")
                camp_name = d.get("campaign_name")
                auto = meta.get("auto_title")
                
                if custom and str(custom).strip():
                    display_title = str(custom).strip()
                elif camp_name and str(camp_name).strip():
                    display_title = str(camp_name).strip()
                elif auto and str(auto).strip():
                    display_title = str(auto).strip()
                else:
                    btype = d.get("business_type")
                    if btype:
                        display_title = f"{btype.capitalize()} Campaign"
                    else:
                        display_title = f"Campaign {d['id'][:8]}"
                
                d["display_title"] = display_title
                
                # Check if campaign approved
                camp_row = conn.cursor().execute("SELECT approved FROM campaigns WHERE session_id = ?", (d["id"],)).fetchone()
                d["is_approved"] = bool(camp_row and camp_row[0])
                results.append(d)
            return results

    # LLM Call logging
    def log_llm_call(self, call_data: Dict[str, Any]):
        timestamp = call_data.get("timestamp") or utc_now_iso()
        call_data["timestamp"] = timestamp
        with self.get_connection() as conn:
            conn.cursor().execute("""
            INSERT INTO llm_calls (
                id, session_id, step_id, purpose, timestamp, model, base_url, temperature,
                full_prompt, full_raw_response, parsed_output, prompt_tokens,
                completion_tokens, latency_ms, retries, status, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                call_data.get("id"),
                call_data.get("session_id"),
                call_data.get("step_id"),
                call_data.get("purpose"),
                timestamp,
                call_data.get("model"),
                call_data.get("base_url"),
                call_data.get("temperature"),
                call_data.get("full_prompt"),
                call_data.get("full_raw_response"),
                call_data.get("parsed_output"),
                call_data.get("prompt_tokens", 0),
                call_data.get("completion_tokens", 0),
                call_data.get("latency_ms", 0),
                call_data.get("retries", 0),
                call_data.get("status", "success"),
                call_data.get("error")
            ))
            conn.commit()
        self._append_jsonl("llm_calls.jsonl", call_data)

    # Pipeline Event logging
    def log_pipeline_event(self, event_data: Dict[str, Any]):
        timestamp = event_data.get("timestamp") or utc_now_iso()
        event_data["timestamp"] = timestamp
        data_json = json.dumps(event_data.get("data") or {})
        with self.get_connection() as conn:
            conn.cursor().execute("""
            INSERT INTO pipeline_events (
                session_id, step_order, step_id, step_title, step_icon, status,
                description, sub_step, sub_status, data_json, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event_data.get("session_id"),
                event_data.get("step_order", 0),
                event_data.get("step_id"),
                event_data.get("step_title"),
                event_data.get("step_icon"),
                event_data.get("status"),
                event_data.get("description"),
                event_data.get("sub_step"),
                event_data.get("sub_status"),
                data_json,
                timestamp
            ))
            conn.commit()
        self._append_jsonl("events.jsonl", event_data)

    def get_pipeline_events(self, session_id: str) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.cursor().execute(
                "SELECT * FROM pipeline_events WHERE session_id = ? ORDER BY step_order ASC, id ASC",
                (session_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # User action logging
    def log_user_action(self, session_id: str, action_type: str, payload: Dict[str, Any]):
        timestamp = utc_now_iso()
        record = {
            "session_id": session_id,
            "action_type": action_type,
            "payload": payload,
            "timestamp": timestamp
        }
        with self.get_connection() as conn:
            conn.cursor().execute("""
            INSERT INTO user_actions (session_id, action_type, payload_json, timestamp)
            VALUES (?, ?, ?, ?)
            """, (session_id, action_type, json.dumps(payload), timestamp))
            conn.commit()
        self._append_jsonl("actions.jsonl", record)

    # Validation logging
    def log_validation(self, session_id: str, validation_type: str, passed: bool, details: Dict[str, Any]):
        timestamp = utc_now_iso()
        record = {
            "session_id": session_id,
            "validation_type": validation_type,
            "passed": 1 if passed else 0,
            "details": details,
            "timestamp": timestamp
        }
        with self.get_connection() as conn:
            conn.cursor().execute("""
            INSERT INTO validations (session_id, validation_type, passed, details_json, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """, (session_id, validation_type, 1 if passed else 0, json.dumps(details), timestamp))
            conn.commit()
        self._append_jsonl("validations.jsonl", record)

    # Memory operation logging & persistence
    def log_memory_op(self, session_id: str, workspace_id: str, operation: str, category: str, key: str, value: Any):
        timestamp = utc_now_iso()
        record = {
            "session_id": session_id,
            "workspace_id": workspace_id,
            "operation": operation,
            "category": category,
            "key": key,
            "value": value,
            "timestamp": timestamp
        }
        with self.get_connection() as conn:
            conn.cursor().execute("""
            INSERT INTO memory_ops (session_id, workspace_id, operation, category, key, value_json, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (session_id, workspace_id, operation, category, key, json.dumps(value), timestamp))
            conn.commit()
        self._append_jsonl("memory_ops.jsonl", record)

    def write_memory(self, workspace_id: str, category: str, key: str, value: Any, confidence: float = 1.0, session_id: str = ""):
        now = utc_now_iso()
        val_str = json.dumps(value)
        with self.get_connection() as conn:
            conn.cursor().execute("""
            INSERT INTO workspace_memory (workspace_id, category, key, value_json, confidence, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id, category, key) DO UPDATE SET
                value_json=?,
                confidence=?,
                updated_at=?
            """, (workspace_id, category, key, val_str, confidence, now, now, val_str, confidence, now))
            conn.commit()
        self.log_memory_op(session_id, workspace_id, "write", category, key, value)

    def get_workspace_memory(self, workspace_id: str, category: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM workspace_memory WHERE workspace_id = ?"
        params: List[Any] = [workspace_id]
        if category:
            query += " AND category = ?"
            params.append(category)
        query += " ORDER BY updated_at DESC"
        with self.get_connection() as conn:
            rows = conn.cursor().execute(query, params).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["value"] = json.loads(d["value_json"]) if d["value_json"] else None
                results.append(d)
            return results

    def delete_workspace_memory(self, workspace_id: str, category: str, key: str, session_id: str = "") -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM workspace_memory WHERE workspace_id = ? AND category = ? AND key = ?", (workspace_id, category, key))
            conn.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            self.log_memory_op(session_id, workspace_id, "delete", category, key, None)
        return deleted

    # Error logging
    def log_error(self, session_id: str, error_type: str, message: str, traceback_str: str):
        timestamp = utc_now_iso()
        record = {
            "session_id": session_id,
            "error_type": error_type,
            "message": message,
            "traceback": traceback_str,
            "timestamp": timestamp
        }
        with self.get_connection() as conn:
            conn.cursor().execute("""
            INSERT INTO errors (session_id, error_type, message, traceback, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """, (session_id, error_type, message, traceback_str, timestamp))
            conn.commit()
        self._append_jsonl("errors.jsonl", record)

    # Final Campaign save/get
    def save_campaign(self, session_id: str, campaign_dict: Dict[str, Any]):
        now = utc_now_iso()
        with self.get_connection() as conn:
            conn.cursor().execute("""
            INSERT INTO campaigns (
                session_id, workspace_id, business_name, campaign_name, business_type,
                config_json, slots_json, selected_tone, rejected_tones_json,
                templates_json, contacts_count, plan_json, approved, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                business_name=?, campaign_name=?, business_type=?,
                config_json=?, slots_json=?, selected_tone=?, rejected_tones_json=?,
                templates_json=?, contacts_count=?, plan_json=?, approved=?, updated_at=?
            """, (
                session_id,
                campaign_dict.get("workspace_id", "default_workspace"),
                campaign_dict.get("business_name", ""),
                campaign_dict.get("campaign_name", ""),
                campaign_dict.get("business_type", ""),
                json.dumps(campaign_dict.get("config", {})),
                json.dumps(campaign_dict.get("slots", {})),
                campaign_dict.get("selected_tone", ""),
                json.dumps(campaign_dict.get("rejected_tones", [])),
                json.dumps(campaign_dict.get("templates", [])),
                campaign_dict.get("contacts_count", 0),
                json.dumps(campaign_dict.get("plan", {})),
                1 if campaign_dict.get("approved", False) else 0,
                now, now,
                campaign_dict.get("business_name", ""),
                campaign_dict.get("campaign_name", ""),
                campaign_dict.get("business_type", ""),
                json.dumps(campaign_dict.get("config", {})),
                json.dumps(campaign_dict.get("slots", {})),
                campaign_dict.get("selected_tone", ""),
                json.dumps(campaign_dict.get("rejected_tones", [])),
                json.dumps(campaign_dict.get("templates", [])),
                campaign_dict.get("contacts_count", 0),
                json.dumps(campaign_dict.get("plan", {})),
                1 if campaign_dict.get("approved", False) else 0,
                now
            ))
            conn.commit()

    def get_campaign(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.cursor().execute("SELECT * FROM campaigns WHERE session_id = ?", (session_id,)).fetchone()
            if row:
                d = dict(row)
                d["config"] = json.loads(d["config_json"]) if d["config_json"] else {}
                d["slots"] = json.loads(d["slots_json"]) if d["slots_json"] else {}
                d["rejected_tones"] = json.loads(d["rejected_tones_json"]) if d["rejected_tones_json"] else []
                d["templates"] = json.loads(d["templates_json"]) if d["templates_json"] else []
                d["plan"] = json.loads(d["plan_json"]) if d["plan_json"] else {}
                d["approved"] = bool(d["approved"])
                return d
            return None

    def save_contacts(self, session_id: str, contacts: List[Dict[str, Any]]) -> None:
        if not contacts:
            return
        now = utc_now_iso()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(contacts)")
            cols = [col[1] for col in cursor.fetchall()]
            has_masked = "masked_phone" in cols

            if has_masked:
                batch = []
                for c in contacts:
                    c_id = c.get("contact_id") or f"c_{uuid.uuid4().hex[:10]}"
                    c["contact_id"] = c_id
                    extra = json.dumps(c.get("extra", {}))
                    masked = c.get("masked_phone") or c.get("phone", "")
                    batch.append((c_id, session_id, c.get("name", ""), c.get("phone", ""), masked, extra, now))
                cursor.executemany("""
                INSERT OR REPLACE INTO contacts (contact_id, session_id, name, phone, masked_phone, extra_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """, batch)
            else:
                batch = []
                for c in contacts:
                    c_id = c.get("contact_id") or f"c_{uuid.uuid4().hex[:10]}"
                    c["contact_id"] = c_id
                    extra = json.dumps(c.get("extra", {}))
                    batch.append((c_id, session_id, c.get("name", ""), c.get("phone", ""), extra, now))
                cursor.executemany("""
                INSERT OR REPLACE INTO contacts (contact_id, session_id, name, phone, extra_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """, batch)
            conn.commit()

    def get_contacts_by_session(self, session_id: str) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.cursor().execute(
                "SELECT * FROM contacts WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,)
            ).fetchall()
            contacts = []
            for r in rows:
                c = dict(r)
                c["extra"] = json.loads(c["extra_json"]) if c.get("extra_json") else {}
                contacts.append(c)
            return contacts

    # Send jobs & send items (Step 5 At-most-once)
    def create_send_job(self, job_id: str, session_id: str, total_items: int, csrf_token: str = "") -> None:
        now = utc_now_iso()
        with self.get_connection() as conn:
            conn.cursor().execute("""
            INSERT INTO send_jobs (job_id, session_id, status, total_items, successful_items, failed_items, csrf_token, created_at, updated_at)
            VALUES (?, ?, 'pending', ?, 0, 0, ?, ?, ?)
            """, (job_id, session_id, total_items, csrf_token, now, now))
            conn.commit()

    def create_send_items(self, items: List[Dict[str, Any]]) -> None:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for it in items:
                cursor.execute("""
                INSERT INTO send_items (item_id, job_id, contact_id, phone, status, message_sid, error, sent_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    it["item_id"], it["job_id"], it.get("contact_id", ""), it["phone"],
                    it.get("status", "pending"), it.get("message_sid"), it.get("error"), it.get("sent_at")
                ))
            conn.commit()

    def update_send_item_status(self, item_id: str, status: str, message_sid: Optional[str] = None, error: Optional[str] = None, sent_at: Optional[str] = None) -> None:
        with self.get_connection() as conn:
            conn.cursor().execute("""
            UPDATE send_items
            SET status = ?, message_sid = COALESCE(?, message_sid), error = COALESCE(?, error), sent_at = COALESCE(?, sent_at)
            WHERE item_id = ?
            """, (status, message_sid, error, sent_at, item_id))
            conn.commit()

    def update_send_job_status(self, job_id: str, status: str, successful: int = 0, failed: int = 0) -> None:
        now = utc_now_iso()
        with self.get_connection() as conn:
            conn.cursor().execute("""
            UPDATE send_jobs
            SET status = ?, successful_items = ?, failed_items = ?, updated_at = ?
            WHERE job_id = ?
            """, (status, successful, failed, now, job_id))
            conn.commit()

    def get_send_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.cursor().execute("SELECT * FROM send_jobs WHERE job_id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def get_send_items_by_job(self, job_id: str) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            rows = conn.cursor().execute("SELECT * FROM send_items WHERE job_id = ?", (job_id,)).fetchall()
            return [dict(r) for r in rows]

    def get_send_job_by_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            row = conn.cursor().execute(
                "SELECT * FROM send_jobs WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
                (session_id,)
            ).fetchone()
            return dict(row) if row else None

    def log_channel_call(self, *args, **kwargs):
        call_id = f"cc_{uuid.uuid4().hex[:12]}"
        now = utc_now_iso()
        session_id = kwargs.get("session_id") or ""
        job_id = kwargs.get("job_id") or kwargs.get("run_id") or ""
        item_id = kwargs.get("item_id") or ""
        tool_name = kwargs.get("tool_name") or ""
        adapter = kwargs.get("adapter") or kwargs.get("tool_name") or (args[0] if len(args) > 0 else "aggregator")
        operation = kwargs.get("operation") or (args[1] if len(args) > 1 else "send_message")
        status_code = kwargs.get("status_code") or (args[2] if len(args) > 2 else 200)
        duration_ms = kwargs.get("duration_ms") or kwargs.get("latency_ms") or (args[3] if len(args) > 3 else 0)
        payload_redacted = kwargs.get("payload_redacted") or kwargs.get("redacted_request_body") or (args[4] if len(args) > 4 else "")
        response_redacted = kwargs.get("response_redacted") or kwargs.get("redacted_response_body") or (args[5] if len(args) > 5 else "")
        error = kwargs.get("error") or (args[6] if len(args) > 6 else None)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(channel_calls)")
            col_info = {col[1]: col[2] for col in cursor.fetchall()}
            
            data_map = {
                "id": call_id,
                "session_id": session_id or "system",
                "job_id": job_id,
                "run_id": job_id,
                "item_id": item_id,
                "tool_name": tool_name or str(adapter),
                "adapter": str(adapter),
                "operation": str(operation),
                "request_url": kwargs.get("request_url") or f"http://provider/{operation}",
                "method": kwargs.get("method") or "POST",
                "status_code": int(status_code or 0),
                "duration_ms": int(duration_ms or 0),
                "latency_ms": int(duration_ms or 0),
                "payload_redacted": str(payload_redacted or ""),
                "redacted_request_body": str(payload_redacted or ""),
                "response_redacted": str(response_redacted or ""),
                "redacted_response_body": str(response_redacted or ""),
                "error": error,
                "timestamp": now,
                "created_at": now
            }
            
            if "INT" in col_info.get("id", "").upper():
                del data_map["id"]
                
            cols_to_insert = [c for c in data_map.keys() if c in col_info]
            vals_to_insert = [data_map[c] for c in cols_to_insert]
            placeholders = ", ".join(["?"] * len(cols_to_insert))
            col_names_str = ", ".join(cols_to_insert)
            
            cursor.execute(f"INSERT INTO channel_calls ({col_names_str}) VALUES ({placeholders})", vals_to_insert)
            if "id" not in data_map and cursor.lastrowid:
                call_id = f"cc_{cursor.lastrowid}"
            conn.commit()
        self._append_jsonl("channel_calls.jsonl", {
            "id": call_id,
            "session_id": session_id,
            "job_id": job_id,
            "item_id": item_id,
            "tool_name": tool_name,
            "adapter": str(adapter),
            "operation": str(operation),
            "status_code": status_code,
            "duration_ms": duration_ms,
            "payload_redacted": payload_redacted,
            "response_redacted": response_redacted,
            "error": error,
            "timestamp": now
        })

    def get_channel_calls(self, session_id: Optional[str] = None, tool_name: Optional[str] = None, status_code: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
        query = "SELECT * FROM channel_calls WHERE 1=1"
        params: List[Any] = []
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if tool_name:
            query += " AND tool_name = ?"
            params.append(tool_name)
        if status_code is not None:
            query += " AND status_code = ?"
            params.append(status_code)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.get_connection() as conn:
            rows = conn.cursor().execute(query, params).fetchall()
            return [dict(r) for r in rows]

    # Phone Configurations (Multi-Aggregator Management)
    def save_phone_configuration(self, phone: str, provider_id: str,
                                 endpoint_url: Optional[str] = None,
                                 config: Optional[Dict[str, Any]] = None,
                                 status: str = "unverified",
                                 capabilities: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
        now = utc_now_iso()
        cfg_str = json.dumps(config or {})
        caps_str = json.dumps(capabilities or {"sms": True, "mms": False, "delivery_receipts": True})
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO phone_configurations (phone, provider_id, endpoint_url, config_json, status, capabilities_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                provider_id=?,
                endpoint_url=?,
                config_json=?,
                status=?,
                capabilities_json=?,
                updated_at=?
            """, (phone, provider_id, endpoint_url, cfg_str, status, caps_str, now, now,
                  provider_id, endpoint_url, cfg_str, status, caps_str, now))
            conn.commit()
        return self.get_phone_configuration(phone) or {}

    def get_phone_configuration(self, phone: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            row = cursor.execute("SELECT * FROM phone_configurations WHERE phone = ?", (phone,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["config"] = json.loads(d.get("config_json") or "{}")
            d["capabilities"] = json.loads(d.get("capabilities_json") or "{}")
            return d

    def list_phone_configurations(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            rows = cursor.execute("SELECT * FROM phone_configurations ORDER BY created_at ASC").fetchall()
            res = []
            for r in rows:
                d = dict(r)
                d["config"] = json.loads(d.get("config_json") or "{}")
                d["capabilities"] = json.loads(d.get("capabilities_json") or "{}")
                res.append(d)
            return res

    def delete_phone_configuration(self, phone: str) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM phone_messages WHERE phone = ?", (phone,))
            cursor.execute("DELETE FROM phone_configurations WHERE phone = ?", (phone,))
            conn.commit()
            return cursor.rowcount > 0

    # Phone Messages (Two-way texting conversation thread)
    def save_phone_message(self, msg_id: str, phone: str, direction: str, body: str,
                           media_urls: Optional[List[str]] = None,
                           status: str = "queued",
                           provider_id: Optional[str] = None,
                           provider_message_id: Optional[str] = None,
                           error: Optional[str] = None,
                           timestamp: Optional[str] = None) -> Dict[str, Any]:
        now = timestamp or utc_now_iso()
        media_str = json.dumps(media_urls or [])
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO phone_messages (id, phone, direction, body, media_urls_json, status, provider_id, provider_message_id, error, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status=COALESCE(?, status),
                error=COALESCE(?, error),
                provider_message_id=COALESCE(?, provider_message_id)
            """, (msg_id, phone, direction, body, media_str, status, provider_id, provider_message_id, error, now,
                  status, error, provider_message_id))
            conn.commit()
        return {
            "id": msg_id,
            "phone": phone,
            "direction": direction,
            "body": body,
            "media_urls": media_urls or [],
            "status": status,
            "provider_id": provider_id,
            "provider_message_id": provider_message_id,
            "error": error,
            "timestamp": now
        }

    def get_phone_messages(self, phone: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            rows = cursor.execute("""
            SELECT * FROM phone_messages
            WHERE phone = ?
            ORDER BY timestamp ASC
            LIMIT ?
            """, (phone, limit)).fetchall()
            res = []
            for r in rows:
                d = dict(r)
                d["media_urls"] = json.loads(d.get("media_urls_json") or "[]")
                res.append(d)
            return res

    def update_phone_message_status(self, provider_message_id: str, status: str, error: Optional[str] = None) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE phone_messages
            SET status = ?, error = ?
            WHERE provider_message_id = ?
            """, (status, error, provider_message_id))
            conn.commit()
            return cursor.rowcount > 0

    def find_or_create_contact_for_inbound(self, from_phone: str, to_phone: str, body: str) -> Dict[str, Any]:
        """
        Guarantees that every inbound message attaches to a valid contact record in CRM.
        If unrecognized, automatically creates a new contact rather than dropping.
        """
        import uuid
        with self.get_connection() as conn:
            cursor = conn.cursor()
            row = cursor.execute("SELECT * FROM contacts WHERE phone = ?", (from_phone,)).fetchone()
            if row:
                d = dict(row)
                d["extra"] = json.loads(d.get("extra_json") or "{}")
                return d

            # Create new contact for unrecognized number
            contact_id = f"cnt_{uuid.uuid4().hex[:8]}"
            name = f"Sender {from_phone[-4:] if len(from_phone) >= 4 else from_phone}"
            extra = {"source": "inbound_sms", "first_message": body[:80], "to_phone": to_phone}
            now = utc_now_iso()
            cursor.execute("PRAGMA table_info(contacts)")
            cols = [col[1] for col in cursor.fetchall()]
            has_masked = "masked_phone" in cols
            masked = f"{from_phone[:3]}••••••{from_phone[-4:]}" if len(from_phone) > 6 else from_phone
            if has_masked:
                cursor.execute("""
                INSERT INTO contacts (contact_id, session_id, name, phone, masked_phone, extra_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (contact_id, "inbound_webhooks", name, from_phone, masked, json.dumps(extra), now))
            else:
                cursor.execute("""
                INSERT INTO contacts (contact_id, session_id, name, phone, extra_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """, (contact_id, "inbound_webhooks", name, from_phone, json.dumps(extra), now))
            conn.commit()
            return {
                "contact_id": contact_id,
                "session_id": "inbound_webhooks",
                "name": name,
                "phone": from_phone,
                "masked_phone": masked,
                "extra": extra,
                "created_at": now
            }

    # Session Deletion & User Actions
    def delete_session(self, session_id: str) -> bool:
        """Deletes a session and its associated memory, actions, campaigns, and jobs."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM user_actions WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM validations WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM memory_ops WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM errors WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM campaigns WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM contacts WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM send_items WHERE job_id IN (SELECT job_id FROM send_jobs WHERE session_id = ?)", (session_id,))
            cursor.execute("DELETE FROM send_jobs WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM channel_calls WHERE session_id = ?", (session_id,))
            cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
            return cursor.rowcount > 0

    def get_user_actions(self, session_id: str) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            rows = cursor.execute(
                "SELECT * FROM user_actions WHERE session_id = ? ORDER BY id ASC",
                (session_id,)
            ).fetchall()
            res = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d.get("payload_json") or "{}")
                res.append(d)
            return res

    # Tool Webhooks Management
    def save_tool_webhook(self, webhook_id: str, name: str, url: str,
                          headers: Optional[Dict[str, str]] = None,
                          enabled: bool = True,
                          last_status: str = "untested",
                          last_status_code: Optional[int] = None) -> Dict[str, Any]:
        now = utc_now_iso()
        hdr_str = json.dumps(headers or {})
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO tool_webhooks (id, name, url, headers_json, enabled, last_status, last_status_code, last_tested_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=?,
                url=?,
                headers_json=?,
                enabled=?,
                last_status=?,
                last_status_code=?,
                updated_at=?
            """, (webhook_id, name, url, hdr_str, 1 if enabled else 0, last_status, last_status_code, now, now, now,
                  name, url, hdr_str, 1 if enabled else 0, last_status, last_status_code, now))
            conn.commit()
        return self.get_tool_webhook(webhook_id) or {}

    def get_tool_webhook(self, webhook_id: str) -> Optional[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            row = cursor.execute("SELECT * FROM tool_webhooks WHERE id = ?", (webhook_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["headers"] = json.loads(d.get("headers_json") or "{}")
            d["enabled"] = bool(d.get("enabled", 1))
            return d

    def list_tool_webhooks(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            rows = cursor.execute("SELECT * FROM tool_webhooks ORDER BY created_at ASC").fetchall()
            res = []
            for r in rows:
                d = dict(r)
                d["headers"] = json.loads(d.get("headers_json") or "{}")
                d["enabled"] = bool(d.get("enabled", 1))
                res.append(d)
            return res

    def delete_tool_webhook(self, webhook_id: str) -> bool:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM tool_webhooks WHERE id = ?", (webhook_id,))
            conn.commit()
            return cursor.rowcount > 0

    def update_tool_webhook_status(self, webhook_id: str, status: str, status_code: Optional[int] = None) -> bool:
        now = utc_now_iso()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE tool_webhooks
            SET last_status = ?, last_status_code = ?, last_tested_at = ?, updated_at = ?
            WHERE id = ?
            """, (status, status_code, now, now, webhook_id))
            conn.commit()
            return cursor.rowcount > 0

# Singleton storage getter
_storage: Optional[Storage] = None

def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
    return _storage
