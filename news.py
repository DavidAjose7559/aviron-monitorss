import os, re, json, feedparser, requests
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, quote_plus
from email.mime.text import MIMEText
from dotenv import load_dotenv
from bs4 import BeautifulSoup

load_dotenv()

EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USER = os.getenv("EMAIL_USER", "")
EMAIL_PASS = os.getenv("EMAIL_PASS", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", EMAIL_USER or "pricebot@example.com")
EMAIL_TO   = os.getenv("EMAIL_TO", "")

NEWS_LOOKBACK_DAYS = int(os.getenv("NEWS_LOOKBACK_DAYS", "14"))
KEY_TERMS = [t.strip().lower() for t in os.getenv("NEWS_KEY_TERMS","").split(",") if t.strip()]

HEADERS = {"User-Agent":"Mozilla/5.0 (Aviron-News-Monitor)"}
HISTORY_FILE = "news_history.json"

def send_email(subject, body):
    import smtplib
    if not (EMAIL_HOST and EMAIL_USER and EMAIL_PASS and EMAIL_TO):
        print("[email disabled] " + subject)
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=30) as s:
        s.starttls()
        s.login(EMAIL_USER, EMAIL_PASS)
        s.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())

def now_utc():
    return datetime.now(timezone.utc)

def in_window(dt):
    return dt >= now_utc() - timedelta(days=NEWS_LOOKBACK_DAYS)

def load_seen():
    if not os.path.exists(HISTORY_FILE): return set()
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(seen):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2)

def try_common_feeds(homepage):
    suffixes = ["feed", "feed.xml", "rss", "rss.xml", "atom.xml", "blog/feed", "news/feed"]
    return [homepage.rstrip("/") + "/" + s for s in suffixes]

def google_news_rss_for_domain(domain):
    q = quote_plus(f"site:{domain}")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

def google_news_rss_for_keyword(keyword):
    q = quote_plus(keyword)
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

def fetch_entries(feed_url):
    try:
        parsed = feedparser.parse(feed_url)
        return parsed.entries or []
    except Exception:
        return []

def normalize_link(link):
    # strip utm params for dedupe
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        parts = list(urlsplit(link))
        qs = [(k,v) for (k,v) in parse_qsl(parts[3]) if not k.lower().startswith("utm_")]
        parts[3] = urlencode(qs)
        return urlunsplit(parts)
    except Exception:
        return link

def main():
    # read competitors file
    import csv
    with open("competitors_news.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    seen = load_seen()
    new_links_global = []
    items_by_comp = {}  # comp -> list of dicts

    for r in rows:
        comp = (r.get("competitor") or "").strip()
        homepage = (r.get("homepage_url") or "").strip().rstrip("/")
        rss = (r.get("news_rss_url") or "auto").strip()
        keyword = (r.get("keyword") or comp).strip() or comp

        feeds = []
        if rss and rss != "auto":
            feeds = [rss]
        else:
            # Try common feed endpoints first
            feeds = try_common_feeds(homepage)
            # Fallbacks: domain-scoped and keyword Google News RSS
            try:
                domain = urlparse(homepage).netloc
            except Exception:
                domain = None
            if domain:
                feeds.append(google_news_rss_for_domain(domain))
            if keyword:
                feeds.append(google_news_rss_for_keyword(keyword))

        for f in feeds:
            entries = fetch_entries(f)
            for e in entries:
                title = getattr(e, "title", "").strip()
                link = normalize_link(getattr(e, "link", "") or getattr(e, "id", ""))
                if not link or link in seen:
                    continue

                # Date: prefer structured times; if missing, treat as "now" (so it's included)
                dt = None
                st = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
                if st:
                    try:
                        dt = datetime(st.tm_year, st.tm_mon, st.tm_mday, st.tm_hour, st.tm_min, st.tm_sec, tzinfo=timezone.utc)
                    except Exception:
                        dt = None
                if dt is None:
                    published_raw = getattr(e, "published", "") or getattr(e, "updated", "")
                    if published_raw:
                        try:
                            import dateutil.parser as dtp
                            dt = dtp.parse(published_raw)
                        except Exception:
                            dt = None
                if dt is None:
                    dt = now_utc()  # ← revert behavior: allow undated items

                if not in_window(dt):
                    continue

                summary = getattr(e, "summary", "")
                try:
                    summary_text = BeautifulSoup(summary, "lxml").get_text("\n", strip=True)[:500]
                except Exception:
                    summary_text = (summary or "")[:500]

                highlight = any(k in title.lower() for k in KEY_TERMS) if KEY_TERMS else False

                items_by_comp.setdefault(comp, []).append({
                    "title": title,
                    "link": link,
                    "date": dt.isoformat(timespec="seconds"),
                    "summary": summary_text,
                    "highlight": highlight,
                })
                seen.add(link)
                new_links_global.append(link)

    # send one email per competitor
    for comp, items in items_by_comp.items():
        if not items:
            continue
        items.sort(key=lambda x: x["date"], reverse=True)
        start = (now_utc() - timedelta(days=NEWS_LOOKBACK_DAYS)).date()
        end = now_utc().date()
        lines = [f"[NEWS DIGEST] {comp} — {start} to {end}\n"]
        for it in items:
            star = "🔎 " if it["highlight"] else ""
            lines.append(f"{star}• {it['title']} ({it['date']})\n{it['link']}")
            if it["summary"]:
                lines.append(f"> {it['summary']}\n")
        body = "\n".join(lines)
        subject = f"[NEWS DIGEST] {comp} — {len(items)} item(s)"
        send_email(subject, body)

    save_seen(seen)
    print(f"Done. Sent {len(items_by_comp)} digest email(s) and {len(new_links_global)} new link(s).")

if __name__ == "__main__":
    main()
