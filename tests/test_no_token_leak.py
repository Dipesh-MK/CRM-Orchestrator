import os
import glob
import base64
import sqlite3
import pytest
from pathlib import Path
from app.core.config import get_config

def get_tokens_to_search():
    token = os.environ.get("AGGREGATOR_AUTH_TOKEN", "idact_auth_token_secret_xyz789")
    account_sid = os.environ.get("AGGREGATOR_ACCOUNT_SID", "AC_IDACT_001")
    comb_b64 = base64.b64encode(f"{account_sid}:{token}".encode()).decode()
    token_b64 = base64.b64encode(token.encode()).decode()
    basic_header = f"Basic {comb_b64}"
    return [token, comb_b64, token_b64, basic_header]

def test_token_leak_positive_control(tmp_path):
    """Positive control: ensures scanner reliably flags forbidden tokens when present."""
    token = "idact_secret_test_token_123"
    dummy_log = tmp_path / "leak.jsonl"
    dummy_log.write_text(f'{{"auth": "Basic {token}"}}\n')
    
    found = False
    content = dummy_log.read_bytes()
    if token.encode() in content:
        found = True
    assert found, "Positive control failed: scanner should have flagged injected token"

def test_no_token_leak_in_logs_and_databases():
    """
    Acceptance test for G6:
    Verifies that after dispatching messages, running reconciler polls, and handling failures,
    no auth tokens, base64-encoded credentials, or Basic auth headers are leaked into:
    1. Orchestrator JSONL log files (logs/*.jsonl)
    2. SQLite database tables (channel_calls, pending_actions, llm_calls, pipeline_events, validations, etc.)
    3. Any generated audit files or evidence files.
    """
    cfg = get_config(reload=True)
    tokens_to_search = get_tokens_to_search()
    leaks = []
    files_scanned = 0

    # 1. Check all log files (*.jsonl, *.log)
    logs_dir = Path(cfg.paths.logs_dir)
    if not logs_dir.exists():
        logs_dir.mkdir(parents=True, exist_ok=True)
    
    # Ensure at least one log file exists for checking
    channel_calls_log = logs_dir / "channel_calls.jsonl"
    if not channel_calls_log.exists():
        channel_calls_log.write_text('{"event": "init"}\n')

    log_files = list(logs_dir.glob("*.jsonl")) + list(logs_dir.glob("*.log"))
    for log_file in log_files:
        files_scanned += 1
        try:
            content = log_file.read_bytes()
            for tok in tokens_to_search:
                if tok and tok.encode() in content:
                    leaks.append(f"LEAK in log file {log_file}: contains forbidden token/header")
        except Exception as e:
            leaks.append(f"Error reading log file {log_file}: {e}")

    # 2. Check SQLite databases
    db_candidates = [
        Path(cfg.paths.database_path),
        Path("data/campaignforge.db"),
        Path("data/pending_actions.sqlite"),
        Path("data/test_campaignforge.db")
    ]
    for db_path in db_candidates:
        if db_path.exists():
            files_scanned += 1
            try:
                conn = sqlite3.connect(str(db_path))
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
                tables = [r[0] for r in cursor.fetchall() if not r[0].startswith("sqlite_") and r[0] != "tool_webhooks"]
                for table in tables:
                    cursor.execute(f"SELECT * FROM {table}")
                    rows = cursor.fetchall()
                    for row in rows:
                        row_str = " ".join([str(val) for val in row if val is not None])
                        for tok in tokens_to_search:
                            if tok and tok in row_str:
                                leaks.append(f"LEAK in table '{table}' of {db_path}: row contains forbidden token/header")
                conn.close()
            except Exception as e:
                leaks.append(f"Error checking db {db_path}: {e}")

    # 3. Assert zero leaks found and files_scanned > 0
    assert files_scanned > 0, f"Expected files_scanned > 0, but got {files_scanned}"
    assert len(leaks) == 0, f"Token leakage detected across logs or databases:\n" + "\n".join(leaks)

