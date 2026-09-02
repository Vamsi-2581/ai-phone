#!/usr/bin/env python3
"""p50/p95 per stage over the last N turns of a turns.jsonl; prints the Milestone 1 gate verdict."""
import json
import sys

path = sys.argv[1]
last = int(sys.argv[sys.argv.index("--last") + 1]) if "--last" in sys.argv else None
rows = [json.loads(l) for l in open(path) if l.strip()]
rows = rows[-last:] if last else rows


def q(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(round((p / 100) * (len(v) - 1))))]


print(f"turns: {len(rows)}   bargeins: {sum(1 for r in rows if r.get('bargein'))}")
for k in ("stt_ms", "llm_prompt_eval_ms", "llm_first_clause_ms", "tts_first_chunk_ms", "egress_ms", "first_audible_ms"):
    v = [r[k] for r in rows if r.get(k) is not None]
    if v:
        print(f"{k:22s} p50={q(v, 50):7.1f}  p95={q(v, 95):7.1f}  n={len(v)}")
fa = [r["first_audible_ms"] for r in rows if r.get("first_audible_ms") is not None]
bs = [r["bargein_stop_ms"] for r in rows if r.get("bargein_stop_ms") is not None]
ok = bool(fa) and q(fa, 50) < 1000.0 and (not bs or q(bs, 50) <= 300.0)
print(f"under/overruns: tx={sum(r.get('tx_underruns', 0) for r in rows)} rx={sum(r.get('rx_overruns', 0) for r in rows)}")
print("GATE:", "PASS" if ok else "FAIL", "(p50 first_audible < 1000 ms; barge-in stop p50 <= 300 ms)")
