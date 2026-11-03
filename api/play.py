"""
Google Play keyword search via the Play Store mobile API (the same API the
Play Store Android app uses). Anonymous session tokens come from the Aurora
Store token dispenser, so no Google account is needed. Pages are followed
through nextPageUrl until `num` apps are collected (Play serves ~250 max).

GET /api/play?term=screen+mirroring&num=250&locale=en_US&debug=0
"""
import json
import os
import time
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

_API = {"obj": None, "locale": None, "ts": 0}
_TOKEN_TTL = 50 * 60  # re-login after 50 minutes


def _get_api(locale, force=False):
    from playstoreapi.googleplay import GooglePlayAPI
    now = time.time()
    if not force and _API["obj"] is not None and _API["locale"] == locale and now - _API["ts"] < _TOKEN_TTL:
        return _API["obj"]
    api = GooglePlayAPI(locale=locale, timezone="America/New_York",
                        device_codename=os.environ.get("PLAYSTORE_DEVICE", "op_8_pro"))
    _localize_device(api, locale)
    gsf = os.environ.get("PLAYSTORE_GSFID")
    tok = os.environ.get("PLAYSTORE_TOKEN")
    if gsf and tok:
        api.login(gsfId=int(gsf), authSubToken=tok, check=False)
    else:
        _anon_login(api)
    _API.update(obj=api, locale=locale, ts=now)
    return api


# MCC+MNC of a big carrier per country: Play uses the SIM/cell operator (X-DFE-MCCMNC)
# to pick the storefront, so a Mexican default SIM would return Mexican rankings.
OPERATORS = {"US": "310260", "GB": "23410", "CA": "302720", "AU": "50501", "DE": "26201", "FR": "20801",
             "IN": "40445", "PK": "41001", "AE": "42402", "SA": "42001", "BR": "72405", "JP": "44010",
             "KR": "45005", "MX": "334020", "ES": "21407", "IT": "22210", "TR": "28601", "ID": "51010",
             "NG": "62130", "PH": "51502"}
TIMEZONES = {"US": "America/New_York", "GB": "Europe/London", "CA": "America/Toronto", "AU": "Australia/Sydney",
             "DE": "Europe/Berlin", "FR": "Europe/Paris", "IN": "Asia/Kolkata", "PK": "Asia/Karachi",
             "AE": "Asia/Dubai", "SA": "Asia/Riyadh", "BR": "America/Sao_Paulo", "JP": "Asia/Tokyo",
             "KR": "Asia/Seoul", "MX": "America/Mexico_City", "ES": "Europe/Madrid", "IT": "Europe/Rome",
             "TR": "Europe/Istanbul", "ID": "Asia/Jakarta", "NG": "Africa/Lagos", "PH": "Asia/Manila"}


def _localize_device(api, locale):
    country = (locale.split("_")[-1] if "_" in locale else "US").upper()
    op = OPERATORS.get(country, OPERATORS["US"])
    tz = TIMEZONES.get(country, "America/New_York")
    d = api.deviceBuilder.device
    d["celloperator"] = op
    d["simoperator"] = op
    d["timezone"] = tz
    api.deviceBuilder.setTimezone(tz)
    cp = api.deviceBuilder.device_case_preserved
    cp["CellOperator"] = op
    cp["SimOperator"] = op
    cp["TimeZone"] = tz


DISPENSERS = [d for d in [os.environ.get("PLAYSTORE_DISPENSER_URL"),
                          "https://auroraoss.com/api/auth",
                          "https://auroraoss.com/api/auth/",
                          "http://goolag.store:1337/api/auth"] if d]

UA_VARIANTS = [
    "com.aurora.store-4.7.2-66",
    "Aurora Store 4.7.2 (Android 13; Pixel 3a)",
    "okhttp/4.12.0",
]


def _anon_login(api):
    """Ask an Aurora token dispenser for an anonymous Play Store session.
    Tries several dispensers / user agents because some block generic clients."""
    import requests
    device = api.deviceBuilder.device_case_preserved
    errors = []
    for url in DISPENSERS:
        for ua in UA_VARIANTS:
            try:
                r = requests.post(url, json=device, timeout=25,
                                  headers={"User-Agent": ua, "Content-Type": "application/json",
                                           "Accept": "application/json"})
                if r.status_code != 200:
                    errors.append(f"{url} [{ua}] -> {r.status_code} {r.text[:120]!r}")
                    continue
                j = r.json()
                api.login(gsfId=int(j["gsfId"], 16), authSubToken=j["authToken"], check=False)
                api.device_config_token = j.get("deviceConfigToken") or None
                api.deviceCheckinConsistencyToken = j.get("deviceCheckInConsistencyToken") or None
                return api
            except Exception as e:  # noqa: BLE001
                errors.append(f"{url} [{ua}] -> {type(e).__name__}: {str(e)[:120]}")
    raise RuntimeError("No token dispenser answered: " + " | ".join(errors))


def _walk(node, out, seen, next_urls):
    """Collect app items and nextPageUrls from a parsed search response."""
    if isinstance(node, list):
        for x in node:
            _walk(x, out, seen, next_urls)
        return
    if not isinstance(node, dict):
        return
    if _is_promo_cluster(node):
        return  # "You might also like" etc.: not search results, and never follow its pages
    cm = node.get("containerMetadata")
    if isinstance(cm, dict) and cm.get("nextPageUrl"):
        next_urls.append(cm["nextPageUrl"])
    pkg = node.get("id") or ""
    details = node.get("details") or {}
    app = details.get("appDetails") if isinstance(details, dict) else None
    if app is not None and pkg and pkg not in seen and node.get("title"):
        seen.add(pkg)
        out.append(_norm(node, app))
    for child in node.get("subItem") or []:
        _walk(child, out, seen, next_urls)


def _is_app(n):
    return isinstance(n, dict) and isinstance(n.get("details"), dict) and n["details"].get("appDetails") is not None


def _is_promo_cluster(n):
    """A titled cluster of apps ("You might also like", "Similar apps", ...) is a recommendation
    rail, not part of the ranked search list. The real results cluster has no title."""
    if not isinstance(n, dict) or _is_app(n):
        return False
    if not (n.get("title") or "").strip():
        return False
    return any(_is_app(c) for c in n.get("subItem") or [])


def _walk_first(docs, out, seen, next_urls):
    """First page: Play mixes extra clusters (promos, 'you might also like') into the response
    on some sessions. Keep only the featured exact-match card and the paginated results list."""
    clusters = []
    all_next = []

    def collect(node):
        if isinstance(node, list):
            for x in node:
                collect(x)
            return
        if not isinstance(node, dict):
            return
        if _is_promo_cluster(node):
            return
        apps = [c for c in node.get("subItem") or [] if _is_app(c)]
        cm = node.get("containerMetadata") or {}
        nxt = cm.get("nextPageUrl") if isinstance(cm, dict) else None
        if nxt and nxt not in all_next:
            all_next.append(nxt)
        if apps:
            clusters.append({"apps": apps, "next": nxt})
        for c in node.get("subItem") or []:
            if not _is_app(c):
                collect(c)

    collect(docs)
    main = next((c for c in clusters if c["next"]), None)
    if main is None:
        _walk(docs, out, seen, next_urls)  # unknown layout: fall back to everything
        return
    before = [c for c in clusters[:clusters.index(main)] if len(c["apps"]) == 1]
    featured = before[-1:]  # the exact-match card sits right above the results list
    for c in featured + [main]:
        for a in c["apps"]:
            pkg = a.get("id") or ""
            if pkg and pkg not in seen and a.get("title"):
                seen.add(pkg)
                out.append(_norm(a, a["details"]["appDetails"]))
    next_urls.extend(all_next)


def _int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _norm(item, app):
    rating = item.get("aggregateRating") or {}
    offers = item.get("offer") or []
    offer = offers[0] if offers else {}
    icon = ""
    for im in item.get("image") or []:
        if im.get("imageType") == 4 and im.get("imageUrl"):
            icon = im["imageUrl"]
            break
    if not icon:
        for im in item.get("image") or []:
            if im.get("imageUrl"):
                icon = im["imageUrl"]
                break
    micros = offer.get("micros")
    try:
        price = (int(micros) / 1e6) if micros is not None else None
    except (TypeError, ValueError):
        price = None
    return {
        "appId": item.get("id"),
        "title": item.get("title", ""),
        "developer": app.get("developerName") or item.get("creator", ""),
        "icon": icon,
        "url": "https://play.google.com/store/apps/details?id=" + str(item.get("id")),
        "score": rating.get("starRating"),
        "ratings": _int(rating.get("ratingsCount")),
        "ratingsText": rating.get("ratingCountLabelAbbreviated") or rating.get("ratingCountLabel") or "",
        "price": price,
        "currency": offer.get("currencyCode", ""),
        "free": (price == 0) if price is not None else None,
        "priceText": offer.get("formattedAmount", ""),
        "installs": app.get("downloadLabelDisplay") or app.get("downloadLabel") or app.get("installNotes") or "",
        "installsAbbrev": app.get("downloadLabelAbbreviated") or "",
        "downloadCount": app.get("downloadCount"),
        "genre": app.get("categoryName") or "",
        "genreId": app.get("appCategory", [None])[0] if isinstance(app.get("appCategory"), list) else app.get("appCategory"),
        "version": app.get("versionString", ""),
        "updated": app.get("infoUpdatedOn", ""),
        "contentRating": app.get("contentRating", ""),
        "offersIAP": app.get("inAppProduct", ""),
        "summary": item.get("promotionalDescription", "") or "",
    }


def _first_page(api, term, qs):
    """First search call; `qs` is an optional raw query-string suffix (e.g. 'n=250')."""
    import requests as _rq
    from playstoreapi.googleplay import URL_SEARCH
    from playstoreapi import utils as _u
    path = URL_SEARCH + "?c=3&q=" + _rq.utils.quote(term) + (("&" + qs) if qs else "")
    data = api.executeRequestApi2(path)
    resp = data.preFetch[0].response if _u.hasPrefetch(data) else data
    return list(map(_u.parseProtobufObj, resp.payload.listResponse.item))


WEB_NOTE = {"note": ""}


def _web_order(term, locale, host=None):
    ids = _web_order_direct(term, locale)
    if ids:
        WEB_NOTE["note"] = "direct:" + str(len(ids))
        return ids
    if host:
        try:
            import requests as _rq
            lang = locale.split("_")[0].lower()
            country = (locale.split("_")[-1] if "_" in locale else "US").lower()
            r = _rq.get("https://" + host + "/api/search", params={"term": term, "country": country, "lang": lang, "num": 50},
                        timeout=25, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0",
                                             "Accept": "application/json"})
            try:
                j = r.json()
            except Exception:
                WEB_NOTE["note"] += " node-http:" + str(r.status_code) + " " + r.text[:60].replace("\n", " ")
                return []
            ids = [x.get("appId") for x in j.get("results", []) if x.get("appId")]
            WEB_NOTE["note"] += " node:" + str(len(ids)) + (" err=" + str(j.get("error"))[:80] if j.get("error") else "")
            return ids
        except Exception as e:  # noqa: BLE001
            WEB_NOTE["note"] += " node-exc:" + str(e)[:80]
    return []


def _web_order_direct(term, locale):
    """Order of the public play.google.com search page (what users and ASO tools see).
    Returns a list of package names in display order (featured card first), or []."""
    import re
    import requests as _rq
    lang = locale.split("_")[0].lower() if locale else "en"
    country = (locale.split("_")[-1] if "_" in locale else "US").lower()
    url = ("https://play.google.com/store/search?q=" + _rq.utils.quote(term) +
           "&c=apps&hl=" + lang + "&gl=" + country)
    try:
        r = _rq.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0 Safari/537.36",
            "Accept-Language": lang + "," + lang + "-" + country.upper() + ";q=0.9"})
        if r.status_code != 200:
            WEB_NOTE["note"] = "direct-http:" + str(r.status_code)
            return []
        html = r.text
        WEB_NOTE["note"] = "direct-len:" + str(len(html))
        # Links appear in the HTML in the same order the page displays them
        # (verified against the rendered DOM), so scan the whole document.
        seen, order = set(), []
        for pkg in re.findall(r"/store/apps/details\?id=([A-Za-z0-9_.]+)", html):
            if pkg not in seen:
                seen.add(pkg)
                order.append(pkg)
        return order
    except Exception as e:  # noqa: BLE001
        WEB_NOTE["note"] = "direct-exc:" + str(e)[:80]
        return []


def _is_rate_limit(e):
    return "429" in str(e) or "Too Many" in str(e)


def run_search(term, num, locale, debug=False, qs="", host=None):
    api = _get_api(locale)
    out, seen, raw_pages = [], set(), 0
    next_urls = []
    relogins = 0
    try:
        pages = _first_page(api, term, qs)
    except Exception as e:
        if not _is_rate_limit(e):
            raise
        api = _get_api(locale, force=True)
        relogins += 1
        pages = _first_page(api, term, qs)
    raw_first = pages
    _walk_first(pages, out, seen, next_urls)
    raw_pages += 1
    visited = set()
    while len(out) < num and next_urls and raw_pages < 60:
        url = next_urls.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            page = api.search(nextPageUrl=url)
        except Exception as e:
            if _is_rate_limit(e) and relogins < 3:
                # Google throttled this anonymous session: get a fresh one and retry the page
                try:
                    api = _get_api(locale, force=True)
                    relogins += 1
                    time.sleep(0.5)
                    page = api.search(nextPageUrl=url)
                except Exception as e2:
                    if debug:
                        out.append({"_pageError": str(e2)})
                    break
            else:
                if debug:
                    out.append({"_pageError": str(e)})
                break
        time.sleep(0.12)
        before = len(out)
        _walk(page, out, seen, next_urls)
        raw_pages += 1
        if len(out) == before and not next_urls:
            break
    # Align the head of the list with the public Play web search order (the ranking users
    # and ASO tools see); the mobile feed supplies the long tail and all metadata.
    WEB_NOTE["note"] = ""
    web_ids = _web_order(term, locale, host)
    by_id = {r["appId"]: r for r in out}
    merged, used = [], set()
    for pkg in web_ids:
        r = by_id.get(pkg)
        if r is None:
            try:
                item = api.details(pkg)
                app = (item.get("details") or {}).get("appDetails")
                if app is not None:
                    r = _norm(item, app)
            except Exception:
                r = None
        if r is not None and pkg not in used:
            r["rankSource"] = "web"
            merged.append(r)
            used.add(pkg)
    for r in out:
        if r["appId"] not in used:
            r["rankSource"] = "api"
            merged.append(r)
            used.add(r["appId"])
    result = merged[:num]
    for i, r in enumerate(result):
        r["rank"] = i + 1
    resp = {"term": term, "locale": locale, "count": len(result), "pagesFetched": raw_pages, "relogins": relogins,
            "webVerified": len([r for r in result if r.get("rankSource") == "web"]), "webNote": WEB_NOTE["note"],
            "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": result}
    if debug:
        resp["debugFirstPage"] = json.loads(json.dumps(raw_first, default=str))[:1]
    return resp


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "s-maxage=600, stale-while-revalidate=1800")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        term = (q.get("term", [""])[0] or "").strip()
        if not term:
            return self._send(400, {"error": "term is required"})
        try:
            num = max(1, min(int(q.get("num", ["250"])[0]), 250))
        except ValueError:
            num = 250
        locale = q.get("locale", ["en_US"])[0]
        if not all(ch.isalnum() or ch == "_" for ch in locale) or len(locale) > 6:
            locale = "en_US"
        debug = q.get("debug", ["0"])[0] == "1"
        qs = q.get("qs", [""])[0]
        if not all(ch.isalnum() or ch in "=&_" for ch in qs) or len(qs) > 60:
            qs = ""
        try:
            host = self.headers.get("x-forwarded-host") or self.headers.get("host")
            self._send(200, run_search(term, num, locale, debug, qs, host))
        except Exception as e:
            _API["obj"] = None  # force re-login next time
            self._send(502, {"error": "Play Store API failed: " + str(e),
                             "trace": traceback.format_exc()[-1500:] if debug else None})
