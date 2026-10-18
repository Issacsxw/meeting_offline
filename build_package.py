#!/usr/bin/env python3
"""Refresh MANIFEST.json checksums and build the portable install zip."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent
EXTRA = ['install.py', '安装说明.txt', 'MANIFEST.json']


def skill_files():
    return sorted(p for p in (ROOT / 'meeting-minutes').rglob('*')
                  if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', help='New version, e.g. 11.1.0')
    parser.add_argument('--zip', type=Path, help='Output zip; omit to only refresh MANIFEST.json')
    args = parser.parse_args()
    manifest_path = ROOT / 'MANIFEST.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if args.version:
        manifest['version'] = args.version
    manifest['files'] = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in skill_files()}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('MANIFEST.json: %d files, version %s' % (len(manifest['files']), manifest['version']))
    if args.zip:
        with zipfile.ZipFile(args.zip, 'w', zipfile.ZIP_DEFLATED) as bundle:
            for path in [ROOT / name for name in EXTRA] + skill_files():
                bundle.write(path, path.relative_to(ROOT).as_posix())
        print('zip: ' + str(args.zip.resolve()))


if __name__ == '__main__':
    main()
