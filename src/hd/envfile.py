"""Typed, non-interactive configuration helpers.

Everything here is pure: no prompts, no network. `EnvFile` edits a .env in
place, `parse_selection` reads a "1,3-5" style choice, and `SetupConfig`
validates the values the wizard (or `hd init`) is about to write, so a typo
fails at write time instead of silently scanning nothing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, field_validator

if TYPE_CHECKING:
    from hd.hd_api.stores import StoreResult


_NEEDS_QUOTING = re.compile(r"[\s#]")


_KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


class EnvFile:
    """A .env file edited in place, preserving comments, order and spacing.

    Rewriting the file from a dict would discard the explanatory comments that
    make the config readable, so existing keys are edited on their own line and
    only genuinely new keys are appended.
    """

    def __init__(self, path: Path, lines: list[str] | None = None) -> None:
        self.path = Path(path)
        self._lines: list[str] = list(lines or [])

    @classmethod
    def load(cls, path: str | Path) -> EnvFile:
        p = Path(path)
        if not p.exists():
            return cls(p, [])
        text = p.read_text(encoding="utf-8", errors="replace")
        return cls(p, text.split("\n"))

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def _indices_of(self, key: str) -> list[int]:
        return [
            i for i, line in enumerate(self._lines)
            if (m := _KEY_LINE.match(line)) and m.group(1) == key
        ]

    def _index_of(self, key: str) -> int | None:
        """Index of the line that actually takes effect.

        python-dotenv keeps the LAST assignment, so a hand-edited file with a
        duplicate key would otherwise have setup edit a line nobody reads —
        the wizard reports success while the scanner runs on the old value.
        """
        found = self._indices_of(key)
        return found[-1] if found else None

    def get(self, key: str) -> str | None:
        i = self._index_of(key)
        if i is None:
            return None
        _, _, raw = self._lines[i].partition("=")
        value = raw.strip()
        if value[:1] in {'"', "'"} and value[:1] == value[-1:] and len(value) > 1:
            return value[1:-1]
        # Strip an inline comment from an unquoted value, as dotenv does.
        return value.split(" #", 1)[0].strip()

    def set(self, key: str, value: str) -> None:
        """Set a key, collapsing any duplicate assignments to one line."""
        found = self._indices_of(key)
        if not found:
            self._lines.append(f"{key}={self._quote(value)}")
            return

        keep = found[-1]
        # Preserve an `export ` prefix — dropping it would silently stop the
        # variable being exported for anyone sourcing this file from a shell.
        prefix = "export " if self._lines[keep].lstrip().startswith("export ") else ""
        self._lines[keep] = f"{prefix}{key}={self._quote(value)}"
        for i in reversed(found[:-1]):
            del self._lines[i]

    def set_many(self, values: dict[str, str]) -> None:
        for k, v in values.items():
            self.set(k, v)

    @staticmethod
    def _quote(value: str) -> str:
        if value and _NEEDS_QUOTING.search(value):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return value

    def render(self) -> str:
        body = "\n".join(self._lines)
        return body + "\n" if body and not body.endswith("\n") else body

    def save(self) -> None:
        """Write atomically with owner-only permissions — the file holds tokens.

        Written to a temporary file and renamed, so an interrupt or a full disk
        cannot leave a truncated .env where the user's only copy of their Slack
        token used to be. Created 0600 rather than chmod-ed afterwards, which
        would expose it at the default umask for the width of the write.
        """
        import os

        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self.render())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise


def parse_selection(raw: str, count: int) -> list[int]:
    """Parse "1,3" or "2" or "1-3" into zero-based indices.

    Raises ValueError with a usable message rather than silently dropping the
    parts it did not understand — a mis-parsed selection would configure the
    wrong store.
    """
    text = raw.strip()
    if not text:
        raise ValueError("Nothing selected")

    picked: list[int] = []
    for chunk in re.split(r"[,\s]+", text):
        if not chunk:
            continue
        if "-" in chunk[1:]:
            lo_s, _, hi_s = chunk.partition("-")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError:
                raise ValueError(f"{chunk!r} is not a number or range") from None
            if lo > hi:
                lo, hi = hi, lo
            candidates = range(lo, hi + 1)
        else:
            try:
                candidates = [int(chunk)]
            except ValueError:
                raise ValueError(f"{chunk!r} is not a number") from None

        for n in candidates:
            if not 1 <= n <= count:
                raise ValueError(f"{n} is not in range 1-{count}")
            if n - 1 not in picked:
                picked.append(n - 1)

    if not picked:
        raise ValueError("Nothing selected")
    return picked


def store_env_value(stores: list[StoreResult]) -> str:
    """The STORES= value for the chosen stores."""
    return ",".join(s.store_id for s in stores)


def build_env_values(
    stores: list[StoreResult], brands: list, filters: str
) -> dict[str, str]:
    """The config these choices imply.

    BRANDS and BRAND_TOKENS are produced together and never separately — a
    brand without its token is the silent-empty-scan failure.
    """
    return {
        "STORES": store_env_value(stores),
        "BRANDS": ",".join(b.name for b in brands),
        "BRAND_TOKENS": ",".join(b.config_entry for b in brands),
        "PRODUCT_LINE_FILTERS": filters,
    }


class SetupConfig(BaseModel):
    """The values a setup run writes to .env, validated before they are written.

    Store ids are digits, brands and tokens come in matched pairs (a brand
    without its token is the silent-empty-scan failure), and an alert webhook
    must be an https URL.
    """

    stores: list[str]
    brands: list[str] = []
    brand_tokens: list[str] = []
    slack_webhook_url: str = ""

    @field_validator("stores")
    @classmethod
    def _stores_are_ids(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("at least one store is required")
        bad = [s for s in v if not s.isdigit()]
        if bad:
            raise ValueError(f"store ids must be numeric, got {bad}")
        return v

    @field_validator("slack_webhook_url")
    @classmethod
    def _https_only(cls, v: str) -> str:
        if v and not v.startswith("https://"):
            raise ValueError("webhook URL must start with https://")
        return v

    def model_post_init(self, __context) -> None:
        if len(self.brands) != len(self.brand_tokens):
            raise ValueError("each brand needs exactly one token")

    @classmethod
    def from_env_values(cls, values: dict[str, str]) -> "SetupConfig":
        def split(key: str) -> list[str]:
            return [p.strip() for p in values.get(key, "").split(",") if p.strip()]

        return cls(
            stores=split("STORES"),
            brands=split("BRANDS"),
            brand_tokens=split("BRAND_TOKENS"),
            slack_webhook_url=values.get("SLACK_WEBHOOK_URL", ""),
        )
