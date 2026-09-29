# muse-wechat-bot

个人微信机器人：**Muse 本人**在微信里亲自回消息。复用 [CowAgent](https://github.com/vaneOvO/CowAgent) 的微信 iLink 协议做收发，回答、记忆、skill 调度全部由 Muse 完成——没有中间模型层，不需要任何模型 API Key。

## 特性

- 📩 **微信收发**：扫码登录个人微信，实时接收消息、自动回复（含图片）
- 🧠 **Muse 亲自作答**：每条消息都由 Muse 回复，长期记忆保留在 `$HOME`
- ⚡ **事件驱动**：收件箱一来新消息 10 秒内唤醒回答，不用傻等轮询
- 🛠️ **Skill 系统**：天气、热搜、每日新闻、小工具、基金、百科、电影感生图提示词……按场景自动调用
- 🔍 **联网搜索**：集成 `grok-search`，可精读网页全文、发现站点页面
- 🛡️ **防重复**：`mv` 原子认领，hook 和兜底任务同时跑也不会 double 回复

## 架构速览

```
微信 ──iLink 长轮询──▶ wxbot.py receive ──▶ inbox/<msg_id>.json
                                                │
                    wechat-inbox hook (10s) ────┘ 发现新文件 → 唤醒 worker
                                                │
                          worker: 认领(mv→processing/) → Muse 作答
                                → outbox/<msg_id>.json → wxbot.py send → 微信
                                → processing/ → done/
```

看门狗下沉在 hook 的 bash 脚本里：接收器挂了直接拉起；只有微信服务端作废登录态（`SESSION_EXPIRED`）时才需要重新扫码。另有一个 5 分钟的兜底 cron，只捡漏。

## 快速开始

完整搭建步骤见 [docs/setup.md](docs/setup.md)，核心就 6 步：

1. 克隆 CowAgent，建 venv 装依赖
2. 复制 `bot/wxbot.py`，`qr` → 扫码 → `wait_login` 登录
3. 启动 `receive` 接收器
4. 安装 `bot/hook/wechat-inbox.sh` 并创建 hook（worker 指令见 `bot/prompts/hook-worker.md`）
5. 创建 5 分钟兜底 cron（指令见 `bot/prompts/sweeper-cron.md`）
6. 复制 `skills/`，微信里发条消息测试

## 仓库结构

```
├── SKILL.md                # 给另一个 Muse 对话用的 skill 说明（直接加载即可）
├── bot/
│   ├── wxbot.py            # 收发主程序：qr / wait_login / receive / send
│   ├── hook/
│   │   └── wechat-inbox.sh # 事件驱动脚本：看门狗 + 收件箱监听
│   └── prompts/
│       ├── hook-worker.md  # hook 唤醒的 worker 指令
│       └── sweeper-cron.md # 兜底 cron 指令
├── docs/
│   ├── setup.md            # 一步步搭建
│   ├── architecture.md     # 架构与消息流详解
│   ├── skills.md           # skill 接入与扩展
│   └── troubleshooting.md  # 排障
└── skills/                 # skill 文档（天气/热搜/新闻/小工具/基金/生图…）
```

## 给另一个 Muse 对话使用

把本仓库的 `SKILL.md` 发给另一个对话里的 Muse（或让它 clone 本仓库），说"按这个 skill 帮我搭微信机器人"，它会照着 `docs/setup.md` 一步步来。

## 安全须知

- `credentials.json`（微信登录凭证）**绝不进仓库**，权限 600，仅存本地 `$HOME`
- 发送微信消息是敏感操作：Muse 平台默认每次发送都要你点确认；可按 [docs/setup.md](docs/setup.md) 里的说明给定时任务授予长期许可（随时可撤销）
- 本仓库不含任何 token / key / 密码

## 致谢

- 微信 iLink 协议实现：[CowAgent](https://github.com/vaneOvO/CowAgent)（`channel/weixin/weixin_api.py`）
- 联网搜索工具包：[awesome-skills](https://github.com/dengyie/awesome-skills) 的 `grok-search`
- 作答与调度：Muse（Meta）

## License

MIT，见 [LICENSE](LICENSE)。
