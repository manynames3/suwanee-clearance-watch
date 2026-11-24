# Suwanee Clearance Watch — quick start

A personal fork of [KenStager/HDScanner](https://github.com/KenStager/HDScanner)
(MIT) with four additions: a SKU watchlist, a dollar floor on alerts, a daily
Markdown digest, and a phone deals page.

The scanner runs on your Mac. Nothing here logs in to Home Depot or buys anything.

## Install (once)

```bash
git clone https://github.com/manynames3/suwanee-clearance-watch.git
cd suwanee-clearance-watch
./mac-setup.sh
```

The script installs the scanner, starts the setup wizard (ZIP 30024, stores
1754 and 6978, your brands, and "yes" to installing the schedule), turns on
the dollar floor and the phone page, logs in to Cloudflare, and runs the
first scan. It is safe to run again.

To scan by hand at any time: `source .venv/bin/activate && hd run-once`.

After each scan:

- **Phone page:** https://clearance-watch.hangi87.workers.dev — deals by store,
  biggest saving first. Anyone who has the address can open it.
- **Digest:** `~/clearance-digest-YYYY-MM-DD.md`, one section per scan.

A scan that was throttled or cut short says `HEALTH_DEGRADED` in both places
and lists what it could not read. It never reports "no deals" in that case.

## Watchlist

`watchlist.txt` holds Home Depot item ids (the "Internet #" on a product
page), one per line. Each is checked at every store on every scan, whatever
its brand. Up to 40 items.

## Settings added by this fork

| `.env` name | What it does | Default |
|---|---|---|
| `WATCHLIST_PATH` | Where the watchlist file is | `watchlist.txt` |
| `WATCHLIST_MAX_ITEMS` | Most items checked per scan | `40` |
| `MIN_DOLLAR_OFF` | Hide deals that save less than this | `0` (off) |
| `DIGEST_DIR` | Folder for the daily digest | `~` |
| `SITE_WORKER` | Cloudflare Worker that serves the phone page | empty (off) |

Everything else is documented in `README.md`.
