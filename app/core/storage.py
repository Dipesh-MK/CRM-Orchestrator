import sqlite3
import json
import os
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
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
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

            conn.commit()

    def _append_jsonl(self, filename: str, data: Dict[str, Any]):
        file_path = Path(self.logs_dir) / filename
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    # Session methods
    def save_session(self, session_id: str, workspace_id: str, business_type: Optional[str] = None,
                     campaign_name: Optional[str] = None, status: str = "active", meta: Optional[Dict[str, Any]] = None):
        now = utc_now_iso()
        meta_str = json.dumps(meta or {})
        with self.get_connection() as conn:
            cursor = conn.cursor()
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
                d["meta"] = json.loads(d["meta_json"]) if d["meta_json"] else {}
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

# Singleton storage getter
_storage: Optional[Storage] = None

def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
    return _storage
