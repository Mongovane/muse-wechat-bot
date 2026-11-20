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

## 4.5 路径替换清单（另一台机器安装必做）

本仓库是在 `/home/hatch` 这台机器上长出来的，有几处写死了这条 home 路径。
新机器上安装时先全局替换（`$HOME` 换成你自己的 home）：

```bash
grep -rn "/home/hatch" <本仓库> --include="*.md" --include="*.json" --include="*.py"
# 逐处把 /home/hatch 换成你的 $HOME，重点是下面两处：
#   - hooks/definitions/wechat-inbox.json 的 prompt（Python 解释器路径）
#   - hooks/definitions/wechat-sweeper-gate.json 的 prompt（同上）
```

- hook 脚本（`hooks/scripts/*.sh`）本身已是 `$HOME` 相对路径，不用改；
  路径不同时也可用环境变量覆盖：`WECHAT_BOT_DIR`（默认 `~/workspace/wechat-bot`）、
  `WECHAT_BOT_PYTHON`（默认 `~/workspace/CowAgent/venv/bin/python`）。
- `wxbot.py` 用 `COWAGENT_HOME` 环境变量找 CowAgent（默认 `/home/hatch/workspace/CowAgent`）。
- `owner.json`（白名单）**不要从别处复制**：首次登录后用
  `wxbot.py approve-owner <你的 sender id>` 从你自己的微信号生成；
  白名单默认拒绝一切陌生发信人，只认你一个人。

## 5. 安装事件驱动 hook

```bash
mkdir -p ~/hooks/scripts ~/hooks/state
cp <本仓库>/hooks/scripts/wechat-inbox.sh ~/hooks/scripts/
cp <本仓库>/hooks/scripts/wechat-receiver-supervisor.sh ~/hooks/scripts/
cp <本仓库>/hooks/scripts/wechat-sweeper-gate.sh ~/hooks/scripts/
chmod +x ~/hooks/scripts/wechat-inbox.sh ~/hooks/scripts/wechat-receiver-supervisor.sh ~/hooks/scripts/wechat-sweeper-gate.sh
```

**发送提速（watch-outbox，常驻代发）**：hook 第 1h 节会自动拉起
`wxbot.py watch-outbox`（0.2 秒轮询 `outbox/`，普通回复文件一出现就代发，
worker 不再调 `finish`，省一次模型动作约 3.5 秒）。不用单独安装，装好 hook
后它自己起来。验证它在跑：`pgrep -f "wxbot.py watch-outbox"` 应有 1 个进程，
且 `~/workspace/wechat-bot/watch-outbox.json` 的心跳在 15 秒内、`sent` 计数随
回复增长。死了 hook 会在 5 秒内重拉；死了期间回复仍由 hook 第 1c 节 auto-finish
兜底，只是慢一点。`finish` 命令仍保留，只给手动收尾与兜底用。

脚本每轮做几件事（纯 bash，不起 agent）：

1. **看门狗**：接收器不在就重启；`receiver.log` 尾部有 `SESSION_EXPIRED` 则唤醒通知用户重扫（用 `~/hooks/state/` 下的 flag 防重复打扰）
2. **收件箱监听**：`inbox/*.json` 非空则 wake，payload 带上 msg_ids
3. **常驻 supervisor（§1f）**：worker 回完写的 `warm-request.json` 会在这里被消费，
   拉起 hook 所有的常驻 warm-wait（24 小时会话、认领满 100 条轮换）；warm 认领后写
   `warm-claim-<id>.json`，下一轮 hook 读它唤醒 worker
4. **接收器 supervisor 兜底（§1g）**：`wechat-receiver-supervisor.sh`（30 秒一轮）保证
   接收器在 hook 轮询本身停摆时也能被重启

然后注册 hook（`poll_interval_secs: 5`，delivery 保持默认当前对话）：

- id：`wechat-inbox`
- script：`~/hooks/scripts/wechat-inbox.sh`
- prompt：以 `<本仓库>/hooks/definitions/wechat-inbox.json` 的 `prompt` 字段为准
  （`bot/prompts/hook-worker.md` 是同一份指令的可读版，两处内容一致；改动只改 JSON，md 同步）

注册后先 `dry_run` 两次：一次空收件箱（应 silent），一次丢个测试文件进 inbox（应 wake 且 payload 带文件名），都对再 `enable`，最后删掉测试文件。

## 6. 创建兜底门控 hook（30 秒）

正常消息都由 hook 处理，兜底只捡漏（hook worker 崩溃导致的消息卡住）。
兜底不是 agent cron（cron 每轮必起 agent，空轮也烧额度），而是 hook 门控：

- id：`wechat-sweeper-gate`，interval `30s`
- script：`<本仓库>/hooks/scripts/wechat-sweeper-gate.sh`
- prompt：以 `<本仓库>/hooks/definitions/wechat-sweeper-gate.json` 的 `prompt` 字段为准
  （`bot/prompts/sweeper-gate-worker.md` 是同一份指令的可读版）

它会：门控脚本在 bash 内做看门狗（接收器死/卡死直接重启，不唤醒 agent）→
只在超过 1 分钟没人认领的 inbox 文件、processing 超时（无 `.alive` 2 分钟 /
有 `.alive` 10 分钟）、登录态过期时唤醒 worker。认领同样用 `mv` 原子操作，
和 hook 不会 double。

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

先跑自带的检查（都在机器人目录里，不发消息）：

```bash
cd ~/workspace/wechat-bot
~/workspace/CowAgent/venv/bin/python wxbot.py preflight   # 环境/登录态/队列自检
~/workspace/CowAgent/venv/bin/python wxbot.py smoke-test   # 合成冒烟：认领→作答→收尾
cd <本仓库>/bot && python -m pytest tests/ -q            # 全量回归测试
```

再让用户从微信发一条"你好"：

1. 5 秒内 hook 应 wake（`hooks.logs` 可查）
2. worker 认领 → Muse 作答 → outbox → send → 微信收到回复
3. `inbox/` 文件被移到 `done/`

查发送耗时：`~/workspace/goals/muse/hidden_files/wechat-latency.jsonl` 里
`reply_to_sent_s` 应在 2 秒上下（watch-outbox 代发；若回到 5 秒上下，
说明 watcher 没起来，worker 又在调 `finish`，按第 5 节查 `watch-outbox.json`）。

再测 skill：发"北京明天天气怎么样"（调天气 API）、"画一张中国龙"（电影感 skill 扩写 + 生图发出）。

常驻验证（三条消息，第二、三条由 warm 认领才算过）：第一条现在发，第二条在
第一条回完后 2 分钟内发，第三条在第二条回完后隔 10 分钟以上再发。
查认领方：`warm-resident.json` 的 claims 计数增长、且唤醒 payload 来自
`warm-claim-*.json` 而非 hook 直接认领。常驻会话 24 小时，到期后第一条消息
冷启动开新周期，属正常。

## 9. （可选）免去每条发送确认

微信发消息是敏感操作，平台默认每次发送都要用户点确认。如用户觉得烦：

- 下次确认卡弹出时找"始终允许 / 长期允许此任务"选项（卡片样式对 agent 不可见，只能指方向）
- 长期许可只覆盖被授权的那一个任务，随时在网页端「设置 → 权限」撤销

**不要**试图绕过审批（比如把发送藏进无需审批的链路里）——这是安全红线。
