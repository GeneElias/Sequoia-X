# 飞书机器人与看板入口

无需创建付费应用，也不需要券商账户。

1. 在希望接收报告的飞书群中，打开群设置 → 群机器人/机器人 → 添加机器人 → 自定义机器人（名称可能因客户端版本略有不同）。
2. 机器人名称可设为“Sequoia-X 选股复盘”。如要求安全配置，选择关键词并填写 `Sequoia-X`，日报和周报标题都包含这个关键词。
3. 复制完整Webhook到项目本地 `.env` 的 `FEISHU_REPORT_WEBHOOK_URL`。Webhook含密钥，不要发到聊天、截图或提交Git。
4. 如果同时启用签名校验，将对应机器人签名密钥填入 `FEISHU_REPORT_SECRET`；未启用则留空。关键词和签名都启用时需同时满足。
5. Pages网站真正上线并确认可访问后，可将其无查询参数的HTTPS地址填入 `FEISHU_DASHBOARD_URL`。当前支持GitHub Pages入口，卡片按钮为“打开看板”；公开页没有虚构样例，未公开真实账本。

```dotenv
FEISHU_REPORT_WEBHOOK_URL=<你的完整机器人Webhook，仅在本地填写>
FEISHU_REPORT_SECRET=<仅启用签名时填写，否则留空>
FEISHU_DASHBOARD_URL=<实际已经上线并可访问的GitHub Pages地址，否则留空>
```

常驻模拟进程每个任务都会重新读取本地 `.env`，飞书相关本地键优先于启动时的进程值；清空本地值可禁用对应设置。修改配置不需要重建账本。

先发一份真实周复盘，检查群消息和业务返回码：

```bash
cd /workspace/Sequoia-X
.venv/bin/python -m sequoia_x.reporting.simulation weekly --send
```

同一机器人、同一内容已发过会去重。超时/结果不明时先检查群消息，不盲目重发。需要原样发送已经生成的日报卡片可用：

```bash
.venv/bin/python -m sequoia_x.reporting --send-existing reports/paper/daily/2026-10-23-A/cards.json --send
```

缺Webhook时本地报告继续生成，但不会推送。GitHub Pages只是静态页面；群里的真实日报/周报由本地任务产生，不会因为打开网页而自动更新或交易。云环境休眠后定时进程停止，不能靠Pages唤醒。

## 当前站点部署状态

2026-10-24 已上传虚构演示站点到 `GeneElias/Sequoia-X` 的新 `gh-pages` 分支，提交 `668b933417bee3eeea64ae6f6a0f39008d79b3a2`。只含 index.html、dashboard.json 和 .nojekyll，没有真实账本或密钥。

当前平台代理阻止访问 `api.github.com` 和 `geneelias.github.io`，无法由本任务启用或确认Pages上线。环境草稿已增加这两个域名，但尚未应用到运行时。

在仓库 Settings → Pages 中，将 Source 选为 **Deploy from a branch**，Branch 选 **gh-pages**，目录选 **/(root)**，保存。此分支是已构建的静态页，不需要先合并到 master，也不需要运行演示Actions工作流。以GitHub页面实际显示的站点链接和部署状态为准。如果账户/私有仓库套餐不支持Pages，不要为此订阅付费套餐；需要另选可用的免费公共演示仓库或私密飞书方案。
