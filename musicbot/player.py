"""One GuildPlayer per server: owns the queue, the voice connection and the live panel."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from .audio import (
    FILTERS,
    AudioFilter,
    ExtractionError,
    StderrBuffer,
    Track,
    build_source,
    describe_ffmpeg_error,
    format_duration,
    redact_links,
)
from .musicqueue import LoopMode, MusicQueue
from .ui import NowPlayingPanel

if TYPE_CHECKING:
    from .cogs.music import Music

log = logging.getLogger("musicbot.player")

PANEL_REFRESH_SECONDS = 10
LAST_ACTION_SHOW_SECONDS = 45
EARLY_FAIL_SECONDS = 3.0  # a song that stops sooner than this didn't really play
MIDSONG_MARGIN = 15  # stopping more than this many seconds before the end = the stream cut out

_LABELS = {
    "pipe": "Streaming",
    "stream": "Streaming (FFmpeg)",
    "download": "Downloading",
    "file": "Downloaded file",
}


class VoiceLost(Exception):
    """The voice connection went away and didn't come back."""


@dataclass
class _Attempt:
    ok: bool
    reason: str = ""
    position: float = 0.0
    voice_problem: bool = False
    midsong: bool = False
    details: str = ""


def _tail(text: str, lines: int = 8) -> str:
    rows = [r for r in redact_links(text).splitlines() if r.strip()]
    return "\n".join("      " + r for r in rows[-lines:])


class GuildPlayer:
    def __init__(self, cog: Music, guild: discord.Guild, text_channel: discord.abc.Messageable) -> None:
        self.cog = cog
        self.bot = cog.bot
        self.guild = guild
        self.config = cog.config
        self.extractor = cog.extractor
        self.text_channel = text_channel

        self.queue = MusicQueue(self.config.max_queue_size)
        self.current: Track | None = None
        self.last_played: Track | None = None
        self.loop = LoopMode.OFF
        self.volume = self.config.default_volume / 100
        self.filter: AudioFilter = FILTERS["none"]
        self.autoplay = False
        self.stay = False  # 24/7 mode

        self._last_action: str | None = None
        self._last_action_at = 0.0

        # playback state
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._track_done = asyncio.Event()
        self._restart_at: float | None = None
        self._skipped = False
        self._going_back = False
        self._stopping = False
        self._no_autoplay = False
        self._offset = 0.0
        self._started = 0.0
        self._paused_at: float | None = None
        self._speed = 1.0
        self._auto_paused = False
        self._alone_task: asyncio.Task | None = None
        self._leave_reason: str | None = None
        self._play_error: Exception | None = None
        self._last_track_failed = False
        self._panel_for: Track | None = None
        self._downloads: dict[int, asyncio.Task] = {}
        self._lookups: dict[int, asyncio.Task] = {}
        self._bg_tasks: set[asyncio.Task] = set()
        self.destroyed = False

        self.panel = NowPlayingPanel(self)
        self._task = asyncio.create_task(self._run(), name=f"player-{guild.id}")
        self._ticker = asyncio.create_task(self._tick(), name=f"panel-{guild.id}")

    # ------------------------------------------------------------------ state
    @property
    def vc(self) -> discord.VoiceClient | None:
        vc = self.guild.voice_client
        return vc if isinstance(vc, discord.VoiceClient) else None

    @property
    def is_playing(self) -> bool:
        vc = self.vc
        return bool(vc and (vc.is_playing() or vc.is_paused()) and self.current)

    @property
    def is_paused(self) -> bool:
        vc = self.vc
        return bool(vc and vc.is_paused())

    @property
    def position(self) -> float:
        if not self.current:
            return 0.0
        now = self._paused_at if self._paused_at is not None else time.monotonic()
        pos = self._offset + max(0.0, now - self._started) * self._speed
        if self.current.duration:
            pos = min(pos, float(self.current.duration))
        return max(0.0, pos)

    @property
    def last_action(self) -> str | None:
        if self._last_action and time.monotonic() - self._last_action_at < LAST_ACTION_SHOW_SECONDS:
            return self._last_action
        return None

    def note(self, text: str) -> None:
        """Remember who did what, shown in the panel footer."""
        self._last_action = text
        self._last_action_at = time.monotonic()

    def control_error(self, member: discord.abc.User) -> str | None:
        """Why this member may NOT control the player (None if they may)."""
        vc = self.vc
        if not vc or not vc.channel:
            return "I'm not in a voice channel right now."
        voice = getattr(member, "voice", None)
        if not voice or voice.channel != vc.channel:
            return f"Join {vc.channel.mention} to control the music."
        return None

    # ----------------------------------------------------------------- queue
    def enqueue(self, tracks: list[Track], *, front: bool = False) -> int:
        added = self.queue.add(tracks, front=front)
        if added:
            self._no_autoplay = False
            self._wake.set()
            if self.current:
                self._preload_next()  # get it ready now if it's up next
        return added

    # -------------------------------------------------------------- controls
    def pause(self) -> bool:
        vc = self.vc
        if vc and vc.is_playing():
            vc.pause()
            self._paused_at = time.monotonic()
            return True
        return False

    def resume(self) -> bool:
        vc = self.vc
        if vc and vc.is_paused():
            vc.resume()
            if self._paused_at is not None:
                self._started += time.monotonic() - self._paused_at
            self._paused_at = None
            self._auto_paused = False
            return True
        return False

    def skip(self) -> bool:
        vc = self.vc
        if vc and self.current and (vc.is_playing() or vc.is_paused()):
            self._skipped = True
            vc.stop()
            return True
        return False

    def stop(self) -> None:
        """Clear the queue and stop playing (stays in the channel)."""
        self.queue.clear()
        self._no_autoplay = True
        if self.skip():
            self._stopping = True

    async def previous(self) -> Track | None:
        prev = self.queue.take_previous()
        if prev is None:
            return None
        prev = prev.copy()
        vc = self.vc
        if self.current and vc and (vc.is_playing() or vc.is_paused()):
            self.queue.tracks[0:0] = [prev, self.current]
            self._going_back = True
            vc.stop()
        else:
            self.queue.tracks.insert(0, prev)
            self._wake.set()
        return prev

    def seek(self, seconds: float) -> bool:
        vc = self.vc
        if not (vc and self.current and (vc.is_playing() or vc.is_paused())):
            return False
        if self.current.is_live:
            return False
        if self.current.duration:
            seconds = min(seconds, max(0, self.current.duration - 1))
        self._restart_at = max(0.0, seconds)
        vc.stop()
        return True

    def set_filter(self, audio_filter: AudioFilter) -> None:
        self.filter = audio_filter
        vc = self.vc
        if vc and self.current and (vc.is_playing() or vc.is_paused()):
            self._restart_at = 0.0 if self.current.is_live else self.position
            vc.stop()

    def wake(self) -> None:
        """Make an idle player look at the queue again."""
        self._wake.set()

    def set_autoplay(self, enabled: bool) -> None:
        self.autoplay = enabled
        if enabled:
            self._no_autoplay = False
            self.wake()

    def set_stay(self, enabled: bool) -> None:
        self.stay = enabled
        if enabled and self._alone_task:
            self._alone_task.cancel()
            self._alone_task = None
        self._wake.set()  # re-evaluate the idle timeout

    def set_volume(self, percent: int) -> int:
        percent = max(0, min(200, percent))
        self.volume = percent / 100
        vc = self.vc
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = self.volume
        return percent

    # ------------------------------------------------------ alone detection
    def on_alone(self) -> None:
        if self.stay or self._alone_task:
            return
        if self.pause():
            self._auto_paused = True
            self.note("⏸️ Paused because everyone left")
            asyncio.create_task(self.panel.refresh())
        self._alone_task = asyncio.create_task(self._leave_when_alone())

    def on_company(self) -> None:
        if self._alone_task:
            self._alone_task.cancel()
            self._alone_task = None
        if self._auto_paused and self.resume():
            self.note("▶️ Welcome back! Resumed")
            asyncio.create_task(self.panel.refresh())

    async def _leave_when_alone(self) -> None:
        try:
            await asyncio.sleep(self.config.idle_timeout)
        except asyncio.CancelledError:
            return
        self._alone_task = None
        await self.destroy("👋 Left the voice channel because everyone left.")

    # ------------------------------------------------------------- main loop
    def _after(self, error: Exception | None) -> None:
        # Runs in the audio thread, hand back to the event loop.
        self._play_error = error
        self._loop.call_soon_threadsafe(self._track_done.set)

    async def _run(self) -> None:
        reason = None
        try:
            while not self.destroyed:
                track = await self._next_track()
                if track is None:
                    reason = self._leave_reason or "💤 Left the voice channel after being idle. Use `/play` to bring me back!"
                    break
                await self._play(track)
        except asyncio.CancelledError:
            return
        except VoiceLost:
            log.warning("Lost the voice connection in guild %s and it didn't come back.", self.guild.id)
            reason = "⚠️ I lost the connection to the voice channel. Use `/play` to try again."
        except Exception:
            log.exception("Player loop crashed in guild %s", self.guild.id)
            reason = "⚠️ Something went wrong with the player, so I left. Check the console for details."
        if not self.destroyed:
            await self.destroy(reason)

    async def _next_track(self) -> Track | None:
        while True:
            self._wake.clear()
            if self.queue:
                return self.queue.pop_next()

            if self.autoplay and not self._no_autoplay and self.last_played:
                related = await self._find_related()
                if related:
                    self.queue.add([related])
                    continue

            if self.config.leave_when_done and not self.stay and self.last_played is not None:
                # The queue is done: leave right away (24/7 mode keeps the bot in the channel).
                if self._last_track_failed:
                    self._leave_reason = "⚠️ The song couldn't be played (the error message says why), so I left the voice channel."
                else:
                    self._leave_reason = "✅ Finished playing, so I left the voice channel. Use `/play` to bring me back!"
                return None

            await self.panel.show_idle()
            timeout = None if self.stay else self.config.idle_timeout
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except asyncio.TimeoutError:
                return None

    async def _find_related(self) -> Track | None:
        assert self.last_played is not None
        try:
            exclude = self.queue.recent_ids([self.last_played])
            track = await self.extractor.related(self.last_played, exclude)
        except Exception as exc:
            log.warning("Autoplay lookup failed: %s", exc)
            return None
        if track and self.bot.user:
            track.requester_id = self.bot.user.id
            track.requester_name = "♾️ Autoplay"
        return track

    # ------------------------------------------------------------ playback
    async def _wait_for_voice(self, timeout: float = 15.0) -> discord.VoiceClient | None:
        """Give a (re)connecting voice client a moment instead of failing right away."""
        deadline = time.monotonic() + timeout
        while not self.destroyed:
            vc = self.vc
            if vc is None:
                return None
            if vc.is_connected():
                return vc
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.25)
        return None

    def _plan(self, track: Track) -> list[str]:
        """How to get the audio, in order of preference (decided after the lookup)."""
        if track.has_local_file:
            return ["file"]
        mode = self.config.audio_mode
        if not track.can_pipe:  # HLS (SoundCloud), live streams, ...
            if track.is_live:
                return ["stream"]
            if mode == "download":
                return ["download", "stream"]
            return ["stream"] if mode == "stream" else ["stream", "download"]
        if mode == "stream":
            return ["stream"]
        if mode == "download" or track.source in self.cog.download_sources:
            return ["download", "pipe"]
        return ["pipe", "download"]

    async def _once(self, tasks: dict[int, asyncio.Task], track: Track, factory) -> None:
        """Run factory() for this track, sharing the work if it's already running (e.g. preload)."""
        task = tasks.get(id(track))
        if task is None or task.done():
            task = asyncio.create_task(factory())
            tasks[id(track)] = task
        try:
            await asyncio.shield(task)
        finally:
            if task.done() and tasks.get(id(track)) is task:
                tasks.pop(id(track), None)

    async def _resolve(self, track: Track) -> None:
        await self._once(self._lookups, track, lambda: self.extractor.resolve(track))

    async def _download(self, track: Track) -> None:
        await self._once(self._downloads, track, lambda: self.extractor.download(track, self.cog.cache))

    async def _preload(self, track: Track) -> None:
        """Get the next song ready while this one plays, so it starts instantly."""
        try:
            if not track.has_local_file and track.needs_resolve:
                await self._resolve(track)
            first = self._plan(track)[0]
            if first == "download":
                await self._download(track)
            elif first == "pipe":
                await self._once(self._downloads, track,
                                 lambda: self.extractor.predownload(track, self.cog.cache))
            log.info("Next song is ready: %s", track.title)
        except Exception as exc:
            log.debug("Preloading %s failed: %s", track.title, exc)

    def _preload_next(self) -> None:
        if not self.queue:
            return
        nxt = self.queue[0]
        if nxt.is_live or id(nxt) in self._lookups or id(nxt) in self._downloads:
            return
        if nxt.has_local_file:
            return
        self._background(self._preload(nxt))

    def _background(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _play(self, track: Track) -> None:
        if await self._wait_for_voice() is None:
            self.queue.tracks.insert(0, track)
            raise VoiceLost()

        self._last_track_failed = False
        self._panel_for = None
        self.last_played = track
        failures: list[str] = []
        played_ok = False

        # 1) Look the song up (once). Already done by /play for single songs.
        if not track.has_local_file and track.needs_resolve:
            try:
                await self._resolve(track)
            except ExtractionError as exc:
                failures.append(f"Looking up the song: {exc}")
                log.warning("Couldn't look up %s: %s", track.title, exc)

        # If the song is still being pre-downloaded, wait for that rather than start over.
        pending = self._downloads.get(id(track))
        if pending is not None and not pending.done() and not failures:
            try:
                await asyncio.wait_for(asyncio.shield(pending), 8)
            except Exception:
                pass

        # 2) Play it, trying the next way to get the audio if one fails.
        plan = [] if failures else self._plan(track)
        position = 0.0
        resumed = False
        index = 0
        while index < len(plan) and not self.destroyed:
            how = plan[index]
            if failures or resumed:
                self._paused_at = time.monotonic()  # freeze the progress bar meanwhile
                if how == "download":
                    self.note("⏬ Streaming didn't work, downloading the song instead…")
                    self._background(self.panel.refresh())
            if how == "download":
                try:
                    await self._download(track)
                except ExtractionError as exc:
                    failures.append(f"{_LABELS[how]}: {exc}")
                    log.warning("%s failed for %s: %s", _LABELS[how], track.title, exc)
                    index += 1
                    continue
                if failures and self.config.audio_mode == "auto":
                    self.cog.remember_download(track.source)

            self.current = track
            log.info("Playing %s (%s)", track.title, _LABELS[how].lower())
            result = await self._play_attempt(track, how=how, position=position)
            if result.ok:
                played_ok = True
                break

            if result.midsong:
                if resumed:
                    log.warning("%s cut out again at %s, moving on.", track.title, format_duration(result.position))
                    played_ok = True
                    break
                resumed = True
                position = result.position
                log.warning("%s cut out at %s (%s), resuming.", track.title, format_duration(position), result.reason)
                continue
            if resumed:
                log.warning("%s couldn't be resumed (%s), moving on.", track.title, result.reason)
                played_ok = True
                break
            failures.append(f"{_LABELS[how]}: {result.reason}")
            log.warning("%s failed for %s: %s%s", _LABELS[how], track.title, result.reason,
                        f"\n    FFmpeg output:\n{result.details}" if result.details else "")
            if result.voice_problem:
                break  # another way of getting the audio won't help
            position = result.position
            index += 1

        finished = self.current
        self.current = None
        self._paused_at = None
        if not played_ok and not self.destroyed and failures:
            self._last_track_failed = True
            title = discord.utils.escape_markdown(track.title)
            reasons = "\n".join(f"• {f}" for f in failures)
            await self.send_notice(f"⚠️ Couldn't play **{title}**\n{reasons}", persist=True)
        if finished and not self._going_back and not self.destroyed and played_ok:
            if self._stopping:
                self.queue.history.append(finished)
            else:
                self.queue.finished(finished, self.loop, skipped=self._skipped)
        self._stopping = False

    async def _play_attempt(self, track: Track, *, how: str, position: float) -> _Attempt:
        """Play until the song ends, restarting FFmpeg for seeks / filter changes."""
        voice_retries = 0
        while not self.destroyed:
            vc = await self._wait_for_voice()
            if vc is None:
                raise VoiceLost()
            stderr = StderrBuffer()
            pipe = None
            # A finished pipe leaves a copy on disk: seeks/replays then use that (instant).
            use_local = how in ("file", "download") or (how == "pipe" and track.has_local_file)
            try:
                if how == "pipe" and not use_local:
                    pipe = self.extractor.open_pipe(track, self.cog.cache)
                source = build_source(
                    track,
                    ffmpeg=self.cog.ffmpeg,
                    position=position,
                    audio_filter=self.filter,
                    volume=self.volume,
                    stderr=stderr,
                    use_local=use_local,
                    pipe=pipe,
                )
            except Exception as exc:
                if pipe:
                    pipe.close()
                log.exception("Could not start FFmpeg")
                return _Attempt(False, f"couldn't start FFmpeg: {exc}", position)

            self._track_done.clear()
            self._restart_at = None
            self._skipped = False
            self._going_back = False
            self._stopping = False
            self._play_error = None
            self._offset = position
            self._speed = self.filter.speed
            self._paused_at = None
            self._started = time.monotonic()
            channel_kbps = getattr(vc.channel, "bitrate", 128_000) // 1000
            try:
                vc.play(source, after=self._after, bitrate=max(64, min(512, channel_kbps)), signal_type="music")
            except discord.ClientException as exc:
                source.cleanup()
                if pipe:
                    pipe.close()
                voice_retries += 1
                if voice_retries <= 3 and not vc.is_connected():
                    continue  # voice is reconnecting, wait and try again
                return _Attempt(False, f"Discord voice wouldn't start playing: {exc}", position, voice_problem=True)

            if self._panel_for is not track:
                self._panel_for = track
                await self.panel.send_new()
                self._preload_next()
            else:
                await self.panel.refresh()

            await self._track_done.wait()

            pipe_error = None
            if pipe is not None:
                pipe.close()
                pipe_error = pipe.error
                if pipe.saved_file:
                    track.local_file = pipe.saved_file

            if self.destroyed or self._skipped or self._going_back or self._stopping:
                return _Attempt(True)
            if self._restart_at is not None:  # seek / filter change
                position = self._restart_at
                continue

            ended_at = self.position
            played_for = time.monotonic() - self._started
            error = self._play_error
            details = _tail(stderr.text())
            reason = pipe_error or describe_ffmpeg_error(stderr.text())

            if error is not None and not isinstance(error, discord.FFmpegProcessError):
                if played_for < EARLY_FAIL_SECONDS:
                    return _Attempt(
                        False, f"sending audio to Discord failed ({type(error).__name__}: {error})",
                        position, voice_problem=True,
                    )
                log.warning("Playback of %s stopped with an error: %r", track.title, error)
                return _Attempt(True)

            near_end = bool(track.duration and position >= track.duration - 10)
            if played_for < EARLY_FAIL_SECONDS and not near_end and not (track.duration and track.duration <= EARLY_FAIL_SECONDS + 1):
                return _Attempt(False, reason, position, details=details)

            if track.duration and not track.is_live and ended_at < track.duration - MIDSONG_MARGIN:
                return _Attempt(False, reason, ended_at, midsong=True, details=details)
            return _Attempt(True)
        return _Attempt(True)

    async def _tick(self) -> None:
        """Keep the progress bar moving."""
        while not self.destroyed:
            await asyncio.sleep(PANEL_REFRESH_SECONDS)
            if self.current and not self.is_paused:
                try:
                    await self.panel.refresh()
                except Exception:
                    log.debug("Panel refresh failed", exc_info=True)

    # ------------------------------------------------------------- helpers
    async def send_notice(self, text: str, *, persist: bool = False) -> None:
        try:
            embed = discord.Embed(description=text[:4000], color=self.config.embed_color)
            await self.text_channel.send(embed=embed, delete_after=None if persist else 30)
        except discord.HTTPException:
            pass

    async def destroy(self, notice: str | None = None) -> None:
        if self.destroyed:
            return
        self.destroyed = True
        self.cog.players.pop(self.guild.id, None)

        current = asyncio.current_task()
        for task in (self._ticker, self._alone_task, self._task, *self._downloads.values(),
                     *self._lookups.values(), *self._bg_tasks):
            if task and task is not current and not task.done():
                task.cancel()

        self.queue.clear()
        self.current = None
        self._track_done.set()

        vc = self.vc
        if vc:
            try:
                vc.stop()
                await vc.disconnect(force=True)
            except Exception:
                log.debug("Disconnect failed", exc_info=True)
        await self.panel.close(notice)
