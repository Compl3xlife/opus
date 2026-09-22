# Opus

Local Windows assistant that lives in the tray, listens for **“Opus”**, and answers using a cloud model. Say **“Opus open your panel”** for settings.

## What it does now

- Runs in the background (system tray)
- Listens on your microphone for the wake word **Opus**
- Opens a settings panel by voice, tray click, or `Ctrl+Alt+O`
- Voice responses, volume, microphone, and speaker selection
- Reads the active tab when the browser extension is installed (Chrome, Edge, Brave, Opera GX, Firefox)
- Looks at your screen when a question is about what you are seeing
- Searches and opens files you ask for
- Records to `D:\Clips\Opus\Recordings` and saves 30-second clips to `D:\Clips\Opus\Clips`. Xbox Game Bar (`Win+Alt+G` / `Win+Alt+R`) is used as a backup.
- Scans new Downloads (and completed browser downloads) with **Windows Defender**

Audio stays on the PC until you say “Opus”. After that, the clip is sent to your STT/LLM API so answers stay fast.

## Setup

1. Run `setup.ps1` in PowerShell (creates a virtualenv and installs packages).
2. Start Opus with `.\run_opus.bat`.
3. Open the panel, paste an OpenAI-compatible API key (OpenAI, Groq, OpenRouter, etc.).
4. Load the unpacked extension:

| Browser | Load unpacked from |
| --- | --- |
| Chrome / Brave / Edge | `extensions\chromium` |
| Opera GX | `extensions\chromium` (opera://extensions, enable developer mode) |
| Firefox | `extensions\firefox` (`about:debugging` → This Firefox → Load Temporary Add-on) |

Keep Opus running. The extension talks only to `127.0.0.1`.

### Voice commands

- “Opus”
- “Opus open your panel”
- “Opus what’s on my screen?”
- “Opus clip that” / “Opus start recording”
- “Opus scan my downloads”
- “Opus who is the president?” (uses live web lookup)

Hotkeys: `Ctrl+Alt+O` panel, `Ctrl+Shift+Space` listen for the next sentence without the wake word.

## Privacy

Opus can read files and capture the screen **on this PC** when those options are on. It does not upload your whole disk. Screenshots are sent to the model only when the question looks visual. Download scanning uses Windows Defender, not a custom antivirus engine.

Turn off screen awareness, file access, or download scanning in the panel if you want a smaller footprint.

## Discord Bot (optional)

Opus can also answer questions in a Discord server.

1. Create a Discord bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable **Message Content Intent** for the bot.
3. Invite it to your server with message read/send permissions.
4. Add these keys to `%AppData%\Roaming\Opus\settings.json`:
   - `"discord_enabled": true`
   - `"discord_bot_token": "YOUR_TOKEN"`
   - Optional `"discord_prefix": "opus"` (default trigger prefix)
   - Optional `"discord_allowed_guilds": "123,456"` and `"discord_allowed_channels": "789,012"` (comma-separated IDs)

When enabled, anyone in allowed servers/channels can ask with:
- `opus your question`
- or by mentioning the bot.

Discord mode is chat-only (Q&A) and does not run local PC control actions.

## Phone version

Opus on iPhone is a **home-screen web app**. It runs on the phone itself — this PC can be off. Chat, weather, Spotify, and talk use the **phone’s internet**. Coin flip, dice, volume, and a few other commands still work offline. If a command needs the network and there isn’t one, Opus says: *Sorry I cannot find a good connection for that at the moment.*

iPhone will **not** let Opus keep the microphone on in the background the way Spotify keeps playing music. Leave the Opus screen open and say **Opus**, then the command. If you switch apps, iPhone pauses the mic. For when the app is closed, use a Shortcut named **Opus** that opens `https://compl3xlife.github.io/opus/?listen=1`, then **Add to Siri**.

Games, screenshots, and Windows control stay on the PC tray app (`.\run_opus.bat`).

### Install on iPhone (no PC required after this)

1. On the iPhone, open **https://compl3xlife.github.io/opus/** in **Safari** (not Chrome).
2. Tap **Share → Add to Home Screen**.
3. Open the Opus icon, paste an API key in settings. For Spotify, add the printed redirect URI in your Spotify Developer app, then Connect.
4. Optional: Shortcuts app → Open URL `https://compl3xlife.github.io/opus/?listen=1` → Add to Siri as **Opus**.

The microphone needs HTTPS, which GitHub Pages provides.

### Preview on this PC (optional)

`.\run_opus_phone.bat` still serves the same page at `http://YOUR-PC-IP:5841` for local testing. The iPhone does not need this after the Pages URL is installed.

To use a **USB cable instead of Wi-Fi** (just this phone and this PC):

1. Plug the iPhone into this PC and tap **Trust**.
2. On the iPhone, turn on **Personal Hotspot** (USB is enough; Wi-Fi hotspot can stay off).
3. Run `.\run_opus_phone.bat` and open the printed **USB phone** address in Safari.

USB hotspot is mainly so the PC can use the phone’s internet. Some iPhones still cannot open the PC’s page over the cable. If Safari fails, use https://compl3xlife.github.io/opus/ instead. The microphone also needs HTTPS, so hold-to-talk may fail on the `http://` USB address.

Settings for the standalone app are stored **on the phone**, not in `%AppData%\Roaming\OpusPhone`.
