# crons

本目录不再有兜底 agent cron。

2026-10-16 之前，兜底是每 3 分钟跑一次的 agent cron
（`wechat-bot-responder`）：每轮必起 agent，哪怕空轮也烧额度。
之后改为 hook 门控：

- 门控脚本：`../hooks/scripts/wechat-sweeper-gate.sh`，每 180 秒轮询一次。
  看门狗（接收器死亡/卡死重启）在 bash 内做，不唤醒 agent；
  只在 stale inbox（>2 分钟）/ processing 超时（无 `.alive` 4 分钟，
  有 `.alive` 10 分钟）/ 登录态过期时唤醒 agent。
- 兜底 worker 指令：`../bot/prompts/sweeper-gate-worker.md`。

旧的 `wechat-bot-responder.md`（3 分钟 agent cron 定义）已删除。
