"""`hd doctor --network`: what kind of trouble is this connection in?

A failing scan has several very different causes that call for different
responses, and treating them alike is how a scanner makes things worse. This
module reads what the scanner has already recorded (the cooldown file and the
per-run request metrics) and names the dominant cause with a plain remediation.

It deliberately recommends only polite remedies — fewer requests, more waiting,
checking the connection. It does not suggest changing how requests look or
where they come from: a refusal is the API's answer, and the scanner's job is
to respect it and report it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from hd.config import Settings
from hd.doctor import FAIL, OK, WARN, Check

# Outcome label -> (class, severity, what it means, what to do)
_CLASSES: dict[str, tuple[str, str, str, str]] = {
    "http_206_quota": (
        "QUOTA", WARN,
        "Home Depot is capping how many requests this connection may make.",
        "Send fewer requests: fewer brands or stores, keep drip-feed on, and let "
        "the cooldown expire. Scheduled scans resume on their own.",
    ),
    "http_429": (
        "RATE_LIMITED", WARN,
        "The API is asking for a slower pace.",
        "The scanner already slows itself after a 429. Keep drip-feed on and "
        "avoid manual back-to-back runs.",
    ),
    "http_403": (
        "REFUSED", FAIL,
        "The API refused the requests outright (HTTP 403).",
        "The scanner stops and waits rather than pressing on. Check the identity "
        "check above is correct, then wait out the cooldown. If refusals continue "
        "for a day or more, reduce coverage or pause scanning.",
    ),
    "challenge_html": (
        "REFUSED", FAIL,
        "A challenge page was served where data was expected.",
        "Treated as a refusal: the scanner stops. Wait out the cooldown and "
        "keep the request rate low; do not retry in a loop.",
    ),
    "soft_block": (
        "SOFT_BLOCK", FAIL,
        "Responses came back 200 but implausibly empty for categories that "
        "used to be populated.",
        "The scanner discards those pages (no inventory is wiped) and stops. "
        "Wait for the cooldown; if it recurs, lower the request volume.",
    ),
    "http_5xx": (
        "UPSTREAM", WARN,
        "Home Depot's servers are erroring.",
        "Nothing to change on your side. Scans retry with backoff.",
    ),
    "timeout": (
        "NETWORK", WARN,
        "Requests are timing out.",
        "Check this machine's internet connection; try again later.",
    ),
    "transport_error": (
        "NETWORK", WARN,
        "Requests are failing before any response.",
        "Check that curl works and this machine is online.",
    ),
}

# Worst first: if several appear, this is the one to talk about.
_PRIORITY = [
    "http_403", "challenge_html", "soft_block", "http_206_quota", "http_429",
    "http_5xx", "timeout", "transport_error",
]


def recent_outcomes(path: str | Path, last_runs: int = 6) -> dict[str, int]:
    """Summed outcome counts over the most recent runs in the metrics file."""
    try:
        lines = Path(path).read_text().splitlines()[-last_runs:]
    except OSError:
        return {}
    totals: dict[str, int] = {}
    for line in lines:
        try:
            by_outcome = json.loads(line).get("by_outcome", {})
        except ValueError:
            continue
        if not isinstance(by_outcome, dict):
            continue
        for key, n in by_outcome.items():
            if isinstance(n, int):
                totals[key] = totals.get(key, 0) + n
    return totals


def classify(outcomes: dict[str, int]) -> str | None:
    """The worst failure outcome present, or None if the runs were clean."""
    for key in _PRIORITY:
        if outcomes.get(key, 0) > 0:
            return key
    return None


def _curl_capabilities() -> Check:
    path = shutil.which("curl")
    if not path:
        return Check("curl", FAIL, "curl not found — every request goes through it",
                     "Install curl.")
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return Check("curl", WARN, f"{path} found but could not report its version")
    first = out.splitlines()[0] if out else "curl"
    http2 = "HTTP2" in out
    tls = re.search(r"(OpenSSL|LibreSSL|SecureTransport|GnuTLS|BoringSSL|Schannel)\S*", out)
    detail = f"{first.split(' (')[0]}; TLS: {tls.group(0) if tls else 'unknown'}; HTTP/2: {'yes' if http2 else 'no'}"
    return Check("curl", OK if http2 else WARN, detail,
                 None if http2 else "A curl build with HTTP/2 support is preferable.")


def check_network(settings: Settings) -> list[Check]:
    from hd.doctor import check_cooldown, check_identity

    checks: list[Check] = [_curl_capabilities()]
    checks.extend(check_identity(settings))
    checks.extend(check_cooldown(settings))

    outcomes = recent_outcomes(settings.metrics_path)
    if not outcomes:
        checks.append(Check("recent-runs", WARN, "no request history recorded yet"))
        return checks

    worst = classify(outcomes)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items(), key=lambda kv: -kv[1])[:6])
    if worst is None:
        checks.append(Check("recent-runs", OK, f"last runs were clean ({summary})"))
        return checks

    klass, severity, meaning, remedy = _CLASSES[worst]
    checks.append(Check("recent-runs", severity, f"{klass}: {meaning} ({summary})", remedy))
    return checks
