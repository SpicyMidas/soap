"""Loads settings from the .env file next to bot.py (no extra packages needed)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

_PLACEHOLDER_TOKENS = {"", "your-bot-token-here", "paste-your-token-here"}


def load_dotenv(path: Path) -> None:
    """Minimal KEY=VALUE parser. Real environment variables take priority."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)


def _int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"[config] {name}={raw!r} is not a number, using {default}.")
        return default
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


def _color(raw: str, default: int) -> int:
    raw = raw.strip().lstrip("#")
    if not raw:
        return default
    try:
        return int(raw, 16)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int | None
    default_volume: int
    idle_timeout: int
    leave_when_done: bool
    max_queue_size: int
    cookies_file: str | None
    cookies_from_browser: str | None
    embed_color: int


def load_config() -> Config:
    load_dotenv(ENV_FILE)

    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if token.lower() in _PLACEHOLDER_TOKENS:
        print(
            "\n[!] No bot token found.\n"
            f"    Open {ENV_FILE} and paste your token after DISCORD_TOKEN=\n"
            "    (Discord Developer Portal -> your application -> Bot -> Reset Token)\n"
            "    On Replit: open the Secrets tab (lock icon) and add a secret named DISCORD_TOKEN.\n"
        )
        sys.exit(1)

    guild_raw = os.environ.get("GUILD_ID", "").strip()
    guild_id = int(guild_raw) if guild_raw.isdigit() else None

    cookies_file = os.environ.get("COOKIES_FILE", "").strip() or None
    if cookies_file:
        path = Path(cookies_file)
        if not path.is_absolute():
            path = ROOT / path
        if path.exists():
            cookies_file = str(path)
        else:
            print(f"[config] COOKIES_FILE {path} does not exist, ignoring it.")
            cookies_file = None

    return Config(
        token=token,
        guild_id=guild_id,
        default_volume=_int("DEFAULT_VOLUME", 80, 0, 200),
        idle_timeout=_int("IDLE_TIMEOUT", 300, 10),
        leave_when_done=_bool("LEAVE_WHEN_DONE", True),
        max_queue_size=_int("MAX_QUEUE_SIZE", 500, 1),
        cookies_file=cookies_file,
        cookies_from_browser=os.environ.get("COOKIES_FROM_BROWSER", "").strip() or None,
        embed_color=_color(os.environ.get("EMBED_COLOR", ""), 0x8B5CF6),
    )
