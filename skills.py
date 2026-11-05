"""Tygar skills: alerts, memory, files, study, money and everyday tools.

Hooked into app.py:
  * handle_tg(chat, text)          Telegram text (after the verification gate)  -> True when handled
  * tg_document(chat, doc, caption) Telegram PDF/DOCX uploads
  * on_callback(chat, uid, data)    inline buttons whose data starts with "sk:"
  * web(uid, text)                  web chat (agent.py)                          -> dict or None
  * prompt_extra(user, text)        extra system text for ask_ai (facts, lite mode, open document)
  * tick()                          called from the 30-second reminder loop (runs work in one background thread)
Everything is free and keyless: trongrid, blockstream, blockscout, open.er-api, CoinGecko, open-meteo, ESPN,
bible-api.com, alquran.cloud. Never asks for or stores seed phrases or private keys.
"""
import base64
import datetime
import hashlib
import html as _html
import io
import json
import math
import os
import random
import re
import threading
import time
import zlib

import requests
from flask import Blueprint, Response

bp = Blueprint("skills", __name__)
H = {}                     # helpers from app.py (init)
WAT = 3600                 # West Africa Time, UTC+1, no DST
BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "Harkaiafricabot")
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
BOT_UA = {"User-Agent": "TygarsBot/1.0 (fan-made Telegram bot; contact harkbotofficial@gmail.com)"}


def init(**kw):
    H.update(kw)


# =====================================================================================
# Database (Neon via hangout.q; SQLite locally). All tables are created on first use.
# =====================================================================================
TABLES = [
    "create table if not exists sk_facts (id bigserial primary key, uk text, fact text, ts double precision)",
    "create table if not exists sk_wallets (id bigserial primary key, uk text, chain text, addr text, last_ts double precision default 0, seen text default '', checked double precision default 0, added double precision)",
    "create table if not exists sk_alerts (id bigserial primary key, uk text, kind text, base text, quote text, op text, target double precision, created double precision)",
    "create table if not exists sk_briefing (uk text primary key, hhmm text, city text, last_day text default '')",
    "create table if not exists sk_pages (id bigserial primary key, uk text, url text, hash text default '', excerpt text default '', checked double precision default 0, added double precision)",
    "create table if not exists sk_follows (id bigserial primary key, uk text, team text, added double precision)",
    "create table if not exists sk_match (uk text, event text, status text, score text, primary key (uk, event))",
    "create table if not exists sk_spend (id bigserial primary key, uk text, amount double precision, cat text, ts double precision)",
    "create table if not exists sk_flags (uk text, k text, v text, primary key (uk, k))",
    "create table if not exists sk_webpages (id text primary key, uk text, html text, created double precision)",
    "create table if not exists sk_jobs (uk text primary key, field text, city text, last_day text default '')",
    "create table if not exists sk_docs (uk text primary key, name text, body text, ts double precision)",
    "create table if not exists sk_ajo (id bigserial primary key, uk text, name text, amount double precision, freq text, members text, rnd int default 1, paid text default '[]', created double precision)",
    "create table if not exists sk_study (uk text primary key, exam text, exam_date text, subjects text, hhmm text, created double precision)",
    "create table if not exists sk_verse (uk text primary key, book text, hhmm text, last_day text default '')",
    "create table if not exists sk_feedback (uk text, msg_id bigint, vote int, ts double precision, primary key (uk, msg_id))",
    "create table if not exists sk_referrals (referee text primary key, referrer text, ts double precision)",
    "create table if not exists sk_jobs_state (k text primary key, v text)",
    "create table if not exists sk_push (uk text primary key, sub text, ts double precision)",
    "create table if not exists sk_mail_block (email text primary key, reason text, ts double precision)",
    "create table if not exists sk_mail_log (id bigserial primary key, uk text, email text, kind text, ts double precision)",
    "create table if not exists sk_reminders (id bigserial primary key, uk text, body text, due double precision, rep text default '', created double precision)",
]
_ready = [False]
_db_lock = threading.Lock()


def q(sql, args=(), fetch=False):
    from hangout import q as hq
    if not _ready[0]:
        with _db_lock:
            if not _ready[0]:
                for s in TABLES:
                    if not os.environ.get("DATABASE_URL"):
                        s = (s.replace("bigserial primary key", "integer primary key autoincrement")
                              .replace("double precision", "real").replace("bigint", "integer"))
                    hq(s)
                _ready[0] = True
    return hq(sql, args, fetch=fetch)


def flag(uk, k, v=None):
    """Read (v=None) or write a small per-user setting."""
    if v is None:
        r = q("select v from sk_flags where uk=? and k=?", (uk, k), fetch=True)
        return r[0][0] if r else None
    q("insert into sk_flags (uk, k, v) values (?, ?, ?) on conflict (uk, k) do update set v=excluded.v", (uk, k, str(v)))
    return v


def unflag(uk, k):
    q("delete from sk_flags where uk=? and k=?", (uk, k))


def state(k, v=None):
    if v is None:
        r = q("select v from sk_jobs_state where k=?", (k,), fetch=True)
        return r[0][0] if r else None
    q("insert into sk_jobs_state (k, v) values (?, ?) on conflict (k) do update set v=excluded.v", (k, str(v)))


# =====================================================================================
# Identity, gate, limits
# =====================================================================================
def chat_of(uk):
    return int(uk[3:]) if uk.startswith("tg:") and uk[3:].lstrip("-").isdigit() else None


def is_dev(uk):
    c = chat_of(uk)
    return bool(c and c in (H.get("dev_ids") or set()))


def gate_ok(uk):
    """Telegram: the existing captcha gate (developers exempt inside is_verified).
    Web: the web app's own daily message cap applies (agent.py CHAT_DAILY)."""
    c = chat_of(uk)
    if c is None:
        return True
    if c < 0:                      # group chat: the sender was already checked by gate_intercept
        return True
    f = H.get("is_verified")
    return bool(f(c)) if f else True


LIMITS = {  # per user per day (WAT); developers are exempt
    "wallet": 10, "alert": 20, "weather": 40, "ics": 20, "units": 100, "page": 10, "follow": 10,
    "briefing": 10, "memory": 40, "spend": 60, "doc": 15, "xlsx": 10, "chart": 10, "slides": 5, "webpage": 3,
    "translate": 3, "yt": 10, "recipe": 10, "quiz": 10, "cv": 3, "invoice": 10, "qr": 20, "jobs": 5, "scam": 15,
    "solve": 20, "study": 5, "flash": 10, "check": 15, "bizname": 10, "logo": 3, "caption": 15, "calc": 60,
    "split": 40, "ajo": 40, "health": 20, "verse": 10, "fuel": 10, "remind": 30,
}
_use = {}


def wat_now():
    return datetime.datetime.utcfromtimestamp(time.time() + WAT)


def wat_day():
    return wat_now().strftime("%Y-%m-%d")


def quota(uk, feat):
    if is_dev(uk):
        return True
    k = (uk, feat, wat_day())
    if _use.get(k, 0) >= LIMITS.get(feat, 20):
        return False
    _use[k] = _use.get(k, 0) + 1
    if len(_use) > 50000:
        today = wat_day()
        for kk in [kk for kk in _use if kk[2] != today]:
            _use.pop(kk, None)
    return True


def over(feat):
    return R(f"You've reached today's limit for this ({LIMITS.get(feat, 20)} a day). Try again tomorrow.")


# =====================================================================================
# Replies: one shape for Telegram and web
# =====================================================================================
def R(text, markup=None, file=None, photo=None):
    """file = (name, bytes, mime); photo = png bytes."""
    return {"text": text, "markup": markup, "file": file, "photo": photo}


def tg_api(method):
    return f"https://api.telegram.org/bot{H.get('token', '')}/{method}"


def send(chat, text, markup=None):
    p = {"chat_id": chat, "text": (text or "")[:4000]}
    if markup:
        p["reply_markup"] = markup
    try:
        r = requests.post(tg_api("sendMessage"), json=p, timeout=20)
        return r.json().get("result", {}).get("message_id") if r.ok else None
    except Exception as e:
        print("skills send error:", e)


def send_file(chat, name, data, mime, caption="", markup=None):
    d = {"chat_id": chat, "caption": caption[:1000]}
    if markup:
        d["reply_markup"] = json.dumps(markup)
    r = requests.post(tg_api("sendDocument"), data=d, files={"document": (name, data, mime)}, timeout=60)
    if r.status_code >= 300:
        print("skills sendDocument error:", r.status_code, r.text[:200])


def send_photo(chat, png, caption="", markup=None):
    d = {"chat_id": chat, "caption": caption[:1000]}
    if markup:
        d["reply_markup"] = json.dumps(markup)
    r = requests.post(tg_api("sendPhoto"), data=d, files={"photo": ("image.png", png, "image/png")}, timeout=60)
    if r.status_code >= 300:
        print("skills sendPhoto error:", r.status_code, r.text[:200])


def typing(chat, action="typing"):
    if not chat:
        return
    try:
        requests.post(tg_api("sendChatAction"), json={"chat_id": chat, "action": action}, timeout=5)
    except Exception:
        pass


def deliver(chat, r):
    if not r:
        return
    if r.get("photo"):
        send_photo(chat, r["photo"], r.get("text") or "", r.get("markup"))
    elif r.get("file"):
        n, data, mime = r["file"]
        send_file(chat, n, data, mime, r.get("text") or "", r.get("markup"))
    else:
        send(chat, r.get("text") or "", r.get("markup"))


def chips_of(markup):
    """Telegram inline keyboard -> web chips [{"text", "data"} | {"text", "url"}]."""
    out = []
    for row in (markup or {}).get("inline_keyboard", []):
        for b in row:
            if b.get("callback_data"):
                out.append({"text": b["text"], "data": b["callback_data"]})
            elif b.get("url"):
                out.append({"text": b["text"], "url": b["url"]})
            elif (b.get("web_app") or {}).get("url"):
                out.append({"text": b["text"], "url": b["web_app"]["url"]})
    return out


def to_web(r):
    out = {"reply": r.get("text") or ""}
    ch = chips_of(r.get("markup"))
    if ch:
        out["chips"] = ch
    if r.get("photo"):
        out["image"] = "data:image/png;base64," + base64.b64encode(r["photo"]).decode()
    if r.get("file"):
        n, data, mime = r["file"]
        if mime == "application/pdf":
            out["pdf"], out["pdf_name"] = "data:application/pdf;base64," + base64.b64encode(data).decode(), n
        else:
            out["file"], out["file_name"] = f"data:{mime};base64," + base64.b64encode(data).decode(), n
    return out


# =====================================================================================
# LLM helpers (existing Groq client from app.py)
# =====================================================================================
def llm(system, user, model=None):
    m = H["groq"]([{"role": "system", "content": system}, {"role": "user", "content": user[:12000]}], tools=False, model=model)
    out = re.sub(r"<think>.*?</think>", "", m.get("content") or "", flags=re.S)
    return out.replace("**", "").strip()


def parse_json(text):
    """First JSON object/array inside an LLM answer."""
    text = re.sub(r"^```(json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    for o, c in (("{", "}"), ("[", "]")):
        i = text.find(o)
        if i < 0:
            continue
        depth, ins, esc = 0, False, False
        for j in range(i, len(text)):
            ch = text[j]
            if ins:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    ins = False
            elif ch == '"':
                ins = True
            elif ch == o:
                depth += 1
            elif ch == c:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i:j + 1])
                    except Exception:
                        break
    return None


def llm_json(system, user, model=None):
    out = parse_json(llm(system + " Reply with ONLY valid JSON, no other text.", user, model))
    if out is None:
        out = parse_json(llm(system + " Reply with ONLY valid JSON, no markdown, no comments.", user, model))
    return out


def wsearch(query):
    try:
        return H["web_search"](query)
    except Exception as e:
        print("skills search error:", e)
        return ""


def money(x, cur="\u20a6"):
    x = float(x)
    s = f"{x:,.2f}" if abs(x) < 100 and x != int(x) else f"{x:,.0f}"
    return f"{cur}{s}"


def parse_amount(s):
    """'3000', '3,000', '\u20a63k', 'N2.5m', '1.2b', 'NGN 4500' -> float or None."""
    m = re.search(r"(?:\u20a6|ngn|n|\$|usd)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|m|b|thousand|million|billion)?\b", (s or "").lower())
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    mult = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}.get(m.group(2) or "", 1)
    return v * mult


# =====================================================================================
# 1. Seed phrase / private key guard (never stored, never sent to the AI)
# =====================================================================================
_BIP39_Z = "eNotmwmWrCoQRLdSW3NApUvBz1B29ep/3OCdPhWJighJkhP0NE9pzek1zfGM7St6BkHuDfxQriFxUXOZIa1Mi697WUV6VZ1lCbVC4urKy5L7oOPxEQNN6bEg99riosJ/PRbuluyXqd8ifVlaLmAZrbY+na9pnW7VWFd+cXFxPF9/1KDIFcF+gp8pLQEaIaHkmS9u2xQLJNP1rUz0Z5+iPrkHfvR5L0HlI0x6Fq+X34jlzkXPYoU751R0/5w7uOQjq3dn4PkZg9o6uT7DF8yP8Mp08MyJl+9jEha1T4WaBS3oG+czfTWaa2qhF+hfTLtoHmh+XmKnupWm80uLaTlgVFqie5522kk7fUx7UfMpXrAuvX0rqZHFBTM05Xb4hfqYtJDSBI3/dWr9xoBAJP3uidHd+cw7V3eYCoRW77tYTO4S1WZZDsDzqyECtFl2WoRr5WIA5aLf5VJjRZ3iTqH70MDISokfX8E/vl1a2CwipUVXaE8u79dU+d2BJ7VOnvxag9H1au2XGqrtuNSPdpyh6bJldaW1aXlDAh1oLba+8uyfgPd/wthX5KrvFjLPQm9me28Z6JcqfUKxCH3yMq0Zimg90zuAxajBPqFmuvNsXcx63nqkar+xvuZp/gqWI5xqWwV9ep5WtTlPu36nBXpG3hIVJWPzdM05iyT9QVLgTf/CSaVi2Z5hKtUrDVRNjfAtHs0aq6ZwDvosmICuOZ/DMrFwRenuHMIm0KqhvNNkODRkSBTv5nB6fYtquoRqOiS3mcIWuaqAGEvbDXEXeUKgpW+mibh8F/oSKb8p+O6QuDkWLkpTk3G8fjJ3Qs2Y8DImD+qc+NgZJp6jIObTbZ05G6umAOoBnt1Ag72q9cyECNVC1vqcs4R6ztcs0OIVdLWX8xvgM1Ik9CYX1qoI8jqjzubcELJ5LLk5/+qn9iRa7mTxxEjmV5D6xQxFLYDhj4vIGEu0FJTIJJS4H7zuz5VYeZ6lbc9I4Q1DS05+OfP5Mta46MMjj7HP6Pi5rwywq3E12KUaT8lSj6e+388ZeAOnH6fVr4jDakyKnw/1so8rGAFfeo3JDO+VlsdE9a/x7++1TPPMIhHV4BdbmgXtXkV8/w1IsgWX4NKigtwCXpDaA5dgot4v6DQ/ybyZPhNNpa/v3f8+cMfm9+42+bOF3+wqmmvBnsFb4xQptFooVl4WvwQxUaU2t1e7m6OKGs47dKFaC3v2621U7EzVWEhCK5OFSV6CTK3mT+MI1A8X2nsJqcIJlbvvShROyOj2gR0SalKE1+3WDutMkVzBG4aLlnGzGtWybNkNLrwbgu9LmAQe7sHqlIleEB9MNY+QA+GVAjUyZlQk+11JmJTIcvQFuyKaaLgXvR13+BtlSC46GAuLeokt/tE2DsYSP1pTy4ltFd5AidsX+gCU0EVCKXjhh1GdrIXltKET2TZQ61LozpyRlrIrMSenu3pK/MG+gg+Ndt7pyJRWvBl2ds9fngbCjCwt26FSL0vetgCh25m5yFoUC09R1UIsgBSlpJpql2HDXxClb/kyM/J1Y0zVNLMKXbubSVuUaRTd7dCokEb7qUbUiwqtZL6ePhF1sqCBBNySMabGTcMFiUFLC/ig9L7b8ZCkj7jXPUrZMaY+d2y4SKkm1cMzw/M3y1IuKCyQ9S+yNfACkm+wQMpkPpbpOcE/NSzhpVLAdooE2giaXlQaC61E2CQFdoO4C/ICKaM7he6llJbmrfQlMq7SgzHS1dIvr+7Sk2t294MPyzGidp/1VO5Ahxv95jeU+9JLzCy0Lu4gTP3fCuuFtamGvLQkG9KfS4cHtkyrFPMqr2ynKPFZbZDX4XStkzXyCjtWL3zflMCtkueVlbwGmdsA0ahFFkyXSLhm6gZcZ8iJFInmMqrDykrBtTZpCS62UWujcTusazj9pTN+XPHCtIhEV0/RPUj2nUSpaZ9OJLjinWv0pRaMEC9AoocTtoaqKZpdCH6lxj1B3sCNYhKVhPKKeMmXGoK3aumemlVR9IcIIrVGudiSjtV9iiiKFarZW10t6js8kZysWugMJ+5W4qIJHbLGM1zy59Zon0cky/ejYIEXAepw5VUYIq3CYGVklmIVbyp02HAVbrNQLBpTCydp5+OZiZ9cfPdP8r1mhygi3cp7lcpZpRaAWz6R6DWZJM9iluHkreSXDHQkd4R4xX9evbaEu6RPhNFpdbEyRB8BK2pEPKv0JXUjH5TgvcEbcENwXOtDoE9KRwukKNeOxPQhp/iyq9Xe6pmWH7q91q+0tr4XJkRaqL7J0ZcnKUyAZEMcjL6jFgQqySwIhqMmmrK8+oDL4sUf1ANYEDZrxLDvr2AfJkR7JuGc5TKFc3VZs1fowRl2xSFQs1dUUdMofCY4Lzmn0VNc1AKaZChE8NhEpLK4GzCD4co2vOG6z8zTOxPsiGrYCk3gv4iEhmUQcHPEYNGMRpT7ymAkY4wtbZaAkHaUgAhrMKTD0hLSD+2nkxUWFGnt4lXSYEwQDex7oSK6gaU4LgpveZ3o8pY0StjCf/gYQs0qPpB+9Kb4mVSkRyRfU+2UrqhYK3TidUVAXzC4R5JjeKTP4TiFJguvoSHNfowZ1op8U8ynJCf8mg+/0m5w5dcxvcjwMlQwy39lL1dTHJvwG6wjRcsSfeOYkC7RODP/v9GNmS2/vpER6vB7o6FEgj96mx2/WoJaNqLZbd1l9MEhmgi8+AZ+eLSvbcJHFtF4BFL3X9GViyhpEUpiNzxKQeX2ZUCCN2LqKpL4LdxIDYHeiFO3adiBbaIZdI8QeRWJBLObY81NgV6BLVuYbGo2dayjyrYgiea1wE3FvAKuFI9zgxnYNEPx4zqsxM2OziYruUVMwhZHALpFrQ7h7vbhpuACmmsxuRsRjm2CYB+30XJb9DsMKOKmb9KFru5nXDd77Fv8fW2nAs3NsZSQCieDPxmiCOM4vXI3fK3tzH6KxyV0FS+t7eyK4TaHUxt8ztJbmxSlviItueFGbZkRZWdHNsIygRrz8iLKrL4gMNkI8tEcaC/RTlvFYfMmV8Ft2YvbnEbYFGdt0qHmUvFQipYROkQFeqQwinqoQR7SI7mzPMZL2hwnCXFXN/kauoXbsfXET3Zzk5trccNJ37onfZ8cRDnY3hWp/34hdu53+rDLy5VfjNYQmf9RYigRObYQ67ld8Yt+1G7UscTt8ie4UFC3o4o0f6JR/BQpvkm8IdIRgF3cc6cOBrRH1OaOvdjjjkbfh4DsUQZn47KoPQzHrmAaQDRFilGysZ/YP+F1V2iejfKOhAzxzH5bc7kTTu8Ixp7XFcHamemdKd4dPChCktWaROstvu7MoYaLfRY3YL58gxmgF46ShQyhoOF2x8rCD26AzDufKuQRdoXJgIIXIXfz4gmQL3mD6l0he7N3hEdLmIa6h6agl/tyMcQXzfT+vV7HhPZy1KWgaxNcsigQi9uB8lLEJbf7IJrkTuFOkZAJP8gwwZfzJMf0vAV/rkKELzilmg5sKvhRM9jMQ/J4DPt4BC0P8Aq+uAVJv6KbcUV4DtUTSLsdaLtDjDxY74d0LRNz5HkGF3yPg4kQBCDilR5j/R0ELEdOrpOpc3Ot6TiyTcxhUyhpGoG0xeqQI0ex8xi1dSisOpDTo8vzBLGsIjSglalIAEpS8mAWFC2urlB8By70OpupXzIeLzzBSBJMMzS9nFYmSIy8JS+QIIeR6ydXwdSKLNpBj1e07YuaM/xlUfSHJBgLJ5J9c2QuRTvGIRJJCGzeROGLyHC/pV/DLxjtzaiAyhORrdMIYsJ6QE5S0xFX4RI50PYiAZGUSm74vDH9BFf66X71Gi3iyQozGQAV5IILSZFTpxJuiYwXCQshNpqiDRMnGfDo8C+QPhWy4DPKn+iPDEsfC4ytJ/yWx3G6A7Vq8aqW+v1BfH5GqupnYsWI8Pv7e/1IdDGcogk8peN/wiNx+MmzflqxPzgVPxIOhAq36Icsk5BJ/SH2/unOUotEsfGHBIKT+G+5GlPJ+fVmTQtugYxjh35fb5IAb0mHfsnXiXLa5W6Lav7f4rN+C2vlzZDf5AspP/H1TjJi7xQ3EMMlfF6n9I1+GoBQvZEaxBk9yc+fpKROwj3xSnzYubpb5rr4orlq06hP4j4w4XjLbE4vMhonUaCgYkwUYLi+QohTeoBymDZA602oucEHRn+fQTpbwv0aAi7EcJ0h1vGUlMI5bqVdukSkCvKNksGL9XPn4EihqAWFWAK5FlKdogU3ReLqFXLClBMzMWz8SQ7WCZaTxSTUFJ24I2f8DwNvP1dOOKaHmFNg9Sa3wKCKebaqlItA/+G3YgRZO/ZBJDQnGxuyI+Jlppvc6bzbMc02KnLZeVUxjB723X732R0wnz1NRi3as/+yks5vwcO92G7SarnUEWkDfVCYJMkX2z0X8aljs2v60WxfzPAl5T5xm7uJrxA+sypE9wza5xbtroeDfEnvD6IuCHe3Wcany9sfLCWO1hQrX5guATy5nDJk4ZfoBpuvitywa2qjxm+85O1c2P1LsiJJvUiLCSwBF7bvCnjnDDGstKOQaxKeREIXefhrpBdEWNI4GR6HfAVBWbiFHF9WUJfzgRd+0kUs4QfN7bZDpuGS3WHEWnwWEvnRb8BycUVix4sVKRh9j8S7QiKESw7H4rdtVUhM8DEW7UXIzTQw/PgbaOHXi+DKM67cpcDnBDEBl9a6fmhJrYFINHiN+FpkcFcOnYaQ6ZVzYld2WyUR/F65SoKlG6+RG/8XIYq4JVknS4f3By6kUBCFpKSuvm087HRKAUC8JchXrx6YXmDQcnyde1cBhqDWru4tzOtbg1wJEYv79VUv08TiSbiJabqly0S8i5AkKV+hO5dGdJHYd0vkcRMhRZJmaH497M5Ipn9BdVK4rDhCFsUPK0/8855ZCr3BleSAI4WnCn71HPU8ZjZlRDtlIjoF9BHFmeQd+GZhrYjQ++wKbRrVm+GAy4koj0tUD2o2ddK6+t5Yv8kpR5KseXq/5FN+BbaL+nSU5El7LIxZ1MPIs+clzx+n7/Kip0KWBMkY2syK2l952/gFl+hC3jAAhB35LbcHPyhbY+Xze92aooyCIyn7Yo9HIiWG5+Q8nIjeuHn/JhDPdxyPb7sQ+fbs5LF3mQsuo4IYu4IjqyRUgKbJVjhDT4sT2CK747ZcyGu85FI5UzAEMvdm70J0XOEKiNjqZwJGMTW97HfhMetH6fe7c9fC9cp/jMXuzj151d4sZyfrboUoC2VFkQThE6hZuq1JhO7FPd3GQjx9T86V3iRabuTTZcnnjVK7rc1ulJggjprOWN9WZkmU1XRj3+7p67V7B3dC89d9UYA6+YmskrgC2/WbiOpFF83gLfvGWIKz3vptSIwoUyiC0WMj5z48fLmpWejsyX18tR7FvFshke7GxaONw9reMdCbuPMLtIJ3KWCk2D42XEjB3vGmnjxsHnvc8e9PLBw8lZvA58+RubvtWTmtqG67F6dTYzc2DdwBhOfOcrwEejnjzd/yygrII6y0iFTrze7vTeb/znJbXaVYCJ3A/VeocfZ7lXbkCWeIdc6N1MDP/CCeIxt2s+HtTyjgopPk7eFrCZsfkyv2bR8FEW00UaQ/fDleRTqFF9IuOp6QHfGlZ6bEjxlS4h+IkrmgzjSJrt0tZVITIk4Ti1otiF7ZL+e8gfcYSCGSpJO5/asmJwLP3v3pM/H13dcVpSSOncD9Gi7/LV8UlXvbjbj7jYh1grq7l7GDpoJH0ItX/G21dZPcYFXe/e8PTn/VUzkXpO2oLZqabIFoYTX+10n1iAMyPJp3zA/wJ2BIinjRHIVjM6rk2Nf7Llp7mmFhzEJ1zgHxmCRpcOmmgm9aOAgC4gYPXaRoWf0pTJv3EcrIJbDJTAOPfn9SMeMUincaiXP4eMATFnonuATvx4oENKYo4l/YnfBD74yU4HmTsNgIleDgR4Q1X8Ie3azCdJ7tFtoSfkbVc/oFvTYKxwi2l88ulGAfTWQ4MCrgWYhk9yOtvinj9bJmKsFaGkmNPLjxjkS8KkXJPZMJImCSGC/dJDhKVQFnVkQhi6tXLTN3qJLkU8fHa62MVhtbnSX0NEb2GeyTk0BnnJsqh2y7eBBnfmw2e5FYyXuhjE39Eje+b5OLSdCLiKmWDdc+X+M9fokg/kNxtrB4h6fgZxfvVmodZWP3haM2LRYncrxYBO/osrqEFJfs8Lg4Sz0O4BSsjRCHu/TZPCcQLlJRBXen9MSPYyyl4z5UdaAO2yLi6LtOGxeRh6cfn4QplaNPYAP4TsXfqSPRXIlCq+xGlXMnmusRRREpob3QiuEgt032MoAJKC7aXa4KY7g6wmWChqwci6KVRU5mLuqexNYb5ypo8dZFqwQk1mSP6/a9Pr9qmPjh2NexLCozX9neWCHIMrG4GwvL0BEVn6yy11nDbhtXvaMBUW+QYIm+VCgxb3XYhT4dfZQDHirEW2VV3BxtO8ASUQRc0bevejgWEFGwJnQSRxRmHHTg8NdIOcgTEi/JHou4Nj5NJVEkQIoq2SGQ2+RghTxkuQi7t2UqKR6hdDvPkIh6yBGmY4eGHWcfZ6ioNvsoNQYmzWLtvcIqJx52EC8ITl46/X0FDegDUYuCd7fraK0w2GhvpiL/iI0ChIrtqG9fOjcgEvkxapS5gN6/0fWV4F5wCUgm1HNojmqLWp3ZrCPiFaFWxk2rmP1qxjqPXS+0oJCGLyKSepHjEHLAoCaUtZBbCZlK5j1ub81cy1vlq9k72dU8z/K7amZWbOmFa3QddQocB0aq8zUcGsOhqYTMNfs+oVr1PGmaACSTXOc/LSZC124UYLX9rjhnfP8mJSGrKcebo3PjnoX3tux4S7gqhPBL5tTtgwgib1/weiRkrPdpxL221kS0b/fQOlcIcPBLSOqz3tZxVUa2Yh8DxOedCMt8Uq2OeEJkjbKionCzWRMoCGAlN+weG7UryIes0YRqtnlkLbipQANMPWmx/MIv46HFpbknzfPRMokDKIxuViBOowqZgoYd3bmUvuB7xbyQm+s2tCp297n7BKyo+zzyoIrkwD4CHFHcVRGUae3j8Gwlvqzdmd3aHcDUzmkaITn9lxNItTsNXTuV2I8Qctyx9tu3bwJSkWI9iGcr8P5Y5bgWiDPmwlD8lXMPvDNOUYo6zqrPP83ymNPPZJBdrw++upCvPlYrDyvnMS8fu8T1wT+o32uGi4qyOEtRvwXp/HpKxgxzANNkf/ncQJusJxqnnDS3wM1jbwU1kiiN/ImwaYJEfuOr+fRiY3e8Ib0Ee/oRSHCgNVYI5UKFSkmxbjvcPBn6ZpvRyBgKmPFmyRdSZFhCNXNkn+hqB6cKhHhEImJSI98NWrm0ccaloVxa3H3n5NqeTOPkiwRPbUsP41msQidfW0TZNxt1RbS4g6Lk6oWys3o581zcOHwROZzXfOwP8W1Uv8YhRIuxgL1KEas4C3WzyiKFqZ98Y+Htr2LthInzq6LNwX/jPE7LJIGa9Ep0z+ziNIcOjQC0ZfXQDmtztNjYWqLp4vRbs9cqTBWpbljd5pNDDX/0fPlc6HCumnmLfydEMTUfPWk+CinczU5ZIoBmsuI6iE9SaBW6Wnfx5IE8fHhU8I2EDJ0Z5nhQ6yNg+rdIW08TQCgsD4+Ekh09gaflCahikSRb3x7UojABtP2Ia1+k9Xs7zOy7vq+WpdDO6dXHUQMRnwZWvOGrcTBliI0wC3xEXYQ8RU9ji0mepn1qUY5mizRA71auSGMnUedYe5K0CbvPKfb0CVzezmb2e/cE9dvbQV0KW4Dm6DdapJdZ1s+KYjheePA+ci7gyHLHo5FE/Gu8jf8Z+EwLy02kS11/JmROnqrs2Gecgxf5cCvxY+/5M8k6CMdRrA8C//GG+iccEdZ8YDaXnMSAOEoXpelQZoBkICxgDj8Etx+OPpwiWux8K5rnH1LddC8u42txsV4nQMwve+0f9iR3qqY2qKwwDRT73aJ+r05ApKk67jfjh5E6y/3xQUkf/v7kk3OpUE6g++jTR+61mn/+gfr9TGruQd+hbwHSIQ/95UwOsmKt+5BKFicels3DFvFjTfh4A+LBP8aYPGNfUYSptaZ+iK1OiJXGE2b9RjgsHf5mpT2B89WSb+95PSjJR8x/vHv1oCOfIwykJTTlYxX54EoKHIE/6DsB32c34SGAfLCzrJKHLbMHwhM+TlblGdtejzetRGgzVsLYB+3zICvPvzMKTz43AUn6J3vFPOxXPig12xsnNwWni5rhxznKB33zFLKmDxtjjMmK7PHJjccG/ItK+8Kvb7Dd++bOj0d2pP6CpEhY8suptb+c/wcWUi83"
_BIP39 = set(zlib.decompress(base64.b64decode(_BIP39_Z)).decode().split())
WIF_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[5KL][1-9A-HJ-NP-Za-km-z]{50,51}(?![1-9A-HJ-NP-Za-km-z])")
XPRV_RE = re.compile(r"\b[xyzt]prv[1-9A-HJ-NP-Za-km-z]{100,112}\b")
HEX64_RE = re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b")
KEYWORD_RE = re.compile(r"\b(private ?key|priv ?key|secret ?key|wallet key|seed|recovery phrase|mnemonic|secret phrase)\b", re.I)
SECRET_WARNING = ("\u26A0\uFE0F Stop: that looks like a wallet seed phrase or private key. Anyone who sees it can take "
                  "everything in that wallet. I did not save it and I didn't send it anywhere.\n\n"
                  "Treat that wallet as exposed: create a new wallet on a device you trust, move your funds to it now, "
                  "and never type the phrase to anyone (no real support agent, bot or site will ever ask for it). "
                  "Tip: delete your message above. To watch a wallet, send only its public address: /watch <address>.")


def detect_secret(text):
    t = text or ""
    if WIF_RE.search(t) or XPRV_RE.search(t):
        return "privkey"
    if HEX64_RE.search(t) and KEYWORD_RE.search(t):
        return "privkey"
    words = re.findall(r"[a-z]+", t.lower())
    run = best = 0
    for w in words:
        run = run + 1 if w in _BIP39 else 0
        best = max(best, run)
    if best >= 12:
        return "seed"
    return None


# =====================================================================================
# 1. Wallet alerts (read-only)
# =====================================================================================
USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRON_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
BTC_RE = re.compile(r"^(bc1[02-9ac-hj-np-z]{11,71}|[13][1-9A-HJ-NP-Za-km-z]{25,34})$")
ETH_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
CHAIN_NAME = {"tron": "TRON (USDT-TRC20)", "btc": "Bitcoin", "eth": "Ethereum"}
EXPLORER = {"tron": "https://tronscan.org/#/transaction/{}", "btc": "https://blockstream.info/tx/{}",
            "eth": "https://eth.blockscout.com/tx/{}"}
MAX_WALLETS = 3


def detect_chain(addr):
    a = (addr or "").strip()
    if TRON_RE.match(a):
        return "tron"
    if ETH_RE.match(a):
        return "eth"
    if BTC_RE.match(a):
        return "btc"
    return None


def _iso_ts(s):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def tron_txs(addr):
    r = requests.get(f"https://api.trongrid.io/v1/accounts/{addr}/transactions/trc20", timeout=20, headers=BOT_UA,
                     params={"limit": 20, "contract_address": USDT_TRC20, "only_confirmed": "true"})
    r.raise_for_status()
    out = []
    for x in r.json().get("data", []):
        dec = int((x.get("token_info") or {}).get("decimals") or 6)
        amt = int(x.get("value") or 0) / 10 ** dec
        inc = (x.get("to") or "") == addr
        out.append({"id": x["transaction_id"], "ts": (x.get("block_timestamp") or 0) / 1000, "dir": "in" if inc else "out",
                    "amount": amt, "coin": (x.get("token_info") or {}).get("symbol") or "USDT",
                    "other": x.get("from") if inc else x.get("to")})
    return out


def btc_txs(addr):
    r = requests.get(f"https://blockstream.info/api/address/{addr}/txs", timeout=20, headers=BOT_UA)
    r.raise_for_status()
    out = []
    for x in r.json()[:25]:
        st = x.get("status") or {}
        if not st.get("confirmed"):
            continue
        got = sum(v.get("value", 0) for v in x.get("vout", []) if v.get("scriptpubkey_address") == addr)
        sent = sum((v.get("prevout") or {}).get("value", 0) for v in x.get("vin", [])
                   if (v.get("prevout") or {}).get("scriptpubkey_address") == addr)
        net = got - sent
        out.append({"id": x["txid"], "ts": float(st.get("block_time") or 0), "dir": "in" if net >= 0 else "out",
                    "amount": abs(net) / 1e8, "coin": "BTC", "other": ""})
    return out


def eth_txs(addr):
    r = requests.get(f"https://eth.blockscout.com/api/v2/addresses/{addr}/transactions", timeout=20, headers=BOT_UA)
    r.raise_for_status()
    out = []
    low = addr.lower()
    for x in r.json().get("items", [])[:25]:
        if x.get("result") not in (None, "success"):
            continue
        frm = ((x.get("from") or {}).get("hash") or "").lower()
        to = ((x.get("to") or {}).get("hash") or "").lower()
        inc = to == low and frm != low
        out.append({"id": x["hash"], "ts": _iso_ts(x.get("timestamp") or ""), "dir": "in" if inc else "out",
                    "amount": int(x.get("value") or 0) / 1e18, "coin": "ETH", "other": frm if inc else to})
    return out


FETCH = {"tron": tron_txs, "btc": btc_txs, "eth": eth_txs}


def new_txs(txs, last_ts, seen):
    """Transactions newer than the stored watermark (seen ids break ties at the same second)."""
    return sorted([t for t in txs if t["ts"] > last_ts or (t["ts"] == last_ts and t["id"] not in seen)],
                  key=lambda t: t["ts"])


def fmt_tx(chain, addr, t):
    when = datetime.datetime.utcfromtimestamp(t["ts"] + WAT).strftime("%a %-d %b, %-I:%M %p WAT")
    arrow = "\U0001F7E2 Received" if t["dir"] == "in" else "\U0001F534 Sent"
    if t["amount"] == 0:
        arrow = "\u2699\uFE0F Contract interaction (0 ETH; may be a token transfer)"
    amt = f"{t['amount']:,.8f}".rstrip("0").rstrip(".") if t["amount"] < 1 else f"{t['amount']:,.4f}".rstrip("0").rstrip(".")
    return (f"\U0001F4B8 Wallet alert ({CHAIN_NAME[chain]})\n{arrow} {amt} {t['coin']}\n"
            f"Wallet: {addr[:6]}...{addr[-4:]}\nTime: {when}\n{EXPLORER[chain].format(t['id'])}")


def wallet_watch(uk, addr):
    addr = addr.strip()
    if detect_secret(addr):
        return R(SECRET_WARNING)
    chain = detect_chain(addr)
    if not chain:
        return R("That doesn't look like a wallet address I can watch. Send a TRON (T...), Bitcoin (1..., 3..., bc1...) "
                 "or Ethereum (0x...) public address, e.g. /watch TQrY8tryqsYVCYS3MFbtffiPp2ccyn4STm")
    rows = q("select addr from sk_wallets where uk=?", (uk,), fetch=True) or []
    if any(r[0] == addr for r in rows):
        return R("You're already watching that wallet. See /wallets.")
    if len(rows) >= MAX_WALLETS:
        return R(f"You can watch up to {MAX_WALLETS} wallets. Remove one with /unwatch first.")
    if not quota(uk, "wallet"):
        return over("wallet")
    try:
        txs = FETCH[chain](addr)
    except Exception as e:
        print("wallet baseline error:", chain, e)
        return R("I couldn't reach the blockchain explorer just now. Please try again in a minute.")
    last = max([t["ts"] for t in txs] or [time.time()])
    seen = ",".join(t["id"] for t in txs if t["ts"] == last)[:2000]
    q("insert into sk_wallets (uk, chain, addr, last_ts, seen, checked, added) values (?, ?, ?, ?, ?, ?, ?)",
      (uk, chain, addr, last, seen, time.time(), time.time()))
    return R(f"\U0001F441 Watching your {CHAIN_NAME[chain]} wallet {addr[:6]}...{addr[-4:]}. I check about every 2 minutes "
             "and message you for each new incoming or outgoing transaction.\n\nRead-only: I only see public data. "
             "Never share your seed phrase or private key with anyone, including me.")


def wallet_list(uk):
    rows = q("select id, chain, addr from sk_wallets where uk=? order by id", (uk,), fetch=True) or []
    if not rows:
        return R("You're not watching any wallets. Add one: /watch <public address> (TRON USDT, Bitcoin or Ethereum).")
    kb = [[{"text": f"\U0001F5D1 Stop {i + 1}", "callback_data": f"sk:uw:{r[0]}"}] for i, r in enumerate(rows)]
    return R("\U0001F441 Wallets I'm watching:\n" + "\n".join(f"{i + 1}. {CHAIN_NAME[r[1]]}: {r[2]}" for i, r in enumerate(rows)),
             {"inline_keyboard": kb})


def wallet_unwatch(uk, arg):
    rows = q("select id, addr from sk_wallets where uk=? order by id", (uk,), fetch=True) or []
    arg = (arg or "").strip()
    if not rows:
        return R("You're not watching any wallets.")
    if not arg:
        return wallet_list(uk)
    pick = [r for r in rows if r[1] == arg] or ([rows[int(arg) - 1]] if arg.isdigit() and 0 < int(arg) <= len(rows) else [])
    if not pick:
        return R("I couldn't find that wallet. Send /wallets to see the list.")
    q("delete from sk_wallets where id=? and uk=?", (pick[0][0], uk))
    return R(f"\U0001F5D1 Stopped watching {pick[0][1][:6]}...{pick[0][1][-4:]}.")


def check_wallets():
    now = time.time()
    rows = q("select id, uk, chain, addr, last_ts, seen, checked from sk_wallets where checked<=? order by checked limit 60",
             (now - 110,), fetch=True) or []
    for wid, uk, chain, addr, last_ts, seen, checked in rows:
        if not q("update sk_wallets set checked=? where id=? and checked=? returning id", (now, wid, checked), fetch=True):
            continue
        try:
            txs = FETCH[chain](addr)
        except Exception as e:
            print("wallet poll error:", chain, str(e)[:120])
            continue
        fresh = new_txs(txs, float(last_ts or 0), set((seen or "").split(",")))
        if not fresh:
            continue
        top = max(t["ts"] for t in txs)
        seen2 = ",".join(t["id"] for t in txs if t["ts"] == top)[:2000]
        q("update sk_wallets set last_ts=?, seen=? where id=?", (top, seen2, wid))
        for t in fresh[-5:]:
            notify(uk, fmt_tx(chain, addr, t), "Wallet alert")
        if len(fresh) > 5:
            notify(uk, f"...and {len(fresh) - 5} more transactions. See the explorer for the full list.", "Wallet alert")
        time.sleep(0.3)


# =====================================================================================
# 2. Price and rate alerts (open.er-api.com + CoinGecko simple price)
# =====================================================================================
CRYPTO = {"btc": "bitcoin", "bitcoin": "bitcoin", "eth": "ethereum", "ethereum": "ethereum", "usdt": "tether",
          "tether": "tether", "bnb": "binancecoin", "sol": "solana", "solana": "solana", "trx": "tron", "tron": "tron",
          "xrp": "ripple", "doge": "dogecoin", "dogecoin": "dogecoin", "ton": "the-open-network", "usdc": "usd-coin",
          "ada": "cardano", "ltc": "litecoin", "not": "notcoin", "pi": "pi-network"}
FIAT_WORDS = {"naira": "NGN", "ngn": "NGN", "dollar": "USD", "dollars": "USD", "usd": "USD", "pound": "GBP", "pounds": "GBP",
              "gbp": "GBP", "euro": "EUR", "euros": "EUR", "eur": "EUR", "cedi": "GHS", "cedis": "GHS", "ghs": "GHS",
              "rand": "ZAR", "zar": "ZAR", "cfa": "XOF", "xof": "XOF", "xaf": "XAF", "shilling": "KES", "kes": "KES",
              "yuan": "CNY", "cny": "CNY", "riyal": "SAR", "sar": "SAR", "dirham": "AED", "aed": "AED", "cad": "CAD",
              "canadian dollar": "CAD", "inr": "INR", "rupee": "INR", "yen": "JPY", "jpy": "JPY"}
MAX_ALERTS = 5
_price_cache = {"fx": (0, None), "cg": (0, {})}


def _asset(word):
    w = (word or "").lower().strip(" .?!,")
    if w in CRYPTO:
        return ("crypto", CRYPTO[w], w.upper() if len(w) <= 5 else w.title())
    if w in FIAT_WORDS:
        return ("fiat", FIAT_WORDS[w], FIAT_WORDS[w])
    if re.fullmatch(r"[a-z]{3}", w) and w.upper() in ("NGN", "USD", "GBP", "EUR", "GHS", "ZAR", "KES", "XOF", "XAF", "CAD", "AED", "SAR", "CNY", "INR", "JPY", "EGP", "MAD", "UGX", "TZS", "RWF"):
        return ("fiat", w.upper(), w.upper())
    return None


def parse_price_alert(text):
    """'alert me when dollar hits 1600 naira', 'alert me when btc goes above 100000', '/alert usdt ngn 1700',
    'tell me when eth drops below 2500 usd' -> {base, quote, op, target, kinds} or None."""
    t = (text or "").lower().replace("\u20a6", " ngn ").replace("$", " usd ")
    t = re.sub(r"\s+", " ", t).strip()
    m = re.match(r"^/alert(?:@\w+)?\s+([a-z]+)\s*(?:/|to|\s)\s*([a-z]+)\s+(above|below|>|<)?\s*([\d.,]+\s*[km]?)$", t)
    if m:
        base, quote, op, tgt = m.group(1), m.group(2), m.group(3), m.group(4)
    else:
        m = re.search(r"\b(?:alert|tell|notify|ping|message) me (?:when|if|once)\s+(?:the\s+)?(?:price of\s+)?([a-z]+)"
                      r"(?:\s*(?:/|to)\s*([a-z]+))?(?:\s+(?:price|rate))?\s+(?:hits|reaches|gets to|is|goes|rises|falls|drops|crosses|passes|touches|go|get)?"
                      r"\s*(above|over|below|under|to)?\s*(?:to\s+)?(?:ngn|usd)?\s*([\d.,]+\s*[km]?)\s*([a-z]+)?", t)
        if not m:
            return None
        base, quote, op, tgt = m.group(1), m.group(2) or m.group(5), m.group(3), m.group(4)
    a = _asset(base)
    if not a:
        return None
    qa = _asset(quote) if quote else None
    if quote and not qa:
        qa = None
    if not qa:
        qa = ("fiat", "NGN", "NGN") if a[0] == "fiat" and a[1] != "NGN" else ("fiat", "USD", "USD")
        if a[0] == "fiat" and a[1] == "NGN":
            return None
    target = parse_amount(tgt)
    if not target:
        return None
    op = {"above": ">", "over": ">", ">": ">", "below": "<", "under": "<", "<": "<"}.get(op or "", "")
    return {"base": a, "quote": qa, "op": op, "target": target}


def fx_rates():
    ts, rates = _price_cache["fx"]
    if rates and time.time() - ts < 1800:
        return rates
    r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=20)
    r.raise_for_status()
    d = r.json()
    if d.get("result") != "success":
        raise RuntimeError("er-api failed")
    _price_cache["fx"] = (time.time(), d["rates"])
    return d["rates"]


def cg_prices(ids, vs=("usd", "ngn")):
    ts, cache = _price_cache["cg"]
    need = [i for i in ids if i not in cache]
    if cache and not need and time.time() - ts < 110:
        return cache
    r = requests.get("https://api.coingecko.com/api/v3/simple/price", timeout=20, headers=BROWSER_UA,
                     params={"ids": ",".join(sorted(set(ids) | set(cache))), "vs_currencies": ",".join(vs)})
    if r.status_code == 429 or r.status_code >= 500:
        if cache and not need:
            return cache
        fb = coinbase_prices(set(ids) | set(cache))        # keyless fallback when CoinGecko throttles
        if fb and all(i in fb for i in ids):
            _price_cache["cg"] = (time.time(), {**cache, **fb})
            return _price_cache["cg"][1]
        raise RuntimeError("CoinGecko is rate-limiting, try again shortly")
    r.raise_for_status()
    _price_cache["cg"] = (time.time(), r.json())
    return _price_cache["cg"][1]


CB_SYMBOL = {"bitcoin": "BTC", "ethereum": "ETH", "tether": "USDT", "solana": "SOL", "ripple": "XRP", "dogecoin": "DOGE",
             "usd-coin": "USDC", "cardano": "ADA", "litecoin": "LTC", "the-open-network": "TON", "tron": "TRX"}


def coinbase_prices(ids):
    """Coinbase public spot price (no key): {id: {"usd": x, "ngn": x * official rate}}."""
    out = {}
    try:
        ngn = fx_rates()["NGN"]
    except Exception:
        ngn = None
    for i in ids:
        sym = CB_SYMBOL.get(i)
        if not sym:
            continue
        try:
            r = requests.get(f"https://api.coinbase.com/v2/prices/{sym}-USD/spot", timeout=10, headers=BOT_UA)
            usd = float(r.json()["data"]["amount"])
            out[i] = {"usd": usd, **({"ngn": usd * ngn} if ngn else {})}
        except Exception as e:
            print("coinbase price error:", sym, str(e)[:80])
    return out


def price_of(base, quote):
    """Current price of base in quote, e.g. (('crypto','bitcoin','BTC'), ('fiat','USD','USD'))."""
    if base[0] == "fiat":
        rates = fx_rates()
        if quote[0] == "fiat":
            return rates[quote[1]] / rates[base[1]]
        p = cg_prices([quote[1]])[quote[1]]["usd"]
        return (1 / rates[base[1]]) / p
    p = cg_prices([base[1]])
    if quote[0] == "fiat":
        cur = quote[1].lower()
        if cur in p[base[1]]:
            return p[base[1]][cur]
        return p[base[1]]["usd"] * fx_rates()[quote[1]]
    p2 = cg_prices([base[1], quote[1]])
    return p2[base[1]]["usd"] / p2[quote[1]]["usd"]


def fmt_price(v):
    return f"{v:,.2f}" if v >= 1 else f"{v:.6f}".rstrip("0")


def alert_add(uk, text):
    p = parse_price_alert(text)
    if not p:
        return R("Tell me like this: \"alert me when dollar hits 1600 naira\", \"alert me when BTC goes above 100000\" "
                 "or /alert usdt ngn 1700")
    n = q("select count(*) from sk_alerts where uk=?", (uk,), fetch=True)[0][0]
    if n >= MAX_ALERTS:
        return R(f"You already have {MAX_ALERTS} price alerts. Remove one with /alerts first.")
    if not quota(uk, "alert"):
        return over("alert")
    try:
        now = price_of(p["base"], p["quote"])
    except Exception as e:
        print("alert price error:", e)
        return R("I couldn't fetch the current price right now. Please try again in a minute.")
    op = p["op"] or (">" if p["target"] > now else "<")
    q("insert into sk_alerts (uk, kind, base, quote, op, target, created) values (?, ?, ?, ?, ?, ?, ?)",
      (uk, p["base"][0], json.dumps(p["base"]), json.dumps(p["quote"]), op, p["target"], time.time()))
    note = ("\nNote: the naira rate is the official/mid-market rate (open.er-api, updated daily), not the street (parallel) rate."
            if "NGN" in (p["base"][1], p["quote"][1]) and p["base"][0] == "fiat" else "")
    return R(f"\U0001F514 Alert set: I'll message you when 1 {p['base'][2]} goes {'above' if op == '>' else 'below'} "
             f"{fmt_price(p['target'])} {p['quote'][2]}. Now: {fmt_price(now)} {p['quote'][2]}.{note}\nSee or remove: /alerts")


def alert_list(uk):
    rows = q("select id, base, quote, op, target from sk_alerts where uk=? order by id", (uk,), fetch=True) or []
    if not rows:
        return R("No price alerts yet. Try: alert me when dollar hits 1600 naira")
    lines, kb = [], []
    for i, (aid, b, qq, op, tg) in enumerate(rows):
        b, qq = json.loads(b), json.loads(qq)
        lines.append(f"{i + 1}. 1 {b[2]} {'above' if op == '>' else 'below'} {fmt_price(tg)} {qq[2]}")
        kb.append([{"text": f"\U0001F5D1 Remove {i + 1}", "callback_data": f"sk:ua:{aid}"}])
    return R("\U0001F514 Your price alerts:\n" + "\n".join(lines), {"inline_keyboard": kb})


def check_alerts():
    rows = q("select id, uk, base, quote, op, target from sk_alerts", fetch=True) or []
    if not rows:
        return
    ids = {json.loads(r[2])[1] for r in rows if json.loads(r[2])[0] == "crypto"} | \
          {json.loads(r[3])[1] for r in rows if json.loads(r[3])[0] == "crypto"}
    try:
        if ids:
            cg_prices(list(ids))
    except Exception as e:
        print("alert cg error:", e)
        return
    for aid, uk, b, qq, op, target in rows:
        b, qq = json.loads(b), json.loads(qq)
        try:
            now = price_of(b, qq)
        except Exception as e:
            print("alert check error:", e)
            continue
        if (op == ">" and now >= target) or (op == "<" and now <= target):
            if q("delete from sk_alerts where id=? returning id", (aid,), fetch=True):
                notify(uk, f"\U0001F6A8 Price alert: 1 {b[2]} is now {fmt_price(now)} {qq[2]} "
                                  f"({'above' if op == '>' else 'below'} your {fmt_price(target)}).\nSet another: alert me when ...")


def rate_text():
    """Quick naira line used by briefings."""
    try:
        r = fx_rates()
        return (f"\U0001F4B1 Official rate: $1 = \u20a6{r['NGN']:,.0f}, \u00a31 = \u20a6{r['NGN'] / r['GBP']:,.0f}, "
                f"\u20ac1 = \u20a6{r['NGN'] / r['EUR']:,.0f} (open.er-api, daily mid-market)")
    except Exception:
        return "\U0001F4B1 Exchange rate unavailable right now."


# =====================================================================================
# 6. Weather (open-meteo)
# =====================================================================================
WMO = {0: "\u2600\uFE0F clear", 1: "\U0001F324 mostly clear", 2: "\u26C5 partly cloudy", 3: "\u2601\uFE0F cloudy",
       45: "\U0001F32B fog", 48: "\U0001F32B fog", 51: "\U0001F326 light drizzle", 53: "\U0001F326 drizzle",
       55: "\U0001F326 heavy drizzle", 61: "\U0001F327 light rain", 63: "\U0001F327 rain", 65: "\U0001F327 heavy rain",
       66: "\U0001F327 freezing rain", 67: "\U0001F327 freezing rain", 71: "\u2744\uFE0F light snow", 73: "\u2744\uFE0F snow",
       75: "\u2744\uFE0F heavy snow", 80: "\U0001F326 showers", 81: "\U0001F327 showers", 82: "\u26C8 heavy showers",
       95: "\u26C8 thunderstorm", 96: "\u26C8 thunderstorm, hail", 99: "\u26C8 thunderstorm, hail"}


def geocode(place):
    r = requests.get("https://geocoding-api.open-meteo.com/v1/search", timeout=15,
                     params={"name": place.strip()[:60], "count": 5, "language": "en"})
    r.raise_for_status()
    res = r.json().get("results") or []
    if not res:
        return None
    ng = [x for x in res if x.get("country_code") == "NG"]
    return (ng or res)[0]


_wx_cache = {}


def weather_text(place):
    key = (place or "").lower().strip()
    hit = _wx_cache.get(key)
    if hit and time.time() - hit[0] < 900:
        return hit[1]
    g = geocode(place)
    if not g:
        return None
    for attempt in range(3):
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=15, params={
        "latitude": g["latitude"], "longitude": g["longitude"], "timezone": g.get("timezone") or "auto", "forecast_days": 3,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"})
        if r.status_code != 429:
            break
        time.sleep(2 + attempt * 3)          # free tier: brief per-IP rate limit
    r.raise_for_status()
    d = r.json()
    c, dl = d.get("current", {}), d.get("daily", {})
    name = ", ".join(x for x in [g.get("name"), g.get("admin1"), g.get("country")] if x)
    lines = [f"\U0001F326 Weather in {name}",
             f"Now: {c.get('temperature_2m')}\u00b0C (feels {c.get('apparent_temperature')}\u00b0C), "
             f"{WMO.get(c.get('weather_code'), 'mixed')}, humidity {c.get('relative_humidity_2m')}%, wind {c.get('wind_speed_10m')} km/h"]
    for i, day in enumerate(dl.get("time", [])[:3]):
        label = ["Today", "Tomorrow", datetime.date.fromisoformat(day).strftime("%A")][i]
        lines.append(f"{label}: {dl['temperature_2m_min'][i]:.0f}-{dl['temperature_2m_max'][i]:.0f}\u00b0C, "
                     f"{WMO.get(dl['weather_code'][i], 'mixed')}, rain chance {dl['precipitation_probability_max'][i]}%")
    lines.append("Source: open-meteo.com")
    out = "\n".join(lines)
    _wx_cache[key] = (time.time(), out)
    return out


def weather(uk, place):
    place = (place or "").strip(" ?.!") or (flag(uk, "city") or "")
    if not place:
        return R("Which town? e.g. \"weather in Enugu\" or /weather Lagos")
    if not quota(uk, "weather"):
        return over("weather")
    try:
        t = weather_text(place)
    except Exception as e:
        print("weather error:", e)
        return R("The weather service didn't answer. Please try again in a minute.")
    if not t:
        return R(f"I couldn't find a place called \"{place}\". Try the nearest big town.")
    flag(uk, "city", place[:60])
    return R(t)


# =====================================================================================
# 7. Calendar file (.ics) from "exam Friday 9am"
# =====================================================================================
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
TIME_RE = re.compile(r"\b(?:at\s*)?(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)\b|\b(?:at\s*)?([01]?\d|2[0-3])[:.]([0-5]\d)\b|\b(noon|midday|midnight)\b", re.I)


def parse_when(text, now=None):
    """Find a date/time in text (WAT). Returns (datetime_wat, has_time, leftover_text) or None."""
    now = now or wat_now()
    t = " " + text + " "
    date = None
    spans = []
    m = re.search(r"\b(today|tonight|tomorrow|tmrw|next week)\b", t, re.I)
    if m:
        w = m.group(1).lower()
        date = now.date() + datetime.timedelta(days={"today": 0, "tonight": 0, "tomorrow": 1, "tmrw": 1, "next week": 7}[w])
        spans.append(m.span())
    if not date:
        m = re.search(r"\b(?:on\s+)?(next\s+|this\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues|tue|wed|thurs|thur|thu|fri|sat|sun)\b", t, re.I)
        if m:
            idx = [w[:3] for w in WEEKDAYS].index(m.group(2).lower()[:3])
            add = (idx - now.weekday()) % 7
            if m.group(1) and m.group(1).strip().lower() == "next" and add == 0:
                add = 7
            date = now.date() + datetime.timedelta(days=add)
            spans.append(m.span())
    if not date:
        m = re.search(r"\b(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?(?:,?\s+(\d{4}))?\b", t, re.I) or \
            re.search(r"\b(?:on\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b", t, re.I)
        if m:
            g = m.groups()
            if g[0].isdigit():
                day, mon = int(g[0]), MONTHS[g[1].lower()[:3]]
            else:
                mon, day = MONTHS[g[0].lower()[:3]], int(g[1])
            year = int(g[2]) if g[2] else now.year
            try:
                date = datetime.date(year, mon, day)
                if not g[2] and date < now.date():
                    date = datetime.date(year + 1, mon, day)
                spans.append(m.span())
            except ValueError:
                date = None
    if not date:
        m = re.search(r"\b(?:on\s+)?(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", t)
        if m:
            day, mon = int(m.group(1)), int(m.group(2))          # Nigerian order: day/month
            year = int(m.group(3)) if m.group(3) else now.year
            year += 2000 if year < 100 else 0
            try:
                date = datetime.date(year, mon, day)
                if not m.group(3) and date < now.date():
                    date = datetime.date(year + 1, mon, day)
                spans.append(m.span())
            except ValueError:
                date = None
    hh = mm = None
    m = TIME_RE.search(t)
    if m:
        if m.group(1):
            hh, mm = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0), int(m.group(2) or 0)
        elif m.group(4):
            hh, mm = int(m.group(4)), int(m.group(5))
        else:
            hh, mm = (0, 0) if m.group(6).lower() == "midnight" else (12, 0)
        spans.append(m.span())
    if date is None and hh is None:
        return None
    if date is None:
        date = now.date()
        if (hh, mm) <= (now.hour, now.minute):
            date += datetime.timedelta(days=1)
    has_time = hh is not None
    dt = datetime.datetime(date.year, date.month, date.day, hh if has_time else 9, mm or 0)
    left = t
    for a, b in sorted(spans, reverse=True):
        left = left[:a] + " " + left[b:]
    left = re.sub(r"\s+", " ", left).strip(" ,.-:")
    return dt, has_time, left


def make_ics(title, start_wat, minutes=60, all_day=False, alarm_min=30):
    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    uid = hashlib.sha1(f"{title}{start_wat}{time.time()}".encode()).hexdigest()[:16] + "@tygar"
    title = title.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", " ")[:120]
    if all_day:
        d = start_wat.strftime("%Y%m%d")
        d2 = (start_wat + datetime.timedelta(days=1)).strftime("%Y%m%d")
        when = f"DTSTART;VALUE=DATE:{d}\r\nDTEND;VALUE=DATE:{d2}"
    else:
        s = start_wat - datetime.timedelta(seconds=WAT)          # WAT -> UTC
        e = s + datetime.timedelta(minutes=minutes)
        when = f"DTSTART:{s.strftime('%Y%m%dT%H%M%SZ')}\r\nDTEND:{e.strftime('%Y%m%dT%H%M%SZ')}"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Tygar Bot//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             "BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}", when, f"SUMMARY:{title}",
             "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{title}", f"TRIGGER:-PT{alarm_min}M", "END:VALARM",
             "END:VEVENT", "END:VCALENDAR"]
    return ("\r\n".join(lines) + "\r\n").encode()


def ics_cmd(uk, text):
    body = re.sub(r"^\s*(/ics(@\w+)?|/calendar(@\w+)?|add (this )?to (my )?calendar:?|calendar( event)?:|make (a |an )?(calendar|ics)( file| event)?( for)?:?)\s*", "", text, flags=re.I)
    p = parse_when(body)
    if not p:
        return R("Tell me what and when, e.g. /ics exam Friday 9am or /ics wedding 12 Dec 2pm (Nigerian time).")
    if not quota(uk, "ics"):
        return over("ics")
    dt, has_time, title = p
    title = (title or "Event")[:100]
    title = title[0].upper() + title[1:]
    data = make_ics(title, dt, all_day=not has_time)
    when = dt.strftime("%a %-d %b %Y") + (dt.strftime(", %-I:%M %p WAT") if has_time else " (all day)")
    fname = (re.sub(r"[^A-Za-z0-9]+", "_", title)[:30].strip("_") or "event") + ".ics"
    return R(f"\U0001F4C5 {title}: {when}. Open the file to add it to your phone calendar (30-minute alert included).",
             file=(fname, data, "text/calendar"))


# =====================================================================================
# 8. Units and time zones (local, no API)
# =====================================================================================
UNITS = {  # unit -> (dimension, factor to base)
    "mm": ("len", 0.001), "millimetre": ("len", 0.001), "millimeter": ("len", 0.001), "cm": ("len", 0.01),
    "centimetre": ("len", 0.01), "centimeter": ("len", 0.01), "m": ("len", 1), "metre": ("len", 1), "meter": ("len", 1),
    "km": ("len", 1000), "kilometre": ("len", 1000), "kilometer": ("len", 1000), "in": ("len", 0.0254), "inch": ("len", 0.0254),
    "inches": ("len", 0.0254), "ft": ("len", 0.3048), "foot": ("len", 0.3048), "feet": ("len", 0.3048), "yd": ("len", 0.9144),
    "yard": ("len", 0.9144), "mi": ("len", 1609.344), "mile": ("len", 1609.344),
    "mg": ("mass", 1e-6), "g": ("mass", 0.001), "gram": ("mass", 0.001), "kg": ("mass", 1), "kilo": ("mass", 1),
    "kilogram": ("mass", 1), "lb": ("mass", 0.45359237), "lbs": ("mass", 0.45359237), "pound": ("mass", 0.45359237),
    "oz": ("mass", 0.028349523), "ounce": ("mass", 0.028349523), "stone": ("mass", 6.35029318), "tonne": ("mass", 1000), "ton": ("mass", 1000),
    "ml": ("vol", 0.001), "millilitre": ("vol", 0.001), "milliliter": ("vol", 0.001), "cl": ("vol", 0.01), "l": ("vol", 1), "litre": ("vol", 1),
    "liter": ("vol", 1), "gallon": ("vol", 3.785411784), "gal": ("vol", 3.785411784), "cup": ("vol", 0.2365882),
    "pint": ("vol", 0.473176473), "tbsp": ("vol", 0.0147868), "tsp": ("vol", 0.00492892), "cl": ("vol", 0.01),
    "km/h": ("speed", 1 / 3.6), "kmh": ("speed", 1 / 3.6), "kph": ("speed", 1 / 3.6), "mph": ("speed", 0.44704),
    "m/s": ("speed", 1), "knot": ("speed", 0.514444), "knots": ("speed", 0.514444),
    "kb": ("data", 1e3), "mb": ("data", 1e6), "gb": ("data", 1e9), "tb": ("data", 1e12),
    "acre": ("area", 4046.8564224), "hectare": ("area", 10000), "ha": ("area", 10000), "sqm": ("area", 1),
    "plot": ("area", 648), "plots": ("area", 648), "sqft": ("area", 0.09290304),
    "c": ("temp", "c"), "celsius": ("temp", "c"), "\u00b0c": ("temp", "c"), "f": ("temp", "f"), "fahrenheit": ("temp", "f"),
    "\u00b0f": ("temp", "f"), "k": ("temp", "k"), "kelvin": ("temp", "k"),
}


def _unit(u):
    u = (u or "").lower().strip(" .?!").replace("degrees ", "").replace("degree ", "")
    if u in UNITS:
        return u
    if u.endswith("es") and u[:-2] in UNITS:
        return u[:-2]
    if u.endswith("s") and u[:-1] in UNITS:
        return u[:-1]
    return None


def convert_units(v, a, b):
    da, fa = UNITS[a]
    db, fb = UNITS[b]
    if da != db:
        return None
    if da == "temp":
        c = v if fa == "c" else (v - 32) * 5 / 9 if fa == "f" else v - 273.15
        return c if fb == "c" else c * 9 / 5 + 32 if fb == "f" else c + 273.15
    return v * fa / fb


def parse_units(text):
    t = text.lower().strip()
    m = re.match(r"^(?:/units(?:@\w+)?\s+|convert\s+|what is\s+|what's\s+)?(-?\d[\d,]*(?:\.\d+)?)\s*([a-z\u00b0/ ]{1,14}?)\s+(?:to|in|into)\s+([a-z\u00b0/ ]{1,14}?)\s*\??$", t)
    if m:
        v, a, b = float(m.group(1).replace(",", "")), _unit(m.group(2)), _unit(m.group(3))
    else:
        m = re.match(r"^how many\s+([a-z\u00b0/]+)\s+(?:are\s+)?(?:in|is)\s+(-?\d[\d,]*(?:\.\d+)?)\s*([a-z\u00b0/ ]+?)\s*\??$", t)
        if not m:
            return None
        v, a, b = float(m.group(2).replace(",", "")), _unit(m.group(3)), _unit(m.group(1))
    if not a or not b or UNITS[a][0] != UNITS[b][0]:
        return None
    return v, a, b


def units_reply(text):
    p = parse_units(text)
    if not p:
        return None
    v, a, b = p
    out = convert_units(v, a, b)
    s = f"{out:,.4f}".rstrip("0").rstrip(".") if abs(out) < 1e6 else f"{out:,.0f}"
    extra = " (1 plot = 648 m\u00b2, the common Lagos/Nigeria size, 18 m x 36 m)" if "plot" in (a, b) else ""
    return R(f"\U0001F4CF {v:g} {a} = {s} {b}{extra}")


CITY_TZ = {
    "lagos": "Africa/Lagos", "abuja": "Africa/Lagos", "nigeria": "Africa/Lagos", "enugu": "Africa/Lagos", "kano": "Africa/Lagos",
    "accra": "Africa/Accra", "ghana": "Africa/Accra", "nairobi": "Africa/Nairobi", "kenya": "Africa/Nairobi",
    "johannesburg": "Africa/Johannesburg", "south africa": "Africa/Johannesburg", "cairo": "Africa/Cairo", "egypt": "Africa/Cairo",
    "london": "Europe/London", "uk": "Europe/London", "manchester": "Europe/London", "dublin": "Europe/Dublin", "paris": "Europe/Paris",
    "berlin": "Europe/Berlin", "madrid": "Europe/Madrid", "rome": "Europe/Rome", "amsterdam": "Europe/Amsterdam",
    "istanbul": "Europe/Istanbul", "moscow": "Europe/Moscow", "dubai": "Asia/Dubai", "uae": "Asia/Dubai", "riyadh": "Asia/Riyadh",
    "saudi": "Asia/Riyadh", "mecca": "Asia/Riyadh", "doha": "Asia/Qatar", "qatar": "Asia/Qatar", "delhi": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata", "india": "Asia/Kolkata", "beijing": "Asia/Shanghai", "china": "Asia/Shanghai", "shanghai": "Asia/Shanghai",
    "guangzhou": "Asia/Shanghai", "hong kong": "Asia/Hong_Kong", "tokyo": "Asia/Tokyo", "japan": "Asia/Tokyo", "seoul": "Asia/Seoul",
    "singapore": "Asia/Singapore", "sydney": "Australia/Sydney", "new york": "America/New_York", "nyc": "America/New_York",
    "washington": "America/New_York", "atlanta": "America/New_York", "houston": "America/Chicago", "chicago": "America/Chicago",
    "dallas": "America/Chicago", "los angeles": "America/Los_Angeles", "la": "America/Los_Angeles", "california": "America/Los_Angeles",
    "toronto": "America/Toronto", "canada": "America/Toronto", "vancouver": "America/Vancouver", "sao paulo": "America/Sao_Paulo",
    "brazil": "America/Sao_Paulo", "kinshasa": "Africa/Kinshasa", "dakar": "Africa/Dakar", "kigali": "Africa/Kigali",
    "addis ababa": "Africa/Addis_Ababa", "kampala": "Africa/Kampala", "dar es salaam": "Africa/Dar_es_Salaam", "usa": "America/New_York",
}


def tz_for(place):
    from zoneinfo import ZoneInfo, available_timezones
    p = (place or "").lower().strip(" ?.!")
    p = re.sub(r"\s+(time|now)$", "", p)
    if p in CITY_TZ:
        return CITY_TZ[p], place.strip(" ?.!").title()
    if p in ("wat", "gmt", "utc"):
        return {"wat": "Africa/Lagos", "gmt": "UTC", "utc": "UTC"}[p], p.upper()
    key = p.replace(" ", "_")
    for z in available_timezones():
        if z.lower().split("/")[-1] == key:
            return z, place.strip(" ?.!").title()
    return None, None


def tz_reply(text):
    from zoneinfo import ZoneInfo
    t = text.strip()
    m = re.match(r"^(?:/time(?:@\w+)?\s+|what(?:'s| is) the time in\s+|what time is it in\s+|time in\s+|current time in\s+)(.+?)\s*\??$", t, re.I)
    if m and not re.match(r"^\d", m.group(1)):
        z, name = tz_for(m.group(1))
        if not z:
            return None
        now = datetime.datetime.now(ZoneInfo(z))
        lag = datetime.datetime.now(ZoneInfo("Africa/Lagos"))
        diff = (now.utcoffset() - lag.utcoffset()).total_seconds() / 3600
        rel = "same time as Nigeria" if diff == 0 else f"{abs(diff):g} h {'ahead of' if diff > 0 else 'behind'} Nigeria"
        return R(f"\U0001F552 {name}: {now.strftime('%-I:%M %p, %a %-d %b')} ({rel}).")
    m = re.match(r"^(?:/time(?:@\w+)?\s+|convert\s+|what(?:'s| is)\s+)?(\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?)\s*(?:in\s+)?([a-z ]+?)?\s+(?:time\s+)?(?:to|in)\s+([a-z ]+?)(?:\s+time)?\s*\??$", t, re.I)
    if not m:
        return None
    src_name = (m.group(2) or "wat").strip()
    zs, ns = tz_for(src_name)
    zd, nd = tz_for(m.group(3))
    if not zs or not zd:
        return None
    tm = TIME_RE.search(m.group(1)) or re.match(r"(\d{1,2})", m.group(1))
    if not tm:
        return None
    if getattr(tm, "re", None) is TIME_RE and tm.group(1):
        hh, mm = int(tm.group(1)) % 12 + (12 if tm.group(3).lower() == "pm" else 0), int(tm.group(2) or 0)
    elif getattr(tm, "re", None) is TIME_RE and tm.group(4):
        hh, mm = int(tm.group(4)), int(tm.group(5))
    else:
        hh, mm = int(tm.group(1)), 0
    if hh > 23:
        return None
    today = datetime.datetime.now(ZoneInfo(zs)).date()
    src = datetime.datetime(today.year, today.month, today.day, hh, mm, tzinfo=ZoneInfo(zs))
    dst = src.astimezone(ZoneInfo(zd))
    day = "" if dst.date() == src.date() else (" (next day)" if dst.date() > src.date() else " (previous day)")
    return R(f"\U0001F552 {src.strftime('%-I:%M %p')} in {ns} = {dst.strftime('%-I:%M %p')} in {nd}{day}.")


# =====================================================================================
# 9. Live match alerts (ESPN public scoreboard JSON, no key)
# =====================================================================================
LEAGUES = ["eng.1", "esp.1", "ita.1", "ger.1", "fra.1", "uefa.champions", "uefa.europa", "uefa.europa.conf", "nga.1",
           "caf.nations", "caf.champions", "fifa.world", "fifa.worldq.caf", "eng.2", "eng.fa", "eng.league_cup", "usa.1",
           "por.1", "ned.1", "sco.1", "fifa.friendly"]
MAX_FOLLOWS = 3
_sb_cache = {}


def scoreboard(league, date=None):
    k = (league, date)
    ts, data = _sb_cache.get(k, (0, None))
    if data is not None and time.time() - ts < 90:
        return data
    p = {"dates": date} if date else {}
    r = requests.get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard", params=p,
                     timeout=15, headers=BROWSER_UA)
    data = r.json().get("events", []) if r.ok else []
    _sb_cache[k] = (time.time(), data)
    return data


def team_match(query, comp):
    t = comp.get("team") or {}
    names = [(t.get(x) or "").lower() for x in ("displayName", "shortDisplayName", "name", "abbreviation", "location")]
    qq = query.lower().strip()
    return any(n and (n == qq or (len(qq) >= 4 and qq in n)) for n in names)


def event_view(ev):
    c = ev["competitions"][0]
    home = next(x for x in c["competitors"] if x.get("homeAway") == "home")
    away = next(x for x in c["competitors"] if x.get("homeAway") == "away")
    st = ev.get("status", {}).get("type", {})
    goals = [d for d in c.get("details", []) if d.get("scoringPlay")]
    return {"id": ev["id"], "home": home["team"]["displayName"], "away": away["team"]["displayName"],
            "hs": home.get("score", "0"), "as": away.get("score", "0"), "state": st.get("state"),
            "detail": st.get("shortDetail") or st.get("detail") or "", "date": ev.get("date"), "goals": goals,
            "comps": c["competitors"], "league": (ev.get("league") or {}).get("abbreviation", "")}


def find_team_events(team, date=None):
    out = []
    for lg in LEAGUES:
        try:
            for ev in scoreboard(lg, date):
                c = ev["competitions"][0]
                if any(team_match(team, comp) for comp in c["competitors"]):
                    v = event_view(ev)
                    v["lg"] = lg
                    out.append(v)
        except Exception as e:
            print("espn error:", lg, str(e)[:80])
    return out


def follow_cmd(uk, team):
    team = (team or "").strip(" .!?")
    if not team:
        return R("Which team? e.g. /follow Arsenal or /follow Super Eagles (Nigeria)")
    rows = q("select team from sk_follows where uk=?", (uk,), fetch=True) or []
    if any(r[0].lower() == team.lower() for r in rows):
        return R(f"You already follow {team}. See /following.")
    if len(rows) >= MAX_FOLLOWS:
        return R(f"You can follow up to {MAX_FOLLOWS} teams. Remove one with /unfollow first.")
    if not quota(uk, "follow"):
        return over("follow")
    q("insert into sk_follows (uk, team, added) values (?, ?, ?)", (uk, team[:40], time.time()))
    return R(f"\u26BD Following {team}. I'll message you at kick-off, for each goal, and at full time (ESPN data, "
             "top leagues + NPFL + CAF + internationals). Stop with /unfollow " + team)


def unfollow_cmd(uk, team):
    rows = q("select id, team from sk_follows where uk=? order by id", (uk,), fetch=True) or []
    team = (team or "").strip()
    pick = [r for r in rows if r[1].lower() == team.lower()] or (rows if not team and len(rows) == 1 else [])
    if not pick:
        return R("Following: " + (", ".join(r[1] for r in rows) or "nobody") + ". Send /unfollow <team>.")
    q("delete from sk_follows where id=?", (pick[0][0],))
    return R(f"Stopped following {pick[0][1]}.")


def check_matches():
    rows = q("select uk, team from sk_follows", fetch=True) or []
    if not rows:
        return
    teams = {}
    for uk, team in rows:
        teams.setdefault(team.lower(), []).append(uk)
    leagues_today = state("espn_leagues")
    if not leagues_today or every("espn_discover", 3 * 3600, peek=True):
        every("espn_discover", 3 * 3600)
        found = set()
        for lg in LEAGUES:
            try:
                for ev in scoreboard(lg):
                    c = ev["competitions"][0]
                    if any(team_match(t, comp) for t in teams for comp in c["competitors"]):
                        found.add(lg)
            except Exception:
                pass
        state("espn_leagues", ",".join(sorted(found)))
        leagues_today = ",".join(sorted(found))
    for lg in [x for x in (leagues_today or "").split(",") if x]:
        try:
            evs = scoreboard(lg)
        except Exception:
            continue
        for ev in evs:
            v = event_view(ev)
            if v["state"] == "pre":
                continue
            for t, uks in teams.items():
                if not any(team_match(t, comp) for comp in v["comps"]):
                    continue
                for uk in uks:
                    _match_notify(uk, v)


def _match_notify(uk, v):
    score = f"{v['hs']}-{v['as']}"
    row = q("select status, score from sk_match where uk=? and event=?", (uk, v["id"]), fetch=True)
    old_status, old_score = (row[0] if row else (None, None))
    line = f"{v['home']} {v['hs']} - {v['as']} {v['away']}"
    msgs = []
    if v["state"] == "in" and old_status is None:
        msgs.append(f"\u26BD Kick-off: {v['home']} vs {v['away']} ({v['detail']})")
    if old_score is not None and score != old_score and v["state"] in ("in", "post"):
        g = v["goals"][-1] if v["goals"] else None
        who = ""
        if g:
            ath = (g.get("athletesInvolved") or [{}])[0].get("displayName", "")
            who = f" {ath} {(g.get('clock') or {}).get('displayValue', '')}".rstrip()
            if g.get("ownGoal"):
                who += " (own goal)"
            elif g.get("penaltyKick"):
                who += " (pen)"
        msgs.append(f"\U0001F945 GOAL!{who}\n{line}")
    if v["state"] == "post" and old_status != "post":
        if old_status is None and old_score is None:
            msgs = []      # first seen after it already ended: stay quiet
        else:
            msgs.append(f"\U0001F3C1 Full time: {line}")
    if row and old_status == v["state"] and old_score == score:
        return
    claimed = True
    if row:
        claimed = bool(q("update sk_match set status=?, score=? where uk=? and event=? and status=? and score=? returning uk",
                         (v["state"], score, uk, v["id"], old_status, old_score), fetch=True))
    else:
        try:
            q("insert into sk_match (uk, event, status, score) values (?, ?, ?, ?)", (uk, v["id"], v["state"], score))
        except Exception:
            claimed = False
    if claimed:
        for m in msgs:
            notify(uk, m, "Match alert")


# =====================================================================================
# 3. Daily briefing  |  4. Watch a page
# =====================================================================================
def parse_hhmm(s):
    m = re.search(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\b", s or "", re.I)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        hh = hh % 12 + (12 if m.group(3).lower() == "pm" else 0)
    if hh > 23 or mm > 59:
        return None
    return f"{hh:02d}:{mm:02d}"


def briefing_cmd(uk, arg):
    a = (arg or "").strip()
    if re.match(r"^(off|stop|cancel)\b", a, re.I):
        q("delete from sk_briefing where uk=?", (uk,))
        return R("Daily briefing turned off.")
    m = re.match(r"^(?:on\s*)?(\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?)?\s*(?:in\s+)?(.*)$", re.sub(r"^on\b", "", a, flags=re.I).strip(), re.I)
    hhmm = parse_hhmm(m.group(1) or "") if m and m.group(1) else "07:00"
    city = (m.group(2) if m else "").strip() or flag(uk, "city") or "Lagos"
    if not quota(uk, "briefing"):
        return over("briefing")
    q("insert into sk_briefing (uk, hhmm, city, last_day) values (?, ?, ?, '') on conflict (uk) do update set hhmm=excluded.hhmm, city=excluded.city",
      (uk, hhmm, city[:40]))
    return R(f"\U0001F305 Daily briefing on: every day at {hhmm} WAT with top headlines, the naira rate and weather for {city}. "
             "Change: /briefing on 6:30 Abuja. Stop: /briefing off")


def briefing_text(city):
    news = wsearch("Nigeria top news today")
    heads = [re.sub(r"^- (\[[^\]]*\]\s*)?", "", l).strip() for l in news.splitlines() if l.startswith("- ")][:5]
    parts = [f"\U0001F305 Good morning! Your briefing for {wat_now().strftime('%A %-d %B')}", ""]
    if heads:
        parts += ["\U0001F4F0 Headlines:"] + [f"\u2022 {h[:180]}" for h in heads] + [""]
    parts.append(rate_text())
    try:
        w = weather_text(city)
        if w:
            parts += ["", "\n".join(w.splitlines()[:3])]
    except Exception:
        pass
    parts += ["", "Turn off: /briefing off"]
    return "\n".join(parts)


def check_briefings():
    now = wat_now()
    today, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    rows = q("select uk, hhmm, city, last_day from sk_briefing", fetch=True) or []
    for uk, hhmm, city, last in rows:
        if last == today or hm < hhmm:
            continue
        late = (now - now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]))).total_seconds()
        if not q("update sk_briefing set last_day=? where uk=? and last_day=? returning uk", (today, uk, last or ""), fetch=True):
            continue
        if late > 3 * 3600:
            continue          # server was asleep; skip a stale briefing
        try:
            notify(uk, briefing_text(city), "Your daily briefing", long=True)
        except Exception as e:
            print("briefing error:", e)


MAX_PAGES = 3


def page_text(url):
    safe = H.get("safe_url")
    u = safe(url) if safe else url
    if not u:
        return None, None
    r = requests.get(u, timeout=20, headers=BROWSER_UA, stream=True)
    raw = r.raw.read(1_500_000, decode_content=True).decode(r.encoding or "utf-8", "ignore")
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw)
    txt = _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw))).strip()
    return u, txt


def norm_for_hash(txt):
    t = re.sub(r"\b\d{1,2}:\d{2}(:\d{2})?\s*(am|pm)?\b", "", txt, flags=re.I)   # clocks change on every load
    t = re.sub(r"\b\d+\s*(seconds?|mins?|minutes?|hours?|hrs?) ago\b", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


def watchpage_cmd(uk, url):
    url = (url or "").strip()
    if not url:
        return R("Send /watchpage <link>, e.g. /watchpage https://www.jamb.gov.ng")
    rows = q("select url from sk_pages where uk=?", (uk,), fetch=True) or []
    if len(rows) >= MAX_PAGES:
        return R(f"You can watch up to {MAX_PAGES} pages. Remove one with /pages first.")
    if not quota(uk, "page"):
        return over("page")
    try:
        u, txt = page_text(url)
    except Exception as e:
        print("watchpage error:", e)
        u, txt = None, None
    if not u:
        return R("I couldn't open that link (it must be a public website).")
    if not txt or len(txt) < 30:
        return R("That page has almost no readable text (it may need JavaScript or a login), so I can't watch it.")
    n = norm_for_hash(txt)
    q("insert into sk_pages (uk, url, hash, excerpt, checked, added) values (?, ?, ?, ?, ?, ?)",
      (uk, u, hashlib.sha256(n.encode()).hexdigest(), n[:4000], time.time(), time.time()))
    return R(f"\U0001F440 Watching {u}. I check every 30 minutes and tell you when its text changes. List/remove: /pages")


def pages_list(uk):
    rows = q("select id, url from sk_pages where uk=? order by id", (uk,), fetch=True) or []
    if not rows:
        return R("You're not watching any pages. Try /watchpage https://www.jamb.gov.ng")
    return R("\U0001F440 Pages I'm watching:\n" + "\n".join(f"{i + 1}. {r[1]}" for i, r in enumerate(rows)),
             {"inline_keyboard": [[{"text": f"\U0001F5D1 Stop {i + 1}", "callback_data": f"sk:up:{r[0]}"}] for i, r in enumerate(rows)]})


def page_diff(old, new):
    import difflib
    a, b = old.split(" "), new.split(" ")
    added = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("insert", "replace"):
            added.append(" ".join(b[j1:j2]))
    s = " ... ".join(x for x in added if x.strip())
    return s[:500]


def check_pages():
    now = time.time()
    rows = q("select id, uk, url, hash, excerpt, checked from sk_pages where checked<=? order by checked limit 20",
             (now - 1790,), fetch=True) or []
    for pid, uk, url, h, ex, checked in rows:
        if not q("update sk_pages set checked=? where id=? and checked=? returning id", (now, pid, checked), fetch=True):
            continue
        try:
            u, txt = page_text(url)
        except Exception as e:
            print("page check error:", str(e)[:100])
            continue
        if not txt:
            continue
        n = norm_for_hash(txt)
        h2 = hashlib.sha256(n.encode()).hexdigest()
        if h2 != h:
            q("update sk_pages set hash=?, excerpt=? where id=?", (h2, n[:4000], pid))
            d = page_diff(ex or "", n[:4000])
            notify(uk, f"\U0001F514 A page you're watching changed:\n{url}" + (f"\n\nNew text: {d}" if d else ""))


# =====================================================================================
# 5. Long-term memory  |  10. Budget tracker
# =====================================================================================
MAX_FACTS = 30
_facts_cache = {}
REMEMBER_RE = re.compile(r"^\s*(?:/remember(?:@\w+)?\s+|(?:please\s+)?(?:remember|note|keep in mind|don'?t forget)(?: that)?\s+)(.{3,300})$", re.I | re.S)


def facts(uk):
    c = _facts_cache.get(uk)
    if c and time.time() - c[0] < 600:
        return c[1]
    try:
        rows = [r[0] for r in (q("select fact from sk_facts where uk=? order by id", (uk,), fetch=True) or [])]
    except Exception as e:
        print("facts error:", e)
        rows = []
    _facts_cache[uk] = (time.time(), rows)
    return rows


def remember_cmd(uk, fact):
    fact = re.sub(r"\s+", " ", fact).strip(" .")
    if detect_secret(fact) or re.search(r"\b(password|pin|otp|bvn|cvv|card number)\b", fact, re.I):
        return R("I won't store passwords, PINs, OTPs, card or bank details, or wallet keys. Keep those private.")
    if re.match(r"^me (to|at|in)\b|^to\b|^(when|the time|what|how|who|where|why|if)\b", fact, re.I):
        return None          # a reminder or a question, not a fact
    if not quota(uk, "memory"):
        return over("memory")
    n = len(facts(uk))
    if n >= MAX_FACTS:
        return R(f"My memory for you is full ({MAX_FACTS} facts). Send /forget to clear it, then add again.")
    fact = re.sub(r"^i\b", "I", fact)
    q("insert into sk_facts (uk, fact, ts) values (?, ?, ?)", (uk, fact[:300], time.time()))
    _facts_cache.pop(uk, None)
    return R(f"\U0001F9E0 Got it, I'll remember: {fact}\nSee everything: /memory. Wipe it: /forget")


def memory_list(uk):
    f = facts(uk)
    if not f:
        return R("I don't have any saved facts about you yet. Say e.g. \"remember I support Arsenal\".")
    return R("\U0001F9E0 What I remember about you:\n" + "\n".join(f"\u2022 {x}" for x in f) + "\n\nWipe all: /forget")


def forget_cmd(uk):
    q("delete from sk_facts where uk=?", (uk,))
    _facts_cache.pop(uk, None)
    return R("\U0001F9F9 Done, I've forgotten everything you asked me to remember.")


CATS = {"transport": ["transport", "bus", "keke", "okada", "uber", "bolt", "taxi", "fare", "fuel", "petrol", "danfo", "bike"],
        "food": ["food", "lunch", "breakfast", "dinner", "rice", "suya", "snacks", "chop", "restaurant", "drink", "eat"],
        "data/airtime": ["data", "airtime", "recharge", "mtn", "glo", "airtel", "9mobile", "subscription"],
        "rent/bills": ["rent", "light", "nepa", "electricity", "water", "bill", "dstv", "gotv", "school fees", "fees"],
        "shopping": ["clothes", "shoes", "shopping", "market", "groceries", "jumia"], "health": ["drugs", "hospital", "pharmacy", "medicine"],
        "family": ["family", "mum", "dad", "church", "offering", "tithe", "gift", "zakat"]}


def parse_spend(text):
    m = re.match(r"^\s*(?:/spent(?:@\w+)?|/spend(?:@\w+)?|i\s+(?:just\s+)?spent|spent|paid|i paid)\s+(?:\u20a6|n|ngn)?\s*([\d.,]+(?:\s*(?:k|m)\b)?)\s*(?:naira)?\s*(?:on|for)?\s*(.*)$", text.strip(), re.I)
    if not m:
        return None
    amt = parse_amount(m.group(1))
    if not amt or amt <= 0 or amt > 1e10:
        return None
    if amt < 50 and not re.search(r"(\u20a6|\bn\s*\d|ngn|naira|\d\s*k\b)", text, re.I):
        return None                      # "paid 2 visits" is not spending
    note = m.group(2).strip(" .") or "other"
    low = note.lower()
    cat = next((c for c, words in CATS.items() if any(re.search(rf"\b{re.escape(w)}\b", low) for w in words)), None)
    return amt, (cat or note.lower()[:30])


def spend_cmd(uk, text):
    p = parse_spend(text)
    if not p:
        return None
    if not quota(uk, "spend"):
        return over("spend")
    q("insert into sk_spend (uk, amount, cat, ts) values (?, ?, ?, ?)", (uk, p[0], p[1], time.time()))
    week = q("select coalesce(sum(amount), 0) from sk_spend where uk=? and ts>=?", (uk, time.time() - 7 * 86400), fetch=True)[0][0]
    return R(f"\U0001F4B0 Logged {money(p[0])} on {p[1]}. Last 7 days: {money(week)}. Summary: /budget")


def budget_cmd(uk, arg=""):
    days = 30 if re.search(r"month|30", arg or "", re.I) else 7
    rows = q("select cat, sum(amount), count(*) from sk_spend where uk=? and ts>=? group by cat order by sum(amount) desc",
             (uk, time.time() - days * 86400), fetch=True) or []
    if not rows:
        return R("No spending logged yet. Try: spent \u20a63000 on transport")
    total = sum(r[1] for r in rows)
    lines = [f"\U0001F4CA Spending, last {days} days: {money(total)}"]
    for cat, s, n in rows[:12]:
        lines.append(f"\u2022 {cat}: {money(s)} ({s / total * 100:.0f}%)")
    lines.append(f"Daily average: {money(total / days)}")
    if days == 7:
        lines.append("Month view: /budget month. Clear all: /budget reset")
    return R("\n".join(lines))


# =====================================================================================
# Background work (called from app.reminder_loop every 30 s)
# =====================================================================================
_last = {}
_busy = threading.Lock()
HEART = [0.0]


def every(name, sec, peek=False):
    now = time.time()
    if now - _last.get(name, 0) >= sec:
        if not peek:
            _last[name] = now
        return True
    return False


JOBS = []   # (name, seconds, function); batches 2-4 append


def tick():
    HEART[0] = time.time()
    if not H.get("token") or not _busy.acquire(blocking=False):
        return
    threading.Thread(target=_tick_work, daemon=True).start()


def _tick_work():
    try:
        for name, sec, fn in JOBS:
            if every(name, sec):
                try:
                    fn()
                except Exception as e:
                    print(f"skills job {name} error:", str(e)[:200])
    finally:
        _busy.release()


JOBS += [("wallets", 120, check_wallets), ("alerts", 120, check_alerts), ("briefing", 60, check_briefings),
         ("pages", 300, check_pages), ("matches", 120, check_matches)]


# =====================================================================================
# Router: one table of triggers for Telegram and web
# =====================================================================================
ROUTES = []          # (prio, name, compiled regex, fn(uk, match, text) -> reply dict or None)
TG_ONLY = set()      # route names that need Telegram (buttons or background delivery)


def route_add(name, pattern, fn, prio=50, flags=re.I | re.S, tg_only=False):
    ROUTES.append((prio, name, re.compile(pattern, flags), fn))
    ROUTES.sort(key=lambda r: r[0])
    if tg_only:
        TG_ONLY.add(name)


def norm_cmd(text):
    """'/weather@Harkaiafricabot Lagos' -> '/weather Lagos'."""
    return re.sub(r"^(/\w+)@\w+", r"\1", (text or "").strip())


def route(uk, text):
    t = norm_cmd(text)
    if not t:
        return None
    if detect_secret(t):
        return R(SECRET_WARNING)
    for prio, name, rx, fn in ROUTES:
        m = rx.search(t)
        if not m:
            continue
        if name in TG_ONLY and chat_of(uk) is None:
            return R("That one works in the Telegram bot (it uses buttons or sends you messages later). "
                     "Open @Harkaiafricabot and send the same message there.")
        try:
            r = fn(uk, m, t)
        except Exception as e:
            print(f"skill {name} error:", repr(e)[:300])
            r = R("Sorry, that didn't work just now. Please try again in a minute.")
        if r is not None:
            r.setdefault("skill", name)
            if name in NOTIFY_SKILLS and chat_of(uk) is None:
                r = with_channel(uk, r)
            remember_output(uk, r)
            return r
    return None


PENDING = {}      # uk -> (skill prompt prefix, ts) set by the skills menu


def handle_tg(chat, text, quoted=""):
    """Returns True when a skill handled the Telegram message."""
    uk = f"tg:{chat}"
    if not gate_ok(uk):
        return False
    if quoted and re.fullmatch(r"\s*/(scam|check|translate|caption|flashcards)(@\w+)?\s*", text or ""):
        text = text.strip() + " " + quoted[:3000]          # reply to a message with /scam etc.
    p = PENDING.pop(uk, None)
    if p and time.time() - p[1] < 900 and not text.startswith("/"):
        text = p[0] + " " + text
    r = route(uk, text)
    if r is None:
        dl = danger_line(text)
        if dl and H.get("ask_ai"):            # danger signs: urgent line first, then the normal answer
            send(chat, dl)
            return False
        return False
    deliver(chat, r)
    return True


def web(uid, text):
    """Web chat: returns a reply dict for agent.py, or None to fall through to the normal chat."""
    r = route(f"web:{uid}", text)
    if r is None:
        dl = danger_line(text)
        if dl and H.get("ask_ai"):
            return web_after(uid, text, dl + "\n\n" + H["ask_ai"](f"web:{uid}", text))
        return None
    return to_web(r)


def on_callback(chat, uid, data, cb=None):
    uk = f"tg:{chat}"
    parts = data.split(":")
    kind = parts[1] if len(parts) > 1 else ""
    arg = ":".join(parts[2:])
    fn = CALLBACKS.get(kind)
    if not fn:
        return
    try:
        r = fn(uk, arg, chat, uid, cb or {}) if kind in CB_WANTS_CB else fn(uk, arg, chat, uid)
    except Exception as e:
        print("skills callback error:", kind, repr(e)[:200])
        r = R("Sorry, something went wrong. Please try again.")
    if r:
        deliver(chat, r)


def web_callback(who, data):
    """A tapped chip in the web chat: same handlers as Telegram buttons."""
    uk = f"web:{who}"
    parts = (data or "").split(":")
    if len(parts) < 2 or parts[0] != "sk":
        return {"reply": ""}
    fn = CALLBACKS.get(parts[1])
    if not fn:
        return {"reply": "That button has expired."}
    try:
        r = fn(uk, ":".join(parts[2:]), None, None, {}) if parts[1] in CB_WANTS_CB else fn(uk, ":".join(parts[2:]), None, None)
    except Exception as e:
        print("web chip error:", repr(e)[:200])
        r = R("Sorry, something went wrong. Please try again.")
    if r:
        remember_output(uk, r)
    return to_web(r) if r else {"reply": "\u2705"}


def _cb_unwatch(uk, arg, chat, uid):
    q("delete from sk_wallets where id=? and uk=?", (int(arg), uk))
    return R("\U0001F5D1 Wallet removed.")


def _cb_unalert(uk, arg, chat, uid):
    q("delete from sk_alerts where id=? and uk=?", (int(arg), uk))
    return R("\U0001F5D1 Alert removed.")


def _cb_unpage(uk, arg, chat, uid):
    q("delete from sk_pages where id=? and uk=?", (int(arg), uk))
    return R("\U0001F5D1 Stopped watching that page.")


CALLBACKS = {"uw": _cb_unwatch, "ua": _cb_unalert, "up": _cb_unpage}
CB_WANTS_CB = set()


def prompt_extra(user, text=""):
    """Extra system text for ask_ai: saved facts, lite mode and the open document."""
    out = ""
    try:
        f = facts(user)
        if f:
            out += (" FACTS THE USER ASKED YOU TO REMEMBER (use them when relevant, don't recite them): "
                    + "; ".join(f)[:1500] + ".")
        for fn in PROMPT_HOOKS:
            out += fn(user, text) or ""
    except Exception as e:
        print("prompt_extra error:", e)
    return out


PROMPT_HOOKS = []


def delete_user(chat):
    """Called by /deletemydata: removes everything skills stored for this Telegram chat."""
    uk = f"tg:{chat}"
    for t in ["sk_facts", "sk_wallets", "sk_alerts", "sk_briefing", "sk_pages", "sk_follows", "sk_match", "sk_spend",
              "sk_flags", "sk_webpages", "sk_jobs", "sk_docs", "sk_ajo", "sk_study", "sk_verse", "sk_feedback"]:
        try:
            q(f"delete from {t} where uk=?", (uk,))
        except Exception as e:
            print("skills delete error:", t, e)
    try:
        q("delete from sk_referrals where referee=?", (uk,))
    except Exception:
        pass
    _facts_cache.pop(uk, None)


# ---------- batch 1 routes ----------
route_add("watch", r"^(?:/watch|(?:please\s+)?(?:watch|track|monitor) (?:my |this )?(?:wallet|address)(?: address)?:?)\s+(\S{20,100})\s*$", lambda uk, m, t: wallet_watch(uk, m.group(1)), 10)
route_add("watch_help", r"^/watch\s*$", lambda uk, m, t: R("Send /watch <public wallet address>. Works with TRON USDT (T...), Bitcoin and Ethereum. Max 3 wallets."), 10)
route_add("unwatch", r"^/unwatch\b\s*(.*)$", lambda uk, m, t: wallet_unwatch(uk, m.group(1)), 10)
route_add("wallets", r"^(/wallets|my wallets)\s*$", lambda uk, m, t: wallet_list(uk), 10)
route_add("alerts", r"^(/alerts|my (price )?alerts)\s*$", lambda uk, m, t: alert_list(uk), 10)
route_add("alert", r"^/alert\b|\b(alert|tell|notify|ping|message) me (when|if|once)\b.{0,40}\b(hits?|reach(es)?|gets? to|above|below|over|under|goes|drops?|falls?|rises?|crosses|is at)\b", lambda uk, m, t: alert_add(uk, t), 20)
route_add("rate", r"^(/rate|/naira)\s*$", lambda uk, m, t: R(rate_text() + "\nStreet (parallel) rates differ; ask \"black market dollar rate today\" for news-based figures."), 10)
route_add("briefing", r"^/briefing\b\s*(.*)$", lambda uk, m, t: briefing_cmd(uk, m.group(1)), 10)
route_add("watchpage", r"^(?:/watchpage|watch (?:this )?(?:page|site|website|link) (?:for changes)?:?)\s*(\S*)\s*$", lambda uk, m, t: watchpage_cmd(uk, m.group(1)), 10)
route_add("pages", r"^/pages\s*$", lambda uk, m, t: pages_list(uk), 10)
route_add("memory", r"^(/memory|what do you (remember|know) about me\??)\s*$", lambda uk, m, t: memory_list(uk), 10)
route_add("forget", r"^(/forget|forget (everything|all)( about me| you know about me)?|wipe (my )?memory)\s*[.!]?$", lambda uk, m, t: forget_cmd(uk), 10)
route_add("remember", REMEMBER_RE.pattern, lambda uk, m, t: remember_cmd(uk, m.group(1)), 30)
WEATHER_STOP = {"the", "nice", "good", "bad", "hot", "cold", "this", "today's", "todays", "what", "how", "lovely", "rainy", "sunny", "my", "your", "our"}
route_add("weather", r"^(?:/weather\b\s*(.*)|(?:what(?:'s| is) the )?weather (?:in|for|at|like in) ([a-z][a-z .'-]{1,40}?)(?: today| now| tomorrow)?|(?:how is|how's) the weather (?:in|at) ([a-z][a-z .'-]{1,40}?)(?: today| now)?|([a-z][a-z .'-]{1,30}) weather(?: today| now)?)\s*\??$",
          lambda uk, m, t: None if (m.group(4) and m.group(4).strip().lower() in WEATHER_STOP) else weather(uk, next((g for g in m.groups() if g is not None), "")), 40)
route_add("ics", r"^(/ics|/calendar|add (this )?to (my )?calendar|calendar( event)?:|make (a |an )?(calendar|ics)( file| event)?)\b", lambda uk, m, t: ics_cmd(uk, t), 10)
route_add("units", r"^(/units\s+|convert\s+|what is\s+|what's\s+)?-?\d[\d,.]*\s*[a-z\u00b0/ ]{1,14}\s+(to|in|into)\s+[a-z\u00b0/ ]{1,14}\??$|^how many [a-z\u00b0/]+ (are )?(in|is) \d",
          lambda uk, m, t: units_reply(t) if quota(uk, "units") else over("units"), 45)
route_add("tz", r"^(/time\s+|what(?:'s| is) the time in |what time is it in |time in |current time in )|^(convert |what(?:'s| is) )?\d{1,2}([:.]\d{2})?\s*(am|pm)?\s+([a-z ]+ )?(time )?(to|in) [a-z ]+$",
          lambda uk, m, t: tz_reply(t), 46)
route_add("follow", r"^/follow\b\s*(.*)$", lambda uk, m, t: follow_cmd(uk, m.group(1)), 10)
route_add("unfollow", r"^/unfollow\b\s*(.*)$", lambda uk, m, t: unfollow_cmd(uk, m.group(1)), 10)
route_add("following", r"^/following\s*$", lambda uk, m, t: R("Following: " + (", ".join(r[0] for r in (q("select team from sk_follows where uk=?", (uk,), fetch=True) or [])) or "nobody yet. Try /follow Arsenal")), 10)
route_add("spend", r"^(/spent|/spend|i (just )?spent|spent|i paid|paid)\s+(\u20a6|n|ngn)?\s*\d", lambda uk, m, t: spend_cmd(uk, t), 30)
route_add("budget_reset", r"^/budget reset\s*$", lambda uk, m, t: (q("delete from sk_spend where uk=?", (uk,)), R("Budget history cleared."))[1], 9)
route_add("budget", r"^(/budget|my budget|my spending)\b\s*(.*)$", lambda uk, m, t: budget_cmd(uk, m.group(2)), 10)


# =====================================================================================
# BATCH 2: files and tools
# =====================================================================================
# ---------- 23. Group chat mode ----------
def group_skip(upd):
    """In Telegram groups, only react when the bot is @mentioned, replied to, or a command names it."""
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return False
    if (msg.get("chat") or {}).get("type") not in ("group", "supergroup"):
        return False
    if upd.get("edited_message"):
        return True
    text = msg.get("text") or msg.get("caption") or ""
    me = "@" + BOT_USERNAME.lower()
    replied = ((msg.get("reply_to_message") or {}).get("from") or {}).get("username", "").lower() == BOT_USERNAME.lower()
    mentioned = me in text.lower()
    if not (mentioned or replied):
        return True
    clean = re.sub(re.escape(me), "", text, flags=re.I).strip() if mentioned else text
    if msg.get("text") is not None:
        msg["text"] = clean or "hi"
    elif msg.get("caption") is not None:
        msg["caption"] = clean
    return False


# ---------- 11/15. Documents: read, Q&A, translate ----------
DOC_MIMES = {"application/pdf": "pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx"}
DOC_TTL = 2 * 3600
MAX_DOC_BYTES = 15_000_000


def doc_kind(name, mime=""):
    k = DOC_MIMES.get(mime or "")
    if k:
        return k
    n = (name or "").lower()
    return "pdf" if n.endswith(".pdf") else "docx" if n.endswith(".docx") else None


def is_doc(doc):
    return bool(doc) and bool(doc_kind(doc.get("file_name"), doc.get("mime_type")))


def extract_text(data, kind):
    if kind == "pdf":
        from pypdf import PdfReader
        rd = PdfReader(io.BytesIO(data))
        if rd.is_encrypted:
            try:
                rd.decrypt("")
            except Exception:
                return None
        return "\n".join((p.extract_text() or "") for p in rd.pages[:80]).strip()
    from docx import Document
    d = Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs]
    for tb in d.tables:
        for row in tb.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(parts).strip()


def save_doc(uk, name, body):
    q("insert into sk_docs (uk, name, body, ts) values (?, ?, ?, ?) on conflict (uk) do update set name=excluded.name, body=excluded.body, ts=excluded.ts",
      (uk, name[:120], body[:60000], time.time()))


def get_doc(uk):
    r = q("select name, body, ts from sk_docs where uk=?", (uk,), fetch=True)
    if r and time.time() - r[0][2] < DOC_TTL:
        return r[0][0], r[0][1]
    return None


def doc_chunks(body, question, n=3, size=1500):
    chunks = [body[i:i + size] for i in range(0, len(body), size)] or [""]
    words = {w for w in re.findall(r"[a-z0-9]{4,}", question.lower())}
    scored = sorted(range(len(chunks)), key=lambda i: -sum(chunks[i].lower().count(w) for w in words))
    pick = sorted(scored[:n]) if words else list(range(min(n, len(chunks))))
    return "\n...\n".join(chunks[i] for i in pick)


def _doc_hook(user, text):
    d = get_doc(user)
    if not d or not text:
        return ""
    return (f" The user recently uploaded a document named '{d[0]}'. If their message is about it, answer from these "
            f"excerpts and say when the document doesn't cover it. EXCERPTS: {doc_chunks(d[1], text)[:4500]}")


PROMPT_HOOKS.append(_doc_hook)
TRANSLATE_RE = re.compile(r"\btranslate\b(?:\s+(?:it|this|that|the (?:document|doc|file|pdf)|my (?:document|doc|file|pdf)))?\s+(?:in)?to\s+([A-Za-z][A-Za-z ()-]{1,30})", re.I)


def doc_reply(uk, name, body, ask):
    if not body or len(body) < 20:
        return R("I couldn't find any text in that file. If it's a scanned PDF (photos of pages), send the pages as photos instead.")
    save_doc(uk, name, body)
    m = TRANSLATE_RE.search(ask or "")
    if m:
        return translate_doc(uk, m.group(1).strip())
    if ask and ask.strip():
        ans = llm("You answer questions about a document for a user in Nigeria. Use only the excerpts; say if the answer "
                  "isn't there. Plain text, short, same language as the question.",
                  f"Question: {ask}\n\nDocument '{name}' excerpts:\n{doc_chunks(body, ask)[:6000]}")
        return R(ans or "I couldn't answer that from the document.")
    summ = llm("Summarise this document for a busy student or worker in Nigeria: 1 line on what it is, then 4-6 short "
               "bullet points with the key facts, numbers and dates. Plain text, '- ' bullets.", body[:9000])
    pages = f" ({len(body):,} characters)"
    return R(f"\U0001F4C4 {name}{pages}\n\n{summ}\n\nAsk me anything about it for the next 2 hours, e.g. \"what are the deadlines?\" "
             "or \"translate it to French\".")


def tg_document(chat, doc, caption):
    uk = f"tg:{chat}"
    if not gate_ok(uk):
        return
    if (doc.get("file_size") or 0) > MAX_DOC_BYTES:
        return send(chat, "That file is too big. Please send a PDF or Word file under 15 MB.")
    if not quota(uk, "doc"):
        return deliver(chat, over("doc"))
    typing(chat, "upload_document")
    try:
        info = requests.get(tg_api("getFile"), params={"file_id": doc["file_id"]}, timeout=15).json()["result"]
        data = requests.get(f"https://api.telegram.org/file/bot{H['token']}/{info['file_path']}", timeout=60).content
        kind = doc_kind(doc.get("file_name"), doc.get("mime_type"))
        body = extract_text(data, kind)
        r = doc_reply(uk, doc.get("file_name") or f"document.{kind}", body, caption)
    except Exception as e:
        print("document error:", repr(e)[:200])
        r = R("Sorry, I couldn't read that file. Make sure it's a normal PDF or .docx (not password-protected).")
    deliver(chat, r)


def web_doc(who, data, name, ask):
    uk = f"web:{who}"
    kind = doc_kind(name)
    if not kind:
        return {"reply": "Please upload a PDF or .docx file."}
    if not quota(uk, "doc"):
        return to_web(over("doc"))
    try:
        return to_web(doc_reply(uk, name, extract_text(data, kind), ask))
    except Exception as e:
        print("web doc error:", repr(e)[:200])
        return {"reply": "Sorry, I couldn't read that file. Make sure it's a normal PDF or .docx (not password-protected)."}


def make_docx(title, paragraphs):
    from docx import Document
    d = Document()
    d.add_heading(title[:120], 1)
    for p in paragraphs:
        d.add_paragraph(p)
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def translate_doc(uk, lang):
    d = get_doc(uk)
    if not d:
        return R("Send me the PDF or Word file first (with the caption \"translate to French\"), then I'll translate it.")
    if not quota(uk, "translate"):
        return over("translate")
    name, body = d
    body = body[:21000]
    chunks, cur = [], ""
    for para in body.split("\n"):
        if len(cur) + len(para) > 3500 and cur:
            chunks.append(cur)
            cur = ""
        cur += para + "\n"
    if cur.strip():
        chunks.append(cur)
    out = []
    for ch in chunks[:6]:
        out.append(llm(f"Translate the user's text into {lang}. Keep paragraphs and numbers. Output only the translation.", ch))
    paras = [p for p in "\n".join(out).split("\n") if p.strip()]
    data = make_docx(f"{name} ({lang})", paras)
    note = " (first ~21,000 characters)" if len(d[1]) > 21000 else ""
    fname = (re.sub(r"[^A-Za-z0-9]+", "_", name.rsplit(".", 1)[0])[:30].strip("_") or "document") + f"_{lang[:12].replace(' ', '_')}.docx"
    return R(f"\U0001F310 Here's your document in {lang}{note}. Machine translation: check names and key terms.",
             file=(fname, data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))


# ---------- 12. Spreadsheets and charts ----------
def make_xlsx(spec):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = re.sub(r"[\[\]:*?/\\]", "", str(spec.get("sheet") or spec.get("title") or "Sheet1"))[:30] or "Sheet1"
    headers = [str(h) for h in (spec.get("headers") or [])][:20]
    rows = [r[:20] for r in (spec.get("rows") or []) if isinstance(r, list)][:500]
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="7B2FF7")
    for r in rows:
        ws.append([v if isinstance(v, (int, float)) or (isinstance(v, str) and v.startswith("=")) else str(v) for v in r])
    numeric = [i for i in range(len(headers)) if rows and all(isinstance(r[i], (int, float)) for r in rows if i < len(r))]
    if spec.get("totals") and numeric and rows:
        n = len(rows) + 1
        tot = ["Total"] + [""] * (len(headers) - 1)
        for i in numeric:
            if i:
                tot[i] = f"=SUM({get_column_letter(i + 1)}2:{get_column_letter(i + 1)}{n})"
        ws.append(tot)
        for c in ws[n + 1]:
            c.font = Font(bold=True)
    for i, h in enumerate(headers, 1):
        width = max([len(str(h))] + [len(str(r[i - 1])) for r in rows if i - 1 < len(r)])
        ws.column_dimensions[get_column_letter(i)].width = min(max(10, width + 2), 50)
    ws.freeze_panes = "A2"
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def xlsx_cmd(uk, ask):
    if len(ask.strip()) < 3:
        return R("Tell me what the spreadsheet is for, e.g. /xlsx monthly budget for a student with rent, food, transport, data")
    if not quota(uk, "xlsx"):
        return over("xlsx")
    spec = llm_json('You design spreadsheets. Return {"title": str, "sheet": str, "headers": [str], "rows": [[values]], "totals": bool}. '
                    "Use numbers (not strings) for numeric cells, naira amounts as plain numbers, max 40 rows. Use the "
                    "user's data exactly when given; otherwise make a sensible template with example rows.", ask)
    if not isinstance(spec, dict) or not spec.get("headers"):
        return R("Sorry, I couldn't build that spreadsheet. Try describing the columns you want.")
    title = str(spec.get("title") or "Spreadsheet")[:60]
    fname = (re.sub(r"[^A-Za-z0-9]+", "_", title)[:30].strip("_") or "sheet") + ".xlsx"
    return R(f"\U0001F4CA {title}: opens in Excel, Google Sheets or WPS.",
             file=(fname, make_xlsx(spec), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))


PAIR_RE = re.compile(r"([A-Za-z][A-Za-z0-9 &/'.-]{0,30}?)\s*[:=\-]?\s*(?:\u20a6|N|\$)?\s*(\d[\d,]*(?:\.\d+)?\s*[kKmM]?)\b")


def parse_pairs(text):
    out = []
    for lab, val in PAIR_RE.findall(text):
        lab = re.sub(r"^(and|with|of|for|chart|graph|bar|line|pie|make|me|a)\s+", "", lab.strip(), flags=re.I).strip()
        v = parse_amount(val)
        if lab and v is not None:
            out.append((lab[:25], v))
    return out


def make_chart(title, labels, values, kind="bar", ylabel=""):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8, 5), dpi=110)
    colors = ["#7b2ff7", "#ff6a00", "#c2189b", "#00a86b", "#1e90ff", "#ffb300", "#e53935", "#5d4037"]
    if kind == "pie":
        ax.pie(values, labels=labels, autopct="%1.0f%%", colors=colors[:len(values)] * 3, startangle=90)
        ax.axis("equal")
    elif kind == "line":
        ax.plot(labels, values, marker="o", color=colors[0], linewidth=2.5)
        ax.grid(alpha=0.3)
    else:
        bars = ax.bar(labels, values, color=colors[:len(values)] * 3)
        for b, v in zip(bars, values):
            ax.annotate(f"{v:,.0f}", (b.get_x() + b.get_width() / 2, b.get_height()), ha="center", va="bottom", fontsize=9)
        ax.grid(axis="y", alpha=0.3)
    if kind != "pie":
        ax.set_ylabel(ylabel)
        plt.setp(ax.get_xticklabels(), rotation=20 if len(labels) > 5 else 0, ha="right" if len(labels) > 5 else "center")
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, p: f"{x:,.0f}"))
    ax.set_title(title, fontsize=14, fontweight="bold")
    fig.text(0.99, 0.01, "Tygar Bot", ha="right", fontsize=7, color="#888")
    fig.tight_layout()
    b = io.BytesIO()
    fig.savefig(b, format="png")
    plt.close(fig)
    return b.getvalue()


def chart_cmd(uk, ask):
    if not quota(uk, "chart"):
        return over("chart")
    kind = "pie" if re.search(r"\bpie\b", ask, re.I) else "line" if re.search(r"\b(line|trend|over time)\b", ask, re.I) else "bar"
    body = re.sub(r"^\s*(/chart|(make|draw|create|plot|give) (me )?(an? )?(bar |line |pie )?(chart|graph)( of| for| showing)?:?)\s*", "", ask, flags=re.I)
    pairs = parse_pairs(body)
    title = re.split(r"[:\n]", body)[0][:60] if ":" in body else "Chart"
    if len(pairs) < 2:
        spec = llm_json('Extract chart data from the user text. Return {"title": str, "labels": [str], "values": [number], "ylabel": str}. '
                        "Use ONLY numbers the user wrote; if there are none, return {\"labels\": []}.", body)
        if isinstance(spec, dict) and len(spec.get("labels") or []) >= 2 and len(spec.get("labels")) == len(spec.get("values") or []):
            try:
                pairs = list(zip([str(x)[:25] for x in spec["labels"]], [float(v) for v in spec["values"]]))
                title = str(spec.get("title") or title)[:60]
            except (TypeError, ValueError):
                pairs = []
    if len(pairs) < 2:
        return R("Give me the numbers, e.g. /chart my spending: rent 50000, food 30000, transport 15000, data 5000 (add \"pie\" or \"line\" for other styles).")
    png = make_chart(title, [p[0] for p in pairs[:20]], [p[1] for p in pairs[:20]], kind)
    return R(f"\U0001F4C8 {title}", photo=png)


# ---------- 13. Slides ----------
def make_pptx(spec):
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    purple = RGBColor(0x7B, 0x2F, 0xF7)

    def band(slide):
        sh = slide.shapes.add_shape(1, 0, 0, prs.slide_width, Inches(0.35))
        sh.fill.solid()
        sh.fill.fore_color.rgb = purple
        sh.line.fill.background()

    s0 = prs.slides.add_slide(prs.slide_layouts[0])
    s0.shapes.title.text = str(spec.get("title") or "Presentation")[:120]
    if len(s0.placeholders) > 1:
        s0.placeholders[1].text = str(spec.get("subtitle") or "Made with Tygar Bot")[:200]
    band(s0)
    for sl in (spec.get("slides") or [])[:15]:
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = str(sl.get("title") or "")[:120]
        s.shapes.title.text_frame.paragraphs[0].font.color.rgb = purple
        body = s.placeholders[1].text_frame
        body.clear()
        for j, b in enumerate((sl.get("bullets") or [])[:7]):
            p = body.paragraphs[0] if j == 0 else body.add_paragraph()
            p.text = str(b)[:220]
            p.font.size = Pt(22)
        if sl.get("notes"):
            s.notes_slide.notes_text_frame.text = str(sl["notes"])[:1500]
        band(s)
    b = io.BytesIO()
    prs.save(b)
    return b.getvalue()


def slides_cmd(uk, ask):
    topic = re.sub(r"^\s*(/slides|/pptx|(make|create|prepare|generate) (me )?(a |some )?(powerpoint|presentation|slides|slide deck|pptx)( on| about| for)?:?)\s*", "", ask, flags=re.I).strip()
    if len(topic) < 3:
        return R("What's the presentation about? e.g. /slides the causes of inflation in Nigeria, 8 slides")
    if not quota(uk, "slides"):
        return over("slides")
    spec = llm_json('You make clear school/business presentations. Return {"title": str, "subtitle": str, "slides": '
                    '[{"title": str, "bullets": [str], "notes": str}]}. 6-10 slides, 3-5 short bullets each, accurate facts, '
                    "last slide = summary or conclusion.", topic)
    if not isinstance(spec, dict) or not spec.get("slides"):
        return R("Sorry, I couldn't make those slides. Try a clearer topic.")
    title = str(spec.get("title") or topic)[:60]
    fname = (re.sub(r"[^A-Za-z0-9]+", "_", title)[:30].strip("_") or "slides") + ".pptx"
    return R(f"\U0001F4FD {title} ({len(spec['slides']) + 1} slides, speaker notes included). Opens in PowerPoint, Google Slides or WPS.",
             file=(fname, make_pptx(spec), "application/vnd.openxmlformats-officedocument.presentationml.presentation"))


# ---------- 14. Mini web page at /p/<id> ----------
PAGE_TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><meta name="description" content="{tagline}"><style>
*{{box-sizing:border-box}}body{{margin:0;font-family:"Segoe UI",system-ui,sans-serif;color:#1d1d1f;background:#faf9fc;line-height:1.6}}
header{{background:linear-gradient(135deg,{color},#111);color:#fff;padding:64px 22px 54px;text-align:center}}
h1{{margin:0 0 8px;font-size:clamp(28px,6vw,46px)}}header p{{margin:0;font-size:19px;opacity:.92}}
main{{max-width:760px;margin:0 auto;padding:28px 20px}}section{{background:#fff;border-radius:16px;padding:22px;margin:0 0 16px;box-shadow:0 2px 14px #0001}}
h2{{margin:0 0 8px;color:{color};font-size:22px}}.c a{{display:inline-block;margin:6px 8px 0 0;padding:10px 18px;border-radius:30px;background:{color};color:#fff;text-decoration:none;font-weight:700}}
footer{{text-align:center;color:#888;font-size:13px;padding:24px}}</style></head><body>
<header><h1>{title}</h1><p>{tagline}</p></header><main>{sections}{contact}</main>
<footer>Made with Tygar Bot \u00b7 user-created page, not verified by Tygar</footer></body></html>"""


def render_page(spec):
    e = lambda x: _html.escape(str(x or ""))[:1500]
    color = spec.get("color") if re.fullmatch(r"#[0-9a-fA-F]{6}", str(spec.get("color") or "")) else "#7b2ff7"
    secs = ""
    if spec.get("about"):
        secs += f"<section><h2>About</h2><p>{e(spec['about'])}</p></section>"
    for s in (spec.get("sections") or [])[:8]:
        secs += f"<section><h2>{e(s.get('heading'))}</h2><p>{e(s.get('text'))}</p></section>"
    c = spec.get("contact") or {}
    links = ""
    phone = re.sub(r"[^\d+]", "", str(c.get("phone") or ""))[:16]
    wa = re.sub(r"[^\d]", "", str(c.get("whatsapp") or ""))[:15]
    if phone:
        links += f'<a href="tel:{phone}">\U0001F4DE Call {e(c.get("phone"))}</a>'
    if wa:
        links += f'<a href="https://wa.me/{wa}">WhatsApp</a>'
    if re.fullmatch(r"[^@\s\"<>]+@[^@\s\"<>]+\.[a-z]{2,}", str(c.get("email") or ""), re.I):
        links += f'<a href="mailto:{e(c["email"])}">\u2709\uFE0F Email</a>'
    addr = f"<p>{e(c.get('address'))}</p>" if c.get("address") else ""
    contact = f'<section class="c"><h2>Contact</h2>{addr}{links}</section>' if (links or addr) else ""
    return PAGE_TEMPLATE.format(title=e(spec.get("title") or "My page"), tagline=e(spec.get("tagline")), color=color,
                                sections=secs, contact=contact)


def webpage_cmd(uk, ask):
    desc = re.sub(r"^\s*(/webpage|/site|(make|create|build|design) (me )?(a |an )?(simple |mini |small )?(web ?page|website|landing page|site)( for| about)?:?)\s*", "", ask, flags=re.I).strip()
    if len(desc) < 8:
        return R("Describe the page: what it's for and the details to show, e.g. /webpage for my bakery Ada's Cakes in Enugu, "
                 "custom birthday cakes from \u20a615,000, WhatsApp 08031234567")
    if not quota(uk, "webpage"):
        return over("webpage")
    spec = llm_json('You write a one-page website for a small business, event or person in Nigeria. Return {"title": str, '
                    '"tagline": str, "about": str, "sections": [{"heading": str, "text": str}], "contact": {"phone": str, '
                    '"whatsapp": str (international digits, e.g. 2348031234567), "email": str, "address": str}, "color": "#rrggbb"}. '
                    "3-5 sections. Only use contact details the user gave; leave others empty. No prices or claims the user didn't give.", desc)
    if not isinstance(spec, dict) or not spec.get("title"):
        return R("Sorry, I couldn't build that page. Try again with a bit more detail.")
    pid = base64.urlsafe_b64encode(os.urandom(6)).decode().rstrip("=")
    page = render_page(spec)
    q("insert into sk_webpages (id, uk, html, created) values (?, ?, ?, ?)", (pid, uk, page, time.time()))
    old = q("select id from sk_webpages where uk=? order by created desc", (uk,), fetch=True) or []
    for (oid,) in old[10:]:
        q("delete from sk_webpages where id=?", (oid,))
    url = f"{H.get('base_url') or ''}/p/{pid}"
    return R(f"\U0001F310 Your page is live: {url}\nShare the link anywhere. You keep up to 10 pages; delete with /deletemydata.")


@bp.get("/p/<pid>")
def serve_page(pid):
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,16}", pid or ""):
        return Response("Not found", 404)
    try:
        r = q("select html from sk_webpages where id=?", (pid,), fetch=True)
    except Exception:
        r = None
    if not r:
        return Response("<h2 style='font-family:sans-serif'>This page doesn't exist (or was deleted).</h2>", 404, mimetype="text/html")
    resp = Response(r[0][0], mimetype="text/html")
    resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src https: data:"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


# ---------- 16. YouTube summary ----------
YT_RE = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def yt_transcript(vid):
    from youtube_transcript_api import YouTubeTranscriptApi
    langs = ["en", "en-US", "en-GB", "fr", "pcm", "yo", "ig", "ha"]
    try:
        fetched = YouTubeTranscriptApi().fetch(vid, languages=langs)          # v1.x API
        return " ".join(getattr(s, "text", "") for s in fetched)
    except AttributeError:
        return " ".join(x["text"] for x in YouTubeTranscriptApi.get_transcript(vid, languages=langs))   # older API


def youtube_cmd(uk, text, vid):
    if not quota(uk, "yt"):
        return over("yt")
    try:
        tr = yt_transcript(vid)
    except Exception as e:
        print("youtube transcript error:", type(e).__name__, str(e)[:120])
        tr = ""
    if not tr or len(tr) < 50:
        return R("I can't fetch this video's captions right now (it may have no captions, or YouTube is blocking my server). "
                 "Paste the transcript or tell me the title and I'll help.")
    ask = YT_RE.sub("", re.sub(r"https?://\S+", "", text)).strip()
    out = llm("Summarise this YouTube video transcript for a viewer in Nigeria: one line on what it's about, then 5-7 short "
              "bullets with the key points (with numbers/names), then one line 'Worth watching if: ...'. Plain text. "
              + (f"Also answer the user's request: {ask}" if len(ask) > 3 else ""), tr[:11000])
    return R(f"\U0001F3AC Video summary\n\n{out}" + ("\n\n(Based on the first part of a long video.)" if len(tr) > 11000 else ""))


# ---------- 17. Recipe + market list in naira ----------
def recipe_cmd(uk, dish):
    dish = dish.strip(" ?.")
    if len(dish) < 3:
        return R("Which dish? e.g. /recipe jollof rice for 6 people")
    if not quota(uk, "recipe"):
        return over("recipe")
    spec = llm_json('You are a Nigerian home cook. Return {"dish": str, "serves": int, "ingredients": [{"item": str, "qty": str}], '
                    '"steps": [str], "time": str}. Use local ingredient names and market units (derica, paint bucket, '
                    "tuber, bunch, sachet).", dish)
    if not isinstance(spec, dict) or not spec.get("ingredients"):
        return R("Sorry, I couldn't put that recipe together. Try another dish name.")
    items = [str(i.get("item")) for i in spec["ingredients"]][:12]
    month = wat_now().strftime("%B %Y")
    found = wsearch(f"current market price {', '.join(items[:5])} Nigeria {month}")
    prices = llm_json('Estimate Nigerian market prices in naira for each ingredient and quantity using the search results when they '
                      'have prices; otherwise give a cautious typical range. Return {"lines": [{"item": str, "qty": str, "naira_low": number, '
                      '"naira_high": number}], "basis": "short note on sources and date"}.',
                      json.dumps(spec["ingredients"][:12]) + "\n\nSearch results:\n" + found[:4000])
    lines = [f"\U0001F372 {spec.get('dish', dish).title()} (serves {spec.get('serves', '?')}, {spec.get('time', '')})".replace(", )", ")"), "",
             "\U0001F6D2 Market list (estimate):"]
    lo = hi = 0
    for ln in (prices or {}).get("lines", [])[:14]:
        try:
            a, b = float(ln.get("naira_low") or 0), float(ln.get("naira_high") or 0)
        except (TypeError, ValueError):
            a = b = 0
        lo, hi = lo + a, hi + b
        lines.append(f"\u2022 {ln.get('item')} ({ln.get('qty')}): {money(a)}-{money(b)}" if b else f"\u2022 {ln.get('item')} ({ln.get('qty')})")
    if not (prices or {}).get("lines"):
        lines += [f"\u2022 {i.get('item')} ({i.get('qty')})" for i in spec["ingredients"][:14]]
    if hi:
        lines.append(f"Total: about {money(lo)}-{money(hi)}")
    lines += ["", "\U0001F468\u200D\U0001F373 Steps:"] + [f"{i + 1}. {s}" for i, s in enumerate(spec.get("steps", [])[:12])]
    lines += ["", f"Prices are estimates for {month} ({(prices or {}).get('basis', 'web search + typical ranges')}); they vary by market and city."]
    return R("\n".join(lines))


# ---------- 18. Exam quiz (WAEC / NECO / JAMB) with buttons ----------
SUBJECTS = ["English", "Mathematics", "Biology", "Chemistry", "Physics", "Economics", "Government", "Literature",
            "Geography", "Commerce", "Accounting", "CRS", "IRS", "Agric", "Civic Education"]
QUIZ = {}


def quiz_cmd(uk, arg):
    a = (arg or "").strip()
    exam = next((e for e in ("JAMB", "WAEC", "NECO", "UTME", "GCE", "POST-UTME") if re.search(rf"\b{e}\b", a, re.I)), "WAEC")
    subj = re.sub(r"\b(jamb|waec|neco|utme|gce|post-utme|quiz|questions?|on|in|for|practice|give|me|a|an|some|start)\b", "", a, flags=re.I).strip(" ,.")
    if not subj:
        kb = [[{"text": s, "callback_data": f"sk:qzs:{exam}:{s[:20]}"} for s in SUBJECTS[i:i + 3]] for i in range(0, len(SUBJECTS), 3)]
        return R(f"\U0001F4DA {exam} practice: pick a subject (or type /quiz jamb physics)", {"inline_keyboard": kb})
    return quiz_start(uk, exam, subj)


def quiz_start(uk, exam, subj):
    if not quota(uk, "quiz"):
        return over("quiz")
    spec = llm_json(f'You set {exam} past-question-style multiple choice questions for Nigerian students. Return {{"questions": '
                    '[{"q": str, "options": [str, str, str, str], "answer": 0-3, "explain": str}]}. Exactly 5 questions on the '
                    "subject, syllabus level, one clearly correct option, short explanation (max 25 words). Double-check answers.",
                    f"Subject: {subj}")
    qs = [x for x in (spec or {}).get("questions", []) if isinstance(x, dict) and len(x.get("options") or []) == 4
          and isinstance(x.get("answer"), int) and 0 <= x["answer"] <= 3][:5]
    if len(qs) < 3:
        return R("Sorry, I couldn't set that quiz right now. Try again in a minute.")
    QUIZ[uk] = {"qs": qs, "i": 0, "score": 0, "exam": exam, "subj": subj[:20], "ts": time.time()}
    return quiz_question(uk)


def quiz_question(uk):
    s = QUIZ[uk]
    x = s["qs"][s["i"]]
    opts = "\n".join(f"{'ABCD'[j]}. {o}" for j, o in enumerate(x["options"]))
    kb = [[{"text": "ABCD"[j], "callback_data": f"sk:qz:{s['i']}:{j}"} for j in range(4)]]
    return R(f"\U0001F4DD {s['exam']} {s['subj']}: question {s['i'] + 1} of {len(s['qs'])}\n\n{x['q']}\n\n{opts}", {"inline_keyboard": kb})


def _cb_quiz(uk, arg, chat, uid):
    s = QUIZ.get(uk)
    i, j = (int(x) for x in arg.split(":"))
    if not s or i != s["i"]:
        return None                                   # old button: ignore
    x = s["qs"][i]
    ok = j == x["answer"]
    s["score"] += ok
    s["i"] += 1
    head = ("\u2705 Correct!" if ok else f"\u274C Not quite. Answer: {'ABCD'[x['answer']]}. {x['options'][x['answer']]}") + f"\n{x.get('explain', '')}"
    if s["i"] < len(s["qs"]):
        nxt = quiz_question(uk)
        nxt["text"] = head + "\n\n" + nxt["text"]
        return nxt
    QUIZ.pop(uk, None)
    n = len(s["qs"])
    msg = {n: "Excellent! \U0001F3C6", n - 1: "Great work! \U0001F4AA"}.get(s["score"], "Keep practising, you'll get there. \U0001F4D6")
    return R(f"{head}\n\n\U0001F3C1 Score: {s['score']}/{n}. {msg}",
             {"inline_keyboard": [[{"text": "\U0001F501 Another 5", "callback_data": f"sk:qzs:{s['exam']}:{s['subj']}"}]]})


def _cb_quiz_start(uk, arg, chat, uid):
    exam, _, subj = arg.partition(":")
    typing(chat)
    return quiz_start(uk, exam or "WAEC", subj or "English")


CALLBACKS.update({"qz": _cb_quiz, "qzs": _cb_quiz_start})


# ---------- 19. CV builder | 20. Invoice/receipt (PDF) ----------
def cv_cmd(uk, details):
    details = details.strip()
    if len(details) < 60:
        return R("Send your details in one message after /cv, for example:\n/cv Name: Ada Obi. Phone: 0803... Email: ada@... "
                 "Goal: entry-level accountant. Education: BSc Accounting, UNN 2024 (2:1). Experience: SIWES at First Bank 2023 (cash "
                 "reconciliation, customer service). Skills: Excel, QuickBooks, teamwork. Languages: English, Igbo. NYSC: completed 2025.")
    if not quota(uk, "cv"):
        return over("cv")
    out = llm("You write clean one- to two-page CVs for Nigerian job seekers. First line: the person's full name only. Then the "
              "CV in plain text using '# ' for section headings (Contact, Profile, Education, Experience, Skills, NYSC/Certifications, "
              "Referees: available on request) and '- ' bullets with strong action verbs. Use ONLY facts the user gave; never invent "
              "employers, dates or grades. No tables, no emojis.", details)
    name, _, body = out.partition("\n")
    name = name.strip("# *").strip()[:60] or "Curriculum Vitae"
    pdf = H["make_pdf"](name, body.strip())
    return R(f"\U0001F4C4 Your CV is ready, {name.split()[0] if name else ''}. Check every detail before sending.".replace(", .", "."),
             file=((re.sub(r"[^A-Za-z0-9]+", "_", name)[:30].strip("_") or "CV") + "_CV.pdf", pdf, "application/pdf"))


def invoice_totals(items, discount=0.0, delivery=0.0, vat_pct=0.0):
    lines = []
    sub = 0.0
    for it in items:
        try:
            qty = float(it.get("qty") or 1)
            price = float(str(it.get("unit_price") or 0).replace(",", ""))
        except (TypeError, ValueError):
            continue
        amt = round(qty * price, 2)
        sub += amt
        lines.append((str(it.get("desc") or "Item")[:60], qty, price, amt))
    disc = float(discount or 0)
    vat = round((sub - disc) * float(vat_pct or 0) / 100, 2)
    total = round(sub - disc + float(delivery or 0) + vat, 2)
    return lines, round(sub, 2), disc, float(delivery or 0), vat, total


def make_invoice_pdf(spec, lines, sub, disc, deliv, vat, total):
    from fpdf import FPDF
    L = H.get("latin") or (lambda x: x.encode("latin-1", "replace").decode("latin-1"))
    kind = "RECEIPT" if str(spec.get("kind", "")).lower().startswith("rec") else "INVOICE"
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 10, L(str(spec.get("business") or "My Business"))[:60], new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    if spec.get("business_contact"):
        pdf.cell(0, 6, L(str(spec["business_contact"]))[:90], new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(123, 47, 247)
    pdf.cell(0, 9, kind, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 10)
    num = str(spec.get("number") or f"{kind[:3]}-{wat_now().strftime('%y%m%d%H%M')}")
    pdf.cell(0, 6, L(f"No: {num}    Date: {wat_now().strftime('%d %b %Y')}"), new_x="LMARGIN", new_y="NEXT")
    if spec.get("customer"):
        pdf.cell(0, 6, L(f"Bill to: {spec['customer']}")[:100], new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_fill_color(123, 47, 247)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 10)
    for w, h in ((90, "Description"), (20, "Qty"), (40, "Unit (NGN)"), (40, "Amount (NGN)")):
        pdf.cell(w, 8, h, border=0, fill=True, align="L" if h == "Description" else "R")
    pdf.ln()
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 10)
    for desc, qty, price, amt in lines:
        pdf.cell(90, 7, L(desc)[:48], border="B")
        pdf.cell(20, 7, f"{qty:g}", border="B", align="R")
        pdf.cell(40, 7, f"{price:,.2f}", border="B", align="R")
        pdf.cell(40, 7, f"{amt:,.2f}", border="B", align="R")
        pdf.ln()
    pdf.ln(2)
    for label, v in (("Subtotal", sub), ("Discount", -disc if disc else None), ("Delivery", deliv or None), ("VAT", vat or None)):
        if v is not None:
            pdf.cell(150, 7, label, align="R")
            pdf.cell(40, 7, f"{v:,.2f}", align="R")
            pdf.ln()
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(150, 9, "TOTAL (NGN)", align="R")
    pdf.cell(40, 9, f"{total:,.2f}", align="R")
    pdf.ln(14)
    pdf.set_font("Helvetica", "", 10)
    if spec.get("payment"):
        pdf.multi_cell(0, 6, L("Payment: " + str(spec["payment"]))[:300], new_x="LMARGIN", new_y="NEXT")
    if spec.get("note"):
        pdf.multi_cell(0, 6, L(str(spec["note"]))[:400], new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 9)
    pdf.cell(0, 6, "Thank you for your business!" if kind == "INVOICE" else "Payment received. Thank you!")
    pdf.set_y(-14)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 6, "Made with Tygar Bot", align="C")
    return bytes(pdf.output())


def invoice_cmd(uk, ask):
    body = re.sub(r"^\s*(/invoice|/receipt|(make|create|generate|prepare|write) (me )?(an? )?(invoice|receipt)( for)?:?)\s*", "", ask, flags=re.I)
    if len(body) < 10:
        return R("Send the details, e.g. /invoice from Ada's Cakes to Mr Bello: 2 birthday cakes at 15000, delivery 2000, "
                 "pay to GTBank 0123456789 (Ada Obi)\nUse /receipt for a payment receipt.")
    if not quota(uk, "invoice"):
        return over("invoice")
    spec = llm_json('Extract invoice data. Return {"kind": "invoice"|"receipt", "business": str, "business_contact": str, '
                    '"customer": str, "items": [{"desc": str, "qty": number, "unit_price": number}], "discount": number, '
                    '"delivery": number, "vat_pct": number, "payment": str, "note": str}. Amounts in naira as plain numbers '
                    "(15k = 15000). Use only what the user wrote; 0 or empty when missing.",
                    ("RECEIPT. " if ask.lower().lstrip("/").startswith("receipt") or " receipt" in ask.lower()[:30] else "") + body)
    if not isinstance(spec, dict) or not spec.get("items"):
        return R("I couldn't find the items and prices. Write them like: 3 bags of cement at 9500, transport 5000")
    lines, sub, disc, deliv, vat, total = invoice_totals(spec["items"], spec.get("discount") or 0, spec.get("delivery") or 0, spec.get("vat_pct") or 0)
    if not lines:
        return R("I couldn't read the item prices. Write them like: 2 cakes at 15000")
    pdf = make_invoice_pdf(spec, lines, sub, disc, deliv, vat, total)
    kind = "Receipt" if str(spec.get("kind", "")).lower().startswith("rec") else "Invoice"
    return R(f"\U0001F9FE {kind} ready: total {money(total)} ({len(lines)} item{'s' if len(lines) > 1 else ''}). Check the details before sending.",
             file=(f"{kind}_{wat_now().strftime('%Y%m%d_%H%M')}.pdf", pdf, "application/pdf"))


# ---------- 21. QR codes ----------
def qr_cmd(uk, data):
    data = data.strip()
    if not data:
        return R("Send /qr followed by a link or text, e.g. /qr https://wa.me/2348031234567")
    if len(data) > 1000:
        return R("That's too long for a QR code (max 1000 characters).")
    if not quota(uk, "qr"):
        return over("qr")
    import qrcode
    img = qrcode.make(data, box_size=10, border=3)
    b = io.BytesIO()
    img.save(b, format="PNG")
    return R(f"\U0001F533 QR code for: {data[:200]}", photo=b.getvalue())


# ---------- 22. Job alerts ----------
def jobs_search_text(field, city):
    res = wsearch(f"{field} jobs in {city} Nigeria hiring {wat_now().strftime('%B %Y')}")
    lines = [l.strip() for l in res.splitlines() if l.strip().startswith("- ")][:7]
    if not lines:
        return None
    return (f"\U0001F4BC Jobs: {field} in {city} ({wat_now().strftime('%-d %b')})\n" + "\n".join(lines)[:3200] +
            "\n\nAlways apply on the employer's official site. Never pay anyone for a job. Stop: /jobs off")


def jobs_cmd(uk, arg):
    a = (arg or "").strip()
    if re.match(r"^(off|stop)\b", a, re.I):
        q("delete from sk_jobs where uk=?", (uk,))
        return R("Job alerts turned off.")
    m = re.match(r"^(?:on\s+|now\s+)?(.+?)\s+(?:jobs?\s+)?in\s+([A-Za-z .'-]{2,40})$", a, re.I)
    if not m:
        row = q("select field, city from sk_jobs where uk=?", (uk,), fetch=True)
        if row:
            return R(f"Daily job alerts: {row[0][0]} in {row[0][1]} (9am WAT). Change: /jobs on nursing in Abuja. Stop: /jobs off")
        return R("Send /jobs on <field> in <city>, e.g. /jobs on accounting in Lagos. I'll send fresh listings every morning at 9am.")
    if not quota(uk, "jobs"):
        return over("jobs")
    field, city = m.group(1).strip()[:40], m.group(2).strip()[:40]
    q("insert into sk_jobs (uk, field, city, last_day) values (?, ?, ?, ?) on conflict (uk) do update set field=excluded.field, city=excluded.city",
      (uk, field, city, wat_day()))
    first = jobs_search_text(field, city)
    head = f"\u2705 Job alerts on: {field} in {city}, every day at 9am WAT.\n\n"
    return R(head + (first or "No fresh listings found right now; I'll keep checking."))


def check_jobs():
    now = wat_now()
    if now.hour < 9:
        return
    today = now.strftime("%Y-%m-%d")
    for uk, field, city, last in q("select uk, field, city, last_day from sk_jobs where last_day<>?", (today,), fetch=True) or []:
        if not q("update sk_jobs set last_day=? where uk=? and last_day=? returning uk", (today, uk, last or ""), fetch=True):
            continue
        if now.hour >= 12:
            continue
        t = jobs_search_text(field, city)
        if t:
            notify(uk, t, "Job alerts", long=True)
        time.sleep(1)


JOBS.append(("jobs", 300, check_jobs))


# ---------- 24. Scam checker ----------
SCAM_FLAGS = [
    (r"\b(double|triple|multiply) (your|ur) (money|investment|cash)\b|\b\d{2,3}% (profit|return|roi)\b|guaranteed (profit|returns?|income)", "Promises guaranteed or very high returns"),
    (r"\b(registration|processing|activation|clearance|delivery|release|unlock)\s+fee\b|pay (a )?(small )?fee", "Asks you to pay a fee first"),
    (r"\b(urgent|immediately|within 24 ?hours|act now|last chance|limited slots?|today only)\b", "Pressure to act fast"),
    (r"\b(otp|bvn|pin|password|seed phrase|private key|card details|cvv|atm card)\b", "Asks for secret details (OTP, BVN, PIN, password or wallet keys)"),
    (r"\b(you (have )?won|winner|lottery|promo winner|selected for (a )?(grant|award))\b", "Says you won something you didn't enter"),
    (r"\b(gift ?card|itunes card|steam card|usdt only|crypto only|bitcoin only)\b", "Wants payment in gift cards or crypto"),
    (r"\b(work from home|earn) .{0,20}(daily|per day|a day)\b|\btask(s)? (and|to) earn\b|\blike (and|&) earn\b", "Easy-money job or task offer"),
    (r"\b(customer care|support) .{0,15}(whatsapp|telegram)\b|\bdm (me|us) on whatsapp\b", "'Support' only on WhatsApp/Telegram"),
    (r"bit\.ly|tinyurl|cutt\.ly|t\.co/|is\.gd|rb\.gy|shorturl", "Uses a hidden (shortened) link"),
    (r"https?://\S*(\d{1,3}\.){3}\d{1,3}|https?://\S*(-login|-verify|-secure|-update)\S*", "Suspicious-looking link"),
]
LOOKALIKE = re.compile(r"(?:paypa1|g00gle|faceb00k|0pay|opayy|palmpayy|m0niepoint|kud4|firstbank-|gtbank-|zenithbank-|access-bank-)", re.I)


def scam_flags(text):
    flags = [msg for rx, msg in SCAM_FLAGS if re.search(rx, text, re.I)]
    if LOOKALIKE.search(text):
        flags.append("Brand name spelt oddly (look-alike website)")
    return flags


def scam_cmd(uk, text):
    if len(text.strip()) < 8:
        return R("Paste the message or link after /scam (or reply to it with /scam) and I'll check it.")
    if not quota(uk, "scam"):
        return over("scam")
    flags = scam_flags(text)
    dom = re.search(r"(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)", text, re.I)
    found = ""
    if dom and "." in dom.group(1) and not re.fullmatch(r"[\d.]+", dom.group(1)):
        found = wsearch(f"{dom.group(1)} scam OR fraud OR reviews")[:3000]
    v = llm_json('You are a fraud-prevention adviser in Nigeria. Judge whether the message is a scam. Return {"verdict": '
                 '"Likely scam"|"Suspicious"|"Looks OK", "red_flags": [str], "advice": str}. Be concrete and short; '
                 "don't call something safe unless it clearly is. Use the web results only as hints.",
                 f"Message:\n{text[:3000]}\n\nLocal red flags found: {flags}\n\nWeb results about the domain:\n{found}")
    verdict = (v or {}).get("verdict") or ("Likely scam" if len(flags) >= 2 else "Suspicious" if flags else "Can't tell")
    red = list(dict.fromkeys(flags + [str(x) for x in (v or {}).get("red_flags", [])]))[:8]
    icon = {"Likely scam": "\U0001F6A8", "Suspicious": "\u26A0\uFE0F", "Looks OK": "\u2705"}.get(verdict, "\U0001F914")
    out = [f"{icon} Verdict: {verdict}"]
    if red:
        out += ["", "Red flags:"] + [f"\u2022 {x}" for x in red]
    out += ["", (v or {}).get("advice") or "Don't send money or codes. Verify through the company's official number or website.",
            "", "Never share OTP, PIN, BVN or wallet seed phrases. Report fraud: your bank, or the EFCC (efcc.gov.ng)."]
    return R("\n".join(out))


# ---------- batch 2 routes ----------
route_add("translate", TRANSLATE_RE.pattern, lambda uk, m, t: translate_doc(uk, m.group(1).strip()) if get_doc(uk) and re.match(r"^\s*(please\s+)?(can you\s+)?translate\s+(it|this|that|the|my)\b", t, re.I) else None, 15)
route_add("xlsx", r"^(/xlsx|/excel|/spreadsheet|(make|create|generate|build|design) (me )?(an? )?(excel|spreadsheet|xlsx)( sheet| file)?)\b(.*)$", lambda uk, m, t: xlsx_cmd(uk, m.group(7)), 12)
route_add("chart", r"^(/chart|(make|draw|create|plot|give) (me )?(an? )?(bar |line |pie )?(chart|graph))\b", lambda uk, m, t: chart_cmd(uk, t), 12)
route_add("slides", r"^(/slides|/pptx|(make|create|prepare|generate) (me )?(a |some )?(powerpoint|presentation|slides|slide deck|pptx))\b", lambda uk, m, t: slides_cmd(uk, t), 12)
route_add("webpage", r"^(/webpage|/site|(make|create|build|design) (me )?(a |an )?(simple |mini |small )?(web ?page|website|landing page))\b", lambda uk, m, t: webpage_cmd(uk, t), 12)
route_add("youtube", YT_RE.pattern, lambda uk, m, t: youtube_cmd(uk, t, m.group(1)), 14)
route_add("recipe", r"^(?:/recipe|recipe for|how (?:do i|to|can i) (?:cook|make|prepare))\s+(.+)$", lambda uk, m, t: recipe_cmd(uk, m.group(1)), 20)
route_add("quiz", r"^(?:/quiz|/practice|(?:give me |start )?(?:a )?(?:jamb|waec|neco) (?:quiz|practice|questions))\b\s*(.*)$", lambda uk, m, t: quiz_cmd(uk, (t if not t.startswith("/") else m.group(1))), 12)
route_add("cv", r"^(?:/cv|/resume|(?:make|create|write|build) (?:me )?(?:a |my )?(?:cv|resume|curriculum vitae))\b:?\s*(.*)$", lambda uk, m, t: cv_cmd(uk, m.group(1)), 12)
route_add("invoice", r"^(/invoice|/receipt|(make|create|generate|prepare|write) (me )?(an? )?(invoice|receipt))\b", lambda uk, m, t: invoice_cmd(uk, t), 12)
route_add("qr", r"^(?:/qr|(?:make|create|generate) (?:me )?(?:a )?qr(?: code)?(?: for| of|:)?)\s*(.*)$", lambda uk, m, t: qr_cmd(uk, m.group(1)), 12)
route_add("jobs", r"^/jobs\b\s*(.*)$", lambda uk, m, t: jobs_cmd(uk, m.group(1)), 10)
route_add("scam", r"^(?:/scam|is (?:this|it|dis) (?:a )?scam\??|scam check:?|check (?:this )?(?:link|message|site|website) for (?:a )?scam:?)\s*(.*)$", lambda uk, m, t: scam_cmd(uk, m.group(1)), 11)


# =====================================================================================
# PARITY + DELIVERY: notify() reaches Telegram users by message, web users by web push or email
# =====================================================================================
NOTIFY_SKILLS = {"watch", "alert", "briefing", "watchpage", "follow", "jobs", "verse", "study", "remind_rep"}
WEB_EMAIL = {}          # web user id ("cem:...") -> signed-in email, noted by agent.py on each request (RAM only)


def note_email(who, email):
    if who and email and re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", email):
        WEB_EMAIL[who] = email.strip().lower()


def push_save(who, sub):
    """Store a browser push subscription for a web user (from the web app's 'Turn on notifications' chip)."""
    if not who or not isinstance(sub, dict) or not str(sub.get("endpoint", "")).startswith("https://"):
        return False
    q("insert into sk_push (uk, sub, ts) values (?, ?, ?) on conflict (uk) do update set sub=excluded.sub, ts=excluded.ts",
      (f"web:{who}", json.dumps(sub)[:4000], time.time()))
    return True


def web_channel(uk):
    who = uk[4:]
    has_push = bool(q("select 1 from sk_push where uk=?", (uk,), fetch=True))
    email = alert_email(uk)
    return has_push, email


def alert_email(uk):
    who = uk[4:] if uk.startswith("web:") else ""
    e = WEB_EMAIL.get(who) or flag(uk, "email")
    if e and who in WEB_EMAIL:
        flag(uk, "email", e)          # remember the signed-in address for background alerts
    return e


def with_channel(uk, r):
    has_push, email = web_channel(uk)
    if has_push:
        r["text"] += "\n\n\U0001F514 I'll alert you with a notification on this device."
        return r
    if email:
        r["text"] += f"\n\n\u2709\uFE0F I'll email alerts to {email}. Turn on notifications for faster alerts:"
    else:
        r["text"] += ("\n\n\u26A0\uFE0F To reach you later, turn on notifications on this device (Chrome/Android or the installed "
                      "app), sign in with your email, or use the Telegram bot @Harkaiafricabot.")
    kb = (r.get("markup") or {}).get("inline_keyboard", []) + [[{"text": "\U0001F514 Turn on notifications", "callback_data": "push:on"}]]
    r["markup"] = {"inline_keyboard": kb}
    return r


def notify(uk, text, title="Tygar", long=False):
    """Deliver a background alert. Telegram: message. Web: push notification, else email to the signed-in address."""
    c = chat_of(uk)
    if c is not None:
        send(c, text)
        return True
    if not uk.startswith("web:"):
        return False
    row = q("select sub from sk_push where uk=?", (uk,), fetch=True)
    if row:
        try:
            import agent
            agent._push_init()
            agent._push_send(row[0][0], title, text[:240] + ("\u2026 (open Tygar for more)" if len(text) > 240 else ""), tag="sk")
            return True
        except Exception as e:
            print("skills push error:", str(e)[:150])
            if "410" in str(e) or "404" in str(e):
                q("delete from sk_push where uk=?", (uk,))
    email = alert_email(uk)
    if email:
        ok, _ = send_mail(uk, email, f"Tygar alert: {title}", text, kind="alert")
        return ok
    return False


# ---------- B. Email delivery (Brevo) ----------
MAIL_DRYRUN = []                     # filled instead of sending when SKILLS_MAIL_DRYRUN=1 (tests)
MAIL_DAY_LIMIT, MAIL_GAP = 5, 60
ALERT_MAIL_DAY_LIMIT = 20
LAST = {}                            # uk -> last Tygar-generated output {"title", "file"?, "text"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}")
ROLE_LOCAL = {"admin", "administrator", "info", "support", "sales", "contact", "noreply", "no-reply", "donotreply", "postmaster",
              "abuse", "webmaster", "hostmaster", "hr", "jobs", "careers", "marketing", "newsletter", "news", "list", "lists",
              "all", "everyone", "staff", "team", "office", "billing", "security", "mailer-daemon", "root", "enquiries", "enquiry",
              "hello", "help", "press", "media", "legal", "compliance", "students", "members", "subscribers", "group"}
BULK_DOMAINS = re.compile(r"(googlegroups\.com|groups\.io|lists?\.|listserv|mailchimp|yahoogroups|groups\.google)", re.I)
MIME_OK = {"application/pdf", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
           "application/vnd.openxmlformats-officedocument.presentationml.presentation",
           "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "text/calendar", "image/png"}


def remember_output(uk, r, title=None):
    """Keep the last Tygar-generated file/answer per user so 'email it to ...' can send it. Never user uploads."""
    if not r or r.get("skill") in ("email", "memory", "forget", "remember", "secret"):
        return
    t = title or re.sub(r"\s+", " ", (r.get("text") or "").split("\n")[0])[:80] or "Tygar"
    entry = {"title": t, "text": r.get("text") or "", "ts": time.time()}
    if r.get("file") and r["file"][2] in MIME_OK:
        entry["file"] = r["file"]
    elif r.get("photo"):
        entry["file"] = ((re.sub(r"[^A-Za-z0-9]+", "_", t)[:30].strip("_") or "image") + ".png", r["photo"], "image/png")
    LAST[uk] = entry
    if len(LAST) > 3000:
        for k in sorted(LAST, key=lambda k: LAST[k]["ts"])[:500]:
            LAST.pop(k, None)


def remember_file(uk, name, data, mime, title):
    """app.py calls this for files it makes itself (e.g. the PDF writer)."""
    remember_output(uk, {"text": title, "file": (name, data, mime), "skill": "pdf"}, title)


def remember_answer(uk, text):
    if text and len(text) > 20:
        LAST[uk] = {"title": re.sub(r"\s+", " ", text.split("\n")[0])[:60], "text": text, "ts": time.time()}


def valid_recipient(addr):
    """Returns (email, None) or (None, reason)."""
    found = EMAIL_RE.findall(addr or "")
    if len(found) != 1:
        return None, ("Send to one address at a time." if len(found) > 1 else "That doesn't look like an email address.")
    e = found[0].lower().strip(".")
    local, _, dom = e.partition("@")
    if ".." in e or local.startswith(".") or not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,24}", dom):
        return None, "That doesn't look like a valid email address."
    if local in ROLE_LOCAL or re.sub(r"[\d._-]+$", "", local) in ROLE_LOCAL:
        return None, "I don't send to shared or role addresses (info@, support@, admin@, lists...). Use a personal address."
    if BULK_DOMAINS.search(dom) or re.search(r"(^|[._-])(list|group|all|everyone)([._-]|$)", local):
        return None, "I don't send to mailing lists or groups."
    return e, None


def blocked(email):
    return bool(q("select 1 from sk_mail_block where email=?", (email.lower(),), fetch=True))


def block_email(email, reason="stop"):
    q("insert into sk_mail_block (email, reason, ts) values (?, ?, ?) on conflict (email) do nothing", (email.lower(), reason[:40], time.time()))


def _stop_token(email):
    return hashlib.sha256(("tygar-mail-stop:" + (H.get("token") or "") + ":" + email.lower()).encode()).hexdigest()[:20]


def mail_footer(email):
    base = H.get("base_url") or ""
    link = f"{base}/mail/stop?e={requests.utils.quote(email)}&t={_stop_token(email)}"
    return ("Sent by Tygar, a free fan-made assistant, because a user asked. Reply STOP to block.", link)


def send_mail(uk, to, subject, text, attachment=None, kind="file"):
    """One recipient, no CC/BCC. Returns (ok, message)."""
    to = to.lower()
    if blocked(to):
        return False, "That address has asked not to receive email from Tygar."
    now = time.time()
    if kind == "file":
        recent = q("select ts from sk_mail_log where uk=? and kind='file' and ts>=? order by ts desc", (uk, now - 86400), fetch=True) or []
        if recent and not is_dev(uk):
            if len(recent) >= MAIL_DAY_LIMIT:
                return False, f"You've sent {MAIL_DAY_LIMIT} emails today, the daily limit. Try again tomorrow."
            if now - recent[0][0] < MAIL_GAP:
                return False, "Please wait a minute between emails."
        same = q("select count(*) from sk_mail_log where email=? and ts>=?", (to, now - 86400), fetch=True)[0][0]
        if same >= 3 and not is_dev(uk):
            return False, "That address has already received 3 Tygar emails today. Try again tomorrow."
    else:
        n = q("select count(*) from sk_mail_log where uk=? and kind='alert' and ts>=?", (uk, now - 86400), fetch=True)[0][0]
        if n >= ALERT_MAIL_DAY_LIMIT:
            return False, "alert email limit"
    foot, link = mail_footer(to)
    body_html = ("<div style='font-family:Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.55;color:#222'>"
                 + _html.escape(text[:20000]).replace("\n", "<br>")
                 + f"<hr style='border:none;border-top:1px solid #ddd;margin:22px 0'><p style='font-size:12px;color:#777'>{_html.escape(foot)}"
                 f"<br><a href='{_html.escape(link)}'>Block all email from Tygar</a></p></div>")
    payload = {"sender": {"name": "Tygar", "email": os.environ.get("MAIL_FROM", "harkbotofficial@gmail.com")},
               "to": [{"email": to}], "subject": subject[:150], "htmlContent": body_html,
               "textContent": text[:20000] + f"\n\n--\n{foot}\nBlock: {link}",
               "headers": {"List-Unsubscribe": f"<{link}>"}}
    if attachment:
        payload["attachment"] = [{"name": attachment[0], "content": base64.b64encode(attachment[1]).decode()}]
    if os.environ.get("SKILLS_MAIL_DRYRUN") == "1":
        MAIL_DRYRUN.append(payload)
    else:
        key = os.environ.get("BREVO_API_KEY", "")
        if not key:
            return False, "Email isn't set up on this server yet."
        try:
            r = requests.post("https://api.brevo.com/v3/smtp/email", timeout=20,
                              headers={"api-key": key, "Content-Type": "application/json"}, json=payload)
        except Exception as e:
            print("skills mail error:", type(e).__name__)
            return False, "The email service didn't answer. Please try again later."
        if r.status_code >= 300:
            print("skills brevo error:", r.status_code, r.text[:200])
            return False, "The email couldn't be sent right now. Please try again later."
    q("insert into sk_mail_log (uk, email, kind, ts) values (?, ?, ?, ?)", (uk, to, kind, now))
    return True, "sent"


EMAIL_CMD_RE = re.compile(r"^(?:please\s+)?(?:can you\s+)?(?:send|email|e-mail|mail|forward)\s+(?:me\s+)?(?:it|this|that|the\s+\w+(?:\s+\w+)?|my\s+\w+)?\s*"
                          r"(?:(?:by|via|through)\s+(?:e-?mail|mail)\s*)?(?:to\s+)?(me|myself|my\s+e-?mail|.*?@.*?)\s*[.!]?$", re.I)


def email_cmd(uk, m, t):
    who_to = (m.group(1) or "").strip()
    if not re.search(r"@|^(me|myself|my\s+e-?mail)$", who_to, re.I):
        return None
    if chat_of(uk) is None and not uk.startswith("web:cem:"):
        return R("To send email from the web app, sign in with your email first (so I know who's sending). Or use the Telegram bot.")
    if re.match(r"^(me|myself|my\s+e-?mail)$", who_to, re.I):
        own = alert_email(uk)
        if not own:
            return R("Tell me the address, e.g. \"send it to ada@gmail.com\".")
        who_to = own
    to, why = valid_recipient(who_to)
    if not to:
        return R(why)
    last = LAST.get(uk)
    if not last or time.time() - last["ts"] > 6 * 3600:
        return R("There's nothing recent to send. Make a PDF, spreadsheet, slides, invoice, CV, QR code, chart or ask a question first, then say \"email it to ...\".")
    att = last.get("file")
    title = last["title"] if not att else re.sub(r"\.[a-z]+$", "", att[0]).replace("_", " ")
    text = (f"Hello,\n\nA Tygar user asked me to send you this file: {att[0]}.\n\n{last['text'][:1500]}" if att
            else f"Hello,\n\nA Tygar user asked me to send you this answer:\n\n{last['text']}")
    ok, msg = send_mail(uk, to, f"Your Tygar file: {title}"[:150], text, attachment=att)
    if not ok:
        return R(msg)
    what = f'"{att[0]}"' if att else "the answer"
    return R(f"\u2709\uFE0F Sent {what} to {to}. (Limit: {MAIL_DAY_LIMIT} emails a day, 1 per minute.)")


@bp.route("/mail/stop", methods=["GET", "POST"])
def mail_stop():
    from flask import request
    e = (request.values.get("e") or "").strip().lower()
    t = request.values.get("t") or ""
    if not EMAIL_RE.fullmatch(e) or t != _stop_token(e):
        return Response("<p style='font-family:sans-serif'>This link is invalid.</p>", 400, mimetype="text/html")
    if request.method == "POST":
        block_email(e, "link")
        return Response("<p style='font-family:sans-serif'>Done. Tygar will never email this address again.</p>", mimetype="text/html")
    return Response(f"<form method='post' style='font-family:sans-serif;padding:30px'><p>Block all email from Tygar to <b>{_html.escape(e)}</b>?</p>"
                    f"<input type='hidden' name='e' value='{_html.escape(e)}'><input type='hidden' name='t' value='{t}'>"
                    "<button style='padding:10px 20px'>Yes, block</button></form>", mimetype="text/html")


def _inbound_token():
    return hashlib.sha256(("tygar-mail-inbound:" + (H.get("token") or "")).encode()).hexdigest()[:24]


@bp.post("/mail/inbound/<tok>")
def mail_inbound(tok):
    """Optional Brevo inbound-parsing webhook: a reply containing STOP blocks the sender."""
    from flask import request
    if tok != _inbound_token():
        return Response("forbidden", 403)
    d = request.get_json(silent=True) or {}
    items = d.get("items") or [d]
    n = 0
    for it in items:
        frm = ((it.get("From") or {}).get("Address") if isinstance(it.get("From"), dict) else it.get("From")) or it.get("sender") or ""
        body = " ".join(str(it.get(k) or "") for k in ("Subject", "RawTextBody", "ExtractedMarkdownMessage", "TextBody", "text"))
        em = EMAIL_RE.search(str(frm))
        if em and re.search(r"\b(stop|unsubscribe)\b", body, re.I):
            block_email(em.group(0), "reply-stop")
            n += 1
    return {"blocked": n}


def mailblock_cmd(uk, m, t):
    if not is_dev(uk):
        return None
    arg = (m.group(1) or "").strip()
    if arg.lower() == "hook":
        return R(f"Brevo inbound webhook URL (keep private): {H.get('base_url')}/mail/inbound/{_inbound_token()}")
    e = EMAIL_RE.search(arg)
    if not e:
        n = q("select count(*) from sk_mail_block", fetch=True)[0][0]
        return R(f"Blocked addresses: {n}. Add: /mailblock someone@example.com. Inbound webhook: /mailblock hook")
    block_email(e.group(0), "developer")
    return R(f"\U0001F6AB {e.group(0).lower()} will never get email from Tygar.")


def _cb_noop(uk, arg, chat, uid):
    return None


route_add("email", EMAIL_CMD_RE.pattern, email_cmd, 8)
route_add("email_me", r"^(?:please\s+)?(?:e-?mail|mail|send) (me) (?:this|it|that|the \w+)(?: by e-?mail| to my e-?mail)?\s*[.!]?$", email_cmd, 8)
route_add("mailblock", r"^/mailblock\b\s*(.*)$", mailblock_cmd, 8)
route_add("stopmail", r"^stop$", lambda uk, m, t: R("If you got an email from Tygar and want no more, use the block link at the bottom of that email."), 99)


# =====================================================================================
# BATCH 3: study, writing, money, health, faith, prices
# =====================================================================================
# ---------- 25. Homework solver (text, and photos through the existing vision model) ----------
SOLVE_SYS = ("You are a patient maths/physics/chemistry tutor for Nigerian secondary and university students. Solve step by "
             "step: 1) what is given and asked, 2) the formula or method, 3) numbered working with units, 4) a line starting "
             "'Answer:'. Check the result. Plain text (use ^ for powers, sqrt() for roots), no LaTeX. If it isn't solvable, say what's missing.")
SOLVE_CAPTION = re.compile(r"\b(solve|homework|assignment|step by step|workings?|calculate|find the value|explain this question)\b", re.I)


def photo_ask(uk_or_chat, caption):
    """Stronger prompt for homework photos (Telegram photo handler and web vision use it)."""
    if caption and SOLVE_CAPTION.search(caption):
        return SOLVE_SYS + "\n\nThe student's request: " + caption[:600]
    return None


def solve_cmd(uk, problem):
    problem = problem.strip()
    if len(problem) < 3:
        return R("Send the question after /solve, e.g. /solve 2x + 5 = 17, or send a photo of it with the caption \"solve\".")
    if not quota(uk, "solve"):
        return over("solve")
    return R("\U0001F9EE " + llm(SOLVE_SYS, problem))


# ---------- 26. Study planner ----------
def build_timetable(start, exam, subjects):
    """Daily plan from start (date) to the day before exam: rotate subjects, weekly review, mock + rest at the end."""
    days = (exam - start).days
    subjects = [s for s in subjects if s] or ["All subjects"]
    plan, k = [], 0
    for i in range(days):
        d = start + datetime.timedelta(days=i)
        left = days - i
        if left == 1:
            task = "Light review of summaries + rest early. Pack pen, pencil, ID, calculator; confirm venue/time."
        elif left == 2:
            task = "Full mock exam (timed past questions), then review mistakes."
        elif (i + 1) % 7 == 0:
            task = "Weekly review: past questions across all subjects + redo wrong answers."
        else:
            task = subjects[k % len(subjects)]
            k += 1
        plan.append((d, task))
    return plan


def parse_study(text):
    """'/study WAEC on 10 Dec: maths, physics, chemistry at 7pm' -> (exam name, date, [subjects], hhmm) or None."""
    body = re.sub(r"^\s*(/study|/studyplan|(make|create|plan) (me )?(a )?(study|revision|reading) (plan|timetable|schedule)( for)?)\s*", "", text, flags=re.I)
    at = re.search(r"\b(?:remind(?: me)?|at)\s+(\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?)\s*$", body, re.I)
    hhmm = parse_hhmm(at.group(1)) if at else None
    if at:
        body = body[:at.start()]
    subj_part = ""
    m = re.search(r"(?:subjects?\s*(?:are|:)?|:)\s*(.+)$", body, re.I)
    if m:
        subj_part, body = m.group(1), body[:m.start()]
    p = parse_when(body)
    if not p:
        return None
    dt, _, left = p
    name = re.sub(r"\b(exam|exams|is|on|my|starts?|the)\b", " ", left, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip(" ,.-") or "Exam"
    subjects = [s.strip(" .") for s in re.split(r",|\band\b|/", subj_part) if s.strip(" .")][:12]
    return name[:40], dt.date(), subjects, hhmm


def study_cmd(uk, text):
    p = parse_study(text)
    if not p:
        return R("Tell me the exam, date and subjects, e.g. /study WAEC on 10 Dec: maths, english, physics, chemistry at 7pm")
    name, exam, subjects, hhmm = p
    today = wat_now().date()
    days = (exam - today).days
    if days < 1:
        return R("That date is today or already past. Give a future exam date.")
    if days > 365:
        return R("That's more than a year away. Pick a date within the next 12 months.")
    if not quota(uk, "study"):
        return over("study")
    plan = build_timetable(today, exam, subjects)
    hhmm = hhmm or "19:00"
    q("insert into sk_study (uk, exam, exam_date, subjects, hhmm, created) values (?, ?, ?, ?, ?, ?) on conflict (uk) do update set "
      "exam=excluded.exam, exam_date=excluded.exam_date, subjects=excluded.subjects, hhmm=excluded.hhmm, created=excluded.created",
      (uk, name, exam.isoformat(), json.dumps(subjects), hhmm, time.time()))
    lines = [f"\U0001F4DA {name}: {days} day{'s' if days != 1 else ''} to go ({exam.strftime('%a %-d %b')})", ""]
    for d, task in plan[:21]:
        lines.append(f"{d.strftime('%a %-d %b')}: {task}")
    if len(plan) > 21:
        lines.append(f"...and {len(plan) - 21} more days (the full plan is in the spreadsheet).")
    lines += ["", "Tip: 45-minute blocks, 10-minute breaks, past questions every session."]
    xl = make_xlsx({"title": f"{name} plan", "sheet": "Plan", "headers": ["Date", "Day", "Study"],
                    "rows": [[d.isoformat(), d.strftime("%A"), t] for d, t in plan]})
    kb = {"inline_keyboard": [[{"text": f"\u23F0 Remind me daily at {hhmm}", "callback_data": f"sk:stdr:{hhmm}"}]]}
    return R("\n".join(lines), kb, file=(re.sub(r"[^A-Za-z0-9]+", "_", name)[:20] + "_study_plan.xlsx", xl,
                                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))


def today_task(uk):
    r = q("select exam, exam_date, subjects, created from sk_study where uk=?", (uk,), fetch=True)
    if not r:
        return None
    name, ed, subj, created = r[0]
    start = datetime.datetime.utcfromtimestamp(created + WAT).date()
    exam = datetime.date.fromisoformat(ed)
    today = wat_now().date()
    for d, task in build_timetable(start, exam, json.loads(subj)):
        if d == today:
            return f"\U0001F4DA Study time! Today: {task}\n{name} in {(exam - today).days} day(s). Plan: /studyplan"
    return None


def _cb_study_rem(uk, arg, chat, uid):
    hhmm = parse_hhmm(arg) or "19:00"
    r = q("select exam_date from sk_study where uk=?", (uk,), fetch=True)
    if not r:
        return R("Make a plan first: /study WAEC on 10 Dec: maths, english")
    add_repeat(uk, [(next_at(hhmm), "daily", "\U0001F4DA Study time! Send /studyplan for today's task.")], tag="study")
    return R(f"\u23F0 Daily study reminder set for {hhmm} WAT until your exam. Stop: /study off")


def studyplan_cmd(uk):
    t = today_task(uk)
    if t:
        return R(t)
    r = q("select exam, exam_date from sk_study where uk=?", (uk,), fetch=True)
    if not r:
        return R("No study plan yet. Try: /study JAMB on 20 April: english, biology, chemistry, physics")
    return R(f"Your {r[0][0]} is on {r[0][1]}. Make a new plan any time with /study.")


def study_off(uk):
    q("delete from sk_study where uk=?", (uk,))
    remove_repeat(uk, "study")
    return R("Study plan and reminders removed.")


# ---------- 34. Repeating reminders (water, pills) on the existing reminder loop ----------
QUIET = (22, 7)          # no interval reminders between 22:00 and 07:00 WAT


def next_at(hhmm, now_ts=None):
    now_ts = now_ts or time.time()
    d = datetime.datetime.utcfromtimestamp(now_ts + WAT)
    due = d.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
    if due <= d:
        due += datetime.timedelta(days=1)
    return (due - datetime.datetime(1970, 1, 1)).total_seconds() - WAT


def next_due(due, rep, now):
    """Next fire time for 'int:<seconds>' repeats, skipping quiet hours."""
    sec = max(1800, int(rep.split(":")[1]))
    nxt = due
    while nxt <= now:
        nxt += sec
    h = datetime.datetime.utcfromtimestamp(nxt + WAT)
    if h.hour >= QUIET[0] or h.hour < QUIET[1]:
        day = h if h.hour < QUIET[1] else h + datetime.timedelta(days=1)
        nxt = (day.replace(hour=QUIET[1], minute=0, second=0, microsecond=0) - datetime.datetime(1970, 1, 1)).total_seconds() - WAT
    return nxt


def fmt_rep(rep):
    sec = int(rep.split(":")[1])
    return f"every {sec // 3600} h" if sec % 3600 == 0 else f"every {sec // 60} min" + " (7am-10pm)"


def parse_repeat(text):
    """Returns [(first_due_epoch, rep, body)] for interval or multi-time daily reminders, else None."""
    t = text.strip()
    low = t.lower()
    m = re.match(r"^/water(?:\s+(on|off))?(?:\s+every\s+(\d+)\s*(h|hours?|m|mins?|minutes?))?\s*$", low)
    if m:
        if m.group(1) == "off":
            return "water-off"
        n = int(m.group(2) or 2)
        sec = n * 3600 if (m.group(3) or "h").startswith("h") else n * 60
        return [(next_due(time.time(), f"int:{max(sec, 1800)}", time.time()), f"int:{max(sec, 1800)}", "\U0001F4A7 Drink a glass of water")]
    m = re.match(r"^(?:/remind\s+|remind me\s+(?:to\s+)?)(.+?)\s+every\s+(\d+)\s*(h|hrs?|hours?|m|mins?|minutes?)\b(.*)$", t, re.I) or \
        re.match(r"^(?:/remind\s+|remind me\s+)every\s+(\d+)\s*(h|hrs?|hours?|m|mins?|minutes?)\s+(?:to\s+)?(.+)$", t, re.I)
    if m:
        g = m.groups()
        if g[0].isdigit():
            n, unit, body = int(g[0]), g[1], g[2]
        else:
            body, n, unit = g[0] + (g[3] or ""), int(g[1]), g[2]
        sec = n * 3600 if unit.lower().startswith("h") else n * 60
        if sec < 1800:
            return "too-often"
        rep = f"int:{sec}"
        return [(next_due(time.time(), rep, time.time()), rep, re.sub(r"^to\s+", "", body.strip(" ."), flags=re.I)[:200])]
    m = re.match(r"^(?:/pills\s+|remind me\s+(?:to\s+)?)(.*?)\b(?:at\s+)?((?:\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?\s*(?:,|and|&)\s*)+\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?)\s*(every ?day|daily|each day)?\s*(.*)$", t, re.I)
    if m and (m.group(3) or low.startswith("/pills")):
        times = [parse_hhmm(x) for x in re.split(r",|and|&", m.group(2), flags=re.I) if x.strip()]
        times = [x for x in times if x][:6]
        body = (m.group(1) + " " + (m.group(4) or "")).strip(" .") or "take your medicine"
        body = re.sub(r"^to\s+", "", body, flags=re.I)
        if len(times) >= 2:
            return [(next_at(h), "daily", f"\U0001F48A {body[:180]}") for h in times]
    return None


def add_repeat(uk, items, tag=""):
    c = chat_of(uk)
    for due, rep, body in items:
        if c is not None and H.get("tq"):
            H["tq"]("insert into bot_reminders (chat, body, due, rep) values (?, ?, ?, ?)", (c, (body + (f" [{tag}]" if tag else ""))[:300], due, rep))
        else:
            q("insert into sk_reminders (uk, body, due, rep, created) values (?, ?, ?, ?, ?)", (uk, body[:300], due, rep + (f"|{tag}" if tag else ""), time.time()))


def remove_repeat(uk, tag):
    c = chat_of(uk)
    if c is not None and H.get("tq"):
        H["tq"]("delete from bot_reminders where chat=? and body like ?", (c, f"%[{tag}]"))
    q("delete from sk_reminders where uk=? and rep like ?", (uk, f"%|{tag}"))


def count_reminders(uk):
    c = chat_of(uk)
    if c is not None and H.get("tq"):
        return H["tq"]("select count(*) from bot_reminders where chat=?", (c,), fetch=True)[0][0]
    return q("select count(*) from sk_reminders where uk=?", (uk,), fetch=True)[0][0]


def repeat_cmd(uk, t):
    p = parse_repeat(t)
    if p is None:
        return None
    if p == "water-off":
        remove_repeat(uk, "water")
        return R("\U0001F4A7 Water reminders off.")
    if p == "too-often":
        return R("The shortest repeat is every 30 minutes.")
    if not quota(uk, "remind"):
        return over("remind")
    if count_reminders(uk) + len(p) > 20:
        return R("You already have 20 reminders. Delete some with /reminders first.")
    tag = "water" if t.lower().startswith("/water") else ""
    add_repeat(uk, p, tag)
    rep = p[0][1]
    when = fmt_rep(rep) if rep.startswith("int:") else "every day at " + ", ".join(
        datetime.datetime.utcfromtimestamp(d + WAT).strftime("%-I:%M %p") for d, _, _ in p)
    return R(f"\u23F0 Repeating reminder set: {p[0][2]}, {when} (WAT). See/delete: /reminders" + (". Stop: /water off" if tag else ""))


def check_web_reminders():
    now = time.time()
    for rid, uk, body, due, rep in q("select id, uk, body, due, rep from sk_reminders where due<=?", (now,), fetch=True) or []:
        base = (rep or "").split("|")[0]
        tag = (rep or "").split("|")[1] if "|" in (rep or "") else ""
        if base.startswith("int:"):
            nxt = next_due(due, base, now)
        elif base in ("daily", "weekly", "weekdays"):
            nxt = due
            while nxt <= now:
                nxt += 7 * 86400 if base == "weekly" else 86400
                while base == "weekdays" and datetime.datetime.utcfromtimestamp(nxt + WAT).weekday() > 4:
                    nxt += 86400
        else:
            nxt = None
        claimed = q("update sk_reminders set due=? where id=? and due=? returning id", (nxt, rid, due), fetch=True) if nxt \
            else q("delete from sk_reminders where id=? and due=? returning id", (rid, due), fetch=True)
        if claimed and now - due < 6 * 3600:
            msg = (today_task(uk) or body) if tag == "study" else body
            notify(uk, "\u23F0 " + msg, "Tygar reminder")


def cleanup_study():
    today = wat_now().date().isoformat()
    for uk, ed in q("select uk, exam_date from sk_study where exam_date<?", (today,), fetch=True) or []:
        remove_repeat(uk, "study")
        q("delete from sk_study where uk=?", (uk,))


def web_reminders_list(uk):
    rows = q("select id, body, due, rep from sk_reminders where uk=? order by due", (uk,), fetch=True) or []
    if not rows:
        return R("No repeating reminders. Try: remind me to drink water every 2 hours")
    lines = []
    for i, (rid, body, due, rep) in enumerate(rows):
        base = rep.split("|")[0]
        when = fmt_rep(base) if base.startswith("int:") else datetime.datetime.utcfromtimestamp(due + WAT).strftime("daily at %-I:%M %p")
        lines.append(f"{i + 1}. {body} ({when})")
    return R("\u23F0 Your reminders:\n" + "\n".join(lines),
             {"inline_keyboard": [[{"text": f"\U0001F5D1 Delete {i + 1}", "callback_data": f"sk:wrd:{r[0]}"}] for i, r in enumerate(rows[:10])]})


CALLBACKS.update({"stdr": _cb_study_rem,
                  "wrd": lambda uk, arg, chat, uid: (q("delete from sk_reminders where id=? and uk=?", (int(arg), uk)), R("\U0001F5D1 Reminder deleted."))[1]})
JOBS += [("webrem", 30, check_web_reminders), ("studyclean", 3600, cleanup_study)]


# ---------- 27. Flashcards ----------
FLASH = {}


def flash_cmd(uk, text):
    notes = re.sub(r"^\s*(/flashcards?|(make|create|generate) (me )?(some )?flash ?cards( from| for| on)?:?)\s*", "", text, flags=re.I).strip()
    if re.fullmatch(r"(it|this|that|the (document|doc|pdf|file|notes)|my (document|doc|pdf|file|notes))?", notes, re.I):
        d = get_doc(uk)
        if not d:
            return R("Paste your notes after /flashcards, or upload a PDF/DOCX first and then say \"make flashcards from it\".")
        notes = d[1][:9000]
    if len(notes) < 40:
        return R("Give me a bit more to work with: paste a paragraph or two of notes after /flashcards.")
    if not quota(uk, "flash"):
        return over("flash")
    spec = llm_json('Make study flashcards from the notes. Return {"cards": [{"q": str, "a": str}]}: 8-10 cards, one fact each, '
                    "short answers (max 25 words), only facts from the notes.", notes[:9000])
    cards = [c for c in (spec or {}).get("cards", []) if isinstance(c, dict) and c.get("q") and c.get("a")][:10]
    if len(cards) < 2:
        return R("I couldn't make flashcards from that. Try clearer notes.")
    FLASH[uk] = {"cards": cards, "i": 0, "known": 0}
    return flash_card(uk)


def flash_card(uk):
    s = FLASH[uk]
    c = s["cards"][s["i"]]
    return R(f"\U0001F0CF Card {s['i'] + 1}/{len(s['cards'])}\n\n{c['q']}",
             {"inline_keyboard": [[{"text": "\U0001F440 Show answer", "callback_data": f"sk:fc:s:{s['i']}"}]]})


def _cb_flash(uk, arg, chat, uid):
    s = FLASH.get(uk)
    act, i = arg.split(":")
    i = int(i)
    if not s or i != s["i"]:
        return None
    c = s["cards"][i]
    if act == "s":
        return R(f"\U0001F0CF {c['q']}\n\n\u27A1\uFE0F {c['a']}",
                 {"inline_keyboard": [[{"text": "\u2705 Knew it", "callback_data": f"sk:fc:k:{i}"},
                                       {"text": "\u274C Didn't", "callback_data": f"sk:fc:n:{i}"}]]})
    s["known"] += act == "k"
    s["i"] += 1
    if s["i"] < len(s["cards"]):
        return flash_card(uk)
    FLASH.pop(uk, None)
    return R(f"\U0001F3C1 Done! You knew {s['known']}/{len(s['cards'])}. Run /flashcards again on the same notes to repeat.")


CALLBACKS["fc"] = _cb_flash


# ---------- 28. Essay / letter checker ----------
def check_cmd(uk, text):
    body = re.sub(r"^\s*(/check|(please\s+)?(check|correct|proofread|mark|review) (my |this )?(essay|letter|application|cv|grammar|composition|email|write-?up|statement)( for me)?:?)\s*", "", text, flags=re.I).strip()
    if len(body) < 30:
        return R("Paste your essay or letter after /check (or reply to it with /check) and I'll correct it and explain each change.")
    if not quota(uk, "check"):
        return over("check")
    out = llm("You are an encouraging English teacher in Nigeria (WAEC/NECO marking style). Reply in plain text with: "
              "'Corrected version:' (the full text with grammar, spelling, punctuation and tense fixed; keep the writer's voice "
              "and Nigerian context), then 'What I changed and why:' as 4-10 '- ' bullets (original -> fix: reason), then "
              "'Score: x/10' and one tip to improve. Don't add new facts.", body[:6000])
    return R("\u270D\uFE0F " + out)


# ---------- 29. Business names, slogans and a simple logo ----------
BIZ = {}


def bizname_cmd(uk, idea):
    idea = idea.strip()
    if len(idea) < 4:
        return R("Tell me about the business, e.g. /bizname a small chops and event catering business in Enugu")
    if not quota(uk, "bizname"):
        return over("bizname")
    spec = llm_json('Suggest catchy, easy-to-say business names for a Nigerian small business. Return {"ideas": [{"name": str, '
                    '"slogan": str, "why": str}]} with 6 ideas: mix English, Pidgin-friendly and local-language flavour; short '
                    "slogans; avoid famous brand names.", idea)
    ideas = [x for x in (spec or {}).get("ideas", []) if isinstance(x, dict) and x.get("name")][:6]
    if not ideas:
        return R("Sorry, I couldn't come up with names right now. Try again in a minute.")
    BIZ[uk] = {"ideas": ideas, "idea": idea}
    lines = ["\U0001F4A1 Name ideas:"] + [f"{i + 1}. {x['name']} \u2014 \"{x.get('slogan', '')}\"\n   {x.get('why', '')}" for i, x in enumerate(ideas)]
    lines.append("\nCheck the name is free at CAC (search.cac.gov.ng) and as a social handle before you print anything.")
    kb = {"inline_keyboard": [[{"text": f"\U0001F3A8 Logo for {i + 1}", "callback_data": f"sk:logo:{i}"} for i in range(min(3, len(ideas)))]]}
    return R("\n".join(lines), kb)


def _cb_logo(uk, arg, chat, uid):
    b = BIZ.get(uk)
    if not b or not arg.isdigit() or int(arg) >= len(b["ideas"]):
        return R("Ask for name ideas first: /bizname <your business>")
    if not quota(uk, "logo"):
        return over("logo")
    name = b["ideas"][int(arg)]["name"]
    typing(chat, "upload_photo")
    img = H["ai_image"](f"minimal flat vector logo for a business called '{name}' ({b['idea'][:80]}), simple icon, "
                        "bold clean typography of the name, plain white background, 2-3 colours") if H.get("ai_image") else None
    if not img:
        return R("The free image maker is busy right now. Tap the logo button again in a minute.")
    return R(f"\U0001F3A8 Logo idea for {name}. Use it as a starting point; a designer can turn it into a final vector logo.", photo=img)


CALLBACKS["logo"] = _cb_logo


# ---------- 30. Social captions ----------
def caption_cmd(uk, text):
    body = re.sub(r"^\s*(/caption|(write|give|make) (me )?(a |some )?captions?( for)?:?|captions? for)\s*", "", text, flags=re.I).strip()
    if len(body) < 3:
        return R("What's the post about? e.g. /caption my new hair salon opening in Owerri, instagram")
    if not quota(uk, "caption"):
        return over("caption")
    plat = next((p for p in ("instagram", "tiktok", "whatsapp", "status", "facebook", "x", "twitter", "linkedin") if re.search(rf"\b{p}\b", body, re.I)), "instagram")
    out = llm(f"Write 3 short, catchy social media captions for {plat} (WhatsApp status = 1-2 lines, no hashtags). Nigerian "
              "audience, natural tone (add light Pidgin only if the user writes Pidgin), 1-3 fitting emojis each. After each "
              "caption (except WhatsApp status) add 5-10 relevant hashtags mixing popular and local ones (#Lagos, #NaijaBusiness...). "
              "Number them 1-3. Plain text.", body)
    return R("\U0001F4F1 " + out)


# ---------- 31. Loan and savings calculator (local maths) ----------
def loan_calc(principal, annual_rate, months, flat=False):
    if flat:
        interest = principal * annual_rate / 100 * months / 12
        monthly = (principal + interest) / months
    else:
        r = annual_rate / 100 / 12
        monthly = principal / months if r == 0 else principal * r / (1 - (1 + r) ** -months)
        interest = monthly * months - principal
    return round(monthly, 2), round(interest, 2), round(monthly * months, 2)


def savings_fv(monthly, annual_rate, months):
    r = annual_rate / 100 / 12
    return round(monthly * months if r == 0 else monthly * ((1 + r) ** months - 1) / r, 2)


def savings_needed(target, annual_rate, months):
    r = annual_rate / 100 / 12
    return round(target / months if r == 0 else target * r / ((1 + r) ** months - 1), 2)


def _months(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|mths?|weeks?)", text, re.I)
    if not m:
        return None
    n = float(m.group(1))
    u = m.group(2).lower()
    return max(1, round(n * 12 if u.startswith("y") else n / 4.345 if u.startswith("w") else n))


def _rate(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(per month|a month|monthly|p\.?m\.?|per annum|a year|yearly|p\.?a\.?|annual(?:ly)?)?", text, re.I)
    if not m:
        return None, False
    v = float(m.group(1))
    monthly = bool(m.group(2)) and re.match(r"(per month|a month|monthly|p\.?m)", m.group(2), re.I) is not None
    return (v * 12 if monthly else v), monthly


def loan_cmd(uk, text):
    if not quota(uk, "calc"):
        return over("calc")
    body = re.sub(r"^/loan\s*", "", text, flags=re.I)
    amt = parse_amount(re.sub(r"\d+(\.\d+)?\s*%.*?(?=$|for|over)", "", body))
    rate, was_monthly = _rate(body)
    months = _months(body)
    if not (amt and rate is not None and months):
        return R("Send it like: /loan 500k at 24% for 12 months (or \"5% monthly\" for loan-app style rates).")
    m1, i1, t1 = loan_calc(amt, rate, months)
    m2, i2, t2 = loan_calc(amt, rate, months, flat=True)
    rtxt = f"{rate / 12:g}% a month ({rate:g}% a year)" if was_monthly else f"{rate:g}% a year"
    return R(f"\U0001F3E6 Loan of {money(amt)} at {rtxt} for {months} months\n\n"
             f"Reducing balance (banks): {money(m1)}/month, interest {money(i1)}, total {money(t1)}\n"
             f"Flat rate (many lenders/loan apps): {money(m2)}/month, interest {money(i2)}, total {money(t2)}\n\n"
             "Ask the lender which method they use and about fees (management, insurance) that raise the real cost. "
             "Only borrow from CBN/FCCPC-registered lenders.")


def save_cmd(uk, text):
    if not quota(uk, "calc"):
        return over("calc")
    body = re.sub(r"^/save\s*", "", text, flags=re.I)
    rate, _ = _rate(body)
    rate = rate or 0.0
    months = _months(body)
    clean = re.sub(r"\d+(\.\d+)?\s*%[a-z ]*", "", body)
    clean = re.sub(r"\d+(\.\d+)?\s*(years?|yrs?|months?|mths?|weeks?)", "", clean, flags=re.I)
    amt = parse_amount(clean)
    if not (amt and months):
        return R("Try: /save 10000 monthly for 2 years at 10%  or  /save target 1m in 12 months at 8%")
    if re.search(r"\b(target|goal|reach|need)\b", body, re.I):
        need = savings_needed(amt, rate, months)
        return R(f"\U0001F3AF To reach {money(amt)} in {months} months at {rate:g}% a year, save about {money(need)} a month "
                 f"({money(need * 12 / 52)} a week).")
    fv = savings_fv(amt, rate, months)
    return R(f"\U0001F4B0 Saving {money(amt)} a month for {months} months at {rate:g}% a year grows to about {money(fv)} "
             f"(you put in {money(amt * months)}, interest {money(fv - amt * months)}). Interest is before tax and fees.")


# ---------- 32. Bill split + ajo/esusu tracker ----------
def split_calc(total, n, tip_pct=0.0):
    grand = total * (1 + tip_pct / 100)
    each = math.floor(grand / n)
    return round(grand, 2), each, round(grand - each * n, 2)


def split_cmd(uk, text):
    if not quota(uk, "split"):
        return over("split")
    body = re.sub(r"^\s*(/split|split( the)?( bill)?)\s*", "", text, flags=re.I)
    tip = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(tip|service)?", body)
    tip_pct = float(tip.group(1)) if tip else 0.0
    if tip:
        body = body[:tip.start()] + body[tip.end():]
    names = []
    m = re.search(r"\b(?:between|among|amongst|with|for)\s+(.+)$", body, re.I)
    if m and not re.fullmatch(r"\s*\d+\s*(people|persons|of us|ways)?\s*", m.group(1), re.I):
        names = [x.strip(" .") for x in re.split(r",|\band\b|&", m.group(1)) if x.strip(" .")]
        body = body[:m.start()]
    nm = re.search(r"\b(\d+)\s*(?:people|persons|ways|of us|friends)\b|\b(?:among|between|by)\s+(\d+)\b", text, re.I)
    n = len(names) or (int(nm.group(1) or nm.group(2)) if nm else 0)
    total = parse_amount(re.sub(r"\b\d+\s*(people|persons|ways|of us|friends)\b", "", body, flags=re.I))
    if not total or n < 2 or n > 100:
        return R("Try: /split 45000 among 4, or /split 45k between Ada, Tolu and Chidi (add \"10% tip\" if needed).")
    grand, each, rem = split_calc(total, n, tip_pct)
    lines = [f"\U0001F9EE Total {money(grand)}" + (f" (incl. {tip_pct:g}% tip)" if tip_pct else "") + f" split {n} ways: {money(each)} each."]
    if rem:
        lines.append(f"{money(rem)} left over: one person adds it.")
    if names:
        lines += [f"\u2022 {x}: {money(each + (rem if i == 0 else 0))}" for i, x in enumerate(names)]
    return R("\n".join(lines))


def _ajo_rows(uk):
    return q("select id, name, amount, freq, members, rnd, paid from sk_ajo where uk=? order by id", (uk,), fetch=True) or []


def ajo_status(uk, row):
    gid, name, amount, freq, members, rnd, paid = row
    members, paid = json.loads(members), set(json.loads(paid or "[]"))
    collector = members[(rnd - 1) % len(members)]
    lines = [f"\U0001F91D {name}: {money(amount)} {freq}, round {rnd} of {len(members)}. Collector this round: {collector}"]
    for i, mm in enumerate(members):
        mark = "\u2705" if i in paid else "\u2B1C"
        lines.append(f"{mark} {mm}")
    lines.append(f"Collected: {money(amount * len(paid))} of {money(amount * len(members))}")
    kb = [[{"text": f"\u2714 {mm[:14]}", "callback_data": f"sk:ajp:{gid}:{i}"} for i, mm in list(enumerate(members))[j:j + 3] if i not in paid]
          for j in range(0, len(members), 3)]
    kb = [r for r in kb if r] + [[{"text": "\u27A1\uFE0F Next round", "callback_data": f"sk:ajn:{gid}"}]]
    return R("\n".join(lines), {"inline_keyboard": kb})


def ajo_cmd(uk, text):
    if not quota(uk, "ajo"):
        return over("ajo")
    a = re.sub(r"^\s*/(ajo|esusu|adashe)\s*", "", text, flags=re.I).strip()
    rows = _ajo_rows(uk)
    m = re.match(r"^new\s+(.+?)\s+(\d[\d,.]*\s*[km]?)\s+(daily|weekly|monthly)\s*:?\s*(.+)$", a, re.I)
    if m:
        if len(rows) >= 3:
            return R("You can track up to 3 ajo groups. End one with /ajo end <name>.")
        members = [x.strip(" .") for x in re.split(r",|\band\b", m.group(4)) if x.strip(" .")][:30]
        if len(members) < 2:
            return R("List at least 2 members, comma-separated.")
        q("insert into sk_ajo (uk, name, amount, freq, members, rnd, paid, created) values (?, ?, ?, ?, ?, 1, '[]', ?)",
          (uk, m.group(1)[:30], parse_amount(m.group(2)), m.group(3).lower(), json.dumps(members), time.time()))
        return ajo_status(uk, _ajo_rows(uk)[-1])
    def pick(nm):
        nm = (nm or "").strip().lower()
        return next((r for r in rows if r[1].lower() == nm), rows[0] if len(rows) == 1 or not nm else None)
    m = re.match(r"^paid\s+(.+?)(?:\s+in\s+(.+))?$", a, re.I)
    if m and rows:
        r = pick(m.group(2))
        if not r:
            return R("Which group? /ajo paid Tolu in Office")
        members = json.loads(r[4])
        idx = next((i for i, x in enumerate(members) if x.lower() == m.group(1).strip().lower()), None)
        if idx is None:
            return R(f"{m.group(1)} isn't in {r[1]}. Members: {', '.join(members)}")
        paid = sorted(set(json.loads(r[6] or "[]")) | {idx})
        q("update sk_ajo set paid=? where id=?", (json.dumps(paid), r[0]))
        return ajo_status(uk, next(x for x in _ajo_rows(uk) if x[0] == r[0]))
    m = re.match(r"^next(?:\s+(.+))?$", a, re.I)
    if m and rows:
        r = pick(m.group(1))
        return _ajo_next(uk, r[0]) if r else R("Which group? /ajo next Office")
    m = re.match(r"^(?:end|delete|remove)\s+(.+)$", a, re.I)
    if m and rows:
        r = pick(m.group(1))
        if r:
            q("delete from sk_ajo where id=?", (r[0],))
            return R(f"Ended the {r[1]} ajo tracker.")
    if not rows:
        return R("Start an ajo/esusu tracker:\n/ajo new Office 5000 weekly: Ada, Tolu, Chidi, Musa\nThen tap names as they pay, or /ajo paid Tolu. "
                 "/ajo next starts the next round (collector rotates in list order).")
    r = pick(a) if a else None
    if r:
        return ajo_status(uk, r)
    return R("Your ajo groups: " + ", ".join(x[1] for x in rows) + ". Send /ajo <name> to see one.")


def _ajo_next(uk, gid):
    r = next((x for x in _ajo_rows(uk) if x[0] == int(gid)), None)
    if not r:
        return R("That ajo group no longer exists.")
    n = len(json.loads(r[4]))
    if r[5] >= n:
        return R(f"\U0001F389 {r[1]} has finished all {n} rounds! Start a new cycle with /ajo new ...")
    q("update sk_ajo set rnd=rnd+1, paid='[]' where id=?", (r[0],))
    return ajo_status(uk, next(x for x in _ajo_rows(uk) if x[0] == r[0]))


def _cb_ajo_paid(uk, arg, chat, uid):
    gid, idx = (int(x) for x in arg.split(":"))
    r = next((x for x in _ajo_rows(uk) if x[0] == gid), None)
    if not r:
        return R("That ajo group no longer exists.")
    paid = sorted(set(json.loads(r[6] or "[]")) | {idx})
    q("update sk_ajo set paid=? where id=?", (json.dumps(paid), gid))
    return ajo_status(uk, next(x for x in _ajo_rows(uk) if x[0] == gid))


CALLBACKS.update({"ajp": _cb_ajo_paid, "ajn": lambda uk, arg, chat, uid: _ajo_next(uk, arg)})


# ---------- 33. Health basics with danger-sign line ----------
DANGER_RE = re.compile(
    r"\b(chest pain|heart attack|can'?t breathe|cannot breathe|difficulty breathing|shortness of breath|not breathing|"
    r"unconscious|fainted|passed out|won'?t wake|seizure|convulsion|fitting|stroke|face (is )?drooping|slurred speech|"
    r"one side (of (the|my|his|her) body )?(is )?weak|heavy bleeding|bleeding (won'?t|will not|that won'?t) stop|vomiting blood|"
    r"coughing (up )?blood|black stool|suicid\w*|kill (myself|me)|end my life|self[- ]harm|want to die|overdose|"
    r"swallowed (bleach|kerosene|petrol|sniper|insecticide|poison|tablets|pills)|drank (bleach|kerosene|sniper|poison)|poisoned|"
    r"snake ?bite|bitten by a snake|severe burn|stiff neck|swollen (throat|tongue|face)|anaphyla\w*|"
    r"pregnant.{0,40}(bleeding|severe pain|swelling|bad headache|fits)|(baby|infant|newborn).{0,40}(fever|not feeding|fits|yellow|breathing))\b",
    re.I | re.S)
EMERGENCY_LINE = ("\U0001F6A8 This could be an emergency. Go to the nearest hospital now or call 112 (Nigeria emergency). "
                  "Don't wait for symptoms to get worse.")
SUICIDE_LINE = ("\U0001F6A8 I'm really sorry you're feeling this way. Please talk to someone now: call 112, go to the nearest hospital, "
                "or tell a trusted family member or friend to stay with you. You don't have to face this alone.")


def danger_line(text):
    m = DANGER_RE.search(text or "")
    if not m:
        return None
    return SUICIDE_LINE if re.search(r"suicid|kill (myself|me)|end my life|self[- ]harm|want to die", m.group(0), re.I) else EMERGENCY_LINE


HEALTH_SYS = ("You give general health information for people in Nigeria. Never diagnose, never prescribe or state medicine doses, "
              "never tell someone to stop prescribed medicine. Explain common causes in general terms, safe self-care, and "
              "which danger signs need urgent care. Short plain text. End with: 'See a doctor or pharmacist for advice about you.'")


def health_cmd(uk, text):
    qtxt = re.sub(r"^\s*/health\s*", "", text, flags=re.I).strip()
    if len(qtxt) < 3:
        return R("Ask a health question after /health, e.g. /health how do I prevent malaria in rainy season?")
    if not quota(uk, "health"):
        return over("health")
    out = llm(HEALTH_SYS, qtxt)
    out = re.sub(r"\b\d+(\.\d+)?\s?(mg|ml|mcg|g|tablets?|caplets?)\b(\s*(every|per|a|twice|three times)[^.]*)?", "[ask a pharmacist for the dose]", out, flags=re.I)
    dl = danger_line(qtxt)
    return R((dl + "\n\n" if dl else "") + "\U0001FA7A " + out)


# ---------- 35. Verse of the day (Bible KJV via bible-api.com, Quran via alquran.cloud; free, no key) ----------
BIBLE_REFS = ["Psalm 23:1", "Philippians 4:13", "Jeremiah 29:11", "Proverbs 3:5-6", "Isaiah 41:10", "Joshua 1:9", "Romans 8:28",
              "Matthew 11:28", "John 3:16", "Psalm 46:1", "2 Timothy 1:7", "Psalm 121:1-2", "Isaiah 40:31", "Matthew 6:33",
              "Romans 12:2", "Galatians 6:9", "Hebrews 11:1", "James 1:5", "1 Peter 5:7", "Psalm 37:4", "Proverbs 16:3",
              "Lamentations 3:22-23", "Micah 6:8", "Colossians 3:23", "1 Corinthians 13:4", "Ephesians 2:8", "Psalm 118:24",
              "Proverbs 22:6", "Matthew 5:9", "Romans 15:13", "Psalm 27:1", "Deuteronomy 31:6", "Isaiah 26:3", "John 14:27",
              "Psalm 34:18", "Proverbs 18:10", "Nahum 1:7", "Psalm 91:1-2", "Matthew 7:7", "Philippians 4:6", "Psalm 119:105",
              "Hebrews 13:5", "1 Thessalonians 5:16-18", "2 Corinthians 5:7", "Zephaniah 3:17", "Psalm 55:22", "John 16:33",
              "Romans 5:8", "Proverbs 4:23", "Ecclesiastes 3:1", "Isaiah 43:2", "Psalm 139:14", "Mark 11:24", "1 John 4:18"]


def _day_index(n, salt=""):
    return int(hashlib.sha256((wat_day() + salt).encode()).hexdigest(), 16) % n


def verse_today(book):
    key = f"verse:{book}:{wat_day()}"
    c = state(key)
    if c:
        return json.loads(c)
    if book == "quran":
        n = _day_index(6236, "q") + 1
        r = requests.get(f"https://api.alquran.cloud/v1/ayah/{n}/editions/quran-uthmani,en.sahih", timeout=15, headers=BOT_UA)
        r.raise_for_status()
        d = r.json()["data"]
        ar, en = d[0], d[1]
        v = {"ref": f"Quran {en['surah']['englishName']} {en['surah']['number']}:{en['numberInSurah']}",
             "text": en["text"], "arabic": ar["text"], "source": "alquran.cloud (Sahih International)"}
    else:
        ref = BIBLE_REFS[_day_index(len(BIBLE_REFS), "b")]
        r = requests.get("https://bible-api.com/" + requests.utils.quote(ref), params={"translation": "kjv"}, timeout=15, headers=BOT_UA)
        r.raise_for_status()
        d = r.json()
        v = {"ref": d.get("reference") or ref, "text": re.sub(r"\s+", " ", d.get("text") or "").strip(), "source": "bible-api.com (KJV, public domain)"}
    try:
        v["reflection"] = llm("Write a warm 2-sentence reflection on this verse for someone starting their day in Nigeria. "
                              "Respectful, practical, non-preachy, no new scripture quotes.", f"{v['ref']}: {v['text']}")[:400]
    except Exception:
        v["reflection"] = ""
    state(key, json.dumps(v))
    return v


def verse_text(book):
    v = verse_today(book)
    head = "\U0001F54C Verse of the day" if book == "quran" else "\u271D\uFE0F Verse of the day"
    parts = [head, "", (v.get("arabic") + "\n") if v.get("arabic") else "", f"\"{v['text']}\" ({v['ref']})"]
    if v.get("reflection"):
        parts += ["", v["reflection"]]
    parts += ["", f"Source: {v['source']}. Stop: /verse off"]
    return "\n".join(p for p in parts if p is not None)


def verse_cmd(uk, arg):
    a = (arg or "").strip().lower()
    if re.match(r"^(off|stop)\b", a):
        q("delete from sk_verse where uk=?", (uk,))
        return R("Verse of the day turned off.")
    book = "quran" if re.search(r"quran|qur'?an|koran|islam|muslim", a) else "bible" if re.search(r"bible|christian|kjv", a) else None
    hhmm = parse_hhmm(a) if re.search(r"\d", a) else None
    if not book:
        cur = q("select book from sk_verse where uk=?", (uk,), fetch=True)
        if cur and not a:
            return R(verse_text(cur[0][0]))
        return R("Choose one, and an optional time (WAT):", {"inline_keyboard": [[
            {"text": "\u271D\uFE0F Bible daily", "callback_data": "sk:vs:bible"}, {"text": "\U0001F54C Quran daily", "callback_data": "sk:vs:quran"}]]})
    if not quota(uk, "verse"):
        return over("verse")
    return _verse_on(uk, book, hhmm or "06:30")


def _verse_on(uk, book, hhmm):
    q("insert into sk_verse (uk, book, hhmm, last_day) values (?, ?, ?, ?) on conflict (uk) do update set book=excluded.book, hhmm=excluded.hhmm",
      (uk, book, hhmm, wat_day()))
    try:
        today = verse_text(book)
    except Exception as e:
        print("verse error:", e)
        today = "(Couldn't load today's verse right now; it will come tomorrow.)"
    return R(f"\u2705 Daily {'Quran' if book == 'quran' else 'Bible'} verse on, every day at {hhmm} WAT. Change: /verse {book} 7am\n\n{today}")


def check_verses():
    now = wat_now()
    today, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    for uk, book, hhmm, last in q("select uk, book, hhmm, last_day from sk_verse where last_day<>?", (today,), fetch=True) or []:
        if hm < hhmm:
            continue
        if not q("update sk_verse set last_day=? where uk=? and last_day=? returning uk", (today, uk, last or ""), fetch=True):
            continue
        late = (now - now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]))).total_seconds()
        if late > 3 * 3600:
            continue
        try:
            notify(uk, verse_text(book), "Verse of the day", long=True)
        except Exception as e:
            print("verse send error:", e)


CALLBACKS["vs"] = lambda uk, arg, chat, uid: _verse_on(uk, "quran" if arg == "quran" else "bible", "06:30")
JOBS.append(("verse", 60, check_verses))


# ---------- 36. Fuel and transport prices (web search, clearly dated) ----------
def fuel_cmd(uk, text):
    if not quota(uk, "fuel"):
        return over("fuel")
    low = text.lower()
    city = re.search(r"\b(?:in|at)\s+([a-z][a-z .'-]{2,30})\s*\??$", low)
    city = city.group(1).strip().title() if city else ""
    transport = re.search(r"\b(fare|transport|bus|keke|okada|bolt|uber|flight|train)\b", low)
    month = wat_now().strftime("%B %Y")
    route_m = re.search(r"from\s+([a-z .'-]+?)\s+to\s+([a-z .'-]+?)(\s|$|\?)", low)
    qtxt = (f"transport fare {route_m.group(1)} to {route_m.group(2)} {month}" if route_m else
            f"transport fare prices Nigeria {city} {month}" if transport else
            f"petrol pump price per litre {city or 'Nigeria'} today {month} NNPC diesel")
    found = wsearch(qtxt)
    spec = llm_json('From the search results, list current prices. Return {"items": [{"what": str, "price": str, "where": str, '
                    '"source": str, "date": str}], "note": str}. Include ONLY prices that appear in the results, each with the '
                    "outlet name and the date written there; skip anything undated. If nothing is dated, return {\"items\": []}.",
                    f"User asked: {text}\n\nSearch results:\n{found[:5000]}")
    items = [x for x in (spec or {}).get("items", []) if isinstance(x, dict) and x.get("price") and x.get("date")][:8]
    if not items:
        return R(f"I couldn't find clearly dated prices for that right now ({wat_now().strftime('%-d %b %Y')}). Prices change fast; "
                 "check a filling station or a recent Punch/Channels/BusinessDay report.")
    lines = [f"\u26FD Prices found ({wat_now().strftime('%-d %b %Y')} search):" if not transport else "\U0001F68C Fares found:"]
    for x in items:
        lines.append(f"\u2022 {x.get('what', '')}: {x['price']}" + (f" in {x['where']}" if x.get("where") else "") + f" ({x.get('source', '?')}, {x['date']})")
    if (spec or {}).get("note"):
        lines += ["", str(spec["note"])[:300]]
    lines += ["", "Reported prices vary by station and change often; dates show when each was reported."]
    return R("\n".join(lines))


# ---------- batch 3 routes ----------
route_add("solve", r"^(?:/solve|(?:please\s+)?(?:solve|help me solve|how do i solve|work out)\b:?)\s*(.*)$", lambda uk, m, t: solve_cmd(uk, m.group(1)), 25)
route_add("study_off", r"^/study\s+(off|stop|cancel)\s*$", lambda uk, m, t: study_off(uk), 9)
route_add("studyplan", r"^/studyplan\s*$|^what (should|do) i (study|read) today\??$", lambda uk, m, t: studyplan_cmd(uk), 9)
route_add("study", r"^(/study|(make|create|plan) (me )?(a )?(study|revision|reading) (plan|timetable|schedule))\b", lambda uk, m, t: study_cmd(uk, t), 10)
route_add("remind_rep", r"^(/water|/pills|/remind|remind me)\b", lambda uk, m, t: repeat_cmd(uk, t), 5)
route_add("web_reminders", r"^(/reminders|my reminders)\s*$", lambda uk, m, t: web_reminders_list(uk) if chat_of(uk) is None else None, 5)
route_add("flash", r"^(/flashcards?|(make|create|generate) (me )?(some )?flash ?cards)\b", lambda uk, m, t: flash_cmd(uk, t), 12)
route_add("check", r"^(/check|(please\s+)?(check|correct|proofread|mark|review) (my |this )?(essay|letter|application|grammar|composition|email|write-?up|statement))\b", lambda uk, m, t: check_cmd(uk, t), 12)
route_add("bizname", r"^(?:/bizname|(?:suggest |give me |i need )?(?:a |some )?(?:business|brand|company|shop) names?(?: ideas?)? for)\s*(.*)$", lambda uk, m, t: bizname_cmd(uk, m.group(1)), 12)
route_add("caption", r"^(/caption|(write|give|make) (me )?(a |some )?captions?|captions? for)\b", lambda uk, m, t: caption_cmd(uk, t), 12)
route_add("loan", r"^/loan\b|^(how much (will|would) i pay|calculate) .{0,20}\bloan\b", lambda uk, m, t: loan_cmd(uk, t), 12)
route_add("save", r"^/save\b", lambda uk, m, t: save_cmd(uk, t), 12)
route_add("split", r"^(/split|split (the )?(bill|cost|money)|split \u20a6?\d)", lambda uk, m, t: split_cmd(uk, t), 12)
route_add("ajo", r"^/(ajo|esusu|adashe)\b", lambda uk, m, t: ajo_cmd(uk, t), 10)
route_add("health", r"^/health\b", lambda uk, m, t: health_cmd(uk, t), 10)
route_add("verse", r"^/verse\b\s*(.*)$|^(bible|quran) verse (of the day|for today)\s*$", lambda uk, m, t: verse_cmd(uk, m.group(1) if m.group(1) is not None else t), 10)
route_add("fuel", r"^/fuel\b|^(what(?:'s| is) the )?(price|cost) of (petrol|fuel|diesel|kerosene|gas|cooking gas)\b|^(petrol|fuel|diesel) price\b|^(transport )?fare from .+ to .+", lambda uk, m, t: fuel_cmd(uk, t), 20)


def strip_tag(body):
    return re.sub(r" \[(study|water)\]$", "", body or "")


def web_remind(uk, m, t):
    """Web users: one-off/daily reminders through the same parser as Telegram, delivered by push or email."""
    if chat_of(uk) is not None or not H.get("parse_reminder"):
        return None
    p = H["parse_reminder"](t)
    if not p:
        return R("Tell me when, e.g. \"remind me tomorrow at 6pm to submit my assignment\" or \"remind me every day at 7am to read\" (WAT).")
    if count_reminders(uk) >= 20:
        return R("You already have 20 reminders. Delete some with /reminders first.")
    q("insert into sk_reminders (uk, body, due, rep, created) values (?, ?, ?, ?, ?)", (uk, p[2], p[0], p[1], time.time()))
    when = H["fmt_due"](p[0], p[1]) if H.get("fmt_due") else ""
    return R(f"\u23F0 Got it. I'll remind you to {p[2]} {when}.\nSee or delete them with /reminders.")


route_add("web_remind", r"^(/remind|remind me|set (a )?reminder)\b", web_remind, 6)
NOTIFY_SKILLS.add("web_remind")


# =====================================================================================
# BATCH 4: tour, tips, lite mode, self-check, feedback, invites
# =====================================================================================
TOUR = [
    ("\U0001F42F Welcome to Tygar! 30-second tour (1/5)\n\nAsk me anything by text, voice note or photo, in English, Pidgin, Yoruba, Igbo or Hausa.",
     [("\U0001F326 Weather in Lagos", "sk:try:weather in Lagos")]),
    ("\U0001F514 Alerts (2/5)\n\nI watch things for you: naira/crypto prices, wallets (read-only), live match goals, page changes and a morning briefing.",
     [("\U0001F4B1 Today's naira rate", "sk:try:/rate")]),
    ("\U0001F4C4 Files (3/5)\n\nSend a PDF/Word file to ask questions or translate it. I also make PDFs, spreadsheets, slides, CVs, invoices and QR codes.",
     [("\U0001F533 Make a QR code", "sk:try:/qr https://t.me/" + BOT_USERNAME)]),
    ("\U0001F4DA Study & money (4/5)\n\nWAEC/JAMB quizzes, study timetables, homework steps, budget tracking, loan maths, bill split and ajo tracking.",
     [("\U0001F4DD Quick JAMB quiz", "sk:try:/quiz jamb english")]),
    ("\u2705 That's it (5/5)\n\nSend /skills for everything, /menu for buttons, /lite to save data, and /me for your invite link.", []),
]


def tour_step(uk, n):
    n = max(0, min(n, len(TOUR) - 1))
    text, tries = TOUR[n]
    row = [{"text": t, "callback_data": d[:64]} for t, d in tries]
    nav = ([{"text": "Next \u27A1\uFE0F", "callback_data": f"sk:tour:{n + 1}"}, {"text": "Skip", "callback_data": "sk:tour:skip"}]
           if n < len(TOUR) - 1 else [{"text": "\U0001F9F0 All skills", "callback_data": "sk:menu:"}])
    return R(text, {"inline_keyboard": ([row] if row else []) + [nav]})


def after_start(chat):
    """First /start: the welcome tour (once per user)."""
    uk = f"tg:{chat}"
    try:
        if flag(uk, "tour"):
            return
        flag(uk, "tour", "1")
        deliver(chat, tour_step(uk, 0))
    except Exception as e:
        print("tour error:", e)


def _cb_tour(uk, arg, chat, uid):
    if arg == "skip":
        return R("No problem. Send /skills any time to see everything I can do.")
    return tour_step(uk, int(arg) if arg.isdigit() else 0)


def _cb_try(uk, arg, chat, uid):
    r = route(uk, arg)
    return r or R("Try typing: " + arg)


# ---------- smart suggestions (tips) + thumbs feedback ----------
TIPS = [  # skill, loose trigger, button text, example
    ("weather", r"\b(weather|rain(ing)?|forecast|temperature|hot today|sunny)\b", "\U0001F4A1 Try: weather in Lagos", "weather in Lagos"),
    ("alert", r"\b(dollar|naira|exchange rate|btc|bitcoin|usdt|crypto price)\b", "\U0001F4A1 Set a price alert", "/alerts"),
    ("budget", r"\b(spent|spending|budget|expenses|save money)\b", "\U0001F4A1 Track spending", "/budget"),
    ("quiz", r"\b(jamb|waec|neco|utme|exam questions|past questions)\b", "\U0001F4A1 Try a quick quiz", "/quiz jamb english"),
    ("study", r"\b(exam (is|starts)|revision|timetable|reading plan)\b", "\U0001F4A1 Make a study plan", "/studyplan"),
    ("follow", r"\b(match|score|goal|premier league|super eagles|arsenal|chelsea|man(chester)? (united|city)|barcelona|real madrid)\b", "\U0001F4A1 Get goal alerts", "/following"),
    ("scam", r"\b(scam|fraud|legit|is this real|fake (alert|account)|ponzi)\b", "\U0001F4A1 Check a message for scams", "/scam"),
    ("loan", r"\b(loan|interest rate|borrow)\b", "\U0001F4A1 Loan calculator", "/loan"),
    ("ics", r"\b(appointment|meeting|wedding|exam) (on|is on|tomorrow|next)\b", "\U0001F4A1 Add it to your calendar", "/ics"),
    ("solve", r"\b(solve|equation|simplify|differentiate|integrate|find x)\b", "\U0001F4A1 Step-by-step solver", "/solve"),
    ("recipe", r"\b(cook|recipe|jollof|egusi|ogbono|soup)\b", "\U0001F4A1 Recipe + market list", "/recipe jollof rice"),
    ("fuel", r"\b(petrol|fuel|diesel) (price|cost)\b", "\U0001F4A1 Dated fuel prices", "/fuel"),
]


def tip_for(uk, text):
    if is_lite(uk):
        return None
    for skill, rx, label, example in TIPS:
        if re.search(rx, text or "", re.I):
            if flag(uk, f"tip:{skill}"):
                continue
            flag(uk, f"tip:{skill}", "1")
            return {"text": label, "callback_data": f"sk:try:{example}"[:64]}
    return None


def feedback_row(rid=None):
    suf = f":{rid}" if rid else ""
    return [{"text": "\U0001F44D", "callback_data": f"sk:fb:1{suf}"}, {"text": "\U0001F44E", "callback_data": f"sk:fb:0{suf}"}]


def reply_markup(chat, text):
    """Buttons under a normal AI reply in Telegram: one-time tip + thumbs (none in lite mode)."""
    uk = f"tg:{chat}"
    try:
        if is_lite(uk) or (text or "").startswith("/"):
            return None
        rows = []
        t = tip_for(uk, text)
        if t:
            rows.append([t])
        rows.append(feedback_row())
        return {"inline_keyboard": rows}
    except Exception as e:
        print("reply_markup error:", e)
        return None


def web_after(uid, text, reply):
    """Web: same tip + feedback chips under normal answers."""
    uk = f"web:{uid}"
    out = {"reply": reply}
    try:
        if not is_lite(uk):
            rows = []
            t = tip_for(uk, text)
            if t:
                rows.append([t])
            rows.append(feedback_row(int(time.time() * 1000) % 2_000_000_000))
            out["chips"] = chips_of({"inline_keyboard": rows})
    except Exception as e:
        print("web_after error:", e)
    return out


def _cb_feedback(uk, arg, chat, uid, cb):
    parts = arg.split(":")
    vote = 1 if parts[0] == "1" else 0
    mid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else int(((cb or {}).get("message") or {}).get("message_id") or 0)
    q("insert into sk_feedback (uk, msg_id, vote, ts) values (?, ?, ?, ?) on conflict (uk, msg_id) do update set vote=excluded.vote, ts=excluded.ts",
      (uk, mid, vote, time.time()))
    if chat and cb:
        try:
            requests.post(tg_api("editMessageReplyMarkup"), timeout=10, json={
                "chat_id": chat, "message_id": mid,
                "reply_markup": {"inline_keyboard": [[{"text": "\u2705 Thanks for the feedback", "callback_data": "sk:noop:"}]]}})
        except Exception:
            pass
        return None
    return R("\u2705 Thanks for the feedback!")


CALLBACKS.update({"tour": _cb_tour, "try": _cb_try, "fb": _cb_feedback, "noop": _cb_noop})
CB_WANTS_CB.add("fb")


# ---------- low-data mode ----------
def is_lite(uk):
    try:
        return flag(uk, "lite") == "1"
    except Exception:
        return False


def lite_cmd(uk, arg):
    a = (arg or "").strip().lower()
    if a in ("on", "off"):
        if a == "on":
            flag(uk, "lite", "1")
        else:
            unflag(uk, "lite")
        return R("\U0001FAB6 Low-data mode on: shorter replies, no extra buttons, no images or voice unless you ask." if a == "on"
                 else "Low-data mode off.")
    return R(f"Low-data mode is {'on' if is_lite(uk) else 'off'}. Use /lite on or /lite off.")


def _lite_hook(user, text):
    return " LOW-DATA MODE: answer in at most 3 short sentences; no lists unless asked." if is_lite(user) else ""


PROMPT_HOOKS.append(_lite_hook)


# ---------- invites (/start ref_<id>) and /me ----------
def note_referral(upd):
    try:
        msg = upd.get("message") or {}
        m = re.match(r"^/start\s+ref_(\d{3,15})\s*$", msg.get("text") or "")
        uid = (msg.get("from") or {}).get("id")
        if not m or not uid or (msg.get("chat") or {}).get("type") != "private" or int(m.group(1)) == int(uid):
            return
        try:
            row = H["tq"]("select first_seen from bot_users where user_id=?", (uid,), fetch=True) if H.get("tq") else None
        except Exception:
            row = None
        if row and row[0][0] and time.time() - float(row[0][0]) > 3600:
            return                          # existing user: not a new invite
        q("insert into sk_referrals (referee, referrer, ts) values (?, ?, ?) on conflict (referee) do nothing",
          (f"tg:{uid}", f"tg:{m.group(1)}", time.time()))
    except Exception as e:
        print("referral error:", e)


def me_cmd(uk):
    n_ref = q("select count(*) from sk_referrals where referrer=?", (uk,), fetch=True)[0][0]
    cnt = lambda t: q(f"select count(*) from {t} where uk=?", (uk,), fetch=True)[0][0]
    lines = ["\U0001F464 You and Tygar"]
    if chat_of(uk) is not None:
        lines += [f"Invite friends: https://t.me/{BOT_USERNAME}?start=ref_{chat_of(uk)}", f"Friends who joined with your link: {n_ref}"]
    else:
        lines.append("Invite links work from the Telegram bot (@" + BOT_USERNAME + ").")
    lines += [f"Saved facts: {len(facts(uk))} (/memory)", f"Wallets watched: {cnt('sk_wallets')}  Price alerts: {cnt('sk_alerts')}",
              f"Teams followed: {cnt('sk_follows')}  Pages watched: {cnt('sk_pages')}",
              f"Low-data mode: {'on' if is_lite(uk) else 'off'} (/lite)", "Privacy: /privacy  Delete everything: /deletemydata"]
    return R("\n".join(lines))


# ---------- daily self-check (developer is messaged only on failure) ----------
def run_selfcheck():
    res = []
    def chk(name, fn):
        try:
            ok = bool(fn())
        except Exception as e:
            ok = False
            name += f" ({type(e).__name__})"
        res.append((name, ok))
    chk("web_search", lambda: (lambda r: r and r.strip() != "No results found.")(H["web_search"]("Nigeria news today")))
    chk("wallet API (blockstream)", lambda: btc_txs("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh") is not None)
    chk("weather API (open-meteo)", lambda: (_wx_cache.pop("lagos", None), weather_text("Lagos"))[1])
    chk("reminder loop heartbeat", lambda: time.time() - HEART[0] < 180)
    def overdue():
        if not H.get("tq"):
            return True
        n = H["tq"]("select count(*) from bot_reminders where due<?", (time.time() - 900,), fetch=True)[0][0]
        return n == 0
    chk("no stuck reminders", overdue)
    return res


def check_selfcheck():
    now = wat_now()
    if now.hour < 6:
        return
    today = now.strftime("%Y-%m-%d")
    last = state("selfcheck_day") or ""
    if last == today:
        return
    if not q("insert into sk_jobs_state (k, v) values (?, ?) on conflict (k) do update set v=excluded.v where sk_jobs_state.v<>? returning k",
             ("selfcheck_day", today, today), fetch=True):
        return
    res = run_selfcheck()
    bad = [n for n, ok in res if not ok]
    if bad:
        for dev in H.get("dev_ids") or []:
            send(dev, "\u26A0\uFE0F Tygar daily self-check failed: " + ", ".join(bad) + "\nPassed: " + ", ".join(n for n, ok in res if ok))


def selfcheck_cmd(uk):
    if not is_dev(uk):
        return None
    res = run_selfcheck()
    return R("\U0001F9EA Self-check\n" + "\n".join(("\u2705 " if ok else "\u274C ") + n for n, ok in res))


JOBS.append(("selfcheck", 600, check_selfcheck))


def stats_lines():
    since = time.time() - 7 * 86400
    up = q("select count(*) from sk_feedback where vote=1 and ts>=?", (since,), fetch=True)[0][0]
    down = q("select count(*) from sk_feedback where vote=0 and ts>=?", (since,), fetch=True)[0][0]
    refs = q("select count(*) from sk_referrals", fetch=True)[0][0]
    refs7 = q("select count(*) from sk_referrals where ts>=?", (since,), fetch=True)[0][0]
    tot = up + down
    lines = ["", f"Feedback (7 days): \U0001F44D {up} / \U0001F44E {down}" + (f" ({up / tot * 100:.0f}% positive)" if tot else "")]
    lines.append(f"Invites: {refs} total, {refs7} this week")
    for t, label in (("sk_wallets", "wallets"), ("sk_alerts", "price alerts"), ("sk_follows", "team follows"), ("sk_briefing", "briefings"),
                     ("sk_verse", "verse subs"), ("sk_jobs", "job alerts"), ("sk_push", "web push subs")):
        try:
            lines.append(f"Skills: {label} {q(f'select count(*) from {t}', fetch=True)[0][0]}")
        except Exception:
            pass
    return lines


# ---------- /skills list and the "More skills" menu ----------
SKILL_CATS = {
    "alerts": ("\U0001F514 Alerts", ["/watch <wallet address> \u2014 TRON USDT, BTC, ETH (read-only)", "alert me when dollar hits 1600 naira",
                                     "/briefing on 7:00 Lagos", "/watchpage <link>", "/follow Arsenal", "/jobs on accounting in Lagos",
                                     "/verse bible 6:30", "/alerts  /wallets  /pages  /following"]),
    "files": ("\U0001F4C4 Files", ["Send a PDF/DOCX to ask about it or \"translate it to French\"", "/xlsx monthly budget", "/chart rent 50000, food 30000",
                                   "/slides causes of inflation", "/webpage for my bakery ...", "/cv <your details>", "/invoice 2 cakes at 15000 ...",
                                   "/qr <link>", "/ics exam Friday 9am", "send it to name@gmail.com (emails the last file)"]),
    "study": ("\U0001F4DA Study", ["/quiz jamb biology", "/solve 2x + 5 = 17 (or a photo captioned solve)", "/study WAEC on 10 Dec: maths, english",
                                   "/flashcards <notes>", "/check <essay or letter>", "YouTube link \u2192 summary"]),
    "money": ("\U0001F4B0 Money", ["spent \u20a63000 on transport  \u2192 /budget", "/loan 500k at 24% for 12 months", "/save 10000 monthly for 2 years at 10%",
                                   "/split 45000 among 4", "/ajo new Office 5000 weekly: Ada, Tolu, Chidi", "/rate", "/fuel Lagos", "/scam <message>"]),
    "daily": ("\U0001F31F Everyday", ["weather in Enugu", "remember I support Arsenal  \u2192 /memory, /forget", "5 km to miles, time in London",
                                      "remind me to drink water every 2 hours, /water on", "/pills 8am 8pm", "/recipe jollof rice", "/health <question>",
                                      "/bizname <business idea>", "/caption <post>", "/lite on", "/me", "/tour"]),
}


def skills_menu(uk, arg=""):
    if arg in SKILL_CATS:
        title, items = SKILL_CATS[arg]
        return R(title + "\n\n" + "\n".join(f"\u2022 {x}" for x in items),
                 {"inline_keyboard": [[{"text": "\u2B05\uFE0F All skills", "callback_data": "sk:menu:"}]]})
    kb = [[{"text": v[0], "callback_data": f"sk:menu:{k}"} for k, v in list(SKILL_CATS.items())[i:i + 2]] for i in range(0, len(SKILL_CATS), 2)]
    return R("\U0001F9F0 Tygar skills: pick a group (or just type what you need).", {"inline_keyboard": kb})


def skills_text():
    out = ["\U0001F9F0 Everything Tygar can do (type it or tap /menu):"]
    for title, items in SKILL_CATS.values():
        out += ["", title] + [f"\u2022 {x}" for x in items]
    return "\n".join(out)


CALLBACKS["menu"] = lambda uk, arg, chat, uid: skills_menu(uk, arg)

# ---------- batch 4 routes ----------
route_add("tour", r"^/tour\s*$", lambda uk, m, t: tour_step(uk, 0), 5)
route_add("skills", r"^(/skills|all skills|what (else )?can you do\??)\s*$", lambda uk, m, t: R(skills_text(), skills_menu(uk)["markup"]), 5)
route_add("lite", r"^/lite\b\s*(.*)$", lambda uk, m, t: lite_cmd(uk, m.group(1)), 5)
route_add("me", r"^/me\s*$", lambda uk, m, t: me_cmd(uk), 5)
route_add("selfcheck", r"^/selfcheck\s*$", lambda uk, m, t: selfcheck_cmd(uk), 5)


def pre_reply(chat, text):
    """Kept for app.py compatibility; danger-sign lines are sent from handle_tg."""
    return None
