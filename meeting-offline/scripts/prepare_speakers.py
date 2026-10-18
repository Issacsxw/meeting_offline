#!/usr/bin/env python3
"""Group local Qwen segments into stable speaker evidence; no network or role inference."""
import argparse
import json
import math
from pathlib import Path


def label(speaker_id):
    if speaker_id is None:
        return 'UNASSIGNED'
    if type(speaker_id) is int and 0 <= speaker_id < 26:
        return chr(65 + speaker_id)
    return 'S' + str(speaker_id)


def prepare(sources):
    state = {'schema_version': 1, 'status': 'awaiting_roles', 'sources': [],
             'speakers': [], 'user_confirmed_merges': [], 'pending_role_keys': []}
    seen_sources = set()
    for spec in sources:
        source_id, sep, raw_path = spec.partition('=')
        if not sep or not source_id or not raw_path or ':' in source_id:
            raise ValueError('Use --source ID=/path/to/qwen.json; ID must not contain a colon.')
        if source_id in seen_sources:
            raise ValueError('Duplicate source ID: ' + source_id)
        seen_sources.add(source_id)
        path = Path(raw_path).expanduser().resolve()
        data = json.loads(path.read_text(encoding='utf-8'))
        segments = data.get('segments')
        if not isinstance(segments, list) or not segments:
            raise ValueError('Expected nonempty Qwen segments: ' + str(path))
        state['sources'].append({'source_id': source_id, 'path': str(path),
                                 'duration_seconds': data.get('duration_seconds'),
                                 'model': data.get('model')})
        grouped = {}
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict) or not isinstance(segment.get('text'), str):
                raise ValueError('Invalid text at segment ' + str(index))
            speaker_id = segment.get('speaker_id')
            if speaker_id is not None and type(speaker_id) not in (str, int):
                raise ValueError('Invalid speaker ID at segment ' + str(index))
            # Preserve type so a numeric 0 never merges with the string "0".
            group_key = (type(speaker_id).__name__, speaker_id)
            start, end = segment.get('start_ms'), segment.get('end_ms')
            for value in (start, end):
                if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                    raise ValueError('Invalid timestamp at segment ' + str(index))
            if start is not None and end is not None and start > end:
                raise ValueError('Reversed timestamps at segment ' + str(index))
            if group_key not in grouped:
                key = source_id + '-' + label(speaker_id)
                if any(key == item['key'] for item in grouped.values()):
                    raise ValueError('Speaker labels collide; normalize source IDs explicitly.')
                grouped[group_key] = {
                    'key': key, 'source_id': source_id, 'source_speaker_id': speaker_id,
                    'role_name': None, 'role_confirmed': False, 'identity_group': None,
                    'ignored_by_user': False, 'diarization_unassigned': speaker_id is None,
                    'core_points': [], 'utterances': []}
            grouped[group_key]['utterances'].append({
                'source_segment_index': index, 'start_ms': start,
                'end_ms': end, 'text': segment['text']})
        for speaker in grouped.values():
            state['speakers'].append(speaker)
            state['pending_role_keys'].append(speaker['key'])
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', action='append', required=True, metavar='ID=PATH')
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    try:
        state = prepare(args.source)
        destination = Path(args.out).expanduser()
        # Never replace role mappings that may already contain user input.
        with destination.open('x', encoding='utf-8') as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc) + '\n')
    print('Prepared ' + str(len(state['speakers'])) + ' speaker groups; roles await user input.')


if __name__ == '__main__':
    main()
