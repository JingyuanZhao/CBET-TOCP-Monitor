# CBET & TOCP 自动监控

自动监控 [CBET 公告](http://www.cbat.eps.harvard.edu/cbet/RecentCBETs.html) 和 [TOCP 报告](http://www.cbat.eps.harvard.edu/unconf/tocp.html)，发现新内容时自动发送邮件通知。

## 功能

- 每 30 分钟自动检查 CBET 和 TOCP 页面
- 发现新公告/报告时，自动发送邮件到指定邮箱
- 邮件中包含公告正文和报告内容

## 项目结构

```
.
├── .github/workflows/monitor.yml   # GitHub Actions 定时任务配置
├── monitor.py                      # 核心监控脚本
└── README.md
```

## 部署到 GitHub Actions

### 1. 创建 GitHub 仓库

在 GitHub 上创建一个**私有仓库**（Private），将本项目代码上传。

### 2. 配置邮箱密钥

进入仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**，添加以下密钥：

| 名称 | 说明 | 示例 |
|------|------|------|
| `SMTP_USER` | 发件邮箱 | `your_email@gmail.com` |
| `SMTP_PASSWORD` | Gmail 应用专用密码 | `your_app_password` |
| `SMTP_SERVER` | SMTP 服务器 | `smtp.gmail.com` |
| `SMTP_PORT` | SMTP 端口 | `587` |
| `TO_EMAIL` | 收件邮箱 | `receiver@example.com` |

> **获取 Gmail 应用专用密码**：Google 账户 → 安全 → 两步验证 → 应用专用密码 → 生成

### 3. 启用工作流

代码推送后，GitHub Actions 会自动开始运行。可在仓库 **Actions** 标签页查看运行状态。

### 4. 手动测试

在 **Actions** → **CBET-TOCP-Monitor** → **Run workflow** 可手动触发一次运行。

## 本地运行

```bash
# 仅解析页面，不发邮件（测试解析是否正常）
python monitor.py --dry-run

# 发送测试邮件
python monitor.py --test

# 正常运行（检查并发送邮件）
python monitor.py
```

## 技术说明

- 纯 Python 标准库实现，无需额外依赖
- 支持多种 HTML 结构解析，兼容页面格式变化
- 使用 GitHub Actions Cache 持久化已见记录，避免重复通知