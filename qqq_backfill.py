#!/usr/bin/env python3
"""One-time equity-history backfill for the iron-condor research track.

Pulls via yfinance (runner-side): QQQ 1h bars (~6mo), QQQ 5m bars (max ~60d),
and daily ^VIX (~7mo). Writes data/hist_eq/*.csv.gz and marks completion.
Self-limiting no-op once complete.
"""
import gzip, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
EQ = os.path.join(HERE, "data", "hist_eq")
os.makedirs(EQ, exist_ok=True)
MARKER = os.path.join(EQ, ".complete")
if os.path.exists(MARKER):
    print("qqq_backfill: complete, nothing to do")
    sys.exit(0)

try:
    import yfinance as yf
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "yfinance"], check=True)
    import yfinance as yf

def dump(df, name):
    if df is None or df.empty:
        print(f"qqq_backfill WARN: {name} empty"); return 0
    df = df.reset_index()
    df.columns = [str(c[0]) if isinstance(c, tuple) else str(c) for c in df.columns]
    with gzip.open(os.path.join(EQ, name), "wt") as f:
        df.to_csv(f, index=False)
    print(f"qqq_backfill: {name} -> {len(df)} rows")
    return len(df)

def yahoo_direct(symbol, interval, rng, name):
    """Fallback: Yahoo v8 chart API via urllib (yfinance wrapper often blocked on CI IPs)."""
    import urllib.request, urllib.parse, csv as _csv
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?interval={interval}&range={rng}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research; contact in repo)"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            j = json.load(r)
        res = j["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
        with gzip.open(os.path.join(EQ, name), "wt", newline="") as f:
            w = _csv.writer(f)
            w.writerow(["t", "o", "h", "l", "c", "v"])
            m = 0
            for i, t in enumerate(ts):
                if q["close"][i] is None:
                    continue
                w.writerow([t, q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]])
                m += 1
        print(f"qqq_backfill (direct): {name} -> {m} rows")
        return m
    except Exception as e:
        print(f"qqq_backfill WARN direct {name}: {e}")
        return 0

n = 0
n += dump(yf.download("QQQ", interval="1h", period="6mo", auto_adjust=False, progress=False), "QQQ_1h_6mo.csv.gz")
n += dump(yf.download("QQQ", interval="5m", period="60d", auto_adjust=False, progress=False), "QQQ_5m_60d.csv.gz")
n += dump(yf.download("^VIX", interval="1d", period="7mo", auto_adjust=False, progress=False), "VIX_1d_7mo.csv.gz")
if n < 1000:
    n = 0
    n += yahoo_direct("QQQ", "1h", "6mo", "QQQ_1h_6mo.csv.gz")
    n += yahoo_direct("QQQ", "5m", "60d", "QQQ_5m_60d.csv.gz")
    n += yahoo_direct("^VIX", "1d", "7mo", "VIX_1d_7mo.csv.gz")
if n > 1000:
    open(MARKER, "w").write("done")
print(f"qqq_backfill: total {n} rows")
