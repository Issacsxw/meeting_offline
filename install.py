#!/usr/bin/env python3
"""Install the integrated skill for local AI tools; initialize only this device's data."""
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


HOME = Path.home()
# Where each tool reads user-level skills; `marker` is the folder that shows the tool is installed.
TARGETS = {
    'codex': {'name': 'Codex', 'skills': HOME / '.agents' / 'skills',
              'marker': Path(os.environ.get('CODEX_HOME') or HOME / '.codex'),
              'use': '重新打开 Codex，输入 $meeting-offline 或直接说“整理这份会议录音”。'},
    'claude': {'name': 'Claude Code', 'skills': HOME / '.claude' / 'skills', 'marker': HOME / '.claude',
               'use': '重新打开 Claude Code，输入 /meeting-offline 或直接说“整理这份会议录音”。'},
    'workbuddy': {'name': 'WorkBuddy', 'skills': HOME / '.workbuddy' / 'skills', 'marker': HOME / '.workbuddy',
                  'use': '在 WorkBuddy 新开会话，直接说“用会议纪要技能整理这份录音”。'},
    'codebuddy': {'name': 'CodeBuddy / WorkBuddy 企业版', 'skills': HOME / '.codebuddy' / 'skills',
                  'marker': HOME / '.codebuddy', 'use': '新开会话，直接说“用会议纪要技能整理这份录音”。'},
}
NAME = 'meeting-offline'
# Copies under the old skill name, or in the old Codex location, would show up as a second skill.
OLD_NAMES = ['meeting-minutes']
LEGACY_CODEX_SKILLS = Path(os.environ.get('CODEX_HOME') or HOME / '.codex') / 'skills'


def verify(bundle, source, parser):
    expected = json.loads((bundle / 'MANIFEST.json').read_text(encoding='utf-8'))['files']
    for relative, digest in expected.items():
        path = (bundle / relative).resolve()
        if not path.is_relative_to(source.resolve()) or not path.is_file():
            parser.exit(1, '包内文件清单无效。\n')
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            parser.exit(1, '文件校验失败：' + relative + '\n')
    actual = {p.relative_to(bundle).as_posix() for p in source.rglob('*') if p.is_file()
              and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    if actual != set(expected):
        parser.exit(1, '包内文件与清单不一致。\n')


def backup(path):
    backups = path.parent.parent / 'skill-backups'
    backups.mkdir(exist_ok=True)
    target = backups / (path.name + '-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    path.rename(target)
    print('旧技能备份：' + str(target))


def install(source, skills_dir):
    destination = skills_dir.expanduser().resolve() / NAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != destination:
        with tempfile.TemporaryDirectory(prefix='.meeting-skill-', dir=str(destination.parent)) as temporary:
            staged = Path(temporary) / NAME
            shutil.copytree(source, staged, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            if destination.exists():
                backup(destination)
            staged.rename(destination)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', action='append', choices=[*TARGETS, 'all'],
                        help='安装到哪个工具，可重复；默认安装到本机检测到的全部工具')
    parser.add_argument('--skills-dir', type=Path, help='直接指定 skills 目录（其他工具或自定义位置）')
    args = parser.parse_args()
    if sys.version_info < (3, 9):
        parser.exit(1, '需要 Python 3.9 或更新版本。\n')
    bundle = Path(__file__).resolve().parent
    source = bundle / 'skills' / NAME
    verify(bundle, source, parser)
    if args.skills_dir:
        if args.target:
            parser.error('--skills-dir 与 --target 只能选一个。')
        chosen = [('custom', {'name': str(args.skills_dir), 'skills': args.skills_dir, 'use': '在对应工具中重新载入技能。'})]
    else:
        keys = list(TARGETS) if args.target and 'all' in args.target else args.target or \
            [key for key, target in TARGETS.items() if target['marker'].is_dir()]
        if not keys:
            parser.exit(1, '没有检测到 Codex、Claude Code、WorkBuddy 或 CodeBuddy。请用 --target 指定，'
                           '或用 --skills-dir 指定其他工具的 skills 目录。\n')
        chosen = [(key, TARGETS[key]) for key in dict.fromkeys(keys)]
    installed = []
    for key, target in chosen:
        destination = install(source, target['skills'])
        stale = [destination.parent / old for old in OLD_NAMES]
        if key == 'codex':
            stale += [LEGACY_CODEX_SKILLS / name for name in [NAME, *OLD_NAMES]]
        for path in stale:
            if path.is_dir() and path.resolve() != destination:
                backup(path)
        installed.append((target, destination))
    # No API-key access, upload, MCP installation or system-setting changes.
    subprocess.run([sys.executable, str(installed[0][1] / 'scripts/device_context.py'), 'init'], check=True)
    for target, destination in installed:
        print('已安装到 ' + target['name'] + '：' + str(destination))
        print('  ' + target['use'])
    print('各工具共用本机同一份角色和术语库。Qwen 密钥需在本机单独配置。')


if __name__ == '__main__':
    main()
