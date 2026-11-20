# 🎧 Soap: a self-hosted Discord music bot

A music bot you run on your own PC. Paste a YouTube link, a playlist, or just type a song name, and it plays in your voice channel. Every song gets a **live control panel**: a progress bar that keeps moving, song info, and buttons for pause, skip, loop, volume, shuffle, audio filters and more.

- **No API keys and no extra servers.** The only thing you need is your Discord bot token.
- Plays **YouTube** (videos, playlists, Shorts, YouTube Music), **SoundCloud**, **Bandcamp**, direct MP3 links and [1000+ other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).
- **26 slash commands**, with live YouTube search suggestions as you type in `/play`.
- **Live Now Playing panel**: 🔴 progress bar · ⏮️ ⏯️ ⏭️ ⏹️ 🔁 · 🔉 🔊 🔀 📜 ♾️ · 🎛️ filter menu.
- **Audio filters**: Bass Boost, Nightcore, Vaporwave, Slowed + Reverb, 8D, Karaoke, Treble, Lo-Fi.
- **Autoplay**: when the queue runs out, it keeps going with similar songs.
- Leaves the voice channel when the queue is finished. It also pauses by itself when everyone leaves, resumes when someone comes back, and leaves after a while. **24/7 mode** keeps it in the channel.
- Works on **Windows, macOS and Linux**. FFmpeg comes bundled, so there's nothing extra to install by hand.

---

## 🚀 Setup (about 5 minutes)

### 1. Install Python
Get **Python 3.10 or newer** from <https://www.python.org/downloads/>.
> **Windows:** tick **"Add python.exe to PATH"** on the first screen of the installer!
>
> **macOS / Linux:** also install the Opus audio library: `brew install opus` (macOS) or `sudo apt install libopus0` (Debian/Ubuntu). Windows doesn't need this.

### 2. Create your bot on Discord
1. Go to <https://discord.com/developers/applications> and click **New Application**. Give it a name (that's your bot's name).
2. *(Optional)* Upload a profile picture under **General Information**.
3. Open the **Bot** tab, click **Reset Token**, then **Copy**. Keep this token secret: anyone who has it can control your bot.

You don't need to turn on any "Privileged Gateway Intents".

### 3. Download this project
Click the green **Code** button on GitHub, choose **Download ZIP**, and extract it somewhere (for example, your Desktop).
Or: `git clone` it.

### 4. Start it
- **Windows:** double-click **`start.bat`**
- **macOS / Linux:** open a terminal in the folder and run `./start.sh`

The first time, it creates a settings file called **`.env`** (on Windows it opens in Notepad).
Paste your token after `DISCORD_TOKEN=` like this, then save:

```
DISCORD_TOKEN=MTIzNDU2Nzg5MDEyMzQ1Njc4.GAbCdE.your-long-token-here
```

Run `start.bat` / `./start.sh` again. It installs everything automatically (the first time takes a minute) and starts the bot.

### 5. Add the bot to your server
When the bot is online, the window shows an **invite link**:

```
================================================================
  🎵 Soap#1234 is online!
  Invite link (open it in your browser to add the bot to a server):
  https://discord.com/oauth2/authorize?client_id=...
================================================================
```

Open it, pick your server and click **Authorize**. Then join a voice channel and type **`/play`**. 🎉

> The bot runs while that window is open. Close it (or press Ctrl+C) to stop the bot.
> Every time you start it, it also updates the YouTube downloader, which keeps YouTube working.

---

## ☁️ Hosting on Replit (instead of your PC)

The project comes ready for Replit, so you don't need to change any code.

1. On <https://replit.com>, click **Create Repl → Import from GitHub** and paste this repository's link.
2. Open the **Secrets** tab (🔒 lock icon) and add a secret named **`DISCORD_TOKEN`** with your bot token as the value. On Replit, don't put the token in a file: anyone who opens your Repl could see it.
3. Press **Run**. The first start takes a minute or two while it installs everything. Then the console shows the invite link, the same as on a PC.

Optional settings from the [Settings table](#️-settings-env) (`GUILD_ID`, `DEFAULT_VOLUME`, ...) also go in **Secrets**.

**Two things to know about Replit:**

- **Keeping it online.** A normal Repl stops soon after you close the browser tab. To keep the bot on 24/7, click **Deploy** and pick **Reserved VM**. That's a paid Replit feature; the project already includes the deployment settings.
- **YouTube is pickier on Replit.** Replit runs on cloud servers, and YouTube shows its *"Sign in to confirm you're not a bot"* check to those far more often than to a home PC. If you see that message, upload a `cookies.txt` file (see [Troubleshooting](#️-troubleshooting)) and add a secret `COOKIES_FILE` with the value `cookies.txt`. SoundCloud, Bandcamp and the other sites keep working either way.

---

## 🎛️ The live control panel

Every time a song starts, the bot posts this panel and keeps it updated:

```
🎶 Now Playing
Rick Astley - Never Gonna Give You Up
▶️ 1:23 ━━━━━━━●─────────── 3:33
🎤 Artist: Rick Astley    🙋 Requested by: @you     🌐 Source: YouTube
🔊 Volume: 80%           🔁 Loop: Off              🎛️ Filter: Off
📜 Queue: 4 songs • 14:02 ♾️ Autoplay: Off          🕒 24/7: Off
⏭️ Up next: Darude - Sandstorm 3:45
⏸️ Paused by Sam

[⏮️] [⏸️] [⏭️] [⏹️] [🔁]
[🔉] [🔊] [🔀] [📜] [♾️]
[🎛️ Audio filter          ▼]
```

| Button | What it does |
|---|---|
| ⏮️ | Previous song (or restarts the song if it's more than 5 s in) |
| ⏸️ / ▶️ | Pause / resume |
| ⏭️ | Skip |
| ⏹️ | Stop and clear the queue |
| 🔁 / 🔂 | Loop: off → song → queue (turns green when on) |
| 🔉 / 🔊 | Volume −10% / +10% |
| 🔀 | Shuffle the queue |
| 📜 | Show the queue (only you see it) |
| ♾️ | Autoplay similar songs when the queue ends |
| 🎛️ menu | Pick an audio filter |

Only people in the bot's voice channel can use the buttons. The panel always moves to the bottom of the chat when a new song starts, and the footer shows who pressed what.

---

## 📖 All commands

| Command | Description |
|---|---|
| `/play <song or link>` | Play or queue a song, playlist, or search (suggestions appear as you type) |
| `/playnext <song or link>` | Put a song at the front of the queue |
| `/search <words>` | Pick from the top 5 YouTube results |
| `/pause`, `/resume` | Pause / resume |
| `/skip`, `/previous` | Next / previous song |
| `/skipto <position>` | Jump to a song in the queue |
| `/seek <time>` | Jump to a time: `1:30`, `90`, `1m30s` |
| `/replay` | Restart the current song |
| `/stop` | Stop and clear the queue |
| `/queue` | Show the queue (with pages) |
| `/nowplaying` | Bring the control panel to this channel |
| `/remove <position>` | Remove a song from the queue |
| `/move <from> <to>` | Move a song in the queue |
| `/shuffle`, `/clear` | Shuffle / empty the queue |
| `/loop [off/song/queue]` | Loop mode |
| `/volume [0-200]` | Set the volume (100% = original) |
| `/filter <effect>` | Bass Boost, Nightcore, Vaporwave, Slowed + Reverb, 8D, Karaoke, Treble, Lo-Fi, Off |
| `/autoplay [on/off]` | Keep playing similar songs when the queue ends |
| `/stay [on/off]` | 24/7 mode: stay in the channel when the music ends |
| `/join`, `/leave` | Join your voice channel / leave |
| `/diagnose` | No sound? Checks the whole audio chain and tells you what's wrong |
| `/help` | Show all commands |

---

## ⚙️ Settings (`.env`)

| Setting | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | *(required)* | Your bot token |
| `GUILD_ID` | *(empty)* | Your server ID. Slash command changes show up instantly there. |
| `DEFAULT_VOLUME` | `80` | Starting volume (0-200) |
| `LEAVE_WHEN_DONE` | `true` | Leave the voice channel as soon as the queue is finished (`/stay` overrides it) |
| `IDLE_TIMEOUT` | `300` | Seconds before leaving when everyone left (or when idle, if `LEAVE_WHEN_DONE=false`) |
| `MAX_QUEUE_SIZE` | `500` | Maximum songs in the queue |
| `EMBED_COLOR` | `#8B5CF6` | Color of the bot's messages |
| `COOKIES_FILE` | *(empty)* | YouTube cookies file (see below) |
| `COOKIES_FROM_BROWSER` | *(empty)* | Read YouTube cookies from a browser: `firefox`, `chrome`, `edge`, ... |
| `OPUS_LIBRARY` | *(empty)* | Full path to libopus, only if the bot says it can't find it |

Restart the bot after changing settings.

---

## 🛠️ Troubleshooting

**The slash commands don't show up.**
Wait a minute, then press **Ctrl+R** in Discord to reload. For instant updates, put your server ID in `GUILD_ID` (in Discord: Settings → Advanced → turn on Developer Mode, then right-click your server icon → **Copy Server ID**).

**"YouTube is asking the bot to sign in" / age-restricted videos.**
This rarely happens on a home internet connection, but if it does, give the bot YouTube cookies:
- **Easiest:** log in to YouTube in **Firefox** and set `COOKIES_FROM_BROWSER=firefox`. Chrome and Edge lock their cookies while they're open, so Firefox works best.
- **Or:** install a "Get cookies.txt LOCALLY" browser extension, open youtube.com while logged in, export the cookies as `cookies.txt` into the bot folder and set `COOKIES_FILE=cookies.txt`.

Using a spare Google account for this is a good idea. Never share your cookies file.

**The bot joins but there's no sound.**
Run **`/diagnose`** while a song is playing. It checks every step of the audio chain and tells you exactly what's wrong.
Common causes:
- **Old setup:** `davey` version 0.2 with discord.py 2.7.1 makes the bot play silence. `requirements.txt` now pins `davey<0.2`, and restarting with the start script or Replit's Run button installs the right version.
- **Other causes:** Make sure the bot has the **Connect** and **Speak** permissions in that channel and isn't server-muted. If the console says *"The Opus audio library was not found"*, install it: `brew install opus` (macOS) or `sudo apt install libopus0` (Linux). On Replit, `replit.nix` already handles this.

**YouTube suddenly stopped working.**
YouTube changes things often. Restart the bot: the start script automatically updates `yt-dlp`, which usually fixes it.

**Spotify links don't work.**
Spotify doesn't allow its music to be streamed by other apps. Type the song name instead, or use a YouTube link.

**"Python was not found" on Windows.**
Reinstall Python and tick **"Add python.exe to PATH"**.

---

## 🧑‍💻 For tinkerers

```
bot.py                  starts the bot, syncs slash commands, prints the invite link
musicbot/config.py      reads .env
musicbot/audio.py       yt-dlp search/extraction, audio filters, FFmpeg streaming
musicbot/musicqueue.py  queue, loop modes and history (plain Python)
musicbot/player.py      one player per server: playback loop, controls, auto-leave
musicbot/ui.py          embeds, live control panel, queue pages, search menu
musicbot/cogs/music.py  all slash commands
tests/                  unit tests:  python -m unittest
.replit, replit.nix     Replit setup (run command, deployment, libopus + ffmpeg)
```

Built with [discord.py](https://github.com/Rapptz/discord.py), [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [FFmpeg](https://ffmpeg.org/).
