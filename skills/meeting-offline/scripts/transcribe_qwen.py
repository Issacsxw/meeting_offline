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
    pass


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
            raise TranscriptionError("Duration must be greater than zero.")
        return supplied
    try:
        value = audio_duration.duration(audio)
        if not math.isfinite(value) or value <= 0:raise ValueError('Invalid audio duration.')
        return value
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise TranscriptionError(str(exc)) from exc


def choose_route(duration_seconds: float) -> tuple[str, str]:
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise TranscriptionError("Duration must be finite and greater than zero.")
    if duration_seconds > MAX_DURATION_SECONDS:
        raise TranscriptionError("Audio exceeds 12 hours; auto-splitting is disabled.")
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


def get_api_key() -> str:
    env_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if env_key:
        return env_key
    if sys.platform != 'darwin':
        raise TranscriptionError('No API key found. Configure DASHSCOPE_API_KEY locally on this device.')
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
            f"No API key found. Run setup_keychain.sh or set DASHSCOPE_API_KEY "
            f"(Keychain service: {service})."
        ) from exc
    key = result.stdout.strip()
    if not key:
        raise TranscriptionError("The Keychain item exists but contains an empty key.")
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
        raise TranscriptionError(f"HTTP {exc.code} from Qwen: {body[:800]}") from exc
    except urllib.error.URLError as exc:
        raise TranscriptionError(f"Network request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise TranscriptionError("Service returned invalid JSON.") from exc


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
        raise TranscriptionError(f"Upload policy missing data: {response}")
    return data


def upload_temporary(audio: Path, policy: dict[str, Any]) -> str:
    curl = shutil.which('curl')
    if not curl:raise TranscriptionError('curl is required for private temporary upload; nothing uploaded.')
    key_path = f"{policy['upload_dir'].rstrip('/')}/{audio.name}"
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
    command[1:1] = ["--connect-timeout", "15", "--max-time", "300"]
    if direct_aliyun(policy["upload_host"]):
        command[1:1] = ["--noproxy", "*"]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise TranscriptionError(f"Temporary upload failed: {exc.stderr[:800]}") from exc
    return f"oss://{key_path}"


def poll_task(
    task_id: str,
    key: str,
    base_url: str,
    poll_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    if poll_seconds < 0.5:
        raise TranscriptionError("--poll-seconds must be at least 0.5.")
    if timeout_seconds <= 0:
        raise TranscriptionError("--timeout-seconds must be greater than zero.")
    deadline = time.monotonic() + timeout_seconds
    while True:
        response = request_json(f"{base_url}/tasks/{task_id}", key=key)
        output = response.get("output", {})
        status = output.get("task_status") if isinstance(output, dict) else None
        if status in TERMINAL_TASK_STATES:
            if status != "SUCCEEDED":
                raise TranscriptionError(f"Qwen task ended with {status}: {response}")
            return response
        if time.monotonic() >= deadline:
            raise TranscriptionError(
                f"Timed out waiting for task {task_id}; no automatic retry was started."
            )
        time.sleep(poll_seconds)


def normalize_long(
    transcript: dict[str, Any], task_response: dict[str, Any], duration_seconds: float
) -> dict[str, Any]:
    segments = []
    full_text = []
    transcripts = transcript.get("transcripts", [])
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


def transcribe_long(
    audio: Path,
    duration_seconds: float,
    key: str,
    base_url: str,
    region: str,
    *,
    diarization: bool,
    keep_dialect: bool,
    poll_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    policy = get_upload_policy(key, region, LONG_MODEL)
    oss_url = upload_temporary(audio, policy)
    payload = {
        "model": LONG_MODEL,
        "input": {"file_urls": [oss_url]},
        "parameters": {
            "channel_id": [0],
            "language_hints": ["zh"],
            "diarization_enabled": diarization,
            "keep_dialect": keep_dialect,
        },
    }
    submitted = request_json(
        f"{base_url}/services/audio/asr/transcription",
        key=key,
        method="POST",
        payload=payload,
        extra_headers={
            "X-DashScope-Async": "enable",
            "X-DashScope-OssResourceResolve": "enable",
        },
    )
    output = submitted.get("output", {})
    task_id = output.get("task_id") if isinstance(output, dict) else None
    if not task_id:
        raise TranscriptionError(f"Task submission returned no task_id: {submitted}")
    task_response = poll_task(
        task_id, key, base_url, poll_seconds, timeout_seconds
    )
    task_output = task_response.get("output", {})
    results = task_output.get("results", []) if isinstance(task_output, dict) else []
    succeeded = [
        item
        for item in results
        if isinstance(item, dict) and item.get("subtask_status") == "SUCCEEDED"
    ]
    if len(succeeded) != 1 or not succeeded[0].get("transcription_url"):
        raise TranscriptionError(f"Long transcription subtask failed: {task_response}")
    transcript = request_json(succeeded[0]["transcription_url"], timeout=120.0)
    return normalize_long(transcript, task_response, duration_seconds)


def main() -> int:
    import qwen_job
    global NETWORK_ROUTE
    args = parse_args()
    NETWORK_ROUTE = args.network_route
    try:
        result = qwen_job.execute(args, sys.modules[__name__])
    except (ValueError, OSError) as exc:
        raise TranscriptionError(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TranscriptionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
