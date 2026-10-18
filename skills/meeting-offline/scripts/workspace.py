#!/usr/bin/env python3
"""Local meeting workspace, role review, scoped corrections and reproducible exports."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from file_lock import locked_file
import device_context
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import memory
import minutes_html
import minutes_md
import terms


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    memory.atomic_write(path, value)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for part in iter(lambda: handle.read(1048576), b''):
            h.update(part)
    return h.hexdigest()


def timestamp(value, srt=False):
    value = int(value or 0)
    h, value = divmod(value, 3600000); m, value = divmod(value, 60000); s, ms = divmod(value, 1000)
    return f'{h:02}:{m:02}:{s:02}' + (f',{ms:03}' if srt else '')


@contextmanager
def locked(root):
    root = Path(root)
    with locked_file(root / '.workspace.lock'):
        if (root / 'state.json').exists():
            assert_workspace(root, read(root / 'state.json'))
        yield


def assert_workspace(root, state):
    device_context.require_device(state.get('device_id'), root)


def pairs(values):
    result = {}
    for value in values or []:
        key, sep, path = value.partition('=')
        if not sep or key in result:
            raise ValueError('Expected unique ID=PATH entries.')
        result[key] = Path(path).expanduser().resolve()
    return result


def initialize(state_path, root, audio_paths):
    root = Path(root).expanduser().resolve()
    if root.exists():
        raise ValueError('Workspace exists; reuse it instead of overwriting confirmed roles.')
    state_path = Path(state_path).expanduser().resolve()
    state = read(state_path)
    state = deepcopy(state)
    if state.get('device_id'):
        device_context.require_device(state['device_id'], state_path)
    state['device_id'] = device_context.device_id()
    ids = [s['source_id'] for s in state['sources']]
    if len(set(ids)) != len(ids) or any(not re.fullmatch(r'[a-zA-Z0-9_-]+', i) for i in ids):
        raise ValueError('Source IDs must be unique letters, digits, hyphens or underscores.')
    if set(audio_paths) - set(ids):
        raise ValueError('Audio source not present in the state.')
    prepared = []
    for source in state['sources']:
        path = Path(source['path'])
        if not path.is_absolute():
            path = (state_path.parent / path).resolve()
        transcript = read(path)
        if not transcript.get('segments'):
            raise ValueError('Source has no segments: ' + source['source_id'])
        audio = audio_paths.get(source['source_id'])
        if audio and not audio.is_file():
            raise ValueError('Audio does not exist: ' + str(audio))
        prepared.append((source, path, transcript, audio))
    root.mkdir(parents=True, mode=0o700)
    for source, path, transcript, audio in prepared:
        local = root / 'transcripts' / (source['source_id'] + '.json')
        write(local, transcript)
        source.update(original_transcript_path=str(path), path=str(local),
                      transcript_sha256=file_digest(local),
                      audio_path=str(audio) if audio else None,
                      audio_sha256=file_digest(audio) if audio else None,
                      title=audio.name if audio else path.name)
    state['schema_version'] = 2
    state['revision'] = 1
    state['created_at'] = now()
    state['pending_role_keys'] = [s['key'] for s in state['speakers'] if not s.get('role_confirmed') and not s.get('ignored_by_user')]
    state['status'] = 'awaiting_roles' if state['pending_role_keys'] else 'ready_for_minutes'
    write(root / 'state.json', state)
    write(root / 'corrections.json', [])
    write(root / 'analysis.json', {'sources': {}})
    write(root / 'manifest.json', {'schema_version': 2, 'revision': 1,
        'status': 'local_transcript_imported', 'created_at': now(),
        'network_calls': 0, 'audio_copied': False})
    snapshot(root)
    export(root)
    return root


def snapshot(root):
    state = read(root / 'state.json')
    write(root / 'revisions' / f"{state['revision']:06}.json", {
        'state': state, 'corrections': read(root / 'corrections.json'),
        'analysis': read(root / 'analysis.json')})


def memory_path(state):
    path = Path(state.get('memory_store') or memory.DEFAULT_STORE).expanduser()
    device_context.require_device(state.get('device_id'), path)
    return path


def commit(root, state, change):
    assert_workspace(root, state)
    state['device_id'] = device_context.device_id()
    state['revision'] += 1
    state['updated_at'] = now()
    state.setdefault('changes', []).append({'at': now(), **change})
    state['pending_role_keys'] = [s['key'] for s in state['speakers'] if not s.get('role_confirmed') and not s.get('ignored_by_user')]
    state['status'] = 'awaiting_roles' if state['pending_role_keys'] else 'ready_for_minutes'
    write(root / 'state.json', state)
    manifest = read(root / 'manifest.json')
    manifest.update(revision=state['revision'], status=state['status'], updated_at=now())
    write(root / 'manifest.json', manifest)
    snapshot(root)
    return export(root)


def check_revision(state, expected):
    if type(expected) is not int or expected != state['revision']:
        raise ValueError('页面内容已更新，请刷新后再保存。')


def confirmed_memory_upsert(store, kind, entry):
    # Use the same locked, versioned writer as the skill CLI.
    with tempfile.TemporaryDirectory(prefix='meeting-confirmed-') as tmp:
        path = Path(tmp) / 'entry.json'; write(path, entry)
        subprocess.run([sys.executable, str(Path(__file__).with_name('memory.py')), '--store', str(store),
                        'upsert', '--kind', kind, '--entry', str(path), '--user-confirmed'],
                       check=True, capture_output=True, text=True)


def save_roles(root, mappings, expected):
    root = Path(root)
    with locked(root):
        state = read(root / 'state.json'); check_revision(state, expected)
        known = memory.read_store(memory_path(state))
        profiles = {p['id']: p for p in known['roles'] if p.get('active', True)}
        speakers = {s['key']: s for s in state['speakers']}
        if not isinstance(mappings, list) or len({m['key'] for m in mappings}) != len(mappings):
            raise ValueError('Invalid or duplicate speaker mappings.')
        resolved = []
        new_profiles = []
        for mapping in mappings:
            if mapping['key'] not in speakers:
                raise ValueError('Unknown speaker.')
            if mapping.get('keep_unknown'):
                resolved.append((mapping['key'], None)); continue
            profile_id = mapping.get('profile_id')
            if profile_id:
                if profile_id not in profiles:
                    raise ValueError('Unknown/inactive profile.')
                profile = profiles[profile_id]
            else:
                name = str(mapping.get('name', '')).strip()
                role = str(mapping.get('role_title', '')).strip()
                scope = str(mapping.get('scope', '')).strip() or '本次会议，组织待补'
                if not name or len(name) > 100 or len(role) > 200:
                    raise ValueError('请填写姓名或角色称呼。')
                entity_type = mapping.get('entity_type', 'role')
                if entity_type not in ('person', 'role'):
                    raise ValueError('请选择具体人物或仅角色称呼。')
                profile_id = entity_type + '-' + hashlib.sha256((scope + '\0' + name).encode()).hexdigest()[:16]
                profile = profiles.get(profile_id) or {
                    'id': profile_id, 'entity_type': entity_type, 'name': name,
                    'scope': scope, 'role_title': role, 'aliases': [],
                    'confirmation': '用户在本地说话人复核页明确填写并保存：' + mapping['key']}
                new_profiles.append(profile)
            resolved.append((mapping['key'], profile))
        for profile in new_profiles:
            confirmed_memory_upsert(memory_path(state), 'role', profile)
            if profile['entity_type'] == 'person':
                confirmed_memory_upsert(memory_path(state), 'term', {
                    'id': 'name-' + digest([profile['scope'], profile['name']])[:16],
                    'canonical': profile['name'], 'scope': profile['scope'], 'aliases': [],
                    'definition': profile.get('role_title', ''),
                    'confirmation': '用户在本地角色复核页明确选择具体人物并填写姓名。'})
        for key, profile in resolved:
            speaker = speakers[key]
            if profile is None:
                speaker.update(role_name=minutes_html.UNKNOWN_ROLE, display_name=None, identity_group=None, accepted_unknown=True)
            else:
                speaker.update(role_name=profile['name'] + ('｜' + profile['role_title'] if profile.get('role_title') else ''),
                               display_name=profile['name'], identity_group=profile['id'], accepted_unknown=False)
            speaker['role_confirmed'] = True
        return commit(root, state, {'kind': 'roles_confirmed', 'speaker_keys': [m[0] for m in resolved], 'actor': 'user'})


def add_term(root, payload, expected):
    root = Path(root)
    with locked(root):
        state = read(root / 'state.json'); check_revision(state, expected)
        sources = {s['source_id']: s for s in state['sources']}
        source_id = payload.get('source_id')
        if source_id not in sources:
            raise ValueError('请选择本次要校对的录音。')
        old, canonical = str(payload.get('heard', '')).strip(), str(payload.get('canonical', '')).strip()
        scope = str(payload.get('scope', '')).strip()
        if not old or not canonical or not scope or old == canonical or max(len(old), len(canonical)) > 100:
            raise ValueError('请填写不同的原识别、标准写法和适用范围（每个词不超过100字）。')
        store = memory.read_store(memory_path(state))
        for term in store['terms']:
            if term.get('active', True) and term['scope'] == scope and old in term.get('aliases', []) and term['canonical'] != canonical:
                raise ValueError('此误识别形式在相同范围已有另一种标准写法，请先明确歧义。')
        existing = next((t for t in store['terms'] if t['scope'] == scope and t['canonical'] == canonical and t.get('active', True)), None)
        term = {**(existing or {}), 'id': existing['id'] if existing else 'term-' + digest([scope, canonical])[:16],
                'canonical': canonical, 'scope': scope,
                'aliases': sorted(set((existing or {}).get('aliases', []) + [old])),
                'confirmation': '用户在本地术语复核页明确确认：' + old + ' → ' + canonical}
        corrections = read(root / 'corrections.json')
        transcript = read(sources[source_id]['path'])
        protected = terms.known_forms(corrections, state.get('unconfirmed_term_candidates', []), store['terms'])
        indices = [i for i, s in enumerate(transcript['segments'])
                   if terms.contains(corrected_text(s['text'], source_id, i, corrections, protected), old, canonical, protected)]
        if not indices:
            raise ValueError('当前录音中未找到该写法；未创建替换，请检查原词。')
        confirmed_memory_upsert(memory_path(state), 'term', term)
        correction = {'id': digest([source_id, old, canonical, state['revision']])[:16],
            'source_id': source_id, 'heard': old, 'canonical': canonical, 'scope': scope,
            'segment_indices': indices, 'active': True, 'confirmed_at': now(), 'term_id': term['id']}
        corrections.append(correction); write(root / 'corrections.json', corrections)
        return commit(root, state, {'kind': 'term_confirmed', 'correction_id': correction['id'], 'actor': 'user'})


def protected_forms(state, corrections):
    """Known spellings in this workspace and its device memory, so a short form never rewrites a longer one."""
    store = memory.read_store(memory_path(state))
    return terms.known_forms(corrections, state.get('unconfirmed_term_candidates', []), store['terms'])


def corrected_text(text, source_id, index, corrections, protected=None):
    if protected is None:
        protected = terms.known_forms(corrections)
    for correction in corrections:
        if correction.get('active', True) and correction['source_id'] == source_id and index in correction['segment_indices']:
            text = terms.replace(text, correction['heard'], correction['canonical'], protected)
    return text


def validate_analysis(root, analysis):
    """Check the assistant-written analysis against the transcript; errors name the offending item."""
    state = read(Path(root) / 'state.json')
    assert_workspace(root, state)
    sources = {s['source_id']: read(s['path']) for s in state['sources']}
    speakers = {s['key']: s for s in state['speakers']}
    if not isinstance(analysis, dict) or not isinstance(analysis.get('sources'), dict):
        raise ValueError('Analysis requires a sources object.')
    for source_id, section in analysis['sources'].items():
        if source_id not in sources:
            raise ValueError(f'Analysis references an unknown source: {source_id}.')
        if not isinstance(section, dict):
            raise ValueError(f'sources.{source_id} must be an object.')
        count = len(sources[source_id]['segments'])

        def check_refs(refs, where, what='valid zero-based segment evidence'):
            if not isinstance(refs, list) or not refs or any(type(i) is not int or not 0 <= i < count for i in refs):
                raise ValueError(f'{where}: requires {what} (segment indices 0–{count - 1}).')

        for field in ('decisions', 'actions', 'open_questions'):
            items = section.get(field, [])
            if not isinstance(items, list):
                raise ValueError(f'{source_id}.{field} must be a list.')
            for n, item in enumerate(items):
                where = f'{source_id}.{field}[{n}]'
                if not isinstance(item, dict):
                    raise ValueError(f'{where} must be an object.')
                check_refs(item.get('evidence'), where)
                for key in ('proposer', 'owner'):
                    if item.get(key) and (item[key] not in speakers or speakers[item[key]]['source_id'] != source_id):
                        raise ValueError(f'{where}.{key}: speaker attribution must reference this source.')
                if field == 'decisions' and item.get('status') not in ('direction', 'proposal', 'decided', 'open'):
                    raise ValueError(f'{where}.status must be direction, proposal, decided, or open.')
                if field == 'actions' and item.get('owner') and item.get('owner_basis') not in ('self_commitment', 'explicit_assignment'):
                    raise ValueError(f'{where}: named action owners require explicit attribution evidence (owner_basis).')
        if 'reader' in section:
            reader = section['reader']
            if not isinstance(reader, dict) or not isinstance(reader.get('points', []), list) or not isinstance(reader.get('todos', []), list):
                raise ValueError(f'{source_id}.reader requires point and todo lists.')
            if 'topic' in reader and not isinstance(reader['topic'], str):
                raise ValueError(f'{source_id}.reader.topic must be text.')
            if 'summary' in reader:
                if not isinstance(reader['summary'], str) or not reader['summary'].strip():
                    raise ValueError(f'{source_id}.reader.summary must be nonempty text.')
                check_refs(reader.get('summary_evidence'), f'{source_id}.reader.summary_evidence', 'valid source evidence')
            for n, point in enumerate(reader.get('points', [])):
                where = f'{source_id}.reader.points[{n}]'
                if not isinstance(point, dict) or any(not isinstance(point.get(k), str) or not point[k].strip() for k in ('title', 'text')):
                    raise ValueError(f'{where} requires title and text.')
                check_refs(point.get('evidence'), where, 'valid source evidence')
                attribution = point.get('attribution', [])
                if not isinstance(attribution, list) or any(k not in speakers or speakers[k]['source_id'] != source_id for k in attribution):
                    raise ValueError(f'{where}.attribution must reference speakers of this source.')
            seen = set()
            for n, todo in enumerate(reader.get('todos', [])):
                where = f'{source_id}.reader.todos[{n}]'
                if not isinstance(todo, dict):
                    raise ValueError(f'{where} must be an object.')
                i = todo.get('action_index')
                if type(i) is not int or not 0 <= i < len(section.get('actions', [])) or i in seen:
                    raise ValueError(f'{where}.action_index must be a unique index into actions.')
                seen.add(i)
                if any(k in todo and (not isinstance(todo[k], str) or not todo[k].strip()) for k in ('task', 'deadline')) or set(todo) - {'action_index', 'task', 'deadline'}:
                    raise ValueError(f'{where}: may only shorten task/deadline text; ownership comes from the original action.')
    return analysis


def set_analysis(root, analysis, expected):
    root = Path(root)
    with locked(root):
        state = read(root / 'state.json'); check_revision(state, expected)
        validate_analysis(root, analysis); write(root / 'analysis.json', analysis)
        return commit(root, state, {'kind': 'analysis_updated', 'actor': 'assistant', 'semantic_review': 'pending'})


def clips_for(speaker):
    turns = [u for u in speaker['utterances'] if type(u.get('start_ms')) in (int, float)
             and type(u.get('end_ms')) in (int, float) and u['end_ms'] - u['start_ms'] >= 3000 and len(u['text']) >= 10]
    if not turns:
        turns = [u for u in speaker['utterances'] if u.get('start_ms') is not None and u.get('end_ms') is not None]
    quotes = {q['text'] for q in speaker.get('recognition_quotes', [])}
    preferred = [u for u in turns if u['text'] in quotes]
    for index in (0, len(turns)//2, len(turns)-1):
        if turns and turns[index] not in preferred:
            preferred.append(turns[index])
    return [{'start_ms': u['start_ms'], 'end_ms': min(u['end_ms'], u['start_ms'] + 20000), 'text': u['text']} for u in preferred[:3]]


def term_preview(root, source_id, heard):
    state = read(Path(root) / 'state.json')
    source = next((s for s in state['sources'] if s['source_id'] == source_id), None)
    if not source or not heard or len(heard) > 100:
        return {'count': 0, 'matches': []}
    corrections = read(Path(root) / 'corrections.json')
    protected = protected_forms(state, corrections)
    matches = []
    for i, segment in enumerate(read(source['path'])['segments']):
        current = corrected_text(segment['text'], source_id, i, corrections, protected)
        if terms.contains(current, heard, None, protected):
            matches.append({'index': i, 'text': current, 'original_text': segment['text'],
                            'start_ms': segment['start_ms'], 'end_ms': min(segment['end_ms'], segment['start_ms'] + 20000)})
    return {'count': len(matches), 'matches': matches[:6]}


def public_state(root):
    import term_review
    root = Path(root); state = read(root / 'state.json')
    assert_workspace(root, state)
    mem = memory.read_store(memory_path(state))
    return {'revision': state['revision'], 'status': state['status'],
        'sources': [{'source_id': s['source_id'], 'title': s['title'], 'has_audio': bool(s.get('audio_path'))} for s in state['sources']],
        'speakers': [{k: s.get(k) for k in ('key', 'source_id', 'role_name', 'role_confirmed', 'identity_group', 'focus', 'core_points', 'recognition_quotes', 'candidate_role')} | {'clips': clips_for(s)} for s in state['speakers']],
        'roles': [p for p in mem['roles'] if p.get('active', True)],
        'terms': [t for t in mem['terms'] if t.get('active', True)],
        'corrections': read(root / 'corrections.json'),
        'term_candidates': term_review.cards(root, state),
        'exports': read(root / 'exports/latest.json') if (root / 'exports/latest.json').exists() else None}


minutes_preferences = device_context.minutes_preferences


def preferred_theme():
    """preferences.meeting_offline.theme, one of minutes_html.THEMES; 暖白陶土 by default."""
    theme = minutes_preferences().get('theme', 'warm')
    if theme not in minutes_html.THEMES:
        raise ValueError('preferences 中的 theme 只能是 ' + '、'.join(minutes_html.THEMES) + '。')
    return theme


def preferred_layout():
    """preferences.meeting_offline.layout, one of minutes_html.LAYOUTS; 经典 by default."""
    layout = minutes_preferences().get('layout', 'classic')
    if layout not in minutes_html.LAYOUTS:
        raise ValueError('preferences 中的 layout 只能是 ' + '、'.join(minutes_html.LAYOUTS) + '。')
    return layout


def display_names():
    """Optional preferences.meeting_offline.display_names, e.g. {"用户本人": "我"}; empty by default."""
    names = minutes_preferences().get('display_names', {})
    if not isinstance(names, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip() for k, v in names.items()):
        raise ValueError('preferences 中的 display_names 必须是“已确认称呼 → 纪要显示名”的文本映射。')
    return names


def vault_links(state):
    """Confirmed participant name -> Obsidian note title, when a vault is set and has a note for them."""
    if not device_context.paths()['vault']:
        return {}
    import obsidian
    names = {s.get('display_name') or (s.get('role_name') or '').split('｜')[0] for s in state['speakers'] if s.get('role_confirmed')}
    return {name: note for name, note in obsidian.links().items() if name in names}


def export(root):
    root = Path(root); state = read(root / 'state.json')
    assert_workspace(root, state)
    analysis = validate_analysis(root, read(root / 'analysis.json'))
    corrections = read(root / 'corrections.json')
    protected = sorted(protected_forms(state, corrections))
    shown_names = display_names(); theme = preferred_theme(); layout = preferred_layout(); links = vault_links(state)
    private = read(root / 'insights.json') if (root / 'insights.json').exists() else {'sources': {}}
    signature = digest({'theme': theme, 'layout': layout, 'insights': private,
                        'insights_renderer': file_digest(Path(__file__).with_name('insights.py')), 'speakers': state['speakers'], 'analysis': analysis, 'corrections': corrections, 'protected': protected,
                        'display_names': shown_names, 'vault_links': links,
                        'html_template': file_digest(minutes_html.TEMPLATE), 'html_renderer': file_digest(minutes_html.__file__),
                        'md_renderer': file_digest(minutes_md.__file__),
                        'audio': [Path(s['audio_path']).name if s.get('audio_path') else None for s in state['sources']],
                        'sources': [(s['source_id'], s['transcript_sha256']) for s in state['sources']]})[:16]
    directory = root / 'exports' / signature; directory.mkdir(parents=True, exist_ok=True)
    speakers = {s['key']: s for s in state['speakers']}
    output = {'version': signature, 'revision': state['revision'], 'generated_at': now(), 'files': {}}
    for source in state['sources']:
        sid = source['source_id']; transcript = read(source['path'])
        if file_digest(source['path']) != source['transcript_sha256']:
            raise ValueError('Immutable raw transcript changed; refusing to export.')
        names = {(type(s['source_speaker_id']).__name__, s['source_speaker_id']): s.get('role_name') or s['key'] for s in state['speakers'] if s['source_id'] == sid}
        lines = [source['title'] + '｜带角色转写', '角色由用户确认；原始转写单独保留。', '']
        subtitles = []; normalized = []
        for i, segment in enumerate(transcript['segments']):
            speaker_id = segment.get('speaker_id')
            name = names.get((type(speaker_id).__name__, speaker_id), '未分离')
            text = corrected_text(segment['text'], sid, i, corrections, protected)
            lines.append(f"[{timestamp(segment['start_ms'])}–{timestamp(segment['end_ms'])}] {name}\n{text}\n")
            subtitles.append(f"{i+1}\n{timestamp(segment['start_ms'], True)} --> {timestamp(segment['end_ms'], True)}\n{name}：{text}")
            normalized.append({**segment, 'speaker_name': name, 'text': text, 'original_text': segment['text'], 'segment_id': sid + ':' + str(i)})
        structured = deepcopy(analysis['sources'].get(sid, {}))
        def name(key):
            return speakers[key]['role_name'] if key else '待明确'
        def evidence(refs):
            return [{'segment_id': sid + ':' + str(i), **transcript['segments'][i]} for i in refs]
        for field in ('decisions', 'actions', 'open_questions'):
            for claim in structured.get(field, []):
                text_key = 'task' if field == 'actions' else 'text'
                original = claim.get(text_key, '')
                revised = terms.correct_derived(original, sid, corrections, protected)
                if revised != original:
                    claim['original_analysis_text'] = original
                    claim[text_key] = revised
                claim['source_evidence'] = evidence(claim['evidence'])
                if claim.get('owner'): claim['owner_name'] = name(claim['owner'])
                if claim.get('proposer'): claim['proposer_name'] = name(claim['proposer'])
        ready = all(s.get('role_confirmed') or s.get('ignored_by_user') for s in state['speakers'] if s['source_id'] == sid)
        minutes = [structured.get('title') or source['title'], '', '会议日期：' + str(structured.get('meeting_date') or '待确认'),
                   '状态：角色已确认；纪要由 AI 整理，结论仍可复核。' if ready else '状态：角色待确认，仅显示观点卡片。', '', '各角色核心观点']
        for speaker in state['speakers']:
            if speaker['source_id'] == sid and not speaker.get('ignored_by_user'):
                minutes.append('\n' + (speaker.get('role_name') or speaker['key']))
                for point in speaker.get('core_points', []):
                    text = point.get('text', '') if isinstance(point, dict) else point
                    # Scoped user-approved term rules can normalize derived prose; raw evidence stays intact.
                    text = terms.correct_derived(text, sid, corrections, protected)
                    minutes.append('- ' + text)
        if ready:
            for field, title in (('decisions', '议题与决定状态'), ('actions', '行动项'), ('open_questions', '待确认事项')):
                minutes.extend(['', title])
                for i, claim in enumerate(structured.get(field, []), 1):
                    text = claim.get('text') or claim.get('task') or ''
                    minutes.append(str(i) + '. ' + text)
                    if field == 'decisions':
                        status = {'direction':'明确的推进方向', 'proposal':'建议或倾向', 'decided':'已决定', 'open':'未定'}[claim['status']]
                        minutes.append('状态：' + status + ('；提出者：' + name(claim.get('proposer')) if claim.get('proposer') else ''))
                    if field == 'actions':
                        minutes.extend(['负责人：' + name(claim.get('owner')), '交付物：' + (claim.get('deliverable') or '待明确'),
                            '期限：' + (claim.get('deadline_text') or '待明确') + ('（会议日期待确认，未换算）' if claim.get('deadline_text') and not structured.get('meeting_date') else '')])
                    for ref in claim['source_evidence']:
                        minutes.append('原文 [' + timestamp(ref['start_ms']) + '] ' + ref['text'])
        kinds = {'transcript': ('转写.txt', '\n'.join(lines)), 'subtitles': ('字幕.srt', '\n\n'.join(subtitles)+'\n'),
                 'minutes': ('纪要.txt', '\n'.join(minutes)+'\n')}
        files = {}
        for kind, (suffix, text) in kinds.items():
            path = directory / (sid + '-' + suffix)
            path.write_text(text, encoding='utf-8'); files[kind] = str(path)
        path = directory / (sid + '-structured.json')
        write(path, {'source_id': sid, 'roles_confirmed': ready, 'transcript': normalized, 'analysis': structured,
                     'semantic_review': 'pending', 'version': signature})
        files['structured'] = str(path)
        if ready and structured:
            document = minutes_html.from_workspace(state, sid, structured, corrections, protected, shown_names, theme, layout)
            path = directory / (sid + '-纪要.html')
            rendered = minutes_html.render(document)
            unchanged = path.exists() and path.read_text(encoding='utf-8') == rendered
            if not unchanged:
                path.write_text(rendered, encoding='utf-8')
            files['html'] = str(path)
            image = path.with_suffix('.png')
            png = {'png': str(image)} if unchanged and image.exists() else minutes_html.png_for(path, image)
            if png['png']:
                files['png'] = png['png']
            else:
                files['png_error'] = png['png_error']
            audio = Path(source['audio_path']).name if source.get('audio_path') else None
            meeting_date = structured.get('meeting_date')
            shown = {(type(s['source_speaker_id']).__name__, s['source_speaker_id']):
                     minutes_html.shown_name(s, shown_names) for s in state['speakers'] if s['source_id'] == sid}
            spoken = [{'speaker': shown.get((type(n.get('speaker_id')).__name__, n.get('speaker_id')), '未分离'),
                       'start_ms': n['start_ms'], 'text': n['text']} for n in normalized]
            for kind, suffix, text in (
                    ('md', '会议纪要.md', minutes_md.minutes(document, meeting_date=meeting_date, links=links, audio=audio)),
                    ('transcript_md', '逐字稿.md', minutes_md.transcript(document['topic'], spoken, meeting_date=meeting_date,
                                                                         people=document['people'], links=links))):
                path = directory / (sid + '-' + suffix)
                path.write_text(text, encoding='utf-8'); files[kind] = str(path)
            if sid in private['sources']:
                # Private analysis of each person; a separate file, never part of the shared minutes.
                import insights
                try:
                    insights.validate(root, {'sources': {sid: private['sources'][sid]}})
                    text = insights.markdown(root, state, sid, private['sources'][sid], corrections, protected, shown_names,
                                             links, document['topic'], meeting_date)
                except (ValueError, OSError, KeyError) as error:  # e.g. a cited earlier meeting was deleted
                    files['insight_error'] = str(error)
                else:
                    path = directory / (sid + '-深度洞察.md'); path.write_text(text, encoding='utf-8'); files['insight_md'] = str(path)
        output['files'][sid] = files
    write(root / 'exports/latest.json', output)
    return output


def publish(root, source_ids=None, png_overrides=None):
    """Record the checked HTML/PNG (and Markdown copies) of the latest export for archive_meeting.py."""
    root = Path(root)
    with locked(root):
        state = read(root / 'state.json'); latest = read(root / 'exports/latest.json')
        if latest['revision'] != state['revision']:
            raise ValueError('导出不是最新版本；请先运行 export。')
        png_overrides = png_overrides or {}
        targets = source_ids or [sid for sid, f in latest['files'].items() if f.get('html')]
        if not targets:
            raise ValueError('还没有可发布的纪要 HTML（角色确认并写入 analysis 后才会生成）。')
        files = {}
        for sid in targets:
            exported = latest['files'].get(sid, {})
            if not exported.get('html'):
                raise ValueError('来源 ' + sid + ' 还没有纪要 HTML。')
            png = png_overrides.get(sid) or exported.get('png')
            if not png:
                raise ValueError('来源 ' + sid + ' 没有 PNG：' + exported.get('png_error', '')
                                 + ' 可在 HTML 中导出图片后用 --png ' + sid + '=路径 指定。')
            documents = [('html', exported['html']), ('png', png)] + [(k, exported[k]) for k in ('md', 'transcript_md', 'insight_md') if exported.get(k)]
            files[sid] = {kind: {'path': str(Path(value).resolve()), 'sha256': file_digest(value)} for kind, value in documents}
        publication = read(root / 'publication.json') if (root / 'publication.json').exists() else {}
        publication.update(export_version=latest['version'], revision=state['revision'], published_at=now())
        publication.setdefault('files', {}).update(files)
        write(root / 'publication.json', publication)
        return publication


def hotwords(root, terms, out, weight=2):
    state = read(Path(root) / 'state.json'); store = memory.read_store(memory_path(state))
    known = {t['canonical'] for t in store['terms'] if t.get('confirmed_by_user') and t.get('active', True)}
    if not terms or set(terms) - known:
        raise ValueError('Select explicit active, user-confirmed canonical terms.')
    if type(weight) is not int or weight not in (1,2,3,4,5):
        raise ValueError('Use a conservative weight from 1 to 5.')
    result = {term: weight for term in sorted(set(terms))}; write(Path(out), result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    imp = sub.add_parser('import'); imp.add_argument('--state', required=True); imp.add_argument('--workspace', required=True); imp.add_argument('--audio', action='append')
    for command in ('show', 'export'):
        p = sub.add_parser(command); p.add_argument('--workspace', required=True)
    a = sub.add_parser('analysis'); a.add_argument('--workspace', required=True); a.add_argument('--file', required=True); a.add_argument('--revision', required=True, type=int)
    c = sub.add_parser('candidates'); c.add_argument('--workspace', required=True); c.add_argument('--file', required=True); c.add_argument('--revision', required=True, type=int)
    ct = sub.add_parser('confirm-terms', help='一次应用用户在对话里确认的术语修正'); ct.add_argument('--workspace', required=True); ct.add_argument('--file', required=True); ct.add_argument('--revision', required=True, type=int)
    pub = sub.add_parser('publish'); pub.add_argument('--workspace', required=True); pub.add_argument('--source', action='append'); pub.add_argument('--png', action='append', help='SID=PATH，浏览器内手动导出的 PNG')
    h = sub.add_parser('hotwords'); h.add_argument('--workspace', required=True); h.add_argument('--term', action='append', required=True); h.add_argument('--out', required=True); h.add_argument('--weight', type=int, default=2)
    args = parser.parse_args()
    try:
        if args.command == 'import': result = str(initialize(args.state, args.workspace, pairs(args.audio)))
        elif args.command == 'show': result = public_state(args.workspace)
        elif args.command == 'analysis': result = set_analysis(args.workspace, read(args.file), args.revision)
        elif args.command == 'candidates':
            import term_review
            result = term_review.prepare(args.workspace, read(args.file), args.revision)
        elif args.command == 'confirm-terms':
            import term_review
            result = term_review.confirm_batch(args.workspace, read(args.file), args.revision)
        elif args.command == 'publish': result = publish(args.workspace, args.source, {k: str(v) for k, v in pairs(args.png).items()})
        elif args.command == 'hotwords': result = hotwords(args.workspace, args.term, args.out, args.weight)
        else: result = export(Path(args.workspace))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
