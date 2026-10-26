# Sequoia-X: 王者回归 | The King Returns

> A 股量化选股系统 V2 | A-Share Quantitative Stock Selection System V2

---

## 简介 | Introduction

Sequoia-X V2 是面向 A 股市场的量化选股系统，基于现代 Python 工程化标准从零重构。
系统以 OOP 架构、向量化计算和增量数据更新为核心设计原则，每日收盘后自动选股并推送至飞书群。

数据层使用 [baostock](http://baostock.com)（免费、无需注册、无限流）拉取历史及增量日 K 数据（后复权），
存储于本地 SQLite，彻底规避东方财富反爬问题。

---

## 两种运行模式

### 新增：两市研究报告与飞书卡片

```bash
uv sync --frozen --extra reports --extra dev
.venv/bin/python -m sequoia_x.reporting
```

默认生成本地 Markdown、JSON 报告和飞书卡片，不发送消息。支持美股 yfinance 主源、global-stock-data 新浪路由备源、A 股免费日线，以及显式复核输入驱动的估值检查。资料不足时标记“待核验”，不会虚构目标价。配置、估值模板和发送命令见 [研究报告与飞书推送](docs/research-reports.md)。

### 每日100股模拟买入与每周复盘（两市）

```bash
.venv/bin/python -m sequoia_x.reporting.simulation daily
.venv/bin/python -m sequoia_x.reporting.simulation weekly
```

只对合格推荐登记每只100股，下个交易日开盘模拟成交，重复推荐可加仓、同日运行去重。两市分别核算，默认持有不卖，支持定时生成日报和周复盘。模拟口径、费用假设和云环境定时限制见 [模拟组合说明](docs/paper-trading.md)。

### 交互看板

```bash
.venv/bin/python -m sequoia_x.reporting.dashboard
```

生成可离线打开的 `reports/dashboard/index.html`，包含两市观察池、收益轨迹、持仓、订单与每周复盘。默认真实数据只在本地；GitHub Pages工作流仅部署虚构演示。平台比较、权限边界与发布步骤见 [看板说明](docs/dashboard.md)。

### GitHub Actions 后台部署

已提供两市每日任务、每周复盘和认证加密状态持久化的工作流。当前迁移版默认启用定时任务，可用仓库变量 `SEQUOIA_ENABLED=false` 暂停。新部署必须先确认真实账本的隐私授权、配置Actions Secrets并验证状态，再启用业务执行，避免与云端进程重复运行。安装完整依赖使用 `uv sync --frozen --extra reports --extra github --extra dev`。步骤见 [GitHub Actions 部署](docs/github-actions.md)。

### 原有 A 股流程

```bash
python main.py               # 日常模式：8进程增量补数据 + 跑策略 + 飞书推送（2~3分钟）
python main.py --backfill     # 回填模式：全市场历史K线一次性灌入（约12分钟）
```

---

## 内置策略 | Strategies

| 策略 | 说明 |
|---|---|
| **TurtleTrade** | 海龟突破：20日新高 + 成交额过亿 + 阳线防诱多，按涨幅排序 |
| **MaVolume** | 均线+放量突破 |
| **HighTightFlag** | 高而窄的旗形整理突破 |
| **LimitUpShakeout** | 涨停洗盘回踩确认 |
| **UptrendLimitDown** | 上升趋势中的跌停反包 |
| **RpsBreakout** | 欧奈尔 RPS 相对强度突破 |

---

## 快速开始 | Quick Start

### 环境要求

- Python >= 3.10

### 1. 安装依赖

```bash
# 推荐使用 uv（快速包管理器）
uv sync

# 或者 pip
pip install .
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填写飞书 Webhook URL
```

### 3. 首次回填历史数据

```bash
python main.py --backfill
```

约 12 分钟完成 ~5200 只 A 股历史后复权日 K 数据回填。

### 4. 日常运行

```bash
python main.py
```

建议配合 crontab 每个交易日收盘后自动执行：

```cron
15 19 * * 1-5 cd /root/Sequoia-X && .venv/bin/python main.py >> log.txt 2>&1
```

---

## 目录结构 | Project Structure

```
Sequoia-X/
├── main.py                      # 入口：argparse 分发日常/回填模式
├── pyproject.toml               # 依赖声明 + ruff/pytest 配置
├── .env.example                 # 环境变量模板
├── data/                        # SQLite 数据库（运行时生成，不入 git）
├── sequoia_x/
│   ├── core/
│   │   ├── config.py            # Pydantic-settings 配置管理
│   │   └── logger.py            # rich 结构化日志
│   ├── data/
│   │   └── engine.py            # 数据引擎（baostock 回填 + 增量同步 + SQLite）
│   ├── strategy/
│   │   ├── base.py              # 策略抽象基类
│   │   ├── turtle_trade.py      # 海龟交易策略
│   │   ├── ma_volume.py         # 均线放量策略
│   │   ├── high_tight_flag.py   # 高窄旗形策略
│   │   ├── limit_up_shakeout.py # 涨停洗盘策略
│   │   ├── uptrend_limit_down.py # 上升跌停策略
│   │   └── rps_breakout.py      # RPS 突破策略
│   └── notify/
│       └── feishu.py            # 飞书 Webhook 推送
└── tests/                       # 属性测试（hypothesis）
```

---

## 数据说明

- **数据源**：[baostock](http://baostock.com)（免费、无需注册、无限流）
- **复权方式**：后复权（hfq）— 历史价格不变，适合增量存储，避免除权导致数据错乱
- **存储**：本地 SQLite（`data/sequoia_v2.db`），可直接拷贝到其他机器使用
- **日常增量**：8 进程并行通过 baostock 拉取，2~3 分钟完成全市场更新

---

## 许可证 | License

MIT
