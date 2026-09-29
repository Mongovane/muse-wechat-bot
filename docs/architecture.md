# 架构

## 组件

```
┌─────────┐  iLink 长轮询   ┌──────────┐  写文件   ┌─────────┐
│  微信    │◀──────────────▶│ wxbot.py │──────────▶│ inbox/  │
│ (ilink)  │  getUpdates     │ receive  │  <msg_id>.json     └────┬────┘
└─────────┘                └──────────┘                          │
       ▲                                                         │ 每 10s 扫描
       │ send_text / send_image_item                             ▼
       │                                                ┌──────────────┐
┌──────────┐  发 outbox     ┌──────────┐  wake      │ wechat-inbox │
│  微信    │◀──────────────│ wxbot.py │◀───────────│    hook      │
└─────────┘   *.json        │  send    │  worker    │  (bash脚本)  │
                            └──────────┘  作答      └──────────────┘
```

## 消息流（一句话）

微信 → 长轮询 → `inbox/<msg_id>.json` → hook 10 秒内发现并唤醒 worker →
worker 用 `mv` 原子认领到 `processing/` → Muse 中文作答（按需调 skill）→
`outbox/<msg_id>.json` → `wxbot.py send` 发出（含图片上传）→ 归档到 `done/`。

图片消息：用户发的图片/截图以 `message_type == 1` 到达，但 `item_list` 里是
`type == 2` 的图片项而非文字。`wxbot.py receive` 会自动从微信 CDN 下载原图到
`media/in_<msg_id>.jpg`，并在 inbox 记录里加 `image` 字段；worker 用 read 工具
打开图片看清内容后再作答。

语音消息：`item_list` 里 `type == 3` 的语音项如带服务端转写文本
（`voice_item.text`），receive 会将其转为 `[语音] <转写>` 的文字消息处理；
无转写的语音目前跳过。

发送：`wxbot.py send` 按 outbox JSON 发送——`text` 分段发送（每段 3500 字），
可选 `"image"`（本地图片路径→CDN 上传→图片消息）、`"video"`（本地视频路径→
视频消息）、`"file"`（本地文件路径→文件消息，原文件名发出）。

## 为什么是事件驱动而不是定时轮询

最早是每分钟 cron 轮询：消息来了平均干等 30 秒才被发现。现在：

- **hook**（`bot/hook/wechat-inbox.sh`，每 10 秒）：纯 bash，零 agent 成本。发现新文件立刻 `wake`，只在有事时才起 worker。
- **看门狗下沉**：原来每分钟起一个 agent 只为了 `pgrep` 看进程，现在 hook 脚本顺手做：挂了 `setsid nohup` 拉起；`SESSION_EXPIRED` 才唤醒通知用户重扫（state flag 防重复）。
- **兜底 cron**（5 分钟）：只处理漏网——超过 3 分钟没人认领的 inbox 文件、超过 10 分钟卡在 processing 的文件（打回 inbox 重认领）。

## 防重复：原子认领

hook worker 和兜底 cron 可能同时看到同一个文件。规则只有一条：

> 先 `mv inbox/<id>.json processing/<id>.json`，mv 失败（文件已不在）就跳过。

`mv` 同一文件系统内是原子的，先搬走的赢，后到的看到文件没了就安静退出。**绝不**先读后删，**绝不**用"检查存在"代替"认领"。

## 登录态

- `credentials.json` 存 `$HOME` 下（沙盒重建不丢），600 权限
- 实测：杀掉接收器重起可直接复用旧凭证，无需重扫
- 唯一需要重扫的情形：微信服务端作废 token，receiver.log 出现 `SESSION_EXPIRED`
  （此时重启也救不回来，hook 会通知用户扫码）

## 收发协议细节

- 接收：`ilink/bot/getupdates`，带 `get_updates_buf` 位点（落盘 `buf.txt`），只收 `message_type == 1` 的用户消息
- 发送：`ilink/bot/sendmessage`，`message_type == 2`，长文本按 3500 字分段、`0.5s` 间隔发出
- 图片：`upload_media_to_cdn`（AES 加密上传）→ `send_image_item`
- 以上全部封装在 CowAgent 的 `channel/weixin/weixin_api.py`，`wxbot.py` 只是薄封装
- 网络优化（2026-09-29）：实测本机到 `ilinkai.weixin.qq.com` 建连仅 6ms、TLS 握手约 90ms，
  网络本身不是瓶颈。但原来每次 `_post` 都是全新 `requests.post`（每次握手），
  已给 `WeixinApi` 加上进程内持久 Session（keep-alive，见 `bot/patches/cowagent-keepalive.patch`，
  已提交进本地 CowAgent 克隆），多段回复/连续调用复用同一连接，每请求省约 0.1 秒；
  连接层错误时自动丢弃重建。短命的 `send` 进程立即生效；常驻 `receive` 进程下次重启后生效
  （它每次 35 秒长轮询，收益可忽略，不主动重启）

## 延迟构成

1. 微信 → 接收器：长轮询，几秒
2. 发现：hook 5 秒内（原来 cron 方案平均 30 秒）
3. 作答：worker agent 运行，简单问题约 20–40 秒，复杂问题（联网查、长思考） irreducible
4. 发送：1–2 秒

### "对方正在输入"的替代方案

ilink 协议的 `send_typing` 实测调通（`typing_ticket` 从 `get_config(user_id)` 取，
每次 `ret=0`），但微信客户端不渲染，属协议限制。所以用 `bot/ack_timer.sh`
代替：worker 认领消息后立刻在后台启动它，10 秒后若还没回完（`processing/<id>.json`
还在、且 `outbox/<id>.json` 还没写好），就发一条"收到，正在想，稍等…"；
回得快则静默退出，绝不打扰。三个分支都经过实测。

## 审批模型

微信发送是"以用户名义对外说话"的敏感操作。默认每次发送都要用户在审批卡上确认。
正道是给定时任务授**长期许可**（只覆盖该任务，设置页可撤销），不要绕过。
详见 [setup.md](setup.md) 第 9 步。

## CowAgent 借鉴（2026-09-29）

深挖 `channel/weixin/` 后搬过来的东西（`wxbot.py` 内有标注出处）：

- **引用消息解析**：`extract_ref_text()` + `msg_cache.json`。实测发现 ilink 的
  `ref_msg` 只带被引用消息的 `msg_id`（`message_item.type == 0`，无原文——
  CowAgent 的内联 title/text 假设不成立）；接收器把双向消息原文按 `msg_id`
  缓存（最近 300 条），引用时查缓存拼出 `[引用: 原文]` 前缀；缓存未命中时
  兜底 `[引用: 早些的消息]`。**注意**：服务端不回显 bot 自己的消息，
  type-2 轮询分支是死代码；发送侧改为从 `sendmessage` 响应的
  `message_id` 字段直接缓存（文字分片/图片/视频/文件均记）。
- **段落感知分段**：`split_text()` 照抄 CowAgent `_split_text`——优先在 `\n\n` /
  `\n` 处断句，取代原来的 3500 字硬切。
- **发送重试**：`send_text_retry()` 文字 chunk 失败/异常时隔 3 秒重试 1 次。
- **发送侧 -14 感知**：`check_send_response()` 发现 `ret/errcode == -14` 时写
  `.send_session_expired` 标记文件，hook 脚本检测到后提醒用户重新扫码；
  整单成功后自动清除标记。
- **视频/文件不再静默丢弃**：收到 `type == 4/5` 的消息转成 `[文件]`/`[视频]`
  进 inbox，由 Muse 如实回复"暂时看不了"，而不是装没看见。

结论：CowAgent 同样是非流式、单 session 串行，速度上没有更快的招；
`send_typing` 之外协议层已无遗漏。它的 D（图/文合并）设计是"文字等图 3 秒、
纯图不触发回复"，与之前否掉的方案不同，如需可再议。

## 会话上下文（2026-09-29，参考 CowAgent Session 思路）

- CowAgent 是常驻进程，Session.messages 放内存；本项目的 worker 每次是全新唤醒，所以会话落盘为 `history/<user_id>.jsonl`（append-only JSONL，每行 `{ts, role, text}`）。
- receiver 写 inbox 时记 `user` 侧；`wxbot.py send` 整单成功后记 `assistant` 侧（多分片合并为一条）。
- 超过 60 行（约 30 轮）从头丢弃，对应 CowAgent 的 `discard_exceeding`。
- worker 作答前读尾部 30 行作上下文；涉及长期记忆（偏好、过去的事、记忆里的人）时用 Muse 的 memory_search，Peter 的持久新事实写回记忆。注意微信对面可能是 Peter 的朋友而非本人，隐私不外传。

## 发送互斥与过期 ack（2026-09-29）

- `wxbot.py send` 用 `send.lock` + `flock(LOCK_EX)` 互斥：ack_timer 和 worker 会并发起 send 进程，无锁时各自看到 outbox 里对方的文件、按文件名排序发出，曾实测导致"收到，正在想，稍等…"排在真正回复之后到达用户。
- 过期 ack 跳过：`_is_stale_ack()` 发现同 id 的真正回复已发出（`sent/<id>.json` 或 `done/<id>.json` 存在）时，直接把 `ack_<id>.json` 归档不再发送。
- 单次发送 API 超时 15s、有重试上限，锁不会被无限持有；flock 随进程结束自动释放。
