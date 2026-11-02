import os
import json
import time
import html
import re
import base64
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import threading
import requests
from xml.sax.saxutils import escape
from flask import Flask, request, Response

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")        # any secret word you make up
WA_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")          # Meta access token
PHONE_ID = os.environ.get("PHONE_NUMBER_ID", "")         # from WhatsApp > API Setup
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")            # free key from Groq
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GRAPH = os.environ.get("GRAPH_VERSION", "v23.0")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")      # from @BotFather
TG_SECRET = os.environ.get("TELEGRAM_SECRET", "")        # optional webhook secret

SYSTEM = (
    "You are Tygars Bot, a friendly AI assistant made by the Unofficial Hark Fan Africa "
    "community. Help with any question people ask: school work, tech, coding, "
    "business, writing, health basics, everyday advice. When asked about Hark: it is a "
    "personal AI that gets real things done, works across email, calendar and files, uses a "
    "real browser to book, order and fill forms, remembers what matters to you, builds pages, "
    "and runs on Android, iPhone, web and Mac; the Hark Pro plan is free with no ads; point "
    "people to hark.com for details. "
    "Write the way Hark does: answer first, short sentences, contractions, warm and precise, "
    "one clear pick when asked to choose, no filler like 'Great question'. Keep replies short "
    "enough for a chat. Use the web_search tool for anything current or that you are unsure of "
    "(news, prices, scores, dates). Only give verified information: state a current fact only "
    "if the search results support it, prefer trusted outlets (BBC, Reuters, AP, ESPN, Punch, "
    "Channels, official sites), and name the source and date, e.g. '(BBC, 1 Nov 2026)'. If "
    "sources disagree, or none confirm it, say you couldn't verify it instead of guessing. Never "
    "invent scores, quotes, numbers or links. "
    "For medical, legal or money emergencies, advise a professional. "
    "If asked who you are, say you're Tygars Bot. Never claim to be Hark itself or to be "
    "made by Hark; you're fan-made and not affiliated with Hark."
)

TG_CREATOR = (" If asked who made or created you, say you were made by Amarachukwu Franklin from "
              "Enugu State, a student at the State University of Medical and Applied Sciences (SUMAS), Igbo-Eno.")

history = {}   # last few messages per user (resets when the server restarts)
seen = set()   # message ids already answered, so Meta retries are ignored


@app.get("/")
def home():
    return "Bot is running", 200


@app.get("/webhook")
def verify():
    if (request.args.get("hub.mode") == "subscribe"
            and request.args.get("hub.verify_token") == VERIFY_TOKEN):
        return request.args.get("hub.challenge", ""), 200
    return "Forbidden", 403


@app.post("/webhook")
def incoming():
    data = request.get_json(silent=True) or {}
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                if msg.get("id") in seen:
                    continue
                seen.add(msg.get("id"))
                user = msg.get("from")
                if msg.get("type") != "text":
                    send(user, "I can only read text messages for now.")
                    continue
                send(user, ask_ai(user, msg["text"]["body"]))
    return "ok", 200


@app.post("/twilio")
def twilio_incoming():
    # Twilio WhatsApp sandbox: form fields Body and From; reply with TwiML
    user = request.form.get("From", "")
    body = (request.form.get("Body") or "").strip()
    if not body:
        reply = "I can only read text messages for now."
    else:
        reply = ask_ai(user, body)
    xml = ('<?xml version="1.0" encoding="UTF-8"?><Response><Message>'
           + escape(reply[:1500]) + '</Message></Response>')
    return Response(xml, mimetype="text/xml")


VONAGE_KEY = os.environ.get("VONAGE_API_KEY", "")
VONAGE_SECRET = os.environ.get("VONAGE_API_SECRET", "")
VONAGE_FROM = os.environ.get("VONAGE_FROM", "14157386102")   # Vonage sandbox number
VONAGE_URL = os.environ.get("VONAGE_URL", "https://messages-sandbox.nexmo.com/v1/messages")


@app.post("/vonage")
def vonage_incoming():
    # Vonage Messages API sandbox inbound webhook (JSON)
    data = request.get_json(silent=True) or {}
    mid = data.get("message_uuid")
    if mid and mid in seen:
        return "ok", 200
    if mid:
        seen.add(mid)
    user = data.get("from")
    if data.get("channel") == "whatsapp" and user:
        text = data.get("text") if data.get("message_type") == "text" else None
        threading.Thread(target=vonage_reply, args=(user, text), daemon=True).start()
    return "ok", 200


@app.post("/vonage-status")
def vonage_status():
    return "ok", 200


def vonage_reply(user, text):
    reply = ask_ai(user, text) if text else "I can only read text messages for now."
    r = requests.post(
        VONAGE_URL,
        auth=(VONAGE_KEY, VONAGE_SECRET),
        json={"from": VONAGE_FROM, "to": user, "channel": "whatsapp",
              "message_type": "text", "text": reply[:4000]},
        timeout=30,
    )
    if r.status_code >= 300:
        print("Vonage error:", r.status_code, r.text)


FINAL_MODEL = os.environ.get("GROQ_FINAL_MODEL", "qwen/qwen3.8-27b")
TAVILY_KEY = os.environ.get("TAVILY_API_KEY", "")   # optional, free tier at tavily.com

TOOLS = [{
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for current facts: news, prices, scores, dates, people, places.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
    },
}, {
    "type": "function",
    "function": {
        "name": "open_url",
        "description": "Open a web page and read its text. Use when the user gives a link or asks what a specific website says.",
        "parameters": {"type": "object",
                       "properties": {"url": {"type": "string"}},
                       "required": ["url"]},
    },
}]


def open_url(url):
    """Mini browser: fetch a public page and return its readable text (no logins, no JavaScript)."""
    import ipaddress, socket
    from urllib.parse import urlparse
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    host = urlparse(url).hostname or ""
    try:
        for fam, _, _, _, addr in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(addr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return "That address isn't a public website."
        r = requests.get(url, timeout=15, stream=True, allow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (TygarsBot; +fan-made)"})
        raw = r.raw.read(1_500_000, decode_content=True).decode(r.encoding or "utf-8", "ignore")
    except Exception as e:
        return f"Couldn't open {url} ({type(e).__name__})."
    if "html" not in r.headers.get("content-type", "html"):
        return f"{url} isn't a web page I can read ({r.headers.get('content-type')})."
    title = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    raw = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    t = html.unescape(title.group(1).strip()) if title else ""
    return f"Page: {url}\nTitle: {t}\n{text[:6000]}" if text else f"{url} has no readable text (it may need JavaScript or a login)."


def web_search(query):
    """Tavily when a key is set, otherwise DuckDuckGo instant answers plus Wikipedia."""
    out = []
    try:
        if TAVILY_KEY:
            r = requests.post("https://api.tavily.com/search", timeout=20,
                              json={"api_key": TAVILY_KEY, "query": query,
                                    "max_results": 5, "include_answer": True})
            d = r.json()
            if d.get("answer"):
                out.append("Answer: " + d["answer"])
            for x in d.get("results", [])[:5]:
                out.append(f"- {x.get('title')}: {x.get('content','')[:300]} ({x.get('url')})")
        else:
            try:
                rss = requests.get("https://news.google.com/rss/search", timeout=12,
                                   params={"q": query, "hl": "en-NG", "gl": "NG", "ceid": "NG:en"})
                root = ET.fromstring(rss.content)
                items = sorted(root.iter("item"),
                               key=lambda i: parsedate_to_datetime(i.findtext("pubDate") or "Thu, 01 Jan 1970 00:00:00 GMT"),
                               reverse=True)[:8]
                for i in items:
                    out.append(f"- [{i.findtext('pubDate')}] {i.findtext('title')}")
            except Exception as e:
                print("news error:", e)
            try:
                d = requests.get("https://api.duckduckgo.com/", timeout=15,
                                 params={"q": query, "format": "json", "no_html": 1}).json()
                if d.get("AbstractText"):
                    out.append(f"{d['AbstractText']} ({d.get('AbstractURL')})")
            except Exception as e:
                print("ddg error:", e)
            try:
                w = requests.get("https://en.wikipedia.org/w/api.php", timeout=10,
                             headers={"User-Agent": "HarkFanAfricaBot/1.0"},
                             params={"action": "query", "list": "search", "srsearch": query,
                                     "format": "json", "srlimit": 2}).json()
                for x in w.get("query", {}).get("search", []):
                    snip = x["snippet"].replace('<span class="searchmatch">', "").replace("</span>", "")
                    out.append(f"- {x['title']}: {snip}")
            except Exception as e:
                print("wiki error:", e)
    except Exception as e:
        print("search error:", e)
    return "\n".join(out) or "No results found."


def groq(messages, tools=True, model=None):
    body = {"model": model or MODEL, "messages": messages, "max_tokens": 900}
    if (model or MODEL).startswith("openai/"):
        body["reasoning_effort"] = "low"
    if tools:
        body["tools"] = TOOLS
    for attempt in range(2):
        r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                          headers={"Authorization": f"Bearer {GROQ_KEY}"}, json=body, timeout=40)
        if r.status_code != 429 or attempt:
            break
        time.sleep(min(float(r.headers.get("retry-after", 7)), 15))   # free-tier rate limit: wait once
    if r.status_code >= 300:
        print("Groq error:", r.status_code, r.text[:500])
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


def ask_ai(user, text):
    past = history.setdefault(user, [])
    past.append({"role": "user", "content": text})
    del past[:-10]
    try:
        today = time.strftime("%A %d %B %Y", time.gmtime())
        sys_msg = SYSTEM + f" Today is {today}. Your built-in knowledge is old, so ALWAYS call web_search before answering (or open_url for a specific site) anything about recent events, winners, prices, news, or people's current roles."
        if user.startswith("tg:"):
            sys_msg += TG_CREATOR
        msgs = [{"role": "system", "content": sys_msg}] + past
        found = []
        rounds = 0
        link = re.search(r"(https?://\S+|www\.\S+)", text)
        if link:
            found.append(open_url(link.group(1).rstrip(").,!?")))
            m = {"tool_calls": True}   # answer straight from the page
            rounds = 1
        else:
            m = groq(msgs)
        while m.get("tool_calls") and rounds < 1:
            rounds += 1
            msgs.append({"role": "assistant", "tool_calls": m["tool_calls"]})
            for c in m["tool_calls"]:
                try:
                    args = json.loads(c["function"]["arguments"])
                except Exception:
                    args = {}
                if c["function"]["name"] == "open_url" and args.get("url"):
                    res = open_url(args["url"])
                    found.append(res)
                else:
                    q = args.get("query", text)
                    res = web_search(q)
                    found.append(f"Search: {q}\n{res}")
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": res})
            m = {"tool_calls": True}
        if m.get("tool_calls"):
            # final answer from what was found, with tools switched off entirely
            final = [{"role": "system", "content": sys_msg}] + past[:-1] + [{
                "role": "user",
                "content": text + "\n\nWeb results:\n" + "\n\n".join(found)
                           + "\n\nAnswer using these results; prefer the newest dated items from trusted outlets, give scores/dates exactly, name the source and date, and if nothing confirms it say you couldn't verify it. Use ONLY facts written in these results: never add a score, number, name or date that isn't in them. Plain text, no markdown, short."}]
            try:
                m = groq(final, tools=False)
            except Exception:
                m = groq(final, tools=False, model=FINAL_MODEL)
        reply = (m.get("content") or "").replace("**", "").strip() or "Sorry, I couldn't find an answer."
    except Exception as e:
        print("AI error:", e)
        reply = "Sorry, I'm having trouble right now. Please try again in a moment."
    past.append({"role": "assistant", "content": reply})
    return reply


def send(to, text):
    r = requests.post(
        f"https://graph.facebook.com/{GRAPH}/{PHONE_ID}/messages",
        headers={"Authorization": f"Bearer {WA_TOKEN}"},
        json={"messaging_product": "whatsapp", "to": to,
              "type": "text", "text": {"body": text[:4000]}},
        timeout=30,
    )
    if r.status_code >= 300:
        print("WhatsApp error:", r.status_code, r.text)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))


@app.post("/telegram")
def telegram_incoming():
    if TG_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TG_SECRET:
        return "forbidden", 403
    upd = request.get_json(silent=True) or {}
    msg = upd.get("message") or {}
    text = msg.get("text")
    chat = (msg.get("chat") or {}).get("id")
    file_id = None
    if msg.get("photo"):
        file_id = (msg["photo"][-2] if len(msg["photo"]) > 1 else msg["photo"][-1])["file_id"]  # ~800px, saves tokens
    elif (msg.get("document") or {}).get("mime_type", "").startswith("image/"):
        file_id = msg["document"]["file_id"]
    voice = msg.get("voice") or msg.get("audio") or msg.get("video_note")
    if chat and voice and not file_id:
        threading.Thread(target=telegram_voice_reply, args=(chat, voice), daemon=True).start()
    elif chat and file_id:
        threading.Thread(target=telegram_photo_reply,
                         args=(chat, file_id, msg.get("caption") or ""), daemon=True).start()
    elif text and chat:
        threading.Thread(target=telegram_reply, args=(chat, text), daemon=True).start()
    return "ok", 200


VISION_MODEL = "qwen/qwen3.8-27b"


def telegram_voice_reply(chat, voice):
    """Voice note -> Groq Whisper transcript -> normal answer."""
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendChatAction",
                      json={"chat_id": chat, "action": "typing"}, timeout=5)
        if voice.get("duration", 0) > 300 or voice.get("file_size", 0) > 19_000_000:
            reply = "That voice note is too long. Please keep it under 5 minutes."
        else:
            info = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getFile",
                                params={"file_id": voice["file_id"]}, timeout=15).json()["result"]
            audio = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{info['file_path']}",
                                 timeout=60).content
            name = info["file_path"].rsplit("/", 1)[-1]
            if name.endswith(".oga"):
                name = name[:-4] + ".ogg"
            r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
                              headers={"Authorization": f"Bearer {GROQ_KEY}"},
                              files={"file": (name, audio)},
                              data={"model": "whisper-large-v3-turbo"}, timeout=60)
            r.raise_for_status()
            said = (r.json().get("text") or "").strip()
            if not said:
                reply = "I couldn't hear anything in that voice note. Try again a bit louder."
            else:
                reply = "\U0001F3A4 \"" + said[:300] + "\"\n\n" + ask_ai(f"tg:{chat}", said)
    except Exception as e:
        print("voice error:", e)
        reply = "Sorry, I couldn't listen to that voice note. Please try again or type it."
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  json={"chat_id": chat, "text": reply[:4000]}, timeout=20)


def telegram_photo_reply(chat, file_id, caption):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendChatAction",
                      json={"chat_id": chat, "action": "typing"}, timeout=5)
        info = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getFile",
                            params={"file_id": file_id}, timeout=15).json()["result"]
        if info.get("file_size", 0) > 4_000_000:
            reply = "That image is too big for me. Please send a smaller photo (under 4 MB)."
        else:
            img = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{info['file_path']}",
                               timeout=30).content
            mime = "image/png" if info["file_path"].lower().endswith(".png") else "image/jpeg"
            b64 = base64.b64encode(img).decode()
            ask = caption or "Look at this image and help with it. If it's a question or homework, solve it step by step. Otherwise say briefly what it shows."
            msgs = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": [
                        {"type": "text", "text": ask},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}]
            reply = (groq(msgs, tools=False, model=VISION_MODEL).get("content") or "").strip()
            reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S).replace("**", "").strip() or \
                "Sorry, I couldn't read that image. Try a clearer photo."
            h = history.setdefault(f"tg:{chat}", [])
            h += [{"role": "user", "content": f"[sent an image] {caption}"},
                  {"role": "assistant", "content": reply}]
            del h[:-10]
    except Exception as e:
        print("photo error:", e)
        reply = "Sorry, I couldn't open that image. Please try sending it again."
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  json={"chat_id": chat, "text": reply[:4000]}, timeout=20)


def telegram_reply(chat, text):
    if text.strip() == "/browse":
        reply = ""
    elif text.strip() == "/start":
        reply = ("Hey, I'm Tygars Bot \U0001F42F Ask me anything (text, photo or voice note): school work, tech, "
                 "business ideas, news, or everyday questions. I'll keep it quick and clear.")
    else:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendChatAction",
                          json={"chat_id": chat, "action": "typing"}, timeout=5)
        except Exception:
            pass
        reply = ask_ai(f"tg:{chat}", text)
    payload = {"chat_id": chat, "text": reply}
    if text.strip() in ("/start", "/browse"):
        if text.strip() == "/browse":
            payload["text"] = "Tap below to browse the web inside Telegram \U0001F310"
        payload["reply_markup"] = {"inline_keyboard": [[{"text": "\U0001F310 Browse the web",
                                   "web_app": {"url": BROWSE_URL}}]]}
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json=payload, timeout=20)
    if r.status_code >= 300:
        print("Telegram error:", r.status_code, r.text)


BROWSE_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://hark-whatsapp-bot.onrender.com") + "/browse"

BROWSE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tygars Browse</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
body{margin:0;font-family:system-ui,sans-serif;background:var(--tg-theme-bg-color,#fff);color:var(--tg-theme-text-color,#111);padding:18px}
h1{font-size:20px;margin:4px 0 14px}
form{display:flex;gap:8px}
input{flex:1;padding:12px;border-radius:12px;border:1px solid #8884;font-size:16px;background:var(--tg-theme-secondary-bg-color,#f3f3f3);color:inherit}
button{padding:12px 16px;border:0;border-radius:12px;font-size:16px;background:var(--tg-theme-button-color,#2a8af6);color:var(--tg-theme-button-text-color,#fff)}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:18px}
.grid a{display:block;text-align:center;padding:14px 6px;border-radius:12px;background:var(--tg-theme-secondary-bg-color,#f3f3f3);color:inherit;text-decoration:none;font-size:14px}
p.note{font-size:12px;opacity:.6;margin-top:18px}
</style></head><body>
<h1>\U0001F42F Tygars Browse</h1>
<form id="f"><input id="q" placeholder="Search or type a website" autocomplete="off" enterkeyhint="go"><button>Go</button></form>
<div class="grid" id="g"></div>
<p class="note">Pages open in Telegram's built-in browser. Fan-made, not affiliated with Hark.</p>
<script>
const tg=window.Telegram&&Telegram.WebApp; if(tg){tg.ready();tg.expand();}
function go(u){ if(tg&&tg.openLink){tg.openLink(u,{try_instant_view:true});} else {location.href=u;} }
const sites=[["Google","https://www.google.com"],["BBC News","https://www.bbc.com/news"],["Football","https://www.bbc.com/sport/football"],["Wikipedia","https://en.m.wikipedia.org"],["YouTube","https://m.youtube.com"],["Punch","https://punchng.com"],["Weather","https://www.accuweather.com"],["JAMB","https://www.jamb.gov.ng"],["Hark","https://hark.com"]];
document.getElementById("g").innerHTML=sites.map(s=>`<a href="#" data-u="${s[1]}">${s[0]}</a>`).join("");
document.getElementById("g").onclick=e=>{const u=e.target.dataset.u; if(u){e.preventDefault();go(u);}};
document.getElementById("f").onsubmit=e=>{e.preventDefault();let v=document.getElementById("q").value.trim(); if(!v)return;
 if(/^(https?:\\/\\/)?[\\w-]+(\\.[\\w-]+)+(\\/\\S*)?$/.test(v) && !v.includes(" ")){ if(!/^https?:/.test(v)) v="https://"+v; go(v);}
 else go("https://www.google.com/search?q="+encodeURIComponent(v)); };
</script></body></html>"""


@app.get("/browse")
def browse_page():
    return Response(BROWSE_HTML, mimetype="text/html")


def keep_awake():
    """Ping our own public URL every 10 minutes so the free Render instance doesn't sleep."""
    url = os.environ.get("RENDER_EXTERNAL_URL", "")
    while url:
        time.sleep(600)
        try:
            requests.get(url, timeout=20)
        except Exception as e:
            print("keep-awake error:", e)


threading.Thread(target=keep_awake, daemon=True).start()
