"""Portable installer checks; no Steam data, network, or actual user installation."""
import base64
import hashlib
import json
import shlex
import subprocess
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest.mock import patch

import bootstrap
import build_release
import install
import manager


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.packaging-test-', dir=Path(__file__).parent)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_upgrade_pauses_old_watcher_and_failure_restores_checks(self):
        previous = {'control_hashes': {'managed': 'hash'}}
        with patch.object(install, 'systemctl') as control:
            with self.assertRaisesRegex(ValueError, 'failed patch'):
                with install.pause_maintenance(previous):
                    self.assertEqual(control.call_args.args[0], 'stop')
                    raise ValueError('failed patch')
            self.assertEqual(control.call_args.args, ('start', 'steam-large-library-fix.path'))
        with patch.object(install, 'systemctl') as control:
            with install.pause_maintenance({}):
                control.assert_not_called()

    def test_desktop_exec_is_one_valid_self_contained_argument(self):
        content = build_release.desktop_source()
        line = next(x for x in content.splitlines() if x.startswith('Exec='))
        argv = shlex.split(line[5:])
        self.assertEqual(argv[:2], ['/usr/bin/python3', '-c'])
        self.assertEqual(len(argv), 4)
        self.assertEqual(argv[3], '%k')
        self.assertLess(len(argv[2]), 100000)
        self.assertNotIn('%', argv[2])
        namespace = {'__name__': 'test_embedded'}
        exec(compile(argv[2], '<desktop>', 'exec'), namespace)
        launcher = self.root / '安装器 with spaces.desktop'
        launcher.write_text(content)
        extracted = self.root / 'extracted'
        extracted.mkdir()
        namespace['unpack'](namespace['read_payload'](str(launcher)), extracted)
        self.assertEqual({p.name for p in extracted.iterdir()}, set(install.PAYLOAD))
        for name in install.PAYLOAD:
            self.assertEqual((extracted / name).read_bytes(), (build_release.HERE / name).read_bytes())
        self.assertTrue((extracted / 'qjs-linux-x86_64').stat().st_mode & 0o100)
        self.assertEqual(namespace['read_payload'](launcher.as_uri()), build_release.encoded_payload())

    def test_corrupted_file_rejected_before_any_write(self):
        bundle = json.loads(zlib.decompress(base64.b64decode(build_release.encoded_payload())))
        bundle['engine.py']['sha256'] = '0' * 64
        encoded = base64.b64encode(zlib.compress(json.dumps(bundle).encode()))
        with self.assertRaisesRegex(ValueError, 'integrity'):
            bootstrap.unpack(encoded, self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unexpected_and_traversal_paths_rejected(self):
        bundle = json.loads(zlib.decompress(base64.b64decode(build_release.encoded_payload())))
        bundle['../outside'] = bundle.pop('LICENSE')
        encoded = base64.b64encode(zlib.compress(json.dumps(bundle).encode()))
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            bootstrap.unpack(encoded, self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_build_is_reproducible_and_source_is_allowlisted(self):
        for name in ['a', 'b']:
            build_release.build(self.root / name)
        first, second = self.root / 'a', self.root / 'b'
        self.assertEqual({p.name: p.read_bytes() for p in first.iterdir()},
                         {p.name: p.read_bytes() for p in second.iterdir()})
        archive = next(first.glob('*.zip'))
        with zipfile.ZipFile(archive) as source:
            prefix = 'SteamDeck-LargeLibraryFix-' + install.engine.VERSION + '/'
            self.assertEqual({n.removeprefix(prefix) for n in source.namelist()},
                             set(build_release.ARCHIVE_FILES))
            for name in build_release.ARCHIVE_FILES:
                self.assertEqual(source.read(prefix + name), (build_release.ROOT / name).read_bytes())
            self.assertFalse(any('notes/' in n or n.endswith('/sp.js') or n.endswith('/chunk.original.js') for n in source.namelist()))
        for name in build_release.DOCUMENTS:
            self.assertEqual((first / name).read_bytes(), (build_release.ROOT / name).read_bytes())

    def test_bootstrap_cleans_temp_files_after_child_failure(self):
        with patch.object(bootstrap, 'PAYLOAD', build_release.encoded_payload()), \
             patch.object(bootstrap.os, 'geteuid', return_value=1000), \
             patch.object(bootstrap.Path, 'home', return_value=self.root), \
             patch.object(bootstrap.subprocess, 'run', return_value=subprocess.CompletedProcess([], 3)):
            self.assertEqual(bootstrap.main(), 3)
        self.assertEqual(list((self.root / '.cache/steam-large-library-fix').iterdir()), [])

    def test_existing_user_edited_control_file_is_untouched(self):
        target = self.root / 'entry.desktop'
        target.write_text('user edited')
        with self.assertRaisesRegex(ValueError, 'user-edited'):
            install.preflight({target: 'new'}, {'control_hashes': {}})
        self.assertEqual(target.read_text(), 'user edited')

    def test_managed_previous_control_file_can_be_upgraded(self):
        target = self.root / 'entry.desktop'
        target.write_text('previous managed content')
        previous = {'control_hashes': {str(target): hashlib.sha256(target.read_bytes()).hexdigest()}}
        install.preflight({target: 'new'}, previous)

    def test_stage_includes_manager_and_refuses_changed_same_version(self):
        target = self.root / 'version'
        install.stage_version(target)
        self.assertEqual((target / 'manager.py').read_bytes(), (build_release.HERE / 'manager.py').read_bytes())
        install.stage_version(target)
        (target / 'README.md').write_text('local change')
        with self.assertRaisesRegex(ValueError, 'no overwrite'):
            install.stage_version(target)

    def test_manager_reports_failure_and_keeps_log(self):
        failure = subprocess.CompletedProcess([], 3, 'unsupported\n', 'unknown client\n')
        with patch.object(manager, 'STATE', self.root), \
             patch.object(manager.subprocess, 'run', return_value=failure):
            with self.assertRaisesRegex(RuntimeError, 'unsupported'):
                manager.run_installer()
        self.assertIn('unknown client', (self.root / 'installer.log').read_text())

    def test_cancel_uninstall_leaves_installation_alone(self):
        with patch.object(manager, 'dialog', return_value=subprocess.CompletedProcess([], 1)), \
             patch.object(manager, 'run_installer') as run:
            manager.uninstall_now()
        run.assert_not_called()

    def test_progress_closed_even_when_install_fails(self):
        reference = subprocess.CompletedProcess([], 0, 'org.kde.kdialog-123 /ProgressDialog\n', '')
        with patch.object(manager, 'dialog', return_value=reference), \
             patch.object(manager.shutil, 'which', return_value='/usr/bin/qdbus6'), \
             patch.object(manager.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'failed'):
                with manager.progress('test'):
                    raise ValueError('failed')
        self.assertEqual(run.call_args.args[0][-1], 'close')


if __name__ == '__main__':
    unittest.main()
