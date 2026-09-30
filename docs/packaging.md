# 打包说明

`muse-wechat-bot.zip` 只含一个顶层目录 `muse-wechat-bot/`（本仓库内容，
排除 `.git/`）。

muse-auto-approve **不打进 zip**：它的代码走稀疏克隆、依赖走 `npm install`，
部署 playbook 完整写在 `skills/muse-auto-approve.md` 的
"部署（新 Muse 上从零安装）"一节（含 keepalive.sh 全文）。

打包命令（workspace 根目录执行）：

```bash
cd ~/workspace
rm -f muse-wechat-bot.zip
zip -rq muse-wechat-bot.zip muse-wechat-bot -x "muse-wechat-bot/.git/*"
```
