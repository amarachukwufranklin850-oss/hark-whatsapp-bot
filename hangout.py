"""Tygars Hangout: one public chat room for students across Africa, plus friends and friend-only private chats.
No sign-up: each browser gets a random id + secret key (kept in localStorage). Nickname only.
Storage: Postgres (DATABASE_URL, Neon) on Render; SQLite file locally for testing."""
import os
import re
import time
import json
import hashlib
import secrets
import threading
from flask import Blueprint, request, Response

bp = Blueprint("hangout", __name__)
DB_URL = os.environ.get("DATABASE_URL", "")
PG = bool(DB_URL)
_lock = threading.Lock()
_pool = None
_ready = False
_last_send = {}
MAX_LEN = 500
HIDE_AT = 3  # reports before a message is hidden for everyone
BAD = re.compile(r"\b(fuck|bitch|nigga|nigger|cunt|whore|pussy|dick)\b", re.I)

SCHEMA_PG = [
    "create table if not exists hg_users (uid text primary key, keyhash text, nick text, seen double precision, bio text default '')",
    "create table if not exists hg_posts (id bigserial primary key, uid text, nick text, body text, ts double precision, reports int default 0)",
    "create table if not exists hg_likes (pid bigint, uid text, primary key (pid, uid))",
    "create table if not exists hg_comments (id bigserial primary key, pid bigint, uid text, nick text, body text, ts double precision, reports int default 0)",
    "create table if not exists hg_msgs (id bigserial primary key, room text, uid text, nick text, body text, ts double precision, reports int default 0)",
    "create index if not exists hg_msgs_room on hg_msgs(room, id)",
    "create table if not exists hg_friends (a text, b text, status text, primary key (a, b))",
    "create table if not exists hg_blocks (a text, b text, primary key (a, b))",
    "create table if not exists hg_reports2 (kind text, mid bigint, uid text, primary key (kind, mid, uid))",
]
SCHEMA_PG += [
    "create table if not exists hg_sessions (keyhash text primary key, uid text, ts double precision)",
    "create table if not exists hg_codes (email text primary key, codehash text, exp double precision, tries int default 0)",
    "create table if not exists hg_notifs (id bigserial primary key, uid text, kind text, from_uid text, from_nick text, pid bigint, ts double precision, seen int default 0)",
    "create table if not exists hg_media (k text primary key, data text)",
]
ALTERS = ["alter table hg_users add column email text", "alter table hg_users add column photo_v int default 0",
          "alter table hg_posts add column img int default 0", "alter table hg_users add column bio text default ''"]
SCHEMA_LITE = [s.replace("bigserial primary key", "integer primary key autoincrement")
               .replace("double precision", "real").replace("bigint", "integer") for s in SCHEMA_PG]


def _conn():
    global _pool
    if PG:
        import psycopg2.pool
        with _lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(1, 6, DB_URL, connect_timeout=8)
        return _pool.getconn()
    import sqlite3
    return sqlite3.connect(os.environ.get("HANGOUT_DB", "/tmp/hangout.db"), timeout=10)


def q(sql, args=(), fetch=False, one=False):
    """Run SQL written with ? placeholders on Postgres or SQLite."""
    global _ready
    c = _conn()
    try:
        cur = c.cursor()
        if not _ready:
            for s in (SCHEMA_PG if PG else SCHEMA_LITE):
                cur.execute(s)
            c.commit()
            for a in ALTERS:
                try:
                    cur.execute(a)
                    c.commit()
                except Exception:
                    c.rollback()
            _ready = True
        cur.execute(sql.replace("?", "%s") if PG else sql, args)
        rows = cur.fetchall() if fetch or one else None
        c.commit()
        if one:
            return rows[0] if rows else None
        return rows
    except Exception:
        c.rollback()
        raise
    finally:
        if PG:
            try:
                _pool.putconn(c)
            except Exception:
                pass
        else:
            c.close()


def _h(key):
    return hashlib.sha256((key or "").encode()).hexdigest()


def auth(d):
    """Check uid + key. Returns (uid, nick) or (None, None)."""
    uid, key = str(d.get("uid", ""))[:24], str(d.get("key", ""))
    if not re.fullmatch(r"[A-Za-z0-9]{8,24}", uid) or len(key) < 16:
        return None, None
    kh = _h(key)
    row = q("select u.nick from hg_sessions s join hg_users u on u.uid=s.uid where s.keyhash=? and s.uid=?",
            (kh, uid), one=True)
    if row:
        return uid, row[0]
    row = q("select keyhash, nick from hg_users where uid=?", (uid,), one=True)
    if not row or row[0] != kh:
        return None, None
    return uid, row[1]


def notify(uid, kind, frm, frm_nick, pid=0):
    if uid and uid != frm:
        q("insert into hg_notifs (uid, kind, from_uid, from_nick, pid, ts) values (?, ?, ?, ?, ?, ?)",
          (uid, kind, frm, frm_nick, pid, time.time()))


def send_code_email(email, code):
    key = os.environ.get("BREVO_API_KEY", "")
    sender = os.environ.get("MAIL_FROM", "harkbotofficial@gmail.com")
    if not key:
        return False
    import requests
    html_body = (f"<div style='font-family:sans-serif;font-size:16px'><p>Your Campus Connect code is</p>"
                 f"<p style='font-size:32px;font-weight:bold;letter-spacing:6px'>{code}</p>"
                 f"<p>It expires in 10 minutes. If you didn't ask for it, ignore this email.</p>"
                 f"<p style='color:#888;font-size:12px'>Campus Connect is a free, unofficial fan-made site.</p></div>")
    r = requests.post("https://api.brevo.com/v3/smtp/email", timeout=15,
                      headers={"api-key": key, "Content-Type": "application/json"},
                      json={"sender": {"name": "Campus Connect", "email": sender}, "to": [{"email": email}],
                            "subject": f"{code} is your Campus Connect code", "htmlContent": html_body})
    if r.status_code >= 300:
        print("brevo error:", r.status_code, r.text[:200])
    return r.status_code < 300


def clean_nick(n):
    n = re.sub(r"[^\w .\-]", "", (n or "").strip())[:20].strip()
    return n if len(n) >= 2 and not BAD.search(n) else ""


def friends_of(uid):
    return {r[0] for r in q("select b from hg_friends where a=? and status='friends'", (uid,), fetch=True)}


def dm_room(a, b):
    return "dm:" + ":".join(sorted([a, b]))


def can_use(uid, room):
    if room == "hangout":
        return True
    m = re.fullmatch(r"dm:([A-Za-z0-9]+):([A-Za-z0-9]+)", room or "")
    if not m or uid not in m.groups():
        return False
    other = m.group(2) if m.group(1) == uid else m.group(1)
    return other in friends_of(uid)


EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[a-z]{2,}$", re.I)
_code_ip = {}


@bp.post("/hg/api/code")
def send_code():
    d = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip().lower()[:120]
    if not EMAIL_RE.match(email):
        return {"error": "Enter a valid email address."}, 400
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0]
    now = time.time()
    hits = [t for t in _code_ip.get(ip, []) if now - t < 3600]
    if len(hits) >= 8:
        return {"error": "Too many codes requested. Try again later."}, 429
    old = q("select exp from hg_codes where email=?", (email,), one=True)
    if old and old[0] - 600 + 45 > now:
        return {"error": "Wait a minute before asking for a new code."}, 429
    code = f"{secrets.randbelow(1000000):06d}"
    q("delete from hg_codes where email=?", (email,))
    q("insert into hg_codes (email, codehash, exp, tries) values (?, ?, ?, 0)", (email, _h(code), now + 600))
    _code_ip[ip] = hits + [now]
    if os.environ.get("HG_DEV_CODES") == "1":
        return {"ok": True, "dev_code": code}
    if not send_code_email(email, code):
        return {"error": "Email sign-in isn't ready yet. Please try again later."}, 503
    new = not q("select 1 from hg_users where email=?", (email,), one=True)
    return {"ok": True, "new": new}


def _email_ready():
    return bool(os.environ.get("BREVO_API_KEY")) or os.environ.get("HG_DEV_CODES") == "1"


@bp.post("/hg/api/mode")
def mode():
    return {"email": _email_ready()}


_join_ip = {}


@bp.post("/hg/api/quickjoin")
def quick_join():
    """Name-only preview join, only while email sign-in isn't set up."""
    if _email_ready():
        return {"error": "Please sign in with email."}, 403
    d = request.get_json(silent=True) or {}
    nick = clean_nick(d.get("nick"))
    if not nick:
        return {"error": "Pick a name of 2 to 20 letters or numbers."}, 400
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0]
    now = time.time()
    hits = [t for t in _join_ip.get(ip, []) if now - t < 3600]
    if len(hits) >= 10:
        return {"error": "Too many new accounts. Try again later."}, 429
    _join_ip[ip] = hits + [now]
    uid = secrets.token_hex(6)
    q("insert into hg_users (uid, keyhash, nick, seen, email) values (?, '', ?, ?, ?)", (uid, nick, now, None))
    key = secrets.token_urlsafe(24)
    q("insert into hg_sessions (keyhash, uid, ts) values (?, ?, ?)", (_h(key), uid, now))
    return {"uid": uid, "key": key, "nick": nick}


@bp.post("/hg/api/verify")
def verify_code():
    d = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip().lower()[:120]
    code = re.sub(r"\D", "", d.get("code") or "")
    row = q("select codehash, exp, tries from hg_codes where email=?", (email,), one=True)
    if not row or row[1] < time.time() or row[2] >= 5:
        return {"error": "That code expired. Ask for a new one."}, 400
    if row[0] != _h(code):
        q("update hg_codes set tries=tries+1 where email=?", (email,))
        return {"error": "Wrong code. Check your email and try again."}, 400
    user = q("select uid, nick from hg_users where email=?", (email,), one=True)
    if not user:
        nick = clean_nick(d.get("nick"))
        if not nick:
            return {"error": "Pick a name of 2 to 20 letters or numbers.", "need_nick": True}, 400
        uid = secrets.token_hex(6)
        q("insert into hg_users (uid, keyhash, nick, seen, email) values (?, '', ?, ?, ?)",
          (uid, nick, time.time(), email))
        user = (uid, nick)
    q("delete from hg_codes where email=?", (email,))
    key = secrets.token_urlsafe(24)
    q("insert into hg_sessions (keyhash, uid, ts) values (?, ?, ?)", (_h(key), user[0], time.time()))
    return {"uid": user[0], "key": key, "nick": user[1]}


@bp.post("/hg/api/logout")
def logout():
    d = request.get_json(silent=True) or {}
    q("delete from hg_sessions where keyhash=?", (_h(d.get("key") or ""),))
    return {"ok": True}


@bp.post("/hg/api/nick")
def set_nick():
    d = request.get_json(silent=True) or {}
    uid, _ = auth(d)
    nick = clean_nick(d.get("nick"))
    if not uid:
        return {"error": "auth"}, 403
    if not nick:
        return {"error": "Pick a name of 2 to 20 letters or numbers."}, 400
    q("update hg_users set nick=? where uid=?", (nick, uid))
    return {"nick": nick}


def _save_media(k, data_url, limit):
    m = re.fullmatch(r"data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=]+)", data_url or "")
    if not m or len(m.group(2)) > limit:
        return False
    q("delete from hg_media where k=?", (k,))
    q("insert into hg_media (k, data) values (?, ?)", (k, data_url))
    return True


@bp.post("/hg/api/photo")
def set_photo():
    d = request.get_json(silent=True) or {}
    uid, _ = auth(d)
    if not uid:
        return {"error": "auth"}, 403
    if not _save_media("u:" + uid, d.get("photo"), 200_000):
        return {"error": "That photo is too big or not an image."}, 400
    q("update hg_users set photo_v=coalesce(photo_v,0)+1 where uid=?", (uid,))
    return {"ok": True, "v": q("select photo_v from hg_users where uid=?", (uid,), one=True)[0]}


@bp.get("/hg/m/<kind>/<key>")
def media(kind, key):
    import base64
    if kind not in ("u", "p") or not re.fullmatch(r"[A-Za-z0-9]{1,24}", key):
        return "", 404
    row = q("select data from hg_media where k=?", (f"{kind}:{key}",), one=True)
    if not row:
        return "", 404
    head, b64 = row[0].split(",", 1)
    mime = head[5:].split(";")[0]
    resp = Response(base64.b64decode(b64), mimetype=mime)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


MANIFEST = {"name": "Campus Connect: for SUMAS students (unofficial)", "short_name": "Campus Connect",
            "start_url": "/hangout", "scope": "/", "display": "standalone", "background_color": "#f0f2f5",
            "theme_color": "#1877f2", "description": "Free, unofficial social app made by students. Not run by SUMAS.",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}


@bp.get("/manifest.webmanifest")
def manifest():
    import json
    return Response(json.dumps(MANIFEST), mimetype="application/manifest+json")


@bp.get("/icon-<int:size>.png")
def icon(size):
    if size not in (192, 512):
        return "", 404
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"icon-{size}.png")
    resp = Response(open(path, "rb").read(), mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


@bp.get("/sw.js")
def sw():
    js = ("self.addEventListener('install',e=>self.skipWaiting());"
          "self.addEventListener('activate',e=>self.clients.claim());"
          "self.addEventListener('fetch',e=>{if(e.request.mode==='navigate'){e.respondWith(fetch(e.request).catch(()=>"
          "new Response('<h2 style=\"font-family:sans-serif;text-align:center;margin-top:40vh\">You are offline. Check your internet.</h2>',"
          "{headers:{'Content-Type':'text/html'}})));}});")
    resp = Response(js, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.get("/logo.jpg")
def logo():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.jpg")
    resp = Response(open(path, "rb").read(), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


@bp.post("/hg/api/search")
def search():
    d = request.get_json(silent=True) or {}
    uid, _ = auth(d)
    if not uid:
        return {"error": "auth"}, 403
    term = re.sub(r"[%_]", "", (d.get("q") or "").strip())[:20]
    if len(term) < 1:
        return {"users": []}
    bl = blocked_set(uid)
    rows = q("select uid, nick, photo_v from hg_users where lower(nick) like ? and nick is not null limit 30",
             ("%" + term.lower() + "%",), fetch=True)
    return {"users": [{"uid": r[0], "nick": r[1], "pv": r[2] or 0} for r in rows if r[0] not in bl][:20]}


@bp.post("/hg/api/notifs")
def notifs():
    d = request.get_json(silent=True) or {}
    uid, _ = auth(d)
    if not uid:
        return {"error": "auth"}, 403
    rows = q("select id, kind, from_uid, from_nick, pid, ts, seen from hg_notifs where uid=? order by id desc limit 40",
             (uid,), fetch=True)
    if d.get("mark"):
        q("update hg_notifs set seen=1 where uid=?", (uid,))
    unread = sum(1 for r in rows if not r[6])
    return {"unread": unread, "items": [{"id": r[0], "kind": r[1], "uid": r[2], "nick": r[3], "pid": r[4],
                                          "ts": r[5], "seen": bool(r[6])} for r in rows]}


@bp.post("/hg/api/msgs")
def msgs():
    d = request.get_json(silent=True) or {}
    uid, _ = auth(d)
    room = d.get("room", "hangout")
    if not uid or not can_use(uid, room):
        return {"error": "auth"}, 403
    after = int(d.get("after") or 0)
    rows = q("select m.id, m.uid, m.nick, m.body, m.ts, u.photo_v from hg_msgs m left join hg_users u on u.uid=m.uid where m.room=? and m.id>? and m.reports<? "
             "order by m.id desc limit 80", (room, after, HIDE_AT), fetch=True)
    q("update hg_users set seen=? where uid=?", (time.time(), uid))
    online = q("select count(*) from hg_users where seen>?", (time.time() - 60,), one=True)[0]
    return {"msgs": [{"id": r[0], "uid": r[1], "nick": r[2], "text": r[3], "ts": r[4], "pv": r[5] or 0} for r in reversed(rows)],
            "online": online}


@bp.post("/hg/api/send")
def send():
    d = request.get_json(silent=True) or {}
    uid, nick = auth(d)
    room = d.get("room", "hangout")
    if not uid or not can_use(uid, room):
        return {"error": "You can only message friends who accepted you."}, 403
    text = (d.get("text") or "").strip()[:MAX_LEN]
    if not text:
        return {"ok": True}
    now = time.time()
    if not rate_ok(uid):
        return {"error": "Slow down a little."}, 429
    text = BAD.sub(lambda m: "*" * len(m.group(0)), text)
    q("insert into hg_msgs (room, uid, nick, body, ts) values (?, ?, ?, ?, ?)", (room, uid, nick, text, now))
    if room == "hangout" and int(now) % 20 == 0:
        q("delete from hg_msgs where room='hangout' and ts<?", (now - 7 * 86400,))
    return {"ok": True}


@bp.post("/hg/api/report")
def report():
    d = request.get_json(silent=True) or {}
    uid, _ = auth(d)
    mid = int(d.get("id") or 0)
    kind = d.get("kind", "msg")
    table = {"msg": "hg_msgs", "post": "hg_posts", "comment": "hg_comments"}.get(kind)
    if not uid or not mid or not table:
        return {"error": "auth"}, 403
    if not q("select 1 from hg_reports2 where kind=? and mid=? and uid=?", (kind, mid, uid), one=True):
        q("insert into hg_reports2 (kind, mid, uid) values (?, ?, ?)", (kind, mid, uid))
        q(f"update {table} set reports=reports+1 where id=?", (mid,))
    return {"ok": True}


def rate_ok(uid, gap=1.5):
    now = time.time()
    if now - _last_send.get(uid, 0) < gap:
        return False
    _last_send[uid] = now
    return True


def blocked_set(uid):
    return {r[0] for r in q("select b from hg_blocks where a=? union select a from hg_blocks where b=?",
                            (uid, uid), fetch=True)}


def pv(u):
    r = q("select photo_v from hg_users where uid=?", (u,), one=True)
    return (r[0] or 0) if r else 0


def post_json(r, uid):
    pid = r[0]
    likes = q("select count(*) from hg_likes where pid=?", (pid,), one=True)[0]
    mine = bool(q("select 1 from hg_likes where pid=? and uid=?", (pid, uid), one=True))
    cc = q("select count(*) from hg_comments where pid=? and reports<?", (pid, HIDE_AT), one=True)[0]
    return {"id": pid, "uid": r[1], "nick": r[2], "text": r[3], "ts": r[4], "likes": likes, "liked": mine,
            "comments": cc, "img": bool(r[5]) if len(r) > 5 else False, "pv": pv(r[1])}


@bp.post("/hg/api/feed")
def feed():
    d = request.get_json(silent=True) or {}
    uid, _ = auth(d)
    if not uid:
        return {"error": "auth"}, 403
    before = int(d.get("before") or 0) or 10 ** 15
    rows = q("select id, uid, nick, body, ts, img from hg_posts where reports<? and id<? order by id desc limit 60",
             (HIDE_AT, before), fetch=True)
    bl = blocked_set(uid)
    if d.get("scope") == "friends":
        fr = friends_of(uid) | {uid}
        rows = [r for r in rows if r[1] in fr]
    if d.get("of"):
        rows = [r for r in rows if r[1] == d["of"]]
    rows = [r for r in rows if r[1] not in bl][:20]
    return {"posts": [post_json(r, uid) for r in rows]}


@bp.post("/hg/api/post")
def new_post():
    d = request.get_json(silent=True) or {}
    uid, nick = auth(d)
    if not uid:
        return {"error": "auth"}, 403
    text = BAD.sub(lambda m: "*" * len(m.group(0)), (d.get("text") or "").strip()[:1000])
    img = d.get("img") or ""
    if not text and not img:
        return {"error": "Write something or add a photo first."}, 400
    if not rate_ok("p" + uid, 20):
        return {"error": "Wait a few seconds before posting again."}, 429
    ts = time.time()
    q("insert into hg_posts (uid, nick, body, ts, img) values (?, ?, ?, ?, 0)", (uid, nick, text, ts))
    pid = q("select id from hg_posts where uid=? and ts=?", (uid, ts), one=True)[0]
    if img:
        if not _save_media(f"p:{pid}", img, 600_000):
            q("delete from hg_posts where id=?", (pid,))
            return {"error": "That photo is too big or not an image."}, 400
        q("update hg_posts set img=1 where id=?", (pid,))
    return {"ok": True}


@bp.post("/hg/api/delete")
def delete_post():
    d = request.get_json(silent=True) or {}
    uid, _ = auth(d)
    pid = int(d.get("id") or 0)
    if not uid:
        return {"error": "auth"}, 403
    if q("select 1 from hg_posts where id=? and uid=?", (pid, uid), one=True):
        q("delete from hg_posts where id=?", (pid,))
        q("delete from hg_media where k=?", (f"p:{pid}",))
    return {"ok": True}


@bp.post("/hg/api/like")
def like():
    d = request.get_json(silent=True) or {}
    uid, _ = auth(d)
    pid = int(d.get("id") or 0)
    if not uid or not pid:
        return {"error": "auth"}, 403
    if q("select 1 from hg_likes where pid=? and uid=?", (pid, uid), one=True):
        q("delete from hg_likes where pid=? and uid=?", (pid, uid))
    else:
        q("insert into hg_likes (pid, uid) values (?, ?)", (pid, uid))
        o = q("select uid from hg_posts where id=?", (pid,), one=True)
        if o:
            notify(o[0], "like", uid, _, pid)
    return {"likes": q("select count(*) from hg_likes where pid=?", (pid,), one=True)[0]}


@bp.post("/hg/api/comments")
def comments():
    d = request.get_json(silent=True) or {}
    uid, _ = auth(d)
    pid = int(d.get("id") or 0)
    if not uid:
        return {"error": "auth"}, 403
    bl = blocked_set(uid)
    rows = q("select id, uid, nick, body, ts from hg_comments where pid=? and reports<? order by id limit 100",
             (pid, HIDE_AT), fetch=True)
    return {"comments": [{"id": r[0], "uid": r[1], "nick": r[2], "text": r[3], "ts": r[4], "pv": pv(r[1])}
                         for r in rows if r[1] not in bl]}


@bp.post("/hg/api/comment")
def comment():
    d = request.get_json(silent=True) or {}
    uid, nick = auth(d)
    pid = int(d.get("id") or 0)
    if not uid or not pid:
        return {"error": "auth"}, 403
    text = BAD.sub(lambda m: "*" * len(m.group(0)), (d.get("text") or "").strip()[:500])
    if not text:
        return {"ok": True}
    if not rate_ok(uid):
        return {"error": "Slow down a little."}, 429
    owner = q("select uid from hg_posts where id=?", (pid,), one=True)
    if not owner or owner[0] in blocked_set(uid):
        return {"error": "You can't comment on this post."}, 403
    q("insert into hg_comments (pid, uid, nick, body, ts) values (?, ?, ?, ?, ?)", (pid, uid, nick, text, time.time()))
    notify(owner[0], "comment", uid, nick, pid)
    return {"ok": True}


@bp.post("/hg/api/profile")
def profile():
    d = request.get_json(silent=True) or {}
    uid, _ = auth(d)
    to = str(d.get("to") or uid)[:24]
    if not uid:
        return {"error": "auth"}, 403
    u = q("select nick, bio, seen, photo_v from hg_users where uid=?", (to,), one=True)
    if not u:
        return {"error": "Not found"}, 404
    st = q("select status from hg_friends where a=? and b=?", (uid, to), one=True)
    nf = q("select count(*) from hg_friends where a=? and status='friends'", (to,), one=True)[0]
    return {"uid": to, "nick": u[0], "bio": u[1] or "", "online": (u[2] or 0) > time.time() - 60,
            "status": "me" if to == uid else (st[0] if st else "none"), "friends": nf, "pv": u[3] or 0}


@bp.post("/hg/api/bio")
def set_bio():
    d = request.get_json(silent=True) or {}
    uid, _ = auth(d)
    if not uid:
        return {"error": "auth"}, 403
    bio = BAD.sub(lambda m: "*" * len(m.group(0)), (d.get("bio") or "").strip()[:160])
    q("update hg_users set bio=? where uid=?", (bio, uid))
    return {"ok": True}


@bp.post("/hg/api/friend")
def friend():
    """action: request | accept | decline | remove | block | unblock"""
    d = request.get_json(silent=True) or {}
    uid, me_nick = auth(d)
    to = str(d.get("to", "")).strip()[:24]
    act = d.get("action")
    if not uid:
        return {"error": "auth"}, 403
    if to == uid or not q("select 1 from hg_users where uid=?", (to,), one=True):
        return {"error": "No one with that friend code."}, 404
    blocked = q("select 1 from hg_blocks where (a=? and b=?) or (a=? and b=?)", (uid, to, to, uid), one=True)
    if act == "request":
        if blocked:
            return {"error": "You can't add this person."}, 403
        theirs = q("select status from hg_friends where a=? and b=?", (to, uid), one=True)
        if theirs and theirs[0] == "pending_out":  # they already asked me -> become friends
            act = "accept"
        else:
            q("delete from hg_friends where (a=? and b=?) or (a=? and b=?)", (uid, to, to, uid))
            q("insert into hg_friends (a, b, status) values (?, ?, 'pending_out')", (uid, to))
            q("insert into hg_friends (a, b, status) values (?, ?, 'pending_in')", (to, uid))
            notify(to, "request", uid, me_nick)
            return {"ok": True, "status": "requested"}
    if act == "accept":
        if not q("select 1 from hg_friends where a=? and b=? and status='pending_in'", (uid, to), one=True) \
                and not q("select 1 from hg_friends where a=? and b=? and status='pending_out'", (to, uid), one=True):
            return {"error": "No request to accept."}, 400
        q("delete from hg_friends where (a=? and b=?) or (a=? and b=?)", (uid, to, to, uid))
        q("insert into hg_friends (a, b, status) values (?, ?, 'friends')", (uid, to))
        q("insert into hg_friends (a, b, status) values (?, ?, 'friends')", (to, uid))
        notify(to, "accept", uid, me_nick)
        return {"ok": True, "status": "friends"}
    if act in ("decline", "remove", "block"):
        q("delete from hg_friends where (a=? and b=?) or (a=? and b=?)", (uid, to, to, uid))
        if act == "block" and not q("select 1 from hg_blocks where a=? and b=?", (uid, to), one=True):
            q("insert into hg_blocks (a, b) values (?, ?)", (uid, to))
        return {"ok": True}
    if act == "unblock":
        q("delete from hg_blocks where a=? and b=?", (uid, to))
        return {"ok": True}
    return {"error": "Unknown action"}, 400


@bp.post("/hg/api/friends")
def friends():
    d = request.get_json(silent=True) or {}
    uid, nick = auth(d)
    if not uid:
        return {"error": "auth"}, 403
    rows = q("select f.b, f.status, u.nick, u.seen, u.photo_v from hg_friends f join hg_users u on u.uid=f.b where f.a=?",
             (uid,), fetch=True)
    blocks = [r[0] for r in q("select b from hg_blocks where a=?", (uid,), fetch=True)]
    now = time.time()
    return {"me": {"uid": uid, "nick": nick},
            "friends": [{"uid": r[0], "status": r[1], "nick": r[2], "online": (r[3] or 0) > now - 60,
                         "room": dm_room(uid, r[0]), "pv": r[4] or 0} for r in rows],
            "blocked": blocks}


@bp.get("/hangout")
def page():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Campus Connect</title><link rel="icon" href="/logo.jpg"><link rel="apple-touch-icon" href="/logo.jpg"><link rel="manifest" href="/manifest.webmanifest"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#1877f2">
<style>
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{font-family:Segoe UI,Helvetica,Arial,system-ui,sans-serif;background:#f0f2f5;color:#050505;display:flex;flex-direction:column}
a{color:#1877f2;text-decoration:none}
.top{background:#fff;box-shadow:0 1px 2px #0002;z-index:3}
.bar{display:flex;align-items:center;gap:8px;padding:8px 12px}
.brand{display:flex;align-items:center;gap:8px;font-weight:800;font-size:26px;color:#1877f2;letter-spacing:-1px}
.brand img{width:38px;height:38px;border-radius:50%;background:#000}
.sbox{flex:1;display:flex;align-items:center;background:#f0f2f5;border-radius:20px;padding:0 12px;max-width:260px;margin-left:auto}
.sbox input{border:0;background:none;padding:9px 6px;font:inherit;font-size:15px;width:100%;outline:none}
.tabs{display:flex;border-top:1px solid #e4e6eb}
.tabs button{flex:1;border:0;background:none;padding:9px 0 7px;font-size:22px;color:#65676b;border-bottom:3px solid transparent;position:relative}
.tabs button.on{color:#1877f2;border-bottom-color:#1877f2}
.badge{position:absolute;top:3px;left:55%;background:#e41e3f;color:#fff;border-radius:10px;font-size:11px;padding:1px 6px;font-weight:700}
main{flex:1;overflow-y:auto}.view{display:none;max-width:680px;margin:0 auto;padding:10px 0}.view.on{display:block}
.card{background:#fff;border-radius:8px;box-shadow:0 1px 2px #0002;margin:0 0 10px;padding:12px}
@media(max-width:680px){.card{border-radius:0}}
.av{width:40px;height:40px;border-radius:50%;flex:none;display:flex;align-items:center;justify-content:center;font-weight:700;color:#fff;background-size:cover;background-position:center;cursor:pointer}
.av.sm{width:32px;height:32px;font-size:13px}.av.xl{width:150px;height:150px;font-size:56px;border:5px solid #fff}
.composer{display:flex;gap:8px;align-items:center}.composer .fake{flex:1;background:#f0f2f5;border-radius:20px;padding:10px 14px;color:#65676b;cursor:pointer}
.cbtns{display:flex;border-top:1px solid #e4e6eb;margin-top:10px;padding-top:6px}
.cbtns button,.acts button{flex:1;border:0;background:none;color:#65676b;font:inherit;font-weight:600;font-size:14px;padding:8px;border-radius:6px;cursor:pointer}
.cbtns button:active,.acts button:active{background:#f0f2f5}
.who{display:flex;gap:9px;align-items:center}.who b{cursor:pointer;font-size:15px}.who small{color:#65676b;font-size:13px}
.body{white-space:pre-wrap;word-wrap:break-word;font-size:15px;line-height:1.35;margin:10px 0 6px}
.pimg{width:calc(100% + 24px);margin:6px -12px;display:block;max-height:520px;object-fit:cover;background:#ddd}
.stats{display:flex;justify-content:space-between;color:#65676b;font-size:14px;padding:6px 0;border-bottom:1px solid #e4e6eb}
.acts{display:flex;padding-top:4px}.acts button.liked{color:#1877f2}
.cm{display:flex;gap:6px;margin:8px 0}.cm .bub{background:#f0f2f5;border-radius:16px;padding:7px 11px;font-size:14px;max-width:85%}.cm .bub b{display:block;font-size:13px;cursor:pointer}
.cin{display:flex;gap:6px;align-items:center;margin-top:6px}
input.f,textarea.f{width:100%;border:1px solid #ccd0d5;border-radius:8px;padding:11px 12px;font:inherit;font-size:16px;background:#fff;resize:none}
.cin input{border-radius:18px;background:#f0f2f5;border:0}
.btn{border:0;border-radius:6px;padding:9px 14px;font:inherit;font-weight:600;font-size:15px;background:#e4e6eb;color:#050505;cursor:pointer}
.btn.p{background:#1877f2;color:#fff}.btn.g{background:#42b72a;color:#fff}.btn.r{background:#fbe2e5;color:#c4122f}.btn.s{padding:6px 11px;font-size:14px}.btn.w{width:100%}
.row{display:flex;align-items:center;gap:10px;padding:8px 0}.row .nm{flex:1;font-weight:600;cursor:pointer}.row small{display:block;color:#65676b;font-weight:400}
.dot{width:10px;height:10px;border-radius:50%;background:#31a24c;border:2px solid #fff;margin:-12px 0 0 -14px;align-self:flex-end}
h2{font-size:20px;margin:4px 0 10px}h4{margin:10px 0 4px;color:#65676b;font-size:15px}
.cover{height:170px;background:linear-gradient(135deg,#1877f2,#0b1a33);border-radius:0 0 8px 8px;position:relative}
.cover img{position:absolute;right:14px;top:14px;width:44px;height:44px;border-radius:50%;opacity:.85}
.phead{text-align:center;margin-top:-80px;position:relative}.phead .av{margin:0 auto}
.phead h1{margin:8px 0 2px;font-size:26px}.phead .sub{color:#65676b}
.pbtns{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin:12px 0}
#chatlog{padding:0 10px;min-height:55vh}
.m{display:flex;gap:6px;margin:6px 0;align-items:flex-end}.m .t{background:#e4e6eb;padding:8px 12px;border-radius:18px;max-width:75%;white-space:pre-wrap;word-wrap:break-word;font-size:15px}
.m .n{font-size:12px;color:#65676b;margin:0 0 2px 4px;cursor:pointer}.m.me{justify-content:flex-end}.m.me .t{background:#0084ff;color:#fff}
.sys{text-align:center;color:#65676b;font-size:13px;margin:10px}
#cform{display:none;gap:8px;padding:8px;background:#fff;border-top:1px solid #e4e6eb}#cform.on{display:flex}
#cform textarea{flex:1;border:0;background:#f0f2f5;border-radius:20px;padding:10px 14px;font:inherit;font-size:16px;resize:none;max-height:110px}
.ov{position:fixed;inset:0;background:#fffc;display:none;align-items:center;justify-content:center;z-index:9;padding:10px}
.ov.on{display:flex}.sheet{background:#fff;width:100%;max-width:520px;border-radius:10px;box-shadow:0 8px 28px #0004;padding:16px;max-height:90vh;overflow-y:auto}
.empty{color:#65676b;text-align:center;padding:24px 10px;font-size:15px}
.code{font-family:monospace;background:#f0f2f5;padding:2px 7px;border-radius:5px}
#auth{position:fixed;inset:0;background:#f0f2f5;z-index:10;overflow-y:auto;display:flex;flex-direction:column;align-items:center;padding:30px 16px}
#auth .logo{width:96px;height:96px;border-radius:50%;background:#000}
#auth h1{color:#1877f2;font-size:44px;margin:8px 0 0;letter-spacing:-2px}#auth .tag{font-size:19px;text-align:center;max-width:380px;margin:6px 0 18px}
#auth .card{width:100%;max-width:400px;padding:16px;border-radius:8px}
#auth .card>*{margin-bottom:10px}.rules{font-size:13px;color:#65676b;text-align:center;max-width:380px}
.nt{display:flex;gap:10px;align-items:center;padding:8px;border-radius:8px;cursor:pointer}.nt.new{background:#e7f3ff}.nt small{color:#1877f2;display:block}
</style></head><body>
<div id="auth"><img class="logo" src="/logo.jpg" alt="Campus Connect logo"><h1>campus connect</h1>
<div class="tag">Connect with SUMAS students and friends. Unofficial, made by students.</div>
<div class="card" id="authcard"></div>
<div class="rules">Free and unofficial fan-made site. Be kind, no scams, and never share passwords or your home address. 13+ only.</div></div>
<div class="top"><div class="bar"><div class="brand"><img src="/logo.jpg" alt="">connect</div>
<div class="sbox">&#x1F50D;<input id="sq" placeholder="Search Campus Connect"></div></div>
<div class="tabs"><button class="on" data-v="feed">&#x1F3E0;</button><button data-v="friends">&#x1F465;<span id="nbf"></span></button>
<button data-v="chat">&#x1F4AC;</button><button data-v="notifs">&#x1F514;<span id="nbn"></span></button><button data-v="me">&#x2630;</button></div></div>
<main>
<section class="view on" id="v-feed">
 <div class="card"><div class="composer"><div id="myav"></div><div class="fake" id="openc">What's on your mind?</div></div>
  <div class="cbtns"><button id="openp">&#x1F5BC;&#xFE0F; Photo</button><button id="openc2">&#x270F;&#xFE0F; Post</button></div></div>
 <div class="card" style="padding:6px 12px;display:flex;gap:6px"><button class="btn s p" id="sAll">Everyone</button><button class="btn s" id="sFr">Friends</button></div>
 <div id="posts"></div><div style="text-align:center;padding:10px"><button class="btn" id="more" style="display:none">See more</button></div>
</section>
<section class="view" id="v-friends"></section>
<section class="view" id="v-chat"><div class="card" id="chathead" style="display:flex;align-items:center;gap:8px"></div><div id="chatlog"></div></section>
<section class="view" id="v-notifs"></section>
<section class="view" id="v-me"></section>
<section class="view" id="v-search"></section>
<section class="view" id="v-profile"></section>
</main>
<form id="cform"><textarea id="q" rows="1" maxlength="500" placeholder="Aa"></textarea><button class="btn p">Send</button></form>
<div class="ov" id="ov"><div class="sheet" id="sheet"></div></div>
<input type="file" id="file" accept="image/*" style="display:none">
<script>
const $=id=>document.getElementById(id);
let me=JSON.parse(localStorage.getItem("tg_me")||"null"),view="feed",room="hangout",roomName="Hangout",last=0,blocked=[],friends=[],timer=null,scope="all",oldest=0,myPv=0;
async function api(p,b){const r=await fetch("/hg/api/"+p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(Object.assign({uid:me&&me.uid,key:me&&me.key},b||{}))});const j=await r.json().catch(()=>({}));
 if(r.status===403&&j.error==="auth"&&p!=="msgs"){logout();}if(!r.ok){const e=new Error(j.error||"Something went wrong");e.data=j;throw e;}return j;}
function esc(s){const d=document.createElement("div");d.textContent=s==null?"":s;return d.innerHTML;}
function col(u){let h=0;for(const c of u||"x")h=(h*31+c.charCodeAt(0))%360;return "hsl("+h+",55%,50%)";}
function av(u,n,pvv,cls){const st=pvv?'background-image:url(/hg/m/u/'+u+'?v='+pvv+')':'background-color:'+col(u);
 return '<div class="av '+(cls||"")+'" data-p="'+u+'" style="'+st+'">'+(pvv?"":esc((n||"?")[0].toUpperCase()))+'</div>';}
function ago(t){const s=Date.now()/1000-t;if(s<60)return "Just now";if(s<3600)return Math.floor(s/60)+"m";if(s<86400)return Math.floor(s/3600)+"h";if(s<604800)return Math.floor(s/86400)+"d";return new Date(t*1000).toLocaleDateString();}
function wire(el){el.querySelectorAll("[data-p]").forEach(x=>x.onclick=e=>{e.stopPropagation();openProfile(x.dataset.p);});}
/* ---------- auth (email code) ---------- */
let aEmail="",aNew=false;
function authStep1(msg){$("authcard").innerHTML=(msg?'<div style="color:#c4122f;font-size:14px">'+esc(msg)+'</div>':'')+
 '<input class="f" id="ae" type="email" placeholder="Email address" autocomplete="email"><button class="btn p w" id="ab">Continue with email</button>'+
 '<div style="font-size:13px;color:#65676b;text-align:center">We\'ll email you a 6-digit code. No password needed.</div>';
 $("ab").onclick=async()=>{aEmail=$("ae").value.trim();$("ab").disabled=true;try{const j=await api("code",{email:aEmail});aNew=j.new!==false;authStep2(j.dev_code);}catch(e){$("ab").disabled=false;authStep1(e.message);}};
 $("ae").onkeydown=e=>{if(e.key==="Enter")$("ab").click();};}
function authStep2(dev,msg){$("authcard").innerHTML='<div style="font-size:15px">Enter the code we sent to <b>'+esc(aEmail)+'</b></div>'+(msg?'<div style="color:#c4122f;font-size:14px">'+esc(msg)+'</div>':'')+
 '<input class="f" id="ac" inputmode="numeric" maxlength="6" placeholder="6-digit code" style="letter-spacing:6px;text-align:center;font-size:22px" value="'+(dev||"")+'">'+
 (aNew?'<input class="f" id="an" maxlength="20" placeholder="Your name (shown to others)">':'')+
 '<button class="btn g w" id="av2">'+(aNew?"Create account":"Log in")+'</button><div style="text-align:center;font-size:14px"><a href="#" id="aback">Use a different email</a> \u00B7 <a href="#" id="aresend">Resend code</a></div><div style="font-size:13px;color:#65676b;text-align:center">Check your spam folder if it isn\'t in your inbox.</div>';
 $("aback").onclick=e=>{e.preventDefault();authStep1();};
 $("aresend").onclick=async e=>{e.preventDefault();try{await api("code",{email:aEmail});alert("New code sent.");}catch(err){alert(err.message);}};
 $("av2").onclick=async()=>{try{me=await api("verify",{email:aEmail,code:$("ac").value,nick:$("an")?$("an").value:""});localStorage.setItem("tg_me",JSON.stringify(me));$("auth").style.display="none";start();}
  catch(e){if(e.data&&e.data.need_nick){aNew=true;authStep2($("ac").value,e.message);}else authStep2("",e.message);}};}
function logout(){if(me)fetch("/hg/api/logout",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key:me.key})});me=null;localStorage.removeItem("tg_me");location.reload();}
function quickJoin(msg){$("authcard").innerHTML=(msg?'<div style="color:#c4122f;font-size:14px">'+esc(msg)+'</div>':'')+
 '<div style="font-size:14px;color:#65676b;text-align:center">Preview: email sign-in is coming soon. For now just pick a name. This account lives on this phone only.</div>'+
 '<input class="f" id="qn" maxlength="20" placeholder="Your name (shown to others)"><button class="btn g w" id="qb">Join Campus Connect</button>';
 $("qb").onclick=async()=>{$("qb").disabled=true;try{me=await api("quickjoin",{nick:$("qn").value});localStorage.setItem("tg_me",JSON.stringify(me));$("auth").style.display="none";start();}catch(e){quickJoin(e.message);}};
 $("qn").onkeydown=e=>{if(e.key==="Enter")$("qb").click();};}
if(me)$("auth").style.display="none";else api("mode").then(j=>j.email?authStep1():quickJoin()).catch(()=>authStep1());
if("serviceWorker" in navigator)navigator.serviceWorker.register("/sw.js").catch(()=>{});
/* ---------- nav ---------- */
document.querySelectorAll(".tabs button").forEach(b=>b.onclick=()=>show(b.dataset.v));
function show(v){view=v;document.querySelectorAll(".tabs button").forEach(b=>b.classList.toggle("on",b.dataset.v===v));
 document.querySelectorAll(".view").forEach(s=>s.classList.toggle("on",s.id==="v-"+v));$("cform").classList.toggle("on",v==="chat");clearTimeout(timer);
 document.querySelector("main").scrollTop=0;
 if(v==="feed")loadFeed(true);if(v==="chat")openRoom(room,roomName);if(v==="friends")friendsView();if(v==="notifs")notifsView();if(v==="me")menuView();}
async function start(){try{const p=await api("profile",{});myPv=p.pv;me.nick=p.nick;}catch(e){}
 $("myav").innerHTML=av(me.uid,me.nick,myPv);wire($("myav"));$("openc").textContent="What's on your mind, "+me.nick.split(" ")[0]+"?";
 show("feed");badges();setInterval(badges,20000);}
async function badges(){try{const j=await api("friends");friends=j.friends;blocked=j.blocked;const n=friends.filter(f=>f.status==="pending_in").length;
 $("nbf").innerHTML=n?'<span class="badge">'+n+'</span>':"";const k=await api("notifs",{});$("nbn").innerHTML=k.unread?'<span class="badge">'+k.unread+'</span>':"";}catch(e){}}
/* ---------- images ---------- */
function pickImage(max,q){return new Promise(res=>{const f=$("file");f.value="";f.onchange=()=>{const file=f.files[0];if(!file)return res(null);
 const img=new Image();img.onload=()=>{const s=Math.min(1,max/Math.max(img.width,img.height));const c=document.createElement("canvas");c.width=Math.round(img.width*s);c.height=Math.round(img.height*s);
  c.getContext("2d").drawImage(img,0,0,c.width,c.height);res(c.toDataURL("image/jpeg",q));URL.revokeObjectURL(img.src);};img.onerror=()=>res(null);img.src=URL.createObjectURL(file);};f.click();});}
/* ---------- composer ---------- */
function composer(withPhoto){let img=null;sheet('<h2 style="text-align:center">Create post</h2><div class="who">'+av(me.uid,me.nick,myPv)+'<b>'+esc(me.nick)+'</b></div>'+
 '<textarea class="f" id="ct" rows="4" maxlength="1000" placeholder="What\'s on your mind, '+esc(me.nick.split(" ")[0])+'?" style="border:0;font-size:20px;margin:8px 0"></textarea><div id="cprev"></div>'+
 '<div style="display:flex;gap:8px;margin-top:8px"><button class="btn" id="cph">\u{1F5BC}\uFE0F Photo</button><button class="btn" id="cx">Cancel</button><button class="btn p" id="cp" style="flex:1">Post</button></div>');
 const addPhoto=async()=>{img=await pickImage(1280,0.8);$("cprev").innerHTML=img?'<img src="'+img+'" style="width:100%;border-radius:8px">':"";};
 $("cph").onclick=addPhoto;$("cx").onclick=close;$("ct").focus();if(withPhoto)addPhoto();
 $("cp").onclick=async()=>{$("cp").disabled=true;try{await api("post",{text:$("ct").value.trim(),img});close();scope="all";loadFeed(true);}catch(e){alert(e.message);$("cp").disabled=false;}};}
$("openc").onclick=()=>composer(false);$("openc2").onclick=()=>composer(false);$("openp").onclick=()=>composer(true);
/* ---------- feed ---------- */
function seg(){$("sAll").className="btn s"+(scope==="all"?" p":"");$("sFr").className="btn s"+(scope==="friends"?" p":"");}
$("sAll").onclick=()=>{scope="all";seg();loadFeed(true);};$("sFr").onclick=()=>{scope="friends";seg();loadFeed(true);};
$("more").onclick=()=>loadFeed(false);
async function loadFeed(reset,box,of){box=box||$("posts");if(reset){if(!of)oldest=0;box.innerHTML="";}
 try{const j=await api("feed",{scope:of?"all":scope,before:reset?0:oldest,of:of||""});
  if(reset&&!j.posts.length)box.innerHTML='<div class="card empty">'+(of?"No posts yet.":scope==="friends"?"No posts from friends yet. Add some friends!":"No posts yet. Be the first to say hi!")+'</div>';
  j.posts.forEach(p=>{if(!of)oldest=p.id;box.appendChild(postCard(p));});if(!of)$("more").style.display=j.posts.length>=20?"":"none";}catch(e){}}
function postCard(p){const d=document.createElement("div");d.className="card";
 d.innerHTML='<div class="who">'+av(p.uid,p.nick,p.pv)+'<div style="flex:1"><b data-p="'+p.uid+'">'+esc(p.nick)+'</b><br><small>'+ago(p.ts)+' \u00B7 \u{1F30D}</small></div><button class="btn s mb" style="background:none;font-size:20px">\u22EF</button></div>'+
 (p.text?'<div class="body">'+esc(p.text)+'</div>':'<div style="height:8px"></div>')+(p.img?'<img class="pimg" loading="lazy" src="/hg/m/p/'+p.id+'">':'')+
 '<div class="stats"><span class="lc">'+(p.likes?"\u{1F44D} "+p.likes:"")+'</span><span>'+(p.comments?p.comments+" comment"+(p.comments>1?"s":""):"")+'</span></div>'+
 '<div class="acts"><button class="lk'+(p.liked?" liked":"")+'">\u{1F44D} Like</button><button class="cb">\u{1F4AC} Comment</button></div><div class="cms"></div>';
 wire(d);
 d.querySelector(".lk").onclick=async e=>{const b=e.currentTarget;const j=await api("like",{id:p.id});b.classList.toggle("liked");d.querySelector(".lc").textContent=j.likes?"\u{1F44D} "+j.likes:"";};
 d.querySelector(".cb").onclick=()=>comments(p,d.querySelector(".cms"),true);
 d.querySelector(".mb").onclick=()=>{if(p.uid===me.uid){sheet('<h2>Your post</h2><button class="btn r w" id="x1">Delete post</button><div style="height:8px"></div><button class="btn w" id="x3">Cancel</button>',()=>{
   $("x1").onclick=()=>api("delete",{id:p.id}).then(()=>{d.remove();close();});$("x3").onclick=close;});}
  else sheet('<h2>Post by '+esc(p.nick)+'</h2><button class="btn w" id="x2">View profile</button><div style="height:8px"></div><button class="btn r w" id="x1">Report post</button><div style="height:8px"></div><button class="btn w" id="x3">Cancel</button>',
   ()=>{$("x1").onclick=async()=>{await api("report",{kind:"post",id:p.id});close();d.remove();alert("Thanks for reporting. We hide posts that get several reports.");};$("x2").onclick=()=>{close();openProfile(p.uid);};$("x3").onclick=close;});};
 return d;}
async function comments(p,box,focus){const j=await api("comments",{id:p.id});
 box.innerHTML=j.comments.map(c=>'<div class="cm">'+av(c.uid,c.nick,c.pv,"sm")+'<div class="bub"><b data-p="'+c.uid+'">'+esc(c.nick)+'</b>'+esc(c.text)+'</div></div>').join("")+
  '<div class="cin">'+av(me.uid,me.nick,myPv,"sm")+'<input class="f" placeholder="Write a comment..." maxlength="500"><button class="btn p s">Send</button></div>';
 wire(box);const inp=box.querySelector("input");if(focus)inp.focus();
 const send=async()=>{const t=inp.value.trim();if(!t)return;try{await api("comment",{id:p.id,text:t});comments(p,box,true);}catch(e){alert(e.message);}};
 box.querySelector(".cin button").onclick=send;inp.onkeydown=e=>{if(e.key==="Enter")send();};}
/* ---------- search ---------- */
let st=null;$("sq").oninput=()=>{clearTimeout(st);st=setTimeout(doSearch,300);};$("sq").onfocus=()=>{if($("sq").value)doSearch();};
async function doSearch(){const t=$("sq").value.trim();if(!t){show("feed");return;}view="search";
 document.querySelectorAll(".view").forEach(s=>s.classList.toggle("on",s.id==="v-search"));$("cform").classList.remove("on");
 const j=await api("search",{q:t});const box=$("v-search");
 box.innerHTML='<div class="card"><h2>People</h2>'+(j.users.length?j.users.map(u=>'<div class="row">'+av(u.uid,u.nick,u.pv)+'<div class="nm" data-p="'+u.uid+'">'+esc(u.nick)+'</div></div>').join(""):'<div class="empty">No one found.</div>')+'</div>';wire(box);}
/* ---------- profile ---------- */
function openProfile(uid){view="profile";document.querySelectorAll(".view").forEach(s=>s.classList.toggle("on",s.id==="v-profile"));$("cform").classList.remove("on");clearTimeout(timer);
 document.querySelectorAll(".tabs button").forEach(b=>b.classList.remove("on"));document.querySelector("main").scrollTop=0;profileView(uid,$("v-profile"));}
async function profileView(uid,box){let p;try{p=await api("profile",{to:uid});}catch(e){box.innerHTML='<div class="card empty">'+esc(e.message)+'</div>';return;}
 let h='<div class="card" style="padding:0 0 12px"><div class="cover"><img src="/logo.jpg" alt=""></div><div class="phead">'+av(p.uid,p.nick,p.pv,"xl")+
  '<h1>'+esc(p.nick)+'</h1><div class="sub">'+p.friends+' friend'+(p.friends===1?"":"s")+(p.online?' \u00B7 <span style="color:#31a24c">Active now</span>':'')+'</div>'+
  '<p style="margin:10px 16px">'+(esc(p.bio)||(p.status==="me"?'<span style="color:#65676b">Add a short bio so people know you.</span>':''))+'</p><div class="pbtns">';
 if(p.status==="me")h+='<button class="btn p" id="pph">\u{1F4F7} Change photo</button><button class="btn" id="eb">\u270F\uFE0F Edit bio</button><button class="btn" id="en">Edit name</button>';
 else{h+=p.status==="friends"?'<button class="btn" id="pff">\u2714 Friends</button><button class="btn p" id="pc">\u{1F4AC} Message</button>':p.status==="pending_in"?'<button class="btn p" id="pa2">Confirm request</button>':p.status==="pending_out"?'<button class="btn" id="pcan">Cancel request</button>':'<button class="btn p" id="pa">\u2795 Add friend</button>';
  h+='<button class="btn r" id="pbk">Block</button>';}
 h+='</div></div></div><div class="card"><h2>Posts</h2></div><div class="pp"></div>';box.innerHTML=h;
 const g=s=>box.querySelector(s),re=()=>profileView(uid,box);
 if(g("#pph"))g("#pph").onclick=async()=>{const im=await pickImage(400,0.85);if(!im)return;try{const j=await api("photo",{photo:im});myPv=j.v;$("myav").innerHTML=av(me.uid,me.nick,myPv);wire($("myav"));re();}catch(e){alert(e.message);}};
 if(g("#eb"))g("#eb").onclick=async()=>{const b=prompt("Your bio (up to 160 letters)",p.bio);if(b===null)return;await api("bio",{bio:b});re();};
 if(g("#en"))g("#en").onclick=async()=>{const n=prompt("Your name",me.nick);if(!n)return;try{const j=await api("nick",{nick:n});me.nick=j.nick;localStorage.setItem("tg_me",JSON.stringify(me));re();}catch(e){alert(e.message);}};
 if(g("#pa"))g("#pa").onclick=()=>act("request",uid).then(re);if(g("#pa2"))g("#pa2").onclick=()=>act("accept",uid).then(re);
 if(g("#pcan"))g("#pcan").onclick=()=>act("decline",uid).then(re);
 if(g("#pff"))g("#pff").onclick=()=>{if(confirm("Unfriend "+p.nick+"?"))act("remove",uid).then(re);};
 if(g("#pc"))g("#pc").onclick=()=>dm(uid,p.nick);
 if(g("#pbk"))g("#pbk").onclick=()=>{if(confirm("Block "+p.nick+"? You won't see each other's posts or messages."))act("block",uid).then(()=>show("feed"));};
 loadFeed(true,g(".pp"),uid);}
/* ---------- chat ---------- */
function dm(uid,nick){room=["dm"].concat([me.uid,uid].sort()).join(":");roomName=nick;show("chat");}
function openRoom(r,name){room=r;roomName=name;last=0;clearTimeout(timer);
 $("chathead").innerHTML=r==="hangout"?'<img src="/logo.jpg" style="width:40px;height:40px;border-radius:50%"><div style="flex:1"><b>Hangout</b><br><small id="onl" style="color:#65676b">Open chat for everyone</small></div><button class="btn s" id="dms">Friends\' chats</button>'
  :'<button class="btn s" id="hb">\u2190</button><b style="flex:1">'+esc(name)+'</b><small style="color:#65676b">\u{1F512} Private</small>';
 if($("hb"))$("hb").onclick=()=>{room="hangout";roomName="Hangout";openRoom("hangout","Hangout");};
 if($("dms"))$("dms").onclick=()=>show("friends");
 $("chatlog").innerHTML=r==="hangout"?'<div class="sys">Be kind and keep personal info private.</div>':'<div class="sys">Only you and '+esc(name)+' can see this chat.</div>';poll();}
async function poll(){if(view!=="chat")return;const log=$("chatlog"),main=document.querySelector("main");
 try{const j=await api("msgs",{room,after:last});const near=main.scrollHeight-main.scrollTop-main.clientHeight<120;let mine=false;
  for(const m of j.msgs){last=Math.max(last,m.id);if(blocked.includes(m.uid))continue;mine=m.uid===me.uid;const d=document.createElement("div");d.className="m"+(mine?" me":"");
   d.innerHTML=mine?'<div class="t">'+esc(m.text)+'</div>':av(m.uid,m.nick,m.pv,"sm")+'<div><div class="n">'+esc(m.nick)+'</div><div class="t">'+esc(m.text)+'</div></div>';
   if(!mine){wire(d);d.querySelector(".t").oncontextmenu=e=>{e.preventDefault();if(confirm("Report this message?"))api("report",{kind:"msg",id:m.id}).then(()=>d.remove());};}log.appendChild(d);}
  if(near||mine)main.scrollTop=main.scrollHeight;if(room==="hangout"&&$("onl"))$("onl").textContent="\u{1F7E2} "+j.online+" active now";
 }catch(e){if(room!=="hangout"){room="hangout";openRoom("hangout","Hangout");return;}}
 timer=setTimeout(poll,2500);}
const q=$("q");q.oninput=()=>{q.style.height="auto";q.style.height=q.scrollHeight+"px";};
q.onkeydown=e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();$("cform").requestSubmit();}};
$("cform").onsubmit=async e=>{e.preventDefault();const t=q.value.trim();if(!t)return;q.value="";q.style.height="auto";
 try{await api("send",{room,text:t});clearTimeout(timer);poll();}catch(err){alert(err.message);q.value=t;}};
/* ---------- friends ---------- */
function sheet(h,after){$("sheet").innerHTML=h;$("ov").classList.add("on");if(after)after();}
$("ov").onclick=e=>{if(e.target.id==="ov")close();};function close(){$("ov").classList.remove("on");}
async function act(a,to){try{await api("friend",{action:a,to});await badges();if(view==="friends")friendsView();}catch(e){alert(e.message);}}
async function friendsView(){await badges();const box=$("v-friends");
 const inc=friends.filter(f=>f.status==="pending_in"),fr=friends.filter(f=>f.status==="friends"),out=friends.filter(f=>f.status==="pending_out");
 let h='<div class="card"><h2>Friend requests'+(inc.length?' <span style="color:#e41e3f">'+inc.length+'</span>':'')+'</h2>'+(inc.length?"":'<div class="empty" style="padding:8px">No new requests.</div>');
 inc.forEach(f=>h+='<div class="row">'+av(f.uid,f.nick,f.pv)+'<div class="nm" data-p="'+f.uid+'">'+esc(f.nick)+'</div><button class="btn p s" data-a="accept" data-u="'+f.uid+'">Confirm</button><button class="btn s" data-a="decline" data-u="'+f.uid+'">Delete</button></div>');
 h+='</div><div class="card"><h2>Your friends</h2>'+(fr.length?"":'<div class="empty" style="padding:8px">No friends yet. Search for people or tap a name on a post.</div>');
 fr.forEach(f=>h+='<div class="row">'+av(f.uid,f.nick,f.pv)+(f.online?'<i class="dot"></i>':'')+'<div class="nm" data-p="'+f.uid+'">'+esc(f.nick)+'<small>'+(f.online?"Active now":"")+'</small></div><button class="btn s" data-c="'+f.uid+'">\u{1F4AC} Message</button></div>');
 h+='</div>';if(out.length){h+='<div class="card"><h2>Sent requests</h2>';out.forEach(f=>h+='<div class="row">'+av(f.uid,f.nick,f.pv)+'<div class="nm" data-p="'+f.uid+'">'+esc(f.nick)+'</div><button class="btn s" data-a="decline" data-u="'+f.uid+'">Cancel</button></div>');h+='</div>';}
 h+='<div class="card"><h4>Add by friend code</h4><div style="display:flex;gap:8px"><input class="f" id="fc" placeholder="Friend code"><button class="btn p" id="fa">Add</button></div><div style="font-size:13px;color:#65676b;margin-top:6px">Your code: <span class="code">'+me.uid+'</span></div></div>';
 box.innerHTML=h;wire(box);
 $("fa").onclick=()=>{const c=$("fc").value.trim();if(c)act("request",c).then(()=>alert("Friend request sent."));};
 box.querySelectorAll("[data-a]").forEach(b=>b.onclick=()=>act(b.dataset.a,b.dataset.u));
 box.querySelectorAll("[data-c]").forEach(b=>b.onclick=()=>{const f=friends.find(x=>x.uid===b.dataset.c);dm(f.uid,f.nick);});}
/* ---------- notifications ---------- */
async function notifsView(){const j=await api("notifs",{mark:1});$("nbn").innerHTML="";const box=$("v-notifs");
 const txt={like:"liked your post.",comment:"commented on your post.",request:"sent you a friend request.",accept:"accepted your friend request."};
 box.innerHTML='<div class="card"><h2>Notifications</h2>'+(j.items.length?j.items.map(n=>'<div class="nt'+(n.seen?"":" new")+'" data-k="'+n.kind+'" data-u="'+n.uid+'">'+av(n.uid,n.nick,0)+'<div><b>'+esc(n.nick)+'</b> '+txt[n.kind]+'<small>'+ago(n.ts)+'</small></div></div>').join(""):'<div class="empty">No notifications yet.</div>')+'</div>';
 box.querySelectorAll(".nt").forEach(x=>x.onclick=()=>{if(x.dataset.k==="request")show("friends");else if(x.dataset.k==="accept")openProfile(x.dataset.u);else openProfile(me.uid);});
 box.querySelectorAll(".nt .av").forEach(a=>a.onclick=e=>{e.stopPropagation();openProfile(a.dataset.p);});}
/* ---------- menu ---------- */
function menuView(){const box=$("v-me");
 box.innerHTML='<div class="card"><div class="row" style="cursor:pointer" id="mp">'+av(me.uid,me.nick,myPv)+'<div class="nm">'+esc(me.nick)+'<small>See your profile</small></div></div></div>'+
 '<div class="card"><div class="row"><div class="nm">Your friend code<small>'+me.uid+'</small></div></div>'+
 (blocked.length?'<div class="row"><div class="nm">Blocked people<small>'+blocked.length+' blocked</small></div><button class="btn s" id="ub">Unblock all</button></div>':'')+
 '<div class="row"><div class="nm">Rules<small>Be kind. No scams, bullying or sharing private info. Report anything bad.</small></div></div>'+
 '<button class="btn w" id="lo">Log out</button></div><div style="text-align:center;color:#65676b;font-size:12px;padding:10px">Campus Connect \u00B7 free, unofficial student-made site, not run by SUMAS</div>';
 $("mp").onclick=()=>openProfile(me.uid);$("lo").onclick=logout;
 if($("ub"))$("ub").onclick=async()=>{for(const u of blocked)await api("friend",{action:"unblock",to:u});await badges();menuView();};}
if(me)start();
</script></body></html>"""
