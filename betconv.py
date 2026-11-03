"""Tygars Bot: bet code converter helpers + /betcheck (reachability test for bookie APIs).
Free for users. 18+ only. No secrets are read or returned here."""
import re
import json
import time
import threading
import requests
from flask import Blueprint, request

bp = Blueprint("betconv", __name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# Bookie domains the probe may touch (nothing else).
ALLOWED = ("sportybet.com", "football.com", "bet9ja.com", "1xbet.ng", "1xbet.com", "1xbet.com.gh", "betking.com",
           "msport.com", "betway.com.ng", "betway.com.gh", "betway.co.za", "betwayafrica.com", "nairabet.com",
           "betwinner.ng", "betwinner.com", "merrybet.com", "bangbet.com", "betano.ng", "22bet.ng", "22bet.com",
           "paripesa.ng", "melbet.ng", "betika.com", "hollywoodbets.net", "supabets.co.za", "sportingbet.co.za",
           "gbets.co.za", "soccabet.com", "stake.com", "betpawa.ng", "betpawa.com.gh")

# Reachability checks run by /betcheck from the server. "*_load" checks read a real booking code (VESTC6, a
# SportyBet-family code) or an invalid one (msport answers "The code is invalid." as JSON = endpoint works).
CHECKS = {
    "sportybet_ng_load": "https://www.sportybet.com/api/ng/orders/share/VESTC6",
    "sportybet_gh_load": "https://www.sportybet.com/api/gh/orders/share/VESTC6",
    "sportybet_za_load": "https://www.sportybet.com/api/za/orders/share/VESTC6",
    "football_load": "https://www.football.com/api/ng/orders/share/VESTC6",
    "msport_ng_load": "https://www.msport.com/api/ng/orders/real-sports/order/share/ABC123",
    "msport_gh_load": "https://www.msport.com/api/gh/orders/real-sports/order/share/ABC123",
    "betpawa_ng_load": "https://www.betpawa.ng/api/sportsbook/v3/booking-number/ABC123",
    "betpawa_gh_load": "https://www.betpawa.com.gh/api/sportsbook/v3/booking-number/ABC123",
    "bet9ja_coupon": "https://coupon.bet9ja.com/desktop/feapi/CouponAjax/GetBookABetCoupon?couponCode=5Y8SWSW&v_cache_version=1.328.0.248",
    "1xbet_ng": "https://1xbet.ng/LineFeed/Get1x2_VZip?sports=1&count=1&lng=en&mode=4",
    "betking": "https://www.betking.com/",
    "betway_ng": "https://www.betway.com.ng/",
    "betway_gh": "https://www.betway.com.gh/",
    "betway_za": "https://www.betway.co.za/",
    "nairabet": "https://nairabet.com/",
    "betwinner": "https://betwinner.ng/",
    "merrybet": "https://www.merrybet.com/",
    "bangbet": "https://www.bangbet.com/",
    "betano_ng": "https://www.betano.ng/",
    "22bet_ng": "https://22bet.ng/",
    "paripesa_ng": "https://paripesa.ng/",
    "melbet_ng": "https://melbet.ng/",
    "betika_gh": "https://www.betika.com/en-gh/",
    "soccabet_gh": "https://www.soccabet.com/",
    "hollywoodbets": "https://www.hollywoodbets.net/",
    "supabets": "https://www.supabets.co.za/",
    "sportingbet_za": "https://www.sportingbet.co.za/",
    "gbets": "https://www.gbets.co.za/",
    "stake_home": "https://stake.com/",
    "stake_api": "https://stake.com/_api/graphql",
}


def hdrs(url, extra=None):
    host = re.match(r"https?://([^/]+)", url).group(1)
    base = ".".join(host.split(".")[-3:]) if re.search(r"\.(com|co)\.(ng|gh|za)$", host) else ".".join(host.split(".")[-2:])
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
         "Referer": f"https://www.{base}/", "Origin": f"https://www.{base}"}
    if "betpawa" in host:
        h["X-Pawa-Brand"], h["X-Pawa-Language"] = ("betpawa-ghana" if host.endswith(".gh") else "betpawa-nigeria"), "en"
    if "bet9ja" in host:
        h["Referer"], h["Origin"] = "https://sports.bet9ja.com/", "https://sports.bet9ja.com"
    if extra:
        h.update(extra)
    return h


def _allowed(url):
    m = re.match(r"https://([^/]+)", url or "")
    return bool(m) and any(m.group(1) == d or m.group(1).endswith("." + d) for d in ALLOWED)


_probe_hits = []


def _check_one(u):
    t = time.time()
    try:
        r = requests.get(u, headers=hdrs(u), timeout=12, allow_redirects=True)
        return {"status": r.status_code, "ms": int((time.time() - t) * 1000),
                "json": r.text.lstrip()[:1] in "{[", "preview": r.text[:160]}
    except Exception as e:
        return {"status": 0, "error": str(e)[:160]}


@bp.get("/betcheck")
def betcheck():
    """Can this server reach each bookie? Returns status codes and a short body preview only."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=10) as ex:
        res = dict(zip(CHECKS, ex.map(_check_one, CHECKS.values())))
    res["_bookies"] = {k: {"label": v[0], "status": v[1], "country": v[2]} for k, v in BOOKIES.items()}
    return res


@bp.route("/betcheck/probe", methods=["GET", "POST"])
def probe():
    """Fetch one bookie URL (GET or POST) and return status + text, optionally only regex matches (g=)."""
    now = time.time()
    _probe_hits[:] = [x for x in _probe_hits if now - x < 60] + [now]
    if len(_probe_hits) > 40:
        return {"error": "slow down"}, 429
    u = request.values.get("u", "")
    if not _allowed(u):
        return {"error": "domain not allowed"}, 400
    method = request.values.get("m", "GET").upper()
    body = request.values.get("d")
    ct = request.values.get("ct", "application/json")
    n = min(int(request.values.get("n", "4000")), 60000)
    try:
        if method == "POST":
            r = requests.post(u, data=(body or "").encode(), headers=hdrs(u, {"Content-Type": ct}), timeout=25)
        else:
            r = requests.get(u, headers=hdrs(u), timeout=25)
    except Exception as e:
        return {"status": 0, "error": str(e)[:300]}
    txt = r.text
    g = request.values.get("g")
    if g:
        hits = [txt[max(0, m.start() - 120): m.end() + 200] for m in re.finditer(g, txt)][:40]
        return {"status": r.status_code, "len": len(txt), "hits": hits}
    return {"status": r.status_code, "len": len(txt), "ctype": r.headers.get("content-type"), "text": txt[:n]}


# ======================= converter =======================
# Bookies in the Betradar family share event ids (sr:match:...) and market ids, so picks move 1:1.
COUNTRIES = (("ng", "\U0001F1F3\U0001F1EC Nigeria"), ("gh", "\U0001F1EC\U0001F1ED Ghana"), ("za", "\U0001F1FF\U0001F1E6 South Africa"),
             ("int", "\U0001F30D International"))
BOOKIES = {  # key: (label, status, country)  status "ok" = loads and books codes from our Render server; anything else = coming soon
    # Nigeria
    "sportybet": ("SportyBet NG", "ok", "ng"),
    "msport": ("MSport NG", "ok", "ng"),
    "football": ("Football.com", "ok", "ng"),
    "betpawa": ("betPawa NG", "ok", "ng"),
    "bet9ja": ("Bet9ja", "blocked", "ng"),
    "betking": ("BetKing", "blocked", "ng"),
    "1xbet": ("1xBet", "soon", "ng"),
    "betway": ("Betway NG", "soon", "ng"),
    "nairabet": ("Nairabet", "soon", "ng"),
    "betwinner": ("BetWinner", "soon", "ng"),
    "merrybet": ("Merrybet", "soon", "ng"),
    "bangbet": ("Bangbet", "soon", "ng"),
    "betano": ("Betano", "soon", "ng"),
    "22bet": ("22Bet", "soon", "ng"),
    "paripesa": ("Paripesa", "soon", "ng"),
    "melbet": ("Melbet", "soon", "ng"),
    # Ghana
    "sportybet_gh": ("SportyBet GH", "ok", "gh"),
    "msport_gh": ("MSport GH", "ok", "gh"),
    "betpawa_gh": ("betPawa GH", "ok", "gh"),
    "betway_gh": ("Betway GH", "soon", "gh"),
    "betika_gh": ("Betika GH", "soon", "gh"),
    "soccabet": ("Soccabet", "soon", "gh"),
    # South Africa
    "sportybet_za": ("SportyBet ZA", "ok", "za"),
    "betway_za": ("Betway ZA", "soon", "za"),
    "hollywoodbets": ("Hollywoodbets", "soon", "za"),
    "supabets": ("Supabets", "soon", "za"),
    "sportingbet": ("Sportingbet ZA", "soon", "za"),
    "gbets": ("Gbets", "soon", "za"),
    # International
    "stake": ("Stake", "soon", "int"),
}
ALIASES = {"sporty": "sportybet", "sb": "sportybet", "sportybetng": "sportybet", "sportyng": "sportybet",
           "footballcom": "football", "football.com": "football", "fc": "football",
           "ms": "msport", "msportng": "msport", "b9ja": "bet9ja", "bet9": "bet9ja", "1x": "1xbet", "onexbet": "1xbet",
           "sportybetgh": "sportybet_gh", "sportygh": "sportybet_gh", "sportybetza": "sportybet_za", "sportyza": "sportybet_za",
           "msportgh": "msport_gh", "betwayng": "betway", "betwaygh": "betway_gh", "betwayza": "betway_za",
           "betika": "betika_gh", "sportingbetza": "sportingbet",
           "pawa": "betpawa", "bp": "betpawa", "betpawang": "betpawa", "pawang": "betpawa", "betpawagh": "betpawa_gh", "pawagh": "betpawa_gh"}
DAILY_LIMIT = 15
RESP = "18+ only. Bet responsibly, never stake money you can't afford to lose."
STATE = {}      # chat -> {"from": key, "to": key}
_uses = {}      # (chat, day) -> count
_deps = {}      # launch / ensure / lock from app.py (headless browser for SportyBet's bot check)
_waf = {}       # domain -> {"cookies": {...}, "ts": float}


def init(launch, ensure, lock):
    _deps.update(launch=launch, ensure=ensure, lock=lock)


def working():
    return [k for k, v in BOOKIES.items() if v[1] == "ok"]


def label(k):
    return BOOKIES.get(k, (k, ""))[0]


def norm_bookie(s):
    s = (s or "").strip().lower().replace(" ", "")
    s = ALIASES.get(s, ALIASES.get(s.replace("_", ""), s))
    return s if s in BOOKIES else None


# ---------- HTTP with SportyBet / Football.com bot check (AWS WAF) ----------
def _domain(url):
    return re.match(r"https?://(?:www\.)?([^/]+)", url).group(1)


def waf_cookies(domain, force=False):
    w = _waf.get(domain)
    if w and not force and time.time() - w["ts"] < 1200:
        return w["cookies"]
    if not _deps:
        return {}
    lock = _deps["lock"]
    if not lock.acquire(timeout=90):
        return (w or {}).get("cookies", {})
    try:
        w = _waf.get(domain)
        if w and not force and time.time() - w["ts"] < 60:
            return w["cookies"]
        if not _deps["ensure"]():
            return {}
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = _deps["launch"](p)
            try:
                ctx = b.new_context(user_agent=UA, locale="en-US")
                ctx.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media", "font", "websocket") else r.continue_())
                pg = ctx.new_page()
                diag = {}
                ck = _solve_waf(pg, ctx, domain, diag)
                _waf[domain] = {"cookies": ck, "ts": time.time(), "diag": diag}
                return ck
            finally:
                b.close()
    except Exception as e:
        print("waf error:", str(e)[:300])
        return {}
    finally:
        lock.release()


class WafBlocked(Exception):
    pass


_tl = threading.local()


def _page_http(pg, method, url, body):
    path = re.sub(r"^https?://[^/]+", "", url)
    js = """([m, p, b]) => new Promise(res => { const x = new XMLHttpRequest(); x.open(m, p);
            if (b !== null) x.setRequestHeader('Content-Type', 'application/json');
            x.onload = () => res([x.status, x.responseText]); x.onerror = () => res([0, '']); x.send(b); })"""
    st, txt = pg.evaluate(js, [method, path, None if body is None else json.dumps(body)])
    if st == 202 and not txt.strip():
        raise WafBlocked(f"{_domain(url)} is blocking our server right now")
    return json.loads(txt)


class page_session:
    """Open a headless browser on a bookie site and send its API calls from inside the page (passes the bot check)."""
    def __init__(self, domain):
        self.domain = domain

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.own = not getattr(_tl, "held", False)
        if self.own:
            if not _deps["lock"].acquire(timeout=120):
                raise WafBlocked("browser busy")
            _tl.held = True
        try:
            _deps["ensure"]()
            self.pw = sync_playwright().start()
            self.b = _deps["launch"](self.pw)
            ctx = self.b.new_context(user_agent=UA, locale="en-US")
            ctx.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media", "font", "websocket") else r.continue_())
            pg = ctx.new_page()
            pg.goto(f"https://www.{self.domain}/api/ng/orders/share/VESTC6", wait_until="domcontentloaded", timeout=45000)
            pages = getattr(_tl, "pages", {})
            pages[self.domain] = pg
            _tl.pages = pages
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *a):
        try:
            getattr(_tl, "pages", {}).pop(self.domain, None)
            if getattr(self, "b", None):
                self.b.close()
            if getattr(self, "pw", None):
                self.pw.stop()
        finally:
            if self.own:
                _tl.held = False
                _deps["lock"].release()


def _solve_waf(pg, ctx, domain, diag):
    """Open the API URL as a page: the bot check then runs its browser challenge and sets aws-waf-token."""
    api = f"https://www.{domain}/api/ng/orders/share/VESTC6"
    ck = {}
    for step, url in enumerate((api, f"https://www.{domain}/ng/", api)):
        try:
            resp = pg.goto(url, wait_until="domcontentloaded", timeout=40000)
            diag[f"goto{step}"] = resp.status if resp else None
        except Exception as e:
            diag[f"goto{step}"] = str(e)[:80]
        for _ in range(15):
            ck = {c["name"]: c["value"] for c in ctx.cookies()}
            if "aws-waf-token" in ck:
                break
            pg.wait_for_timeout(1000)
        if "aws-waf-token" in ck:
            break
    try:
        diag["body"] = (pg.evaluate("document.body ? document.body.innerText : ''") or "")[:100]
        diag["wafjs"] = pg.evaluate("typeof window.AwsWafIntegration")
        diag["xhr"] = pg.evaluate("""() => new Promise(res => { const x = new XMLHttpRequest();
            x.open('GET', '/api/ng/orders/share/VESTC6'); x.onload = () => res(x.status + ' ' + x.responseText.slice(0, 40));
            x.onerror = () => res('err'); x.send(); })""")
    except Exception as e:
        diag["eval"] = str(e)[:120]
    return ck


def http(method, url, body=None):
    """JSON request to a bookie. SportyBet/Football.com sit behind a bot check: retry with browser cookies,
    or send from inside a headless page when a page_session is open."""
    d = _domain(url)
    pg = getattr(_tl, "pages", {}).get(d)
    if pg:
        return _page_http(pg, method, url, body)
    needs_waf = d in ("sportybet.com", "football.com")
    h = hdrs(url, {"Content-Type": "application/json"} if body is not None else None)
    r = requests.request(method, url, json=body, headers=h, timeout=25)
    if r.status_code == 202 and not r.text.strip() and needs_waf:
        # The bot check rejects plain server requests; a real browser page gets through (verified 2026-11-02).
        raise WafBlocked(f"{d} is blocking direct requests from our server")
    try:
        return r.json()
    except Exception:
        raise RuntimeError(f"{d} answered {r.status_code} without data")


# ---------- load / create per bookie ----------
SHARE = {"sportybet": "https://www.sportybet.com/api/ng/orders/share",
         "sportybet_gh": "https://www.sportybet.com/api/gh/orders/share",
         "sportybet_za": "https://www.sportybet.com/api/za/orders/share",
         "football": "https://www.football.com/api/ng/orders/share",
         "msport": "https://www.msport.com/api/ng/orders/real-sports/order/share",
         "msport_gh": "https://www.msport.com/api/gh/orders/real-sports/order/share",
         "betpawa": "https://www.betpawa.ng/api/sportsbook/v3/booking-number",
         "betpawa_gh": "https://www.betpawa.com.gh/api/sportsbook/v3/booking-number"}
SPORTY_FAMILY = ("sportybet", "sportybet_gh", "sportybet_za", "football")  # same API shape; msport* is the other shape


class BetError(Exception):
    pass


def load_code(bookie, code):
    """Returns list of picks: dict(eventId, marketId, specifier, outcomeId, game, market, pick, odds, started)."""
    if bookie in PAWA:
        return pawa_load(bookie, code)
    j = http("GET", f"{SHARE[bookie]}/{code}")
    msg = (j.get("message") or "")
    if bookie in SPORTY_FAMILY:
        if j.get("bizCode") != 10000 or not j.get("data"):
            raise BetError(msg or "That code could not be loaded.")
        data = j["data"]
        picks, sels = [], {(s.get("eventId"), str(s.get("marketId")), s.get("specifier") or None): s
                           for s in ((data.get("ticket") or {}).get("selections") or [])}
        for o in (data.get("outcomes") or []) + (data.get("unavailableOutcomes") or []):
            for m in o.get("markets") or []:
                for oc in m.get("outcomes") or []:
                    picks.append({"eventId": o["eventId"], "marketId": str(m["id"]), "specifier": m.get("specifier") or None,
                                  "outcomeId": str(oc["id"]), "game": f"{o.get('homeTeamName')} vs {o.get('awayTeamName')}",
                                  "market": m.get("desc") or m.get("name") or "", "pick": oc.get("desc") or "",
                                  "odds": _f(oc.get("odds")), "started": o.get("status", 0) != 0 or o.get("matchStatus") not in (None, "Not start"),
                                  "home": o.get("homeTeamName"), "away": o.get("awayTeamName"), "start": _ms(o.get("estimateStartTime")),
                                  "sport": (o.get("sport") or {}).get("id")})
        return picks
    # msport
    if msg.lower() != "success" or not j.get("data"):
        raise BetError(msg or "That code could not be loaded.")
    picks = []
    for s in j["data"].get("bettableBetSlip") or []:
        ev, m, oc = s.get("event") or {}, s.get("market") or {}, s.get("outcome") or {}
        picks.append({"eventId": ev.get("eventId"), "marketId": str(m.get("id")), "specifier": m.get("specifiers") or None,
                      "outcomeId": str(oc.get("id")), "game": f"{ev.get('homeTeam')} vs {ev.get('awayTeam')}",
                      "market": m.get("description") or "", "pick": oc.get("description") or "",
                      "odds": _f(oc.get("odds")), "started": ev.get("status", 0) not in (0, None),
                      "home": ev.get("homeTeam"), "away": ev.get("awayTeam"), "start": _ms(ev.get("startTime")), "sport": ev.get("sportId")})
    return picks


def _f(x):
    try:
        return float(x)
    except Exception:
        return 0.0


def _ms(x):
    try:
        return int(x) / 1000.0
    except Exception:
        return None


def _sel(p):
    if p.get("dstsel"):
        return p["dstsel"]
    return {"eventId": p["eventId"], "marketId": p["marketId"], "specifier": p["specifier"], "outcomeId": p["outcomeId"]}


def create_code(bookie, picks):
    """Book picks on bookie. Returns code or raises BetError(message)."""
    if bookie in PAWA:
        return pawa_create(bookie, [p["priceId"] for p in picks])
    j = http("POST", SHARE[bookie], {"selections": [_sel(p) for p in picks]})
    code = ((j.get("data") or {}).get("shareCode"))
    if (j.get("message") or "").lower() == "success" and code:
        return code
    raise BetError(j.get("message") or "booking failed")


def total_odds(picks):
    t = 1.0
    for p in picks:
        t *= p["odds"] or 1.0
    return round(t, 2)


def convert(code, src, dst):
    """Full conversion. Returns dict with new code, odds and unmatched picks."""
    code = re.sub(r"[^A-Za-z0-9]", "", code or "").upper()
    if not code:
        raise BetError("Please send a valid booking code.")
    picks = load_code(src, code)
    if not picks:
        raise BetError("No games found in that code (it may have expired).")
    unmatched, ok = [], []
    for p in picks:
        if p["started"]:
            unmatched.append((p, "already started or finished"))
        else:
            ok.append(p)
    if src in PAWA or dst in PAWA:
        ok, um = translate(ok, src, dst)
        unmatched += um
    if ok:
        try:
            create_code(dst, ok)
        except BetError:
            keep = []
            for p in ok:          # find which picks the target refuses
                try:
                    create_code(dst, [p])
                    keep.append(p)
                except BetError:
                    unmatched.append((p, f"market not offered on {label(dst)}"))
                except Exception:
                    unmatched.append((p, f"could not check on {label(dst)}"))
            ok = keep
    if not ok:
        return {"code": None, "src_odds": total_odds(picks), "dst_odds": 0, "matched": [], "unmatched": unmatched, "total": len(picks)}
    new = create_code(dst, ok)
    try:
        dpicks = load_code(dst, new)
        dst_odds = total_odds(dpicks)
    except Exception:
        dst_odds = None
    return {"code": new, "src_odds": total_odds(picks), "kept_src_odds": total_odds(ok), "dst_odds": dst_odds,
            "matched": ok, "unmatched": unmatched, "total": len(picks), "note": picks[0].get("_note")}


def convert_in_browser(code, src, dst):
    doms = sorted({_domain(SHARE[b]) for b in (src, dst)} & {"sportybet.com", "football.com"})
    if not doms:
        return convert(code, src, dst)
    with page_session(doms[0]):
        if len(doms) > 1:
            with page_session(doms[1]):
                return convert(code, src, dst)
        return convert(code, src, dst)


def format_result(r, code, src, dst):
    lines = [f"\U0001F3AB {label(src)} {code.upper()} \u2192 {label(dst)}"]
    if r["code"]:
        lines.append(f"\u2705 New {label(dst)} code: {r['code']}")
        lines.append(f"Picks moved: {len(r['matched'])} of {r['total']}")
        lines.append(f"Total odds on {label(src)}: {r['src_odds']}" + (f" ({r['kept_src_odds']} for the moved picks)" if r["unmatched"] else ""))
        lines.append(f"Total odds on {label(dst)}: {r['dst_odds'] if r['dst_odds'] else 'not shown'}")
    else:
        lines.append(f"\u274C None of the {r['total']} picks could be booked on {label(dst)}, so no code was made.")
    if r.get("note"):
        lines.append(f"\u2139 {r['note']}")
    if r["unmatched"]:
        lines.append("\nNot moved:")
        for p, why in r["unmatched"][:25]:
            lines.append(f"\u2022 {p['game']}: {p['market']} {p['pick']} ({why})")
        if len(r["unmatched"]) > 25:
            lines.append(f"\u2022 ...and {len(r['unmatched']) - 25} more")
    lines.append("\nOdds can change before you place the bet. " + RESP)
    return "\n".join(lines)


# ---------- Telegram flow ----------
def _kb(exclude=None, step="f"):
    """Picker grouped by country: a header button per country, then that country's working bookies."""
    rows = []
    for c, cname in COUNTRIES:
        btns = [{"text": label(k), "callback_data": f"bc:{step}:{k}"} for k in working() if k != exclude and BOOKIES[k][2] == c]
        if not btns:
            continue
        rows.append([{"text": cname, "callback_data": f"bc:h:{c}"}])
        rows += [btns[i:i + 2] for i in range(0, len(btns), 2)]
    return {"inline_keyboard": rows}


def soon_text():
    parts = []
    for c, cname in COUNTRIES:
        s = [label(k) for k, v in BOOKIES.items() if v[1] != "ok" and v[2] == c]
        if s:
            parts.append(f"{cname.split(' ', 1)[1]}: " + ", ".join(s))
    return "Coming soon \u2014 " + "; ".join(parts) + "."


def start(chat, send):
    STATE[chat] = {}
    send(chat, "\U0001F3AB Bet code converter (free, " + RESP.split('.')[0] + ")\n\nStep 1: which bookie is your code FROM?\n" + soon_text(), _kb(step="f"))


def on_callback(chat, data, send, pending_mode):
    _, step, k = data.split(":", 2)
    if step == "h":
        return  # country header button, nothing to do
    if k not in working():
        return send(chat, f"{label(k)} isn't available yet. " + soon_text())
    if step == "f":
        STATE[chat] = {"from": k}
        return send(chat, f"From: {label(k)} \u2714\nStep 2: convert TO which bookie?", _kb(exclude=k, step="t"))
    st = STATE.get(chat) or {}
    if not st.get("from"):
        return start(chat, send)
    st["to"] = k
    STATE[chat] = st
    pending_mode[chat] = ("betcode", time.time())
    send(chat, f"{label(st['from'])} \u2192 {label(k)} \u2714\nStep 3: send me the {label(st['from'])} booking code.")


def quota_ok(chat):
    day = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 3600))
    n = _uses.get((chat, day), 0)
    if n >= DAILY_LIMIT:
        return False
    _uses[(chat, day)] = n + 1
    return True


def run(chat, code, src, dst, send):
    if src == dst:
        return send(chat, "Pick two different bookies.")
    for b in (src, dst):
        if b not in working():
            return send(chat, f"{label(b)} isn't available yet. Working now: " + ", ".join(label(x) for x in working()) + ".")
    if not quota_ok(chat):
        return send(chat, f"You've used today's {DAILY_LIMIT} free conversions. Come back tomorrow. " + RESP)
    send(chat, f"\u23F3 Converting {code.upper()} from {label(src)} to {label(dst)}... this can take up to a minute.")
    try:
        try:
            r = convert(code, src, dst)
        except WafBlocked:
            r = convert_in_browser(code, src, dst)
        send(chat, format_result(r, code, src, dst))
    except BetError as e:
        send(chat, f"\u26A0 {label(src)} said: {e}\nCheck the code and the bookie, then try again.")
    except Exception as e:
        print("convert error:", e)
        send(chat, f"\u26A0 Sorry, I couldn't reach the bookie right now ({str(e)[:80]}). Please try again in a few minutes.")


def reply_code(chat, text, send):
    st = STATE.pop(chat, None) or {}
    if not (st.get("from") and st.get("to")):
        return start(chat, send)
    run(chat, text.strip().split()[0], st["from"], st["to"], send)


def convert_cmd(chat, text, send):
    parts = text.split()
    if len(parts) < 4:
        names = ", ".join(k for k in working())
        return send(chat, f"Use: /convert CODE FROM TO\ne.g. /convert VESTC6 sportybet msport_gh\nWorking bookies: {names}\nOr tap the Bet code converter button in /menu.")
    src, dst = norm_bookie(parts[2]), norm_bookie(parts[3])
    if not src or not dst:
        return send(chat, "I don't know that bookie. Working now: " + ", ".join(working()))
    run(chat, parts[1], src, dst, send)


_jobs = {}


def _job(key, code, src, dst):
    t = time.time()
    try:
        try:
            r = convert(code, src, dst)
            how = "direct"
        except WafBlocked:
            r = convert_in_browser(code, src, dst)
            how = "browser"
        _jobs[key] = {"status": "done", "ok": True, "how": how, "secs": round(time.time() - t, 1), "text": format_result(r, code, src, dst)}
    except BaseException as e:
        _jobs[key] = {"status": "done", "ok": False, "secs": round(time.time() - t, 1), "error": f"{type(e).__name__}: {str(e)[:300]}"}


@bp.get("/betcheck/convert")
def betcheck_convert():
    """Test a conversion without Telegram. Non-blocking: call again with the same args to read the result."""
    src, dst, code = norm_bookie(request.args.get("from")), norm_bookie(request.args.get("to")), request.args.get("code", "")
    if not src or not dst or src not in SHARE or dst not in SHARE:
        return {"error": "bad bookie"}, 400
    key = (code.upper(), src, dst)
    cur = _jobs.get(key)
    if cur and not request.args.get("new"):
        return cur
    if cur and cur.get("status") == "running":
        return cur
    now = time.time()
    _probe_hits[:] = [x for x in _probe_hits if now - x < 60] + [now]
    if len(_probe_hits) > 40 or len(_jobs) > 200:
        return {"error": "slow down"}, 429
    _jobs[key] = {"status": "running"}
    threading.Thread(target=_job, args=(key, code, src, dst), daemon=True).start()
    return {"status": "started, check again in a minute"}


_wafdiag = {}


def _waf_diag(d):
    t = time.time()
    _wafdiag[d] = {"status": "running"}
    out = {}
    try:
        ck = waf_cookies(d, force=True)
        out.update(got_token="aws-waf-token" in ck, cookie_names=sorted(ck)[:20], secs=round(time.time() - t, 1),
                   page=_waf.get(d, {}).get("diag"))
        r = requests.get(f"https://www.{d}/api/ng/orders/share/VESTC6", headers=hdrs(f"https://www.{d}/"), cookies=ck, timeout=20)
        out["share_status"], out["share_preview"] = r.status_code, r.text[:120]
    except BaseException as e:
        out["error"] = repr(e)[:300]
    out["status"] = "done"
    _wafdiag[d] = out


@bp.get("/betcheck/waf")
def betcheck_waf():
    """Non-blocking: first call starts the headless-browser bot-check test, later calls return its result."""
    d = request.args.get("d", "sportybet.com")
    if d not in ("sportybet.com", "football.com"):
        return {"error": "bad domain"}, 400
    cur = _wafdiag.get(d)
    if request.args.get("new") or not cur:
        if not cur or cur.get("status") != "running":
            threading.Thread(target=_waf_diag, args=(d,), daemon=True).start()
        return {"status": "started, check again in a minute"}
    return cur


# ======================= betPawa =======================
# betPawa has its own event and price ids. We find the same match by its Betradar id (betPawa lists it as the
# event's SPORTRADAR widget) or else by team names + kickoff time, then map common markets through a shared key.
# Public JSON endpoints the betpawa.ng site itself calls (brand chosen by the X-Pawa-Brand header).
PAWA = {"betpawa": "https://www.betpawa.ng", "betpawa_gh": "https://www.betpawa.com.gh"}
PAWA_ERR = {"BOOKING_CODE_NOT_FOUND": "The booking code can't be found (it doesn't exist or expired).",
            "BOOKING_CODE_INVALID": "The booking code can't be found (it doesn't exist or expired).",
            "SPORTSBOOK_WRONG_SELECTION": "a pick is no longer available",
            "SPORTSBOOK_DUPLICATE_EVENT_ID": "two picks are on the same match"}
# Betradar (market id, outcome id) -> shared key. SportyBet, Football.com and MSport all use Betradar ids.
BR = {("1", "1"): ("1x2", "1"), ("1", "2"): ("1x2", "X"), ("1", "3"): ("1x2", "2"),
      ("10", "9"): ("dc", "1X"), ("10", "10"): ("dc", "12"), ("10", "11"): ("dc", "X2"),
      ("18", "12"): ("ou", "over"), ("18", "13"): ("ou", "under"),
      ("29", "74"): ("btts", "yes"), ("29", "76"): ("btts", "no"),
      ("11", "4"): ("dnb", "1"), ("11", "5"): ("dnb", "2"),
      ("60", "1"): ("1x2_1h", "1"), ("60", "2"): ("1x2_1h", "X"), ("60", "3"): ("1x2_1h", "2"),
      ("68", "12"): ("ou_1h", "over"), ("68", "13"): ("ou_1h", "under")}
BR_REV = {v: k for k, v in BR.items()}
LINED = ("ou", "ou_1h")
# betPawa market type id -> kind, price type id -> outcome (football)
PW_M = {"3743": "1x2", "4693": "dc", "5000": "ou", "3795": "btts", "4703": "dnb", "3668": "1x2_1h", "4958": "ou_1h"}
PW_O = {"3744": "1", "3745": "X", "3746": "2", "4694": "1X", "4695": "X2", "4696": "12", "5001": "over", "5002": "under",
        "3796": "yes", "3797": "no", "4704": "1", "4705": "2", "3669": "1", "3670": "X", "3671": "2", "4959": "over", "4960": "under"}


def _line(x):
    try:
        return f"{float(str(x).replace('total=', '')):g}"
    except Exception:
        return None


def br_key(p):
    k = BR.get((str(p.get("marketId")), str(p.get("outcomeId"))))
    if not k:
        return None
    line = None
    if k[0] in LINED:
        line = _line(re.sub(r".*total=([0-9.]+).*", r"\1", p.get("specifier") or ""))
        if not line:
            return None
    return (k[0], line, k[1])


def pawa_http(bookie, method, path, body=None):
    url = PAWA[bookie] + path
    r = requests.request(method, url, json=body, headers=hdrs(url, {"Content-Type": "application/json"}), timeout=25)
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"{_domain(url)} answered {r.status_code} without data")
    if isinstance(j, dict) and j.get("error"):
        raise BetError(PAWA_ERR.get(j["error"], j["error"]))
    return j


def _ts(s):
    """betPawa startTime -> epoch seconds (ISO 8601; tolerant of other formats)."""
    from datetime import datetime, timezone, timedelta
    if not s:
        return None
    s = str(s)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        pass
    m = re.search(r"(\d{4}-\d\d-\d\d) (\d\d?):(\d\d) ?(AM|PM)?.*?UTC([+-]\d\d):(\d\d)", s)
    if m:
        h = int(m.group(2)) % 12 + (12 if m.group(4) == "PM" else 0) if m.group(4) else int(m.group(2))
        off = int(m.group(5)) * 60 + (int(m.group(6)) if m.group(5)[0] != "-" else -int(m.group(6)))
        d = datetime.strptime(m.group(1), "%Y-%m-%d").replace(hour=h, minute=int(m.group(3)), tzinfo=timezone(timedelta(minutes=off)))
        return d.timestamp()
    return None


def _sr(ev):
    for w in ev.get("widgets") or []:
        if w.get("type") == "SPORTRADAR" and w.get("id"):
            return str(w["id"])
    return None


def pawa_load(bookie, code):
    j = pawa_http(bookie, "GET", f"/api/sportsbook/v3/booking-number/{code}")
    picks = []
    for it in j.get("items") or []:
        ev = it.get("eventInfo") or {}
        parts = sorted(ev.get("participants") or [], key=lambda x: x.get("position", 0))
        home, away = (parts[0].get("name") if parts else None), (parts[1].get("name") if len(parts) > 1 else None)
        for s in it.get("selections") or []:
            m, si = s.get("market") or {}, s.get("selectionInfo") or {}
            kind, out = PW_M.get(str(m.get("typeId"))), PW_O.get(str(si.get("typeId")))
            line = _line(si.get("handicap") or m.get("formattedHandicap")) if kind in LINED else None
            key = (kind, line, out) if kind and out and (kind not in LINED or line) and (ev.get("category") or {}).get("id") == "2" else None
            picks.append({"eventId": str(ev.get("id")), "priceId": str(si.get("id")), "pawaEvent": str(ev.get("id")),
                          "marketId": str(m.get("typeId")), "specifier": None, "outcomeId": str(si.get("typeId")),
                          "game": f"{home} vs {away}", "market": m.get("displayName") or m.get("name") or "",
                          "pick": (si.get("displayName") or si.get("name") or "").replace("{formattedHandicap}", m.get("formattedHandicap") or ""),
                          "odds": _f((it.get("odds") or {}).get("price")) if len(it.get("selections") or []) == 1 else 0.0,
                          "started": bool((ev.get("additionalInfo") or {}).get("live")),
                          "home": home, "away": away, "start": _ts(ev.get("startTime")), "key": key, "sport": "pawa:" + str((ev.get("category") or {}).get("id"))})
    missing = (j.get("originalCount") or 0) - len(j.get("items") or [])
    if missing > 0 and picks:
        picks[0]["_note"] = f"{missing} pick(s) in this code are no longer available on {label(bookie)}"
    return picks


def pawa_create(bookie, price_ids):
    j = pawa_http(bookie, "POST", "/api/sportsbook/v3/booking-number",
                  {"selections": {"selections": [{"type": "SINGLE", "selections": [str(x)]} for x in price_ids]}})
    if j.get("code"):
        return j["code"]
    raise BetError("booking failed")


_STOP = {"fc", "cf", "sc", "afc", "ac", "fk", "sk", "cd", "ca", "club", "de", "the", "sv", "if", "bk", "ud", "us", "as", "kv", "nk", "vfb", "vfl", "tsg", "1", "u19", "u21", "u23"}


def _norm(n):
    import unicodedata
    n = unicodedata.normalize("NFKD", (n or "").lower()).encode("ascii", "ignore").decode()
    n = re.sub(r"\butd\b", "united", re.sub(r"[^a-z0-9 ]", " ", n))
    return [w for w in n.split() if w not in _STOP]


def _sim(a, b):
    from difflib import SequenceMatcher
    ta, tb = _norm(a), _norm(b)
    if not ta or not tb:
        return 0.0
    r = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    sa, sb = set(ta), set(tb)
    if sa <= sb or sb <= sa:
        r = max(r, 0.85)
    return r


def _ev_teams(ev):
    parts = sorted(ev.get("participants") or [], key=lambda x: x.get("position", 0))
    if len(parts) >= 2:
        return parts[0].get("name"), parts[1].get("name")
    nm = (ev.get("name") or "").split(" - ", 1)
    return (nm[0], nm[1]) if len(nm) == 2 else (None, None)


def _search_terms(name):
    words = [w for w in _norm(name) if len(w) >= 4]
    out = [name] if name else []
    out += sorted(words, key=len, reverse=True)[:2]
    return list(dict.fromkeys(out))


def pawa_find_event(bookie, p, cache):
    """Find the betPawa event for a pick: Betradar id first, else team names + kickoff (within 20 minutes)."""
    sr = (p.get("eventId") or "").split(":")[-1] if str(p.get("eventId") or "").startswith("sr:match:") else None
    ck = (sr or p.get("game"))
    if ck in cache:
        return cache[ck]
    best, best_score = None, 0.0
    for team in (p.get("home"), p.get("away")):
        for term in _search_terms(team):
            try:
                j = pawa_http(bookie, "GET", "/api/sportsbook/v3/search?" + requests.compat.urlencode({"name": term}))
            except BetError:
                continue
            for ev in j.get("events") or []:
                if (ev.get("category") or {}).get("id") not in (None, "2"):
                    continue
                if sr and _sr(ev) == sr:
                    cache[ck] = ev
                    return ev
                h, a = _ev_teams(ev)
                sc = min(_sim(p.get("home"), h), _sim(p.get("away"), a))
                st, et = p.get("start"), _ts(ev.get("startTime"))
                if st and et and abs(st - et) > 1200:
                    continue
                if not (st and et):
                    sc -= 0.1
                if sc > best_score:
                    best, best_score = ev, sc
            if best_score >= 0.75:
                break
        if best_score >= 0.75:
            break
    ev = best if best_score >= 0.7 else None
    cache[ck] = ev
    return ev


def pawa_price(bookie, ev_id, key, cache):
    ck = ("ev", ev_id)
    if ck not in cache:
        cache[ck] = pawa_http(bookie, "GET", f"/api/sportsbook/v4/events/{ev_id}")
    ev = cache[ck]
    kind, line, out = key
    for m in ev.get("markets") or []:
        if PW_M.get(str((m.get("marketType") or {}).get("id"))) != kind:
            continue
        for row in m.get("row") or []:
            for pr in row.get("prices") or []:
                if PW_O.get(str(pr.get("typeId"))) != out:
                    continue
                if kind in LINED and _line((row.get("specifier") or {}).get("total") or pr.get("handicap")) != line:
                    continue
                if pr.get("suspended"):
                    return None, "suspended right now"
                return pr, None
    return None, "market or line not offered"


def translate(picks, src, dst):
    """Rewrite picks so create_code(dst, ...) can book them. Returns (ok, unmatched)."""
    ok, um, cache = [], [], {}
    for p in picks:
        try:
            if src in PAWA and dst in PAWA:
                ok.append(p)
            elif dst in PAWA:
                key = br_key(p)
                if not key or p.get("sport") not in (None, "sr:sport:1"):
                    um.append((p, f"market not supported for {label(dst)} yet"))
                    continue
                ev = pawa_find_event(dst, p, cache)
                if not ev:
                    um.append((p, f"match not found on {label(dst)}"))
                    continue
                pr, why = pawa_price(dst, ev["id"], key, cache)
                if not pr:
                    um.append((p, f"{why} on {label(dst)}"))
                    continue
                q = dict(p, priceId=str(pr["id"]), dst_odds=_f(pr.get("odds")))
                ok.append(q)
            else:  # betPawa -> Betradar family (SportyBet / Football.com / MSport)
                key = p.get("key")
                if not key:
                    um.append((p, f"market not supported for {label(dst)} yet"))
                    continue
                ck = ("ev", p["pawaEvent"])
                if ck not in cache:
                    cache[ck] = pawa_http(src, "GET", f"/api/sportsbook/v4/events/{p['pawaEvent']}")
                sr = _sr(cache[ck])
                if not sr:
                    um.append((p, f"match not found on {label(dst)}"))
                    continue
                mk, oc = BR_REV[(key[0], key[2])]
                ok.append(dict(p, dstsel={"eventId": f"sr:match:{sr}", "marketId": mk,
                                          "specifier": f"total={key[1]}" if key[0] in LINED else None, "outcomeId": oc}))
        except BetError as e:
            um.append((p, str(e)))
        except Exception as e:
            um.append((p, f"could not check on {label(dst)} ({str(e)[:60]})"))
    return ok, um
