# macOS setup

This Skill targets macOS Sonoma 14 or later. macOS Sequoia or later is preferred for Apple's built-in transcripts.

## 1. Check prerequisites

```bash
node --version
npm --version
python3 --version
```

Node.js must be 18 or later. Python 3.9 or later is recommended. No Python package installation is required.

Open Apple Voice Memos once before continuing.

## 2. Install the read-only MCP server

```bash
npm install -g apple-voice-memo-mcp
```

Register it with the AI tool you use, exposing only the read-only tools:

- **Codex** — add to `~/.codex/config.toml`:

  ```toml
  [mcp_servers.apple-voice-memo-mcp]
  command = "npx"
  args = ["-y", "apple-voice-memo-mcp"]
  enabled_tools = ["list_voice_memos", "get_voice_memo", "get_audio", "get_transcript"]
  enabled = true
  ```

- **Claude Code** — run `claude mcp add apple-voice-memo -- npx -y apple-voice-memo-mcp`, then allow only the four tools above (for example in `permissions.allow` of `~/.claude/settings.json`) and deny `transcribe_memo`.
- **WorkBuddy / CodeBuddy** — add an MCP server in the app settings with command `npx` and arguments `-y apple-voice-memo-mcp`, and enable only the four tools above.

The restricted tool list intentionally excludes `transcribe_memo` and any future write-like tools. Cloud sandboxes (ChatGPT, Claude.ai) cannot reach Voice Memos; export the recording and upload it instead.

Restart the AI tool after changing the MCP configuration.

## 3. Grant Full Disk Access

Open **System Settings → Privacy & Security → Full Disk Access**.

- A desktop app (Codex, WorkBuddy, Claude desktop): enable that app.
- A CLI (Codex CLI, Claude Code) in Terminal or iTerm: enable **Terminal** or **iTerm**.

Grant access to the application that actually launches the MCP server. Voice Memos itself does not need additional Full Disk Access for this workflow.

If `Voice Memos database not found` remains after granting access, fully quit and reopen the authorized app, then verify that Voice Memos has been opened once and iCloud Voice Memos sync has completed.

## 4. Store the Qwen key safely

Run the included interactive helper. The key is entered invisibly and stored in macOS Keychain, not in this Skill:

```bash
bash <skill-dir>/scripts/setup_keychain.sh
```

The default Keychain service name is device-scoped; read it with `python3 <skill-dir>/scripts/device_context.py keychain-service`. To use a session-only environment variable instead:

```bash
export DASHSCOPE_API_KEY='your-key'
```

Do not put the key in `SKILL.md`, shell history, AI tool configuration, or a transcript file.

## 5. Verify without uploading audio

```bash
python3 <skill-dir>/scripts/transcribe_qwen.py \
  --audio /path/to/one-recording.m4a \
  --dry-run
```

The dry run reports duration, model, and route. It does not read a key, upload audio, or call Qwen.

Then ask your AI tool:

> 用 Apple 语音备忘录转写 Skill，列出最近 5 条；先看苹果转写，再让我确认是否云端转写最新一条。

## Region and endpoints

The default is Alibaba Cloud Model Studio Beijing (`--region cn`). Use `--region intl` only with a Singapore-region key. The Skill uses the legacy public DashScope endpoints because they require no Workspace ID and remain supported; set `DASHSCOPE_BASE_URL` if your account requires a workspace-specific endpoint.

## Data handling

- Short route (≤5 minutes): audio is embedded in one HTTPS request to Qwen.
- Long route (>5 minutes): the file is stored in DashScope temporary storage for up to 48 hours. The temporary flow does not offer immediate manual deletion.
- Transcription result download links expire after 24 hours.
- The source Voice Memo is never changed or deleted.

## Durable job commands

The runner now defaults to direct connections for Alibaba endpoints, leaving unrelated/system proxy settings alone. Use `--network-route proxy` only when the account route requires it.

```bash
python3 <skill-dir>/scripts/transcribe_qwen.py --audio /path/to/recording.m4a --dry-run
python3 <skill-dir>/scripts/transcribe_qwen.py --audio /path/to/recording.m4a --confirm-upload
```

The same audio/settings reuse a completed result without reading a key or making a network call. If a long task already has a task ID, repeat the command to resume querying it. If an upload failed, one deliberate attempt can use `--retry-upload --confirm-upload`. A submission with an uncertain outcome is not automatically repeated; inspect it first. `--job-dir` selects a dedicated directory and rejects a directory belonging to other audio/settings.

Register a normalized result from an earlier run locally:

```bash
python3 <skill-dir>/scripts/transcribe_qwen.py \
  --audio /path/to/recording.m4a --import-result /path/to/normalized-result.json
```

The importer checks model, duration and segment shape; the caller must ensure the old transcript really belongs to that recording. No imported result is claimed to have used a newly supplied hotword list.

For a confirmed, meeting-specific vocabulary, pass a JSON object such as `{"产品标准名": 2}`:

```bash
python3 <skill-dir>/scripts/transcribe_qwen.py \
  --audio /path/to/recording.m4a --hotwords /path/to/hotwords.json --dry-run
```

Weights are 1–5, or 50 for a super hotword; no more than 50 super hotwords or 2000 total entries. For audio up to 5 minutes, add `--filetrans` explicitly when using this file-model-only parameter. Use small relevant vocabularies and compare on authorized future recordings before claiming improved accuracy.

Job manifests, raw long-task responses, normalized transcripts and hashes are stored in the `jobs` path printed by `device_context.py init` (by default `~/.meeting-offline/devices/<device-id>/transcription-jobs/`). They are private local data and excluded from the Skill package. No API key is stored there. A changed hotword list selects a different ASR job; correcting downstream role names or terms should reuse the original ASR result through the meeting workspace instead.
