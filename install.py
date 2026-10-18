#!/usr/bin/env python3
"""Install the integrated skill locally; initialize only this device's data."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skills-dir', type=Path, help='Override the local skills directory')
    args = parser.parse_args()
    if sys.version_info < (3, 9):parser.exit(1, '需要 Python 3.9 或更新版本。\n')
    bundle = Path(__file__).resolve().parent
    source = bundle / 'meeting-minutes'
    manifest = json.loads((bundle / 'MANIFEST.json').read_text(encoding='utf-8'))
    expected = manifest['files']
    for relative, digest in expected.items():
        path = (bundle / relative).resolve()
        if not path.is_relative_to(source.resolve()) or not path.is_file():
            parser.exit(1, '包内文件清单无效。\n')
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            parser.exit(1, '文件校验失败：' + relative + '\n')
    actual = {p.relative_to(bundle).as_posix() for p in source.rglob('*') if p.is_file()
              and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    if actual != set(expected):parser.exit(1, '包内文件与清单不一致。\n')
    codex_home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()
    destination = (args.skills_dir or codex_home / 'skills').expanduser().resolve() / 'meeting-minutes'
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != destination:
        with tempfile.TemporaryDirectory(prefix='.meeting-skill-', dir=str(destination.parent)) as temporary:
            staged = Path(temporary) / 'meeting-minutes'
            shutil.copytree(source, staged, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            if destination.exists():
                backups = destination.parent.parent / 'skill-backups'
                backups.mkdir(exist_ok=True)
                backup = backups / ('meeting-minutes-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
                destination.rename(backup)
                print('旧技能备份：' + str(backup))
            staged.rename(destination)
    # No API-key access, upload, MCP installation or system-setting changes.
    subprocess.run([sys.executable, str(destination / 'scripts/device_context.py'), 'init'], check=True)
    print('安装完成：' + str(destination))
    print('在 Codex 中重新载入技能，然后使用 $meeting-minutes。Qwen 密钥需在本机配置。')


if __name__ == '__main__':main()
