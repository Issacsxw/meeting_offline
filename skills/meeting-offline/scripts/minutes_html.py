"""Generate a portable, offline editable HTML with native PNG export."""
import argparse
import html
import json
from pathlib import Path
import terms


TEMPLATE = Path(__file__).resolve().parents[1] / 'assets/minutes.html'
UNKNOWN_ROLE = '身份未确认'
# Styles offered in the editor's 样式 menu; keys match data-theme in the template.
THEMES = {'warm': '暖白陶土', 'business': '商务蓝', 'mono': '极简黑白', 'fresh': '清新绿', 'dark': '深色'}
# How settled a point is; shown as a tag on the point in every layout.
STATUS = {'decided': '已决定', 'direction': '推进方向', 'proposal': '建议', 'open': '讨论中'}
# Layouts offered in the 版式 menu, combinable with any theme; keys match data-layout.
LAYOUTS = {'classic': '经典', 'official': '公文', 'cards': '卡片', 'timeline': '时间轴', 'checklist': '极简清单'}


def render(document):
    if not isinstance(document, dict):
        raise ValueError('Meeting document must be an object.')
    for field in ('topic', 'meeting_time', 'participants'):
        if not isinstance(document.get(field), str):
            raise ValueError('Missing text field: ' + field)
    if 'summary' in document and not isinstance(document['summary'], str):
        raise ValueError('Summary must be text.')
    if document.get('theme', 'warm') not in THEMES:
        raise ValueError('未知样式：' + str(document.get('theme')) + '；可选 ' + '、'.join(THEMES))
    if document.get('layout', 'classic') not in LAYOUTS:
        raise ValueError('未知版式：' + str(document.get('layout')) + '；可选 ' + '、'.join(LAYOUTS))
    for field, required in [('points', ('title', 'text')), ('todos', ('task', 'owner', 'deadline'))]:
        if not isinstance(document.get(field), list):
            raise ValueError('Missing list: ' + field)
        for item in document[field]:
            if not isinstance(item, dict) or any(not isinstance(item.get(k), str) for k in required):
                raise ValueError('Invalid ' + field + ' item.')
            if field == 'points' and item.get('status') not in (None, '', *STATUS):
                raise ValueError('要点状态只能是 ' + '、'.join(STATUS))
    # people carries confirmed names for the Markdown links; the shared page only shows display names.
    document = {k: v for k, v in document.items() if k != 'people'}
    payload = json.dumps(document, ensure_ascii=False).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    source = TEMPLATE.read_text(encoding='utf-8').replace('__MEETING_DATA__', payload)
    return source.replace('<title>会议纪要</title>', '<title>' + html.escape(document['topic']) + '</title>')


def png_for(html_path, out=None):
    """Export the PNG next to the HTML; a failed render never blocks the HTML or role/term saves."""
    import render_png
    import subprocess
    try:
        return render_png.render(html_path, out)
    except (render_png.BrowserNotFound, RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        return {'png': None, 'png_error': str(error)}


def shown_name(speaker, display_names=None):
    """How a speaker reads in the minutes: the confirmed name through the device's display_names, else the label."""
    if speaker.get('diarization_unassigned') and not speaker.get('identity_group'):
        return '说话人未分离'
    if speaker.get('accepted_unknown') or speaker.get('role_name') == UNKNOWN_ROLE:
        return f'{UNKNOWN_ROLE}（{speaker["key"]}）'
    if not speaker.get('role_confirmed'):
        return speaker['key']
    name = speaker.get('display_name') or (speaker.get('role_name') or speaker['key']).split('｜')[0]
    return (display_names or {}).get(name, name)


def from_workspace(state, sid, section, corrections, protected=(), display_names=None, theme='warm', layout='classic'):
    """Use assistant-authored concise prose; never infer owners or summaries here.

    display_names is the device preference mapping a confirmed name to how it reads in the minutes."""
    speakers = {s['key']: s for s in state['speakers'] if s['source_id'] == sid and not s.get('ignored_by_user')}
    display_names = display_names or {}
    def confirmed(key):
        speaker = speakers[key]
        # Workspaces from before display_name stored the role title after '｜' in role_name.
        return speaker.get('display_name') or (speaker.get('role_name') or key).split('｜')[0]
    def display(key):
        if not key:
            return '—'
        return shown_name(speakers[key], display_names)
    def corrected(text):
        return text if section.get('automatic_content') else terms.correct_derived(text, sid, corrections, protected)
    brief = section.get('reader', {})
    points = []
    if brief.get('points'):
        points = [{'title': corrected(p['title']), 'text': corrected(p['text']),
                   **({'status': p['status']} if p.get('status') else {})} for p in brief['points']]
    else:
        # A fallback preserves all authored viewpoints; brevity requires an authored reader section.
        for speaker in speakers.values():
            prose = [p if isinstance(p, str) else p.get('text', '') for p in speaker.get('core_points', [])]
            if prose:
                points.append({'title': display(speaker['key']), 'text': corrected('；'.join(prose))})
        for item in section.get('decisions', []):
            label = {'direction': '推进方向', 'proposal': '建议', 'decided': '已决定', 'open': '待定'}[item['status']]
            points.append({'title': label, 'text': corrected(item['text'])})
    if section.get('automatic_content'):
        for item in section.get('decisions', []):
            points.append({'title': '议题与决定', 'text': corrected(item['text']), 'status': item['status']})
        for item in section.get('open_questions', []):
            points.append({'title': '待明确事项', 'text': corrected(item['text']), 'status': 'open'})
    overrides = {item['action_index']: item for item in brief.get('todos', [])}
    todos = []
    for i, action in enumerate(section.get('actions', [])):
        override = overrides.get(i, {})
        todos.append({'task': corrected(override.get('task') or action['task']),
            'owner': display(action.get('owner')),
            'deadline': override.get('deadline') or action.get('deadline_text') or '—', 'done': False})
    people = []
    identities = set()
    unknown = 0
    for key, speaker in speakers.items():
        if not speaker.get('role_confirmed'): continue
        if speaker.get('diarization_unassigned') and not speaker.get('identity_group'):
            continue  # unattributed speech is not a participant
        if speaker.get('accepted_unknown') or speaker.get('role_name') == UNKNOWN_ROLE:
            unknown += 1
            continue
        identity = speaker.get('identity_group') or key
        if identity not in identities:
            identities.add(identity)
            people.append({'name': confirmed(key), 'shown': display(key)})
    participants = '、'.join(p['shown'] for p in people)
    if unknown:
        participants = (participants + '等' if participants else '') + str(len(people) + unknown) + '人'
    return {'schema_version': 2, 'source_id': sid, 'theme': theme, 'layout': layout,
        'topic': corrected(brief.get('topic') or section.get('title') or '会议纪要'),
        'meeting_time': str(section.get('meeting_time') or section.get('meeting_date') or ''),
        'participants': participants, 'people': people,
        'summary': corrected(brief.get('summary', '')),
        'points': points, 'todos': todos}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--document', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--no-png', action='store_true', help='只生成 HTML，不自动导出同名 PNG')
    parser.add_argument('--theme', choices=THEMES, help='纪要配色：' + '、'.join(f'{k}（{v}）' for k, v in THEMES.items()))
    parser.add_argument('--layout', choices=LAYOUTS, help='纪要版式：' + '、'.join(f'{k}（{v}）' for k, v in LAYOUTS.items()))
    args = parser.parse_args()
    document = json.loads(args.document.read_text(encoding='utf-8'))
    if args.theme:
        document['theme'] = args.theme
    if args.layout:
        document['layout'] = args.layout
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(document), encoding='utf-8')
    print(json.dumps({'html': str(args.out.resolve()), **({} if args.no_png else png_for(args.out))},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
