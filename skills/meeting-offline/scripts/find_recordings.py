#!/usr/bin/env python3
"""List recent local recordings in iCloud Drive, Downloads and Desktop. Read-only; no network."""
import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

import audio_duration

AUDIO = {'.m4a', '.wav', '.mp4'}
SKIP_DIRS = {'node_modules', '__pycache__', 'Library', 'Applications', '$RECYCLE.BIN'}


def default_locations(home=None):
    """(label, folder) pairs that exist on this computer, in display order."""
    home = Path(home or Path.home())
    candidates = [
        ('iCloud 云盘', home / 'Library' / 'Mobile Documents' / 'com~apple~CloudDocs'),  # macOS
        ('iCloud 云盘', home / 'iCloudDrive'),  # iCloud for Windows
        ('下载', home / 'Downloads'),
        ('桌面', home / 'Desktop'),
        ('桌面', home / 'OneDrive' / 'Desktop'),  # Windows desktop redirected to OneDrive
        ('桌面', home / 'OneDrive' / '桌面'),
    ]
    seen, result = set(), []
    for label, folder in candidates:
        if folder.is_dir() and folder.resolve() not in seen:
            seen.add(folder.resolve()); result.append((label, folder))
    return result


def cloud_placeholder(path):
    """macOS keeps evicted iCloud files as '.<name>.icloud'; returns the real name, or None."""
    name = path.name
    if name.startswith('.') and name.endswith('.icloud'):
        real = name[1:-len('.icloud')]
        return real if Path(real).suffix.lower() in AUDIO else None
    return None


def walk(folder, depth, excluded):
    """Files under folder up to depth levels, skipping hidden folders, bundles and excluded trees."""
    stack = [(folder, 0)]
    while stack:
        current, level = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    if (level < depth and not entry.name.startswith('.') and entry.name not in SKIP_DIRS
                            and path.suffix not in ('.app', '.photoslibrary', '.musiclibrary')
                            and path.resolve() not in excluded):
                        stack.append((path, level + 1))
                elif entry.is_file(follow_symlinks=False):
                    yield path, entry.stat()
            except OSError:
                continue


def find(days=30, limit=20, extra=(), depth=4, home=None, excluded=()):
    since = datetime.now() - timedelta(days=days)
    locations = default_locations(home) + [('指定位置', Path(p).expanduser()) for p in extra]
    excluded = {Path(p).resolve() for p in excluded}
    found = {}
    for label, folder in locations:
        for path, stat in walk(folder, depth, excluded):
            real = cloud_placeholder(path)
            if not real and path.suffix.lower() not in AUDIO:
                continue
            modified = datetime.fromtimestamp(stat.st_mtime)
            if modified < since:
                continue
            target = path.with_name(real) if real else path
            found.setdefault(target.resolve(), {
                'path': str(target), 'name': target.name, 'location': label,
                'modified': modified.isoformat(timespec='minutes'),
                'size_mb': None if real else round(stat.st_size / 1048576, 1),
                'downloaded': not real})
    items = sorted(found.values(), key=lambda item: item['modified'], reverse=True)[:limit]
    for item in items:
        item['duration_seconds'] = None
        if item['downloaded']:
            try:
                item['duration_seconds'] = round(audio_duration.duration(item['path']))
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                pass  # an unreadable length leaves the field empty; the file is still listed
    return {'searched': [{'location': label, 'path': str(folder)} for label, folder in locations],
            'since': since.date().isoformat(), 'recordings': items}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=30, help='只列出最近多少天修改过的文件（默认 30）')
    parser.add_argument('--limit', type=int, default=20, help='最多列出多少条（默认 20）')
    parser.add_argument('--dir', action='append', default=[], help='额外搜索的文件夹，可重复')
    parser.add_argument('--depth', type=int, default=4, help='向下搜索的层数（默认 4）')
    args = parser.parse_args()
    try:
        import device_context
        archive = [device_context.paths()['archive']]  # already archived meetings are not "new" recordings
    except (ValueError, RuntimeError, OSError):
        archive = []
    result = find(args.days, args.limit, args.dir, args.depth, excluded=archive)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2); print()


if __name__ == '__main__':
    main()
