#!/usr/bin/env python3
"""
Survivorship probe for the meme-coin question.

The premise being tested is NOT "is this token good". It is: do the free data
providers still serve price history for tokens that have since died?

If a provider returns candles for the tokens that survived and 404s the ones
that went to zero, then any backtest built on it will manufacture a profitable
strategy out of nothing -- the exact failure that killed XSEC-MOM-01, where the
benchmark collapsed once the delisted corpses were put back in the basket.
That question has to be answered before a single line of strategy code.

The cohort is data/meta_watch.jsonl: every trending token this sentinel logged
since 2026-07-16, with its first-seen timestamp. It is a promoted-token sample,
not a launch sample -- that is a real bias and it is recorded, not hidden. What
matters here is that it was written down BEFORE the outcome was known, so
nothing in it is survivor-selected by us.

Secondary payoff: meta_watch only started recording prices on 2026-07-30. If
GeckoTerminal serves history back to pool creation, this back-fills entry
prices for the whole cohort and makes the missed-opportunity ledger scorable
for the first time.

Self-limiting: writes data/survivor_probe.json, resumes across runs, and turns
into a no-op once every token has been probed.
"""
import json, os, time, urllib.request, urllib.error

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
OUT = os.path.join(D, "survivor_probe.json")
BUDGET_S = 55                      # one cycle's share; the loop runs every 5 min
GT = "https://api.geckoterminal.com/api/v2"
UA = {"User-Agent": "trading-sentinel/1.0 (research probe)"}
START = time.time()


def get(url, tries=2):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            # 404 is a RESULT here, not a failure -- it is the whole point of
            # the probe. Distinguish it from transport errors.
            if e.code == 404:
                return {"__http__": 404}
            if e.code == 429:
                time.sleep(4)
                continue
            return {"__http__": e.code}
        except Exception:
            time.sleep(1.5)
    return None


def cohort():
    """address -> first timestamp this sentinel ever saw it."""
    first = {}
    path = os.path.join(D, "meta_watch.jsonl")
    if not os.path.exists(path):
        return first
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            for t in row.get("trending") or []:
                a, ch = t.get("address"), t.get("chain")
                if a and ch == "solana" and a not in first:
                    first[a] = {"first_seen": row["ts"], "desc": (t.get("desc") or "")[:80]}
    return first


def probe(addr):
    """-> dict describing what each provider knows about this token now."""
    r = {"pools_http": None, "n_pools": 0, "pool": None, "live_liquidity_usd": None,
         "ohlcv_http": None, "n_candles": 0, "first_candle_ts": None,
         "first_candle_close": None, "last_candle_ts": None, "last_candle_close": None}

    d = get(f"{GT}/networks/solana/tokens/{addr}/pools")
    if d is None:
        r["pools_http"] = "transport_error"
        return r
    if "__http__" in d:
        r["pools_http"] = d["__http__"]
        return r
    r["pools_http"] = 200
    pools = d.get("data") or []
    r["n_pools"] = len(pools)
    if not pools:
        return r                       # provider knows the token, has no market for it

    # deepest pool; thin pools quote nonsense
    def liq(p):
        try:
            return float((p.get("attributes") or {}).get("reserve_in_usd") or 0)
        except Exception:
            return 0.0
    best = max(pools, key=liq)
    r["pool"] = (best.get("attributes") or {}).get("address")
    r["live_liquidity_usd"] = liq(best)

    time.sleep(2.2)                    # free tier is ~30 req/min, stay well under
    o = get(f"{GT}/networks/solana/pools/{r['pool']}/ohlcv/hour?aggregate=1&limit=1000")
    if o is None:
        r["ohlcv_http"] = "transport_error"
        return r
    if "__http__" in o:
        r["ohlcv_http"] = o["__http__"]
        return r
    r["ohlcv_http"] = 200
    lst = ((o.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    r["n_candles"] = len(lst)
    if lst:
        lst = sorted(lst, key=lambda c: c[0])          # [ts, o, h, l, c, v]
        r["first_candle_ts"], r["first_candle_close"] = lst[0][0], lst[0][4]
        r["last_candle_ts"], r["last_candle_close"] = lst[-1][0], lst[-1][4]
    return r


def main():
    state = {}
    if os.path.exists(OUT):
        try:
            state = json.load(open(OUT))
        except Exception:
            state = {}
    results = state.setdefault("results", {})
    coh = cohort()
    state["cohort_size"] = len(coh)

    todo = [a for a in coh if a not in results]
    if not todo:
        print(f"survivor_probe: complete, {len(results)}/{len(coh)} probed, nothing to do")
        return

    n = 0
    for addr in todo:
        if time.time() - START > BUDGET_S:
            break
        r = probe(addr)
        r.update(coh[addr])
        results[addr] = r
        n += 1
        print(f"probe {addr[:8]} pools={r['pools_http']}/{r['n_pools']} "
              f"ohlcv={r['ohlcv_http']}/{r['n_candles']} liq={r['live_liquidity_usd']}")
        time.sleep(2.2)

    state["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["probed"] = len(results)
    state["remaining"] = len(coh) - len(results)
    json.dump(state, open(OUT, "w"), indent=1)
    print(f"survivor_probe: +{n} this run, {len(results)}/{len(coh)} done")


if __name__ == "__main__":
    main()
