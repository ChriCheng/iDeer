#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
手动重发 iDeer 已生成的 report 邮件。

特点：
1. 不重新抓取数据
2. 不重新调用 LLM
3. 优先发送 history/reports/<date>/report.html
4. 如果没有 report.html，则读取 report.md 并转换成 HTML
5. 兼容两套环境变量：
   - SMTP_SERVER / SMTP_PORT / SMTP_SENDER / SMTP_RECEIVER / SMTP_PASSWORD
   - IDEER_SMTP_SERVER / IDEER_SMTP_PORT / IDEER_SMTP_SENDER / IDEER_SMTP_RECEIVER / IDEER_SMTP_PASSWORD
"""

from __future__ import annotations

import argparse
import html
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr
from pathlib import Path
import socket
from urllib.parse import urlparse

DEFAULT_TITLE = "Daily Personal Briefing"


def load_dotenv(path: Path) -> None:
    """简易 .env 加载器：和 iDeer 类似，不覆盖已有环境变量。"""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if key.startswith("export "):
            key = key[len("export ") :].strip()

        if (
            value
            and len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ("'", '"')
        ):
            value = value[1:-1]

        os.environ.setdefault(key, value)


def enable_proxy(proxy_url: str) -> None:
    """
    让 smtplib 的 socket 连接走代理。
    推荐使用：
      socks5h://127.0.0.1:7890
    """
    try:
        import socks  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "Missing PySocks. Install it with: pip install PySocks"
        ) from e

    if "://" not in proxy_url:
        proxy_url = "socks5h://" + proxy_url

    parsed = urlparse(proxy_url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    port = parsed.port

    if not host or not port:
        raise RuntimeError(f"Invalid proxy URL: {proxy_url}")

    if scheme in {"socks5", "socks5h"}:
        proxy_type = socks.SOCKS5
        rdns = scheme == "socks5h"
    elif scheme == "socks4":
        proxy_type = socks.SOCKS4
        rdns = False
    elif scheme in {"http", "https"}:
        proxy_type = socks.HTTP
        rdns = False
    else:
        raise RuntimeError(f"Unsupported proxy scheme: {scheme}")

    socks.set_default_proxy(
        proxy_type,
        host,
        port,
        username=parsed.username,
        password=parsed.password,
        rdns=rdns,
    )

    socket.socket = socks.socksocket

    print(f"[Proxy] SMTP socket proxy enabled: {proxy_url}")


def env_first(*names: str, default: str | None = None, required: bool = False) -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip() != "":
            return value.strip()

    if required:
        raise RuntimeError(f"Missing required env var: one of {', '.join(names)}")

    return default or ""


def find_project_root() -> Path:
    """
    假设脚本位置是：
      iDeer/scripts/resend_report_email.py

    那么项目根目录是脚本父目录的父目录。
    如果用户从别的地方运行，也尽量回退到当前工作目录。
    """
    script_path = Path(__file__).resolve()

    if script_path.parent.name == "scripts":
        return script_path.parent.parent

    return Path.cwd().resolve()


def find_latest_report(reports_root: Path) -> Path:
    if not reports_root.exists():
        raise FileNotFoundError(f"reports directory not found: {reports_root}")

    candidates: list[Path] = []

    for day_dir in reports_root.iterdir():
        if not day_dir.is_dir():
            continue

        html_path = day_dir / "report.html"
        md_path = day_dir / "report.md"

        if html_path.exists():
            candidates.append(html_path)
        elif md_path.exists():
            candidates.append(md_path)

    if not candidates:
        raise FileNotFoundError(
            f"No report.html or report.md found under: {reports_root}"
        )

    def sort_key(path: Path):
        # 优先按日期目录名排序，失败则按修改时间
        try:
            return datetime.strptime(path.parent.name, "%Y-%m-%d")
        except ValueError:
            return datetime.fromtimestamp(path.stat().st_mtime)

    return sorted(candidates, key=sort_key)[-1]


def resolve_report_path(
    project_root: Path, date: str | None, report_path: str | None
) -> Path:
    if report_path:
        path = Path(report_path).expanduser()
        if not path.is_absolute():
            path = project_root / path
        if not path.exists():
            raise FileNotFoundError(f"report file not found: {path}")
        return path

    reports_root = project_root / "history" / "reports"

    if date:
        day_dir = reports_root / date
        html_path = day_dir / "report.html"
        md_path = day_dir / "report.md"

        if html_path.exists():
            return html_path
        if md_path.exists():
            return md_path

        raise FileNotFoundError(
            f"No report.html or report.md found for date {date}: {day_dir}"
        )

    return find_latest_report(reports_root)


def markdown_to_html(md_text: str, title: str) -> str:
    """
    优先使用 markdown 库；如果环境没装 markdown，就退化成 <pre>。
    """
    try:
        import markdown  # type: ignore

        body = markdown.markdown(
            md_text,
            extensions=["extra", "tables", "sane_lists"],
            output_format="html5",
        )
    except Exception:
        body = "<pre>" + html.escape(md_text) + "</pre>"

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.65;
  color: #24292f;
  background: #ffffff;
  padding: 24px;
}}
h1, h2, h3 {{
  line-height: 1.35;
}}
a {{
  color: #0969da;
}}
pre {{
  white-space: pre-wrap;
  background: #f6f8fa;
  padding: 16px;
  border-radius: 8px;
}}
code {{
  background: #f6f8fa;
  padding: 2px 4px;
  border-radius: 4px;
}}
table {{
  border-collapse: collapse;
  width: 100%;
}}
th, td {{
  border: 1px solid #d0d7de;
  padding: 6px 10px;
}}
blockquote {{
  border-left: 4px solid #d0d7de;
  padding-left: 12px;
  color: #57606a;
}}
</style>
</head>
<body>
{body}
</body>
</html>"""


def load_report_html(report_file: Path, title: str) -> str:
    content = report_file.read_text(encoding="utf-8")

    if report_file.suffix.lower() in {".html", ".htm"}:
        return content

    return markdown_to_html(content, title)


def format_addr(s: str) -> str:
    name, addr = parseaddr(s)
    return formataddr((Header(name, "utf-8").encode(), addr))


def parse_date_from_report_path(report_file: Path) -> datetime:
    """
    从 history/reports/2026-05-04/report.md 里提取日期。
    提取失败则用当前 UTC 时间。
    """
    for part in reversed(report_file.parts):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
            try:
                return datetime.strptime(part, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                pass

    return datetime.now(timezone.utc)


def send_email_html(
    html_content: str,
    *,
    smtp_server: str,
    smtp_port: int,
    sender: str,
    password: str,
    receiver: str,
    title: str,
    run_datetime: datetime,
    timeout: int = 30,
) -> None:
    receivers = [addr.strip() for addr in receiver.split(",") if addr.strip()]
    if not receivers:
        raise RuntimeError("SMTP_RECEIVER / IDEER_SMTP_RECEIVER is empty")

    msg = MIMEText(html_content, "html", "utf-8")
    msg["From"] = format_addr(f"{title} <{sender}>")
    msg["To"] = ",".join(format_addr(f"You <{addr}>") for addr in receivers)

    today = run_datetime.strftime("%Y/%m/%d")
    msg["Subject"] = Header(f"{title} {today}", "utf-8").encode()

    context = ssl.create_default_context()

    if smtp_port == 465:
        print(f"[SMTP] Using SMTP_SSL: {smtp_server}:{smtp_port}")
        with smtplib.SMTP_SSL(
            smtp_server,
            smtp_port,
            timeout=timeout,
            context=context,
        ) as server:
            server.login(sender, password)
            server.sendmail(sender, receivers, msg.as_string())
    else:
        try:
            print(f"[SMTP] Using SMTP + STARTTLS: {smtp_server}:{smtp_port}")
            with smtplib.SMTP(smtp_server, smtp_port, timeout=timeout) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(sender, password)
                server.sendmail(sender, receivers, msg.as_string())
        except Exception as e:
            print(f"[WARN] STARTTLS mode failed: {e}")
            print(f"[SMTP] Trying SMTP_SSL fallback: {smtp_server}:{smtp_port}")
            with smtplib.SMTP_SSL(
                smtp_server,
                smtp_port,
                timeout=timeout,
                context=context,
            ) as server:
                server.login(sender, password)
                server.sendmail(sender, receivers, msg.as_string())

    print(f"[OK] Email '{title}' sent to {receivers}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Resend generated iDeer report email.")
    parser.add_argument(
        "--date",
        help="指定报告日期，例如 2026-05-04。默认自动寻找最新 report。",
    )
    parser.add_argument(
        "--report",
        help="直接指定 report.html 或 report.md 路径。",
    )
    parser.add_argument(
        "--title",
        help="邮件标题。默认读取 REPORT_TITLE / IDEER_REPORT_TITLE。",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="SMTP 连接超时时间，默认 30 秒。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要发送的配置，不真正发送。",
    )
    parser.add_argument(
        "--proxy",
        help="让 SMTP 连接走代理，例如 socks5h://127.0.0.1:7890",
    )

    args = parser.parse_args()

    project_root = find_project_root()

    # 加载 .env，先加载项目根目录，再加载当前目录，方便手动覆盖。
    load_dotenv(project_root / ".env")
    load_dotenv(Path.cwd() / ".env")

    title = args.title or env_first(
        "REPORT_TITLE", "IDEER_REPORT_TITLE", default=DEFAULT_TITLE
    )

    smtp_server = env_first("SMTP_SERVER", "IDEER_SMTP_SERVER", required=True)
    smtp_port = int(env_first("SMTP_PORT", "IDEER_SMTP_PORT", default="465"))
    sender = env_first("SMTP_SENDER", "IDEER_SMTP_SENDER", required=True)
    receiver = env_first("SMTP_RECEIVER", "IDEER_SMTP_RECEIVER", required=True)
    password = env_first("SMTP_PASSWORD", "IDEER_SMTP_PASSWORD", required=True)

    report_file = resolve_report_path(project_root, args.date, args.report)
    run_datetime = parse_date_from_report_path(report_file)

    print(f"[Project root] {project_root}")
    print(f"[Report file]  {report_file}")
    print(f"[SMTP]        {smtp_server}:{smtp_port}")
    print(f"[Sender]      {sender}")
    print(f"[Receiver]    {receiver}")
    print(f"[Title]       {title}")

    if args.dry_run:
        print("[DRY RUN] Not sending email.")
        return 0
    if args.proxy:
        enable_proxy(args.proxy)
    html_content = load_report_html(report_file, title)

    send_email_html(
        html_content,
        smtp_server=smtp_server,
        smtp_port=smtp_port,
        sender=sender,
        password=password,
        receiver=receiver,
        title=title,
        run_datetime=run_datetime,
        timeout=args.timeout,
    )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        raise SystemExit(1)
