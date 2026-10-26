# 零预算研究报告与飞书推送

从仓库根目录执行。报告入口独立于原 `main.py`，无需配置 Webhook 即可预览。原日常选股入口保持兼容，仍是原 A 股流程。

## 安装与生成

```bash
uv sync --frozen --extra reports --extra dev
.venv/bin/python -m sequoia_x.reporting
```

默认在 `reports/<UTC时间戳>/` 输出：

- `report.md`：可阅读的完整报告。
- `report.json`：所有观察项、原始指标、来源、异常和估值假设结果。
- `cards.json`：可直接发送的飞书自定义机器人交互卡片数组，按市场和字节数分页。

默认两市各 25 支**静态研究观察池**，每市展示最多 10 支，不能称为全市场前十或指数历史成分股。JSON 包含完整观察池与所有有效股票。没有通过估值和量价检查时，不凑买入名单。

```bash
# 自定义池；至少2支有效股票才计算池内RPS
.venv/bin/python -m sequoia_x.reporting --market US --us-symbols MSFT AAPL NVDA
.venv/bin/python -m sequoia_x.reporting --market A --a-symbols 600519 000333 600036
```

报告只使用最后完整交易日，收盘后额外保留30分钟数据发布缓冲；美股处理假日、夏令时和半日市，A股使用上海交易日历。最近121个交易日必须连续且最新，停牌、缺失、过期或复权口径不明的股票不能参与排名。池缺任何一支时，不输出RPS及买入候选，保留已有观察项并显示异常。

美股 yfinance 主源，global-stock-data 的新浪日线接口路由为备源。新浪复权口径未验证，当前只作异常诊断，**不会作为合格数据参加量价排名**。A股优先 AKShare/东方财富，失败再用 yfinance；后者没有可信原始成交额时，不计算“成交额过亿”的海龟确认。

Yahoo OHLC 统一乘 `Adj Close / Close`，未复权收盘价单独保留用于当前估值。Yahoo成交量使用供应商报告值，不能当作逐笔成交或保证实时全市场feed。量比用此前20日均量；MaVolume确认使用含当日的20日均量以匹配原策略。A股涨跌停规则不套用于美股。

仅缓存已验证最新、复权口径一致的日线到 `reports/_state/cache/`。`--refresh` 可手动刷新；程序不循环请求、绕过平台代理或轮换身份以回避限流。每次运行默认只生成本地文件。

## 估值 double check

本次实现 AI Berkshire 方法的可复算部分：FCFF DCF、悲观/基准/乐观现值、反向增长率、资本成本上调2个百分点压力测试。**不宣称已自动运行完整四大师研究，也不自动生成增长率、治理结论或护城河评分。**

```bash
cp examples/valuations.template.json valuations.local.json
# 填写并核实财报与假设后，再标记 reviewed=true；不要直接照抄旧报告预设
.venv/bin/python -m sequoia_x.reporting --valuations valuations.local.json
```

模板没有虚构数值，未填写时估值为“待核验”。请勿把私人分析输入误提交到仓库。

输入以 `US:MSFT` 或 `A:600519` 为键：

- `fcff`、`net_debt`、`shares` 为**同一币种基本单位**，股数为股，不是“亿股”；用十进制字符串保存。FCFF不是不加处理的经营现金流减资本支出，应解释利息、税、非经营资产与正常化调整。
- `discount_rate` 使用同币种WACC，`terminal_growth` 是预测期后的永续增长；要求 `r-g >= 0.05`。`growth` 为三个情景的预测期FCFF年增长率。
- `financial_as_of` 必须在行情截止日之前450天内，`reviewed_at` 在此前90天内且不能是未来。两个日期检查不替代“是否已经发布更新财报”的人工检查。
- `sources` 至少两种上游；Yahoo的不同客户端不算两源。系统规范化 Yahoo/SEC/东方财富域名，但不自动判断所有供应商间的转录关系。
- `source_checks` 必须核对FCFF、股数和净债务，primary必须与模型输入完全一致；差异超过1%记录，超过5%阻止估值通过。两来源都为零时差异为零。
- `reviewed=true` 是**用户对输入的复核声明**，程序不会仅凭两个URL就声称已阅读、独立核验原文。`quality_status=pass` 需有事实依据；`fail` 直接剔除，未知则待核验。
- 只支持适合FCFF的普通非金融公司。银行、保险、亏损成长公司等不能强套该模型；当前应保留待核验，不填人为乐观输入。
- `required_margin` 是显式研究假设，不是保证收益的阈值。安全边际=`1-当前价/基准现值`，悲观估值不是最大损失。长期模型也不能单独替代质量和风险判断。

报告将质量原因、财报日期、三情景现值、安全边际、观察价和反向增长率保存为结构化输出。若缺少材料，继续生成报告但明确显示“待核验”，不会发出已通过估值的买入提示。

## 飞书配置与发送

在飞书目标群添加“自定义机器人”，复制其 Webhook 到**本地 `.env`**（文件已被Git忽略），不要发送到聊天或写进脚本：

```dotenv
FEISHU_REPORT_WEBHOOK_URL=<目标机器人的真实HTTPS地址>
# 如机器人启用签名校验，再填写该机器人的签名密钥
FEISHU_REPORT_SECRET=<可选>
```

也支持原有 `FEISHU_WEBHOOK_URL` 作为 fallback。只允许 `open.feishu.cn` 和 `open.larksuite.com` 的HTTPS机器人路径。不需要交易账户或资金；飞书及数据接口的实际使用权限由其平台条款决定。

```bash
# 推荐：查看生成的 report.md 后，发送同一份已生成的卡片
.venv/bin/python -m sequoia_x.reporting --send-existing reports/<时间戳>/cards.json --send

# 或取数、生成并直接发送（这会向群发消息）
.venv/bin/python -m sequoia_x.reporting --send
```

每张卡片要求 HTTP 200 且飞书 `code=0` 才记为成功。签名可选，错误消息不输出Webhook、响应正文或原始网络异常。`reports/_state/delivery.sqlite` 按机器人地址哈希和卡片内容去重；同一天同内容只发一次。

超时、无法解析响应或进程中断可能代表“已发但未确认”，记录为unknown/pending并阻止自动重发。先检查群消息，确需重发才使用 `--force-resend`。部分成功时再运行会跳过已成功卡片。去重依赖本地状态保留；删除状态或更改 `--output` 相当于使用新账本。

目前未安装定时任务。确认实际发送与时区行为后，可由调用方在交易日收盘并留出发布缓冲后运行；模型参数不应在无人核验时自动标为已复核。

## 验证

```bash
.venv/bin/python -m pytest -q tests/test_reporting.py
```

该套离线测试覆盖交易日历、盘中行过滤、行情完整性、主备失败、估值缺失/来源冲突/质量否决、反向DCF、卡片大小、业务返回码、签名、去重和超时。原 `tests/test_feishu.py` 仍会联网查股票名，运行原套件时需要单独隔离该外部查询；新测试不发送真实通知。
