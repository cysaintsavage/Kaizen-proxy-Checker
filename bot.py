"""
Advanced Proxy Checker Bot  |  Dev: @Sy_saint
Stack: aiogram 3 + aiohttp + aiohttp_socks
"""
import asyncio
import html
import io
import logging
import os
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import aiohttp
from aiohttp_socks import ProxyConnector
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery, InputMediaPhoto, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from banner import render_banner

# ----------------------------------------------------------------- CONFIG
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNELS = [c.strip() for c in os.getenv("CHANNELS", "").split(",") if c.strip()]  # e.g. @mychannel
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
MAX_PROXIES = int(os.getenv("MAX_PROXIES", "3000"))
DEV = "Sy_saint"
HEADERS_URL = "http://httpbin.org/headers"
GEO_URL = "http://ip-api.com/json/?fields=status,country,countryCode,isp,query"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("proxybot")
router = Router()
JOBS: dict[int, asyncio.Event] = {}


# --------------------------------------------------------------------- DB
db = sqlite3.connect("bot.db", check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, name TEXT, joined INTEGER,
    checks INTEGER DEFAULT 0, tested INTEGER DEFAULT 0, alive INTEGER DEFAULT 0,
    timeout INTEGER DEFAULT 10, threads INTEGER DEFAULT 100, geo INTEGER DEFAULT 1,
    banner INTEGER DEFAULT 1)""")
try:
    db.execute("ALTER TABLE users ADD COLUMN banner INTEGER DEFAULT 1")
except sqlite3.OperationalError:
    pass
db.commit()


@dataclass
class Settings:
    timeout: int = 10
    threads: int = 100
    geo: bool = True
    banner: bool = True


def upsert_user(uid: int, name: str):
    db.execute("INSERT OR IGNORE INTO users(id,name,joined) VALUES(?,?,?)", (uid, name, int(time.time())))
    db.commit()


def get_settings(uid: int) -> Settings:
    r = db.execute("SELECT timeout,threads,geo,banner FROM users WHERE id=?", (uid,)).fetchone()
    return Settings(r[0], r[1], bool(r[2]), bool(r[3])) if r else Settings()


def save_setting(uid: int, col: str, val: int):
    assert col in ("timeout", "threads", "geo", "banner")
    db.execute(f"UPDATE users SET {col}=? WHERE id=?", (val, uid))
    db.commit()


def add_stats(uid: int, tested: int, alive: int):
    db.execute("UPDATE users SET checks=checks+1, tested=tested+?, alive=alive+? WHERE id=?", (tested, alive, uid))
    db.commit()


# ------------------------------------------------------------ FSM / KEYBOARDS
class Form(StatesGroup):
    waiting = State()


def main_kb():
    b = InlineKeyboardBuilder()
    b.button(text="⚡ Check Proxies", callback_data="check")
    b.button(text="⚙️ Settings", callback_data="settings")
    b.button(text="📊 My Stats", callback_data="stats")
    b.button(text="📖 Help", callback_data="help")
    if CHANNELS:
        b.button(text="📢 Channel", url=f"https://t.me/{CHANNELS[0].lstrip('@')}")
    b.button(text="👨‍💻 Developer", url=f"https://t.me/{DEV}")
    b.adjust(1, 2, 2, 1) if CHANNELS else b.adjust(1, 2, 1, 1)
    return b.as_markup()


def back_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🏠 Main Menu", callback_data="menu")
    return b.as_markup()


def type_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🌐 HTTP/HTTPS", callback_data="type:http")
    b.button(text="🧦 SOCKS4", callback_data="type:socks4")
    b.button(text="🧦 SOCKS5", callback_data="type:socks5")
    b.button(text="🤖 Auto-Detect (All)", callback_data="type:auto")
    b.button(text="🏠 Back", callback_data="menu")
    b.adjust(1, 2, 1, 1)
    return b.as_markup()


def settings_kb(s: Settings):
    b = InlineKeyboardBuilder()
    b.button(text=f"⏱ Timeout: {s.timeout}s", callback_data="set:timeout")
    b.button(text=f"🧵 Threads: {s.threads}", callback_data="set:threads")
    b.button(text=f"🌍 Geo Lookup: {'ON ✅' if s.geo else 'OFF ❌'}", callback_data="set:geo")
    b.button(text=f"🖼 Scan Banner: {'ON ✅' if s.banner else 'OFF ❌'}", callback_data="set:banner")
    b.button(text="🏠 Back", callback_data="menu")
    b.adjust(1)
    return b.as_markup()


def footer() -> str:
    return f"\n\n<i>Dev: @{DEV}</i>"


async def safe_edit(msg: Message, text: str, kb=None):
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


# --------------------------------------------------------------- FORCE JOIN
async def not_joined(bot: Bot, uid: int) -> list[str]:
    missing = []
    for ch in CHANNELS:
        try:
            m = await bot.get_chat_member(ch, uid)
            if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                missing.append(ch)
        except Exception as e:  # bot not admin / wrong channel
            log.warning("Force-join check failed for %s: %s", ch, e)
    return missing


def join_kb(missing: list[str]):
    b = InlineKeyboardBuilder()
    for ch in missing:
        b.button(text=f"📢 Join {ch}", url=f"https://t.me/{ch.lstrip('@')}")
    b.button(text="✅ I've Joined", callback_data="check_sub")
    b.adjust(1)
    return b.as_markup()


JOIN_TEXT = "🔒 <b>Access Locked</b>\n\nJoin the channel(s) below to use this bot, then tap <b>I've Joined</b>."


class ForceJoin(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if not CHANNELS or not user or user.id in ADMIN_IDS:
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)
        missing = await not_joined(data["bot"], user.id)
        if not missing:
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer("Join the channel first!", show_alert=True)
            await event.message.answer(JOIN_TEXT, reply_markup=join_kb(missing))
        else:
            await event.answer(JOIN_TEXT, reply_markup=join_kb(missing))


# ------------------------------------------------------------ PROXY PARSING
PROXY_RE = re.compile(
    r"^(?:(?P<scheme>https?|socks4|socks5)://)?"
    r"(?:(?P<user>[^:@\s]+):(?P<pw>[^@\s]+)@)?"
    r"(?P<host>[A-Za-z0-9.\-]+):(?P<port>\d{2,5})"
    r"(?::(?P<u2>[^:\s]+):(?P<p2>[^:\s]+))?$"
)


@dataclass
class Proxy:
    host: str
    port: int
    user: Optional[str] = None
    pw: Optional[str] = None
    scheme: Optional[str] = None

    @property
    def raw(self) -> str:
        auth = f"{self.user}:{self.pw}@" if self.user else ""
        return f"{auth}{self.host}:{self.port}"


def parse_proxies(text: str) -> list[Proxy]:
    seen, out = set(), []
    for line in text.replace(",", "\n").splitlines():
        m = PROXY_RE.match(line.strip())
        if not m:
            continue
        port = int(m["port"])
        if not 1 <= port <= 65535:
            continue
        scheme = m["scheme"]
        if scheme == "https":
            scheme = "http"
        p = Proxy(m["host"], port, m["user"] or m["u2"], m["pw"] or m["p2"], scheme)
        key = (p.raw, p.scheme)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


# ------------------------------------------------------------------ CHECKER
@dataclass
class Result:
    proxy: Proxy
    ptype: str
    latency: int
    anon: str
    country: str = "??"
    isp: str = "-"


async def probe(p: Proxy, ptype: str, s: Settings, real_ip: str) -> Optional[Result]:
    auth = f"{quote(p.user)}:{quote(p.pw)}@" if p.user else ""
    url = f"{ptype}://{auth}{p.host}:{p.port}"
    timeout = aiohttp.ClientTimeout(total=s.timeout)
    kw = {}
    if ptype == "http":
        sess = aiohttp.ClientSession(timeout=timeout)
        kw["proxy"] = url
    else:
        sess = aiohttp.ClientSession(connector=ProxyConnector.from_url(url), timeout=timeout)
    async with sess:
        t0 = time.perf_counter()
        async with sess.get(HEADERS_URL, **kw) as r:
            if r.status != 200:
                return None
            body = await r.text()
        latency = int((time.perf_counter() - t0) * 1000)
        low = body.lower()
        if real_ip and real_ip in body:
            anon = "Transparent"
        elif any(h in low for h in ("x-forwarded-for", "\"via\"", "x-real-ip", "forwarded")):
            anon = "Anonymous"
        else:
            anon = "Elite"
        res = Result(p, ptype, latency, anon)
        if s.geo:
            try:
                async with sess.get(GEO_URL, **kw) as g:
                    j = await g.json(content_type=None)
                    if j.get("status") == "success":
                        res.country = j.get("countryCode", "??")
                        res.isp = j.get("isp", "-")
            except Exception:
                pass
        return res


async def check_one(p: Proxy, mode: str, s: Settings, real_ip: str) -> Optional[Result]:
    types = [p.scheme] if (mode == "auto" and p.scheme) else (["http", "socks5", "socks4"] if mode == "auto" else [mode])
    for t in types:
        try:
            r = await probe(p, t, s, real_ip)
            if r:
                return r
        except Exception:
            continue
    return None


async def get_real_ip() -> str:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get("https://api.ipify.org") as r:
                return (await r.text()).strip()
    except Exception:
        return ""


def bar(done: int, total: int) -> str:
    n = int(10 * done / total) if total else 0
    return "█" * n + "░" * (10 - n)


def stop_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🛑 Stop", callback_data="stop")
    return b.as_markup()


def result_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🔁 Check Again", callback_data="check")
    b.button(text="🏠 Main Menu", callback_data="menu")
    b.button(text="👨‍💻 Developer", url=f"https://t.me/{DEV}")
    b.adjust(2, 1)
    return b.as_markup()


def mask_host(h: str) -> str:
    parts = h.split(".")
    if len(parts) == 4 and all(x.isdigit() for x in parts):
        return f"{parts[0]}.{parts[1]}.xx.xx"
    return (h[:8] + "...") if len(h) > 8 else h


async def run_job(msg: Message, uid: int, proxies: list[Proxy], mode: str):
    s = get_settings(uid)
    stop = asyncio.Event()
    real_ip = await get_real_ip()
    sem = asyncio.Semaphore(s.threads)
    results: list[Result] = []
    state = {"done": 0, "frame": 0, "current": "initializing"}
    total = len(proxies)
    started = time.time()

    def snapshot(final: bool = False) -> dict:
        live = len(results)
        done = state["done"]
        elapsed = time.time() - started
        return dict(
            mode=mode, threads=s.threads, timeout=s.timeout, total=total, done=done,
            live=live, dead=done - live,
            leaked=sum(1 for r in results if r.anon == "Transparent"),
            public=sum(1 for r in results if not r.proxy.user),
            private=sum(1 for r in results if r.proxy.user),
            elite=sum(1 for r in results if r.anon == "Elite"),
            avg=int(sum(r.latency for r in results) / live) if live else 0,
            elapsed=elapsed, speed=(done / elapsed) if elapsed > 0 else 0.0,
            current=state["current"], frame=state["frame"], final=final,
            stopped=stop.is_set(), dev=DEV)

    async def push(status: Message, final: bool = False, kb=None):
        snap = snapshot(final)
        if s.banner:
            img = await asyncio.to_thread(render_banner, snap)
            if final:
                cap = "🛑 <b>Scan stopped</b>" if snap["stopped"] else "✅ <b>Scan complete</b>"
            else:
                cap = f"🛰 <b>Scanning…</b> {snap['done']}/{total}"
            await status.edit_media(
                InputMediaPhoto(media=BufferedInputFile(img, "scan.png"), caption=cap + footer()),
                reply_markup=kb)
        else:
            d = snap["done"]
            await status.edit_text(
                f"🔍 <b>Checking…</b> [{mode.upper()}]\n\n"
                f"<code>{bar(d, total)}</code> {d}/{total}\n"
                f"🟢 Live: <b>{snap['live']}</b>   🔴 Dead: <b>{snap['dead']}</b>\n"
                f"⚠️ Leaked: <b>{snap['leaked']}</b>\n"
                f"🌐 Public: <b>{snap['public']}</b>   🔒 Private: <b>{snap['private']}</b>\n"
                f"⏱ {int(snap['elapsed'])}s" + footer(), reply_markup=kb)

    if s.banner:
        img = await asyncio.to_thread(render_banner, snapshot())
        status = await msg.answer_photo(BufferedInputFile(img, "scan.png"),
                                        caption="🛰 <b>Scanning…</b>" + footer(), reply_markup=stop_kb())
    else:
        status = await msg.answer("⏳ Starting…", reply_markup=stop_kb())
    JOBS[uid] = stop

    async def worker(p: Proxy):
        async with sem:
            if stop.is_set():
                return
            state["current"] = f"{mask_host(p.host)}:{p.port}"
            r = await check_one(p, mode, s, real_ip)
            state["done"] += 1
            if r:
                results.append(r)

    async def progress():
        while True:
            await asyncio.sleep(3)
            state["frame"] += 1
            try:
                await push(status, kb=stop_kb())
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramBadRequest:
                pass
            except Exception:
                log.exception("progress update failed")

    ptask = asyncio.create_task(progress())
    try:
        await asyncio.gather(*(worker(p) for p in proxies))
    finally:
        ptask.cancel()
        JOBS.pop(uid, None)

    d = state["done"]
    alive = len(results)
    add_stats(uid, d, alive)
    results.sort(key=lambda r: r.latency)
    stopped = stop.is_set()
    snap = snapshot(True)

    types = Counter(r.ptype.upper() for r in results)
    anons = Counter(r.anon for r in results)
    countries = Counter(r.country for r in results).most_common(5)

    lines = [f"{'🛑 <b>Stopped</b>' if stopped else '✅ <b>Check Complete</b>'}\n",
             f"📦 Tested: <b>{d}/{total}</b>",
             f"🟢 Live: <b>{alive}</b>   🔴 Dead: <b>{d - alive}</b>",
             f"⚠️ Leaked: <b>{snap['leaked']}</b>   💎 Elite: <b>{snap['elite']}</b>",
             f"🌐 Public: <b>{snap['public']}</b>   🔒 Private: <b>{snap['private']}</b>",
             f"📈 Live rate: <b>{(alive / d * 100) if d else 0:.1f}%</b>",
             f"⚡ Avg ping: <b>{snap['avg']} ms</b>   ⏱ Time: <b>{snap['elapsed']:.1f}s</b>"]
    if alive:
        lines.append("\n<b>Types:</b> " + " | ".join(f"{k}: {v}" for k, v in types.items()))
        lines.append("<b>Anonymity:</b> " + " | ".join(f"{k}: {v}" for k, v in anons.items()))
        if s.geo:
            lines.append("<b>Top countries:</b> " + ", ".join(f"{c} ({n})" for c, n in countries))
        lines.append("\n🏆 <b>Fastest:</b>")
        for r in results[:3]:
            lines.append(f"• <code>{html.escape(r.proxy.raw)}</code> — {r.latency}ms [{r.ptype.upper()}]")
    summary = "\n".join(lines) + footer()

    if s.banner:
        try:
            await push(status, final=True, kb=None)
        except (TelegramBadRequest, TelegramRetryAfter):
            pass
        await msg.answer(summary, reply_markup=result_kb())
    else:
        await safe_edit(status, summary, result_kb())

    if alive:
        plain = "\n".join(r.proxy.raw for r in results)
        detail = "\n".join(
            f"{r.ptype}://{r.proxy.raw} | {r.latency}ms | {r.anon} | "
            f"{'PRIVATE' if r.proxy.user else 'PUBLIC'} | "
            f"{'LEAKED' if r.anon == 'Transparent' else 'CLEAN'} | {r.country} | {r.isp}" for r in results)
        await msg.answer_document(BufferedInputFile(plain.encode(), "working.txt"),
                                  caption="✅ Live proxies (fastest first)")
        elite = [r for r in results if r.anon == "Elite"]
        if elite:
            await msg.answer_document(BufferedInputFile("\n".join(r.proxy.raw for r in elite).encode(), "elite.txt"),
                                      caption="💎 Elite (non-leaking) proxies")
        await msg.answer_document(BufferedInputFile(detail.encode(), "detailed_report.txt"),
                                  caption=f"📄 Detailed report\n<i>Dev: @{DEV}</i>")


# ----------------------------------------------------------------- HANDLERS
WELCOME = ("👋 <b>Welcome, {name}!</b>\n\n"
           "I'm an <b>Advanced Proxy Checker</b>.\n"
           "Supports HTTP/HTTPS, SOCKS4, SOCKS5 with latency, anonymity level & geo info.\n\n"
           "Choose an option below 👇")


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    upsert_user(m.from_user.id, m.from_user.full_name)
    await m.answer(WELCOME.format(name=html.escape(m.from_user.first_name)) + footer(), reply_markup=main_kb())


@router.callback_query(F.data == "check_sub")
async def check_sub(c: CallbackQuery):
    missing = await not_joined(c.bot, c.from_user.id)
    if missing:
        return await c.answer("❌ You haven't joined all channels yet!", show_alert=True)
    upsert_user(c.from_user.id, c.from_user.full_name)
    await c.answer("✅ Verified!")
    await safe_edit(c.message, WELCOME.format(name=html.escape(c.from_user.first_name)) + footer(), main_kb())


@router.callback_query(F.data == "menu")
async def menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c.message, WELCOME.format(name=html.escape(c.from_user.first_name)) + footer(), main_kb())
    await c.answer()


@router.callback_query(F.data == "check")
async def check_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c.message, "🔎 <b>Select proxy type to check:</b>", type_kb())
    await c.answer()


@router.callback_query(F.data.startswith("type:"))
async def pick_type(c: CallbackQuery, state: FSMContext):
    mode = c.data.split(":")[1]
    await state.set_state(Form.waiting)
    await state.update_data(mode=mode)
    await safe_edit(
        c.message,
        f"📥 <b>Mode: {mode.upper()}</b>\n\nSend your proxies as <b>text</b> or upload a <b>.txt</b> file.\n"
        f"Max <b>{MAX_PROXIES}</b> per check.\n\n<b>Formats:</b>\n"
        "<code>ip:port</code>\n<code>user:pass@ip:port</code>\n<code>ip:port:user:pass</code>\n"
        "<code>socks5://ip:port</code>", back_kb())
    await c.answer()


@router.message(Form.waiting)
async def receive(m: Message, state: FSMContext, bot: Bot):
    uid = m.from_user.id
    if uid in JOBS:
        return await m.answer("⚠️ You already have a check running. Stop it first.")
    text = m.text or ""
    if m.document:
        if m.document.file_size and m.document.file_size > 2_000_000:
            return await m.answer("❌ File too large (max 2MB).")
        buf = io.BytesIO()
        await bot.download(m.document, destination=buf)
        text = buf.getvalue().decode("utf-8", errors="ignore")
    proxies = parse_proxies(text)
    if not proxies:
        return await m.answer("❌ No valid proxies found. Check the format and try again.", reply_markup=back_kb())
    if len(proxies) >MAX_PROXIES:
        proxies = proxies[:MAX_PROXIES]
        await m.answer(f"⚠️ Limited to the first {MAX_PROXIES} proxies.")
    mode = (await state.get_data())["mode"]
    await state.clear()
    asyncio.create_task(run_job(m, uid, proxies, mode))


@router.callback_query(F.data == "stop")
async def stop_job(c: CallbackQuery):
    ev = JOBS.get(c.from_user.id)
    if ev:
        ev.set()
        await c.answer("🛑 Stopping…")
    else:
        await c.answer("No active check.")


@router.callback_query(F.data == "settings")
async def settings(c: CallbackQuery):
    await safe_edit(c.message, "⚙️ <b>Settings</b>\nTap to change a value." + footer(), settings_kb(get_settings(c.from_user.id)))
    await c.answer()


@router.callback_query(F.data.startswith("set:"))
async def change_setting(c: CallbackQuery):
    key = c.data.split(":")[1]
    s = get_settings(c.from_user.id)
    if key == "timeout":
        opts = [5, 10, 15, 20, 30]
        save_setting(c.from_user.id, "timeout", opts[(opts.index(s.timeout) + 1) % len(opts)] if s.timeout in opts else 10)
    elif key == "threads":
        opts = [50, 100, 200, 300, 500]
        save_setting(c.from_user.id, "threads", opts[(opts.index(s.threads) + 1) % len(opts)] if s.threads in opts else 100)
    elif key == "geo":
        save_setting(c.from_user.id, "geo", int(not s.geo))
    elif key == "banner":
        save_setting(c.from_user.id, "banner", int(not s.banner))
    await safe_edit(c.message, "⚙️ <b>Settings</b>\nTap to change a value." + footer(), settings_kb(get_settings(c.from_user.id)))
    await c.answer("Saved ✅")


@router.callback_query(F.data == "stats")
async def stats(c: CallbackQuery):
    r = db.execute("SELECT checks,tested,alive FROM users WHERE id=?", (c.from_user.id,)).fetchone() or (0, 0, 0)
    rate = (r[2] / r[1] * 100) if r[1] else 0
    await safe_edit(c.message, f"📊 <b>Your Stats</b>\n\n🔁 Checks run: <b>{r[0]}</b>\n📦 Proxies tested: <b>{r[1]}</b>\n"
                               f"🟢 Alive found: <b>{r[2]}</b>\n📈 Success rate: <b>{rate:.1f}%</b>" + footer(), back_kb())
    await c.answer()


@router.callback_query(F.data == "help")
async def help_(c: CallbackQuery):
    await safe_edit(
        c.message,
        "📖 <b>Help</b>\n\n1️⃣ Tap <b>Check Proxies</b>\n2️⃣ Pick a type (or Auto-Detect)\n3️⃣ Send proxies or a .txt file\n"
        "4️⃣ Get results + downloadable lists\n\n<b>Anonymity levels</b>\n"
        "• <b>Elite</b> – hides your IP & proxy use\n• <b>Anonymous</b> – hides IP, reveals proxy\n"
        "• <b>Transparent</b> – leaks your real IP\n\n"
        f"Need help? Contact @{DEV}", back_kb())
    await c.answer()


# -------------------------------------------------------------------- ADMIN
@router.message(Command("stats"))
async def admin_stats(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    u, c, t, a = db.execute("SELECT COUNT(*),SUM(checks),SUM(tested),SUM(alive) FROM users").fetchone()
    await m.answer(f"👑 <b>Admin Stats</b>\n\nUsers: {u}\nChecks: {c or 0}\nTested: {t or 0}\nAlive: {a or 0}")


@router.message(Command("broadcast"))
async def broadcast(m: Message, bot: Bot):
    if m.from_user.id not in ADMIN_IDS:
        return
    text = (m.text or "").partition(" ")[2].strip()
    if not text:
        return await m.answer("Usage: /broadcast your message")
    ok = fail = 0
    for (uid,) in db.execute("SELECT id FROM users").fetchall():
        try:
            await bot.send_message(uid, text)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await m.answer(f"📣 Sent: {ok} | Failed: {fail}")


# --------------------------------------------------------------------- MAIN
async def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN in .env")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(ForceJoin())
    dp.callback_query.outer_middleware(ForceJoin())
    dp.include_router(router)
    log.info("Bot started | Dev: @%s", DEV)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
