"""Markdown copies of the minutes and the speaker-labelled transcript, for Obsidian or any editor.

File names inside links match the archived folder: 会议纪要.png / 会议纪要.html / 逐字稿.md.
"""
import re

MINUTES = '会议纪要.md'
TRANSCRIPT = '逐字稿.md'
PARAGRAPH_CHARS = 600  # start a new timestamped paragraph in long monologues


def escape(text):
    """Keep transcript text literal: no headings, lists, quotes, links or HTML from what someone said."""
    text = re.sub(r'([\\`*_\[\]<>|])', r'\\\1', str(text).replace('\r', ''))
    return re.sub(r'(?m)^(\s*)([#>+-]|\d+\.)', r'\1\\\2', text)


def yaml(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def link(person, links):
    """[[note|shown]] when the vault has a note for this confirmed name, plain text otherwise."""
    note = links.get(person['name'])
    if not note:
        return escape(person['shown'])
    return f'[[{note}]]' if note == person['shown'] else f'[[{note}|{person["shown"]}]]'


def target(name):
    return name.replace(' ', '%20').replace('(', '%28').replace(')', '%29')


def front(fields):
    lines = ['---']
    for key, value in fields:
        if isinstance(value, list):
            lines.append(f'{key}:')
            lines.extend(f'  - {yaml(v)}' for v in value)
        elif value:
            lines.append(f'{key}: {yaml(value)}')
    return lines + ['---', '']


def minutes(document, *, meeting_date=None, links=None, audio=None):
    links = links or {}
    people = document.get('people') or []
    topic = document['topic']
    named = [link(p, links) for p in people]
    lines = front([('title', topic), ('date', meeting_date), ('participants', [link(p, links) if links.get(p['name']) else p['shown'] for p in people]),
                   ('tags', ['会议纪要']), ('aliases', [f'{meeting_date} {topic}' if meeting_date else topic]),
                   ('created_by', 'meeting-offline')])
    lines += ['# ' + escape(topic), '']
    meta = []
    if document.get('meeting_time'):
        meta.append('时间：' + escape(document['meeting_time']))
    if document.get('participants'):
        plain = '、'.join(p['shown'] for p in people)
        # Linked names, keeping a suffix such as “等5人” for speakers whose identity was left unconfirmed.
        shown = ('、'.join(named) + escape(document['participants'][len(plain):])
                 if people and document['participants'].startswith(plain) else escape(document['participants']))
        meta.append('参会人：' + shown)
    if meta:
        lines += ['> ' + '　·　'.join(meta), '']
    if document.get('summary'):
        lines += ['## 概要', '', escape(document['summary']), '']
    if document['points']:
        lines += ['## 核心要点', '']
        for n, point in enumerate(document['points'], 1):
            lines += [f'### {n}. {escape(point["title"])}', '', escape(point['text']), '']
    lines += ['## 主要 Todo', '']
    if document['todos']:
        for todo in document['todos']:
            details = [f'负责人：{escape(todo["owner"])}' if todo['owner'] not in ('', '—') else '负责人待明确',
                       f'期限：{escape(todo["deadline"])}' if todo['deadline'] not in ('', '—') else '期限待明确']
            lines.append(f'- [{"x" if todo.get("done") else " "}] {escape(todo["task"])}（{"，".join(details)}）')
    else:
        lines.append('本次未形成明确待办。')
    lines += ['', '## 附件', '', '![会议纪要](会议纪要.png)', '',
              f'- [逐字稿]({target(TRANSCRIPT)})', '- [可编辑网页](会议纪要.html)']
    if audio:
        lines += ['', f'![录音]({target(audio)})']
    return '\n'.join(lines) + '\n'


def transcript(topic, segments, *, meeting_date=None, people=(), links=None):
    """segments: [{speaker, start_ms, text}] in order; consecutive turns by one speaker become a paragraph."""
    links = links or {}
    lines = front([('title', f'{topic} 逐字稿'), ('date', meeting_date),
                   ('participants', [link(p, links) if links.get(p['name']) else p['shown'] for p in people]),
                   ('tags', ['逐字稿']), ('created_by', 'meeting-offline')])
    lines += [f'# {escape(topic)} · 逐字稿', '',
              f'> 说话人由用户确认，专有名词按已确认的写法修正。[返回纪要]({target(MINUTES)})', '']
    current = None
    for segment in segments:
        text = segment['text'].strip()
        if not text:
            continue
        if current and current['speaker'] == segment['speaker'] and len(current['text']) + len(text) <= PARAGRAPH_CHARS:
            current['text'] += text
            continue
        if current:
            lines += paragraph(current)
        current = {'speaker': segment['speaker'], 'start_ms': segment['start_ms'], 'text': text}
    if current:
        lines += paragraph(current)
    return '\n'.join(lines).rstrip() + '\n'


def clock(ms):
    seconds = int(ms or 0) // 1000
    h, rest = divmod(seconds, 3600)
    return f'{h:02}:{rest // 60:02}:{rest % 60:02}'


def paragraph(turn):
    return [f'**{escape(turn["speaker"])}**　`{clock(turn["start_ms"])}`', escape(turn['text']), '']
