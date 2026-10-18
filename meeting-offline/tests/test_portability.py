import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import device_context as d

INSTALLER = Path(__file__).resolve().parents[2] / 'install.py'


class DataHomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.home = Path(self.tmp.name)
        clean = {k: v for k, v in os.environ.items() if not k.startswith(('MEETING_OFFLINE_', 'MEETING_MINUTES_'))}
        self.env = patch.dict(os.environ, {**clean, 'HOME': str(self.home), 'USERPROFILE': str(self.home),
                                           'MEETING_OFFLINE_DEVICE_ID': 'computer-a'}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def test_new_install_uses_tool_neutral_home(self):
        self.assertEqual(d.paths()['base'], (self.home / '.meeting-offline').resolve())

    def test_existing_data_from_older_versions_keeps_being_used(self):
        (self.home / '.codex' / 'meeting-minutes-memory').mkdir(parents=True)
        self.assertEqual(d.paths()['base'], (self.home / '.codex' / 'meeting-minutes-memory').resolve())
        (self.home / '.meeting-minutes').mkdir()
        self.assertEqual(d.paths()['base'], (self.home / '.meeting-minutes').resolve())
        (self.home / '.meeting-offline').mkdir()
        self.assertEqual(d.paths()['base'], (self.home / '.meeting-offline').resolve())

    def test_device_id_and_old_variable_names_survive_the_rename(self):
        expected = d.hashlib.sha256(b'meeting-minutes:v1:manual:computer-a').hexdigest()[:24]
        self.assertEqual(d.device_id(), expected)
        with patch.dict(os.environ, {'MEETING_MINUTES_DEVICE_ID': 'computer-a', 'MEETING_MINUTES_DATA_HOME': str(self.home / 'old')}):
            del os.environ['MEETING_OFFLINE_DEVICE_ID']
            self.assertEqual(d.device_id(), expected)
            self.assertEqual(d.paths()['base'], (self.home / 'old').resolve())
            os.environ['MEETING_OFFLINE_DATA_HOME'] = str(self.home / 'new')
            self.assertEqual(d.paths()['base'], (self.home / 'new').resolve())

    def test_sandbox_identity_is_shared_and_never_a_real_device(self):
        del os.environ['MEETING_OFFLINE_DEVICE_ID']
        with patch.dict(os.environ, {'MEETING_OFFLINE_SANDBOX': '1', 'MEETING_OFFLINE_DATA_HOME': str(self.home / 'out')}):
            paths = d.initialize()
            self.assertTrue(paths['sandbox'])
            self.assertEqual(paths['archive'], (self.home / 'out' / '会议纪要').resolve())
            sandbox_id = paths['device_id']
            memory = json.loads(paths['memory'].read_text(encoding='utf-8'))
            self.assertEqual(memory['device_id'], sandbox_id)
        self.assertNotEqual(sandbox_id, d.hashlib.sha256(b'meeting-minutes:v1:manual:computer-a').hexdigest()[:24])
        with patch.dict(os.environ, {'MEETING_OFFLINE_SANDBOX': 'yes'}):
            self.assertEqual(d.device_id(), sandbox_id)
        with patch.dict(os.environ, {'MEETING_OFFLINE_DEVICE_ID': 'computer-a'}):
            with self.assertRaises(ValueError):
                d.require_device(sandbox_id, paths['memory'])


@unittest.skipUnless(INSTALLER.is_file(), 'installer only ships with the install bundle')
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.home = Path(self.tmp.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('MEETING_OFFLINE_', 'MEETING_MINUTES_', 'CODEX_HOME'))}
        self.env.update(HOME=str(self.home), USERPROFILE=str(self.home), MEETING_OFFLINE_DEVICE_ID='computer-a')

    def tearDown(self):
        self.tmp.cleanup()

    def run_installer(self, *args):
        return subprocess.run([sys.executable, str(INSTALLER), *args], env=self.env,
                              capture_output=True, text=True, encoding='utf-8')

    def test_auto_installs_into_detected_tools_only(self):
        self.assertNotEqual(self.run_installer().returncode, 0)
        (self.home / '.claude').mkdir(); (self.home / '.workbuddy').mkdir()
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in ('.claude/skills', '.workbuddy/skills'):
            self.assertTrue((self.home / path / 'meeting-offline' / 'SKILL.md').is_file())
        self.assertFalse((self.home / '.agents').exists())
        self.assertTrue((self.home / '.meeting-offline').is_dir())

    def test_codex_moves_legacy_copy_aside(self):
        legacy = self.home / '.codex' / 'skills' / 'meeting-minutes'
        legacy.mkdir(parents=True); (legacy / 'SKILL.md').write_text('old', encoding='utf-8')
        result = self.run_installer('--target', 'codex')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / '.agents' / 'skills' / 'meeting-offline' / 'SKILL.md').is_file())
        self.assertFalse(legacy.exists())
        self.assertEqual(len(list((self.home / '.codex' / 'skill-backups').iterdir())), 1)

    def test_old_skill_name_is_moved_aside(self):
        old = self.home / '.claude' / 'skills' / 'meeting-minutes'
        old.mkdir(parents=True); (old / 'SKILL.md').write_text('old', encoding='utf-8')
        result = self.run_installer('--target', 'claude')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in (self.home / '.claude' / 'skills').iterdir()), ['meeting-offline'])
        backups = list((self.home / '.claude' / 'skill-backups').iterdir())
        self.assertEqual(len(backups), 1); self.assertTrue(backups[0].name.startswith('meeting-minutes-'))

    def test_custom_skills_dir(self):
        result = self.run_installer('--skills-dir', str(self.home / 'other'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / 'other' / 'meeting-offline' / 'SKILL.md').is_file())


if __name__ == '__main__':
    unittest.main()
