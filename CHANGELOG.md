# 更新记录

## 11.5.0

- 技能改名为 `meeting-offline`（仓库名 `meeting_offline`），调用方式改为 `$meeting-offline` / `/meeting-offline`。
- 从旧版升级无需手动处理：旧的 `meeting-minutes` 技能会移到 `skill-backups/`；旧数据目录、`MEETING_MINUTES_*` 环境变量和偏好设置继续有效，已积累的角色和术语不受影响。
- 新增 README、MIT 许可证，以及打 tag 后自动发布 Release 的流程。

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
