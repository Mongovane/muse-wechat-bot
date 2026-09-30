# 搭建步骤

> 约定：`~` = 用户 home；`$BOT` = 机器人目录（默认 `~/workspace/wechat-bot`）；
> `COWAGENT_HOME` 默认为 `/home/hatch/workspace/CowAgent`，按实际修改。

## 0. 前置条件

- 一台长期在线的 Linux/macOS 机器（Muse 沙盒 / VPS 都行，`$HOME` 持久化）
- Python 3.10+，Node.js 18.17+（grok-search 用）
- 一个个人微信号（扫码登录用）
- 当前对话的 Muse 支持 hooks / cron（事件驱动和兜底任务用）

## 1. 安装 CowAgent（只取协议层）

```bash
git clone https://github.com/vaneOvO/CowAgent.git ~/workspace/CowAgent
cd ~/workspace/CowAgent
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

我们只用它的两样东西：

- `channel/weixin/weixin_api.py`（微信 iLink 协议实现）
- `venv/`（Python 环境）

**不用 CowAgent 当 Agent**，它的模型层、config.json 里的 Key 都不需要。

## 2. 部署 wxbot.py

```bash
mkdir -p ~/workspace/wechat-bot
cp <本仓库>/bot/wxbot.py ~/workspace/wechat-bot/
# 如 CowAgent 不在默认路径：
export COWAGENT_HOME=/path/to/CowAgent
```

`wxbot.py` 的四个子命令：

| 命令 | 作用 |
|---|---|
| `qr` | 取登录二维码 → `qr.json` / `qr.png` |
| `wait_login` | 等扫码确认 → 落盘 `credentials.json`（600 权限） |
| `receive` | 常驻：长轮询收消息 → `inbox/<msg_id>.json` |
| `send` | 把 `outbox/*.json` 发出去 → `sent/`（文字自动分段，`"image"` 字段发图片） |

## 3. 扫码登录

```bash
cd ~/workspace/wechat-bot
~/workspace/CowAgent/venv/bin/python wxbot.py qr        # 生成二维码
~/workspace/CowAgent/venv/bin/python wxbot.py wait_login # 用户扫码后执行
```

- 把 `qr.png` 发给用户扫码（微信扫一扫确认登录）
- 成功后生成 `credentials.json`（`baseurl` / `bot_token` / `ilink_bot_id` / `ilink_user_id`）
- **只向用户报字段名，不报值**；文件权限 600，绝不进 git
- 登录态长期有效：**只有**微信服务端作废 token（`SESSION_EXPIRED`）才需重扫，其他掉线直接复用旧凭证重启

## 4. 启动接收器

```bash
cd ~/workspace/wechat-bot
setsid nohup ~/workspace/CowAgent/venv/bin/python wxbot.py receive \
  > receiver.log 2>&1 < /dev/null &
```

`setsid` 让它脱离终端会话，ssh 断开也不死。用 `pgrep -f "wxbot.py receive"` 验证存活。

## 5. 安装事件驱动 hook

```bash
mkdir -p ~/hooks/scripts ~/hooks/state
cp <本仓库>/bot/hook/wechat-inbox.sh ~/hooks/scripts/
chmod +x ~/hooks/scripts/wechat-inbox.sh
```

脚本每轮做两件事（纯 bash，不起 agent）：

1. **看门狗**：接收器不在就重启；`receiver.log` 尾部有 `SESSION_EXPIRED` 则唤醒通知用户重扫（用 `~/hooks/state/` 下的 flag 防重复打扰）
2. **收件箱监听**：`inbox/*.json` 非空则 wake，payload 带上 msg_ids

然后注册 hook（`poll_interval_secs: 10`，delivery 保持默认当前对话）：

- id：`wechat-inbox`
- script：`~/hooks/scripts/wechat-inbox.sh`
- prompt：全文复制 `<本仓库>/bot/prompts/hook-worker.md`

注册后先 `dry_run` 两次：一次空收件箱（应 silent），一次丢个测试文件进 inbox（应 wake 且 payload 带文件名），都对再 `enable`，最后删掉测试文件。

## 6. 创建兜底 cron（5 分钟）

正常消息都由 hook 处理，cron 只捡漏（hook worker 崩溃导致的消息卡住）。新建定时任务：

- id：`wechat-bot-responder`，interval `5m`
- body：全文复制 `<本仓库>/bot/prompts/sweeper-cron.md`

它会：备用看门狗 → 处理超过 3 分钟没人认领的 inbox 文件 → 把超过 10 分钟的 `processing/` 文件打回 inbox。认领同样用 `mv` 原子操作，和 hook 不会 double。

## 7. 装 skills

```bash
cp <本仓库>/skills/*.md ~/workspace/wechat-bot/skills/
```

grok-search（精读网页 / 站点发现）按需装：

```bash
cd /tmp && git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/dengyie/awesome-skills.git
cd awesome-skills && git sparse-checkout set grok-search
cp -r grok-search/scripts grok-search/SKILL.md grok-search/package.json \
  ~/workspace/wechat-bot/skills/grok-search/
cd ~/workspace/wechat-bot/skills/grok-search && npm install
```

- `fetch.js --provider direct` / `map.js`：免 key，可直接用
- `search.js`：需要 `GROK_API_KEY`（没有也能跑，Muse 自带搜索顶上）

skill 映射关系写在两份 prompt 里，加新 skill 时同步加一行。详见 [skills.md](skills.md)。

## 8. 端到端测试

让用户从微信发一条"你好"：

1. 10 秒内 hook 应 wake（`hooks.logs` 可查）
2. worker 认领 → Muse 作答 → outbox → send → 微信收到回复
3. `inbox/` 文件被移到 `done/`

再测 skill：发"北京明天天气怎么样"（调天气 API）、"画一张中国龙"（电影感 skill 扩写 + 生图发出）。

## 9. （可选）免去每条发送确认

微信发消息是敏感操作，平台默认每次发送都要用户点确认。如用户觉得烦：

- 下次确认卡弹出时找"始终允许 / 长期允许此任务"选项（卡片样式对 agent 不可见，只能指方向）
- 长期许可只覆盖被授权的那一个任务，随时在网页端「设置 → 权限」撤销

**不要**试图绕过审批（比如把发送藏进无需审批的链路里）——这是安全红线。

## 10. （可选）网络审批自动放行

微信机器人访问新域名时，muse.ai 网页版会弹网络审批卡。`muse-auto-approve`
daemon 可自动以 `allow_always` 永久放行，免去手动点卡。部署方法见
`skills/muse-auto-approve.md` 的"部署（新 Muse 上从零安装）"一节：
稀疏克隆代码 → `npm install` → `--smoke` 登录一次（要该 Muse 账号的
muse.ai 密码，单次使用不写盘）→ 起 daemon → 建保活 cron → 实战验证。
验证：让微信机器人访问一个全新域名，`log/daemon-log.ndjson` 出现
`pending_found` → `decided (allow_always)`，网页版全程无卡。
