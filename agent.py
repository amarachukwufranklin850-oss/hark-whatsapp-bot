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
    raise RuntimeError("search engines unavailable")


def _run_light(token, goal):
    live = LIVE[token]
    notes, answer, history = [], "", []
    try:
        url, title, text, links = _lsearch(goal)
        history.append(url)
        live["steps"].append(f"1. \U0001F50E Searching: {goal[:60]}")
        t0, y = time.time(), 0
        for step in range(2, MAX_STEPS + 2):
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


CHAT_DAILY = 30
CHAT_SYS = ("You are Tygar, a free, friendly, fan-made AI chat assistant for people across Africa, made by Unofficial Hark Fan Africa "
            "(not affiliated with Hark). Reply in the user's language, including Naija Pidgin. Keep answers short and clear. "
            "You can't browse, log in, buy, post or message anyone from this chat; for web research tell them to use the Agent box above. "
            "Never ask for passwords, OTPs, card or bank details.")


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
    out = _llm([{"role": "system", "content": CHAT_SYS}] + msgs, json_mode=False, limit=40)
    out = re.sub(r"<think>.*?</think>", "", out or "", flags=re.S).strip()
    return {"reply": out or "Sorry, I'm busy right now. Try again in a minute."}


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
            "answer": v.get("answer", "") if v["done"] else "", "phase": v.get("phase", ""), "llm_s": v.get("llm_s"), "view": v.get("view"), "engine": _ENGINE_USED[0],
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
.screen{border:1px solid #2a2d36;border-radius:12px;overflow:hidden;background:#fff;color:#222}
#view{padding:12px;min-height:220px;max-height:360px;overflow:hidden;font-size:13px;line-height:1.45}#vurl{color:#1a7f37;font-size:12px;word-break:break-all}
#vtitle{font-size:16px;font-weight:700;margin:4px 0 6px;color:#1a0dab}#vtext{white-space:pre-wrap;color:#333}#vlinks{margin-top:8px;color:#1a0dab;font-size:12px}
.steps{background:#141720;border:1px solid #2a2d36;border-radius:12px;padding:10px 12px;font-size:14px;line-height:1.6}
.steps ol{margin:0;padding:0;list-style:none}#status{color:#f5b301;margin-top:6px}
#ans{display:none;margin-top:14px;background:#141720;border:1px solid #2f6;border-radius:12px;padding:14px;white-space:pre-wrap;line-height:1.5}
footer{font-size:11px;color:#777;margin-top:22px}
.top{display:flex;align-items:center;justify-content:space-between}#acct{font-size:13px;color:#9aa}#acct a{color:#f5b301}
#login{display:none;background:#141720;border:1px solid #2a2d36;border-radius:12px;padding:12px;margin-bottom:12px}
#login input{width:100%;margin-bottom:8px}#login button{padding:11px 16px}#lmsg{font-size:13px;color:#ff7b7b;margin-top:6px}
#share{display:none;margin-top:10px;gap:8px;align-items:center;flex-wrap:wrap;font-size:14px}
#chat{margin-top:22px;background:#141720;border:1px solid #2a2d36;border-radius:12px;padding:12px}#chat h2{font-size:16px;margin:0 0 8px}
#msgs{max-height:340px;overflow-y:auto;font-size:15px;line-height:1.5;display:flex;flex-direction:column}.m{margin:6px 0;padding:8px 11px;border-radius:12px;white-space:pre-wrap;max-width:88%}
.u{background:#f5b301;color:#111;align-self:flex-end}.a{background:#1d2130;align-self:flex-start}#cf{margin-top:8px}
#share a{padding:7px 12px;border:1px solid #2a2d36;border-radius:20px;color:#cfd;text-decoration:none}</style></head><body><div class="wrap">
<div class="top"><h1>\U0001F42F Tygar Agent</h1><span id="acct"><a href="#" id="si">Sign in</a></span></div>
<p class="sub">Give it a task. Watch it search and read the web live, then get the answer. 3 free tasks a day, 5 when you sign in.</p>
<div id="login"><input id="em" type="email" placeholder="Your email" autocomplete="email"><button id="sc" type="button">Send code</button>
<div id="cbox" style="display:none;margin-top:8px"><input id="cd" inputmode="numeric" maxlength="6" placeholder="6-digit code"><button id="vc" type="button">Sign in</button></div>
<div id="lmsg"></div></div>
<form id="fm"><input id="g" placeholder="e.g. cheapest iPhone 13 on Jumia Nigeria" maxlength="200" autocomplete="off"><button id="go">Start</button></form>
<div class="chips"><span class="chip">Compare MTN and Airtel 10GB data plans</span><span class="chip">Latest Super Eagles news</span><span class="chip">Best laptops under 400k naira</span></div>
<div id="err"></div>
<div id="run"><div class="bar"><span class="dot"></span><b id="goal"></b></div>
<div class="grid"><div class="screen"><div id="view"><div id="vurl"></div><div id="vtitle"></div><div id="vtext">Opening the web...</div><div id="vlinks"></div></div></div>
<div class="steps"><ol id="steps"></ol><div id="status">Starting...</div></div></div>
<div id="ans"></div>
<div id="share"><span>Share:</span><a id="sx" target="_blank" rel="noopener">X</a><a id="sw" target="_blank" rel="noopener">WhatsApp</a><a id="sf" target="_blank" rel="noopener">Facebook</a><a href="#" id="cp">Copy</a></div></div>
<div id="chat"><h2>\U0001F4AC Chat with Tygar</h2><div id="msgs"></div>
<form id="cf"><input id="ci" placeholder="Ask Tygar anything..." maxlength="1500" autocomplete="off"><button id="cs">Send</button></form></div>
<footer>Look-only agent: it reads public pages and never logs in, posts, buys or reads email. Sign-in uses your email only to send a code; it isn't stored on our server. Sharing opens your own X, WhatsApp or Facebook app, so you post it yourself. Fan-made by Unofficial Hark Fan Africa, not affiliated with Hark.</footer></div>
<script>
let T=null,n=-1;const $=id=>document.getElementById(id);
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
let CH=[];
function addm(r,t){const e=document.createElement('div');e.className='m '+(r=='user'?'u':'a');e.textContent=t;$('msgs').appendChild(e);$('msgs').scrollTop=1e9;return e;}
$('cf').onsubmit=async e=>{e.preventDefault();const t=$('ci').value.trim();if(!t)return;$('ci').value='';CH.push({role:'user',content:t});addm('user',t);
const w=addm('assistant','...');$('cs').disabled=true;
try{const r=await fetch('/agent/chat',{method:'POST',headers:{'Content-Type':'application/json','X-Agent-Auth':AUTH},body:JSON.stringify({messages:CH.slice(-10)})});
const d=await r.json();w.textContent=d.reply||d.error;if(d.reply)CH.push({role:'assistant',content:d.reply});}catch(x){w.textContent='Could not reach Tygar. Try again.';}
$('cs').disabled=false;};
function share(goal,ans){const t=('Tygar Agent: '+goal+'\\n\\n'+ans).slice(0,1500),u=location.origin+'/agent';
$('sx').href='https://x.com/intent/post?text='+encodeURIComponent(('Tygar Agent: '+goal+'\\n'+ans).slice(0,230))+'&url='+encodeURIComponent(u);
$('sw').href='https://wa.me/?text='+encodeURIComponent(t+'\\n\\n'+u);
$('sf').href='https://www.facebook.com/sharer/sharer.php?u='+encodeURIComponent(u);
$('cp').onclick=e=>{e.preventDefault();navigator.clipboard.writeText(t+'\\n\\n'+u);$('cp').textContent='Copied';};
$('share').style.display='flex';}
document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{$('g').value=c.textContent;});
$('fm').onsubmit=async e=>{e.preventDefault();$('err').textContent='';$('go').disabled=true;
try{const r=await fetch('/agent/start',{method:'POST',headers:{'Content-Type':'application/json','X-Agent-Auth':AUTH},body:JSON.stringify({goal:$('g').value})});
const d=await r.json();if(d.error){$('err').textContent=d.error;$('go').disabled=false;return;}
T=d.token;n=-1;$('run').style.display='block';$('run').classList.remove('done');$('ans').style.display='none';$('share').style.display='none';$('steps').innerHTML='';$('vtext').textContent='Opening the web...';$('vurl').textContent='';$('vtitle').textContent='';$('vlinks').textContent='';tick();}
catch(x){$('err').textContent='Could not start. Please try again.';$('go').disabled=false;}};
async function tick(){try{const s=await (await fetch('/live/'+T+'/state',{cache:'no-store'})).json();
if(s.gone){$('status').textContent='This task has ended.';$('go').disabled=false;return;}
$('goal').textContent=s.goal;$('status').textContent=s.status;
if(s.n!==n){n=s.n;$('steps').innerHTML=s.steps.map(x=>'<li>'+x.replace(/</g,'&lt;')+'</li>').join('');}
if(s.view){$('vurl').textContent=s.view.url;$('vtitle').textContent=s.view.title;$('vtext').textContent=s.view.text;$('vlinks').textContent=(s.view.links||[]).map(x=>'\u2192 '+x).join('\\n');}
if(s.done){$('run').classList.add('done');$('ans').textContent=s.answer;$('ans').style.display='block';share(s.goal,s.answer);$('go').disabled=false;return;}}catch(e){}
setTimeout(tick,1500);}
</script></body></html>"""
