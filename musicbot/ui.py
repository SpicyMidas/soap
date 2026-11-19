"""Embeds, the live "Now Playing" control panel, queue pages and search menus."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import TYPE_CHECKING, Awaitable, Callable

import discord

from .audio import FILTERS, Track, format_duration
from .musicqueue import LoopMode

if TYPE_CHECKING:
    from .player import GuildPlayer

log = logging.getLogger("musicbot.ui")

BAR_LENGTH = 18
QUEUE_PAGE_SIZE = 10
VOLUME_STEP = 10


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def clean(text: str, limit: int = 80) -> str:
    """Safe to put inside [link text](url)."""
    text = discord.utils.escape_markdown(truncate(text, limit))
    return text.replace("[", "(").replace("]", ")")


def track_link(track: Track, limit: int = 60) -> str:
    return f"[{clean(track.title, limit)}]({track.url})"


def progress_bar(position: float, duration: int | None) -> str:
    if not duration:
        return "🔴 **LIVE**"
    frac = max(0.0, min(1.0, position / duration))
    filled = round(frac * BAR_LENGTH)
    if filled >= BAR_LENGTH:
        return "━" * BAR_LENGTH + "●"
    return "━" * filled + "●" + "─" * (BAR_LENGTH - filled)


def queue_time(player: GuildPlayer) -> str:
    total = player.queue.total_duration
    text = format_duration(total)
    return text + "+" if player.queue.has_live else text


def error_embed(text: str) -> discord.Embed:
    return discord.Embed(description=f"❌ {text}", color=discord.Color.red())


def ok_embed(text: str, color: int) -> discord.Embed:
    return discord.Embed(description=text, color=color)


# --------------------------------------------------------------------------- #
# Embeds
# --------------------------------------------------------------------------- #


def now_playing_embed(player: GuildPlayer) -> discord.Embed:
    track = player.current
    color = player.config.embed_color
    if track is None:
        return idle_embed(player)

    paused = player.is_paused
    status = "⏸️ Paused" if paused else "🎶 Now Playing"
    embed = discord.Embed(
        title=truncate(track.title, 256),
        url=track.url,
        color=discord.Color.dark_grey() if paused else color,
    )
    bot_user = player.bot.user
    embed.set_author(name=status, icon_url=bot_user.display_avatar.url if bot_user else None)

    pos = player.position
    icon = "⏸️" if paused else "▶️"
    if track.duration:
        embed.description = (
            f"{icon} `{format_duration(pos)}` {progress_bar(pos, track.duration)} `{format_duration(track.duration)}`"
        )
    else:
        embed.description = f"{icon} `{format_duration(pos)}` {progress_bar(pos, None)}"

    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)

    embed.add_field(name="🎤 Artist", value=truncate(track.uploader or "Unknown", 100), inline=True)
    embed.add_field(name="🙋 Requested by", value=f"<@{track.requester_id}>" if track.requester_id else track.requester_name, inline=True)
    embed.add_field(name="🌐 Source", value=track.source, inline=True)

    embed.add_field(name="🔊 Volume", value=f"{round(player.volume * 100)}%", inline=True)
    embed.add_field(name="🔁 Loop", value=f"{player.loop.emoji} {player.loop.label}", inline=True)
    embed.add_field(name="🎛️ Filter", value=f"{player.filter.emoji} {player.filter.label}", inline=True)

    queue_text = f"{len(player.queue)} song{'s' if len(player.queue) != 1 else ''}"
    if len(player.queue):
        queue_text += f" • {queue_time(player)}"
    embed.add_field(name="📜 Queue", value=queue_text, inline=True)
    embed.add_field(name="♾️ Autoplay", value="On" if player.autoplay else "Off", inline=True)
    embed.add_field(name="🕒 24/7", value="On" if player.stay else "Off", inline=True)

    if player.queue:
        nxt = player.queue[0]
        embed.add_field(
            name="⏭️ Up next",
            value=f"{track_link(nxt, 70)} `{format_duration(nxt.duration)}`",
            inline=False,
        )
    elif player.loop is LoopMode.TRACK:
        embed.add_field(name="⏭️ Up next", value="This song again (looping)", inline=False)
    elif player.autoplay:
        embed.add_field(name="⏭️ Up next", value="A similar song picked by autoplay", inline=False)

    embed.set_footer(text=player.last_action or "Use the buttons below or /help for all commands")
    return embed


def idle_embed(player: GuildPlayer) -> discord.Embed:
    if player.stay:
        leave = "I'll stay here (24/7 mode is on)."
    else:
        leave = f"I'll leave if nothing plays for {format_duration(player.config.idle_timeout)}."
    embed = discord.Embed(
        title="✅ Queue finished",
        description=f"Add more songs with `/play`.\n{leave}",
        color=player.config.embed_color,
    )
    if player.last_action:
        embed.set_footer(text=player.last_action)
    return embed


def queue_embed(player: GuildPlayer, page: int) -> discord.Embed:
    color = player.config.embed_color
    pages = max(1, math.ceil(len(player.queue) / QUEUE_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    embed = discord.Embed(title=f"📜 Queue for {player.guild.name}", color=color)

    lines = []
    if player.current:
        cur = player.current
        state = "⏸️" if player.is_paused else "▶️"
        lines.append(
            f"**Now:** {state} {track_link(cur, 70)} `{format_duration(player.position)}/{format_duration(cur.duration)}`\n"
        )
    start = page * QUEUE_PAGE_SIZE
    for i, track in enumerate(player.queue.tracks[start : start + QUEUE_PAGE_SIZE], start=start + 1):
        lines.append(f"`{i:>2}.` {track_link(track, 55)} `{format_duration(track.duration)}` • {clean(track.requester_name, 20)}")
    if not player.queue:
        lines.append("*The queue is empty. Add songs with `/play`!*")

    embed.description = "\n".join(lines)
    embed.set_footer(
        text=f"Page {page + 1}/{pages} • {len(player.queue)} songs • {queue_time(player)} total • "
        f"Loop: {player.loop.label}"
    )
    return embed


def added_embed(player: GuildPlayer, tracks: list[Track], playlist: str | None, *, front: bool, playing_now: bool) -> discord.Embed:
    color = player.config.embed_color
    if playlist or len(tracks) > 1:
        total = sum(t.duration or 0 for t in tracks)
        embed = discord.Embed(
            title="📥 Playlist added" if playlist else "📥 Songs added",
            description=f"**{clean(playlist or 'Selection', 200)}**\n{len(tracks)} songs • {format_duration(total)}",
            color=color,
        )
        if tracks and tracks[0].thumbnail:
            embed.set_thumbnail(url=tracks[0].thumbnail)
        return embed

    track = tracks[0]
    if playing_now:
        title = "▶️ Starting"
    elif front:
        title = "⏭️ Playing next"
    else:
        title = "📥 Added to queue"
    embed = discord.Embed(title=title, description=f"**{track_link(track, 200)}**", color=color)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    embed.add_field(name="Duration", value=format_duration(track.duration))
    if not playing_now:
        position = 1 if front else len(player.queue)
        embed.add_field(name="Position", value=f"#{position}")
    if track.uploader:
        embed.add_field(name="Artist", value=truncate(track.uploader, 100))
    return embed


HELP_SECTIONS = {
    "🎵 Playing": [
        ("/play <song or link>", "Play a YouTube link, playlist, SoundCloud/Bandcamp link or search words"),
        ("/playnext <song or link>", "Put a song at the front of the queue"),
        ("/search <words>", "Pick from the top 5 YouTube results"),
        ("/nowplaying", "Bring the live control panel to this channel"),
    ],
    "⏯️ Controls": [
        ("/pause • /resume • /skip • /previous", "The basics (also on the panel buttons)"),
        ("/seek <1:30>", "Jump to a time in the song"),
        ("/replay", "Start the current song over"),
        ("/volume <0-200>", "Change the volume"),
        ("/filter", "Bass boost, nightcore, vaporwave, 8D, karaoke…"),
    ],
    "📜 Queue": [
        ("/queue", "See the queue (with pages)"),
        ("/skipto <pos> • /remove <pos> • /move <from> <to>", "Rearrange the queue"),
        ("/shuffle • /clear • /loop", "Shuffle, empty or loop the queue/song"),
        ("/autoplay", "Keep playing similar songs when the queue ends"),
    ],
    "🔌 Voice": [
        ("/join • /leave • /stop", "Join your channel, leave, or stop and clear"),
        ("/stay", "24/7 mode: never leave because of inactivity"),
    ],
}


def help_embed(color: int, bot_user: discord.ClientUser | None) -> discord.Embed:
    embed = discord.Embed(
        title="🎧 Music Bot Help",
        description="Join a voice channel and use `/play` to start. "
        "The **live panel** under each song has buttons for everything!",
        color=color,
    )
    if bot_user:
        embed.set_thumbnail(url=bot_user.display_avatar.url)
    for section, rows in HELP_SECTIONS.items():
        embed.add_field(name=section, value="\n".join(f"`{cmd}` - {desc}" for cmd, desc in rows), inline=False)
    embed.set_footer(text="Panel buttons: ⏮️ previous • ⏯️ pause • ⏭️ skip • ⏹️ stop • 🔁 loop • 🔉🔊 volume • 🔀 shuffle • 📜 queue • ♾️ autoplay")
    return embed


# --------------------------------------------------------------------------- #
# Base view with friendly error handling
# --------------------------------------------------------------------------- #


class BaseView(discord.ui.View):
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.exception("Error in view %s", type(self).__name__, exc_info=error)
        await send_ephemeral(interaction, error_embed("Something went wrong. Check the console for details."))


async def send_ephemeral(interaction: discord.Interaction, embed: discord.Embed) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        pass


# --------------------------------------------------------------------------- #
# The live control panel
# --------------------------------------------------------------------------- #


class FilterSelect(discord.ui.Select):
    def __init__(self, player: GuildPlayer) -> None:
        options = [
            discord.SelectOption(
                label=f.label,
                value=f.key,
                emoji=f.emoji,
                description=f.description[:100],
                default=f.key == player.filter.key,
            )
            for f in FILTERS.values()
        ]
        super().__init__(placeholder="🎛️ Audio filter", options=options, row=2)
        self.player = player

    async def callback(self, interaction: discord.Interaction) -> None:
        audio_filter = FILTERS[self.values[0]]
        self.player.set_filter(audio_filter)
        self.player.note(f"🎛️ {interaction.user.display_name} set the filter to {audio_filter.label}")
        await self.view.update(interaction)  # type: ignore[union-attr]


class ControlsView(BaseView):
    """Buttons under the Now Playing message."""

    def __init__(self, player: GuildPlayer) -> None:
        super().__init__(timeout=None)
        self.player = player

        paused = player.is_paused
        self.pause_button.emoji = "▶️" if paused else "⏸️"
        self.pause_button.style = discord.ButtonStyle.success if paused else discord.ButtonStyle.primary

        self.loop_button.emoji = "🔂" if player.loop is LoopMode.TRACK else "🔁"
        self.loop_button.style = (
            discord.ButtonStyle.success if player.loop is not LoopMode.OFF else discord.ButtonStyle.secondary
        )
        self.autoplay_button.style = discord.ButtonStyle.success if player.autoplay else discord.ButtonStyle.secondary
        self.vol_down.disabled = player.volume <= 0
        self.vol_up.disabled = player.volume >= 2
        self.shuffle_button.disabled = len(player.queue) < 2
        self.add_item(FilterSelect(player))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.player.destroyed:
            await send_ephemeral(interaction, error_embed("This player has been closed. Use `/play` to start again."))
            return False
        reason = self.player.control_error(interaction.user)
        if reason:
            await send_ephemeral(interaction, error_embed(reason))
            return False
        return True

    async def update(self, interaction: discord.Interaction) -> None:
        """Redraw the panel right away as the reply to a button press."""
        await self.player.panel.redraw_from(interaction)

    # Row 0 ------------------------------------------------------------------
    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
    async def previous_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.player
        name = interaction.user.display_name
        if player.current and player.position > 5 and not player.current.is_live:
            player.seek(0)
            player.note(f"⏮️ {name} restarted the song")
            return await self.update(interaction)
        prev = await player.previous()
        if prev is None:
            if player.current and not player.current.is_live:
                player.seek(0)
                player.note(f"⏮️ {name} restarted the song")
                return await self.update(interaction)
            return await send_ephemeral(interaction, error_embed("There's no previous song."))
        player.note(f"⏮️ {name} went back to the previous song")
        await interaction.response.defer()

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.primary, row=0)
    async def pause_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.player
        name = interaction.user.display_name
        if player.is_paused:
            player.resume()
            player.note(f"▶️ Resumed by {name}")
        elif player.pause():
            player.note(f"⏸️ Paused by {name}")
        else:
            return await send_ephemeral(interaction, error_embed("Nothing is playing."))
        await self.update(interaction)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.player
        if not player.current:
            return await send_ephemeral(interaction, error_embed("Nothing is playing."))
        player.note(f"⏭️ {interaction.user.display_name} skipped {truncate(player.current.title, 60)}")
        player.skip()
        await interaction.response.defer()

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.player.note(f"⏹️ Stopped by {interaction.user.display_name}")
        self.player.stop()
        await interaction.response.defer()

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=0)
    async def loop_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.player
        player.loop = player.loop.next()
        player.note(f"{player.loop.emoji} {interaction.user.display_name} set loop to {player.loop.label}")
        await self.update(interaction)

    # Row 1 ------------------------------------------------------------------
    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, row=1)
    async def vol_down(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        percent = self.player.set_volume(round(self.player.volume * 100) - VOLUME_STEP)
        self.player.note(f"🔉 {interaction.user.display_name} set the volume to {percent}%")
        await self.update(interaction)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, row=1)
    async def vol_up(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        percent = self.player.set_volume(round(self.player.volume * 100) + VOLUME_STEP)
        self.player.note(f"🔊 {interaction.user.display_name} set the volume to {percent}%")
        await self.update(interaction)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.player.queue.shuffle()
        self.player.note(f"🔀 {interaction.user.display_name} shuffled the queue")
        await self.update(interaction)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, row=1)
    async def queue_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = QueueView(self.player, interaction.user.id)
        await interaction.response.send_message(embed=queue_embed(self.player, 0), view=view, ephemeral=True)

    @discord.ui.button(emoji="♾️", style=discord.ButtonStyle.secondary, row=1)
    async def autoplay_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.player
        player.set_autoplay(not player.autoplay)
        player.note(f"♾️ {interaction.user.display_name} turned autoplay {'on' if player.autoplay else 'off'}")
        await self.update(interaction)


class NowPlayingPanel:
    """Owns the single live panel message for a server."""

    def __init__(self, player: GuildPlayer) -> None:
        self.player = player
        self.message: discord.Message | None = None
        self.view: ControlsView | None = None
        self._lock = asyncio.Lock()

    def _render(self) -> tuple[discord.Embed, ControlsView | None]:
        if self.view:
            self.view.stop()
        if self.player.current is None:
            self.view = None
            return idle_embed(self.player), None
        self.view = ControlsView(self.player)
        return now_playing_embed(self.player), self.view

    async def _safe(self, coro_factory: Callable[[], Awaitable[discord.Message | None]]) -> discord.Message | None:
        try:
            return await coro_factory()
        except discord.NotFound:
            self.message = None
        except discord.HTTPException as exc:
            log.debug("Panel update failed: %s", exc)
        return None

    async def send_new(self) -> None:
        """Post the panel for a new song (re-posting it at the bottom of the chat)."""
        async with self._lock:
            embed, view = self._render()
            channel = self.player.text_channel
            msg = self.message
            if msg and msg.channel.id == channel.id and getattr(channel, "last_message_id", None) == msg.id:
                # Already the newest message, just edit it.
                if await self._safe(lambda: msg.edit(embed=embed, view=view)) is not None:
                    return
            if msg:
                await self._safe(lambda: msg.delete())
            self.message = await self._safe(lambda: channel.send(embed=embed, view=view))

    async def refresh(self) -> None:
        async with self._lock:
            if not self.message:
                return
            embed, view = self._render()
            msg = self.message
            await self._safe(lambda: msg.edit(embed=embed, view=view))

    async def redraw_from(self, interaction: discord.Interaction) -> None:
        """Reply to a button press by redrawing the panel."""
        async with self._lock:
            embed, view = self._render()
            if interaction.message and self.message and interaction.message.id == self.message.id:
                await interaction.response.edit_message(embed=embed, view=view)
            else:
                await interaction.response.defer()
                if self.message:
                    msg = self.message
                    await self._safe(lambda: msg.edit(embed=embed, view=view))

    async def show_idle(self) -> None:
        async with self._lock:
            if self.view:
                self.view.stop()
                self.view = None
            if not self.message:
                return
            msg = self.message
            await self._safe(lambda: msg.edit(embed=idle_embed(self.player), view=None))

    async def close(self, notice: str | None) -> None:
        async with self._lock:
            if self.view:
                self.view.stop()
                self.view = None
            msg = self.message
            self.message = None
            embed = discord.Embed(
                description=notice or "👋 Disconnected. Use `/play` to start again!",
                color=self.player.config.embed_color,
            )
            if msg:
                if await self._safe(lambda: msg.edit(embed=embed, view=None)) is not None:
                    return
            if notice:
                try:
                    await self.player.text_channel.send(embed=embed)
                except discord.HTTPException:
                    pass


# --------------------------------------------------------------------------- #
# Queue pages
# --------------------------------------------------------------------------- #


class QueueView(BaseView):
    def __init__(self, player: GuildPlayer, owner_id: int | None = None) -> None:
        super().__init__(timeout=180)
        self.player = player
        self.owner_id = owner_id
        self.page = 0
        self._sync()

    @property
    def pages(self) -> int:
        return max(1, math.ceil(len(self.player.queue) / QUEUE_PAGE_SIZE))

    def _sync(self) -> None:
        self.page = max(0, min(self.page, self.pages - 1))
        self.first.disabled = self.prev.disabled = self.page == 0
        self.next.disabled = self.last.disabled = self.page >= self.pages - 1
        self.counter.label = f"{self.page + 1}/{self.pages}"

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=queue_embed(self.player, self.page), view=self)

    @discord.ui.button(emoji="⏪", style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page = 0
        await self._show(interaction)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page -= 1
        await self._show(interaction)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.primary)
    async def counter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._show(interaction)  # acts as a refresh button

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page += 1
        await self._show(interaction)

    @discord.ui.button(emoji="⏩", style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page = self.pages - 1
        await self._show(interaction)


# --------------------------------------------------------------------------- #
# /search result picker
# --------------------------------------------------------------------------- #


class SearchSelect(discord.ui.Select):
    def __init__(self, results: list[Track]) -> None:
        options = [
            discord.SelectOption(
                label=truncate(t.title, 100),
                value=str(i),
                description=truncate(f"{t.uploader or 'Unknown'} • {format_duration(t.duration)}", 100),
                emoji=["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"][i],
            )
            for i, t in enumerate(results[:10])
        ]
        super().__init__(placeholder="🔎 Pick a song to queue", options=options)
        self.results = results

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SearchView = self.view  # type: ignore[assignment]
        track = self.results[int(self.values[0])]
        await view.on_pick(interaction, track)


class SearchView(BaseView):
    def __init__(
        self,
        results: list[Track],
        owner_id: int,
        on_pick: Callable[[discord.Interaction, Track], Awaitable[None]],
    ) -> None:
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.on_pick = on_pick
        self.add_item(SearchSelect(results))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await send_ephemeral(interaction, error_embed("Run `/search` yourself to pick a song."))
            return False
        return True


def search_embed(query: str, results: list[Track], color: int) -> discord.Embed:
    lines = [
        f"`{i}.` {track_link(t, 70)} `{format_duration(t.duration)}`\n　 {clean(t.uploader or 'Unknown', 40)}"
        for i, t in enumerate(results, start=1)
    ]
    embed = discord.Embed(title=f"🔎 Results for “{truncate(query, 80)}”", description="\n".join(lines), color=color)
    embed.set_footer(text="Pick a song from the menu below")
    return embed
