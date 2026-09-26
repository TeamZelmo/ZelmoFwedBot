"""
Telegram Message Forwarder Bot - Pyrogram
Multi-user, production-grade, obfuscated
"""
import os, re, asyncio, logging, base64, hashlib, time
from typing import Dict, Optional, Tuple
from dataclasses import dataclass, field
from pyrogram import Client, filters, enums
from pyrogram.types import (Message, InlineKeyboardMarkup,
                            InlineKeyboardButton)
from pyrogram.errors import (FloodWait, MessageIdInvalid,
                              ChannelPrivate, ChatAdminRequired,
                              UserNotParticipant, RPCError)

# ── logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_L = logging.getLogger("fwd")

# ── config (env-driven) ──────────────────────────────────────────────
_E = os.environ.get
_ζ = lambda k, d=None: _E(k, d)          # env fetcher alias
_API_ID   = int(_ζ("API_ID",  "0"))
_API_HASH = _ζ("API_HASH",  "")
_BOT_TOK  = _ζ("BOT_TOKEN", "")
_OWNER    = int(_ζ("OWNER_ID", "0"))     # optional: restrict to owner
_DELAY    = float(_ζ("MSG_DELAY", "0.8"))  # seconds between forwards
_MAX_RTRY = int(_ζ("MAX_RETRY", "3"))

# ── internal state ───────────────────────────────────────────────────
@dataclass
class _Sess:
    """Per-user batch session."""
    uid:   int
    src:   str          # source chat username or id
    dst:   int          # destination chat id
    lo:    int          # start msg-id
    hi:    int          # end   msg-id
    cur:   int  = 0     # current progress
    done:  int  = 0     # successfully forwarded
    skip:  int  = 0     # skipped (deleted/unavailable)
    err:   int  = 0     # errors
    alive: bool = True  # cancel flag
    pmsg:  Optional[Message] = None   # progress msg
    _t0:   float = field(default_factory=time.time)

    @property
    def total(self) -> int:
        return self.hi - self.lo + 1

    @property
    def pct(self) -> float:
        done = self.cur - self.lo
        return (done / self.total * 100) if self.total else 0

    @property
    def eta(self) -> str:
        elapsed = time.time() - self._t0
        done = self.cur - self.lo
        if done <= 0:
            return "—"
        rate = done / elapsed
        rem  = (self.hi - self.cur) / rate if rate else 0
        m, s = divmod(int(rem), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

_sessions: Dict[int, _Sess] = {}   # uid → session

# ── helpers ──────────────────────────────────────────────────────────
_PBARS = "▓░"

def _bar(pct: float, w: int = 16) -> str:
    f = int(pct / 100 * w)
    return _PBARS[0] * f + _PBARS[1] * (w - f)

def _fmt_prog(s: _Sess) -> str:
    bar = _bar(s.pct)
    return (
        f"**📦 Batch Forward**\n"
        f"`{bar}` {s.pct:.1f}%\n\n"
        f"• Forwarded : `{s.done}`\n"
        f"• Skipped   : `{s.skip}`\n"
        f"• Errors    : `{s.err}`\n"
        f"• Progress  : `{s.cur - s.lo}/{s.total}`\n"
        f"• ETA       : `{s.eta}`\n\n"
        f"_Send /cancel to stop_"
    )

def _parse_link(raw: str) -> Optional[Tuple[str, int]]:
    """
    Parse a t.me link and return (chat_ref, msg_id).
    Supports:
      https://t.me/username/123
      https://t.me/c/1234567890/123
    """
    raw = raw.strip()
    m = re.match(
        r"https?://t(?:elegram)?\.me/(?:c/(\d+)|([a-zA-Z]\w{3,}?))/(\d+)",
        raw
    )
    if not m:
        return None
    priv_id, uname, mid = m.groups()
    chat = int(f"-100{priv_id}") if priv_id else uname
    return chat, int(mid)

def _parse_batch(text: str) -> Optional[Tuple[str, int, int]]:
    """
    Parse: <link_start>-<end_id>
    e.g.  https://t.me/c/123/40756-40900
    Returns (chat_ref, start_id, end_id) or None.
    """
    m = re.match(
        r"(https?://t(?:elegram)?\.me/[^\s]+?/(\d+))-(\d+)\s*$",
        text.strip()
    )
    if not m:
        return None
    full_link, start_str, end_str = m.groups()
    parsed = _parse_link(full_link)
    if not parsed:
        return None
    chat, start = parsed
    end = int(end_str)
    if end < start:
        return None
    return chat, start, end

# obfuscated media-type dispatcher
_ξ_COPY_TYPES = frozenset({
    "text", "photo", "video", "document", "audio",
    "voice", "video_note", "sticker", "animation",
    "poll", "contact", "venue", "location",
})

async def _ψ_fwd_one(
    app: Client,
    src_chat,
    dst_chat: int,
    mid: int,
) -> str:
    """
    Forward a single message, preserving all media types.
    Returns status: 'ok' | 'skip' | 'err'
    """
    for attempt in range(1, _MAX_RTRY + 1):
        try:
            msg: Message = await app.get_messages(src_chat, mid)

            # deleted / empty
            if msg is None or msg.empty:
                return "skip"

            # try copy_message first (fastest, handles all types)
            await app.copy_message(
                chat_id=dst_chat,
                from_chat_id=src_chat,
                message_id=mid,
                disable_notification=True,
            )
            return "ok"

        except FloodWait as fw:
            _L.warning("FloodWait %ds on msg %d", fw.value, mid)
            await asyncio.sleep(fw.value + 1)
            # don't count as attempt
            continue

        except MessageIdInvalid:
            return "skip"

        except (ChannelPrivate, ChatAdminRequired, UserNotParticipant) as e:
            _L.error("Access error on %s: %s", src_chat, e)
            return "err"

        except RPCError as e:
            _L.warning("RPC %s (attempt %d/%d) on msg %d",
                       e, attempt, _MAX_RTRY, mid)
            if attempt < _MAX_RTRY:
                await asyncio.sleep(2 ** attempt)
            else:
                return "err"

        except Exception as e:
            _L.exception("Unexpected error msg %d: %s", mid, e)
            return "err"

    return "err"

async def _ψ_run_batch(app: Client, sess: _Sess) -> None:
    """Core batch worker — runs in background task."""
    uid  = sess.uid
    prog_update_every = max(1, (sess.total) // 50)   # update progress ~50×

    for mid in range(sess.lo, sess.hi + 1):
        if not sess.alive:
            break
        sess.cur = mid
        status = await _ψ_fwd_one(app, sess.src, sess.dst, mid)

        if status == "ok":
            sess.done += 1
        elif status == "skip":
            sess.skip += 1
        else:
            sess.err += 1

        # throttle
        await asyncio.sleep(_DELAY)

        # update progress message periodically
        step = mid - sess.lo + 1
        if step % prog_update_every == 0 or mid == sess.hi:
            if sess.pmsg:
                try:
                    await sess.pmsg.edit_text(
                        _fmt_prog(sess),
                        parse_mode=enums.ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass

    # ── final summary ────────────────────────────────────────────────
    reason = "✅ Completed" if sess.alive else "🛑 Cancelled"
    summary = (
        f"{reason}\n\n"
        f"**Batch**: `{sess.src}` → `{sess.dst}`\n"
        f"**Range**: `{sess.lo}` – `{sess.hi}` ({sess.total} msgs)\n\n"
        f"• Forwarded : `{sess.done}`\n"
        f"• Skipped   : `{sess.skip}`\n"
        f"• Errors    : `{sess.err}`\n"
        f"• Time      : `{int(time.time()-sess._t0)}s`"
    )
    if sess.pmsg:
        try:
            await sess.pmsg.edit_text(
                summary, parse_mode=enums.ParseMode.MARKDOWN
            )
        except Exception:
            pass

    _sessions.pop(uid, None)
    _L.info("Batch done uid=%d done=%d skip=%d err=%d",
            uid, sess.done, sess.skip, sess.err)

# ── bot handlers ─────────────────────────────────────────────────────
app = Client(
    "fwd_bot",
    api_id=_API_ID,
    api_hash=_API_HASH,
    bot_token=_BOT_TOK,
)

# owner-only guard (optional; set OWNER_ID=0 to disable)
def _owner_only(_, __, msg: Message) -> bool:
    return _OWNER == 0 or msg.from_user.id == _OWNER

_OwnerFilter = filters.create(_owner_only)


@app.on_message(filters.command("start") & filters.private)
async def _cmd_start(_, msg: Message):
    uid = msg.from_user.id
    txt = (
        "👋 **Message Forwarder Bot**\n\n"
        "**Commands:**\n"
        "• `/batch <link_range> <dest>` — start forwarding\n"
        "• `/cancel` — stop current batch\n"
        "• `/status` — check running batch\n\n"
        "**Batch syntax:**\n"
        "`/batch https://t.me/chan/100-200 -1001234567890`\n"
        "`/batch https://t.me/c/123456/100-200 me`\n\n"
        "_Supports all media types: text, photo, video, document,_\n"
        "_voice, sticker, animation, poll, contact, venue, etc._"
    )
    await msg.reply(txt, parse_mode=enums.ParseMode.MARKDOWN)


@app.on_message(filters.command("batch") & filters.private)
async def _cmd_batch(_, msg: Message):
    uid  = msg.from_user.id
    args = msg.text.split(maxsplit=2)

    if len(args) < 3:
        return await msg.reply(
            "❌ Usage: `/batch <link_range> <dest_chat_id>`\n\n"
            "Example:\n"
            "`/batch https://t.me/mychan/40756-40900 -1001234567890`",
            parse_mode=enums.ParseMode.MARKDOWN,
        )

    raw_range = args[1].strip()
    raw_dst   = args[2].strip()

    # parse source range
    parsed = _parse_batch(raw_range)
    if not parsed:
        return await msg.reply(
            "❌ Invalid range link.\n\n"
            "Format: `https://t.me/<chan>/<start>-<end>`",
            parse_mode=enums.ParseMode.MARKDOWN,
        )

    src_chat, lo, hi = parsed

    # parse destination
    try:
        if raw_dst.lstrip("-").isdigit():
            dst_chat = int(raw_dst)
        else:
            dst_chat = raw_dst   # username like "me" or "@chan"
    except Exception:
        return await msg.reply("❌ Invalid destination chat ID.")

    # conflict check
    if uid in _sessions and _sessions[uid].alive:
        return await msg.reply(
            "⚠️ You already have a running batch.\n"
            "Use /cancel to stop it first."
        )

    # create session
    sess = _Sess(uid=uid, src=src_chat, dst=dst_chat, lo=lo, hi=hi, cur=lo)
    _sessions[uid] = sess

    pm = await msg.reply(
        _fmt_prog(sess), parse_mode=enums.ParseMode.MARKDOWN
    )
    sess.pmsg = pm

    # fire background task
    asyncio.create_task(_ψ_run_batch(app, sess))
    _L.info("Batch started uid=%d src=%s lo=%d hi=%d dst=%s",
            uid, src_chat, lo, hi, dst_chat)


@app.on_message(filters.command("cancel") & filters.private)
async def _cmd_cancel(_, msg: Message):
    uid = msg.from_user.id
    sess = _sessions.get(uid)
    if not sess or not sess.alive:
        return await msg.reply("ℹ️ No active batch to cancel.")
    sess.alive = False
    await msg.reply("🛑 Cancellation requested — stopping after current message…")


@app.on_message(filters.command("status") & filters.private)
async def _cmd_status(_, msg: Message):
    uid  = msg.from_user.id
    sess = _sessions.get(uid)
    if not sess or not sess.alive:
        return await msg.reply("ℹ️ No active batch.")
    await msg.reply(_fmt_prog(sess), parse_mode=enums.ParseMode.MARKDOWN)


# ── entry point ───────────────────────────────────────────────────────
if __name__ == "__main__":
    _L.info("Starting forwarder bot…")
    app.run()
