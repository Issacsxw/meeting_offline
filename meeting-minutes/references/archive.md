# 录音与交付归档

默认目录由 `device_context.py init` 的 `archive` 返回，新设备通常是 `~/Documents/会议纪要/<device-id>`。每场一个 `YYYY-MM-DD_会议主题` 文件夹，里面放移动后的原录音、`会议纪要.html`、`会议纪要.png`。原录音保留原文件名；转写、词库与角色档案仍保存在独立本地工作区，不混入对外交付文件。仅列出最近录音不会触发移动，选定要处理的文件后才执行。

## 选定录音后

```bash
python3 <skill-dir>/scripts/archive_meeting.py \
  --audio /path/to/录音.m4a --topic 会议主题 --meeting-date 2026-10-12
```

`--meeting-date` 仅填有依据的会议日期。未知时省略，脚本用本机当天日期并明确命名 `YYYY-MM-DD（归档）_主题`；可用 `--archive-date YYYY-MM-DD` 显式指定当前本地归档日期。不要把导出文件的创建日期当作会议日期。主题暂不明确时可省略，先用原文件名，完成后会按最终主题调整同一个文件夹。下游转写、工作区导入应使用脚本返回的 `audio` 路径。

普通本地录音默认移动原文件，不保留源目录副本。Apple 语音备忘录应用托管的库文件仍只读，先由只读导出流程取得普通本地文件，再移动导出文件。此归档操作本身没有网络请求，也不扩大转写的云端授权。

## 正文与 PNG 完成后

工作区导出已自动生成 HTML 与 PNG。打开 PNG 检查后：

```bash
python3 <skill-dir>/scripts/workspace.py publish --workspace /path/to/workspace
python3 <skill-dir>/scripts/archive_meeting.py \
  --workspace /path/to/workspace --source 4
```

脚本核对发布版本、HTML/PNG 校验值和录音校验值，再归档并记录新 `audio_path`、原路径及修订；现有复核服务按新路径回听。检查音频 Range 请求、文件夹内文件及校验值后交付。

独立模式可补齐同一录音的交付物：

```bash
python3 <skill-dir>/scripts/archive_meeting.py \
  --audio /path/to/归档后的录音.m4a --topic 最终会议主题 \
  --html /path/to/已核对的纪要.html --png /path/to/对应纪要.png
```

可用 `--root /path/to/会议纪要` 覆盖归档根目录。同一音频按校验值复用目录；不同音频的同名会议加后缀，不覆盖原文件。隐藏的 `.archive-index.json` 和 `.archive.json` 用于续做、查验和恢复路径。已有归档 HTML/PNG 若被用户修改，保留原稿，新生成稿另存为 `会议纪要-更新版-校验值前缀`。再次执行前沿用工作区/脚本返回的新路径，不继续使用已移动的旧路径。
