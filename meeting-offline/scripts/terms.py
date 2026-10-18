"""Standalone term matching shared by review cards, scoped corrections and derived prose."""
import re


ASCII_WORD = re.compile(r'[A-Za-z0-9]')


def known_forms(corrections=(), candidates=(), store_terms=()):
    """Every spelling the workspace knows about; longer ones shield their shorter substrings."""
    forms = set()
    for correction in corrections:
        if correction.get('active', True):
            forms.update((correction['heard'], correction['canonical']))
    for candidate in candidates:
        forms.update(candidate.get('forms', []))
        forms.update(o['text'] for o in candidate.get('options', []) if isinstance(o, dict) and o.get('text'))
    for term in store_terms:
        if term.get('active', True):
            forms.add(term['canonical']); forms.update(term.get('aliases', []))
    return forms


def spans(text, heard, canonical=None, protected=()):
    """Occurrences of heard that are not part of canonical, a longer known form, or an ASCII word."""
    if not heard:
        return []
    blocked = []
    for longer in {canonical, *protected}:
        if longer and longer != heard and heard in longer:
            blocked += [m.span() for m in re.finditer(re.escape(longer), text)]
    found = []
    for match in re.finditer(re.escape(heard), text):
        start, end = match.span()
        if any(b_start <= start and end <= b_end for b_start, b_end in blocked):
            continue
        if ASCII_WORD.match(heard[0]) and start and ASCII_WORD.match(text[start - 1]):
            continue
        if ASCII_WORD.match(heard[-1]) and end < len(text) and ASCII_WORD.match(text[end]):
            continue
        found.append((start, end))
    return found


def contains(text, heard, canonical=None, protected=()):
    return bool(spans(text, heard, canonical, protected))


def replace(text, heard, canonical, protected=()):
    for start, end in reversed(spans(text, heard, canonical, protected)):
        text = text[:start] + canonical + text[end:]
    return text


def correct_derived(text, source_id, corrections, protected=()):
    """Apply only corrections confirmed for the whole recording to assistant-written prose."""
    for correction in corrections:
        if correction.get('active', True) and correction.get('apply_to_derived', True) and correction['source_id'] == source_id:
            text = replace(text, correction['heard'], correction['canonical'], protected)
    return text
