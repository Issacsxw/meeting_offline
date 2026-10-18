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
            if path and (path / '.obsidian').is_dir():
                found[path.resolve()] = {'path': str(path), 'name': path.name, 'last_opened': entry.get('ts'),
                                         'open': bool(entry.get('open')), 'source': 'Obsidian 设置'}
    home = Path.home()
    for parent in (home / 'Library' / 'Mobile Documents' / 'iCloud~md~obsidian' / 'Documents', home / 'Documents', home):
        try:
            children = [p for p in parent.iterdir() if p.is_dir()]
        except OSError:
            continue
        for path in children:
            if (path / '.obsidian').is_dir() and path.resolve() not in found:
                found[path.resolve()] = {'path': str(path), 'name': path.name, 'last_opened': None,
                                         'open': False, 'source': '文件夹扫描'}
    return sorted(found.values(), key=lambda v: (not v['open'], -(v['last_opened'] or 0), v['name']))


def write_preference(update):
    current = device_context.paths()
    path = current['preferences']
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'schema_version': 1, 'device_id': current['device_id']}
    device_context.require_device(data.get('device_id'), path)
    section = data.setdefault('meeting_offline', data.pop('meeting_minutes', {}) or {})
    section.update(update)
    device_context.atomic_json(path, data)


def use(vault, folder='会议纪要'):
    vault = Path(vault).expanduser().resolve()
    if not (vault / '.obsidian').is_dir():
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


def notes(vault, skip=()):
    """(relative path, title, aliases, tags, is_person) for each Markdown note, skipping hidden and given folders."""
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
                head = path.read_text(encoding='utf-8', errors='replace')[:4000]
            except OSError:
                continue
            meta = frontmatter(head)
            tags = [t.lstrip('#') for t in as_list(meta.get('tags') or meta.get('tag'))]
            relative = path.relative_to(vault)
            person = (any(p.lower() in PEOPLE_FOLDERS for p in relative.parts[:-1])
                      or any(t.lower() in PEOPLE_TAGS for t in tags)
                      or str(meta.get('type', '')).lower() in ('person', 'people', '人物'))
            yield {'path': relative.as_posix(), 'title': path.stem, 'aliases': as_list(meta.get('aliases') or meta.get('alias')),
                   'tags': tags, 'person': person}


def usable(term):
    term = term.strip()
    return (2 <= len(term) <= 20 and not re.fullmatch(r'[\d\W_]+', term)
            and not re.fullmatch(r'\d{4}[-./年]\d{1,2}([-./月]\d{1,2}日?)?.*', term))


def vocabulary(vault=None):
    """Terms from the vault: titles and aliases point at their note; people are marked."""
    vault = Path(vault) if vault else device_context.paths()['vault']
    if not vault:
        raise ValueError('还没有设置 Obsidian 库；先运行 obsidian.py use --vault <库路径>。')
    archive = device_context.paths()['archive']
    terms = {}
    for note in notes(vault, skip=[archive]):  # our own meeting notes would feed recognition errors back
        for text, kind in [(note['title'], 'title')] + [(a, 'alias') for a in note['aliases']]:
            if usable(text) and (text not in terms or kind == 'title'):
                terms[text] = {'term': text, 'note': note['title'], 'path': note['path'], 'kind': kind, 'person': note['person']}
        for tag in note['tags']:
            tag = tag.split('/')[-1]
            if usable(tag) and tag not in terms:
                terms[tag] = {'term': tag, 'note': None, 'path': note['path'], 'kind': 'tag', 'person': False}
    return {'vault': str(vault), 'terms': sorted(terms.values(), key=lambda t: (not t['person'], t['kind'] != 'title', t['term']))}


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


def readings(text, table, cap=16):
    """All toneless pinyin spellings of text (capped), or none if a character is unknown."""
    options = [table.get(c) for c in text]
    if not all(options):
        return []
    return [' '.join(combo) for combo in itertools.islice(itertools.product(*sorted(map(sorted, options))), cap)]


def suggest(root, vault=None, limit=40):
    """Transcript words that sound like a vault term but are written differently -> term_review candidates."""
    import memory
    import workspace
    root = Path(root)
    state = workspace.read(root / 'state.json')
    corrections = workspace.read(root / 'corrections.json')
    found = vocabulary(vault)
    vault_name = Path(found['vault']).name
    store = memory.read_store(workspace.memory_path(state))
    known = {t['term'] for t in found['terms']} | {t['canonical'] for t in store['terms'] if t.get('active', True)}
    table = pinyin_table()
    by_sound = defaultdict(list)
    for term in found['terms']:
        if CJK.match(term['term']) and 2 <= len(term['term']) <= 8:
            for key in readings(term['term'], table):
                by_sound[key].append(term)
    lengths = sorted({len(t['term']) for terms in by_sound.values() for t in terms})
    hits = defaultdict(lambda: {'segments': set()})
    for source in state['sources']:
        sid = source['source_id']
        for index, segment in enumerate(workspace.read(source['path'])['segments']):
            text = workspace.corrected_text(segment['text'], sid, index, corrections)
            for size in lengths:
                for start in range(len(text) - size + 1):
                    window = text[start:start + size]
                    if window in known or not CJK.match(window):
                        continue
                    for key in readings(window, table):
                        for term in by_sound.get(key, ()):
                            if term['term'] != window:
                                hit = hits[(sid, window, term['term'])]
                                hit.update(term=term); hit['segments'].add(index)
    ranked = sorted(hits.items(), key=lambda kv: (-len(kv[1]['segments']), not kv[1]['term']['person'], kv[0]))
    candidates = []
    for (sid, heard, canonical), hit in ranked[:limit]:
        term = hit['term']
        basis = f'Obsidian 笔记《{term["note"]}》' + ('的别名' if term['kind'] == 'alias' else '') if term['note'] else f'Obsidian 标签 #{canonical}'
        # Stable across runs so a re-run updates the same card instead of adding a duplicate.
        cid = 'obsidian-' + hashlib.sha256('\0'.join((sid, heard, canonical)).encode()).hexdigest()[:16]
        candidates.append({'id': cid, 'source_id': sid,
                           'title': f'“{heard}”是否应为“{canonical}”', 'category': '人名' if term['person'] else '专有名词',
                           'scope': f'Obsidian 库「{vault_name}」', 'forms': [heard],
                           'question': f'转写里的“{heard}”和库里的“{canonical}”读音相同，是否按库里的写法修正？',
                           'options': [{'text': canonical, 'basis': basis + '，读音相同'}],
                           'suggested': canonical, 'segments': sorted(hit['segments'])})
    return candidates


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
