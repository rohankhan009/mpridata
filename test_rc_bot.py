# Unit tests for standalone Telegram RC bot (/app/railway_bot/rc_bot.py) - all network mocked
import importlib.util
import os
import random
import re
import tempfile
import threading
import csv
import io

import pytest

_TMP = tempfile.mkdtemp()
os.environ["RC_DB"] = os.path.join(_TMP, "rc_test.db")
os.environ["TELEGRAM_BOT_TOKEN"] = "x"
os.environ["RELAY_PREFIX"] = ""
os.environ["PROXY_LIST"] = "http://p1:1,http://p2:2"
os.environ["SUPER_ADMINS"] = ""  # allow bootstrap test
os.environ["MAX_WORKERS"] = "8"

_spec = importlib.util.spec_from_file_location("rc_bot", "/app/railway_bot/rc_bot.py")
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)

# Plates with data in the WB01AB4049.. series: every 3rd number has a mobile
def has_data(n):
    return n % 3 == 0


def mobile_for(plate):
    n = int(re.search(r"(\d+)$", plate).group(1))
    return "9" + str(n).zfill(9)[-9:]


class Resp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status


def make_get(fail_rate=0.0, limit_after=None, bad_mobile=None, seen=None):
    lock = threading.Lock()
    state = {"calls": 0, "proxies": set()}

    def fake_get(url, timeout=None, headers=None, proxies=None, **kw):
        with lock:
            state["calls"] += 1
            if proxies:
                state["proxies"].add(proxies.get("https"))
        assert "r.jina.ai" not in url
        plate = url.split("v2num=")[1]
        n = int(re.search(r"(\d+)$", plate).group(1))
        if fail_rate and random.random() < fail_rate:
            if random.random() < 0.5:
                raise ConnectionError("proxy flake")
            return Resp("<html>bad gateway</html>", random.choice([502, 503, 200]))
        if limit_after is not None and n >= limit_after:
            return Resp('{"success":false,"code":"KEY_LIMIT_REACHED","message":"API key limit reached"}')
        if seen is not None:
            with lock:
                seen.append(plate)
        if bad_mobile is not None and n in bad_mobile:
            return Resp('{"success":true,"data":{"reg_no":"%s","mobile":"%s"}}' % (plate, bad_mobile[n]))
        if has_data(n):
            return Resp('{"success":true,"data":{"reg_no":"%s","mobile":"%s"}}' % (plate, mobile_for(plate)))
        return Resp('{"success":true,"data":{"reg_no":"%s","mobile":""}}' % plate)

    return fake_get, state


@pytest.fixture(autouse=True)
def env(monkeypatch):
    rc.db_init()
    with rc._lock:
        rc._db.execute("DELETE FROM admins")
        rc._db.execute("DELETE FROM users")
        rc._db.execute("DELETE FROM settings")
        rc._db.commit()
    rc.SESSIONS.clear()
    sent, docs = [], []
    monkeypatch.setattr(rc, "send", lambda cid, text: sent.append((cid, text)) or {"ok": True})
    monkeypatch.setattr(rc, "send_document",
                        lambda cid, fn, content, caption="": docs.append((cid, fn, content, caption)) or {"ok": True})
    monkeypatch.setattr(rc.time, "sleep", lambda s: None)
    return sent, docs


def parse_csv(content):
    rows = list(csv.reader(io.StringIO(content.decode())))
    assert rows[0] == ["vehicle_number", "mobile_number"]
    return rows[1:]


def expected_plates(start, count):
    out, n = [], start
    while len(out) < count:
        if has_data(n):
            out.append(f"WB01AB{n}")
        n += 1
    return out


# --- config sanity
def test_proxy_mode_enabled():
    assert rc.USE_PROXY is True
    assert rc.RELAY_PREFIX == ""
    assert len(rc.PROXY_LIST) == 2


# --- NO-SKIP under flaky transport (55% failure), repeated runs
@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_no_skip_flaky(env, monkeypatch, seed):
    random.seed(seed)
    sent, docs = env
    fg, st = make_get(fail_rate=0.55)
    monkeypatch.setattr(rc.SESSION, "get", fg)
    got = rc.run_bulk(111, "WB01AB4049", 15)
    assert got == 15
    assert len(docs) == 1
    rows = parse_csv(docs[0][2])
    plates = [r[0] for r in rows]
    assert plates == expected_plates(4049, 15)  # all, ascending, no gaps
    assert len(set(plates)) == 15
    for p, m in rows:  # exact pairing
        assert m == mobile_for(p)
    assert st["proxies"] == {"http://p1:1", "http://p2:2"}


# --- lookup marks failures as error (not empty)
def test_lookup_error_flag(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("x")
    monkeypatch.setattr(rc.SESSION, "get", boom)
    r = rc.lookup("WB01AB4050", rc.active_config())
    assert r["error"] is True and r["success"] is False and r["mobile_number"] == ""


def test_lookup_success_and_empty(monkeypatch):
    fg, _ = make_get()
    monkeypatch.setattr(rc.SESSION, "get", fg)
    cfg = rc.active_config()
    r = rc.lookup("WB01AB4050", cfg)
    assert r["success"] and r["mobile_number"] == mobile_for("WB01AB4050") and not r["error"]
    r2 = rc.lookup("WB01AB4049", cfg)
    assert not r2["success"] and not r2["error"] and not r2["limit_hit"]


# --- VALID MOBILE FILTER
@pytest.mark.parametrize("val", ["Not Available", "0", "", "12345", "1234567890", "NA"])
def test_invalid_mobiles_rejected(val):
    assert rc.valid_mobile(val) is None


@pytest.mark.parametrize("val,exp", [("9876543210", "9876543210"), ("+91 98765 43210", "9876543210"),
                                     ("6000000000", "6000000000")])
def test_valid_mobiles(val, exp):
    assert rc.valid_mobile(val) == exp


def test_bad_mobile_not_written(env, monkeypatch):
    sent, docs = env
    # 4050 & 4053 would normally have data; make them invalid
    fg, _ = make_get(bad_mobile={4050: "Not Available", 4053: "0"})
    monkeypatch.setattr(rc.SESSION, "get", fg)
    got = rc.run_bulk(1, "WB01AB4049", 3)
    assert got == 3
    plates = [r[0] for r in parse_csv(docs[0][2])]
    assert "WB01AB4050" not in plates and "WB01AB4053" not in plates
    assert plates == ["WB01AB4056", "WB01AB4059", "WB01AB4062"]


# --- LIMIT-HIT
def test_limit_hit_stops(env, monkeypatch):
    sent, docs = env
    fg, st = make_get(limit_after=4060)
    monkeypatch.setattr(rc.SESSION, "get", fg)
    got = rc.run_bulk(1, "WB01AB4049", 50)
    # numbers with data in 4049..4059 => 4050,4053,4056,4059
    assert got == 4
    assert len(docs) == 1
    assert "API limit used up" in docs[0][3]
    assert [r[0] for r in parse_csv(docs[0][2])] == ["WB01AB4050", "WB01AB4053", "WB01AB4056", "WB01AB4059"]
    assert st["calls"] < 500  # stopped, didn't scan forever


def test_limit_hit_no_results(env, monkeypatch):
    sent, docs = env
    fg, _ = make_get(limit_after=0)
    monkeypatch.setattr(rc.SESSION, "get", fg)
    assert rc.run_bulk(1, "WB01AB4049", 10) == 0
    assert not docs
    assert any("API limit used up" in t for _, t in sent)


def test_lookup_limit_flag(monkeypatch):
    fg, _ = make_get(limit_after=0)
    monkeypatch.setattr(rc.SESSION, "get", fg)
    r = rc.lookup("WB01AB4049", rc.active_config())
    assert r["limit_hit"] is True


# --- PER-USER LIMIT / ADMIN
def msg(cid, text):
    return {"chat": {"id": cid}, "text": text, "from": {"first_name": "T", "username": "u%d" % cid}}


def test_admin_bootstrap_and_limits(env, monkeypatch):
    sent, docs = env
    calls = []
    monkeypatch.setattr(rc, "run_bulk", lambda cid, v, t, requester="": calls.append((cid, v, t)) or t)
    assert rc.admin_count() == 0
    rc.handle(msg(100, "/admin"))
    assert rc.is_admin(100)
    assert "ADMIN" in sent[-1][1]
    rc.handle(msg(200, "/admin"))
    assert not rc.is_admin(200) and "Not an admin" in sent[-1][1]

    # user 200 with default limit 0 -> blocked
    rc.handle(msg(200, "/start")); rc.handle(msg(200, "WB01AB4049")); rc.handle(msg(200, "5"))
    assert not calls and "limit is over" in sent[-1][1]

    # non-admin cannot setlimit
    rc.handle(msg(200, "/setlimit 200 999"))
    assert rc.user_info(200)["lim"] == 0

    rc.handle(msg(100, "/setlimit 200 10"))
    assert rc.user_info(200)["lim"] == 10
    assert any(c == 200 and "unlocked 10" in t for c, t in sent)

    rc.handle(msg(200, "/start")); rc.handle(msg(200, "WB01AB4049")); rc.handle(msg(200, "25"))
    assert calls[-1] == (200, "WB01AB4049", 10)  # capped
    assert any("only 10 left" in t for _, t in sent)
    assert rc.user_info(200)["used"] == 10

    n = len(calls)
    rc.handle(msg(200, "/start")); rc.handle(msg(200, "WB01AB4049")); rc.handle(msg(200, "1"))
    assert len(calls) == n and "limit is over" in sent[-1][1]

    # admin unlimited, not charged
    rc.handle(msg(100, "/start")); rc.handle(msg(100, "WB01AB4049")); rc.handle(msg(100, "5000"))
    assert calls[-1] == (100, "WB01AB4049", 5000)
    rc.handle(msg(100, "/myid"))
    assert "unlimited" in sent[-1][1]


def test_partial_found_charges_only_found(env, monkeypatch):
    sent, docs = env
    rc.add_admin(1)
    rc.set_user_limit(300, 10)
    monkeypatch.setattr(rc, "run_bulk", lambda cid, v, t, requester="": 3)
    rc.handle(msg(300, "/start")); rc.handle(msg(300, "WB01AB4049")); rc.handle(msg(300, "10"))
    assert rc.user_info(300)["used"] == 3
    rc.handle(msg(300, "/myid"))
    assert "left: 7" in sent[-1][1]


def test_setlimit_preserves_used(env):
    rc.set_user_limit(400, 10); rc.add_used(400, 4); rc.set_user_limit(400, 20)
    u = rc.user_info(400)
    assert u["lim"] == 20 and u["used"] == 4


# --- LIMIT-HIT + flaky interaction: an errored plate in the same round as a limit hit
def test_limit_hit_does_not_silently_skip_errored(env, monkeypatch):
    sent, docs = env
    base, _ = make_get(limit_after=4060)
    cnt = {"n": 0}
    def fg(url, **kw):
        if url.endswith("WB01AB4050"):
            cnt["n"] += 1
            if cnt["n"] <= 3:  # fails all 3 attempts in round 1 only
                raise ConnectionError("flake")
        return base(url, **kw)
    monkeypatch.setattr(rc.SESSION, "get", fg)
    rc.run_bulk(1, "WB01AB4049", 50)
    plates = [r[0] for r in parse_csv(docs[0][2])] if docs else []
    assert "WB01AB4050" in plates, "4050 has data but was marked empty because limit hit in same round"


# ================= iteration_4: re-verification after fixes =================
def run_with_timeout(fn, secs=20):
    """Run fn in a daemon thread; fail if it does not finish (hang/deadlock/runaway)."""
    box = {}
    def t():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa
            box["e"] = e
    th = threading.Thread(target=t, daemon=True)
    th.start()
    th.join(secs)
    assert not th.is_alive(), f"TIMEOUT after {secs}s (hang / infinite loop)"
    if "e" in box:
        raise box["e"]
    return box.get("v")


# --- REGRESSION: deadlock in get_user stays fixed
def test_lock_is_reentrant():
    assert type(rc._lock).__name__ in ("RLock", "_RLock")


def test_new_user_start_myid_no_hang(env):
    sent, _ = env
    rc.add_admin(1)
    rc.set_setting("default_limit", 7)
    run_with_timeout(lambda: rc.handle(msg(5551, "/start")), 5)
    run_with_timeout(lambda: rc.handle(msg(5552, "/myid")), 5)
    assert rc.user_info(5551)["lim"] == 7 and rc.user_info(5552)["lim"] == 7
    assert "limit: 7" in sent[-1][1]
    u = run_with_timeout(lambda: rc.get_user(5553, "N", "un"), 5)
    assert u["chat_id"] == 5553 and u["lim"] == 7


def test_get_user_concurrent_threads_no_hang():
    def work():
        ths = [threading.Thread(target=rc.get_user, args=(9000 + i, "n", "u")) for i in range(30)]
        [t.start() for t in ths]; [t.join(5) for t in ths]
        return all(not t.is_alive() for t in ths)
    assert run_with_timeout(work, 10) is True


def make_get_v6(fail_rate, limit_from=None, exc_only=True):
    """Mock parsing number via v[6:] per review note; flaky BEFORE limit check."""
    lock = threading.Lock()
    st = {"calls": 0, "maxn": 0}
    def fg(url, timeout=None, headers=None, proxies=None, **kw):
        v = url.split("v2num=")[1]
        n = int(v[6:])
        with lock:
            st["calls"] += 1
            st["maxn"] = max(st["maxn"], n)
        if random.random() < fail_rate and (limit_from is None or n < limit_from):
            if exc_only or random.random() < 0.5:
                raise ConnectionError("flaky")
            return Resp("oops", random.choice([502, 503, 504, 429]))
        if limit_from is not None and n >= limit_from:
            return Resp('{"success":false,"code":"KEY_LIMIT_REACHED"}')
        if has_data(n):
            return Resp('{"success":true,"data":{"reg_no":"%s","mobile":"%s"}}' % (v, mobile_for(v)))
        return Resp('{"success":true,"data":{"reg_no":"%s","mobile":"Not Available"}}' % v)
    return fg, st


# --- NO-SKIP pure flaky 60-70%
@pytest.mark.parametrize("seed,rate", [(11, 0.6), (12, 0.65), (13, 0.7), (14, 0.7), (15, 0.6)])
def test_no_skip_pure_flaky_60_70(env, monkeypatch, seed, rate):
    random.seed(seed)
    sent, docs = env
    fg, st = make_get_v6(rate, exc_only=(seed % 2 == 0))
    monkeypatch.setattr(rc.SESSION, "get", fg)
    got = run_with_timeout(lambda: rc.run_bulk(1, "WB01AB4049", 15), 30)
    assert got == 15
    rows = parse_csv(docs[0][2])
    plates = [r[0] for r in rows]
    assert plates == expected_plates(4049, 15)
    assert plates == sorted(plates) and len(set(plates)) == 15
    assert all(m == mobile_for(p) for p, m in rows)


# --- NO-SKIP with limit boundary + 50-60% flaky below N
@pytest.mark.parametrize("seed,rate,N", [(21, 0.5, 4070), (22, 0.55, 4060), (23, 0.6, 4100),
                                         (24, 0.6, 4051), (25, 0.55, 4049), (26, 0.6, 4200)])
def test_no_skip_with_limit_boundary(env, monkeypatch, seed, rate, N):
    random.seed(seed)
    sent, docs = env
    fg, st = make_get_v6(rate, limit_from=N)
    monkeypatch.setattr(rc.SESSION, "get", fg)
    got = run_with_timeout(lambda: rc.run_bulk(1, "WB01AB4049", 500), 60)
    exp = [f"WB01AB{n}" for n in range(4049, N) if has_data(n)]
    assert got == len(exp)
    if exp:
        rows = parse_csv(docs[0][2])
        assert [r[0] for r in rows] == exp, "data plate skipped below limit"
        assert all(m == mobile_for(p) for p, m in rows)
        assert "API LIMIT" in docs[0][3] or "API limit used up" in docs[0][3]
        cap = docs[0][3]
    else:
        assert not docs
        cap = sent[-1][1]
    assert "API limit used up" in cap
    # summary counts only plates before the boundary
    assert f"Checked: {N - 4049}" in cap, cap
    if N > 4049:
        assert f"WB01AB{N-1}" in cap
    # no runaway: never scanned far beyond one window past N
    assert st["maxn"] < N + 1000, st["maxn"]


# --- limit boundary mid-window, data plates right before boundary flaky in many rounds
def test_limit_with_stubborn_flaky_before_boundary(env, monkeypatch):
    sent, docs = env
    base, _ = make_get_v6(0.0, limit_from=4060)
    cnt = {}
    def fg(url, **kw):
        v = url.split("v2num=")[1]
        if v in ("WB01AB4050", "WB01AB4059"):
            cnt[v] = cnt.get(v, 0) + 1
            if cnt[v] <= 3 * 5:  # fail 5 full rounds
                raise ConnectionError("x")
        return base(url, **kw)
    monkeypatch.setattr(rc.SESSION, "get", fg)
    got = run_with_timeout(lambda: rc.run_bulk(1, "WB01AB4049", 50), 20)
    assert got == 4
    assert [r[0] for r in parse_csv(docs[0][2])] == ["WB01AB4050", "WB01AB4053", "WB01AB4056", "WB01AB4059"]


# --- always-failing network: must terminate (no infinite loop)
def test_always_failing_terminates(env, monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("down")
    monkeypatch.setattr(rc.SESSION, "get", boom)
    monkeypatch.setattr(rc, "RC_MAX_SCAN", 200)
    got = run_with_timeout(lambda: rc.run_bulk(1, "WB01AB4049", 5), 30)
    assert got == 0
