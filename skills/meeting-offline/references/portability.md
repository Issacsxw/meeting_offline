# 安装、工具适配与设备隔离

这是一个整合技能：本地录音归档、Qwen 转写、角色认领、术语复核、可编辑 HTML、PNG 和本地积累都由同一个 `meeting-offline` 提供。纪要由运行它的 AI 助手阅读转写后编写，不是独立运行的自动摘要程序。技能采用通用的 SKILL.md 格式，不依赖某个工具的专属功能。

## 支持的工具

| 工具 | 运行方式 | 安装 | 调用 |
| --- | --- | --- | --- |
| Codex | 本机 | `npx skills add sunxuwang001-sketch/meeting_offline --skill meeting-offline -g -a codex`，或 `python3 install.py --target codex` → `~/.agents/skills/` | `$meeting-offline` 或直接描述任务 |
| Claude Code | 本机 | 插件市场：`/plugin install meeting-offline --marketplace sunxuwang001-sketch/meeting_offline`；或 `python3 install.py --target claude` → `~/.claude/skills/` | `/meeting-offline` 或直接描述任务 |
| WorkBuddy | 本机 | `python3 install.py --target workbuddy` → `~/.workbuddy/skills/`；或在应用里“专家 → 技能 → 添加技能 → 导入技能”拖入上传包 | 新开会话，直接描述任务 |
| CodeBuddy / WorkBuddy 企业版 | 本机 | `python3 install.py --target codebuddy` → `~/.codebuddy/skills/` | 新开会话，直接描述任务 |
| ChatGPT | 云端沙箱 | 上传 `meeting_offline-<版本>-upload.zip`（需要支持技能的工作区套餐） | 直接描述任务 |
| Claude.ai / Claude 桌面应用 | 云端沙箱 | 设置 → 功能 → 技能 → 上传同一个上传包 | 直接描述任务 |

- 用户让 AI 助手安装时，助手按仓库根目录的 `INSTALL.md` 选择方式。技能文件夹在仓库的 `skills/meeting-offline/`，`npx skills`、Claude Code 插件市场和 Codex skill-installer 都直接安装这个文件夹；不经过 `install.py` 时，数据目录在首次运行 `device_context.py init` 时创建。
- 不带 `--target` 时，安装器会装到本机检测到的全部工具；`--target all` 全部安装；其他工具用 `--skills-dir <该工具的 skills 目录>`。
- Codex 以前读 `$CODEX_HOME/skills`。安装到 `~/.agents/skills` 时，旧位置的 `meeting-offline` 会移到 `skill-backups/`，避免出现两份；仍使用旧版 Codex 时用 `--skills-dir ~/.codex/skills`。
- 改名前的 `meeting-minutes` 技能会移到 `skill-backups/`，避免和 `meeting-offline` 同时出现。已有同名技能会先备份，数据目录不会被覆盖。安装只复制技能、初始化本机目录，不上传文件、不读取密钥，也不安装 MCP。
- 同一台电脑上的各个本机工具共用同一份角色库、术语库和归档，在哪个工具里确认的内容都会继续积累。

需要 Python 3.9+、Chrome/Edge/Chromium 之一（用于直接生成 PNG；缺少时可在 HTML 页面手动导出），以及进行 Qwen 文件上传时可用的 curl。macOS 语音备忘录读取需按 [只读 MCP 配置](voice-memos-setup.md) 单独设置；直接处理本地录音不需要。已在 macOS 验证；跨平台文件锁和时长读取有离线测试，Windows/Linux 尚未做实机端到端验证。

首次云端转写前，在本机配置 DashScope 密钥：macOS 运行 `scripts/setup_keychain.sh`，其他平台设置环境变量 `DASHSCOPE_API_KEY`。密钥不写入技能或会议文件；钥匙串服务名包含设备 ID，避免沿用其他电脑的钥匙串项。云端录音处理仍以当次用户授权为准。

## 每次开始

```bash
python3 <skill-dir>/scripts/device_context.py init
```

使用命令返回的 `memory`、`preferences`、`meetings`、`jobs`、`archive` 路径，不凭旧聊天或另一台电脑的绝对路径寻找记忆。设备 ID 来自 macOS 的 IOPlatformUUID、Windows 的 MachineGuid 或 Linux 的 machine-id；只保存带固定命名空间的 SHA-256 截断摘要，原始标识不落盘、不上传。读不到稳定标识时停止初始化，为该电脑单独设置 `MEETING_OFFLINE_DEVICE_ID`；不要给不同电脑复用同一值。

默认数据目录是 `~/.meeting-offline/devices/<device-id>/`。旧版本的数据目录（改名前的 `~/.meeting-minutes/`、更早的 `~/.codex/meeting-minutes-memory/`）存在时继续使用，不需要迁移；设备 ID 和钥匙串名称没有随改名变化。里面包含 `memory.json`、`preferences.json`、`meetings/`、`transcription-jobs/`。归档目录是 `~/Documents/会议纪要/<device-id>/`。同设备重复安装继续使用原库；不同设备自动使用空库，即使 home 或 Documents 被同步，也不扫描或合并其他设备的目录。

工作区、记忆库、归档索引和转写任务都写入设备归属。遇到其他设备标记会拒绝续做，不自动恢复它的角色、热词或云端任务。用户提供的新录音/转写是新的输入，仍需按本场确认角色和术语。默认不导入未标注设备的旧库。

`MEETING_OFFLINE_DATA_HOME` 可修改数据根目录，通常无需设置。改名前的 `MEETING_MINUTES_*` 环境变量仍然有效。`--store`、`--job-dir`、`--root` 等自定义路径仍检查归属；不能通过改路径借用另一台电脑的数据。设备目录用于分开积累，不是文件加密。

## 云端沙箱（ChatGPT、Claude.ai）

云端沙箱没有稳定的机器标识，会话结束后文件不保留，也接触不到用户电脑。每条命令前加 `MEETING_OFFLINE_SANDBOX=1`，并把数据目录设到平台可下载的输出目录（ChatGPT 通常是 `/mnt/data`，Claude.ai 通常是 `/mnt/user-data/outputs`）：

```bash
MEETING_OFFLINE_SANDBOX=1 MEETING_OFFLINE_DATA_HOME=/mnt/data/meeting-offline-data \
  python3 <skill-dir>/scripts/device_context.py init
```

- **输入**：用户上传的会议录音。转写需要沙箱能访问 DashScope，并通过平台的密钥或环境变量设置提供 `DASHSCOPE_API_KEY`（不要让用户把密钥贴进对话）；沙箱没有钥匙串。做不到时如实告诉用户这个环境无法转写，建议改用本机的 AI 工具，不要尝试绕过网络限制。
- **认领与术语**：在聊天里完成，不启动本机复核页面。
- **交付**：HTML 与 PNG 写到输出目录供下载；沙箱里没有浏览器时只交付 HTML，并告诉用户在页面点“导出图片”。没有本机录音可移动，归档只整理输出目录。
- **积累**：会话结束时把 `memory.json` 一起交给用户下载，下次上传后放回 init 返回的 `memory` 路径即可继续积累。所有沙箱会话共用一个固定的沙箱身份，所以能互相读取；本机电脑的库不能直接拿到沙箱里用，反之亦然。
- 不要在本机电脑上设置 `MEETING_OFFLINE_SANDBOX`。

## 本机旧版积累

原使用电脑可有一份仅保存在本机的 `devices/<device-id>/device.json`，将旧数据根目录与旧归档目录绑定到该设备。仅当设备 ID 一致时生效，不复制进分发包；其他电脑忽略它并从空库开始。不要自动把陌生的旧目录登记为本机目录，也不要批量复制旧设备档案来“修复”不匹配。

## 打包边界

`build_package.py --out <目录>` 生成两个包：`meeting_offline-<版本>-local.zip`（含安装器、清单和测试，用于本机工具）和 `meeting_offline-<版本>-upload.zip`（只含技能文件夹，用于 ChatGPT、Claude.ai 上传或 WorkBuddy 导入）。两者都排除本地库、偏好中的个人信息、设备档案、录音、真实转写与纪要、任务缓存、会话链接和密钥。`MANIFEST.json` 提供文件校验值，安装器先校验再复制。不要把整个数据目录或 `会议纪要` 文件夹一起打包。
