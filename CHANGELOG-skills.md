# Tygar skills changelog (2026-11-05, not deployed)

All new logic lives in `skills.py` (one router serves Telegram and the web chat). Free, keyless services only.
Every skill runs behind the existing gate (Telegram captcha, developers exempt; web: the web app's 30-messages-a-day cap)
and has per-user daily limits (`LIMITS` in skills.py; developers exempt). New tables are created with
`CREATE TABLE IF NOT EXISTS` on first use (all prefixed `sk_`).

## Hooks in existing files
- **app.py**: imports/registers `skills`; SYSTEM prompt skill list; `ask_ai` adds saved facts / lite mode / open-document
  context and stores the last answer for email; group filter + invite tracking before the gate; PDF/DOCX uploads;
  homework-photo prompt (Telegram + web vision); `telegram_reply` calls `skills.handle_tg` and adds tip + thumbs buttons,
  `/start ref_x` deep links, a tour on first /start and after the captcha; "More skills" menu button; `sk:` callbacks;
  `/deletemydata` also wipes skills data; privacy text updated; reminder loop supports `int:<sec>` repeats (quiet hours
  22:00-07:00 WAT) and calls `skills.tick()`; `/stats` adds feedback, invites and skill counts; `web_chat` routes through
  skills first and adds tip/feedback chips; existing PDF maker results can be emailed.
- **agent.py**: `/agent/chat/doc` (PDF/DOCX upload), `/agent/chat/chip` (tapped button chips), `/agent/push/save`
  (browser push subscription for alerts); job results carry `file` and `chips`; page has a document button, renders
  chips and generic file downloads; signed-in email is passed to skills.
- **requirements.txt**: pypdf, python-docx, openpyxl, matplotlib, python-pptx, qrcode, pillow, youtube-transcript-api, tzdata.

## Batch 1 (alerts and memory)
Wallet alerts (TRON USDT-TRC20 / BTC / ETH, read-only, 3 per user, ~2 min), seed-phrase/private-key guard (never stored or
sent to the AI), price/rate alerts (open.er-api + CoinGecko, Coinbase spot fallback when CoinGecko throttles, 5 per user),
daily briefing (/briefing on HH:MM city), page watch (/watchpage, 30 min, 3 per user), long-term memory (remember / /memory
/ /forget), weather (open-meteo, cached 15 min, retries on 429), .ics calendar files, units + time zones (local), live match
alerts (/follow, ESPN scoreboard JSON with a browser User-Agent), budget tracker (spent ... / /budget).

## Batch 2 (files and tools)
PDF/DOCX Q&A (Telegram + web), whole-document translation to .docx, /xlsx, /chart (PNG), /slides (.pptx), /webpage mini
sites at /p/<id> (template-rendered and HTML-escaped, CSP, max 10 per user), YouTube summaries (graceful message when
captions can't be fetched), /recipe with dated naira estimates, /quiz WAEC/NECO/JAMB with buttons, /cv PDF, /invoice and
/receipt PDF (totals computed locally), /qr, /jobs daily alerts, group mode (reply only when @mentioned or replied to),
/scam checker (local red flags + domain search + LLM verdict).

## Batch 3
/solve (and photos captioned "solve"), /study timetable + .xlsx + daily reminder, /flashcards (notes or uploaded doc),
/check essays, /bizname + logo via existing image generator, /caption, /loan and /save (local maths), /split and /ajo
tracker, /health (no diagnoses or doses; emergency line for danger signs on any message), repeating reminders
(every N hours, multi-time daily, /water, /pills), /verse (bible-api.com KJV or alquran.cloud), /fuel (dated results only).

## Batch 4
Welcome tour (5 steps, skippable, once), one-time smart tips, /lite low-data mode, daily 06:00 WAT self-check (messages the
developer only on failure; /selfcheck on demand), thumbs feedback stored in `sk_feedback` + /stats summary, invite links
`t.me/Harkaiafricabot?start=ref_<id>` counted in /me (no rewards), /skills list and menu.

## Parity and email
Same triggers on web; files become downloads, buttons become chips; web alerts go by browser push, else email to the
signed-in address. "send it to name@gmail.com" / "email me this" sends the last Tygar-generated file or answer via Brevo:
one recipient, no CC/BCC, 5 a day and 1 a minute per user (3 a day per recipient), role/list addresses rejected, footer +
one-click block link, blocklist `sk_mail_block` (block link, optional Brevo inbound STOP webhook, developer /mailblock).
Web users must be signed in to send email. `SKILLS_MAIL_DRYRUN=1` records instead of sending.

## Tests
`tests/test_skills_offline.py` (58, no network), `tests/test_skills_integration.py` (16, drives /telegram and /agent with
fake Telegram/Groq), `tests/test_skills_live.py` (free APIs).
