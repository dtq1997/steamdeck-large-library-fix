"""Update selection, integrity failures and installer/menu lifecycle checks."""
import fcntl
import contextlib
import hashlib
import io
import json
import socket
import subprocess
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import build_release
import engine
import manager
import updater


def release_fixture(number='0.4.99'):
    filename = 'SteamDeck-LargeLibraryFix-' + number + '.desktop'
    data = build_release.desktop_source().encode()
    checksum = (hashlib.sha256(data).hexdigest() + '  ' + filename + '\n').encode()
    blobs = {filename: data, 'SHA256SUMS': checksum}
    release = {'tag_name': 'v' + number, 'published_at': '2026-10-15', 'draft': False,
               'prerelease': True, 'assets': [
                   {'name': name, 'size': len(blob),
                    'digest': 'sha256:' + hashlib.sha256(blob).hexdigest(),
                    'browser_download_url': updater.DOWNLOAD + 'v' + number + '/' + name}
                   for name, blob in blobs.items()]}
    return release, blobs


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.update-test-', dir=Path(__file__).parent)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_selects_newest_preview_without_downgrade_or_drafts(self):
        old, _ = release_fixture('0.4.9')
        current, _ = release_fixture(engine.VERSION)
        newest, _ = release_fixture('0.4.99')
        draft = dict(newest, tag_name='v9.0.0', draft=True)
        nightly = dict(newest, tag_name='nightly')
        with patch.object(updater, 'fetch', return_value=json.dumps([old, draft, nightly, newest, current])):
            self.assertEqual(updater.check(engine.VERSION), newest)
        with patch.object(updater, 'fetch', return_value=json.dumps([old, current])):
            self.assertIsNone(updater.check(engine.VERSION))

    def test_missing_installer_is_error_not_silent_downgrade(self):
        release, _ = release_fixture()
        release['assets'].pop()
        with patch.object(updater, 'fetch', return_value=json.dumps([release])):
            with self.assertRaisesRegex(updater.UpdateError, 'missing'):
                updater.check(engine.VERSION)

    def test_verified_package_uses_python_without_shell_and_cleans_stage(self):
        release, blobs = release_fixture()
        def fetch(url, limit):
            return blobs[url.rsplit('/', 1)[-1]]
        with patch.object(updater, 'fetch', side_effect=fetch):
            command = updater.prepare(release, self.root)
        self.assertEqual(command[1], '-c')
        self.assertEqual(Path(command[-1]).read_bytes(), blobs[Path(command[-1]).name])
        compile(command[2], '<verified-bootstrap>', 'exec')

    def test_corrupted_payload_never_written(self):
        release, blobs = release_fixture()
        filename = next(name for name in blobs if name.endswith('.desktop'))
        blobs[filename] += b'corrupt'
        with patch.object(updater, 'fetch', side_effect=lambda url, limit: blobs[url.rsplit('/', 1)[-1]]):
            with self.assertRaisesRegex(updater.UpdateError, 'integrity'):
                updater.prepare(release, self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_checksum_manifest_disagreement_and_duplicate_rejected(self):
        for value in ('0' * 64 + '  {name}\n', '{digest}  {name}\n{digest}  {name}\n'):
            release, blobs = release_fixture()
            filename = next(name for name in blobs if name.endswith('.desktop'))
            blobs['SHA256SUMS'] = value.format(name=filename, digest=hashlib.sha256(blobs[filename]).hexdigest()).encode()
            asset = next(a for a in release['assets'] if a['name'] == 'SHA256SUMS')
            asset.update(size=len(blobs['SHA256SUMS']), digest='sha256:' + hashlib.sha256(blobs['SHA256SUMS']).hexdigest())
            with patch.object(updater, 'fetch', side_effect=lambda url, limit: blobs[url.rsplit('/', 1)[-1]]):
                with self.assertRaises(updater.UpdateError):
                    updater.prepare(release, self.root)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_foreign_asset_and_insecure_redirect_rejected(self):
        release, _ = release_fixture()
        release['assets'][0]['browser_download_url'] = 'https://example.com/installer.desktop'
        with self.assertRaisesRegex(updater.UpdateError, 'Unexpected'):
            updater.release_assets(release)
        handler = updater.SecureRedirect()
        for url in ('http://github.com/file', 'https://evilgithubusercontent.com/file'):
            with self.assertRaisesRegex(updater.UpdateError, 'redirect'):
                handler.redirect_request(None, None, 302, '', {}, url)

    def test_network_timeout_rate_limit_and_invalid_json_have_visible_errors(self):
        errors = [(socket.timeout(), 'timed out'),
                  (urllib.error.URLError('offline'), 'connect'),
                  (urllib.error.HTTPError(updater.API, 403, 'limited', {}, None), 'rate limit')]
        for error, message in errors:
            with patch.object(updater.urllib.request, 'build_opener') as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaisesRegex(updater.UpdateError, message):
                    updater.fetch(updater.API, 1024, json_response=True)
                self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'], 15)
        for value in ('<html>oops</html>', '{}'):
            with patch.object(updater, 'fetch', return_value=value):
                with self.assertRaisesRegex(updater.UpdateError, 'Invalid'):
                    updater.check(engine.VERSION)

    def test_json_content_type_size_bound_and_response_closed(self):
        for mime, data, limit in [('text/html', b'bad', 10), ('application/json', b'1234', 3)]:
            response = io.BytesIO(data)
            response.headers = Message()
            response.headers['Content-Type'] = mime
            with patch.object(updater.urllib.request, 'build_opener') as opener:
                opener.return_value.open.return_value = response
                with self.assertRaises(updater.UpdateError):
                    updater.fetch(updater.API, limit, json_response=True)
            self.assertTrue(response.closed)

    def test_cancel_update_does_not_download_or_install(self):
        with patch.object(manager, 'progress', return_value=contextlib.nullcontext()), \
             patch.object(manager.updater, 'check', return_value={'tag_name': 'v0.4.99'}), \
             patch.object(manager, 'dialog', return_value=subprocess.CompletedProcess([], 1)), \
             patch.object(manager.updater, 'prepare') as prepare:
            manager.update_now()
        prepare.assert_not_called()

    def test_menu_releases_install_lock_before_update(self):
        def update():
            with (self.root / 'installer.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with patch.object(manager, 'STATE', self.root), \
             patch.object(manager, 'check_environment'), \
             patch.object(manager, 'manage', return_value='update'), \
             patch.object(manager, 'update_now', side_effect=update) as run, \
             patch.object(manager.sys, 'argv', ['manager.py']):
            self.assertEqual(manager.main(), 0)
        run.assert_called_once()

    def test_failed_child_cleans_download_directory_and_uses_no_shell(self):
        def prepare(release, path):
            (path / 'installer.desktop').write_text('verified')
            return ['python3', '-c', 'pass']
        with patch.object(manager, 'HOME', self.root), \
             patch.object(manager, 'progress', return_value=contextlib.nullcontext()), \
             patch.object(manager.updater, 'check', return_value={'tag_name': 'v0.4.99'}), \
             patch.object(manager, 'dialog', return_value=subprocess.CompletedProcess([], 0)), \
             patch.object(manager.updater, 'prepare', side_effect=prepare), \
             patch.object(manager.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)) as run:
            with self.assertRaisesRegex(RuntimeError, 'Update did not finish'):
                manager.update_now()
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertEqual(list((self.root / '.cache/steam-large-library-fix').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
