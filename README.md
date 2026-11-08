# FX 生存战

一个用虚拟资金玩的外汇保证金交易模拟器。起始 ¥300,000，目标 ¥20,000,000，
可以选择 10～500 倍杠杆做多/做空，扛不住就会爆仓。

![screenshot](docs/screenshot.png)

> 纯模拟，不涉及任何真实资金，也不构成投资建议。

## 特性

- **实时行情**：服务端每秒生成一个 tick（= 1 分钟游戏时间），WebSocket 推送
- **价格锚定**：每个品种围绕均衡价做均值回复，并有 ±35% 边界，长期运行不会漂到 0 或无穷大
- **AI 玩家**：一键添加 5 种策略的 AI 对手，自动开平仓、参与排行榜和用户主页
- **真实行情**：独立真实盘账户（初始 ¥1,000,000），按 Swissquote 真实报价交易，支持休市/延迟提示
- **30 天回放**：用 Yahoo 最近 30 天的 5 分钟真实数据，自选起点和倍速重玩，独立回放账户
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
| `FX_PRICE_HALFLIFE` | `1800` | 价格回归均衡价的半衰期（tick），越小越稳 |
| `FX_PRICE_BAND` | `0.35` | 相对均衡价的最大偏离（0.35 = ±35%） |
| `FX_MAX_AI` | `8` | 最多可添加的 AI 玩家数量，设为 0 关闭 |
| `FX_REAL_START_BALANCE` | `1000000` | 真实盘独立账户初始资金 |
| `FX_REAL_POLL_SECONDS` | `10` | 真实盘行情轮询间隔（秒） |
| `FX_REAL_STALE_SECONDS` | `120` | 超过多久没有新报价就标记为休市/延迟 |
| `FX_REAL_HISTORY` | `1` | 是否用 Yahoo 1 分钟历史初始化图表 |
| `FX_REPLAY_START_BALANCE` | `1000000` | 回放账户初始资金 |
| `FX_REPLAY_DEFAULT_SPEED` | `5` | 回放默认速度（根/秒） |
| `FX_REPLAY_SPEEDS` | `1,3,5,10,30,60` | 页面可选回放速度 |
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

价格模型：对数价格围绕各品种的均衡价做均值回复，新闻库的期望偏置会被逐 tick 抵消；
极端行情下价格还会被限制在均衡价 ±`FX_PRICE_BAND` 的范围内，所以不会出现 0、负数或
“USD/JPY 跌到 1 以下”这种长期漂移。

## AI 玩家

排行榜面板右上角有「+ 添加 AI」和「清空 AI」。AI 会像玩家一样占用保证金、被强平、
进排行榜，也可以在排行榜里点开主页看交易记录。

| 策略 | 风格 | 默认杠杆档位 | 特点 |
|---|---|---|---|
| 趋势猎手 | 趋势跟随 | 中高 | 看 20/100 tick 均线，信号反转会平仓 |
| 新闻赌徒 | 消息驱动 | 高 | 新消息出现时按影响方向进场 |
| 均值猎手 | 逆势 | 中 | 短期急涨急跌后反向开仓 |
| 梭哈狂魔 | 随机高杠杆 | 最高 | 大仓位、高杠杆、频繁出手 |
| 稳健老王 | 低频低杠杆 | 最低 | 信号阈值高、仓位小、持有久 |

AI 的账户和交易是进程内存里的，名单会写入 `meta`；服务重启后 AI 会以初始资金重新入场。
AI 不写 `users` / `accounts` / `positions` / `trades` / `equity` 表，不会污染真人数据。
最多同时存在 `FX_MAX_AI` 个。

## 真实行情版块

主页面右上角的「真实行情」进入 `/real`。这是完全独立的真实盘：

- 账户独立：初始资金 `FX_REAL_START_BALANCE`（默认 ¥1,000,000），和模拟盘互不影响
- 行情来源：Swissquote 公共实时报价；失败时用 Frankfurter ECB 日线回退
- 图表历史：可选用 Yahoo Finance 的 1 分钟数据初始化，之后由轮询报价持续追加
- 轮询间隔：`FX_REAL_POLL_SECONDS`（默认 10 秒）
- 休市/延迟：报价超过 `FX_REAL_STALE_SECONDS` 没有更新时，页面显示「休市 / 数据延迟」，
  周末和网络异常时会保持最后的真实报价
- 风控规则和模拟盘一致：维持率预警、50% 强平、爆仓、独立成交与战绩统计
- 真实盘数据存在 `real_accounts` / `real_positions` / `real_trades` 表，服务重启不丢

> 真实盘仍然只是模拟资金，不涉及任何真实资金。行情来自公共接口，可能延迟、中断或
> 与真实成交价有差异，不能用于真实交易决策。

### 30 天历史回放

真实盘页面右上角的「历史回放」进入 `/replay`：

- 数据：Yahoo Finance 最近 30 天的 5 分钟 K 线，5 个货币对合成同一条时间线
- 起点：在最近 30 天内选择任意日期时间，服务端吸附到最近的 K 线
- 倍速：每秒推进 1～60 根 K 线，可暂停、继续、重新开始
- 账户：独立回放账户 ¥1,000,000，每次开始/重新开始都会重置，不影响实时真实盘和模拟盘
- 交易：回放中的开仓、平仓、保证金、维持率预警和强平规则与真实盘一致
- 页面：回放进度、当前光标时间、走势图、持仓和成交；成交时间使用历史时间
- 生命周期：回放会话只存在进程内存里，服务重启或长时间不访问后消失

> 回放数据来自 Yahoo Finance 公共接口，本机或服务器无法访问 Yahoo 时该版块会提示数据源不可用。
> 回放同样只是模拟资金，不构成投资建议。

## 技术栈

- 后端：FastAPI + Uvicorn，单进程，状态在内存
- 存储：SQLite（WAL），每 30 tick 落一次盘；真实盘账户/持仓/成交实时落库
- 推送：模拟盘 WebSocket，1 条消息/秒/连接；真实盘页面每 3 秒轮询、每 10 秒拉历史
- 前端：原生 HTML/CSS/JS，无构建步骤，Canvas 画 K 线/真实盘走势
- 真实行情：Swissquote 公共接口 + Frankfurter 日线回退 + Yahoo 1 分钟历史
- 历史回放：Yahoo 最近 30 天 5 分钟历史，会话在内存中按倍速推进

**必须单 worker**：行情引擎在进程内存里，开多 worker 会出现多套价格源和重复撮合。

## 测试

```bash
python -m unittest discover
```

覆盖价格长期漂移、极端价格回收、AI 自动交易、真实盘解析/撮合和 30 天回放。

## 部署到服务器

见 [`docs/deploy.md`](docs/deploy.md)，用的是 systemd + Nginx + Certbot。

要点：
- `uvicorn --workers 1`
- Nginx 必须配 `proxy_set_header Upgrade / Connection` 才能透传 WebSocket
- 公网必须上 HTTPS，否则密码和 token 明文传输

## 项目结构

```text
app/store.py       SQLite 访问层：用户、账户、持仓、成交、净值快照
app/engine.py      模拟盘行情引擎：价格生成、新闻、撮合、风控、AI 玩家
app/real.py        真实盘引擎：Swissquote/Frankfurter 报价、Yahoo 历史、独立账户与风控
app/real_store.py  真实盘 SQLite 表：账户、持仓、成交
app/replay.py      30 天回放引擎：Yahoo 5m 历史、回放会话、独立账户与风控
app/main.py        FastAPI 路由、WebSocket、真实盘/回放接口
static/app.js      模拟盘前端逻辑与渲染
static/real.js     真实盘前端逻辑与图表
static/real.html   真实盘页面
static/replay.js   30 天回放前端逻辑与图表
static/replay.html 30 天回放页面
static/sound.js    Web Audio 音效合成
```

## 免责声明

本项目仅用于编程学习和游戏娱乐。模拟盘价格由程序生成，与真实市场无关；真实盘引用
公共行情接口，可能存在延迟或中断，不构成任何投资建议，也不能用于真实交易决策。
请勿将其用于任何真实交易决策。

## License

[MIT](LICENSE)