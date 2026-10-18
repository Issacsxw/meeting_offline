"""Generate a portable, offline editable HTML with native PNG export."""
import argparse
import html
import json
from pathlib import Path


TEMPLATE = Path(__file__).resolve().parents[1] / 'assets/minutes.html'


def render(document):
    if not isinstance(document, dict):
        raise ValueError('Meeting document must be an object.')
    for field in ('topic', 'meeting_time', 'participants'):
        if not isinstance(document.get(field), str):
            raise ValueError('Missing text field: ' + field)
    if 'summary' in document and not isinstance(document['summary'], str):
        raise ValueError('Summary must be text.')
    for field, required in [('points', ('title', 'text')), ('todos', ('task', 'owner', 'deadline'))]:
        if not isinstance(document.get(field), list):
            raise ValueError('Missing list: ' + field)
        for item in document[field]:
            if not isinstance(item, dict) or any(not isinstance(item.get(k), str) for k in required):
                raise ValueError('Invalid ' + field + ' item.')
    payload = json.dumps(document, ensure_ascii=False).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    source = TEMPLATE.read_text(encoding='utf-8').replace('__MEETING_DATA__', payload)
    return source.replace('<title>会议纪要</title>', '<title>' + html.escape(document['topic']) + '</title>')


def from_workspace(state, sid, section, corrections):
    """Use assistant-authored concise prose; never infer owners or summaries here."""
    speakers = {s['key']: s for s in state['speakers'] if s['source_id'] == sid and not s.get('ignored_by_user')}
    def display(key, short=False):
        if not key:
            return '—'
        name = speakers[key].get('role_name') or key
        if short:
            name = name.split('｜')[0]
        return name.replace('用户本人', '我')
    def corrected(text):
        for c in corrections:
            if c.get('active', True) and c.get('apply_to_derived', True) and c['source_id'] == sid:
                text = text.replace(c['heard'], c['canonical'])
        return text
    brief = section.get('reader', {})
    points = []
    if brief.get('points'):
        points = [{'title': corrected(p['title']),
                   'text': corrected(p['text'])} for p in brief['points']]
    else:
        # A fallback preserves all authored viewpoints; brevity requires an authored reader section.
        for speaker in speakers.values():
            prose = [p if isinstance(p, str) else p.get('text', '') for p in speaker.get('core_points', [])]
            if prose:
                points.append({'title': display(speaker['key'], True), 'text': corrected('；'.join(prose))})
        for item in section.get('decisions', []):
            label = {'direction': '推进方向', 'proposal': '建议', 'decided': '已决定', 'open': '待定'}[item['status']]
            points.append({'title': label, 'text': corrected(item['text'])})
    overrides = {item['action_index']: item for item in brief.get('todos', [])}
    todos = []
    for i, action in enumerate(section.get('actions', [])):
        override = overrides.get(i, {})
        todos.append({'task': corrected(override.get('task') or action['task']),
            'owner': display(action.get('owner'), True),
            'deadline': override.get('deadline') or action.get('deadline_text') or '—', 'done': False})
    people = []
    identities = set()
    unknown = 0
    for key, speaker in speakers.items():
        if speaker.get('role_name') == '身份未确认':
            unknown += 1
            continue
        identity = speaker.get('identity_group') or key
        if identity not in identities:
            identities.add(identity)
            people.append(display(key, True))
    participants = '、'.join(people)
    if unknown:
        participants = (participants + '等' if participants else '') + str(len(people) + unknown) + '人'
    return {'schema_version': 2, 'source_id': sid,
        'topic': corrected(brief.get('topic') or section.get('title') or '会议纪要'),
        'meeting_time': str(section.get('meeting_time') or section.get('meeting_date') or ''),
        'participants': participants,
        'summary': corrected(brief.get('summary', '')),
        'points': points, 'todos': todos}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--document', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    document = json.loads(args.document.read_text(encoding='utf-8'))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(document), encoding='utf-8')
    print(str(args.out.resolve()))


if __name__ == '__main__':
    main()
