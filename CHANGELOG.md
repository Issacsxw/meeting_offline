# 更新记录

## 11.8.0

- 五种纪要样式：暖白陶土（默认）、商务蓝、极简黑白、清新绿、深色。网页工具栏随时切换，保存 HTML 记住所选样式，导出图片跟着变；也可以在偏好里设为默认。
- 没有 API Key 也能转写：新增本地识别 `scripts/transcribe_local.py`，可选 FunASR（中文最好）或 Whisper（更轻、多语言），都能区分说话人，录音不离开电脑。
- 新增转写方案指南：云端与本地的对比、阿里云百炼 API Key 申请步骤、其他云服务参考。
- 修复：API Key 填错一次后这段录音再也无法重新转写的问题；长录音上传不再固定 5 分钟超时；上传到阿里云临时存储时不再带原文件名。
- 改进：常见错误改为中文提示并说明怎么处理；macOS 钥匙串脚本不再把密钥放在命令行参数里。
- README 重新整理，加入样式展示和转写方案对比。

## 11.7.0

- 新增“找最近的录音”：`scripts/find_recordings.py` 在 iCloud 云盘、下载、桌面（Windows 包括 OneDrive 桌面）中列出最近的 m4a、wav、mp4 录音，标出还没从 iCloud 下载的文件；只读，不移动、不上传。
- 输入改为只接受会议录音：只有文字稿、没有录音的情况不再支持；同一段录音已有的转写结果仍会复用。

## 11.6.0

- 让 AI 助手直接从 GitHub 安装：新增写给 AI 的 `INSTALL.md`；README 开头加上“让 AI 帮你安装”。
- 支持 Claude Code 插件市场（`/plugin install meeting-offline --marketplace sunxuwang001-sketch/meeting_offline`）和 `npx skills add sunxuwang001-sketch/meeting_offline`。
- 技能文件夹移到 `skills/meeting-offline/`，以便各类技能安装工具自动发现；本地安装包和上传包的内容不变。

## 11.5.0

- 技能改名为 `meeting-offline`（仓库名 `meeting_offline`），调用方式改为 `$meeting-offline` / `/meeting-offline`。
- 从旧版升级无需手动处理：旧的 `meeting-minutes` 技能会移到 `skill-backups/`；旧数据目录、`MEETING_MINUTES_*` 环境变量和偏好设置继续有效，已积累的角色和术语不受影响。
- 新增 README、MIT 许可证，以及版本号更新合并到 main 后自动发布 Release 的流程。

## 11.4.0

- 支持 Codex、Claude Code、WorkBuddy、CodeBuddy（本机安装），以及 ChatGPT、Claude.ai（上传技能包，在云端沙箱运行）。
- `install.py --target` 按工具安装，默认装到本机检测到的全部工具；同一台电脑上的各工具共用一份角色和术语库。
- 新增云端沙箱模式和独立的上传包。

## 11.3.0

- 短会议快速通道：不超过 2 位发言人时，认领卡片和纪要草稿放在同一条消息里，回复一次即可定稿。
- 去掉写死的个人化处理；纪要里的显示名改为每台设备可选的 `display_names` 设置。

## 11.2.0

- SKILL.md 从 141 行精简到约 70 行，细节移到 `references/roles.md` 和 `references/memory.md`；补充触发词。
- 修复术语替换：不再重复替换已正确的写法，不再改动更长的已知词（如“王总监”）和英文单词内部。

## 11.1.0

- PNG 由本机浏览器在后台直接生成，与页面里“导出图片”的结果一致；修改 HTML 后可重新出图。
- 新增 `workspace.py publish`，自动记录发布文件的校验值供归档使用。

## 11.0.0

- 首个公开版本的基线。
