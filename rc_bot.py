#!/usr/bin/env python3
"""
RC → Owner Bulk Lookup Telegram Bot  (SELF-CONTAINED, for Railway / VPS)
=======================================================================
No web app, no MongoDB needed. Everything (API config, admins, user RC
limits, forward channel) is stored in a local SQLite file (rc_bot.db).

WHAT IT DOES
  Client:  /start  ->  send vehicle series (e.g. WB01AB4049)  ->  send how
  many numbers you need (e.g. 100).  The bot scans the series forward (no
  skipping), pulls each vehicle's mobile from your API, and returns a CSV
  (vehicle_number, mobile_number) + a preview.

ADMIN (whoever is in SUPER_ADMINS, or first person to /admin)
  /apilist                      list APIs
  /setapi                       add + activate a new API (asks name, url, paths)
  /activate <no>                switch active API
  /readjson <vehicle>           see the API's raw JSON (to learn its format)
  /setlimit <chat_id> <limit>   unlock RC for a user
  /userinfo <chat_id>           a user's RC usage
  /setdefault <limit>           default RC limit auto-given to new users
  /setchannel <channel_id>      forward every result file to a channel (off=disable)
  /addadmin <id>  /deladmin <id>
  /myid                         your chat id & RC balance

RUN
  pip install -r requirements.txt
  set env vars:
     TELEGRAM_BOT_TOKEN   (required, from @BotFather)
     SUPER_ADMINS         (optional, comma chat ids; default 8922842091)
     RELAY_PREFIX         (optional; default https://r.jina.ai/ to bypass
                           datacenter IP blocks. Set to empty if your IP is
                           allowed by the API -> much faster.)
     TRANSPORT            (optional; auto (default) = try DIRECT first, if the API
                           blocks this server -> proxy -> relay. Or force direct/relay/proxy)
     MAX_WORKERS          (optional; parallel requests. Default 25)
     PROGRESS_EVERY       (optional; live count every N seconds, default 10)
  User: /exit (or STOP button) stops a running scan and sends found numbers.
     JINA_API_KEY         (optional; higher relay rate limit for big bulk)
     RC_DB                (optional; sqlite path, default rc_bot.db)
  python rc_bot.py
"""

import csv
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone

import requests

# ----------------------------- Config ----------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "9010080670:AAFMS2pHpKFWKaW3hYoefBcTxFObzNtJjro").strip()
SUPER_ADMINS = os.environ.get("SUPER_ADMINS", "8922842091")
RELAY_PREFIX = os.environ.get("RELAY_PREFIX", "https://r.jina.ai/").strip()
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "25"))
TRANSPORT = os.environ.get("TRANSPORT", "auto").strip().lower()  # auto | direct | relay | proxy
CONNECT_TIMEOUT = float(os.environ.get("CONNECT_TIMEOUT", "6"))
READ_TIMEOUT = float(os.environ.get("READ_TIMEOUT", "20"))
RETRY_ROUNDS = int(os.environ.get("RETRY_ROUNDS", "12"))
PROGRESS_EVERY = int(os.environ.get("PROGRESS_EVERY", "10"))
SWITCH_AFTER = int(os.environ.get("SWITCH_AFTER", "6"))  # consecutive failures before changing route mid-scan
REPORT_HOUR = int(os.environ.get("REPORT_HOUR", "22"))  # daily admin report, India time (IST)
JINA_API_KEY = os.environ.get("JINA_API_KEY", "").strip()
RC_DB = os.environ.get("RC_DB", "rc_bot.db")
RC_MAX_SCAN = int(os.environ.get("RC_MAX_SCAN", "1000000"))

# Proxy pool: PROXY_LIST (comma/newline separated) or single PROXY_URL.
# If any proxy is set, we go DIRECT through a rotating proxy (relay is skipped) —
# this is the fastest path and bypasses IP/SNI blocks.
import random

PROXY_LIST = [p.strip() for p in re.split(r"[,\n]", os.environ.get("PROXY_LIST", "")) if p.strip()]
_single_proxy = os.environ.get("PROXY_URL", "").strip()
if _single_proxy and _single_proxy not in PROXY_LIST:
    PROXY_LIST.append(_single_proxy)
USE_PROXY = len(PROXY_LIST) > 0  # env-only flag at boot; live check uses bool(PROXY_LIST)

TG = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

SESSION = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=200, pool_maxsize=200, max_retries=0)  # lookup() does its own retries
SESSION.mount("http://", _adapter)
SESSION.mount("https://", _adapter)


def _pick_proxy():
    if PROXY_LIST:
        p = random.choice(PROXY_LIST)
        return {"http": p, "https": p}
    return None


SESSIONS = {}  # chat_id -> conversation state (in memory)

# ----------------------------- SQLite -----------------------------------------
_lock = threading.RLock()  # re-entrant: get_user() -> default_limit() -> setting() nests the lock
_db = sqlite3.connect(RC_DB, check_same_thread=False)
_db.row_factory = sqlite3.Row


def db_init():
    with _lock:
        _db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS apis(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT, url_template TEXT, mobile_path TEXT,
                vehicle_path TEXT, active INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS admins(chat_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS users(
                chat_id INTEGER PRIMARY KEY, lim INTEGER DEFAULT 0,
                used INTEGER DEFAULT 0, name TEXT, username TEXT);
            CREATE TABLE IF NOT EXISTS scans(
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, requester TEXT,
                start_plate TEXT, last_plate TEXT, target INTEGER, found INTEGER,
                checked INTEGER, status TEXT, ts REAL);
            CREATE TABLE IF NOT EXISTS proxies(id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS resume_state(
                chat_id INTEGER PRIMARY KEY, next_plate TEXT, remaining INTEGER, ts REAL);
            """
        )
        _db.commit()
        # seed a default API once
        if _db.execute("SELECT COUNT(*) c FROM apis").fetchone()["c"] == 0:
            _db.execute(
                "INSERT INTO apis(name,url_template,mobile_path,vehicle_path,active) VALUES(?,?,?,?,1)",
                ("Paanel Shop", "https://api.paanel.shop/api/gateway.php?key=DuXxZxX&v2num={VEH}",
                 "data.mobile", "data.reg_no"),
            )
            _db.commit()
    for a in SUPER_ADMINS.split(","):
        a = a.strip()
        if a:
            add_admin(int(a))
    for p in db_proxies():
        if p not in PROXY_LIST:
            PROXY_LIST.append(p)


IP_RE = re.compile(r"^(\d{1,3}(\.\d{1,3}){3}|[a-z0-9.-]+\.[a-z]{2,})$", re.I)


def parse_proxy(raw):
    # accepts ip:port:login:pass | login:pass@ip:port | login:pass:ip:port | ip:port | http(s)/socks5 links
    t = (raw or "").strip()
    scheme = "http"
    m = re.match(r"^(https?|socks5h?)://(.+)$", t, re.I)
    if m:
        scheme, t = m.group(1).lower(), m.group(2)
    if scheme == "socks5":
        scheme = "socks5h"
    if "@" in t:
        auth, host = t.rsplit("@", 1)
        hp = host.split(":")
        if len(hp) != 2 or not hp[1].isdigit():
            return None
        return f"{scheme}://{auth}@{host}"
    parts = t.split(":")
    if len(parts) == 2 and parts[1].isdigit() and IP_RE.match(parts[0]):
        return f"{scheme}://{parts[0]}:{parts[1]}"
    if len(parts) >= 4:
        if IP_RE.match(parts[0]) and parts[1].isdigit():
            return f"{scheme}://{parts[2]}:{':'.join(parts[3:])}@{parts[0]}:{parts[1]}"
        if IP_RE.match(parts[-2]) and parts[-1].isdigit():
            return f"{scheme}://{parts[0]}:{':'.join(parts[1:-2])}@{parts[-2]}:{parts[-1]}"
    return None


def mask_proxy(url):
    return re.sub(r"(://[^:@/]+:)[^@]+@", r"\1****@", url)


def db_proxies():
    with _lock:
        return [r["url"] for r in _db.execute("SELECT url FROM proxies ORDER BY id").fetchall()]


def add_proxy_db(url):
    with _lock:
        _db.execute("INSERT OR IGNORE INTO proxies(url) VALUES(?)", (url,))
        _db.commit()
    if url not in PROXY_LIST:
        PROXY_LIST.append(url)
    _route["mode"] = None


def del_proxy_db(url):
    with _lock:
        _db.execute("DELETE FROM proxies WHERE url=?", (url,))
        _db.commit()
    if url in PROXY_LIST:
        PROXY_LIST.remove(url)
    _route["mode"] = None


def test_proxy(url, cfg, vehicle="HR76E2990"):
    px = {"http": url, "https": url}
    t0 = time.time()
    try:
        r = SESSION.get(cfg["url_template"].replace("{VEH}", vehicle), proxies=px, timeout=(8, 25))
        ms = int((time.time() - t0) * 1000)
        if _extract_json(r.text) is not None:
            return True, f"API reply OK in {ms} ms (HTTP {r.status_code})"
        return False, f"proxy works but API sent no JSON (HTTP {r.status_code})"
    except requests.exceptions.ProxyError as e:
        if "407" in str(e) or "auth" in str(e).lower():
            return False, "login/password wrong or proxy not active (407 auth failed)"
        return False, "proxy refused connection"
    except requests.exceptions.ConnectTimeout:
        return False, "proxy not reachable (timeout)"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"


def setting(key, default=""):
    with _lock:
        row = _db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with _lock:
        _db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=?",
                    (key, str(value), str(value)))
        _db.commit()


def is_admin(chat_id):
    with _lock:
        return _db.execute("SELECT 1 FROM admins WHERE chat_id=?", (chat_id,)).fetchone() is not None


def admin_count():
    with _lock:
        return _db.execute("SELECT COUNT(*) c FROM admins").fetchone()["c"]


def add_admin(chat_id):
    with _lock:
        _db.execute("INSERT OR IGNORE INTO admins(chat_id) VALUES(?)", (chat_id,))
        _db.commit()


def remove_admin(chat_id):
    with _lock:
        _db.execute("DELETE FROM admins WHERE chat_id=?", (chat_id,))
        _db.commit()


def default_limit():
    return int(setting("default_limit", "0") or 0)


def get_user(chat_id, name="", username=""):
    with _lock:
        row = _db.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            _db.execute("INSERT INTO users(chat_id,lim,used,name,username) VALUES(?,?,0,?,?)",
                        (chat_id, default_limit(), name, username))
            _db.commit()
            row = _db.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        elif name or username:
            _db.execute("UPDATE users SET name=?,username=? WHERE chat_id=?",
                        (name or row["name"], username or row["username"], chat_id))
            _db.commit()
    return dict(row)


def set_user_limit(chat_id, limit):
    with _lock:
        _db.execute("INSERT INTO users(chat_id,lim,used) VALUES(?,?,0) "
                    "ON CONFLICT(chat_id) DO UPDATE SET lim=?", (chat_id, int(limit), int(limit)))
        _db.commit()


def add_used(chat_id, n):
    if n > 0:
        with _lock:
            _db.execute("UPDATE users SET used=used+? WHERE chat_id=?", (n, chat_id))
            _db.commit()


def user_info(chat_id):
    with _lock:
        row = _db.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    return dict(row) if row else None


def list_apis():
    with _lock:
        return [dict(r) for r in _db.execute("SELECT * FROM apis ORDER BY id").fetchall()]


def active_config():
    with _lock:
        row = _db.execute("SELECT * FROM apis WHERE active=1 ORDER BY id LIMIT 1").fetchone() \
            or _db.execute("SELECT * FROM apis ORDER BY id LIMIT 1").fetchone()
    return dict(row) if row else None


def add_api(name, url_template, mobile_path, vehicle_path):
    with _lock:
        _db.execute("UPDATE apis SET active=0")
        _db.execute("INSERT INTO apis(name,url_template,mobile_path,vehicle_path,active) VALUES(?,?,?,?,1)",
                    (name, url_template, mobile_path, vehicle_path))
        _db.commit()


def activate_api(idx):
    apis = list_apis()
    if not (1 <= idx <= len(apis)):
        return None
    target = apis[idx - 1]
    with _lock:
        _db.execute("UPDATE apis SET active=0")
        _db.execute("UPDATE apis SET active=1 WHERE id=?", (target["id"],))
        _db.commit()
    return target


def log_scan(chat_id, requester, start_plate, last_plate, target, found, checked, status):
    with _lock:
        _db.execute("INSERT INTO scans(chat_id,requester,start_plate,last_plate,target,found,checked,status,ts) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (chat_id, requester, start_plate, last_plate, target, found, checked, status, time.time()))
        _db.commit()


def save_resume(chat_id, next_plate, remaining):
    with _lock:
        _db.execute("INSERT OR REPLACE INTO resume_state(chat_id,next_plate,remaining,ts) VALUES(?,?,?,?)",
                    (chat_id, next_plate, remaining, time.time()))
        _db.commit()


def get_resume(chat_id):
    with _lock:
        r = _db.execute("SELECT * FROM resume_state WHERE chat_id=?", (chat_id,)).fetchone()
    return dict(r) if r else None


def clear_resume(chat_id):
    with _lock:
        _db.execute("DELETE FROM resume_state WHERE chat_id=?", (chat_id,))
        _db.commit()


# ----------------------------- Telegram --------------------------------------
def tg(method, **payload):
    try:
        return SESSION.post(f"{TG}/{method}", json=payload, timeout=60).json()
    except Exception as e:
        print("tg error", method, e)
        return {"ok": False}


def send(chat_id, text, reply_markup=None):
    if reply_markup:
        return tg("sendMessage", chat_id=chat_id, text=text, reply_markup=reply_markup)
    return tg("sendMessage", chat_id=chat_id, text=text)


def send_document(chat_id, filename, content_bytes, caption=""):
    try:
        return SESSION.post(f"{TG}/sendDocument",
                            data={"chat_id": chat_id, "caption": caption[:1000]},
                            files={"document": (filename, content_bytes, "text/csv")},
                            timeout=120).json()
    except Exception as e:
        print("sendDocument error", e)
        return {"ok": False}


# ----------------------------- Lookup core -----------------------------------
VEHICLE_RE = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{0,3}\d+$")


def normalize_vehicle(raw):
    v = re.sub(r"[\s\-\.]", "", (raw or "").upper())
    return v if VEHICLE_RE.match(v) else None


def split_plate(vehicle):
    m = re.match(r"^(.*?)(\d+)$", vehicle)
    return m.group(1), int(m.group(2)), len(m.group(2))


def get_path(obj, path):
    cur = obj
    for part in (path or "").split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def valid_mobile(raw):
    d = re.sub(r"\D", "", str(raw or ""))
    if len(d) >= 10 and d[-10] in "6789":
        return d[-10:]
    return None


def _extract_json(text):
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _build(target, mode):
    if mode == "relay":
        h = {"X-Return-Format": "text"}
        if JINA_API_KEY:
            h["Authorization"] = "Bearer " + JINA_API_KEY
        return RELAY_PREFIX + target, h, None
    if mode == "proxy":
        return target, {}, _pick_proxy()
    return target, {}, None


def fetch(target, mode, timeout=None):
    url, h, px = _build(target, mode)
    return SESSION.get(url, timeout=timeout or (CONNECT_TIMEOUT, READ_TIMEOUT), headers=h, proxies=px)


_route = {"mode": None, "at": 0.0}


def pick_route(cfg, vehicle):
    # auto: try DIRECT first (fastest, works on most VPS). Blocked -> proxy -> relay.
    if TRANSPORT in ("direct", "relay") or (TRANSPORT == "proxy" and PROXY_LIST):
        return TRANSPORT
    if _route["mode"] and time.time() - _route["at"] < 600:
        return _route["mode"]
    mode = None
    try:
        r = fetch(cfg["url_template"].replace("{VEH}", vehicle), "direct", timeout=(5, 12))
        if _extract_json(r.text) is not None:
            mode = "direct"
    except Exception:
        pass
    if not mode:
        mode = "proxy" if PROXY_LIST else ("relay" if RELAY_PREFIX else "direct")
    _route.update(mode=mode, at=time.time())
    print("route selected:", mode, flush=True)
    return mode


def lookup(vehicle, cfg, mode, delay=0.0, retries=3):
    # out["error"]=True means we NEVER got a valid response (transient/proxy fail)
    # -> caller must re-queue it, NOT treat as "no data" (prevents skips).
    if delay:
        time.sleep(delay)
    out = {"vehicle_number": vehicle, "mobile_number": "", "success": False, "limit_hit": False, "error": False}
    target = cfg["url_template"].replace("{VEH}", vehicle)
    busy = False
    for attempt in range(retries):
        try:
            busy = False
            r = fetch(target, mode)
            data = _extract_json(r.text)
            # API answers "No data found" with HTTP 502 -> that's a definitive empty, not a network error
            api_empty = isinstance(data, dict) and "no data" in str(data.get("message", "")).lower()
            if not api_empty and (r.status_code in (429, 451, 502, 503, 504) or (mode == "relay" and r.status_code >= 500)):
                busy = r.status_code in (429, 503)  # API itself overloaded, route is fine
                raise ValueError("transient")
            if not isinstance(data, dict):
                raise ValueError("no json")
        except Exception:
            if attempt < retries - 1:
                time.sleep((1.0 if busy else 0.3) * (attempt + 1))
                continue
            out["error"] = True
            out["busy"] = busy
            return out
        blob = str(data).lower()
        if data.get("code") == "KEY_LIMIT_REACHED" or "api key limit" in blob \
                or "key limit reached" in blob or "quota exceeded" in blob:
            out["limit_hit"] = True
            return out
        mobile = valid_mobile(get_path(data, cfg["mobile_path"]))
        if mobile:
            out["mobile_number"] = mobile
            out["success"] = True
        return out  # definitive (data or confirmed-empty)
    out["error"] = True
    return out


STOP_KB = {"inline_keyboard": [[{"text": "⛔ STOP / CANCEL", "callback_data": "stop_scan"}]]}
CANCEL = {}  # chat_id -> threading.Event of the running scan


def _progress_text(checked, found, target, last_plate, started, par=None):
    secs = max(int(time.time() - started), 1)
    p = f" · 🔧 {par} parallel" if par else ""
    return (f"⏳ 𝗟𝗜𝗩𝗘 𝗣𝗥𝗢𝗚𝗥𝗘𝗦𝗦\n━━━━━━━━━━━━━━━━\n🚗 Checked: {checked}\n✅ Found: {found}/{target}\n"
            f"📍 At: {last_plate}\n⚡ Speed: {checked * 60 // secs}/min · ⏱️ {secs}s{p}\n\n⛔ Stop: button below or /exit")


def _fallback(mode):
    # direct -> proxy -> relay
    chain = [m for m in ("direct", "proxy", "relay")
             if m == "direct" or (m == "proxy" and PROXY_LIST) or (m == "relay" and RELAY_PREFIX)]
    return chain[chain.index(mode) + 1] if mode in chain and chain.index(mode) + 1 < len(chain) else None


def scan(chat_id, prefix, start, width, target, cfg, mode, ev, started):
    # Streaming pipeline: always MAX_WORKERS requests in flight, results consumed
    # strictly in plate order -> no skip, correct pairing, no batch waiting.
    found, checked, last_plate = [], 0, f"{prefix}{str(start).zfill(width)}"
    state = {"limit": False, "net": False, "cancel": False, "mode": mode}
    streak = 0
    q, nxt, last_ping = deque(), 0, time.time()
    base_w = par_workers()  # admin /parallel or /setworkers overrides env
    auto = par_mode() == "auto"
    workers = min(base_w, 8) if (auto and mode == "relay" and not JINA_API_KEY) else base_w  # free relay is rate-limited
    ahead = workers * 2
    prog_id = (send(chat_id, _progress_text(0, 0, target, last_plate, started), STOP_KB).get("result") or {}).get("message_id")
    ex = ThreadPoolExecutor(max_workers=workers)
    # auto-speed: API busy/errors -> fewer parallel requests, smooth replies -> slowly back up
    lim = {"v": float(workers)}
    lim_lock = threading.Lock()

    def _feedback(f):
        if not auto:
            return  # MANUAL: admin's number stays fixed
        try:
            r = f.result()
        except Exception:
            return
        with lim_lock:
            lim["v"] = max(2.0, lim["v"] * 0.7) if r.get("error") else min(float(workers), lim["v"] + 0.25)

    rx = ThreadPoolExecutor(max_workers=4)  # retries get their own lane, never wait behind the queue

    def _submit(p, delay=0.0, retry=False):
        f = (rx if retry else ex).submit(lookup, p, cfg, state["mode"], delay)
        f.add_done_callback(_feedback)
        return f

    try:
        while len(found) < target and checked < RC_MAX_SCAN:
            if ev.is_set():
                state["cancel"] = True
                break
            running = sum(1 for it in q if not it[2].done())
            while len(q) < ahead and nxt < RC_MAX_SCAN and running < int(lim["v"]):
                p = f"{prefix}{str(start + nxt).zfill(width)}"
                q.append([p, 0, _submit(p)])
                nxt += 1
                running += 1
            item = q[0]
            try:
                res = item[2].result(timeout=1)
            except FutureTimeout:
                res = None
            if res is not None:
                if res.get("error"):
                    streak += 0 if res.get("busy") else 1  # API busy is not a route failure
                    nxt_mode = _fallback(state["mode"]) if streak >= SWITCH_AFTER else None
                    if nxt_mode:
                        send(chat_id, f"🔀 {state['mode'].upper()} route failing → switching to {nxt_mode.upper()} "
                                      f"(continuing from {item[0]}, nothing skipped)")
                        print("route switch:", state["mode"], "->", nxt_mode, flush=True)
                        state["mode"] = nxt_mode
                        _route.update(mode=nxt_mode, at=time.time())
                        if nxt_mode == "relay" and not JINA_API_KEY:
                            ahead = min(ahead, 8)
                        streak, item[1] = 0, 0
                    if item[1] >= RETRY_ROUNDS:
                        state["net"] = True
                        break
                    item[1] += 0.5 if res.get("busy") else 1
                    item[2] = _submit(item[0], min((1.5 if res.get("busy") else 0.5) * item[1], 6), retry=True)
                else:
                    streak = 0
                    q.popleft()
                    if res.get("limit_hit"):
                        state["limit"] = True
                        break
                    checked += 1
                    last_plate = item[0]
                    if res.get("success") and res.get("mobile_number"):
                        found.append(res)
            if prog_id and time.time() - last_ping >= PROGRESS_EVERY:
                tg("editMessageText", chat_id=chat_id, message_id=prog_id, reply_markup=STOP_KB,
                   text=_progress_text(checked, len(found), target, last_plate, started,
                                      f"{int(lim['v'])} ({'AUTO' if auto else 'MANUAL'})"))
                last_ping = time.time()
    finally:
        ev.set()  # wakes nothing, just marks scan over
        ex.shutdown(wait=False, cancel_futures=True)
        rx.shutdown(wait=False, cancel_futures=True)
    if prog_id:
        tg("editMessageText", chat_id=chat_id, message_id=prog_id,
           text=_progress_text(checked, len(found), target, last_plate, started).split("\n\n⛔")[0] + "\n\n🏁 Scan finished")
    return found[:target], checked, last_plate, state


def run_bulk(chat_id, vehicle, target, requester="", ev=None):
    cfg = active_config()
    if not cfg or not cfg.get("url_template"):
        send(chat_id, "⚠️ No API configured. Admin: use /setapi first.")
        return 0
    ev = ev or threading.Event()
    prefix, start, width = split_plate(vehicle)
    started = time.time()
    mode = pick_route(cfg, vehicle)
    send(chat_id,
         "🚀 𝗦𝗖𝗔𝗡 𝗦𝗧𝗔𝗥𝗧𝗘𝗗!\n━━━━━━━━━━━━━━━━\n"
         f"🎯 Target: {target} numbers\n🚗 Start: {vehicle}\n🔌 Source: {cfg['name']}\n"
         f"⚡ Route: {mode.upper()} · {par_workers()} parallel ({par_mode().upper()})\n"
         f"🔎 I'll keep trying the series until {target} numbers are found...\n"
         "⏳ Live count every few seconds 🔥")
    found, checked, last_plate, st = scan(chat_id, prefix, start, width, target, cfg, mode, ev, started)
    limit_hit, net_stop, cancelled = st["limit"], st["net"], st["cancel"]
    reached = len(found) >= target
    next_plate = f"{prefix}{str(start + checked).zfill(width)}"
    status = "done" if reached else "limit" if limit_hit else "network" if net_stop else "cancelled" if cancelled else "partial"
    log_scan(chat_id, requester, vehicle, last_plate, target, len(found), checked, status)
    if reached:
        clear_resume(chat_id)
    else:
        save_resume(chat_id, next_plate, target - len(found))
    if net_stop:
        _route["mode"] = None  # re-detect route next time

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["vehicle_number", "mobile_number"])
    for r in found:
        w.writerow([r["vehicle_number"], r["mobile_number"]])
    csv_bytes = buf.getvalue().encode("utf-8")

    summary = (("✅ 𝗗𝗢𝗡𝗘! Target reached 🎉" if reached else
                ("⛔ 𝗦𝗧𝗢𝗣𝗣𝗘𝗗 by you" if cancelled else "⚠️ Sending the numbers found so far")) +
               f"\n━━━━━━━━━━━━━━━━\n🎯 Target: {target}\n📞 Numbers found: {len(found)}\n"
               f"❌ Empty: {checked - len(found)}\n🚗 Range: {vehicle} → {last_plate}\n"
               f"🔢 Total checked: {checked}\n⏱️ Time: {int(time.time() - started)}s")
    if limit_hit:
        summary = ("🚫 𝗔𝗣𝗜 𝗟𝗜𝗠𝗜𝗧 𝗥𝗘𝗔𝗖𝗛𝗘𝗗!\n━━━━━━━━━━━━━━━━\n"
                   f"📞 Found so far: {len(found)}\n🚗 Range: {vehicle} → {last_plate}\n"
                   f"🔢 Checked: {checked}\n\n⏰ API limit used up. Try later or switch API.")
    elif net_stop:
        summary += ("\n\n⚠️ Network/proxy was unstable at this point, so I stopped here instead "
                    "of skipping any vehicle.")
    if not reached:
        summary += f"\n\n▶️ /resume → continues from {next_plate} for the remaining {target - len(found)}"

    if found:
        send_document(chat_id, f"leads_{vehicle}_{target}.csv", csv_bytes, caption=summary)
        preview = "\n".join(f"{i+1}. 🚗 {r['vehicle_number']}  📞 {r['mobile_number']}"
                            for i, r in enumerate(found[:10]))
        send(chat_id, "📋 𝗣𝗥𝗘𝗩𝗜𝗘𝗪 (first 10):\n━━━━━━━━━━━━━━━━\n" + preview)
        channel = setting("forward_channel", "")
        if channel and str(channel) != str(chat_id):
            send_document(channel, f"leads_{vehicle}_{target}.csv", csv_bytes,
                          caption=f"📥 New result\n👤 {requester or chat_id}\n🚗 {vehicle} · 🎯 {target} · 📞 {len(found)}")
    else:
        send(chat_id, summary + ("" if (limit_hit or cancelled) else "\n\n😶 No numbers in this range — admin can switch API (/apilist)."))
    return len(found)


# ----------------------------- Admin flow ------------------------------------
ADMIN_MENU = (
    "🛠️ 𝗔𝗗𝗠𝗜𝗡 𝗣𝗔𝗡𝗘𝗟\n━━━━━━━━━━━━━━━━\n"
    "🔌 /apilist\n➕ /setapi\n✅ /activate <no>\n🔎 /readjson <vehicle>\n\n"
    "👤 /setlimit <chat_id> <limit>\n📊 /userinfo <chat_id>\n⚙️ /setdefault <limit>\n"
    "🌐 /addproxy · /proxylist · /delproxy <no> · /testproxy\n⚡ /parallel (AUTO / MANUAL speed)\n📊 /report (last 24h, auto daily 10 PM IST)\n📣 /setchannel <channel_id>\n👑 /addadmin <id> · /deladmin <id>\n\n🆔 /myid"
)


def fmt_apilist():
    apis = list_apis()
    if not apis:
        return "No APIs. Use /setapi."
    out = ["🔌 𝗔𝗣𝗜𝘀:\n━━━━━━━━━━━━━━━━"]
    for i, d in enumerate(apis, 1):
        out.append(f"{i}. {'✅ ACTIVE' if d['active'] else '⚪'} {d['name']}\n    mobile: {d['mobile_path']}  vehicle: {d['vehicle_path'] or '-'}")
    return "\n".join(out)


def handle_admin_flow(chat_id, text, s):
    step = s["step"]
    if step == "admin_proxy":
        SESSIONS.pop(chat_id, None)
        admin_add_proxies(chat_id, text)
        return
    if step == "admin_api_name":
        s["api"] = {"name": text}; s["step"] = "admin_api_url"
        send(chat_id, "🔗 Send the API URL — put {VEH} where the vehicle number goes.\nExample:\nhttps://api.site.com/rc?key=XXXX&num={VEH}")
    elif step == "admin_api_url":
        if "{VEH}" not in text:
            send(chat_id, "❗ URL must contain {VEH}. Send again."); return
        s["api"]["url_template"] = text.strip(); s["step"] = "admin_api_mobile"
        send(chat_id, "📞 JSON field path for the mobile? e.g. data.mobile  or  mobile_number")
    elif step == "admin_api_mobile":
        s["api"]["mobile_path"] = text.strip(); s["step"] = "admin_api_vehicle"
        send(chat_id, "🚗 Vehicle-number field path? (optional — send `-` to skip) e.g. data.reg_no")
    elif step == "admin_api_vehicle":
        a = s["api"]; a["vehicle_path"] = "" if text.strip() == "-" else text.strip()
        add_api(a["name"], a["url_template"], a["mobile_path"], a["vehicle_path"])
        SESSIONS.pop(chat_id, None)
        send(chat_id, f"✅ API saved & activated!\n🔌 {a['name']}\n📞 {a['mobile_path']}\n\nTry: /readjson <vehicle>")


def admin_add_proxies(chat_id, text):
    cfg = active_config()
    lines = [x for x in re.split(r"[\n,\s]+", text.strip()) if x]
    if not lines:
        send(chat_id, PROXY_HELP); return
    if not cfg:
        send(chat_id, "⚠️ No active API — add one with /setapi first (needed to test the proxy)."); return
    for raw in lines:
        url = parse_proxy(raw)
        if not url:
            send(chat_id, f"❗ Format not understood: {raw[:60]}\n\n" + PROXY_HELP); continue
        send(chat_id, f"🧪 Testing {mask_proxy(url)} ...")
        ok, info = test_proxy(url, cfg)
        if ok:
            add_proxy_db(url)
            send(chat_id, f"✅ Proxy saved & active!\n🌐 {mask_proxy(url)}\n⚡ {info}\n📦 Total proxies: {len(PROXY_LIST)}")
        else:
            send(chat_id, f"❌ Not saved — {info}\n🌐 {mask_proxy(url)}")


PROXY_HELP = ("🌐 𝗔𝗗𝗗 𝗣𝗥𝗢𝗫𝗬\n━━━━━━━━━━━━━━━━\nSend it exactly as your proxy site gives it:\n"
              "• ip:port:login:password\n• login:password@ip:port\n• ip:port\n"
              "Example:\n151.242.176.12:59100:mylogin:mypass\n\nMultiple? one per line.")


def fmt_proxylist():
    if not PROXY_LIST:
        return "🌐 No proxy added.\n➕ /addproxy to add one."
    saved = set(db_proxies())
    rows = [f"{i+1}. {mask_proxy(p)}{'' if p in saved else '  (env)'}" for i, p in enumerate(PROXY_LIST)]
    return "🌐 𝗣𝗥𝗢𝗫𝗜𝗘𝗦\n━━━━━━━━━━━━━━━━\n" + "\n".join(rows) + "\n\n🗑️ /delproxy <no|all>  ·  🧪 /testproxy"


# ----------------------------- Message handler -------------------------------
def handle(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    frm = msg.get("from", {})
    name = " ".join(filter(None, [frm.get("first_name"), frm.get("last_name")]))
    username = frm.get("username", "")

    if text.startswith(("/exit", "/stop", "/cancel")):
        ev = CANCEL.get(chat_id)
        SESSIONS.pop(chat_id, None)
        if ev and not ev.is_set():
            ev.set()
            send(chat_id, "⛔ Stopping scan... sending the numbers found so far 📄")
        else:
            send(chat_id, "ℹ️ No scan running. Send /start to begin 🙂")
        return

    s = SESSIONS.get(chat_id)
    if s and s.get("step", "").startswith("admin_") and not text.startswith("/"):
        if is_admin(chat_id):
            handle_admin_flow(chat_id, text, s)
        return

    if text.startswith("/myid"):
        u = get_user(chat_id, name, username)
        if is_admin(chat_id):
            send(chat_id, f"🆔 {chat_id}\n👑 ADMIN (unlimited RC).")
        else:
            send(chat_id, f"🆔 {chat_id}\n📞 limit: {u['lim']} · used: {u['used']} · left: {max(u['lim']-u['used'],0)}")
        return

    if text.startswith("/resume"):
        r = get_resume(chat_id)
        running = CANCEL.get(chat_id)
        if running and not running.is_set():
            send(chat_id, "⏳ A scan is already running. Wait or send /exit to stop it."); return
        if not r or r["remaining"] <= 0:
            send(chat_id, "ℹ️ Nothing to resume. Send /start to begin 🙂"); return
        SESSIONS.pop(chat_id, None)
        send(chat_id, f"▶️ Resuming from {r['next_plate']} · remaining {r['remaining']} numbers")
        start_scan(chat_id, r["next_plate"], r["remaining"], name, username)
        return

    if text.startswith("/admin") or text.startswith("/menu"):
        if admin_count() == 0:
            add_admin(chat_id)
            send(chat_id, f"👑 You are now ADMIN!\n🆔 {chat_id}\n\n" + ADMIN_MENU)
        elif is_admin(chat_id):
            send(chat_id, ADMIN_MENU)
        else:
            send(chat_id, f"⛔ Not an admin.\n🆔 Your chat id: {chat_id}\nAsk the admin to unlock RC for you.")
        return

    if is_admin(chat_id):
        if text.startswith("/addproxy"):
            rest = text[len("/addproxy"):].strip()
            if rest:
                admin_add_proxies(chat_id, rest)
            else:
                SESSIONS[chat_id] = {"step": "admin_proxy"}
                send(chat_id, PROXY_HELP)
            return
        if text.startswith("/parallel"):
            t, kb = parallel_panel()
            send(chat_id, t, kb); return
        if text.startswith("/setworkers"):
            p = text.split()
            if len(p) < 2 or not p[1].isdigit() or not (1 <= int(p[1]) <= 100):
                send(chat_id, f"Usage: /setworkers <1-100>\nNow: {setting('workers', '') or MAX_WORKERS}\n"
                              "💡 1 fixed-IP proxy → 2-3 · rotating residential → 20-30"); return
            set_setting("workers", int(p[1]))
            send(chat_id, f"✅ Parallel requests set to {p[1]} (next scan)"); return
        if text.startswith("/proxylist"):
            send(chat_id, fmt_proxylist()); return
        if text.startswith("/delproxy"):
            p = text.split()
            if len(p) < 2:
                send(chat_id, "Usage: /delproxy <no|all>\n\n" + fmt_proxylist()); return
            if p[1].lower() == "all":
                for u in db_proxies():
                    del_proxy_db(u)
                send(chat_id, "🗑️ All saved proxies removed.\n\n" + fmt_proxylist()); return
            if not p[1].isdigit() or not (1 <= int(p[1]) <= len(PROXY_LIST)):
                send(chat_id, "❗ Wrong number.\n\n" + fmt_proxylist()); return
            u = PROXY_LIST[int(p[1]) - 1]
            if u not in db_proxies():
                send(chat_id, "ℹ️ This one comes from Railway/VPS variables — remove it there."); return
            del_proxy_db(u)
            send(chat_id, f"🗑️ Removed {mask_proxy(u)}\n\n" + fmt_proxylist()); return
        if text.startswith("/testproxy"):
            cfg = active_config()
            if not PROXY_LIST or not cfg:
                send(chat_id, fmt_proxylist()); return
            out = [f"{i+1}. {'✅' if ok else '❌'} {mask_proxy(u)} — {info}"
                   for i, u in enumerate(list(PROXY_LIST)) for ok, info in [test_proxy(u, cfg)]]
            send(chat_id, "🧪 𝗣𝗥𝗢𝗫𝗬 𝗧𝗘𝗦𝗧\n━━━━━━━━━━━━━━━━\n" + "\n".join(out)); return
        if text.startswith("/report"):
            send(chat_id, daily_report_text()); return
        if text.startswith("/apilist"):
            send(chat_id, fmt_apilist()); return
        if text.startswith("/setapi"):
            SESSIONS[chat_id] = {"step": "admin_api_name"}
            send(chat_id, "➕ 𝗔𝗗𝗗 𝗔𝗣𝗜\n━━━━━━━━━━━━━━━━\n📛 Send a name (e.g. Paanel Shop):"); return
        if text.startswith("/activate"):
            p = text.split()
            if len(p) < 2 or not p[1].isdigit() or not activate_api(int(p[1])):
                send(chat_id, "Usage: /activate <number>\n\n" + fmt_apilist())
            else:
                send(chat_id, f"✅ Active API: {activate_api(int(p[1]))['name']}")
            return
        if text.startswith("/readjson"):
            p = text.split()
            if len(p) < 2:
                send(chat_id, "Usage: /readjson <vehicle>"); return
            cfg = active_config()
            if not cfg:
                send(chat_id, "No active API."); return
            veh = normalize_vehicle(p[1]) or p[1].upper()
            t = cfg["url_template"].replace("{VEH}", veh)
            try:
                mode = pick_route(cfg, veh)
                raw = _extract_json(fetch(t, mode).text)
                mob = get_path(raw, cfg["mobile_path"]) if raw else None
                send(chat_id, f"🔎 {cfg['name']} — {veh} · route {mode}\n📞 {cfg['mobile_path']} → {mob}\n\n🧾 {json.dumps(raw, indent=2, ensure_ascii=False)[:3500]}")
            except Exception as e:
                send(chat_id, f"⚠️ {type(e).__name__}: {e}")
            return
        if text.startswith("/setlimit"):
            p = text.split()
            if len(p) < 3 or not p[1].lstrip("-").isdigit() or not p[2].isdigit():
                send(chat_id, "Usage: /setlimit <chat_id> <limit>"); return
            set_user_limit(int(p[1]), int(p[2]))
            send(chat_id, f"✅ RC limit set!\n👤 {p[1]}\n📞 {p[2]}")
            try:
                send(int(p[1]), f"🎉 Admin unlocked {p[2]} RC lookups for you.\nSend /start to begin. 🚗")
            except Exception:
                pass
            return
        if text.startswith("/userinfo"):
            p = text.split()
            if len(p) < 2 or not p[1].lstrip("-").isdigit():
                send(chat_id, "Usage: /userinfo <chat_id>"); return
            u = user_info(int(p[1]))
            send(chat_id, "No such user yet." if not u else
                 f"👤 {u['chat_id']}\n📛 {u['name']} @{u['username']}\n📞 limit:{u['lim']} used:{u['used']} left:{max(u['lim']-u['used'],0)}")
            return
        if text.startswith("/setdefault"):
            p = text.split()
            if len(p) < 2 or not p[1].isdigit():
                send(chat_id, "Usage: /setdefault <limit>"); return
            set_setting("default_limit", int(p[1]))
            send(chat_id, f"✅ Default RC limit for new users: {p[1]}"); return
        if text.startswith("/setchannel"):
            p = text.split()
            if len(p) < 2:
                send(chat_id, f"Usage: /setchannel <channel_id> (or off)\nCurrent: {setting('forward_channel','') or 'off'}\n💡 Add the bot to the channel as ADMIN, then send its id (e.g. -1001234567890)."); return
            if p[1].lower() == "off":
                set_setting("forward_channel", ""); send(chat_id, "📣 Forwarding OFF."); return
            set_setting("forward_channel", p[1])
            ok = send(p[1], "✅ This channel is linked. Result files will be forwarded here. 📥").get("ok")
            send(chat_id, f"✅ Channel set: {p[1]}" if ok else f"⚠️ Saved {p[1]}, but couldn't post. Add the bot as channel ADMIN and retry.")
            return
        if text.startswith("/addadmin"):
            p = text.split()
            if len(p) < 2 or not p[1].lstrip("-").isdigit():
                send(chat_id, "Usage: /addadmin <chat_id>"); return
            add_admin(int(p[1])); send(chat_id, f"👑 Added admin: {p[1]}"); return
        if text.startswith("/deladmin"):
            p = text.split()
            if len(p) < 2 or not p[1].lstrip("-").isdigit():
                send(chat_id, "Usage: /deladmin <chat_id>"); return
            remove_admin(int(p[1])); send(chat_id, f"🗑️ Removed admin: {p[1]}"); return

    # ---- user flow ----
    if text.startswith("/start"):
        get_user(chat_id, name, username)
        SESSIONS[chat_id] = {"step": "series"}
        send(chat_id, "📥 𝗩𝗘𝗛𝗜𝗖𝗟𝗘 → 𝗢𝗪𝗡𝗘𝗥 𝗡𝗨𝗠𝗕𝗘𝗥 (𝗕𝗨𝗟𝗞)\n🔌 Secure Premium Server ⚡\n🛡️ Validation: ON\n━━━━━━━━━━━━━━━━━━\n\n🚗 Send your vehicle series 👇\n📝 Example: WB01AB4049")
        return

    if not s:
        send(chat_id, "👋 Send /start to begin 🙂"); return

    if s["step"] == "series":
        v = normalize_vehicle(text)
        if not v:
            send(chat_id, "❗ That doesn't look right 🤔\n✅ Example: WB01AB4049"); return
        s["series"] = v; s["step"] = "quantity"
        send(chat_id, f"✅ Series set: {v} 🚗\n\n🔢 How many 𝗡𝗨𝗠𝗕𝗘𝗥𝗦 (mobiles) do you need?\n📝 Just send a number, e.g. 100")
        return

    if s["step"] == "quantity":
        d = re.sub(r"\D", "", text)
        if not d or int(d) < 1:
            send(chat_id, "❗ Send a valid number 🔢 (e.g. 100)"); return
        SESSIONS.pop(chat_id, None)
        start_scan(chat_id, s["series"], int(d), name, username)
        return


def start_scan(chat_id, vehicle, target, name, username):
    running = CANCEL.get(chat_id)
    if running and not running.is_set():
        send(chat_id, "⏳ A scan is already running. Wait or send /exit to stop it."); return
    if is_admin(chat_id):
        effective = target
    else:
        u = get_user(chat_id, name, username)
        left = max(u["lim"] - u["used"], 0)
        if left <= 0:
            send(chat_id, f"🔒 Your RC limit is over.\n📞 limit:{u['lim']} used:{u['used']}\n🆔 Your chat id: {chat_id}\nAsk the admin to unlock more."); return
        effective = min(target, left)
        if effective < target:
            send(chat_id, f"ℹ️ You asked {target}, only {left} left. Fetching {effective}.")
    ev = threading.Event()
    CANCEL[chat_id] = ev
    threading.Thread(target=_scan_job, args=(chat_id, vehicle, effective, f"{name} @{username} ({chat_id})", ev),
                     daemon=True).start()


def _scan_job(chat_id, vehicle, effective, requester, ev):
    # runs in background so the bot keeps reading /exit and the STOP button
    try:
        got = run_bulk(chat_id, vehicle, effective, requester=requester, ev=ev)
        if not is_admin(chat_id) and got:
            add_used(chat_id, got)
            u = user_info(chat_id)
            send(chat_id, f"📊 RC balance → used:{u['used']} left:{max(u['lim']-u['used'],0)}")
    except Exception as e:
        send(chat_id, f"⚠️ Something went wrong: {e}")
    finally:
        ev.set()
        if CANCEL.get(chat_id) is ev:
            CANCEL.pop(chat_id, None)


def par_mode():
    return setting("par_mode", "") or "auto"


def par_workers():
    return int(setting("workers", "") or MAX_WORKERS)


PAR_CHOICES = [2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30]


def parallel_panel():
    m, w = par_mode(), par_workers()
    text = ("⚡ 𝗣𝗔𝗥𝗔𝗟𝗟𝗘𝗟 𝗦𝗘𝗧𝗧𝗜𝗡𝗚\n━━━━━━━━━━━━━━━━\n"
            f"Mode: {'🤖 AUTO' if m == 'auto' else '✋ MANUAL'} · Number: {w}\n\n"
            "🤖 AUTO → starts at this number, goes down when API is busy, back up when smooth\n"
            "✋ MANUAL → exactly this number, always\n\n"
            "👇 Pick mode, then tap a number (or /setworkers <1-100>)")
    mark = lambda on: "✅ " if on else ""
    kb = [[{"text": mark(m == "auto") + "🤖 AUTO", "callback_data": "par_auto"},
           {"text": mark(m == "manual") + "✋ MANUAL", "callback_data": "par_manual"}]]
    row = []
    for n in PAR_CHOICES:
        row.append({"text": ("•" if n == w else "") + str(n), "callback_data": f"par_n_{n}"})
        if len(row) == 6:
            kb.append(row); row = []
    if row:
        kb.append(row)
    return text, {"inline_keyboard": kb}


def handle_callback(cb):
    chat_id = cb["message"]["chat"]["id"]
    data = cb.get("data", "")
    if data.startswith("par_"):
        if not is_admin(chat_id):
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="Admin only"); return
        if data in ("par_auto", "par_manual"):
            set_setting("par_mode", data[4:])
        elif data.startswith("par_n_") and data[6:].isdigit():
            set_setting("workers", int(data[6:]))
        text, kb = parallel_panel()
        tg("editMessageText", chat_id=chat_id, message_id=cb["message"]["message_id"], text=text, reply_markup=kb)
        tg("answerCallbackQuery", callback_query_id=cb["id"], text=f"Saved: {par_mode().upper()} · {par_workers()} (next scan)")
        return
    ev = CANCEL.get(chat_id)
    if cb.get("data") == "stop_scan" and ev and not ev.is_set():
        ev.set()
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Stopping... ⛔")
        send(chat_id, "⛔ Stopping scan... sending the numbers found so far 📄")
    else:
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="No scan running")


IST = timezone(timedelta(hours=5, minutes=30))


def daily_report_text():
    since = time.time() - 86400
    with _lock:
        tot = _db.execute("SELECT COUNT(*) n, COALESCE(SUM(found),0) f, COALESCE(SUM(checked),0) c, "
                          "COUNT(DISTINCT chat_id) u FROM scans WHERE ts>=?", (since,)).fetchone()
        top = _db.execute("SELECT chat_id, MAX(requester) who, COUNT(*) n, SUM(found) f FROM scans WHERE ts>=? "
                          "GROUP BY chat_id ORDER BY f DESC LIMIT 5", (since,)).fetchall()
        stops = _db.execute("SELECT status, COUNT(*) n FROM scans WHERE ts>=? GROUP BY status", (since,)).fetchall()
    lines = [f"📊 𝗗𝗔𝗜𝗟𝗬 𝗥𝗘𝗣𝗢𝗥𝗧 · {datetime.now(IST).strftime('%d %b %Y')}", "━━━━━━━━━━━━━━━━",
             f"🔁 Scans: {tot['n']}", f"📞 Numbers found: {tot['f']}", f"🚗 Vehicles checked: {tot['c']}",
             f"👥 Active users: {tot['u']}"]
    if stops:
        lines.append("🧾 " + " · ".join(f"{r['status']}: {r['n']}" for r in stops))
    if top:
        lines += ["", "🏆 𝗧𝗢𝗣 𝗨𝗦𝗘𝗥𝗦"] + [f"{i+1}. {r['who'] or r['chat_id']} — 📞 {r['f']} ({r['n']} scans)"
                                         for i, r in enumerate(top)]
    else:
        lines.append("\n😴 No scans in the last 24 hours.")
    return "\n".join(lines)


def report_loop():
    while True:
        try:
            now = datetime.now(IST)
            today = now.strftime("%Y-%m-%d")
            if now.hour >= REPORT_HOUR and setting("last_report", "") != today:
                set_setting("last_report", today)
                text = daily_report_text()
                with _lock:
                    admins = [r["chat_id"] for r in _db.execute("SELECT chat_id FROM admins").fetchall()]
                for a in admins:
                    send(a, text)
        except Exception as e:
            print("report error:", e, flush=True)
        time.sleep(30)


# ----------------------------- Polling loop ----------------------------------
def main():
    if not TELEGRAM_BOT_TOKEN:
        sys.exit("FATAL: TELEGRAM_BOT_TOKEN env var is NOT set. Add it and restart.")
    db_init()
    tg("deleteWebhook", drop_pending_updates=False)
    threading.Thread(target=report_loop, daemon=True).start()
    me = tg("getMe")
    if not me.get("ok"):
        print("FATAL: getMe failed -> token is probably wrong:", me)
        sys.exit(1)
    print("Bot live:", me["result"].get("username"),
          "| transport:", TRANSPORT, "| proxies:", len(PROXY_LIST), "| relay:", RELAY_PREFIX or "off",
          "| workers:", MAX_WORKERS, flush=True)
    offset = None
    while True:
        try:
            params = {"timeout": 25, "allowed_updates": json.dumps(["message", "callback_query"])}
            if offset:
                params["offset"] = offset
            r = SESSION.get(f"{TG}/getUpdates", params=params, timeout=40).json()
            if not r.get("ok"):
                # 409 = another instance is polling the same bot token
                print("getUpdates not ok:", r.get("error_code"), r.get("description"), flush=True)
                time.sleep(3)
                continue
            for upd in r["result"]:
                offset = upd["update_id"] + 1
                m = upd.get("message")
                try:
                    if m and "text" in m:
                        handle(m)
                    elif upd.get("callback_query"):
                        handle_callback(upd["callback_query"])
                except Exception as e:
                    print("handle error:", e, flush=True)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("poll error:", e, flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
