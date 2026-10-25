# 部署到 Ubuntu 服务器

## 目录

1. [架构与前提](#1-架构与前提)
2. [准备服务器](#2-准备服务器)
3. [获取代码](#3-获取代码)
4. [Python 环境](#4-python-环境)
5. [配置文件](#5-配置文件)
6. [systemd 服务](#6-systemd-服务)
7. [Nginx 反向代理](#7-nginx-反向代理)
8. [HTTPS](#8-https)
9. [防火墙](#9-防火墙)
10. [验证](#10-验证)
11. [备份与恢复](#11-备份与恢复)
12. [升级](#12-升级)
13. [日志与监控](#13-日志与监控)
14. [故障排查](#14-故障排查)
15. [性能与容量](#15-性能与容量)
16. [安全清单](#16-安全清单)
17. [备选方案](#17-备选方案)

---

## 1. 架构与前提

```text
Browser ── HTTPS/WSS ──► Nginx ── HTTP ──► Uvicorn(127.0.0.1:8000) ──► SQLite
                                                      │
                                                  行情引擎（内存）
```

### 硬性前提

| 事项 | 要求 | 原因 |
|---|---|---|
| 进程数 | **必须单 worker** | 行情引擎和账户余额在进程内存里，多 worker 会有多套价格源、重复撮合 |
| Python | 3.10+ | 代码用了 `X \| None` 类型标注 |
| 内存 | 512 MB 起 | 单进程约 80–150 MB，行情历史很小 |
| CPU | 1 核够用 | 每秒一次 tick，几乎不吃 CPU |
| 端口 | 80 / 443 对外开放 | 8000 只监听 127.0.0.1 |
| 域名 | 一个 A 记录指向服务器 | 没有域名也能跑 HTTP，但密码明文传输，不推荐 |

### 重要限制

前端资源写的是绝对路径 `/static/...`，**必须部署在域名根目录**。不要挂到 `https://example.com/fx/` 这种子路径下，会 404。用子域名（`fx.example.com`）最省事。

### 简版部署（先跑通再优化）

如果只是想快速验证，跳过 Nginx，直接：

```bash
sudo ufw allow 8000/tcp
sudo -u fxsim /opt/fxsim/venv/bin/uvicorn app.main:app \
  --host 0.0.0.0 --port 8000 --workers 1
```

浏览器访问 `http://服务器IP:8000`。**仅限内网或临时测试**，公网上这样跑等于把密码裸奔在互联网上。正式环境请继续往下走。

---

## 2. 准备服务器

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git nginx sqlite3 curl
```

创建专用系统用户（不能登录 shell，权限最小）：

```bash
sudo adduser --system --group --home /opt/fxsim --shell /usr/sbin/nologin fxsim
sudo mkdir -p /opt/fxsim
sudo chown -R fxsim:fxsim /opt/fxsim
```

确认：

```bash
id fxsim
python3 --version    # 需要 >= 3.10
nginx -v
```

如果 `python3 --version` 低于 3.10（Ubuntu 20.04 就是 3.8），装 3.12：

```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.12 python3.12-venv
```

后面所有 `python3` 换成 `python3.12`。

---

## 3. 获取代码

```bash
sudo -u fxsim git clone https://github.com/<你的用户名>/fx-survivor.git /opt/fxsim
```

如果仓库是私有的，用 deploy key 或 token：

```bash
sudo -u fxsim git clone https://<用户名>:<token>@github.com/<用户名>/fx-survivor.git /opt/fxsim
```

不想用 Git 也可以在本地打包上传：

```bash
# 本地 PowerShell
tar -czf fxsim.tar.gz -x '__pycache__' -x 'venv' -x 'data/*.db*' -x '.git' .
scp fxsim.tar.gz root@服务器IP:/tmp/

# 服务器
sudo tar -xzf /tmp/fxsim.tar.gz -C /opt/fxsim
sudo chown -R fxsim:fxsim /opt/fxsim
```

确认关键文件都在：

```bash
ls -l /opt/fxsim
```

应该看到 `app/`、`static/`、`requirements.txt`、`run.sh`。`data/` 可能不存在，程序会自动创建。

---

## 4. Python 环境

```bash
sudo -u fxsim python3 -m venv /opt/fxsim/venv

sudo -u fxsim /opt/fxsim/venv/bin/pip install --upgrade pip
sudo -u fxsim /opt/fxsim/venv/bin/pip install -r /opt/fxsim/requirements.txt
```

确认 `__init__.py` 存在（这是新手最常漏的一步，漏了会 `ModuleNotFoundError`）：

```bash
ls -l /opt/fxsim/app/__init__.py || sudo -u fxsim touch /opt/fxsim/app/__init__.py
```

手动试跑一次，确认能启动（Ctrl+C 退出）：

```bash
cd /opt/fxsim
sudo -u fxsim /opt/fxsim/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

另开一个窗口：

```bash
curl -s http://127.0.0.1:8000/api/health
# {"ok":true,"tick":3,"players":0}
```

看到 `"ok":true` 就可以继续了。这一步通过再配 systemd，否则后面全靠猜。

---

## 5. 配置文件

所有参数通过环境变量注入。用 systemd 的 `EnvironmentFile`，不需要 `.env` 加载库。

创建 `/etc/fxsim.env`：

```bash
sudo tee /etc/fxsim.env >/dev/null <<'EOF'
# FX 生存战 运行配置
# 语法：KEY=VALUE，一行一个，不要写 export，值不需要引号
# 修改后需 sudo systemctl restart fxsim

FX_DB=/opt/fxsim/data/fxsim.db

# 行情节奏
FX_TICK_SECONDS=1
FX_CANDLE_TICKS=5

# 玩家初始条件
FX_START_BALANCE=300000
FX_GOAL=20000000
FX_LEVERAGES=10,50,100,200,500
FX_MIN_AMOUNT=1000
FX_MAX_AMOUNT=10000000

# 风控三档
FX_STOPOUT=50
FX_WARN=130
FX_TOPUP_TARGET=160
FX_MAX_DEPOSIT=1000000

# 新闻驱动
FX_NEWS=1
FX_NEWS_MIN=90
FX_NEWS_MAX=240
FX_NEWS_GAP=0.35
EOF
```

收紧权限（虽然里面没有密钥，但没必要让普通用户读）：

```bash
sudo chown root:root /etc/fxsim.env
sudo chmod 600 /etc/fxsim.env
```

### 参数释义

| 变量 | 默认 | 说明 |
|---|---|---|
| `FX_DB` | `<项目>/data/fxsim.db` | SQLite 路径，必须有写权限 |
| `FX_TICK_SECONDS` | 1 | 每 tick 的真实秒数。调到 0.2 会非常刺激 |
| `FX_CANDLE_TICKS` | 5 | 每根 K 线几个 tick |
| `FX_START_BALANCE` | 300000 | 起始资金 |
| `FX_GOAL` | 20000000 | 顶部进度条的目标 |
| `FX_LEVERAGES` | 10,50,100,200,500 | 玩家可选杠杆 |
| `FX_MIN_AMOUNT` / `FX_MAX_AMOUNT` | 1000 / 10000000 | 单笔投入上下限 |
| `FX_STOPOUT` | 50 | 维持率跌破即全平 |
| `FX_WARN` | 130 | 预警线 |
| `FX_TOPUP_TARGET` | 160 | 补仓目标维持率 |
| `FX_MAX_DEPOSIT` | 1000000 | 累计补仓上限（防止无限充值） |
| `FX_NEWS` | 1 | 设为 0 关闭新闻 |
| `FX_NEWS_MIN` / `MAX` | 90 / 240 | 新闻间隔 tick |
| `FX_NEWS_GAP` | 0.35 | 消息瞬间走完的比例 |

### 上线前值得调的两项

**想给新手更温和的体验**，把杠杆改成：

```text
FX_LEVERAGES=1,5,10,50
```

1 倍杠杆意味着「投多少最多亏多少」，最容易理解；没有低倍选项时，新手第一笔往往直接选 100 倍，三十秒就爆仓走人。

**想降低服务器负载**，把 `FX_TICK_SECONDS` 调到 2。玩家感知不到，但每秒广播量减半。

---

## 6. systemd 服务

创建 `/etc/systemd/system/fxsim.service`：

```bash
sudo tee /etc/systemd/system/fxsim.service >/dev/null <<'EOF'
[Unit]
Description=FX Survivor game server
Documentation=https://github.com/<你的用户名>/fx-survivor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=fxsim
Group=fxsim
WorkingDirectory=/opt/fxsim

EnvironmentFile=/etc/fxsim.env
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1

ExecStart=/opt/fxsim/venv/bin/uvicorn app.main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips=127.0.0.1 \
    --no-server-header

Restart=always
RestartSec=2
StartLimitBurst=10

# 安全加固
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
ReadWritePaths=/opt/fxsim/data

LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF
```

### 几个参数为什么必须这样

- `--workers 1`：**不能改**。见第 15 节。
- `PYTHONDONTWRITEBYTECODE=1`：`ProtectSystem=strict` 把整个文件系统挂成只读，Python 想写 `__pycache__` 会报 `Read-only file system`。这个变量让 Python 不写缓存。
- `ReadWritePaths=/opt/fxsim/data`：唯一允许写入的目录，SQLite 的 `-wal`、`-shm` 也在这里。
- `--forwarded-allow-ips=127.0.0.1`：让 Uvicorn 信任 Nginx 传来的 `X-Forwarded-For`，否则日志里所有请求的 IP 都是 127.0.0.1。

### 启动

```bash
sudo mkdir -p /opt/fxsim/data
sudo chown -R fxsim:fxsim /opt/fxsim/data

sudo systemctl daemon-reload
sudo systemctl enable --now fxsim
sudo systemctl status fxsim --no-pager
```

期望看到 `Active: active (running)`。

如果失败：

```bash
sudo journalctl -u fxsim -n 50 --no-pager
```

常见原因：`__init__.py` 不存在、`requirements.txt` 没装、`/opt/fxsim/data` 权限不对。

### 验证加固没有挡住写权限

```bash
sudo systemctl restart fxsim
sleep 3
ls -l /opt/fxsim/data
```

应该出现 `fxsim.db`、`fxsim.db-wal`、`fxsim.db-shm`。如果只有 `fxsim.db` 没有 wal，说明 WAL 没开或写入失败，看日志。

---

## 7. Nginx 反向代理

### 7.1 WebSocket 升级映射

写入 `/etc/nginx/conf.d/websocket.conf`：

```bash
sudo tee /etc/nginx/conf.d/websocket.conf >/dev/null <<'EOF'
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
EOF
```

这个 `map` 必须放在 `http` 上下文，Ubuntu 的 `/etc/nginx/nginx.conf` 已经把 `conf.d/*.conf` 包含在 `http` 里了。

### 7.2 限流区

写入 `/etc/nginx/conf.d/ratelimit.conf`：

```bash
sudo tee /etc/nginx/conf.d/ratelimit.conf >/dev/null <<'EOF'
# 登录/注册：每 IP 每分钟 20 次，允许短时突发 10 次
limit_req_zone $binary_remote_addr zone=fxauth:10m rate=20r/m;
# 下单：每 IP 每秒 5 次
limit_req_zone $binary_remote_addr zone=fxorder:10m rate=5r/s;
EOF
```

后端只对下单做了 0.3 秒节流，登录和注册**完全没有**防暴力破解，必须靠这一层。

### 7.3 站点配置

写入 `/etc/nginx/sites-available/fxsim`：

```bash
sudo tee /etc/nginx/sites-available/fxsim >/dev/null <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name fx.example.com;

    client_max_body_size 1m;
    server_tokens off;

    # 访问日志
    access_log /var/log/nginx/fxsim.access.log;
    error_log  /var/log/nginx/fxsim.error.log warn;

    # 静态资源：交给后端，但让浏览器缓存
    location /static/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;

        expires 1h;
        add_header Cache-Control "public";
        gzip on;
        gzip_types text/css application/javascript image/svg+xml;
    }

    # 登录 / 注册：限流
    location = /api/login {
        limit_req zone=fxauth burst=10 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /api/register {
        limit_req zone=fxauth burst=10 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 下单：限流
    location = /api/orders {
        limit_req zone=fxorder burst=10 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # WebSocket：长连接，必须单独超时设置
    location = /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # 其余请求
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_read_timeout 60s;
    }
}
EOF
```

启用：

```bash
sudo ln -sf /etc/nginx/sites-available/fxsim /etc/nginx/sites-enabled/fxsim
sudo rm -f /etc/nginx/sites-enabled/default   # 避免默认站点抢请求
sudo nginx -t
sudo systemctl reload nginx
```

`nginx -t` 必须显示 `syntax is ok` 和 `test is successful` 才能 reload。

### 7.4 关键点说明

**WebSocket 必须三件套**：`proxy_http_version 1.1` + `Upgrade` 头 + `Connection` 头。缺一个浏览器就会报 `WebSocket connection failed`，然后前端每 3 秒重连一次，日志里刷屏。

**`proxy_read_timeout 3600s`** 只加在 `/ws` 上。默认 60 秒会让连接每分钟断一次，玩家看到价格卡住、重连、再卡住。其他接口保持 60 秒，避免异常连接长期占用。

**`proxy_buffering off`** 让推送立即发出。开着 buffering 的话消息会攒在 Nginx 里，行情看起来一顿一顿的。

---

## 8. HTTPS

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d fx.example.com
```

交互提示：

- 邮箱：填真实邮箱，证书过期前会提醒
- 同意条款：`A`
- 接收 EFF 邮件：`N`
- 是否把 HTTP 重定向到 HTTPS：**选 2（Redirect）**

Certbot 会自动改写 `sites-available/fxsim`，插入 443 端口和证书路径。完成后验证：

```bash
curl -sI https://fx.example.com | head -5
sudo certbot renew --dry-run
```

`renew --dry-run` 显示 `Congratulations, all simulated renewals succeeded` 就没问题。certbot 会自己装 systemd timer（`certbot.timer`），不用手动加 cron。

### 证书自动续期检查

```bash
systemctl list-timers | grep certbot
```

### 只上 HTTP 不上 HTTPS 的情况

只能用在内网。公网 HTTP 意味着：

- 密码明文传输
- token 明文传输，任何中间人可以重放
- 浏览器会显示「不安全」

---

## 9. 防火墙

```bash
sudo apt install -y ufw
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
sudo ufw status verbose
```

期望输出包含 22、80、443，且 8000 **不在**列表里（它只监听 127.0.0.1，本来就访问不到）。

如果有云厂商安全组（阿里云、腾讯云、AWS），还要在控制台放行 80/443。

---

## 10. 验证

### 10.1 健康检查

```bash
curl -s http://127.0.0.1:8000/api/health
curl -s https://fx.example.com/api/health
```

两者都应返回 `{"ok":true,"tick":N,"players":M}`。`tick` 每次刷新会变大，说明引擎在跑。

### 10.2 注册与登录

```bash
curl -s -X POST https://fx.example.com/api/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"tester","password":"secret123"}'
```

返回 `{"token":"..."}` 即成功。

```bash
TOKEN='上一步拿到的 token'

curl -s https://fx.example.com/api/state -H "Authorization: Bearer $TOKEN" | head -c 400
```

### 10.3 下单和平仓

```bash
curl -s -X POST https://fx.example.com/api/orders \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"pair":"USDJPY","side":1,"amount":10000,"leverage":10}'
```

返回 `{"ok":true,"position_id":1}`。

```bash
curl -s https://fx.example.com/api/state -H "Authorization: Bearer $TOKEN"
```

应能看到 `positions` 里有一笔、`account.margin` 变成 10000。

```bash
curl -s -X POST https://fx.example.com/api/positions/1/close \
  -H "Authorization: Bearer $TOKEN"
```

返回 `{"ok":true,"pnl":N}`。

### 10.4 WebSocket

浏览器打开 `https://fx.example.com`，F12 → Network → WS：

- 应有一个到 `/ws` 的连接，状态 **101 Switching Protocols**
- Messages 面板每秒多一条 `{"type":"tick",...}`
- 价格在跳、K 线在生长

如果状态是 400/404，看第 14 节。

### 10.5 页面与静态资源

```bash
curl -sI https://fx.example.com/ | head -3
curl -sI https://fx.example.com/static/app.js | head -3
curl -sI https://fx.example.com/static/sound.js | head -3
```

全部应为 `200`。`/static/style.css` 也是。

### 10.6 首页完整清单

打开页面，逐项确认：

| 检查项 | 期望 |
|---|---|
| 登录页 | 深色卡片，标题「生存战」是粉色 |
| 注册后进入 | 顶栏有净值/余额/可用/保证金/维持率 |
| K 线 | 有蜡烛，每秒最后一根在动 |
| 新闻 | 面板出现条目，约 1.5 分钟后 |
| 声音 | 点过页面后，新闻到达时有提示音 |
| 排行榜 | 5 秒刷新，自己那行高亮 |
| 用户主页 | 点排行榜名字能弹出战绩 |
| 交易记录 | 平仓后出现一行，成功率更新 |

---

## 11. 备份与恢复

### 11.1 备份脚本

```bash
sudo tee /opt/fxsim/backup.sh >/dev/null <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

DB=/opt/fxsim/data/fxsim.db
DIR=/opt/fxsim/data/backups
KEEP=14

mkdir -p "$DIR"
STAMP=$(date +%F_%H%M)

# .backup 是 SQLite 官方在线备份，会正确处理 WAL，不需要停服
sqlite3 "$DB" ".backup '$DIR/fx-$STAMP.db'"
gzip -f "$DIR/fx-$STAMP.db"

find "$DIR" -name 'fx-*.db.gz' -mtime +$KEEP -delete

echo "[$(date -Is)] backup ok: $DIR/fx-$STAMP.db.gz"
EOF

sudo chown fxsim:fxsim /opt/fxsim/backup.sh
sudo chmod 750 /opt/fxsim/backup.sh
```

手动跑一次确认能成功：

```bash
sudo -u fxsim /opt/fxsim/backup.sh
ls -lh /opt/fxsim/data/backups/
```

### 11.2 定时任务

```bash
sudo tee /etc/cron.d/fxsim-backup >/dev/null <<'EOF'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
30 4 * * * fxsim /opt/fxsim/backup.sh >> /opt/fxsim/data/backups/backup.log 2>&1
EOF

sudo chmod 644 /etc/cron.d/fxsim-backup
```

每天 04:30 备份，保留 14 天。

> 备份放在 `data/backups/` 里是符合 systemd 的 `ReadWritePaths` 的，不需要额外授权。

### 11.3 异地备份（强烈建议）

服务器磁盘挂了，本地备份也没了。用 `rclone` 同步到对象存储，或者最简单的：

```bash
# 本地定期拉取
rsync -avz --delete root@服务器IP:/opt/fxsim/data/backups/ ./fxsim-backups/
```

### 11.4 恢复

```bash
sudo systemctl stop fxsim

sudo -u fxsim cp /opt/fxsim/data/backups/fx-2026-10-25_0430.db.gz /tmp/restore.db.gz
sudo -u fxsim gunzip -f /tmp/restore.db.gz

sudo -u fxsim rm -f /opt/fxsim/data/fxsim.db /opt/fxsim/data/fxsim.db-wal /opt/fxsim/data/fxsim.db-shm
sudo -u fxsim cp /tmp/restore.db /opt/fxsim/data/fxsim.db
sudo chown fxsim:fxsim /opt/fxsim/data/fxsim.db

sudo systemctl start fxsim
curl -s https://fx.example.com/api/health
```

恢复后建议立刻登录一个测试账号，确认数据完整。

---

## 12. 升级

### 标准流程

```bash
# 1. 先备份
sudo -u fxsim /opt/fxsim/backup.sh

# 2. 拉代码
cd /opt/fxsim
sudo -u fxsim git pull

# 3. 依赖有变化才需要
sudo -u fxsim /opt/fxsim/venv/bin/pip install -r /opt/fxsim/requirements.txt

# 4. 重启
sudo systemctl restart fxsim

# 5. 验证
sleep 2
sudo systemctl status fxsim --no-pager
curl -s https://fx.example.com/api/health
```

### 重启会丢什么

| 数据 | 是否丢失 |
|---|---|
| 用户、账户余额、累计投入 | 不丢（实时落库） |
| 持仓 | 不丢（开仓即落库） |
| 成交记录、战绩 | 不丢 |
| 累计补仓金额 | 不丢 |
| 价格走势 | **最多回退 30 tick**（每 30 tick 存一次快照） |
| K 线历史 | 重启后用当前价重新随机生成，形态会变 |
| 新闻历史 | 最多回退 30 tick |

所以**不要在别人重仓时重启**——价格跳回 30 秒前，持仓的浮盈浮亏会突变，可能误触发强平。选在线人数少的时候操作。

### 数据库结构变更

如果新版给表加了列，`store.init_db()` 里有自动迁移（`ALTER TABLE ... ADD COLUMN`）。旧库不会被删，启动时自动补列。不需要手动改库。

### 回滚

```bash
cd /opt/fxsim
sudo -u fxsim git log --oneline -5
sudo -u fxsim git checkout <上一个提交的哈希>
sudo systemctl restart fxsim
```

如果新版动了表结构，回滚代码后旧代码读不到新列可能报错，这时用备份恢复数据库。

---

## 13. 日志与监控

### 实时日志

```bash
sudo journalctl -u fxsim -f
```

只看到正常 tick 时没有输出（代码不打印成功日志），出异常才有 traceback。

### 按时间筛

```bash
sudo journalctl -u fxsim --since "1 hour ago" --no-pager
sudo journalctl -u fxsim -p err --no-pager
```

### Nginx 日志

```bash
sudo tail -f /var/log/nginx/fxsim.access.log
sudo tail -f /var/log/nginx/fxsim.error.log
```

### 日志占用控制

```bash
sudo journalctl --disk-usage
sudo tee /etc/systemd/journald.conf.d/fxsim.conf >/dev/null <<'EOF'
[Journal]
SystemMaxUse=500M
SystemMaxFileSize=50M
EOF
sudo systemctl restart systemd-journald
```

### Uptime 监控

用 Uptime Kuma 或任意监控服务探活：

```text
URL: https://fx.example.com/api/health
期望: HTTP 200，响应体包含 "ok":true
间隔: 60 秒
```

`/api/health` 不打日志、不查库，适合高频探测。

### 会触发告警的信号

| 现象 | 含义 |
|---|---|
| `sqlite3.OperationalError: database is locked` | 有别的进程在写，或备份脚本卡住 |
| 反复 `tick failed` | 引擎逻辑异常，立刻看 traceback |
| `players` 突然变成 0 | `engine.load()` 失败，数据可能没读到 |
| 502 | 后端挂了或端口不对 |
| 504 | Nginx 超时，通常是事件循环被阻塞 |

---

## 14. 故障排查

### 502 Bad Gateway

后端没起来或端口不对。

```bash
sudo systemctl status fxsim --no-pager
sudo journalctl -u fxsim -n 80 --no-pager
ss -tlnp | grep 8000
```

`ss` 没有输出说明 Uvicorn 没监听。常见原因：`__init__.py` 缺失、依赖没装、`EnvironmentFile` 有语法错误（systemd 会静默忽略格式不对的行，导致 `FX_DB` 没生效但服务还是起来了）。

### WebSocket 连不上

表现：页面能打开，但价格不动，Console 显示 `WebSocket connection failed`。

按顺序查：

```bash
# 1. nginx 配置里 /ws 是否有三件套
sudo nginx -T | grep -A12 "location = /ws"

# 2. map 是否加载
sudo nginx -T | grep -A3 "map \$http_upgrade"

# 3. 后端是否接受
curl -sI http://127.0.0.1:8000/ws
```

如果 `curl` 返回 `426 Upgrade Required`，说明后端正常，问题在 Nginx 的 Upgrade 头。

### 页面样式全丢、按钮没反应

`/static/style.css` 或 `/static/app.js` 返回 404。

```bash
ls -l /opt/fxsim/static/
curl -sI https://fx.example.com/static/style.css
```

常见原因：上传代码时漏了 `static/`，或者 `WorkingDirectory` 不对导致 `StaticFiles` 找不到目录。`main.py` 里的 `STATIC` 是相对文件位置算的，和 `WorkingDirectory` 无关，所以基本只会是文件缺失。

### static 返回 403 Forbidden

`fxsim` 用户读不到文件：

```bash
sudo chown -R fxsim:fxsim /opt/fxsim/static
sudo chmod -R 755 /opt/fxsim/static
```

### 登录后立刻掉线

WS 返回 `4401` 会让前端清 token 并刷新。原因通常是 token 过期（30 天）或数据库被重置。

```bash
sqlite3 /opt/fxsim/data/fxsim.db "SELECT count(*) FROM sessions;"
```

如果用户反映频繁掉线，检查服务器时间是否准确：

```bash
timedatectl
sudo timedatectl set-ntp true
```

### 数据库被锁

```bash
lsof /opt/fxsim/data/fxsim.db
```

如果备份脚本正在跑，等它结束（`.backup` 是唯一会长时间持有读锁的操作）。如果同时跑了两份备份，检查 cron 是否重复配置。

正常运行时**不要**用 `cp` 直接复制数据库文件，WAL 模式下会得到不一致的副本。

### 排行榜一直是空的

```bash
sqlite3 /opt/fxsim/data/fxsim.db "SELECT id, username FROM users;"
```

有用户但排行榜空，说明 `engine.accounts` 没加载。看启动日志里有没有 `tick failed`。

### 中文用户名显示成问号

Nginx 默认没问题，通常是 SSH 终端的编码。检查：

```bash
locale
```

`LANG` 应为 `C.UTF-8` 或 `zh_CN.UTF-8`。数据库本身存的是 UTF-8，不受影响。

### Ubuntu 24.04 上 pip 报 externally-managed-environment

说明在用系统 Python。必须走 venv，确认命令里是 `/opt/fxsim/venv/bin/pip` 而不是裸 `pip`。

---

## 15. 性能与容量

### 为什么必须单 worker

`engine.accounts`、`engine.positions`、`engine.prices` 都在**进程内存**里。

如果 `--workers 2`：

- 两个进程各自跑一套行情，价格不同
- 玩家 A 在 worker1 开仓，请求落到 worker2 时看不到这笔持仓
- 平仓请求打到另一进程 → `持仓不存在`
- 强平可能被执行两次

这不是配置能解决的问题，是架构决定的。**要扩多进程，必须先把引擎拆成独立进程**，用 Redis pub/sub 或 Postgres 共享状态，那是另一个量级的改造。

### 容量估算

| 项目 | 量级 |
|---|---|
| 单连接带宽 | 约 1 KB/s（每秒一条 tick） |
| 100 人在线 | 约 100 KB/s，1 Mbps 出口够用 |
| 单进程内存 | 80–150 MB（5000 持仓以内） |
| SQLite 写入 | 每 30 tick 写一次全体账户，1000 人 = 约 33 次写/秒 |
| CPU | 1 核，实际占用通常低于 10% |

### 什么时候该换 Postgres

出现以下任一情况就考虑迁移：

- 在线人数长期超过 300
- `database is locked` 开始频繁出现
- 需要多台服务器
- 需要复杂查询（按时间段统计、跨用户排行）

`store.py` 是唯一碰 SQL 的文件，接口都是普通函数，替换连接层即可，`engine.py` 不用改。

### 索引

当前已有：

```sql
idx_positions_user, idx_orders_user, idx_trades_user
```

`trades` 的统计查询会扫该用户全部记录。单用户超过 5000 笔时，可以加一个 `user_stats` 汇总表在 `insert_trade` 里增量更新，页面就不用每次全表扫。

### 磁盘

数据量参考：100 个用户玩一周，数据库约 10–20 MB。净值快照会持续增长，代码里有 `prune_equity` 保留最近 1500 条，不会无限涨。

---

## 16. 安全清单

上线前逐项打勾：

- [ ] HTTPS 已启用，HTTP 自动跳转
- [ ] 8000 端口只监听 127.0.0.1，`ss -tlnp | grep 8000` 显示 `127.0.0.1:8000`
- [ ] `/api/login`、`/api/register` 有 `limit_req`
- [ ] `/etc/fxsim.env` 权限 600，属主 root
- [ ] `/opt/fxsim/data` 权限 750，属主 fxsim
- [ ] 数据库文件不在 Web 可访问目录（在 `/opt/fxsim/data`，不在 `static/`，安全）
- [ ] 备份脚本运行正常，且有一份在服务器之外
- [ ] `server_tokens off`（不暴露 Nginx 版本）
- [ ] UFW 只开 22 / 80 / 443
- [ ] 服务器 SSH 用密钥登录，禁用密码（可选但推荐）

### 关于密码存储

`store.py` 用 `hashlib.scrypt`（N=16384, r=8, p=1），每个用户独立随机盐。这是目前 Python 标准库里最合适的方案，不需要额外依赖。不要改成 md5/sha1，也不要加"我自己写的加密"。

### 关于 token

随机 32 字节 URL-safe，存库 30 天。目前没有"强制下线"功能；要踢人就直接删 `sessions` 表对应行：

```bash
sqlite3 /opt/fxsim/data/fxsim.db "DELETE FROM sessions WHERE user_id = 5;"
```

### 内容合规

如果公开运营，页面上保留「纯模拟交易，不涉及真实资金，也不构成投资建议」的声明。这类题材容易被误解为带单工具，声明放在登录页和 README 里能省掉很多麻烦。

**不要**使用「FX战士久留美」的原作名称、角色形象或 logo 作为站点标识，玩法和数值致敬没问题，标识必须原创。

---

## 17. 备选方案

### 17.1 Docker

项目根目录放 `Dockerfile`：

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

ENV FX_DB=/data/fxsim.db \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

VOLUME ["/data"]
EXPOSE 8000

# 必须 --workers 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

```bash
docker build -t fx-survivor .
docker run -d \
  --name fx \
  --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -v fxdata:/data \
  fx-survivor
```

后面 Nginx 配置不变，仍然 `proxy_pass http://127.0.0.1:8000`。

注意 `-p 127.0.0.1:8000:8000` 的 `127.0.0.1:` 前缀——不加的话 Docker 会把端口暴露到所有网卡，绕过防火墙。

### 17.2 反向代理放在别的机器

只要 Nginx 能访问到后端即可，但**必须**保证：

- `--forwarded-allow-ips` 改成代理机的 IP，或设为 `*`（信任所有，仅在内网安全时用）
- WebSocket 的超时和 Upgrade 头照旧

### 17.3 不用域名，只用 IP

```bash
sudo tee /etc/nginx/sites-available/fxsim >/dev/null <<'EOF'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }
}
EOF
```

能用，但没有 HTTPS，密码明文。只适合临时演示。

---

## 附录 A：一条命令装完（新服务器专用）

确认过所有步骤后，可以固化成脚本。**首次部署请手动逐条执行**，出问题才知道是哪一步。

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/<你的用户名>/fx-survivor.git"
DOMAIN="fx.example.com"
DIR=/opt/fxsim

sudo apt update
sudo apt install -y python3 python3-venv git nginx sqlite3 curl

sudo adduser --system --group --home "$DIR" --shell /usr/sbin/nologin fxsim || true
sudo mkdir -p "$DIR"
sudo chown -R fxsim:fxsim "$DIR"

sudo -u fxsim git clone "$REPO" "$DIR" 2>/dev/null || sudo -u fxsim git -C "$DIR" pull
sudo -u fxsim python3 -m venv "$DIR/venv"
sudo -u fxsim "$DIR/venv/bin/pip" install -q -U pip
sudo -u fxsim "$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"
sudo -u fxsim touch "$DIR/app/__init__.py"

sudo mkdir -p "$DIR/data"
sudo chown -R fxsim:fxsim "$DIR/data"

echo "→ 请先创建 /etc/fxsim.env 和 /etc/systemd/system/fxsim.service（见文档第 5、6 节）"
echo "→ 然后运行：sudo systemctl enable --now fxsim"
echo "→ Nginx 与证书：见文档第 7、8 节（域名 $DOMAIN）"
```

## 附录 B：部署检查表

```text
[ ] curl http://127.0.0.1:8000/api/health 返回 ok:true
[ ] ss -tlnp | grep 8000 显示 127.0.0.1:8000
[ ] systemctl status fxsim 为 active (running)
[ ] nginx -t 通过
[ ] curl -sI https://域名/ 返回 200
[ ] curl -sI https://域名/static/app.js 返回 200
[ ] 浏览器 WS 连接为 101
[ ] 注册 → 下单 → 平仓 → 交易记录出现一行
[ ] 排行榜有数据
[ ] 用户主页能打开
[ ] 新闻 2 分钟内出现一条
[ ] 备份脚本手动跑一次成功
[ ] cron 已配置
[ ] ufw 只开 22/80/443
[ ] 清空 data 后能自动建库启动（新机器首次部署时验证）
```

最后一条最容易被忽略：很多「我这儿能跑」的情况，是因为作者机器上有旧数据库。新服务器上第一次启动就是从零建库，如果能起来，说明代码真没依赖本地残留。

---

有问题先看 `sudo journalctl -u fxsim -n 100 --no-pager`，90% 的启动失败原因都在里面。