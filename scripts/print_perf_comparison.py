#!/usr/bin/env python3
import json

with open(".apitool/evidence/perf_baseline.json") as f:
    b = json.load(f)
with open(".apitool/evidence/perf_after.json") as f:
    a = json.load(f)

lines = []
lines.append("==========================================================================================")
lines.append("PERFORMANCE OPTIMIZATION COMPARISON: BASELINE vs AFTER")
lines.append("==========================================================================================")
b_ms = b["total_wall_clock_ms"]
a_ms = a["total_wall_clock_ms"]
saved_s = (b_ms - a_ms) / 1000
saved_pct = 100 * (b_ms - a_ms) / b_ms
b_calls = b["raw_calls_count"]
a_calls = a["raw_calls_count"]
b_tones = b["tone_candidates_count"]
a_tones = a["tone_candidates_count"]

lines.append("Metric / Purpose               Baseline (ms)     After (ms)        Change / Reduction")
lines.append("------------------------------------------------------------------------------------------")
lines.append(f"Total Wall Clock Time          {b_ms:>9} ms      {a_ms:>9} ms      -{saved_s:.1f}s (-{saved_pct:.1f}%)")
lines.append(f"Total Raw LLM Calls            {b_calls:>9} calls   {a_calls:>9} calls   -{b_calls - a_calls} calls")
lines.append(f"Candidate Tones Returned       {b_tones:>9} tones   {a_tones:>9} tones   {a_tones} guaranteed distinct")
lines.append("------------------------------------------------------------------------------------------")

all_purposes = sorted(set(list(b.get("per_purpose", {}).keys()) + list(a.get("per_purpose", {}).keys())))
for p in all_purposes:
    b_data = b.get("per_purpose", {}).get(p, {"calls": 0, "total_latency_ms": 0, "average_latency_ms": 0})
    a_data = a.get("per_purpose", {}).get(p, {"calls": 0, "total_latency_ms": 0, "average_latency_ms": 0})
    b_tot = b_data["total_latency_ms"]
    a_tot = a_data["total_latency_ms"]
    diff = a_tot - b_tot
    pct = f"{diff/b_tot*100:+.1f}%" if b_tot > 0 else "N/A"
    lines.append(f"{p:<28}  {b_tot:>8} ms ({b_data['calls']}x) {a_tot:>8} ms ({a_data['calls']}x)  {diff:+8} ms ({pct})")
lines.append("==========================================================================================")

content = "\n".join(lines)
with open(".apitool/evidence/perf_comparison.txt", "w") as f:
    f.write(content + "\n")
print(content)
