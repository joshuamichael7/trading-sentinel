#!/usr/bin/env python3
"""One-time equity-history backfill (stdlib only — no pip installs on the runner).

Pulls QQQ 1h (~6mo), QQQ 5m (~60d), and daily ^VIX (~7mo) from Yahoo's v8 chart API
via urllib. Writes data/hist_eq/*.csv.gz, marks completion, and ALWAYS exits 0 so a
data-source refusal can never fail the workflow.
"""
import csv, gzip, json, os, sys, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
EQ = os.path.join(HERE, "data", "hist_eq")
os.makedirs(EQ, exist_ok=True)
MARKER = os.path.join(EQ, ".complete")
if os.path.exists(MARKER):
    print("qqq_backfill: complete, nothing to do")
    sys.exit(0)

def pull(symbol, interval, rng, name):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?interval={interval}&range={rng}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (paper-trading research)"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            j = json.load(r)
        res = j["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
        n = 0
        with gzip.open(os.path.join(EQ, name), "wt", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t", "o", "h", "l", "c", "v"])
            for i, t in enumerate(ts):
                if q["close"][i] is None:
                    continue
                w.writerow([t, q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]])
                n += 1
        print(f"qqq_backfill: {name} -> {n} rows")
        return n
    except Exception as e:
        print(f"qqq_backfill WARN {name}: {e}")
        return 0

try:
    n = pull("QQQ", "1h", "6mo", "QQQ_1h_6mo.csv.gz")
    n += pull("QQQ", "5m", "60d", "QQQ_5m_60d.csv.gz")
    n += pull("^VIX", "1d", "7mo", "VIX_1d_7mo.csv.gz")
    if n > 1000:
        open(MARKER, "w").write("done")
    print(f"qqq_backfill: total {n} rows")
except Exception as e:
    print(f"qqq_backfill WARN (outer): {e}")
sys.exit(0)
