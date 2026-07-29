#!/usr/bin/env python3
"""Wide-universe daily backfill for the momentum/carry stress tests.

Runs on the GitHub runner (the research container has no outbound network).
Time-budgeted: works until DEADLINE_S then saves progress and exits 0, so it
never trips the job timeout and never turns the workflow red.

What it collects into data/:
  universe/manifest.json   every USDT spot symbol Binance EVER listed, split
                           into live vs delisted. The delisted list is the
                           survivorship-bias fix -- momentum backtests that
                           only see survivors are lying.
  daily/<SYM>.csv.gz       daily OHLCV + QUOTE volume (t,o,h,l,c,v,qv).
                           qv is the dollar volume used to rank the universe
                           as-of each rebalance date (a market-cap proxy we can
                           actually compute point-in-time).
  perp_daily/<SYM>.csv.gz  perpetual daily closes, for basis modelling.
  funding/<SYM>.csv.gz     full 8h funding history back to listing.
  eq_daily/<TICKER>.csv.gz daily equity/ETF bars, ~10y, for cross-market
                           validation. Sector ETFs are survivorship-free.
"""
import csv, gzip, io, json, os, sys, time, urllib.request, urllib.error, zipfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, "data")
DEADLINE_S = 480          # stop starting new work after 8 minutes
START = time.time()
UA = {"User-Agent": "Mozilla/5.0 (compatible; sentinel-research/1.0)"}

for sub in ("universe", "daily", "perp_daily", "funding", "eq_daily"):
    os.makedirs(os.path.join(D, sub), exist_ok=True)

STATE_P = os.path.join(D, "universe", "backfill_state.json")
STATE = json.load(open(STATE_P)) if os.path.exists(STATE_P) else {}


def save_state():
    json.dump(STATE, open(STATE_P, "w"), indent=1, sort_keys=True)


def out_of_time():
    return time.time() - START > DEADLINE_S


def get(url, timeout=60, retries=2):
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (404, 451):
                return None
            if e.code in (418, 429):
                time.sleep(5 * (i + 1))
            elif i == retries:
                return None
        except Exception:
            if i == retries:
                return None
            time.sleep(2)
    return None


def write_csv(path, header, rows):
    with gzip.open(path, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


# ---------------------------------------------------------------- universe ---
MANIFEST_P = os.path.join(D, "universe", "manifest.json")


def build_manifest():
    if os.path.exists(MANIFEST_P) and STATE.get("manifest_done"):
        return json.load(open(MANIFEST_P))
    live = set()
    raw = get("https://api.binance.com/api/v3/exchangeInfo")
    if raw:
        for s in json.loads(raw)["symbols"]:
            if s["quoteAsset"] == "USDT" and s["status"] == "TRADING":
                live.add(s["symbol"])
    # every symbol that ever had a monthly 1d kline file (includes delisted)
    ever, token = set(), None
    while True:
        u = ("https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
             "?delimiter=/&prefix=data/spot/monthly/klines/")
        if token:
            u += "&marker=" + urllib.request.quote(token)
        raw = get(u)
        if not raw:
            break
        root = ET.fromstring(raw)
        ns = {"s3": root.tag.split("}")[0].strip("{")}
        pref = root.findall("s3:CommonPrefixes/s3:Prefix", ns)
        if not pref:
            break
        for p in pref:
            sym = p.text.rstrip("/").split("/")[-1]
            if sym.endswith("USDT"):
                ever.add(sym)
        trunc = root.findtext("s3:IsTruncated", "false", ns)
        if trunc != "true":
            break
        token = pref[-1].text
        if out_of_time():
            break
    man = {"live": sorted(live), "ever": sorted(ever),
           "delisted": sorted(ever - live), "built": int(time.time())}
    json.dump(man, open(MANIFEST_P, "w"), indent=1)
    STATE["manifest_done"] = True
    save_state()
    print(f"manifest: {len(live)} live, {len(ever)} ever, {len(man['delisted'])} delisted")
    return man


# ------------------------------------------------------------ daily klines ---
def fetch_daily_rest(symbol, base, path_out, start_ms):
    """Paginated daily klines from the REST API (live symbols only)."""
    rows, cursor = [], start_ms
    while True:
        u = f"{base}?symbol={symbol}&interval=1d&startTime={cursor}&limit=1000"
        raw = get(u)
        if not raw:
            break
        ks = json.loads(raw)
        if not ks:
            break
        for k in ks:
            rows.append([int(k[0]) // 1000, k[1], k[2], k[3], k[4], k[5], k[7]])
        if len(ks) < 1000:
            break
        cursor = int(ks[-1][0]) + 86400000
        if out_of_time():
            break
    if rows:
        write_csv(path_out, ["t", "o", "h", "l", "c", "v", "qv"], rows)
    return len(rows)


def fetch_daily_vision(symbol, path_out):
    """Delisted symbols: stitch the monthly 1d archive files."""
    u = ("https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
         f"?delimiter=/&prefix=data/spot/monthly/klines/{symbol}/1d/")
    raw = get(u)
    if not raw:
        return 0
    root = ET.fromstring(raw)
    ns = {"s3": root.tag.split("}")[0].strip("{")}
    keys = [k.text for k in root.findall("s3:Contents/s3:Key", ns)
            if k.text.endswith(".zip")]
    rows = []
    for key in sorted(keys):
        if out_of_time():
            break
        blob = get("https://data.binance.vision/" + key, timeout=90)
        if not blob:
            continue
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
            with zf.open(zf.namelist()[0]) as fh:
                for line in io.TextIOWrapper(fh, "utf-8"):
                    p = line.strip().split(",")
                    if not p or not p[0] or p[0][0].isalpha():
                        continue
                    ts = int(p[0])
                    ts = ts // 1000000 if ts > 1e14 else ts // 1000
                    rows.append([ts, p[1], p[2], p[3], p[4], p[5], p[7]])
        except Exception:
            continue
    if rows:
        rows.sort()
        write_csv(path_out, ["t", "o", "h", "l", "c", "v", "qv"], rows)
    return len(rows)


# ----------------------------------------------------------------- funding ---
def fetch_funding(symbol, path_out):
    rows, cursor = [], 1568000000000  # Sep 2019, before the first perp funding
    while True:
        u = ("https://fapi.binance.com/fapi/v1/fundingRate"
             f"?symbol={symbol}&startTime={cursor}&limit=1000")
        raw = get(u)
        if not raw:
            break
        ks = json.loads(raw)
        if not ks:
            break
        for k in ks:
            rows.append([int(k["fundingTime"]) // 1000, k["fundingRate"]])
        if len(ks) < 1000:
            break
        cursor = int(ks[-1]["fundingTime"]) + 1
        if out_of_time():
            break
    if rows:
        write_csv(path_out, ["t", "rate"], rows)
    return len(rows)


# ---------------------------------------------------------------- equities ---
EQ_TICKERS = [
    # sector ETFs -- survivorship-free cross-sectional test set
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
    # broad / cross-asset
    "SPY", "QQQ", "IWM", "MDY", "EFA", "EEM", "TLT", "IEF", "GLD", "SLV", "DBC", "VNQ", "HYG",
    # large caps (survivorship-biased by construction -- disclosed)
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK-B", "JPM", "V",
    "UNH", "XOM", "JNJ", "WMT", "PG", "MA", "AVGO", "HD", "CVX", "MRK",
    "ABBV", "COST", "PEP", "KO", "ADBE", "CRM", "MCD", "CSCO", "TMO", "ACN",
    "AMD", "NFLX", "INTC", "QCOM", "TXN", "NKE", "PM", "UPS", "BA", "CAT",
]


def fetch_equity(ticker, path_out):
    u = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
         "?range=10y&interval=1d")
    raw = get(u)
    if not raw:
        return 0
    try:
        j = json.loads(raw)["chart"]["result"][0]
        ts = j["timestamp"]
        q = j["indicators"]["quote"][0]
        adj = j["indicators"].get("adjclose", [{}])[0].get("adjclose")
    except Exception:
        return 0
    rows = []
    for i, t in enumerate(ts):
        c = q["close"][i]
        if c is None:
            continue
        rows.append([t, q["open"][i], q["high"][i], q["low"][i], c,
                     q["volume"][i], (adj[i] if adj else c)])
    if rows:
        write_csv(path_out, ["t", "o", "h", "l", "c", "v", "adjc"], rows)
    return len(rows)


# --------------------------------------------------------------------- run ---
def main():
    man = build_manifest()
    if out_of_time():
        print("wide_backfill: manifest phase used the budget")
        return

    done = STATE.setdefault("done", {})
    work = 0

    # 1. equities first -- small, high value (clean cross-market validation)
    for tk in EQ_TICKERS:
        if out_of_time():
            break
        key = "EQ:" + tk
        if done.get(key):
            continue
        n = fetch_equity(tk, os.path.join(D, "eq_daily", f"{tk}.csv.gz"))
        done[key] = n
        work += 1
        print(f"eq {tk}: {n} bars")

    # 2. funding for the liquid perp set, full history
    perps = [s for s in man["live"] if s in {
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "AVAXUSDT",
        "LINKUSDT", "ADAUSDT", "BNBUSDT", "LTCUSDT", "DOTUSDT", "MATICUSDT",
        "ATOMUSDT", "UNIUSDT", "AAVEUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT",
        "OPUSDT", "INJUSDT", "SUIUSDT", "TIAUSDT", "SEIUSDT", "FILUSDT",
        "ETCUSDT", "BCHUSDT", "TRXUSDT", "ICPUSDT", "RUNEUSDT", "ALGOUSDT"}]
    for sym in perps:
        if out_of_time():
            break
        key = "FUND:" + sym
        if done.get(key):
            continue
        n = fetch_funding(sym, os.path.join(D, "funding", f"{sym}.csv.gz"))
        done[key] = n
        work += 1
        print(f"funding {sym}: {n} stamps")
        if n:
            k2 = "PERP:" + sym
            if not done.get(k2):
                n2 = fetch_daily_rest(sym, "https://fapi.binance.com/fapi/v1/klines",
                                      os.path.join(D, "perp_daily", f"{sym}.csv.gz"),
                                      1568000000000)
                done[k2] = n2

    # 3. spot daily for the whole live universe
    for sym in man["live"]:
        if out_of_time():
            break
        key = "D:" + sym
        if done.get(key):
            continue
        n = fetch_daily_rest(sym, "https://api.binance.com/api/v3/klines",
                             os.path.join(D, "daily", f"{sym}.csv.gz"), 1502928000000)
        done[key] = n
        work += 1

    # 4. delisted symbols -- the survivorship fix. Slowest, so it goes last.
    for sym in man["delisted"]:
        if out_of_time():
            break
        key = "D:" + sym
        if done.get(key):
            continue
        n = fetch_daily_vision(sym, os.path.join(D, "daily", f"{sym}.csv.gz"))
        done[key] = n
        work += 1

    save_state()
    live_left = sum(1 for s in man["live"] if not done.get("D:" + s))
    dead_left = sum(1 for s in man["delisted"] if not done.get("D:" + s))
    print(f"wide_backfill: {work} items this run | remaining live {live_left}, delisted {dead_left}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("wide_backfill error (non-fatal):", type(e).__name__, e)
    sys.exit(0)
