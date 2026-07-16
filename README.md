# Paper-Trading Code Sentinel

Zero-token market watcher for the AI paper-trading experiment. Runs every ~5 minutes on
GitHub Actions, samples crypto + meme-token prices, logs the trending meme meta, checks
alert rules (stops, targets, trailing stops, liquidation proximity, liquidity drains),
and notifies instantly when something needs attention. The AI check-ins read this repo's
`data/` files to fill exits at exact prices and analyze missed opportunities.

**Paper trading only. No keys, no funds, no trades — just watching and logging.**

## Setup (~5 minutes)

1. Create a **public** GitHub repo (public = unlimited free Actions minutes; a 5-min cron
   on a private repo would exceed the 2,000 free monthly minutes — if you want private,
   change the cron in `.github/workflows/watch.yml` to `*/30 * * * *`).
2. Upload everything in this folder to the repo (drag-and-drop on github.com works:
   `watcher.py`, `watcher_config.json`, `README.md`, and `.github/workflows/watch.yml`
   — make sure the workflow file keeps its exact path).
3. Go to the repo's **Actions** tab and enable workflows if prompted. Then click
   **market-sentinel → Run workflow** to test. A green run should add a first sample to
   `data/samples.jsonl`.
4. Optional but recommended — instant notifications (pick either):
   - **Discord**: in any server you own → channel settings → Integrations → Webhooks →
     New Webhook → copy URL. In the repo: Settings → Secrets and variables → Actions →
     New repository secret, name `DISCORD_WEBHOOK`, paste the URL.
   - **Telegram**: create a bot with @BotFather (get token), message the bot once, get
     your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`. Add secrets
     `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
   - **Neither configured?** Real alerts make the workflow exit non-zero, so GitHub
     emails you a workflow-failure notice — crude but functional.
5. Tell Claude the repo URL (e.g. `https://github.com/you/trading-sentinel`) so the
   scheduled check-ins read `data/` from it and the hourly AI sentinel can be retired.

## How it stays in sync

`watcher_config.json` mirrors the experiment ledger (positions, stops, targets, trailing
rules). When positions change at a check-in, Claude regenerates it — either pushed here
automatically (if you later add a fine-grained PAT) or handed to you to paste in. Stale
config only means the watcher guards old levels for a few hours; the check-ins remain
the source of truth and settle everything against the logged samples.

## Notes

- GitHub cron is best-effort: expect 5–15 minute real spacing, occasionally more.
- GitHub disables schedules on repos with no activity for 60 days; the data commits
  every run count as activity, so this effectively never triggers while running.
- Stocks are not watched here (no good free equities API); the twice-daily AI check-ins
  handle those directly.
- Alert cooldown is 1 hour per unique condition to prevent notification spam.
