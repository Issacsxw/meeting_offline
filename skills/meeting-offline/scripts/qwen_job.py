"""Durable one-recording Qwen jobs. Keeps credentials out of job files."""
from __future__ import annotations
import hashlib
import json
import math
import os
import tempfile
from file_lock import locked_file
import device_context
import audio_duration
from datetime import datetime, timezone
from pathlib import Path

ROOT = device_context.paths()['jobs']


def now():
    return datetime.now(timezone.utc).isoformat()


def digest_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def save_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.qwen-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_vocabulary(path, error):
    if not path:
        return {}
    try:
        words = json.loads(Path(path).expanduser().read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise error('读不了热词 JSON 文件。') from exc
    if not isinstance(words, dict) or len(words) > 2000:
        raise error('热词必须是 JSON 对象，最多 2000 个。')
    if any(not isinstance(w, str) or not w.strip() or type(n) is not int or n not in (1, 2, 3, 4, 5, 50) for w, n in words.items()):
        raise error('热词不能为空，权重只能是 1～5 或 50。')
    if sum(n == 50 for n in words.values()) > 50:
        raise error('权重为 50 的超级热词最多 50 个。')
    return words


def check_result(result, config, error):
    if not isinstance(result, dict) or result.get('model') != config['model']:
        raise error('转写结果的模型和本次任务不一致。')
    duration = result.get('duration_seconds')
    if type(duration) not in (int, float) or not math.isfinite(duration) or abs(duration - config['duration_seconds']) > 1:
        raise error('转写结果的时长和这段录音不一致，可能不是同一段录音。')
    segments = result.get('segments')
    if not isinstance(segments, list) or not segments:
        raise error('没有得到带时间戳的转写结果；原始返回内容已保留在任务目录。')
    for segment in segments:
        start, end = segment.get('start_ms'), segment.get('end_ms')
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)
                or not 0 <= start <= end <= duration * 1000 + 2000
                or not isinstance(segment.get('text'), str)):
            raise error('转写结果里有时间戳异常的片段；原始返回内容已保留在任务目录。')


def require_mono(audio, error):
    # Filetrans defaults to channel 0, and its diarization supports mono only. Never silently lose tracks.
    try:
        channels = audio_duration.audio_channels(audio)
    except ValueError as exc:
        raise error(str(exc)) from exc
    if channels != [1]:
        raise error('文件转写只接受单音轨、单声道录音；检测到各音轨声道数为 '
                    + str(channels) + '。仅提交声道 0 会漏掉其他声道；请先确认并准备单声道副本。不会自动混音或上传。')


def warn_legacy_channels(manifest, config):
    if (config['route'] == 'temporary_storage' and manifest.get('audio_channels') != [1]
            and manifest.get('origin') != 'imported_existing_transcript'):
        import sys
        print('[Qwen] 旧任务未做声道预检，原请求仅选择声道 0；若录音还有其他声道，其内容可能未转写。复用或续查不会重新上传。',
              file=sys.stderr, flush=True)


def describe(args, api):
    audio = Path(args.audio).expanduser().resolve()
    if not audio.is_file():
        raise api.TranscriptionError('找不到录音：' + str(audio))
    duration = api.get_duration(audio, args.duration_seconds)
    route, model = api.choose_route(duration)
    words = load_vocabulary(args.hotwords, api.TranscriptionError)
    if args.filetrans:
        route, model = 'temporary_storage', api.LONG_MODEL
    if words and route != 'temporary_storage':
        raise api.TranscriptionError('5 分钟以内的录音使用热词需要加 --filetrans（改用文件转写）；不会自动切换。')
    if audio.stat().st_size > api.MAX_TEMP_UPLOAD_BYTES:
        raise api.TranscriptionError('录音超过 1 GB，不会自动切分；请先剪成较短的几段。')
    if duration > api.DIARIZATION_LIMIT_SECONDS and not args.no_diarization and not args.confirm_over_2h:
        raise api.TranscriptionError('超过 2 小时的录音区分说话人效果可能下降（阿里云建议 2 小时以内）；用户确认后加 --confirm-over-2h。')
    if not math.isfinite(args.poll_seconds) or args.poll_seconds < 0.5 or not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        raise api.TranscriptionError('查询间隔或超时设置无效；录音尚未上传。')
    config = {'model': model, 'route': route, 'duration_seconds': round(duration, 3),
              'region': args.region, 'base_url': api.region_base(args.region),
              'diarization': not args.no_diarization, 'keep_dialect': args.keep_dialect,
              'vocabulary': words}
    sha = digest_file(audio)
    job_id = hashlib.sha256(json.dumps({'audio_sha256': sha, 'config': config}, sort_keys=True).encode()).hexdigest()[:24]
    directory = Path(args.job_dir).expanduser().resolve() if args.job_dir else ROOT / job_id
    return audio, config, sha, job_id, directory


def execute(args, api):
    import sys
    audio, config, sha, job_id, directory = describe(args, api)
    manifest_path = directory / 'job.json'
    prior = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else None
    if prior:
        device_context.require_device(prior.get('device_id'), directory)
    if prior and (prior.get('audio_sha256') != sha or prior.get('config') != config):
        raise api.TranscriptionError('这个任务目录属于另一段录音或另一组设置；请换一个目录。')
    if args.dry_run:
        if (config['route'] == 'temporary_storage' and not args.import_result
                and not (prior and (prior.get('task_id') or
                         (prior.get('status') == 'completed' and (directory / 'result.json').exists())))):
            require_mono(audio, api.TranscriptionError)
        return {'dry_run': True, 'audio': audio.name, **config, 'audio_sha256': sha,
                'job_dir': str(directory), 'job_status': prior.get('status') if prior else 'new',
                'network_route': args.network_route, 'cloud_transfer': not bool(args.import_result or (prior and (prior.get('status') == 'completed' or prior.get('task_id') or prior.get('oss_url')))),
                'temporary_audio_retention_hours': 48 if config['route'] == 'temporary_storage' else 0}
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked_file(directory / '.lock', blocking=False,
                     busy_error=api.TranscriptionError('这段录音正在转写中，请等它完成。')):
        manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {
            'schema_version': 1, 'device_id': device_context.device_id(), 'job_id': job_id, 'audio_path': str(audio),
            'audio_sha256': sha, 'config': config, 'status': 'planned', 'created_at': now()}
        if manifest.get('audio_sha256') != sha or manifest.get('config') != config:
            raise api.TranscriptionError('任务在加锁期间被改动，请重新运行。')
        device_context.require_device(manifest.get('device_id'), directory)
        manifest.setdefault('device_id', device_context.device_id())
        def stage(value, **extra):
            manifest.update(status=value, updated_at=now(), **extra)
            save_json(manifest_path, manifest)
            print('[Qwen] ' + value, file=sys.stderr, flush=True)
        result_path = directory / 'result.json'
        if manifest['status'] == 'completed' and result_path.exists():
            result = json.loads(result_path.read_text(encoding='utf-8'))
            check_result(result, config, api.TranscriptionError)
            warn_legacy_channels(manifest, config)
            print('[Qwen] 复用本机已完成的转写结果，未联网、未读取密钥', file=sys.stderr)
            return result
        if args.import_result:
            if config['vocabulary']:
                raise api.TranscriptionError('导入的已有结果不能标记为“使用了热词”；去掉 --hotwords 再导入。')
            if manifest.get('task_id'):
                raise api.TranscriptionError('这段录音已有进行中的云端任务，不能用导入结果替换。')
            result = json.loads(Path(args.import_result).expanduser().read_text(encoding='utf-8'))
            check_result(result, config, api.TranscriptionError)
            save_json(result_path, result)
            stage('completed', origin='imported_existing_transcript', imported_at=now())
            return result
        raw_path, task_path = directory / 'transcript-raw.json', directory / 'task-response.json'
        if (config['route'] == 'temporary_storage' and manifest.get('task_id')
                and raw_path.exists() and task_path.exists()):
            # The result URL expires. A crash after downloading must not make our local copy unusable.
            try:
                raw = json.loads(raw_path.read_text(encoding='utf-8'))
                task = json.loads(task_path.read_text(encoding='utf-8'))
                recovered = api.normalize_long(raw, task, config['duration_seconds'])
                check_result(recovered, config, api.TranscriptionError)
            except (OSError, ValueError, TypeError, AttributeError, api.TranscriptionError):
                print('[Qwen] 已保存的原始结果无效，将只重新获取现有任务的结果，不重新上传或提交。', file=sys.stderr)
            else:
                save_json(result_path, recovered)
                stage('completed')
                print('[Qwen] 从本机原始结果恢复，未联网、未读取密钥', file=sys.stderr)
                return recovered
        if manifest['status'] in ('submitting', 'uncertain_submission'):
            raise api.TranscriptionError('上次提交的结果不确定（可能已在阿里云处理并计费），为避免重复扣费不会自动重试。请先在百炼控制台确认，再用新的 --job-dir 重新提交。')
        if manifest['status'] in ('uploading', 'upload_failed') and not args.retry_upload:
            raise api.TranscriptionError('上次上传没有完成；确认后加 --retry-upload 重试一次。')
        needs_transfer = not manifest.get('task_id') and not manifest.get('oss_url')
        if needs_transfer and not args.confirm_upload:
            raise api.TranscriptionError('这段录音尚未上传。先用 --dry-run 查看计划，用户同意后加 --confirm-upload。')
        if config['route'] == 'temporary_storage':
            if not manifest.get('task_id'):
                require_mono(audio, api.TranscriptionError)
                manifest['audio_channels'] = [1]  # metadata only: keep the existing config/hash and resumable job ID
            else:
                warn_legacy_channels(manifest, config)
        key = api.get_api_key()
        if config['route'] == 'direct_base64':
            stage('submitting')
            try:
                result = api.transcribe_short(audio, config['duration_seconds'], key, config['base_url'],
                    diarization=config['diarization'], keep_dialect=config['keep_dialect'])
                save_json(directory / 'response-normalized.json', result)
                check_result(result, config, api.TranscriptionError)
            except Exception as exc:
                stage('failed' if getattr(exc, 'not_processed', False) else 'uncertain_submission', last_error=str(exc)[:300])
                raise
            save_json(result_path, result)
            stage('completed')
            return result
        if not manifest.get('task_id'):
            if not manifest.get('oss_url'):
                policy = api.get_upload_policy(key, args.region, config['model'])
                stage('uploading')
                try:
                    oss_url = api.upload_temporary(audio, policy)
                except Exception:
                    stage('upload_failed')
                    raise
                stage('uploaded', oss_url=oss_url, uploaded_at=now())
            else:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(manifest['uploaded_at'])
                if age.total_seconds() >= 48 * 3600:
                    raise api.TranscriptionError('临时上传可能已过期（超过 48 小时）；请用新的 --job-dir 重新提交，不会自动重新上传。')
            parameters = {'channel_id': [0], 'language_hints': ['zh'],
                          'diarization_enabled': config['diarization'], 'keep_dialect': config['keep_dialect']}
            if config['vocabulary']:
                parameters['vocabulary'] = config['vocabulary']
            stage('submitting')
            try:
                submitted = api.request_json(config['base_url'] + '/services/audio/asr/transcription',
                    key=key, method='POST', payload={'model': config['model'],
                    'input': {'file_urls': [manifest['oss_url']]}, 'parameters': parameters},
                    extra_headers={'X-DashScope-Async': 'enable', 'X-DashScope-OssResourceResolve': 'enable'})
                save_json(directory / 'submission.json', submitted)
                task_id = submitted.get('output', {}).get('task_id')
                if not task_id:
                    raise api.TranscriptionError('提交后没有拿到任务编号；返回内容已保留，请先查看再决定是否重试。')
            except Exception as exc:
                stage('failed' if getattr(exc, 'not_processed', False) else 'uncertain_submission', last_error=str(exc)[:300])
                raise
            stage('submitted', task_id=task_id)
        if manifest['status'] == 'result_ready' and task_path.exists():
            task = json.loads(task_path.read_text(encoding='utf-8'))
        else:
            stage('processing')
            # A timeout retains task_id; the next run resumes polling, never uploading.
            task = api.poll_task(manifest['task_id'], key, config['base_url'], args.poll_seconds, args.timeout_seconds)
            save_json(task_path, task)
            stage('result_ready')
        results = task.get('output', {}).get('results', [])
        succeeded = [r for r in results if r.get('subtask_status') == 'SUCCEEDED' and r.get('transcription_url')]
        if len(succeeded) != 1:
            raise api.TranscriptionError('转写任务没有返回唯一的成功结果；返回内容已保留在任务目录。')
        transcript = api.request_json(succeeded[0]['transcription_url'])
        save_json(raw_path, transcript)
        result = api.normalize_long(transcript, task, config['duration_seconds'])
        check_result(result, config, api.TranscriptionError)
        save_json(result_path, result)
        stage('completed')
        return result
