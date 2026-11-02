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
    row = q("select keyhash, nick from hg_users where uid=?", (uid,), one=True)
    if not row or row[0] != _h(key):
        return None, None
    return uid, row[1]


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


@bp.post("/hg/api/join")
def join():
    d = request.get_json(silent=True) or {}
    nick = clean_nick(d.get("nick"))
    if not nick:
        return {"error": "Pick a nickname of 2 to 20 letters or numbers."}, 400
    uid, key = d.get("uid"), d.get("key")
    if uid and key and auth(d)[0]:
        q("update hg_users set nick=?, seen=? where uid=?", (nick, time.time(), uid))
        return {"uid": uid, "key": key, "nick": nick}
    uid, key = secrets.token_hex(6), secrets.token_urlsafe(24)
    q("insert into hg_users (uid, keyhash, nick, seen) values (?, ?, ?, ?)", (uid, _h(key), nick, time.time()))
    return {"uid": uid, "key": key, "nick": nick}


@bp.post("/hg/api/msgs")
def msgs():
    d = request.get_json(silent=True) or {}
    uid, _ = auth(d)
    room = d.get("room", "hangout")
    if not uid or not can_use(uid, room):
        return {"error": "auth"}, 403
    after = int(d.get("after") or 0)
    rows = q("select id, uid, nick, body, ts from hg_msgs where room=? and id>? and reports<? "
             "order by id desc limit 80", (room, after, HIDE_AT), fetch=True)
    q("update hg_users set seen=? where uid=?", (time.time(), uid))
    online = q("select count(*) from hg_users where seen>?", (time.time() - 60,), one=True)[0]
    return {"msgs": [{"id": r[0], "uid": r[1], "nick": r[2], "text": r[3], "ts": r[4]} for r in reversed(rows)],
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


def post_json(r, uid):
    pid = r[0]
    likes = q("select count(*) from hg_likes where pid=?", (pid,), one=True)[0]
    mine = bool(q("select 1 from hg_likes where pid=? and uid=?", (pid, uid), one=True))
    cc = q("select count(*) from hg_comments where pid=? and reports<?", (pid, HIDE_AT), one=True)[0]
    return {"id": pid, "uid": r[1], "nick": r[2], "text": r[3], "ts": r[4], "likes": likes, "liked": mine,
            "comments": cc}


@bp.post("/hg/api/feed")
def feed():
    d = request.get_json(silent=True) or {}
    uid, _ = auth(d)
    if not uid:
        return {"error": "auth"}, 403
    before = int(d.get("before") or 0) or 10 ** 15
    rows = q("select id, uid, nick, body, ts from hg_posts where reports<? and id<? order by id desc limit 60",
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
    if not text:
        return {"error": "Write something first."}, 400
    if not rate_ok("p" + uid, 20):
        return {"error": "Wait a few seconds before posting again."}, 429
    q("insert into hg_posts (uid, nick, body, ts) values (?, ?, ?, ?)", (uid, nick, text, time.time()))
    return {"ok": True}


@bp.post("/hg/api/delete")
def delete_post():
    d = request.get_json(silent=True) or {}
    uid, _ = auth(d)
    pid = int(d.get("id") or 0)
    if not uid:
        return {"error": "auth"}, 403
    q("delete from hg_posts where id=? and uid=?", (pid, uid))
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
    return {"comments": [{"id": r[0], "uid": r[1], "nick": r[2], "text": r[3], "ts": r[4]}
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
    return {"ok": True}


@bp.post("/hg/api/profile")
def profile():
    d = request.get_json(silent=True) or {}
    uid, _ = auth(d)
    to = str(d.get("to") or uid)[:24]
    if not uid:
        return {"error": "auth"}, 403
    u = q("select nick, bio, seen from hg_users where uid=?", (to,), one=True)
    if not u:
        return {"error": "Not found"}, 404
    st = q("select status from hg_friends where a=? and b=?", (uid, to), one=True)
    nf = q("select count(*) from hg_friends where a=? and status='friends'", (to,), one=True)[0]
    return {"uid": to, "nick": u[0], "bio": u[1] or "", "online": (u[2] or 0) > time.time() - 60,
            "status": "me" if to == uid else (st[0] if st else "none"), "friends": nf}


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
    uid, _ = auth(d)
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
            return {"ok": True, "status": "requested"}
    if act == "accept":
        if not q("select 1 from hg_friends where a=? and b=? and status='pending_in'", (uid, to), one=True) \
                and not q("select 1 from hg_friends where a=? and b=? and status='pending_out'", (to, uid), one=True):
            return {"error": "No request to accept."}, 400
        q("delete from hg_friends where (a=? and b=?) or (a=? and b=?)", (uid, to, to, uid))
        q("insert into hg_friends (a, b, status) values (?, ?, 'friends')", (uid, to))
        q("insert into hg_friends (a, b, status) values (?, ?, 'friends')", (to, uid))
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
    rows = q("select f.b, f.status, u.nick, u.seen from hg_friends f join hg_users u on u.uid=f.b where f.a=?",
             (uid,), fetch=True)
    blocks = [r[0] for r in q("select b from hg_blocks where a=?", (uid,), fetch=True)]
    now = time.time()
    return {"me": {"uid": uid, "nick": nick},
            "friends": [{"uid": r[0], "status": r[1], "nick": r[2], "online": (r[3] or 0) > now - 60,
                         "room": dm_room(uid, r[0])} for r in rows],
            "blocked": blocks}


@bp.get("/hangout")
def page():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Tygars Hangout</title>
<style>
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#0f1115;color:#eef0f4;display:flex;flex-direction:column}
a{color:#f5a623}
header{display:flex;align-items:center;gap:10px;padding:12px 14px;background:#171a21;border-bottom:1px solid #262a33}
header b{font-size:17px;flex:1}header small{opacity:.6;font-weight:400;font-size:12px;display:block}
.btn{border:0;border-radius:18px;padding:8px 13px;font:inherit;font-size:14px;background:#262b35;color:#eef0f4;cursor:pointer}
.btn.p{background:#f5a623;color:#111;font-weight:600}.btn.r{background:#b3261e;color:#fff}.btn.s{padding:5px 10px;font-size:13px}
main{flex:1;overflow-y:auto}.view{display:none;padding:12px}.view.on{display:block}
nav{display:flex;background:#171a21;border-top:1px solid #262a33}
nav button{flex:1;border:0;background:none;color:#9aa0ab;padding:10px 0 12px;font:inherit;font-size:12px}
nav button i{display:block;font-style:normal;font-size:20px}nav button.on{color:#f5a623}
.card{background:#171a21;border:1px solid #232731;border-radius:14px;padding:12px;margin-bottom:12px}
.who{display:flex;align-items:center;gap:9px;margin-bottom:8px}.who b{cursor:pointer}.who small{opacity:.55;font-size:12px}
.av{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;color:#111;flex:none}
.body{white-space:pre-wrap;word-wrap:break-word;line-height:1.45;font-size:15px}
.acts{display:flex;gap:6px;margin-top:10px;border-top:1px solid #232731;padding-top:8px}
.acts button{flex:1;background:none;border:0;color:#aab;font:inherit;font-size:14px;padding:6px;border-radius:8px}
.acts button.liked{color:#f5a623;font-weight:600}
.cm{background:#0f1115;border-radius:12px;padding:7px 10px;margin:6px 0;font-size:14px}.cm b{cursor:pointer;font-size:13px}
textarea,input{width:100%;border:1px solid #2c313c;background:#0f1115;color:#eef0f4;border-radius:12px;padding:11px 13px;font:inherit;font-size:16px;resize:none}
.seg{display:flex;gap:6px;margin-bottom:12px}.seg button{flex:1}
#chatlog{min-height:50vh}
.m{margin:7px 0;max-width:86%}.m .n{font-size:12px;opacity:.65;margin:0 6px 2px;cursor:pointer}
.m .t{background:#1d212a;padding:9px 12px;border-radius:14px;border-bottom-left-radius:4px;white-space:pre-wrap;word-wrap:break-word;line-height:1.4;display:inline-block}
.m.me{margin-left:auto;text-align:right}.m.me .t{background:#f5a623;color:#111;border-bottom-left-radius:14px;border-bottom-right-radius:4px;text-align:left}
.sys{text-align:center;font-size:12px;opacity:.55;margin:10px}
#cform{display:none;gap:8px;padding:10px;background:#171a21;border-top:1px solid #262a33}#cform.on{display:flex}
#cform textarea{border-radius:20px}
.ov{position:fixed;inset:0;background:#000a;display:none;align-items:flex-end;justify-content:center;z-index:9}
.ov.on{display:flex}.sheet{background:#171a21;width:100%;max-width:560px;border-radius:18px 18px 0 0;padding:18px;max-height:88vh;overflow-y:auto}
.sheet h3{margin:0 0 6px}.row{display:flex;align-items:center;gap:8px;padding:9px 0;border-bottom:1px solid #262a33}
.row span{flex:1;cursor:pointer}.dot{width:8px;height:8px;border-radius:50%;background:#555;display:inline-block;margin-right:6px}.dot.on{background:#3ecf6e}
.code{font-family:monospace;background:#0f1115;padding:3px 7px;border-radius:6px}
#join{position:fixed;inset:0;background:#0f1115;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;text-align:center;z-index:10}
#join h1{margin:0 0 6px}#join p{opacity:.7;max-width:340px}#join input{max-width:300px;margin:12px 0}
.rules{font-size:13px;opacity:.6;max-width:340px;margin-top:14px}
.badge{background:#b3261e;color:#fff;border-radius:9px;padding:1px 6px;font-size:11px;margin-left:3px}
.empty{opacity:.6;text-align:center;padding:30px 10px;font-size:14px}
</style></head><body>
<div id="join"><h1>&#x1F42F; Tygars Hangout</h1><p>Post, make friends and chat with students across Africa. Just pick a nickname.</p>
<input id="nick" maxlength="20" placeholder="Your nickname"><button class="btn p" id="go">Join</button>
<div class="rules">Be kind. No bullying, no scams, and never share your phone number, address or passwords. 13+ only. Tap a name to add, block or report.</div></div>
<header><b id="title">&#x1F42F; Tygars Hangout<small id="sub"></small></b><button class="btn s" id="back" style="display:none">&#x2190; Back</button></header>
<main>
 <section class="view on" id="v-feed">
  <div class="card"><textarea id="pt" rows="2" maxlength="1000" placeholder="What's on your mind?"></textarea>
   <div style="text-align:right;margin-top:8px"><button class="btn p" id="pb">Post</button></div></div>
  <div class="seg"><button class="btn p" id="sAll">Everyone</button><button class="btn" id="sFr">Friends</button></div>
  <div id="posts"></div><div style="text-align:center"><button class="btn" id="more" style="display:none">Load more</button></div>
 </section>
 <section class="view" id="v-chat"><div id="chatlog"></div></section>
 <section class="view" id="v-friends"></section>
 <section class="view" id="v-me"></section>
</main>
<form id="cform"><textarea id="q" rows="1" maxlength="500" placeholder="Say something nice"></textarea><button class="btn p">Send</button></form>
<nav><button class="on" data-v="feed"><i>&#x1F3E0;</i>Feed</button><button data-v="chat"><i>&#x1F4AC;</i>Hangout</button>
<button data-v="friends"><i>&#x1F465;</i>Friends<span id="nb"></span></button><button data-v="me"><i>&#x1F642;</i>Profile</button></nav>
<div class="ov" id="ov"><div class="sheet" id="sheet"></div></div>
<script>
const $=id=>document.getElementById(id);
let me=JSON.parse(localStorage.getItem("hg_me")||"null"),view="feed",room="hangout",last=0,blocked=[],friends=[],timer=null,scope="all",oldest=0;
async function api(p,b){const r=await fetch("/hg/api/"+p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(Object.assign({uid:me&&me.uid,key:me&&me.key},b||{}))});const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||"Something went wrong");return j;}
function esc(s){const d=document.createElement("div");d.textContent=s==null?"":s;return d.innerHTML;}
function col(u){let h=0;for(const c of u)h=(h*31+c.charCodeAt(0))%360;return "hsl("+h+",70%,62%)";}
function av(u,n){return '<div class="av" style="background:'+col(u)+'">'+esc((n||"?")[0].toUpperCase())+'</div>';}
function ago(t){const s=Date.now()/1000-t;if(s<60)return "now";if(s<3600)return Math.floor(s/60)+"m";if(s<86400)return Math.floor(s/3600)+"h";return Math.floor(s/86400)+"d";}
async function join(){try{me=await api("join",{nick:$("nick").value.trim()});localStorage.setItem("hg_me",JSON.stringify(me));$("join").style.display="none";start();}catch(e){alert(e.message);}}
$("go").onclick=join;$("nick").onkeydown=e=>{if(e.key==="Enter")join();};
if(me){$("join").style.display="none";}
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>show(b.dataset.v));
function show(v){view=v;document.querySelectorAll("nav button").forEach(b=>b.classList.toggle("on",b.dataset.v===v));
 document.querySelectorAll(".view").forEach(s=>s.classList.toggle("on",s.id==="v-"+v));
 $("cform").classList.toggle("on",v==="chat");clearTimeout(timer);$("back").style.display="none";$("sub").textContent="";
 $("title").firstChild.textContent="\u{1F42F} Tygars Hangout";
 if(v==="feed")loadFeed(true);if(v==="chat")openRoom("hangout","Hangout");if(v==="friends")friendsView();if(v==="me")profileView(me.uid,$("v-me"));}
function start(){show("feed");refreshFriends();setInterval(refreshFriends,20000);}
/* ---------- feed ---------- */
$("sAll").onclick=()=>{scope="all";$("sAll").className="btn p";$("sFr").className="btn";loadFeed(true);};
$("sFr").onclick=()=>{scope="friends";$("sFr").className="btn p";$("sAll").className="btn";loadFeed(true);};
$("pb").onclick=async()=>{const t=$("pt").value.trim();if(!t)return;try{await api("post",{text:t});$("pt").value="";loadFeed(true);}catch(e){alert(e.message);}};
$("more").onclick=()=>loadFeed(false);
async function loadFeed(reset,box,of){box=box||$("posts");if(reset){oldest=0;box.innerHTML="";}
 try{const j=await api("feed",{scope:of?"all":scope,before:reset?0:oldest,of:of||""});
  if(reset&&!j.posts.length)box.innerHTML='<div class="empty">'+(of?"No posts yet.":scope==="friends"?"No posts from friends yet.":"No posts yet. Say hi to everyone!")+'</div>';
  j.posts.forEach(p=>{oldest=p.id;box.appendChild(postCard(p));});
  if(!of)$("more").style.display=j.posts.length>=20?"":"none";}catch(e){}}
function postCard(p){const d=document.createElement("div");d.className="card";
 d.innerHTML='<div class="who">'+av(p.uid,p.nick)+'<div><b>'+esc(p.nick)+'</b><br><small>'+ago(p.ts)+'</small></div></div><div class="body">'+esc(p.text)+'</div>'+
 '<div class="acts"><button class="lk'+(p.liked?" liked":"")+'">\u{1F44D} <span>'+p.likes+'</span></button><button class="cb">\u{1F4AC} '+p.comments+'</button><button class="mb">\u22EF</button></div><div class="cms"></div>';
 d.querySelector(".who b").onclick=()=>openProfile(p.uid);
 d.querySelector(".lk").onclick=async e=>{const b=e.currentTarget;const j=await api("like",{id:p.id});b.classList.toggle("liked");b.querySelector("span").textContent=j.likes;};
 d.querySelector(".cb").onclick=()=>comments(p,d.querySelector(".cms"));
 d.querySelector(".mb").onclick=()=>{if(p.uid===me.uid){if(confirm("Delete this post?"))api("delete",{id:p.id}).then(()=>d.remove());}
  else sheet('<h3>Post by '+esc(p.nick)+'</h3><div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn" id="x1">Report post</button><button class="btn" id="x2">View profile</button><button class="btn" id="x3">Close</button></div>',
   ()=>{$("x1").onclick=async()=>{await api("report",{kind:"post",id:p.id});close();d.remove();alert("Reported. Thanks for keeping the Hangout safe.");};$("x2").onclick=()=>{close();openProfile(p.uid);};$("x3").onclick=close;});};
 return d;}
async function comments(p,box){if(box.dataset.open){box.innerHTML="";delete box.dataset.open;return;}box.dataset.open=1;
 const j=await api("comments",{id:p.id});
 box.innerHTML=j.comments.map(c=>'<div class="cm"><b data-u="'+c.uid+'">'+esc(c.nick)+'</b> '+esc(c.text)+'</div>').join("")+
  '<div style="display:flex;gap:6px;margin-top:6px"><input placeholder="Write a comment" maxlength="500"><button class="btn p s">Send</button></div>';
 box.querySelectorAll("b[data-u]").forEach(b=>b.onclick=()=>openProfile(b.dataset.u));
 const inp=box.querySelector("input"),send=async()=>{const t=inp.value.trim();if(!t)return;try{await api("comment",{id:p.id,text:t});delete box.dataset.open;comments(p,box);}catch(e){alert(e.message);}};
 box.querySelector("button").onclick=send;inp.onkeydown=e=>{if(e.key==="Enter")send();};}
/* ---------- profiles ---------- */
async function openProfile(uid){if(uid===me.uid){show("me");return;}const box=document.createElement("div");sheet("",()=>{$("sheet").appendChild(box);});profileView(uid,box);}
async function profileView(uid,box){let p;try{p=await api("profile",{to:uid});}catch(e){box.innerHTML='<div class="empty">'+esc(e.message)+'</div>';return;}
 let h='<div class="card" style="text-align:center"><div style="display:flex;justify-content:center">'+av(p.uid,p.nick).replace('width:36px','')+'</div>'+
  '<h3 style="margin:8px 0 2px">'+esc(p.nick)+'</h3><div style="opacity:.6;font-size:13px"><i class="dot'+(p.online?" on":"")+'"></i>'+(p.online?"online":"offline")+' \u00B7 '+p.friends+' friends</div>'+
  '<p style="opacity:.85">'+(esc(p.bio)||'<span style="opacity:.5">No bio yet</span>')+'</p><div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap">';
 if(p.status==="me")h+='<button class="btn" id="eb">Edit bio</button><button class="btn" id="en">Change nickname</button></div><p style="font-size:13px;opacity:.6">Your friend code: <span class="code">'+me.uid+'</span></p>';
 else{h+=p.status==="friends"?'<button class="btn p" id="pc">Message</button><button class="btn" id="pr">Unfriend</button>':p.status==="pending_in"?'<button class="btn p" id="pa2">Accept request</button>':p.status==="pending_out"?'<button class="btn" disabled>Request sent</button>':'<button class="btn p" id="pa">Add friend</button>';
  h+='<button class="btn r" id="pbk">Block</button></div>';}
 h+='</div><h4 style="margin:6px 2px">Posts</h4><div class="pp"></div>';box.innerHTML=h;
 const q2=s=>box.querySelector(s);
 if(q2("#eb"))q2("#eb").onclick=async()=>{const b=prompt("Your bio (160 letters)",p.bio);if(b===null)return;await api("bio",{bio:b});profileView(uid,box);};
 if(q2("#en"))q2("#en").onclick=async()=>{const n=prompt("New nickname",me.nick);if(!n)return;try{me=await api("join",{nick:n});localStorage.setItem("hg_me",JSON.stringify(me));profileView(uid,box);}catch(e){alert(e.message);}};
 if(q2("#pa"))q2("#pa").onclick=()=>act("request",uid,"Friend request sent to "+p.nick+".").then(()=>profileView(uid,box));
 if(q2("#pa2"))q2("#pa2").onclick=()=>act("accept",uid,"You and "+p.nick+" are now friends.").then(()=>profileView(uid,box));
 if(q2("#pr"))q2("#pr").onclick=()=>{if(confirm("Unfriend "+p.nick+"?"))act("remove",uid).then(()=>profileView(uid,box));};
 if(q2("#pc"))q2("#pc").onclick=()=>{close();dm(uid,p.nick);};
 if(q2("#pbk"))q2("#pbk").onclick=()=>{if(confirm("Block "+p.nick+"? You won't see each other's posts or messages."))act("block",uid,p.nick+" is blocked.").then(()=>{close();if(view==="feed")loadFeed(true);});};
 loadFeed(true,q2(".pp"),uid);}
/* ---------- chat ---------- */
function dm(uid,nick){show("chat");openRoom(["dm"].concat([me.uid,uid].sort()).join(":"),nick);}
function openRoom(r,name){room=r;last=0;clearTimeout(timer);const log=$("chatlog");
 $("title").firstChild.textContent=(r==="hangout"?"\u{1F4AC} Hangout":"\u{1F512} "+name);$("back").style.display=r==="hangout"?"none":"";
 log.innerHTML=r==="hangout"?'<div class="sys">Open chat for everyone. Be kind and keep personal info private.</div>':'<div class="sys">Private chat with '+esc(name)+'. Only you two can see this.</div>';poll();}
$("back").onclick=()=>show(view==="chat"?"friends":view);
async function poll(){if(view!=="chat")return;const log=$("chatlog"),main=document.querySelector("main");
 try{const j=await api("msgs",{room,after:last});const near=main.scrollHeight-main.scrollTop-main.clientHeight<100;let mine=false;
  for(const m of j.msgs){last=Math.max(last,m.id);if(blocked.includes(m.uid))continue;mine=m.uid===me.uid;
   const d=document.createElement("div");d.className="m"+(m.uid===me.uid?" me":"");
   d.innerHTML=(m.uid===me.uid?"":'<div class="n">'+esc(m.nick)+' \u00B7 '+ago(m.ts)+'</div>')+'<div class="t">'+esc(m.text)+'</div>';
   if(m.uid!==me.uid)d.querySelector(".n").onclick=()=>person(m);log.appendChild(d);}
  if(near||mine)main.scrollTop=main.scrollHeight;if(room==="hangout")$("sub").textContent=j.online+" online";
 }catch(e){if(e.message==="auth"&&room!=="hangout"){openRoom("hangout","Hangout");return;}}
 timer=setTimeout(poll,2500);}
function person(m){sheet('<h3>'+esc(m.nick)+'</h3><div style="display:flex;flex-wrap:wrap;gap:8px"><button class="btn p" id="z1">View profile</button><button class="btn" id="z2">Report message</button><button class="btn" id="z3">Close</button></div>',
 ()=>{$("z1").onclick=()=>{close();openProfile(m.uid);};$("z2").onclick=async()=>{await api("report",{kind:"msg",id:m.id});close();alert("Reported. Thanks.");};$("z3").onclick=close;});}
const q=$("q");q.oninput=()=>{q.style.height="auto";q.style.height=q.scrollHeight+"px";};
q.onkeydown=e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();$("cform").requestSubmit();}};
$("cform").onsubmit=async e=>{e.preventDefault();const t=q.value.trim();if(!t)return;q.value="";q.style.height="auto";
 try{await api("send",{room,text:t});clearTimeout(timer);poll();}catch(err){alert(err.message);q.value=t;}};
/* ---------- friends ---------- */
function sheet(h,after){$("sheet").innerHTML=h;$("ov").classList.add("on");if(after)after();}
$("ov").onclick=e=>{if(e.target.id==="ov")close();};function close(){$("ov").classList.remove("on");}
async function act(a,to,msg){try{await api("friend",{action:a,to});await refreshFriends();if(msg)alert(msg);if(view==="friends")friendsView();}catch(e){alert(e.message);}}
async function refreshFriends(){try{const j=await api("friends");friends=j.friends;blocked=j.blocked;
 const n=friends.filter(f=>f.status==="pending_in").length;$("nb").innerHTML=n?'<span class="badge">'+n+'</span>':"";}catch(e){}}
async function friendsView(){await refreshFriends();const box=$("v-friends");
 const inc=friends.filter(f=>f.status==="pending_in"),fr=friends.filter(f=>f.status==="friends"),out=friends.filter(f=>f.status==="pending_out");
 let h='<div class="card"><div style="font-size:14px;opacity:.75;margin-bottom:8px">Your friend code: <span class="code">'+me.uid+'</span> \u2014 share it so people can add you.</div>'+
  '<div style="display:flex;gap:8px"><input id="fc" placeholder="Enter a friend code"><button class="btn p" id="fa">Add</button></div></div>';
 if(inc.length){h+='<h4>Friend requests</h4>';inc.forEach(f=>h+='<div class="row"><span data-p="'+f.uid+'">'+esc(f.nick)+'</span><button class="btn p s" data-a="accept" data-u="'+f.uid+'">Confirm</button><button class="btn s" data-a="decline" data-u="'+f.uid+'">Delete</button></div>');}
 h+='<h4>Friends ('+fr.length+')</h4>'+(fr.length?"":'<div class="empty">No friends yet. Tap a name on a post or in the Hangout to add someone.</div>');
 fr.forEach(f=>h+='<div class="row"><span data-p="'+f.uid+'"><i class="dot'+(f.online?" on":"")+'"></i>'+esc(f.nick)+'</span><button class="btn p s" data-c="'+f.uid+'">Message</button></div>');
 if(out.length){h+='<h4>Sent requests</h4>';out.forEach(f=>h+='<div class="row"><span data-p="'+f.uid+'">'+esc(f.nick)+'</span><button class="btn s" data-a="decline" data-u="'+f.uid+'">Cancel</button></div>');}
 if(blocked.length)h+='<p style="font-size:13px;opacity:.6">'+blocked.length+' blocked \u00B7 <a href="#" id="ub">Unblock all</a></p>';
 box.innerHTML=h;
 $("fa").onclick=()=>{const c=$("fc").value.trim();if(c)act("request",c,"Friend request sent.");};
 box.querySelectorAll("[data-a]").forEach(b=>b.onclick=()=>act(b.dataset.a,b.dataset.u));
 box.querySelectorAll("[data-p]").forEach(b=>b.onclick=()=>openProfile(b.dataset.p));
 box.querySelectorAll("[data-c]").forEach(b=>b.onclick=()=>{const f=friends.find(x=>x.uid===b.dataset.c);dm(f.uid,f.nick);});
 if($("ub"))$("ub").onclick=async e=>{e.preventDefault();for(const u of blocked)await api("friend",{action:"unblock",to:u});friendsView();};}
if(me)start();
</script></body></html>"""
