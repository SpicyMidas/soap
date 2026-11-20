"""Startup checks and the /diagnose report: find out *why* there's no sound."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

import discord

from .audio import ensure_opus

if TYPE_CHECKING:
    from .player import GuildPlayer

log = logging.getLogger("musicbot.diagnostics")


# --------------------------------------------------------------------------- #
# Library checks
# --------------------------------------------------------------------------- #


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text)[:3])


def _pkg_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _discord_supports_new_davey() -> bool:
    """davey 0.2 needs prepare_transition() calls that discord.py 2.7.1 doesn't make."""
    try:
        import discord.gateway

        return "prepare_transition" in inspect.getsource(discord.gateway)
    except Exception:
        return True  # can't tell, don't raise a false alarm


@dataclass
class Check:
    ok: bool
    text: str
    fix: str | None = None


@dataclass
class LibraryReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def check_voice_libraries() -> LibraryReport:
    report = LibraryReport()

    report.checks.append(
        Check(True, f"discord.py {discord.__version__}")
    )

    if ensure_opus():
        report.checks.append(Check(True, "Opus audio library loaded"))
    else:
        report.checks.append(
            Check(False, "Opus audio library NOT found", "Linux: `sudo apt install libopus0` • macOS: `brew install opus` • Replit: keep `pkgs.libopus` in replit.nix")
        )

    davey_version = _pkg_version("davey")
    if davey_version is None:
        report.checks.append(Check(False, "davey (voice encryption) is NOT installed", "Run: `pip install -r requirements.txt`"))
    elif _version_tuple(davey_version) >= (0, 2) and not _discord_supports_new_davey():
        report.checks.append(
            Check(
                False,
                f"davey {davey_version} doesn't work with discord.py {discord.__version__}: the bot plays SILENCE",
                'Run: `pip install "davey<0.2"` (or just restart with the start script / Replit Run button)',
            )
        )
    else:
        report.checks.append(Check(True, f"Voice encryption library: davey {davey_version}"))

    nacl_version = _pkg_version("PyNaCl")
    if nacl_version:
        report.checks.append(Check(True, f"PyNaCl {nacl_version}"))
    else:
        report.checks.append(Check(False, "PyNaCl is NOT installed", "Run: `pip install -r requirements.txt`"))
    return report


def log_startup_checks() -> None:
    report = check_voice_libraries()
    for check in report.checks:
        if check.ok:
            log.info("✓ %s", check.text)
        else:
            log.error("✗ %s\n    Fix: %s", check.text, check.fix)
    if not report.ok:
        log.error("Music will NOT be audible until the problem above is fixed.")


# --------------------------------------------------------------------------- #
# Dropped packet counter
# --------------------------------------------------------------------------- #


class _DropCounter(logging.Filter):
    """discord.py silently drops audio packets it can't send (e.g. UDP blocked).

    It only says so in a DEBUG log line, so we count those lines and hide the
    rest of the DEBUG noise.
    """

    def __init__(self) -> None:
        super().__init__()
        self.dropped = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno <= logging.DEBUG:
            if "packet has been dropped" in record.getMessage():
                self.dropped += 1
            return False
        return True


drop_counter = _DropCounter()


def install_drop_counter() -> None:
    logger = logging.getLogger("discord.voice_client")
    if drop_counter not in logger.filters:
        logger.addFilter(drop_counter)
    logger.setLevel(logging.DEBUG)


# --------------------------------------------------------------------------- #
# /diagnose
# --------------------------------------------------------------------------- #


def _line(check: Check) -> str:
    text = f"{'✅' if check.ok else '❌'} {check.text}"
    if not check.ok and check.fix:
        text += f"\n　↳ **Fix:** {check.fix}"
    return text


async def build_report(player: GuildPlayer | None, ffmpeg: str, color: int) -> discord.Embed:
    lib = check_voice_libraries()
    lines = [_line(c) for c in lib.checks]
    lines.append(f"✅ FFmpeg: `{ffmpeg}`")

    verdict = None
    if not lib.ok:
        verdict = "Fix the ❌ item(s) above, then restart the bot."

    vc = player.vc if player else None
    if not vc or not vc.is_connected():
        lines.append("ℹ️ Not in a voice channel.")
        verdict = verdict or "The libraries look fine. Play a song with `/play`, then run `/diagnose` again to test the sound itself."
        return _embed(lines, verdict, color)

    latency = vc.latency
    ping = f"{latency * 1000:.0f} ms" if latency != float("inf") else "measuring…"
    lines.append(f"✅ Connected to {vc.channel.mention} • voice ping {ping} • mode `{vc.mode}`")

    conn = getattr(vc, "_connection", None)
    dave_version = getattr(conn, "dave_protocol_version", 0)
    dave_ready = bool(getattr(conn, "can_encrypt", False))
    if dave_version == 0:
        lines.append("✅ Discord voice encryption (DAVE): not used in this call")
    elif dave_ready:
        lines.append(f"✅ Discord voice encryption (DAVE v{dave_version}): active and ready")
    else:
        lines.append(f"❌ Discord voice encryption (DAVE v{dave_version}) is NOT ready: listeners can't decode the audio")
        verdict = verdict or 'Voice encryption never got ready. Make sure `davey` is below 0.2 (`pip install "davey<0.2"`) and restart.'

    if not (player and player.current and vc.is_playing()):
        lines.append("ℹ️ Nothing is playing right now (or it's paused), so the audio flow wasn't tested.")
        verdict = verdict or "Play a song (not paused), then run `/diagnose` again to test the audio flow."
        return _embed(lines, verdict, color)

    seq_before, drops_before = vc.sequence, drop_counter.dropped
    await asyncio.sleep(2)
    sent = (vc.sequence - seq_before) % 65536
    dropped = drop_counter.dropped - drops_before

    if sent == 0:
        lines.append("❌ No audio packets in the last 2 s: FFmpeg isn't delivering any sound")
        verdict = verdict or (
            "The song's stream isn't delivering audio. Try a different song or a SoundCloud link. "
            "If only YouTube fails, YouTube is probably blocking the server: add cookies (see the README)."
        )
    elif dropped >= sent * 0.5:
        lines.append(f"❌ {dropped} of {sent} audio packets could NOT be sent in the last 2 s")
        verdict = verdict or (
            "Your host is blocking Discord's voice traffic (UDP). The bot can't fix that itself: "
            "use a host that allows outgoing UDP, or run the bot on your own PC."
        )
    else:
        lines.append(f"✅ Sending audio: {sent} packets in the last 2 s (≈100 expected)" + (f", {dropped} dropped" if dropped else ""))

    if player.volume == 0:
        lines.append("❌ The bot's volume is 0%")
        verdict = verdict or "Turn the volume up with `/volume 80` or the 🔊 button."

    me = player.guild.me
    voice_state = getattr(me, "voice", None)
    if voice_state and (voice_state.mute or voice_state.suppress):
        lines.append("❌ The bot is server-muted (or a stage audience member)")
        verdict = verdict or "Unmute the bot: right-click it in the voice channel and untick **Server Mute**."

    verdict = verdict or (
        "Everything on the bot's side works: it's sending encrypted audio to Discord. If you still hear nothing, "
        "right-click the bot in the voice channel and check its **User Volume** isn't at 0, and that you haven't muted it."
    )
    return _embed(lines, verdict, color)


def _embed(lines: list[str], verdict: str, color: int) -> discord.Embed:
    embed = discord.Embed(title="🔧 Voice diagnostics", description="\n".join(lines), color=color)
    embed.add_field(name="🩺 Verdict", value=verdict, inline=False)
    return embed
