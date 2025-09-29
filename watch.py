import os, csv, json, re, requests, smtplib, time, random
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from email.mime.text import MIMEText
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, quote

# =======================
# Config / ENV
# =======================
load_dotenv()
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "")


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36 Aviron-Price-Monitor",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
HISTORY_FILE = "history.json"

# Email
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USER = os.getenv("EMAIL_USER", "")
EMAIL_PASS = os.getenv("EMAIL_PASS", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", EMAIL_USER or "pricebot@example.com")
EMAIL_TO   = os.getenv("EMAIL_TO", "")  # allow comma-separated list

# Price parsing defaults
DEFAULT_CURRENCY = (os.getenv("DEFAULT_CURRENCY", "USD") or "USD").strip()
DEFAULT_REGEX = os.getenv("NORMALIZE_REGEX", r"[^0-9\.]")

# Digest behavior
SEND_EMPTY_DIGEST = os.getenv("SEND_EMPTY_DIGEST", "0") == "1"  # send even if no events
DIGEST_SUBJECT_PREFIX = os.getenv("DIGEST_SUBJECT_PREFIX", "[PRICE DIGEST]")

# Optional: ignore tiny changes (0 = alert on any change)
CHANGE_THRESHOLD_PCT = float(os.getenv("CHANGE_THRESHOLD_PCT", "0"))

# Optional: normalize URLs by removing utm_* so history keys stay stable
STRIP_UTM = os.getenv("STRIP_UTM", "1") == "1"

# Optional: floor for final fallback (avoid picking monthly fees etc.)
MIN_PRICE_FLOOR = float(os.getenv("MIN_PRICE_FLOOR", "400"))

# Polite crawling / retry
_LAST_FETCH = {}            # domain -> last fetch time
MIN_DOMAIN_GAP = 3.0        # seconds between same-domain requests
MAX_TRIES = 3               # retries on 429/5xx


# =======================
# Email helper
# =======================
def send_email(subject, body):
    if not (EMAIL_HOST and EMAIL_USER and EMAIL_PASS and EMAIL_TO):
        print("[email disabled]", subject)
        return
    recipients = [e.strip() for e in EMAIL_TO.split(",") if e.strip()]
    if not recipients:
        print("[email disabled - no recipients]", subject)
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=30) as s:
        s.starttls()
        s.login(EMAIL_USER, EMAIL_PASS)
        s.sendmail(EMAIL_FROM, recipients, msg.as_string())


# =======================
# Utilities
# =======================
def _throttle(url: str):
    dom = urlsplit(url).netloc
    now = time.time()
    last = _LAST_FETCH.get(dom, 0)
    gap = now - last
    wait = MIN_DOMAIN_GAP - gap + random.uniform(-5, 5)  # +/- 5s jitter
    if wait > 0:
        time.sleep(wait)
    _LAST_FETCH[dom] = time.time()

from requests.utils import quote  # if not already imported

def maybe_proxy(url: str) -> str:
    """Use scraping proxy only for hydrow.com when a key is present."""
    if SCRAPERAPI_KEY and "hydrow.com" in url:
        return f"https://api.scraperapi.com/?api_key={SCRAPERAPI_KEY}&url={quote(url, safe='')}"
    return url


def http_get_with_backoff(url: str):
    delay = 2.0
    for attempt in range(1, MAX_TRIES + 1):
        _throttle(url)  # keep this small (e.g., 2–3s)
        target = maybe_proxy(url)   # <<<<< use proxy for hydrow.com
        resp = requests.get(target, headers=HEADERS, timeout=45)
        if resp.status_code == 429:
            wait = delay + random.uniform(0, 1.5)
            print(f"[throttle] 429 from {url} — retry {attempt}/{MAX_TRIES} after {wait:.1f}s")
            time.sleep(wait)
            delay = min(delay * 2, 30)
            continue
        if 500 <= resp.status_code < 600:
            wait = delay + random.uniform(0, 1.0)
            print(f"[retry] {resp.status_code} from {url} — retry {attempt}/{MAX_TRIES} after {wait:.1f}s")
            time.sleep(wait)
            delay = min(delay * 2, 30)
            continue
        resp.raise_for_status()
        return resp
    raise ValueError(f"Too Many Requests / server errors after {MAX_TRIES} tries for {url}")


def norm_price(text, regex=DEFAULT_REGEX):
    if text is None:
        return None
    cleaned = re.sub(regex, "", str(text))
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "", cleaned.count(".") - 1)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def normalize_url(u: str) -> str:
    if not u or not STRIP_UTM:
        return u
    try:
        parts = list(urlsplit(u))
        qs = [(k, v) for (k, v) in parse_qsl(parts[3]) if not k.lower().startswith("utm_")]
        parts[3] = urlencode(qs)
        return urlunsplit(parts)
    except Exception:
        return u


def extract_price(url, selector, attr="inner_text", regex=DEFAULT_REGEX, product_hint=None):
    """Extract a price using selector -> JSON-LD -> Peloton-aware fallback -> final largest-$ fallback."""
    resp = http_get_with_backoff(url)
    html = resp.text

    # BeautifulSoup with lxml fallback
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # 1) Primary: user-provided selector
    node = soup.select_one(selector) if selector else None
    if node:
        raw = node.get(attr) if attr and attr != "inner_text" else node.get_text(strip=True)
        amount = norm_price(raw, regex)
        if amount is not None:
            return amount
        # fall through

    # 2) Fallback: JSON-LD price fields
    import json as _json
    for s in soup.select('script[type="application/ld+json"]'):
        try:
            data = _json.loads(s.get_text(strip=True))
            def find_price(obj):
                if isinstance(obj, dict):
                    if "price" in obj and obj["price"]:
                        return obj["price"]
                    if "offers" in obj:
                        p = find_price(obj["offers"])
                        if p is not None:
                            return p
                    for v in obj.values():
                        p = find_price(v)
                        if p is not None:
                            return p
                elif isinstance(obj, list):
                    for it in obj:
                        p = find_price(it)
                        if p is not None:
                            return p
                return None
            p = find_price(data)
            if p is not None:
                amount = norm_price(str(p), regex)
                if amount is not None:
                    return amount
        except Exception:
            pass

    # 3) Fallback (Peloton): Affirm footnote "Based on a price of $X" matched to the product
    if "onepeloton.com" in url and product_hint:
        text = re.sub(r"\s+", " ", html)
        ph = product_hint.lower()
        if "bike+" in ph or "bike plus" in ph:
            pat = r"Get the Peloton Bike\+.*?Based on a price of\s*\$([0-9][\d,\.]+)"
        elif "bike" in ph:
            pat = r"Get the Peloton Bike(?!\+).*?Based on a price of\s*\$([0-9][\d,\.]+)"
        elif "tread" in ph or "treadmill" in ph:
            pat = r"Get the Peloton Tread(?!\+).*?Based on a price of\s*\$([0-9][\d,\.]+)"
        elif "row" in ph:
            pat = r"Get the Peloton Row.*?Based on a price of\s*\$([0-9][\d,\.]+)"
        else:
            pat = None
        if pat:
            m = re.search(pat, text, flags=re.I)
            if m:
                amt = norm_price(m.group(1), regex)
                if amt is not None:
                    return amt

    # 4) FINAL fallback: pick the largest dollar amount on page (above a floor)
    amounts = [norm_price(n, regex) for n in re.findall(r"\$([0-9][\d,\.]+)", html)]
    amounts = [a for a in amounts if a is not None]
    if amounts:
        candidates = [a for a in amounts if a >= MIN_PRICE_FLOOR] or amounts
        return max(candidates)

    raise ValueError("Selector not found and no fallback price detected")


def load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_history(data):
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        os.replace(tmp, HISTORY_FILE)  # atomic replace (good for OneDrive/Dropbox)
    except Exception:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


# =======================
# Main
# =======================
def main():
    # Load watchlist
    with open("watchlist.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    history = load_history()

    # Buckets for digest
    events_by_comp = defaultdict(list)  # competitor -> [lines]
    init_count = 0
    change_count = 0
    error_count = 0

    for r in rows:
        comp = (r.get("competitor") or "").strip()
        name = (r.get("product_name") or "").strip() or "Product"
        url = normalize_url((r.get("product_url") or "").strip())
        selector = (r.get("price_selector_css") or "").strip()
        attr = (r.get("price_attribute") or "inner_text").strip() or "inner_text"
        currency = (r.get("currency") or DEFAULT_CURRENCY).strip() or DEFAULT_CURRENCY
        regex = (r.get("normalize_regex") or DEFAULT_REGEX) or DEFAULT_REGEX

        if not url or not selector:
            print(f"[skip] {comp} — {name}: missing url or selector")
            continue

        try:
            amount = extract_price(url, selector, attr, regex, product_hint=name)
        except Exception as e:
            line = f"ERROR • {comp} — {name}: {e}\n{url}"
            print(line)
            events_by_comp[comp].append(line)
            error_count += 1
            continue

        # Previous value (if any)
        key = url  # -> history key is the exact (normalized) URL
        prev = history.get(key, [{}])[-1].get("amount") if history.get(key) else None

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "competitor": comp,
            "product_name": name,
            "amount": amount,
            "currency": currency
        }
        history.setdefault(key, []).append(entry)

        if prev is None:
            line = f"INIT • {comp} — {name}: {amount} {currency}\n{url}"
            print(line)
            events_by_comp[comp].append(line)
            init_count += 1
        else:
            if amount != prev:
                pct = (abs(amount - prev) / prev * 100) if prev else 100.0
                if pct >= CHANGE_THRESHOLD_PCT:
                    line = f"CHANGE • {comp} — {name}: {prev} → {amount} {currency} ({pct:.2f}%)\n{url}"
                    print(line)
                    events_by_comp[comp].append(line)
                    change_count += 1
                else:
                    print(f"[minor change ignored] {comp} — {name}: {prev} → {amount} {currency} ({pct:.2f}%)")
            else:
                print(f"[no change] {comp} — {name}: {amount} {currency}")

    save_history(history)

    # ---- Send one digest email for all events ----
    total_items = sum(len(v) for v in events_by_comp.values())
    if total_items == 0 and not SEND_EMPTY_DIGEST:
        print("No INIT/CHANGE/ERROR events. (Digest not sent)")
        return

    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    subject = f"{DIGEST_SUBJECT_PREFIX} • {change_count} change(s), {init_count} init(s), {error_count} error(s)"

    lines = [
        f"{DIGEST_SUBJECT_PREFIX} — {date_str}",
        f"Changes: {change_count} | Inits: {init_count} | Errors: {error_count}",
        ""
    ]

    # group by competitor
    for comp in sorted(events_by_comp.keys()):
        lines.append(f"{comp}")
        for item in events_by_comp[comp]:
            for ln in item.splitlines():
                if ln.startswith(("INIT", "CHANGE", "ERROR")):
                    lines.append("  • " + ln)
                else:
                    lines.append("    " + ln)
        lines.append("")

    body = "\n".join(lines).rstrip()
    send_email(subject=subject, body=body)
    print(f"Sent digest with {total_items} item(s).")


if __name__ == "__main__":
    main()
