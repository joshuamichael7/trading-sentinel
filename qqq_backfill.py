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

n = 0
n += dump(yf.download("QQQ", interval="1h", period="6mo", auto_adjust=False, progress=False), "QQQ_1h_6mo.csv.gz")
n += dump(yf.download("QQQ", interval="5m", period="60d", auto_adjust=False, progress=False), "QQQ_5m_60d.csv.gz")
n += dump(yf.download("^VIX", interval="1d", period="7mo", auto_adjust=False, progress=False), "VIX_1d_7mo.csv.gz")
if n > 1000:
    open(MARKER, "w").write("done")
print(f"qqq_backfill: total {n} rows")
