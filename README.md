# FX 生存战

一个用虚拟资金玩的外汇保证金交易模拟器。起始 ¥300,000，目标 ¥20,000,000，
可以选择 10～500 倍杠杆做多/做空，扛不住就会爆仓。

![screenshot](docs/screenshot.png)

> 纯模拟，不涉及任何真实资金，也不构成投资建议。

## 特性

- **实时行情**：服务端每秒生成一个 tick（= 1 分钟游戏时间），WebSocket 推送
- **K 线图**：Canvas 绘制，周期可配（默认 5 分钟一根）
- **杠杆自选**：10 / 50 / 100 / 200 / 500 倍，下单前提示「约可承受反向波动 X%」
- **三档风控**：维持率 130% 预警 → 50% 强制平仓 → 净值归零爆仓
- **新闻驱动**：22 条模拟消息，覆盖央行、通胀、避险、乌龙指与黑天鹅
- **补仓机制**：预警时自动算出建议追加金额，补仓计入累计投入，不刷榜
- **战绩统计**：成功率、盈亏比、连胜连亏、净值曲线
- **用户主页**：`/#/u/用户名` 可直接分享
- **排行榜**：按净值排序，5 秒刷新
- **音效**：Web Audio 实时合成，无音频文件依赖

## 快速开始

需要 Python 3.10+。

### Windows

```bat
git clone https://github.com/<你的用户名>/fx-survivor.git
cd fx-survivor
run.bat
```

打开 <http://127.0.0.1:8000>。

### macOS / Linux

```bash
git clone https://github.com/<你的用户名>/fx-survivor.git
cd fx-survivor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 配置

全部通过环境变量，改完重启生效。

| 变量 | 默认 | 说明 |
|---|---|---|
| `FX_DB` | `data/fxsim.db` | SQLite 路径 |
| `FX_TICK_SECONDS` | `1` | 每 tick 的真实秒数 |
| `FX_START_BALANCE` | `300000` | 初始资金 |
| `FX_GOAL` | `20000000` | 通关目标（进度条） |
| `FX_LEVERAGES` | `10,50,100,200,500` | 可选杠杆倍数 |
| `FX_MIN_AMOUNT` | `1000` | 单笔最小投入 |
| `FX_MAX_AMOUNT` | `10000000` | 单笔最大投入 |
| `FX_CANDLE_TICKS` | `5` | 每根 K 线包含几个 tick |
| `FX_STOPOUT` | `50` | 爆仓线（维持率 %） |
| `FX_WARN` | `130` | 预警线（维持率 %） |
| `FX_TOPUP_TARGET` | `160` | 补仓目标维持率 |
| `FX_MAX_DEPOSIT` | `1000000` | 累计补仓上限 |
| `FX_NEWS` | `1` | 是否启用新闻驱动 |
| `FX_NEWS_MIN` / `FX_NEWS_MAX` | `90` / `240` | 两条新闻间隔的 tick 范围 |
| `FX_NEWS_GAP` | `0.35` | 新闻瞬间走完的比例 |

想快速看爆仓：

```bat
set FX_LEVERAGES=500
set FX_MAX_DEPOSIT=0
run.bat
```

## 数值公式

```text
名义价值 = 投入金额 × 杠杆倍数
浮动盈亏 = (现价 − 开仓价) ÷ 开仓价 × 名义价值 × 方向
占用保证金 = 投入金额
维持率   = 净值 ÷ 占用保证金 × 100%
可承受波动 = (净值 − 占用保证金 × 爆仓线) ÷ 名义价值 × 100%
```

起始 30 万全仓时的可承受反向波动：

| 杠杆 | 名义价值 | 可承受波动 |
|---|---|---|
| 10× | 300 万 | 5.00% |
| 50× | 1500 万 | 1.00% |
| 100× | 3000 万 | 0.50% |
| 500× | 1.5 亿 | 0.10% |

## 技术栈

- 后端：FastAPI + Uvicorn，单进程，状态在内存
- 存储：SQLite（WAL），每 30 tick 落一次盘
- 推送：WebSocket，1 条消息/秒/连接
- 前端：原生 HTML/CSS/JS，无构建步骤，Canvas 画 K 线

**必须单 worker**：行情引擎在进程内存里，开多 worker 会出现多套价格源和重复撮合。

## 部署到服务器

见 [`docs/deploy.md`](docs/deploy.md)，用的是 systemd + Nginx + Certbot。

要点：
- `uvicorn --workers 1`
- Nginx 必须配 `proxy_set_header Upgrade / Connection` 才能透传 WebSocket
- 公网必须上 HTTPS，否则密码和 token 明文传输

## 项目结构

```text
app/store.py     SQLite 访问层：用户、账户、持仓、成交、净值快照
app/engine.py    行情引擎：价格生成、新闻、撮合、风控
app/main.py      FastAPI 路由与 WebSocket
static/app.js    前端逻辑与渲染
static/sound.js  Web Audio 音效合成
```

## 免责声明

本项目仅用于编程学习和游戏娱乐。所有价格均为程序生成，与真实市场无关。
请勿将其用于任何真实交易决策。

## License

[MIT](LICENSE)