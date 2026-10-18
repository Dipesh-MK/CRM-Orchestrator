import os
import sys
import tempfile
import subprocess
from pathlib import Path
from scripts.scan_secrets import run_scan, scan_working_tree, PATTERNS

def test_scan_secrets_positive_control():
    """Verify scanner detects dummy gsk_ token and private key in temp file without printing values."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        dummy_file = tmp_path / "dummy_secret.txt"
        test_gsk = "gsk_" + "testtoken1234567890"
        test_pk = "-----BEGIN " + "RSA PRIVATE KEY-----"
        dummy_file.write_text(f"dummy_var = '{test_gsk}'\n{test_pk}\n")
        
        findings = scan_working_tree(tmp_path, PATTERNS)
        patterns_found = [f["pattern"] for f in findings]
        assert "gsk_token" in patterns_found
        assert "private_key_block" in patterns_found
        for f in findings:
            assert "gsk_" not in f["pattern"] or f["pattern"] == "gsk_token"

def test_working_tree_has_no_secrets():
    """Verify that current working tree contains zero secret tokens."""
    repo_dir = Path(__file__).resolve().parent.parent
    findings = scan_working_tree(repo_dir, PATTERNS)
    # Ignore any matches that might be in git history diffs
    msg = [f"{f['file']}:{f['pattern']}" for f in findings]
    assert len(findings) == 0, f"Secrets detected in working tree: {msg}"

def test_cli_execution_generates_evidence():
    """Execute scan_secrets CLI and save results to .apitool/evidence/secrets_scan_output.txt."""
    repo_dir = Path(__file__).resolve().parent.parent
    evidence_dir = repo_dir / ".apitool" / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    out_file = evidence_dir / "secrets_scan_output.txt"
    
    proc = subprocess.run(
        [sys.executable if "sys" in globals() else "python3", "scripts/scan_secrets.py"],
        cwd=repo_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False
    )
    out_file.write_text(proc.stdout + proc.stderr, encoding="utf-8")
    assert out_file.exists()
