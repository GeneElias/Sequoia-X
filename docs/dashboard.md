# 两市研究与模拟组合看板

公开站点：https://geneelias.github.io/Sequoia-X/

## 当前公开内容

已删除所有虚构观察项、示例持仓、模拟收益曲线和演示标识。公开页面显示空状态，不把尚未公开的数据写成零收益或真实推荐。

真实日报、持仓和模拟账本仍通过飞书及加密状态分支保存。用户此前允许的是公开保存密文，并未授权把明文持仓发布到Pages；删除示例不等于取得明文公开授权。

## 本地真实快照

```bash
.venv/bin/python -m sequoia_x.reporting.dashboard
```

只读当前本地报告/账本，输出 reports/dashboard/index.html。GitHub已接管后，旧云端副本可能过期，不把它当成最新生产账本。

## 无示例的公开页面

```bash
python -m sequoia_x.reporting.dashboard --public-empty --output _site
```

此模式不读取真实报告、数据库、.env或密钥，不生成虚构记录。两市切换、搜索、排序和图表控件保留，但没有已公开数据时均显示空状态。页面没有CDN或外部API请求。

人工发布工作流是 `.github/workflows/dashboard-pages.yml`，固定构建公开空页面。原 dashboard-demo.yml、--demo 参数和虚构数据生成函数已移除，避免重新发布旧样例。

当前站点使用 gh-pages 分支根目录的预构建静态文件。发布时快进更新此分支即可；不要强推覆盖站点历史，不要上传reports/或私密账本。以GitHub Pages构建状态和实际HTTP页面内容验证更新。

## 飞书与后台

飞书消息入口名称为“打开看板”，不再含演示字样。已发送的历史消息不会被这次网页更新自动修改。

选股、模拟记账、周复盘由GitHub Actions运行，Pages只是静态页面，不负责计算。真实日报和账本不会因打开网页而公开，也不因清理网页示例而被删除。

若后续需要在公网看板展示真实股票或收益，须另行明确公开字段和数据范围；若只给群成员看，可用飞书原生看板或有访问控制的托管。
