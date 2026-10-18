#!/usr/bin/env python3
"""Watch a folder for new meeting recordings, transcribe them locally, and tell the user when they are ready.

  setup --folder DIR [--engine funasr|whisper] [--model small] [--dry-run]   turn it on (macOS: a launchd agent)
  remove                                                                      turn it off
  run                                                                         one pass (what the agent runs)
  watch [--every 60]                                                          keep running in this terminal
  status                                                                      the queue

A recording is processed only after it has stopped changing (synced completely), once per content
(SHA-256), one at a time. Only local recognition is used: nothing is uploaded. Who is who, terms and
the minutes are still settled with the user in the AI tool; this only gets the waiting out of the way.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time

import device_context
from file_lock import locked_file

AUDIO = {'.m4a', '.wav', '.mp4'}
LABEL = 'com.meeting-offline.inbox'
SETTLE_S = 20      # a file must look the same for this long, across two passes, before it is read
INTERVAL_S = 120   # the agent also wakes on changes to the folder
ATTEMPTS = 3


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def default_folder():
    home = Path.home()
    for icloud in (home / 'Library' / 'Mobile Documents' / 'com~apple~CloudDocs', home / 'iCloudDrive'):
        if icloud.is_dir():
            return icloud / '会议录音'
    return home / 'Downloads' / '会议录音'


def settings():
    chosen = device_context.minutes_preferences().get('inbox')
    return chosen if isinstance(chosen, dict) and chosen.get('folder') else None


def queue_path():
    return device_context.paths()['data_root'] / 'inbox-queue.json'


def load_queue():
    path = queue_path()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'schema_version': 1, 'device_id': device_context.device_id(), 'items': {}}
    except ValueError:  # an interrupted write; start again rather than stop working
        path.replace(path.with_name(path.name + '.damaged'))
        return {'schema_version': 1, 'device_id': device_context.device_id(), 'items': {}}
    device_context.require_device(data.get('device_id'), path)
    return data


def save_queue(queue):
    device_context.atomic_json(queue_path(), queue)


def notify(title, message):
    """A desktop notification; the file name only, never meeting content."""
    if sys.platform == 'darwin' and shutil.which('osascript'):
        script = f'display notification {json.dumps(message, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)}'
        subprocess.run(['osascript', '-e', script], capture_output=True, timeout=10)
    print(f'[{title}] {message}', file=sys.stderr, flush=True)


def placeholder(path):
    """macOS keeps a not-yet-downloaded iCloud file as '.<name>.icloud'."""
    name = path.name
    return name.startswith('.') and name.endswith('.icloud') and Path(name[1:-7]).suffix.lower() in AUDIO


def candidates(folder):
    for path in sorted(Path(folder).iterdir()):
        if path.is_file() and placeholder(path):
            yield path, True
        elif path.is_file() and not path.name.startswith('.') and path.suffix.lower() in AUDIO:
            yield path, False


def digest(path):
    import hashlib
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def hotword_list():
    """Confirmed terms (and Obsidian names when a vault is set); local recognition keeps them on this computer."""
    import memory
    words = [t['canonical'] for t in memory.read_store(device_context.paths()['memory'])['terms']
             if t.get('active', True) and t.get('confirmed_by_user')]
    if device_context.paths()['vault']:
        import obsidian
        try:
            words += [t['term'] for t in obsidian.vocabulary()['terms'] if t['kind'] != 'tag' and len(t['term']) <= 12]
        except (ValueError, OSError):
            pass
    return list(dict.fromkeys(w for w in words if w and not any(c.isspace() for c in w)))[:300]


def known_recordings():
    """Audio hashes this device already has a workspace for, so a recording handled by hand is not redone."""
    found = set()
    meetings = device_context.paths()['meetings']
    for state in meetings.glob('*/state.json') if meetings.is_dir() else ():
        try:
            found.update(s.get('audio_sha256') for s in json.loads(state.read_text(encoding='utf-8')).get('sources', []))
        except (ValueError, OSError):
            continue
    return found - {None}


def prepare_workspace(audio, result, sha):
    """Speaker cards ready for the user: a workspace under this device's meetings folder, nothing confirmed."""
    import prepare_speakers
    import workspace
    root = device_context.paths()['meetings'] / ('inbox-' + sha[:12])
    if (root / 'exports' / 'latest.json').exists():  # written last by workspace.initialize
        return root
    if root.exists():  # an earlier run stopped half way through creating it; only this inbox ever writes here
        shutil.rmtree(root)
    state = prepare_speakers.prepare(['1=' + result['result_path']])
    state_path = device_context.paths()['data_root'] / 'inbox-states' / (sha[:12] + '.json')
    workspace.write(state_path, state)
    workspace.initialize(state_path, root, {'1': audio})
    return root


def process(item, config):
    import transcribe_local
    audio = Path(item['path'])
    result = transcribe_local.transcribe(audio, config.get('engine', 'funasr'), config.get('model'),
                                         hotwords=hotword_list())
    root = prepare_workspace(audio, result, item['sha256'])
    item.update(status='ready', result_path=result['result_path'], workspace=str(root),
                speaker_separation=result['speaker_separation'], speakers=result['speakers_detected'],
                coverage=result['speaker_coverage'], finished_at=now())
    extra = {'voice': f'{result["speakers_detected"]} 位发言人', 'partial': f'只分出 {round(result["speaker_coverage"] * 100)}% 的说话人',
             'none': '没有分出说话人'}[result['speaker_separation']]
    notify('会议录音已转写', f'{audio.name} 已转写好（{extra}）。回到 AI 工具说“整理最新的会议录音”，由它检查真正影响结论的疑点。')


def arrival(key):
    return {'path': key, 'name': Path(key).name, 'first_seen': now(), 'status': 'syncing'}


def run_once(folder=None, clock=time.time):
    """One pass: notice new files, wait until they settle, de-duplicate, then process one queued recording."""
    config = settings() or {}
    folder = Path(folder or config.get('folder') or default_folder()).expanduser()
    if not folder.is_dir():
        return {'folder': str(folder), 'note': '收件文件夹不存在'}
    lock = device_context.paths()['data_root'] / 'inbox.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        with locked_file(lock, blocking=False, busy_error=Busy()):
            return one_pass(folder, config, clock)
    except Busy:  # another pass (the agent, or watch in a terminal) is already on it
        return {**status(), 'note': '另一次处理正在进行，这次跳过。'}


class Busy(Exception):
    pass


def one_pass(folder, config, clock):
    queue = load_queue()
    items = queue['items']
    for item in items.values():  # this pass holds the lock, so a "transcribing" item is from a run that died
        if item.get('status') == 'transcribing':
            item.update(status='failed', error='上次处理被中断（电脑休眠或进程被结束），会自动重试。')
    # a recording that failed or went away does not block the same audio arriving under another name
    seen = {i['sha256'] for i in items.values() if i.get('sha256') and i['status'] in ('queued', 'transcribing', 'ready')} | known_recordings()
    try:
        entries = list(candidates(folder))
    except PermissionError:
        if not queue.get('permission_denied'):  # once, not every two minutes
            notify('会议录音', f'没有权限读取“{folder.name}”文件夹。请在 系统设置 → 隐私与安全性 → 文件与文件夹 里允许 Python 访问。')
            queue['permission_denied'] = now(); save_queue(queue)
        return {'folder': str(folder), 'error': 'permission'}
    queue.pop('permission_denied', None)
    for path, cloud in entries:
        key = str(path if not cloud else path.with_name(path.name[1:-7]))
        item = items.setdefault(key, arrival(key))
        if cloud:  # ask macOS to download it; it is read on a later pass
            if item['status'] == 'syncing':
                if shutil.which('brctl'):
                    subprocess.run(['brctl', 'download', key], capture_output=True, timeout=30)
                item['note'] = '等待 iCloud 下载'
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        fingerprint = [stat.st_size, int(stat.st_mtime)]
        if item['status'] == 'gone' or (item['status'] not in ('syncing', 'transcribing') and item.get('fingerprint') not in (None, fingerprint)):
            # a different file under a name seen before (语音备忘录 reuses “新录音 3”), or one put back
            if item['status'] == 'ready':
                items[key + '#' + item['sha256'][:12]] = item
            item = items[key] = arrival(key)
        if item['status'] != 'syncing':
            continue
        item.pop('note', None)
        if item.get('fingerprint') != fingerprint:
            item.update(fingerprint=fingerprint, steady_since=clock())  # still being written or synced; look again later
            continue
        # copies keep the original modification time, so also time how long the file has looked the same
        if clock() - item.get('steady_since', 0) < SETTLE_S or clock() - stat.st_mtime < SETTLE_S:
            continue
        try:
            sha = digest(path)
        except FileNotFoundError:
            continue
        if sha in seen:
            item.update(status='duplicate', sha256=sha)
            continue
        seen.add(sha)
        item.update(status='queued', sha256=sha, queued_at=now())
    for key, item in items.items():  # a file that disappeared before it was done (moved, deleted)
        if item['status'] in ('syncing', 'queued', 'failed') and not Path(key).exists() and not any(
                str(p.with_name(p.name[1:-7])) == key for p, c in entries if c):
            item['status'] = 'gone'
    save_queue(queue)
    todo = next((i for i in items.values() if i['status'] == 'queued' or (i['status'] == 'failed' and i.get('attempts', 0) < ATTEMPTS)), None)
    if todo:
        todo.update(status='transcribing', started_at=now(), attempts=todo.get('attempts', 0) + 1)
        save_queue(queue)
        try:
            process(todo, config)
        except Exception as error:  # report and retry later; the queue must survive any failure
            todo.update(status='failed', error=str(error)[:400])
            if todo['attempts'] >= ATTEMPTS:
                notify('会议录音处理失败', f'{todo["name"]} 试了 {ATTEMPTS} 次都没成功：{str(error)[:80]}')
        save_queue(queue)
    return status(queue)


def status(queue=None):
    queue = queue or load_queue()
    items = sorted(queue['items'].values(), key=lambda i: i.get('first_seen', ''), reverse=True)
    for item in items:
        record = None
        if item.get('workspace') and (Path(item['workspace']) / 'auto-minutes').is_dir():
            import auto_minutes
            try:
                record = auto_minutes.status(item['workspace'])
                item.update(pipeline_state=record['pipeline_state'], content_state=record['content_state'], open_issues=len(record.get('issues') or []))
            except (ValueError, OSError, KeyError): item.update(pipeline_state='awaiting_ai', content_state='draft')
        if item.get('status') in ('ready', 'done') and item.get('workspace') and (Path(item['workspace']) / 'publication.json').exists():
            import workspace
            try:
                latest = workspace.current_exports(item['workspace']) or {}
                publication = workspace.read(Path(item['workspace']) / 'publication.json')
                automatic = latest.get('automatic_minutes') or record is not None
                current = not automatic or bool(record and record.get('finalization') and publication.get('export_version') == latest.get('version') and any(files.get('html') for files in latest.get('files', {}).values()))
            except (ValueError, OSError, KeyError): current = False
            item['status'] = 'done' if current else 'ready'
    return {'settings': settings(), 'items': items,
            'ready': [i for i in items if i.get('status') == 'ready']}


def plist(folder, script=None):
    paths = device_context.paths()
    return {'Label': LABEL, 'ProgramArguments': [sys.executable, str(script or Path(__file__).resolve()), 'run'],
            'WatchPaths': [str(folder)], 'StartInterval': INTERVAL_S, 'RunAtLoad': True,
            'ProcessType': 'Background', 'LowPriorityIO': True, 'Nice': 10,
            'StandardOutPath': str(paths['data_root'] / 'inbox.log'), 'StandardErrorPath': str(paths['data_root'] / 'inbox.log'),
            'EnvironmentVariables': {**{k: v for k, v in os.environ.items() if k.startswith(('MEETING_OFFLINE_', 'MEETING_MINUTES_'))},
                                     'PATH': search_path()}}


def search_path():
    """launchd starts agents with a bare PATH; ffmpeg (for m4a) usually lives in Homebrew's folders."""
    parts = os.environ.get('PATH', '').split(os.pathsep) + ['/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin']
    return os.pathsep.join(dict.fromkeys(p for p in parts if p))


def agent_path():
    return Path.home() / 'Library' / 'LaunchAgents' / (LABEL + '.plist')


def setup(folder, engine='funasr', model=None, dry_run=False):
    import obsidian
    import transcribe_local
    folder = Path(folder).expanduser()
    missing = transcribe_local.installed(engine)
    if missing and not dry_run:
        raise ValueError(f'本地识别引擎 {engine} 还没装好（缺少 {", ".join(missing)}）；先按 transcribe_local.py --check 安装。')
    agent = plist(folder)
    if dry_run:
        return {'folder': str(folder), 'engine': engine, 'python': sys.executable, 'agent': agent}
    folder.mkdir(parents=True, exist_ok=True)
    obsidian.write_preference({'inbox': {'folder': str(folder), 'engine': engine, 'model': model, 'local_only': True}})
    if sys.platform == 'darwin':
        target = agent_path(); target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('wb') as handle:
            plistlib.dump(agent, handle)
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}', str(target)], capture_output=True)
        for _ in range(10):  # bootout finishes in the background; bootstrap fails (error 5) until it has
            loaded = subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(target)], capture_output=True, text=True)
            if not loaded.returncode:
                break
            time.sleep(1)
        else:
            raise ValueError('后台任务没能启动：' + (loaded.stderr.strip() or '未知原因'))
        return {'folder': str(folder), 'engine': engine, 'python': sys.executable, 'agent': str(target), 'mode': 'launchd'}
    return {'folder': str(folder), 'engine': engine, 'python': sys.executable, 'mode': 'manual',
            'note': '这台电脑不是 macOS：用 inbox.py watch 在终端里运行，或用系统的计划任务定时运行 inbox.py run。'}


def remove():
    import obsidian
    obsidian.write_preference({'inbox': None})
    target = agent_path()
    if sys.platform == 'darwin' and target.exists():
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}', str(target)], capture_output=True)
        target.unlink()
    return {'removed': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('setup'); s.add_argument('--folder', default=None); s.add_argument('--engine', choices=('funasr', 'whisper'), default='funasr')
    s.add_argument('--model'); s.add_argument('--dry-run', action='store_true')
    sub.add_parser('remove'); sub.add_parser('run'); sub.add_parser('status')
    w = sub.add_parser('watch'); w.add_argument('--every', type=int, default=60)
    args = parser.parse_args()
    try:
        if args.command == 'setup':
            result = setup(args.folder or default_folder(), args.engine, args.model, args.dry_run)
        elif args.command == 'remove':
            result = remove()
        elif args.command == 'status':
            result = status()
        elif args.command == 'watch':
            while True:
                print(summary(run_once()), flush=True); time.sleep(max(15, args.every))
        else:
            trim_log()
            print(summary(run_once()))  # launchd appends this to inbox.log on every pass: one line, not the queue
            return
    except (ValueError, OSError) as error:
        parser.exit(2, str(error) + '\n')
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2); print()


def summary(result):
    counts = {}
    for item in result.get('items', []):
        counts[item['status']] = counts.get(item['status'], 0) + 1
    extra = result.get('note') or result.get('error') or ''
    return f'{now()} ' + ' '.join(f'{k}={v}' for k, v in sorted(counts.items())) + (f' {extra}' if extra else '')


def trim_log(limit=1 << 20):
    log = device_context.paths()['data_root'] / 'inbox.log'
    try:
        if log.stat().st_size > limit:
            log.replace(log.with_name('inbox.log.old'))
    except OSError:
        pass


if __name__ == '__main__':
    main()
