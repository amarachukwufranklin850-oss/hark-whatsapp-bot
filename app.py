import os
import json
import time
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
}]


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
        sys_msg = SYSTEM + f" Today is {today}. Your built-in knowledge is old, so ALWAYS call web_search before answering anything about recent events, winners, prices, news, or people's current roles."
        msgs = [{"role": "system", "content": sys_msg}] + past
        m = groq(msgs)
        found = []
        rounds = 0
        while m.get("tool_calls") and rounds < 1:
            rounds += 1
            msgs.append({"role": "assistant", "tool_calls": m["tool_calls"]})
            for c in m["tool_calls"]:
                try:
                    q = json.loads(c["function"]["arguments"]).get("query", text)
                except Exception:
                    q = text
                res = web_search(q)
                found.append(f"Search: {q}\n{res}")
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": res})
            m = {"tool_calls": True}
        if m.get("tool_calls"):
            # final answer from what was found, with tools switched off entirely
            final = [{"role": "system", "content": sys_msg}] + past[:-1] + [{
                "role": "user",
                "content": text + "\n\nWeb search results:\n" + "\n\n".join(found)
                           + "\n\nAnswer using these results; prefer the newest dated items from trusted outlets, give scores/dates exactly, name the source and date, and if nothing confirms it say you couldn't verify it. Plain text, short."}]
            try:
                m = groq(final, tools=False)
            except Exception:
                m = groq(final, tools=False, model=FINAL_MODEL)
        reply = (m.get("content") or "").strip() or "Sorry, I couldn't find an answer."
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
    if chat and file_id:
        threading.Thread(target=telegram_photo_reply,
                         args=(chat, file_id, msg.get("caption") or ""), daemon=True).start()
    elif text and chat:
        threading.Thread(target=telegram_reply, args=(chat, text), daemon=True).start()
    return "ok", 200


VISION_MODEL = "qwen/qwen3.8-27b"


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
    if text.strip() == "/start":
        reply = ("Hey, I'm Tygars Bot \U0001F42F Ask me anything: school work, tech, "
                 "business ideas, news, or everyday questions. I'll keep it quick and clear.")
    else:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendChatAction",
                          json={"chat_id": chat, "action": "typing"}, timeout=5)
        except Exception:
            pass
        reply = ask_ai(f"tg:{chat}", text)
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": chat, "text": reply}, timeout=20)
    if r.status_code >= 300:
        print("Telegram error:", r.status_code, r.text)


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
