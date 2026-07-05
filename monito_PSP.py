import os
import re
import json
import smtplib
import time
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin

import requests

URL = "https://download.china-vo.org/psp/hmt/PSP-HMT-DATA/data/"
STATE_FILE = Path("state.json")
DATE_FOLDER_RE = re.compile(r"^\d{8}$")
SUBFOLDER_RE = re.compile(r'href="([^"]+)/"')
START_YEAR_MONTH = (2026, 7)  # 只监控该年月及之后的日期文件夹


def fetch_page(page_url, retries=3):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
    }
    proxies = {"http": None, "https": None}
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                page_url, headers=headers, proxies=proxies, timeout=30
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            last_error = e
            print(f"请求失败（{attempt}/{retries}）: {e}")
            if attempt < retries:
                time.sleep(2 ** attempt)

    raise last_error


def parse_subfolders(html, base_url):
    seen_names = set()
    subfolders = []
    for match in SUBFOLDER_RE.finditer(html):
        name = match.group(1)
        if name == "..":
            continue
        if name in seen_names:
            continue
        seen_names.add(name)
        subfolders.append(urljoin(base_url, name + "/"))
    return subfolders


def should_skip(folder_url):
    """根据 START_YEAR_MONTH 判断是否需要跳过该文件夹。"""
    relative = folder_url[len(URL):].strip("/").split("/")
    depth = len(relative)
    if depth == 1 and relative[0].isdigit() and len(relative[0]) == 4:
        return int(relative[0]) < START_YEAR_MONTH[0]
    if depth >= 2 and relative[0].isdigit() and len(relative[0]) == 4 and relative[1].isdigit():
        return (int(relative[0]), int(relative[1])) < START_YEAR_MONTH
    return False


def collect_date_folders(page_url, visited=None):
    if visited is None:
        visited = set()
    if page_url in visited:
        return set()
    visited.add(page_url)

    html = fetch_page(page_url)
    date_folders = set()
    for sub in parse_subfolders(html, page_url):
        name = sub.rstrip("/").split("/")[-1]
        if should_skip(sub):
            continue
        if DATE_FOLDER_RE.match(name):
            date_folders.add(sub)
        else:
            date_folders.update(collect_date_folders(sub, visited))
    return date_folders


def fetch_date_folders():
    return sorted(collect_date_folders(URL))


def load_seen():
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        seen = set(data.get("seen", []))
        # 兼容旧格式：旧状态保存的是 8 位日期字符串，新格式是完整 URL
        if seen and not all(isinstance(s, str) and s.startswith("http") for s in seen):
            return set()
        return seen
    return set()


def save_seen(folders):
    data = {"seen": sorted(folders)}
    STATE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def send_email(new_folders):
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ["SMTP_PORT"])
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    from_email = os.environ["FROM_EMAIL"]
    to_email = os.environ["TO_EMAIL"]

    names = [f.rstrip("/").split("/")[-1] for f in new_folders]
    urls = "\n".join(new_folders)
    subject = f"[PSP] 观测提醒：{', '.join(names)}"
    body = f"检测到以下新日期文件夹：\n\n{urls}\n\n-- \n自动监控提醒"

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email

    if smtp_port == 465:
        server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
    else:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
        server.starttls()

    with server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(from_email, [to_email], msg.as_string())


def main():
    folders = fetch_date_folders()
    if not folders:
        print("未解析到任何日期文件夹，请检查页面结构是否变化。")
        return

    seen = load_seen()

    if not seen:
        print("首次运行，初始化状态文件，不发送邮件。")
        save_seen(folders)
        return

    new_folders = [name for name in folders if name not in seen]

    if new_folders:
        print(f"发现新文件夹: {new_folders}")
        send_email(new_folders)
        print("邮件已发送。")
    else:
        print(f"没有新文件夹。当前最新: {folders[-1]}")

    save_seen(folders)


if __name__ == "__main__":
    main()
