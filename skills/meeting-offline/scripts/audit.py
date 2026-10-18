"""Check that what the minutes say is what the cited words say: numbers, dates, names, deadlines, negation, status.

  workspace.py audit --workspace W                         list what does not match its evidence
  workspace.py audit --workspace W --resolve F --revision N  record how each item was settled

A heuristic, not a reader: it catches a changed amount, a lost 不, a moved date or a 已决定 built on
“再考虑”, and leaves the judgement to the assistant and the user. No network, no model.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

STATUS = {'decided': '已决定', 'direction': '推进方向', 'proposal': '建议', 'open': '讨论中'}
CN = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
CN_CHARS = '零〇一二两三四五六七八九十百千万亿'
# Chinese numerals count as a number only next to a measure: 三方, 一起 and 万一 are words, not amounts.
MEASURE = '元块万亿千百个人天周月号日点成倍次台件家条份年岁期批轮名位款项张单'
WEEKDAY = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '日': 7, '天': 7}
RELATIVE = ('今天', '明天', '后天', '今晚', '明早', '月底', '月初', '月中', '年底', '年初', '季度末', '下个月', '下月', '下周', '本周', '这周', '月内', '周内')
NEGATION = re.compile('不|没|未|无|别|勿|取消|暂停|否决|拒绝|放弃')
NOT_NEGATION = ('不错', '不过', '没问题', '无论', '不管', '不仅', '差不多', '不少', '不断', '不然', '不如', '无所谓', '不一定',
                '要不', '不好意思', '不要紧', '了不起', '不得不', '不一会', '无关', '别的', '别人', '没多久')
HEDGE = ('考虑', '可能', '也许', '建议', '要不', '再看', '再议', '讨论', '商量', '待定', '看情况', '评估', '倾向', '如果', '尽量', '争取', '初步', '想想', '研究一下')
DECISIVE = ('定了', '决定', '就这么', '确定', '拍板', '同意', '就按', '按这个', '没问题', '可以', '行', '好的', '就这样', '定下来')


def cn_number(text):
    """十六 → 16, 两万五 → 25000, 一百二 → 120, 一万零五 → 10005; None when it is not a plain numeral."""
    if not text or any(c not in CN_CHARS for c in text):
        return None
    total, section, digit, last_unit, zero = 0, 0, None, 1, False
    for char in text:
        if char in '零〇':
            zero = True
        elif char in CN:
            if digit is not None:  # 一二三 read digit by digit
                section = section * 10 + digit
            digit = CN[char]
        elif char in '十百千':
            unit = {'十': 10, '百': 100, '千': 1000}[char]
            section += (1 if digit is None else digit) * unit
            digit, last_unit, zero = None, unit, False
        else:  # 万 亿
            unit = 10000 if char == '万' else 100000000
            total += (section + (digit or 0)) * unit
            section, digit, last_unit, zero = 0, None, unit, False
    if digit is not None:
        if last_unit == 1 and section:  # 一二三: digit by digit
            return total + section * 10 + digit
        section += digit * (1 if zero or last_unit < 100 else last_unit // 10)  # 一百二 → 120, 两万五 → 25000
    return total + section


def number(value):
    """A canonical spelling for a number, so 16, 16.0 and 十六 compare equal."""
    value = round(float(value), 4)
    return str(int(value)) if value == int(value) else str(value)


UNIT = {'万': 10000, 'w': 10000, 'W': 10000, '亿': 100000000, 'k': 1000, 'K': 1000, '千': 1000}
DATE = re.compile(r'(\d{1,2}|[一二三四五六七八九十]{1,3})\s*月\s*(\d{1,2}|[一二三四五六七八九十]{1,3})\s*[日号]')
DAY = re.compile(r'(\d{1,2}|[一二三四五六七八九十]{1,3})\s*[日号](?![子常期])')
WEEK = re.compile(r'(下下|下|这|本|上)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天])')
PERCENT = re.compile(r'(\d+(?:\.\d+)?)\s*%|百分之([零〇一二两三四五六七八九十百]+|\d+(?:\.\d+)?)|([一二三四五六七八九]|\d)\s*成(?![本功员绩为交])')
ARABIC = re.compile(r'(\d+(?:\.\d+)?)\s*(万|亿|千|k|K|w|W)?')
CHINESE = re.compile(r'([零〇一二两三四五六七八九十百千万亿]{1,8})\s*(?=[' + MEASURE + r'])')


def as_int(token):
    return int(token) if token.isdigit() else cn_number(token)


def facts(text):
    """{('number', '16'), ('date', '10-20'), ('day', '16'), ('week', 'next-5'), ('percent', '30'), ('when', '月底')}"""
    found, text = set(), str(text or '')
    rest = text
    for m in DATE.finditer(text):
        month, day = as_int(m.group(1)), as_int(m.group(2))
        if month and day:
            found.add(('date', f'{month}-{day}'))
    rest = DATE.sub(' ', rest)
    for m in DAY.finditer(rest):
        day = as_int(m.group(1))
        if day and day <= 31:
            found.add(('day', str(day)))
    rest = DAY.sub(' ', rest)
    for m in WEEK.finditer(rest):
        prefix = {'下': 'next', '下下': 'next2', '上': 'last'}.get(m.group(1) or '', 'this')
        found.add(('week', f'{prefix}-{WEEKDAY[m.group(2)]}'))
    rest = WEEK.sub(' ', rest)
    for m in PERCENT.finditer(rest):
        if m.group(1):
            found.add(('percent', number(m.group(1))))
        elif m.group(2):
            value = float(m.group(2)) if m.group(2)[0].isdigit() else cn_number(m.group(2))
            if value is not None:
                found.add(('percent', number(value)))
        else:
            value = as_int(m.group(3))
            found.add(('percent', number(value * 10)))
    rest = PERCENT.sub(' ', rest)
    for word in RELATIVE:
        if word in rest:
            found.add(('when', '这周' if word in ('本周', '这周') else '下个月' if word == '下月' else word))
    for m in ARABIC.finditer(rest):
        value = float(m.group(1)) * UNIT.get(m.group(2) or '', 1)
        found.add(('number', number(value)))
    for m in CHINESE.finditer(ARABIC.sub(' ', rest)):
        value = cn_number(m.group(1))
        if value is not None and m.group(1) not in ('万',):
            found.add(('number', number(value)))
    return found


def supported(fact, evidence):
    kind, value = fact
    if fact in evidence:
        return True
    if kind == 'date':  # “10 月 16 日” is backed by “16 号” said in the meeting
        return ('day', value.split('-')[1]) in evidence
    if kind == 'week' and value.startswith('this-'):  # 周五 and 这周五 are the same day
        return any(k == 'week' and v.endswith(value[4:]) and v.startswith('this') for k, v in evidence)
    if kind == 'number':  # 1.5万 and 15000 already compare equal; also accept a percent said as a number
        return ('percent', value) in evidence
    return False


def negations(text):
    """What is negated: 不打折 → 打折, 没确定 → 确定. Set phrases like 没问题 or 要不 are not negations."""
    text = str(text or '')
    for phrase in NOT_NEGATION:
        text = text.replace(phrase, '□' * len(phrase))
    return [text[m.end():m.end() + 2] for m in NEGATION.finditer(text) if re.match(r'[一-鿿]{2}', text[m.end():m.end() + 2])]


def negated(text):
    return bool(negations(text))


LABEL = {'number': '数字或金额', 'percent': '比例', 'date': '日期', 'day': '日期', 'week': '星期', 'when': '时间说法'}
DAYS = '一二三四五六日'


def pretty(fact):
    kind, value = fact
    if kind == 'date':
        month, day = value.split('-'); return f'{month}月{day}日'
    if kind == 'day':
        return value + '号'
    if kind == 'week':
        prefix, day = value.split('-'); return {'next': '下', 'next2': '下下', 'last': '上', 'this': ''}[prefix] + '周' + DAYS[int(day) - 1]
    if kind == 'percent':
        return value + '%'
    return value


def issue(sid, where, kind, claim, detail, refs):
    key = hashlib.sha256(json.dumps([sid, where, kind, detail], ensure_ascii=False).encode()).hexdigest()[:12]
    return {'id': key, 'source_id': sid, 'where': where, 'kind': kind, 'claim': claim, 'detail': detail, 'evidence': refs}


def check_claim(sid, where, claim, refs, texts, names=(), allowed_names=(), status=None):
    """Problems with one piece of minutes text against the segments it cites."""
    problems, said = [], [texts[i] for i in refs if 0 <= i < len(texts)]
    evidence = set().union(*(facts(t) for t in said)) if said else set()
    for fact in sorted(facts(claim)):
        if not supported(fact, evidence):
            problems.append(issue(sid, where, 'fact', claim, f'{LABEL[fact[0]]}“{pretty(fact)}”在引用原话里找不到', refs))
    joined = '\n'.join(said)
    for name in names:
        if name and name in claim and name not in joined and name not in allowed_names:
            problems.append(issue(sid, where, 'name', claim, f'人名“{name}”不在引用原话里，也不是这条的发言人', refs))
    said_no, claim_no = {p for t in said for p in negations(t)}, set(negations(claim))
    for what in sorted(said_no - claim_no):  # 原话“不打折”，纪要写成“打折”
        if what in claim:
            problems.append(issue(sid, where, 'negation', claim, f'原话是否定的“…{what}”（不/没/未），纪要里写成了肯定', refs))
    for what in sorted(claim_no - said_no):  # 纪要“不打折”，原话只说“打折”
        if what in joined or not said_no:
            problems.append(issue(sid, where, 'negation', claim, f'纪要说“不/没{what}”，引用原话里没有这个否定', refs))
    if status == 'decided' and said and any(h in joined for h in HEDGE) and not any(d in joined for d in DECISIVE):
        problems.append(issue(sid, where, 'status', claim, '标为“已决定”，但原话是“考虑/建议/再看看”之类的说法', refs))
    return problems


def audit(root):
    """Every mismatch between the reader-facing minutes and their evidence, per source."""
    import workspace as w
    root = Path(root)
    state = w.read(root / 'state.json'); w.assert_workspace(root, state)
    analysis = w.read(root / 'analysis.json')
    corrections = w.read(root / 'corrections.json')
    protected = w.protected_forms(state, corrections)
    problems = []
    for source in state['sources']:
        sid = source['source_id']
        section = analysis['sources'].get(sid)
        if not section:
            continue
        segments = w.read(source['path'])['segments']
        texts = [w.corrected_text(s['text'], sid, i, corrections, protected) for i, s in enumerate(segments)]
        speakers = {s['key']: s for s in state['speakers'] if s['source_id'] == sid}
        names = sorted({(s.get('display_name') or (s.get('role_name') or '').split('｜')[0]) for s in speakers.values()
                        if s.get('role_confirmed') and s.get('identity_group')} - {''}, key=len, reverse=True)
        name_of = lambda key: (speakers[key].get('display_name') or (speakers[key].get('role_name') or '').split('｜')[0]) if key in speakers else ''
        reader = section.get('reader', {})
        if reader.get('summary'):
            problems += check_claim(sid, 'summary', reader['summary'], reader.get('summary_evidence', []), texts, names, names)
        for n, point in enumerate(reader.get('points', [])):
            where = f'points[{n}]'
            if point.get('status') not in STATUS:
                problems.append(issue(sid, where, 'status', point.get('title', ''), '要点没有标明状态（已决定 / 推进方向 / 建议 / 讨论中）', point.get('evidence', [])))
            allowed = [name_of(k) for k in point.get('attribution', [])]
            problems += check_claim(sid, where, point.get('title', '') + '：' + point.get('text', ''), point.get('evidence', []),
                                    texts, names, allowed, point.get('status'))
        overrides = {t['action_index']: t for t in reader.get('todos', [])}
        for n, action in enumerate(section.get('actions', [])):
            where, todo = f'actions[{n}]', overrides.get(n, {})
            task = todo.get('task') or action.get('task', '')
            allowed = [name_of(action.get('owner')), name_of(action.get('proposer'))]
            problems += check_claim(sid, where, task, action.get('evidence', []), texts, names, allowed)
            deadline = todo.get('deadline') or action.get('deadline_text')
            if deadline:
                for p in check_claim(sid, where + '.deadline', deadline, action.get('evidence', []), texts):
                    problems.append({**p, 'kind': 'deadline', 'detail': '期限' + p['detail']})
        for n, decision in enumerate(section.get('decisions', [])):
            problems += check_claim(sid, f'decisions[{n}]', decision.get('text', ''), decision.get('evidence', []), texts,
                                    names, [name_of(decision.get('proposer'))], decision.get('status'))
    return problems


RESOLUTIONS = {'fixed': '已改正文', 'supported': '原话确实支持（附说明）', 'user_confirmed': '用户确认无误'}


def report(root):
    """Current problems, which of them were already settled, and whether the minutes count as checked."""
    import workspace as w
    root = Path(root)
    latest = w.read(root / 'exports/latest.json')
    saved = w.read(root / 'semantic_review.json') if (root / 'semantic_review.json').exists() else {}
    settled = saved.get('resolutions', {})
    problems = audit(root)
    open_ = [p for p in problems if p['id'] not in settled]
    return {'version': latest['version'], 'checked': not problems or (not open_ and saved.get('version') == latest['version']),
            'unresolved': len(open_), 'issues': [{**p, 'resolution': settled.get(p['id'])} for p in problems]}


def resolve(root, resolutions, expected):
    """Record how each listed problem was settled; the minutes count as checked once none is left open."""
    import workspace as w
    root = Path(root)
    with w.locked(root):
        state = w.read(root / 'state.json'); w.check_revision(state, expected)
        latest = w.read(root / 'exports/latest.json')
        if latest['revision'] != state['revision']:
            raise ValueError('导出不是最新版本；请先运行 export。')
        problems = {p['id']: p for p in audit(root)}
        saved = w.read(root / 'semantic_review.json') if (root / 'semantic_review.json').exists() else {}
        settled = {k: v for k, v in saved.get('resolutions', {}).items() if k in problems}  # carry over what still applies
        if not isinstance(resolutions, dict):
            raise ValueError('确认结果应是 {问题 id: {resolution, note}}。')
        for key, value in resolutions.items():
            if key not in problems:
                raise ValueError(f'问题 {key} 已不存在（可能正文已改）；请重新运行 audit。')
            if not isinstance(value, dict) or value.get('resolution') not in ('supported', 'user_confirmed'):
                raise ValueError(f'{key}: resolution 只能是 supported（原话确实支持）或 user_confirmed（用户确认）；'
                                 '改了正文的问题会自动消失，不需要登记。')
            note = str(value.get('note', '')).strip()
            if not note:
                raise ValueError(f'{key}: 请写明依据，例如“十六号即 16 号”或“用户确认金额是 229 元”。')
            settled[key] = {'resolution': value['resolution'], 'note': note[:300], 'at': w.now()}
        open_ = [k for k in problems if k not in settled]
        record = {'version': latest['version'], 'revision': state['revision'], 'resolutions': settled,
                  'status': 'checked' if not open_ else 'pending', 'unresolved': open_, 'updated_at': w.now()}
        w.write(root / 'semantic_review.json', record)
        return {**record, 'issues': len(problems)}


def is_checked(root):
    """Nothing flagged, or every flagged item settled for this exact export."""
    import workspace as w
    root = Path(root)
    problems = audit(root)
    if not problems:
        return True
    if not (root / 'semantic_review.json').exists():
        return False
    saved, latest = w.read(root / 'semantic_review.json'), w.read(root / 'exports/latest.json')
    return saved.get('version') == latest['version'] and all(p['id'] in saved.get('resolutions', {}) for p in problems)
