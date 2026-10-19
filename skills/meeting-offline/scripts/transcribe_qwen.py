#!/usr/bin/env python3
"""Route one local audio file to the appropriate Qwen ASR model."""

from __future__ import annotations

import argparse
import base64
import json
import math
import mimetypes
import shutil
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
import device_context
import audio_duration


SHORT_MODEL = "qwen-audio-3.1-asr-flash"
LONG_MODEL = "qwen-audio-3.1-asr-flash-filetrans"
KEYCHAIN_SERVICE = device_context.paths()['keychain_service']
SHORT_LIMIT_SECONDS = 5 * 60
DIARIZATION_LIMIT_SECONDS = 2 * 60 * 60
MAX_DURATION_SECONDS = 12 * 60 * 60
MAX_TEMP_UPLOAD_BYTES = 1024 * 1024 * 1024
NETWORK_ROUTE = "direct"
TERMINAL_TASK_STATES = {"SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"}


class TranscriptionError(RuntimeError):
    """`not_processed` marks failures where the provider certainly did not run the request, so a retry is safe."""

    def __init__(self, message, *, not_processed=False):
        super().__init__(message)
        self.not_processed = not_processed


HTTP_HINTS = {
    400: '请求参数或音频格式不被接受',
    401: 'API Key 无效；请检查密钥是否复制完整，或重新创建',
    403: '没有权限：请确认已在百炼控制台开通该模型，且 Key 与地域一致（北京用默认，新加坡加 --region intl）',
    404: '接口或任务不存在',
    429: '请求过于频繁或额度已用完；稍后再试，或在百炼控制台查看额度',
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcribe one audio file with Qwen; never modifies the source."
    )
    parser.add_argument("--audio", required=True, help="Path to exactly one audio file")
    parser.add_argument("--duration-seconds", type=float, help="Explicit trusted duration override")
    parser.add_argument("--region", choices=("cn", "intl"), default="cn")
    parser.add_argument("--dry-run", action="store_true", help="Plan only; no key or network")
    parser.add_argument(
        "--confirm-upload",
        action="store_true",
        help="Required acknowledgement that this one file may be sent to Qwen",
    )
    parser.add_argument(
        "--confirm-over-2h",
        action="store_true",
        help="Required for diarization above Alibaba's recommended 2-hour limit",
    )
    parser.add_argument(
        "--confirm-channel-0", action="store_true",
        help="Accept possible loss of other channels/tracks when filetrans selects only channel 0",
    )
    parser.add_argument("--no-diarization", action="store_true")
    parser.add_argument("--keep-dialect", action="store_true")
    parser.add_argument(
        "--poll-seconds", type=float, default=2.0, help="Long-task polling interval"
    )
    parser.add_argument(
        "--timeout-seconds", type=float, default=1800.0, help="Bounded long-task wait"
    )
    parser.add_argument("--job-dir", help="Persistent directory for this exact recording/settings")
    parser.add_argument("--hotwords", help="JSON object of confirmed hotwords and weights")
    parser.add_argument("--filetrans", action="store_true", help="Explicitly use file transcription for short audio/hotwords")
    parser.add_argument("--network-route", choices=("direct", "proxy"), default="direct")
    parser.add_argument("--retry-upload", action="store_true", help="One explicit retry of a failed upload")
    parser.add_argument("--import-result", help="Register a prior normalized result locally; never uploads")
    return parser.parse_args()


def get_duration(audio: Path, supplied: float | None = None) -> float:
    if supplied is not None:
        if not math.isfinite(supplied) or supplied <= 0:
            raise TranscriptionError("时长必须大于 0。")
        return supplied
    try:
        value = audio_duration.duration(audio)
        if not math.isfinite(value) or value <= 0:raise ValueError('Invalid audio duration.')
        return value
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise TranscriptionError(str(exc)) from exc


def choose_route(duration_seconds: float) -> tuple[str, str]:
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise TranscriptionError("时长必须大于 0。")
    if duration_seconds > MAX_DURATION_SECONDS:
        raise TranscriptionError("录音超过 12 小时，不会自动切分；请先剪成较短的几段。")
    if duration_seconds <= SHORT_LIMIT_SECONDS:
        return "direct_base64", SHORT_MODEL
    return "temporary_storage", LONG_MODEL


def region_base(region: str) -> str:
    override = os.environ.get("DASHSCOPE_BASE_URL", "").rstrip("/")
    if override:
        return override
    if region == "intl":
        return "https://dashscope-intl.aliyuncs.com/api/v1"
    return "https://dashscope.aliyuncs.com/api/v1"


NO_KEY = ('没有找到阿里云 DashScope API Key。申请方法见 references/transcription-options.md；'
          '不想申请也可以改用本地识别 scripts/transcribe_local.py。')


def get_api_key() -> str:
    env_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if env_key:
        return env_key
    if sys.platform != 'darwin':
        raise TranscriptionError(NO_KEY + '在本机设置环境变量 DASHSCOPE_API_KEY 后重试。', not_processed=True)
    service = os.environ.get("DASHSCOPE_KEYCHAIN_SERVICE", KEYCHAIN_SERVICE)
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-w", "-s", service],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise TranscriptionError(
            NO_KEY + f"运行 setup_keychain.sh 存入钥匙串，或设置环境变量 DASHSCOPE_API_KEY（钥匙串服务名：{service}）。",
            not_processed=True) from exc
    key = result.stdout.strip()
    if not key:
        raise TranscriptionError("钥匙串里的 API Key 是空的；请重新运行 setup_keychain.sh。", not_processed=True)
    return key


def direct_aliyun(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return NETWORK_ROUTE == "direct" and (host == "aliyuncs.com" or host.endswith(".aliyuncs.com"))


def request_json(
    url: str,
    *,
    key: str | None = None,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    headers = dict(extra_headers or {})
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with (urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout)
              if direct_aliyun(url) else urllib.request.urlopen(request, timeout=timeout)) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        hint = HTTP_HINTS.get(exc.code, '服务暂时出错' if exc.code >= 500 else '请求被拒绝')
        # 4xx means the provider rejected the request without running it.
        raise TranscriptionError(f"阿里云返回 HTTP {exc.code}：{hint}。详情：{body[:500]}",
                                 not_processed=400 <= exc.code < 500 and exc.code != 408) from exc
    except urllib.error.URLError as exc:
        raise TranscriptionError(f"连不上阿里云：{exc.reason}。请检查网络后重试。", not_processed=True) from exc
    except json.JSONDecodeError as exc:
        raise TranscriptionError("阿里云返回的内容无法解析。") from exc


def audio_format(audio: Path) -> str:
    suffix = audio.suffix.lower().lstrip(".")
    return {"mp4": "mp4", "m4a": "m4a", "wave": "wav"}.get(suffix, suffix or "m4a")


def audio_mime(audio: Path) -> str:
    if audio.suffix.lower() == ".m4a":
        return "audio/mp4"
    return mimetypes.guess_type(audio.name)[0] or "application/octet-stream"


def unwrap_short_output(response: dict[str, Any]) -> dict[str, Any]:
    output = response.get("output", {})
    while isinstance(output, dict) and isinstance(output.get("output"), dict):
        output = output["output"]
    return output if isinstance(output, dict) else {}


def normalize_short(
    response: dict[str, Any], duration_seconds: float
) -> dict[str, Any]:
    output = unwrap_short_output(response)
    sentences = output.get("sentences")
    if not isinstance(sentences, list):
        sentence = output.get("sentence")
        sentences = [sentence] if isinstance(sentence, dict) else []
    segments = []
    for item in sentences:
        if not isinstance(item, dict):
            continue
        segments.append(
            {
                "speaker_id": item.get("speaker_id"),
                "start_ms": item.get("begin_time"),
                "end_ms": item.get("end_time"),
                "text": item.get("text", ""),
            }
        )
    text = output.get("text") or "".join(x["text"] for x in segments)
    return {
        "route": "direct_base64",
        "model": SHORT_MODEL,
        "duration_seconds": round(duration_seconds, 3),
        "text": text,
        "segments": segments,
        "usage": response.get("usage", output.get("usage")),
    }


def transcribe_short(
    audio: Path,
    duration_seconds: float,
    key: str,
    base_url: str,
    *,
    diarization: bool,
    keep_dialect: bool,
) -> dict[str, Any]:
    encoded = base64.b64encode(audio.read_bytes()).decode("ascii")
    data_uri = f"data:{audio_mime(audio)};base64,{encoded}"
    parameters: dict[str, Any] = {
        "format": audio_format(audio),
        "speaker_diarization_enabled": diarization,
    }
    if not diarization:
        parameters["keep_dialect"] = keep_dialect
        parameters["language_hints"] = ["zh"]
    payload = {
        "model": SHORT_MODEL,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": data_uri},
                        }
                    ],
                }
            ]
        },
        "parameters": parameters,
    }
    response = request_json(
        f"{base_url}/services/aigc/multimodal-generation/generation",
        key=key,
        method="POST",
        payload=payload,
        extra_headers={"X-DashScope-SSE": "disable"},
        timeout=300.0,
    )
    return normalize_short(response, duration_seconds)


def get_upload_policy(key: str, region: str, model: str) -> dict[str, Any]:
    upload_base = (
        "https://dashscope-intl.aliyuncs.com/api/v1"
        if region == "intl"
        else "https://dashscope.aliyuncs.com/api/v1"
    )
    query = urllib.parse.urlencode({"action": "getPolicy", "model": model})
    response = request_json(f"{upload_base}/uploads?{query}", key=key)
    data = response.get("data")
    if not isinstance(data, dict):
        raise TranscriptionError(f"获取上传凭证失败：{response}", not_processed=True)
    return data


def upload_temporary(audio: Path, policy: dict[str, Any]) -> str:
    curl = shutil.which('curl')
    if not curl:raise TranscriptionError('上传长录音需要 curl；请先安装 curl。录音尚未上传。', not_processed=True)
    # A neutral object name: file names often carry client or project names.
    key_path = f"{policy['upload_dir'].rstrip('/')}/{secrets.token_hex(8)}{audio.suffix.lower()}"
    command = [
        curl,
        "--silent",
        "--show-error",
        "--fail-with-body",
        "--request",
        "POST",
        policy["upload_host"],
        "--form-string",
        f"OSSAccessKeyId={policy['oss_access_key_id']}",
        "--form-string",
        f"Signature={policy['signature']}",
        "--form-string",
        f"policy={policy['policy']}",
        "--form-string",
        f"x-oss-object-acl={policy['x_oss_object_acl']}",
        "--form-string",
        f"x-oss-forbid-overwrite={policy['x_oss_forbid_overwrite']}",
        "--form-string",
        f"key={key_path}",
        "--form-string",
        "success_action_status=200",
        "--form",
        f"file=@{audio}",
    ]
    # Allow at least 100 KB/s for large files instead of a fixed five minutes.
    max_time = max(300, audio.stat().st_size // 100_000)
    command[1:1] = ["--connect-timeout", "15", "--max-time", str(max_time)]
    if direct_aliyun(policy["upload_host"]):
        command[1:1] = ["--noproxy", "*"]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise TranscriptionError(f"录音上传失败：{exc.stderr[:500]}") from exc
    return f"oss://{key_path}"


def poll_task(
    task_id: str,
    key: str,
    base_url: str,
    poll_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    if poll_seconds < 0.5:
        raise TranscriptionError("--poll-seconds 至少为 0.5。")
    if timeout_seconds <= 0:
        raise TranscriptionError("--timeout-seconds 必须大于 0。")
    deadline = time.monotonic() + timeout_seconds
    while True:
        response = request_json(f"{base_url}/tasks/{task_id}", key=key)
        output = response.get("output", {})
        status = output.get("task_status") if isinstance(output, dict) else None
        if status in TERMINAL_TASK_STATES:
            if status != "SUCCEEDED":
                raise TranscriptionError(f"阿里云转写任务结束，状态为 {status}：{response}")
            return response
        if time.monotonic() >= deadline:
            raise TranscriptionError(
                f"等待转写任务 {task_id} 超时。任务仍在阿里云处理，再次运行同一命令会继续查询，不会重新上传。"
            )
        time.sleep(poll_seconds)


def normalize_long(
    transcript: dict[str, Any], task_response: dict[str, Any], duration_seconds: float
) -> dict[str, Any]:
    segments = []
    full_text = []
    transcripts = transcript.get("transcripts", [])
    if isinstance(transcripts, list) and len(transcripts) > 1:
        # Speaker IDs are local to each channel. Flattening channels would merge unrelated people.
        raise TranscriptionError('文件转写返回了多个声道结果，不能合并说话人；原始返回内容已保留，请先检查录音声道。')
    for channel in transcripts if isinstance(transcripts, list) else []:
        if not isinstance(channel, dict):
            continue
        channel_text = channel.get("text")
        if isinstance(channel_text, str):
            full_text.append(channel_text)
        for item in channel.get("sentences", []):
            if not isinstance(item, dict):
                continue
            segments.append(
                {
                    "speaker_id": item.get("speaker_id"),
                    "start_ms": item.get("begin_time"),
                    "end_ms": item.get("end_time"),
                    "text": item.get("text", ""),
                }
            )
    text = "".join(full_text) or "".join(x["text"] for x in segments)
    return {
        "route": "temporary_storage",
        "model": LONG_MODEL,
        "duration_seconds": round(duration_seconds, 3),
        "temporary_audio_retention_hours": 48,
        "result_url_valid_hours": 24,
        "text": text,
        "segments": segments,
        "usage": task_response.get("usage"),
    }


def main() -> int:
    import qwen_job
    global NETWORK_ROUTE
    args = parse_args()
    NETWORK_ROUTE = args.network_route
    try:
        result = qwen_job.execute(args, sys.modules[__name__])
    except (ValueError, OSError) as exc:
        raise TranscriptionError(str(exc)) from exc
    import prepare_speakers
    summary = prepare_speakers.separation(result.get("segments", []))
    result = {**result, "speaker_separation": summary["status"], "speaker_coverage": summary["coverage"],
              "speakers_detected": summary["speakers"]}
    if summary["status"] != "voice":
        print(f"[Qwen] 注意：只有 {round(summary['coverage'] * 100)}% 的发言分出了说话人，其余标为“说话人未分离”。",
              file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TranscriptionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
