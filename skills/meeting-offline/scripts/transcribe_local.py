#!/usr/bin/env python3
"""Transcribe one recording on this computer, with speakers separated by voice. Audio never leaves the machine.

Two optional engines, chosen by the user:
  funasr   FunASR Paraformer + CAM++ (best Chinese; needs PyTorch, models from ModelScope)
  whisper  faster-whisper + sherpa-onnx speaker diarization (lighter, multilingual; models from
           Hugging Face and GitHub)
The result has the same shape as transcribe_qwen.py output, so prepare_speakers.py takes it unchanged.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

import audio_duration
import device_context
import prepare_speakers

ENGINES = {
    'funasr': {'name': 'FunASR', 'modules': ['funasr', 'torch'], 'packages': 'funasr torch torchaudio modelscope'},
    'whisper': {'name': 'faster-whisper + sherpa-onnx', 'modules': ['faster_whisper', 'sherpa_onnx'],
                'packages': 'faster-whisper sherpa-onnx'},
}
WHISPER_MODELS = ('small', 'medium', 'large-v3', 'turbo')
WHISPER_PROMPT = '以下是普通话会议录音，请使用简体中文和标点。'
MAX_SEGMENT_MS = 30000  # keep merged segments short enough to quote and replay
# Invalidate final Whisper assignments made before bounded-pause matching. Raw text/turns remain reusable.
SPEAKER_ASSIGNMENT_VERSION = 2
# sherpa-onnx speaker diarization models (pyannote segmentation + 3D-Speaker embedding for Chinese).
SEGMENTATION_URL = ('https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/'
                    'sherpa-onnx-pyannote-segmentation-3-0.tar.bz2')
EMBEDDING_URL = ('https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/'
                 '3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx')


class LocalError(RuntimeError):
    pass


def installed(engine):
    return [m for m in ENGINES[engine]['modules'] if importlib.util.find_spec(m) is None]


def check():
    """Which engines can run here, and the exact install command for the current Python."""
    report = {'python': sys.executable, 'ffmpeg': bool(shutil.which('ffmpeg')), 'engines': {}}
    for engine, spec in ENGINES.items():
        missing = installed(engine)
        report['engines'][engine] = {
            'name': spec['name'], 'ready': not missing, 'missing': missing,
            'install': f'"{sys.executable}" -m pip install -U {spec["packages"]}'}
    return report


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for part in iter(lambda: handle.read(1048576), b''):
            h.update(part)
    return h.hexdigest()


def load_hotwords(path):
    """Same file as transcribe_qwen.py --hotwords: {"标准词": weight}. Local engines use the words, not weights."""
    if not path:
        return []
    try:
        words = json.loads(Path(path).expanduser().read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise LocalError('读不了热词文件：' + str(path)) from error
    if not isinstance(words, dict) or any(not isinstance(w, str) for w in words):
        raise LocalError('热词文件必须是 {"标准词": 权重} 形式的 JSON 对象。')
    # Highest weight first, then file order: engines cut long lists, so the most important words must lead.
    ranked = sorted(words, key=lambda w: -words[w] if isinstance(words[w], (int, float)) else 0)
    kept = list(dict.fromkeys(w.strip() for w in ranked if w.strip() and not any(c.isspace() for c in w.strip())))
    dropped = len({w.strip() for w in words if w.strip()}) - len(kept)
    if dropped:
        note(f'有 {dropped} 个热词包含空格，本地引擎无法使用，已跳过。')
    return kept[:1000]


def cache_path(audio_sha, engine, model, speakers=None, hotwords=()):
    # Without hotwords the name is the one earlier versions used, so their cached results are reused.
    tag = '-' + hashlib.sha256('\n'.join(hotwords).encode()).hexdigest()[:8] if hotwords else ''
    name = f'{audio_sha[:24]}-{engine}-{model}-{speakers or "auto"}{tag}.json'
    return device_context.paths()['jobs'] / 'local' / name


PAUSE_S = 1.0  # a word this close to a speaker turn is theirs; farther away it stays unassigned
SEPARATION_NOTE = {
    'partial': '注意：只有 {pct}% 的发言分出了说话人，其余标为“说话人未分离”。认领时这部分不能算到任何人头上。',
    'none': '注意：没有分出说话人，所有发言都标为“说话人未分离”。纪要里不能据此判断谁说了什么、谁负责。'}


def note(message):
    print(message, file=sys.stderr, flush=True)


def to_wav(audio, folder):
    """16 kHz mono WAV via ffmpeg when available; FunASR reads WAV without extra codecs."""
    if audio.suffix.lower() in ('.wav', '.wave') or not shutil.which('ffmpeg'):
        return audio
    target = Path(folder) / 'audio.wav'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(audio), '-ac', '1', '-ar', '16000', str(target)],
                   check=True, timeout=3600)
    return target


def run_funasr(audio, speakers=None, device='auto', hotwords=()):
    from funasr import AutoModel
    if device == 'auto':
        import torch
        device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
    model = AutoModel(model='paraformer-zh', vad_model='fsmn-vad', punc_model='ct-punc', spk_model='cam++',
                      device=device, disable_update=True)
    options = {'batch_size_s': 300}
    if hotwords:
        options['hotword'] = ' '.join(hotwords)  # paraformer-zh is SeACo-Paraformer, which biases toward these
    if speakers:
        options['preset_spk_num'] = speakers
    with tempfile.TemporaryDirectory(prefix='meeting-local-') as tmp:
        try:
            results = model.generate(input=str(to_wav(audio, tmp)), **options)
        except Exception as error:  # FunASR surfaces decoder problems as many exception types
            hint = '' if shutil.which('ffmpeg') else '；m4a/mp4 录音需要 ffmpeg（macOS: brew install ffmpeg；Windows: winget install ffmpeg）'
            raise LocalError('FunASR 识别失败：' + str(error) + hint) from error
    sentences = (results[0] if results else {}).get('sentence_info') or []
    segments = [{'speaker_id': int(s['spk']) if s.get('spk') is not None else None,
                 'start_ms': int(s['start']), 'end_ms': int(s['end']), 'text': s.get('text', '').strip()}
                for s in sentences if s.get('text', '').strip()]
    return 'paraformer-zh+cam++', segments


def model_file(url, folder, member=None):
    """Download a diarization model once into this device's data directory."""
    folder.mkdir(parents=True, exist_ok=True)
    name = member.split('/')[-1] if member else url.split('/')[-1]
    target = folder / (url.split('/')[-1].split('.tar')[0] + '-' + name if member else name)
    if target.exists():
        return target
    with tempfile.TemporaryDirectory(dir=folder) as tmp:
        download = Path(tmp) / url.split('/')[-1]
        note('首次使用，下载说话人分离模型：' + url)
        try:
            with urllib.request.urlopen(url, timeout=60) as response, download.open('wb') as handle:
                shutil.copyfileobj(response, handle)
        except OSError as error:
            raise LocalError(f'下载说话人分离模型失败：{url}（{error}）。可以手动下载后放到 {folder}') from error
        if member:
            with tarfile.open(download) as archive:
                source = archive.extractfile(member)
                if source is None:
                    raise LocalError('模型压缩包内容不符合预期：' + member)
                Path(tmp, name).write_bytes(source.read())
            Path(tmp, name).replace(target)
        else:
            download.replace(target)
    return target


def assign_speakers(pieces, turns):
    """Give each timed piece the speaker whose turns overlap it most, then merge runs of one speaker."""
    segments = []
    for start, end, text in pieces:
        best, overlap = None, 0.0
        for turn_start, turn_end, speaker in turns:
            shared = min(end, turn_end) - max(start, turn_start)
            if shared > overlap:
                best, overlap = speaker, shared
        if best is None and turns:  # inside a short pause: the nearest turn; farther away stays unassigned
            gap, near = min((max(t[0] - end, start - t[1], 0), t[2]) for t in turns)
            best = near if gap <= PAUSE_S else None
        last = segments[-1] if segments else None
        if (last and last['speaker_id'] == best and not last['text'].endswith(('。', '？', '！', '?', '!'))
                and end * 1000 - last['start_ms'] <= MAX_SEGMENT_MS):
            segments[-1]['end_ms'] = int(end * 1000); segments[-1]['text'] += text
        else:
            segments.append({'speaker_id': best, 'start_ms': int(start * 1000), 'end_ms': int(end * 1000), 'text': text})
    for segment in segments:
        segment['text'] = segment['text'].strip()
    return [s for s in segments if s['text']]


def stage_path(sha, name):
    return device_context.paths()['jobs'] / 'local' / f'{sha[:24]}-{name}.json'


def valid_cache_rows(rows, needs):
    """A parseable JSON list can still be an unusable transcription/checkpoint."""
    if not isinstance(rows, list) or (needs == 'segments' and not rows):
        return False
    for row in rows:
        if needs == 'segments':
            if not isinstance(row, dict) or not isinstance(row.get('text'), str):
                return False
            start, end, speaker = row.get('start_ms'), row.get('end_ms'), row.get('speaker_id')
        else:
            if not isinstance(row, (list, tuple)) or len(row) != 3:
                return False
            start, end, value = row
            if needs == 'pieces' and not isinstance(value, str):
                return False
            if needs == 'turns' and type(value) is not int:
                return False
            speaker = value if needs == 'turns' else None
        if speaker is not None and (type(speaker) is not int or speaker < 0):
            return False
        try:
            if (type(start) not in (int, float) or type(end) not in (int, float)
                    or not math.isfinite(start) or not math.isfinite(end) or not 0 <= start <= end
                    or (needs != 'segments' and not math.isfinite(end * 1000))):
                return False
        except OverflowError:
            return False
    return True


def load_json(path, needs):
    """A cached file, or None when missing or damaged (an interrupted write); damaged files are set aside."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        data = None
    if isinstance(data, dict):
        # Another device's file is refused, not discarded, even when its rows are unusable.
        if data.get('device_id') is not None:
            device_context.require_device(data['device_id'], path)
        if valid_cache_rows(data.get(needs), needs):
            device_context.require_device(data.get('device_id'), path)
            return data
    broken = path.with_name(path.name + '.damaged')
    path.replace(broken)
    note(f'缓存文件损坏（可能上次处理被中断），已移到 {broken.name}，这一步会重新计算。')
    return None


def save_json(path, value):
    device_context.atomic_json(path, {**value, 'device_id': device_context.device_id()})


def run_whisper(audio, model='small', speakers=None, device='auto', hotwords=(), sha=None, allow_unseparated=False,
                force=False):
    """Whisper text plus sherpa-onnx speakers, each step saved so a failure later never repeats an earlier one."""
    from faster_whisper import WhisperModel, decode_audio
    import sherpa_onnx
    folder = device_context.paths()['data_root'] / 'local-models'
    try:  # fetch the separation models before the long transcription, not after
        segmentation = model_file(SEGMENTATION_URL, folder, 'sherpa-onnx-pyannote-segmentation-3-0/model.onnx')
        embedding = model_file(EMBEDDING_URL, folder)
    except LocalError:
        if not allow_unseparated:
            raise
        segmentation = embedding = None
    tag = hashlib.sha256('\n'.join(hotwords).encode()).hexdigest()[:8] if hotwords else 'nohw'
    asr_path, samples = stage_path(sha, f'whisper-{model}-{tag}-text'), None
    saved = None if force else load_json(asr_path, 'pieces')
    if saved:
        pieces = [tuple(p) for p in saved['pieces']]
        note('复用已保存的转写文字，直接进行说话人分离。')
    else:
        samples = decode_audio(str(audio), sampling_rate=16000)
        whisper = WhisperModel(model, device=device, compute_type='default')
        options = {'language': 'zh', 'initial_prompt': WHISPER_PROMPT, 'vad_filter': True, 'word_timestamps': True}
        if hotwords:
            options['hotwords'] = '、'.join(hotwords)  # faster-whisper 1.0+; only passed when there are any
        parts, _ = whisper.transcribe(samples, **options)
        pieces = []
        for part in parts:
            words = part.words or []
            pieces += [(w.start, w.end, w.word) for w in words] if words else [(part.start, part.end, part.text)]
        save_json(asr_path, {'pieces': pieces})
    turns_path = stage_path(sha, f'sherpa-{speakers or "auto"}-speakers')
    saved = None if force else load_json(turns_path, 'turns')
    if saved:
        turns = [tuple(t) for t in saved['turns']]
    elif segmentation is None:
        turns = []
    else:
        try:
            config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
                segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                    pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(segmentation))),
                embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(embedding)),
                clustering=sherpa_onnx.FastClusteringConfig(num_clusters=speakers or -1, threshold=0.5))
            if not config.validate():
                raise LocalError('说话人分离模型配置无效，请删除 ' + str(folder) + ' 后重试。')
            if samples is None:
                samples = decode_audio(str(audio), sampling_rate=16000)
            result = sherpa_onnx.OfflineSpeakerDiarization(config).process(samples).sort_by_start_time()
            turns = [(t.start, t.end, int(t.speaker)) for t in result]
            save_json(turns_path, {'turns': turns})
        except Exception as error:  # sherpa-onnx raises several types; the text is already saved
            if not allow_unseparated:
                raise LocalError('说话人分离失败：' + str(error) + '。转写文字已保存，修好后再运行会直接从分离这一步开始；'
                                 '或者加 --allow-unseparated，先得到不区分说话人的结果。') from error
            turns = []
    return model, assign_speakers(pieces, turns)


def with_separation(result):
    """Status from the segments themselves; also corrects results cached by earlier versions, which always said 'voice'."""
    summary = prepare_speakers.separation(result['segments'])
    return {**result, 'speaker_separation': summary['status'], 'speaker_coverage': summary['coverage'],
            'speakers_detected': summary['speakers']}


def transcribe(audio, engine, model=None, speakers=None, device='auto', force=False, hotwords=(), allow_unseparated=False):
    audio = Path(audio).expanduser().resolve()
    if not audio.is_file():
        raise LocalError('录音不存在：' + str(audio))
    missing = installed(engine)
    if missing:
        raise LocalError(f'{ENGINES[engine]["name"]} 尚未安装（缺少 {", ".join(missing)}）。安装命令：'
                         + check()['engines'][engine]['install'])
    model = model or ('paraformer-zh+cam++' if engine == 'funasr' else 'small')
    sha = file_digest(audio)
    hotwords = list(hotwords)
    cached = cache_path(sha, engine, model, speakers, hotwords)
    result = None if force else load_json(cached, 'segments')
    if result and engine == 'whisper' and result.get('speaker_assignment_version') != SPEAKER_ASSIGNMENT_VERSION:
        result = None  # reassign from raw checkpoints instead of retaining obsolete speaker guesses
    if result:
        result = with_separation(result)
        # a result made with --allow-unseparated is not the answer to a normal run: try the separation again
        # (the text is checkpointed, so only that step runs)
        if not (engine == 'whisper' and result['speaker_separation'] == 'none' and not allow_unseparated):
            return {**result, 'result_path': str(cached), 'reused': True}
    started = datetime.now(timezone.utc)
    note(f'开始本地识别（{ENGINES[engine]["name"]}）。首次运行会先下载模型；录音不会离开这台电脑。')
    used, segments = (run_funasr(audio, speakers, device, hotwords) if engine == 'funasr'
                      else run_whisper(audio, model, speakers, device, hotwords, sha, allow_unseparated, force))
    if not segments:
        raise LocalError('没有识别出任何语音；请确认录音里有人说话。')
    try:
        length = audio_duration.duration(audio)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        length = segments[-1]['end_ms'] / 1000
    result = {'route': 'local', 'engine': engine, 'model': f'{engine}:{used}', 'device_id': device_context.device_id(),
              'audio_sha256': sha, 'duration_seconds': round(length, 3), 'hotwords_used': hotwords,
              'started_at': started.isoformat(), 'finished_at': datetime.now(timezone.utc).isoformat(),
              'text': ''.join(s['text'] for s in segments), 'segments': segments}
    if engine == 'whisper':
        result['speaker_assignment_version'] = SPEAKER_ASSIGNMENT_VERSION
    result = with_separation(result)
    device_context.atomic_json(cached, result)  # an interrupted run never leaves half a file behind
    return {**result, 'result_path': str(cached), 'reused': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--check', action='store_true', help='检查本机能用哪个引擎，并给出安装命令')
    parser.add_argument('--audio', help='一个录音文件')
    parser.add_argument('--engine', choices=ENGINES, help='funasr 或 whisper')
    parser.add_argument('--model', choices=WHISPER_MODELS, help='whisper 模型大小（默认 small）')
    parser.add_argument('--speakers', type=int, help='已知的发言人数，提高区分准确度（可选）')
    parser.add_argument('--device', default='auto', help='auto、cpu 或 cuda')
    parser.add_argument('--hf-mirror', action='store_true', help='从 hf-mirror.com 下载 whisper 模型（国内网络）')
    parser.add_argument('--hotwords', help='已确认标准词的 JSON（与 transcribe_qwen.py --hotwords 相同格式）')
    parser.add_argument('--force', action='store_true', help='忽略本机缓存，重新识别')
    parser.add_argument('--allow-unseparated', action='store_true',
                        help='说话人分离失败时仍输出结果（发言标为“说话人未分离”），只在用户同意后使用')
    args = parser.parse_args()
    if args.check:
        print(json.dumps(check(), ensure_ascii=False, indent=2)); return
    if not args.audio or not args.engine:
        parser.error('需要 --audio 和 --engine，或使用 --check。')
    if args.model and args.engine != 'whisper':
        parser.error('--model 只用于 whisper 引擎。')
    if args.speakers is not None and args.speakers < 1:
        parser.error('--speakers 必须是正整数。')
    if args.hf_mirror:
        os.environ.setdefault('HF_ENDPOINT', 'https://hf-mirror.com')
    try:
        result = transcribe(args.audio, args.engine, args.model, args.speakers, args.device, args.force,
                            load_hotwords(args.hotwords), args.allow_unseparated)
    except (LocalError, ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(2, str(error) + '\n')
    summary = {k: result[k] for k in ('engine', 'model', 'duration_seconds', 'reused', 'result_path')}
    summary['hotwords_used'] = len(result.get('hotwords_used', []))
    summary.update(speakers=result['speakers_detected'], speaker_separation=result['speaker_separation'],
                   speaker_coverage=result['speaker_coverage'], segments=len(result['segments']))
    if result['speaker_separation'] != 'voice':
        note(SEPARATION_NOTE[result['speaker_separation']].format(pct=round(result['speaker_coverage'] * 100)))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
