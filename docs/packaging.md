# 打包说明

`muse-wechat-bot.zip` 只含一个顶层目录 `muse-wechat-bot/`（本仓库内容，
排除 `.git/`）。

打包命令（仓库目录执行；用 git archive 只打已入库文件，
`__pycache__` / `.pytest_cache` / `*.log` 等被忽略的运行时文件不会进包）：

```bash
cd ~/workspace/muse-wechat-bot
git archive --format=zip --prefix=muse-wechat-bot/ HEAD -o ../muse-wechat-bot.zip
```

包里必须有的关键内容（发包前核对）：

- `bot/wxbot.py` 与线上 `~/workspace/wechat-bot/wxbot.py` 一致（diff 为空）
- `hooks/definitions/wechat-inbox.json`、
  `hooks/definitions/wechat-sweeper-gate.json`（hook 注册定义，prompt 以此为准）
- `hooks/scripts/wechat-inbox.sh`、`wechat-sweeper-gate.sh`、
  `wechat-receiver-supervisor.sh`（与线上 `~/hooks/scripts/` 一致）
- `bot/tests/`（全量回归）、`docs/setup.md`（安装步骤）

绝不能进包的：`credentials.json`、聊天记录、运行时缓存、qr 图片、
`run.log`、`.pytest_cache`、`__pycache__`、运行时状态目录
（`inbox/`、`outbox/`、`sent/`、`done/`、`processing/`、`media/` 内容）。
打包前先跑全量测试（`cd bot && python -m pytest tests/ -q`），再解压到干净
目录重跑一次，确认无凭证、无绝对路径依赖（除安装文档里注明的
`/home/hatch` 替换清单外）。
