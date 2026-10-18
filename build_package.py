#!/usr/bin/env python3
"""Refresh MANIFEST.json checksums and build the install and upload zips."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent
EXTRA = ['install.py', '安装说明.txt', 'MANIFEST.json']


def skill_files(include_tests=True):
    return sorted(p for p in (ROOT / 'meeting-offline').rglob('*')
                  if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'
                  and (include_tests or 'tests' not in p.relative_to(ROOT / 'meeting-offline').parts))


def write_zip(path, files):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for file in files:
            bundle.write(file, file.relative_to(ROOT).as_posix())
    print('zip: ' + str(path.resolve()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', help='New version, e.g. 11.4.0')
    parser.add_argument('--out', type=Path, help='Directory for the zips; omit to only refresh MANIFEST.json')
    args = parser.parse_args()
    manifest_path = ROOT / 'MANIFEST.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if args.version:
        manifest['version'] = args.version
    manifest['files'] = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in skill_files()}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('MANIFEST.json: %d files, version %s' % (len(manifest['files']), manifest['version']))
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        version = manifest['version']
        # Local tools (Codex, Claude Code, WorkBuddy): run install.py from the unpacked folder.
        write_zip(args.out / ('meeting_offline-' + version + '-本地安装.zip'), [ROOT / n for n in EXTRA] + skill_files())
        # Cloud upload (ChatGPT, Claude.ai, WorkBuddy import): only the skill folder, SKILL.md one level down.
        write_zip(args.out / ('meeting_offline-' + version + '-upload.zip'), skill_files(include_tests=False))


if __name__ == '__main__':
    main()
