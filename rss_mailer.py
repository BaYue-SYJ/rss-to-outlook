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

import argostranslate.package
import argostranslate.translate


OPML_PATH = "feeds.opml"

FEED_TIMEOUT_SECONDS = 15
PER_FEED_LIMIT = 10
LOOKBACK_HOURS = 24


def escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load_feeds_from_opml_file(opml_path: str) -> list[str]:
    with open(opml_path, "r", encoding="utf-8") as f:
        content = f.read().strip()

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


def fetch_feed_bytes(url: str, timeout: int) -> bytes:
    req = Request(url, headers={"User-Agent": "rss-mailer/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def safe_parse_feed(url: str, timeout: int):
    """
    对每个 feed 单独设置超时；失败则返回 (None, error_message)。
    """
    try:
        data = fetch_feed_bytes(url, timeout=timeout)
        parsed = feedparser.parse(data)

        # feedparser 有时会给出 bozo_exception（解析异常/不规范）
        if getattr(parsed, "bozo", 0):
            ex = getattr(parsed, "bozo_exception", None)
            if ex:
                return parsed, f"bozo_exception: {type(ex).__name__}: {ex}"

        return parsed, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


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


def fetch_recent_items(feed_urls: list[str], since_utc: datetime, per_feed_limit: int):
    items = []
    failures = []  # (url, reason)

    for url in feed_urls:
        parsed, err = safe_parse_feed(url, timeout=FEED_TIMEOUT_SECONDS)
        if parsed is None:
            print(f"[SKIP] {url} -> {err}")
            failures.append((url, err))
            continue

        if err:
            print(f"[WARN] {url} -> {err}")

        feed_title = getattr(parsed.feed, "title", url) if hasattr(parsed, "feed") else url
        entries = getattr(parsed, "entries", [])[:per_feed_limit]

        for e in entries:
            t = entry_time_utc(e)
            if t and t < since_utc:
                continue

            items.append(
                {
                    "feed": str(feed_title),
                    "title": e.get("title", "无标题"),
                    "link": e.get("link", ""),
                    "time": (t.isoformat() if t else ""),
                }
            )

    return items, failures


def ensure_argos_en_zh_installed():
    """
    确保 Argos 的 en->zh 翻译模型已安装（首次会下载并安装）。
    GitHub Actions 建议用 cache 缓存 ~/.local/share/argos-translate。
    """
    try:
        argostranslate.translate.get_translation_from_codes("en", "zh")
        return
    except Exception:
        pass

    print("[INFO] 安装离线翻译模型（en -> zh），首次运行会下载模型，请稍等...")
    argostranslate.package.update_package_index()
    available = argostranslate.package.get_available_packages()

    pkg = None
    for p in available:
        if p.from_code == "en" and p.to_code == "zh":
            pkg = p
            break
    if not pkg:
        raise RuntimeError("未找到 Argos 的 en->zh 翻译模型（请稍后重试）")

    package_path = pkg.download()
    argostranslate.package.install_from_path(package_path)
    print("[INFO] 离线翻译模型安装完成")


_translate_cache: dict[str, str] = {}


def translate_en_to_zh(text: str) -> str:
    """
    尝试把英文翻译成中文；失败则返回原文。（带缓存）
    """
    text = (text or "").strip()
    if not text:
        return text

    if text in _translate_cache:
        return _translate_cache[text]

    try:
        translator = argostranslate.translate.get_translation_from_codes("en", "zh")
        zh = translator.translate(text)
    except Exception:
        zh = text

    _translate_cache[text] = zh
    return zh


def zh_en_pair(s: str) -> str:
    """
    输出：中文（英文）
    如果翻译失败/本来就是中文，则仅输出原文。
    """
    s = (s or "").strip()
    if not s:
        return ""
    zh = translate_en_to_zh(s)
    if not zh or zh.strip() == s.strip():
        return escape_html(s)
    return f"{escape_html(zh)}（{escape_html(s)}）"


def build_html(items, failures):
    ensure_argos_en_zh_installed()

    parts = []

    if not items:
        parts.append(f"<p>过去 {LOOKBACK_HOURS} 小时没有抓到新的 RSS 条目。</p>")
    else:
        by_feed = {}
        for it in items:
            by_feed.setdefault(it["feed"], []).append(it)

        parts.append(f"<p>每日 RSS 摘要（过去 {LOOKBACK_HOURS} 小时，共 {len(items)} 条）</p>")
        for feed, lst in by_feed.items():
            parts.append(f"<h3>{zh_en_pair(feed)}</h3><ul>")
            for it in lst:
                title = zh_en_pair(it["title"])
                link = it["link"]
                time_s = escape_html(it["time"])
                parts.append(f'<li><a href="{link}">{title}</a> <small>{time_s}</small></li>')
            parts.append("</ul>")

    if failures:
        parts.append(f"<hr/><p>抓取失败（已跳过）: {len(failures)} 个</p><ul>")
        for url, reason in failures[:30]:
            parts.append(
                f"<li><code>{escape_html(url)}</code><br/><small>{zh_en_pair(reason)}</small></li>"
            )
        if len(failures) > 30:
            parts.append(f"<li>……省略 {len(failures) - 30} 个</li>")
        parts.append("</ul>")

    return "\n".join(parts)


def send_email(html_body: str):
    """
    通用 SMTP 发信：
    - 465: SMTP_SSL（QQ 邮箱常用）
    - 587: STARTTLS（部分服务商常用）
    """
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))

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

    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=60, context=context) as server:
            server.login(email_user, email_pass)
            server.sendmail(email_user, [email_to], msg.as_string())
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(email_user, email_pass)
            server.sendmail(email_user, [email_to], msg.as_string())


def main():
    feeds = load_feeds_from_opml_file(OPML_PATH)
    if not feeds:
        raise RuntimeError("feeds.opml 里没有任何 xmlUrl")

    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    items, failures = fetch_recent_items(feeds, since_utc=since, per_feed_limit=PER_FEED_LIMIT)
    html = build_html(items, failures)
    send_email(html)


if __name__ == "__main__":
    main()
