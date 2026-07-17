#!/usr/bin/env python3
"""One-time historical backfill: Binance public 1m klines -> data/hist/*.csv.gz

Runs as a step in the watch workflow; processes up to MAX_FILES_PER_RUN missing
month-files per run (spreads work across runs, stays within job timeout), then
becomes a fast no-op once data/hist/.complete covers everything.

Note: USDT pairs proxy USD pairs; fine for strategy research. Binance monthly
spot klines use microsecond timestamps from 2025-01 onward — normalized to
epoch seconds here. Stored columns: t,o,h,l,c,v.
"""
import csv, gzip, io, json, os, sys, urllib.request, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "data", "hist")
os.makedirs(HIST, exist_ok=True)
MARKER = os.path.join(HIST, ".complete.json")

PAIRS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
MAX_FILES_PER_RUN = 6

done = {}
if os.path.exists(MARKER):
    done = json.load(open(MARKER))

todo = [(s, p, m) for s, p in PAIRS.items() for m in MONTHS if not done.get(f"{s}-{m}")]
if not todo:
    print("backfill: complete, nothing to do")
    sys.exit(0)

processed = 0
for sym, pair, month in todo[:MAX_FILES_PER_RUN]:
    url = f"https://data.binance.vision/data/spot/monthly/klines/{pair}/1m/{pair}-1m-{month}.zip"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sentinel-backfill/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            blob = r.read()
        zf = zipfile.ZipFile(io.BytesIO(blob))
        name = zf.namelist()[0]
        out_path = os.path.join(HIST, f"{sym}_1m_{month}.csv.gz")
        n = 0
        with gzip.open(out_path, "wt", newline="") as gz:
            w = csv.writer(gz)
            w.writerow(["t", "o", "h", "l", "c", "v"])
            for row in csv.reader(io.TextIOWrapper(zf.open(name))):
                if not row or not row[0].strip().isdigit():
                    continue  # skip header line if present
                t = int(row[0])
                if t > 1e14: t //= 1_000_000   # microseconds -> seconds
                elif t > 1e11: t //= 1000       # milliseconds -> seconds
                w.writerow([t, row[1], row[2], row[3], row[4], row[5]])
                n += 1
        done[f"{sym}-{month}"] = n
        processed += 1
        print(f"backfill: {sym} {month} -> {n} candles")
    except Exception as e:
        print(f"backfill WARN: {sym} {month} failed: {e}")

json.dump(done, open(MARKER, "w"), indent=0)
remaining = len(todo) - processed
print(f"backfill: {processed} file(s) this run, {remaining} remaining")
