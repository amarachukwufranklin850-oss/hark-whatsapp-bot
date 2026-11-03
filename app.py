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
from hangout import bp as hangout_bp
app.register_blueprint(hangout_bp)
import betconv
app.register_blueprint(betconv.bp)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")        # any secret word you make up
WA_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")          # Meta access token
PHONE_ID = os.environ.get("PHONE_NUMBER_ID", "")         # from WhatsApp > API Setup
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")            # free key from Groq
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GRAPH = os.environ.get("GRAPH_VERSION", "v23.0")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")      # from @BotFather
TG_SECRET = os.environ.get("TELEGRAM_SECRET", "")        # optional webhook secret

SYSTEM = (
    "You are Tygar, a friendly AI assistant made by a fan community in Africa. Help with any "
    "question people ask: school work, tech, coding, business, writing, health basics, everyday "
    "advice. "
    "LANGUAGE: always reply in the same language and style the person wrote in. If they write "
    "Nigerian Pidgin (e.g. 'how far', 'wetin dey happen', 'abeg', 'una', 'dey', 'no wahala'), "
    "reply fully in natural Naija Pidgin, not English. Same for Yoruba, Igbo, Hausa, French, "
    "Swahili, Arabic, Portuguese or any other language. If they mix languages, mix the same way. "
    "Only switch language if they ask. "
    "NAME: don't introduce yourself or mention your name, your makers, or Hark unless the person "
    "asks who you are or asks about Hark. Never say you're from, part of, or admitted to Hark. "
    "If asked who you are: you're Tygar, a fan-made assistant, not affiliated with Hark. "
    "Only if asked about Hark: it is a personal AI that gets real things done (email, calendar, "
    "files, a real browser to book and order, memory, pages) on Android, iPhone, web and Mac, "
    "with a free plan; point people to hark.com. "
    "STYLE: answer first, short sentences, warm and precise, one clear pick when asked to "
    "choose, no filler like 'Great question'. Keep replies short enough for a chat. "
    "When asked to write a message, email, or reply for someone, write the full ready-to-send "
    "text so they can copy it. "
    "Use the web_search tool for anything current or that you are unsure of (news, prices, "
    "scores, dates). Only give verified information: state a current fact only if the search "
    "results support it, prefer trusted outlets (BBC, Reuters, AP, ESPN, Punch, Channels, "
    "official sites), and name the source and date. If sources disagree or none confirm it, say "
    "you couldn't verify it instead of guessing. Never invent scores, quotes, numbers or links. "
    "For medical, legal or money emergencies, advise a professional."
)

TG_CREATOR = (" If asked who made or created you, say you were made by Amarachukwu Franklin from "
              "Enugu State, a student at the State University of Medical and Applied Sciences (SUMAS), Igbo-Eno.")

DB_URL = os.environ.get("DATABASE_URL", "")


class Memory(dict):
    """Per-user chat history. Kept in RAM and saved to Postgres (Neon) when DATABASE_URL is set."""

    ready = False

    def _conn(self):
        import psycopg2
        c = psycopg2.connect(DB_URL, connect_timeout=8)
        if not Memory.ready:
            with c, c.cursor() as cur:
                cur.execute("create table if not exists bot_memory (user_id text primary key, "
                            "history jsonb, facts text, updated_at timestamptz default now())")
            Memory.ready = True
        return c

    def setdefault(self, user, default=None):
        if user not in self and DB_URL:
            try:
                with self._conn() as c, c.cursor() as cur:
                    cur.execute("select history from bot_memory where user_id=%s", (user,))
                    row = cur.fetchone()
                    if row:
                        dict.__setitem__(self, user, list(row[0]))
            except Exception as e:
                print("memory load error:", e)
        return dict.setdefault(self, user, default if default is not None else [])

    def save(self, user):
        if not DB_URL or user not in self:
            return
        try:
            with self._conn() as c, c.cursor() as cur:
                cur.execute("insert into bot_memory (user_id, history, updated_at) values (%s, %s::jsonb, now()) "
                            "on conflict (user_id) do update set history=excluded.history, updated_at=now()",
                            (user, json.dumps(self[user][-20:])))
        except Exception as e:
            print("memory save error:", e)


history = Memory()   # remembers each user across restarts when the database is connected
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
    del past[:-20]
    try:
        today = time.strftime("%A %d %B %Y", time.gmtime())
        sys_msg = SYSTEM + f" Today is {today}. Your built-in knowledge is old, so ALWAYS call web_search before answering (or open_url for a specific site) anything about recent events, winners, prices, news, or people's current roles."
        if user.startswith("tg:"):
            sys_msg += TG_CREATOR
        msgs = [{"role": "system", "content": sys_msg}] + past[-12:]
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
            final = [{"role": "system", "content": sys_msg}] + past[-12:-1] + [{
                "role": "user",
                "content": text + "\n\nWeb results:\n" + "\n\n".join(found)
                           + "\n\nAnswer using these results; prefer the newest dated items from trusted outlets, give scores/dates exactly, name the source and date, and if nothing confirms it say you couldn't verify it. Use ONLY facts written in these results: never add a score, number, name or date that isn't in them. Plain text, no markdown, short. Reply in the same language and style the user wrote in (Naija Pidgin if they wrote Pidgin)."}]
            try:
                m = groq(final, tools=False)
            except Exception:
                m = groq(final, tools=False, model=FINAL_MODEL)
        reply = (m.get("content") or "").replace("**", "").strip() or "Sorry, I couldn't find an answer."
    except Exception as e:
        print("AI error:", e)
        reply = "Sorry, I'm having trouble right now. Please try again in a moment."
    past.append({"role": "assistant", "content": reply})
    del past[:-20]
    history.save(user)
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


# Webhook secret: use TELEGRAM_SECRET if set, else derive one from the bot token. Register it
# with Telegram on startup, and enforce it only once Telegram has accepted it.
import hashlib as _hl
_TG_SECRET_EFF = TG_SECRET or (_hl.sha256(("tygar-webhook:" + TG_TOKEN).encode()).hexdigest()[:48] if TG_TOKEN else "")
_TG_SECRET_ON = {"v": bool(TG_SECRET)}


def _register_tg_secret():
    if not (TG_TOKEN and _TG_SECRET_EFF) or TG_SECRET:
        return
    hook = os.environ.get("RENDER_EXTERNAL_URL", "https://hark-whatsapp-bot.onrender.com") + "/telegram"
    for _ in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/setWebhook",
                              data={"url": hook, "secret_token": _TG_SECRET_EFF}, timeout=20)
            if r.ok and r.json().get("ok"):
                _TG_SECRET_ON["v"] = True
                print("Telegram webhook secret registered")
                return
            print("setWebhook failed:", r.status_code, r.text[:200])
        except Exception as e:
            print("setWebhook error:", e)
        time.sleep(5)


threading.Thread(target=_register_tg_secret, daemon=True).start()


@app.get("/tgsecretcheck")
def tg_secret_check():
    return {"enforced": _TG_SECRET_ON["v"]}


@app.post("/telegram")
def telegram_incoming():
    if _TG_SECRET_ON["v"] and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != _TG_SECRET_EFF:
        return "forbidden", 403
    upd = request.get_json(silent=True) or {}
    if gate_intercept(upd):          # not verified yet: only the verification flow runs
        return "ok", 200
    if upd.get("callback_query"):
        threading.Thread(target=telegram_callback, args=(upd["callback_query"],), daemon=True).start()
        return "ok", 200
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
        quoted = (msg.get("reply_to_message") or {}).get("text") or ""
        threading.Thread(target=telegram_reply, args=(chat, text, quoted), daemon=True).start()
    return "ok", 200


VISION_MODEL = "qwen/qwen3.8-27b"


def telegram_voice_reply(chat, voice):
    """Voice note -> Groq Whisper transcript -> normal answer (or transcript only in speech-to-text mode)."""
    only_text = take_mode(chat) == "stt"
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
            elif only_text:
                reply = "\U0001F4DD Transcript:\n\n" + said
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
            del h[:-20]
            history.save(f"tg:{chat}")
    except Exception as e:
        print("photo error:", e)
        reply = "Sorry, I couldn't open that image. Please try sending it again."
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  json={"chat_id": chat, "text": reply[:4000]}, timeout=20)


PDF_ASK = re.compile(r"\b(make|create|generate|write|turn|convert|give|send|prepare)\b.*\bpdf\b|\bpdf\b.*\b(of|on|about|for)\b", re.I | re.S)


def latin(t):
    """Core PDF fonts are Latin-1 only; swap common Unicode punctuation."""
    for a, b in {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-",
                 "\u2014": "-", "\u2011": "-", "\u2022": "-", "\u2026": "...", "\u00a0": " ",
                 "\u20a6": "N", "\u2192": "->", "\u00d7": "x"}.items():
        t = t.replace(a, b)
    import unicodedata
    t = unicodedata.normalize("NFKC", t).replace("\u2212", "-").replace("\u2011", "-")
    return t.encode("latin-1", "replace").decode("latin-1").replace("?\u00b0", "\u00b0")


def make_pdf(title, body):
    from fpdf import FPDF
    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 18)
    pdf.multi_cell(0, 9, latin(title), align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    for line in body.splitlines():
        line = line.rstrip()
        if not line:
            pdf.ln(3)
            continue
        if line.startswith("#"):
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 13)
            pdf.multi_cell(0, 7, latin(line.lstrip("# ").strip()), align="L", new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.set_font("Helvetica", "", 11)
            if line.lstrip().startswith(("- ", "* ")):
                line = "  - " + line.lstrip()[2:]
            pdf.multi_cell(0, 6, latin(line.replace("**", "")), align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(-14)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 6, "Made with Tygars Bot", align="C")
    return bytes(pdf.output())


def telegram_pdf_reply(chat, text):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendChatAction",
                      json={"chat_id": chat, "action": "upload_document"}, timeout=5)
        msgs = [{"role": "system", "content": SYSTEM + " You are writing the content of a PDF document. "
                 "First line: a short title only. Then the document in plain text: use lines starting with "
                 "'# ' for section headings and '- ' for bullet points. No tables, no emojis. Be thorough "
                 "but clear (about 300-900 words unless asked otherwise)."},
                {"role": "user", "content": text}]
        out = (groq(msgs, tools=False).get("content") or "").strip()
        out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()
        title, _, body = out.partition("\n")
        title = title.strip("# *").strip() or "Tygars Document"
        data = make_pdf(title, body.strip())
        fname = re.sub(r"[^A-Za-z0-9]+", "_", title)[:40].strip("_") + ".pdf"
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                          data={"chat_id": chat, "caption": f"Here's your PDF: {title}"[:1000]},
                          files={"document": (fname, data, "application/pdf")}, timeout=60)
        if r.status_code >= 300:
            print("sendDocument error:", r.status_code, r.text)
            raise RuntimeError("send failed")
        h = history.setdefault(f"tg:{chat}", [])
        h += [{"role": "user", "content": text}, {"role": "assistant", "content": f"[sent a PDF: {title}]"}]
        del h[:-20]
        history.save(f"tg:{chat}")
    except Exception as e:
        print("pdf error:", e)
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": chat, "text": "Sorry, I couldn't make that PDF. Please try again."}, timeout=20)


# ---------- picture & video creation (free: Pollinations images + ffmpeg) ----------
IMG_ASK = re.compile(r"^\s*(please\s+)?(draw|paint|sketch|generate|create|make|design|show me)\b.{0,40}\b(image|picture|pic|photo|drawing|art|artwork|logo|poster|wallpaper|illustration)s?\b", re.I | re.S)
VID_ASK = re.compile(r"^\s*(please\s+)?(generate|create|make|animate)\b.{0,40}\b(video|clip|animation)\b", re.I | re.S)
MEDIA_BLOCK = re.compile(r"\b(nude|naked|nsfw|porn|sex|sexy|boobs|explicit|gore|beheading|child|kid|minor|underage|schoolgirl)\b", re.I)
MEDIA_LIMITS = {"img": 30, "vid": 6}
_media_use = {}


def media_quota(chat, kind):
    day = time.strftime("%Y-%m-%d")
    k = (chat, kind, day)
    if _media_use.get(k, 0) >= MEDIA_LIMITS[kind]:
        return False
    _media_use[k] = _media_use.get(k, 0) + 1
    return True


def media_prompt(text, cmd):
    p = text[len(cmd):].strip() if text.lower().startswith(cmd) else text.strip()
    p = re.sub(r"^\s*(please\s+)?(draw|paint|sketch|generate|create|make|design|show me|animate)\s+(me\s+)?(an?\s+)?"
               r"(image|picture|pic|photo|drawing|art|artwork|video|clip|animation)?\s*(of|showing|about|for)?\s*", "", p, flags=re.I)
    return p.strip(" .")[:400]


_img_lock = threading.Lock()
_img_last = [0.0]
IMG_GAP = 46   # free tier: 1 image per 45 s per server IP (POLLINATIONS_KEY lifts it)


def ai_image(prompt, seed=None, size=768):
    """Free Pollinations image. Without a key the server gets 1 image / 45 s, so requests queue."""
    from urllib.parse import quote
    seed = seed if seed is not None else int(time.time()) % 100000
    key = os.environ.get("POLLINATIONS_KEY", "")
    url = (f"https://image.pollinations.ai/prompt/{quote(prompt)}?width={size}&height={size}"
           f"&seed={seed}&nologo=true&safe=true")
    hdr = {"Authorization": f"Bearer {key}"} if key else {}
    with _img_lock:
        for _ in range(3):
            if not key:
                wait = IMG_GAP - (time.time() - _img_last[0])
                if wait > 0:
                    time.sleep(wait)
            try:
                r = requests.get(url, headers=hdr, timeout=90)
                _img_last[0] = time.time()
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                    return r.content
                print("image gen status:", r.status_code, r.text[:200])
                m = re.search(r"Retry after (\d+)s", r.text)
                time.sleep(int(m.group(1)) + 1 if m else 5)
            except Exception as e:
                print("image gen error:", e)
    return None


def tg_text(chat, t):
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": chat, "text": t}, timeout=20)


def remember(chat, user_text, note):
    try:
        h = history.setdefault(f"tg:{chat}", [])
        h += [{"role": "user", "content": user_text}, {"role": "assistant", "content": note}]
        del h[:-20]
        history.save(f"tg:{chat}")
    except Exception as e:
        print("history error:", e)

UA = {"User-Agent": "TygarsBot/1.0 (fan-made Telegram bot; contact harkbotofficial@gmail.com)"}


def web_photos(query, n=1):
    """Real photos from a quick web search (Wikimedia Commons, then Openverse). Returns [(bytes, title, page)]."""
    found = []
    try:
        r = requests.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=25, params={
            "action": "query", "generator": "search", "gsrnamespace": 6, "gsrsearch": query,
            "gsrlimit": 12, "prop": "imageinfo", "iiprop": "url|mime", "iiurlwidth": 1280, "format": "json"})
        pages = sorted((r.json().get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
        for p in pages:
            ii = (p.get("imageinfo") or [{}])[0]
            if ii.get("mime") in ("image/jpeg", "image/png"):
                found.append((ii.get("thumburl") or ii.get("url"), p["title"][5:].rsplit(".", 1)[0], ii.get("descriptionurl", "")))
    except Exception as e:
        print("commons error:", e)
    if len(found) < n:
        try:
            r = requests.get("https://api.openverse.org/v1/images/", headers=UA, timeout=25,
                             params={"q": query, "page_size": 10, "mature": "false"})
            for x in r.json().get("results", []):
                found.append((x["url"], x.get("title") or query, x.get("foreign_landing_url", "")))
        except Exception as e:
            print("openverse error:", e)
    out = []
    for url, title, page in found:
        if len(out) >= n:
            break
        try:
            g = requests.get(url, headers=UA, timeout=30)
            if g.status_code == 200 and g.headers.get("content-type", "").startswith("image/") and len(g.content) < 9_000_000:
                out.append((g.content, title, page))
        except Exception as e:
            print("photo fetch error:", e)
    return out


def telegram_image_reply(chat, text):
    prompt = media_prompt(text, "/image")
    if not prompt:
        return tg_text(chat, "Tell me what to draw, e.g. /image a lion wearing agbada in Lagos at sunset")
    if MEDIA_BLOCK.search(prompt):
        return tg_text(chat, "Sorry, I can't make that kind of picture. Try something else.")
    if not media_quota(chat, "img"):
        return tg_text(chat, f"You've hit today's limit of {MEDIA_LIMITS['img']} pictures. Try again tomorrow.")
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendChatAction", json={"chat_id": chat, "action": "upload_photo"}, timeout=5)
    pics = web_photos(prompt, 1)
    if not pics:
        return tg_text(chat, f"I couldn't find a photo of \"{prompt}\" online. Try different words.")
    img, title, page = pics[0]
    cap = f"\U0001F4F7 {title}" + (f"\nSource: {page}" if page else "")
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                      data={"chat_id": chat, "caption": cap[:1000]},
                      files={"photo": ("photo.jpg", img, "image/jpeg")}, timeout=60)
    if r.status_code >= 300:
        print("sendPhoto error:", r.status_code, r.text)
        return tg_text(chat, "Sorry, I couldn't send the photo.")
    remember(chat, text, f"[sent a web photo: {title}]")


def make_clip(frames, secs=3):
    """Turn still frames into a short MP4 with slow zoom and crossfades."""
    import subprocess, tempfile, imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    d = tempfile.mkdtemp()
    ins, parts = [], []
    fps, n = 24, len(frames)
    for i, f in enumerate(frames):
        p = os.path.join(d, f"f{i}.jpg")
        open(p, "wb").write(f)
        ins += ["-i", p]
        z = "zoom+0.0012" if i % 2 == 0 else "if(eq(on,0),1.15,zoom-0.0012)"
        parts.append(f"[{i}:v]scale=1024:1024:force_original_aspect_ratio=increase,crop=1024:1024,zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                     f"d={secs*fps}:s=640x640:fps={fps},setsar=1,format=yuv420p[v{i}]")
    chain, last, off = "", "v0", secs - 0.6
    for i in range(1, n):
        chain += f";[{last}][v{i}]xfade=transition=fade:duration=0.6:offset={off:.2f}[x{i}]"
        last, off = f"x{i}", off + secs - 0.6
    out = os.path.join(d, "out.mp4")
    cmd = [ff, "-y", *ins, "-filter_complex", ";".join(parts) + chain, "-map", f"[{last}]",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "26", "-movflags", "+faststart", out]
    subprocess.run(cmd, check=True, capture_output=True, timeout=240)
    data = open(out, "rb").read()
    subprocess.run(["rm", "-rf", d])
    return data


def telegram_video_reply(chat, text):
    prompt = media_prompt(text, "/video")
    if not prompt:
        return tg_text(chat, "Tell me what the video should show, e.g. /video a rocket launching from Lagos at night")
    if MEDIA_BLOCK.search(prompt):
        return tg_text(chat, "Sorry, I can't make that kind of video. Try something else.")
    if not media_quota(chat, "vid"):
        return tg_text(chat, f"You've hit today's limit of {MEDIA_LIMITS['vid']} videos. Try again tomorrow.")
    tg_text(chat, "\U0001F3AC Making your video, this takes about a minute...")
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendChatAction", json={"chat_id": chat, "action": "upload_video"}, timeout=5)
    frames = [p[0] for p in web_photos(prompt, 4)]
    if len(frames) < 2:
        return tg_text(chat, f"I couldn't find enough photos of \"{prompt}\" online for a video. Try different words.")
    try:
        vid = make_clip(frames, secs=3)
    except Exception as e:
        print("video error:", e)
        return tg_text(chat, "Sorry, I couldn't put the video together. Please try again.")
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendVideo",
                      data={"chat_id": chat, "caption": f"\U0001F3AC {prompt}"[:1000], "supports_streaming": "true"},
                      files={"video": ("tygars.mp4", vid, "video/mp4")}, timeout=120)
    if r.status_code >= 300:
        print("sendVideo error:", r.status_code, r.text)
        return tg_text(chat, "Sorry, I couldn't send the video.")
    remember(chat, text, f"[sent a photo slideshow video: {prompt}]")


TTS_VOICE = "en-NG-AbeoNeural"   # Nigerian English voice (Microsoft Edge TTS, free)
VOICE_ASK = re.compile(r"\b(voice note|voice reply|in (a )?voice|audio|read (it|this|that) (out|aloud)|say it|speak it|talk to me)\b", re.I)


def tts_mp3(text):
    import asyncio, tempfile, edge_tts
    clean = re.sub(r"[*_#`]", "", text)[:1500]
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        path = f.name
    asyncio.run(edge_tts.Communicate(clean, TTS_VOICE).save(path))
    data = open(path, "rb").read()
    os.remove(path)
    return data


def send_voice(chat, text, caption=""):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendChatAction",
                      json={"chat_id": chat, "action": "record_voice"}, timeout=5)
        audio = tts_mp3(text)
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendAudio",
                          data={"chat_id": chat, "title": "Tygars Bot", "performer": "Tygars",
                                "caption": caption[:1000]},
                          files={"audio": ("tygars.mp3", audio, "audio/mpeg")}, timeout=60)
        if r.status_code >= 300:
            print("sendAudio error:", r.status_code, r.text)
            raise RuntimeError("send failed")
        return True
    except Exception as e:
        print("tts error:", e)
        return False


def telegram_reply(chat, text, reply_to_text=""):
    low0 = text.strip().lower()
    mode = take_mode(chat) if not text.startswith("/") else (pending_mode.pop(chat, None) and None)
    if mode == "betcode":
        return betconv.reply_code(chat, text, tg_send)
    if low0.startswith("/convert"):
        return betconv.convert_cmd(chat, text, tg_send)
    if mode == "image":
        return telegram_image_reply(chat, "/image " + text)
    if mode == "video":
        return telegram_video_reply(chat, "/video " + text)
    if mode == "tts":
        return telegram_reply(chat, "/speak " + text)
    if mode == "pdf":
        return telegram_pdf_reply(chat, "Make a PDF about: " + text)
    if mode == "remind":
        return reminder_cmd(chat, "remind me " + text)
    if mode == "todo":
        return todo_cmd(chat, "/todo " + text)
    if mode == "stt":
        return tg_send(chat, "Please send a voice note and I'll write out what was said.")
    if low0.startswith("/open"):
        return telegram_open(chat, text)
    if mode == "web":
        return telegram_open(chat, "/open " + text)
    if low0 in ("/menu", "menu", "/help"):
        return tg_send(chat, "🐯 Tygars Bot menu. Tap what you need:", main_menu())
    if low0.startswith("/reminders") or low0 in ("my reminders", "show my reminders"):
        return list_reminders(chat)
    if low0.startswith("/remind") or re.match(r"^\s*(remind me|set (a )?reminder)\b", text, re.I):
        return reminder_cmd(chat, text)
    if low0.startswith("/todo") or re.match(r"^\s*(add to (my )?(to-?do|todo)|todo:)", text, re.I) or low0 in ("my todo list", "my to-do list", "show my todo"):
        return todo_cmd(chat, text)
    low = text.strip().lower()
    if low.startswith("/image") or (IMG_ASK.search(text) and not text.startswith("/")):
        return telegram_image_reply(chat, text)
    if low.startswith("/video") or (VID_ASK.search(text) and not text.startswith("/")):
        return telegram_video_reply(chat, text)
    if PDF_ASK.search(text) and not text.startswith("/"):
        return telegram_pdf_reply(chat, text)
    if text.startswith("/speak"):
        words = text[6:].strip() or reply_to_text
        if not words:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": chat, "text": "Send /speak followed by the text, or reply to a message with /speak."}, timeout=20)
        elif not send_voice(chat, words):
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": chat, "text": "Sorry, I couldn't make the audio right now."}, timeout=20)
        return
    if VOICE_ASK.search(text):
        answer = ask_ai(f"tg:{chat}", text + "\n(Reply in a few spoken-style sentences; it will be read aloud.)")
        if send_voice(chat, answer, caption=answer):
            return
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": chat, "text": answer}, timeout=20)
        return
    if text.strip() == "/browse":
        reply = ""
    elif text.strip() == "/start":
        reply = ("Hey, I'm Tygars Bot \U0001F42F Ask me anything (text, photo or voice note), set reminders, keep a to-do list, find photos, make PDFs and more. Tap a button below or send /menu anytime. Ask me about school work, tech, "
                 "business ideas, news, or everyday questions. I'll keep it quick and clear.")
    else:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendChatAction",
                          json={"chat_id": chat, "action": "typing"}, timeout=5)
        except Exception:
            pass
        reply = ask_ai(f"tg:{chat}", text)
    payload = {"chat_id": chat, "text": reply}
    if text.strip() == "/start":
        payload["reply_markup"] = main_menu()
    elif text.strip() == "/browse":
        payload["text"] = "Tap below to browse the web inside Telegram \U0001F310"
        payload["reply_markup"] = {"inline_keyboard": [[
            {"text": "\U0001F4AC Live chat", "web_app": {"url": BROWSE_URL.replace("/browse", "/chat")}},
            {"text": "\U0001F310 Browse the web", "web_app": {"url": BROWSE_URL}}]]}
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
<h1>\U0001F42F Tygars Browse <a href="/chat" style="float:right;font-size:14px;font-weight:400;color:var(--tg-theme-link-color,#2a8af6);text-decoration:none">Chat</a></h1>
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


CHAT_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Tygars Chat</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{font-family:system-ui,sans-serif;background:var(--tg-theme-bg-color,#fff);color:var(--tg-theme-text-color,#111);display:flex;flex-direction:column}
header{padding:12px 16px;font-weight:600;border-bottom:1px solid #8883;display:flex;justify-content:space-between;align-items:center}
header a{font-weight:400;font-size:14px;color:var(--tg-theme-link-color,#2a8af6);text-decoration:none}
#log{flex:1;overflow-y:auto;padding:14px}
.m{max-width:85%;padding:10px 13px;border-radius:16px;margin:6px 0;white-space:pre-wrap;line-height:1.4;font-size:15px}
.u{margin-left:auto;background:var(--tg-theme-button-color,#2a8af6);color:var(--tg-theme-button-text-color,#fff);border-bottom-right-radius:4px}
.b{background:var(--tg-theme-secondary-bg-color,#f1f1f1);border-bottom-left-radius:4px}
.hint{opacity:.6;text-align:center;font-size:14px;margin-top:30%}
form{display:flex;gap:8px;padding:10px;border-top:1px solid #8883}
textarea{flex:1;resize:none;padding:11px;border-radius:20px;border:1px solid #8884;font:inherit;font-size:16px;max-height:120px;background:var(--tg-theme-secondary-bg-color,#f3f3f3);color:inherit}
button{border:0;border-radius:50%;width:44px;height:44px;font-size:18px;background:var(--tg-theme-button-color,#2a8af6);color:var(--tg-theme-button-text-color,#fff)}
button.sm{width:auto;height:30px;border-radius:15px;padding:0 10px;font-size:14px;background:var(--tg-theme-secondary-bg-color,#eee);color:inherit}
button.on{background:#e53935;color:#fff}
#mic.rec{background:#e53935;animation:p 1s infinite}@keyframes p{50%{opacity:.6}}
#st{text-align:center;font-size:13px;opacity:.7;min-height:0}
</style></head><body>
<header><span>\U0001F42F Tygars Chat</span><span><button id="spk" class="sm" title="Speak replies">\U0001F507</button> <button id="live" class="sm" title="Live voice chat">\U0001F399 Live</button> <a href="/browse">Browse</a></span></header>
<div id="st"></div>
<div id="log"><div class="hint">Ask me anything. I'll answer live.</div></div>
<form id="f"><button type="button" id="mic" title="Talk">\U0001F3A4</button><textarea id="q" rows="1" placeholder="Message Tygars"></textarea><button>\u27A4</button></form>
<script>
const tg=window.Telegram&&Telegram.WebApp; if(tg){tg.ready();tg.expand();}
const log=document.getElementById("log"), q=document.getElementById("q");
function add(cls,t){const d=document.createElement("div");d.className="m "+cls;d.textContent=t;log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}
q.addEventListener("input",()=>{q.style.height="auto";q.style.height=q.scrollHeight+"px";});
q.addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();document.getElementById("f").requestSubmit();}});
document.getElementById("f").onsubmit=async e=>{e.preventDefault();const t=q.value.trim();if(!t)return;
 const h=log.querySelector(".hint");if(h)h.remove();
 add("u",t);q.value="";q.style.height="auto";const b=add("b","\u2026");
 try{const r=await fetch("/api/chat",{method:"POST",headers:{"Content-Type":"application/json"},
   body:JSON.stringify({text:t,initData:tg?tg.initData:""})});const j=await r.json();
   await type(b,j.reply||"Sorry, something went wrong.");
   if(speak&&j.reply){fetch("/api/tts",{method:"POST",headers:{"Content-Type":"application/json"},
     body:JSON.stringify({text:j.reply,initData:tg?tg.initData:""})}).then(r=>r.json()).then(k=>{if(k.audio)play(k.audio);});}
 }catch(err){b.textContent="Connection problem. Please try again.";}
};
async function type(b,t){const w=t.split(/(\\s+)/);b.textContent="";for(const x of w){b.textContent+=x;log.scrollTop=log.scrollHeight;await new Promise(r=>setTimeout(r,18));}}
let speak=false, live=false, rec=null, stream=null, player=null;
const st=document.getElementById("st"), mic=document.getElementById("mic");
function status(t){st.textContent=t||"";}
document.getElementById("spk").onclick=e=>{speak=!speak;e.target.textContent=speak?"\U0001F50A":"\U0001F507";};
function play(b64){return new Promise(res=>{if(player)player.pause();player=new Audio("data:audio/mpeg;base64,"+b64);
  player.onended=res;player.onerror=res;player.play().catch(res);});}
async function listen(){
 if(rec)return; if(player)player.pause();
 try{stream=stream||await navigator.mediaDevices.getUserMedia({audio:true});}
 catch(e){status("Microphone blocked. Allow mic access for Telegram, then try again.");stopLive();return;}
 const type=["audio/webm","audio/mp4","audio/ogg"].find(t=>window.MediaRecorder&&MediaRecorder.isTypeSupported(t))||"";
 rec=new MediaRecorder(stream,type?{mimeType:type}:{}); const chunks=[]; rec.ondataavailable=e=>chunks.push(e.data);
 const ctx=new (window.AudioContext||window.webkitAudioContext)(), an=ctx.createAnalyser(); ctx.createMediaStreamSource(stream).connect(an);
 const buf=new Uint8Array(an.fftSize); let heard=false, quiet=Date.now(), start=Date.now();
 const tick=setInterval(()=>{an.getByteTimeDomainData(buf);let m=0;for(const v of buf)m=Math.max(m,Math.abs(v-128));
   if(m>12){heard=true;quiet=Date.now();}
   if((heard&&Date.now()-quiet>1500)||Date.now()-start>30000||(!heard&&live&&Date.now()-start>12000)){if(rec&&rec.state==="recording")rec.stop();}},120);
 rec.onstop=async()=>{clearInterval(tick);ctx.close();mic.classList.remove("rec");const r0=rec;rec=null;
   if(!heard){status(live?"Didn't catch anything. Live chat paused.":"");stopLive();return;}
   status("Thinking\u2026");const blob=new Blob(chunks,{type:r0.mimeType||"audio/webm"});
   const h=log.querySelector(".hint");if(h)h.remove();const u=add("u","\U0001F3A4 \u2026"),b=add("b","\u2026");
   const fd=new FormData();fd.append("audio",blob,"voice");fd.append("initData",tg?tg.initData:"");fd.append("speak",(live||speak)?"1":"0");
   try{const j=await (await fetch("/api/voice",{method:"POST",body:fd})).json();
     u.textContent="\U0001F3A4 "+(j.heard||"(couldn't hear)");await type(b,j.reply||"I didn't catch that.");
     if(j.audio){status("Speaking\u2026");await play(j.audio);}
   }catch(e){b.textContent="Connection problem. Please try again.";}
   status(live?"Listening\u2026":"");if(live)listen();};
 rec.start();mic.classList.add("rec");status(live?"Listening\u2026 talk any time":"Listening\u2026 tap the mic to send");
}
mic.onclick=()=>{if(rec&&rec.state==="recording")rec.stop();else listen();};
const lb=document.getElementById("live");
function stopLive(){live=false;lb.classList.remove("on");lb.textContent="\U0001F399 Live";}
lb.onclick=()=>{if(live){stopLive();if(rec&&rec.state==="recording")rec.stop();if(player)player.pause();status("");return;}
 live=true;lb.classList.add("on");lb.textContent="\u23F9 End";listen();};
</script></body></html>"""


def tg_user_from_init(init_data):
    """Check Telegram WebApp initData signature; return the user id or None."""
    import hmac, hashlib
    from urllib.parse import parse_qsl
    if not init_data or not TG_TOKEN:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    got = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", TG_TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
        return None
    if time.time() - int(pairs.get("auth_date", "0")) > 86400:
        return None
    try:
        return json.loads(pairs.get("user", "{}")).get("id")
    except Exception:
        return None


@app.get("/chat")
def chat_page():
    return Response(CHAT_HTML, mimetype="text/html")


@app.post("/api/chat")
def chat_api():
    d = request.get_json(silent=True) or {}
    uid = tg_user_from_init(d.get("initData", ""))
    if not uid:
        return {"reply": "Please open Tygars Chat from inside the Telegram bot."}, 403
    if not is_verified(uid):
        return {"reply": NOT_VERIFIED_WEB}, 403
    text = (d.get("text") or "").strip()[:2000]
    if not text:
        return {"reply": ""}
    return {"reply": ask_ai(f"tg:{uid}", text)}


def transcribe(name, audio):
    r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
                      headers={"Authorization": f"Bearer {GROQ_KEY}"},
                      files={"file": (name, audio)},
                      data={"model": "whisper-large-v3-turbo"}, timeout=60)
    r.raise_for_status()
    return (r.json().get("text") or "").strip()


@app.post("/api/voice")
def voice_api():
    import base64
    uid = tg_user_from_init(request.form.get("initData", ""))
    if not uid:
        return {"reply": "Please open Tygars Chat from inside the Telegram bot."}, 403
    if not is_verified(uid):
        return {"reply": NOT_VERIFIED_WEB}, 403
    f = request.files.get("audio")
    if not f:
        return {"reply": "No audio received."}, 400
    audio = f.read(8_000_000)
    ext = "mp4" if "mp4" in (f.mimetype or "") else "ogg" if "ogg" in (f.mimetype or "") else "webm"
    try:
        heard = transcribe(f"voice.{ext}", audio)
    except Exception as e:
        print("live voice stt error:", e)
        return {"heard": "", "reply": "Sorry, I couldn't hear that. Please try again."}
    if not heard:
        return {"heard": "", "reply": ""}
    reply = ask_ai(f"tg:{uid}", heard)
    out = {"heard": heard, "reply": reply}
    if request.form.get("speak", "1") == "1":
        try:
            out["audio"] = base64.b64encode(tts_mp3(reply)).decode()
        except Exception as e:
            print("live voice tts error:", e)
    return out


@app.post("/api/tts")
def tts_api():
    import base64
    d = request.get_json(silent=True) or {}
    tuid = tg_user_from_init(d.get("initData", ""))
    if not tuid or not is_verified(tuid):
        return {}, 403
    try:
        return {"audio": base64.b64encode(tts_mp3((d.get("text") or "")[:1500])).decode()}
    except Exception as e:
        print("tts api error:", e)
        return {}


@app.get("/browse")
def browse_page():
    return Response(BROWSE_HTML, mimetype="text/html")


# ---------- daily helper: reminders, to-do list, menu buttons ----------
WAT = 3600            # users are in West Africa (UTC+1)
pending_mode = {}     # chat -> what the next message is for (set by a menu button)
X_HANDLES = [("@hackmacos", "https://x.com/hackmacos")]


def tq(sql, args=(), fetch=False):
    """Small DB helper for reminders/to-dos (Neon on Render, SQLite locally)."""
    from hangout import q
    if not getattr(tq, "ready", False):
        for s in ["create table if not exists bot_reminders (id bigserial primary key, chat bigint, body text, due double precision, rep text default '')",
                  "create table if not exists bot_todos (id bigserial primary key, chat bigint, body text, done int default 0, ts double precision)",
                  "create table if not exists bot_verified (user_id bigint primary key, method text, granted_at double precision, clock text default '')",
                  "create table if not exists bot_verify_shots (user_id bigint, day text, n int default 0, primary key (user_id, day))"]:
            if not os.environ.get("DATABASE_URL"):
                s = s.replace("bigserial primary key", "integer primary key autoincrement").replace("double precision", "real").replace("bigint", "integer")
            q(s)
        tq.ready = True
    return q(sql, args, fetch=fetch)


# ---------- verification gate (captcha + screenshot of following @hackmacos on X) ----------
# Every Telegram user, old and new, must pass this once before any feature works. Stored in bot_verified.
GATE_HANDLE = "hackmacos"
GATE_URL = "https://x.com/hackmacos"
SHOT_LIMIT = 5                    # screenshot checks per user per day (WAT day)
GATE = {}                         # user_id -> {"cap": answer, "cap_ok": ts}
_verified = set()                 # cache of verified user ids
NOT_VERIFIED_WEB = "Please verify in the Tygar bot first: open the bot and send /start."
GATE_INTRO = ("\U0001F512 Quick verification before you use Tygar (one time only):\n\n"
              "1\u20e3 Solve a quick captcha.\n"
              "2\u20e3 Follow @hackmacos on X, open its profile so the button shows \"Following\", take a screenshot "
              "with your phone's clock visible at the top, and send it here right away (within 5 minutes).\n\n"
              "Send the screenshot as a normal photo, not forwarded.")


def is_verified(uid):
    if not uid:
        return False
    if uid in _verified:
        return True
    try:
        if tq("select 1 from bot_verified where user_id=?", (uid,), fetch=True):
            _verified.add(uid)
            return True
    except Exception as e:
        print("verify lookup error:", e)
    return False


def grant_verified(uid, clock=""):
    try:
        tq("delete from bot_verified where user_id=?", (uid,))
        tq("insert into bot_verified (user_id, method, granted_at, clock) values (?, ?, ?, ?)",
           (uid, "captcha+x_follow_screenshot", time.time(), clock[:20]))
    except Exception as e:
        print("verify save error:", e)
    _verified.add(uid)


def gate_kb(after_captcha=False):
    rows = [[{"text": "\U0001D54F Follow @hackmacos", "url": GATE_URL}]]
    if after_captcha:
        rows.append([{"text": "\U0001F4F8 Send screenshot", "callback_data": "gv:shot"}])
    else:
        rows.append([{"text": "\U0001F9E9 Start captcha", "callback_data": "gv:cap"}])
    return {"inline_keyboard": rows}


def gate_prompt(chat, uid):
    g = GATE.get(uid) or {}
    if g.get("cap_ok") and time.time() - g["cap_ok"] < 1800:
        return tg_send(chat, "\u2705 Captcha done. Now send your screenshot of @hackmacos on X showing \"Following\", "
                             "with your phone's clock visible at the top (taken in the last 5 minutes).", gate_kb(True))
    tg_send(chat, GATE_INTRO, gate_kb(False))


def send_captcha(chat, uid):
    import random
    a, b = random.randint(2, 19), random.randint(2, 19)
    ans = a + b
    opts = {ans}
    while len(opts) < 4:
        opts.add(ans + random.choice([-3, -2, -1, 1, 2, 3, 10, -10]))
    opts = sorted(o for o in opts if o > 0)
    GATE[uid] = {"cap": ans, "cap_ts": time.time()}
    tg_send(chat, f"\U0001F9E9 Captcha: what is {a} + {b}?",
            {"inline_keyboard": [[{"text": str(o), "callback_data": f"gv:a:{o}"} for o in opts]]})


def gate_callback(cb, chat, uid):
    data = cb.get("data") or ""
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb.get("id")}, timeout=10)
    if data == "gv:cap":
        return send_captcha(chat, uid)
    if data.startswith("gv:a:"):
        g = GATE.get(uid) or {}
        if g.get("cap") is not None and data[5:] == str(g["cap"]) and time.time() - g.get("cap_ts", 0) < 600:
            GATE[uid] = {"cap_ok": time.time()}
            return gate_prompt(chat, uid)
        tg_send(chat, "\u274C Not quite. Here's a new one.")
        return send_captcha(chat, uid)
    if data == "gv:shot":
        return gate_prompt(chat, uid)
    gate_prompt(chat, uid)        # any other button before verification


def shots_used(uid, add=False):
    day = time.strftime("%Y-%m-%d", time.gmtime(time.time() + WAT))
    rows = tq("select n from bot_verify_shots where user_id=? and day=?", (uid, day), fetch=True)
    n = rows[0][0] if rows else 0
    if add:
        if rows:
            tq("update bot_verify_shots set n=? where user_id=? and day=?", (n + 1, uid, day))
        else:
            tq("insert into bot_verify_shots (user_id, day, n) values (?, ?, ?)", (uid, day, 1))
        n += 1
    return n


def clock_fresh(clock, now=None):
    """True if the status-bar time is within 5 minutes of now in GMT, WAT or SAST (12h clocks allowed)."""
    m = re.search(r"(\d{1,2})\s*[:.]\s*(\d{2})\s*([ap])?\.?\s*m?", (clock or "").lower())
    if not m:
        return False
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if h > 23 or mi > 59:
        return False
    if ap:
        h = (h % 12) + (12 if ap == "p" else 0)
        cands = [h * 60 + mi]
    else:
        cands = [h * 60 + mi] + ([((h % 12) + 12) * 60 + mi, (h % 12) * 60 + mi] if h <= 12 else [])
    now = time.time() if now is None else now
    for off in (0, 1, 2):          # GMT, WAT, SAST
        t = time.gmtime(now + off * 3600)
        cur = t.tm_hour * 60 + t.tm_min
        for c in cands:
            d = abs(cur - c) % 1440
            if min(d, 1440 - d) <= 5:
                return True
    return False


SHOT_PROMPT = ("You are checking a verification screenshot. Answer ONLY with one JSON object, no other text:\n"
               '{"x_profile": true/false, "handle": "the @handle of the profile shown, without @", '
               '"following": true/false, "clock": "the time shown in the phone status bar at the very top, exactly as shown, or empty", '
               '"reason": "one short sentence"}\n'
               "x_profile = the image is a screenshot of an X (Twitter) profile page (app or website).\n"
               "following = the profile's follow button clearly reads \"Following\" (meaning the viewer follows this account). "
               "A \"Follow\" button means false. Be strict: if unsure, false.\n"
               "clock = only the phone/computer status-bar clock; leave empty if there is no status bar or clock visible.")


def gate_photo(chat, uid, file_id, forwarded):
    try:
        g = GATE.get(uid) or {}
        if not (g.get("cap_ok") and time.time() - g["cap_ok"] < 1800):
            tg_send(chat, "Please solve the captcha first, then send your screenshot.")
            return send_captcha(chat, uid)
        if forwarded:
            return tg_send(chat, "\u274C Forwarded photos can't be used. Take a fresh screenshot and send it directly.", gate_kb(True))
        if shots_used(uid) >= SHOT_LIMIT:
            return tg_send(chat, f"You've used today's {SHOT_LIMIT} screenshot tries. Please try again tomorrow.")
        info = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getFile", params={"file_id": file_id}, timeout=15).json()["result"]
        if info.get("file_size", 0) > 8_000_000:
            return tg_send(chat, "That image is too big. Please send the screenshot as a normal photo.")
        img = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{info['file_path']}", timeout=30).content
        mime = "image/png" if info["file_path"].lower().endswith(".png") else "image/jpeg"
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": SHOT_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(img).decode()}"}}]}]
        tg_send(chat, "\U0001F50D Checking your screenshot...")
        raw = (groq(msgs, tools=False, model=VISION_MODEL).get("content") or "")
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
        m = re.search(r"\{.*\}", raw, re.S)
        v = json.loads(m.group(0)) if m else {}
    except Exception as e:
        print("gate photo error:", e)
        return tg_send(chat, "Sorry, I couldn't check that screenshot right now. Please send it again in a minute.", gate_kb(True))
    n = shots_used(uid, add=True)
    left = max(0, SHOT_LIMIT - n)
    handle = str(v.get("handle") or "").strip().lstrip("@").lower()
    clock = str(v.get("clock") or "").strip()
    why = None
    if not v.get("x_profile"):
        why = "this doesn't look like an X (Twitter) profile page."
    elif handle != GATE_HANDLE:
        why = f"this is not the @hackmacos profile" + (f" (I see @{handle})." if handle else ".")
    elif not v.get("following"):
        why = "the button doesn't show \"Following\" yet. Tap Follow on @hackmacos first."
    elif not clock:
        why = "I can't see your phone's clock in the status bar at the top. Take a full screenshot so the time shows."
    elif not clock_fresh(clock):
        why = f"the clock in your screenshot ({clock[:10]}) isn't within 5 minutes of now. Please take a fresh screenshot."
    if why:
        return tg_send(chat, f"\u274C Not verified: {why}\nTries left today: {left}.", gate_kb(True))
    grant_verified(uid, clock)
    GATE.pop(uid, None)
    tg_send(chat, "\u2705 Verified, thank you for following @hackmacos! You now have full access to Tygar.", main_menu())


def gate_intercept(upd):
    """Return True when the update was handled by the verification gate (user not verified yet)."""
    cb = upd.get("callback_query")
    if cb:
        uid = (cb.get("from") or {}).get("id")
        chat = ((cb.get("message") or {}).get("chat") or {}).get("id") or uid
        if not uid or is_verified(uid):
            return False
        threading.Thread(target=gate_callback, args=(cb, chat, uid), daemon=True).start()
        return True
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return False
    uid = (msg.get("from") or {}).get("id")
    chat = (msg.get("chat") or {}).get("id")
    if not uid or not chat or is_verified(uid):
        return False
    if upd.get("edited_message"):
        return True
    forwarded = bool(msg.get("forward_origin") or msg.get("forward_date") or msg.get("forward_from")
                     or msg.get("forward_from_chat") or msg.get("forward_sender_name"))
    file_id = None
    if msg.get("photo"):
        file_id = msg["photo"][-1]["file_id"]          # full size so the status-bar clock is readable
    elif (msg.get("document") or {}).get("mime_type", "").startswith("image/"):
        file_id = msg["document"]["file_id"]
    if file_id:
        threading.Thread(target=gate_photo, args=(chat, uid, file_id, forwarded), daemon=True).start()
    else:
        threading.Thread(target=gate_prompt, args=(chat, uid), daemon=True).start()
    return True


def wat_now():
    import datetime
    return datetime.datetime.utcfromtimestamp(time.time() + WAT)


def parse_reminder(text):
    """'remind me every day at 7am to read' -> (due_epoch, repeat, what) or None."""
    import datetime
    t = re.sub(r"^\s*(/remind|remind me|reminder|set (a )?reminder)\s*(?:(?:to|that|about)\b)?\s*", "", text, flags=re.I).strip()
    rep = ""
    if re.search(r"\b(every ?day|daily|each day|every morning|every night|every evening)\b", t, re.I):
        rep = "daily"
    elif re.search(r"\b(weekdays|every weekday|monday to friday)\b", t, re.I):
        rep = "weekdays"
    m_wd = re.search(r"\bevery (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", t, re.I)
    if m_wd:
        rep = "weekly"
    now = wat_now()
    due = None
    m = re.search(r"\bin (\d+)\s*(min|mins|minutes?|hours?|hrs?|h|days?)\b", t, re.I)
    if m:
        n, u = int(m.group(1)), m.group(2).lower()
        due = now + datetime.timedelta(minutes=n if u.startswith("m") else 0, hours=n if u.startswith("h") else 0,
                                       days=n if u.startswith("d") else 0)
    else:
        m = re.search(r"\b(?:at\s*)?(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)\b|\bat\s*(\d{1,2})[:.](\d{2})\b|\bat\s*(\d{1,2})\b", t, re.I)
        hh = mm = None
        if m:
            if m.group(1):
                hh, mm = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0), int(m.group(2) or 0)
            elif m.group(4):
                hh, mm = int(m.group(4)), int(m.group(5))
            else:
                hh, mm = int(m.group(6)), 0
        elif re.search(r"\bmorning\b", t, re.I):
            hh, mm = 7, 0
        elif re.search(r"\b(evening|night)\b", t, re.I):
            hh, mm = 20, 0
        elif re.search(r"\bnoon|afternoon\b", t, re.I):
            hh, mm = 13, 0
        if hh is None or hh > 23 or mm > 59:
            return None
        due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if re.search(r"\btomorrow\b", t, re.I):
            due += datetime.timedelta(days=1)
        if m_wd:
            want = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"].index(m_wd.group(1).lower())
            due += datetime.timedelta(days=(want - due.weekday()) % 7)
        while due <= now or (rep == "weekdays" and due.weekday() > 4):
            due += datetime.timedelta(days=1 if rep != "weekly" else 7)
    what = re.sub(r"\b(every ?day|daily|each day|every (morning|night|evening|weekday|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|weekdays|"
                  r"monday to friday|tomorrow|today|in \d+\s*\w+|at\s*\d{1,2}([:.]\d{2})?\s*(am|pm)?|\d{1,2}([:.]\d{2})?\s*(am|pm)|morning|evening|night|noon|afternoon)\b",
                  "", t, flags=re.I)
    what = re.sub(r"^\s*(to|that|about|,)\s+", "", re.sub(r"\s{2,}", " ", what)).strip(" ,.") or "your reminder"
    epoch = (due - datetime.datetime(1970, 1, 1)).total_seconds() - WAT
    return epoch, rep, what[:300]


def fmt_due(epoch, rep):
    import datetime
    d = datetime.datetime.utcfromtimestamp(epoch + WAT)
    when = d.strftime("%-I:%M %p")
    if rep == "daily":
        return f"every day at {when}"
    if rep == "weekdays":
        return f"every weekday at {when}"
    if rep == "weekly":
        return f"every {d.strftime('%A')} at {when}"
    day = "today" if d.date() == wat_now().date() else d.strftime("%a %-d %b")
    return f"{day} at {when}"


def tg_send(chat, text, markup=None):
    p = {"chat_id": chat, "text": text[:4000]}
    if markup:
        p["reply_markup"] = markup
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=p, timeout=20)


def reminder_cmd(chat, text):
    p = parse_reminder(text)
    if not p:
        return tg_send(chat, "Tell me when, e.g. \"remind me every day at 7am to read\", \"remind me in 30 minutes to call mum\" "
                             "or \"remind me tomorrow at 6pm to submit my assignment\". Times are Nigerian time (WAT).")
    n = tq("select count(*) from bot_reminders where chat=?", (chat,), fetch=True)[0][0]
    if n >= 20:
        return tg_send(chat, "You already have 20 reminders. Delete some with /reminders first.")
    tq("insert into bot_reminders (chat, body, due, rep) values (?, ?, ?, ?)", (chat, p[2], p[0], p[1]))
    tg_send(chat, f"\u23F0 Got it. I'll remind you to {p[2]} {fmt_due(p[0], p[1])}.\nSee or delete them with /reminders.")


def list_reminders(chat):
    rows = tq("select id, body, due, rep from bot_reminders where chat=? order by due", (chat,), fetch=True)
    if not rows:
        return tg_send(chat, "You have no reminders. Try: remind me every day at 7am to read.")
    lines = [f"{i+1}. {r[1]} ({fmt_due(r[2], r[3])})" for i, r in enumerate(rows)]
    kb = [[{"text": f"\U0001F5D1 Delete {i+1}", "callback_data": f"rmdel:{r[0]}"}] for i, r in enumerate(rows[:10])]
    tg_send(chat, "\u23F0 Your reminders:\n" + "\n".join(lines), {"inline_keyboard": kb})


def todo_cmd(chat, text):
    item = re.sub(r"^\s*(/todo|/add|add to (my )?(to-?do|todo)( list)?:?|todo:?)\s*", "", text, flags=re.I).strip()
    if item and item.lower() not in ("list", "show"):
        n = tq("select count(*) from bot_todos where chat=? and done=0", (chat,), fetch=True)[0][0]
        if n >= 50:
            return tg_send(chat, "Your list is full (50). Tick some off first.")
        tq("insert into bot_todos (chat, body, ts) values (?, ?, ?)", (chat, item[:200], time.time()))
    rows = tq("select id, body from bot_todos where chat=? and done=0 order by id", (chat,), fetch=True)
    if not rows:
        return tg_send(chat, "Your to-do list is empty. Add one with: /todo buy data")
    head = "\u2705 Added. " if item and item.lower() not in ("list", "show") else ""
    kb = [[{"text": f"\u2714 {r[1][:30]}", "callback_data": f"tddone:{r[0]}"}] for r in rows[:15]]
    tg_send(chat, head + "\U0001F4DD Your to-do list (tap to tick off):\n" + "\n".join(f"{i+1}. {r[1]}" for i, r in enumerate(rows)),
            {"inline_keyboard": kb})


def main_menu():
    rows = [[{"text": "\U0001F5BC Image search", "callback_data": "m:image"}, {"text": "\U0001F3AC Photo video", "callback_data": "m:video"}],
            [{"text": "\U0001F50A Text to speech", "callback_data": "m:tts"}, {"text": "\U0001F3A4 Speech to text", "callback_data": "m:stt"}],
            [{"text": "\U0001F4C4 Make a PDF", "callback_data": "m:pdf"}, {"text": "\u23F0 Reminders", "callback_data": "m:remind"}],
            [{"text": "\U0001F4DD To-do list", "callback_data": "m:todo"}, {"text": "\U0001F4AC Live chat", "web_app": {"url": BROWSE_URL.replace("/browse", "/chat")}}],
            [{"text": "\U0001F5A5 Open a website", "callback_data": "m:web"}, {"text": "\U0001F310 Browse the web", "web_app": {"url": BROWSE_URL}}],
            [{"text": "\U0001F3AB Bet code converter", "callback_data": "m:betconv"}]]
    rows += [[{"text": f"\U0001D54F Follow {h} on X", "url": u}] for h, u in X_HANDLES]
    return {"inline_keyboard": rows}


MENU_PROMPTS = {
    "image": "\U0001F5BC Send me what you want a photo of (e.g. Lagos skyline).",
    "video": "\U0001F3AC Send me a topic and I'll make a short photo video (e.g. Enugu city).",
    "tts": "\U0001F50A Send me the text and I'll read it aloud.",
    "stt": "\U0001F3A4 Send me a voice note and I'll write out what was said.",
    "pdf": "\U0001F4C4 What should the PDF be about?",
    "remind": "\u23F0 What should I remind you about, and when? e.g. every day at 7am to read",
    "todo": "\U0001F4DD Send the task to add to your to-do list.",
    "web": "\U0001F5A5 Send a website and I'll open it and show you, e.g. jumia.com.ng",
}


def telegram_callback(cb):
    chat = ((cb.get("message") or {}).get("chat") or {}).get("id")
    data = cb.get("data") or ""
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb.get("id")}, timeout=10)
    if not chat:
        return
    try:
        if data == "m:betconv":
            pending_mode.pop(chat, None)
            betconv.start(chat, tg_send)
        elif data.startswith("bc:"):
            betconv.on_callback(chat, data, tg_send, pending_mode)
        elif data.startswith("m:"):
            k = data[2:]
            if k == "remind" and tq("select count(*) from bot_reminders where chat=?", (chat,), fetch=True)[0][0]:
                list_reminders(chat)
            if k == "todo" and tq("select count(*) from bot_todos where chat=? and done=0", (chat,), fetch=True)[0][0]:
                todo_cmd(chat, "/todo list")
            pending_mode[chat] = (k, time.time())
            tg_send(chat, MENU_PROMPTS.get(k, "Send it over."))
        elif data.startswith("w:") or data.startswith("wl:"):
            web_callback(chat, data)
        elif data.startswith("rmdel:"):
            tq("delete from bot_reminders where id=? and chat=?", (int(data[6:]), chat))
            tg_send(chat, "\U0001F5D1 Reminder deleted.")
            list_reminders(chat)
        elif data.startswith("tddone:"):
            tq("update bot_todos set done=1 where id=? and chat=?", (int(data[7:]), chat))
            tg_send(chat, "\u2714 Ticked off. Nice work!")
            todo_cmd(chat, "/todo list")
    except Exception as e:
        print("callback error:", e)
        tg_send(chat, "Sorry, something went wrong. Please try again.")


def take_mode(chat):
    m = pending_mode.pop(chat, None)
    return m[0] if m and time.time() - m[1] < 900 else None


def reminder_loop():
    import datetime
    time.sleep(20)
    while True:
        try:
            now = time.time()
            for rid, chat, body, due, rep in tq("select id, chat, body, due, rep from bot_reminders where due<=?", (now,), fetch=True) or []:
                if rep:
                    nxt = due
                    while nxt <= now:
                        nxt += 7 * 86400 if rep == "weekly" else 86400
                        if rep == "weekdays":
                            while datetime.datetime.utcfromtimestamp(nxt + WAT).weekday() > 4:
                                nxt += 86400
                    claimed = tq("update bot_reminders set due=? where id=? and due=? returning id", (nxt, rid, due), fetch=True)
                else:
                    claimed = tq("delete from bot_reminders where id=? and due=? returning id", (rid, due), fetch=True)
                if claimed and now - due < 6 * 3600:
                    tg_send(chat, f"\u23F0 Reminder: {body}")
        except Exception as e:
            print("reminder loop error:", e)
        time.sleep(30)


if TG_TOKEN:
    threading.Thread(target=reminder_loop, daemon=True).start()


# ---------- /open and /record: read-only web viewer with screenshots and screen video ----------
_pw_lock = threading.Lock()
_pw_ready = [False]
last_page = {}        # chat -> {"url": str, "y": int, "links": [(text, href)]}
WEB_LIMITS = {"open": 25, "rec": 4}


def ensure_browser():
    """Download Chromium (headless shell) on first use; no system packages needed."""
    if _pw_ready[0]:
        return True
    import subprocess, sys
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium", "--only-shell"],
                       check=True, capture_output=True, timeout=300)
        _pw_ready[0] = True
    except Exception as e:
        print("playwright install error:", str(getattr(e, "stderr", None) or e)[-500:])
    return _pw_ready[0]


def safe_url(u):
    import ipaddress, socket
    from urllib.parse import urlparse
    u = u.strip().strip("<>")
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname or "." not in p.hostname:
        return None
    try:
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return None
    except Exception:
        return None
    return u


def web_quota(chat, kind):
    day = time.strftime("%Y-%m-%d")
    k = (chat, "web" + kind, day)
    if _media_use.get(k, 0) >= WEB_LIMITS[kind]:
        return False
    _media_use[k] = _media_use.get(k, 0) + 1
    return True


def _launch(p):
    return p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                                   "--single-process", "--no-zygote", "--js-flags=--max-old-space-size=128"])


def shoot_page(url, y=0, record=False):
    """Open url (read-only), scroll to y. Returns dict with png, title, text, links, final url, video."""
    import tempfile
    from playwright.sync_api import sync_playwright
    out = {}
    vdir = tempfile.mkdtemp() if record else None
    with sync_playwright() as p:
        b = _launch(p)
        try:
            opts = dict(viewport={"width": 1366, "height": 768}, device_scale_factor=1,
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 TygarsBot")
            if record:
                opts.update(device_scale_factor=1, record_video_dir=vdir, record_video_size={"width": 412, "height": 860})
            ctx = b.new_context(**opts)
            ctx.route("**/*", lambda r: r.abort() if r.request.resource_type in ("media", "websocket") else r.continue_())
            pg = ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_timeout(2500)
            if record:
                for _ in range(5):
                    pg.mouse.wheel(0, 500)
                    pg.wait_for_timeout(1100)
                pg.wait_for_timeout(600)
            elif y:
                pg.evaluate(f"window.scrollTo(0, {int(y)})")
                pg.wait_for_timeout(900)
            out["png"] = pg.screenshot(type="jpeg", quality=70)
            out["title"] = (pg.title() or "")[:120]
            out["url"] = pg.url
            out["text"] = pg.evaluate("document.body ? document.body.innerText : ''")[:5000]
            out["links"] = pg.evaluate("""() => [...document.querySelectorAll('a[href^="http"]')]
                .map(a => [(a.innerText||'').trim().replace(/\\s+/g,' ').slice(0,40), a.href])
                .filter(x => x[0].length > 2).slice(0, 40)""")
            vpath = pg.video.path() if record else None
            ctx.close()
            if record and vpath:
                out["video"] = to_mp4(vpath)
        finally:
            b.close()
    return out


def to_mp4(webm):
    import subprocess, imageio_ffmpeg
    mp4 = webm.rsplit(".", 1)[0] + ".mp4"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", webm, "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "28", "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-movflags", "+faststart", mp4],
                   check=True, capture_output=True, timeout=120)
    return open(mp4, "rb").read()


def page_summary(title, text):
    try:
        msgs = [{"role": "system", "content": "Summarize this web page for a phone user in 2-4 short sentences: what the site is and the main things on screen. Plain text, no markdown."},
                {"role": "user", "content": f"Title: {title}\n\n{text[:3500]}"}]
        out = (groq(msgs, tools=False).get("content") or "").strip()
        return re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()[:700]
    except Exception as e:
        print("summary error:", e)
        return ""


def web_buttons(chat):
    lp = last_page.get(chat) or {}
    rows = [[{"text": "\u2B07 Scroll down", "callback_data": "w:down"}, {"text": "\U0001F517 Links", "callback_data": "w:links"}],
            []]
    if lp.get("url"):
        rows[1].append({"text": "\U0001F310 Open in browser", "url": lp["url"]})
    return {"inline_keyboard": rows}


SIGNUP_ASK = re.compile(r"\b(register|sign ?up|log ?in|create (an )?account|fill|enter my|my (number|phone|password|otp|pin|bvn))\b", re.I)


def microlink_shot(url):
    """Fallback when Chromium can't run on the server: free Microlink screenshot (about 50 a day)."""
    r = requests.get("https://api.microlink.io/", timeout=60, params={
        "url": url, "screenshot": "true", "meta": "true", "viewport.width": 1366, "viewport.height": 768})
    j = r.json()
    if j.get("status") != "success":
        raise RuntimeError(j.get("message") or "microlink failed")
    d = j["data"]
    png = requests.get(d["screenshot"]["url"], timeout=60).content
    return {"png": png, "title": (d.get("title") or "")[:120], "url": d.get("url") or url,
            "text": (d.get("description") or ""), "links": [], "fallback": True}


_webcheck = {}


def _run_webcheck():
    if not _pw_lock.acquire(blocking=False):
        return
    try:
        _webcheck["status"] = "checking"
        _webcheck["install"] = ensure_browser()
        try:
            r = shoot_page("https://example.com")
            _webcheck["ok"] = bool(r.get("png"))
        except Exception as e:
            _webcheck["ok"] = False
            _webcheck["error"] = str(e).splitlines()[0][:200]
            m = re.search(r"error while loading shared libraries: (\S+)", str(e))
            if m:
                _webcheck["missing"] = m.group(1)
    except BaseException as e:
        _webcheck["ok"] = False
        _webcheck["error"] = repr(e)[:200]
    finally:
        _webcheck["status"] = "done"
        _pw_lock.release()


@app.get("/webcheck")
def webcheck():
    """Tells whether the web viewer's browser works on this server (no page content returned). Never blocks."""
    if not _webcheck:
        threading.Thread(target=_run_webcheck, daemon=True).start()
        return {"status": "started, check again in 1-2 minutes"}
    return dict(_webcheck)


threading.Thread(target=lambda: (time.sleep(20), _run_webcheck()), daemon=True).start()


def telegram_open(chat, text, record=False, y=0, url=None):
    arg = text.split(None, 1)[1].strip() if text and len(text.split(None, 1)) > 1 else ""
    if arg and SIGNUP_ASK.search(arg) and not re.match(r"^\S+\.\S+$", arg):
        return tg_send(chat, "I can show you a site and explain the steps, but I never sign up, log in or type phone numbers, "
                             "passwords or codes for anyone. Keep those private. Send /open with the website, e.g. /open jumia.com.ng")
    target = url or (re.search(r"\S+\.\S+", arg).group(0) if re.search(r"\S+\.\S+", arg) else "")
    if not target:
        return tg_send(chat, "Send a website after the command, e.g. /open jumia.com.ng")
    u = safe_url(target)
    if not u:
        return tg_send(chat, "That link doesn't look like a public website.")
    if not web_quota(chat, "rec" if record else "open"):
        return tg_send(chat, "You've reached today's limit for that. Try again tomorrow.")
    if not _pw_lock.acquire(blocking=False):
        tg_send(chat, "\u23F3 Someone else is using the web viewer, you're next...")
        _pw_lock.acquire()
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendChatAction",
                      json={"chat_id": chat, "action": "record_video" if record else "upload_photo"}, timeout=5)
        if not _pw_ready[0] and _webcheck.get("ok") is not False:
            tg_send(chat, "\U0001F310 Starting the web viewer (first time takes about a minute)...")
        r = None
        if _webcheck.get("ok") is not False and ensure_browser():
            try:
                r = shoot_page(u, y=y, record=record)
            except Exception as e:
                print("open error:", str(e)[:300])
                if "shared libraries" in str(e) or "launch" in str(e):
                    _webcheck["ok"] = False
        if r is None and not y:
            try:
                r = microlink_shot(u)
            except Exception as e:
                print("microlink error:", e)
        if r is None:
            return tg_send(chat, "Sorry, that page didn't load. It may be down or blocking bots.")
    finally:
        _pw_lock.release()
    last_page[chat] = {"url": r["url"], "y": y, "links": r.get("links", []), "fallback": r.get("fallback")}
    if record and r.get("video"):
        resp = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendVideo",
                             data={"chat_id": chat, "caption": f"\U0001F3A5 {r['title'] or r['url']}"[:1000], "supports_streaming": "true",
                                   "reply_markup": json.dumps(web_buttons(chat))},
                             files={"video": ("screen.mp4", r["video"], "video/mp4")}, timeout=120)
    else:
        summ = page_summary(r["title"], r["text"]) if not y else ""
        cap = f"\U0001F310 {r['title'] or r['url']}" + (f"\n\n{summ}" if summ else "")
        resp = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                             data={"chat_id": chat, "caption": cap[:1000], "reply_markup": json.dumps(web_buttons(chat))},
                             files={"photo": ("page.jpg", r["png"], "image/jpeg")}, timeout=60)
    if resp.status_code >= 300:
        print("web send error:", resp.status_code, resp.text[:300])
        tg_send(chat, "Sorry, I couldn't send that.")
    else:
        remember(chat, text or u, f"[showed the web page {r['url']}: {r['title']}]")


def web_callback(chat, data):
    lp = last_page.get(chat)
    if not lp:
        return tg_send(chat, "Open a page first, e.g. /open jumia.com.ng")
    if data == "w:down" and lp.get("fallback"):
        return tg_send(chat, "Scrolling isn't available for this page right now. Tap Open in browser to see more.")
    if data == "w:down":
        return telegram_open(chat, "", y=lp["y"] + 700, url=lp["url"])
    if data == "w:links":
        links = lp.get("links") or []
        if not links:
            return tg_send(chat, "I didn't find links on that page.")
        lp["pick"] = links[:10]
        kb = [[{"text": t[:40], "callback_data": f"wl:{i}"}] for i, (t, h) in enumerate(links[:10])]
        return tg_send(chat, "Tap a link to open it:", {"inline_keyboard": kb})
    if data.startswith("wl:"):
        pick = lp.get("pick") or []
        i = int(data[3:])
        if i < len(pick):
            return telegram_open(chat, "", url=pick[i][1])


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
betconv.init(_launch, ensure_browser, _pw_lock)


@app.get("/gatecheck")
def gatecheck():
    """Is the verification gate's table reachable? Returns only a count, no user data."""
    try:
        n = tq("select count(*) from bot_verified", fetch=True)[0][0]
        return {"gate": "on", "table_ok": True, "verified_users": n, "shot_limit": SHOT_LIMIT}
    except Exception as e:
        return {"gate": "on", "table_ok": False, "error": type(e).__name__}
