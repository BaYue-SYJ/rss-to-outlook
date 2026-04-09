import os
import ssl
import smtplib
import feedparser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from dateutil import parser as dtparser
from urllib.request import urlopen, Request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


# 你提供的 OPML（raw 链接）
OPML_URL = "https://gist.github.com/emschwartz/e6d2bf860ccc367fe37ff953ba6de66b/raw/426957f043dc0054f95aae6c19de1d0b4ecc2bb2/hn-popular-blogs-2025.opml"


def download_text(url: str) -> str:
    req = Request(url, headers={"User-Agent": "rss-mailer/1.0"})
    with urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def load_feeds_from_opml_url(opml_url: str) -> list[str]:
    content = download_text(opml_url)
    root = ET.fromstring(content)

    urls: list[str] = []
    for node in root.findall(".//outline"):
        xml_url = node.attrib.get("xmlUrl")
        if xml_url:
            urls.append(xml_url.strip())

    # 去重且保序
    seen = set()
    out = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def entry_time_utc(entry) -> datetime | None:
    for k in ("published", "updated"):
        v = entry.get(k)
        if not v:
            continue
        try:
            dt = dtparser.parse(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return None


def fetch_recent_items(feed_urls: list[str], since_utc: datetime, per_feed_limit: int = 10):
    items = []
    for url in feed_urls:
        d = feedparser.parse(url)
        feed_title = getattr(d.feed, "title", url) if hasattr(d, "feed") else url

        entries = getattr(d, "entries", [])[:per_feed_limit]
        for e in entries:
            t = entry_time_utc(e)
            if t and t < since_utc:
                continue

            items.append({
                "feed": str(feed_title),
                "title": e.get("title", "无标题"),
                "link": e.get("link", ""),
                "time": (t.isoformat() if t else ""),
            })
    return items


def escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_html(items):
    if not items:
        return "<p>过去 24 小时没有抓到新的 RSS 条目。</p>"

    by_feed = {}
    for it in items:
        by_feed.setdefault(it["feed"], []).append(it)

    parts = [f"<p>每日 RSS 摘要（过去 24 小时，共 {len(items)} 条）</p>"]
    for feed, lst in by_feed.items():
        parts.append(f"<h3>{escape_html(feed)}</h3><ul>")
        for it in lst:
            title = escape_html(it["title"])
            link = it["link"]
            time_s = escape_html(it["time"])
            parts.append(f'<li><a href="{link}">{title}</a> <small>{time_s}</small></li>')
        parts.append("</ul>")
    return "\n".join(parts)


def send_email_outlook(html_body: str):
    # Outlook SMTP：STARTTLS
    smtp_host = os.environ.get("SMTP_HOST", "smtp-mail.outlook.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    email_user = os.environ["EMAIL_USER"]
    email_pass = os.environ["EMAIL_PASS"]
    email_to = os.environ["EMAIL_TO"]
    subject = os.environ.get("EMAIL_SUBJECT", "每日 RSS 摘要")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_user
    msg["To"] = email_to
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(email_user, email_pass)
        server.sendmail(email_user, [email_to], msg.as_string())


def main():
    feeds = load_feeds_from_opml_url(OPML_URL)
    if not feeds:
        raise RuntimeError("OPML 没有解析到任何 xmlUrl")

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    items = fetch_recent_items(feeds, since_utc=since, per_feed_limit=10)
    html = build_html(items)
    send_email_outlook(html)


if __name__ == "__main__":
    main()
