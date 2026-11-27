# Clearance Watch: quick start

Five minutes from nothing to a scanner watching the stores near you. It logs in to
nothing and buys nothing.

## 1. Install

```bash
git clone https://github.com/manynames3/suwanee-clearance-watch.git
cd suwanee-clearance-watch
./mac-setup.sh 30024        # your ZIP code (macOS)
```

On Linux, or if you prefer to do it by hand:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dashboard]"
hd init --zip 30024 --radius 25 --no-schedule
```

`hd init` looks up the nearest stores once, writes them with their addresses, resolves
your brands (default: Milwaukee, DEWALT, Makita, Ryobi) and installs three scans a day on
macOS. Use `--dry-run` first to see which stores it would watch, `--max-stores 3` for
more, `--brands "Ryobi,Craftsman"` for others. If Home Depot rate limits the brand
lookup it says so; run the same command again later and it only redoes what's missing.

Docker instead: `docker compose run --rm scanner hd init --zip 30024 --no-schedule`
then `docker compose up -d`.

## 2. First scan

```bash
hd run-once      # 10 to 30 minutes, paced on purpose
hd alerts
```

The first run mostly records a baseline; deal alerts start from the second.

## 3. Get notified (optional)

Add any of these to `.env` (see `.env.example`), then run `hd notify-test`:

```
NTFY_TOPIC=a-long-random-topic        # ntfy.sh app on your phone
NOTIFY_RULES=[{"min_percent_off": 50, "min_confidence": 60}]
```

Pushover, Discord, Telegram, email and Slack work the same way.

## 4. Phone page (optional)

Every run writes an installable page to `.site_out/`. To publish it, set
`SITE_WORKER=<a-cloudflare-worker-name>` (needs Node.js and one `npx wrangler login`), or
serve the folder yourself. Open it on your phone and choose Add to Home Screen.

## Good to know

- `watchlist.txt`: item ids ("Internet #") priced at every store each scan, up to 40.
- `MIN_DOLLAR_OFF`: hide deals that save less than this. Default 0.
- `hd doctor`: what's wrong, `hd doctor --network`: what kind of request trouble it is.
- `hd canary`: has Home Depot's response shape changed?
- A scan that was throttled or cut short says `HEALTH_DEGRADED` and lists what it
  couldn't read. It never reports "no deals" in that case.
- Prices and stock are estimates. Verify in store.

Everything else: [docs/REFERENCE.md](docs/REFERENCE.md).
