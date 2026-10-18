"""Bounded JSON interface; production Codex spawning remains deliberately unverified/disabled."""
from __future__ import annotations
import json

MAX_OUTPUT_BYTES = 1_000_000
MAX_DEPTH = 20

class AIUnavailable(RuntimeError):
    def __init__(self, code, message, *, not_started=True):
        super().__init__(message); self.code = code; self.not_started = not_started

class InvalidOutput(ValueError):
    pass


def bounded_json(value):
    if isinstance(value, (str, bytes)):
        raw = value.encode('utf-8') if isinstance(value, str) else value
        if len(raw) > MAX_OUTPUT_BYTES: raise InvalidOutput('输出超过大小限制。')
        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result: raise ValueError('Duplicate JSON key')
                result[key] = item
            return result
        try:
            value = json.loads(raw, object_pairs_hook=unique,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
        except (ValueError, UnicodeError, RecursionError) as error:
            raise InvalidOutput('输出不是有效、唯一键的 JSON。') from error
    try:
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')) > MAX_OUTPUT_BYTES:
            raise InvalidOutput('输出超过大小限制。')
    except (TypeError, ValueError, RecursionError) as error:
        raise InvalidOutput('输出不是受支持的 JSON。') from error
    def walk(item, depth=0):
        if depth > MAX_DEPTH: raise InvalidOutput('输出嵌套过深。')
        if isinstance(item, dict):
            if any(not isinstance(k, str) for k in item): raise InvalidOutput('对象键必须是文字。')
            for v in item.values(): walk(v, depth + 1)
        elif isinstance(item, list):
            for v in item: walk(v, depth + 1)
        elif item is not None and type(item) not in (str, bool, int, float):
            raise InvalidOutput('输出包含未知对象。')
    walk(value)
    return value


class CodexJsonRunner:
    provenance = {'provider': 'codex-cli', 'capability': 'unverified', 'live_verified': False}
    def generate(self, stage, request):
        raise AIUnavailable('capability_unverified', '尚未证明当前 Codex CLI 的完整工具/配置隔离；未启动进程。请由当前宿主助手通过 prepare/submit 继续。')
