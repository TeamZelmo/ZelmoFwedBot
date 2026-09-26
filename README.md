<!-- ═══════════════════════════════════════════════════════════════════ -->
<!--                  coded by devgagan · github.com/devagganin          -->
<!-- ═══════════════════════════════════════════════════════════════════ -->

<div align="center">

```
██████╗ ███████╗██╗   ██╗ ██████╗  █████╗  ██████╗  █████╗ ███╗   ██╗
██╔══██╗██╔════╝██║   ██║██╔════╝ ██╔══██╗██╔════╝ ██╔══██╗████╗  ██║
██║  ██║█████╗  ██║   ██║██║  ███╗███████║██║  ███╗███████║██╔██╗ ██║
██║  ██║██╔══╝  ╚██╗ ██╔╝██║   ██║██╔══██║██║   ██║██╔══██║██║╚██╗██║
██████╔╝███████╗ ╚████╔╝ ╚██████╔╝██║  ██║╚██████╔╝██║  ██║██║ ╚████║
╚═════╝ ╚══════╝  ╚═══╝   ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═══╝
```

# `[ TG-FORWARDER ]`

**`// crypcoded · multi-user · production-grade · pyrogram`**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Pyrogram](https://img.shields.io/badge/Pyrogram-2.0.106-2CA5E0?style=flat-square&logo=telegram&logoColor=white)](https://pyrogram.org)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![Author](https://img.shields.io/badge/Author-devgagan-red?style=flat-square&logo=github)](https://github.com/devagganin)

```
╔══════════════════════════════════════════════════════════╗
║  coded by  devgagan  ·  github.com/devgaganin            ║
╚══════════════════════════════════════════════════════════╝
```

</div>

---

## `> cat description.txt`

```
A highly async, multi-user Telegram message forwarder
built on Pyrogram. Copies entire message ranges — text, photo, video,
document, audio, voice, sticker, animation, poll, contact, venue,
location — from any public or accessible channel to any destination,
with live progress tracking, FloodWait handling, exponential backoff,
and per-user session isolation.
```

---

## `> ls -la features/`

```
drwxr-xr-x  features/
├── [✓]  all media types          — copy_message() handles everything natively
├── [✓]  batch range forwarding   — t.me/chan/100-500 one-liner syntax
├── [✓]  multi-user sessions      — every user gets an isolated _Sess dataclass
├── [✓]  live progress bar        — ▓▓▓▓▓░░░░░ with ETA, counts, %
├── [✓]  /start /batch /cancel /status
├── [✓]  FloodWait auto-sleep     — exact Telegram-mandated delay
├── [✓]  exponential backoff      — 2^n retry on transient RPC errors
├── [✓]  skip deleted msgs        — MessageIdInvalid handled silently
├── [✓]  env-driven config        — zero hardcoded secrets
├── [✓]  Docker ready             — one-liner deploy
├── [✓]  obfuscated internals     — ζ ψ ξ Ω — Greek/unicode identifiers
└── [✓]  owner-lock optional      — OWNER_ID=0 → open to all users
```

---

## `> cat commands.md`

| Command | Syntax | Description |
|---|---|---|
| `/start` | `/start` | Show help & syntax |
| `/batch` | `/batch <link_range> <dest>` | Start forwarding a message range |
| `/cancel` | `/cancel` | Stop your active batch mid-run |
| `/status` | `/status` | Show live progress of running batch |

### Batch Syntax

```bash
# Public channel (username)
/batch https://t.me/channelname/40756-40900 -1001234567890

# Private channel (numeric ID via t.me/c/...)
/batch https://t.me/c/3475816102/40756-40900 -1009876543210

# Forward to "Saved Messages"
/batch https://t.me/mychan/1-500 me
```

> `start_id` and `end_id` are extracted automatically from the link range.
> The bot copies every message ID from `start` to `end` inclusive.

---

## `> cat .env.example`

```env
# ── Required ───────────────────────────────────────────────
API_ID=12345678
API_HASH=abcdef1234567890abcdef1234567890
BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# ── Optional ───────────────────────────────────────────────
OWNER_ID=0          # Restrict to one Telegram user ID. 0 = open
MSG_DELAY=0.8       # Seconds between each forwarded message
MAX_RETRY=3         # Max retries per message on RPC error
```

Get your credentials:
- `API_ID` / `API_HASH` → [my.telegram.org](https://my.telegram.org)
- `BOT_TOKEN` → [@BotFather](https://t.me/BotFather)

---

## `> ./deploy.sh`

---

### `[1] VPS — bare metal / Ubuntu`

```bash
# ── system prep ─────────────────────────────────────
sudo apt update && sudo apt install -y python3 python3-pip git screen

# ── clone & enter ───────────────────────────────────
git clone https://github.com/devagganin/tg-forwarder
cd tg-forwarder

# ── install deps ────────────────────────────────────
pip3 install -r requirements.txt

# ── configure ───────────────────────────────────────
cp .env.example .env
nano .env                  # fill in API_ID, API_HASH, BOT_TOKEN

# ── run in background (screen) ──────────────────────
screen -S fwdbot
python3 forwarder_bot.py
# Ctrl+A then D to detach · screen -r fwdbot to reattach

# ── OR use systemd (recommended for production) ─────
sudo nano /etc/systemd/system/fwdbot.service
```

```ini
[Unit]
Description=TG Forwarder Bot
After=network.target

[Service]
WorkingDirectory=/root/tg-forwarder
EnvironmentFile=/root/tg-forwarder/.env
ExecStart=/usr/bin/python3 forwarder_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fwdbot
sudo systemctl status fwdbot
```

---

### `[2] Docker — any Linux host`

```bash
# ── build ───────────────────────────────────────────
docker build -t fwdbot .

# ── run ─────────────────────────────────────────────
docker run -d \
  --name fwdbot \
  --restart unless-stopped \
  -e API_ID=12345678 \
  -e API_HASH=abcdef... \
  -e BOT_TOKEN=123:AAxx... \
  -e OWNER_ID=0 \
  -e MSG_DELAY=0.8 \
  -v $(pwd)/sessions:/app/sessions \
  fwdbot

# ── logs ────────────────────────────────────────────
docker logs -f fwdbot
```

Or with `docker-compose`:

```yaml
version: "3.9"
services:
  fwdbot:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./sessions:/app/sessions
```

```bash
docker compose up -d
docker compose logs -f
```

---

### `[3] Heroku`

```bash
# ── prerequisites ───────────────────────────────────
# Install Heroku CLI: https://devcenter.heroku.com/articles/heroku-cli

heroku login
heroku create your-fwdbot-name

# ── set env vars ────────────────────────────────────
heroku config:set API_ID=12345678
heroku config:set API_HASH=abcdef1234567890abcdef1234567890
heroku config:set BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
heroku config:set OWNER_ID=0
heroku config:set MSG_DELAY=0.8
heroku config:set MAX_RETRY=3

# ── push & scale ────────────────────────────────────
git push heroku main
heroku ps:scale worker=1

# ── logs ────────────────────────────────────────────
heroku logs --tail
```

> ⚠️ Add a `Procfile` in the project root:
> ```
> worker: python forwarder_bot.py
> ```

> ⚠️ Heroku's ephemeral filesystem loses session files on restart.
> Use a Redis/Postgres add-on or attach persistent storage if needed.

---

## `> cat architecture.txt`

```
┌─────────────────────────────────────────────────────────┐
│                    USER (Telegram)                       │
│           /batch  /cancel  /status  /start               │
└──────────────────────────┬──────────────────────────────┘
                           │
                    ┌──────▼──────┐
                    │  Bot Client  │  ← Pyrogram Client
                    │  (async)     │
                    └──────┬───────┘
                           │
          ┌────────────────▼─────────────────┐
          │         _sessions: Dict           │
          │  uid_1 → _Sess(lo, hi, dst, …)   │
          │  uid_2 → _Sess(lo, hi, dst, …)   │
          │  uid_n → …                        │
          └────────────────┬─────────────────┘
                           │
              ┌────────────▼────────────┐
              │   _ψ_run_batch(task)    │  asyncio background task
              │   per user, isolated    │
              └────────────┬────────────┘
                           │
              for mid in range(lo, hi+1):
                           │
              ┌────────────▼────────────┐
              │    _ψ_fwd_one(mid)      │
              │  get_messages → copy    │
              │  FloodWait / retry      │
              └─────────────────────────┘
```

---

## `> cat media_support.txt`

```
TYPE          METHOD           NOTES
──────────    ──────────────   ────────────────────────────────
text          copy_message()   captions preserved
photo         copy_message()   all resolutions
video         copy_message()   streaming-safe
document      copy_message()   any mime type / size
audio         copy_message()   mp3, ogg, flac …
voice         copy_message()   ogg/opus blobs
video_note    copy_message()   round video messages
sticker       copy_message()   static + animated (tgs) + video
animation     copy_message()   gif/mp4
poll          copy_message()   quiz + regular polls
contact       copy_message()   vCard data
venue         copy_message()   location + title
location      copy_message()   lat/lng coordinates
```

---

## `> cat error_handling.md`

```python
FloodWait      → sleep(fw.value + 1)   # exact Telegram mandate
MessageIdInvalid → "skip"              # deleted msg, non-fatal
ChannelPrivate   → "err"               # access denied, stops msg
RPCError         → retry ×MAX_RETRY    # exponential backoff 2^n
Exception        → log + "err"         # catch-all, non-fatal
```

---

## `> cat obfuscation.md`

```
Internal symbols use Greek/Unicode identifiers to obscure logic:

  _ζ   →  env-var fetcher lambda
  _ψ   →  async forward functions prefix
  _ξ   →  constants / frozensets prefix
  _L   →  logger handle
  _Sess → session dataclass (abbreviated)

Variable names: lo, hi, cur, uid, dst, src, pm, fw
All internal helpers are underscore-prefixed (private by convention).
Progress bar uses Unicode block chars: ▓ ░
```

---

## `> tree .`

```
tg-forwarder/
├── forwarder_bot.py     ← main bot  (obfuscated)
├── requirements.txt     ← pyrogram + tgcrypto
├── Dockerfile           ← docker deploy
├── .env.example         ← config template
└── README.md            ← you are here
```

---

## `> cat license.txt`

```
MIT License — use freely, credit appreciated.
```

---

<div align="center">

```
╔═══════════════════════════════════════════════╗
║                                               ║
║   coded by  devgagan                          ║
║   github.com/devgaganin                       ║
║                                               ║
║   // build fast. break nothing. stay anon.    ║
║                                               ║
╚═══════════════════════════════════════════════╝
```

</div>
