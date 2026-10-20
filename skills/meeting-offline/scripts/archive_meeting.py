#!/usr/bin/env python3
"""Move a selected local recording and collect its deliverables (HTML, PNG, Markdown) into one folder."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import date
from file_lock import locked_file
import device_context
import json
from pathlib import Path
import re
import shutil
import workspace as w


DEFAULT_ROOT = device_context.paths()['archive']
# Archived names per deliverable; Markdown links in 会议纪要.md point at these.
NAMES = {'html': '会议纪要.html', 'png': '会议纪要.png', 'md': '会议纪要.md', 'transcript_md': '逐字稿.md', 'insight_md': '深度洞察.md'}


def safe_name(value):
    value = re.sub(r'[\x00-\x1f/\\:*?"<>|]', ' ', str(value)).strip(' .')
    while len(value.encode('utf-8')) > 160:
        value = value[:-1]
    return value or '会议'


def checked_date(value):
    return date.fromisoformat(value).isoformat()


@contextmanager
def archive_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with locked_file(root / '.archive.lock'):
        yield


def unique_path(path, identity):
    candidate = path
    number = 0
    while candidate.exists() or candidate.is_symlink():
        number += 1
        candidate = path.with_name(f'{path.stem}-{identity[:8]}-{number}{path.suffix}')
    return candidate


def managed_folder(root, name):
    path = root / name
    if path.parent.resolve() != root or path.is_symlink():
        raise ValueError('归档索引中的目录不安全。')
    return path


def index_file(root):
    """A shared folder (an Obsidian vault synced between computers) keeps one index per device."""
    vault = device_context.paths()['vault']
    if vault and root.is_relative_to(vault):
        return root / f'.archive-index-{device_context.device_id()[:12]}.json'
    return root / '.archive-index.json'


def collect(audio, *, root=DEFAULT_ROOT, topic=None, meeting_date=None,
            archive_date=None, html=None, png=None, expected_hash=None, extra=None, withdraw=(), _checked_workspace=False):
    audio = Path(audio).expanduser().absolute()
    requested_audio = audio
    missing_audio = not audio.exists()
    if audio.is_symlink() or (not missing_audio and not audio.is_file()) or (missing_audio and not expected_hash):
        raise ValueError('录音必须是存在的普通文件：' + str(audio))
    # Voice Memos app-managed originals are read-only; use an exported local file.
    if '/Library/' in str(audio) and any(s in str(audio) for s in ('VoiceMemos', 'voicememos')):
        raise ValueError('请先只读导出语音备忘录，再归档导出的本地文件。')
    if bool(html) != bool(png):
        raise ValueError('HTML 与 PNG 必须同时提供。')
    documents = {}
    extra = extra or {}
    if set(extra) - set(NAMES) or (extra and not html):
        raise ValueError('附加文件只能是 ' + '、'.join(k for k in NAMES if k not in ('html', 'png')) + '，且需和 HTML/PNG 一起归档。')
    for kind, value in [('html', html), ('png', png), *extra.items()]:
        if value:
            path = Path(value).expanduser().resolve(strict=True)
            documents[kind] = (path, w.file_digest(path))
    if html and not _checked_workspace and 'meeting-speaker-edits' in Path(html).read_text(encoding='utf-8'):
        raise ValueError('工作区纪要必须使用 --workspace --source 归档，以核验内容和发布状态。')
    audio_hash = expected_hash if missing_audio else w.file_digest(audio)
    if expected_hash and audio_hash != expected_hash:
        raise ValueError('录音校验值与工作区记录不一致；未移动文件。')
    root = Path(root).expanduser().resolve()
    with archive_lock(root):
        index_path = index_file(root)
        index = w.read(index_path) if index_path.exists() else {'schema_version': 1, 'device_id': device_context.device_id(), 'recordings': {}}
        device_context.require_device(index.get('device_id'), root)
        index.setdefault('device_id', device_context.device_id())
        entry = index['recordings'].get(audio_hash, {})
        old_folder = managed_folder(root, entry['folder']) if entry else None
        previous = w.read(old_folder / '.archive.json') if old_folder and (old_folder / '.archive.json').exists() else {}
        if previous:
            device_context.require_device(previous.get('device_id'), root)
        if missing_audio:
            # Only reclaim a moved recording from a matching, verified checkpoint.
            # A hash alone is not proof that this missing source belongs to that archive.
            name = previous.get('audio_file')
            recorded_source = previous.get('move_source_path', previous.get('original_audio_path'))
            if (not old_folder or not isinstance(name, str) or not name or
                    Path(name).name != name or name in ('.', '..') or
                    previous.get('audio_sha256') != expected_hash or
                    recorded_source != str(audio)):
                raise ValueError('归档恢复记录与原录音不一致；未移动文件。')
            recovered = old_folder / name
            if (recovered.is_symlink() or not recovered.is_file() or
                    w.file_digest(recovered) != expected_hash):
                raise ValueError('归档恢复录音校验失败；未移动文件。')
            audio = recovered
        topic = topic or previous.get('topic') or audio.stem
        meeting_date = meeting_date or previous.get('meeting_date')
        archive_date = checked_date(archive_date or previous.get('archive_date') or date.today().isoformat())
        prefix = checked_date(meeting_date) if meeting_date else archive_date + '（归档）'
        target_folder = root / safe_name(prefix + '_' + topic)
        if target_folder != old_folder and target_folder.exists():
            target_folder = unique_path(target_folder, audio_hash)
        if old_folder and old_folder.exists() and old_folder != target_folder:
            relative_audio = audio.relative_to(old_folder) if audio.is_relative_to(old_folder) else None
            old_folder.rename(target_folder)
            if relative_audio is not None:
                audio = target_folder / relative_audio
            documents = {kind: (target_folder / path.relative_to(old_folder) if path.is_relative_to(old_folder) else path, checksum)
                         for kind, (path, checksum) in documents.items()}
        else:
            target_folder.mkdir(exist_ok=True)
        manifest = previous or {'schema_version': 1, 'original_audio_path': str(audio), 'files': {}}
        manifest['device_id'] = device_context.device_id()
        manifest.update(topic=topic, meeting_date=meeting_date, archive_date=archive_date,
                        date_basis='meeting' if meeting_date else 'archive', audio_sha256=audio_hash)
        destination = target_folder / audio.name
        if audio != destination:
            destination = unique_path(destination, audio_hash)
        manifest['audio_file'] = destination.name
        if requested_audio != destination:
            # Folder renames relocate the recording too; retain the caller's
            # pre-rename path, including across a failed recovery retry.
            manifest['move_source_path'] = str(requested_audio)
        # Write a recovery record before moving; never overwrite a different recording.
        w.write(target_folder / '.archive.json', manifest)
        index['recordings'][audio_hash] = {'folder': target_folder.name}
        w.write(index_path, index)
        if audio != destination:
            shutil.move(str(audio), str(destination))
        if w.file_digest(destination) != audio_hash:
            raise ValueError('归档录音校验失败，请检查：' + str(destination))
        for kind, (source, checksum) in documents.items():
            target = target_folder / NAMES[kind]
            last = manifest['files'].get(kind, {})
            if target.exists() and w.file_digest(target) != checksum:
                # Preserve edits made in the archived files (HTML, image, or notes edited in Obsidian).
                if w.file_digest(target) != last.get('generated_sha256'):
                    target = target.with_name(f'{target.stem}-更新版-{checksum[:8]}{target.suffix}')
                    if target.exists() and w.file_digest(target) != checksum:
                        target = unique_path(target, checksum)
            if not target.exists() or w.file_digest(target) != checksum:
                temporary = unique_path(target.with_name('.' + target.name + '.tmp'), checksum)
                try:
                    shutil.copy2(source, temporary)
                    temporary.replace(target)
                finally:
                    if temporary.exists():
                        temporary.unlink()
            manifest['files'][kind] = {'name': target.name, 'generated_sha256': checksum}
        for kind in withdraw:
            # A deliverable that no longer holds (a stale 深度洞察) goes, unless the user edited it.
            last = manifest['files'].get(kind)
            path = target_folder / last['name'] if last else None
            if path and path.is_file() and w.file_digest(path) == last['generated_sha256']:
                path.unlink()
            if path and not path.exists():
                del manifest['files'][kind]
        w.write(target_folder / '.archive.json', manifest)
        return {'folder': str(target_folder), 'audio': str(destination),
                **{kind: str(target_folder / details['name']) for kind, details in manifest['files'].items()}}


def collect_workspace(root, source_id, *, archive_root=DEFAULT_ROOT, archive_date=None):
    root = Path(root).expanduser().resolve()
    with w.locked(root):
        state = w.read(root / 'state.json')
        source = next(s for s in state['sources'] if s['source_id'] == source_id)
        analysis = w.read(root / 'analysis.json')['sources'].get(source_id, {})
        latest = w.read(root / 'exports/latest.json')
        publication = w.read(root / 'publication.json')
        if publication['export_version'] != latest['version'] or latest['revision'] != state['revision']:
            raise ValueError('纪要发布版本已过期；先生成并核对当前 HTML 和 PNG。')
        import audit
        if not audit.is_checked(root):
            raise ValueError('内容核对仍有未处理的不一致；修正文稿或由用户确认后才能归档。')
        documents = publication.get('files', {}).get(source_id)
        if not documents:
            raise ValueError('来源 ' + source_id + ' 的当前版本尚未发布；先核对并发布该来源的 HTML 和 PNG。')
        for kind in documents:
            if w.file_digest(documents[kind]['path']) != documents[kind]['sha256']:
                raise ValueError('发布文件已改变；请先核对 HTML/PNG 并更新 publication.json。')
        result = collect(source['audio_path'], root=archive_root,
                         topic=analysis.get('reader', {}).get('topic') or analysis.get('title') or source.get('title'),
                         meeting_date=analysis.get('meeting_date'), archive_date=archive_date,
                         expected_hash=source.get('audio_sha256'), _checked_workspace=True,
                         html=documents['html']['path'], png=documents['png']['path'],
                         extra={k: v['path'] for k, v in documents.items() if k not in ('html', 'png')},
                         withdraw=[k for k in ('insight_md',) if k not in documents])
        if source.get('audio_path') != result['audio'] or source.get('archive_dir') != result['folder']:
            source.setdefault('original_audio_path', source['audio_path'])
            source.update(audio_path=result['audio'], archive_dir=result['folder'])
            w.commit(root, state, {'kind': 'audio_archived', 'source_id': source_id,
                                   'audio_action': 'move', 'archive_dir': result['folder']})
        publication['revision'] = state['revision']
        publication.setdefault('archives', {})[source_id] = result
        w.write(root / 'publication.json', publication)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--audio', type=Path)
    mode.add_argument('--workspace', type=Path)
    parser.add_argument('--source')
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--topic')
    parser.add_argument('--meeting-date')
    parser.add_argument('--archive-date')
    parser.add_argument('--html', type=Path)
    parser.add_argument('--png', type=Path)
    args = parser.parse_args()
    try:
        if args.workspace:
            if not args.source:
                parser.error('--workspace 需要 --source。')
            result = collect_workspace(args.workspace, args.source, archive_root=args.root, archive_date=args.archive_date)
        else:
            result = collect(args.audio, root=args.root, topic=args.topic, meeting_date=args.meeting_date,
                             archive_date=args.archive_date, html=args.html, png=args.png)
    except (ValueError, OSError, KeyError, StopIteration) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
