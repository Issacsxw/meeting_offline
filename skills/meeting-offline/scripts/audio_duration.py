"""Read duration locally: macOS afinfo, WAV/M4A stdlib parsing, or optional ffprobe."""
import json
import math
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import wave


def mp4_duration(path):
    with Path(path).open('rb') as handle:
        def scan(start, end):
            pos = start
            while pos + 8 <= end:
                handle.seek(pos); size, kind = struct.unpack('>I4s', handle.read(8)); header = 8
                if size == 1:
                    size = struct.unpack('>Q', handle.read(8))[0]; header = 16
                if size == 0:size = end - pos
                if size < header or pos + size > end:raise ValueError('Invalid MP4 atom size.')
                if kind == b'moov':return scan(pos + header, pos + size)
                if kind == b'mvhd':
                    handle.seek(pos + header); payload = handle.read(min(size - header, 40))
                    offset, duration_bytes = (20, 8) if payload[0] == 1 else (12, 4)
                    timescale = int.from_bytes(payload[offset:offset + 4], 'big')
                    duration = int.from_bytes(payload[offset + 4:offset + 4 + duration_bytes], 'big')
                    if not timescale or not duration:raise ValueError('No MP4 duration.')
                    return duration / timescale
                pos += size
            raise ValueError('No movie header found.')
        return scan(0, Path(path).stat().st_size)


def duration(path):
    path = Path(path)
    if sys.platform == 'darwin':
        try:
            result = subprocess.run(['/usr/bin/afinfo', str(path)], capture_output=True, text=True, check=True, timeout=20)
            match = re.search(r'estimated duration:\s*([0-9.]+)\s*sec', result.stdout)
            if match:return float(match.group(1))
        except (OSError, subprocess.SubprocessError):pass
    if path.suffix.lower() in ('.m4a', '.mp4', '.mov'):
        try:return mp4_duration(path)
        except (OSError, ValueError, IndexError, struct.error):pass
    if path.suffix.lower() in ('.wav', '.wave'):
        try:
            with wave.open(str(path), 'rb') as source:return source.getnframes() / source.getframerate()
        except (OSError, EOFError, wave.Error):pass
    binary = shutil.which('ffprobe')
    if binary:
        result = subprocess.run([binary, '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(path)],
                                capture_output=True, text=True, check=True, timeout=30)
        value = float(json.loads(result.stdout)['format']['duration'])
        if math.isfinite(value) and value > 0:return value
    raise ValueError('无法本地读取时长；请安装 ffprobe 或提供可信的 --duration-seconds。未上传录音。')
