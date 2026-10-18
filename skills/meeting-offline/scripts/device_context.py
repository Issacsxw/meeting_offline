#!/usr/bin/env python3
"""Local, hashed device identity and device-scoped meeting data. No network."""
from __future__ import annotations
import argparse
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


@lru_cache(maxsize=1)
def system_identity():
    if sys.platform == 'darwin':
        value = subprocess.run(['/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'],
                               capture_output=True, text=True, check=True, timeout=10).stdout
        match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', value)
        if match:
            return 'macos:' + match.group(1).strip().lower()
    elif sys.platform == 'win32':
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography',
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            return 'windows:' + str(winreg.QueryValueEx(key, 'MachineGuid')[0]).strip().lower()
    else:
        for name in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
            path = Path(name)
            if path.is_file() and path.read_text(encoding='utf-8').strip():
                return 'linux:' + path.read_text(encoding='utf-8').strip().lower()
    raise RuntimeError('无法读取稳定设备 ID；请为本机单独设置 MEETING_OFFLINE_DEVICE_ID，不能沿用其他设备的值。'
                       '在 ChatGPT、Claude.ai 等云端沙箱中改用 MEETING_OFFLINE_SANDBOX=1。')


def env(name):
    """MEETING_OFFLINE_<name>, falling back to the MEETING_MINUTES_<name> used before the rename."""
    return (os.environ.get('MEETING_OFFLINE_' + name) or os.environ.get('MEETING_MINUTES_' + name) or '').strip()


def sandbox():
    """Cloud sandboxes (ChatGPT, Claude.ai uploads) have no stable machine or persistent home."""
    return env('SANDBOX').lower() in ('1', 'true', 'yes')


def device_id():
    override = env('DEVICE_ID')
    if override:
        raw = 'manual:' + override
    elif sandbox():
        # One shared identity for all sandbox sessions, so a downloaded memory.json can be uploaded
        # again later; it never matches a real computer, so local data is not mixed in.
        raw = 'sandbox:v1'
    else:
        raw = system_identity()
    # The namespace keeps its pre-rename value so existing devices keep their IDs and data.
    return hashlib.sha256(('meeting-minutes:v1:' + raw).encode()).hexdigest()[:24]


def data_home():
    if env('DATA_HOME'):
        return Path(env('DATA_HOME'))
    if sandbox():
        return Path.cwd() / 'meeting-offline-data'
    # Shared by every local AI tool on this computer. Existing data stays where it is:
    # 11.4 used ~/.meeting-minutes, earlier versions ~/.codex/meeting-minutes-memory.
    current = Path.home() / '.meeting-offline'
    for legacy in (Path.home() / '.meeting-minutes', Path.home() / '.codex' / 'meeting-minutes-memory'):
        if not current.is_dir() and legacy.is_dir():
            return legacy
    return current


def paths():
    identity = device_id()
    base = data_home().expanduser().resolve()
    profile_dir = base / 'devices' / identity
    profile_path = profile_dir / 'device.json'
    profile = json.loads(profile_path.read_text(encoding='utf-8')) if profile_path.exists() else {}
    if profile and profile.get('device_id') != identity:
        raise ValueError('设备档案归属不匹配，不读取其中的数据路径。')
    data_root = Path(profile.get('data_root') or profile_dir).expanduser().resolve()
    legacy = profile.get('legacy_root') == str(base) and data_root == base
    if data_root != profile_dir and not legacy:
        raise ValueError('设备档案包含未经绑定的数据目录。')
    default_archive = base / '会议纪要' if sandbox() else Path.home() / 'Documents' / '会议纪要' / identity
    archive = Path(profile.get('archive_root') or default_archive).expanduser().resolve()
    return {'device_id': identity, 'sandbox': sandbox(), 'base': base, 'profile_dir': profile_dir,
            'profile': profile_path, 'data_root': data_root, 'legacy': legacy,
            'memory': data_root / 'memory.json', 'preferences': data_root / 'preferences.json',
            'meetings': data_root / 'meetings', 'jobs': data_root / 'transcription-jobs',
            'archive': archive,
            'keychain_service': profile.get('keychain_service') or 'codex-dashscope-api-key-' + identity}


def require_device(stored_id, path=None):
    current = paths()
    if stored_id == current['device_id']:
        return
    if not stored_id and path is not None and current['legacy']:
        path = Path(path).expanduser().resolve()
        if path.is_relative_to(current['data_root']) or path.is_relative_to(current['archive']):
            return
    raise ValueError('数据属于其他设备或未绑定设备；本机需从空库开始，不能自动沿用。')


def atomic_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.device-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2); handle.write('\n')
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def initialize():
    current = paths()
    for key in ('profile_dir', 'meetings', 'jobs'):
        current[key].mkdir(parents=True, exist_ok=True)
    if not current['profile'].exists():
        atomic_json(current['profile'], {'schema_version': 1, 'device_id': current['device_id']})
    if not current['memory'].exists():
        atomic_json(current['memory'], {'schema_version': 1, 'device_id': current['device_id'],
                                       'roles': [], 'terms': [], 'history': []})
    else:
        require_device(json.loads(current['memory'].read_text(encoding='utf-8')).get('device_id'), current['memory'])
    if not current['preferences'].exists():
        atomic_json(current['preferences'], {'schema_version': 1, 'device_id': current['device_id'],
            'meeting_offline': {'theme': 'warm',
                'default_outputs': ['editable-html', 'png'],
                'archive': {'root': str(current['archive']), 'audio_action': 'move'}}})
    else:
        require_device(json.loads(current['preferences'].read_text(encoding='utf-8')).get('device_id'), current['preferences'])
    return current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['show', 'init', 'keychain-service'], nargs='?', default='show')
    args = parser.parse_args()
    if args.command == 'keychain-service':
        print(paths()['keychain_service']); return
    result = initialize() if args.command == 'init' else paths()
    print(json.dumps({k: str(v) if isinstance(v, Path) else v for k, v in result.items()}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:main()
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr); sys.exit(1)
