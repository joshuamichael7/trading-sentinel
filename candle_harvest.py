#!/usr/bin/env python3
"""
Harvest full hourly OHLCV for the meme cohort, so the "lottery ticket" portfolio
question can actually be backtested instead of argued about.

WHY THIS EXISTS
survivor_probe.py already answered the gating question: GeckoTerminal returned
candles for 154 of 154 tokens, including ones that fell 95%+ and ones whose pools
are now near-empty. The provider does NOT delete corpses, so a backtest built on
it is not automatically survivor-selected. That was the precondition. But the
probe only stored SUMMARY fields (first/last candle), which cannot express a
path-dependent rule -- and a stop-loss is nothing but path dependence. This
stores the arrays.

WHAT IT DOES NOT FIX
The cohort is still a PROMOTED-token sample: these are tokens that trended. It is
not a sample of all launches. Anything measured here describes "coins that got
noticed", which is the universe a human actually trades, but it is a different and
much kinder universe than "coins that existed". That bias is upward and it is
declared here rather than discovered later.

Self-limiting: resumable across cycles, hard per-cycle time budget, becomes a
no-op once every pool has been harvested.
"""
import json, os, time, urllib.request, urllib.error

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PROBE = os.path.join(D, "survivor_probe.json")
OUT = os.path.join(D, "candles.jsonl")
STATE = os.path.join(D, "candle_harvest_state.json")
GT = "https://api.geckoterminal.com/api/v2"
UA = {"User-Agent": "trading-sentinel/1.0 (research probe)"}
BUDGET_S = 50
START = time.time()


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries - 1:
                time.sleep(8 * (i + 1)); continue
            return e.code, None
        except Exception:
            if i == tries - 1:
                return 0, None
            time.sleep(2 * (i + 1))
    return 0, None


def main():
    if not os.path.exists(PROBE):
        print("candle_harvest: no survivor_probe.json yet, skipping"); return
    probe = json.load(open(PROBE)).get("results", {})

    done = set()
    if os.path.exists(STATE):
        try: done = set(json.load(open(STATE)).get("done", []))
        except Exception: done = set()

    todo = [(a, r) for a, r in probe.items()
            if a not in done and r.get("pool") and r.get("n_candles")]
    if not todo:
        print("candle_harvest: complete (%d pools harvested)" % len(done)); return

    fh = open(OUT, "a")
    n = 0
    for addr, r in todo:
        if time.time() - START > BUDGET_S:
            break
        # 1000 hourly candles ~= 41 days, which outruns the life of almost every
        # one of these pools. Where it does not, the truncation is at the OLD end,
        # so entry prices could be missing -- those get dropped at analysis time
        # rather than silently entered at a wrong price.
        code, o = get(f"{GT}/networks/solana/pools/{r['pool']}/ohlcv/hour?aggregate=1&limit=1000")
        lst = (((o or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        # GeckoTerminal returns [ts, o, h, l, c, v], newest first.
        lst = sorted(lst, key=lambda x: x[0])
        fh.write(json.dumps({
            "address": addr,
            "pool": r["pool"],
            "http": code,
            "first_seen": r.get("first_seen"),
            "desc": (r.get("desc") or "")[:120],
            "live_liquidity_usd": r.get("live_liquidity_usd"),
            "ohlcv": [[int(c[0])] + [float(x) if x is not None else None for x in c[1:]] for c in lst],
        }) + "\n")
        done.add(addr); n += 1
        time.sleep(2.2)          # GeckoTerminal free tier: ~30 calls/min
    fh.close()
    json.dump({"done": sorted(done), "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
              open(STATE, "w"))
    print("candle_harvest: +%d pools this cycle, %d/%d done" % (n, len(done), len(probe)))


if __name__ == "__main__":
    main()
