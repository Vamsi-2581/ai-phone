#!/usr/bin/env python3
"""p50/p95 per stage over the last N turns of a turns.jsonl; prints the Milestone 1 gate verdict."""
import json
import sys

path = sys.argv[1]
last = int(sys.argv[sys.argv.index("--last") + 1]) if "--last" in sys.argv else None
expect = int(sys.argv[sys.argv.index("--expect") + 1]) if "--expect" in sys.argv else None
rows = [json.loads(l) for l in open(path) if l.strip()]
rows = rows[-last:] if last else rows
incomplete = [r for r in rows if r.get("incomplete")]
rows = [r for r in rows if not r.get("incomplete")]


def q(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(round((p / 100) * (len(v) - 1))))]


print(f"turns: {len(rows)}   incomplete (caller hung up mid-turn): {len(incomplete)}   bargeins: {sum(1 for r in rows if r.get('bargein'))}")
for r in incomplete:
    print(f"  incomplete: state={r.get('state_at_end')} stt_ms={r.get('stt_ms')} stt_text={r.get('stt_text')!r}")
for k in ("endpoint_ms", "stt_ms", "llm_prompt_eval_ms", "llm_first_clause_ms", "tts_first_chunk_ms", "egress_ms", "first_audible_ms"):
    v = [r[k] for r in rows if r.get(k) is not None]
    if v:
        print(f"{k:22s} p50={q(v, 50):7.1f}  p95={q(v, 95):7.1f}  n={len(v)}")
fa = [r["first_audible_ms"] for r in rows if r.get("first_audible_ms") is not None]
bs = [r["bargein_stop_ms"] for r in rows if r.get("bargein_stop_ms") is not None]
ok = bool(fa) and q(fa, 50) < 1000.0 and (not bs or q(bs, 95) <= 300.0)
if expect is not None and len(rows) < expect:
    print(f"only {len(rows)} complete turns, expected {expect}")
    ok = False
print(f"under/overruns: tx={sum(r.get('tx_underruns', 0) for r in rows)} rx={sum(r.get('rx_overruns', 0) for r in rows)}")
print("GATE:", "PASS" if ok else "FAIL", "(p50 first_audible < 1000 ms; barge-in stop p95 <= 300 ms" + (f"; {expect} complete turns" if expect else "") + ")")
sys.exit(0 if ok else 1)
