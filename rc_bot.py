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
     MAX_WORKERS          (optional; higher = faster. Default 6. Big VPS -> 15-20)
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

# ----------------------------- Config ----------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "9010080670:AAFMS2pHpKFWKaW3hYoefBcTxFObzNtJjro").strip()
SUPER_ADMINS = os.environ.get("SUPER_ADMINS", "8922842091")
RELAY_PREFIX = os.environ.get("RELAY_PREFIX", "https://r.jina.ai/").strip()
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "6"))
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
USE_PROXY = len(PROXY_LIST) > 0

TG = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

SESSION = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=200, pool_maxsize=200, max_retries=1)
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


# ----------------------------- Telegram --------------------------------------
def tg(method, **payload):
    try:
        return SESSION.post(f"{TG}/{method}", json=payload, timeout=60).json()
    except Exception as e:
        print("tg error", method, e)
        return {"ok": False}


def send(chat_id, text):
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


def lookup(vehicle, cfg, retries=3):
    # out["error"]=True means we NEVER got a valid response (transient/proxy fail)
    # -> caller must re-queue it, NOT treat as "no data" (prevents skips).
    out = {"vehicle_number": vehicle, "mobile_number": "", "success": False, "limit_hit": False, "error": False}
    target = cfg["url_template"].replace("{VEH}", vehicle)
    use_relay = RELAY_PREFIX and not USE_PROXY
    url = (RELAY_PREFIX + target) if use_relay else target
    headers = {"X-Return-Format": "text"} if use_relay else {}
    if use_relay and JINA_API_KEY:
        headers["Authorization"] = "Bearer " + JINA_API_KEY
    for attempt in range(retries):
        try:
            proxies = _pick_proxy() if USE_PROXY else None
            r = SESSION.get(url, timeout=(10, 45), headers=headers, proxies=proxies)
            if r.status_code in (429, 451, 502, 503, 504) or (use_relay and r.status_code >= 500):
                raise ValueError("transient")
            body = r.text.strip()
            if not body:
                raise ValueError("empty")
            data = _extract_json(body)
            if data is None:
                raise ValueError("no json")
        except Exception:
            if attempt < retries - 1:
                time.sleep(0.3 * (attempt + 1))  # retry, rotate to another proxy
                continue
            out["error"] = True  # could not get a definitive answer
            return out
        if not isinstance(data, dict):
            out["error"] = True
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


def resolve_plates(plates, cfg):
    """Return (results_by_plate, limit_hit). Errored plates are retried across
    several rounds (rotating proxies) so a flaky network NEVER causes a skip.
    Even if a limit response appears, flaky plates BEFORE it keep retrying, so
    data plates are resolved; limit plates are marked so run_bulk stops at them."""
    pending = list(plates)
    results = {}
    limit_hit = False
    for _ in range(12):  # up to 12 rounds of re-queue for stubborn/flaky ones
        if not pending:
            break
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            outs = list(ex.map(lambda v: lookup(v, cfg), pending))
        nxt = []
        for v, res in zip(pending, outs):
            if res.get("limit_hit"):
                limit_hit = True
                results[v] = res  # mark this plate as a limit boundary
            elif res.get("error"):
                nxt.append(v)  # retry next round (flaky) — do NOT give up
            else:
                results[v] = res  # definitive data / empty
        pending = nxt
        if pending:
            time.sleep(0.5)
    # Anything still failing after all rounds is left ABSENT (not marked empty),
    # so run_bulk stops honestly at it instead of silently skipping a possible data plate.
    return results, limit_hit


def run_bulk(chat_id, vehicle, target, requester=""):
    cfg = active_config()
    if not cfg or not cfg.get("url_template"):
        send(chat_id, "⚠️ No API configured. Admin: use /setapi first.")
        return 0
    prefix, start, width = split_plate(vehicle)
    send(chat_id,
         "🚀 𝗦𝗖𝗔𝗡 𝗦𝗧𝗔𝗥𝗧𝗘𝗗!\n━━━━━━━━━━━━━━━━\n"
         f"🎯 Target: {target} numbers\n🚗 Start: {vehicle}\n🔌 Source: {cfg['name']}\n"
         f"🔎 I'll keep trying the series until {target} numbers are found...\n"
         "⏳ Please wait, fetching data 🔥")

    found, checked, last_plate = [], 0, vehicle
    last_ping, offset, stop, limit_hit, net_stop = time.time(), 0, False, False, False
    while len(found) < target and checked < RC_MAX_SCAN and not stop:
        remaining = target - len(found)
        window_n = min(max(remaining * 2, 40), 400)
        window = [start + offset + i for i in range(window_n)]
        offset += window_n
        plates = [f"{prefix}{str(n).zfill(width)}" for n in window]
        results, _lim = resolve_plates(plates, cfg)
        for p in plates:  # strict ascending order -> no skip, correct pairing
            res = results.get(p)
            if res is None:  # unresolved after all retries (very bad network) -> stop honestly
                net_stop = True
                stop = True
                break
            if res.get("limit_hit"):
                limit_hit = True
                stop = True
                break
            checked += 1
            last_plate = p
            if res.get("success") and res.get("mobile_number"):
                found.append(res)
            if len(found) >= target or checked >= RC_MAX_SCAN:
                stop = True
                break
        if not stop and time.time() - last_ping > 15:
            send(chat_id, f"⏳ Checked {checked} vehicles 🚗  |  ✅ {len(found)}/{target} found 📞")
            last_ping = time.time()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["vehicle_number", "mobile_number"])
    for r in found:
        w.writerow([r["vehicle_number"], r["mobile_number"]])
    csv_bytes = buf.getvalue().encode("utf-8")

    reached = len(found) >= target
    summary = (("✅ 𝗗𝗢𝗡𝗘! Target reached 🎉" if reached else "⚠️ Sending the numbers found so far") +
               f"\n━━━━━━━━━━━━━━━━\n🎯 Target: {target}\n📞 Numbers found: {len(found)}\n"
               f"❌ Empty: {checked - len(found)}\n🚗 Range: {vehicle} → {last_plate}\n"
               f"🔢 Total checked: {checked}")
    if limit_hit:
        summary = ("🚫 𝗔𝗣𝗜 𝗟𝗜𝗠𝗜𝗧 𝗥𝗘𝗔𝗖𝗛𝗘𝗗!\n━━━━━━━━━━━━━━━━\n"
                   f"📞 Found so far: {len(found)}\n🚗 Range: {vehicle} → {last_plate}\n"
                   f"🔢 Checked: {checked}\n\n⏰ API limit used up. Try later or switch API.")
    elif net_stop:
        summary += ("\n\n⚠️ Network/proxy was unstable at this point, so I stopped here instead "
                    "of skipping any vehicle. Please run /start again to continue from the next series, "
                    "or use a more stable proxy.")

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
        send(chat_id, summary + ("" if limit_hit else "\n\n😶 No numbers in this range — admin can switch API (/apilist)."))
    return len(found)


# ----------------------------- Admin flow ------------------------------------
ADMIN_MENU = (
    "🛠️ 𝗔𝗗𝗠𝗜𝗡 𝗣𝗔𝗡𝗘𝗟\n━━━━━━━━━━━━━━━━\n"
    "🔌 /apilist\n➕ /setapi\n✅ /activate <no>\n🔎 /readjson <vehicle>\n\n"
    "👤 /setlimit <chat_id> <limit>\n📊 /userinfo <chat_id>\n⚙️ /setdefault <limit>\n"
    "📣 /setchannel <channel_id>\n👑 /addadmin <id> · /deladmin <id>\n\n🆔 /myid"
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


# ----------------------------- Message handler -------------------------------
def handle(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    frm = msg.get("from", {})
    name = " ".join(filter(None, [frm.get("first_name"), frm.get("last_name")]))
    username = frm.get("username", "")

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
            use_relay = RELAY_PREFIX and not USE_PROXY
            url = (RELAY_PREFIX + t) if use_relay else t
            hdr = {"X-Return-Format": "text"} if use_relay else {}
            try:
                raw = _extract_json(SESSION.get(url, timeout=(10, 45), headers=hdr, proxies=_pick_proxy() if USE_PROXY else None).text)
                mob = get_path(raw, cfg["mobile_path"]) if raw else None
                send(chat_id, f"🔎 {cfg['name']} — {veh}\n📞 {cfg['mobile_path']} → {mob}\n\n🧾 {json.dumps(raw, indent=2, ensure_ascii=False)[:3500]}")
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
        target = int(d); vehicle = s["series"]; SESSIONS.pop(chat_id, None)
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
        try:
            got = run_bulk(chat_id, vehicle, effective, requester=f"{name} @{username} ({chat_id})")
            if not is_admin(chat_id) and got:
                add_used(chat_id, got)
                u = user_info(chat_id)
                send(chat_id, f"📊 RC balance → used:{u['used']} left:{max(u['lim']-u['used'],0)}")
        except Exception as e:
            send(chat_id, f"⚠️ Something went wrong: {e}")
        return


# ----------------------------- Polling loop ----------------------------------
def main():
    if not TELEGRAM_BOT_TOKEN:
        sys.exit("FATAL: TELEGRAM_BOT_TOKEN env var is NOT set. Add it and restart.")
    db_init()
    tg("deleteWebhook", drop_pending_updates=False)
    me = tg("getMe")
    if not me.get("ok"):
        print("FATAL: getMe failed -> token is probably wrong:", me)
        sys.exit(1)
    print("Bot live:", me["result"].get("username"),
          "| transport:", ("proxy x" + str(len(PROXY_LIST))) if USE_PROXY else (RELAY_PREFIX or "direct"),
          "| workers:", MAX_WORKERS, flush=True)
    offset = None
    while True:
        try:
            params = {"timeout": 25, "allowed_updates": ["message"]}
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
                if m and "text" in m:
                    try:
                        handle(m)
                    except Exception as e:
                        print("handle error:", e, flush=True)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("poll error:", e, flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
