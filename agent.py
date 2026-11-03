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
TIME_LIMIT = 240
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
Prefer reliable sites. Finish with done as soon as your notes answer the goal (usually 3-8 steps)."""


def init(**helpers):
    H.update(helpers)


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
    text = pg.evaluate("document.body ? document.body.innerText : ''")[:3500]
    links = pg.evaluate("""() => {
        const seen = new Set(); const out = [];
        for (const a of document.querySelectorAll('a[href^="http"]')) {
            const r = a.getBoundingClientRect();
            const t = (a.innerText || a.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').slice(0, 70);
            if (t.length < 3 || seen.has(a.href) || r.width < 2 || r.height < 2) continue;
            seen.add(a.href); out.push([t, a.href]);
            if (out.length >= 30) break;
        }
        return out; }""")
    links = [(t, h) for t, h in links if not BLOCK_WORDS.search(h)]
    return title, text, links


def _ok_url(u):
    if not u or BLOCK_WORDS.search(u):
        return None
    return H["safe_url"](u)


ENGINES = ["https://www.bing.com/search?setlang=en&cc=NG&q=", "https://www.mojeek.com/search?q="]


def _search(pg, q):
    """Open a results page; fall back to the next engine when one blocks the browser."""
    for base in ENGINES:
        try:
            pg.goto(base + quote_plus(q), wait_until="domcontentloaded", timeout=25000)
            pg.wait_for_timeout(1200)
            host = urlparse(base).hostname
            n = pg.evaluate("h => [...document.querySelectorAll('a[href^=\"http\"]')].filter(a => !a.href.includes(h) || a.href.includes('/ck/a')).length", host)
            if n >= 5:
                return True
            print("agent search: few results on", host, n)
        except Exception as e:
            print("agent search error:", base, str(e)[:150])
    return False


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


def web_start(goal, who):
    goal = (goal or "").strip()[:200]
    if len(goal) < 4:
        return {"error": "Tell me what to find, e.g. cheapest iPhone 13 on Jumia Nigeria."}
    if BAD_GOAL.search(goal):
        return {"error": "I'm a look-only agent: I search, read and compare public pages. I never log in, sign up, read email, post, buy or message anyone."}
    key = ("web:" + who, _day())
    if _uses.get(key, 0) >= DAILY:
        return {"error": f"You've used today's {DAILY} free tasks. Come back tomorrow!"}
    _uses[key] = _uses.get(key, 0) + 1
    token = secrets.token_urlsafe(9)
    LIVE[token] = {"frame": b"", "steps": [], "status": "\u23F3 Starting the browser...", "done": False, "goal": goal,
                   "ts": time.time(), "url": H["base_url"] + "/live/" + token, "answer": ""}
    threading.Thread(target=_run, args=(None, None, token, goal), daemon=True).start()
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
                ctx = b.new_context(viewport={"width": 1100, "height": 720}, device_scale_factor=1,
                                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 TygarsBot")
                ctx.route("**/*", lambda r: r.abort() if r.request.resource_type in ("media", "websocket") else r.continue_())
                pg = ctx.new_page()
                pg.set_default_timeout(20000)
                pg.set_default_navigation_timeout(30000)
                _search(pg, goal)
                live["steps"].append(f"1. \U0001F50E Searching: {goal[:60]}")
                t0 = time.time()
                for step in range(2, MAX_STEPS + 2):
                    pg.wait_for_timeout(1200)
                    frame = pg.screenshot(type="jpeg", quality=55)
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
                            {"role": "user", "content": f"Goal: {goal}\nStep {step} of {MAX_STEPS + 1}.\nNotes so far:\n" + ("\n".join(notes) or "(none)") +
                             f"\n\nURL: {pg.url}\nTitle: {title}\nText:\n{text}\n\nLinks:\n{numbered}"}]
                    print("agent step", step, pg.url[:80], len(links), "links")
                    try:
                        d = _json(H["groq"](msgs, tools=False).get("content")) or {}
                    except Exception as e:
                        print("agent llm error:", e)
                        d = {}
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
                            pg.mouse.wheel(0, 650)
                        elif act == "back":
                            live["steps"].append(f"{step}. \u21A9 {say}")
                            pg.go_back(wait_until="domcontentloaded", timeout=20000)
                        else:
                            live["steps"].append(f"{step}. \u2705 {say}")
                            break
                    except Exception as e:
                        live["steps"].append(f"{step}. \u26A0 That page didn't load, trying another way")
                        print("agent step error:", str(e)[:200])
                ctx.close()
            finally:
                b.close()
        live["status"] = "\U0001F9E0 Writing up what I found..."
        _update_tg(chat, mid, live, None)
        msgs = [{"role": "system", "content": "Answer the user's goal using ONLY these research notes. Be concrete: names, prices, numbers. "
                                              "Plain text, short, no markdown tables. Say if something couldn't be confirmed. End with the main source links."},
                {"role": "user", "content": f"Goal: {goal}\n\nNotes:\n" + ("\n".join(notes) or "(no useful notes)")}]
        try:
            answer = re.sub(r"<think>.*?</think>", "", H["groq"](msgs, tools=False).get("content") or "", flags=re.S).strip()
        except Exception as e:
            print("agent final error:", e)
        live["status"] = "\u2705 Done"
    except Exception as e:
        print("agent error:", str(e)[:300])
        live["status"] = "\u26A0 Sorry, the agent hit a problem. Please try again later."
    finally:
        live["done"] = True
        lock.release()
    live["answer"] = answer or "Sorry, I couldn't find a clear answer this time. Try rewording the task."
    _update_tg(chat, mid, live, None)
    if chat:
        H["send"](chat, ("\U0001F916 " + live["answer"])[:4000])


def _cleanup():
    now = time.time()
    for k in [k for k, v in LIVE.items() if now - v["ts"] > 3600]:
        LIVE.pop(k, None)


@bp.get("/agent")
def agent_home():
    return Response(APP_HTML, mimetype="text/html")


@bp.post("/agent/start")
def agent_start():
    import hashlib
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
    who = hashlib.sha256(ip.encode()).hexdigest()[:16]
    d = request.get_json(silent=True) or {}
    out = web_start(d.get("goal"), who)
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
    if not v["done"] and time.time() - v["ts"] > 480:
        v["status"] = "\u26A0 This task took too long and was stopped."
    return {"goal": v["goal"], "steps": v["steps"], "status": v["status"], "done": v["done"], "n": len(v["steps"]),
            "answer": v.get("answer", "") if v["done"] else "", "phase": v.get("phase", "")}


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
<div class="screen"><img id="f" alt="Waiting for the first screen..."></div>
<div id="status">Starting...</div><ol id="steps"></ol>
<footer>Look-only agent: it reads public pages and never logs in, posts or buys. Fan-made, not affiliated with Hark.</footer>
<script>
const T="__T__";let n=-1;
async function tick(){try{const s=await (await fetch('/live/'+T+'/state',{cache:'no-store'})).json();
if(s.gone){document.getElementById('status').textContent='This live view has ended.';return;}
document.getElementById('goal').textContent=s.goal;document.getElementById('status').textContent=s.status;
if(s.n!==n){n=s.n;document.getElementById('steps').innerHTML=s.steps.map(x=>'<li>'+x.replace(/</g,'&lt;')+'</li>').join('');}
document.getElementById('f').src='/live/'+T+'/frame.jpg?'+Date.now();
if(s.done){document.body.classList.add('done');document.getElementById('live').textContent='DONE';return;}}catch(e){}
setTimeout(tick,1500);}tick();</script></body></html>"""


APP_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tygar Agent</title><style>
*{box-sizing:border-box}body{margin:0;background:#0d0f14;color:#e8e8ea;font-family:system-ui,sans-serif}
.wrap{max-width:920px;margin:0 auto;padding:16px}
h1{font-size:22px;margin:6px 0 2px}.sub{color:#9aa;font-size:14px;margin:0 0 14px}
form{display:flex;gap:8px}input{flex:1;padding:13px;border-radius:10px;border:1px solid #2a2d36;background:#161922;color:#fff;font-size:16px}
button{padding:0 18px;border:0;border-radius:10px;background:#f5b301;color:#111;font-weight:700;font-size:15px}
button:disabled{opacity:.5}.chips{margin:10px 0 4px;display:flex;flex-wrap:wrap;gap:6px}
.chip{font-size:13px;padding:6px 10px;border:1px solid #2a2d36;border-radius:20px;color:#cfd;cursor:pointer}
#err{color:#ff7b7b;font-size:14px;margin-top:8px}
#run{display:none;margin-top:16px}.bar{display:flex;align-items:center;gap:8px;font-size:14px}
.dot{width:9px;height:9px;border-radius:50%;background:#e33;animation:b 1s infinite}.done .dot{background:#2c2;animation:none}
@keyframes b{50%{opacity:.3}}
.grid{display:grid;grid-template-columns:1.6fr 1fr;gap:12px;margin-top:10px}@media(max-width:700px){.grid{grid-template-columns:1fr}}
.screen{border:1px solid #2a2d36;border-radius:12px;overflow:hidden;background:#000}.screen img{width:100%;display:block;min-height:200px}
.steps{background:#141720;border:1px solid #2a2d36;border-radius:12px;padding:10px 12px;font-size:14px;line-height:1.6}
.steps ol{margin:0;padding:0;list-style:none}#status{color:#f5b301;margin-top:6px}
#ans{display:none;margin-top:14px;background:#141720;border:1px solid #2f6;border-radius:12px;padding:14px;white-space:pre-wrap;line-height:1.5}
footer{font-size:11px;color:#777;margin-top:22px}</style></head><body><div class="wrap">
<h1>\U0001F42F Tygar Agent</h1><p class="sub">Give it a task. Watch it search and read the web live, then get the answer. 3 free tasks a day.</p>
<form id="fm"><input id="g" placeholder="e.g. cheapest iPhone 13 on Jumia Nigeria" maxlength="200" autocomplete="off"><button id="go">Start</button></form>
<div class="chips"><span class="chip">Compare MTN and Airtel 10GB data plans</span><span class="chip">Latest Super Eagles news</span><span class="chip">Best laptops under 400k naira</span></div>
<div id="err"></div>
<div id="run"><div class="bar"><span class="dot"></span><b id="goal"></b></div>
<div class="grid"><div class="screen"><img id="f" alt="Waiting for the first screen..."></div>
<div class="steps"><ol id="steps"></ol><div id="status">Starting...</div></div></div>
<div id="ans"></div></div>
<footer>Look-only agent: it reads public pages and never logs in, posts, buys or reads email. Fan-made by Unofficial Hark Fan Africa, not affiliated with Hark.</footer></div>
<script>
let T=null,n=-1;const $=id=>document.getElementById(id);
document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{$('g').value=c.textContent;});
$('fm').onsubmit=async e=>{e.preventDefault();$('err').textContent='';$('go').disabled=true;
try{const r=await fetch('/agent/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({goal:$('g').value})});
const d=await r.json();if(d.error){$('err').textContent=d.error;$('go').disabled=false;return;}
T=d.token;n=-1;$('run').style.display='block';$('run').classList.remove('done');$('ans').style.display='none';$('steps').innerHTML='';$('f').removeAttribute('src');tick();}
catch(x){$('err').textContent='Could not start. Please try again.';$('go').disabled=false;}};
async function tick(){try{const s=await (await fetch('/live/'+T+'/state',{cache:'no-store'})).json();
if(s.gone){$('status').textContent='This task has ended.';$('go').disabled=false;return;}
$('goal').textContent=s.goal;$('status').textContent=s.status;
if(s.n!==n){n=s.n;$('steps').innerHTML=s.steps.map(x=>'<li>'+x.replace(/</g,'&lt;')+'</li>').join('');}
$('f').src='/live/'+T+'/frame.jpg?'+Date.now();
if(s.done){$('run').classList.add('done');$('ans').textContent=s.answer;$('ans').style.display='block';$('go').disabled=false;return;}}catch(e){}
setTimeout(tick,1500);}
</script></body></html>"""
