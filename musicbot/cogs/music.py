"""All the slash commands."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..audio import FILTERS, ExtractionError, Extractor, Track, find_ffmpeg, format_duration, is_url, parse_timestamp
from ..config import Config
from ..musicqueue import LoopMode, QueueFull
from ..player import GuildPlayer
from ..ui import (
    QueueView,
    SearchView,
    added_embed,
    clean,
    error_embed,
    help_embed,
    ok_embed,
    queue_embed,
    search_embed,
    track_link,
    truncate,
)

log = logging.getLogger("musicbot.music")

AUTOCOMPLETE_TIMEOUT = 2.5
AUTOCOMPLETE_CACHE_SIZE = 256


class MusicError(app_commands.AppCommandError):
    """An error message that is shown to the user as-is."""


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot, config: Config) -> None:
        self.bot = bot
        self.config = config
        self.extractor = Extractor(config)
        self.ffmpeg = find_ffmpeg()
        self.players: dict[int, GuildPlayer] = {}
        self._voice_locks: dict[int, asyncio.Lock] = {}
        self._ac_cache: OrderedDict[str, list[Track]] = OrderedDict()
        log.info("Using FFmpeg: %s", self.ffmpeg)
        log.info("Using JavaScript runtime for YouTube: %s", ", ".join(self.extractor.js_runtimes))

    async def cog_unload(self) -> None:
        for player in list(self.players.values()):
            await player.destroy("🔄 The bot is restarting.")

    @property
    def color(self) -> int:
        return self.config.embed_color

    # ------------------------------------------------------------------ helpers
    def _member(self, interaction: discord.Interaction) -> discord.Member:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            raise MusicError("This only works in a server.")
        return interaction.user

    async def ensure_voice(self, interaction: discord.Interaction) -> GuildPlayer:
        """Join the user's voice channel if needed and return the player."""
        member = self._member(interaction)
        guild = member.guild
        if not member.voice or not member.voice.channel:
            raise MusicError("Join a voice channel first, then try again!")
        channel = member.voice.channel

        lock = self._voice_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            vc = guild.voice_client
            player = self.players.get(guild.id)

            if vc is None:
                perms = channel.permissions_for(guild.me)
                if not perms.connect or not perms.speak:
                    raise MusicError(f"I need the **Connect** and **Speak** permissions in {channel.mention}.")
                if channel.user_limit and len(channel.members) >= channel.user_limit and not perms.move_members:
                    raise MusicError(f"{channel.mention} is full.")
                try:
                    await channel.connect(timeout=20, self_deaf=True)
                except asyncio.TimeoutError:
                    raise MusicError("Couldn't connect to the voice channel (timed out). Try again.") from None
                except discord.ClientException as exc:
                    raise MusicError(f"Couldn't connect to voice: {exc}") from None
                if isinstance(channel, discord.StageChannel):
                    try:
                        await guild.me.edit(suppress=False)
                    except discord.HTTPException:
                        pass
            elif vc.channel != channel:
                if player and (player.current or player.queue):
                    raise MusicError(f"I'm already playing music in {vc.channel.mention}. Join me there!")
                await vc.move_to(channel)  # type: ignore[union-attr]

            if player is None or player.destroyed:
                player = GuildPlayer(self, guild, interaction.channel)  # type: ignore[arg-type]
                self.players[guild.id] = player
            if interaction.channel is not None:
                player.text_channel = interaction.channel  # type: ignore[assignment]
            return player

    def get_controllable(self, interaction: discord.Interaction, *, need_track: bool = False) -> GuildPlayer:
        """Return the player if the user is allowed to control it."""
        member = self._member(interaction)
        player = self.players.get(member.guild.id)
        if player is None or player.destroyed:
            raise MusicError("I'm not playing anything right now. Use `/play` to start!")
        reason = player.control_error(member)
        if reason:
            raise MusicError(reason)
        if need_track and not player.current:
            raise MusicError("Nothing is playing right now.")
        return player

    async def reply(self, interaction: discord.Interaction, text: str, *, ephemeral: bool = False) -> None:
        embed = ok_embed(text, self.color)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

    async def refresh_panel(self, player: GuildPlayer) -> None:
        if player.current:
            await player.panel.refresh()

    async def _queue_query(self, interaction: discord.Interaction, query: str, *, front: bool) -> None:
        await interaction.response.defer(thinking=True)
        player = await self.ensure_voice(interaction)
        was_idle = not player.current and not player.queue

        try:
            tracks, playlist = await self.extractor.fetch(query, limit=max(1, player.queue.space_left))
        except ExtractionError as exc:
            raise MusicError(str(exc)) from None
        if not tracks:
            if player.queue.space_left == 0:
                raise MusicError(f"The queue is full ({self.config.max_queue_size} songs).")
            raise MusicError(f"No results for **{clean(query, 100)}**.")

        for track in tracks:
            track.requested_by(interaction.user)
        try:
            added = player.enqueue(tracks, front=front)
        except QueueFull as exc:
            raise MusicError(str(exc)) from None

        tracks = tracks[:added]
        name = interaction.user.display_name
        if playlist or len(tracks) > 1:
            player.note(f"📥 {name} added {len(tracks)} songs")
        else:
            player.note(f"📥 {name} added {truncate(tracks[0].title, 60)}")

        embed = added_embed(player, tracks, playlist, front=front, playing_now=was_idle and len(tracks) == 1)
        await interaction.followup.send(embed=embed)
        await self.refresh_panel(player)

    # ----------------------------------------------------------- autocomplete
    async def _suggest(self, current: str) -> list[app_commands.Choice[str]]:
        current = current.strip()
        if len(current) < 3 or is_url(current):
            return []
        key = current.lower()
        results = self._ac_cache.get(key)
        if results is None:
            try:
                results = await asyncio.wait_for(self.extractor.search(current, limit=5), AUTOCOMPLETE_TIMEOUT)
            except Exception:
                return []
            self._ac_cache[key] = results
            while len(self._ac_cache) > AUTOCOMPLETE_CACHE_SIZE:
                self._ac_cache.popitem(last=False)
        choices = []
        for t in results:
            if len(t.url) > 100:
                continue
            label = f"{t.title} — {t.uploader or 'Unknown'} ({format_duration(t.duration)})"
            choices.append(app_commands.Choice(name=truncate(label, 100), value=t.url))
        return choices

    # ================================================================ commands
    @app_commands.command(description="Play a song: YouTube link, playlist, or search words")
    @app_commands.describe(query="A link (YouTube, SoundCloud, Bandcamp, ...) or what to search for")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        await self._queue_query(interaction, query, front=False)

    @play.autocomplete("query")
    async def play_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._suggest(current)

    @app_commands.command(description="Add a song to the front of the queue")
    @app_commands.describe(query="A link or what to search for")
    async def playnext(self, interaction: discord.Interaction, query: str) -> None:
        await self._queue_query(interaction, query, front=True)

    @playnext.autocomplete("query")
    async def playnext_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._suggest(current)

    @app_commands.command(description="Search YouTube and pick a result")
    @app_commands.describe(query="What to search for")
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        self._member(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            results = await self.extractor.search(query, limit=5)
        except ExtractionError as exc:
            raise MusicError(str(exc)) from None
        if not results:
            raise MusicError(f"No results for **{clean(query, 100)}**.")

        async def on_pick(pick_inter: discord.Interaction, track: Track) -> None:
            await pick_inter.response.defer()
            try:
                player = await self.ensure_voice(pick_inter)
            except MusicError as exc:
                await pick_inter.followup.send(embed=error_embed(str(exc)), ephemeral=True)
                return
            was_idle = not player.current and not player.queue
            picked = track.copy().requested_by(pick_inter.user)
            try:
                player.enqueue([picked])
            except QueueFull as exc:
                await pick_inter.followup.send(embed=error_embed(str(exc)), ephemeral=True)
                return
            player.note(f"📥 {pick_inter.user.display_name} added {truncate(picked.title, 60)}")
            await pick_inter.edit_original_response(
                embed=ok_embed(f"✅ Queued {track_link(picked)}", self.color), view=None
            )
            embed = added_embed(player, [picked], None, front=False, playing_now=was_idle)
            await pick_inter.followup.send(embed=embed)
            await self.refresh_panel(player)

        view = SearchView(results, interaction.user.id, on_pick)
        await interaction.followup.send(embed=search_embed(query, results, self.color), view=view, ephemeral=True)

    @app_commands.command(description="Pause the music")
    async def pause(self, interaction: discord.Interaction) -> None:
        player = self.get_controllable(interaction, need_track=True)
        if not player.pause():
            raise MusicError("The music is already paused. Use `/resume`.")
        player.note(f"⏸️ Paused by {interaction.user.display_name}")
        await self.reply(interaction, "⏸️ Paused.")
        await self.refresh_panel(player)

    @app_commands.command(description="Resume the music")
    async def resume(self, interaction: discord.Interaction) -> None:
        player = self.get_controllable(interaction, need_track=True)
        if not player.resume():
            raise MusicError("The music isn't paused.")
        player.note(f"▶️ Resumed by {interaction.user.display_name}")
        await self.reply(interaction, "▶️ Resumed.")
        await self.refresh_panel(player)

    @app_commands.command(description="Skip the current song")
    async def skip(self, interaction: discord.Interaction) -> None:
        player = self.get_controllable(interaction, need_track=True)
        title = player.current.title if player.current else "the song"
        player.note(f"⏭️ {interaction.user.display_name} skipped {truncate(title, 60)}")
        player.skip()
        await self.reply(interaction, f"⏭️ Skipped **{clean(title)}**.")

    @app_commands.command(description="Go back to the previous song")
    async def previous(self, interaction: discord.Interaction) -> None:
        player = self.get_controllable(interaction)
        prev = await player.previous()
        if prev is None:
            raise MusicError("There's no previous song.")
        player.note(f"⏮️ {interaction.user.display_name} went back")
        await self.reply(interaction, f"⏮️ Going back to {track_link(prev)}.")

    @app_commands.command(description="Restart the current song")
    async def replay(self, interaction: discord.Interaction) -> None:
        player = self.get_controllable(interaction, need_track=True)
        if not player.seek(0):
            raise MusicError("Can't restart this song (live streams can't be restarted).")
        player.note(f"🔄 {interaction.user.display_name} restarted the song")
        await self.reply(interaction, "🔄 Restarted the song.")

    @app_commands.command(description="Jump to a time in the song, like 1:30 or 90")
    @app_commands.describe(time="Where to jump to, e.g. 1:30, 2:05:00, 90 or 1m30s")
    async def seek(self, interaction: discord.Interaction, time: str) -> None:
        player = self.get_controllable(interaction, need_track=True)
        seconds = parse_timestamp(time)
        if seconds is None:
            raise MusicError("I didn't understand that time. Try `1:30`, `90` or `1m30s`.")
        track = player.current
        if track and track.duration and seconds >= track.duration:
            raise MusicError(f"This song is only {format_duration(track.duration)} long.")
        if not player.seek(seconds):
            raise MusicError("You can't seek in a live stream.")
        player.note(f"⏩ {interaction.user.display_name} jumped to {format_duration(seconds)}")
        await self.reply(interaction, f"⏩ Jumped to `{format_duration(seconds)}`.")

    @app_commands.command(description="Stop the music and clear the queue")
    async def stop(self, interaction: discord.Interaction) -> None:
        player = self.get_controllable(interaction)
        player.note(f"⏹️ Stopped by {interaction.user.display_name}")
        player.stop()
        await self.reply(interaction, "⏹️ Stopped the music and cleared the queue.")

    @app_commands.command(description="Make the bot join your voice channel")
    async def join(self, interaction: discord.Interaction) -> None:
        player = await self.ensure_voice(interaction)
        channel = player.vc.channel if player.vc else None
        await self.reply(interaction, f"👋 Joined {channel.mention if channel else 'your channel'}. Use `/play` to start!")

    @app_commands.command(description="Make the bot leave the voice channel")
    async def leave(self, interaction: discord.Interaction) -> None:
        member = self._member(interaction)
        player = self.players.get(member.guild.id)
        if player:
            reason = player.control_error(member)
            if reason:
                raise MusicError(reason)
            await self.reply(interaction, "👋 Bye! Left the voice channel.")
            await player.destroy(f"👋 {member.display_name} made me leave. Use `/play` to start again!")
        elif member.guild.voice_client:
            await member.guild.voice_client.disconnect(force=True)
            await self.reply(interaction, "👋 Left the voice channel.")
        else:
            raise MusicError("I'm not in a voice channel.")

    @app_commands.command(name="nowplaying", description="Show the live music control panel here")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        member = self._member(interaction)
        player = self.players.get(member.guild.id)
        if not player or not player.current:
            raise MusicError("Nothing is playing right now. Use `/play` to start!")
        if interaction.channel is not None:
            player.text_channel = interaction.channel  # type: ignore[assignment]
        await interaction.response.send_message(embed=ok_embed("🎛️ Here's the control panel:", self.color), ephemeral=True)
        await player.panel.send_new()

    @app_commands.command(description="Show the song queue")
    async def queue(self, interaction: discord.Interaction) -> None:
        member = self._member(interaction)
        player = self.players.get(member.guild.id)
        if not player:
            raise MusicError("The queue is empty. Use `/play` to add songs!")
        await interaction.response.send_message(embed=queue_embed(player, 0), view=QueueView(player))

    @app_commands.command(description="Remove a song from the queue")
    @app_commands.describe(position="Its number in /queue")
    async def remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1]) -> None:
        player = self.get_controllable(interaction)
        try:
            track = player.queue.remove(position - 1)
        except IndexError as exc:
            raise MusicError(str(exc) if player.queue else "The queue is empty.") from None
        player.note(f"🗑️ {interaction.user.display_name} removed {truncate(track.title, 60)}")
        await self.reply(interaction, f"🗑️ Removed {track_link(track)}.")
        await self.refresh_panel(player)

    @app_commands.command(description="Move a song to a different place in the queue")
    @app_commands.describe(from_position="The song's current number in /queue", to_position="Where it should go")
    @app_commands.rename(from_position="from", to_position="to")
    async def move(
        self,
        interaction: discord.Interaction,
        from_position: app_commands.Range[int, 1],
        to_position: app_commands.Range[int, 1],
    ) -> None:
        player = self.get_controllable(interaction)
        try:
            track = player.queue.move(from_position - 1, to_position - 1)
        except IndexError as exc:
            raise MusicError(str(exc) if player.queue else "The queue is empty.") from None
        new_pos = player.queue.tracks.index(track) + 1
        await self.reply(interaction, f"↕️ Moved {track_link(track)} to position **#{new_pos}**.")
        await self.refresh_panel(player)

    @app_commands.command(description="Skip straight to a song in the queue")
    @app_commands.describe(position="Its number in /queue")
    async def skipto(self, interaction: discord.Interaction, position: app_commands.Range[int, 1]) -> None:
        player = self.get_controllable(interaction)
        try:
            track = player.queue.skip_to(position - 1, player.loop)
        except IndexError as exc:
            raise MusicError(str(exc) if player.queue else "The queue is empty.") from None
        player.note(f"⏭️ {interaction.user.display_name} skipped to {truncate(track.title, 60)}")
        if not player.skip():
            player.wake()  # it was idle, start playing
        await self.reply(interaction, f"⏭️ Skipping to {track_link(track)}.")

    @app_commands.command(description="Shuffle the queue")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        player = self.get_controllable(interaction)
        if len(player.queue) < 2:
            raise MusicError("Need at least 2 songs in the queue to shuffle.")
        player.queue.shuffle()
        player.note(f"🔀 {interaction.user.display_name} shuffled the queue")
        await self.reply(interaction, f"🔀 Shuffled **{len(player.queue)}** songs.")
        await self.refresh_panel(player)

    @app_commands.command(description="Remove every song from the queue (keeps the current one)")
    async def clear(self, interaction: discord.Interaction) -> None:
        player = self.get_controllable(interaction)
        count = player.queue.clear()
        player.note(f"🧹 {interaction.user.display_name} cleared the queue")
        await self.reply(interaction, f"🧹 Cleared **{count}** songs from the queue.")
        await self.refresh_panel(player)

    @app_commands.command(description="Loop the current song or the whole queue")
    @app_commands.describe(mode="Leave empty to switch to the next mode")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Off", value=LoopMode.OFF.value),
            app_commands.Choice(name="Song", value=LoopMode.TRACK.value),
            app_commands.Choice(name="Queue", value=LoopMode.QUEUE.value),
        ]
    )
    async def loop(self, interaction: discord.Interaction, mode: Optional[app_commands.Choice[int]] = None) -> None:
        player = self.get_controllable(interaction)
        player.loop = LoopMode(mode.value) if mode else player.loop.next()
        player.note(f"{player.loop.emoji} {interaction.user.display_name} set loop to {player.loop.label}")
        await self.reply(interaction, f"{player.loop.emoji} Loop is now **{player.loop.label}**.")
        await self.refresh_panel(player)

    @app_commands.command(description="Change the volume (0-200%)")
    @app_commands.describe(level="Volume in percent. Leave empty to see the current volume.")
    async def volume(self, interaction: discord.Interaction, level: Optional[app_commands.Range[int, 0, 200]] = None) -> None:
        if level is None:
            member = self._member(interaction)
            player = self.players.get(member.guild.id)
            current = round(player.volume * 100) if player else self.config.default_volume
            await self.reply(interaction, f"🔊 The volume is **{current}%**.", ephemeral=True)
            return
        player = self.get_controllable(interaction)
        percent = player.set_volume(level)
        icon = "🔇" if percent == 0 else "🔉" if percent < 50 else "🔊"
        player.note(f"{icon} {interaction.user.display_name} set the volume to {percent}%")
        await self.reply(interaction, f"{icon} Volume set to **{percent}%**.")
        await self.refresh_panel(player)

    @app_commands.command(name="filter", description="Add an audio effect: bass boost, nightcore, 8D, ...")
    @app_commands.describe(effect="The effect to use")
    @app_commands.choices(
        effect=[app_commands.Choice(name=f"{f.label} - {f.description}"[:100], value=f.key) for f in FILTERS.values()]
    )
    async def filter_(self, interaction: discord.Interaction, effect: app_commands.Choice[str]) -> None:
        player = self.get_controllable(interaction)
        audio_filter = FILTERS[effect.value]
        player.set_filter(audio_filter)
        player.note(f"🎛️ {interaction.user.display_name} set the filter to {audio_filter.label}")
        await self.reply(interaction, f"{audio_filter.emoji} Filter set to **{audio_filter.label}**.")
        await self.refresh_panel(player)

    @app_commands.command(description="Keep playing similar songs when the queue runs out")
    @app_commands.describe(enabled="On or off. Leave empty to toggle.")
    async def autoplay(self, interaction: discord.Interaction, enabled: Optional[bool] = None) -> None:
        player = self.get_controllable(interaction)
        player.set_autoplay((not player.autoplay) if enabled is None else enabled)
        player.note(f"♾️ {interaction.user.display_name} turned autoplay {'on' if player.autoplay else 'off'}")
        await self.reply(interaction, f"♾️ Autoplay is now **{'on' if player.autoplay else 'off'}**.")
        await self.refresh_panel(player)

    @app_commands.command(description="24/7 mode: stay in the voice channel even when idle")
    @app_commands.describe(enabled="On or off. Leave empty to toggle.")
    async def stay(self, interaction: discord.Interaction, enabled: Optional[bool] = None) -> None:
        player = self.get_controllable(interaction)
        player.set_stay((not player.stay) if enabled is None else enabled)
        player.note(f"🕒 {interaction.user.display_name} turned 24/7 mode {'on' if player.stay else 'off'}")
        if player.stay:
            await self.reply(interaction, "🕒 24/7 mode **on**: I'll stay in the channel until you `/leave`.")
        else:
            await self.reply(interaction, "🕒 24/7 mode **off**: I'll leave when idle.")
        await self.refresh_panel(player)

    @app_commands.command(name="help", description="Show all music commands")
    async def help_(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=help_embed(self.color, self.bot.user), ephemeral=True)

    # ================================================================ events
    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        guild = member.guild
        player = self.players.get(guild.id)
        if player is None:
            return

        if self.bot.user and member.id == self.bot.user.id:
            if after.channel is None:
                await player.destroy("👋 I was disconnected from the voice channel.")
                return
            if before.channel == after.channel:
                return
            # The bot was moved: check if anyone is in the new channel.

        vc = guild.voice_client
        if not vc or not vc.channel:
            return
        channel = vc.channel
        if member.id != getattr(self.bot.user, "id", None) and before.channel != channel and after.channel != channel:
            return
        humans = [m for m in channel.members if not m.bot]
        if humans:
            player.on_company()
        else:
            player.on_alone()


async def setup(bot: commands.Bot) -> None:
    config: Config = bot.config  # type: ignore[attr-defined]
    await bot.add_cog(Music(bot, config))
