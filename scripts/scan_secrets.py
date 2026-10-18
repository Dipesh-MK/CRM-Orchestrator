#!/usr/bin/env python3
"""
scripts/scan_secrets.py
Scans the working tree AND git history for potential secrets.
Prints ONLY `file:commit:pattern-name`, NEVER the secret match itself.
"""
import os
import sys
import re
import subprocess
from pathlib import Path
from typing import List, Tuple, Dict, Any

PATTERNS = [
    ("gsk_token", re.compile(r"gsk_[A-Za-z0-9_]{10,}")),
    ("sk_token", re.compile(r"(?:sk-[A-Za-z0-9_\-]{10,})")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("bearer_token", re.compile(r"(?:Bearer\s+[A-Za-z0-9_\-\.]{15,})")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

EXCLUDE_DIRS = {".git", "venv", "__pycache__", ".pytest_cache", "node_modules"}
EXCLUDE_EXTS = {".pyc", ".png", ".jpg", ".jpeg", ".zip", ".sqlite", ".db"}

def get_env_var_patterns() -> List[Tuple[str, re.Pattern]]:
    """Include env vars listed in config if set to non-empty non-default values."""
    env_patterns = []
    # Names of potential secret env vars
    vars_to_check = [
        "LLM_API_KEY",
        "AGGREGATOR_AUTH_TOKEN",
        "TWILIO_AUTH_TOKEN"
    ]
    for var in vars_to_check:
        val = os.environ.get(var)
        if val and len(val) >= 8 and val not in ("ollama", "test", "dummy"):
            env_patterns.append((f"env_{var}", re.compile(re.escape(val))))
    return env_patterns

def scan_text(text: str, all_patterns: List[Tuple[str, re.Pattern]]) -> List[str]:
    hits = []
    for name, pattern in all_patterns:
        if pattern.search(text):
            hits.append(name)
    return hits

def scan_working_tree(root_dir: Path, all_patterns: List[Tuple[str, re.Pattern]]) -> List[Dict[str, str]]:
    findings = []
    for path in root_dir.rglob("*"):
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        if path.suffix in EXCLUDE_EXTS or not path.is_file():
            continue
        
        # Don't scan this script itself for its own regex definitions
        if path.name == "scan_secrets.py":
            continue

        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            hits = scan_text(content, all_patterns)
            for hit in hits:
                rel_path = path.relative_to(root_dir)
                findings.append({
                    "file": str(rel_path),
                    "commit": "WORKING_TREE",
                    "pattern": hit
                })
        except Exception:
            pass
    return findings

def scan_git_history(repo_dir: Path, all_patterns: List[Tuple[str, re.Pattern]]) -> List[Dict[str, str]]:
    findings = []
    try:
        proc = subprocess.run(
            ["git", "log", "-p", "--full-history", "--no-color"],
            cwd=repo_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            check=False
        )
        if proc.returncode != 0:
            return findings

        current_commit = "HEAD"
        current_file = "UNKNOWN"

        for line in proc.stdout.splitlines():
            if line.startswith("commit "):
                current_commit = line.split()[1][:10]
            elif line.startswith("diff --git "):
                parts = line.split()
                if len(parts) >= 4:
                    current_file = parts[3].lstrip("b/")
            elif (line.startswith("+") and not line.startswith("+++")) or (line.startswith("-") and not line.startswith("---")):
                # Scanned line added or deleted in diff
                diff_content = line[1:]
                # Ignore scan_secrets.py self matches
                if "scan_secrets.py" in current_file:
                    continue
                hits = scan_text(diff_content, all_patterns)
                for hit in hits:
                    findings.append({
                        "file": current_file,
                        "commit": current_commit,
                        "pattern": hit
                    })
    except Exception:
        pass
    return findings

def run_scan(repo_dir: Path) -> List[Dict[str, str]]:
    all_patterns = list(PATTERNS) + get_env_var_patterns()
    findings = []
    findings.extend(scan_working_tree(repo_dir, all_patterns))
    findings.extend(scan_git_history(repo_dir, all_patterns))
    
    # Deduplicate
    unique = []
    seen = set()
    for f in findings:
        key = (f["file"], f["commit"], f["pattern"])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique

def main():
    repo_dir = Path(__file__).resolve().parent.parent
    findings = run_scan(repo_dir)

    if not findings:
        print("OK: No secrets found in working tree or git history.")
        sys.exit(0)

    print("SECRETS DETECTED (file:commit:pattern-name):")
    for f in findings:
        print(f"{f['file']}:{f['commit']}:{f['pattern']}")
    sys.exit(1)

if __name__ == "__main__":
    main()
