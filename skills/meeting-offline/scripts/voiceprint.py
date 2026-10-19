#!/usr/bin/env python3
"""Opt-in, device-local voice suggestions. A suggestion never confirms identity."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import wave

import device_context
from file_lock import locked_file

# Conservative starting points, NOT calibrated probabilities. See references/voiceprint.md.
SIMILARITY_THRESHOLD = 0.80
SIMILARITY_MARGIN = 0.10
MIN_SEGMENT_SECONDS = 2.0
MIN_TOTAL_SECONDS = 10.0
MAX_TOTAL_SECONDS = 60.0
MODEL_ID = '3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx'


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = None
    try:
        fd, name = tempfile.mkstemp(prefix='.voiceprint-', dir=str(path.parent))
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write('\n'); out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def location():
    return device_context.paths()['data_root'] / 'voiceprints.json'


def model_path():
    return device_context.paths()['data_root'] / 'models' / 'voiceprint' / MODEL_ID


def status():
    if device_context.sandbox():
        return {'enabled': False, 'available': False, 'reason': 'sandbox_disabled'}
    settings = device_context.minutes_preferences().get('voiceprint', {})
    enabled = isinstance(settings, dict) and settings.get('enabled') is True and bool(settings.get('consented_at'))
    available = enabled and model_path().is_file() and importlib.util.find_spec('sherpa_onnx') is not None
    return {'enabled': enabled, 'available': available,
            'consented_at': settings.get('consented_at') if isinstance(settings, dict) else None,
            'reason': 'ready' if available else ('dependencies_missing' if enabled else 'consent_required')}


def set_preference(value):
    paths = device_context.paths(); path = paths['preferences']
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked_file(path.with_suffix('.lock')):
        stored = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if stored.get('device_id') not in (None, paths['device_id']):
            raise ValueError('Preferences belong to another device.')
        section = stored.setdefault('meeting_offline', dict(stored.get('meeting_minutes') or {}))
        section['voiceprint'] = value
        stored['device_id'] = paths['device_id']
        atomic_write(path, stored)


def enable(user_consented=False, install_dependencies=False):
    if device_context.sandbox():
        raise ValueError('Voiceprints are unavailable in cloud sandbox mode.')
    if user_consented is not True:
        raise ValueError('Explain biometric local storage and deletion, obtain user consent first; then use --user-consented.')
    # No install, import of the optional engine, or download occurs before this consent gate.
    set_preference({'enabled': True, 'consented_at': now()})
    if install_dependencies:
        if importlib.util.find_spec('sherpa_onnx') is None:
            subprocess.run([sys.executable, '-m', 'pip', 'install', 'sherpa-onnx'], check=True)
        from transcribe_local import EMBEDDING_URL, model_file
        model_file(EMBEDDING_URL, model_path().parent)
    return status()


def read_library():
    path = location()
    if not path.exists():
        return {'schema_version': 1, 'device_id': device_context.device_id(), 'model': MODEL_ID, 'profiles': {}}
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema_version') != 1 or data.get('device_id') != device_context.device_id() or \
            data.get('model') != MODEL_ID or not isinstance(data.get('profiles'), dict):
        raise ValueError('Incompatible voiceprint library; existing data left unchanged.')
    return data


def forget(profile_id):
    path = location()
    if not path.exists():
        return {'removed': False}
    with locked_file(path.with_suffix('.lock')):
        data = read_library(); removed = data['profiles'].pop(profile_id, None) is not None
        if removed:
            atomic_write(path, data)
    return {'removed': removed}


def off():
    # Disable before deleting, and share the enrollment lock so no concurrent update survives.
    path = location(); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked_file(path.with_suffix('.lock')):
        set_preference({'enabled': False, 'disabled_at': now()})
        if path.exists():
            path.unlink()
    return {'enabled': False, 'deleted_all': True}


def unassigned(speaker):
    return speaker.get('diarization_unassigned') is True or str(speaker.get('key', '')).endswith('-UNASSIGNED')


def normalize(vector):
    if not isinstance(vector, (list, tuple)) or not vector or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
        raise ValueError('Invalid voice vector.')
    norm = math.sqrt(sum(x*x for x in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError('Empty voice vector.')
    return [x/norm for x in vector]


def cosine(left, right):
    left, right = normalize(left), normalize(right)
    if len(left) != len(right):
        raise ValueError('Voice vector dimensions differ.')
    return max(-1.0, min(1.0, sum(x*y for x, y in zip(left, right))))


def intervals(speaker):
    result = []
    for utterance in speaker.get('utterances', []):
        a, b = utterance.get('start_ms'), utterance.get('end_ms')
        if type(a) in (int, float) and type(b) in (int, float) and math.isfinite(a) and math.isfinite(b) and 0 <= a < b:
            result.append((a/1000.0, b/1000.0))
    return result


def reliable_segments(speaker, speakers):
    """Subtract all other/unknown speech, union duplicates, then apply duration gates."""
    if unassigned(speaker):
        return []
    merged = []
    for a, b in sorted(intervals(speaker)):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    others = [pair for other in speakers if other.get('source_id') == speaker.get('source_id') and
              other.get('key') != speaker.get('key') for pair in intervals(other)]
    for a, b in others:
        pieces = []
        for x, y in merged:
            if b <= x or a >= y:
                pieces.append((x, y))
            else:
                if x < a: pieces.append((x, a))
                if b < y: pieces.append((b, y))
        merged = pieces
    selected = []; total = 0.0
    for a, b in merged:
        length = min(b-a, MAX_TOTAL_SECONDS-total)
        if length >= MIN_SEGMENT_SECONDS:
            selected.append((a, a+length)); total += length
    return selected if total >= MIN_TOTAL_SECONDS else []


def choose_matches(vectors, profiles, reserved=()):
    """Greedy highest-confidence assignment; collision losers remain unknown (no fallback)."""
    candidates = []
    for key, vector in vectors.items():
        if key.endswith('-UNASSIGNED'):
            continue
        scores = []
        for pid, profile in profiles.items():
            try:
                scores.append((cosine(vector, profile['vector']), pid))
            except (ValueError, KeyError, TypeError):
                continue
        scores.sort(reverse=True)
        if not scores:
            continue
        best, pid = scores[0]; second = scores[1][0] if len(scores) > 1 else -1.0
        if best > SIMILARITY_THRESHOLD and best-second >= SIMILARITY_MARGIN:
            candidates.append((best, key, pid))
    used = set(reserved); matches = {}
    for score, key, pid in sorted(candidates, key=lambda x: (-x[0], x[1])):
        if pid not in used:
            matches[key] = {'profile_id': pid, 'similarity': score}; used.add(pid)
    return matches


def pcm_mono_16k(raw, width, channels, rate):
    """Decode integer PCM and linearly resample locally without optional libraries."""
    if width not in (1, 2, 3, 4) or channels < 1 or rate <= 0:
        raise ValueError('Unsupported PCM WAV format.')
    frame_size = width * channels
    if len(raw) % frame_size:
        raise ValueError('Truncated PCM audio frame.')
    mono = []
    scale = float(1 << (width * 8 - 1))
    for offset in range(0, len(raw), frame_size):
        values = []
        for channel in range(channels):
            start = offset + channel * width
            value = int.from_bytes(raw[start:start+width], 'little', signed=width != 1)
            if width == 1:
                value -= 128
            values.append(value / scale)
        mono.append(sum(values) / channels)
    if rate == 16000 or not mono:
        return mono
    count = int(len(mono) * 16000 / rate)
    converted = []
    for index in range(count):
        position = index * rate / 16000
        left = int(position); fraction = position - left
        right = min(left + 1, len(mono) - 1)
        converted.append(mono[left] * (1 - fraction) + mono[right] * fraction)
    return converted


def extract_vector(audio, clips):
    """Local PCM extraction. Optional codec/engine are never installed or downloaded here."""
    if not status()['enabled']:
        raise ValueError('Voiceprint consent is required.')
    if not model_path().is_file():
        raise ValueError('Voiceprint model missing; use enable --user-consented --install-dependencies after consent.')
    import sherpa_onnx
    from transcribe_local import to_wav
    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model_path()), num_threads=1, provider='cpu')
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
    vectors = []; weights = []
    with tempfile.TemporaryDirectory(prefix='meeting-voice-') as tmp:
        decoded = to_wav(Path(audio), tmp)
        with wave.open(str(decoded), 'rb') as handle:
            rate, channels, width = handle.getframerate(), handle.getnchannels(), handle.getsampwidth()
            if handle.getcomptype() != 'NONE':
                raise ValueError('Voiceprint requires uncompressed PCM WAV; convert locally first.')
            duration = handle.getnframes()/float(rate)
            if any(b > duration+1/rate for _, b in clips):
                raise ValueError('Transcript timestamps exceed the original audio.')
            for a, b in clips:
                handle.setpos(int(a*rate)); raw = handle.readframes(int((b-a)*rate))
                samples = pcm_mono_16k(raw, width, channels, rate)
                stream = extractor.create_stream(); stream.accept_waveform(16000, samples); stream.input_finished()
                if not extractor.is_ready(stream):
                    continue
                vector = normalize(list(extractor.compute(stream)))
                vectors.append(vector); weights.append(len(samples)/16000)
    if sum(weights) < MIN_TOTAL_SECONDS or not vectors:
        raise ValueError('Not enough usable speech for a voiceprint.')
    if any(len(v) != len(vectors[0]) for v in vectors):
        raise ValueError('Inconsistent voice vector dimensions.')
    return normalize([sum(v[i]*w for v, w in zip(vectors, weights))/sum(weights) for i in range(len(vectors[0]))])


def speaker_vectors(root, state, keys=None):
    sources = {s['source_id']: s for s in state.get('sources', [])}
    result = {}; skipped = {}
    for speaker in state.get('speakers', []):
        key = speaker['key']
        if keys is not None and key not in keys:
            continue
        clips = reliable_segments(speaker, state['speakers'])
        source = sources.get(speaker.get('source_id'), {})
        audio = source.get('audio_path')
        if not clips or not audio:
            skipped[key] = 'insufficient_reliable_audio'; continue
        try:
            if source.get('audio_sha256'):
                digest = hashlib.sha256()
                with Path(audio).open('rb') as original:
                    for chunk in iter(lambda: original.read(1024 * 1024), b''):
                        digest.update(chunk)
                if digest.hexdigest() != source['audio_sha256']:
                    raise ValueError('Original recording changed; voiceprint extraction refused.')
            result[key] = extract_vector(audio, clips)
        except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError, wave.Error) as error:
            skipped[key] = str(error)
    return result, skipped


def confirmed_profiles(state):
    import memory
    path = Path(state.get('memory_store') or device_context.paths()['memory'])
    return {p['id']: p for p in memory.read_store(path)['roles'] if p.get('active', True) and p.get('confirmed_by_user') is True and p.get('confirmation')}


def match_workspace(root, state=None):
    if state is None:
        import workspace
        root = Path(root)
        with workspace.locked(root):
            state = workspace.read(root/'state.json'); workspace.assert_workspace(root, state)
            before = json.dumps(state, sort_keys=True)
            result = match_workspace(root, state)
            state['voiceprint_match'] = result
            if before != json.dumps(state, sort_keys=True):
                workspace.commit(root, state, {'kind': 'voice_suggestions', 'actor': 'automatic'})
            return result
    device_context.require_device(state.get('device_id'))
    if not status()['enabled']:
        return {'enabled': False, 'matches': {}, 'reason': status()['reason']}
    known = confirmed_profiles(state)
    library = {pid: value for pid, value in read_library()['profiles'].items() if pid in known}
    keys = {s['key'] for s in state['speakers'] if not s.get('role_confirmed') and not s.get('ignored_by_user')}
    vectors, skipped = speaker_vectors(root, state, keys) if library else ({}, {})
    reserved = {s.get('identity_group') for s in state['speakers'] if s.get('role_confirmed')}
    if not status()['enabled']:
        return {'enabled': False, 'matches': {}, 'reason': 'disabled_during_match'}
    matches = choose_matches(vectors, library, reserved)
    for speaker in state['speakers']:
        if speaker['key'] not in keys:
            continue
        # Re-matching cannot retain an obsolete identity suggestion.
        if speaker.get('role_source') == 'voice_match':
            for field in ('role_source', 'voice_profile_id', 'voice_similarity', 'display_name', 'role_name'):
                speaker.pop(field, None)
        match = matches.get(speaker['key'])
        if match and not unassigned(speaker):
            profile = known[match['profile_id']]
            speaker.update(role_source='voice_match', voice_profile_id=profile['id'],
                           voice_similarity=match['similarity'], display_name=profile['name'],
                           role_name=profile['name'], role_confirmed=False, identity_group=None)
    return {'enabled': True, 'matches': matches, 'skipped': skipped,
            'reason': 'matched' if matches else 'no_match'}


def enroll_confirmed(root, state, speaker_keys):
    """Only call from user-confirmation routes, after confirmed memory has been committed."""
    device_context.require_device(state.get('device_id'))
    if not status()['enabled']:
        return {'enrolled': [], 'reason': 'disabled'}
    known = confirmed_profiles(state)
    eligible = {s['key']: s for s in state['speakers'] if s['key'] in speaker_keys and
                s.get('role_confirmed') is True and not s.get('accepted_unknown') and not unassigned(s) and
                s.get('identity_group') in known}
    vectors, skipped = speaker_vectors(root, state, set(eligible))
    if not vectors:
        return {'enrolled': [], 'skipped': skipped}
    path = location(); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked_file(path.with_suffix('.lock')):
        if not status()['enabled']:
            return {'enrolled': [], 'reason': 'disabled'}
        data = read_library(); enrolled = []
        for key, vector in vectors.items():
            speaker = eligible[key]; pid = speaker['identity_group']
            source = next(s for s in state['sources'] if s['source_id'] == speaker['source_id'])
            sample_id = hashlib.sha256(json.dumps([str(Path(root).resolve()), key, source.get('audio_sha256'),
                                                  intervals(speaker)], sort_keys=True).encode()).hexdigest()
            for previous_id, previous_record in list(data['profiles'].items()):
                if previous_id == pid:
                    continue
                old_samples = previous_record.get('sources', [])
                removed = [sample for sample in old_samples if sample.get('sample_id') == sample_id]
                if not removed:
                    continue
                remaining = [sample for sample in old_samples if sample.get('sample_id') != sample_id]
                if not remaining or any('vector' not in sample for sample in remaining):
                    # Never keep a contaminated center when its contribution cannot be undone.
                    del data['profiles'][previous_id]
                else:
                    center = [sum(sample['vector'][i] for sample in remaining)
                              for i in range(len(remaining[0]['vector']))]
                    previous_record.update(vector=normalize(center), vector_sum=center,
                                           count=len(remaining), sources=remaining, updated_at=now())
            old = data['profiles'].get(pid, {}); samples = old.get('sources', [])
            if any(s.get('sample_id') == sample_id for s in samples):
                continue
            vector = normalize(vector); n = old.get('count', 0)
            if n:
                previous = normalize(old['vector'])
                if len(previous) != len(vector):
                    raise ValueError('Voice model dimension changed; forget the old profile before enrolling.')
                # Store the sum, rather than repeatedly normalizing an arithmetic mean.
                total = old.get('vector_sum', [v*n for v in previous])
                total = [a+b for a, b in zip(total, vector)]
            else:
                total = vector
            data['profiles'][pid] = {'profile_id': pid, 'vector': normalize(total), 'vector_sum': total,
                'count': n+1, 'updated_at': now(), 'sources': samples + [{'sample_id': sample_id,
                'meeting': Path(root).name, 'source_id': source['source_id'], 'speaker_key': key,
                'confirmed_at': now(), 'vector': vector}]}
            enrolled.append(key)
        if enrolled:
            atomic_write(path, data)
    return {'enrolled': enrolled, 'skipped': skipped}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status'); sub.add_parser('off')
    enabled = sub.add_parser('enable'); enabled.add_argument('--user-consented', action='store_true')
    enabled.add_argument('--install-dependencies', action='store_true')
    match = sub.add_parser('match'); match.add_argument('--workspace', required=True, type=Path)
    forgotten = sub.add_parser('forget'); forgotten.add_argument('--profile', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'enable': result = enable(args.user_consented, args.install_dependencies)
        elif args.command == 'status': result = status()
        elif args.command == 'off': result = off()
        elif args.command == 'forget': result = forget(args.profile)
        else: result = match_workspace(args.workspace)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr); return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
