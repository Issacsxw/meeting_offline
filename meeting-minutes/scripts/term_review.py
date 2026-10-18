"""Human terminology choices with immutable source evidence. No network access."""
from copy import deepcopy
from file_lock import locked_file
import os
from pathlib import Path
import memory
import workspace as w


def candidate_id(candidate):
    return candidate.get('id') or 'candidate-' + w.digest([candidate['source_id'], candidate['forms']])[:16]


def prepare(root, candidates, expected):
    """Prepare evidence-backed suggestions; this never confirms a term."""
    root = Path(root)
    with w.locked(root):
        state = w.read(root / 'state.json')
        w.check_revision(state, expected)
        if not isinstance(candidates, list):
            raise ValueError('术语候选必须是列表。')
        sources = {s['source_id']: w.read(s['path'])['segments'] for s in state['sources']}
        existing = {candidate_id(c): c for c in state.get('unconfirmed_term_candidates', [])}
        seen = set()
        for candidate in candidates:
            if not isinstance(candidate, dict) or candidate.get('source_id') not in sources:
                raise ValueError('术语候选必须关联已导入的录音。')
            forms = candidate.get('forms')
            if not isinstance(forms, list) or not forms or any(not isinstance(f, str) or not f.strip() or len(f) > 100 for f in forms) or len(set(forms)) != len(forms):
                raise ValueError('每组需提供不重复的原识别写法（每个不超过100字）。')
            if any(not any(f in s['text'] for s in sources[candidate['source_id']]) for f in forms):
                raise ValueError('原识别写法必须在原转写中出现；推测写法应放入 options。')
            options = candidate.get('options', [])
            if not isinstance(options, list) or any(not isinstance(o, dict) or not isinstance(o.get('text'), str) or not o['text'].strip() or len(o['text']) > 100 or not isinstance(o.get('basis', ''), str) for o in options):
                raise ValueError('options 需提供 text 和 basis，标明候选依据。')
            cid = candidate_id(candidate)
            if not isinstance(cid, str) or not cid or len(cid) > 100 or cid in seen:
                raise ValueError('候选 ID 无效或重复。')
            seen.add(cid)
            if cid in state.get('term_reviews', {}) and cid in existing and any(candidate.get(k) != existing[cid].get(k) for k in ('source_id', 'forms')):
                raise ValueError('已有复核记录的卡片不能改变来源或原识别写法；请另建候选。')
            existing[cid] = {**candidate, 'id': cid, 'status': 'unconfirmed'}
        state['unconfirmed_term_candidates'] = list(existing.values())
        return w.commit(root, state, {'kind': 'term_candidates_prepared', 'candidate_ids': list(seen), 'actor': 'assistant'})


def cards(root, state=None):
    root = Path(root)
    state = state or w.read(root / 'state.json')
    sources = {s['source_id']: s for s in state['sources']}
    transcripts = {}
    corrections = w.read(root / 'corrections.json')
    result = []
    for candidate in state.get('unconfirmed_term_candidates', []):
        sid = candidate['source_id']
        if sid not in sources:
            continue
        if sid not in transcripts:
            transcripts[sid] = w.read(sources[sid]['path'])['segments']
        segments = transcripts[sid]
        speakers = [s for s in state['speakers'] if s['source_id'] == sid]
        variants = []
        for form in candidate['forms']:
            occurrences = []
            for i, segment in enumerate(segments):
                if form not in segment['text']:
                    continue
                speaker = next((s for s in speakers if type(s['source_speaker_id']) is type(segment.get('speaker_id'))
                                and s['source_speaker_id'] == segment.get('speaker_id')), None)
                # Short names alone are unhelpful; include neighboring turns verbatim.
                context = []
                if len(segment['text']) < 30:
                    for j in (i - 1, i + 1):
                        if 0 <= j < len(segments):
                            context.append({'index': j, 'text': segments[j]['text'], 'start_ms': segments[j]['start_ms']})
                current = w.corrected_text(segment['text'], sid, i, corrections)
                occurrences.append({'index': i, 'text': segment['text'], 'current_text': current,
                    'available': form in current, 'context': context,
                    'speaker': (speaker.get('role_name') or speaker['key']) if speaker else '说话人未分离',
                    'start_ms': segment['start_ms'], 'end_ms': min(segment['end_ms'], segment['start_ms'] + 30000),
                    'clip_start_ms': max(0, segment['start_ms'] - 2000),
                    'clip_end_ms': min(segments[-1]['end_ms'], segment['end_ms'] + 4000)})
            variants.append({'form': form, 'occurrences': occurrences})
        cid = candidate_id(candidate)
        result.append({'id': cid, 'source_id': sid, 'title': candidate.get('title') or '术语待确认',
            'question': candidate.get('question', ''), 'scope': candidate.get('scope', ''),
            'category': candidate.get('category', '专有名词'),
            'options': candidate.get('options') or [{'text': f, 'basis': '原转写中出现，尚未确认'} for f in candidate['forms']],
            'variants': variants, 'review': state.get('term_reviews', {}).get(cid)})
    return result


def save(root, payload, expected):
    root = Path(root)
    with w.locked(root):
        state = w.read(root / 'state.json')
        w.check_revision(state, expected)
        card = next((c for c in cards(root, state) if c['id'] == payload.get('candidate_id')), None)
        if card is None:
            raise ValueError('找不到这张术语卡片，请刷新。')
        decision = payload.get('decision')
        if decision not in ('confirm', 'different', 'defer'):
            raise ValueError('请选择标准写法、不同含义或暂不确定。')
        record = {'decision': decision, 'at': w.now(), 'actor': 'user'}
        if decision != 'confirm':
            # These are review outcomes, never confirmed spellings or glossary entries.
            state.setdefault('term_reviews', {})[card['id']] = record
            return w.commit(root, state, {'kind': 'term_review_' + decision, 'candidate_id': card['id'], 'actor': 'user'})
        canonical = payload.get('canonical', '')
        scope = payload.get('scope', '')
        if not isinstance(canonical, str) or not canonical.strip() or len(canonical) > 100:
            raise ValueError('请选择或填写标准写法（不超过100字）。')
        if not isinstance(scope, str) or not scope.strip() or len(scope) > 200:
            raise ValueError('请填写适用业务或项目范围（不超过200字）。')
        canonical, scope = canonical.strip(), scope.strip()
        selections = payload.get('selections')
        if not isinstance(selections, list) or not selections:
            raise ValueError('请勾选至少一段原文作为确认依据。')
        variants = {v['form']: v for v in card['variants']}
        seen = set()
        planned = []
        aliases = []
        evidence = []
        for selection in selections:
            form = selection.get('heard')
            if form not in variants or form in seen:
                raise ValueError('写法无效或重复，请刷新卡片。')
            seen.add(form)
            indices = selection.get('segment_indices')
            available = {o['index'] for o in variants[form]['occurrences'] if o['available']}
            original_indices = {o['index'] for o in variants[form]['occurrences']}
            if not isinstance(indices, list) or not indices or any(type(i) is not int for i in indices) or len(set(indices)) != len(indices) or not set(indices) <= available:
                raise ValueError('原文选择已变化或无效，请刷新后重新勾选。')
            learn = selection.get('learn_alias', False)
            if type(learn) is not bool or (learn and set(indices) != original_indices):
                raise ValueError('只选部分段落时不能将整种写法记作别名，请取消“记住对应关系”。')
            evidence.extend({'source_id': card['source_id'], 'segment_index': i, 'heard': form} for i in sorted(indices))
            if form != canonical:
                if learn:
                    aliases.append(form)
                planned.append({'id': w.digest([card['id'], form, canonical, state['revision']])[:16],
                    'candidate_id': card['id'], 'source_id': card['source_id'], 'heard': form,
                    'canonical': canonical, 'scope': scope, 'segment_indices': sorted(indices),
                    'active': True, 'confirmed_at': w.now(),
                    # Partial choices do not authorize changes throughout derived prose.
                    'apply_to_derived': set(indices) == original_indices, 'learned_alias': learn})
        # Validate sources before changing any persistent memory.
        source = next(s for s in state['sources'] if s['source_id'] == card['source_id'])
        if w.file_digest(source['path']) != source['transcript_sha256']:
            raise ValueError('原始转写已变化，请先恢复来源快照。')
        w.validate_analysis(root, w.read(root / 'analysis.json'))
        store_path = w.memory_path(state)
        store_path.parent.mkdir(parents=True, exist_ok=True)
        with locked_file(str(store_path) + '.lock'):
            store = memory.read_store(store_path)
            active = [t for t in store['terms'] if t.get('active', True) and t['scope'] == scope]
            for term in active:
                if term['canonical'] == canonical:
                    continue
                # Check proposed correspondences too, even if not learning a reusable alias.
                old_forms = {c['heard'] for c in planned}
                if canonical in term.get('aliases', []) or old_forms & ({term['canonical']} | set(term.get('aliases', []))):
                    raise ValueError('相同范围已有不同的术语对应关系，请细化业务范围，保留歧义。')
            prior = next((t for t in active if t['canonical'] == canonical), None)
            term = {**(prior or {}), 'id': prior['id'] if prior else 'term-' + w.digest([scope, canonical])[:16],
                'canonical': canonical, 'scope': scope, 'category': card['category'],
                'aliases': sorted(set((prior or {}).get('aliases', []) + aliases)),
                'confirmation': '用户在本地术语选择卡片查看原文后明确确认标准写法及勾选范围。',
                'confirmed_by_user': True, 'active': True, 'updated_at': w.now(),
                'created_at': prior['created_at'] if prior else w.now(),
                'revision': prior.get('revision', 1) + 1 if prior else 1,
                'review_refs': (prior or {}).get('review_refs', []) + [{
                    'meeting_id': root.name, 'candidate_id': card['id'], 'at': w.now(),
                    'evidence': evidence, 'aliases_confirmed': aliases}]}
            memory.validate_entry('term', term)
            if prior:
                store['history'].append({'kind': 'term', 'replaced_at': w.now(), 'previous': deepcopy(prior)})
            store['terms'] = [t for t in store['terms'] if t['id'] != term['id']] + [term]
            memory.atomic_write(store_path, store)
        corrections = w.read(root / 'corrections.json')
        for correction in planned:
            correction['term_id'] = term['id']
        corrections.extend(planned)
        w.write(root / 'corrections.json', corrections)
        record.update(canonical=canonical, scope=scope, term_id=term['id'], selections=selections,
                      corrected_segments=sum(len(c['segment_indices']) for c in planned))
        state.setdefault('term_reviews', {})[card['id']] = record
        return w.commit(root, state, {'kind': 'term_choice_confirmed', 'candidate_id': card['id'],
                                     'term_id': term['id'], 'actor': 'user'})
