#!/usr/bin/env python3
"""24-urenloop: minimale estafette-server zonder externe dependencies.

Rollen:
  teller  - drukt op 1 knop bij elke wissel van de stok (gsm)
  admin   - beheert de wachtlijst, gangen, start/stop (laptop); mag ook tellen

Alle data staat in één SQLite-bestand (WAL, synchronous=FULL) met periodieke backups.
"""
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

DB_PATH = os.environ.get("DB_PATH", "data/24urenloop.db")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
TELLER_PIN = os.environ.get("TELLER_PIN", "")
ADMIN_PIN = os.environ.get("ADMIN_PIN", "")
MIN_LAP_MS = int(os.environ.get("MIN_LAP_SECONDS", "5")) * 1000
UNDO_MS = int(os.environ.get("UNDO_SECONDS", "60")) * 1000
BACKUP_EVERY_S = int(os.environ.get("BACKUP_MINUTES", "5")) * 60
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "100"))
BACKUP_DIR = os.environ.get("BACKUP_DIR") or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")
# Map waarin het publieke scorebord (index.html + scorebord.json) periodiek wordt weggeschreven,
# om met rsync naar de thuisserver te pushen. Leeg = uit.
PUBLIC_DIR = os.environ.get("PUBLIC_DIR", "")
PUBLIC_EVERY_S = max(1, int(os.environ.get("PUBLIC_SECONDS", "10")))
COOKIE_MAX_AGE = 7 * 24 * 3600
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Hoe ver een door de gsm meegegeven klik-tijdstip mag afwijken van de servertijd.
# Zo telt bij slecht netwerk het moment van klikken, niet het moment van aankomen.
CLIENT_TS_MAX_PAST_MS = 10 * 60 * 1000
CLIENT_TS_MAX_FUTURE_MS = 2 * 1000

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gangs(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS queue(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    gang TEXT NOT NULL,
    pos INTEGER NOT NULL,
    added_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS laps(
    id INTEGER PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE,
    name TEXT NOT NULL,
    gang TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER,
    queued_ms INTEGER
);
CREATE TABLE IF NOT EXISTS passes(client_id TEXT PRIMARY KEY, lap_id INTEGER, received_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY, at_ms INTEGER NOT NULL, role TEXT, action TEXT NOT NULL, detail TEXT);
"""


def now_ms():
    return int(time.time() * 1000)


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class DB:
    """Eén connectie, één lock: eenvoudig en strikt sequentieel (belasting is minimaal)."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    @contextmanager
    def read(self):
        with self.lock:
            yield self.conn

    def backup(self, dest):
        with self.lock:
            target = sqlite3.connect(dest)
            try:
                self.conn.backup(target)
            finally:
                target.close()


db = DB(DB_PATH)


# ---------------------------------------------------------------- helpers

def get_setting(c, key, default=None):
    row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(c, key, value):
    c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, str(value)))


def log(c, role, action, detail=None):
    c.execute("INSERT INTO log(at_ms,role,action,detail) VALUES(?,?,?,?)",
              (now_ms(), role, action, json.dumps(detail, ensure_ascii=False) if detail is not None else None))


def clean_text(value, field, max_len=80, allow_empty=False):
    if not isinstance(value, str):
        raise ApiError(400, f"{field} ontbreekt")
    value = " ".join(value.split())
    if not value and not allow_empty:
        raise ApiError(400, f"{field} is leeg")
    if len(value) > max_len:
        raise ApiError(400, f"{field} is te lang")
    return value


def require_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, f"{field} ongeldig")
    return value


def renumber_queue(c, ids):
    for i, qid in enumerate(ids):
        c.execute("UPDATE queue SET pos=? WHERE id=?", (i, qid))


def queue_ids(c):
    return [r["id"] for r in c.execute("SELECT id FROM queue ORDER BY pos, id")]


def current_lap(c):
    return c.execute("SELECT * FROM laps WHERE end_ms IS NULL ORDER BY seq DESC LIMIT 1").fetchone()


def lap_dict(r):
    if r is None:
        return None
    return {"id": r["id"], "seq": r["seq"], "name": r["name"], "gang": r["gang"],
            "start_ms": r["start_ms"], "end_ms": r["end_ms"]}


def public_state(c):
    st = get_setting(c, "state", "idle")
    cur = current_lap(c)
    nxt = c.execute("SELECT id,name,gang FROM queue ORDER BY pos, id LIMIT 2").fetchall()
    prev = None
    if cur is not None:
        prev = c.execute("SELECT * FROM laps WHERE seq=?", (cur["seq"] - 1,)).fetchone()
    else:
        prev = c.execute("SELECT * FROM laps ORDER BY seq DESC LIMIT 1").fetchone()
    done = c.execute("SELECT COUNT(*) n FROM laps WHERE end_ms IS NOT NULL").fetchone()["n"]
    qlen = c.execute("SELECT COUNT(*) n FROM queue").fetchone()["n"]
    start = get_setting(c, "start_ms")
    end = get_setting(c, "end_ms")
    return {
        "now": now_ms(),
        "state": st,
        "start_ms": int(start) if start else None,
        "end_ms": int(end) if end else None,
        "current": lap_dict(cur),
        "previous": lap_dict(prev),
        "next": [{"id": r["id"], "name": r["name"], "gang": r["gang"]} for r in nxt],
        "queue_length": qlen,
        "laps_done": done,
        "min_lap_ms": MIN_LAP_MS,
        "undo_ms": UNDO_MS,
    }


def admin_state(c):
    s = public_state(c)
    s["queue"] = [dict(r) for r in c.execute("SELECT id,name,gang,added_ms FROM queue ORDER BY pos, id")]
    s["gangs"] = [dict(r) for r in c.execute("SELECT id,name FROM gangs ORDER BY name COLLATE NOCASE")]
    s["laps"] = [lap_dict(r) for r in c.execute("SELECT * FROM laps ORDER BY seq DESC LIMIT 50")]
    return s


# ---------------------------------------------------------------- acties

def do_start(c, role):
    if get_setting(c, "state", "idle") != "idle":
        raise ApiError(409, "Het event is al gestart")
    first = c.execute("SELECT * FROM queue ORDER BY pos, id LIMIT 1").fetchone()
    if first is None:
        raise ApiError(409, "Zet eerst minstens één loper in de wachtlijst")
    t = now_ms()
    c.execute("DELETE FROM queue WHERE id=?", (first["id"],))
    c.execute("INSERT INTO laps(seq,name,gang,start_ms,queued_ms) VALUES(1,?,?,?,?)",
              (first["name"], first["gang"], t, first["added_ms"]))
    set_setting(c, "state", "running")
    set_setting(c, "start_ms", t)
    log(c, role, "start", {"at": t, "runner": first["name"]})


def do_stop(c, role):
    if get_setting(c, "state", "idle") != "running":
        raise ApiError(409, "Het event loopt niet")
    t = now_ms()
    cur = current_lap(c)
    if cur is not None:
        c.execute("UPDATE laps SET end_ms=? WHERE id=?", (max(t, cur["start_ms"]), cur["id"]))
    set_setting(c, "state", "finished")
    set_setting(c, "end_ms", t)
    log(c, role, "stop", {"at": t})


def do_reset(c, role):
    if get_setting(c, "state", "idle") == "running":
        raise ApiError(409, "Stop eerst het event")
    n = c.execute("SELECT COUNT(*) n FROM laps").fetchone()["n"]
    c.execute("DELETE FROM laps")
    c.execute("DELETE FROM passes")
    c.execute("DELETE FROM settings WHERE key IN ('state','start_ms','end_ms')")
    log(c, role, "reset", {"laps_deleted": n})


def do_pass(c, role, client_id, client_ts):
    """Stok doorgegeven: huidige ronde afsluiten, volgende loper uit de wachtlijst laten starten.

    Idempotent per client_id, zodat een herhaalde (retry) aanvraag nooit dubbel telt.
    """
    received = now_ms()
    done = c.execute("SELECT lap_id FROM passes WHERE client_id=?", (client_id,)).fetchone()
    if done is not None:
        return {"duplicate": True}
    if get_setting(c, "state", "idle") != "running":
        raise ApiError(409, "Het event loopt niet")
    cur = current_lap(c)
    if cur is None:
        raise ApiError(409, "Er loopt niemand")

    ts = received
    if isinstance(client_ts, int) and not isinstance(client_ts, bool):
        if received - CLIENT_TS_MAX_PAST_MS <= client_ts <= received + CLIENT_TS_MAX_FUTURE_MS:
            ts = min(client_ts, received)
    if ts - cur["start_ms"] < MIN_LAP_MS:
        raise ApiError(409, "Te snel na de vorige wissel (dubbele klik?)")

    nxt = c.execute("SELECT * FROM queue ORDER BY pos, id LIMIT 1").fetchone()
    c.execute("UPDATE laps SET end_ms=? WHERE id=?", (ts, cur["id"]))
    if nxt is not None:
        c.execute("DELETE FROM queue WHERE id=?", (nxt["id"],))
        name, gang, queued = nxt["name"], nxt["gang"], nxt["added_ms"]
    else:
        # Mag niet voorkomen, maar een wissel mag nooit verloren gaan: beheer vult de naam later in.
        name, gang, queued = "?", "", None
    cur_id = c.execute("INSERT INTO laps(seq,name,gang,start_ms,queued_ms) VALUES(?,?,?,?,?)",
                       (cur["seq"] + 1, name, gang, ts, queued)).lastrowid
    c.execute("INSERT INTO passes(client_id,lap_id,received_ms) VALUES(?,?,?)", (client_id, cur_id, received))
    log(c, role, "pass", {"lap": cur["seq"], "runner": cur["name"], "ms": ts - cur["start_ms"],
                          "next": name, "ts": ts, "received": received})
    return {"duplicate": False}


def do_undo(c, role, lap_id):
    """Laatste wissel terugdraaien: huidige loper terug vooraan de wachtlijst, vorige ronde terug open."""
    if get_setting(c, "state", "idle") != "running":
        raise ApiError(409, "Het event loopt niet")
    cur = current_lap(c)
    if cur is None or cur["id"] != lap_id:
        raise ApiError(409, "Er is intussen al iets veranderd, vernieuw de pagina")
    prev = c.execute("SELECT * FROM laps WHERE seq=?", (cur["seq"] - 1,)).fetchone()
    if prev is None:
        raise ApiError(409, "Er is geen wissel om ongedaan te maken")
    if role != "admin" and now_ms() - cur["start_ms"] > UNDO_MS:
        raise ApiError(409, "Te laat om ongedaan te maken, vraag het aan de wachtlijstbeheerder")
    c.execute("DELETE FROM laps WHERE id=?", (cur["id"],))
    c.execute("UPDATE laps SET end_ms=NULL WHERE id=?", (prev["id"],))
    if cur["name"] != "?":
        ids = queue_ids(c)
        qid = c.execute("INSERT INTO queue(name,gang,pos,added_ms) VALUES(?,?,0,?)",
                        (cur["name"], cur["gang"], cur["queued_ms"] or now_ms())).lastrowid
        renumber_queue(c, [qid] + ids)
    log(c, role, "undo", {"removed_lap": cur["seq"], "runner": cur["name"], "reopened": prev["name"]})


def queue_add(c, role, name, gang):
    pos = c.execute("SELECT COALESCE(MAX(pos),-1)+1 p FROM queue").fetchone()["p"]
    qid = c.execute("INSERT INTO queue(name,gang,pos,added_ms) VALUES(?,?,?,?)",
                    (name, gang, pos, now_ms())).lastrowid
    log(c, role, "queue_add", {"id": qid, "name": name, "gang": gang})


def queue_move(c, role, qid, before):
    ids = queue_ids(c)
    if qid not in ids:
        raise ApiError(409, "Die loper staat niet (meer) in de wachtlijst")
    ids.remove(qid)
    if before is None:
        ids.append(qid)
    else:
        if before not in ids:
            raise ApiError(409, "De wachtlijst is intussen veranderd, probeer opnieuw")
        ids.insert(ids.index(before), qid)
    renumber_queue(c, ids)
    log(c, role, "queue_move", {"id": qid, "before": before})


# ---------------------------------------------------------------- statistieken

def stats(c):
    dur = "(end_ms - start_ms)"
    total = c.execute(f"SELECT COUNT(*) n, AVG({dur}) avg, MIN({dur}) best "
                      "FROM laps WHERE end_ms IS NOT NULL").fetchone()
    gangs = c.execute(f"SELECT gang, COUNT(*) n, SUM({dur}) total, AVG({dur}) avg, MIN({dur}) best, "
                      "COUNT(DISTINCT name) runners FROM laps WHERE end_ms IS NOT NULL "
                      "GROUP BY gang ORDER BY n DESC, avg").fetchall()
    runners = c.execute(f"SELECT name, gang, COUNT(*) n, AVG({dur}) avg, MIN({dur}) best "
                        "FROM laps WHERE end_ms IS NOT NULL GROUP BY name, gang ORDER BY best").fetchall()
    start = get_setting(c, "start_ms")
    hours = []
    if start:
        hours = c.execute("SELECT (end_ms - ?) / 3600000 hour, COUNT(*) n FROM laps "
                          "WHERE end_ms IS NOT NULL GROUP BY hour ORDER BY hour", (int(start),)).fetchall()
    return {
        "total": dict(total),
        "gangs": [dict(r) for r in gangs],
        "runners": [dict(r) for r in runners],
        "hours": [dict(r) for r in hours],
    }


def scoreboard(c):
    """Alleen-lezen momentopname voor het publieke scorebord: geen id's, geen wachtlijst."""
    s = public_state(c)
    cur = s["current"]
    nxt = c.execute("SELECT name,gang FROM queue ORDER BY pos, id LIMIT 3").fetchall()
    recent = c.execute("SELECT seq,name,gang,start_ms,end_ms FROM laps WHERE end_ms IS NOT NULL "
                       "ORDER BY seq DESC LIMIT 10").fetchall()
    return {
        "generated_ms": s["now"],
        "state": s["state"],
        "start_ms": s["start_ms"],
        "end_ms": s["end_ms"],
        "laps_done": s["laps_done"],
        "current": {k: cur[k] for k in ("seq", "name", "gang", "start_ms")} if cur else None,
        "next": [dict(r) for r in nxt],
        "recent": [dict(r) for r in recent],
        "stats": stats(c),
    }


def fmt_ts(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if ms else ""


def laps_csv(c):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["ronde", "naam", "gang", "start", "einde", "duur_s", "wachttijd_s", "start_ms", "einde_ms"])
    for r in c.execute("SELECT * FROM laps ORDER BY seq"):
        dur = (r["end_ms"] - r["start_ms"]) / 1000 if r["end_ms"] else ""
        wait = (r["start_ms"] - r["queued_ms"]) / 1000 if r["queued_ms"] else ""
        w.writerow([r["seq"], r["name"], r["gang"], fmt_ts(r["start_ms"]), fmt_ts(r["end_ms"]),
                    dur, wait, r["start_ms"], r["end_ms"] or ""])
    return "﻿" + buf.getvalue()  # BOM zodat Excel UTF-8 herkent


# ---------------------------------------------------------------- authenticatie

def get_secret():
    env = os.environ.get("SECRET")
    if env:
        return env.encode()
    with db.tx() as c:
        s = get_setting(c, "secret")
        if not s:
            s = secrets.token_hex(32)
            set_setting(c, "secret", s)
    return s.encode()


SECRET = get_secret()


def sign(role):
    return role + "." + hmac.new(SECRET, role.encode(), hashlib.sha256).hexdigest()


def verify(token):
    if not token or "." not in token:
        return None
    role = token.split(".", 1)[0]
    if role in ("admin", "teller") and hmac.compare_digest(token, sign(role)):
        return role
    return None


_fail_lock = threading.Lock()
_fails = []  # tijdstippen van mislukte logins (globaal, tegen brute force)


def login_allowed():
    with _fail_lock:
        cutoff = time.time() - 60
        _fails[:] = [t for t in _fails if t > cutoff]
        return len(_fails) < 20


def login_failed():
    with _fail_lock:
        _fails.append(time.time())


# ---------------------------------------------------------------- HTTP

PAGES = {"/login": ("login.html", None), "/scorebord": ("scorebord.html", None), "/teller": ("teller.html", "teller"), "/beheer": ("beheer.html", "admin")}
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".html": "text/html; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}


class Handler(BaseHTTPRequestHandler):
    server_version = "24urenloop"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("ACCESS_LOG"):
            super().log_message(fmt, *args)

    # -- utils
    def role(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return verify(cookie["auth"].value) if "auth" in cookie else None

    def send(self, status, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, status, obj, headers=None):
        self.send(status, json.dumps(obj, ensure_ascii=False), headers=headers)

    def redirect(self, location, headers=None):
        h = {"Location": location}
        h.update(headers or {})
        self.send(303, b"", "text/plain", h)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            raise ApiError(413, "Te groot")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "Ongeldige JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "Ongeldige JSON")
        return data

    def need(self, role, wanted):
        if role is None:
            raise ApiError(401, "Niet ingelogd")
        if wanted == "admin" and role != "admin":
            raise ApiError(403, "Enkel voor de wachtlijstbeheerder")

    def secure(self):
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def cookie_header(self, value, max_age):
        parts = [f"auth={value}", "Path=/", "HttpOnly", "SameSite=Strict", f"Max-Age={max_age}"]
        if self.secure():
            parts.append("Secure")
        return "; ".join(parts)

    # -- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.handle_get()
        except ApiError as e:
            self.json(e.status, {"error": e.message})
        except Exception as e:  # nooit de server laten crashen
            print("FOUT", self.path, repr(e), file=sys.stderr, flush=True)
            self.json(500, {"error": "Interne fout"})

    def handle_get(self):
        path = urlparse(self.path).path
        role = self.role()
        if path == "/":
            return self.redirect({"admin": "/beheer", "teller": "/teller"}.get(role, "/login"))
        if path == "/healthz":
            return self.send(200, "ok", "text/plain")
        if path in PAGES:
            fname, wanted = PAGES[path]
            if wanted and (role is None or (wanted == "admin" and role != "admin")):
                return self.redirect("/login?next=" + path)
            return self.static(fname)
        if path.startswith("/static/"):
            return self.static(path[len("/static/"):])
        if path == "/scorebord.json":
            with db.read() as c:
                return self.json(200, scoreboard(c))
        if path == "/api/state":
            self.need(role, "teller")
            with db.read() as c:
                s = public_state(c)
            s["role"] = role
            return self.json(200, s)
        if path == "/api/admin":
            self.need(role, "admin")
            with db.read() as c:
                return self.json(200, admin_state(c))
        if path == "/api/stats":
            self.need(role, "admin")
            with db.read() as c:
                return self.json(200, stats(c))
        if path == "/export/laps.csv":
            self.need(role, "admin")
            with db.read() as c:
                data = laps_csv(c)
            fn = "24urenloop-" + datetime.now().strftime("%Y%m%d-%H%M") + ".csv"
            return self.send(200, data, "text/csv; charset=utf-8",
                             {"Content-Disposition": f'attachment; filename="{fn}"'})
        raise ApiError(404, "Niet gevonden")

    def static(self, rel):
        full = os.path.realpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
            raise ApiError(404, "Niet gevonden")
        ctype = STATIC_TYPES.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            self.send(200, f.read(), ctype)

    # -- POST
    def do_POST(self):
        try:
            self.handle_post()
        except ApiError as e:
            self.json(e.status, {"error": e.message})
        except Exception as e:
            print("FOUT", self.path, repr(e), file=sys.stderr, flush=True)
            self.json(500, {"error": "Interne fout"})

    def handle_post(self):
        path = urlparse(self.path).path
        data = self.body()
        role = self.role()

        if path == "/api/login":
            if not login_allowed():
                raise ApiError(429, "Te veel pogingen, wacht een minuut")
            pin = data.get("pin")
            new_role = None
            if isinstance(pin, str) and pin:
                if hmac.compare_digest(pin.encode(), ADMIN_PIN.encode()):
                    new_role = "admin"
                elif hmac.compare_digest(pin.encode(), TELLER_PIN.encode()):
                    new_role = "teller"
            if new_role is None:
                login_failed()
                time.sleep(1)
                raise ApiError(401, "Verkeerde code")
            return self.json(200, {"role": new_role},
                             {"Set-Cookie": self.cookie_header(sign(new_role), COOKIE_MAX_AGE)})
        if path == "/api/logout":
            return self.json(200, {}, {"Set-Cookie": self.cookie_header("", 0)})

        # Alles hieronder vereist een login.
        self.need(role, "teller")

        if path == "/api/pass":
            client_id = clean_text(data.get("id"), "id", 64)
            with db.tx() as c:
                result = do_pass(c, role, client_id, data.get("ts"))
                result["state"] = public_state(c)
            return self.json(200, result)
        if path == "/api/undo":
            lap_id = require_int(data.get("lap_id"), "lap_id")
            with db.tx() as c:
                do_undo(c, role, lap_id)
                return self.json(200, {"state": public_state(c)})

        self.need(role, "admin")
        with db.tx() as c:
            if path == "/api/admin/start":
                do_start(c, role)
            elif path == "/api/admin/stop":
                do_stop(c, role)
            elif path == "/api/admin/reset":
                if data.get("confirm") != "RESET":
                    raise ApiError(400, "Bevestiging ontbreekt")
                do_reset(c, role)
            elif path == "/api/admin/queue/add":
                name = clean_text(data.get("name"), "Naam")
                gang = clean_text(data.get("gang"), "Gang")
                queue_add(c, role, name, gang)
            elif path == "/api/admin/queue/update":
                qid = require_int(data.get("id"), "id")
                name = clean_text(data.get("name"), "Naam")
                gang = clean_text(data.get("gang"), "Gang")
                if c.execute("UPDATE queue SET name=?, gang=? WHERE id=?", (name, gang, qid)).rowcount == 0:
                    raise ApiError(409, "Die loper staat niet (meer) in de wachtlijst")
                log(c, role, "queue_update", {"id": qid, "name": name, "gang": gang})
            elif path == "/api/admin/queue/delete":
                qid = require_int(data.get("id"), "id")
                row = c.execute("SELECT name FROM queue WHERE id=?", (qid,)).fetchone()
                if row is None:
                    raise ApiError(409, "Die loper staat niet (meer) in de wachtlijst")
                c.execute("DELETE FROM queue WHERE id=?", (qid,))
                log(c, role, "queue_delete", {"id": qid, "name": row["name"]})
            elif path == "/api/admin/queue/move":
                qid = require_int(data.get("id"), "id")
                before = data.get("before")
                if before is not None:
                    before = require_int(before, "before")
                queue_move(c, role, qid, before)
            elif path == "/api/admin/gangs/add":
                name = clean_text(data.get("name"), "Gang")
                try:
                    c.execute("INSERT INTO gangs(name) VALUES(?)", (name,))
                except sqlite3.IntegrityError:
                    raise ApiError(409, "Die gang bestaat al")
                log(c, role, "gang_add", {"name": name})
            elif path == "/api/admin/gangs/delete":
                gid = require_int(data.get("id"), "id")
                c.execute("DELETE FROM gangs WHERE id=?", (gid,))
                log(c, role, "gang_delete", {"id": gid})
            elif path == "/api/admin/laps/update":
                lid = require_int(data.get("id"), "id")
                name = clean_text(data.get("name"), "Naam")
                gang = clean_text(data.get("gang"), "Gang", allow_empty=True)
                old = c.execute("SELECT name,gang FROM laps WHERE id=?", (lid,)).fetchone()
                if old is None:
                    raise ApiError(404, "Ronde niet gevonden")
                c.execute("UPDATE laps SET name=?, gang=? WHERE id=?", (name, gang, lid))
                log(c, role, "lap_update", {"id": lid, "old": dict(old), "name": name, "gang": gang})
            else:
                raise ApiError(404, "Niet gevonden")
            return self.json(200, admin_state(c))


# ---------------------------------------------------------------- backups

def backup_loop():
    folder = BACKUP_DIR
    os.makedirs(folder, exist_ok=True)
    while True:
        time.sleep(BACKUP_EVERY_S)
        try:
            dest = os.path.join(folder, datetime.now().strftime("backup-%Y%m%d-%H%M%S.db"))
            db.backup(dest)
            files = sorted(f for f in os.listdir(folder) if f.startswith("backup-"))
            for old in files[:-BACKUP_KEEP]:
                os.remove(os.path.join(folder, old))
        except Exception as e:
            print("Backup mislukt:", repr(e), file=sys.stderr, flush=True)


# ---------------------------------------------------------------- publieke export

def write_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def export_public():
    with open(os.path.join(STATIC_DIR, "scorebord.html"), "rb") as f:
        write_atomic(os.path.join(PUBLIC_DIR, "index.html"), f.read())
    with db.read() as c:
        data = scoreboard(c)
    write_atomic(os.path.join(PUBLIC_DIR, "scorebord.json"), json.dumps(data, ensure_ascii=False).encode())


def public_loop():
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    while True:
        try:
            export_public()
        except Exception as e:
            print("Publieke export mislukt:", repr(e), file=sys.stderr, flush=True)
        time.sleep(PUBLIC_EVERY_S)


def main():
    if not TELLER_PIN or not ADMIN_PIN:
        sys.exit("Zet de omgevingsvariabelen TELLER_PIN en ADMIN_PIN (verschillend van elkaar).")
    if TELLER_PIN == ADMIN_PIN:
        sys.exit("TELLER_PIN en ADMIN_PIN moeten verschillend zijn.")
    if BACKUP_EVERY_S > 0:
        threading.Thread(target=backup_loop, daemon=True).start()
    if PUBLIC_DIR:
        threading.Thread(target=public_loop, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"24-urenloop draait op http://{HOST}:{PORT}  (db: {DB_PATH})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
