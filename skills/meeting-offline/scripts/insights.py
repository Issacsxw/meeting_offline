#!/usr/bin/env python3
"""Private per-person insight for one meeting: what each confirmed person wanted, the likely reasons,
how their position moved during the meeting and against their earlier meetings on this device.

  history --workspace W --out F            earlier meetings of each confirmed person (same identity_group)
  save --workspace W --file F --revision N validate the assistant's insight JSON and write 深度洞察.md

Every claim cites transcript segments; inferences carry a confidence level. The result never enters
the shared minutes (HTML, PNG, 会议纪要.md) or the long-term memory.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

import device_context
import minutes_html
import minutes_md
import workspace as w

BASIS = {'fact': '事实', 'inference': '推断'}
CONFIDENCE = {'high': '把握较高', 'medium': '把握中等', 'low': '把握较低'}
SECTIONS = (('focus', '本场诉求'), ('considerations', '背后考虑'), ('shifts', '场内变化'), ('history', '跨会议变化'))
# A backstop for the rules in references/insights.md: work positions only, never character or private life.
OFF_LIMITS = ('性格', '人品', '情商', '智商', '心理问题', '抑郁', '怀孕', '宗教', '信仰', '政治倾向', '性取向', '能力差', '不靠谱')


def confirmed(speaker):
    return speaker.get('role_confirmed') and not speaker.get('ignored_by_user') and not speaker.get('accepted_unknown') \
        and speaker.get('role_name') != minutes_html.UNKNOWN_ROLE and speaker.get('identity_group')


def speaker_of(state, sid, segment):
    value = segment.get('speaker_id')
    return next((s for s in state['speakers'] if s['source_id'] == sid and type(s['source_speaker_id']) is type(value)
                 and s['source_speaker_id'] == value), None)


def workspaces(root):
    """Other meeting workspaces of this device: the init 'meetings' folder and the current workspace's siblings."""
    root = Path(root).resolve()
    seen = set()
    for parent in (device_context.paths()['meetings'], root.parent):
        try:
            children = sorted(Path(parent).iterdir())
        except OSError:
            continue
        for child in children:
            child = child.resolve()
            if child == root or child in seen or not (child / 'state.json').is_file():
                continue
            seen.add(child)
            try:
                state = w.read(child / 'state.json')
                device_context.require_device(state.get('device_id'), child)
            except (ValueError, OSError):
                continue  # another device's or unreadable workspace
            yield child, state


def meeting_label(root, state, sid):
    analysis = w.read(root / 'analysis.json')['sources'].get(sid, {}) if (root / 'analysis.json').exists() else {}
    source = next(s for s in state['sources'] if s['source_id'] == sid)
    topic = analysis.get('reader', {}).get('topic') or analysis.get('title') or source.get('title') or root.name
    return {'topic': topic, 'meeting_date': analysis.get('meeting_date'), 'analysis': analysis}


def history(root, meetings=5, quotes=12):
    """For each confirmed person here, their most recent earlier meetings with points, actions and quotes."""
    root = Path(root).resolve()
    state = w.read(root / 'state.json'); w.assert_workspace(root, state)
    people = {}
    for speaker in state['speakers']:
        if confirmed(speaker) and speaker['identity_group'] not in people:
            people[speaker['identity_group']] = {'speaker': speaker['key'], 'name': minutes_html.shown_name(speaker),
                                                 'identity_group': speaker['identity_group'], 'meetings': []}
    for other, other_state in workspaces(root):
        try:
            person_meetings(other, other_state, people, quotes)
        except (ValueError, OSError, KeyError, StopIteration):
            continue  # an older or damaged workspace is skipped rather than ending the whole history
    for person in people.values():
        person['meetings'].sort(key=lambda m: (m['meeting_date'] or '', m['created_at'] or ''), reverse=True)
        del person['meetings'][meetings:]
    return {'workspace': root.name, 'people': list(people.values()),
            'note': '只包含本机工作区里、由用户确认为同一人的会议；没有记录不代表没有开过会。'}


def person_meetings(other, other_state, people, quotes):
    """Add this workspace's meetings to each matching person in people."""
    corrections = w.read(other / 'corrections.json') if (other / 'corrections.json').exists() else []
    previous = w.read(other / 'insights.json') if (other / 'insights.json').exists() else {'sources': {}}
    for speaker in other_state['speakers']:
        person = people.get(speaker.get('identity_group'))
        if not person or not confirmed(speaker):
            continue
        sid = speaker['source_id']
        label = meeting_label(other, other_state, sid)
        section = label.pop('analysis')
        segments = w.read(next(s for s in other_state['sources'] if s['source_id'] == sid)['path'])['segments']
        own = [(i, s) for i, s in enumerate(segments) if speaker_of(other_state, sid, s) is speaker and len(s['text'].strip()) >= 12]
        step = max(1, len(own) // quotes) if own else 1
        person['meetings'].append({
            'meeting': other.name, 'source_id': sid, 'speaker': speaker['key'], **label,
            'created_at': other_state.get('created_at'),
            'points': [p['title'] + '：' + p['text'] for p in section.get('reader', {}).get('points', []) if speaker['key'] in p.get('attribution', [])],
            'actions': [a['task'] for a in section.get('actions', []) if a.get('owner') == speaker['key']],
            'insight': next((p for p in previous['sources'].get(sid, {}).get('people', []) if p.get('speaker') == speaker['key']), None),
            'quotes': [{'segment': i, 'time': w.timestamp(s['start_ms']), 'text': w.corrected_text(s['text'], sid, i, corrections)}
                       for i, s in own[::step][:quotes]]})


def resolve_past(root, ref, identity):
    """A cited segment from an earlier meeting, checked to be spoken by the same confirmed person."""
    if not isinstance(ref, dict) or set(ref) != {'meeting', 'source_id', 'segment'} or type(ref['segment']) is not int:
        raise ValueError('past 每项需要 meeting、source_id、segment（来自 history 输出）。')
    for other, other_state in workspaces(root):
        if other.name != ref['meeting']:
            continue
        source = next((s for s in other_state['sources'] if s['source_id'] == ref['source_id']), None)
        segments = w.read(source['path'])['segments'] if source else []
        if not 0 <= ref['segment'] < len(segments):
            break
        speaker = speaker_of(other_state, ref['source_id'], segments[ref['segment']])
        if not speaker or not confirmed(speaker) or speaker['identity_group'] != identity:
            raise ValueError(f'{ref["meeting"]} 第 {ref["segment"]} 段不是同一位已确认的人说的。')
        corrections = w.read(other / 'corrections.json') if (other / 'corrections.json').exists() else []
        label = meeting_label(other, other_state, ref['source_id'])  # OSError here is reported by the caller
        return {**ref, 'topic': label['topic'], 'meeting_date': label['meeting_date'],
                'time': w.timestamp(segments[ref['segment']]['start_ms']),
                'text': w.corrected_text(segments[ref['segment']]['text'], ref['source_id'], ref['segment'], corrections)}
    raise ValueError(f'找不到本机会议 {ref["meeting"]} 的第 {ref["segment"]} 段；past 只能引用 history 列出的会议。')


def validate(root, insights):
    root = Path(root)
    state = w.read(root / 'state.json'); w.assert_workspace(root, state)
    if not isinstance(insights, dict) or not isinstance(insights.get('sources'), dict):
        raise ValueError('深度洞察需要 sources 对象。')
    for sid, section in insights['sources'].items():
        source = next((s for s in state['sources'] if s['source_id'] == sid), None)
        if not source or not isinstance(section, dict):
            raise ValueError(f'未知来源：{sid}。')
        segments = w.read(source['path'])['segments']
        speakers = {s['key']: s for s in state['speakers'] if s['source_id'] == sid}

        def claim(item, where, person=None, kind=None):
            if not isinstance(item, dict) or not isinstance(item.get('text'), str) or not item['text'].strip() or len(item['text']) > 400:
                raise ValueError(f'{where}.text 必须是 400 字以内的文字。')
            if any(word in item['text'] for word in OFF_LIMITS):
                raise ValueError(f'{where}：深度洞察只分析工作上的立场和考虑，不评价性格、能力、健康或私人情况。')
            refs = item.get('evidence')
            if not isinstance(refs, list) or not refs or any(type(i) is not int or not 0 <= i < len(segments) for i in refs):
                raise ValueError(f'{where}.evidence 需要本场转写的段落编号（0–{len(segments) - 1}）。')
            basis = item.get('basis', 'fact' if kind == 'focus' else None)
            if basis not in BASIS:
                raise ValueError(f'{where}.basis 必须是 fact（会上说过的）或 inference（推断）。')
            if basis == 'inference' and item.get('confidence') not in CONFIDENCE:
                raise ValueError(f'{where}：推断需要 confidence（high、medium 或 low）。')
            if kind == 'considerations' and basis != 'inference':
                raise ValueError(f'{where}：背后考虑是推断，basis 应为 inference，并给出把握程度。')
            if person:
                own = [i for i in refs if speaker_of(state, sid, segments[i]) is person]
                if not own:
                    raise ValueError(f'{where}：依据里至少要有一段是 {person["key"]} 本人的发言。')
                if kind == 'shifts' and len({segments[i]['start_ms'] for i in own}) < 2:
                    raise ValueError(f'{where}：场内变化需要本人前后至少两段发言作依据。')
            if kind == 'history':
                past = item.get('past')
                if not isinstance(past, list) or not past:
                    raise ValueError(f'{where}.past 需要引用之前会议里本人的发言（来自 history 输出）。')
                for ref in past:
                    resolve_past(root, ref, person['identity_group'])

        people = section.get('people', [])
        if not isinstance(people, list) or not people:
            raise ValueError(f'{sid}.people 至少要有一位已确认的参会人。')
        seen = set()
        for n, entry in enumerate(people):
            where = f'{sid}.people[{n}]'
            person = speakers.get(entry.get('speaker')) if isinstance(entry, dict) else None
            if not person or not confirmed(person):
                raise ValueError(f'{where}.speaker 必须是本场已由用户确认身份的说话人。')
            if person['identity_group'] in seen:
                raise ValueError(f'{where}：同一个人只写一次。')
            seen.add(person['identity_group'])
            # Recorded on save: a later change of who this speaker is makes the insight stale.
            if entry.get('identity_group', person['identity_group']) != person['identity_group']:
                raise ValueError(f'{person["key"]} 的身份在写洞察之后改过了，深度洞察需要重新生成。')
            if set(entry) - {'speaker', 'identity_group', *(k for k, _ in SECTIONS)}:
                raise ValueError(f'{where} 只能包含 speaker、focus、considerations、shifts、history。')
            if not entry.get('focus'):
                raise ValueError(f'{where}.focus（本场诉求）不能为空。')
            for kind, _ in SECTIONS:
                items = entry.get(kind, [])
                if not isinstance(items, list):
                    raise ValueError(f'{where}.{kind} 必须是列表。')
                for m, item in enumerate(items):
                    claim(item, f'{where}.{kind}[{m}]', person, kind)
        for m, item in enumerate(section.get('dynamics', [])):
            where = f'{sid}.dynamics[{m}]'
            claim(item, where)
            if any(k not in speakers or not confirmed(speakers[k]) for k in item.get('speakers', [])):
                raise ValueError(f'{where}.speakers 必须是本场已确认的说话人。')
        if set(section) - {'people', 'dynamics'}:
            raise ValueError(f'{sid} 只能包含 people 和 dynamics。')
    return insights


def save(root, insights, expected):
    root = Path(root)
    with w.locked(root):
        state = w.read(root / 'state.json'); w.check_revision(state, expected)
        validate(root, insights)
        speakers = {s['key']: s for s in state['speakers']}
        for section in insights['sources'].values():
            for entry in section['people']:
                entry['identity_group'] = speakers[entry['speaker']]['identity_group']
        w.write(root / 'insights.json', insights)
        return w.commit(root, state, {'kind': 'insights_updated', 'actor': 'assistant'})


def quote(text, limit=120):
    text = re.sub(r'\s+', ' ', text).strip()
    return text if len(text) <= limit else text[:limit - 1] + '…'


def markdown(root, state, sid, section, corrections, protected, shown_names, links, topic, meeting_date=None):
    root = Path(root)
    segments = w.read(next(s for s in state['sources'] if s['source_id'] == sid)['path'])['segments']
    speakers = {s['key']: s for s in state['speakers'] if s['source_id'] == sid}
    esc = minutes_md.escape

    def label(item):
        basis = item.get('basis', 'fact')
        return BASIS[basis] + (' · ' + CONFIDENCE[item['confidence']] if basis == 'inference' else '')

    def evidence(item):
        lines = []
        for i in sorted(item['evidence'], key=lambda i: segments[i]['start_ms']):
            speaker = speaker_of(state, sid, segments[i])
            who = minutes_html.shown_name(speaker, shown_names) if speaker else '未分离'
            text = w.corrected_text(segments[i]['text'], sid, i, corrections, protected)
            lines.append(f'  > `{w.timestamp(segments[i]["start_ms"])}` {esc(who)}：{esc(quote(text))}')
        return lines

    def person_name(speaker):
        name = speaker.get('display_name') or (speaker.get('role_name') or speaker['key']).split('｜')[0]
        return minutes_md.link({'name': name, 'shown': minutes_html.shown_name(speaker, shown_names)}, links)

    lines = minutes_md.front([('title', f'{topic} 深度洞察'), ('date', meeting_date), ('tags', ['深度洞察', '私密']),
                              ('private', True), ('created_by', 'meeting-offline')])
    lines += [f'# {esc(topic)} · 深度洞察', '',
              '> 仅供你本人参考：这是 AI 根据会上发言做的分析，标“推断”的不是事实，请结合你的了解判断。'
              f'转发会议文件夹前请删除本文件。[会议纪要]({minutes_md.target(minutes_md.MINUTES)})', '']
    for entry in section['people']:
        speaker = speakers[entry['speaker']]
        lines += [f'## {person_name(speaker)}', '']
        for kind, title in SECTIONS:
            items = entry.get(kind, [])
            if not items:
                continue
            lines += [f'**{title}**', '']
            for item in items:
                lines.append(f'- {esc(item["text"])}（{label(item)}）')
                lines += evidence(item)
                for ref in item.get('past', []):
                    past = resolve_past(root, ref, speaker['identity_group'])
                    when = past['meeting_date'] or '日期未知'
                    lines.append(f'  > {esc(when)}《{esc(past["topic"])}》 `{past["time"]}`：{esc(quote(past["text"]))}')
            lines.append('')
    if section.get('dynamics'):
        lines += ['## 整体', '']
        for item in section['dynamics']:
            lines.append(f'- {esc(item["text"])}（{label(item)}）')
            lines += evidence(item)
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    h = sub.add_parser('history'); h.add_argument('--workspace', required=True); h.add_argument('--out', required=True)
    h.add_argument('--meetings', type=int, default=5)
    s = sub.add_parser('save'); s.add_argument('--workspace', required=True); s.add_argument('--file', required=True)
    s.add_argument('--revision', type=int, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'history':
            result = history(args.workspace, args.meetings)
            device_context.atomic_json(Path(args.out).expanduser(), result)
            result = {'out': args.out, 'people': [{'name': p['name'], 'earlier_meetings': len(p['meetings'])} for p in result['people']]}
        else:
            exported = save(args.workspace, w.read(args.file), args.revision)
            result = {sid: files.get('insight_md') for sid, files in exported['files'].items()}
    except (ValueError, OSError, KeyError) as error:
        parser.exit(2, str(error) + '\n')
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2); print()


if __name__ == '__main__':
    main()
