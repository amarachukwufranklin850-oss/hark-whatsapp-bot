import os
import requests
from flask import Flask, request

app = Flask(__name__)

VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]        # any secret word you make up
WA_TOKEN = os.environ["WHATSAPP_TOKEN"]          # Meta access token
PHONE_ID = os.environ["PHONE_NUMBER_ID"]         # from WhatsApp > API Setup
GROQ_KEY = os.environ["GROQ_API_KEY"]            # free key from Groq
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GRAPH = os.environ.get("GRAPH_VERSION", "v23.0")

SYSTEM = (
    "You are the WhatsApp assistant for Unofficial Hark Fan Africa, a fan community "
    "that is not affiliated with Hark. Hark is a personal AI that gets real things done: "
    "it works across email, calendar and files, uses a real browser to book, order and "
    "fill forms, remembers what matters to you, builds pages, and runs on Android, iPhone, "
    "web and Mac. The Hark Pro plan is free with no ads. Answer questions about Hark and "
    "the fan community in short, friendly messages. If you are not sure, say so and point "
    "people to hark.com. Stay on these topics."
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


def ask_ai(user, text):
    past = history.setdefault(user, [])
    past.append({"role": "user", "content": text})
    del past[:-10]
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json={"model": MODEL,
                  "messages": [{"role": "system", "content": SYSTEM}] + past,
                  "max_tokens": 400},
            timeout=30,
        )
        r.raise_for_status()
        reply = r.json()["choices"][0]["message"]["content"].strip()
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
