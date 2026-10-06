"""
Telegram Batch Forwarder (Pyrogram)

Login 2 tarah se:
  1) Phone number -> OTP -> (2FA password)   [bot ke andar]
  2) Session string

Login ke baad user ka apna account (assistant) source se padhta hai aur
destination me copy karta hai. Private channel ke liye wo account
channel ka member hona chahiye.

Env vars:
  API_ID, API_HASH, BOT_TOKEN   (required)
  ALLOWED_USERS  : comma separated user ids (khali = sab use kar sakte hain)
  ENC_KEY        : Fernet key, sessions DB me encrypt hongi (recommended)
  DB_PATH        : default sessions.db
  MSG_DELAY      : default 1.0 (groups me min 3.0)
  MAX_RETRY      : default 3
  MAX_RANGE      : default 5000
  MAX_ACTIVE     : default 3
  LOGIN_TTL      : login steps ka timeout seconds, default 300

ENC_KEY banane ke liye:
  python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"

Terminal se session string banani ho to:
  python forwarder.py gen
"""
import os
import re
import sys
import time
import sqlite3
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, Tuple, Union

from pyrogram import Client, filters, enums, idle, errors as E
from pyrogram.errors import FloodWait, MessageIdInvalid, RPCError
from pyrogram.types import (
    BotCommand, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fwd")

# ── config ───────────────────────────────────────────────────────────
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


# ── gen mode ─────────────────────────────────────────────────────────
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


# ── session storage ──────────────────────────────────────────────────
_fernet = None
if ENC_KEY:
    from cryptography.fernet import Fernet
    _fernet = Fernet(ENC_KEY.encode())
else:
    log.warning("ENC_KEY set nahi hai: sessions DB me plain text me save hongi.")

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
        log.error("Session decrypt fail uid=%s (ENC_KEY badla?)", uid)
        return None


def db_del(uid: int) -> None:
    _db.execute("DELETE FROM sessions WHERE uid = ?", (uid,))
    _db.commit()


# ── clients ──────────────────────────────────────────────────────────
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
        async for _ in c.get_dialogs():
            pass
    except Exception as e:
        log.warning("Dialog warm-up failed: %s", e)


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


# ── login state ──────────────────────────────────────────────────────
@dataclass
class LoginState:
    step: str                      # phone | code | password | string
    client: Optional[Client] = None
    phone: str = ""
    code_hash: str = ""
    tries: int = 0
    ts: float = field(default_factory=time.time)


logins: Dict[int, LoginState] = {}


async def _disconnect(c: Optional[Client]) -> None:
    if c is not None and c.is_connected:
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
                await bot.send_message(
                    uid, "⌛ Login session expire ho gaya. /login se dubara shuru karo."
                )
            except Exception:
                pass
            return


async def begin_login(uid: int, step: str) -> LoginState:
    await drop_login(uid)
    st = LoginState(step=step)
    logins[uid] = st
    spawn(_login_watch(uid, st))
    return st


# ── batch state ──────────────────────────────────────────────────────
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
    def total(self) -> int:
        return self.hi - self.lo + 1

    @property
    def processed(self) -> int:
        return max(0, self.cur - self.lo + 1)

    @property
    def pct(self) -> float:
        return self.processed / self.total * 100 if self.total else 0.0

    @property
    def eta(self) -> str:
        n = self.processed
        if n <= 0:
            return "—"
        rate = n / max(time.time() - self.t0, 1e-6)
        rem = int((self.hi - self.cur) / rate) if rate else 0
        m, s = divmod(rem, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


sessions: Dict[int, Sess] = {}


# ── texts & keyboards ────────────────────────────────────────────────
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
        "Poori guide ke liye /help dabao."
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
    "/cancel: batch ya login roko\n\n"
    "**Dhyan rakho**\n"
    "• Max `{max_range}` messages ek batch me\n"
    "• Speed Telegram limits ke hisaab se rakhi gayi hai (groups me dheema)\n"
    "• 'Restrict Saving Content' wale chats copy nahi hote\n"
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


# ── helpers ──────────────────────────────────────────────────────────
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


_RANGE_RE = re.compile(
    r"^https?://t(?:elegram)?\.me/(?:c/(\d+)|([A-Za-z]\w{3,}))/(\d+)-(\d+)$"
)


def parse_range(text: str) -> Optional[Tuple[ChatRef, int, int]]:
    m = _RANGE_RE.match(text.strip())
    if not m:
        return None
    priv, uname, a, b = m.groups()
    chat: ChatRef = int(f"-100{priv}") if priv else uname
    lo, hi = int(a), int(b)
    return (chat, lo, hi) if hi >= lo else None


def parse_dst(raw: str) -> Optional[ChatRef]:
    raw = raw.strip()
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    m = re.fullmatch(r"@?([A-Za-z]\w{3,})", raw)
    if m:
        return m.group(1)
    m = re.fullmatch(r"https?://t(?:elegram)?\.me/([A-Za-z]\w{3,})/?", raw)
    return m.group(1) if m else None


def _is_allowed(_, __, update) -> bool:
    return bool(update.from_user) and (not ALLOWED or update.from_user.id in ALLOWED)


def _in_login(_, __, msg: Message) -> bool:
    return (
        bool(msg.from_user)
        and msg.from_user.id in logins
        and bool(msg.text)
        and not msg.text.startswith("/")
    )


allowed_filter = filters.create(_is_allowed)
login_filter = filters.create(_in_login)
_SESSION_RE = re.compile(r"^[A-Za-z0-9_\-=]{200,}$")


async def edit_progress(s: Sess, text: Optional[str] = None) -> None:
    if not s.pmsg:
        return
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


# ── core copy logic ──────────────────────────────────────────────────
async def copy_one(s: Sess, m: Message) -> None:
    attempts = 0
    while True:
        try:
            if m.media_group_id:
                await s.client.copy_media_group(
                    s.dst, s.src, m.id, disable_notification=True
                )
            else:
                await s.client.copy_message(
                    s.dst, s.src, m.id, disable_notification=True
                )
            return
        except FloodWait as fw:
            log.warning("FloodWait %ss (msg %s)", fw.value, m.id)
            await asyncio.sleep(fw.value + 1)
        except FATAL:
            raise
        except MessageIdInvalid:
            raise
        except RPCError as e:
            attempts += 1
            log.warning("RPC %s attempt %d/%d msg %s", e, attempts, MAX_RETRY, m.id)
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
        except Exception as e:
            s.note = (f"Source access nahi mila ({type(e).__name__}). "
                      "Tumhara account us chat ka member hona chahiye.")
            return
        try:
            dst_chat = await s.client.get_chat(s.dst)
        except Exception as e:
            s.note = (f"Destination access nahi mila ({type(e).__name__}). "
                      "Tumhara account dst me hona chahiye.")
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
                s.note = "Session expire/revoke ho gaya. /login dubara karo."
                db_del(s.uid)
                clients.pop(s.uid, None)
                return
            except FATAL as e:
                s.note = f"Source error: {type(e).__name__}"
                return
            except RPCError as e:
                log.error("get_messages failed: %s", e)
                s.err += len(ids)
                s.cur = ids[-1]
                continue

            for m in msgs:
                if not s.alive:
                    break
                s.cur = m.id if m.id else s.cur + 1

                if m.empty or m.service:
                    s.skip += 1
                    continue

                if m.media_group_id:
                    if m.media_group_id in seen_groups:
                        s.done += 1
                        continue
                    seen_groups.add(m.media_group_id)

                try:
                    await copy_one(s, m)
                    s.done += 1
                except MessageIdInvalid:
                    s.skip += 1
                except FATAL as e:
                    name = type(e).__name__
                    if name == "ChatForwardsRestricted":
                        s.note = "Source me 'Restrict Saving Content' on hai, copy nahi ho sakta."
                    else:
                        s.note = f"Access error: {name}"
                    s.err += 1
                    return
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
        s.note = "Unexpected error, logs check karo."
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
        log.info("Batch end uid=%s done=%d skip=%d err=%d", s.uid, s.done, s.skip, s.err)


# ── login flow ───────────────────────────────────────────────────────
async def complete_login(uid: int, chat_id: int, string: str) -> None:
    """Session string se client start karo, validate karo, save karo."""
    if uid in sessions:
        await bot.send_message(chat_id, "⚠️ Batch chal raha hai. Pehle /cancel karo, phir login badlo.")
        return

    wait = await bot.send_message(chat_id, "🔐 Login final ho raha hai, chats load ho rahi hain…")
    try:
        c = await _start_user_client(uid, string)
        me = await c.get_me()
        await _warm_cache(c)
    except AUTH_DEAD:
        await wait.edit_text("❌ Ye session ab valid nahi hai. Dubara /login karo.")
        return
    except Exception as e:
        log.error("Login failed uid=%s: %s", uid, e)
        await wait.edit_text(f"❌ Login fail: `{type(e).__name__}`")
        return

    async with _cl_lock:
        old = clients.pop(uid, None)
        clients[uid] = c
    if old is not None:
        try:
            await old.stop()
        except Exception:
            pass

    db_save(uid, string)
    uname = f" (@{me.username})" if me.username else ""
    await wait.edit_text(
        f"✅ **Login ho gaya!**\n\n👤 {me.first_name}{uname}\n\n"
        "Ab `/batch <link_range> <dest>` use kar sakte ho.\n"
        "Guide: /help"
    )


async def _finish_phone_login(uid: int, chat_id: int, st: LoginState) -> None:
    c = st.client
    try:
        string = await c.export_session_string()
    except Exception as e:
        await drop_login(uid)
        await bot.send_message(chat_id, f"❌ Session export fail: `{type(e).__name__}`")
        return
    await _disconnect(c)
    logins.pop(uid, None)
    await complete_login(uid, chat_id, string)


@bot.on_message(filters.private & login_filter)
async def on_login_text(_, msg: Message):
    uid, chat_id = msg.from_user.id, msg.chat.id
    st = logins.get(uid)
    if not st:
        return
    text = (msg.text or "").strip()
    st.ts = time.time()

    # sensitive message turant delete
    try:
        await msg.delete()
    except Exception:
        pass

    async def say(t: str, **kw):
        return await bot.send_message(chat_id, t, **kw)

    # ── string ──
    if st.step == "string":
        await drop_login(uid)
        if not _SESSION_RE.match(text):
            return await say("❌ Ye valid session string nahi lag rahi. /login se dubara try karo.")
        return await complete_login(uid, chat_id, text)

    # ── phone ──
    if st.step == "phone":
        phone = re.sub(r"[\s\-()]", "", text)
        if not re.fullmatch(r"\+?\d{8,15}", phone):
            return await say(
                "❌ Number sahi nahi hai. Country code ke saath bhejo, jaise `+919876543210`",
                reply_markup=kb_cancel(),
            )
        if not phone.startswith("+"):
            phone = "+" + phone

        note = await say("📨 Code bhej raha hoon…")
        c = Client(
            f"login_{uid}", api_id=API_ID, api_hash=API_HASH,
            in_memory=True, no_updates=True,
        )
        try:
            await c.connect()
            sent = await c.send_code(phone)
        except E.PhoneNumberInvalid:
            await _disconnect(c)
            return await note.edit_text("❌ Ye phone number invalid hai. Dubara bhejo.", reply_markup=kb_cancel())
        except E.PhoneNumberBanned:
            await _disconnect(c)
            await drop_login(uid)
            return await note.edit_text("🚫 Ye number Telegram pe banned hai.")
        except FloodWait as fw:
            await _disconnect(c)
            await drop_login(uid)
            return await note.edit_text(f"⏳ Telegram ne rok diya. {fw.value}s baad try karo.")
        except Exception as e:
            log.error("send_code failed uid=%s: %s", uid, e)
            await _disconnect(c)
            await drop_login(uid)
            return await note.edit_text(f"❌ Code nahi bhej paya: `{type(e).__name__}`")

        st.client, st.phone, st.code_hash, st.step = c, phone, sent.phone_code_hash, "code"
        st.ts = time.time()
        return await note.edit_text(
            "✅ **Code bhej diya!**\n\n"
            "Telegram app ki official **Telegram** chat (ya SMS) me code check karo.\n\n"
            "Code **space ke saath** bhejo, jaise:\n`1 2 3 4 5`\n\n"
            "⚠️ Seedha `12345` bhejoge to Telegram code block kar deta hai.",
            reply_markup=kb_cancel(),
        )

    # ── code ──
    if st.step == "code":
        digits = re.sub(r"\D", "", text)
        if not 4 <= len(digits) <= 7:
            return await say("❌ Code sahi nahi lag raha. Jaise `1 2 3 4 5` bhejo.", reply_markup=kb_cancel())
        try:
            res = await st.client.sign_in(st.phone, st.code_hash, digits)
        except E.SessionPasswordNeeded:
            st.step = "password"
            st.tries = 0
            return await say(
                "🔒 **2-step verification password** bhejo.\n__Message turant delete ho jayega.__",
                reply_markup=kb_cancel(),
            )
        except E.PhoneCodeInvalid:
            st.tries += 1
            if st.tries >= 3:
                await drop_login(uid)
                return await say("❌ 3 baar galat code. /login se dubara shuru karo.")
            return await say(
                f"❌ Galat code ({st.tries}/3). Dubara bhejo (`1 2 3 4 5` format me).",
                reply_markup=kb_cancel(),
            )
        except E.PhoneCodeExpired:
            await drop_login(uid)
            return await say("⌛ Code expire ho gaya. /login se naya code mangwao.")
        except FloodWait as fw:
            await drop_login(uid)
            return await say(f"⏳ Telegram ne rok diya. {fw.value}s baad try karo.")
        except Exception as e:
            log.error("sign_in failed uid=%s: %s", uid, e)
            await drop_login(uid)
            return await say(f"❌ Login fail: `{type(e).__name__}`")

        if res is False or not hasattr(res, "id"):
            await drop_login(uid)
            return await say(
                "❌ Is number ka Telegram account nahi mila ya terms accept nahi hue. "
                "Pehle Telegram app me account bana lo."
            )
        return await _finish_phone_login(uid, chat_id, st)

    # ── 2FA password ──
    if st.step == "password":
        try:
            await st.client.check_password(text)
        except E.PasswordHashInvalid:
            st.tries += 1
            if st.tries >= 3:
                await drop_login(uid)
                return await say("❌ 3 baar galat password. /login se dubara shuru karo.")
            return await say(f"❌ Galat password ({st.tries}/3). Dubara bhejo.", reply_markup=kb_cancel())
        except FloodWait as fw:
            await drop_login(uid)
            return await say(f"⏳ Telegram ne rok diya. {fw.value}s baad try karo.")
        except Exception as e:
            log.error("check_password failed uid=%s: %s", uid, e)
            await drop_login(uid)
            return await say(f"❌ Login fail: `{type(e).__name__}`")
        return await _finish_phone_login(uid, chat_id, st)


# ── commands ─────────────────────────────────────────────────────────
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
    if len(args) == 2:  # /login <session_string>
        try:
            await msg.delete()
        except Exception:
            pass
        string = args[1].strip()
        if not _SESSION_RE.match(string):
            return await bot.send_message(msg.chat.id, "❌ Ye valid session string nahi lag rahi.")
        return await complete_login(uid, msg.chat.id, string)
    await msg.reply(LOGIN_CHOOSE_TEXT, reply_markup=kb_login())


@bot.on_message(filters.command("logout") & filters.private & allowed_filter)
async def cmd_logout(_, msg: Message):
    uid = msg.from_user.id
    await drop_login(uid)
    s = sessions.get(uid)
    if s:
        s.alive = False
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
    if c is not None:
        try:
            await c.log_out()      # Telegram side se session terminate
            revoked = True
        except Exception as e:
            log.warning("log_out failed uid=%s: %s", uid, e)
            try:
                await c.stop()
            except Exception:
                pass
    db_del(uid)
    if not (had or c):
        return await msg.reply("ℹ️ Tum logged in nahi ho.")
    await msg.reply(
        "✅ **Logout ho gaya.**\n"
        + ("Session Telegram se bhi terminate kar diya gaya.\n" if revoked else
           "Session bot se hata diya. Pura revoke karne ke liye Telegram → Settings → Devices "
           "me session terminate karo.\n")
        + "\nDubara login ke liye /login."
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
        return await msg.reply(
            "📦 **Batch usage**\n\n"
            "`/batch <link_range> <dest>`\n\n"
            "**Examples**\n"
            "`/batch https://t.me/mychan/100-200 -1001234567890`\n"
            "`/batch https://t.me/c/1234567890/100-200 @mychannel`\n\n"
            "Detail: /help"
        )

    parsed = parse_range(args[1])
    if not parsed:
        return await msg.reply(
            "❌ **Range sahi nahi hai.**\n\n"
            "Format:\n`https://t.me/<channel>/<start>-<end>`\n"
            "`https://t.me/c/<id>/<start>-<end>`"
        )
    src, lo, hi = parsed

    dst = parse_dst(args[2])
    if dst is None:
        return await msg.reply("❌ Destination sahi nahi hai. Chat ID (`-100…`) ya `@username` do.")

    if hi - lo + 1 > MAX_RANGE:
        return await msg.reply(f"❌ Range bahut bada hai. Ek batch me max `{MAX_RANGE}` messages.")

    if uid in sessions:
        return await msg.reply("⚠️ Pehle se batch chal raha hai. /status dekho ya /cancel karo.")
    if len(sessions) >= MAX_ACTIVE:
        return await msg.reply("⏳ Abhi bot busy hai, thodi der baad try karo.")

    client = await get_client(uid)
    if client is None:
        return await msg.reply("🔑 Pehle /login karke apna account connect karo.")

    s = Sess(uid=uid, src=src, dst=dst, lo=lo, hi=hi, client=client, cur=lo - 1)
    sessions[uid] = s
    s.pmsg = await msg.reply(fmt_progress(s))
    spawn(run_batch(s))
    log.info("Batch start uid=%s src=%s %d-%d dst=%s", uid, src, lo, hi, dst)


@bot.on_message(filters.command("cancel") & filters.private & allowed_filter)
async def cmd_cancel(_, msg: Message):
    uid = msg.from_user.id
    if uid in logins:
        await drop_login(uid)
        return await msg.reply("🛑 Login cancel kar diya.")
    s = sessions.get(uid)
    if not s or not s.alive:
        return await msg.reply("ℹ️ Koi active batch ya login nahi hai.")
    s.alive = False
    await msg.reply("🛑 Cancel request mili. Current message ke baad ruk jayega.")


@bot.on_message(filters.command("status") & filters.private & allowed_filter)
async def cmd_status(_, msg: Message):
    s = sessions.get(msg.from_user.id)
    if not s:
        return await msg.reply("ℹ️ Koi active batch nahi hai.\n\nNaya batch: /batch")
    await msg.reply(fmt_progress(s))


# ── inline buttons ───────────────────────────────────────────────────
@bot.on_callback_query(allowed_filter)
async def on_callback(_, cb: CallbackQuery):
    uid = cb.from_user.id
    data = cb.data or ""
    msg = cb.message

    async def show(text: str, kb: Optional[InlineKeyboardMarkup] = None):
        try:
            await msg.edit_text(text, reply_markup=kb)
        except Exception:
            pass

    if data == "menu_start":
        await show(start_text(cb.from_user.first_name or "dost"), kb_start())
    elif data == "menu_help":
        await show(HELP_TEXT, kb_back())
    elif data == "menu_login":
        await show(LOGIN_CHOOSE_TEXT, kb_login())
    elif data == "menu_me":
        text, _ok = await account_text(uid)
        await show(text, kb_back())
    elif data == "login_phone":
        if uid in sessions:
            return await cb.answer("Batch chal raha hai, pehle /cancel karo.", show_alert=True)
        await begin_login(uid, "phone")
        await show(PHONE_PROMPT, kb_cancel())
    elif data == "login_string":
        if uid in sessions:
            return await cb.answer("Batch chal raha hai, pehle /cancel karo.", show_alert=True)
        await begin_login(uid, "string")
        await show(STRING_PROMPT, kb_cancel())
    elif data == "login_cancel":
        await drop_login(uid)
        await show("🛑 Login cancel kar diya.\n\nDubara: /login", kb_back())
    await cb.answer()


# ── entry point ──────────────────────────────────────────────────────
async def main() -> None:
    await bot.start()
    try:
        await bot.set_bot_commands([
            BotCommand("start", "Bot shuru karo"),
            BotCommand("help", "Poori guide"),
            BotCommand("login", "Account login (phone / string)"),
            BotCommand("batch", "Messages forward karo"),
            BotCommand("status", "Batch progress"),
            BotCommand("cancel", "Batch ya login roko"),
            BotCommand("me", "Logged-in account"),
            BotCommand("logout", "Logout + session revoke"),
        ])
    except Exception as e:
        log.warning("set_bot_commands failed: %s", e)
    me = await bot.get_me()
    log.info("Bot ready: @%s", me.username)
    await idle()
    await bot.stop()


if __name__ == "__main__":
    if not (API_ID and API_HASH and BOT_TOKEN):
        sys.exit("API_ID, API_HASH, BOT_TOKEN set karo.")
    log.info("Starting forwarder bot…")
    bot.run(main())
