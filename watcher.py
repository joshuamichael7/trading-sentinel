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
    hw = max(state["high_water"].get(pid, 0), p.get("high_water") or 0, px)
    state["high_water"][pid] = hw
    stop, tp1 = p.get("stop"), p.get("tp1")

    br = p.get("be_ratchet")
    ratcheted = state["ratchet_done"].get(pid) or (br or {}).get("done")
    if br and not ratcheted and px >= br["trigger"]:
        state["ratchet_done"][pid] = True
        stop = br["ratchet_stop_to"]
        if cooled(f"{pid}:ratchet"):
            alerts.append(f"INFO {pid} ({s}): breakeven ratchet fired at {px} — stop is now entry ({stop}). No action needed; FYI.")
    elif br and ratcheted:
        stop = max(stop or 0, br["ratchet_stop_to"])

    if stop and px <= stop and cooled(f"{pid}:stop"):
        alerts.append(f"ALERT {pid} ({s}): CROSSED STOP {stop} — price {px}")
    elif stop and px <= stop * (1 + R["near_band_pct"]) and cooled(f"{pid}:nearstop"):
        alerts.append(f"ALERT {pid} ({s}): within {R['near_band_pct']*100:.0f}% of stop {stop} — price {px}")
    if tp1 and not p.get("tp1_hit") and px >= tp1 and cooled(f"{pid}:tp1"):
        alerts.append(f"ALERT {pid} ({s}): CROSSED TP1 {tp1} — price {px}")
    if p.get("trail_pct") and p.get("tp1_hit") and px <= hw * (1 - p["trail_pct"]) and cooled(f"{pid}:trail"):
        alerts.append(f"ALERT {pid} ({s}): trailing stop hit ({p['trail_pct']*100:.0f}% off high {hw}) — price {px}")
    if p.get("liquidation_price") and px <= p["liquidation_price"] * (1 + R["liq_proximity_pct"]) and cooled(f"{pid}:liq"):
        alerts.append(f"ALERT {pid} ({s}): within {R['liq_proximity_pct']*100:.0f}% of LIQUIDATION {p['liquidation_price']} — price {px}")
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
            req = urllib.request.Request(hook, data=json.dumps({"content": msg[:1900]}).encode(),
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
