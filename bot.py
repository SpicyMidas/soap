"""Start the music bot:  python bot.py"""

from __future__ import annotations

import sys

if sys.version_info < (3, 10):
    sys.exit("This bot needs Python 3.10 or newer. Download it from https://www.python.org/downloads/")

import logging

import discord
from discord import app_commands
from discord.ext import commands

from musicbot import __version__
from musicbot.config import Config, load_config

log = logging.getLogger("musicbot")

INVITE_PERMISSIONS = discord.Permissions(
    view_channel=True,
    send_messages=True,
    send_messages_in_threads=True,
    embed_links=True,
    attach_files=True,
    read_message_history=True,
    use_external_emojis=True,
    connect=True,
    speak=True,
    use_voice_activation=True,
    request_to_speak=True,
)


class MusicBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()  # no privileged intents needed
        intents.message_content = False
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            activity=discord.Activity(type=discord.ActivityType.listening, name="/play"),
            allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=False, private_channel=False),
            allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
        )
        self.config = config
        self.tree.on_error = self.on_app_command_error

    async def setup_hook(self) -> None:
        await self.load_extension("musicbot.cogs.music")

        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d slash commands to server %s (instant).", len(synced), self.config.guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands globally.", len(synced))

    async def on_ready(self) -> None:
        assert self.user is not None
        invite = discord.utils.oauth_url(
            self.user.id, permissions=INVITE_PERMISSIONS, scopes=("bot", "applications.commands")
        )
        line = "=" * 64
        print(
            f"\n{line}\n"
            f"  🎵 {self.user} is online!  (v{__version__}, discord.py {discord.__version__})\n"
            f"  In {len(self.guilds)} server(s)\n\n"
            f"  Invite link (open it in your browser to add the bot to a server):\n"
            f"  {invite}\n"
            f"{line}\n",
            flush=True,
        )

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        from musicbot.audio import ExtractionError
        from musicbot.cogs.music import MusicError
        from musicbot.ui import error_embed

        original = getattr(error, "original", error)
        if isinstance(error, MusicError) or isinstance(original, ExtractionError):
            message = str(original)
        elif isinstance(error, app_commands.CommandOnCooldown):
            message = f"Slow down! Try again in {error.retry_after:.0f}s."
        elif isinstance(error, app_commands.BotMissingPermissions):
            message = f"I'm missing permissions: {', '.join(error.missing_permissions)}"
        elif isinstance(error, app_commands.CheckFailure):
            message = str(error) or "You can't use this command here."
        else:
            log.error("Error in /%s", interaction.command.name if interaction.command else "?", exc_info=original)
            message = "Something went wrong. Check the bot's console for details."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=error_embed(message), ephemeral=True)
            else:
                await interaction.response.send_message(embed=error_embed(message), ephemeral=True)
        except discord.HTTPException:
            pass


def main() -> None:
    config = load_config()
    discord.utils.setup_logging(level=logging.INFO)
    logging.getLogger("discord.voice_state").setLevel(logging.WARNING)
    logging.getLogger("discord.player").setLevel(logging.WARNING)

    bot = MusicBot(config)
    try:
        bot.run(config.token, log_handler=None)
    except discord.LoginFailure:
        print(
            "\n[!] Discord rejected the token. Copy a fresh one from the Developer Portal\n"
            "    (your application -> Bot -> Reset Token) into the .env file.\n"
        )
        sys.exit(1)
    except discord.PrivilegedIntentsRequired:
        print("\n[!] Discord says an intent is missing. This bot doesn't need any - please report this.\n")
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
