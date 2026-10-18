# 安装与设备隔离

这是一个整合技能：本地录音归档、Qwen 转写、角色认领、术语复核、可编辑 HTML、PNG 和本地积累均由同一个 `meeting-minutes` 提供。真实摘要由 Codex 阅读转写后编写，不是独立运行的自动摘要应用。

## 在另一台电脑安装

解压完整压缩包，在解压目录运行 `python3 install.py`；Windows 可用 `py -3 install.py`。默认安装到本机 `$CODEX_HOME/skills/meeting-minutes`，没有设置 CODEX_HOME 时使用 `~/.codex/skills/meeting-minutes`。已有同名技能会先备份，数据目录不会被覆盖。安装只复制技能、初始化本机目录，不上传文件、不读取密钥，也不安装 MCP。

需要 Python 3.9+、浏览器，以及进行 Qwen 文件上传时可用的 curl。macOS 语音备忘录读取需按 [只读 MCP 配置](voice-memos-setup.md) 单独设置；直接处理本地录音无需这个 MCP。Apple 语音备忘录集成仅用于 macOS；Windows/Linux 可走本地文件路线。已在 macOS 验证；跨平台文件锁和时长读取有离线测试，Windows/Linux 尚未做实机端到端验证。

在 Codex 重新载入技能后可说：“使用 $meeting-minutes，整理这份录音。” 首次云端转写前，在本机配置 DashScope 密钥：macOS 运行 `scripts/setup_keychain.sh`，其他平台设置本机环境变量 `DASHSCOPE_API_KEY`。密钥不写入技能或会议文件；服务名默认也包含设备 ID，避免沿用其他电脑的钥匙串项。云端录音处理仍以当次用户授权为准。

## 每次开始

```bash
python3 <skill-dir>/scripts/device_context.py init
```

使用命令返回的 `memory`、`preferences`、`meetings`、`jobs`、`archive` 路径，不凭旧聊天或另一台电脑的绝对路径寻找记忆。设备 ID 来自 macOS 的 IOPlatformUUID、Windows 的 MachineGuid 或 Linux 的 machine-id；只保存带固定命名空间的 SHA-256 截断摘要，原始标识不落盘、不上传。读不到稳定标识时停止初始化，为该电脑单独设置 `MEETING_MINUTES_DEVICE_ID`；不要给不同电脑复用同一值。

新设备默认数据目录：`~/.codex/meeting-minutes-memory/devices/<device-id>/`，包含 `memory.json`、`preferences.json`、`meetings/`、`transcription-jobs/`。归档目录是 `~/Documents/会议纪要/<device-id>/`。同设备重复安装继续使用原库，不重置积累；不同设备自动使用空库，即使 home 或 Documents 被同步，也不扫描或合并其他设备的目录。

工作区、记忆库、归档索引和转写任务均写入设备归属。遇到其他设备标记会拒绝续做，不自动恢复它的角色、热词或云端任务。用户提供一份新录音/转写是新的输入，仍需按本场确认角色和术语。默认不导入未标注设备的旧库。

`MEETING_MINUTES_DATA_HOME` 可修改数据根目录，通常无需设置。`--store`、`--job-dir`、`--root` 等自定义路径仍检查归属；不能通过改路径自动借用另一台电脑的数据。设备目录用于分开积累，不是文件加密。

## 本机旧版积累

原使用电脑可有一份仅保存在本机的 `devices/<device-id>/device.json`，将旧数据根目录与旧归档目录绑定到该设备。仅当设备 ID 一致时生效，不复制进分发包；其他电脑忽略它并从空库开始。不要自动把陌生的旧目录登记为本机目录，也不要批量复制旧设备档案来“修复”不匹配。

## 打包边界

分发仅包含技能说明、通用暖白模板、脚本、测试和安装器。排除本地库、偏好中的个人信息、设备档案、录音、真实转写与纪要、任务缓存、会话链接和密钥。包根目录 `MANIFEST.json` 提供文件校验值，安装器先校验再复制。不要把整个 `.codex` 或 `会议纪要` 文件夹一起打包。
