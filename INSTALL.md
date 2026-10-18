# 安装说明（写给 AI 助手）

用户让你安装 meeting_offline 时，按这份说明做。技能名是 `meeting-offline`，技能文件夹在本仓库的 `skills/meeting-offline/`，里面的 `SKILL.md` 是入口。

- 仓库：https://github.com/sunxuwang001-sketch/meeting_offline
- 用途：把会议录音或转写整理成会议纪要（主题、时间、参会人、核心要点、主要 Todo），交付可编辑 HTML 和 PNG；说话人身份由用户确认，确认过的角色和术语保存在本机。
- 依赖：Python 3.9+（只用标准库）。生成 PNG 用本机的 Chrome、Edge 或 Chromium，没有也能用。
- 许可证：MIT。

## 先告诉用户你要做什么

执行任何安装命令前，用一两句话告诉用户：要从这个仓库安装 `meeting-offline` 技能、装到哪里、会运行哪条命令。用户同意后再执行。

## 按你所在的工具选一种方式

### Claude Code

```bash
claude plugin marketplace add sunxuwang001-sketch/meeting_offline
claude plugin install meeting-offline@meeting-offline
```

用户也可以在 Claude Code 里自己输入：`/plugin install meeting-offline --marketplace sunxuwang001-sketch/meeting_offline`。

### Codex、Cursor 等支持 `npx skills` 的工具

```bash
npx skills add sunxuwang001-sketch/meeting_offline --skill meeting-offline -g -y
```

需要指定工具时加 `-a`，例如 `-a codex` 或 `-a claude-code`。

Codex 也可以用自带的 skill-installer，路径是 `https://github.com/sunxuwang001-sketch/meeting_offline/tree/main/skills/meeting-offline`。

### WorkBuddy、CodeBuddy，或上面的方式不可用时

用仓库自带的安装器。它会校验文件、装到对应工具的 skills 目录，并初始化本机数据目录：

```bash
git clone --depth 1 https://github.com/sunxuwang001-sketch/meeting_offline.git
python3 meeting_offline/install.py --target workbuddy
```

- `--target` 可选 `codex`、`claude`、`workbuddy`、`codebuddy`、`all`；不加时装到本机检测到的全部工具。
- Windows 用 `py -3` 代替 `python3`。
- 没有 git 时，从 [最新 Release](https://github.com/sunxuwang001-sketch/meeting_offline/releases/latest) 下载 `meeting_offline-<版本>-local.zip`，解压后在解压目录运行 `python3 install.py`。
- 安装完成后，可以删除克隆或解压出的临时目录。

### ChatGPT、Claude.ai（网页 / App）

你无法替用户安装。请告诉用户：从 [最新 Release](https://github.com/sunxuwang001-sketch/meeting_offline/releases/latest) 下载 `meeting_offline-<版本>-upload.zip`，然后在技能设置里上传（Claude.ai：设置 → 功能 → 技能；ChatGPT 需要支持技能的工作区套餐）。

## 确认装好了

找到装好的技能文件夹（包含 `SKILL.md` 的 `meeting-offline` 目录），运行：

```bash
python3 <技能文件夹>/scripts/device_context.py init
```

输出包含 `memory`、`archive` 等路径就说明可以用了。

## 装好后告诉用户

1. 重新打开（或新开一个会话）让技能生效。
2. 直接说“整理这份会议录音”或“把这份文字稿整理成纪要”即可使用；Codex 可输入 `$meeting-offline`，Claude Code 可输入 `/meeting-offline`。
3. 只有需要把录音转成文字时，才要配置阿里云 DashScope API Key：macOS 运行 `<技能文件夹>/scripts/setup_keychain.sh`，其他系统设置环境变量 `DASHSCOPE_API_KEY`。已有文字稿不需要。

## 注意

- 不要让用户把 API Key 粘贴到对话里，也不要把密钥写进任何文件。
- 如果本机已有旧名称的 `meeting-minutes` 技能，用 `install.py` 安装会把它移到 `skill-backups/`；用其他方式安装时，提醒用户删除旧的 `meeting-minutes` 文件夹，避免出现两个相同的技能。用户的角色库和术语库不受影响。
- 不要修改或删除 `~/.meeting-offline/`（以及旧版的 `~/.meeting-minutes/`、`~/.codex/meeting-minutes-memory/`）里的数据，那是用户积累的角色和术语。
