#!/usr/bin/env python3
"""COPY-PERP-01 step 0: what does Hyperliquid's public API actually expose?

Why this file exists at all
---------------------------
The established-coin version of the copy-trading question needs per-account
positions on deep markets. Two candidate sources were checked before writing
any code:

  * Dune. Their Hyperliquid community table is `hyperliquid.market_data`, whose
    columns are time, coin, funding, open_interest, prev_day_px, day_ntl_vlm,
    premium, oracle_px, mark_px, mid_px, impact_bid_px, impact_ask_px. That is
    exchange-wide aggregate. There is not one wallet address in it. The Dune
    path is dead for this question and no credits should be spent on it.

  * Hyperliquid's own info endpoint. Documented request types include
    userFills / userFillsByTime (per-address trade history), clearinghouseState
    (open positions), portfolio (account value and PnL over time), vaultDetails
    and userVaultEquities. Free, no key, no credits.

So the data probably exists. "Probably" is not good enough to design a study
on, hence this probe: POST each request type we would depend on, record the
status and the actual response shape, and commit the answer. Nothing here
computes a result or ranks an account. It establishes what is fetchable.

Why it runs on the Actions runner and not in the research container
-------------------------------------------------------------------
api.hyperliquid.xyz returns 000 through the container's egress proxy. The
runner reaches the open internet (it already fetches GeckoTerminal). Same
arrangement as survivor_probe.py.

The constraint that shapes the whole study
-------------------------------------------
userFillsByTime serves at most the 10,000 most recent fills per address, and
at most 2,000 per response. For an active account 10,000 fills may span only
days. That means account history here is ROLLING: it is not a database we can
query backwards at leisure, it is a window that closes.

That is a real limitation and it is also, accidentally, the strongest
methodological property available. Selection window A has to be captured now
and measurement window B captured later, because B does not exist yet. There
is no way to peek at the outcome while choosing the cohort. The
survivorship/lookahead failure that killed XSEC-MOM-01, and that every
retrospective screen has to be defended against, is structurally impossible
here as long as collection starts before selection.

Self-limiting: writes data/hl_probe.json, and is a no-op once every request
type has a recorded answer.
"""
import json, os, time, urllib.request, urllib.error

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
OUT = os.path.join(D, "hl_probe.json")
API = "https://api.hyperliquid.xyz/info"
HDR = {"Content-Type": "application/json",
       "User-Agent": "trading-sentinel/1.0 (research probe)"}
BUDGET_S = 40
START = time.time()

# HLP, the protocol's own market-making vault. Used purely as a known-good
# address to test the per-account request types against -- it is public,
# it is not a trading candidate, and nothing about it is being evaluated.
HLP = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"

PROBES = [
    ("meta",                  {"type": "meta"}),
    ("metaAndAssetCtxs",      {"type": "metaAndAssetCtxs"}),
    # Can we enumerate vaults at all? Step 1 of the study depends on it:
    # vaults ARE copy trading with real money and published depositor results,
    # so if depositors do not beat holding the coin, the premise is refuted
    # before a single account is screened. Neither of these is in the public
    # docs; both are probed rather than assumed.
    ("vaultSummaries",        {"type": "vaultSummaries"}),
    ("leaderboard",           {"type": "leaderboard"}),
    ("vaultDetails",          {"type": "vaultDetails", "vaultAddress": HLP}),
    # The per-account types the study would actually run on.
    ("clearinghouseState",    {"type": "clearinghouseState", "user": HLP}),
    ("portfolio",             {"type": "portfolio", "user": HLP}),
    ("userFills",             {"type": "userFills", "user": HLP,
                               "aggregateByTime": False}),
]


def shape(o, depth=0):
    """Describe a response without dumping it. Keys and counts, not contents."""
    if depth > 2:
        return "..."
    if isinstance(o, dict):
        return {k: shape(v, depth + 1) for k, v in list(o.items())[:12]}
    if isinstance(o, list):
        return [f"list[{len(o)}]"] + ([shape(o[0], depth + 1)] if o else [])
    if isinstance(o, str):
        return f"str[{len(o)}]"
    return type(o).__name__


def post(body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(API, data=data, headers=HDR, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode()
            return {"http": r.status, "bytes": len(raw), "body": json.loads(raw)}
    except urllib.error.HTTPError as e:
        # A 422 here is a RESULT: it means the request type is not served.
        # Distinguish it from a transport failure, which is not an answer --
        # the same distinction that poisoned a third of the survivorship probe
        # before it was fixed.
        try:
            detail = e.read().decode()[:300]
        except Exception:
            detail = ""
        return {"http": e.code, "bytes": 0, "body": None, "detail": detail}
    except Exception as e:
        return {"http": "transport_error", "err": f"{type(e).__name__}: {e}"}


def main():
    state = {}
    if os.path.exists(OUT):
        try:
            state = json.load(open(OUT))
        except Exception:
            state = {}
    res = state.setdefault("results", {})

    def unresolved(name):
        r = res.get(name)
        if r is None:
            return True
        return r.get("http") == "transport_error" and r.get("attempts", 1) < 4

    todo = [(n, b) for n, b in PROBES if unresolved(n)]
    if not todo:
        print(f"hl_probe: complete, {len(res)}/{len(PROBES)} request types resolved")
        return

    for name, body in todo:
        if time.time() - START > BUDGET_S:
            break
        prior = res.get(name, {}).get("attempts", 0)
        r = post(body)
        rec = {"http": r.get("http"), "bytes": r.get("bytes"),
               "attempts": prior + 1, "request": body}
        if r.get("body") is not None:
            rec["shape"] = shape(r["body"])
            # For the enumeration probes, the count is the whole point.
            if isinstance(r["body"], list):
                rec["n_items"] = len(r["body"])
        if r.get("detail"):
            rec["detail"] = r["detail"]
        if r.get("err"):
            rec["err"] = r["err"]
        res[name] = rec
        print(f"hl_probe {name}: http={rec['http']} bytes={rec.get('bytes')} "
              f"n={rec.get('n_items')}")
        time.sleep(1.2)          # 1200 weight/min shared; this is far under

    state["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["resolved"] = sum(1 for n, _ in PROBES if not unresolved(n))
    state["total"] = len(PROBES)
    state["note"] = ("Capability probe only. No account is ranked, scored or "
                     "selected here. See COPY-PERP-01 in the project ledger "
                     "for the pre-registered kill criteria.")
    json.dump(state, open(OUT, "w"), indent=1)
    print(f"hl_probe: {state['resolved']}/{state['total']} resolved")


if __name__ == "__main__":
    main()
