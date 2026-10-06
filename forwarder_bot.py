"""
Telegram Batch Forwarder - in-bot session-string login

Har user bot me /login karke apna session string deta hai. Us user ka
assistant account (user session) source se padhta hai aur destination me
copy karta hai. Private channel ke liye wo account channel ka member hona chahiye.

Env vars:
  API_ID, API_HASH, BOT_TOKEN   (required)
  ALLOWED_USERS  : comma separated user ids (khali = sab use kar sakte hain)
  ENC_KEY        : Fernet key, session strings DB me encrypt hongi (recommended)
  DB_PATH        : default sessions.db
  MSG_DELAY      : default 1.0  (groups me min 3.0)
  MAX_RETRY      : default 3
  MAX_RANGE      : default 5000
  MAX_ACTIVE     : default 3

Session string banane ke liye (terminal me):
  python forwarder.py gen

ENC_KEY banane ke liye:
  python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
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

from pyrogram import Client, filters, enums, errors as E
from pyrogram.errors import FloodWait, MessageIdInvalid, RPCError
from pyrogram.types import Message

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
CHUNK = 100

ChatRef = Union[int, str]


def _names(*names):
    return tuple(getattr(E, n) for n in names if hasattr(E, n))


# in errors pe poora batch rok do
FATAL = _names(
    "ChannelPrivate", "ChatAdminRequired", "UserNotParticipant",
    "ChatForwardsRestricted", "ChatWriteForbidden", "ChannelInvalid",
    "PeerIdInvalid", "UserBannedInChannel",
)
# in errors ka matlab session dead hai
AUTH_DEAD = _names(
    "AuthKeyUnregistered", "AuthKeyInvalid", "SessionRevoked",
    "SessionExpired", "UserDeactivated", "UserDeactivatedBan",
)


# ── gen mode (session string banao) ──────────────────────────────────
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


# ── session storage (sqlite, optional encryption) ────────────────────
_fernet = None
if ENC_KEY:
    from cryptography.fernet import Fernet
    _fernet = Fernet(ENC_KEY.encode())
else:
    log.warning("ENC_KEY set nahi hai: session strings DB me plain text me save hongi.")

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

clients: Dict[int, Client] = {}      # uid -> running user client
_cl_lock = asyncio.Lock()
awaiting: Set[int] = set()           # users jo abhi string bhejne wale hain


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
    """Dialogs load karo taaki private channel / dst ids resolve ho sakein."""
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


# ── batch session state ──────────────────────────────────────────────
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
tasks: Set[asyncio.Task] = set()


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
        f"_/cancel bhej ke rok sakte ho_"
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


def _is_allowed(_, __, msg: Message) -> bool:
    return bool(msg.from_user) and (not ALLOWED or msg.from_user.id in ALLOWED)


def _is_awaiting(_, __, msg: Message) -> bool:
    return (
        bool(msg.from_user)
        and msg.from_user.id in awaiting
        and bool(msg.text)
        and not msg.text.startswith("/")
    )


allowed_filter = filters.create(_is_allowed)
awaiting_filter = filters.create(_is_awaiting)

_SESSION_RE = re.compile(r"^[A-Za-z0-9_\-=]{200,}$")


async def edit_progress(s: Sess, text: Optional[str] = None) -> None:
    if not s.pmsg:
        return
    try:
        await s.pmsg.edit_text(text or fmt_progress(s))
    except Exception:
        pass


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
            + (f"_{s.note}_\n" if s.note else "")
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
async def do_login(msg: Message, string: str) -> None:
    uid = msg.from_user.id
    awaiting.discard(uid)

    # string wala message turant delete karo
    try:
        await msg.delete()
    except Exception:
        pass

    string = string.strip()
    if not _SESSION_RE.match(string):
        return await bot.send_message(
            msg.chat.id, "❌ Ye valid session string nahi lag rahi. /login se dubara try karo."
        )
    if uid in sessions:
        return await bot.send_message(
            msg.chat.id, "⚠️ Batch chal raha hai. Pehle /cancel karo, phir login badlo."
        )

    wait = await bot.send_message(msg.chat.id, "🔐 Login ho raha hai, chats load ho rahi hain…")
    try:
        c = await _start_user_client(uid, string)
        me = await c.get_me()
        await _warm_cache(c)
    except AUTH_DEAD:
        return await wait.edit_text("❌ Ye session ab valid nahi hai. Nayi string banao.")
    except Exception as e:
        log.error("Login failed uid=%s: %s", uid, e)
        return await wait.edit_text(f"❌ Login fail: `{type(e).__name__}`")

    async with _cl_lock:
        old = clients.pop(uid, None)
        clients[uid] = c
    if old is not None:
        try:
            await old.stop()
        except Exception:
            pass

    db_save(uid, string)
    name = me.first_name or "User"
    uname = f" (@{me.username})" if me.username else ""
    await wait.edit_text(
        f"✅ Logged in as **{name}**{uname}\n\n"
        "Ab `/batch` use kar sakte ho. Logout ke liye /logout."
    )


# ── bot handlers ─────────────────────────────────────────────────────
@bot.on_message(filters.command("start") & filters.private & allowed_filter)
async def cmd_start(_, msg: Message):
    await msg.reply(
        "👋 **Message Forwarder**\n\n"
        "1️⃣ `/login` — apna session string do\n"
        "2️⃣ `/batch <link_range> <dest>` — forwarding shuru\n\n"
        "Other: `/status`, `/cancel`, `/me`, `/logout`\n\n"
        "**Examples:**\n"
        "`/batch https://t.me/chan/100-200 -1001234567890`\n"
        "`/batch https://t.me/c/123456/100-200 @mychannel`\n\n"
        "_Private channel ke liye tumhara account us channel me joined hona chahiye._"
    )


@bot.on_message(filters.command("login") & filters.private & allowed_filter)
async def cmd_login(_, msg: Message):
    uid = msg.from_user.id
    args = (msg.text or "").split(maxsplit=1)
    if len(args) == 2:
        return await do_login(msg, args[1])
    awaiting.add(uid)
    await msg.reply(
        "🔑 Ab apna **session string** bhejo (agla message).\n\n"
        "⚠️ Ye string tumhare account ka full access hai. Sirf wahi bot use karo jise "
        "tum trust karte ho. Main message turant delete kar dunga.\n\n"
        "_Cancel karne ke liye /cancel_"
    )


@bot.on_message(filters.private & awaiting_filter)
async def on_string(_, msg: Message):
    await do_login(msg, msg.text)


@bot.on_message(filters.command("logout") & filters.private & allowed_filter)
async def cmd_logout(_, msg: Message):
    uid = msg.from_user.id
    awaiting.discard(uid)
    s = sessions.get(uid)
    if s:
        s.alive = False
    async with _cl_lock:
        c = clients.pop(uid, None)
    if c is not None:
        try:
            await c.stop()
        except Exception:
            pass
    had = db_load(uid) is not None
    db_del(uid)
    if not (had or c):
        return await msg.reply("ℹ️ Tum logged in nahi ho.")
    await msg.reply(
        "✅ Logout ho gaya, string bot se hata di gayi.\n\n"
        "Pura revoke karne ke liye Telegram → Settings → Devices me ja ke "
        "us session ko terminate bhi kar do."
    )


@bot.on_message(filters.command("me") & filters.private & allowed_filter)
async def cmd_me(_, msg: Message):
    c = await get_client(msg.from_user.id)
    if not c:
        return await msg.reply("ℹ️ Logged in nahi ho. /login karo.")
    try:
        me = await c.get_me()
    except Exception as e:
        return await msg.reply(f"❌ `{type(e).__name__}` - /login dubara karo.")
    uname = f" (@{me.username})" if me.username else ""
    await msg.reply(f"👤 Logged in as **{me.first_name}**{uname}")


@bot.on_message(filters.command("batch") & filters.private & allowed_filter)
async def cmd_batch(_, msg: Message):
    uid = msg.from_user.id
    args = (msg.text or "").split(maxsplit=2)
    if len(args) < 3:
        return await msg.reply(
            "❌ Usage: `/batch <link_range> <dest>`\n"
            "Example: `/batch https://t.me/mychan/40756-40900 -1001234567890`"
        )

    parsed = parse_range(args[1])
    if not parsed:
        return await msg.reply(
            "❌ Invalid range.\nFormat: `https://t.me/<chan>/<start>-<end>` "
            "ya `https://t.me/c/<id>/<start>-<end>`"
        )
    src, lo, hi = parsed

    dst = parse_dst(args[2])
    if dst is None:
        return await msg.reply("❌ Invalid destination (chat id ya @username do).")

    if hi - lo + 1 > MAX_RANGE:
        return await msg.reply(f"❌ Range bahut bada hai. Max `{MAX_RANGE}` messages.")

    if uid in sessions:
        return await msg.reply("⚠️ Pehle se batch chal raha hai. /cancel karo.")
    if len(sessions) >= MAX_ACTIVE:
        return await msg.reply("⏳ Abhi server busy hai, thodi der baad try karo.")

    client = await get_client(uid)
    if client is None:
        return await msg.reply("🔑 Pehle /login karke session string do.")

    s = Sess(uid=uid, src=src, dst=dst, lo=lo, hi=hi, client=client, cur=lo - 1)
    sessions[uid] = s
    s.pmsg = await msg.reply(fmt_progress(s))

    t = asyncio.create_task(run_batch(s))
    tasks.add(t)
    t.add_done_callback(tasks.discard)
    log.info("Batch start uid=%s src=%s %d-%d dst=%s", uid, src, lo, hi, dst)


@bot.on_message(filters.command("cancel") & filters.private & allowed_filter)
async def cmd_cancel(_, msg: Message):
    uid = msg.from_user.id
    if uid in awaiting:
        awaiting.discard(uid)
        return await msg.reply("🛑 Login cancel kar diya.")
    s = sessions.get(uid)
    if not s or not s.alive:
        return await msg.reply("ℹ️ Koi active batch nahi hai.")
    s.alive = False
    await msg.reply("🛑 Cancel request mili — current message ke baad ruk jayega.")


@bot.on_message(filters.command("status") & filters.private & allowed_filter)
async def cmd_status(_, msg: Message):
    s = sessions.get(msg.from_user.id)
    if not s:
        return await msg.reply("ℹ️ Koi active batch nahi hai.")
    await msg.reply(fmt_progress(s))


# ── entry point ──────────────────────────────────────────────────────
if __name__ == "__main__":
    if not (API_ID and API_HASH and BOT_TOKEN):
        sys.exit("API_ID, API_HASH, BOT_TOKEN set karo.")
    log.info("Starting forwarder bot…")
    bot.run()
