"""Everything audio: finding songs with yt-dlp and turning them into FFmpeg streams."""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import shutil
import time
from dataclasses import dataclass, field, replace
from typing import Any

import discord
import yt_dlp

from .config import Config

log = logging.getLogger("musicbot.audio")

URL_RE = re.compile(r"^https?://", re.IGNORECASE)
YOUTUBE_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/|music\.youtube\.com/watch\?(?:.*&)?v=)"
    r"([A-Za-z0-9_-]{11})"
)
UNAVAILABLE_TITLES = {"[private video]", "[deleted video]", "[unavailable video]"}

# A resolved stream URL is reused for this long before asking yt-dlp again
# (YouTube links expire after a few hours).
STREAM_URL_TTL = 30 * 60


# --------------------------------------------------------------------------- #
# Audio filters
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AudioFilter:
    key: str
    label: str
    emoji: str
    ffmpeg: str | None
    speed: float = 1.0  # how fast the song plays compared to normal
    description: str = ""


FILTERS: dict[str, AudioFilter] = {
    f.key: f
    for f in (
        AudioFilter("none", "Off", "🚫", None, description="Original sound"),
        AudioFilter(
            "bassboost", "Bass Boost", "🔈",
            "bass=g=9:f=110:w=0.6,alimiter=limit=0.95",
            description="Heavier low end",
        ),
        AudioFilter(
            "nightcore", "Nightcore", "🌙",
            "aresample=48000,asetrate=60000,aresample=48000", 1.25,
            description="Faster and higher pitched",
        ),
        AudioFilter(
            "vaporwave", "Vaporwave", "🌴",
            "aresample=48000,asetrate=38400,aresample=48000", 0.8,
            description="Slower and lower pitched",
        ),
        AudioFilter(
            "slowed", "Slowed + Reverb", "🌫️",
            "aresample=48000,asetrate=40800,aresample=48000,aecho=0.8:0.88:60|90:0.35|0.25", 0.85,
            description="Slowed down with a dreamy echo",
        ),
        AudioFilter(
            "8d", "8D Audio", "🎧",
            "apulsator=hz=0.08",
            description="Sound circles around your head (use headphones)",
        ),
        AudioFilter(
            "karaoke", "Karaoke", "🎤",
            "stereotools=mlev=0.05",
            description="Tries to remove the vocals",
        ),
        AudioFilter(
            "treble", "Treble Boost", "✨",
            "treble=g=6,alimiter=limit=0.95",
            description="Crisper highs",
        ),
        AudioFilter(
            "lofi", "Lo-Fi", "📻",
            "highpass=f=200,lowpass=f=3200",
            description="Muffled old-radio sound",
        ),
    )
}


# --------------------------------------------------------------------------- #
# Track model
# --------------------------------------------------------------------------- #


@dataclass
class Track:
    title: str
    url: str  # page URL (stable); the stream URL is looked up right before playing
    duration: int | None = None  # seconds; None for live streams / unknown
    uploader: str | None = None
    thumbnail: str | None = None
    source: str = "Unknown"
    video_id: str | None = None
    is_live: bool = False
    requester_id: int = 0
    requester_name: str = "Unknown"

    stream_url: str | None = field(default=None, repr=False)
    http_headers: dict[str, str] = field(default_factory=dict, repr=False)
    protocol: str | None = field(default=None, repr=False)
    resolved_at: float = field(default=0.0, repr=False)

    @property
    def is_youtube(self) -> bool:
        return self.video_id is not None

    @property
    def is_hls(self) -> bool:
        return bool(self.protocol and "m3u8" in self.protocol)

    @property
    def needs_resolve(self) -> bool:
        return not self.stream_url or time.time() - self.resolved_at > STREAM_URL_TTL

    def copy(self) -> Track:
        return replace(self, http_headers=dict(self.http_headers))

    def requested_by(self, user: discord.abc.User) -> Track:
        self.requester_id = user.id
        self.requester_name = user.display_name
        return self


def is_url(text: str) -> bool:
    return bool(URL_RE.match(text.strip()))


def youtube_id(url: str) -> str | None:
    m = YOUTUBE_ID_RE.search(url)
    return m.group(1) if m else None


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "LIVE"
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_timestamp(text: str) -> int | None:
    """'90', '1:30', '1:02:03', '1m30s', '2m' -> seconds."""
    text = text.strip().lower()
    if not text:
        return None
    if re.fullmatch(r"\d+(:\d{1,2}){0,2}", text):
        total = 0
        for part in text.split(":"):
            total = total * 60 + int(part)
        return total
    m = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", text)
    if m and any(m.groups()):
        h, mi, s = (int(g) if g else 0 for g in m.groups())
        return h * 3600 + mi * 60 + s
    return None


# --------------------------------------------------------------------------- #
# Tool discovery
# --------------------------------------------------------------------------- #


def find_ffmpeg() -> str:
    """System FFmpeg if installed, otherwise the copy bundled with imageio-ffmpeg."""
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - depends on the machine
        raise RuntimeError(
            "FFmpeg was not found. Run the start script again (it installs it automatically) "
            "or install FFmpeg from https://ffmpeg.org/download.html"
        ) from exc


def find_js_runtimes() -> dict[str, dict[str, Any]]:
    """yt-dlp needs a JavaScript runtime for YouTube. Prefer the pip-installed Deno."""
    try:
        import deno

        return {"deno": {"path": deno.find_deno_bin()}}
    except Exception:
        pass
    for name in ("deno", "node", "bun"):
        path = shutil.which(name)
        if path:
            return {name: {"path": path}}
    log.warning(
        "No JavaScript runtime found (deno/node/bun). YouTube playback may fail. "
        "Run: pip install -U \"yt-dlp[default,deno]\""
    )
    return {"deno": {}}


# --------------------------------------------------------------------------- #
# yt-dlp wrapper
# --------------------------------------------------------------------------- #


class _YtdlLogger:
    def debug(self, msg: str) -> None:
        if msg.startswith("[debug] "):
            log.debug(msg)

    def info(self, msg: str) -> None:
        log.debug(msg)

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)


class ExtractionError(Exception):
    """A friendly error to show to the user."""


def _friendly_error(exc: Exception) -> ExtractionError:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
    text = text.replace("ERROR: ", "")
    low = text.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return ExtractionError(
            "YouTube is asking the bot to sign in (anti-bot check). "
            "Set COOKIES_FILE or COOKIES_FROM_BROWSER in your .env - see the README."
        )
    if "private video" in low:
        return ExtractionError("That video is private.")
    if "age" in low and ("restrict" in low or "confirm your age" in low):
        return ExtractionError("That video is age-restricted. Add YouTube cookies to play it (see the README).")
    if "unsupported url" in low:
        return ExtractionError("I can't play that link. Try a YouTube, SoundCloud, Bandcamp or direct audio link.")
    if "video unavailable" in low or "not available" in low:
        return ExtractionError("That video is unavailable.")
    # keep the last line, it usually carries the reason
    reason = text.strip().splitlines()[-1] if text.strip() else "unknown error"
    return ExtractionError(reason[:300])


def _thumbnail(entry: dict[str, Any], video_id: str | None) -> str | None:
    if entry.get("thumbnail"):
        return entry["thumbnail"]
    thumbs = entry.get("thumbnails") or []
    for thumb in reversed(thumbs):
        if thumb.get("url"):
            return thumb["url"]
    if video_id:
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return None


def _source_name(extractor: str) -> str:
    if not extractor or extractor.lower() == "generic":
        return "Web"
    if extractor == "YouTube":
        return extractor
    pretty = {"soundcloud": "SoundCloud", "bandcamp": "Bandcamp", "twitch": "Twitch", "vimeo": "Vimeo"}
    base = extractor.split(":")[0].lower()
    return pretty.get(base, extractor.split(":")[0].title())


def track_from_entry(entry: dict[str, Any]) -> Track | None:
    """Build a Track from a full or 'flat' yt-dlp result."""
    title = (entry.get("title") or "").strip()
    if title.lower() in UNAVAILABLE_TITLES:
        return None

    extractor = entry.get("ie_key") or entry.get("extractor_key") or entry.get("extractor") or ""
    page_url = entry.get("webpage_url") or entry.get("original_url")
    if not page_url:
        page_url = entry.get("url")
    if not page_url:
        return None

    video_id = None
    if extractor.lower().startswith("youtube") and entry.get("id") and len(str(entry["id"])) == 11:
        video_id = str(entry["id"])
    video_id = video_id or youtube_id(page_url)
    if video_id:
        page_url = f"https://www.youtube.com/watch?v={video_id}"
        extractor = "YouTube"

    duration = entry.get("duration")
    is_live = bool(entry.get("is_live") or entry.get("live_status") == "is_live")

    track = Track(
        title=title or page_url,
        url=page_url,
        duration=int(duration) if duration and not is_live else None,
        uploader=entry.get("uploader") or entry.get("channel") or entry.get("artist") or entry.get("creator"),
        thumbnail=_thumbnail(entry, video_id),
        source=_source_name(extractor),
        video_id=video_id,
        is_live=is_live,
    )
    if entry.get("formats") is not None and entry.get("url"):
        _apply_stream(track, entry)
    return track


def _apply_stream(track: Track, info: dict[str, Any]) -> None:
    track.stream_url = info["url"]
    track.http_headers = dict(info.get("http_headers") or {})
    track.protocol = info.get("protocol")
    track.resolved_at = time.time()


class Extractor:
    """Runs yt-dlp in background threads so the bot never freezes."""

    FORMAT = "bestaudio[acodec=opus]/bestaudio/best[height<=480]/best"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.js_runtimes = find_js_runtimes()

    def _options(self, **extra: Any) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "skip_download": True,
            "logger": _YtdlLogger(),
            "format": self.FORMAT,
            "noplaylist": True,
            "extract_flat": "in_playlist",
            "default_search": "ytsearch",
            "js_runtimes": self.js_runtimes,
            "socket_timeout": 15,
        }
        if self.config.cookies_file:
            opts["cookiefile"] = self.config.cookies_file
        if self.config.cookies_from_browser:
            opts["cookiesfrombrowser"] = (self.config.cookies_from_browser,)
        opts.update(extra)
        return opts

    def _extract(self, query: str, **extra: Any) -> dict[str, Any] | None:
        with yt_dlp.YoutubeDL(self._options(**extra)) as ydl:
            return ydl.extract_info(query, download=False)

    async def _run(self, query: str, **extra: Any) -> dict[str, Any] | None:
        try:
            return await asyncio.to_thread(self._extract, query, **extra)
        except yt_dlp.utils.DownloadError as exc:
            raise _friendly_error(exc) from None
        except yt_dlp.utils.YoutubeDLError as exc:
            raise _friendly_error(exc) from None

    async def search(self, query: str, limit: int = 5) -> list[Track]:
        info = await self._run(f"ytsearch{limit}:{query}")
        entries = (info or {}).get("entries") or []
        return [t for t in (track_from_entry(e) for e in entries if e) if t]

    async def fetch(self, query: str, limit: int = 500) -> tuple[list[Track], str | None]:
        """Turn whatever the user typed into tracks.

        Returns (tracks, playlist_title). playlist_title is None for a single song/search.
        """
        query = query.strip().strip("<>")
        if not query:
            return [], None

        if is_url(query):
            info = await self._run(query, playlistend=limit)
        else:
            info = await self._run(f"ytsearch1:{query}")
        if not info:
            return [], None

        if info.get("_type") == "playlist" or "entries" in info:
            entries = [e for e in (info.get("entries") or []) if e]
            tracks = [t for t in (track_from_entry(e) for e in entries[:limit]) if t]
            is_search = (info.get("extractor_key") or "").lower().endswith("search")
            return tracks, None if is_search else (info.get("title") or "Playlist")

        track = track_from_entry(info)
        return ([track] if track else []), None

    async def resolve(self, track: Track) -> Track:
        """Make sure the track has a fresh, playable stream URL."""
        if not track.needs_resolve:
            return track
        info = await self._run(track.url, extract_flat=False)
        if info and info.get("entries"):
            info = next((e for e in info["entries"] if e), None)
        if not info or not info.get("url"):
            raise ExtractionError("Couldn't find a playable audio stream for this song.")

        _apply_stream(track, info)
        fresh = track_from_entry(info)
        if fresh:
            track.title = fresh.title or track.title
            track.duration = fresh.duration if fresh.duration is not None else track.duration
            track.uploader = fresh.uploader or track.uploader
            track.thumbnail = fresh.thumbnail or track.thumbnail
            track.is_live = fresh.is_live
            if fresh.is_live:
                track.duration = None
        return track

    async def related(self, track: Track, exclude: set[str]) -> Track | None:
        """Pick a song to play next for autoplay, using YouTube's own 'Mix' playlist."""
        video_id = track.video_id
        if not video_id:
            results = await self.search(f"{track.title} {track.uploader or ''}".strip(), limit=1)
            if not results or not results[0].video_id:
                return None
            video_id = results[0].video_id
            exclude = exclude | {video_id}

        mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
        info = await self._run(mix_url, noplaylist=False, playlistend=25)
        for entry in (info or {}).get("entries") or []:
            if not entry:
                continue
            candidate = track_from_entry(entry)
            if candidate and candidate.video_id and candidate.video_id not in exclude:
                if candidate.duration and candidate.duration > 60 * 20:
                    continue  # skip hour-long mixes
                return candidate
        return None


# --------------------------------------------------------------------------- #
# FFmpeg
# --------------------------------------------------------------------------- #


def build_source(
    track: Track,
    *,
    ffmpeg: str,
    position: float = 0.0,
    audio_filter: AudioFilter = FILTERS["none"],
    volume: float = 1.0,
) -> discord.PCMVolumeTransformer:
    """Create the FFmpeg -> PCM -> volume pipeline for a resolved track."""
    if not track.stream_url:
        raise ExtractionError("Track was not resolved before playing.")

    before = ["-nostdin"]
    if track.stream_url.startswith(("http://", "https://")):
        # Recover from network hiccups instead of ending the song early.
        before += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    user_agent = track.http_headers.get("User-Agent")
    if user_agent:
        before += ["-user_agent", user_agent]
    options = ["-vn", "-sn", "-dn"]

    position = max(0.0, position)
    if position >= 1 and not track.is_live:
        # Seeking before -i is instant for normal files but fails on HLS (SoundCloud),
        # where seeking after -i works reliably.
        if track.is_hls:
            options += ["-ss", f"{position:.2f}"]
        else:
            before += ["-ss", f"{position:.2f}"]

    if audio_filter.ffmpeg:
        options += ["-af", audio_filter.ffmpeg]

    source = discord.FFmpegPCMAudio(
        track.stream_url,
        executable=ffmpeg,
        before_options=shlex.join(before),
        options=shlex.join(options),
    )
    return discord.PCMVolumeTransformer(source, volume=volume)
