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


LOGO_B64 = "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"
FAV_B64 = "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"


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
    return {"reply": j["reply"], "image": j.get("image")}


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
<title>Tygar Agent</title><link rel="icon" href="/agent/favicon.png"><meta property="og:image" content="/agent/logo.png"><style>
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;min-height:100vh;color:#1d1240;font-family:"Segoe UI",system-ui,sans-serif;font-size:18px;line-height:1.5;
background:linear-gradient(135deg,#fff3c4,#ffd6e8 35%,#d9e8ff 70%,#d6fff1);background-size:300% 300%;animation:bg 18s ease infinite}
@keyframes bg{0%,100%{background-position:0 50%}50%{background-position:100% 50%}}
body::before{content:"";position:fixed;inset:0;background:url(/agent/logo.png) center 55%/min(80vw,620px) no-repeat;opacity:.07;pointer-events:none;z-index:0}
.wrap{position:relative;z-index:1;max-width:960px;margin:0 auto;padding:18px}
.top{display:flex;align-items:center;justify-content:space-between;gap:10px}
h1{font-size:34px;margin:6px 0;font-weight:900;display:flex;align-items:center}
.logo{width:64px;height:64px;border-radius:50%;border:3px solid #fff;box-shadow:0 0 0 4px #ff3d7f,0 8px 22px #ff3d7f66;margin-right:12px;animation:pop 3s ease-in-out infinite}
@keyframes pop{50%{transform:scale(1.06) rotate(-3deg)}}
.logo.sm{width:44px;height:44px;margin-right:10px;box-shadow:0 0 0 3px #7b2ff7;animation:none}
.grad{background:linear-gradient(90deg,#ff3d7f,#ff8a00,#7b2ff7,#00a8e8);background-size:200%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:sh 6s linear infinite}
@keyframes sh{to{background-position:200% 0}}
#acct{font-size:16px;font-weight:700}#acct a{color:#fff;background:#7b2ff7;padding:8px 16px;border-radius:30px;text-decoration:none;box-shadow:0 4px 12px #7b2ff755}
.hero{background:linear-gradient(120deg,#ff3d7f,#ff8a00);color:#fff;border-radius:22px;padding:22px;margin:14px 0 18px;box-shadow:0 12px 30px #ff3d7f44}
.hero h2{font-size:28px;margin:0 0 6px;font-weight:900}.hero p{margin:0;font-size:18px;opacity:.95}
.badges{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}.badges span{background:#ffffff33;border:1px solid #ffffff88;padding:5px 12px;border-radius:20px;font-size:15px;font-weight:700}
.feats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:0 0 20px}@media(max-width:760px){.feats{grid-template-columns:repeat(2,1fr)}}
.feat{background:#fff;border-radius:18px;padding:14px;text-align:center;box-shadow:0 6px 18px #7b2ff722;border-top:6px solid var(--c);transition:transform .2s}
.feat:hover{transform:translateY(-4px)}.feat b{display:block;font-size:19px;margin-top:4px}.feat small{color:#5a4f7a;font-size:15px}.feat .ic{font-size:34px}
.card{background:#ffffffee;border-radius:22px;padding:18px;box-shadow:0 10px 28px #7b2ff722;margin-bottom:20px}
.card h2{font-size:26px;margin:0 0 4px;display:flex;align-items:center;font-weight:900}
.sub{color:#5a4f7a;font-size:17px;margin:0 0 14px}
form{display:flex;gap:10px}input{flex:1;padding:16px;border-radius:14px;border:3px solid #c9b6ff;background:#fff;color:#1d1240;font-size:18px;outline:none}
input:focus{border-color:#7b2ff7;box-shadow:0 0 0 4px #7b2ff733}
button{padding:0 24px;border:0;border-radius:14px;background:linear-gradient(90deg,#ff3d7f,#ff8a00);color:#fff;font-weight:900;font-size:18px;box-shadow:0 6px 16px #ff3d7f55;cursor:pointer;transition:transform .15s}
button:hover{transform:scale(1.04)}button:disabled{opacity:.5}
.chips{margin:12px 0 4px;display:flex;flex-wrap:wrap;gap:8px}
.chip{font-size:15px;padding:8px 14px;border-radius:30px;color:#1d1240;cursor:pointer;background:#fff;border:2px solid #ddd;font-weight:600}
.chip:nth-child(4n+1){background:#e0f6ff;border-color:#00a8e8}.chip:nth-child(4n+2){background:#e3ffef;border-color:#19c37d}
.chip:nth-child(4n+3){background:#fff6d6;border-color:#ffb800}.chip:nth-child(4n+4){background:#ffe3ee;border-color:#ff3d7f}
#err{color:#d6004c;font-size:16px;margin-top:8px;font-weight:700}
#run{display:none;margin-top:18px}.bar{display:flex;align-items:center;gap:10px;font-size:18px}
.dot{width:12px;height:12px;border-radius:50%;background:#ff3d7f;animation:b 1s infinite}.done .dot{background:#19c37d;animation:none}
@keyframes b{50%{opacity:.3}}
.grid{display:grid;grid-template-columns:1.6fr 1fr;gap:14px;margin-top:12px}@media(max-width:760px){.grid{grid-template-columns:1fr}}
.screen{border:3px solid #7b2ff7;border-radius:16px;overflow:hidden;background:#fff;color:#222}
#view{padding:14px;min-height:240px;max-height:380px;overflow:hidden;font-size:15px;line-height:1.5}#vurl{color:#1a7f37;font-size:13px;word-break:break-all}
#vtitle{font-size:19px;font-weight:800;margin:4px 0 6px;color:#1a0dab}#vtext{white-space:pre-wrap;color:#333}#vlinks{margin-top:8px;color:#1a0dab;font-size:14px}
.steps{background:#f4efff;border:3px solid #c9b6ff;border-radius:16px;padding:12px 14px;font-size:16px;line-height:1.7}
.steps ol{margin:0;padding:0;list-style:none}#status{color:#d6004c;margin-top:6px;font-weight:800}
#ans{display:none;margin-top:16px;background:#eafff4;border:3px solid #19c37d;border-radius:16px;padding:16px;white-space:pre-wrap;line-height:1.6;font-size:17px}
#login{display:none;background:#fff;border:3px solid #c9b6ff;border-radius:16px;padding:14px;margin-bottom:14px}
#login input{width:100%;margin-bottom:10px}#login button{padding:13px 20px}#lmsg{font-size:15px;color:#d6004c;margin-top:6px}
#share{display:none;margin-top:12px;gap:8px;align-items:center;flex-wrap:wrap;font-size:16px;font-weight:700}
#share a{padding:8px 16px;border-radius:30px;color:#fff;text-decoration:none;font-weight:800}
#sx{background:#111}#sw{background:#25d366}#sf{background:#1877f2}#cp{background:#7b2ff7}
#chat{border:3px solid #ff3d7f}#chat .sub{margin:4px 0 10px}
#msgs{max-height:420px;overflow-y:auto;font-size:17px;line-height:1.55;display:flex;flex-direction:column;background:#faf7ff;border-radius:14px;padding:10px;margin-top:8px}
.m{margin:6px 0;padding:10px 14px;border-radius:18px;white-space:pre-wrap;max-width:88%}
.u{background:linear-gradient(90deg,#ff3d7f,#ff8a00);color:#fff;align-self:flex-end;border-bottom-right-radius:4px}
.a{background:#fff;border:2px solid #e6dcff;align-self:flex-start;border-bottom-left-radius:4px}#cf{margin-top:10px}
.tg{display:inline-block;margin-top:12px;background:#229ed9;color:#fff;padding:10px 18px;border-radius:30px;font-weight:800;text-decoration:none;font-size:16px}
footer{font-size:14px;color:#5a4f7a;margin:24px 0 10px;text-align:center}
</style></head><body><div class="wrap">
<div class="top"><h1><img class="logo" src="/agent/logo.png" alt=""><span class="grad">Tygar Agent</span></h1><span id="acct"><a href="#" id="si">Sign in</a></span></div>
<div class="hero"><h2>Your free AI helper for Africa \U0001F30D</h2><p>Ask anything, research the web live, convert bet codes and make pictures. No app needed.</p>
<div class="badges"><span>\u26A1 100% free</span><span>\U0001F1F3\U0001F1EC English + Pidgin</span><span>\U0001F512 No passwords</span></div>
<a class="tg" href="https://t.me/Harkaiafricabot" target="_blank" rel="noopener">\u2708 Also on Telegram</a></div>
<div class="feats"><a href="#agentcard" class="feat" style="--c:#00a8e8;text-decoration:none;color:inherit"><div class="ic">\U0001F50E</div><b>Live Agent</b><small>Watch it search</small></a>
<a href="#chat" class="feat" style="--c:#ff3d7f;text-decoration:none;color:inherit"><div class="ic">\U0001F4AC</div><b>Chat</b><small>Ask anything</small></a>
<a href="#chat" class="feat" style="--c:#19c37d;text-decoration:none;color:inherit"><div class="ic">\U0001F3AB</div><b>Bet codes</b><small>Convert in seconds</small></a>
<a href="#chat" class="feat" style="--c:#ffb800;text-decoration:none;color:inherit"><div class="ic">\U0001F3A8</div><b>Images</b><small>Type and get art</small></a></div>
<div class="card" id="agentcard"><h2>\U0001F50E Live web Agent</h2>
<p class="sub">Give it a task, watch it read the web live, then get the answer. 3 free tasks a day, 5 when you sign in.</p>
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
<div id="share"><span>Share:</span><a id="sx" target="_blank" rel="noopener">X</a><a id="sw" target="_blank" rel="noopener">WhatsApp</a><a id="sf" target="_blank" rel="noopener">Facebook</a><a href="#" id="cp">Copy</a></div></div></div>
<div id="chat" class="card"><h2><img class="logo sm" src="/agent/logo.png" alt=""> Chat with Tygar</h2>
<p class="sub">Your free AI friend. Just type: ask anything, convert bet codes, make images, paste a link to summarise. English or Pidgin.</p>
<div class="chips"><span class="chip cc">What can you do?</span><span class="chip cc">convert VESTC6 from SportyBet to MSport GH</span><span class="chip cc">draw a lion in a Super Eagles jersey</span><span class="chip cc">Wetin dey happen for Naija today?</span></div>
<div id="msgs"><div class="m a">Hi! I'm Tygar \U0001F44B Ask me anything, or type "what can you do?"</div></div>
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
let CH=[];document.querySelectorAll('.cc').forEach(c=>c.onclick=()=>{$('ci').value=c.textContent;$('ci').focus();});
function addm(r,t){const e=document.createElement('div');e.className='m '+(r=='user'?'u':'a');e.textContent=t;$('msgs').appendChild(e);$('msgs').scrollTop=1e9;return e;}
$('cf').onsubmit=async e=>{e.preventDefault();const t=$('ci').value.trim();if(!t)return;$('ci').value='';CH.push({role:'user',content:t});addm('user',t);
const w=addm('assistant','...');$('cs').disabled=true;
try{const r=await fetch('/agent/chat',{method:'POST',headers:{'Content-Type':'application/json','X-Agent-Auth':AUTH},body:JSON.stringify({messages:CH.slice(-10)})});
let d=await r.json();
const jid=d.job;while(jid&&(d.job||d.pending)){await new Promise(z=>setTimeout(z,1500));d=await (await fetch('/agent/chat/'+jid)).json();}
w.textContent=d.reply||d.error;if(d.image){const im=document.createElement('img');im.src=d.image;im.style.cssText='width:100%;border-radius:10px;margin-top:6px';w.appendChild(im);}if(d.reply)CH.push({role:'assistant',content:d.reply});}catch(x){w.textContent='Could not reach Tygar. Try again.';}
$('cs').disabled=false;};
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
