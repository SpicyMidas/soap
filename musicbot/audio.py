"""Everything audio: finding songs with yt-dlp and turning them into FFmpeg streams."""

from __future__ import annotations

import asyncio
import copy
import glob
import logging
import os
import re
import shlex
import shutil
import tempfile
import threading
import time
from pathlib import Path
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
    local_file: str | None = field(default=None, repr=False)  # set when downloaded instead of streamed
    http_headers: dict[str, str] = field(default_factory=dict, repr=False)
    info: dict[str, Any] | None = field(default=None, repr=False, compare=False)  # full yt-dlp result
    filesize: int | None = field(default=None, repr=False)
    chunk_size: int | None = field(default=None, repr=False)
    protocol: str | None = field(default=None, repr=False)
    resolved_at: float = field(default=0.0, repr=False)

    @property
    def is_youtube(self) -> bool:
        return self.video_id is not None

    @property
    def is_hls(self) -> bool:
        return bool(self.protocol and "m3u8" in self.protocol)

    @property
    def can_pipe(self) -> bool:
        """Plain web file (not HLS/DASH/live): we can fetch it ourselves and pipe it into FFmpeg."""
        return bool(
            self.stream_url
            and self.stream_url.startswith(("http://", "https://"))
            and self.protocol in ("http", "https")
            and not self.is_live
        )

    @property
    def has_local_file(self) -> bool:
        return bool(self.local_file and os.path.exists(self.local_file))

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


_OPUS_DIRS = (
    "/opt/homebrew/lib",  # macOS (Apple Silicon Homebrew)
    "/usr/local/lib",  # macOS (Intel Homebrew) / manual installs
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib/aarch64-linux-gnu",
    "/usr/lib64",
    "/usr/lib",
)


def ensure_opus() -> bool:
    """Load the Opus library discord.py needs to send voice.

    Windows: bundled with discord.py. macOS/Linux: uses the system library, which
    discord.py can't always find on its own (e.g. on Replit/Nix or Homebrew).
    """
    if discord.opus.is_loaded() or discord.opus._load_default():
        return True

    candidates: list[str] = []
    if os.environ.get("OPUS_LIBRARY"):
        candidates.append(os.environ["OPUS_LIBRARY"])
    dirs: list[str] = []
    for var in ("REPLIT_LD_LIBRARY_PATH", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        dirs += [d for d in os.environ.get(var, "").split(os.pathsep) if d]
    dirs += list(_OPUS_DIRS)
    for d in dirs:
        for pattern in ("libopus.so*", "libopus*.dylib"):
            candidates += sorted(glob.glob(os.path.join(d, pattern)))

    for path in candidates:
        try:
            discord.opus.load_opus(path)
        except Exception:
            continue
        if discord.opus.is_loaded():
            log.info("Loaded Opus from %s", path)
            return True

    log.error(
        "The Opus audio library was not found, so the bot can't send music to voice channels!\n"
        "    Linux (Debian/Ubuntu): sudo apt install libopus0\n"
        "    macOS:                 brew install opus\n"
        "    Replit:                make sure replit.nix contains pkgs.libopus\n"
        "    Or set OPUS_LIBRARY in .env to the full path of libopus."
    )
    return False


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
    track.filesize = info.get("filesize")  # exact size only: an estimate could cut the song short
    track.chunk_size = (info.get("downloader_options") or {}).get("http_chunk_size")
    track.info = info
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
            # Search and look up the audio in one go (saves a whole round trip).
            info = await self._run(f"ytsearch1:{query}", extract_flat=False)
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

    def _download(self, url: str, folder: Path, info: dict[str, Any] | None = None) -> str:
        opts = self._options(
            skip_download=False,
            extract_flat=False,
            outtmpl=str(folder / "%(extractor_key)s-%(id)s.%(ext)s"),
            nopart=True,
            overwrites=True,
            retries=3,
            fragment_retries=3,
            noprogress=True,
        )
        with yt_dlp.YoutubeDL(opts) as ydl:
            if info is not None:
                # Reuse the lookup we already did instead of asking YouTube again.
                info = ydl.process_ie_result(copy.deepcopy(info), download=True)
            else:
                info = ydl.extract_info(url, download=True)
            if info and info.get("entries"):
                info = next((e for e in info["entries"] if e), None)
            if not info:
                raise ExtractionError("Download returned nothing.")
            for item in info.get("requested_downloads") or []:
                path = item.get("filepath")
                if path and os.path.exists(path):
                    return path
            path = ydl.prepare_filename(info)
            if os.path.exists(path):
                return path
        raise ExtractionError("The download finished but the file is missing.")

    async def download(self, track: Track, cache: AudioCache) -> Track:
        """Let yt-dlp download the audio (more reliable than streaming on some networks)."""
        if track.has_local_file:
            return track
        if track.is_live:
            raise ExtractionError("Live streams can't be downloaded.")
        if track.duration and track.duration > MAX_DOWNLOAD_SECONDS:
            raise ExtractionError("This video is too long to download (over 3 hours).")
        cache.trim()
        info = track.info if track.info is not None and not track.needs_resolve else None
        try:
            path = await asyncio.to_thread(self._download, track.url, cache.folder, info)
        except yt_dlp.utils.YoutubeDLError as exc:
            raise _friendly_error(exc) from None
        track.local_file = path
        return track

    def open_pipe(self, track: Track, cache: AudioCache) -> PipeReader:
        """Download with yt-dlp's own downloader while FFmpeg plays the file as it grows."""
        cache.trim()
        key = re.sub(r"[^A-Za-z0-9_-]", "_", track.video_id or str(abs(hash(track.url))))
        ext = (track.info or {}).get("ext") or "audio"
        info = track.info if track.info is not None and not track.needs_resolve else None
        url = track.url

        def run_download(path: Path, hooks: list) -> None:
            opts = self._options(
                skip_download=False,
                extract_flat=False,
                outtmpl=str(path).replace("%", "%%"),
                nopart=True,
                overwrites=True,
                updatetime=False,
                progress_hooks=hooks,
                retries=3,
                fragment_retries=3,
            )
            with yt_dlp.YoutubeDL(opts) as ydl:
                if info is not None:
                    # Reuse the lookup we already did: no extra request to YouTube.
                    ydl.process_ie_result(copy.deepcopy(info), download=True)
                else:
                    ydl.extract_info(url, download=True)

        return PipeReader(
            run_download=run_download,
            save_to=cache.folder / f"{key}.{ext}",
            # From now on seeks, replays and loops use the file (instant, no network).
            on_complete=lambda path: setattr(track, "local_file", path),
        )

    async def predownload(self, track: Track, cache: AudioCache) -> Track:
        """Download the next song the fast, yt-dlp-style way so it starts instantly."""
        if track.has_local_file:
            return track
        pipe = self.open_pipe(track, cache)
        pipe.start()
        assert pipe._thread is not None
        await asyncio.to_thread(pipe._thread.join)
        pipe.close()
        if pipe.error or not track.has_local_file:
            raise ExtractionError(pipe.error or "pre-download didn't finish")
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


MAX_DOWNLOAD_SECONDS = 3 * 60 * 60


class PipeReader:
    """File-like object: yt-dlp downloads the song into the cache while FFmpeg plays it.

    Some networks get "403 Forbidden" when anything but yt-dlp's own downloader asks YouTube
    for the audio. So a background thread runs yt-dlp's real downloader into a file, and read()
    hands FFmpeg whatever has been written so far: music starts with the first bytes instead of
    after the whole download. Once complete, the file is reused for seeks/replays/loops.
    read() never raises: errors are stored in .error and the stream just ends.
    """

    BLOCK = 64 * 1024

    def __init__(self, *, run_download, save_to: Path | None, on_complete=None) -> None:
        self._run_download = run_download
        self._on_complete = on_complete
        self.path = save_to or Path(tempfile.gettempdir()) / f"soap-pipe-{id(self)}.audio"
        self.keep = save_to is not None
        self.error: str | None = None
        self.complete = False
        self.saved_file: str | None = None
        self._read_pos = 0
        self._finished = False
        self._closed = False
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._reader = None

    # -- background download -----------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._download, name="soap-pipe", daemon=True)
            self._thread.start()

    def _hook(self, status: dict) -> None:
        if self._closed:
            raise yt_dlp.utils.DownloadCancelled("stopped")
        with self._cond:
            self._cond.notify_all()  # new data on disk

    def _download(self) -> None:
        try:
            try:
                self.path.unlink()  # never play a stale copy
            except OSError:
                pass
            self._run_download(self.path, [self._hook])
            if not self._closed and self.path.exists():
                self.complete = True
        except Exception as exc:
            if not self._closed:
                self.error = redact_links(self._describe(exc))
                log.debug("Download for streaming failed: %s", exc)
        finally:
            if self.complete and self.keep:
                self.saved_file = str(self.path)
                if self._on_complete is not None:
                    try:
                        self._on_complete(self.saved_file)
                    except Exception:
                        log.debug("on_complete failed", exc_info=True)
            with self._cond:
                self._finished = True
                self._cond.notify_all()

    def _size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    @staticmethod
    def _describe(exc: Exception) -> str:
        text = str(exc)
        if "403" in text:
            return "YouTube refused the audio download (HTTP 403)"
        if isinstance(exc, yt_dlp.utils.YoutubeDLError):
            return str(_friendly_error(exc))
        return f"the audio download failed ({type(exc).__name__}: {text[:120]})"

    # -- file-like API for discord.py (called from its writer thread) ---------------
    def read(self, n: int = -1) -> bytes:
        self.start()
        n = n if n and n > 0 else self.BLOCK
        with self._cond:
            while True:
                if self._closed:
                    self._close_reader()
                    return b""
                available = self._size() - self._read_pos
                if available > 0:
                    break
                if self._finished:
                    self._close_reader()
                    return b""  # finished (or failed: see .error)
                self._cond.wait(0.1)
        try:
            if self._reader is None:
                self._reader = open(self.path, "rb")
            self._reader.seek(self._read_pos)
            data = self._reader.read(min(n, available))
        except (OSError, ValueError) as exc:  # ValueError: closed by cleanup meanwhile
            self.error = self.error or f"couldn't read the downloaded audio ({exc})"
            return b""
        self._read_pos += len(data)
        return data

    def _close_reader(self) -> None:
        if self._reader is not None:
            try:
                self._reader.close()
            except OSError:
                pass
            self._reader = None

    def close(self) -> None:
        """Stop. Never blocks. Half-downloaded files are removed by the cleanup thread."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        threading.Thread(target=self._cleanup, name="soap-pipe-cleanup", daemon=True).start()

    def _cleanup(self) -> None:
        if self._thread is not None:
            self._thread.join(timeout=20)
        self._close_reader()
        if not self.complete or not self.keep:
            for _ in range(10):  # Windows can't delete a file that's still open; retry briefly
                try:
                    if self.path.exists():
                        self.path.unlink()
                    break
                except OSError:
                    time.sleep(0.3)


class AudioCache:
    """Folder for downloaded songs. Emptied on start, keeps only the newest files."""

    def __init__(self, max_files: int = 12) -> None:
        self.folder = Path(tempfile.gettempdir()) / "soap-music-bot-cache"
        self.max_files = max_files
        shutil.rmtree(self.folder, ignore_errors=True)
        self.folder.mkdir(parents=True, exist_ok=True)

    def trim(self) -> None:
        try:
            files = sorted(self.folder.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for old in files[self.max_files :]:
            try:
                old.unlink()
            except OSError:
                pass


class StderrBuffer:
    """Collects the last few KB of FFmpeg's error output so we can show *why* it failed."""

    LIMIT = 8192

    def __init__(self) -> None:
        self._data = bytearray()
        self._lock = threading.Lock()

    def write(self, data: bytes) -> int:
        if not data:
            time.sleep(0.05)  # discord.py keeps calling us after FFmpeg exits; don't spin the CPU
            return 0
        with self._lock:
            self._data += data
            del self._data[: -self.LIMIT]
        return len(data)

    def flush(self) -> None:
        pass

    def text(self) -> str:
        with self._lock:
            return self._data.decode(errors="replace")


_LINK_RE = re.compile(r"(?:https?|file)://\S+|[A-Za-z]:\\\S+|/\S*/\S+")


def redact_links(text: str) -> str:
    """Remove stream links / file paths (YouTube links contain your IP address)."""
    return _LINK_RE.sub("<link>", text)


def describe_ffmpeg_error(stderr: str) -> str:
    """Turn FFmpeg's error output into a short, human explanation (links removed)."""
    stderr = redact_links(stderr)
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    low = stderr.lower()
    if "403" in low and "forbidden" in low:
        hint = "the site refused the audio stream (HTTP 403)"
    elif "404" in low and "not found" in low:
        hint = "the audio stream link wasn't found (HTTP 404)"
    elif "timed out" in low or "connection refused" in low or "network is unreachable" in low:
        hint = "a network problem while fetching the audio"
    elif "invalid data found" in low:
        hint = "FFmpeg couldn't read that audio format"
    elif "option" in low and "not found" in low:
        hint = "this FFmpeg version doesn't support an option the bot uses"
    elif not lines:
        return "the audio stream ended right away without an error message"
    else:
        hint = "FFmpeg couldn't play the audio"
        detail = " | ".join(lines[-2:])[:200]
        return f"{hint} (FFmpeg says: {detail})"
    return hint


class _FFmpegPCMAudio(discord.FFmpegPCMAudio):
    """discord.py 2.7.1's pipe writer crashes (AttributeError on a cleared stdin) when a song
    is stopped while it waits for data. Same logic, but it checks for that."""

    def _pipe_writer(self, source) -> None:  # type: ignore[override]
        while self._process:
            data = source.read(self.BLOCKSIZE)
            stdin = self._stdin
            if not data:
                if stdin:  # discord's MISSING placeholder is falsy
                    try:
                        stdin.close()
                    except Exception:
                        pass
                return
            if not stdin:
                return
            try:
                stdin.write(data)
            except Exception:
                process = self._process
                if process:
                    try:
                        process.terminate()
                    except Exception:
                        pass
                return


def build_source(
    track: Track,
    *,
    ffmpeg: str,
    position: float = 0.0,
    audio_filter: AudioFilter = FILTERS["none"],
    volume: float = 1.0,
    stderr: StderrBuffer | None = None,
    use_local: bool = True,
    pipe: PipeReader | None = None,
) -> discord.PCMVolumeTransformer:
    """Create the FFmpeg -> PCM -> volume pipeline for a resolved or downloaded track."""
    local = track.local_file if use_local and track.has_local_file and pipe is None else None
    if pipe is None and not local and not track.stream_url:
        raise ExtractionError("Track was not resolved before playing.")
    source_path = local or track.stream_url or ""

    before = [] if pipe is not None else ["-nostdin"]
    if pipe is None and not local and source_path.startswith(("http://", "https://")):
        # Recover from network hiccups instead of ending the song early.
        before += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    user_agent = track.http_headers.get("User-Agent")
    if user_agent and not local and pipe is None:
        before += ["-user_agent", user_agent]
    options = ["-vn", "-sn", "-dn"]

    position = max(0.0, position)
    if position >= 1 and not track.is_live:
        # Seeking before -i is instant for normal files but fails on HLS (SoundCloud),
        # where seeking after -i works reliably.
        if (track.is_hls and not local) or pipe is not None:
            options += ["-ss", f"{position:.2f}"]  # can't jump ahead in a pipe/HLS, so skip forward
        else:
            before += ["-ss", f"{position:.2f}"]

    if audio_filter.ffmpeg:
        options += ["-af", audio_filter.ffmpeg]

    source = _FFmpegPCMAudio(
        pipe if pipe is not None else source_path,  # type: ignore[arg-type]
        executable=ffmpeg,
        pipe=pipe is not None,
        before_options=shlex.join(before),
        options=shlex.join(options),
        stderr=stderr,  # type: ignore[arg-type]  # any object with write() works
    )
    return discord.PCMVolumeTransformer(source, volume=volume)
