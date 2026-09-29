# 排障

## receiver.log 出现 SESSION_EXPIRED

微信服务端作废了登录 token，重启救不回来。**唯一需要重新扫码的情形**。

```bash
cd ~/workspace/wechat-bot
~/workspace/CowAgent/venv/bin/python wxbot.py qr        # 新二维码发给用户扫
~/workspace/CowAgent/venv/bin/python wxbot.py wait_login # 确认后落盘新凭证
setsid nohup ~/workspace/CowAgent/venv/bin/python wxbot.py receive \
  > receiver.log 2>&1 < /dev/null &
rm -f ~/hooks/state/wechat-bot-relogin.flag             # 清掉"已通知"标记
```

hook 平时会替你发现并通知用户，这里是手动流程。

## 接收器掉了但没 SESSION_EXPIRED

直接重启即可，旧 `credentials.json` 照用，不用重扫：

```bash
pgrep -f "wxbot.py receive" || \
  (cd ~/workspace/wechat-bot && setsid nohup ~/workspace/CowAgent/venv/bin/python \
    wxbot.py receive > receiver.log 2>&1 < /dev/null &)
```

hook 的看门狗一般 10 秒内已经自动拉起了，先 `pgrep` 确认再手动。

## 消息没人回（inbox 积压）

1. `ls ~/workspace/wechat-bot/inbox/` 看是否有文件
2. `hooks.logs --id wechat-inbox` 看 hook 有没有 wake
3. 有 wake 但 worker 没处理 → 看兜底 cron 日志；超过 3 分钟的会被 cron 捡走
4. `processing/` 里超过 10 分钟的文件会被 cron 打回 inbox，属正常自愈

## 重复回复了

检查两处：

1. worker 是否都走了 `mv inbox/ → processing/` 原子认领（prompt 里有，不要手写"先读后删"）
2. `wxbot.py send` 是否对同一个 outbox 文件发了两次（正常 send 后文件会移到 `sent/`，只有 `ret != 0` 才会留下重试）

## 回复慢

先分段定位（见 [architecture.md](architecture.md) 的延迟构成）：

- 发现慢 → hook 没 enable？`hooks.list` 确认
- 作答慢 → 正常。worker 是完整 agent，简单问题 20–40 秒；让 prompt 强调"简单问题直答、少做多余搜索"
- 发送慢 → 看 `receiver.log` / 网络

## 每次发送都要点确认

见 [setup.md](setup.md) 第 9 步：给定时任务授长期许可。不要绕过审批。

## send 报 SEND_FAIL

- `ret != 0`：看返回体里的错误码；图片失败多为上传问题（检查文件存在、大小）
- `SEND_ERROR`：多为网络抖动，文件留在 outbox，下次 send 会重试（注意幂等：文字分段发送失败可能导致部分重复，属已知取舍）

## 扫码后 wait_login 超时

二维码有效期短，过期重取：`wxbot.py qr` 重新生成再扫。确认手机微信已登录且网络通畅。
