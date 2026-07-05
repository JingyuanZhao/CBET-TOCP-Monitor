#!/usr/bin/env python3
"""
CBET & TOCP 自动监控脚本
监控 RecentCBETs.html 和 tocp.html 页面，发现新公告/报告时自动发送邮件通知。

用法:
  python monitor.py            # 检查并发送邮件（正常模式）
  python monitor.py --dry-run  # 仅检查，不发送邮件，打印详情
  python monitor.py --test     # 测试邮件发送功能
"""

import urllib.request
import urllib.error
import re
import os
import json
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import time
import sys

# ============================================================
# 配置 - 请根据实际情况修改以下内容
# ============================================================

# --- 邮箱配置 ---
# 优先从环境变量读取（GitHub Actions部署时使用），否则使用下面的默认值
# 发件邮箱（使用Gmail SMTP，需要开启两步验证并生成应用专用密码）
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")  # Gmail应用专用密码，请通过环境变量设置

# 收件邮箱
TO_EMAIL = os.environ.get("TO_EMAIL", "")

# --- 监控的页面 ---
CBET_URL = "http://www.cbat.eps.harvard.edu/cbet/RecentCBETs.html"
TOCP_URL = "http://www.cbat.eps.harvard.edu/unconf/tocp.html"

# --- 数据存储 ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

CBET_SEEN_FILE = os.path.join(DATA_DIR, "cbet_seen.json")
TOCP_SEEN_FILE = os.path.join(DATA_DIR, "tocp_seen.json")

# ============================================================
# HTML 解析工具
# ============================================================

class HTMLStripper(HTMLParser):
    """去除HTML标签，提取纯文本"""
    def __init__(self):
        super().__init__()
        self.text = []
        self.skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'head'):
            self.skip = True

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'head'):
            self.skip = False
        if tag in ('br', 'p', 'div', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'pre'):
            self.text.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.text.append(data)

    def get_text(self):
        return ''.join(self.text)


def strip_html(html):
    stripper = HTMLStripper()
    stripper.feed(html)
    text = stripper.get_text()
    lines = []
    for line in text.split('\n'):
        stripped = line.strip()
        if stripped:
            lines.append(stripped)
    return '\n'.join(lines)


def fetch_url(url, max_retries=3):
    """获取网页内容"""
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                for encoding in ['utf-8', 'latin-1', 'iso-8859-1', 'cp1252']:
                    try:
                        return raw.decode(encoding)
                    except (UnicodeDecodeError, LookupError):
                        continue
                return raw.decode('utf-8', errors='replace')
        except Exception as e:
            print(f"  [!] 获取失败 (尝试 {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(5)
            else:
                raise
    return None


def fetch_last_modified(url):
    """获取URL的Last-Modified响应头，没有则回退到Date头，返回UTC+8格式的时间字符串"""
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            from email.utils import parsedate_to_datetime
            from datetime import timedelta

            last_mod = resp.getheader('Last-Modified')
            if not last_mod:
                last_mod = resp.getheader('Date')
            if last_mod:
                dt_utc = parsedate_to_datetime(last_mod)
                dt_cn = dt_utc + timedelta(hours=8)
                return f"{dt_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC ({dt_cn.strftime('%Y-%m-%d %H:%M:%S')} UTC+8)"
    except Exception as e:
        print(f"    获取时间失败: {e}")
    return None


# ============================================================
# CBET 解析
# ============================================================

def parse_cbet_items(html):
    """
    解析 RecentCBETs.html 中的CBET公告条目。
    页面结构: <li> CBET   5712 : 20260704 : <a href="/iau/cbet/005700/CBET005712.txt">(7040) HARWOOD</a>
    """
    items = {}

    # 匹配 <li> CBET  NNNN : YYYYMMDD : <a href="...">标题</a>
    # 也兼容链接在 "CBET NNNN" 前面的格式
    li_pattern = re.compile(
        r'(?:<li>)?\s*(?:<a[^>]*>[^<]*</a>\s*:\s*)?'
        r'(?:CBET|cbet)\s*(\d+)\s*:\s*(\d{8})\s*:\s*'
        r'<a\s+[^>]*href\s*=\s*["\']([^"\']+\.(?:txt|html?))["\'][^>]*>(.*?)</a>',
        re.IGNORECASE
    )

    # 备用模式: <a href="...">CBET XXXX</a> 然后是标题
    alt_pattern = re.compile(
        r'<a\s+[^>]*href\s*=\s*["\']([^"\']*?CBET[^"\']*?\.(?:txt|html?))["\'][^>]*>'
        r'\s*(?:CBET\s*(\d+)|(.*?))\s*</a>\s*(?:[:：\-]\s*(.*?))?(?:<|$)',
        re.IGNORECASE
    )

    found_ids = set()

    for match in li_pattern.finditer(html):
        cbet_num = match.group(1).strip()
        date_str = match.group(2).strip()
        href = match.group(3)
        title = re.sub(r'<[^>]+>', '', match.group(4)).strip()

        cbet_id = f"CBET{cbet_num}"
        if cbet_id in found_ids:
            continue
        found_ids.add(cbet_id)

        # 格式化日期 YYYYMMDD -> YYYY-MM-DD
        date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

        if href.startswith('http'):
            full_url = href
        elif href.startswith('/'):
            full_url = 'http://www.cbat.eps.harvard.edu' + href
        else:
            full_url = 'http://www.cbat.eps.harvard.edu/' + href.lstrip('/')

        items[cbet_id] = {
            'id': cbet_id,
            'title': title,
            'date': date_fmt,
            'url': full_url,
        }

    # 如果上面没匹配到，试试备用模式
    if not items:
        for match in alt_pattern.finditer(html):
            href = match.group(1)
            cbet_num = match.group(2)
            title_from_link = match.group(3)
            title_after = match.group(4)

            if cbet_num:
                cbet_id = f"CBET{cbet_num.strip()}"
                title = title_after.strip() if title_after else ''
            else:
                continue

            if cbet_id in found_ids:
                continue
            found_ids.add(cbet_id)

            if href.startswith('http'):
                full_url = href
            elif href.startswith('/'):
                full_url = 'http://www.cbat.eps.harvard.edu' + href
            else:
                full_url = 'http://www.cbat.eps.harvard.edu/' + href.lstrip('/')

            items[cbet_id] = {
                'id': cbet_id,
                'title': title,
                'date': '',
                'url': full_url,
            }

    return items


def fetch_cbet_content(url):
    """获取CBET公告的正文内容"""
    try:
        content = fetch_url(url)
        if not content:
            return "无法获取正文"

        # 如果是纯文本文件（.txt URL），直接返回
        if url.lower().endswith('.txt'):
            return content[:8000] if len(content) > 8000 else content

        # 如果是HTML，解析 body
        body_match = re.search(r'<body[^>]*>(.*?)</body>', content, re.DOTALL | re.IGNORECASE)
        body_html = body_match.group(1) if body_match else content
        text = strip_html(body_html)
        return text[:8000] if len(text) > 8000 else text
    except Exception as e:
        return f"获取正文失败: {e}"


# ============================================================
# TOCP 解析
# ============================================================

def parse_tocp_items(html):
    """
    解析 tocp.html 中的TOCP报告条目。
    条目格式: <a href="/unconf/followups/XXXXX.html">名称</a> + 日期/描述
    正文在详情页中。
    """
    items = {}

    # 先找到 name="entries" 锚点之后的内容，这是主要条目区域
    entries_match = re.search(r'<a\s+name\s*=\s*["\']entries["\'][^>]*>', html, re.IGNORECASE)
    if entries_match:
        content_html = html[entries_match.start():]
    else:
        content_html = html

    # 匹配 followups 链接，格式: <a href="/unconf/followups/XXXXX.html">名称</a>
    fu_pattern = re.compile(
        r'<a\s+[^>]*href\s*=\s*["\']([^"\']*followups/[^"\']*\.(?:html?|txt))["\'][^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL
    )

    for match in fu_pattern.finditer(content_html):
        href = match.group(1)
        link_text = re.sub(r'<[^>]+>', '', match.group(2)).strip()
        link_end = match.end()

        # 提取链接前后的上下文，找日期和描述
        context_start = max(0, match.start() - 100)
        context_before = content_html[context_start:match.start()]
        context_after = content_html[link_end:link_end + 300]

        # 提取日期 - 日期在链接后面（同一行），先搜 after 再搜 before
        date = ''
        date_patterns = [
            r'(\d{4}\s+\d{2}\s+\d{2}(?:\.\d+)?)',  # TOCP: 2011 01 03.12
            r'(\d{4}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2})',
            r'(\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{4})',
        ]
        for dp in date_patterns:
            dm = re.search(dp, context_after, re.IGNORECASE)
            if dm:
                date = dm.group(1)
                break
            dm = re.search(dp, context_before, re.IGNORECASE)
            if dm:
                date = dm.group(1)
                break

        # 提取描述/坐标 - 在链接前面或后面的文本
        desc = ''
        desc_match = re.search(r'<td[^>]*>(.*?)</td>', context_before, re.DOTALL | re.IGNORECASE)
        if desc_match:
            desc = re.sub(r'<[^>]+>', '', desc_match.group(1)).strip()
        if not desc:
            desc_match = re.search(r'<td[^>]*>(.*?)</td>', context_after, re.DOTALL | re.IGNORECASE)
            if desc_match:
                desc = re.sub(r'<[^>]+>', '', desc_match.group(1)).strip()

        # 构建标题
        title = link_text if link_text else desc
        if not title:
            # 从URL中提取名称
            title_match = re.search(r'followups/([^/]+)\.', href)
            title = title_match.group(1) if title_match else f"TOCP Entry"

        # 构建完整URL
        if href.startswith('http'):
            full_url = href
        elif href.startswith('/'):
            full_url = 'http://www.cbat.eps.harvard.edu' + href
        else:
            full_url = 'http://www.cbat.eps.harvard.edu/' + href.lstrip('/')

        item_id = hashlib.md5((title + date).encode()).hexdigest()[:12]

        if item_id not in items:
            items[item_id] = {
                'id': item_id,
                'title': title,
                'date': date,
                'url': full_url,
            }

    return items


def fetch_tocp_content(url):
    """获取TOCP报告详情页的正文内容"""
    try:
        html = fetch_url(url)
        if not html:
            return None

        body_match = re.search(r'<body[^>]*>(.*?)</body>', html, re.DOTALL | re.IGNORECASE)
        body_html = body_match.group(1) if body_match else html

        # 找到 "Transient Object Followup Reports" 之后的内容，跳过导航菜单
        content_start = 0
        title_patterns = [
            r'Transient\s+Object\s+Followup\s+Reports',
            r'Followup\s+Reports',
        ]
        for tp in title_patterns:
            m = re.search(tp, body_html, re.IGNORECASE)
            if m:
                content_start = m.end()
                break

        content_html = body_html[content_start:]

        # 提取纯文本
        text = re.sub(r'<br\s*/?>', '\n', content_html, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = text.strip()

        # 跳过空行和残留的导航文字，找到实际内容
        lines = text.split('\n')
        result_lines = []
        started = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if started:
                    result_lines.append('')
                continue
            # 当遇到看起来像天体名称的行时开始收集
            if re.match(r'(?:PSN|PNV|TCP|OGLE|MASTER|ASASSN|AT|GRB|COMET|NOVA|SUPERNOVA)\s', stripped, re.IGNORECASE):
                started = True
            if started:
                result_lines.append(stripped)
            elif re.match(r'^(?:PNV|PSN|TCP|OGLE)\s+J\d+', stripped):
                started = True
                result_lines.append(stripped)

        # 跳过第一行（页面标题，与正文第一行重复）
        if result_lines:
            result_lines.pop(0)
            # 跳过标题后的空行
            while result_lines and not result_lines[0].strip():
                result_lines.pop(0)

        if not result_lines:
            result_lines = [l.strip() for l in lines if l.strip()]

        content = '\n'.join(result_lines)
        return content[:8000] if len(content) > 8000 else content
    except Exception as e:
        return f"获取正文失败: {e}"


# ============================================================
# 状态管理
# ============================================================

def load_seen(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_seen(filepath, seen):
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def find_new_items(current_items, seen):
    new_items = {}
    for item_id, item in current_items.items():
        if item_id not in seen:
            new_items[item_id] = item
    return new_items


# ============================================================
# 邮件发送
# ============================================================

def send_email(subject, html_body):
    """发送邮件通知"""
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = SMTP_USER
    msg['To'] = TO_EMAIL
    msg['Date'] = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')

    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    try:
        if SMTP_PORT == 465:
            # SSL 直连模式
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_USER, [TO_EMAIL], msg.as_string())
        else:
            # STARTTLS 模式 (Gmail 587端口推荐)
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_USER, [TO_EMAIL], msg.as_string())
        print(f"  [+] 邮件已发送到 {TO_EMAIL}")
        return True
    except Exception as e:
        print(f"  [!] 邮件发送失败: {e}")
        return False


def build_cbet_email(item):
    """构建单条CBET通知邮件HTML"""
    cbet_display = item['id'].replace('CBET', 'CBET ', 1)  # CBET5712 -> CBET 5712
    lines = []
    lines.append('<!DOCTYPE html>')
    lines.append('<html><head><meta charset="utf-8"></head><body style="font-size:15px;">')
    lines.append(f'<p style="font-weight:bold;font-size:15px;">{cbet_display}: {item.get("title", "")}</p>')
    lines.append('<hr>')

    if item.get('date'):
        lines.append(f'<p><strong>发布时间:</strong> {item["date"]}</p>')

    print(f'    获取正文: {item["url"]}')
    content = fetch_cbet_content(item['url'])
    if content:
        lines.append(f'<p style="color:#999;margin:0;">==================</p>')
        lines.append(f'<pre style="white-space:pre-wrap;margin:0;">{content}</pre>')
        lines.append(f'<p style="color:#999;margin:0;">==================</p>')

    lines.append(f'<p><strong>链接:</strong> <a href="{item["url"]}">{item["url"]}</a></p>')
    lines.append(f'<p style="color:#999;font-size:12px;">自动发送于 {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>')
    lines.append('</body></html>')
    return '\n'.join(lines)


def build_tocp_email(item):
    """构建单条TOCP通知邮件HTML"""
    lines = []
    lines.append('<!DOCTYPE html>')
    lines.append('<html><head><meta charset="utf-8"></head><body style="font-size:15px;">')

    if item.get('url'):
        print(f'    获取TOCP正文: {item["url"]}')
        content = fetch_tocp_content(item['url'])
        if content:
            lines.append(f'<pre style="white-space:pre-wrap;">{content}</pre>')

    lines.append(f'<p style="color:#999;">==================</p>')
    if item.get('url'):
        lines.append(f'<p><strong>链接:</strong> <a href="{item["url"]}">{item["url"]}</a></p>')

    lines.append(f'<p style="color:#999;font-size:12px;">自动发送于 {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>')
    lines.append('</body></html>')
    return '\n'.join(lines)


# ============================================================
# 测试邮件
# ============================================================

def test_email():
    """发送测试邮件"""
    print("\n[*] 发送测试邮件...")
    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head><body>
<h2>CBET & TOCP 监控测试邮件</h2>
<p>这是一封测试邮件，用于验证SMTP邮件发送配置是否正确。</p>
<p>如果您收到此邮件，说明邮件发送功能正常工作。</p>
<hr>
<p style="color:#999;font-size:12px;">发送时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
</body></html>"""
    return send_email("[测试] CBET & TOCP 监控系统测试邮件", body)


# ============================================================
# 主逻辑
# ============================================================

def check_cbet(dry_run=False):
    """检查CBET新公告"""
    print(f"\n{'='*60}")
    print(f"[*] 检查 CBET 公告 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    try:
        html = fetch_url(CBET_URL)
        print(f"  [+] 成功获取 RecentCBETs.html ({len(html)} bytes)")

        current_items = parse_cbet_items(html)
        print(f"  [*] 解析到 {len(current_items)} 条CBET条目")

        if dry_run:
            print("\n  --- 当前页面条目预览 ---")
            for item_id, item in list(current_items.items())[:10]:
                print(f"    {item['id']}: {item.get('title', 'N/A')[:80]}")
                print(f"      日期: {item.get('date', 'N/A')}, URL: {item['url']}")
            if len(current_items) > 10:
                print(f"    ... 还有 {len(current_items) - 10} 条")
            return current_items

        seen = load_seen(CBET_SEEN_FILE)
        new_items = find_new_items(current_items, seen)

        if new_items:
            print(f"  [+] 发现 {len(new_items)} 条新CBET公告:")
            for item_id, item in new_items.items():
                print(f"      - {item['id']}: {item.get('title', 'N/A')[:60]}")

            # 首次运行 - 只记录不发送邮件，也不取Last-Modified
            if not seen:
                print("  [*] 首次运行，仅记录已见条目，不发送邮件")
            else:
                # 获取每个新公告的发布时间（Last-Modified）
                for item_id, item in new_items.items():
                    lm = fetch_last_modified(item['url'])
                    if lm:
                        item['date'] = lm
                        current_items[item_id]['date'] = lm
                        print(f"      发布时间: {lm}")
                for item_id, item in new_items.items():
                    email_body = build_cbet_email(item)
                    subject = f"[CBET] {item['id'].replace('CBET', 'CBET ', 1)}: {item.get('title', '')[:50]}"
                    if send_email(subject, email_body):
                        seen[item_id] = current_items[item_id]
                        new_items[item_id]['_sent'] = True

            # 首次运行：保存所有条目；后续运行：只保存已发送成功的
            if not seen:
                for item_id in new_items:
                    seen[item_id] = current_items[item_id]
                new_seen = {}
                for item_id in new_items:
                    new_seen[item_id] = current_items[item_id]
                for item_id in seen:
                    new_seen[item_id] = seen[item_id]
                seen = new_seen
                save_seen(CBET_SEEN_FILE, seen)
                print(f"  [+] 已更新本地记录")
            else:
                # 只保存发送成功的条目
                sent_count = sum(1 for v in new_items.values() if v.get('_sent'))
                if sent_count > 0:
                    new_seen = {}
                    for item_id, item in new_items.items():
                        if item.get('_sent'):
                            new_seen[item_id] = current_items[item_id]
                    for item_id in seen:
                        new_seen[item_id] = seen[item_id]
                    seen = new_seen
                    save_seen(CBET_SEEN_FILE, seen)
                    print(f"  [+] 已更新本地记录 ({sent_count} 条)")
                if sent_count < len(new_items):
                    print(f"  [!] {len(new_items) - sent_count} 条发送失败，下次将重试")
        else:
            print("  [*] 没有新的CBET公告")

        return current_items

    except Exception as e:
        print(f"  [!] CBET检查失败: {e}")
        import traceback
        traceback.print_exc()
        return {}


def check_tocp(dry_run=False):
    """检查TOCP新报告"""
    print(f"\n{'='*60}")
    print(f"[*] 检查 TOCP 报告 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    try:
        html = fetch_url(TOCP_URL)
        print(f"  [+] 成功获取 tocp.html ({len(html)} bytes)")

        current_items = parse_tocp_items(html)
        print(f"  [*] 解析到 {len(current_items)} 条TOCP条目")

        if dry_run:
            print("\n  --- 当前页面条目预览 ---")
            for item_id, item in list(current_items.items())[:10]:
                print(f"    [{item_id}]: {item.get('title', 'N/A')[:80]}")
                print(f"      日期: {item.get('date', 'N/A')}")
            if len(current_items) > 10:
                print(f"    ... 还有 {len(current_items) - 10} 条")
            return current_items

        seen = load_seen(TOCP_SEEN_FILE)
        new_items = find_new_items(current_items, seen)

        if new_items:
            print(f"  [+] 发现 {len(new_items)} 条新TOCP报告:")
            for item_id, item in new_items.items():
                print(f"      - {item.get('title', 'N/A')[:60]}")

            # 首次运行 - 只记录不发送邮件
            if not seen:
                print("  [*] 首次运行，仅记录已见条目，不发送邮件")
                for item_id in new_items:
                    seen[item_id] = current_items[item_id]
                save_seen(TOCP_SEEN_FILE, seen)
                print(f"  [+] 已更新本地记录")
            else:
                for item_id, item in new_items.items():
                    email_body = build_tocp_email(item)
                    subject = f"[TOCP] {item.get('title', '')[:50]}"
                    if send_email(subject, email_body):
                        seen[item_id] = current_items[item_id]
                        new_items[item_id]['_sent'] = True
                sent_count = sum(1 for v in new_items.values() if v.get('_sent'))
                if sent_count > 0:
                    save_seen(TOCP_SEEN_FILE, seen)
                    print(f"  [+] 已更新本地记录 ({sent_count} 条)")
                if sent_count < len(new_items):
                    print(f"  [!] {len(new_items) - sent_count} 条发送失败，下次将重试")
        else:
            print("  [*] 没有新的TOCP报告")

        return current_items

    except Exception as e:
        print(f"  [!] TOCP检查失败: {e}")
        import traceback
        traceback.print_exc()
        return {}


def main():
    dry_run = '--dry-run' in sys.argv or '--dry' in sys.argv
    test_mode = '--test' in sys.argv

    print("=" * 60)
    print("  CBET & TOCP 自动监控系统")
    print(f"  运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if dry_run:
        print("  *** 试运行模式 (不发送邮件) ***")
    print("=" * 60)

    if test_mode:
        test_email()
        return

    if dry_run:
        check_cbet(dry_run=True)
        check_tocp(dry_run=True)
    else:
        check_cbet()
        check_tocp()

    print(f"\n[*] 检查完成 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == '__main__':
    main()