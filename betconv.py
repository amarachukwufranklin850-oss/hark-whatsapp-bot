"""Tygars Bot: bet code converter helpers + /betcheck (reachability test for bookie APIs).
Free for users. 18+ only. No secrets are read or returned here."""
import re
import time
import requests
from flask import Blueprint, request

bp = Blueprint("betconv", __name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# Bookie domains the probe may touch (nothing else).
ALLOWED = ("sportybet.com", "football.com", "bet9ja.com", "1xbet.ng", "1xbet.com", "betking.com",
           "msport.com", "betway.com.ng", "nairabet.com", "betwinner.ng", "betwinner.com", "merrybet.com")

CHECKS = {
    "sportybet": "https://www.sportybet.com/api/ng/orders/share/VESTC6",
    "football": "https://www.football.com/api/ng/orders/share/VESTC6",
    "bet9ja_coupon": "https://coupon.bet9ja.com/desktop/feapi/CouponAjax/GetBookABetCoupon?couponCode=5Y8SWSW&v_cache_version=1.328.0.248",
    "bet9ja_sports": "https://sports.bet9ja.com/desktop/feapi/PalimpsestAjax/GetSports?DISP=0&v_cache_version=1.328.0.248",
    "bet9ja_apigw": "https://apigw.bet9ja.com/",
    "1xbet": "https://1xbet.ng/LineFeed/Get1x2_VZip?sports=1&count=1&lng=en&mode=4",
    "betking": "https://www.betking.com/api/sports/v1/bet/Booked/ABC123/en",
    "msport": "https://www.msport.com/api/ng/facts-center/query/frontend/sport-list",
    "betway": "https://www.betway.com.ng/",
    "nairabet": "https://www.nairabet.com/",
    "betwinner": "https://betwinner.ng/",
    "merrybet": "https://www.merrybet.com/",
}


def hdrs(url, extra=None):
    host = re.match(r"https?://([^/]+)", url).group(1)
    base = ".".join(host.split(".")[-2:]) if not host.endswith(".com.ng") else ".".join(host.split(".")[-3:])
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
         "Referer": f"https://www.{base}/", "Origin": f"https://www.{base}"}
    if "bet9ja" in host:
        h["Referer"], h["Origin"] = "https://sports.bet9ja.com/", "https://sports.bet9ja.com"
    if extra:
        h.update(extra)
    return h


def _allowed(url):
    m = re.match(r"https://([^/]+)", url or "")
    return bool(m) and any(m.group(1) == d or m.group(1).endswith("." + d) for d in ALLOWED)


_probe_hits = []


@bp.get("/betcheck")
def betcheck():
    """Can this server reach each bookie? Returns status codes and a short body preview only."""
    out = {}
    for k, u in CHECKS.items():
        t = time.time()
        try:
            r = requests.get(u, headers=hdrs(u), timeout=15, allow_redirects=True)
            out[k] = {"status": r.status_code, "ms": int((time.time() - t) * 1000),
                      "json": r.text.lstrip()[:1] in "{[", "preview": r.text[:160]}
        except Exception as e:
            out[k] = {"status": 0, "error": str(e)[:160]}
    return out


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
