# 转写方案怎么选

纪要需要“带时间戳、区分说话人”的转写。本技能有三种做法，用户只需选一种：

| | 云端：阿里云百炼（Qwen） | 本地：FunASR | 本地：Whisper |
| --- | --- | --- | --- |
| 需要 API Key | 需要 | 不需要 | 不需要 |
| 录音去向 | 上传到阿里云 | 不离开电脑 | 不离开电脑 |
| 中文准确度 | 高 | 高 | 中等（偶有错字、繁体） |
| 区分说话人 | 支持 | 支持（CAM++） | 支持（sherpa-onnx） |
| 额外安装 | 不用；ffprobe（FFmpeg）可选，用于检查声道/读取部分格式时长 | 约 0.5～3 GB（含 PyTorch；Linux 默认带显卡支持，最大） | 约 0.2～0.5 GB |
| 首次下载模型 | 无 | 约 1～1.5 GB，来自 ModelScope（国内快） | 0.5～3 GB，来自 Hugging Face（国内需加 `--hf-mirror`）；说话人模型约 40 MB，来自 GitHub |
| 速度 | 快，不占电脑 | CPU 上较快 | 取决于模型大小，`small` 较快、`large-v3` 很慢 |
| 擅长 | 各种场景 | 普通话会议 | 中英混说、多语言 |
| 费用 | 按量计费（新用户有免费额度） | 免费 | 免费 |

**建议**：有阿里云 Key 就用云端，最省事；没有 Key 或不想上传录音，普通话为主选 FunASR，中英混说多或想装得轻一点选 Whisper。本地识别的说话人区分通常不如云端稳定，认领时多看一眼原话。

## 云端：阿里云百炼 API Key

1. 登录 [阿里云百炼控制台](https://bailian.console.aliyun.com/)（没有阿里云账号先注册，并完成实名认证）。
2. 按提示开通百炼和模型服务。新用户首次开通会自动获得各模型的免费额度（通常 30～90 天有效），具体额度在“模型广场”点开模型查看。
3. 在左侧（或右上角头像菜单）找到“API-Key”，点击“创建 API Key”，复制保存。密钥只完整显示一次。
4. 在本机配置，**不要把密钥发到对话里**：
   - macOS：运行 `bash <skill-dir>/scripts/setup_keychain.sh`，按提示粘贴，密钥存进钥匙串。
   - Windows：在“系统 → 关于 → 高级系统设置 → 环境变量”里新建用户变量 `DASHSCOPE_API_KEY`。
   - Linux：在 `~/.bashrc` 或 `~/.zshrc` 里加 `export DASHSCOPE_API_KEY=...`。
5. 新加坡地域的 Key 转写时加 `--region intl`。

账户欠费时，即使还有免费额度也无法调用。

## 本地：FunASR

阿里达摩院开源的语音识别（Paraformer 模型 + CAM++ 说话人区分），中文效果最好。

```bash
python3 <skill-dir>/scripts/transcribe_local.py --check           # 查看缺什么，给出安装命令
"<python>" -m pip install -U funasr torch torchaudio modelscope    # 用 --check 输出里的命令
python3 <skill-dir>/scripts/transcribe_local.py --audio 录音.m4a --engine funasr [--speakers 3]
```

- 处理 m4a、mp4 建议安装 ffmpeg（macOS：`brew install ffmpeg`；Windows：`winget install ffmpeg`）。
- 国内 pip 慢时可加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。
- 有 NVIDIA 显卡时自动使用；Mac 默认用 CPU。

## 本地：Whisper

OpenAI 开源 Whisper 的加速版 faster-whisper，配合 sherpa-onnx 区分说话人，不需要 PyTorch。

```bash
python3 <skill-dir>/scripts/transcribe_local.py --check
"<python>" -m pip install -U faster-whisper sherpa-onnx
python3 <skill-dir>/scripts/transcribe_local.py --audio 录音.m4a --engine whisper --model small [--hf-mirror] [--speakers 3]
```

- `--model`：`small`（默认，约 0.5 GB）、`medium`、`large-v3`（最准也最慢，约 3 GB）、`turbo`。
- 国内下载 Whisper 模型加 `--hf-mirror`（使用 hf-mirror.com）。说话人模型从 GitHub 下载，下载失败时按报错提示手动放到指定文件夹。
- 已加简体中文提示和静音过滤，仍可能出现个别繁体字或长时间静音处的重复文字，认领和术语复核时留意。

## 其他云服务（暂未接入）

以下服务都提供“录音文件识别 + 说话人区分”，本技能暂未接入，列出供参考；额度和价格以官网为准：

- **腾讯云** 语音识别 · 录音文件识别：需要 SecretId / SecretKey。
- **讯飞开放平台** 录音文件转写：需要 APPID 和 APIKey。
- **火山引擎（豆包语音）** 录音文件识别：需要 App ID 和 Access Token。
- **百度智能云** 音频文件转写：提供 10 小时免费额度（1 年有效）。

MiniMax 目前主要提供语音合成和声音复刻，没有查到录音转写接口，所以不在此列。

想接入其中某一家，欢迎在仓库提 Issue。

## 给助手的规则

- 用户没有 Key 时，先说明上表三种做法的差别，让用户选；不要替用户决定，也不要自动安装。
- 安装本地引擎前说明要装的包、大致大小和下载来源，用户同意后再执行 `--check` 给出的命令；装到技能运行用的同一个 Python。
- 本地识别不上传录音，但首次会联网下载模型；不要说成“完全离线”。
- 转写结果里的 `speaker_separation`（`voice` / `partial` / `none`）和 `speaker_coverage` 是实际分离情况，按 [角色认领](roles.md#输入与来源) 如实告诉用户。离说话段超过 1 秒的词不会硬分给最近的人，而是标为“说话人未分离”。
- 处理分步保存：Whisper 会先检查、下载说话人分离模型，再开始转写；转写文字和分离结果各自保存，中途失败后再运行会从失败的那一步继续，不会重转整段。缓存文件损坏（例如上次被强行中断）会被移到一边并自动重算。分离模型下载不了而用户同意先拿到不分说话人的文字时，加 `--allow-unseparated`。
- 本地结果与云端结果格式相同，直接交给 `prepare_speakers.py`；同一录音、同一引擎、发言人数和热词会复用本机缓存（`--force` 重新识别）。
- 两种本地引擎都可加 `--hotwords hotwords.json`（格式同云端），让识别偏向已确认的人名和术语；热词不出电脑。词表来自 `workspace.py hotwords`（已确认术语）或 `obsidian.py hotwords`（再加 Obsidian 库里的人名和标题，见 [Obsidian 库](obsidian.md)）。含空格的词本地引擎用不了，会自动跳过。
