"""Synthetic MP4 metadata only: no recordings or encoded speech."""
import struct


def atom(kind, payload, extended=False):
    if extended:
        return struct.pack('>I4sQ', 1, kind, len(payload) + 16) + payload
    return struct.pack('>I4s', len(payload) + 8, kind) + payload


def descriptor(tag, payload, padded=False):
    size = len(payload)
    encoded = [size & 127]
    size >>= 7
    while size:
        encoded.insert(0, 128 | (size & 127))
        size >>= 7
    if padded:
        encoded = [128] * (4 - len(encoded)) + encoded
    return bytes([tag] + encoded) + payload


def aac_config(channels=1, object_type=2, sync_extension=False):
    bits = f'{object_type:05b}0100{channels:04b}000'
    if sync_extension:
        bits += f'{0x2b7:011b}001010'
    bits += '0' * (-len(bits) % 8)
    return int(bits, 2).to_bytes(len(bits) // 8, 'big')


def audio_entry(channels=1, config=None, version=0, codec=b'mp4a', padded=False, wave=False):
    header = b'\0' * 6 + struct.pack('>HHHIHHHHI', 1, version, 0, 0, channels, 16, 0, 0, 44100 << 16)
    if version == 1:
        header += b'\0' * 16
    if config is not None:
        decoder = bytes([0x40, 0x15]) + b'\0' * 11 + descriptor(5, config, padded)
        es = b'\0\1\0' + descriptor(4, decoder, padded) + descriptor(6, b'\2', padded)
        extension = atom(b'esds', b'\0' * 4 + descriptor(3, es, padded))
        header += atom(b'wave', extension) if wave else extension
    return atom(codec, header)


def track(entries, handler=b'soun', extended=False):
    hdlr = atom(b'hdlr', b'\0' * 8 + handler + b'\0' * 12)
    stsd = atom(b'stsd', b'\0' * 4 + struct.pack('>I', len(entries)) + b''.join(entries))
    return atom(b'trak', atom(b'mdia', hdlr + atom(b'minf', atom(b'stbl', stsd))), extended)


def m4a(*tracks, extended=False):
    return atom(b'ftyp', b'M4A \0\0\0\0M4A isom') + atom(b'moov', b''.join(tracks), extended)
