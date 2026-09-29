# Skill 系统

worker（hook 唤醒 / 兜底 cron）回答问题时，按下表先读对应 skill 文档再执行。
映射关系同时写在 `bot/prompts/` 的两份 prompt 里，加新 skill 时三处同步。

## 内置 skill 一览

| 用户问什么 | 读哪个文档 | 备注 |
|---|---|---|
| 天气 / 预报 / 空气质量 | `skills/weather-query.md` | 免费 API |
| 微博 / 知乎 / B站热搜 | `skills/hot-topics.md` | 免费 API |
| 今日新闻 / 60秒早报 | `skills/daily-news-60s.md` | 免费 API |
| 翻译 / 查IP / 二维码 / 哈希 / 域名 / 密码 | `skills/utility-tools.md` | 免费 API |
| 基金行情分析 | `skills/fund-market-analysis.md` | — |
| 影视娱乐资讯 | `skills/entertainment.md` | — |
| 媒体信息查询 | `skills/media-info.md` | — |
| 百科知识 | `skills/knowledge-wiki.md` | — |
| 数据查询 | `skills/data-query.md` | — |
| 精读某个网页全文 | `skills/grok-search/scripts/fetch.js <URL> --provider direct` | 免 key |
| 发现某站点有哪些页面 | `skills/grok-search/scripts/map.js <站点> --limit 20` | 免 key |
| 全网实时搜索 | Muse 自带搜索；`grok-search` 的 `search.js` 需 `GROK_API_KEY` | 未配 key 时用自带搜索 |
| 生图 / 画图 | 先读 `skills/GPT电影感生图skill_巨物大片优化版.md` 扩写提示词，再用 Muse 图片能力出图 | outbox 加 `"image"` 字段发出 |
| 网络审批卡 / 新域名自动放行 | `skills/muse-auto-approve.md` | 运维 skill：常驻 daemon 自动 `allow_always`，见下 |

其中天气 / 热搜 / 新闻 / 小工具类调用的是免 key 的 `60s.vaneus.ccwu.cc` API，直接 curl 即可。
各文档来自 CowAgent 仓库的 skill 说明（已获开源授权，见致谢）。

## grok-search

来自 [dengyie/awesome-skills](https://github.com/dengyie/awesome-skills) 的实时联网搜索包。
安装见 [setup.md](setup.md) 第 7 步。注意：

- `fetch.js` / `map.js` 的 direct 模式免 key 可用（已验证）
- `search.js` 需要 `GROK_API_KEY` + `GROK_API_URL`；没配时 worker 用 Muse 自带搜索代替
- 不要笼统假设"所有 endpoint 都可用"——新 endpoint 先单独 curl 验证再写入 prompt

## 电影感生图 skill

`skills/GPT电影感生图skill_巨物大片优化版.md`（情绪驱动型电影感提示词）：

- 用户只说"中国龙"这类极简输入时，**不要反问**，直接按文档规则扩写成完整电影级提示词
- 默认中文输出；用户要视频提示词时按文档的视频结构输出
- 出图后把图片路径写进 outbox JSON 的 `"image"` 字段，`wxbot.py send` 会上传并发出

## muse-auto-approve（运维 skill）

`skills/muse-auto-approve.md`：微信机器人访问新域名时网页版会弹网络审批卡，
常驻 daemon（`~/workspace/muse-auto-approve`）每 10 秒轮询并自动 `allow_always`
永久放行。这是运维文档，不是问答 skill——只有当用户问起"审批""自动放行""新域名"
相关问题，或报障"又有卡弹出来了"时，worker 才读它做状态检查与排障。
部署细节（保活 cron、密码规则）见文档正文。

## 加一个新 skill（三步）

1. 把 skill 说明（.md）丢进 `skills/`
2. 在 `bot/prompts/hook-worker.md` 和 `bot/prompts/sweeper-cron.md` 的映射表里加一行："用户问 X → 先读 skills/xxx.md"
3. 微信里实测一条真实问题，确认链路通

如果是需要 key 的第三方 skill：key 走 Muse 的 Secure Vault / 环境变量，**绝不**写进仓库和 prompt。
