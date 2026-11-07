"""
Capture snapshots of CRM Orchestrator v2 Chat UI and Agent Observatory views using headless Chromium.
"""

import subprocess
import time
import os

SNAPSHOT_DIR = "reports/snapshots"
os.makedirs(SNAPSHOT_DIR, exist_ok=True)

TARGETS = [
    ("obs_traces.png", "http://localhost:8080/observatory", 1440, 900),
    ("obs_banned.png", "http://localhost:8080/observatory#banned", 1440, 900),
    ("obs_orgs.png", "http://localhost:8080/observatory#orgs", 1440, 900),
    ("obs_quarantine.png", "http://localhost:8080/observatory#quarantine", 1440, 900),
    ("obs_inspector.png", "http://localhost:8080/observatory#inspect", 1440, 900),
    ("chat_phishing_refusal.png", "http://localhost:8080/#demo_phishing_refusal", 1200, 850),
    ("chat_review_card.png", "http://localhost:8080/#demo_review_card", 1200, 850),
    ("chat_loop_failure.png", "http://localhost:8080/#demo_loop_failure", 1200, 850),
]

def capture_all():
    print(f"Capturing {len(TARGETS)} snapshots using /usr/bin/chromium...")
    for filename, url, width, height in TARGETS:
        out_path = os.path.join(SNAPSHOT_DIR, filename)
        cmd = [
            "/usr/bin/chromium",
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            f"--window-size={width},{height}",
            f"--screenshot={out_path}",
            url
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            sz = os.path.getsize(out_path)
            print(f"✓ Saved {filename} ({sz // 1024} KB)")
        except Exception as e:
            print(f"✗ Failed {filename}: {e}")

if __name__ == "__main__":
    capture_all()
