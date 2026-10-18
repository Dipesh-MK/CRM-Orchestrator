import json
from app.core.config import get_config
from app.api.dispatch_routes import verify_loopback_headers
from fastapi import Request
from starlette.datastructures import Headers

cfg = get_config()

# Test simulated browser headers for both localhost and 127.0.0.1 on port 8000
browser_cases = [
    {
        "description": "Browser opened at http://localhost:8000",
        "host": "localhost:8000",
        "origin": "http://localhost:8000",
    },
    {
        "description": "Browser opened at http://127.0.0.1:8000",
        "host": "127.0.0.1:8000",
        "origin": "http://127.0.0.1:8000",
    },
    {
        "description": "Browser opened at http://[::1]:8000",
        "host": "[::1]:8000",
        "origin": "http://[::1]:8000",
    },
    {
        "description": "Spoofed/Foreign Host (e.g. attacker.com)",
        "host": "attacker.com",
        "origin": "http://localhost:8000",
    },
    {
        "description": "Spoofed/Foreign Origin (e.g. http://malicious.site)",
        "host": "localhost:8000",
        "origin": "http://malicious.site",
    }
]

results = []
for case in browser_cases:
    passed = False
    error = None
    try:
        # Mock request with headers
        req = Request(scope={
            "type": "http",
            "headers": [
                (b"host", case["host"].encode()),
                (b"origin", case["origin"].encode()),
            ]
        })
        verify_loopback_headers(req, case["host"], case["origin"], cfg)
        passed = True
    except Exception as e:
        error = str(e)
    results.append({
        "case": case["description"],
        "host": case["host"],
        "origin": case["origin"],
        "verified": passed,
        "error": error
    })

output = {
    "config_allowed_hosts": cfg.dispatch.allowed_hosts,
    "config_allowed_origins": cfg.dispatch.allowed_origins,
    "verification_results": results
}

with open(".apitool/evidence/browser_headers_check.txt", "w") as f:
    json.dump(output, f, indent=2)

print("BROWSER HEADERS CHECK COMPLETE. Results written to .apitool/evidence/browser_headers_check.txt")
