#!/usr/bin/env python3
"""Code sentinel for the AI paper-trading experiment.

Runs on a schedule (GitHub Actions cron), costs zero AI tokens. Each run:
  1. Fetches crypto prices (Kraken public API) and meme-token prices/liquidity
     (DEX Screener public API) for everything in watcher_config.json.
  2. Appends a sample to data/samples.jsonl and the trending meme meta to
     data/meta_watch.jsonl (the AI check-ins replay these for exact fills and
     missed-opportunity analysis).
  3. Evaluates alert rules (stop/target/trailing/liquidation proximity, fast
     meme moves, liquidity drains) with per-alert cooldowns.
  4. On alert: sends a Discord and/or Telegram notification if configured
     (DISCORD_WEBHOOK / TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID env vars);
     otherwise exits non-zero so GitHub emails a workflow-failure notice.

No external dependencies — stdlib only.
"""
import json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
NOW = datetime.now(timezone.utc)
TS = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

def fetch_json(url, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "paper-trading-sentinel/1.0"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except Exception as e:
            if i == tries - 1:
                print(f"WARN: fetch failed {url}: {e}")
                return None
            time.sleep(2 * (i + 1))

def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

cfg = load(os.path.join(HERE, "watcher_config.json"), None)
if not cfg:
    print("FATAL: watcher_config.json missing/invalid"); sys.exit(1)
state = load(os.path.join(DATA, "state.json"), {"high_water": {}, "last_alert": {}, "ratchet_done": {}})

# ---------------- 1. prices ----------------
prices, liquidity = {}, {}
kr = fetch_json("https://api.kraken.com/0/public/Ticker?pair=XBTUSD,ETHUSD,SOLUSD")
if kr and kr.get("result"):
    for k, v in kr["result"].items():
        px = float(v["c"][0])
        if "XBT" in k: prices["BTC"] = px
        elif "ETH" in k: prices["ETH"] = px
        elif "SOL" in k: prices["SOL"] = px

for tok in cfg.get("meme_tokens", []):
    d = fetch_json(f"https://api.dexscreener.com/latest/dex/tokens/{tok['address']}")
    pairs = (d or {}).get("pairs") or []
    if pairs:
        best = max(pairs, key=lambda p: float(((p.get("liquidity") or {}).get("usd")) or 0))
        try:
            prices[tok["symbol"]] = float(best.get("priceUsd") or 0)
            liquidity[tok["symbol"]] = float(((best.get("liquidity") or {}).get("usd")) or 0)
        except (TypeError, ValueError):
            pass

if not prices:
    print("FATAL: no prices fetched at all"); sys.exit(1)

# previous sample (for fast-move checks) BEFORE appending the new one
prev = None
spath = os.path.join(DATA, "samples.jsonl")
if os.path.exists(spath):
    with open(spath, "rb") as f:
        try:
            f.seek(-4096, os.SEEK_END)
        except OSError:
            f.seek(0)
        tail = f.read().decode(errors="ignore").strip().split("\n")
        if tail and tail[-1].startswith("{"):
            try: prev = json.loads(tail[-1])
            except Exception: prev = None

with open(spath, "a") as f:
    f.write(json.dumps({"ts": TS, "prices": prices, "liquidity": liquidity}) + "\n")

# ---------------- 1a. cross-exchange top-of-book spread sampling ----------------
# For arbitrage research: synchronized best bid/ask across venues, logged with the
# net executable spread after 0.1%/side taker fees. Failures are skipped silently.
kr_book = fetch_json("https://api.kraken.com/0/public/Ticker?pair=XBTUSD,ETHUSD,SOLUSD")
books = {}  # sym -> {venue: (bid, ask)}
if kr_book and kr_book.get("result"):
    for k, v in kr_book["result"].items():
        sym = "BTC" if "XBT" in k else ("ETH" if "ETH" in k else "SOL")
        books.setdefault(sym, {})["kraken"] = (float(v["b"][0]), float(v["a"][0]))
for sym, prod in {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}.items():
    d = fetch_json(f"https://api.exchange.coinbase.com/products/{prod}/ticker", tries=1)
    if d and d.get("bid"):
        books.setdefault(sym, {})["coinbase"] = (float(d["bid"]), float(d["ask"]))
for sym, inst in {"BTC": "BTC-USDT", "ETH": "ETH-USDT", "SOL": "SOL-USDT"}.items():
    d = fetch_json(f"https://www.okx.com/api/v5/market/ticker?instId={inst}", tries=1)
    row = ((d or {}).get("data") or [{}])[0]
    if row.get("bidPx"):
        books.setdefault(sym, {})["okx"] = (float(row["bidPx"]), float(row["askPx"]))
spread_rows = []
TAKER = 0.001
for sym, venues in books.items():
    # sanity: drop venues whose mid deviates >3% from the median mid (bad tick guard)
    mids = {v: (b + a) / 2 for v, (b, a) in venues.items()}
    med = sorted(mids.values())[len(mids) // 2]
    venues = {v: ba for v, ba in venues.items() if abs(mids[v] / med - 1) <= 0.03}
    if len(venues) < 2:
        continue
    best_bid_v, (best_bid, _) = max(venues.items(), key=lambda kv: kv[1][0])
    best_ask_v, (_, best_ask) = min(venues.items(), key=lambda kv: kv[1][1])
    gross_pct = (best_bid - best_ask) / best_ask * 100
    net_pct = gross_pct - 2 * TAKER * 100
    spread_rows.append({"sym": sym, "buy_on": best_ask_v, "sell_on": best_bid_v,
                        "gross_pct": round(gross_pct, 4), "net_pct": round(net_pct, 4),
                        "venues": {v: [b, a] for v, (b, a) in venues.items()}})
if spread_rows:
    with open(os.path.join(DATA, "spreads.jsonl"), "a") as f:
        f.write(json.dumps({"ts": TS, "spreads": spread_rows}) + "\n")

# ---------------- 1b. 1-minute OHLC archive (BTC/ETH/SOL) ----------------
# Builds a continuous 1m candle dataset for future short-timeframe strategy research.
# Kraken serves ~12h of 1m candles per request, so gaps between runs self-heal.
OHLC_DIR = os.path.join(DATA, "ohlc")
os.makedirs(OHLC_DIR, exist_ok=True)
PAIR_MAP = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}
ohlc_state = state.setdefault("ohlc_last_ts", {})
for sym, pair in PAIR_MAP.items():
    since = ohlc_state.get(sym, 0)
    d = fetch_json(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1&since={since}")
    res = (d or {}).get("result") or {}
    rows = next((v for k, v in res.items() if k != "last" and isinstance(v, list)), [])
    if not rows:
        continue
    rows = rows[:-1]  # drop the still-forming candle
    new = [r for r in rows if int(r[0]) > since]
    if new:
        with open(os.path.join(OHLC_DIR, f"{sym}_1m.jsonl"), "a") as f:
            for r in new:
                f.write(json.dumps({"t": int(r[0]), "o": float(r[1]), "h": float(r[2]),
                                    "l": float(r[3]), "c": float(r[4]), "v": float(r[6])}) + "\n")
        ohlc_state[sym] = int(new[-1][0])

# ---------------- 2. trending meta snapshot ----------------
boosts = fetch_json("https://api.dexscreener.com/token-boosts/top/v1")
if isinstance(boosts, list) and boosts:
    trending = [{"chain": b.get("chainId"), "address": b.get("tokenAddress"),
                 "desc": (b.get("description") or "")[:120]} for b in boosts[:8]]
    with open(os.path.join(DATA, "meta_watch.jsonl"), "a") as f:
        f.write(json.dumps({"ts": TS, "trending": trending}) + "\n")

# ---------------- 3. alert rules ----------------
R = cfg["rules"]
alerts = []

def cooled(key):
    last = state["last_alert"].get(key, 0)
    if time.time() - last >= R["alert_cooldown_seconds"]:
        state["last_alert"][key] = time.time()
        return True
    return False

for p in cfg["positions"]:
    if p.get("watch") is False:
        continue
    s = p["symbol"]
    px = prices.get(s)
    if not px:
        continue
    pid = p["id"]
    short = p.get("side") == "short"
    sign = -1 if short else 1
    # water mark: high-water for longs, low-water for shorts
    if short:
        wm = min(x for x in [state["high_water"].get(pid), p.get("high_water"), px] if x is not None)
    else:
        wm = max(state["high_water"].get(pid, 0), p.get("high_water") or 0, px)
    state["high_water"][pid] = wm
    stop, tp1 = p.get("stop"), p.get("tp1")

    br = p.get("be_ratchet")
    ratcheted = state["ratchet_done"].get(pid) or (br or {}).get("done")
    if br and not ratcheted and sign * (px - br["trigger"]) >= 0:
        state["ratchet_done"][pid] = True
        stop = br["ratchet_stop_to"]
        if cooled(f"{pid}:ratchet"):
            alerts.append(f"INFO {pid} ({s}): breakeven ratchet fired at {px} — stop is now entry ({stop}). No action needed; FYI.")
    elif br and ratcheted:
        stop = (min if short else max)(x for x in [stop, br["ratchet_stop_to"]] if x is not None)

    # stop is adverse: below price for longs, above for shorts
    if stop and sign * (stop - px) >= 0 and cooled(f"{pid}:stop"):
        alerts.append(f"ALERT {pid} ({s}, {'short' if short else 'long'}): CROSSED STOP {stop} — price {px}")
    elif stop and sign * (stop * (1 + sign * R["near_band_pct"]) - px) >= 0 and cooled(f"{pid}:nearstop"):
        alerts.append(f"ALERT {pid} ({s}, {'short' if short else 'long'}): within {R['near_band_pct']*100:.0f}% of stop {stop} — price {px}")
    if tp1 and not p.get("tp1_hit") and sign * (px - tp1) >= 0 and cooled(f"{pid}:tp1"):
        alerts.append(f"ALERT {pid} ({s}, {'short' if short else 'long'}): CROSSED TP1 {tp1} — price {px}")
    if p.get("trail_pct") and p.get("tp1_hit") and sign * (wm * (1 - sign * p["trail_pct"]) - px) >= 0 and cooled(f"{pid}:trail"):
        alerts.append(f"ALERT {pid} ({s}): trailing stop hit ({p['trail_pct']*100:.0f}% off {'low' if short else 'high'} {wm}) — price {px}")
    lq = p.get("liquidation_price")
    if lq and sign * (lq * (1 + sign * R["liq_proximity_pct"]) - px) >= 0 and cooled(f"{pid}:liq"):
        alerts.append(f"ALERT {pid} ({s}): within {R['liq_proximity_pct']*100:.0f}% of LIQUIDATION {lq} — price {px}")
    if p["track"] == "meme":
        if prev and s in prev.get("prices", {}) and prev["prices"][s] > 0:
            chg = (px / prev["prices"][s] - 1) * 100
            if abs(chg) >= R["meme_fast_move_pct"] * 100 and cooled(f"{pid}:move"):
                alerts.append(f"ALERT {pid} ({s}): moved {chg:+.1f}% since last sample — price {px}")
        lq = liquidity.get(s)
        if lq is not None:
            if lq < R["meme_liquidity_floor_usd"] and cooled(f"{pid}:liqfloor"):
                alerts.append(f"ALERT {pid} ({s}): liquidity ${lq:,.0f} below ${R['meme_liquidity_floor_usd']:,} floor — possible drain/rug")
            elif (R.get("meme_liquidity_halved_from_entry") and p.get("liquidity_at_entry")
                  and lq < 0.5 * p["liquidity_at_entry"] and cooled(f"{pid}:liqhalf")):
                alerts.append(f"ALERT {pid} ({s}): liquidity halved from entry (${lq:,.0f} vs ${p['liquidity_at_entry']:,.0f})")

json.dump(state, open(os.path.join(DATA, "state.json"), "w"), indent=2)

# ---------------- 4. notify ----------------
# one-shot test alert: commit a file named data/test_alert_requested to trigger
flag = os.path.join(DATA, "test_alert_requested")
if os.path.exists(flag):
    os.remove(flag)
    alerts.append("TEST alert — the sentinel is live and notifications are working. "
                  "This is what a real alert will look like. No action needed.")

if alerts:
    with open(os.path.join(DATA, "alerts.jsonl"), "a") as f:
        for a in alerts:
            f.write(json.dumps({"ts": TS, "alert": a}) + "\n")
    msg = "📟 Paper-trading sentinel:\n" + "\n".join(alerts) + \
          "\n\n(Open Claude and say 'check the experiment now' to act; otherwise the next scheduled check-in will process exits at correct prices from the log.)"
    sent = False
    hook = os.environ.get("DISCORD_WEBHOOK")
    if hook:
        try:
            dmsg = "@everyone " + msg  # forces a push notification even on default channel settings
            req = urllib.request.Request(hook, data=json.dumps({"content": dmsg[:1900]}).encode(),
                                         headers={"Content-Type": "application/json", "User-Agent": "sentinel"})
            urllib.request.urlopen(req, timeout=15); sent = True
        except Exception as e:
            print(f"WARN: discord notify failed: {e}")
    tg_tok, tg_chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tg_tok and tg_chat:
        try:
            req = urllib.request.Request(f"https://api.telegram.org/bot{tg_tok}/sendMessage",
                                         data=json.dumps({"chat_id": tg_chat, "text": msg[:4000]}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15); sent = True
        except Exception as e:
            print(f"WARN: telegram notify failed: {e}")
    print("\n".join(alerts))
    real_alerts = [a for a in alerts if a.startswith("ALERT")]
    if real_alerts and not sent:
        sys.exit(1)  # no channel configured/working -> fail the workflow so GitHub emails the owner
else:
    print(f"OK {TS} — sampled {len(prices)} assets, nothing to do")
