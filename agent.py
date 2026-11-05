"""Tygar Agent: a look-only web agent with a live view.

It searches, opens public pages, clicks links and scrolls, step by step, then sums up what it found.
Everyone can watch it live on a web page, and the Telegram message updates with each step.
Never: logins, typing into forms, posting, buying, sign-ups, or reading anyone's email.
"""
import json, re, secrets, threading, time
from urllib.parse import quote_plus, urlparse

from flask import Blueprint, Response, request

bp = Blueprint("agent", __name__)
H = {}            # helpers from app.py, set by init()
LIVE = {}         # token -> {"frame": bytes, "steps": [...], "status": str, "done": bool, "goal": str, "ts": float}
DAILY = 3
MAX_STEPS = 12
TIME_LIMIT = 300

# Flip to True before an upload/deploy to lock the whole web app behind an
# "Updating..." screen; flip back to False in the same commit that ships the
# finished change. While True, pages show the lock and all APIs return 503.
UPDATING = True
_MAINT_OK = {"/agent/logo.png", "/agent/favicon.png", "/favicon.ico",
             "/agent/manifest.json", "/agent/icon-192.png", "/agent/icon-512.png",
             "/agent/sw.js", "/.well-known/assetlinks.json"}

MAINT_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tygar is updating</title><link rel="icon" href="/agent/favicon.png"><meta http-equiv="refresh" content="15"><style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;
color:#fff;font-family:"Segoe UI",system-ui,sans-serif;padding:24px;
background:linear-gradient(160deg,#5b0eeb,#c2189b 45%,#ff6a00 100%);background-size:200% 200%;animation:bg 14s ease infinite}
@keyframes bg{0%,100%{background-position:0 0}50%{background-position:100% 100%}}
img{width:88px;height:88px;border-radius:50%;border:3px solid #fff;box-shadow:0 0 0 3px #ffe600,0 0 30px #ffe600aa;background:#000;margin-bottom:22px}
.sp{width:46px;height:46px;border:5px solid #ffffff55;border-top-color:#fff;border-radius:50%;animation:spin 1s linear infinite;margin:6px 0 18px}
@keyframes spin{to{transform:rotate(360deg)}}
h1{font-size:28px;margin:0 0 8px;font-weight:900}p{font-size:17px;opacity:.95;max-width:420px;margin:0}</style></head>
<body><img src="/agent/logo.png" alt=""><div class="sp"></div>
<h1>Tygar is updating \U0001F6E0</h1><p>I'm shipping a new version right now. This page comes back on its own in a moment \u2014 nothing's broken.</p></body></html>"""


@bp.before_request
def _maint_gate():
    if not UPDATING:
        return None
    p = request.path
    if p in _MAINT_OK or p == "/agent/tygar.apk":
        return None
    if request.method == "GET" and (p == "/agent" or p.startswith("/live/")):
        return Response(MAINT_HTML, mimetype="text/html", status=503)
    return Response('{"error":"Tygar is updating, back in a moment."}',
                    mimetype="application/json", status=503)
_uses = {}        # (uid, day) -> n
_queue = [0]

BLOCK_WORDS = re.compile(r"(log ?in|sign ?in|sign ?up|register|checkout|/cart|basket|payment|/pay\b|wallet|account|password|otp|"
                         r"mail\.google|outlook\.live|mail\.yahoo|webmail|bank)", re.I)
BAD_GOAL = re.compile(r"\b(sign ?up|register|create (an |a )?account|log ?in|sign ?in|password|otp|my (e-?mail|inbox|mail)|"
                      r"read (my )?(e-?mail|inbox)|post (on|to)|tweet|comment on|like (posts|my)|follow (people|accounts)|"
                      r"buy|order|pay|book (a|me)|send (money|a message|an email)|hack|crack|bypass|captcha)\b", re.I)

SYSTEM = """You are Tygar Agent, a careful web researcher driving a read-only browser for a user in Africa.
Each turn you get the goal, your notes so far, and the current page (URL, title, text, numbered links).
Reply with ONLY a JSON object, no other text:
{"say": "<one short line for the user about what you're doing, max 12 words>",
 "note": "<facts from THIS page that help the goal, with numbers/prices/names, or empty>",
 "action": "search" | "goto" | "click" | "scroll" | "back" | "done",
 "arg": "<search words, a full https URL, a link number, or empty>"}
Rules: you can only search, open public pages, click the numbered links, scroll and go back.
Never try to log in, sign up, fill forms, buy, post or message anyone. If the goal needs that, choose done and explain.
If a results page is empty or you already ran that search, do NOT search the same words again: open a result, try different words, or goto a reliable site directly (e.g. https://punchng.com, https://www.bbc.com/sport/football, https://www.jumia.com.ng).
Prefer reliable sites. Finish with done as soon as your notes answer the goal (usually 3-8 steps)."""


def init(**helpers):
    H.update(helpers)


# ---------- Web push reminders for the to-do list ----------
import os as _os

_PUSH = {"pem": None, "pub": None, "ready": False}
_push_started = [False]
PUSH_SUB = "mailto:harkbotofficial@gmail.com"


def _pq(sql, args=(), fetch=False):
    from hangout import q
    if not _os.environ.get("DATABASE_URL"):
        sql = (sql.replace("bigserial primary key", "integer primary key autoincrement")
                  .replace("double precision", "real"))
    return q(sql, args, fetch=fetch)


def _push_init():
    if _PUSH["ready"]:
        return
    try:
        _pq("create table if not exists tygar_settings (k text primary key, v text)")
        _pq("create table if not exists tygar_reminders (id bigserial primary key, sub text, task text, "
            "due double precision, sent int default 0, created double precision)")
    except Exception as e:
        print("push table err:", e)
        return
    try:
        row = _pq("select v from tygar_settings where k='vapid'", fetch=True)
        if row:
            d = json.loads(row[0][0])
            _PUSH["pem"], _PUSH["pub"] = d["pem"], d["pub"]
            _PUSH["ready"] = True
            return
    except Exception as e:
        print("push load err:", e)
    try:
        import base64
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        priv = ec.generate_private_key(ec.SECP256R1())
        pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()).decode()
        raw = priv.public_key().public_bytes(serialization.Encoding.X962,
                                              serialization.PublicFormat.UncompressedPoint)
        pub = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        _pq("insert into tygar_settings (k, v) values ('vapid', ?) "
            "on conflict (k) do update set v=excluded.v", (json.dumps({"pem": pem, "pub": pub}),))
        _PUSH["pem"], _PUSH["pub"], _PUSH["ready"] = pem, pub, True
    except Exception as e:
        print("push keygen err:", e)


def _push_send(sub_json, title, body, tag=None):
    from pywebpush import webpush
    from py_vapid import Vapid
    vp = Vapid.from_pem(_PUSH["pem"].encode())
    webpush(subscription_info=json.loads(sub_json),
            data=json.dumps({"title": title, "body": body, "tag": tag or ""}),
            vapid_private_key=vp, vapid_claims={"sub": PUSH_SUB})


def _push_loop():
    time.sleep(15)
    _push_init()
    while True:
        try:
            now = time.time()
            rows = _pq("select id, sub, task from tygar_reminders where sent=0 and due<=? and due>?",
                       (now, now - 6 * 3600), fetch=True) or []
            for rid, sub, task in rows:
                claimed = _pq("update tygar_reminders set sent=1 where id=? and sent=0 returning id",
                              (rid,), fetch=True)
                if not claimed:
                    continue
                try:
                    _push_send(sub, "\u23F0 Tygar reminder", task, tag="rem" + str(rid))
                except Exception as e:
                    print("push send err:", e)
            _pq("delete from tygar_reminders where sent=1 and due<?", (now - 3 * 86400,))
        except Exception as e:
            print("push loop err:", e)
        time.sleep(30)


def _push_start():
    if _push_started[0]:
        return
    _push_started[0] = True
    threading.Thread(target=_push_loop, daemon=True).start()


_push_start()


@bp.get("/agent/push/key")
def push_key():
    if not _PUSH["ready"]:
        _push_init()
    return Response(json.dumps({"key": _PUSH["pub"] or ""}), mimetype="application/json")


@bp.post("/agent/push/remind")
def push_remind():
    d = request.get_json(silent=True) or {}
    sub = d.get("sub")
    task = (d.get("task") or "").strip()[:200]
    due = d.get("due")
    if not sub or not task or due is None:
        return Response('{"error":"missing"}', status=400, mimetype="application/json")
    try:
        due = float(due)
    except Exception:
        return Response('{"error":"bad time"}', status=400, mimetype="application/json")
    try:
        rid = _pq("insert into tygar_reminders (sub, task, due, created) values (?,?,?,?) returning id",
                  (json.dumps(sub), task, due, time.time()), fetch=True)
        return Response(json.dumps({"id": (rid[0][0] if rid else 1)}), mimetype="application/json")
    except Exception as e:
        print("remind save err:", e)
        return Response('{"error":"save failed"}', status=500, mimetype="application/json")


def _day():
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 3600))


def quota_left(uid):
    if int(uid) in H.get("dev_ids", set()):
        return 99
    return DAILY - _uses.get((uid, _day()), 0)


def _json(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _page_state(pg):
    title = (pg.title() or "")[:120]
    text = pg.evaluate("document.body ? document.body.innerText : ''")[:2500]
    links = pg.evaluate("""() => {
        const seen = new Set(); const out = [];
        for (const a of document.querySelectorAll('a[href^="http"]')) {
            const r = a.getBoundingClientRect();
            const t = (a.innerText || a.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').slice(0, 70);
            if (t.length < 3 || seen.has(a.href) || r.width < 2 || r.height < 2) continue;
            seen.add(a.href); out.push([t, a.href]);
            if (out.length >= 25) break;
        }
        return out; }""")
    links = [(t, h) for t, h in links if not BLOCK_WORDS.search(h)]
    return title, text, links


def _ok_url(u):
    if not u or BLOCK_WORDS.search(u):
        return None
    return H["safe_url"](u)


ENGINES = [("https://html.duckduckgo.com/html/?q=", "a.result__a"),
           ("https://www.bing.com/search?setlang=en&cc=NG&q=", "li.b_algo h2 a"),
           ("https://www.mojeek.com/search?q=", "ul.results-standard li a.ob, ul.results-standard li h2 a")]


def _search(pg, q):
    """Open a results page; fall back to the next engine when one blocks the browser."""
    for base, sel in ENGINES:
        host = urlparse(base).hostname
        try:
            pg.goto(base + quote_plus(q), wait_until="domcontentloaded", timeout=25000)
            pg.wait_for_timeout(1200)
            n = pg.evaluate("s => document.querySelectorAll(s).length", sel)
            if n >= 3:
                return True
            print("agent search: few results on", host, n)
        except Exception as e:
            print("agent search error:", base, str(e)[:150])
    return False


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 TygarsBot"
HEAVY = re.compile(r"(doubleclick|googlesyndication|adservice|googletagmanager|google-analytics|facebook\.net|taboola|outbrain|"
                   r"criteo|amazon-adsystem|adnxs|scorecardresearch|hotjar|quantserve|moatads|pubmatic|rubiconproject)", re.I)


def _route(r):
    rq = r.request
    if rq.resource_type in ("media", "websocket", "font") or HEAVY.search(rq.url):
        return r.abort()
    return r.continue_()


AGENT_MODELS = ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
LAST_ERR = [""]


def _llm(msgs, json_mode=True, limit=80):
    """Fast Groq call with a hard time limit, so a slow model never freezes the agent."""
    import concurrent.futures as cf

    def call(model):
        body = {"model": model, "messages": msgs, "max_tokens": 700, "temperature": 0.2}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if model.startswith("openai/"):
            body["reasoning_effort"] = "low"
            body["max_tokens"] = 1500
        r = H["requests"].post("https://api.groq.com/openai/v1/chat/completions", timeout=(20, 75),
                               headers={"Authorization": f"Bearer {H['groq_key']}"}, json=body)
        if r.status_code >= 300:
            raise RuntimeError(f"groq {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"].get("content") or ""

    for model in AGENT_MODELS[:2]:
        ex = cf.ThreadPoolExecutor(max_workers=1)
        try:
            return ex.submit(call, model).result(timeout=limit)
        except Exception as e:
            LAST_ERR[0] = f"{model}: {type(e).__name__} {str(e)[:150]}"
            print("agent llm", model, "failed:", str(e)[:200])
        finally:
            ex.shutdown(wait=False)
    return ""


def _tg(method, **kw):
    try:
        return H["requests"].post(f"https://api.telegram.org/bot{H['token']}/{method}", timeout=30, **kw)
    except Exception as e:
        print("agent tg error:", e)


def _update_tg(chat, mid, live, frame):
    if not chat:
        return
    steps = live["steps"][-6:]
    cap = (f"\U0001F916 Tygar Agent: {live['goal'][:80]}\n\n" + "\n".join(steps) + f"\n\n{live['status']}")[:1000]
    kb = {"inline_keyboard": [[{"text": "\U0001F4FA Watch live", "url": live["url"]}]]}
    if mid and frame:
        _tg("editMessageMedia", data={"chat_id": chat, "message_id": mid,
                                      "media": json.dumps({"type": "photo", "media": "attach://f", "caption": cap}),
                                      "reply_markup": json.dumps(kb)}, files={"f": ("live.jpg", frame, "image/jpeg")})
    elif mid:
        _tg("editMessageCaption", json={"chat_id": chat, "message_id": mid, "caption": cap, "reply_markup": kb})


def start(chat, uid, goal):
    send = H["send"]
    goal = (goal or "").strip()
    if not goal:
        return send(chat, "\U0001F916 Tell me what to find, e.g.\n/agent cheapest iPhone 13 on Jumia Nigeria\n/agent latest Super Eagles news\n/agent best laptops under 400k naira")
    if BAD_GOAL.search(goal):
        return send(chat, "I'm a look-only agent: I search, read and compare public pages for you. "
                          "I never log in, sign up, read email, post, buy or message anyone. Try something like: /agent compare data plans MTN vs Airtel")
    if quota_left(uid) <= 0:
        return send(chat, f"You've used today's {DAILY} free agent tasks. Come back tomorrow!")
    _uses[(uid, _day())] = _uses.get((uid, _day()), 0) + 1
    token = secrets.token_urlsafe(9)
    url = H["base_url"] + "/live/" + token
    live = {"frame": b"", "steps": [], "status": "\u23F3 Starting the browser...", "done": False, "goal": goal, "ts": time.time(), "url": url}
    LIVE[token] = live
    r = _tg("sendPhoto", data={"chat_id": chat, "caption": f"\U0001F916 Tygar Agent: {goal[:80]}\n\n\u23F3 Getting ready...",
                               "reply_markup": json.dumps({"inline_keyboard": [[{"text": "\U0001F4FA Watch live", "url": url}]]})},
            files={"photo": ("logo.jpg", H["placeholder"](), "image/jpeg")})
    mid = None
    try:
        mid = r.json()["result"]["message_id"]
    except Exception:
        pass
    threading.Thread(target=_run, args=(chat, mid, token, goal), daemon=True).start()


def web_start(goal, who, limit=DAILY):
    goal = (goal or "").strip()[:200]
    if len(goal) < 4:
        return {"error": "Tell me what to find, e.g. cheapest iPhone 13 on Jumia Nigeria."}
    if BAD_GOAL.search(goal):
        return {"error": "I'm a look-only agent: I search, read and compare public pages. I never log in, sign up, read email, post, buy or message anyone."}
    key = ("web:" + who, _day())
    if _uses.get(key, 0) >= limit:
        extra = "" if limit > DAILY else f" Sign in to get {DAILY_SIGNED} a day."
        return {"error": f"You've used today's {limit} free tasks. Come back tomorrow!" + extra}
    _uses[key] = _uses.get(key, 0) + 1
    token = secrets.token_urlsafe(9)
    LIVE[token] = {"frame": b"", "steps": [], "status": "\u23F3 Starting the browser...", "done": False, "goal": goal,
                   "ts": time.time(), "url": H["base_url"] + "/live/" + token, "answer": ""}
    threading.Thread(target=_run_light, args=(token, goal), daemon=True).start()
    return {"token": token}


def _run(chat, mid, token, goal):
    live = LIVE[token]
    lock = H["lock"]
    if not lock.acquire(blocking=False):
        live["status"] = "\u23F3 Another task is running, you're in the queue..."
        _update_tg(chat, mid, live, None)
        lock.acquire()
    notes, answer = [], ""
    try:
        if not H["ensure_browser"]():
            raise RuntimeError("browser not available")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = H["launch"](p)
            try:
                state = {"b": b}

                def fresh_page():
                    """New context/page; relaunch Chromium if a heavy page crashed it (512 MB server)."""
                    try:
                        state["b"].contexts
                        c = state["b"].new_context(viewport={"width": 1000, "height": 640}, device_scale_factor=1, user_agent=UA)
                    except Exception:
                        try:
                            state["b"].close()
                        except Exception:
                            pass
                        state["b"] = H["launch"](p)
                        c = state["b"].new_context(viewport={"width": 1000, "height": 640}, device_scale_factor=1, user_agent=UA)
                    c.route("**/*", _route)
                    np = c.new_page()
                    np.set_default_timeout(20000)
                    np.set_default_navigation_timeout(30000)
                    return np

                pg = fresh_page()
                _search(pg, goal)
                history = [pg.url]
                live["steps"].append(f"1. \U0001F50E Searching: {goal[:60]}")
                t0 = time.time()
                for step in range(2, MAX_STEPS + 2):
                    try:
                        pg.wait_for_timeout(1200)
                        frame = pg.screenshot(type="jpeg", quality=55)
                    except Exception as e:
                        print("agent page crashed, recovering:", str(e)[:120])
                        live["steps"].append(f"{step}. \u26A0 That page was too heavy, going back to the results")
                        try:
                            pg.context.close()
                        except Exception:
                            pass
                        pg = fresh_page()
                        _search(pg, goal)
                        continue
                    live["frame"] = frame
                    live["status"] = f"\U0001F310 On {urlparse(pg.url).hostname or ''}"
                    _update_tg(chat, mid, live, frame)
                    if time.time() - t0 > TIME_LIMIT:
                        break
                    live["phase"] = f"reading page {step}"
                    title, text, links = _page_state(pg)
                    live["phase"] = f"thinking {step}"
                    numbered = "\n".join(f"[{i}] {t}" for i, (t, h) in enumerate(links))
                    msgs = [{"role": "system", "content": SYSTEM},
                            {"role": "user", "content": f"Goal: {goal}\nStep {step} of {MAX_STEPS + 1}.\nYour actions so far:\n" + "\n".join(live["steps"][-6:]) + "\nNotes so far:\n" + ("\n".join(notes) or "(none)") +
                             f"\n\nURL: {pg.url}\nTitle: {title}\nText:\n{text}\n\nLinks:\n{numbered}"}]
                    print("agent step", step, pg.url[:80], len(links), "links")
                    # Close the page while the AI thinks: the 512 MB free server has no room for both
                    cur_url = pg.url
                    try:
                        cur_y = pg.evaluate("window.scrollY") or 0
                    except Exception:
                        cur_y = 0
                    try:
                        pg.goto("about:blank", timeout=10000)
                    except Exception:
                        pass
                    t1 = time.time()
                    try:
                        d = _json(_llm(msgs)) or {}
                    except Exception as e:
                        print("agent llm error:", e)
                        d = {}
                    live["llm_s"] = round(time.time() - t1, 1)
                    if d.get("note"):
                        notes.append(f"- ({pg.url[:80]}) {str(d['note'])[:400]}")
                    live["phase"] = f"acting {step}: {d.get('action')}"
                    act, arg = (d.get("action") or "done"), str(d.get("arg") or "").strip()
                    say = str(d.get("say") or act)[:70]
                    try:
                        if act == "search" and arg:
                            live["steps"].append(f"{step}. \U0001F50E {say}")
                            _search(pg, arg)
                        elif act == "goto" and _ok_url(arg):
                            live["steps"].append(f"{step}. \U0001F517 {say}")
                            pg.goto(_ok_url(arg), wait_until="domcontentloaded", timeout=30000)
                        elif act == "click" and arg.isdigit() and int(arg) < len(links) and _ok_url(links[int(arg)][1]):
                            live["steps"].append(f"{step}. \U0001F446 {say}")
                            pg.goto(_ok_url(links[int(arg)][1]), wait_until="domcontentloaded", timeout=30000)
                        elif act == "scroll":
                            live["steps"].append(f"{step}. \u2B07 {say}")
                            pg.goto(cur_url, wait_until="domcontentloaded", timeout=30000)
                            pg.wait_for_timeout(800)
                            pg.evaluate("y => window.scrollTo(0, y)", cur_y + 650)
                        elif act == "back":
                            live["steps"].append(f"{step}. \u21A9 {say}")
                            prev = history[-2] if len(history) > 1 else None
                            if prev:
                                history.pop()
                                pg.goto(prev, wait_until="domcontentloaded", timeout=30000)
                            else:
                                _search(pg, goal)
                        else:
                            live["steps"].append(f"{step}. \u2705 {say}")
                            break
                        if act in ("search", "goto", "click"):
                            history.append(pg.url)
                    except Exception as e:
                        live["steps"].append(f"{step}. \u26A0 That page didn't load, trying another way")
                        print("agent step error:", str(e)[:200])
                try:
                    pg.context.close()
                except Exception:
                    pass
            finally:
                try:
                    state["b"].close()
                except Exception:
                    pass
        live["status"] = "\U0001F9E0 Writing up what I found..."
        _update_tg(chat, mid, live, None)
        msgs = [{"role": "system", "content": "Answer the user's goal using ONLY these research notes. Be concrete: names, prices, numbers. "
                                              "Plain text, short, no markdown tables. Say if something couldn't be confirmed. End with the main source links."},
                {"role": "user", "content": f"Goal: {goal}\n\nNotes:\n" + ("\n".join(notes) or "(no useful notes)")}]
        try:
            answer = re.sub(r"<think>.*?</think>", "", _llm(msgs, json_mode=False, limit=80), flags=re.S).strip()
        except Exception as e:
            print("agent final error:", e)
        live["status"] = "\u2705 Done"
    except Exception as e:
        print("agent error:", str(e)[:300])
        live["phase"] = (live.get("phase", "") + " | error: " + type(e).__name__ + " " + str(e)[:120])
        live["status"] = "\u26A0 Sorry, the agent hit a problem. Please try again later."
    finally:
        live["done"] = True
        lock.release()
    live["answer"] = answer or "Sorry, I couldn't find a clear answer this time. Try rewording the task."
    _update_tg(chat, mid, live, None)
    if chat:
        H["send"](chat, ("\U0001F916 " + live["answer"])[:4000])


# ---- Light mode: no Chromium. The free server (0.1 CPU) starves when a browser runs, so we read pages over HTTP. ----
from html.parser import HTMLParser
from urllib.parse import urljoin, parse_qs, unquote

HDRS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
        "Accept-Language": "en-NG,en;q=0.9"}


class _Reader(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "iframe", "form", "nav", "footer"}

    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.depth, self.title, self.text, self.links, self._a, self._in_title = base, 0, "", [], [], None, False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "a" and not self.depth:
            h = dict(attrs).get("href") or ""
            self._a = [urljoin(self.base, h), ""]
        if tag in ("p", "br", "li", "h1", "h2", "h3", "div", "tr"):
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth:
            self.depth -= 1
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._a:
            t = re.sub(r"\s+", " ", self._a[1]).strip()[:70]
            if len(t) >= 3 and self._a[0].startswith("http"):
                self.links.append((t, self._a[0]))
            self._a = None

    def handle_data(self, d):
        if self._in_title:
            self.title += d
        if self.depth:
            return
        self.text.append(d)
        if self._a is not None:
            self._a[1] += d


def _unwrap(u):
    """Search engines wrap result links; pull out the real address."""
    try:
        q = parse_qs(urlparse(u).query)
        if "duckduckgo.com" in u and q.get("uddg"):
            return unquote(q["uddg"][0])
        if "yahoo.com" in u and "/RU=" in u:
            return unquote(u.split("/RU=", 1)[1].split("/R", 1)[0])
        if "bing.com/ck/a" in u and q.get("u"):
            import base64
            b = q["u"][0][2:]
            return base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode("utf-8", "ignore")
    except Exception:
        pass
    return u


def _fetch(url):
    r = H["requests"].get(url, headers=HDRS, timeout=(8, 15), allow_redirects=True)
    if "text/html" not in r.headers.get("content-type", "text/html"):
        raise RuntimeError("not a web page")
    rd = _Reader(r.url)
    rd.feed(r.text[:600000])
    text = re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", "".join(rd.text))).strip()
    seen, links = set(), []
    for t, h in rd.links:
        h = _unwrap(h)
        if h in seen or BLOCK_WORDS.search(h) or not h.startswith("http"):
            continue
        seen.add(h)
        links.append((t, h))
    return r.url, (rd.title or "").strip()[:120], text, links


LIGHT_ENGINES = ["https://html.duckduckgo.com/html/?q=", "https://search.yahoo.com/search?p=", "https://www.mojeek.com/search?q=",
                 "https://www.startpage.com/do/search?q=", "https://www.bing.com/search?setlang=en&cc=NG&q="]
ENGINE_HOSTS = ("duckduckgo.com", "yahoo.com", "mojeek.com", "startpage.com", "bing.com", "microsoft.com", "yimg.com", "msn.com/en-us/feed")


_ENGINE_USED = [""]


def _lsearch(q):
    for base in LIGHT_ENGINES:
        try:
            url, title, text, links = _fetch(base + quote_plus(q))
            host = urlparse(base).hostname
            res = [(t, h) for t, h in links if not any(e in h for e in ENGINE_HOSTS)]
            words = {w for w in re.findall(r"[a-z0-9]{4,}", q.lower())} - {"latest", "best", "news", "what", "with", "from"}
            hits = sum(1 for t, h in res[:12] if any(w in (t + " " + h).lower() for w in words)) if words else len(res)
            if len(res) >= 3 and hits >= 2:
                _ENGINE_USED[0] = host
                return url, f"Search: {q}", text, res[:25]
            print("agent light search: weak results on", host, len(res), hits)
        except Exception as e:
            print("agent light search error:", base, str(e)[:150])
    try:   # last resort: Bing News RSS (a feed, not a scraped page)
        import xml.etree.ElementTree as ET
        r = H["requests"].get("https://www.bing.com/news/search", params={"q": q, "format": "rss", "cc": "NG"}, headers=HDRS, timeout=15)
        items, lines = [], []
        for it in ET.fromstring(r.content).iter("item"):
            t, l = (it.findtext("title") or "").strip(), it.findtext("link") or ""
            real = parse_qs(urlparse(l).query).get("url", [l])[0]
            if t and real.startswith("http") and not BLOCK_WORDS.search(real):
                items.append((t[:90], real))
                lines.append(f"{t} ({it.findtext('pubDate') or ''})\n{(it.findtext('description') or '')[:250]}")
        if items:
            _ENGINE_USED[0] = "bing news rss"
            return r.url, f"News search: {q}", "\n\n".join(lines), items[:20]
    except Exception as e:
        print("agent rss error:", str(e)[:150])
    raise RuntimeError("search engines unavailable")


_SHOTS = {"day": "", "n": 0}
SHOT_DAILY = 45


def _shot_async(live, url):
    """Real page screenshot via free Microlink (about 50 a day), shown in the live view. Text view stays as fallback."""
    day = time.strftime("%Y-%m-%d")
    if _SHOTS["day"] != day:
        _SHOTS.update(day=day, n=0)
    if _SHOTS["n"] >= SHOT_DAILY or not url:
        return
    _SHOTS["n"] += 1

    def go():
        try:
            rq = H["requests"]
            r = rq.get("https://api.microlink.io/", timeout=(5, 25), params={
                "url": url, "screenshot": "true", "meta": "false", "viewport.width": 412, "viewport.height": 780,
                "viewport.isMobile": "true", "screenshot.type": "jpeg"})
            j = r.json()
            if j.get("status") != "success":
                if "rate" in str(j.get("message", "")).lower() or r.status_code == 429:
                    _SHOTS["n"] = SHOT_DAILY
                return
            img = rq.get(j["data"]["screenshot"]["url"], timeout=(5, 25)).content
            if img and len(img) > 2000 and live.get("view", {}).get("url") == url:
                live["frame"] = img
                live["fn"] = live.get("fn", 0) + 1
        except Exception as e:
            print("agent shot error:", str(e)[:150])
    threading.Thread(target=go, daemon=True).start()


def _run_light(token, goal):
    live = LIVE[token]
    notes, answer, history = [], "", []
    try:
        url, title, text, links = _lsearch(goal)
        history.append(url)
        live["steps"].append(f"1. \U0001F50E Searching: {goal[:60]}")
        t0, y = time.time(), 0
        for step in range(2, MAX_STEPS + 2):
            if live.get("view", {}).get("url") != url:
                live["view"] = {"url": url}
                if "duckduckgo" not in url and "bing.com" not in url:
                    _shot_async(live, url)
            live["view"] = {"url": url, "title": title, "text": text[y:y + 700], "links": [t for t, h in links[:8]]}
            live["status"] = f"\U0001F310 On {urlparse(url).hostname or ''}"
            if time.time() - t0 > TIME_LIMIT:
                break
            live["phase"] = f"thinking {step}"
            numbered = "\n".join(f"[{i}] {t} ({urlparse(h).hostname})" for i, (t, h) in enumerate(links[:25]))
            msgs = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": f"Goal: {goal}\nStep {step} of {MAX_STEPS + 1}.\nYour actions so far:\n" + "\n".join(live["steps"][-6:]) +
                     "\nNotes so far:\n" + ("\n".join(notes) or "(none)") + f"\n\nURL: {url}\nTitle: {title}\nText:\n{text[y:y + 2500]}\n\nLinks:\n{numbered}"}]
            t1 = time.time()
            d = _json(_llm(msgs)) or {}
            live["llm_s"] = round(time.time() - t1, 1)
            if d.get("note"):
                notes.append(f"- ({url[:80]}) {str(d['note'])[:400]}")
            act, arg = (d.get("action") or "done"), str(d.get("arg") or "").strip()
            live["phase"] = f"acting {step}: {act}"
            say = str(d.get("say") or act)[:70]
            try:
                if act == "search" and arg:
                    live["steps"].append(f"{step}. \U0001F50E {say}")
                    url, title, text, links = _lsearch(arg); y = 0
                elif act in ("goto", "click"):
                    target = arg if act == "goto" else (links[int(arg)][1] if arg.isdigit() and int(arg) < len(links) else "")
                    target = _ok_url(target)
                    if not target:
                        live["steps"].append(f"{step}. \u26A0 Skipped a page I'm not allowed to open")
                        continue
                    icon = "\U0001F517" if act == "goto" else "\U0001F446"
                    live["steps"].append(f"{step}. {icon} {say}")
                    url, title, text, links = _fetch(target); y = 0
                    history.append(url)
                elif act == "scroll":
                    live["steps"].append(f"{step}. \u2B07 {say}")
                    y = min(y + 2500, max(0, len(text) - 500))
                elif act == "back" and len(history) > 1:
                    live["steps"].append(f"{step}. \u21A9 {say}")
                    history.pop()
                    url, title, text, links = _fetch(history[-1]); y = 0
                else:
                    live["steps"].append(f"{step}. \u2705 {say}")
                    break
            except Exception as e:
                live["steps"].append(f"{step}. \u26A0 That page didn't load, trying another way")
                print("agent light step error:", str(e)[:200])
        live["status"] = "\U0001F9E0 Writing up what I found..."
        msgs = [{"role": "system", "content": "Answer the user's goal using ONLY these research notes. Be concrete: names, prices, numbers. "
                                              "Plain text, short, no markdown tables. Say if something couldn't be confirmed. End with the main source links."},
                {"role": "user", "content": f"Goal: {goal}\n\nNotes:\n" + ("\n".join(notes) or "(no useful notes)")}]
        answer = re.sub(r"<think>.*?</think>", "", _llm(msgs, json_mode=False, limit=80), flags=re.S).strip()
        live["status"] = "\u2705 Done"
    except Exception as e:
        print("agent light error:", str(e)[:300])
        live["phase"] = live.get("phase", "") + " | error: " + type(e).__name__ + " " + str(e)[:120]
        live["status"] = "\u26A0 Sorry, the agent hit a problem. Please try again later."
    finally:
        live["done"] = True
    live["answer"] = answer or "Sorry, I couldn't find a clear answer this time. Try rewording the task."


def _cleanup():
    now = time.time()
    for k in [k for k, v in LIVE.items() if now - v["ts"] > 3600]:
        LIVE.pop(k, None)


# ---- Sign-in to the Agent page (email code). No site logins ever pass through here. ----
DAILY_SIGNED = 5
_codes = {}       # email -> (code, expires, tries)
_sends = {}       # key -> [timestamps]
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[a-z]{2,}$", re.I)


def _ser():
    import hashlib
    from itsdangerous import URLSafeTimedSerializer
    secret = hashlib.sha256(("tygar-agent:" + (H.get("token") or "x")).encode()).hexdigest()
    return URLSafeTimedSerializer(secret, salt="agent-login")


def _who_email():
    tok = request.headers.get("X-Agent-Auth", "")
    if not tok:
        return None
    try:
        return _ser().loads(tok, max_age=30 * 86400)
    except Exception:
        return None


def _limited(key, n, window):
    now = time.time()
    ts = [t for t in _sends.get(key, []) if now - t < window]
    _sends[key] = ts
    if len(ts) >= n:
        return True
    ts.append(now)
    return False


def _ip_hash():
    import hashlib
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


@bp.post("/agent/login/send")
def login_send():
    d = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip().lower()[:254]
    if not EMAIL_RE.match(email):
        return {"error": "Enter a valid email address."}, 400
    if _limited("e:" + email, 3, 3600) or _limited("i:" + _ip_hash(), 6, 3600):
        return {"error": "Too many codes asked for. Try again in an hour."}, 429
    code = f"{secrets.randbelow(1000000):06d}"
    _codes[email] = (code, time.time() + 600, 0)
    key = __import__("os").environ.get("BREVO_API_KEY", "")
    if not key:
        return {"error": "Sign-in isn't available right now."}, 503
    html_body = (f"<div style='font-family:sans-serif;font-size:16px'><p>Your Tygar Agent sign-in code is</p>"
                 f"<p style='font-size:32px;font-weight:bold;letter-spacing:6px'>{code}</p>"
                 f"<p>It expires in 10 minutes. If you didn't ask for it, ignore this email.</p>"
                 f"<p style='color:#888;font-size:12px'>Tygar Agent is free and fan-made, not affiliated with Hark.</p></div>")
    try:
        r = H["requests"].post("https://api.brevo.com/v3/smtp/email", timeout=15,
                               headers={"api-key": key, "Content-Type": "application/json"},
                               json={"sender": {"name": "Tygar Agent", "email": __import__("os").environ.get("MAIL_FROM", "harkbotofficial@gmail.com")},
                                     "to": [{"email": email}], "subject": f"{code} is your Tygar Agent code", "htmlContent": html_body})
        if r.status_code >= 300:
            print("agent brevo error:", r.status_code, r.text[:200])
            return {"error": "Couldn't send the code. Try again later."}, 502
    except Exception as e:
        print("agent brevo error:", str(e)[:150])
        return {"error": "Couldn't send the code. Try again later."}, 502
    return {"ok": True}


@bp.post("/agent/login/verify")
def login_verify():
    d = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip().lower()[:254]
    code = re.sub(r"\D", "", str(d.get("code") or ""))
    rec = _codes.get(email)
    if not rec or time.time() > rec[1]:
        return {"error": "That code expired. Ask for a new one."}, 400
    if rec[2] >= 5:
        _codes.pop(email, None)
        return {"error": "Too many wrong tries. Ask for a new code."}, 400
    if not secrets.compare_digest(code, rec[0]):
        _codes[email] = (rec[0], rec[1], rec[2] + 1)
        return {"error": "Wrong code. Check your email and try again."}, 400
    _codes.pop(email, None)
    return {"token": _ser().dumps(email), "email": email, "daily": DAILY_SIGNED}


LOGO_B64 = "iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAAAAADRE4smAAAAIGNIUk0AAHomAACAhAAA+gAAAIDoAAB1MAAA6mAAADqYAAAXcJy6UTwAAAACYktHRAD/h4/MvwAAAAlwSFlzAAALEgAACxIB0t1+/AAAAAd0SU1FB+oLBAAkIEk/9WwAAIAASURBVHja7J13nCRF2cd/z1NV3T1hdy8ABxw5g2QJokgyISaMyGsAFBUFBQFzFlRQMaGCCgYwggiCAhIlJ8lIPDIHXL4NM9PdVfU87x8ze4kDDnb3bg/n+4G7z93tzPR0P/XUU08EunTp0qVLly5dunTp0qVLly5dunTp0qVLly5dunTp0qVLly5dunTp0qVLly5dunTp0qVLly5dunTp0qVLly5dunTp0qVLl5UFouFfunTp8j9Jd/l3+V/kf17uqfMrAVAFAOiKvqYuyxECiE1nHRjzP7ck/se+7jMwkV2p4A03rYfH7uuHc03W/yUd8D8sAEwwnhyCbrj369dfvxpnPHrXH24oU80BE9n4FX2BXcYWwzBZjbDZt6blUWPM8yADf9nd2TqnlpP/4aXxPwLBVgymfPZRHcq19ei9D/ZrmeuckzZAzSXMbkVfX5cxhsig561XiQ7q3DMOWn/imu86/SltDer9+8PVyPCKvr4uY4rlzGD7P5Xa8uV5uzDABHrVWT42o561GazJuq7hlyqGAVNLsMaxs3UoyHVvr3PVGcM2Q/Xt16gf0Cc+3Ycsc6YrAi81CAbWsqOJXHnfLVIGvf+QPpiEASLiag2TP/+YNgu94vVA3TrHRIAz7dd2WdkhSzDGwTC2Or2UIZ3z06kOlg2DQDAV0+d4q9828/ky8wdTYTM4Y5jBjG6w8CUAGVgy5FLmTzyiLR//uRsjTQltXzCBk6ySkn3PLVrkeud+MBYAUgvqPv2XBAQD1Cw2/nssm/r4x6twhgFnQCACMVub9FYw6UuztJHnJ66FJHEgy90N4KWBSVCpoPqBaWFeHPrNhkCaZJkFQEQgsCEigmNg53MlDOktrwG7GsNQ91D4kiBLqIZNfqN5S297d8YmTaoAGMQgAhFMQrDMxmLyJ57U+fr01ydhcgIL7mqAlRpq63CuMh1wb5jjB3/1MiRsK2w+sT0cASlAYAIRwDYhV8PO52ne0jM2Rt0REbo6YKXGprAJOeo7fl4c0AcOcnaCqRms/btZu8GAqtZUkRgYhjEE55JaT5p9cZ72692vZ5MaIkKFQNQ1B1dODIiTKrb7p85qhrM3Q90lCdKP3a9fgM0AgIwjmDQhQ4YJsFmlilfdpANafpFtylnNgNE+MHRZ6WC2WVox/zddZumsQ62pUEa0xW+83rUeZ2BiZrzzgKkVgEyaOJAhdsZgyq9jXuhPV8EkMhnbjrHYZaWDM5qY9v58yA/IuS8n65xF7ehpOm/gJE5ABkxVvP3Wh8458o2rg0zmyIKYM6bkE3OKpr9kZ9R7kNqOu6DLSgcndWx2tswp9MeroF61Fi8/T0PDN/Z2E8AwxhmDtb4jOuean+xZAyeGkRmTJgleN03n6a1vRMU5Qttf0GUlg2BWwZ736KA+dTCyKiXIjnpKW42mXj8JDMMAyBlLe9ykQzpw/UemAKkznKKSJtjyKpknD78bfQ7U1QArEW2L3aTE1lXMh2fqfL12B1QricNmf/CxEUNDD0c6nBBKxiVY45fabKk+cMxmYHZMPSZLeeLvirlh6P1ImRhEZkV/sS7LRttiNw7GsDkmD4X+aSqqlBq8/1FtBvE+zng50gULmohTk35upuYt1Ye+vx4odbZWZ5NUv14OhPxIVGptp1GXlQJqh3CY+jDx962h5sBXU5PaKqZ8v5XPLn3hW3qmcYs9TltjvOlhLVq5xge+tgaqxsBxkuHTZVGU30VSgevuACsLHQFADza+Shph1v8Rp8Zhp2u1vywbUgYvH4Pr/Ggb60wfdrlX89L3F3rru1OkSWrSusOhjXJIv2+cQdLdAlYS2gJAVWzzHx2U23dHb8Jp+p7HNS+KoF5yfXANu7hNz2BTxaZXh/mlhKaUv9uOU1cHVyzeOUMH43dtxiZZ0V+sy7JBAEA1vOK+OFsv3ghJ5lD7esMPhBCLGIpcL0K2mAQQkzXIaK2ztSzFF14f2B/GkEOa4PWPassfn1jXPQeuJBCIrcEej+aDet4a1EM9WOuvsdUUCRJFYrM4CNniFh0xgzmj1f+iRQgSc2n8YDUkbAxV+DWzNC+/iyo540DdrPHxDYHgbJLR/z2pLT21lmZcwytvz2MzikgUFR8fWQ/J4gLARExECab+PgyJBG14vWALJNYmroI3Pxnz+AX0GbYJu+5hYFxDBLCp2YNm+oaemGQGCd42T/NcOs9fhvRXMEvV5hYOa52rg0FiDLk+uDsqaRXVCXjTzLwoD6MeTh11C0fGN0RAYnHAnFazPKaS1pI6PjVYSJQQJIqKlr7/tXBLPdQZhxo2ulznSlFqCPrI+4xlcFLHO+aXLX8AVkUl7T7/8Q2BydKBs0PR+pKhzCbJN/xQqxTvRUREpaXn1Q0v3a+bgB02vUNz76WMQzH/KOqJRcXiwGZRzn0TJriuP3CcQzAp3jcnDIQvYlKW2cpPJQyGGEWCF4nBl4PvhTVLFwByhCp2ezwWEn30eT70RaTWEKU42s+N03eArXePAuOZtsP2tXOLRn6kNUmdVvudNnMNsZRQiGoIpV7ZZ+3Sn6IxYEYP3tWfl7EU9U1pfRGcOtAE8w3f0NvW5gSm/UFdxiFsrJ2EvWc3G+VRZLMEq/xNWw0tREVV1MdS8uK9qDzrGxAATnCchigqKqX4j8OmxmRJcpIO6Lk9Ey3A3VzRcQoTVsGrH9KB4uvsbBWrn69lITEXFRHRGMtCz8CzdwBo+xBNMulizUVERcowtB+qyNLUrHlhyPVnLiOQ7aqAcYmBzbDtnX5m/JZLUoe1ztfBXGMZOxpACm3M3wbPnt/ZzhCnPmz9eLPlo4j6lva/ARXKTA82ndZsyaGombax0GXcYYjNOnfIoB6XpCa1a12kRZBQRilUVETjYNDPwLExzykABgmOjjGKqGhs6ANbo0oOVd79Ya+zdod1DLuiv2uXpZEZd5bO0z9S3aV2ynnazGPuNZSiIqqiLf33xIT42TVAO/PHmaq9XKOIiPfaH69YBdYal+HdfjDeti6M6SYHjEtsBT/XmXp+zboEE8/TIV9IjF5Cez/XspyxM5Ln8OR0kr8ZjJ0H8zyIxtiILf2LMYYSm+ArOkfPmGyc6doA4wzLlKU1PjjMjretgzSxlVN1yEt77Yv6UksfWoPvQ8+yrF0ig5O1P0YREQleD0ONktTRxAt0VjgCE03aFYBxBTNn6MM+80KYvjMqieETtNmQWEYVFRVfFlHzcAzssj05cnj5YxqKGFUklvrUK+HqBhVseK/qtJ25t6sBxhdkHNV582lla/57MMk6+2nfasSylKgiorGUKEP6ix5XWQZPLgHkUvxQCxFRUYktvXkNk5nU9OH/5pZ6SY/hrgCMKwiU9GZnab9+E72mB++Y32qqb4n37QNgiHm/njEFlSpVluHNQLZmtn2s7Q4SkZjrT5CklKLPfFtzPaqbITjOYMDgGJ2rf0h6jcV207QoxEvItW3/SZ7LX1dHzfEy1niSy3CaFm0NoKIN/24YcmSxxnXanLkjKtStFx1HODZ449xWfu26ZA1N+Le21Jfifd52AcrgUDhpAhKT0TL3AXTYY2YM7eNDKL2/fy2ylHCGvftL/atpBxS7AjA+YOOwxi06d94eqFJGJ+uASAziRTXGoHmzHPiMYwMQY1nDuZzgbA3ScSKWLf0JOYPEGP6ZFq23wibohoZXPG0lbNLM/DDO0S+gl6t4byvmwbdVd4y5zwud9nZybJdx7beVu7XYd/gUEYOWg4P7ImFkFttMy/XvE53LKOv6g8YHto53lg09o5dMwi97sPDio7YTQELDN4vrdkFPZZlPbu2sYjbW3KBlx4jMi0G9fhUwsXH4spbxfchMVwOME4gx9SZpTt8Kk6lW+Yv2hzJ4ba/dvJyvf1sPaRV2Wf337X4gcA7v1zKKimgRBuJ8ORaOmFw65f6gl0yooasAxgls8XMd0MPhkgo+2mxIKCWIqohGP6B/7gMD6TK/XVsDGDZY9VYt21uAtEKez9sZbByneI96PQB121UB4wOHNxez9bwktQnWf1RDXsRiOAWgof/sSRwnxMiW8XEREYEsDPPRWgZRFW2GGAv9E9dtL8j0XqJyWb2vGxIcHzD6LtHyya3gKMPPtFGGGNounCgtvX0jTAIAmBd2ZmOwxSZ3axmjRK9eYixa70IfO0rxLh1svhvVZTUru4whCRw+qjPjsciQ4PUDwbd9N1EkBO8H90bvsmv/xSCu8De08GWQdjR5QC/qZWfIYvLVXv8AZ7o6YMXDjPUeUH/nKgzHfRdrq2P/i/oopf6YJnL9xSxUMkBKm04LUSR09pOmfgRMsAmOimH+q1Fd0V++CxtU8U2Zq+8lNgkO17LVSf8KWsSifHBDZ15kXTeBq3Uco6Uf9gbJoF6zChnDdWx8T7/+GN164RUOuxSbPz4U/prVXOLWu0OLGNpPKwYfB/VryAxeVDEPgQnWbvxEzEVUVUVFWvpppEwVh+O0fHwzpN1wwAqnB1+L5ezd4LhG39bcFyraEQAfnn4l3Iss62Yww9TwXS1y0U5iSTPctQqzRYqd5zb1aDhwVwJWLAm2fKShv3VVSnnTp0IpRcd5JyJe/wDD7F5cEj+BHJPZcm4oOzIlXgr9IqwxMMk5Eq+sMXeny6xQiGr4mhazd04yruLUIniJnVNAKdLSQ5G86PZu7eaADt/TvC1P6sui0FvXhSNUsX851HwXKsQvcovpMgoQEqz1gNffIk3q2OmRGHy7BkBFtMj1vg1Gkr9NBJgEWz+sQVVEVEMRon4CaZIQVrs9lH+rJcYm3aDwioMIh2hjYCeQtThRSy8ynMMRfaGXYQTtHAhoewR/qo12WoB4CU29eW0kQB++oOX0zcHtZJQuKwaCvcPrzxIHyy+fKWUUldDJA9Vcfwz74ldn2yMMi1c9nvuOAKiUDf0wem2SYsvHQvFlVBN068RWHAZvKZvFa2Ec8DMd0li217+KSgz5m0fc4JdAFZyoRVRRlRijer16lbQKdvityq1rIqNugugKgwzOi/r31ck4bD4nttH2qV2K4F/Wts9elBR0cv4Ylnac5dsVAqWPMZb6PlSQ9OKtDa8fgl32JKMuowkBbLH7U7l+ChnAP9Y8+qDDfiAJPvRvNvJnY4C+5IdaaFCRKOJjqRdWEgNO3RWq/+wz3SqhFYMFTIrv6sDtG8JabPKAdjz2bURCfGCd0TDQE5tu92QIIcZORECG3oQKKMVR0tA9YbqngBUCkTXJmjepfh8ZMhyrMXZKANtOm1DKdX2j8WycAU7UVgydNDPN9bw0JWNp88dL/Q0l3WYRKwYyDu8v/fzXoZew1j2aex1e/qqiUuoltZF/CAA47DCzbOjCDNH+16JCXMOvtHhiqyWbTq4krJQXvSjESsmbnL3zYtes4B2b5UvkZxEgIw/XK5igPf/5o3OeWQFSZt/7AQqQiDNQrLEf/Ep/L1dKiBjbPurlG6iA+q7SopPDv8AGKPWCnlH4HGMYhtZ9RAfb24toWYbZe8ERoe8yldvWewHphuOIlV5qlS12XYfn/g2S6e67lKSRdME/ApEQZcRfkxAjG8ke/YMkQPsD2Ojkdzpla/v/imLTXSEr+l78j9GZCFJD/bKof+EEFT5Ri+BVFioAFS31X6NWxW2xyYMx13aOuAQfH98EbB1t/ojq+ZldGXMDV8JLXgiRknjssJvHheKS1tR9fWGMLrTGFRCCAXQEn7LI54XKA3/h9vonhbKu8UGuBHX3/AOtPXYLWfckuDxpN3NLqzhjKL9rLSQJ9tfBUstSF9UA0euVfaMm52m63n1atgNNEiTow+uQManZfnauf3TmBWYcjwdWag0AAFJusatLz3wiCXAfBLsl+zYxkNlRWplkC/vYqWFYnxAhrHeQVrXM7vy3HXjLttp1BSxPCO2JMMdq+eQuxibYcFbMC1EfF3MElP6/a46WnFOS2Ck3tcuERFQl13umuIwc9gkt/QncSqgCVl6IADDWvlH1jIzSDJ9XL2XbU7eIAHh9YpPR2pudTTJ8oNAF3mDv9ShUiannci2e2oS7ArBcIWsTHFYWQ+9HrWqrF2upEpfwA6hI42WjJQAMWEsXaIgiUSWKFHrLFJNQBftLQ7+LSrdOdDnCKVIz4XItblgdfSn2mrHYAXCBAIRi89E0dSx2GSqlLDoRh4YeCWsMT71ew61rp1m3f/Tyw1hMwL46L3wWFVfDN7WQTjHQIgIgEootRk0AiIktfqjNhRmi8d41kMHgUB3QT6F3Rd+U/yGIkFQrF+rQ/VOMMz2Tr9RCn6kBREI5egLAhkBu9bvjoHRaD+qAfhpVTrH2XaXcuDpc1xewvCA2FbyjHNRPgBKH1/cXYSkCEEdVAMgQ4Mx7w2A5XCiY671TbEoJvqIt+RjqK//JeqXBZCY7X/3dazhkxpykrbikAMiwAIxathYTiOp0sg4OnwS8lJ9FBRWsc6/Eq1frKoDliMM+/S39PJwl9/KnpXymAhhlASADGJOkdpO7g0g78ySUesc6qFAFx2tL3wnbPQouJ8iZvvO1uGsDZJzip5p7GestwDkYsjap4l2t0vsYo0Qf8/BlVExi13usGa9OyHSTQ5cLRCn2LQb0S6iRwTbzNKofawEgg2xqCmcmmB9rM0rQUKq09K7N0WssjtHB8I5ucvDygUDouTQvH9gQbGv4jQYdew3AFZrwtysOWxVIJ16sQ0UIKiFIrl9GSmzXnVb4q5apEXWXEUIgJHhLMZB/CnVUsOf8lvfFWAsAWUwwJ2pz1ql7roV1r4tlnquEoA15cks2LsXXdah473NMo+oyShCIHKZcGvWOKRY1l56tpfdxrI+BnKCO3efHUvTa4zf8uHoftNQQJdfvUS8yu/atqpfWaGVMDFmpIBCTof2Kph6JxKbYz+dl1LDkMVBklLeAhK2zZ2ojbwV9/D+hCCGEEEW8zNwZqXX4ig6U/7fkTPIuo4yBNUgx4Wqf3766oYTWuFqLECUuuf5FRIOPxZajdTi3hmvYfbb6mM/V9hxqiSohDOqvUaUMGz1c6uUTTYJuw4ixxCLjCj7SmK9HI0UPDpdGZ7qbPkMDiLSam4/WeDfLqDLO15YvxBcxdrICog/5zN2QUAWf1abuz8TdqOAYQhY25VX/o63rJoLZrXFfbEmUZ8YCRURjkHyLUftkQlrFG4uGRN8ZJSWiGoMM6OmocmqnPFnEmyvWdNvHjiEMZ+o4XOf7DyIxCX6gjfZEkGc8/ygaB/SyyaPnCTYwhPM19+0+9BJl2N/ceANSU8fBOksPRb1rB4wlDjVa//4iXDHB2AQ7zdKiMxGyrfM7/6vEGKM0/QHL3iH8ebEEwh4DxVBnBol0GtHIkP6j5lJDk2/Q1oNTORtBS4ouz4dD1R6r/WE/uIRqf9MiDD/8zn+dpxOjhEG9duroOecI1jLzidoankTX6UQTilbYDzXbg/2ac/RbmMTd9rFjBsHgZY809fyazSzekUsuGr2KaAwqXrStmKP3RStI+QFkS7TyfNEWevupmmTDe30u7WhQpxFJHht6TZ+lDD0XiH98G6RdARgzqEr4ZZyn+yK1pudfWkRf+hCDxDIEH6LE4ItWp1P8k4eh7mwCAO1BUWTaHT9G8vk4uBgU74ddD6Ihagx6MBKuYu+BQT0R3WrxsSNLsdfsGfrXSpo57KctH/IYJY9aivhmMy+iimjIp9986/mfXp/IuqTd6YsAwyBmwkjGPpNNcJY2Yyll6BgfQcR7/c/qYJvgdPX9r1lZYkIrnZgSQI5/cnDZ2udapzLhgp1Kw6AyZiLUtBVQOVTOnvbQzP65Tz4xp6CQ2habSMJMyKEgRiQOIyoWqzW2OmsDtVKkyqQEJVXhMvvCcbWG89tdHXovekfwgI5OSVqXRSDmCvbMm/oLGJPhKG15kehDKJotFX3s0rOO2HXrNZ91AyZrqOra0aQXfwk2w7u9tkSk6FiChYbg9b6NYI3Dt3Vu+AgMVoYW0uP/Cpe8YCJLf9p3MH/d7VluV7voZVo6RFYw0WPn3HrjAx4AUF9zjamr91WtuCBJCKQcBub2z5j1WD8AGFcov+hqblKbNPnYL7QMwypBQQIu1RWVY75qI8maV65lbn/TDFLQKJWldlkAmwRv0kE9FpYm4BjNvbT3/Bm/fcNkYLVNX/7aDx534W33PjEnX9w1JCrl/NmPT7v7v/8+uI4RBewMmxQ9p2ke24PpRUNsSixL//gmibEOB2rDfw3Zir5XL0UsI6teo8Vj68NZs/lD2lQJEvr//bG10i3f8+3zny7LYcs8el8WRVHmjaHBoVJENLYaT1z311+fsJelkflpUgNnV/2HDoioBGmfQUW0qT+Ec8b0XKYDM3YeSX/SLkvFGHCGj0qpX4RJE3xXc5Egfu4vdnvrLy95REXUlzHGUMbgYwjel+32rjL7zhsvPuGgt7x6/RQAjBvJ5kcGmaMsWf9yHSyjSpTOdHHRsv/lcMjw2rktPd1ay90O4qMKM5xd5RYf71/HpgZbTm/lUUKUxrTHVVVaZQhRYik+D3kzdpZl49bfH3/4XutOrgAAW5dkjmBHYp9lPYBDDza4WZsxitfQcUV6r6ehZqnHHqd52NcydQVgVCFQisPjgH4aFXLuGM19VPVBNR9oDJVRYpRQNkMMoqK+Mfum335k3+1WTwBQzVGaJM4QeKTzvvng245ZuwbD65yv4r2E2PELx1AMvgkVpGbjx0Rv6q2ulH2jxi/ExDz1+qA3rw1OsPH9WqhEUQ2t3IcgRdlsFO1T2ZO3XXjKZ1+5atb23bKz7AyzJRCzc0Q0AkddVlvl67P0vi/sMQn4xlwJUizoTV8Wcn4lSVHDEToon0XatQJGFWLGITKkh6FOFXxXSx+jjxKjlGUsylJFm0/dd8WJRxz46skAwC5xzGwMEbO1htq1XSA3kqYhxIztzm2oXPqDd085RX2nIl1Uypi3ht6PqknRd7k2p2/TnSIymhCSFKtfJ/rfyUgMNpimhZQSg0Rph+fDfef8/NCdNlkVAGAMcZoMv7Lt+aXF3u1FY2yK9AM3B5F41+0+D7EzUVKi5r559dpIbIb9mk09mSmB5e5YudGBTMXiQB3Sw2G4iuN1tsRYaGwORdUw/Z+ff/OGGQCCTRM3xkcwa7D2sTP8QNAYJBYdGyCoaKGfROo4xR90cGh3qqQVcHeUwKhAhk39OpG7JzpOsO5/tfDBh9ZglIH//HzfKQyAbFZbHvsuEacGe1wiraGy6X1rOCtAo496+xowmcWWT6jetAoqznSLxUYHZof9dFA/A1s3+KYOlj4UA0EfOuk1VYAqFcMJYTkl4xGSXvR+7jFpxtK3/AIN4H1Lv4GehKo4Sgf1CKRZ0hWA0YFcDVdp/O/aRBnWmSa+5fMY7/zaFkCSGWuNBZhoOWViOIvMYItTfav05XCCWAyiUZ96GRKDZNINEh/dBBm6KmB0oAxva+R6LKVk8DltNUPURz6/KaheYbAzxCM63b3Qq0mSNHVp8ub7dCjETlZqDCFKqX9CxaKGN4Vm/FmFXTcoMCqQMe48bTyxHTJrJj8Qi6YUp2wIJJVOgk/b0l+u5fnsGGuc3IpFWwQkBo1S6NBrUaMEyWmSz9/L9XDXHzRSCCBrseMTEn9JNZvhaBnw5fVvJ4y1uf8810WuSnjP7SrNloQQfVFICEHP67GWKrTj47me11cz3UPAyGEYwvfUD7wWtRqvcmMeGydPQcWu2KnNxIyMaYNjHtWiJTG0YlHkkjf0/agj68GxoYwfGsnYyi7DGKRY56YglyZJUsNBrfjUwY6rWXvPX2EyQMQEzgzt8PPZmufNWGjptfDl3eukCVy2yp1R7lnTdj1BI4fJ4W3i9WOwFTvp7/rfVyXWGdt2ta5IAYCxXKtbs+MpM7QcikUMXn2px8NR5vA+P6Tf6zqERw6BkJ2qQ0/Xra3jDXrLpjBJaiorOPueyBjihCuZI+xwypNa5hpiLFphzm6wVU7TSzWfvS1sN0l8hFgibDa7qcdbcqhdcMNUJARyy9fmfybEBgBbQzAZaPufz9JYFCGGpp7nUpM67DY0oOeQM6YrAiOCE4NDwlCxC2Cw5RkbI7M00rD+6F1cO9zkHOyWJ8zScijXstAPoJIgw8+1f+gdqHbzw0aItfin6iV1VGEqE1FJRpLYPRYQsU2Y8LLvPKTSLL3eOzVBkmDLh0JxwUSky81F+dLEMNZ+JNcvIHWgFFXD464pLxEnSDPGJp+/T6Vs6veASpbhaxrCh1Glrik4ElKHw8PQ069ACmMMkmQEJZ5jBic2TVILrHXEbSp+3k6mytasfofqDavAjb/rXYlgquNk1at72YHBVfCIcnrGCGJYBllnMPmom3I9qy+tkMFB2tIPwHC3aciLhy2mXqP6IxhrYAyRG1Fx15hdpgMcAYkF6h8444lDkBhgwuWq16/G3b5RI6GK1xXF4LthiI1NwTyeu7IzwRoAL9s9IZMYHOh93B+2mxf0YiAiIrDFflo+vi0crxSj7gjgpF2DQhaTrpBwfcWNywsfj9e0GKqqSkL8Kth777YKro57lwoRCIjeWkJUU5n7Gw1b7e3deBwuPO4FoF1jTZi4I+KdpRGZ9IqKjnMJUFWFqsYIcFLA/OtWyt5hxmXPiPEvAABI2aereX4SYEzeceU4T7dLk6EBpX3q7xjc5+XedDa08cRKIQAAYUrdlPciMNZa26/oq3lBVy4GgssHeMLLQYCOt6Yh418AVBVQbNbHs26GCtadElb0Jb0QBBLVXnt9HW+eIONr8QNYGQQAgIKweSrz5zix2LTWsso8Dr0Az3r1AXKXxHV7Pbf/PJ5YKQQAYKRAQQqgV2CNjEeD+tlRnDfAm+64oi9jaawkAgCkoPmqUNQJyiAaXwvpuRG6uaF21HpWjyYriQAoWWB2IFaYvqTQ8ZIJsKzY8BDKKeNM+wNYaQRAlEHzhSAo110dslKtfwAaL7fJBtVxeNUriQAoADRBUC7WWmdFX80Lx+Iu6Aarj0MP1koiAAYKMCIIgrXAK5cJAFXMgF+zb0Vfx1JYKQSA2v/3gYjFYzJkPDpVn+XaiYgQ0ISkhPGnAlYKAQAARVybQgTmYFsnPO4DQguvW1Wp/Tth/CmulUQAGAVkFWsMcI/u3YMyrjyOACLSzqi7FX0pS2ElEYCIObCASRQPPL3WlJE0e1/utL3/4zWDZXxe1TMQPFBQbXLplebMxpvBI2r0uDwh0LDm1/GotVYSATCYNg+rbgujbtY0vAnsx1tU7dlod6omMODLcegJWikEQAmYMeh7toFT07oe620aKmxWJmegwTrgh58ehw7MlUIAAOH8cWdWQxCDe+av+y5JVpYrBwCQxy6w/5nD408FrCy30c65Fth+UqCI6+7Gqyb0sx93i+k5ILNzxMM6Du/2OLykpaGEO9Hccotg82T2zdhjJ2Uz7hbT0iEAJBPWF8wdfxvAyiMAEfc8kUzcGVDGec3KW+HiOLybS4UAxs6T3dw7xt8GsLJAzDX8VfUsTozlyvU6fRuMu+LQZ7t2AhxOEH/rlPFYzbRyaAAVibg66o47l0q2dRLWfBeUYZGN/6AAAynVXwF755xEuzrgxWJhNnywqccjtSmtcbnevGHmAOZxLwAMSxb7DMbynd368BHAyHCKlvethzRx+HCQn6BqwCMa/rWcLtxwgm9rfv+G3X6RLx6CIeyWD+phqLmUei7TJ7aDXc5tQV8sTOveofojdDuEjAACjDtX9eoeZq7i3Q09zYCZxv+iosTgk3Fo/muQjPv9ahxDIIP/i039JGqwKb6h/fvBGTvuBYBthtVukvDPCd3+ECOCyGHVyzTcuUqSVEG9/9RLp3CajfstwGQOB8r8uC9c1wYcIQYfKlrxWFQdJdjxDv0yUjfe226QYUy9XvSiquu2jB4pbLNzYpj+chiiBLs80v9quBXbK3pZLjrDVzXX12MSxv12Nd6hFK/pb+rZPRmcqeENs6/trWbjz7e2OAl2nDmkv6/ZetcEGCnk6vixNuUIcGpshj3nnoRknOtVtj3/1NaT2yNzyXiX1XEPscMqt/rw+K4GxiLFbk9/eHx3YCVj8S2ZJ0cjMabbI2qkMNk63pwP6k29xiZZUsEu17xhPC8r4hRvHOyXM2zKBmZlymQdZ3SmQlBiDb6j8/S3Fa7CUkabfnLKOEy3bV8uwVaw5j3RP7wxpd3hkS8aIlpwR4kNTfyHztOvwRlUEuZaL4HHW9fY9iUnnJg1L/EDug8qoJWnlGW8MSwABNgMVLHr3aZF/4HITJI5Zy2IxpcAtK+HQBl6/6xD+g3O7MhH1v/vskAAiNgY4yZjj9tL3/927iEYQxhvAkDDvS2pak/URvGrVali0/Ez3GClo72aiJiYkmpWRdUepvPinL1gGZbH2X0drgAjUIIjB0udvhcbSlx3B3ixEBY8f+NSYNJOXz7n7iEdzGfuihQ8vjyBRCCmYR3wQZ/Py+fffeZBa3WmynR54TAMgQHLjgDe+fMXz44+etXHznoNagbGEMN0evGvwFs8HOpjLGgKbV7/o4c1SisUN313LwKYLcxK1N1sHGBce3u3aUZAtvmXb5ylPheJjQsP3DgBEZGx1qZgi7pdkQkXTCBmg4zJAdZQlsIST/3AxbM1n5Pr/H9/YiqDrXNgEGB4vAy8GBcXsVQYQgBBnSkE2TabHbwz2YZL9Mn/XnjOo0JGVTJukvWTd7xlvm2i5ssVeb1WBD1+tY/Rw7felQOcSOxtFnb9j7xh/Z5cUjPrTxddOchWBNYrKdtyXDQLGL8CADIiZDhGZJu+YYdd1wiiaTH3xgv/e72HscEQYkB99W332njPv3w8ck4xrsCLZVGTSv3PrxGefuMd1z0wa5CtMIeA3j3euMcGiScb/3XJuQ9y6olUKaqOCwEYxxDZLDXgzQ87+zGvrbLQJ845YusqkCTkwClQ3+Pzf//vfI25/jarup4VGW4zgKuZ5HSdW7Zy0bl3/fqA7QiAS2oOvNZ7fv+4+qaW95y4axWunsCOO+/VuIMSB1DPx057TCTPxYdLD98+A2yapVkCcH3vE6+cEyTE+Xk5pKfZNK2uwIsFktQkx4ciL3zRbImIPvCnY16ZMZD1ZhbJFp+6Ltey1DnnvncSYJlWqurm5QoBTOwImLj3r2YUUjajzrn6K1tnIOsSZxwjWf9df3q4VNFW4RshiA7oeZW0YmhFld4YIofKEVrmQbSQGEJZRpWB23625zoMqmR9GbLd/j4jhjzGWz+9NpBUCF3n0GJQ26tPhhLHQLLLl2/QwgeN/Zd94ZUEsHNJ5oB06yNOnaFRJcYgIioiKq147hqUmBQJL98DN3V+NQYf6280yiilqEiMMRYtLyL3nfKJbRLA2YSw3TF3aZGr3v3NzRnWsDPtk0vXSwS0BcAAhmwG3ugTFz2lvllo/+Xf2W0y4DJjnQPMdode8JBoCN5LjDFGERHRWA7qRWtzYhKkyzf3sv3oTJbhNTO15UXFx7ZYqohILKPqgxceucMEUKXPYL2P3BhCw+sDP9ozgcksYBJLXScRADCTSVyWEbI3/frB9vO97/tv6GNUkrSaEWA2/OQZTwcNc4akDCptVFU0xtjQy9e3mclWRHSYKMOW96oXEYkyLJaiKiJFEUT9Uxd+6WWEdDJj8rv/VehQs5zx590A6nVEy1lnjVeIDZmKhdvg6JsGxA9o+fjpb1qTQRVjKxUDM+Vdf3tKxYdWjL6IEtozWVRVVTTEUIQrN0eVVkCOELHBGufrwEAriMTQfvAxRJEYRGIoQx7Vz/rb/pMYfYb63vDX/lg247w/vMKg2glodiFjkwT11500S8ui0P6LD18bMEliObWM3tf+4GENOlhqWcTQakjwOqwAVDQ2Qyhb4aatUF8BdhVRFcdpUBEtGy0vIto2TVQkikqUovA+itzxte1hehNjXnHaDJVSB3/7GsCN/+KG5QJbwmofuLChrWZs3X3cHr0gZxNjnEOy1fev7NeyNRRCHqP30ce4pALwPrbm6G2vJLv8PcLEprLDPu/40RUPzhMVLfKy9FFEorStQRUJQSREr0+etU8VacbY4xczY5Hr/D+/lpAl/8MiQAZgOAfCFl+6rQwtL/POP2B1B1MhkxKIp37gjDlRJW+JRAkiQSS07axFJEAkapCm3rkjXAKzvDtJkgVgJ671iiN+cuZ9rSii0ftWGaKP0t4JokTxLe917hUfraFWhdnhx3N0sCnzT3sVYA3c/2bKIDFMymkKs+NPp7cKr+X9P3h1Bamt1FzGwIQ3nHhPDL7lg2iM2lGuujjSMQjVz5bH90TFJrScU/CIbOLaTgi30Zv3++olD84qVONAw+e+iD7EUGoZVaQstWhef+RkqlfAO5/whORz4+APt3em5lAdNxGi5XnnkgRphsorfzMYG0M6dNGn1gLVao7T1MBs9slLctViSJbCEjIgIhq9Noun34rMGV4BxfhExGlKAEw6YcdDf3HObU0V9QNF0fK+iGVUCRJbQfJw+yFTkFqLjX84QwcG48wvro5ayoSVqgfuqNwzNkkG+5ozh2Ij14E/viUFVzJramQw8V2nP66Sl14kRl1y1S8F0VCGwTjjnUhWUGzYGcAlWWJdAgBr7vXRr137hFfR3JdFEYOIipdQlqXedGAvsmoVL//FkLSi3v5/dfT+j1WPEgAyicEr/zQ3NJs69xfbpOCkbhML4OVfur0l6ueHtjGlS1n1iykAVVEJEqQZZu+PNDHJCvhKbA0R2GVcSdMKMYA11tnl6D/fMjuqxtj0GkVjDEWzIf66t8Jkq1ja9aymn1uGszbD+G94M6oQAZaw0Tef1lYuD5+wJSOtcMK9xqy6z59mq5atEGMMQSQPy6gBvBSSx7kHorYiVCk5CwBsCNYQw6VJBQBx364f+t4tM7yqbxVRYgxa5EPqT38ZuGaBd14Vyrn66Bd68D9zHCAADJdg0kH3arOhjx+/MZBaosQAG3/02qaK9yLi2161533y0va+BomlhFKah6LOMHaFxFo6n0ltk84RpwyA1tz1i+c+oiJF7n2hPkrp9akvrwmqVdB3xIPSX+Z/2xyJbTuyXtrGIBPBVF1SwQ5/12JQmyduTpQ4uMwCmxw7TWSwETuuFB0O9yyLAMQ8hOCDl6FD0ZtaCzccq1nR3zhJANitP/zDe6KItJrRS+mLpt7wNuYky7DZKY04JzxwUGL7kDhL7fgQvTRtQiKADfWk2O8JHSiaf9uOrLMJ2yqw6UnTo5RF0JDrsxj8zyEHZT7Yylu+FYZaR6JqYDoSsMIFAAvygtfd9dPnz1SRMjS8+EZs/Wwq0lqG5I13aP+QP2YSEsNkCQyMx8aSo4GBSYBa6o7Vodl694dTmMQakxra+JiZGnwhIkXUZX7wC41AH0R9EX0rDhVHcCWFGSeBVsMAp1m1YgBUdjrqwrkafWu+90Uht78O9Wo1wRonSqNfz5lqsgT80u4oZizg7IRf6ryi8dP10FdhR1kFG35umhaFL4rStx2oz6XzteMUWrAFqGgs9ZYZGmJT8/6G/zwSQ+NmUA8BAFcS45KU0LfXCTc3NTZbwc+XeV+bwNZUEhzwqM7WW7dA1eElu/oBgCpwGVb7Y5wXHnk3cZIyMovaobeLb/hYBF+KhPjs274ssAs6SkI0BhUNhZ657UOah2ZsxaJfvo0kG0+dBDm1MASTptbBTHnvuQ0NIZfcx39ujBSVCra6QoZ02p5wKcZXxcvoYlP0mOznOis+tD0qCZOtGrzh8qC+UC8xRglR4rOe/BYmW0jHLSzqvUjw8ofqRzUv8xi0lBD0+KqtjRcNACJikG1bdja1jOrOf/Eay1CWet/enHFW4Um/16H4+OtQIQt6aYoAETjhDEfrUJi2C6oETjKs86MhycsyxFLb2RT6rIa/aJQixEKazSjq2/uAj6Vvxh9lldu0NawfWq1w/ITR9a2M5hNxDsDu5wxqEfIyNj+FemId1Y/RRpi1O1WNtTRsw77UMPUq3poX/XNfj8xYrhvzzns0eJFW3hS/MJj+bArAayMv+/OoRauzU4gWRUN/STiwv6k+alsq+pv6q1XH8TLi3hR42999bMam+G8kiU0S0JGtweb0V6DuqoyXqgA4vOqJYr7/CKrMLkXt2/2hWcYoUkTJtbP7P4cF0Cy11dRLfxRK39EAIZT6+0qP/Zs2YuevYpBBf90G4/ckzY6oNhGTD71PfSiH9NQeVCnN8OGhIkxbl8iQdeOv+8kIIQLIYINbm7k/1qQ1MNFaf9ah/igiUkre0hie+wAoKg1tlfr39KQi5u3UQC0LvXy1CrZ5SEL7+CCireDlW+O5AsNkCScTq9jhOl9qbOnfJqPKpoqDw9x4/RpcA43rDlgvCjaAc6uer009GZUqGccb36WDsZBYRM1bWkZ9dvu/Y/GJyqD+q7pPU/LgY4wSxes9G6MXn9YixtDWACKFPrSZSwmjmnQ5as45ZhgDdq4X61yuAzGUetk6SMlZHKb98YxV0WPGdw+0F4O1bNLs1zpfz02ThJC51S/TwjcLCSJBZ9zUjD7E5/H7ljKkl69au1rzhvedPNx5r0GamJO1EdrHB9EYm9ruI0ijuZLYjJpZ2W4j4mgCNpumZVmW+u81UWNXMSfooJ6ArPaS2wFAmbH0jbKll21gM6ohc39sDRWx1BhEvf5pi+na1GfRADKcD+TL+OiO+Ig2RHwM3msZ9FCkFawzzUeNZSdrKJfGPsiIya3SyREbsRgQOUduFJ8KA8Yhw+GaF8G39JJ12VFSr/2xzPOPwbzkgoOGnTOH5i29YzvmamorONS3JIpEFdFCz8DfNS/jcwtADM3iQ1jtBvUSRaQMvqlnV2yS4XVaSugcIEWDXpGlGdhucPJUpGRG4VxtjOF204JRsyuImRlb3K8+lrHQv0/lzFV5yhXNxvRXJHbcODJHB7ZJin3mtco5b0EPUpdgq8d9kNAJ+ZR6Ad4Si/LZDwDtkF+pf67gfdqIKiJexIf7NsUEm9ivaxlk+PlL0OPQa7mCD8unkI6OZ9UQ9vr65FEt6iMmsjhZ8xiCb+rvEmuSFDvMKuJVNYP2fvMSkQICVbDD494PvQcTODOo4Ffa9CF2HPqlXlKpXqGFPpcTQKIPT21WnXCZFlFFpZDQKj+E1BhnrtTmcOqoivrmrkjYOJyu/90MFjTiHZUcJn3N/8FgNItQiQkWb+tvlUF92dIfUWaSFP/n54fjOBsn4ezR+abEdV7/Bm3qUUgSTpBiy0c1LFDtWuoNq+FgbcalZf4Oq4AYSv0O6FUN79sv9Ln+2STEoPqjGlTKTu5w0PsSwDBWvUb1mHbZ4YguH3DY7KKgR8OMalkvAUR9t0up0beKQr7AWUJV8zMt83ej76WzBxCzsRP+KXP0B0hsYgz34JNaRD+c7K1eb56KNW9R/6wZIG0F8MRazN/2Q+39oCjLgS1hTcLYcZ62FYCKihb6BxgDg5dNi3rb5jAj8wcQiB1+qo/rx2BH0QZoi1LKf9FCg4Tgy/KdqHOK9W4K5d3r1NOXjAQYWGt/o7P17F52zGSZ+/6lhUZZKAD3bG7wBS0kqizFEhQVkZDr96xd5XKNoioxyqB+qZrAIcVHNIiIhtgpFDkIhjnBK2c0mvIBpCPcAYgN2YvFP73byDTJUknsD3QoRq+FNopHt08mUIq3DJX6a2fH/2S8ZYHgKHH0TRnQK9ZAwkTGOFrvIc0X+HxFgz6yNXjzh6UsvJRLE4DoY5639iLsPrfV8feUcdqazoFg8a22+SASRVVKfRXIWoMtniyH9PfVbKRxIXLY+H71t64+BgJg8Gkt29WOGvT8Vakns/il5sW+cGzH0p29nMRLQeT8AV8o7f0fnlUJAJRUt1o1OiigOvxDBuaef1BuFTYu2UNJScl6TS+8SbF9jxIAJVE+YYZEVjASLDKZVY3kgEZC/1yb6utflo/YH0hYd2pun5o9JvdsWotJQSBF+cbPqorScdOs/eqaHmM6Jns5CQBBV2285QSS/IPTkgAQqSheVo+sWNDql2TSBDj8dmYKF+LSrixKYuSqwYrZvb0qSKO96i8OpASF6zx/IgDKD80ERGHmPkGmWHUXcBjxl+it5pgeWEb9/igebhFACoC4PHifISV+6Efc3O5TqDLr2InA8hAAIhjO5ux44qR+96kbKrmHqkIIfYBg4UKnWK1D0pvPTwolfkYTNVIiFfv0lZDe1UGAkoqRX8012lYhw20ClaAQPDYAAiK1ZkEM9q770Si8MfLIWAyAVgzmGB4tbcqen2wI4ux3/6o3PvbqIStm7BrKLQcBIAUHN7jJyWvNnfS13zswKxEUwJLFO8wJyohzvCntMxWfkpJp0cMPZ8X6a3mjBIXy7RckOQQKxlMw1LmLBMGTBQAoox9C2HYdGWmtkKIHtnEfxkAClEL/gi6yoV5u+FUxQkPfny8TPpMmY5nWthwEQIkkK3q/u93AxB9/38VWe/8HCxyUsYh6C0iRCV95m1PRpVyZSrS4aE6K7aaCBIACf5pNEQ5EiofmGwDaVtCKGTkUDCCHUpywE4oRf9kMGma2E7ZH95mw+nnQ4VuhXOz/gdwGc+kfe+a/5d2DMoYdMMdcAJgc1BT48puG+s78FidsRF3sSxMxYNCCx6+kIEQAdt5ZRBB65sZHJiT+ASimwDMpSbT3/8mJQYQawZUtktCWLkDRhAGUgKfBFCu7jNR3Q4IUJrSgWGC3jhZifXNYs5ATF+xntygM4fjHenDkanEMp86OuQCo8Y5YD//MQM9tnxlyDWXq87t+tx7wTFVKSCCiuGymEZYljQACiHn6vYgwADgUJDjzSQlKaigE7r8L3oRFvLQEUhU8khsQ1nEywoemMGCJY2EDgBpPQTsORkXImpsfCSO9j51g8u0+Bh67g8DYCwDZ0vkDvtXf++gnH+PcGE/zNznpLRPECjpafBEmQBHNnVfbYJ7hbldAGfffamN7BqdqxNw/Q1QEAhDJGYhwcdEevAoo7ptLADbbJI74y45dZ0dTPoCOPqQcbFjes3/DFOb3/8784Tv5dMyswLHfAkKsF687Djzw6WvrpRpJdO3fbzrhlVAgAEvc0QkA1OYXIIZnKgglFcxEEpCBAJOG9Lx7DAGqChBw4bRqsWQAlZSeHIKQX2etkT88xlhYgABg8CSocwxwTD4r6keuZovK3O81islHjOFzGnsNoMnQFj9YzWdHnz2hKShN6Pn+jkPZKwHAQ3XxOzoBIA507WPVZ+rrdjrhNEBRAUE5Ks6NVgACFBKrT5wCF1iEFnuVaTQgEO4d+Zcdu9NYwDwMH3xMMCyZ3+GQVtp0F/+t3tx332LMSsXGXgCycrXvbdmccPwp6SClwUV8/l1z+sIGmRKeuZ3WDEglveca4mcYgUqqXN6GktiBoBSqN1yDiPbmTBZqf3+VVTGEYSNNFQoNQ2AhTMKIHTgKHWXrrwMJyoVFTKSsnnHIZh6J/8nTWeXw3nKsHtQYCwBTVrrj3zivfs4JENVgTfjAp4sJkdZZQwyGFt9VCei1AWJV/+1paZa2oLgTomRBBLE4Z0bWPiEpALSS6T9qUBQopP3FCABUngKRoj7C76IAgcmNkRkQFpwCQVEsmXKNz0ullf3n99zc7Z1jVio2hgJgiKyz4G8fOL9+wyfn28CESr7vj8nC0dRVwJgLXmCxtee/TXQRCAFXTjd+KTYAIT6BzilAlWf+a1hIBBqAou/c42w0IRCC7ziEVEnuRbQj/7IKEIytjUWauSpCGD7tK6AlRxPetlvDRvzkLqeHrE40NuPnx1QDGInV/COHzak8dPATqatIpMFtf9SnjiNXaxC0llhMipShUGPvvXsp+poADAz7egTKN9/qFvXvUxzCd07PCmPLtp+o/aaCJ0AAZIRbOAEFynTCmNwqRQgLbkYA2RhIJhyZISaPn2gGdjrAj1FL8bETAAMKXOl/y+eQ5IfeVQlli1O/9i/WLdmreGRQ9D9jZ6slUFBpcfGzXNnsAJCWAEhx4SLPGWg3bT/8gqyppCZb8LcWc8GqCCMTAAIjR8jWHgtTkAghDHt7iKnpNRMje7+pTCT9zb8n+o9tqLVi9D93LAUgRkbW2PKEdXNz1KW1nBJNpHr8jg3DTA6agjBQLO7sIaQGBCXP/24+I3RDCmB2hBptQlnd01c8Q02ozPvotXXP7GVBJ3+gHRwqR7h5EyOHmo3H6CwQw4LdUKUHNhIPJZ/o9RL8D4vm+kchjklawNgJAFFIG2ueuvHsvpN+nbRIm7Wg39wvr5SRVBANCM2SFo0GAmh320yjeeRekkXswM7vhHnCajAIheK6+5aMk0bWypOfui/LF5bUKASraETEyBYQAYQGCOuRjIkNoGFYnYXkhs/1c9Og2nz122Mlppec0ef336PhRj8MPZYCoC4te3+606xVzvx6KiSxEsujPh2cJswajbEg+Gdsy+1DQWSaf8WCv2j/pm2XQRMAYQiqqo8US1T8EOfik5s/9GRWYoFnnSytTaQKPzINQNQOOFfd2GwBZvjLasR13z3sqZ68tGw+OiHC5j+fGSccUhmTc8BYCAAZODIMoq+9bd6q//54EYOCOd/nC7CGmZVNsH2IKD1ZXeSxELxCAJGIGxFpoZdIAYJoiUjKEf+Z44RbF+niW4CKQEORXvsV0QJIQFAQKe0A4myEXgAVCegfcJi82hj4gxWoTQwMKCCMOTjngNmZV6s7vjlPivT6M7M573h7bhlIRtkYHAsBUENenZX8kMMalfuPmKPEYrLG1j/tE8bwLPhq+3svtpoU7RbPQoSHZ5sIidJ257dFQRO0NCWDex+3wTx9J7AUbUxS+d0PsqSFVS0BEA59q2kcDS++4qnHknLjrcbGEcA07PyU2f9Gcsl7Z9uCS3dIrWSLnzw82X64l2BNaeyoaqCxEABbguE17Psdoeaht7sAJWqu/eP1c+48cQImLfn0O3cBSkRGzAP3mIg4rPXauVLqcS/KWPLM6Qh4sMlhKctaJGRfPsMQVquUAMHJKzdppTIKAiA0eyby2iZjIQAEMzxgXNMnb0jK2qUHelFtbPc6pkb9/pMxtNfbSwRjdaSZbUvc8dH/LlCQoBK3/2HaModd0qsgmFj5zh6NBO3GNwrCqu2ffMZLQUpgMQOPQXVhlzcCgcUMXQViqYSro+DKQaWlleqqj/FLD/b5qRUQEUq8s5KTAjxi893MnwNgyshdys+AuJ3U0taOl0K4MeHC95iimlQ/kgTbMD+9pSafWV1NOdofPhYCICzIhqZ8d91ywnF/6h201rugX3rf4HA4lqDA5Pbj0GGvffuPPgIQjaBwP6yySjv7F4AiqjvtbsuMgBtnW9wTXPIMbzEBYjxP+0pu1p4IgjjZbu9mVTrJQSN8TGjBYuJYJIUKUlIlhYL9KQBV++nCI7NGGfbYI6Zqmt+nxssOU4M4yv1vx2QLMMblvd9+TaPnd9/o8apkCQccGZP6Qr89oaezr7cN/E4mnyjAAAsDtwyRRmGotu1EjaV78KTIXuHd7fel+TzQ0teDpDH906ncWwGD1By0Bo1aq50h6Nj0oSb0QCMAUrrlQUZeUOj7+VdrSV79SNrMIs75W0/5wS3LKpuR5rUszhh8GfJRI751YKt+2Wd0qAUJSfGG4yolcXutkwIBNWZEE9v7ARFIgwZ+usUCiBLh8X7fjvMSqRCCRNc67q4kEkHtnFv4yrvh4zPSxhQANSniJ7emCaIzYdKr0O67mI1wC1AFpiOiJx0TK3AimFURFN8rmCgKBtITznRJeNuOyupaP5nbXPtIGONHORtxVN+t3eA4QZ/83ycG3bRPzLImobBqY8sfrhbTRXWXIrOELFVAIe0yb4OIawYsExQiaLUssQFp7onKYITtd05xWWRlKnAWnnw6kaUnzAsz1R74KQISL3jFRkXbsBj5AAHFjGgxaSwmEURMABgUo7n+WsSoYKgtv/F4Es0nsuCiueqPE4p9XznISMb7VAFyKXad4/2sXcCpSYjdKpdrq1RZ0P5LpNA76havGQqd4uAgsdQyzn0FkoQYIIvVrlQfVQoV0VAWAy09AiY1cIbgEnznECRm6VXaRIweSq8+FOQYP9KG9xpLPWrEImDw2vmF3rraGOhNtr/QQqK0vB4EBgMwjB58VBpx8JVwWS9v8+R8Pa/auyIGYbxAUrPBPY1mvj+yzFhOq3yaDhVRg+/UAYpoqQ9MsnhfDO0uH6INiS2vx6JGTEQAM/9CfRSJPmgoijk6+2Nppc7kiADL1VoPu2fJzrawhqo48KMwFtV/akuD11K/POIxMoyNnihl2lpjIQD1v2lLQ+n1XxPYsiXAskuq9ixt6smOuFbBcdqI+6BnvM+cJoNV/1PO12O4bo2zSYJjtGx3/+0U/kuMpT60SorPaRhuCN+SwUE9pw8M256rnuAL7X/1IealyM+3Rw/DMoMpBZJ2wsezPInUAnZir60CGz0kwUvUXL874qJOxsQHxfdvNPoCQKbvQm1FaWn/6+Co7Q8zGTt6xUDpZ+yISmJ5s8ejXjulPVpqnEoAEbEx1VN1np7akyRkEqrjg80YJBRFe5CiiEgMhU6bmPFx2lSJMXrfKoL4k2ppBdzxiWf4RGzGsgzBRwlXvN6ZrP3YLcHCknGEZxkNxwZgawwRp9hZylgEiQP6ixEPEiPU79WWbjH65cFke/6peV7k+qOFrQcMTJLQCTpff4bMwOHj6sNRqADLeSjesmMNI8XhOqQ3ZyBjDXqw65OaS4whyvCwdwm+WTw4xdKPdTB4nzebRZD8lv2oWmEabvCe4RBp+DJGjbP+/uYMttaZMwMY4gXZMUvV6tQuHOCMM7xa81g2W635etqIK+0J9Qe00G1GXwOw67tIvW/pPRuhUyJGoCrSBLvMkOZTO8CwxcQrVR5YCzx+p0tZshXsM5APPbyDNdYYTnnT28OghCBFjEHK2IwhqB/K9fG1kf5aW1JGlcase//6/klI04WSTQ77PdXwQ7Meu+TYncBp9qKas5HBrq3ZeVQt5875UXWk4TRCdsXA02H70e/lz6bv3P6n5syacxDSToNQIiLDVMGfda7+DJaswzvKpn4fVZMgGa1NYHS/SCaiL7tg1Wb2/r/Wc0RKwoTTXt/oiUQ+ocACLjIUhk3jon/+Lrg3bjQzU8nSOQ8+/WTOmZcFjkECx1W37FtlcPbMh4fAFqL6wv1vBNLerbPaOogDT/TPnO7KF/wWiz8mijvV8+zmfhCNbnYwWdm2Z8hS/uCA86xKJASYqBXNP3Qyof9NN5jI1pz5pnL2Prc7Liv5WCQHjBi2mHKlzpQjTR9S43odTtY5RfRe8+BFYggaBkvt/9b2fUicXZj0Y2ytx1JmrTVsmA0bM3zcTSqJMy5NnHOWDS8jho21LnFp7wJ1yZlZ9tcv/S3bnRutJdO50NHBsKnU20sxq2XWGmPYULuvlTMT7taW/hEpWYe3zin1l6bGGcawXPDFQjBJtXaa9utPkJIlaywOlwHpL2MuEgofJITQr7N/uDoorSzTO45DlpP5ZVzKMAkow6+1iMXrUXGU4nca8j2RgEZryuhoNrsiNfj2Z5rVy9/aAJEYLvc5g2FZfaW00SgXMGKmH3GO1ThxygY9jokLJzAIkiE3NgwXRwJQhmpwpEaDWiiB8IIiYZ3YKrnBesngwFFHar0rKRN1WhPQaO4BVFoLUavB6vwi1xnzhhpIJRgIhw/9nCU9+51JaYzf+axV3EVvZYmjGxQele/A7HBQa1Dv2Qw1Tiwy7PRwaEmzKPMgQe/6daMsc7ltF6pj3c9fdu9Ao4hlqSEEUdFQDk+HHR4FHkMQDSJBVMplmhy61LZyPkZRieJfwPy5Z3+7KDGEYXfGCN9t8beOGmOMwYdQtObMvPvGsz40lciA2GLjpvii+Xb0ENfph9rwH0Rl3PUOYwLXsMuMQT9jd2TGsKu5dW+MQ7E92scXcvUqf9N+vXcr24uP3iexkHJuU0N71lsUEQnDXqFh55BIDFHao6NfNMMvju0ZpCNjeJrNqLPg4lQ7F6ytu4/qNRbMWHO6hlL/NdFSVsHW/1W9caqtjJYJMGpFh2xjY6NfrFbod65wEdTb8Pr9bYuaAqxQE8nmx672qjvef1el8dOPhbk1Kd3EeQ8+cv9jzZbwKNvUKzsEFaUpM9MBkCoP3rMmtLnbW3+XRe694w/fKrbf//ujmxc2Cjib2gkX6jz9LaUGhkwfvhUb7fmPIqKF3r4h3vafN6NGJ+hQU0Khj35vjzEpdnoJwcyAxQ+1DE29bl0YZ2nKzRoe2WI8jcMDAEIyAT/U+eG8ScyoIOnBEb5VtIZnOgUv07ZFtk6a4rCibPn50Z+8CcFUHLFLE2u6LA6TzYx17ajIZzVXaehXQbA9OLAc0pMt2XE1CcdwhgNaTblzfdQzOOPw1qfyYth2E425PrYjUhhsOj0Mhdn5Pe+wqFbIdLL+iKnLojAzEwFMSHCUtiT38sCmsA5J9g8tWq9HwuMpIkSWXjO94Wfsil6wMTVs/kjZkDx6GW4F71vvQFqp4Cgd8IN603ZIsmRBMG88fZXxgrVozxA3+LzmkktLT0QKm2DPOQN62QQeL3ORYZgs0Sb/1UZxAOq2Ypgx6XL16mPUKJ3j06B+Fs6hdlcMMd6/M62SMlFCNOod1146EHeK5j+vhcYy6sDrUDcJ4Wc6Xw5GapjHwVAhShJjqhWcrQ35FifGWEuc/TrmndNMpw+0tPTrsMZs1pJQtt6LSRkRM43XsOb4gQzoW5qrqHi9oFJ1xmKTR4ryvnWQwIyDqYiWgNSlX9e54XQyGTG5FF/RVux0/e9sAb6pPwA7vD/4Uv+CioUBEXVX//NCqPy1PUJBgn4EvUCKT2pDT0TFIF3xAgCYag3va/UX124EQ8ymin2bjdZiDf9FfalXTUKGT2re6t+BUjtavuz/AVa5sz1XRYPesRESJFy/XMp5uyI1ZhwIgDUVvPqpIX34FagwOUqww4O+VRaLPX8RkbAdHD6ug/F0tokZh9Gs8Qlhvdllx/s8qCfDJKjiLQ2v51aQjcHoghcKo4L1b9TmwLvQA2SWea0bNM99sYjrXVS8NPRzIBwizfheZA5dDbCMMLZqlZ05WK3B+a+DSbMK/0QG9FAk48AGMMw9v9XZ8nW4lEFUoT/qUOnjYtES0VIKvabq8OGiuH99ONRXvOiuHJDBTu1JiqISG3r+anBUpXXulfjEBitUATATMxmX4Is6X8+qpwnYoI6vaxmjyKITgNtxHZ23C+O9g/GCGo98gtv/Doz3dPKpNZRlv34WNesc3hfmh1OS1BnYUU9QW0YsoWqR4F2tvLhlDfSQAyX0Pu9DO/S2qABEiaL6KeBt/XpWalbUJa+EEOMYLToVNd5H//jLUYelntNjq7EvarD1FXUvMyaHHuzwWLP19G6UgAgJdno8H466Lq4BghR6IrDXgP7OrDCZXQlhwvna7GwBPsqgnlWhHpNgx+nNcM1a7MAYvXnWL+jKGImrYPKVOjs/CBPIGZO4tW7UoWKpA9+85HoG+BUD+mvjugKwzDDjP5p3BEC0LJv6cfSRSfEFP1+/i8wyVtRh0MAl+JkO6LdthYwjTmp/0GY+nNOjqgsyOzTEGPRM2G369Y+uqwGWHYOeezR0TgFlGSTGuzdFwtYmV+jQ03uAUlpRsZSMEnzGt/TCrKduE7DFF7XVbCw59lVERQsJpf4BvMUcPbvStQGWHYuN2vN1VUR9oUH75RSuMKrYbVYu101iGFoRyQEMZx1e0x+Khze3qXPgKt7U0HKRzb896TNG9aox+qB/hlljhp5f564ALDMJ3jCvk4bYqauMOvAe9IAsvi5D+lWkiV0BuQHMqa1jg7vygZlvRWIsVQ22vU9LWdz/E6P4UmLZChKDngUzcaae39MVgGUnwYdDK8YFRoCI5nrXhpkldr0X563+18GyTZbv/WzXp9pq7z90dv5JVMlYrprVLtUiLjr3uz3cWWJnnG+pV2WY8LRe0BWAF0CCL2reWVUdk6r0+uusCmTYbf5AuGpt04MVEVW1vfiRztLfciVzQG9CP9WhGEUXqgBRiRLD4FDwPpYa9O71UHtcL+ntCsCy4/BDzcNiXtWY57I/MnAd39KGfoecXRECkOLAOFcvmgKDCpkUBzdaS/h/RSXq7CdUYu5DU6JO3x59T+jFXQF4AVicrmVoh9Y7p6sYSv/ABsaxdRMu1lbxfiTLWQAIxClePbMRH94KNmOkGXadF/NyOAVAFmgA/eWrj/rLE2UULRth/l7ofVgvn9AVgGWGTd+l6jvJNZ2aBI2NXE9PrKk7s8uTordutLwTa4gppam3xiF/EDKm1Dmaeq2XfKGtOmyxRP0QjNlgn+/eOF1Eht6N6t164yoYD6lMKwcGG96lPkYR0Sjig4hIEQtfHIAJcBk+Hob0d5XlpwIIgCE2SXqqDur3UHVsjK2kf9TW4ud/URGNPrwetQQAb/3pX95WvBvVu/S2qbCwXRWwTBjsODP30ZcxxlKKoHmIUVS93r8pV2zqamdIKxzNlcQuH3cQAUiJKzhK5+klPYYpganjS60YY1iw9y8iAHsidbaSEIC137IxV67X/64LOw4SGVYOLHZveK+ai48yILf9K8QgUUUaeqqrszPY6P5yaPae6DHLzRJkmAre0D9QPrk5ssRam+G1T2sj+LioDhgWgD2QMBkim1UZSN3l+uCmMF0BWEYc3qi+VB+jhCF9YL2f60DbHPAt/SQsmR7sN7+pV0+1VFleu4AxFhver81yH6o6ay3TundqWWqIpT5DAPye7fJDmzI7Q6m7UB98GUzXBlg2KMF71Af1IddBfXT7qQ9p6dvWVSM8spWtmDTFCTpHf8kZLadVRbZaM+fqLP0SbJIyUO05U/OoGpfIA20LwO6wIAYRrAUDf9RHXt4VgGWFHT6vXlREGuHRXbDH43GonW4dY1MvqtuUq+g9V4aan0a6nOwqSmrJcToof64nCVm2GT6vPpdSFjSCXIoAtM8pBIOf64ydhxthdXkuCICx5ufqg6jP/cw3AHsPFK0QNYqKhJZ+E3WHFJv8N/qndxnxZMxlwpCt4IBGI960gcsSRpZh73llKbK0LDARH/ZCwguLfxjH68A+sOOlqmlcQwRG5RwtYiyKcuD9qOJ4LTpeAQ0hyJy9UUkoxZvnhnDT2rAWCdzYpNsSEcEkFUsJdnmsOdTYCy6DyTK78d3aiHHJJJAFAvAamEWWO+PrGt8z3mqbxy8Gk//j89DUweIIZJm7UAd0OEW0bHq9aT3m1PXh62VDf1UHuTHzsLRHWRgDxur/DoUezQnXXAqX/V5bGoIuGgZcVABeh2ThNZHF0aoHdM8Ay4rl1W7QQqPXb9Q5wXp3aVM78ZbgRVr6a9jUuSz7qw7p0UhBGKNMcQIRbOpsip/pTD3Nppklm/bgc1KE/Dk0wN6oLqIBLD6iekjXBFxWLK1xlwQ/pD8xqa1h/7JU7zstF4KEVjN+CCm5FFvd6efNeS1qSO3YrK52HV+9bvDx2NAbVzcMAvrwpn5fdE4mSxOAMr4TySKt1RjvVf1ktyB4WbHYfLq0WnpunRNO8C3NY2exieZBxOvMVyGxqGCXOT4+uBVSWx07A5tge7DX7NI//Br0GJdQVlnjVg1FDM8qAIUegUVsQCK8TfXT6BoBywIRDPaarXP0yrVtX6UXU27VohyOC2rhYyktvWKycSn34BP6tF4ywSVu1No+LQoTE8NUsPY12mp+DBmDjXX2t1oElVyWugWIiNeTsUgJMBP2Vj0K46q5xbiFiBj762C4fydYmyR4nRYSgwzP3ShFffT6Pe51SJ39qfbr90yPM2ORHsLETAkhO13n689sxSTkKMPhvtPeb+lN80SiXrpoGwM22Ev1s0i6foBlgZjxxTB/xu5I4FJHv9W4aLaNqKj4lj8ACVWsnXiBDhUfQw1gHv0uqwxjqI4jQ67/WQMTjTFI8PKnNB+OUS/l6Yto0DszwwsqgclgN9UvYpRHn71UISZ8Q59+GxwbyrDunEUXW0cCYlnO2Bxk04rd4pFW8+lXokrA6JcKODBq2G1mPvj0jqgYA+eS3n9ra6nPfuE+EPShVUALBcDiFarf7JoAywYx45v6DiQGLklxmD5T0YrogF6xpklT9OKDvl+vnwTHZEbdEDDWob72jT4vD0BmYdkm+KYWGp6rZapo1Ke3XTT9w2H7XL+PcdHcZvxDTDjpS6ixYcpq9SvaHZeWMLWKmOuvODPMCT6js+MvKiAzaoMSh7Fs0l6cHufrLywba2Bq2Hd+M+ZRn1MDxOg/vGg3UoMtZupPwd148LJATLz1eta6BLUevLtVyDMFIBaSF3IYMrKm0ntWmOMPY0tm1I2Aqu3BUTogN0yASSg1DlNv0CKU/rk1gBT6o0WntTE2ekh/AdMVgGWBmNg4Q844h/o/ZWlVlxJjGWTe3qjbLMXG/w0DQ68mO8omAIEqDrvOkmLGq5DAkjGpPUUHgz6nCaCqUurvYe3wzHgYXv1W/QPRGJipL0GI2STEALPF+4eKpfVMF4mhzOX+jSmx5PC62YU+vD4qPKohIYJN3ZSrdV78FBICElTxnqKRR32+569eb1wNDOq4EgmTr9N/9WCFVLOtdBAxM9iADepnacsvte46Bh8G9Mwa9xJbHBoH9cKaG113EBFWtyfqLP0Z+owxJmXa4lHxUfR5JEA0+taesGxARCAC+i7Sm1YHjYm76iUHERE4MXB4V7vsaikC0PG4/cJQSkjcKdrQH6A6qn24CTX+WFH4W6ciSZ2D6cGfdaidmfQ8AiC5vh9JWwMARKj+RW9bvRsMWFYI7SFiU25X35SlHgNFVTQ29NOos3Vu9f9ImPf+UXYEMF42VBSDO9saMlhn8Ck/oAsaACycoLAUCSj1J5QMh5NBbE/Wu9YAL7fZOys/xEjxY821aDeLW3C/2zXjbfJWPu9tqCeo4hVPNcrHdhrVNmyE7GId0C/CcIVMUsU2T+Rent0FvKgEeL1jdcNtGwBkDY7TR9aFGauw9UsQRoJ3NlpShuHm69JZ+u0hJlEkhuib/sGNYRJr8NFY6FW10fp4YiaLb+qgnlMFrLFsuO9f2grt8SnaLv59RlugBQIQ4pyNF8aDyOFYnfWyxbKEujwXZFHDureoj8EvGKsUY4ztwetFKGNo+Zg3hxp60QSY1CT8M23or2DIjEr9TVaxeP3gYOu/G5LjzDBX8YXgJe+EpKKPGodn3ixFAGLU/ch0BIAoxbd0/nbdtOBlxlFSq52nrba9JSIiQSTE6GPMG6VojBJi9EFjqd9HStZi4r9jszgQvS59/nX2vNY4sce6X6/3x28+WGkZr5I2d/2kibDCChDIqnKEWeoMGyUI4TVn6vCMNVVEGIDH5dzL8Yhnap345qEMALXdKWpElElbLiH/1MM33TUQVJH0rrvjJh9/6kcmcjrvq6f31o69+V7XopEPt6PUmRN1pv4YdSbnkHLfFZLnPpbtStCo0UuM7XYgS7NSC7m+slDODD6rze27W8AyYwyOlFYeFxp/WhSl+jyKPvnXj27Ws/BHaxP/78i0YsB1HNgY0vNrrjIaeRcGBzQH9Ko1GWQocYxjdCiPsezEo6PGsozBhwUdwYbLmDsNTbzM3n5YAIgdDtVi++4GsKwQej83v8hD58gtKiK55P0i/rpDt3YA2yRLrHNp5ozjhGGYalX8Upv6rUWd8C+aBJs9GvM5r0aSMIypYLc5DR9je9xjOyitEkVCUXYS1nXBs1cRDYWGjwwXrpJN8CEtX94VgGWFa7VLdagY9MPnveCLMng/65e79wLGZGwNAQRYQyZlUzUJ4Gjy5TrUejeyEd5oAnH1gjCgnwSbChly6L1Ic/WFD+1+gNq65dpp02fN7/cqUuRFiCF0Zh9qFBUNRZQT4doD0WEc3t3yu3QzQpYZ4r6vzYpaNktflK1my6vEmf86dCrgqsZYMLFltLsvO1giIkdkse2cEO/cBI5hXmxnZiKXmgSf0fl6Vg8Y7dlgX1OvC13Aov7v+++wzpZ7vOEzf75yelARzZu+8FKIqhfvQwilPrC6c2xhDCHBHnP17Ui78wKWFQJ2+vnt5fD8NXnypm/sUYVxNiUwmYzSiknYMZkFvh+HxNCnY7+egiSlqkncc779s/2LWsCWO53bmzyx9902KkjrxcsvSYnbpiUpKalP5j9w8Rl3CkzfulusM2nqK9ZXUvFs8grDw0DVDux3YRLao5+d3+XsKQefStav6Bu7smASH7DaG7euTZ1Cs5+cPeOW2+cBifFKIZUFd9GQApHag7dZLPkJv3hPyR8/xRgJldaL+WAm5hrVztf5jYMTRwzApatf025cvEgsKp8XdPYf3+5sFQCyjV518DnTChUpfBlFfRRtyAntzciAHLZ5RI9Ed/0vK8zOJSkDtnfSeqtbANalKQPkbALa4F0nnH/+T9+9DihlQ9z2uBPB9mCrhwcbj2/EVHX0ooxBgrF19wXt11+jU22S4jhtLNEG3IdiaE7U/K9vqaParlCmvr2Ov3R2qRJaPoqPDb1ucpYRwcAm2Oh+/SKSrggsIwQiZltvR/dsPePUEhFgK+Re/ZN72v7g6d/fHJXeBercMYzFe3y/nmKrcC8y9OaogjfO9XHahiYDCHB43WDw5WIhYJG8qVGKoK2/7mGRWM6YiJDt+fl/PKoihY861GjtDWMYBOvs2nfq95B1g0HLDBvDbMglxqYwlm1iQGSt2ea0fpVYlCGI6JOfq6Ov/QILEMik5jRttd6L9MU6A4w1ky7VRv4e2JSI2dLEazUPfhF/j6gEESm1LIpCZ5+8KSoJDJlKYg1qW3/y/AGNRREG9cudLDVj0zVu0dO6o0KXGRr+NTUwgHUwRMYwJnzlydjKOx6CMo967nZgQyAiawCyBhvep3rvRpzwi0oOcLaCb2q/norEEKzhhH6oeYi6mMtfoopE1RCljPrw5ycjczDgNE0yoLLnNx8Qnd/w96+N1BEAh1Wu1wvA3R3gxcAEA+uyxGKvG0XyQmOIsZ0V1NQH9geSBATnACKX4b3lXD0FKb+Y9BtCgh3LIb17HdQTYpga3tiK4ZkRn+EWljGG0uu/dnHJBNgEGdjVDLDKof+er03ZC649YxITr9J/d4NBLw6CYU57Eqz6o5aWeVgQHdSm5vNj45Q14LLUAoaJYPpwhg4M7YXkha82IkJl4r91qPEupMRMlrDWTepjXCILTBZLUQgNaXy5joojhmOwswnDvfGXj+rZmYNhwKL3Mr32Rdsl/+M4iwSJ4XfcrK3BvPBRpB2JV++lLAq94TWmkhDIghxXHa3/cKHn119EmQgxpficDuhP4ZARIenDj7W19CKwRc6EUobyot1cloEYxgJkrYXb5H3fqToHAgj1y/Qm2K4CeBGwQwbC1B8MqffNGNoxeIkxePEtCUXLz/nMJGQOIGa2aQ2f1gE54oVXYhFxih2fbumtazpjjAUs3tfwfqlpyQsTwmIZm6EVZ3zIIUtAIE4SZpdYkK04hoExpnaJ3grXDQe+MNpJ1ZU0qWC367QxpE0VkRi89977svS+9KXEwutFW4Gz9vyADGnf5WU+bcMke4F3m9gY9wcpBvejukvYMadr3atN0eeuBBQfYjMW3v9mLaozGdvxQjtOODMwBolN0n/onUhcVwBeKAROerjni/M19yIx+LIo/MLCfNFWo4ytQZ1zUB8ySykyygze1e/DMUhf2JRpMlTHwdqvv7YmMZQyO/qNNsNSs1LbiWnarlSVqFFiS/+zC4xJQOS4nf/pACQEaxI6Xe9OrevuAS8IJga4FxterEXwGspQiIpoY/b9113811N/9eeLrr0/lyLE2Aj65y2RVduJt4n5lS8e3QEZL6vVRURwxpnN75byno2oZqzlahXvbkgZn1sAOlpApBVyfWJ/JEmVl3zORA4/0AcmwYxGqPp/B8vgxNbwhru10SrL3KvowB3/+up+b95t09V6DQDbs+62H5gTyhjCoDx2ZIqqg7FIsM0TTf0lKsyG2xvJ8wkCwRAyOkWH4gHI0gobVDD1bo3F4hk/i2WCLzADRGIoB3TAt76eIjNLNq0jcvi6PrxmtzTohUGJMcbWvjIvzm9J1DK/86dH777mKpX2v7qerd9y4GFHf+77M0VikLJV+CveimoCspTh69p8eiekGPYGPL8AMKd4x2BTT6vXEnbGsOXjVJstfVYNMNy1pJOnHKXIY3FSH2dLZqUyZ/icPrJht1foC4AAWJNizTO11RAtn7zssN3WBjkCaPWN3/C10y+8ZtqQdCb0iIZW9FFn/2xVpDaxWXXt+5v6W5cBplOf9fwfV6eea1Qe3RYGDOdq2HfQh9IvFgNY+OgX+UN7YnAu3oc8139O5QoWUzlElOFwnb5Nd3r8skNMgMmw87+1iDr9b5/arALAEbDxe4+99ummimhZhLzwIXhfRlEJPo965UauTtY6fFzKYncYS7yMApCk+KI29YsgWGZMNGtcH8tYxiV6QcfYTgaPwXsfQgihfSgJUor4kOd6/aboXbKLfYYD4/w9uwKwzBAzwWT8joc0zPvnwVsTLFtjeL2v/POJziTBoO1JQkWr0NJHjSEUoaV3bokqmwTr3F3oqY4y06nOWeztl+Yj0nKbjxeVq3+O2pBwtK148M5lsAwh0kVfTSqqRExEpJ33VVJV60mCtUM7//2g67Igi6QmszIaZdYHIepmhi8bSoQYjzi2cssFZz44ZFLyacuKrrv6Y9d5H3NhoqRv0mprrT8lyUtFqgKCoWz+Fme++y4XkDz2qxP69/vdlVGB50sSJwKsRc+p2mq+HY4AQhW7zC4LlUW9gKIiIYagIurnz376kYduu+ySyy659Lo7Hnz8yafmNkREY6sxGO5/PWpmEakjVPDmhj/4+esRunTigExJisk/fuqPB65KsD1VsAMZprYONQxYZ5xL+nb4xM9uKTRIaHvnfTmgN01NMptirf829XQk6dKU/+IPQmFitNj9nc3qqWfbqICyVj8zOc904SV1fpSVMf3+1uP33T+vNc/HXEU5YZtUK9UpW67Xt9qq61RVNz75ExfWWqS64AMs8jC6tcsvXQhKkET9Bh+d+pY7iwmVUhoKAXG0wo6MFhFAACJQ/uc/4E3f/551YBSkMKE2sMN3PhTIp0+cdpzuvtONzpXP/IzFBYAFZCj5Rm947JskSkqoNA5+SyOLRmlx/UHePn382bObS73yv4L7Jm+09pTtXrH+WYf+gdSWBLQVkKCMGMN2pi8tSNnFCHvqAzbUhzSCjYo4E0UKAHbjjdeevCpHNVA1TkLZmrlhFFYFStJK8Z7rTspyQ3//2Nprf/DGpVZvLaEBODLKo7ZvZSc/lniAyLY2+IwatQo8w3qoHfjemurgoG8OzJ/f3xwcyhteZGheKwq0f/40oLKae/VuVz2qfsFRQAtAUOsKwDIThfl+YmRDqTdRwJZLD159rS1227peq01Y3C5TUiKAFImQ8/ZLN9xaaSYPnnW0vPmXdzh6phGwpAAwh7UPlcoVv7SBoEQRh2wylBnhJV+phNo2wx+upN6QAILiyemPzHv87vvn5MGpPEbTTqNglQIpAKOkKHNUgZEXrb3kISWl1CuCM0VUCmISiyJw7yY7bfnqjRIApD4IWVI1BBWKjvOUlAAlAczQ1C+/1wP+7APr6+53B7E+vxmY4QSNA/shMcTMFrv2DxR5uUR7IlGRELQsy7IZQtksgvehLKOETg+DgTv+ccwemzjApj0pVTNLxMyUuNRi82n6QyzncdcrI9QOn4Nc2xRMkiQj0Hqv/vrFM1XE+7yZlz7GGKMPIjGIRPUioR2YkTIGLcvyg8iMtb9TmbYR3POn4jns+JjoGYnJYInYJH/T0uvSBEBEyqhBJIpqDG0noEgZ8kZzqBVVBh88/QtvXwNoxzITAGSBjDe4W3+C6oq+veMeIgIbMobYmHYiMGjrT/7mjpaIFM12u4C2N6b9OKKISNQ4XLTnY5DYCDevDlOj1/fn+mkQYcnOHAv8wx0zX6sHr90ofl6mpVOWtPXO1wfrdcnQXedc51QZYFUQQZVJwKawxkTxYrIN1qf84Tkz7/3v7U+RoJIob/HOy/7uQgGG0vMqo/9pCIpE2vZ9+9FQzyvfuOuaq7HGEsY6EKlCQQSoor31A+0APEFJDViRtrb/4HeTwl5xzevLj5ykGuwSJ4GOAHQehuViz/cN1f/0b1MqizFhwlE1iWYpjScVACmo41ygEI2VSMYbiUbZQCgGkmRzJW0Ql7X+WDXMbtNLB0IBB08cVvRNHtcou9Kklaya1evVxLr193p5jUk9DCx1zlSkaJ+wSRd0YCBt7xtKCgWookefd5/a4tTXyuZv/1Mmz2V9EXPF/EGHntoAFhacVPGpGIuosrRA8GLbQYgi3vvWgFeNTzVDIRKil+i9n1eo+tnT52sZvI/3TKbsfD0N3B0e/JwQrO372t+uvPXeR+d2hkQFH32IEsuoz/E4lngyUXP9GVXh6udreRVV7XM1aGN2eGPe0G/AOTjYFBveo2UU8Yt3flhQ+90ZXhGj+FgWZfCqxb9+8ppP3a9lITGKL6OIDl12ymG7bfYVzX0o89lTgD/pvyYiW9G3eHxDsNhjXqcC3+dFngeNMUaJ8Vna8y9lWapKDBKf2I5thgO0MbAPkiXNwMWOgar8qTTe9yuKYj1CxPs2a2aswqSLnjZJlVTbG42SqqoyE0nzyTPvfeSmtT/0znUKY6Kn1CIMnn3JzIcfVtBEWIK4KrG0sPl687oWwPOg6E1a5BREzIQIJRVDbb3fUfTP/QYEBWnppr7rVst86S3bmHedrxxZl3rvCeAM+zYG9AtIEjhixqaPi29HmuKi9YBROqPLoojG0otonHXrz9+6Qa/BZicPqGgoml514J4/vWe1CoBqD97fDEWIMbY2MDhJ87ei6w1+TggpDoiFlgss/NjOtum0XXi+Fn3DPyESizhtXXBGX9Xy4a2tpaVGAwmuJCP8gergQ2eDS3holn9wrUZmlKCLBxEVkZRIISZGZ6k5+5L/Xn0jQLrRoQf1aYvYJDrnhuvvvGwQSFItdXC9gyuRCIhm4sNowG177oq+w+McBVDVCNtWvdTWuyCo8vAp7LlUwIKfULJhrb1+k4pe8JHJ673+Dms0LuUEZgiEDK+bnev3kTKBKKGtnvTRL2j3slC2yhhiLAuvZaE6cOVP3/CyGlzVYd1vPqBlc6gVdeCSI3aYCNjEMRFxYn6ueVQpNfhXM44X/bXppoQ9DwmO1mJ4ncvwiCBZ0CfyeVt0inRqNYKelWSugp+pXj0FziyWjdXRABFgUewzed7gmcwCEEE/sEaeMCktKWus5BkAmO4+68arhwIn1aI5+UMfXz8MOqr52//xt2lDSFKNXq2IrQ7sd5AIe+MAnihw0Xpl0z0GPhckmAKWjgem4xNcuPCff/ksiL5QwNab3slEZx2gO+10HsxSe3NYwGDDaaKnIzOGAIttH8+9f2bnN9FY+LLRVP/Y7960GuCyao3Qc8h/Y5g70NLGte+dbGCssYYMAWTA6z8QGypFjFrKB4HjVH85+kNtXmIQ2TM0yLId+J7DEoiiRVM/hj525oK8/C2SZKknQUMAPqZDjX2QsCWQxbe0uTDhe5Gu0OqlmevT5x6xGgjOWFvj9M3/1pAPRp3/p7dl1C4JQbvfPWDdmdooYhDxWujhwHdUf2q6OWHPDWPCjeqfeybT0tT+EnuDiGpo6B8ck8OHtHh6IzzLsA4iuLNVbqjBGgIMNrhVQ14uegEynHmq+X8/t30VLq06conFK3/fr615pc76xc4JbGoctTNQAWbK8FFpxRi8BtFCPw8cF/QXtqsBnhvGmg9pocULEYBOpe6ShwEp88c2RprR2ndoeRDc0jKyiVyClw809aMwxGSMxVFaSozDnal9jD5q9GWhOuuP71qLkVYyA0oIax83S4uGyPQTX57CuuE3hCVYgsMWj7QL2VREvP4QODbor6irAZ4TstjwCY0vSAOIxiBFO0V0MQGQoG9CZmG/r3otksUiO52FyIDHdgnPvAEKISXpeQMWHP5IoQLWMia2uOnPlzzaj1SDGq3EcvJ7Dt+0NXtiMuPMU+6K7GIn1VMBAYlWhL+ybpkp2rkKQAVQwij0sH2JQ6gYITW6zLkzSgpSg4VxgY7ZqCC8+Z+izl90SHWTXa+2S0vHdcR0juo5FTYMwGLbIiyS+18ELcuQ65wL3pmByDhLJkVawV6XtLS/0Me+txWoki5agNYuQspwsA8hxuG+sXoKcEzU07u1gc8NJdh9XowvaAPQGEofltAZnZ6992XglFe/TsI3F/f+Dv9BgNrm0fyn5YKykAl7Jn6BtaCABGN15un/vDGYShD1YCl7Bicd/4G+RqN31mmn3uMyI2HRhBMFkCJ/5TEQaocoAQgqAAFZt0/g87JmBc+fx70YJGTaUcJF1mH7lzVffk3aqjx9yU5m94nzTFz4mmEBiDasV5NwK8BRoGT3Bi/4fA1sMHDTn/71lKRpM4oKkTgZ3P9LLxtoVZon//S/1lluWpcvfj2KcvI3pzTqQdu5SKSKCsBAYrs5Yc8NYQ0XHDCcOUFA+xYu+L/924Ljfrs5f7uOCEumcPr6PtcQe9w61LvlZtexLLz3C1zBNmw30d1+B6ICzpfZ+mjv2goRduHRi8+9rGnSQhosEaSO83U/f0jZ7G3+9VdXlZVYBrV2sefPShTtUa/JM1GfdsLQjGpnfiA4osuzQoKNOCbtgP/wwyfo8LNXUm3/cfgFUArTJ9dbSTvzcnHLgbClbSaa3HDfjhN3v0EXLdVZ+EPrVzBjRtvjbMTWAEJUKSK7ePuX3/qxfxRGcgQIGadZLPf9+yFNm9z24YMvVc5LgEK+2IeKURvecFiwqjASIBAFIQNAWDyVtcszUawCVlAEQUWVQKoCgrYXuihpWJBXpVBV6EnHFZUgQgrIYrsHYasdIB7T70bcZ0LQhfV6Fhi28lchSDAalT1TlSCBmJDwvMv+euksWIoCG9IgJmqt1Xfsh81gz7wTf/GkbW/nCiweZjSU5usfV/EmOts06dwqsRHAAY1oLUxXATwXGk0PCjYCjSAGRWGjUCiodARPFAwgC1ewapHu/9qHvrNuQaDAFM3wIlMA+dT1rk8D4ar9ZPMN5i65+giEBCep/hWmHWui6vnqg4g0bvnithUkVcPMBEPMhioGr7pN5xf6r53gMvvMTNN2DSL32d/ofO+9hoaeflgIMZegVwMfz/XC3q4j6DlhTLhAg6iUeRFj2WyVRVm2ijL6ZqNVDpadXNBy2FGrIhJb+gfe4AIthjT4xTr6irT0+8hgMPXxQo9+hjOQQHD4terp7TFThgh7/mNW/6xrvv3WOsNOqA4PfuUEpp6aI+aUuc74XIbUmGfNNO7BIVpGkVbZ1HPrX9CijBL1WuC1s/WCevcc+Nwwdj3u1LNveqJ/oOkXNONb6N7JB8rQKDuDO4enicRG3BcTTtHYDIu08xQR0aZeO9EaInNN0/8JvLBPY3shkrZdBsN/qwldfscGq869awhwxg9pJ2fYIbjK4Oon7D+f3bnH/sfUG6wdu2QJCMngrt8Ag0qLysXv3+5L0o43EJBHJEmjewh4TsReezXshFXXtJVVN9p0sstsmc+7q5Enq6w+pdqzXiX1IYvBDlf8EgBl7754RX7UzMMTz6Edb1fSTmXmKqvOczHLL3hlvuN6jzIWcwYRASl+pfr39sRRBySJA2BTZ8l0hn+DkbhkFbz61jDXP3RwL1UMOoPpFlcCholNyqvdrENl9NLUezasXaOFH4ri9XqDQ4JetGq3OOy5IWZXGfaX257JU9edOrldTOF6pmy27zdOu0ujSNlaME1KRWNsxi/CJV+eq81y2PuuEkOIMcx7G6pIsb34cm9Ys3jbBgJlOEn1Qmu4owisMalrp4MPu4TJJUlKH51RtMLZ2yGrgIezOpYQACIyaXKqFkUcKH3hP4yvadkUX0qpNyQ4yOsVXQF4HojIgiouyVLbCa6C2aaODBsAyVqvPuqKAem06+44/bUpM7biVZOjGzrcR7bdwaMIXr8Mx4R1HizjQUjcM25/BSerXlgxS28rS8zGGKpw8t0wO0w/vIIe8yxJPUSJTSmhQ+J8LyGULf0+9pgbvKiE6PXWHmz0iF6/ardX7DJD7btvDLe7fLEx7QEOPPEDf3tSY17EGEOIPoQwqL+huqWP9xdBY4ghRIm+aEhjSE+mBI7T07Q8C5af0aUtw3Gql9SsW/qD4Qw2zewaZ5SzBv71CtQrNmN+FgEwmXXYeXB2Q6UsG/rrpO96bYZ2Z0u9YyKyq/T6VV5UB+v/QZ7Z143w/+2de7RdVXX/v3OutfY+j/skEPKAiE14BCEBQUSwCpYKlYq/4hB+tkqxWrVV29oOwY7+bPX3s6V1tGpph1L7sx2gVqs/baWCUhxAeRTCQxKBEMIjJCQhz/u+55y991pz/v7Y59z3vcmFm5NB2J9/Tk723Y+z1txzveb6TuRWwMYAZ/7VVvVpkiSZ9yGELO1/O7pK0UdHJfiajmZB/fAtfXXZo19E2VrG51V/VjVmmn5fhE8kel/vLCrehjlCFavuzdK9n6hSZ9UQM8/yxBZVWr1Ba4kP0tC/K+HPdCQPLJCgTy0j8139+ZKiCThI8vFXU+GvpfFDILKOyFXBq67brWmQzAfxktT1lg4qxfiTzPvgvQ+Z6lV/o+Hur/+oAmscPqn+mcVw0yzA4rJB3bwadmbfTDGXseYprd19Bsolg2j2LSaErlLv3TqoSdBB+UYHveH5tLmBUYNuOZH5b3TjsiJ7+EFCkxR2xr8QMzFxZEAn35hmQ8HXg2jwiV6OMsX2Szrks0xEhvTzy57TDe/43GpYsvi1JNtzLqaNwtli7U4dfAdmFvHlMnVHb3heBz9XJksgM8cOIzKxuV4bIQ3a0LuOLvFXNG3OVEjQnWuZPqubli5IVstXCzTxs/nFxkAu6x3DXXybbwwnjTTVEdF1Udk40/mvWg+SBq3rP+ITWr+0ozMybHHWVt94P+IpLTAZg6WbM/0Q3CzpXCp00T698y1wZQYRZn+BiRz+yDeSzEuiz51ncN7+tNGUkw1e95xt+JP6zPFFxoiDY5K66xSpVyK27GBLBr0f365DjTT4JKR6NbpsiZffrolkIST6f7HsZ/phxDGxw8pNwV8zPYeEsxQ/5vV/ozK9ZhjEnXjnwOiXFqEcE88uN0swAOOKvmTYJ5ok4WrE8fe1kbVGpV72nw+8S184oegDvFxaoeKwZOIIZ/0wJDVNNU31sW5Tps5o9WZNJfiaftPSR/QPUQGzxZJHVL8w3QAsA7epfhFTRwHGwjBivDt99N2WHGHO8Vts4PDLO2r9YVR8qn+PbnxgJMkarYBSnw78EvBW3XVKYQALBBGYyHW5+Pd3aRqCBKl/FLZC3bi8nnrxiX63hBO2fBbWAIyeu1W/gnhKTKYhMP6P6q3dUy2ALOAMLpLvv4Y6D6TwS8QGp27Uel28juiD3eAVmzTNsuZcVQjZ6GXAudp3ftEELBREREyxgzv99uBTX0v0gaNhy7YDn9EkhFG9eZGjG78KQ4bJVu5Q/To6Wzlbmh9BjcWGrPGWZZi6YYcN+XDWDX/w7u1RTQ4QomSMscu+trpuSsBItPvDg71y8ck1YZbm6IVM3AukiHsKA1goVKGWkxCFxy+/ruEysF97ESTVUXPdd6MRjdGosd67D2wVFCkDDG5F745VgwheTKW6dNrlU7F81Pm/e310EDEcjKz0T+ensQui5D++vlKzv+JLEzsNarqABK7rcBfbkYRSJiyeS43/9b6tcVAp/WZVSFHyf/p8Zypo1Am3fs9pAOCJAEWY+iZbMJY+kOhfTH0zS44MOsswldKBRT1ctfodHUzDaJbszT5FlRin7sgyzYXmcykRvRZY7fV3CrnohcQwwExxFWtu00bma+fDMcUOV9XCkF6PLoK1AJFB932qXx3fHND6JJB58SHb+NWpE0FJYGeH62Wp6ZzunwgUEb5+xUinSSuZHv3FL3aGgKuXafDSXLEkQBGBNGBR0QlcSIICTJqkHT9//z+wrZcvZZE4ofJ3fswl7EVKVoQiBaESewyOTy20DCADETaxW7o2TJkKUkkCUx2UhNksgECAiaK0fMOVg3FAlGp8w2e1VvJrLoeJTYUIBIUCKRjqazgKrUxYBQuBAj4A6t3A7/55KIcPLAk2sammXxgKjQHkYh4pQdFR9boDJjRLfswTsAfueR69V2GGlAK5HsxclWVhODn+W+8bqQYmX6984w8aFBL85kpPecBifh1lWFBowEEAVS3CQhYUn4bMfu7Dg2bxO0EwphGt+0qJhprhH2yVcPyxbmT/eFe/ZQACEG3+GeispRpNkgPJwZwio6TGmMYv/vTStGpNo17rvvmPEqtovPZiCKiZNYTyhaIeaEjyuejCASw4QpTG//S+F/h/wiElMH/7Ra41A78kMzFO6ZWBxzA1JJdBBmW8TweyD6JjvvVCoFIF79+jI0mjP8mC//dFNmZYvFdH/KRQNvH6DeD4J/VrbFC0AAsOgdhwJ9Zu2bcGDsZQBd/OXsut6bu4hC+pbIzH15TGOoEKBPrJvd32ikUjPH/HbGsfuaGyn6HdbttNv3bVkCEBYVkeB6i5HwFUFB0xkiHEFAhFA7DAEEMl0EjXhkv8BxGXgrDHD67fApuXNIfGkjNADyVumkOPwcQl/J4Ohd/BfJNNE6H3C4mKiu798Z+dBioZQ0wGV2m+xXTMA/hMb+ug3tv1e/HBJbErmC8MW0UVF960FCWwIecsU3MK1zm8SxrZZTBjddxaFUwhpGnpa1ed1fj9n2yxnueT1EcJl73niXXPb+zP9u8eZmN8ACkU9z+1yhsDIFBzb6iio3Mk60MHCrHgQ4PAExrlu54DEhIYgUItxAZlkfIV5Hc8xkLC06uXUMJ7Gnv1W65sDcyBpcUnnFrqaq3v58pARADH+FR9JMkykRDqqeZRoU+uRPlGvbOjiAg5hBAbynd/MDOBwA62hC534dBAuA6YZUmPjTF/q/16HeJOPght+XHy+FBDzNwKIybAccena5oMpyHzKl7UZ3V9bjXcV/XBY4rFgEPOeC0zgYGS63xEh7augZ05XRwMO1r+qG/IZ2DjeXkAGAvK9xliLHLJUsylK2+uawghbaRpWm9kXp49HfhL3bSyMIBDDE2oQGaiuETlmxr75dOIcgGf6WfAOUO/uNsPpZ8iU56XATRXJVsXAggow3aU0HXmR3862JSbE81055nAH+vO18+7q1lwsDSFfWiiDzDWovJ3oU9/UqUYdvLu4LETKTDf8+GbSsmfd36p385j/y4TBYWV8Y0iytowkoby8KOP/mN15VnnHEdxtaqoPD3KMoKO6uEupSOY1pZxjHXkIyWka675jb6jNvzeaMnzTF1AAAREtowrd6ejetNajDXoY0fn9Al5mPjYn7TOozFXxJ2LepcetziK8DGtXVR4gENFcx/fWDwxu9gC1d9YFxJ9/EJU8v2/s7XAxDEue1aH9YVrumCiyOYbUQiYc1xAucOhaSFj+QHjnBuLRHb4mNbeURjAocKwAYgtG8Mmcg4ATr36P+ujQ+mtZyHiuafg2ZgOnPOIDmXJuo/0ALZkTckCbAlsrJ292lpJa2aSC2j+g5mZqYTf1to7CwM4dBA4ssaYOALAnb/02Ru3DSQ16f/yCkRj+z1nO5dN1I2e64azAS+bPnN6BSAbxdYwmvvJ7My3pLHdK1OOzJCw+rc0ubwwgIWn1fvjyNpSbIBoxZm/9W+PD2gWMh39j1+2FDUleuZozYktxRW87Y5M0lRrt1574QoCYEpx7CzP1grMaQBT2gWDD2jjPYUBHCpiRzHB2F94z7Xf2tJU+G1su/GdFsYChicZwPTXWUEmcZU7Hrn06gsp2It/ZXDzzvvv69+1Pz9MxnhtpiIgjC/1Q1vJ53T6FTFJQ4IwmrpiFHBIYFYkQHTSay44Y/GpBtKIQcPbt3z7sQ2IST2TTM42M4M/VwnQRqnxL7dccOXbj4KUznzDpUPZlod37N+1t3/PEIQIKmhWbD5UEIWiKWg2xQI0jwSb+JAYqPX0FjFhC0fz7TLOewF633z6G8/qqnIQkNgX79t637rhERgTPAxkSsbGmRr0AHg0jBv+4S2vO+fKE46zyp18zBuU/GBtcDQVNdTo76sPPfv0vtHaiDSnC5xTLyowCKBWDJHmAoYTk9oBghFFN7SQinw5EKkyqcBJsCQE1wgBPcvPfvuaEyrcUGEeGnj8zg0vPJ/BiiEfAARMmCUAMEuPDgAgwZRlw4Zv9l504dLXH6OkGexiJSgJiJSgkJHt27bv2TMyOpTu2TlUQyxaMgkiLyBVZrF59PkkPUMo4D26isXAl4cSKYJxGZiiBnOS2VNXn3Hhmio8p67U2LDjgXs39gtgSsggs8dzYtYjCms484A75/hFp5+6rMtF1jVTU6vA+GBN/i2r7d6bbvp/j43WQAoLIq/KECadLEakBIUJa3903E1Xz1MIt2AKhkXFwVuvLNXuMy5cffoKhYelbN+6u59Yvz8gQloJmeRpZmdmdg+gMPABVPak9wG2p2yiZT3LlvRWHOAq1ahrSSeJMkGJO7tPpAveu/fppzbuGXhxiwdAVm2AzDDjSIosQU+hFPnyIFEm9YqMV5104iWndPVqvr1n/TO3PjMwSBr1NBoU1ciwn+NNm90ACAHWhlAnMhFrtg/AZiDXlCOAe5cf1dXT0XHMiuVHV6Srk6izvOqSzKVbn9y/5edPDdSHAywzRMeVi7UZW1yvo8v4ohc4XyYq7BoTPFDpPenNp61dCYNMuNa/7YEH1z+bATBxooMBnJYkm1HHr8UcHoBYNQHAyAAiQ0AAMZMnQ97L3r3NJ6l2lavL1r7+hJM7lU2IVp5Ikvb3bb//yb6ntgNgA1XJ7UZz6xmto8MmRSdwvmhLJJwoeI/uU15/3jnHdit5gOobtt1174tDgCllEqU19mxC4EQxZ/DlHG8hkQAsaOpITujIKQAiUhgGmvt+QItfd/rJx516bOw0iMAZpfTJh3ZsuHs/AMTRCHMGgGzG0vOj8zed38cHHXP26oNIJgyfmQIMeeLAJFbIA27Rmy5Yc9rRKmAFb7znmfXrhgFE7D0mFOwURcjpzKHVpQrkp+vk/pq2jlKWC1YBzKp7dt+Bcm/52Nedu+y4JZ2klIU1a3VgcOfNj27ZmiQG2qGjUG8VmYcp4kFmhKBW8uk1zXVYVCEECGw5idKADOVjVp3xP1Ye1akSmNM9m3565849GdiCJBurtJwDvmMvox0eyyYCADCGFEgBEGt15drXLjv1tZ1lW7euQWbXPZsee2hfA4iADAzzw0s2n7e/8ACzYj2sNEO3iYwYZk5TAHHc86Ylp731BI2yesWmQ5vvffSBrQrARJnnGSdiD3Cnl/6Qk+8kUAHBOK/sh9evB7pXLTvt/N7XLI+9HHOFGX1uyz3r9j2bAlFA8CiWAmaBoKywAgUDTOJV8o08Rx3fufyC1YtWlTRkpG7okaf/a+OT/QBZEgl1wKbAfKOtF7InTlDmoAbKUIKKwlbeeMYvvHW1QjUYjO6866mHH0qM6r9f9uzbthUeYCZIozBp2ibu7OnpOXZJ5eg1q3piC8qYiLbe8ez9j4x6gI1qABNIXlJxLpReJymgxALWgHwjIRtDfuT2281xK05/29lLSFznSaforqf/+bsNVbiuYjFgJghga22GhAAACOVJREFUf1Tsj128uHvV0t4edpGN41K5REqZOtWsMbT+vx94flsAq2VPogI6cFvfJkyeDwhohhyQi8oWAEpn/+k379yRpX5En1wF/EBffGPRCMwIMVb8pw4nSTqWAVI0iIpoMrD9ths/d0aZAFDZsbPjodUHUO+alQVW7G0tOKA1VsgAGENZ42cPo/vkNee95fh6z9HPgGHKRdqoGVFG95sytWJVW5L/I8P1+tD2jTsGn9jswapE0AbB6/juLX2JE+ttkGymEGDYxDL84IP/fNH3Sr3LAYWtFksBs9HZUYu5sXu0v384qYesf9u2vtro8LAHYCsSPEGhNClF6EvmkBtAcxmCvAdFlYHHJelYAnhwZ2EAs2EQDfzxg74xWk8ljLlJdqQqNXYkpMDC1P+hN4D8GQMi9eqHIL6CGEhhuuY9Ynl1QIpe8J6b9+TfQMyqSqwhAxNIs1ysTxem9Nqj2k9ACsMq0MyAgBrKPQCoCAqZjuJEYF/NQPL5dgRhlQAwiTCPVfvClFx7DEBhKEgAoCYpM9CAqRbDwNk4AbR7FPkUvICb0ZYETAmvWAjalbcjb8lIawoYIAMWA0VU2Ewon6b0tFrfXMVTxdS4qgWk3UsyYgwIGFGUioigWaAeQjLuHw9tKbXdAPIFyv1JsTt8Vrgq8O0qn7YbgAICDCRFD2B2KoR6u+7VdgPwBAKGD6Q7/mrGOsIQ2jRN2l4DIGjmERkUiWPnoOxUBiHtcZFt9gDECYEdBDhQ8olXJQQiRKDhvnZ5yPYagBD2ROgI6KshFI3ANIiZGOeW8OJz7bpluz1AuhGIQzyQtquNe2UhAsIVcbq3n9s0RdLmPgDjMcgim/XvL4aBM6CKsp5wdhY9PGiPyGGgCrZCz+5E351I2nrnVwRkQd6/90TX90OENsX4tP093DZslh9bcg8UPYAZcckp7wc9/Lg7UkcB2LUtlM6t4YlaTzEKmIqSY/rY6nryr/vJHJEz5dY4/JXq7Yjw4+/DgScFss1lEDT9+IHs53Da1zSJvBkj9nK1dGYCDIjIlLrxydqQ/gDuCJVRZxfh10fq+y4GXXINDDfThozJC80OTdc2I8z7lEPATE/ezJCS6/UxEYGYJphwLqfG47+aDIMZzuHdI6PDz5yG6MAZ2l6RMEpYcr/ov9lyvNSBTS4gNbO61OQynS5s1XYDmNFOaSYDpkkyrU0J3fw/mcfSNBERNw3DOAcXO1y1r9ZIr0AVdERm1SMDG+EaX9dPYQUYlCcif6UYwDx/7DjMYx88GWOYrbPsbLVMWP6F+sCI/zgiKh+hIfPsENmlj0q6883oaLYAE3Qk5yrOl9AEHKhVObjGZ+IZxhhjrTHWWusiF0VR5JxzUYs4J3LWWmetMczOWSYYZ5iITfPGM1571cee0P6s/mlTMTFMu2y3va8IKzRO3vWdUH7mqnURpUp5hGvzKebKSzb9KB3oFKU5/6CpqthskSdILzBAmJwolyCigoNPojL5Z4MIxGxMABkXlA0bwFgSsAhb6lm9tvdXj9aG233Nv1QyiepmdlWfhaXNBiAsDLnu2tS+8MH/Cp0+IRUyU3a1TZ4EbQa/Eoggue5YLjs2XmfNP1GwKjNDpBlMNXbfhQinYoaJLFtnXeQMx2VYY1hBbCyBjGVVEJhIycXOsLHOMTEbpsgIO2cdiC07zhNrGCaAIDBcXRRr8KX6LX+9zmUATJuqv+1jJVJS4tKXP5T69IYv73ZGBIzgyJOo5oNfJei4mwwgYyCi8tLKxHAuW9fqRBAhT7MqxMYYa20pcoy4GsFYR9ZYqljj4sg5stYwEzGRYWMMkYki6yxHkWE2+RFCrpMB4mbgJgDNA7jnLNw8FSMFYgJIvaGhB75yb18kCO2cAGivAZCSkppSev1H90VdG776H7vm4VOZ2IIswxDIOsPWOdOsV2ttHDkbk4tiGxljnTWuVI4NIa9FtlEUGUcASIMalsBgNgRDUCMckwg1uxXNjCeYqQ41dz4qLa9DrTkKbZ47X5QUydC+Z+/66VOJjdOsvYGybTcAQG01NR/6k96AaMutz72wq9HdgUrFOEOAtYbUMSDSFLSvEsCGyLAl26GlcoSIiR2VO1w5mvsta1YWVIhUlfIYBKUDvJuzX02AIM0Q7WY+TAY0iCgkIw0agmgQn6WJIMuSLFgVCSogFc1GSVUpkwmdEwMNKQYeXZ8BLkqJ0/Zu9G23AQCkzIiTs699e7mBEiUjDVeJDevBPYqSKogwnogSBKghJR6vWKU5L6ckDQ6jGkiEPSgdgmmkPm8hCEFD8KISvM9kWCQEEUHI0rTOyVA9X7NVzU2AkSdGRBYACUFE83PFaxA56M6HiVLEiXJ7G4C2G0Be/pZEKTrvisurjpigSWBmJkJLBGvsDVVSodbuCKhabXUYFQQhZlVAKWRJkgbJ1PtM6iEdGfVQCaLwWZ68ViRIngMZDZXRUfG1VOAzDez8YIBvKrLkAtgqCs39/HwTXBNLrpObj/m4mTCRmo4DU8c8zb1zAWPdh7YmVD0sMyVEaiEqJ62+5MxeLnc4w5qlifeJqBIFCUEgHqKqwyN1afR5FlEVoDFcR5ZlWVAJEpRIoKpIM++DIgAiSCCNlxBuQhgbkNL4MFBaKY4VBAhYlVWpKZymzbFmq60Z103SXBaVmlLp2rrsbONS0jxgvr3VfzhXTIzxAqCra8kxXc74kcGBkfr+kM0lanjwv8owUS5Rmc/CjrmNsd9MEib/9pcgsHQkcPgMgBhQIj/p/5R4bA9Uy18qoOOr1jplCVtmmCEiKOiArnvCT5/w5r7aOJyLTkyqYCixIRWRfPivE3LPARCAiFpilIqD8BD5bBGx6Pzcyatyo+LhXXVkymNDqSUnfHBVMGEYd7B1Nq1yX5W1PQOHcdUp72/TWK7Zls+fHKTEtABGemQurh8BtFbNm0vlh2ABt5XLrqCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCg4JXJ/wdKoqMbPplKAQAAACV0RVh0ZGF0ZTpjcmVhdGUAMjAyNi0xMS0wNFQwMDozNjoxNyswMDowMIUo470AAAAldEVYdGRhdGU6bW9kaWZ5ADIwMjYtMTEtMDRUMDA6MzY6MTcrMDA6MDD0dVsBAAAAKHRFWHRkYXRlOnRpbWVzdGFtcAAyMDI2LTExLTA0VDAwOjM2OjMyKzAwOjAws31SBAAAAABJRU5ErkJggg=="
FAV_B64 = "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"


ASSETLINKS = [{"relation": ["delegate_permission/common.handle_all_urls"], "target": {"namespace": "android_app",
               "package_name": "com.onrender.hark_whatsapp_bot.twa",
               "sha256_cert_fingerprints": ["35:5F:BC:7D:44:B6:30:7E:DB:8D:B4:4E:15:42:29:40:73:2F:CE:15:3F:B7:0B:67:5B:AD:01:ED:A4:76:28:D6"]}}]


@bp.get("/.well-known/assetlinks.json")
def assetlinks():
    return Response(json.dumps(ASSETLINKS), mimetype="application/json")


@bp.get("/agent/tygar.apk")
def agent_apk():
    import os
    from flask import send_file
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "tygar.apk"),
                     mimetype="application/vnd.android.package-archive", as_attachment=True, download_name="Tygar.apk")


@bp.get("/agent/logo.png")
def agent_logo():
    import base64
    return Response(base64.b64decode(LOGO_B64), mimetype="image/png", headers={"Cache-Control": "public, max-age=86400"})


@bp.get("/favicon.ico")
@bp.get("/agent/favicon.png")
def agent_fav():
    import base64
    return Response(base64.b64decode(FAV_B64), mimetype="image/png", headers={"Cache-Control": "public, max-age=86400"})


CHAT_DAILY = 30
CHAT_SYS = ("You are Tygar, a free, friendly, fan-made AI chat assistant for people across Africa, made by Unofficial Hark Fan Africa "
            "(not affiliated with Hark). Reply in the user's language, including Naija Pidgin. Keep answers short and clear. "
            "You can't browse, log in, buy, post or message anyone from this chat; for web research tell them to use the Agent box above. "
            "Never ask for passwords, OTPs, card or bank details.")


CHAT_JOBS = {}


@bp.get("/agent/chat/<jid>")
def agent_chat_job(jid):
    j = CHAT_JOBS.get(jid)
    if not j:
        return {"reply": "That message expired. Please send it again."}
    if "reply" not in j:
        return {"pending": True}
    return {"reply": j["reply"], "image": j.get("image"), "pdf": j.get("pdf"), "pdf_name": j.get("pdf_name"), "heard": j.get("heard"), "video": j.get("video")}


@bp.post("/agent/chat")
def agent_chat():
    import hashlib
    d = request.get_json(silent=True) or {}
    email = _who_email()
    who = ("cem:" + hashlib.sha256(email.encode()).hexdigest()[:16]) if email else ("c:" + _ip_hash())
    key = (who, _day())
    if _uses.get(key, 0) >= CHAT_DAILY:
        return {"error": f"You've used today's {CHAT_DAILY} free chat messages. Come back tomorrow!"}, 429
    msgs = []
    for m in (d.get("messages") or [])[-10:]:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
            msgs.append({"role": m["role"], "content": m["content"][:1500]})
    if not msgs or msgs[-1]["role"] != "user" or not msgs[-1]["content"].strip():
        return {"error": "Type a message first."}, 400
    _uses[key] = _uses.get(key, 0) + 1
    if H.get("web_chat"):
        jid = secrets.token_urlsafe(9)
        CHAT_JOBS[jid] = {"ts": time.time()}

        def work():
            try:
                CHAT_JOBS[jid].update(H["web_chat"](who, msgs[-1]["content"]))
            except Exception as e:
                print("web chat error:", str(e)[:200])
                CHAT_JOBS[jid]["reply"] = "Sorry, I hit a problem. Please try again in a minute."
        for k in [k for k, v in CHAT_JOBS.items() if time.time() - v["ts"] > 900]:
            CHAT_JOBS.pop(k, None)
        threading.Thread(target=work, daemon=True).start()
        return {"job": jid}
    out = _llm([{"role": "system", "content": CHAT_SYS}] + msgs, json_mode=False, limit=40)
    out = re.sub(r"<think>.*?</think>", "", out or "", flags=re.S).strip()
    return {"reply": out or "Sorry, I'm busy right now. Try again in a minute."}


@bp.post("/agent/chat/audio")
def agent_chat_audio():
    import hashlib
    email = _who_email()
    who = ("cem:" + hashlib.sha256(email.encode()).hexdigest()[:16]) if email else ("c:" + _ip_hash())
    key = (who, _day())
    if _uses.get(key, 0) >= CHAT_DAILY:
        return {"error": f"You've used today's {CHAT_DAILY} free chat messages. Come back tomorrow!"}, 429
    f = request.files.get("audio")
    if not f or not H.get("transcribe"):
        return {"error": "No audio received."}, 400
    audio = f.read(19_000_001)
    if len(audio) > 19_000_000:
        return {"error": "That audio is too big. Please keep it under about 5 minutes."}, 400
    mt = (f.mimetype or "") + " " + (f.filename or "")
    ext = next((e for e in ("mp3", "m4a", "mp4", "wav", "ogg", "oga", "opus", "flac", "aac") if e in mt.lower()), "webm")
    ext = {"oga": "ogg", "aac": "m4a"}.get(ext, ext)
    _uses[key] = _uses.get(key, 0) + 1
    jid = secrets.token_urlsafe(9)
    CHAT_JOBS[jid] = {"ts": time.time()}

    def work():
        try:
            heard = H["transcribe"](audio, f"voice.{ext}")
            if not heard:
                CHAT_JOBS[jid]["reply"] = "I couldn't hear anything in that. Try again a bit louder."
                return
            out = H["web_chat"](who, heard)
            out["heard"] = heard
            CHAT_JOBS[jid].update(out)
        except Exception as e:
            print("web voice error:", str(e)[:200])
            CHAT_JOBS[jid]["reply"] = "Sorry, I couldn't listen to that. Please try again or type it."
    threading.Thread(target=work, daemon=True).start()
    return {"job": jid}


def _chat_who():
    import hashlib
    email = _who_email()
    return ("cem:" + hashlib.sha256(email.encode()).hexdigest()[:16]) if email else ("c:" + _ip_hash())


@bp.post("/agent/chat/photo")
def agent_chat_photo():
    who = _chat_who()
    key = (who, _day())
    if _uses.get(key, 0) >= CHAT_DAILY:
        return {"error": f"You've used today's {CHAT_DAILY} free chat messages. Come back tomorrow!"}, 429
    f = request.files.get("photo")
    if not f or not H.get("vision"):
        return {"error": "No photo received."}, 400
    img = f.read(4_000_001)
    if len(img) > 4_000_000:
        return {"error": "That photo is too big. Please use one under 4 MB."}, 400
    mime = "image/png" if "png" in (f.mimetype or "") else "image/webp" if "webp" in (f.mimetype or "") else "image/jpeg"
    ask = (request.form.get("ask") or "")[:800]
    _uses[key] = _uses.get(key, 0) + 1
    jid = secrets.token_urlsafe(9)
    CHAT_JOBS[jid] = {"ts": time.time()}

    def work():
        try:
            CHAT_JOBS[jid].update(H["vision"](who, img, mime, ask))
        except Exception as e:
            print("web photo error:", str(e)[:200])
            CHAT_JOBS[jid]["reply"] = "Sorry, I couldn't read that photo. Please try again."
    threading.Thread(target=work, daemon=True).start()
    return {"job": jid}


_tts_use = {}


@bp.post("/agent/speak")
def agent_speak():
    who = _chat_who()
    key = (who, _day())
    if _tts_use.get(key, 0) >= 40 or not H.get("tts"):
        return {"error": "Voice limit reached for today."}, 429
    t = ((request.get_json(silent=True) or {}).get("text") or "").strip()[:1200]
    if not t:
        return {"error": "Nothing to read."}, 400
    _tts_use[key] = _tts_use.get(key, 0) + 1
    try:
        return Response(H["tts"](t), mimetype="audio/mpeg")
    except Exception as e:
        print("web tts error:", str(e)[:200])
        return {"error": "Voice is busy, try again."}, 503


_place_use = {}


@bp.get("/agent/places")
def agent_places():
    """Find a shop/place by name with free OpenStreetMap data (Photon, then Nominatim), biased to the user's location."""
    who = _chat_who()
    key = (who, _day())
    if _place_use.get(key, 0) >= 60:
        return {"error": "You've used today's free searches. Come back tomorrow!"}, 429
    q = (request.args.get("q") or "").strip()[:120]
    if len(q) < 2:
        return {"error": "Type a shop or place name."}, 400
    _place_use[key] = _place_use.get(key, 0) + 1
    try:
        lat = float(request.args.get("lat")); lon = float(request.args.get("lon"))
    except Exception:
        lat = lon = None
    R = H["requests"].Session()
    R.trust_env = False   # skip netrc/proxy lookups (they deadlocked a worker on Render)
    ua = {"User-Agent": "TygarAgent/1.0 (fan-made free helper; hark-whatsapp-bot.onrender.com)"}
    out = []
    try:
        p = {"q": q, "limit": 8, "lang": "en"}
        if lat is not None:
            p.update(lat=lat, lon=lon)
        for f in R.get("https://photon.komoot.io/api/", params=p, headers=ua, timeout=(4, 8)).json().get("features", []):
            pr = f.get("properties", {})
            c = f.get("geometry", {}).get("coordinates") or [None, None]
            addr = ", ".join(x for x in [pr.get("housenumber"), pr.get("street"), pr.get("district") or pr.get("locality"),
                                         pr.get("city") or pr.get("county"), pr.get("state"), pr.get("country")] if x)
            kind = (pr.get("osm_value") or "").replace("_", " ")
            out.append({"name": pr.get("name") or q, "addr": addr, "kind": kind, "lat": c[1], "lon": c[0]})
    except Exception as e:
        print("photon error:", str(e)[:120])
    if not out:
        try:
            p = {"q": q, "format": "jsonv2", "limit": 8, "addressdetails": 0}
            if lat is not None:
                p["viewbox"] = f"{lon-1},{lat+1},{lon+1},{lat-1}"
            for r in R.get("https://nominatim.openstreetmap.org/search", params=p, headers=ua, timeout=(4, 8)).json():
                out.append({"name": r.get("name") or q, "addr": r.get("display_name", ""), "kind": (r.get("type") or "").replace("_", " "),
                            "lat": float(r["lat"]), "lon": float(r["lon"])})
        except Exception as e:
            print("nominatim error:", str(e)[:120])
    out = [o for o in out if o["lat"] is not None]
    if lat is not None:
        import math
        for o in out:
            dl, dn = math.radians(o["lat"] - lat), math.radians(o["lon"] - lon)
            h = math.sin(dl/2)**2 + math.cos(math.radians(lat))*math.cos(math.radians(o["lat"]))*math.sin(dn/2)**2
            o["km"] = round(6371 * 2 * math.asin(math.sqrt(h)), 2)
        out.sort(key=lambda o: o["km"])
    return {"results": out[:6]}


ICON192_B64 = "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"
ICON512_B64 = "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"
AGENT_MANIFEST = {"name": "Tygar - free AI buddy for Africa", "short_name": "Tygar", "start_url": "/agent?app=1", "scope": "/agent",
                  "display": "standalone", "background_color": "#5b0eeb", "theme_color": "#c2189b", "orientation": "portrait",
                  "description": "Chat, voice, pictures, PDFs, videos, bet code converter, shop finder and a live web agent. Free.",
                  "icons": [{"src": "/agent/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                            {"src": "/agent/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]}
AGENT_SW = """const C='tygar-v1';self.addEventListener('install',e=>{self.skipWaiting();e.waitUntil(caches.open(C).then(c=>c.addAll(['/agent','/agent/logo.png','/agent/icon-192.png'])));});
self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
self.addEventListener('fetch',e=>{const r=e.request;if(r.method!=='GET')return;const u=new URL(r.url);
if(u.pathname==='/agent'){e.respondWith(fetch(r).then(x=>{if(x.ok){const y=x.clone();caches.open(C).then(c=>c.put('/agent',y));}return x;}).catch(()=>caches.match('/agent')));}});
self.addEventListener('push',e=>{let d={};try{d=e.data.json();}catch(x){d={title:'Tygar',body:(e.data&&e.data.text())||'Reminder'};}
e.waitUntil(self.registration.showNotification(d.title||'Tygar reminder',{body:d.body||'',icon:'/agent/icon-192.png',badge:'/agent/icon-192.png',tag:d.tag||('t'+Date.now()),data:{url:'/agent'},vibrate:[120,60,120]}));});
self.addEventListener('notificationclick',e=>{e.notification.close();e.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(ls=>{for(const c of ls){if(c.url.includes('/agent')&&'focus'in c)return c.focus();}return clients.openWindow('/agent');}));});"""


@bp.get("/agent/manifest.json")
def agent_manifest():
    import json
    return Response(json.dumps(AGENT_MANIFEST), mimetype="application/manifest+json")


@bp.get("/agent/icon-192.png")
@bp.get("/agent/icon-512.png")
def agent_icon():
    import base64
    b = ICON512_B64 if request.path.endswith("512.png") else ICON192_B64
    return Response(base64.b64decode(b), mimetype="image/png", headers={"Cache-Control": "public, max-age=86400"})


@bp.get("/agent/sw.js")
def agent_sw():
    return Response(AGENT_SW, mimetype="application/javascript", headers={"Service-Worker-Allowed": "/agent", "Cache-Control": "no-cache"})


@bp.get("/agent")
def agent_home():
    return Response(APP_HTML, mimetype="text/html")


@bp.post("/agent/start")
def agent_start():
    import hashlib
    d = request.get_json(silent=True) or {}
    email = _who_email()
    if email:
        out = web_start(d.get("goal"), "em:" + hashlib.sha256(email.encode()).hexdigest()[:16], DAILY_SIGNED)
    else:
        out = web_start(d.get("goal"), _ip_hash())
    return (out, 400) if "error" in out else out


@bp.get("/live/<token>")
def live_page(token):
    if token not in LIVE:
        return "This live view has ended.", 404
    return Response(LIVE_HTML.replace("__T__", token), mimetype="text/html")


@bp.get("/live/<token>/state")
def live_state(token):
    _cleanup()
    v = LIVE.get(token)
    if not v:
        return {"gone": True}
    if not v["done"] and time.time() - v["ts"] > 600:
        v["status"] = "\u26A0 This task took too long and was stopped."
    return {"goal": v["goal"], "steps": v["steps"], "status": v["status"], "done": v["done"], "n": len(v["steps"]),
            "answer": v.get("answer", "") if v["done"] else "", "phase": v.get("phase", ""), "llm_s": v.get("llm_s"), "view": v.get("view"), "fn": v.get("fn", 0), "engine": _ENGINE_USED[0],
            "llm_err": re.sub(r"(gsk_|Bearer )\S+", "***", LAST_ERR[0])[:200]}


@bp.get("/live/<token>/frame.jpg")
def live_frame(token):
    v = LIVE.get(token)
    if not v or not v["frame"]:
        return "", 204
    return Response(v["frame"], mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


LIVE_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tygar Agent - live</title><style>
body{margin:0;background:#0d0f14;color:#e8e8ea;font-family:system-ui,sans-serif}
header{padding:12px 14px;border-bottom:1px solid #222;display:flex;align-items:center;gap:8px}
.dot{width:9px;height:9px;border-radius:50%;background:#e33;animation:b 1s infinite}.done .dot{background:#2c2;animation:none}
@keyframes b{50%{opacity:.3}}h1{font-size:15px;margin:0;flex:1}
.screen{margin:12px;border:1px solid #2a2d36;border-radius:10px;overflow:hidden;background:#000}
.screen img{width:100%;display:block;min-height:180px}
#status{margin:0 14px;color:#f5b301;font-size:14px}ol{margin:8px 14px 20px;padding-left:4px;list-style:none;font-size:14px;line-height:1.6}
footer{font-size:11px;color:#777;padding:0 14px 16px}</style></head><body>
<header><span class="dot"></span><h1 id="goal">Tygar Agent</h1><span id="live">LIVE</span></header>
<div class="screen" style="background:#fff;color:#222;padding:12px;font-size:13px;min-height:200px"><div id="vurl" style="color:#1a7f37;font-size:12px;word-break:break-all"></div><b id="vtitle" style="color:#1a0dab;font-size:16px"></b><div id="vtext" style="white-space:pre-wrap;max-height:340px;overflow:hidden"></div></div>
<div id="status">Starting...</div><ol id="steps"></ol>
<footer>Look-only agent: it reads public pages and never logs in, posts or buys. Fan-made, not affiliated with Hark.</footer>
<script>
const T="__T__";let n=-1;
async function tick(){try{const s=await (await fetch('/live/'+T+'/state',{cache:'no-store'})).json();
if(s.gone){document.getElementById('status').textContent='This live view has ended.';return;}
document.getElementById('goal').textContent=s.goal;document.getElementById('status').textContent=s.status;
if(s.n!==n){n=s.n;document.getElementById('steps').innerHTML=s.steps.map(x=>'<li>'+x.replace(/</g,'&lt;')+'</li>').join('');}
if(s.view){document.getElementById('vurl').textContent=s.view.url;document.getElementById('vtitle').textContent=s.view.title;document.getElementById('vtext').textContent=s.view.text;}
if(s.done){document.body.classList.add('done');document.getElementById('live').textContent='DONE';return;}}catch(e){}
setTimeout(tick,1500);}tick();</script></body></html>"""


APP_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tygar Agent</title><link rel="manifest" href="/agent/manifest.json"><meta name="theme-color" content="#c2189b"><link rel="apple-touch-icon" href="/agent/icon-192.png"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes"><link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"><link rel="icon" href="/agent/favicon.png"><meta property="og:image" content="/agent/logo.png"><style>
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;min-height:100vh;color:#fff;font-family:"Segoe UI",system-ui,sans-serif;font-size:17px;line-height:1.55;
background:linear-gradient(160deg,#5b0eeb,#c2189b 45%,#ff6a00 100%);background-size:200% 200%;animation:bg 14s ease infinite;background-attachment:fixed}
@keyframes bg{0%,100%{background-position:0 0}50%{background-position:100% 100%}}
body::before{content:"";position:fixed;inset:0;background:url(/agent/logo.png) center 60%/min(85vw,520px) no-repeat;opacity:.08;mix-blend-mode:screen;pointer-events:none;z-index:0}
.blob{position:fixed;border-radius:50%;filter:blur(60px);opacity:.55;z-index:0;pointer-events:none;animation:fl 12s ease-in-out infinite}
.b1{width:260px;height:260px;background:#00e5ff;top:-60px;left:-80px}.b2{width:300px;height:300px;background:#ffe600;bottom:-90px;right:-100px;animation-delay:-6s}
@keyframes fl{50%{transform:translate(40px,30px) scale(1.15)}}
.wrap{position:relative;z-index:1;max-width:720px;margin:0 auto;padding:16px 16px 28px}
.top{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:6px}
h1{font-size:26px;margin:0;font-weight:900;display:flex;align-items:center;letter-spacing:.3px}
.logo{width:52px;height:52px;border-radius:50%;border:3px solid #fff;box-shadow:0 0 0 3px #ffe600,0 0 24px #ffe600aa;margin-right:10px;animation:pop 3s ease-in-out infinite;background:#000}
@keyframes pop{50%{transform:scale(1.07) rotate(-4deg)}}
.logo.sm{width:38px;height:38px;margin-right:8px;box-shadow:0 0 0 3px #ff2e88;animation:none}
.grad{background:linear-gradient(90deg,#fff,#ffe600,#00e5ff,#fff);background-size:200%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:sh 5s linear infinite}
@keyframes sh{to{background-position:200% 0}}
#acct{font-size:14px;font-weight:800;white-space:nowrap}#acct a{color:#5b0eeb;background:#ffe600;padding:8px 14px;border-radius:30px;text-decoration:none;box-shadow:0 4px 14px #0004}
.hero{text-align:center;padding:26px 8px 18px}
.hero h2{font-size:30px;line-height:1.2;margin:0 0 10px;font-weight:900;text-shadow:0 3px 18px #0005}
.hero p{margin:0 auto;font-size:18px;max-width:520px;opacity:.95}
.badges{display:flex;justify-content:center;flex-wrap:wrap;gap:8px;margin-top:16px}
.badges span{background:#ffffff26;backdrop-filter:blur(6px);border:1px solid #ffffff70;padding:6px 14px;border-radius:30px;font-size:14px;font-weight:800}
.tg{display:inline-block;margin-top:16px;background:#fff;color:#229ed9;padding:12px 22px;border-radius:30px;font-weight:900;text-decoration:none;font-size:16px;box-shadow:0 6px 20px #0003}
.feats{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:8px 0 22px}
.feat{border-radius:20px;padding:18px 12px;text-align:center;color:#fff;text-decoration:none;box-shadow:0 8px 22px #0003;transition:transform .2s}
.feat:active,.feat:hover{transform:translateY(-3px) scale(1.02)}
.feat .ic{font-size:36px;line-height:1}.feat b{display:block;font-size:18px;margin-top:8px}.feat small{font-size:14px;opacity:.9}
.f1{background:linear-gradient(135deg,#00c6ff,#0072ff)}.f2{background:linear-gradient(135deg,#ff2e88,#ff7a59)}
.f3{background:linear-gradient(135deg,#00d27a,#00a3a3)}.f4{background:linear-gradient(135deg,#ffb800,#ff6a00)}
.card{background:#fff;color:#1d1240;border-radius:24px;padding:20px 16px;box-shadow:0 14px 36px #0004;margin-bottom:22px}
.card h2{font-size:23px;margin:0 0 6px;display:flex;align-items:center;font-weight:900}
.sub{color:#5a4f7a;font-size:16px;margin:0 0 14px}
form{display:flex;gap:8px}input{flex:1;min-width:0;padding:15px;border-radius:16px;border:3px solid #e2d7ff;background:#faf8ff;color:#1d1240;font-size:17px;outline:none}
input:focus{border-color:#7b2ff7;box-shadow:0 0 0 4px #7b2ff733}
button{padding:0 20px;border:0;border-radius:16px;background:linear-gradient(90deg,#ff2e88,#ff6a00);color:#fff;font-weight:900;font-size:17px;box-shadow:0 6px 16px #ff2e8855;cursor:pointer}
button:disabled{opacity:.5}
.chips{margin:12px -16px 2px;padding:0 16px 6px;display:flex;gap:8px;overflow-x:auto;scrollbar-width:none}.chips::-webkit-scrollbar{display:none}
.chip{flex:0 0 auto;font-size:14px;padding:9px 14px;border-radius:30px;cursor:pointer;font-weight:700;color:#fff;white-space:nowrap}
.chip:nth-child(4n+1){background:#0072ff}.chip:nth-child(4n+2){background:#00a86b}.chip:nth-child(4n+3){background:#ff8a00}.chip:nth-child(4n+4){background:#e0217a}
#err{color:#d6004c;font-size:15px;margin-top:8px;font-weight:700}
#run{display:none;margin-top:16px}.bar{display:flex;align-items:center;gap:10px;font-size:16px}
.dot{width:12px;height:12px;border-radius:50%;background:#ff2e88;animation:b 1s infinite;flex:0 0 auto}.done .dot{background:#00c46a;animation:none}
@keyframes b{50%{opacity:.3}}
.grid{display:grid;grid-template-columns:1fr;gap:12px;margin-top:12px}
.screen{border:3px solid #7b2ff7;border-radius:18px;overflow:hidden;background:#fff;color:#222}
#view{padding:14px;min-height:200px;max-height:320px;overflow:hidden;font-size:15px;line-height:1.5}#vurl{color:#1a7f37;font-size:13px;word-break:break-all}
#vtitle{font-size:18px;font-weight:800;margin:4px 0 6px;color:#1a0dab}#vtext{white-space:pre-wrap;color:#333}#vlinks{margin-top:8px;color:#1a0dab;font-size:14px}
.steps{background:#f4efff;border-radius:18px;padding:12px 14px;font-size:15px;line-height:1.7}
.steps ol{margin:0;padding:0;list-style:none}#status{color:#d6004c;margin-top:6px;font-weight:800}
#ans{display:none;margin-top:14px;background:#eafff4;border:3px solid #00c46a;border-radius:18px;padding:16px;white-space:pre-wrap;line-height:1.6;font-size:16px}
#login{display:none;background:#fff;color:#1d1240;border-radius:20px;padding:16px;margin-bottom:16px;box-shadow:0 10px 30px #0004}
#login input{width:100%;margin-bottom:10px}#login button{padding:13px 20px}#lmsg{font-size:14px;color:#d6004c;margin-top:6px}
#share{display:none;margin-top:12px;gap:8px;align-items:center;flex-wrap:wrap;font-size:15px;font-weight:800}
#share a{padding:9px 16px;border-radius:30px;color:#fff;text-decoration:none;font-weight:800}
#sx{background:#111}#sw{background:#25d366}#sf{background:#1877f2}#cp{background:#7b2ff7}
#chat .sub{margin:4px 0 6px}
#msgs{max-height:60vh;min-height:160px;overflow-y:auto;font-size:16px;line-height:1.6;display:flex;flex-direction:column;gap:10px;background:#f7f7fa;border-radius:16px;padding:14px 12px;margin-top:10px}
.m{padding:11px 15px;border-radius:18px;white-space:pre-wrap;max-width:86%;box-shadow:none}
.u{background:#e9ddff;color:#241046;align-self:flex-end;border-bottom-right-radius:6px}
.a{background:#fff;color:#1d1240;align-self:flex-start;border:1px solid #ececf2;border-bottom-left-radius:6px}
#cf{margin-top:12px;align-items:center;background:#f1f0f6;border:1px solid #e4e2ee;border-radius:28px;padding:5px 6px}
#cf:focus-within{border-color:#7b2ff7;box-shadow:0 0 0 3px #7b2ff722}
#ci{background:transparent;border:0;padding:12px 8px}#ci:focus{box-shadow:none}
#cf .ib{background:transparent;width:42px}
#cs{width:46px;height:46px;border-radius:50%;background:linear-gradient(135deg,#7b2ff7,#ff2e88);font-size:20px}
.card.main{padding:16px 14px 12px;border:1px solid #efe8ff;box-shadow:0 18px 40px #0004}
.card.main h2{font-size:20px}
.card.main #msgs{min-height:46vh;max-height:60vh}
.on{margin-left:auto;font-size:13px;color:#00b86b;font-weight:800}
.ib{flex:0 0 auto;width:48px;padding:0;display:flex;align-items:center;justify-content:center;font-size:22px;border-radius:16px;background:#f1eaff;box-shadow:none;cursor:pointer;color:#5b0eeb}
#mic.live{background:#ff2e88;color:#fff;animation:b 1s infinite}#cs{width:54px;padding:0;font-size:22px}
#cf{gap:6px}#rec{font-size:14px;color:#d6004c;font-weight:700;margin-top:6px}
.pdf{display:inline-block;margin-top:8px;background:#7b2ff7;color:#fff;padding:9px 16px;border-radius:30px;text-decoration:none;font-weight:800}
.more{text-align:center;font-size:20px;margin:6px 0 12px;font-weight:900}
.hero{padding:14px 6px 16px}.hero h2{font-size:26px}.hero p{font-size:16px}
.badges{margin:4px 0 6px}.tgw{text-align:center;margin-bottom:22px}
#map{height:300px;border-radius:18px;margin-top:12px;border:3px solid #7b2ff7}
.res{background:#f4efff;border-radius:14px;padding:10px 12px;margin-top:8px;cursor:pointer;font-size:15px}.res b{color:#5b0eeb}.res small{color:#5a4f7a;display:block}
#nav{margin-top:12px;text-align:center}#dist{font-size:40px;font-weight:900;background:linear-gradient(90deg,#ff2e88,#ff6a00);-webkit-background-clip:text;background-clip:text;color:transparent}
#dsub{color:#5a4f7a;font-size:15px;margin-bottom:8px}
#tl{list-style:none;padding:0;margin:10px 0 0}#tl li{display:flex;gap:10px;align-items:center;padding:10px 12px;background:#f4efff;border-radius:14px;margin-top:6px;font-size:16px}
#tl li.d span{text-decoration:line-through;opacity:.5}#tl li span{flex:1}#tl li i{cursor:pointer;font-style:normal}
.tools{margin-top:6px;display:flex;gap:6px}.tools a{font-size:13px;background:#f1eaff;color:#5b0eeb;padding:4px 10px;border-radius:20px;text-decoration:none;font-weight:700}
.you{width:18px;height:18px;border-radius:50%;background:#0072ff;border:3px solid #fff;box-shadow:0 0 0 6px #0072ff44}
#inst{display:flex;align-items:center;gap:10px;background:#fff;color:#1d1240;border-radius:20px;padding:10px 12px;margin:8px 0;box-shadow:0 8px 22px #0003}
#inst b{flex:1;font-size:16px}#inst small{display:none}#inst button{padding:10px 18px}
footer{font-size:12px;opacity:.85;margin:10px 0 0;text-align:center;line-height:1.5}
@media(min-width:760px){.feats{grid-template-columns:repeat(4,1fr)}.grid{grid-template-columns:1.5fr 1fr}.hero h2{font-size:38px}.wrap{max-width:900px}}
</style></head><body><div class="blob b1"></div><div class="blob b2"></div><div class="wrap">
<div class="top"><h1><img class="logo" src="/agent/logo.png" alt=""><span class="grad">Tygar Agent</span></h1><span id="acct"><a href="#" id="si">Sign in</a></span></div>
<div id="inst" style="display:none"><b>\U0001F4F2 Get the Tygar app</b><small>Free, no Play Store needed</small><button id="ib2" type="button" style="display:none">Install</button><a id="apk" href="/agent/tygar.apk" download="Tygar.apk" style="background:linear-gradient(90deg,#7b2ff7,#f107a3);color:#fff;border-radius:14px;padding:10px 14px;font-weight:700;text-decoration:none;white-space:nowrap">\u2B07 Download app</a></div>
<div class="hero"><h2>Your free AI buddy for Africa \U0001F30D\u2728</h2><p>Chat, talk, make pictures and PDFs, convert bet codes. English or Pidgin.</p></div>
<div id="chat" class="card main"><h2><img class="logo sm" src="/agent/logo.png" alt=""> Chat with Tygar <span class="on">\u25CF online</span></h2>
<div class="chips"><span class="chip cc">What can you do?</span><span class="chip cc">make a PDF on how to start a small business</span><span class="chip cc">convert VESTC6 from SportyBet to MSport GH</span><span class="chip cc">draw a lion in a Super Eagles jersey</span><span class="chip cc">make a video of Lagos at night</span><span class="chip cc">Wetin dey happen for Naija today?</span></div>
<div id="msgs"><div class="m a">Hi! I'm Tygar \U0001F44B Ask me anything, or type "what can you do?"</div></div>
<form id="cf"><button type="button" id="mic" class="ib" title="Talk">\U0001F3A4</button><label class="ib" title="Send a photo">\U0001F4F7<input id="pf" type="file" accept="image/*" hidden></label><label class="ib" title="Upload audio">\U0001F3B5<input id="af" type="file" accept="audio/*" hidden></label><input id="ci" placeholder="Ask Tygar anything..." maxlength="1500" autocomplete="off"><button id="cs">\u27A4</button></form><div id="rec"></div></div>
<div class="card" id="shop"><h2>\U0001F4CD Find a shop</h2>
<p class="sub">Type a shop's name. I'll pin it on the map, show you, and count down the distance live as you walk.</p>
<form id="sf2"><input id="sq" placeholder="e.g. Shoprite Enugu, Chicken Republic Warri" maxlength="120" autocomplete="off"><button id="sb">Find</button></form>
<div id="sres"></div><div id="map" style="display:none"></div>
<div id="nav" style="display:none"><div id="dist">--</div><div id="dsub"></div>
<a id="dir" class="pdf" target="_blank" rel="noopener">\U0001F9ED Directions</a> <a href="#" id="stop" class="pdf" style="background:#444">Stop</a></div></div>
<div class="card" id="agentcard"><h2>\U0001F50E Live web Agent</h2>
<p class="sub">Give it a task and watch it read the web live. 3 free a day, 5 signed in.</p>
<div id="login"><input id="em" type="email" placeholder="Your email" autocomplete="email"><button id="sc" type="button">Send code</button>
<div id="cbox" style="display:none;margin-top:8px"><input id="cd" inputmode="numeric" maxlength="6" placeholder="6-digit code"><button id="vc" type="button">Sign in</button></div>
<div id="lmsg"></div></div>
<form id="fm"><input id="g" placeholder="e.g. cheapest iPhone 13 on Jumia Nigeria" maxlength="200" autocomplete="off"><button id="go">Start</button></form>
<div class="chips"><span class="chip">Compare MTN and Airtel 10GB data plans</span><span class="chip">Latest Super Eagles news</span><span class="chip">Best laptops under 400k naira</span></div>
<div id="err"></div>
<div id="run"><div class="bar"><span class="dot"></span><b id="goal"></b></div>
<div class="grid"><div class="screen"><img id="shot" alt="Live page" style="display:none;width:100%;display:none;border-bottom:2px solid #7b2ff7"><div id="view"><div id="vurl"></div><div id="vtitle"></div><div id="vtext">Opening the web...</div><div id="vlinks"></div></div></div>
<div class="steps"><ol id="steps"></ol><div id="status">Starting...</div></div></div>
<div id="ans"></div>
<div id="share"><span>Share:</span><a id="sx" target="_blank" rel="noopener">X</a><a id="sw" target="_blank" rel="noopener">WhatsApp</a><a id="sf" target="_blank" rel="noopener">Facebook</a><a href="#" id="cp">Copy</a></div></div></div>
<div class="card" id="todo"><h2>\u2705 My to-do</h2><p class="sub">Saved on this phone only.</p>
<form id="tf"><input id="ti" placeholder="Add a task..." maxlength="200" autocomplete="off"><button>Add</button></form><ul id="tl"></ul>
<input id="rt" type="datetime-local" style="position:absolute;opacity:0;width:1px;height:1px;pointer-events:none"><div id="rmsg" style="font-size:13px;color:#5a4f7a;margin-top:6px"></div></div>
<h3 class="more">More from Tygar</h3>
<div class="feats"><a href="#agentcard" class="feat f1"><div class="ic">\U0001F50E</div><b>Live Agent</b><small>Watch it search</small></a>
<a href="#chat" class="feat f2"><div class="ic">\U0001F4AC</div><b>Chat</b><small>Ask anything</small></a>
<a href="#chat" class="feat f3"><div class="ic">\U0001F3AB</div><b>Bet codes</b><small>Convert in seconds</small></a>
<a href="#chat" class="feat f4"><div class="ic">\U0001F3A8</div><b>Images</b><small>Type and get art</small></a>
<a href="#shop" class="feat f1"><div class="ic">\U0001F4CD</div><b>Shop finder</b><small>Live distance</small></a>
<a href="#chat" class="feat f2"><div class="ic">\U0001F3A4</div><b>Voice</b><small>Talk, it listens</small></a>
<a href="#chat" class="feat f3"><div class="ic">\U0001F4C4</div><b>PDFs</b><small>Notes in seconds</small></a>
<a href="#chat" class="feat f4"><div class="ic">\U0001F3AC</div><b>Videos</b><small>Short clips</small></a></div>
<div class="badges"><span>\u26A1 100% free</span><span>\U0001F1F3\U0001F1EC English + Pidgin</span><span>\U0001F512 No passwords</span></div>
<a class="tg" href="https://t.me/Harkaiafricabot" target="_blank" rel="noopener">\u2708 Also on Telegram</a>
<a class="tg" href="https://wa.me/2349162737819" target="_blank" rel="noopener" style="color:#25d366;margin-left:8px">\U0001F4AC Message support on WhatsApp</a>
<footer>Look-only agent: it reads public pages and never logs in, posts, buys or reads email. Sign-in uses your email only to send a code; it isn't stored on our server. Sharing opens your own X, WhatsApp or Facebook app, so you post it yourself. Fan-made by Unofficial Hark Fan Africa, not affiliated with Hark.</footer></div>
<script>
let T=null,n=-1,F=0;const $=id=>document.getElementById(id);
let AUTH=localStorage.getItem('ta_auth')||'',EM=localStorage.getItem('ta_em')||'';
function acct(){$('acct').innerHTML=AUTH?(EM.replace(/</g,'&lt;')+' &middot; <a href="#" id="so">Sign out</a>'):'<a href="#" id="si">Sign in</a>';
if(AUTH){$('so').onclick=e=>{e.preventDefault();AUTH='';EM='';localStorage.removeItem('ta_auth');localStorage.removeItem('ta_em');acct();};}
else{$('si').onclick=e=>{e.preventDefault();$('login').style.display=$('login').style.display=='block'?'none':'block';};}}
acct();
async function post(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});return r.json();}
$('sc').onclick=async()=>{$('lmsg').textContent='';const d=await post('/agent/login/send',{email:$('em').value});
if(d.error){$('lmsg').textContent=d.error;return;}$('cbox').style.display='block';$('lmsg').style.color='#9c9';$('lmsg').textContent='Code sent. Check your inbox (and spam).';};
$('vc').onclick=async()=>{const d=await post('/agent/login/verify',{email:$('em').value,code:$('cd').value});
$('lmsg').style.color='#ff7b7b';if(d.error){$('lmsg').textContent=d.error;return;}
AUTH=d.token;EM=d.email;localStorage.setItem('ta_auth',AUTH);localStorage.setItem('ta_em',EM);$('login').style.display='none';acct();};
let CH=[];document.querySelectorAll('.cc').forEach(c=>c.onclick=()=>{$('ci').value=c.textContent;$('ci').focus();});
function addm(r,t){const e=document.createElement('div');e.className='m '+(r=='user'?'u':'a');e.textContent=t;$('msgs').appendChild(e);$('msgs').scrollTop=1e9;return e;}
function tools(w,txt){const b=document.createElement('div');b.className='tools';
const sp=document.createElement('a');sp.href='#';sp.textContent='\U0001F50A Listen';sp.onclick=async ev=>{ev.preventDefault();sp.textContent='\u23F3';
try{const r=await fetch('/agent/speak',{method:'POST',headers:{'Content-Type':'application/json','X-Agent-Auth':AUTH},body:JSON.stringify({text:txt})});
if(!r.ok)throw 0;const u=URL.createObjectURL(await r.blob());new Audio(u).play();sp.textContent='\U0001F50A Listen';}catch(x){sp.textContent='Voice busy';}};
const cp=document.createElement('a');cp.href='#';cp.textContent='\U0001F4CB Copy';cp.onclick=ev=>{ev.preventDefault();navigator.clipboard.writeText(txt);cp.textContent='Copied';};
b.appendChild(sp);b.appendChild(cp);w.appendChild(b);}
async function finish(w,d){
const jid=d.job;while(jid&&(d.job||d.pending)){await new Promise(z=>setTimeout(z,1500));d=await (await fetch('/agent/chat/'+jid)).json();}
if(d.heard){const h=w.previousSibling;if(h&&h.classList.contains('u'))h.textContent='\U0001F3A4 '+d.heard;CH.push({role:'user',content:d.heard});}
w.textContent=d.reply||d.error;
if(d.image){const im=document.createElement('img');im.src=d.image;im.style.cssText='width:100%;border-radius:12px;margin-top:6px';w.appendChild(im);
const a=document.createElement('a');a.href=d.image;a.download='tygar.jpg';a.className='pdf';a.textContent='\u2B07 Save image';w.appendChild(document.createElement('br'));w.appendChild(a);}
if(d.video){const v=document.createElement('video');v.src=d.video;v.controls=true;v.playsInline=true;v.style.cssText='width:100%;border-radius:12px;margin-top:6px';w.appendChild(v);
const a=document.createElement('a');a.href=d.video;a.download='tygar.mp4';a.className='pdf';a.textContent='\u2B07 Save video';w.appendChild(document.createElement('br'));w.appendChild(a);}
if(d.pdf){const a=document.createElement('a');a.href=d.pdf;a.download=d.pdf_name||'tygar.pdf';a.className='pdf';a.textContent='\u2B07 Download PDF';w.appendChild(document.createElement('br'));w.appendChild(a);}
if(d.reply){CH.push({role:'assistant',content:d.reply});if(!d.image&&!d.video&&!d.pdf)tools(w,d.reply);}$('msgs').scrollTop=1e9;}
$('cf').onsubmit=async e=>{e.preventDefault();const t=$('ci').value.trim();if(!t)return;$('ci').value='';CH.push({role:'user',content:t});addm('user',t);
const w=addm('assistant','...');$('cs').disabled=true;
try{const r=await fetch('/agent/chat',{method:'POST',headers:{'Content-Type':'application/json','X-Agent-Auth':AUTH},body:JSON.stringify({messages:CH.slice(-10)})});
await finish(w,await r.json());}catch(x){w.textContent='Could not reach Tygar. Try again.';}
$('cs').disabled=false;};
async function sendFile(url,field,blob,name,label){addm('user',label);const w=addm('assistant','...');
const fd=new FormData();fd.append(field,blob,name);if(field=='photo'&&$('ci').value.trim()){fd.append('ask',$('ci').value.trim());$('ci').value='';}
try{const r=await fetch(url,{method:'POST',headers:{'X-Agent-Auth':AUTH},body:fd});await finish(w,await r.json());}catch(x){w.textContent='Could not send that. Try again.';}}
$('af').onchange=()=>{const f=$('af').files[0];if(f)sendFile('/agent/chat/audio','audio',f,f.name,'\U0001F3B5 '+f.name);$('af').value='';};
$('pf').onchange=()=>{const f=$('pf').files[0];if(!f)return;const u=URL.createObjectURL(f);sendFile('/agent/chat/photo','photo',f,f.name,'\U0001F4F7 Photo').then(()=>{});
const im=document.createElement('img');im.src=u;im.style.cssText='width:100%;border-radius:12px;margin-top:6px';[...document.querySelectorAll('.m.u')].pop().appendChild(im);$('pf').value='';};
let MR=null,CK=[];$('mic').onclick=async()=>{if(MR&&MR.state=='recording'){MR.stop();return;}
try{const st=await navigator.mediaDevices.getUserMedia({audio:true});MR=new MediaRecorder(st);CK=[];
MR.ondataavailable=e=>CK.push(e.data);MR.onstop=()=>{st.getTracks().forEach(t=>t.stop());$('mic').classList.remove('live');$('rec').textContent='';
const b=new Blob(CK,{type:MR.mimeType||'audio/webm'});const ext=(MR.mimeType||'').includes('mp4')?'m4a':'webm';sendFile('/agent/chat/audio','audio',b,'voice.'+ext,'\U0001F3A4 (voice note)');};
MR.start();$('mic').classList.add('live');$('rec').textContent='\u25CF Recording... tap the mic again to send';setTimeout(()=>{if(MR.state=='recording')MR.stop();},120000);}
catch(x){$('rec').textContent='Allow microphone access to talk to Tygar.';}};
/* to-do (stored on this device) */

if('serviceWorker' in navigator)navigator.serviceWorker.register('/agent/sw.js',{scope:'/agent'}).catch(()=>{});
let DP=null;const SA=matchMedia('(display-mode: standalone)').matches||document.referrer.startsWith('android-app://');if(!SA&&/Android/i.test(navigator.userAgent))$('inst').style.display='flex';window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();DP=e;if(!SA){$('inst').style.display='flex';$('ib2').style.display='inline-block';}});
$('ib2').onclick=async()=>{if(!DP)return;DP.prompt();await DP.userChoice;DP=null;$('inst').style.display='none';};
window.addEventListener('appinstalled',()=>{$('inst').style.display='none';});
let TD=JSON.parse(localStorage.getItem('ta_todo')||'[]');
function rtd(){localStorage.setItem('ta_todo',JSON.stringify(TD));$('tl').innerHTML='';TD.forEach((x,i)=>{const li=document.createElement('li');if(x.d)li.className='d';
li.innerHTML='<i>'+(x.d?'\u2705':'\u2B1C')+'</i><span></span><i title="Remind me">'+(x.r?'\u23F0':'\U0001F514')+'</i><i title="Delete">\U0001F5D1</i>';
li.querySelector('span').textContent=x.t+(x.r&&x.rt?(' \u00b7 '+new Date(x.rt).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})):'');
li.children[0].onclick=()=>{x.d=!x.d;rtd();};li.children[2].onclick=()=>pickRem(i);li.children[3].onclick=()=>{TD.splice(i,1);rtd();};$('tl').appendChild(li);});}
rtd();$('tf').onsubmit=e=>{e.preventDefault();const t=$('ti').value.trim();if(t){TD.push({t,d:false});$('ti').value='';rtd();}};
function ub64(s){const p='='.repeat((4-s.length%4)%4);const b=atob((s+p).replace(/-/g,'+').replace(/_/g,'/'));const a=new Uint8Array(b.length);for(let i=0;i<b.length;i++)a[i]=b.charCodeAt(i);return a;}
async function ensurePush(){if(!('serviceWorker'in navigator)||!('PushManager'in window)||!('Notification'in window))throw 'Reminders need Chrome on Android, or installing the app.';
const reg=await navigator.serviceWorker.ready;let sub=await reg.pushManager.getSubscription();
if(!sub){const perm=await Notification.requestPermission();if(perm!=='granted')throw 'Tap Allow so Tygar can remind you.';
const k=await (await fetch('/agent/push/key')).json();if(!k.key)throw 'Reminders are not ready yet, try again shortly.';
sub=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:ub64(k.key)});}
return sub.toJSON?sub.toJSON():sub;}
let REMI=-1;
function pickRem(i){REMI=i;const d=new Date(Date.now()+3600000-new Date().getTimezoneOffset()*60000);$('rt').value=d.toISOString().slice(0,16);
try{if($('rt').showPicker)$('rt').showPicker();else $('rt').click();}catch(e){$('rt').click();}}
$('rt').onchange=async()=>{const v=$('rt').value,i=REMI;if(i<0||!v)return;const due=Date.parse(v)/1000;
if(!due||due*1000<Date.now()){$('rmsg').textContent='Pick a future time.';return;}
$('rmsg').textContent='Setting reminder\u2026';
try{const sub=await ensurePush();
const r=await (await fetch('/agent/push/remind',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({sub,task:TD[i].t,due})})).json();
if(r.id){TD[i].r=r.id;TD[i].rt=v;rtd();$('rmsg').textContent='\u23F0 Reminder set. You\\'ll get a Chrome notification then, even if the app is closed.';}
else $('rmsg').textContent='Could not set that reminder.';
}catch(e){$('rmsg').textContent=(typeof e==='string'?e:'Could not set that reminder.');}};
/* shop finder */
let ME=null,MAP=null,YOU=null,SHOP=null,LINE=null,WID=null,TGT=null;
function km(a,b,c,d){const R=6371,r=Math.PI/180,x=Math.sin((c-a)*r/2)**2+Math.cos(a*r)*Math.cos(c*r)*Math.sin((d-b)*r/2)**2;return 2*R*Math.asin(Math.sqrt(x));}
function fmt(k){return k<1?Math.round(k*1000)+' m':k.toFixed(k<10?2:1)+' km';}
function locate(){return new Promise(ok=>{if(!navigator.geolocation)return ok(null);navigator.geolocation.getCurrentPosition(p=>{ME=[p.coords.latitude,p.coords.longitude];ok(ME);},()=>ok(null),{enableHighAccuracy:true,timeout:10000});});}
$('sf2').onsubmit=async e=>{e.preventDefault();const q=$('sq').value.trim();if(!q)return;$('sb').disabled=true;$('sres').innerHTML='<div class="res">Finding your location and the shop...</div>';
await locate();let u='/agent/places?q='+encodeURIComponent(q);if(ME)u+='&lat='+ME[0]+'&lon='+ME[1];
try{const d=await (await fetch(u,{headers:{'X-Agent-Auth':AUTH}})).json();$('sres').innerHTML='';
if(d.error||!d.results.length){$('sres').innerHTML='<div class="res">'+(d.error||'No match found. Try adding the town, e.g. "Shoprite Enugu".')+'</div>';}
else{if(!ME)$('sres').innerHTML='<div class="res"><small>Turn on location to see how far each one is.</small></div>';
d.results.forEach(r=>{const el=document.createElement('div');el.className='res';el.innerHTML='<b></b><small></small>';el.querySelector('b').textContent=r.name+(r.km!=null?' \u00B7 '+fmt(r.km):'');
el.querySelector('small').textContent=[r.kind,r.addr].filter(Boolean).join(' \u2014 ');el.onclick=()=>go(r);$('sres').appendChild(el);});go(d.results[0]);}}
catch(x){$('sres').innerHTML='<div class="res">Search is busy. Try again.</div>';}$('sb').disabled=false;};
function go(r){TGT=r;$('map').style.display='block';$('nav').style.display='block';
if(!MAP){MAP=L.map('map');L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'\u00A9 OpenStreetMap'}).addTo(MAP);}
if(SHOP)SHOP.remove();SHOP=L.marker([r.lat,r.lon]).addTo(MAP).bindPopup(r.name).openPopup();
$('dir').href='https://www.google.com/maps/dir/?api=1&destination='+r.lat+','+r.lon;$('dsub').textContent=r.name+(r.addr?' \u2014 '+r.addr:'');
if(ME){MAP.fitBounds([ME,[r.lat,r.lon]],{padding:[40,40]});}else MAP.setView([r.lat,r.lon],16);upd();
if(WID==null&&navigator.geolocation)WID=navigator.geolocation.watchPosition(p=>{ME=[p.coords.latitude,p.coords.longitude];upd();},()=>{},{enableHighAccuracy:true,maximumAge:3000});
setTimeout(()=>MAP.invalidateSize(),200);$('map').scrollIntoView({behavior:'smooth',block:'center'});}
function upd(){if(!TGT)return;if(!ME){$('dist').textContent='Location off';return;}
if(!YOU)YOU=L.marker(ME,{icon:L.divIcon({className:'',html:'<div class="you"></div>',iconSize:[18,18]})}).addTo(MAP);else YOU.setLatLng(ME);
if(LINE)LINE.remove();LINE=L.polyline([ME,[TGT.lat,TGT.lon]],{color:'#ff2e88',dashArray:'6 8',weight:4}).addTo(MAP);
const k=km(ME[0],ME[1],TGT.lat,TGT.lon);$('dist').textContent=k<0.03?'\U0001F389 You are there!':fmt(k)+' away';}
$('stop').onclick=e=>{e.preventDefault();if(WID!=null)navigator.geolocation.clearWatch(WID);WID=null;$('nav').style.display='none';};
function share(goal,ans){const t=('Tygar Agent: '+goal+'\\n\\n'+ans).slice(0,1500),u=location.origin+'/agent';
$('sx').href='https://x.com/intent/post?text='+encodeURIComponent(('Tygar Agent: '+goal+'\\n'+ans).slice(0,230))+'&url='+encodeURIComponent(u);
$('sw').href='https://wa.me/?text='+encodeURIComponent(t+'\\n\\n'+u);
$('sf').href='https://www.facebook.com/sharer/sharer.php?u='+encodeURIComponent(u);
$('cp').onclick=e=>{e.preventDefault();navigator.clipboard.writeText(t+'\\n\\n'+u);$('cp').textContent='Copied';};
$('share').style.display='flex';}
document.querySelectorAll('.chip:not(.cc)').forEach(c=>c.onclick=()=>{$('g').value=c.textContent;});
$('fm').onsubmit=async e=>{e.preventDefault();$('err').textContent='';$('go').disabled=true;
try{const r=await fetch('/agent/start',{method:'POST',headers:{'Content-Type':'application/json','X-Agent-Auth':AUTH},body:JSON.stringify({goal:$('g').value})});
const d=await r.json();if(d.error){$('err').textContent=d.error;$('go').disabled=false;return;}
T=d.token;n=-1;F=0;$('shot').style.display='none';$('run').style.display='block';$('run').classList.remove('done');$('ans').style.display='none';$('share').style.display='none';$('steps').innerHTML='';$('vtext').textContent='Opening the web...';$('vurl').textContent='';$('vtitle').textContent='';$('vlinks').textContent='';tick();}
catch(x){$('err').textContent='Could not start. Please try again.';$('go').disabled=false;}};
async function tick(){try{const s=await (await fetch('/live/'+T+'/state',{cache:'no-store'})).json();
if(s.gone){$('status').textContent='This task has ended.';$('go').disabled=false;return;}
$('goal').textContent=s.goal;$('status').textContent=s.status;
if(s.n!==n){n=s.n;$('steps').innerHTML=s.steps.map(x=>'<li>'+x.replace(/</g,'&lt;')+'</li>').join('');}
if(s.fn&&s.fn!==F){F=s.fn;$('shot').src='/live/'+T+'/frame.jpg?'+F;$('shot').style.display='block';}
if(s.view){$('vurl').textContent=s.view.url;$('vtitle').textContent=s.view.title;$('vtext').textContent=s.view.text;$('vlinks').textContent=(s.view.links||[]).map(x=>'\u2192 '+x).join('\\n');}
if(s.done){$('run').classList.add('done');$('ans').textContent=s.answer;$('ans').style.display='block';share(s.goal,s.answer);$('go').disabled=false;return;}}catch(e){}
setTimeout(tick,1500);}
</script><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script></body></html>"""
