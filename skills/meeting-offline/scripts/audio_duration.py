"""Read duration and channel layouts locally, with optional ffprobe fallback."""
import json
import math
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import wave


def audio_channels(path):
    """Channel counts for every audio stream; never assume an unreadable file is mono."""
    path = Path(path)
    if path.suffix.lower() in ('.wav', '.wave'):
        try:
            with wave.open(str(path), 'rb') as source:
                return [source.getnchannels()]
        except (OSError, EOFError, wave.Error):
            pass
    if path.suffix.lower() in ('.m4a', '.mp4', '.mov'):
        try:
            return mp4_channels(path)
        except (OSError, ValueError, struct.error):
            pass
    binary = shutil.which('ffprobe')
    if not binary:
        raise ValueError('无法本地确认录音声道；ffprobe 是可选工具，可安装后检查，或自行核对音轨。本次探测未发起上传或转写请求。')
    try:
        result = subprocess.run([binary, '-v', 'error', '-select_streams', 'a', '-show_entries',
                                 'stream=index,channels', '-of', 'json', str(path)],
                                capture_output=True, text=True, check=True, timeout=30)
        data = json.loads(result.stdout)
        streams = data.get('streams') if isinstance(data, dict) else None
        if (not isinstance(streams, list) or not streams
                or any(not isinstance(stream, dict) or type(stream.get('channels')) is not int
                       or stream['channels'] <= 0 for stream in streams)):
            raise ValueError('Missing audio channel layout.')
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise ValueError('无法本地确认录音声道；可检查文件或准备经核对的单声道副本。本次探测未发起上传或转写请求。') from error
    return [stream['channels'] for stream in streams]


def _mp4_atoms(handle, start, end):
    """Yield bounded atom payloads without loading media data into memory."""
    while start < end:
        if end - start < 8:
            raise ValueError('Truncated MP4 atom.')
        handle.seek(start)
        size, kind = struct.unpack('>I4s', handle.read(8))
        header = 8
        if size == 1:
            if end - start < 16:
                raise ValueError('Truncated extended MP4 atom.')
            size = struct.unpack('>Q', handle.read(8))[0]
            header = 16
        elif size == 0:
            size = end - start
        if size < header or size > end - start:
            raise ValueError('Invalid MP4 atom size.')
        yield kind, start + header, start + size
        start += size


def _mp4_child(handle, start, end, kind):
    children = [(a, b) for tag, a, b in _mp4_atoms(handle, start, end) if tag == kind]
    if len(children) != 1:
        raise ValueError('Missing or ambiguous MP4 metadata.')
    return children[0]


def _mp4_payload(handle, start, end, limit=65536):
    if end - start > limit:
        raise ValueError('MP4 audio metadata too large.')
    handle.seek(start)
    data = handle.read(end - start)
    if len(data) != end - start:
        raise ValueError('Truncated MP4 audio metadata.')
    return data


def _mp4_descriptors(data):
    """Read MPEG-4 descriptor lengths, including four-byte padded encodings."""
    pos = 0
    while pos < len(data):
        tag = data[pos]
        pos += 1
        size = 0
        for _ in range(4):
            if pos >= len(data):
                raise ValueError('Truncated MPEG-4 descriptor length.')
            value = data[pos]
            pos += 1
            size = (size << 7) | (value & 127)
            if not value & 128:
                break
        else:
            raise ValueError('Invalid MPEG-4 descriptor length.')
        if size > len(data) - pos:
            raise ValueError('Truncated MPEG-4 descriptor.')
        yield tag, data[pos:pos + size]
        pos += size


def _mp4_descriptor(data, kind):
    matches = [value for tag, value in _mp4_descriptors(data) if tag == kind]
    if len(matches) != 1:
        raise ValueError('Missing or ambiguous MPEG-4 descriptor.')
    return matches[0]


def _aac_channels(config):
    # Read the common AAC-LC AudioSpecificConfig. PCE (configuration 0), HE-AAC,
    # parametric stereo and unfamiliar extensions deliberately fall back to ffprobe.
    bits = ''.join(format(value, '08b') for value in config)
    pos = 0

    def read(count):
        nonlocal pos
        if pos + count > len(bits):
            raise ValueError('Truncated AAC configuration.')
        value = int(bits[pos:pos + count], 2)
        pos += count
        return value

    if read(5) != 2:
        raise ValueError('Unsupported AAC object type.')
    frequency = read(4)
    if frequency == 15:
        if not read(24):
            raise ValueError('Invalid AAC sample rate.')
    elif frequency > 12:
        raise ValueError('Invalid AAC sample rate.')
    layout = read(4)
    channels = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 8}.get(layout)
    if not channels:
        raise ValueError('Unsupported AAC channel configuration.')
    # GASpecificConfig: frameLengthFlag, dependsOnCoreCoder, extensionFlag.
    read(1)
    if read(1):
        read(14)
    if read(1):
        raise ValueError('Unsupported AAC extension.')
    if '1' in bits[pos:]:
        # Common encoders append an explicit "SBR absent" sync extension.
        # Anything that could expand the decoded channel layout stays unknown.
        if read(11) != 0x2b7 or read(5) != 5 or read(1) != 0 or '1' in bits[pos:]:
            raise ValueError('Unsupported AAC sync extension.')
    return channels


def _esds_channels(data):
    if len(data) < 4 or data[:4] != b'\0' * 4:
        raise ValueError('Unsupported ES descriptor version.')
    es = _mp4_descriptor(data[4:], 3)
    if len(es) < 3:
        raise ValueError('Truncated ES descriptor.')
    flags = es[2]
    pos = 3 + (2 if flags & 128 else 0)
    if flags & 64:
        if pos >= len(es):
            raise ValueError('Truncated ES URL.')
        pos += 1 + es[pos]
    if flags & 32:
        pos += 2
    decoder = _mp4_descriptor(es[pos:], 4)
    if len(decoder) < 13 or decoder[0] not in (0x40, 0x67) or decoder[1] >> 2 != 5:
        raise ValueError('Unsupported MPEG-4 audio decoder.')
    return _aac_channels(_mp4_descriptor(decoder[13:], 5))


def _mp4_audio_entry(handle, start, end):
    if end - start < 28:
        raise ValueError('Truncated MP4 audio sample entry.')
    header = _mp4_payload(handle, start, start + 28)
    version = int.from_bytes(header[8:10], 'big')
    if version not in (0, 1):
        raise ValueError('Unsupported MP4 audio sample entry version.')
    channels = int.from_bytes(header[16:18], 'big')
    child_start = start + (44 if version == 1 else 28)
    if child_start > end:
        raise ValueError('Truncated MP4 audio sample extension.')
    atoms = list(_mp4_atoms(handle, child_start, end))
    descriptors = []
    for tag, a, b in atoms:
        if tag == b'esds':
            descriptors.append((a, b))
        elif tag == b'wave':
            descriptors.extend((a, b) for tag, a, b in _mp4_atoms(handle, a, b) if tag == b'esds')
    if descriptors:
        if len(descriptors) != 1:
            raise ValueError('Ambiguous MP4 audio configuration.')
        # AAC's actual layout overrides the mp4a field, often hardcoded to 2.
        return _esds_channels(_mp4_payload(handle, *descriptors[0]))
    if not channels:
        raise ValueError('Missing MP4 audio channels.')
    return channels


def mp4_channels(path):
    """Inspect every sound track; never return a partial set of known tracks."""
    with Path(path).open('rb') as handle:
        moov = _mp4_child(handle, 0, Path(path).stat().st_size, b'moov')
        tracks = [(a, b) for tag, a, b in _mp4_atoms(handle, *moov) if tag == b'trak']
        channels = []
        for track in tracks:
            media = _mp4_child(handle, *track, b'mdia')
            handler = _mp4_payload(handle, *_mp4_child(handle, *media, b'hdlr'))
            if len(handler) < 12 or handler[0] != 0:
                raise ValueError('Invalid MP4 track handler.')
            if handler[8:12] != b'soun':
                if handler[8:12] not in (b'vide', b'hint', b'text', b'sbtl', b'subt', b'clcp',
                                         b'meta', b'mdir', b'mdta', b'tmcd'):
                    raise ValueError('Unknown MP4 track handler.')
                continue
            media_info = _mp4_child(handle, *media, b'minf')
            samples = _mp4_child(handle, *media_info, b'stbl')
            start, end = _mp4_child(handle, *samples, b'stsd')
            if end - start < 8:
                raise ValueError('Truncated MP4 sample description.')
            header = _mp4_payload(handle, start, start + 8)
            entries = list(_mp4_atoms(handle, start + 8, end))
            if header[:4] != b'\0' * 4 or len(entries) != int.from_bytes(header[4:8], 'big') or not entries:
                raise ValueError('Invalid MP4 sample descriptions.')
            counts = []
            for tag, a, b in entries:
                if tag != b'mp4a':
                    raise ValueError('Unsupported MP4 audio codec.')
                counts.append(_mp4_audio_entry(handle, a, b))
            if len(set(counts)) != 1:
                raise ValueError('Changing MP4 channel layout.')
            channels.append(counts[0])
        if not channels:
            raise ValueError('No MP4 audio tracks.')
        return channels


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
        except (OSError, wave.Error):pass
    binary = shutil.which('ffprobe')
    if binary:
        result = subprocess.run([binary, '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(path)],
                                capture_output=True, text=True, check=True, timeout=30)
        value = float(json.loads(result.stdout)['format']['duration'])
        if math.isfinite(value) and value > 0:return value
    raise ValueError('无法本地读取时长；请安装 ffprobe 或提供可信的 --duration-seconds。未上传录音。')
