"""Durable one-recording Qwen jobs. Keeps credentials out of job files."""
from __future__ import annotations
import hashlib
import json
import math
import os
import tempfile
from file_lock import locked_file
import device_context
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
        raise error('Cannot read hotword JSON.') from exc
    if not isinstance(words, dict) or len(words) > 2000:
        raise error('Hotwords must be an object with at most 2000 entries.')
    if any(not isinstance(w, str) or not w.strip() or type(n) is not int or n not in (1, 2, 3, 4, 5, 50) for w, n in words.items()):
        raise error('Hotword keys must be nonempty strings; weights must be 1–5 or 50.')
    if sum(n == 50 for n in words.values()) > 50:
        raise error('At most 50 super hotwords are allowed.')
    return words


def check_result(result, config, error):
    if not isinstance(result, dict) or result.get('model') != config['model']:
        raise error('Result model does not match this job.')
    duration = result.get('duration_seconds')
    if type(duration) not in (int, float) or not math.isfinite(duration) or abs(duration - config['duration_seconds']) > 1:
        raise error('Result duration does not match this recording.')
    segments = result.get('segments')
    if not isinstance(segments, list) or not segments:
        raise error('No usable timestamped transcript returned; raw response retained.')
    for segment in segments:
        start, end = segment.get('start_ms'), segment.get('end_ms')
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)
                or not 0 <= start <= end <= duration * 1000 + 2000
                or not isinstance(segment.get('text'), str)):
            raise error('Invalid timestamped segment returned; raw response retained.')


def describe(args, api):
    audio = Path(args.audio).expanduser().resolve()
    if not audio.is_file():
        raise api.TranscriptionError('Audio file not found: ' + str(audio))
    duration = api.get_duration(audio, args.duration_seconds)
    route, model = api.choose_route(duration)
    words = load_vocabulary(args.hotwords, api.TranscriptionError)
    if args.filetrans:
        route, model = 'temporary_storage', api.LONG_MODEL
    if words and route != 'temporary_storage':
        raise api.TranscriptionError('Inline hotwords require --filetrans for short recordings; no automatic route change.')
    if audio.stat().st_size > api.MAX_TEMP_UPLOAD_BYTES:
        raise api.TranscriptionError('Audio exceeds 1 GB; automatic splitting is disabled.')
    if duration > api.DIARIZATION_LIMIT_SECONDS and not args.no_diarization and not args.confirm_over_2h:
        raise api.TranscriptionError('Diarization above 2 hours requires --confirm-over-2h.')
    if not math.isfinite(args.poll_seconds) or args.poll_seconds < 0.5 or not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        raise api.TranscriptionError('Invalid polling interval or timeout; nothing uploaded.')
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
        raise api.TranscriptionError('This job directory belongs to different audio/settings; use a new directory.')
    if args.dry_run:
        return {'dry_run': True, 'audio': audio.name, **config, 'audio_sha256': sha,
                'job_dir': str(directory), 'job_status': prior.get('status') if prior else 'new',
                'network_route': args.network_route, 'cloud_transfer': not bool(args.import_result or (prior and (prior.get('status') == 'completed' or prior.get('task_id') or prior.get('oss_url')))),
                'temporary_audio_retention_hours': 48 if config['route'] == 'temporary_storage' else 0}
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked_file(directory / '.lock', blocking=False,
                     busy_error=api.TranscriptionError('This recording is already being processed.')):
        manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {
            'schema_version': 1, 'device_id': device_context.device_id(), 'job_id': job_id, 'audio_path': str(audio),
            'audio_sha256': sha, 'config': config, 'status': 'planned', 'created_at': now()}
        if manifest.get('audio_sha256') != sha or manifest.get('config') != config:
            raise api.TranscriptionError('Job changed while acquiring its lock.')
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
            print('[Qwen] reused local completed result; no key or network', file=sys.stderr)
            return result
        if args.import_result:
            if config['vocabulary']:
                raise api.TranscriptionError('Imported results cannot be relabeled as hotword-tested.')
            if manifest.get('task_id'):
                raise api.TranscriptionError('Cannot replace an active cloud task with imported text.')
            result = json.loads(Path(args.import_result).expanduser().read_text(encoding='utf-8'))
            check_result(result, config, api.TranscriptionError)
            save_json(result_path, result)
            stage('completed', origin='imported_existing_transcript', imported_at=now())
            return result
        if manifest['status'] in ('submitting', 'uncertain_submission'):
            raise api.TranscriptionError('Submission outcome is uncertain; do not resubmit blindly. Check the provider before creating a new job.')
        if manifest['status'] in ('uploading', 'upload_failed') and not args.retry_upload:
            raise api.TranscriptionError('Previous upload did not complete. Review it and use --retry-upload for one explicit retry.')
        needs_transfer = not manifest.get('task_id') and not manifest.get('oss_url')
        if needs_transfer and not args.confirm_upload:
            raise api.TranscriptionError('This recording is not uploaded. Review --dry-run and provide --confirm-upload.')
        key = api.get_api_key()
        if config['route'] == 'direct_base64':
            stage('submitting')
            try:
                result = api.transcribe_short(audio, config['duration_seconds'], key, config['base_url'],
                    diarization=config['diarization'], keep_dialect=config['keep_dialect'])
                save_json(directory / 'response-normalized.json', result)
                check_result(result, config, api.TranscriptionError)
            except Exception:
                stage('uncertain_submission')
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
                    raise api.TranscriptionError('Temporary upload may have expired; create a new reviewed job instead of silently reuploading.')
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
                    raise api.TranscriptionError('Submission returned no task ID; inspect the retained response before retrying.')
            except Exception:
                stage('uncertain_submission')
                raise
            stage('submitted', task_id=task_id)
        task_path = directory / 'task-response.json'
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
            raise api.TranscriptionError('Task did not return exactly one successful transcript; response retained.')
        transcript = api.request_json(succeeded[0]['transcription_url'])
        save_json(directory / 'transcript-raw.json', transcript)
        result = api.normalize_long(transcript, task, config['duration_seconds'])
        check_result(result, config, api.TranscriptionError)
        save_json(result_path, result)
        stage('completed')
        return result
