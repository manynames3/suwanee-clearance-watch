# Changelog

## 0.3.0

### Added
- **Stock confidence (0 to 100)** on every deal, from pickup state, stock movement,
  seasonality, single-unit leftovers and cross-store agreement. **No-BS mode** hides
  low-confidence deals by default (`NO_BS_MODE`, `MIN_STOCK_CONFIDENCE`).
- Markdown cadence (`.00`, `.06`, `.03`, `.02`, `.01`), days at the current price, a usual
  price from recorded history, and an inflated-discount flag.
- Stage-2 probe: top candidates are re-checked with a single-item lookup.
- Push notifiers: ntfy, Pushover, Discord, Telegram, email, with `NOTIFY_RULES`
  subscriptions and de-duplication. `hd notify-test`.
- Installable phone page (PWA): offline shell, "Worth the Drive" store ranking,
  in-aisle hunter card with a client-side UPC barcode, and a 48-hour "not on the shelf" hide.
- `hd init --zip --radius`: setup from any ZIP. Replaces the hardcoded store setup in
  `mac-setup.sh`.
- `hd canary` and a typed upstream schema (`hd_api/schema.py`); offline schema-canary workflow.
- `hd doctor --network`.
- Dockerfile, docker-compose, GitHub Actions (tests on 3.11 to 3.13, ruff, mypy, image build).
- `RetailerAdapter` protocol with Home Depot as the first adapter.

### Changed
- Request pacing: drip-feed (one request at a time, 4 to 10 s apart, periodic rests),
  adaptive slow-down on `429`, tiered polling (the online-network tier at most daily).
- Soft-block detection: an implausibly empty `200` on a known-populated category is a refusal.
- Internals split into `cli/`, `pipeline/{nav_planner,fetcher,normalizer,persister,runner}`
  and `db/repository.py`; old import paths keep working. Typed config helpers moved to `envfile.py`.
- README rewritten; the previous manual is `docs/REFERENCE.md`.

### Removed
- The stray research artifact `compass_artifact_*.md`.

### Compatibility
- Database changes are additive (`item_fulfillment`); existing databases upgrade in place.
  `backup`, `prune` and `vacuum` are unchanged.

### Deliberately not included
- TLS/browser impersonation, cookie harvesting, proxy rotation. This project stops on a
  refusal rather than working around it.

## 0.2.0
Fork of KenStager/HDScanner with a SKU watchlist, a dollar floor, a daily Markdown digest
and a phone deals page.
