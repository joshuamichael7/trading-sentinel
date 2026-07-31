#!/usr/bin/env python3
"""COPY-PERP-01 step 1a: build a vault directory Hyperliquid does not publish.

The problem this solves
-----------------------
hl_probe.py settled step 0: every per-address request type we need works, and
`vaultDetails` alone returns leader, apr, leaderCommission and 100 followers
each with pnl / allTimePnl / daysFollowing / vaultEntryTime. That is a
leader-versus-depositor comparison in one response.

What does NOT work is enumeration. `leaderboard` 422s (the request type is not
served in that form) and `vaultSummaries` returns an empty list. So we can read
any address we can name and we cannot obtain the list of addresses.

The snowball
------------
    HLP.followers               -> ~100 real user addresses
    userVaultEquities(address)  -> which OTHER vaults that user holds
    vaultDetails(new vault)     -> its leader, its apr, its 100 followers
    repeat

Each vault teaches us about users; each user teaches us about vaults. Starting
from the one vault address that is public knowledge, the frontier expands.

Three survivorship layers, declared before the first request
------------------------------------------------------------
1. It can only discover vaults that are ALIVE right now. A vault that blew up
   and closed has no followers to crawl and no equity to appear in anyone's
   userVaultEquities. It is not under-weighted, it is invisible.
2. It over-samples vaults whose depositors also hold HLP -- the seed biases the
   frontier toward the conservative, protocol-adjacent end of the vault space.
3. Within a surviving vault, `followers` lists CURRENT depositors. Anyone who
   withdrew at a loss is gone from the record.

All three bias the same direction: up. This is not a flaw to be apologised for
later, it is the reason the step-1 test is worth running at all. If depositors
in this deliberately flattered sample still fail to beat holding the coin, that
is strong evidence, because the sample was built to make them look good. If
they succeed, it means almost nothing and MUST NOT be read as support. The test
is one-directional by construction and is pre-registered as such in
COPY-PERP-01.enumeration_workaround_preregistered.

Nothing here scores, ranks or selects an account. It collects names.

Budget discipline
-----------------
Runs as a side-car off watcher.py every ~5 minutes with a hard wall-clock
budget, resumes from data/hl_crawl.json, and stops on its own once the frontier
is exhausted or the caps are hit. Hyperliquid allows 1200 weight/min per IP;
most info requests weigh 20, so ~60 requests/min is the ceiling. This paces far
under that.
"""
import json, os, time, urllib.request, urllib.error

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
OUT = os.path.join(D, "hl_crawl.json")
API = "https://api.hyperliquid.xyz/info"
HDR = {"Content-Type": "application/json",
       "User-Agent": "trading-sentinel/1.0 (research crawl)"}

BUDGET_S      = 70      # wall clock per invocation
MAX_REQ       = 22      # requests per invocation; ~1 every 3s, far under the cap
MAX_VAULTS    = 400     # stop growing the directory past this
MAX_USERS     = 4000    # frontier cap, so this cannot run away
SEED = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"   # HLP, the only public handle

START = time.time()


def post(body, tries=2):
    """A 422/400 is a RESULT. A transport failure is NOT an answer.

    The survivorship probe was poisoned for a third of its run by conflating
    those two, so they stay separated everywhere in this project.
    """
    for attempt in range(tries):
        req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                     headers=HDR, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode()), None
        except urllib.error.HTTPError as e:
            return None, f"http_{e.code}"
        except Exception as e:
            if attempt + 1 == tries:
                return None, f"transport_{type(e).__name__}"
            time.sleep(2)
    return None, "transport_exhausted"


def load():
    if os.path.exists(OUT):
        try:
            return json.load(open(OUT))
        except Exception:
            pass
    return {"vaults": {},          # vault addr -> summary record
            "users_pending": [],   # users whose vault holdings we have not read
            "users_done": [],
            "vaults_pending": [SEED],
            "errors": {},
            "requests_total": 0,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def main():
    st = load()
    if st.get("complete"):
        print(f"hl_crawl: complete, {len(st['vaults'])} vaults known")
        return

    done_u = set(st["users_done"])
    pend_u = st["users_pending"]
    vaults = st["vaults"]
    pend_v = st["vaults_pending"]
    n = 0

    def budget_left():
        return n < MAX_REQ and (time.time() - START) < BUDGET_S

    # Phase A: expand known vaults into their follower lists.
    while pend_v and budget_left() and len(vaults) < MAX_VAULTS:
        v = pend_v.pop(0)
        if v in vaults:
            continue
        body, err = post({"type": "vaultDetails", "vaultAddress": v})
        n += 1
        st["requests_total"] += 1
        time.sleep(1.5)
        if err or not isinstance(body, dict):
            st["errors"][v] = err or "unexpected_shape"
            continue
        followers = body.get("followers") or []
        # Store only what step 1 will need. No scoring, no ranking.
        vaults[v] = {
            "name": body.get("name"),
            "leader": body.get("leader"),
            "apr": body.get("apr"),
            "leaderFraction": body.get("leaderFraction"),
            "leaderCommission": body.get("leaderCommission"),
            "isClosed": body.get("isClosed"),
            "n_followers": len(followers),
            "followers": [
                {k: f.get(k) for k in ("user", "vaultEquity", "pnl", "allTimePnl",
                                       "daysFollowing", "vaultEntryTime")}
                for f in followers
            ],
            "seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        for f in followers:
            u = (f.get("user") or "").lower()
            if u and u not in done_u and u not in pend_u and len(pend_u) < MAX_USERS:
                pend_u.append(u)

    # Phase B: expand users into the other vaults they hold.
    while pend_u and budget_left() and len(vaults) < MAX_VAULTS:
        u = pend_u.pop(0)
        body, err = post({"type": "userVaultEquities", "user": u})
        n += 1
        st["requests_total"] += 1
        done_u.add(u)
        time.sleep(1.5)
        if err:
            st["errors"][u] = err
            continue
        for eq in (body or []):
            va = (eq.get("vaultAddress") or "").lower()
            if va and va not in vaults and va not in pend_v:
                pend_v.append(va)

    st["users_done"] = sorted(done_u)
    st["users_pending"] = pend_u
    st["vaults_pending"] = pend_v
    st["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    st["complete"] = (not pend_v and not pend_u) or len(vaults) >= MAX_VAULTS
    st["bias_note"] = ("Living vaults only; seeded from HLP; followers lists show "
                       "CURRENT depositors. Three upward biases. A positive step-1 "
                       "result from this sample is not evidence. A negative one is.")
    json.dump(st, open(OUT, "w"), indent=1)
    print(f"hl_crawl: {len(vaults)} vaults, {len(done_u)} users read, "
          f"{len(pend_v)} vaults / {len(pend_u)} users pending, "
          f"{st['requests_total']} requests total, complete={st['complete']}")


if __name__ == "__main__":
    main()
