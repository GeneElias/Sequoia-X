# 将两市日报、模拟账本和周复盘部署到 GitHub Actions

仓库：`GeneElias/Sequoia-X`（公开）。标准Linux托管runner在公开仓库通常免费；不使用大型runner、不购买数据。GitHub Pages已构建的是**虚构演示看板**，与后台任务是否运行独立。

## 定时工作流

`.github/workflows/research-schedule.yml` 需要位于默认分支 `master`。时间为：

| 任务 | UTC cron | 上海时间 |
|---|---|---|
| A股日报与模拟记账 | `0 9 * * 1-5` | 周一至周五17:00 |
| 美股日报与模拟记账 | `30 23 * * 1-5` | 周二至周六07:30 |
| 两市周复盘 | `0 1 * * 6` | 周六09:00 |

GitHub可能延迟甚至漏掉计划任务，不能承诺准点到分钟；无活动的公开仓库也可能被GitHub暂停定时工作流，需关注平台通知。任务错过开盘成交日不会追溯补买，沿用原模拟口径。

自动任务只有仓库变量 **`SEQUOIA_ENABLED=true`** 时才执行；没有该变量时不启动runner。手动 Run workflow 可选择 `validate`（仅解密与完整性检查、不发送）、`daily_A`、`daily_US` 或 `weekly`。

## 真实账本隐私与持久化

Actions每次使用临时机器，不能把本机SQLite当成下次一定存在，也不能依赖Actions缓存作为唯一账本。

当前实现使用 `sequoia-state` 专用分支中的 `state.fernet`，Fernet认证加密，密钥独立保存在Actions Secret。只打包paper.sqlite、delivery.sqlite、两市最新日报和近52期周复盘，以及可选的已复核估值输入。`.env`、密钥文件、行情缓存、原始工作区不会被打包。SQLite通过backup API取一致快照。远端仅有密文和说明文本，历史使用快进提交，不强推。

**该仓库公开，所以密文本身也会公开可下载。** 不能因为有加密就默认用户同意上传私人账本；初始化真实状态分支前，必须获得用户对此目的地与数据范围的明确同意。若不同意，应保留云端运行，或另选获授权的私有部署方案，不能偷偷重置组合或转用公开Artifacts绕过。

缺少分支、密钥错误、归档异常、SQLite不完整或远端被其他进程更新时均停止，不创建“新空账户”掩盖问题。归档路径白名单、解压大小限制、禁止符号链接与路径越界。发送前先保存pending记录，发送成功/未知后再保存，避免runner中断造成重复群消息；未知发送仍需人工检查，不盲目重发。

## 所需仓库配置

Settings → Secrets and variables → Actions → Secrets：

- `SEQUOIA_STATE_KEY`：初始化状态时生成并安全备份的Fernet密钥。不能每次任务随机更换，不能提交源码或发到聊天。
- `FEISHU_REPORT_WEBHOOK_URL`：已使用的同一个飞书机器人Webhook。
- `FEISHU_REPORT_SECRET`：如果机器人启用了签名，填写对应签名密钥。

Variables：

- `SEQUOIA_ENABLED`：迁移验证结束前保持缺失或false。准备接管时才设true。
- `FEISHU_DASHBOARD_URL`：可选，已上线的公开演示Pages链接。仅用于标记“公开演示”的按钮，不发布真实看板。

当前连接可读写Git仓库，但Actions设置和Secrets管理接口返回 `Resource not accessible by integration`。仓库admin状态不等于当前连接拥有Secrets接口权限；不能因而声称凭证已配置或自动任务已启用，也不应为此向用户索取PAT。可由仓库管理员在上述页面保存所需Secrets。

## 迁移顺序

1. 合并/部署源码与工作流，自动任务保持关闭。测试：`uv sync --frozen --extra reports --extra github --extra dev` 后运行测试。
2. 明确真实状态存储的隐私授权；得到允许后，备份密钥并初始化加密状态分支，保留已有持仓和发送去重记录。
3. 管理员配置上述Secrets。Actions中先手动运行 `validate`，确认解密、账本完整性成功。
4. 停止本次云环境中已启动的模拟daemon，避免云端和Actions成为两个写入者。再设 `SEQUOIA_ENABLED=true`，手动跑一个实际任务并检查飞书业务返回和新的加密状态提交。
5. 成功后才能宣称GitHub已接管每日执行。GitHub失败时不要让旧云端写过期副本；回滚也应先停一端、恢复最新远端状态后再启动另一端。

未完成这些步骤时，原云端进程继续运行，不能把“源码已上传”说成“后台已迁移完成”。

## 维护边界

- 标准任务不生成或公开真实Pages数据，只在加密状态中保存私密报告；公开站点仍是演示。
- 后续更新财报假设需基于最新远端状态，不能用过期本机账本覆盖线上状态。
- 不把Secrets放在命令行参数、Git远端URL、日志或Artifacts里；不要打开shell tracing或dump环境。
- 当前加密分支会保留密文历史，长期使用需关注仓库体积；不自动删历史或重置账本。
