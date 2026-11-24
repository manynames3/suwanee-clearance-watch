#!/bin/bash
# One-time setup for Suwanee Clearance Watch on a Mac.
# Safe to run again: each step is skipped when it is already done.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

say "1/6 Finding Python 3.11 or newer"
PY=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
    PY="$candidate"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11+ is not installed. Install it with:  brew install python@3.12"
  echo "Then run this script again."
  exit 1
fi
echo "Using $($PY --version)"

say "2/6 Installing the scanner"
[ -d .venv ] || "$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -e ".[dashboard]"
[ -f watchlist.txt ] || cp watchlist.example.txt watchlist.txt

say "3/6 Setting stores, brands and the schedule"
# No questions and no store lookup: the stores are written directly, and only
# brands not already saved are looked up. Override with e.g.
#   BRANDS_WANTED="Milwaukee,Ryobi" ./mac-setup.sh
BRANDS_WANTED="${BRANDS_WANTED:-Milwaukee,DEWALT,Makita,Ryobi}"
READY=1
hd quick-setup \
  --store "1754|Suwanee|Suwanee|GA|30024" \
  --store "6978|Brookwood|Suwanee|GA|30024" \
  --brands "$BRANDS_WANTED" || READY=0

say "4/6 Turning on the dollar floor and the phone page"
add_setting() {
  if grep -q "^$1=" .env; then echo "$1 already set"; else echo "$1=$2" >> .env; echo "$1=$2 added"; fi
}
# Make sure the file ends with a newline so a new setting starts its own line.
[ -f .env ] || touch .env
[ -z "$(tail -c1 .env)" ] || echo >> .env
add_setting MIN_DOLLAR_OFF 20
add_setting SITE_WORKER clearance-watch

say "5/6 Connecting Cloudflare for the phone page"
if ! command -v npx >/dev/null 2>&1; then
  echo "Node.js is not installed, so the phone page will not update."
  echo "Install it with 'brew install node' and run this script again."
elif npx --yes wrangler whoami >/dev/null 2>&1; then
  echo "Already logged in to Cloudflare."
else
  echo "A browser window will open. Approve the Cloudflare login there."
  npx --yes wrangler login
fi

say "6/6 Running the first scan"
if [ "$READY" = 1 ]; then
  hd run-once || echo "The scan reported a problem (see above). The schedule will try again."
else
  echo "Skipped: no brand could be looked up yet because Home Depot is rate limiting."
  echo "Run ./mac-setup.sh again in about an hour. Nothing else needs redoing."
fi

cat <<DONE

Done.
  Phone page:  https://clearance-watch.hangi87.workers.dev
  Digest:      ~/clearance-digest-$(date +%Y-%m-%d).md
  Watchlist:   $(pwd)/watchlist.txt
DONE
