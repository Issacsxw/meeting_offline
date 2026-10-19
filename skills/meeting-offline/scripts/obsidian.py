#!/usr/bin/env python3
"""Use an Obsidian vault: archive meetings into it, and use its notes to catch recognition errors.

Reads the vault's Markdown locally; never uploads it and never edits existing notes.
  find                 list vaults Obsidian knows on this computer
  use --vault PATH     archive future meetings into PATH/<folder>
  off                  stop using a vault (and stop offering one)
  status               show the chosen vault
  vocab                note titles, aliases and tags that can serve as vocabulary
  hotwords --out F     confirmed terms + vault names as a hotword JSON for transcription
  suggest --workspace  find transcript words that sound like vault terms (term-review candidates)
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import sys

import device_context

PEOPLE_FOLDERS = {'人物', '人', '人员', '同事', '联系人', '客户', 'people', 'person', 'persons', 'contacts', 'team'}
PEOPLE_TAGS = {'人物', '人', '同事', '联系人', '客户', 'person', 'people', 'contact', 'colleague'}
SKIP_FOLDERS = {'.obsidian', '.trash', '.git', 'node_modules'}
PINYIN_FILE = Path(__file__).resolve().parents[1] / 'assets' / 'pinyin.txt'
CJK = re.compile(r'^[一-鿿]+$')


def config_files():
    home = Path.home()
    return [home / 'Library' / 'Application Support' / 'obsidian' / 'obsidian.json',
            Path(os.environ.get('APPDATA', home / 'AppData' / 'Roaming')) / 'obsidian' / 'obsidian.json',
            Path(os.environ.get('XDG_CONFIG_HOME', home / '.config')) / 'obsidian' / 'obsidian.json']


def discover():
    """Vaults registered in Obsidian's own config, plus vault folders in the usual iCloud/Documents spots."""
    found = {}
    for config in config_files():
        try:
            vaults = json.loads(config.read_text(encoding='utf-8')).get('vaults', {})
        except (OSError, ValueError, AttributeError):
            continue
        for entry in (vaults.values() if isinstance(vaults, dict) else []):
            path = Path(entry.get('path', '')) if isinstance(entry, dict) else None
            if path and readable_dir(path / '.obsidian'):
                found[path.resolve()] = {'path': str(path), 'name': path.name, 'last_opened': entry.get('ts'),
                                         'open': bool(entry.get('open')), 'source': 'Obsidian 设置'}
    home = Path.home()
    for parent in (home / 'Library' / 'Mobile Documents' / 'iCloud~md~obsidian' / 'Documents', home / 'Documents', home):
        try:
            children = [p for p in parent.iterdir() if readable_dir(p)]
        except OSError:
            continue
        for path in children:
            if readable_dir(path / '.obsidian') and path.resolve() not in found:
                found[path.resolve()] = {'path': str(path), 'name': path.name, 'last_opened': None,
                                         'open': False, 'source': '文件夹扫描'}
    return sorted(found.values(), key=lambda v: (not v['open'], -(v['last_opened'] or 0), v['name']))


def write_preference(update):
    current = device_context.paths()
    path = current['preferences']
    from file_lock import locked_file
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked_file(path.with_suffix('.lock')):
        data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
            'schema_version': 1, 'device_id': current['device_id']}
        device_context.require_device(data.get('device_id'), path)
        # Keep settings written before the rename under 'meeting_minutes'; newer keys win.
        legacy = data.pop('meeting_minutes', None)
        data['meeting_offline'] = {**(legacy if isinstance(legacy, dict) else {}), **(data.get('meeting_offline') or {}), **update}
        device_context.atomic_json(path, data)


def use(vault, folder='会议纪要'):
    vault = Path(vault).expanduser().resolve()
    if not readable_dir(vault / '.obsidian'):
        raise ValueError(f'{vault} 不是 Obsidian 库（里面没有 .obsidian 文件夹）。可以先运行 find 查看本机的库。')
    folder = folder.strip('/\\') or '会议纪要'
    if Path(folder).is_absolute() or '..' in Path(folder).parts:
        raise ValueError('folder 必须是库里的相对路径，例如“会议纪要”或“工作/会议”。')
    write_preference({'obsidian': {'vault': str(vault), 'folder': folder}})
    return status()


def off():
    write_preference({'obsidian': {'declined': True}})
    return status()


def status():
    current = device_context.paths()
    chosen = device_context.minutes_preferences().get('obsidian') or {}
    missing = chosen.get('vault') and not current['vault']
    return {'vault': str(current['vault']) if current['vault'] else None, 'archive': str(current['archive']),
            'configured': chosen or None, 'declined': bool(chosen.get('declined')),
            'note': '设置的库已不存在，暂时归档到默认位置。' if missing else None}


def frontmatter(text):
    """The small YAML subset Obsidian properties use: scalars, [a, b] lists and '- item' lists."""
    if not text.startswith('---'):
        return {}
    end = text.find('\n---', 3)
    if end < 0:
        return {}
    data, key = {}, None
    for line in text[3:end].splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        item = re.match(r'^\s*-\s+(.*)$', line)
        if item and key:
            data.setdefault(key, [])
            if isinstance(data[key], list):
                data[key].append(item.group(1).strip().strip('"\''))
            continue
        pair = re.match(r'^([A-Za-z_一-鿿][\w一-鿿 -]*):\s*(.*)$', line)
        if not pair:
            continue
        key, value = pair.group(1).strip().lower(), pair.group(2).strip()
        if value.startswith('[') and value.endswith(']'):
            data[key] = [v.strip().strip('"\'') for v in value[1:-1].split(',') if v.strip()]
        elif value:
            data[key] = value.strip('"\'')
    return data


def as_list(value):
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return [v.strip() for v in value.split(',')] if isinstance(value, str) else []


def readable_dir(path):
    """is_dir() that treats an unreadable folder (no permission, offline drive) as absent instead of raising."""
    try:
        return Path(path).is_dir()
    except OSError:
        return False


def notes(vault, skip=()):
    """(relative path, title, aliases, tags, is_person, head) per Markdown note, skipping hidden and given folders."""
    vault = Path(vault)
    skip = {Path(s).resolve() for s in skip}
    for folder, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in SKIP_FOLDERS
                   and (Path(folder) / d).resolve() not in skip]
        for name in files:
            if not name.endswith('.md'):
                continue
            path = Path(folder) / name
            try:
                with path.open('rb') as handle:  # properties sit at the top; large notes are not read whole
                    head = handle.read(8192).decode('utf-8', errors='replace')
            except OSError:
                continue
            meta = frontmatter(head)
            tags = [t.lstrip('#') for t in as_list(meta.get('tags') or meta.get('tag'))]
            relative = path.relative_to(vault)
            person = (any(p.lower() in PEOPLE_FOLDERS for p in relative.parts[:-1])
                      or any(t.lower() in PEOPLE_TAGS for t in tags)
                      or str(meta.get('type', '')).lower() in ('person', 'people', '人物'))
            yield {'path': relative.as_posix(), 'title': path.stem, 'aliases': as_list(meta.get('aliases') or meta.get('alias')),
                   'tags': tags, 'person': person, 'head': head}


def usable(term):
    term = term.strip()
    return (2 <= len(term) <= 20 and not re.fullmatch(r'[\d\W_]+', term)
            and not re.fullmatch(r'\d{4}[-./年]\d{1,2}([-./月]\d{1,2}日?)?.*', term))


def vocabulary(vault=None, corpus=False):
    """Terms from the vault: titles and aliases point at their note; people are marked.

    corpus=True also returns the notes' opening text, used to tell real words from recognition errors."""
    vault = Path(vault) if vault else device_context.paths()['vault']
    if not vault:
        raise ValueError('还没有设置 Obsidian 库；先运行 obsidian.py use --vault <库路径>。')
    archive = device_context.paths()['archive']
    terms, heads = {}, []
    for note in notes(vault, skip=[archive]):  # our own meeting notes would feed recognition errors back
        heads.append(note['head'])
        for text, kind in [(note['title'], 'title')] + [(a, 'alias') for a in note['aliases']]:
            if usable(text) and (text not in terms or kind == 'title'):
                terms[text] = {'term': text, 'note': note['title'], 'path': note['path'], 'kind': kind, 'person': note['person']}
        for tag in note['tags']:
            tag = tag.split('/')[-1]
            if usable(tag) and tag not in terms:
                terms[tag] = {'term': tag, 'note': None, 'path': note['path'], 'kind': 'tag', 'person': False}
    found = {'vault': str(vault), 'terms': sorted(terms.values(), key=lambda t: (not t['person'], t['kind'] != 'title', t['term']))}
    if corpus:
        found['corpus'] = '\n'.join(heads)
    return found


def links(vault=None):
    """Name or alias -> note title, for turning participants into [[links]]. Empty without a vault."""
    try:
        found = vocabulary(vault)['terms']
    except ValueError:
        return {}
    return {t['term']: t['note'] for t in found if t['note'] and t['kind'] in ('title', 'alias')}


def hotwords(out, limit=300, vault=None):
    """Confirmed memory terms first, then vault people and named notes; same JSON as transcribe_qwen --hotwords."""
    import memory
    words = {}
    store = memory.read_store(device_context.paths()['memory'])
    for term in store['terms']:
        if term.get('active', True) and term.get('confirmed_by_user'):
            words.setdefault(term['canonical'], 2)
    try:
        ranked = [t for t in vocabulary(vault)['terms'] if t['kind'] != 'tag' and len(t['term']) <= 12]
    except ValueError:
        ranked = []
    for term in ranked:
        if len(words) >= limit:
            break
        words.setdefault(term['term'], 2)
    device_context.atomic_json(Path(out).expanduser(), words)
    return {'out': str(Path(out).expanduser()), 'count': len(words)}


def pinyin_table():
    table = defaultdict(set)
    for line in PINYIN_FILE.read_text(encoding='utf-8').splitlines():
        if line and not line.startswith('#'):
            syllable, chars = line.split(' ', 1)
            for char in chars:
                table[char].add(syllable)
    return table


def readings(text, table, toned=True, cap=64):
    """Pinyin spellings of text (capped), with tone numbers or without; none if a character is unknown."""
    options = []
    for char in text:
        found = table.get(char)
        if not found:
            return []
        options.append(sorted(found if toned else {s.rstrip('12345') for s in found}))
    return [' '.join(combo) for combo in itertools.islice(itertools.product(*options), cap)]


def toned(text):
    """Two-character words must also match in tone: toneless, 实是 or 事实 would pass for 实施."""
    return len(text) <= 2


def covered(text, starts):
    """Character positions of text inside a known spelling, so no window cuts into a correct word."""
    marks = set()
    for i, char in enumerate(text):
        for term in starts.get(char, ()):
            if text.startswith(term, i):
                marks.update(range(i, i + len(term)))
    return marks


def suggest(root, vault=None, limit=40):
    """Transcript words that sound like a vault term but are written differently -> term_review candidates.

    Skips text inside a known spelling, words the vault itself uses (real words, not recognition errors)
    and matches that only occur inside a longer match; one card per heard form, listing every vault spelling."""
    import memory
    import workspace
    root = Path(root)
    state = workspace.read(root / 'state.json')
    corrections = workspace.read(root / 'corrections.json')
    found = vocabulary(vault, corpus=True)
    vault_name = Path(found['vault']).name
    store = memory.read_store(workspace.memory_path(state))
    known = {t['term'] for t in found['terms']} | {t['canonical'] for t in store['terms'] if t.get('active', True)}
    starts = defaultdict(list)
    for term in known:
        if len(term) >= 2:
            starts[term[0]].append(term)
    table = pinyin_table()
    by_sound = defaultdict(list)
    for term in found['terms']:
        if CJK.match(term['term']) and 2 <= len(term['term']) <= 8:
            for key in readings(term['term'], table, toned(term['term'])):
                by_sound[key].append(term)
    lengths = sorted({len(t['term']) for terms in by_sound.values() for t in terms})
    hits = defaultdict(lambda: {'terms': {}, 'spans': set()})
    for source in state['sources']:
        sid = source['source_id']
        for index, segment in enumerate(workspace.read(source['path'])['segments']):
            text = workspace.corrected_text(segment['text'], sid, index, corrections)
            inside = covered(text, starts)
            for size in lengths:
                for start in range(len(text) - size + 1):
                    window = text[start:start + size]
                    # term_review only accepts forms present in the original transcript.
                    if not CJK.match(window) or window not in segment['text'] or inside & set(range(start, start + size)):
                        continue
                    for key in readings(window, table, toned(window)):
                        for term in by_sound.get(key, ()):
                            if term['term'] != window:
                                hit = hits[(sid, window)]
                                hit['terms'][term['term']] = term; hit['spans'].add((index, start, start + size))
    spans = defaultdict(list)
    for (sid, heard), hit in hits.items():
        for index, start, end in hit['spans']:
            spans[(sid, index)].append((start, end))
    candidates = []
    for (sid, heard), hit in hits.items():
        if heard in found['corpus']:
            continue  # the user writes this word in their own notes, so it is not a misrecognition
        if all(any(a <= start and end <= b and (a, b) != (start, end) for a, b in spans[(sid, index)])
               for index, start, end in hit['spans']):
            continue  # only ever part of a longer match, which gets its own card
        options = []
        for canonical, term in sorted(hit['terms'].items(), key=lambda kv: (not kv[1]['person'], kv[0])):
            basis = (f'Obsidian 笔记《{term["note"]}》' + ('的别名' if term['kind'] == 'alias' else '')
                     if term['note'] else f'Obsidian 标签 #{canonical}')
            options.append({'text': canonical, 'basis': basis + '，读音相同'})
        names = '、'.join(o['text'] for o in options)
        segments = sorted({index for index, _, _ in hit['spans']})
        candidates.append({
            # Stable across runs so a re-run updates the same card instead of adding a duplicate.
            'id': 'obsidian-' + hashlib.sha256('\0'.join((sid, heard)).encode()).hexdigest()[:16], 'source_id': sid,
            'title': f'“{heard}”是否应为“{names}”', 'scope': f'Obsidian 库「{vault_name}」', 'forms': [heard],
            'category': '人名' if any(t['person'] for t in hit['terms'].values()) else '专有名词',
            'question': (f'转写里的“{heard}”和库里的“{names}”读音相同，是否按库里的写法修正？' if len(options) == 1 else
                         f'转写里的“{heard}”和库里的 {names} 读音都相同，应该是哪一个？'),
            'options': options, 'suggested': options[0]['text'] if len(options) == 1 else None, 'segments': segments})
    candidates.sort(key=lambda c: (-len(c['segments']), c['category'] != '人名', c['forms'][0]))
    return candidates[:limit]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('find'); sub.add_parser('status'); sub.add_parser('off')
    u = sub.add_parser('use'); u.add_argument('--vault', required=True); u.add_argument('--folder', default='会议纪要')
    v = sub.add_parser('vocab'); v.add_argument('--vault'); v.add_argument('--limit', type=int, default=200)
    h = sub.add_parser('hotwords'); h.add_argument('--out', required=True); h.add_argument('--limit', type=int, default=300)
    h.add_argument('--vault')
    s = sub.add_parser('suggest'); s.add_argument('--workspace', required=True); s.add_argument('--out', required=True)
    s.add_argument('--vault'); s.add_argument('--limit', type=int, default=40)
    args = parser.parse_args()
    try:
        if args.command == 'find':
            result = {'vaults': discover()}
        elif args.command == 'status':
            result = status()
        elif args.command == 'off':
            result = off()
        elif args.command == 'use':
            result = use(args.vault, args.folder)
        elif args.command == 'vocab':
            found = vocabulary(args.vault)
            result = {**found, 'total': len(found['terms']), 'terms': found['terms'][:args.limit]}
        elif args.command == 'hotwords':
            result = hotwords(args.out, args.limit, args.vault)
        else:
            candidates = suggest(args.workspace, args.vault, args.limit)
            device_context.atomic_json(Path(args.out).expanduser(), candidates)
            result = {'out': args.out, 'candidates': len(candidates)}
    except (ValueError, OSError, KeyError) as error:
        parser.exit(2, str(error) + '\n')
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2); print()


if __name__ == '__main__':
    main()
