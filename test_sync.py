"""Tests for sync.py on synthetic trees: python -m unittest -v test_sync"""
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
import uuid
from unittest import mock

import sync

T0 = 1790000000000  # epoch ms, 2026-09
MS = 1000000
ACC_A, ACC_B, ACC_C, ACC_D = (str(uuid.UUID(int=i)) for i in (0xA, 0xB, 0xC, 0xD))
ORG = str(uuid.UUID(int=0x0F))


def uid():
    return str(uuid.uuid4())


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, 'Claude', 'claude-code-sessions')
        self.projects = os.path.join(self.tmp, 'projects')
        self.config = os.path.join(self.tmp, 'Claude', 'config.json')
        self.tool = os.path.join(self.tmp, 'tool')
        os.makedirs(self.tool)
        self.saved_tool_dir = sync.TOOL_DIR
        sync.TOOL_DIR = self.tool

    def tearDown(self):
        sync.TOOL_DIR = self.saved_tool_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers
    def folder(self, acc, org=ORG):
        path = os.path.join(self.root, acc, org)
        os.makedirs(path, exist_ok=True)
        st = os.path.join(path, 'scheduled-tasks.json')
        if not os.path.exists(st):
            with open(st, 'wb') as fh:
                fh.write(b'{"scheduledTasks":[]}')
        return path

    def card(self, folder, lid, cli='same', at=T0, title='t', transcript=True, body=None):
        cli = lid if cli == 'same' else cli
        data = {'sessionId': 'local_' + lid, 'cliSessionId': cli, 'cwd': 'D:\\x', 'title': title,
                'isArchived': False, 'lastActivityAt': at, 'createdAt': T0 - 1000, 'body': body}
        if cli is None:
            del data['cliSessionId']
        path = os.path.join(folder, 'local_%s.json' % lid)
        with open(path, 'wb') as fh:
            fh.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
        os.utime(path, ns=(at * MS, at * MS))
        if transcript and cli:
            os.makedirs(os.path.join(self.projects, 'D--x'), exist_ok=True)
            open(os.path.join(self.projects, 'D--x', cli + '.jsonl'), 'wb').close()
        return path

    def tomb(self, folder, x, t):
        path = os.path.join(folder, 'deleted_' + x)
        with open(path, 'wb') as fh:
            fh.write(str(t).encode('ascii'))
        os.utime(path, ns=(t * MS, t * MS))
        return path

    def run_sync(self, *extra):
        if not {'--assume-running', '--assume-not-running'} & set(extra):
            extra = ('--assume-not-running',) + extra
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = sync.main(['--root', self.root, '--projects', self.projects,
                            '--config', self.config] + list(extra))
        self.assertEqual(rc, 0, self.read_log() + out.getvalue())
        return out.getvalue()

    def snapshot(self, top=None):
        snap = {}
        for dirpath, _, files in os.walk(top or self.root):
            for name in files:
                p = os.path.join(dirpath, name)
                with open(p, 'rb') as fh:
                    snap[os.path.relpath(p, top or self.root)] = (fh.read(), os.stat(p).st_mtime_ns)
        return snap

    def read_log(self):
        try:
            with open(os.path.join(self.tool, 'session-sync.log'), encoding='utf-8') as fh:
                return fh.read()
        except OSError:
            return ''

    def read(self, path):
        with open(path, 'rb') as fh:
            return fh.read()

    def assertSameFile(self, a, b):
        self.assertEqual(self.read(a), self.read(b))
        self.assertEqual(os.stat(a).st_mtime_ns, os.stat(b).st_mtime_ns)

    # -- tests
    def test_add_missing_card(self):
        a, b = self.folder(ACC_A), self.folder(ACC_B)
        x = uid()
        src = self.card(a, x, title='Ph\u00e2n t\u00edch m\u00e3 HPG')
        self.run_sync()
        self.assertSameFile(src, os.path.join(b, 'local_%s.json' % x))
        self.assertIn('ADD', self.read_log())
        self.assertIn('Ph\u00e2n t\u00edch m\u00e3 HPG', self.read_log())

    def test_update_older_copy(self):
        a, b = self.folder(ACC_A), self.folder(ACC_B)
        x = uid()
        old = self.card(a, x, at=T0, body='v1')
        new = self.card(b, x, at=T0 + 5000, body='v2')
        self.run_sync()
        self.assertSameFile(old, new)
        self.assertIn(b'v2', self.read(old))

    def test_newer_copy_never_overwritten(self):
        a, b = self.folder(ACC_A), self.folder(ACC_B)
        x = uid()
        self.card(a, x, at=T0, body='v1')
        new = self.card(b, x, at=T0 + 5000, body='v2')
        before = (self.read(new), os.stat(new).st_mtime_ns)
        self.run_sync()
        self.assertEqual(before, (self.read(new), os.stat(new).st_mtime_ns))

    def test_tombstone_removes_older_copies_and_propagates(self):
        a, b, c, d = (self.folder(acc) for acc in (ACC_A, ACC_B, ACC_C, ACC_D))
        x, cli, t = uid(), uid(), T0 + 100000
        self.tomb(a, x, t)
        self.tomb(a, cli, t)
        self.card(b, x, cli=cli, at=t - 1000)
        self.card(c, x, cli=cli, at=t + 30000)  # inside the 60 s slack: still removed
        self.run_sync()
        for f in (b, c, d):
            self.assertFalse(os.path.exists(os.path.join(f, 'local_%s.json' % x)))
            for y in (x, cli):
                self.assertEqual(self.read(os.path.join(f, 'deleted_' + y)), str(t).encode())
        self.assertFalse(os.path.exists(os.path.join(a, 'local_%s.json' % x)))
        self.assertEqual(self.read_log().count('REMOVE'), 2)

    def test_tombstone_older_than_card_keeps_card(self):
        a, b = self.folder(ACC_A), self.folder(ACC_B)
        x, t = uid(), T0
        self.tomb(a, x, t)
        kept = self.card(b, x, at=t + 120000)
        self.run_sync()
        self.assertTrue(os.path.exists(kept))
        self.assertFalse(os.path.exists(os.path.join(b, 'deleted_' + x)))
        self.assertNotIn('REMOVE', self.read_log())

    def test_recreated_after_delete_keeps_card(self):
        a, b = self.folder(ACC_A), self.folder(ACC_B)
        x, t = uid(), T0
        self.tomb(a, x, t)
        kept = self.card(a, x, at=t + 300000)  # same folder, re-created later
        self.run_sync()
        self.assertTrue(os.path.exists(kept))
        self.assertNotIn('REMOVE', self.read_log())
        self.assertFalse(os.path.exists(os.path.join(b, 'local_%s.json' % x)))

    def test_duplicate_cli_not_added_twice_newest_wins(self):
        a, b, d = self.folder(ACC_A), self.folder(ACC_B), self.folder(ACC_D)
        cli, old_id, new_id = uid(), uid(), uid()
        self.card(a, old_id, cli=cli, at=T0)
        self.card(b, new_id, cli=cli, at=T0 + 5000)
        self.run_sync()
        names = lambda f: sorted(n for n in os.listdir(f) if n.startswith('local_'))
        self.assertEqual(names(d), ['local_%s.json' % new_id])
        self.assertEqual(names(a), ['local_%s.json' % old_id])
        self.assertEqual(names(b), ['local_%s.json' % new_id])

    def test_card_without_cli_or_transcript_not_propagated(self):
        a, b = self.folder(ACC_A), self.folder(ACC_B)
        self.card(a, uid(), cli=None)
        self.card(a, uid(), cli='')
        self.card(a, uid(), cli=uid(), transcript=False)
        before = self.snapshot(b)
        self.run_sync()
        self.assertEqual(self.snapshot(b), before)

    def test_active_folder_untouched_while_running(self):
        a, b = self.folder(ACC_A), self.folder(ACC_B)
        self.folder(ACC_A, str(uuid.UUID(int=0x1F)))  # second org of the active account
        x, y, z = uid(), uid(), uid()
        self.card(a, x)
        self.card(b, y)
        self.card(a, z, at=T0)
        self.tomb(b, z, T0 + 100000)
        with open(self.config, 'w', encoding='utf-8') as fh:
            json.dump({'lastKnownAccountUuid': ACC_A, 'oauth:tokenCache': 'secret-token'}, fh)
        before = self.snapshot(os.path.join(self.root, ACC_A))
        self.run_sync('--assume-running')
        self.assertEqual(self.snapshot(os.path.join(self.root, ACC_A)), before)
        self.assertTrue(os.path.exists(os.path.join(b, 'local_%s.json' % x)))
        self.assertNotIn('secret-token', self.read_log())

    def test_nothing_written_when_running_and_active_unknown(self):
        a, b = self.folder(ACC_A), self.folder(ACC_B)
        self.card(a, uid())
        self.tomb(b, uid(), T0)
        before = self.snapshot()
        self.run_sync('--assume-running')  # no config.json
        self.assertEqual(self.snapshot(), before)
        self.assertIn('active account unknown', self.read_log())

    def test_unparseable_card_ignored(self):
        a, b, c = self.folder(ACC_A), self.folder(ACC_B), self.folder(ACC_C)
        x, y, z = uid(), uid(), uid()
        half = []
        for i in (x, y, z):
            p = os.path.join(a, 'local_%s.json' % i)
            with open(p, 'wb') as fh:
                fh.write(b'{"sessionId": "local_')
            os.utime(p, ns=(T0 * MS, T0 * MS))
            half.append(p)
        self.card(b, x, at=T0 + 5000)  # newer valid copy elsewhere
        self.tomb(b, y, T0 + 100000)  # deleted elsewhere after the half copy
        before = [(self.read(p), os.stat(p).st_mtime_ns) for p in half]
        self.run_sync()
        self.assertEqual([(self.read(p), os.stat(p).st_mtime_ns) for p in half], before)
        self.assertFalse(os.path.exists(os.path.join(c, 'local_%s.json' % z)))
        self.assertTrue(os.path.exists(os.path.join(c, 'local_%s.json' % x)))

    def test_other_files_never_touched(self):
        a, b = self.folder(ACC_A), self.folder(ACC_B)
        extras = []
        for f in (a, b):
            for name, body in (('notes.txt', b'keep'), ('deleted_x.bak', b'1'), ('local_abc.json', b'{}')):
                p = os.path.join(f, name)
                with open(p, 'wb') as fh:
                    fh.write(body)
                os.utime(p, ns=(T0 * MS, T0 * MS))
            extras += [os.path.join(f, n) for n in os.listdir(f)]
        self.card(a, uid())
        self.tomb(b, uid(), T0)
        before = {p: (self.read(p), os.stat(p).st_mtime_ns) for p in extras}
        self.run_sync()
        self.assertEqual({p: (self.read(p), os.stat(p).st_mtime_ns) for p in extras}, before)
        for f in (a, b):
            for n in os.listdir(f):
                self.assertTrue(os.path.join(f, n) in extras or n.startswith(('local_', 'deleted_')), n)

    def test_mtimes_preserved_and_second_run_noop(self):
        a, b, c = self.folder(ACC_A), self.folder(ACC_B), self.folder(ACC_C)
        x, y, z, cli = uid(), uid(), uid(), uid()
        src = self.card(a, x, at=T0 + 1234)
        self.card(a, y, at=T0, body='old')
        self.card(b, y, at=T0 + 9999, body='new')
        self.card(c, z, cli=cli, at=T0)
        self.tomb(b, z, T0 + 100000)
        self.tomb(b, cli, T0 + 100000)
        self.run_sync()
        for f in (b, c):
            self.assertSameFile(src, os.path.join(f, 'local_%s.json' % x))
        first = self.snapshot()
        self.run_sync()
        self.assertEqual(self.snapshot(), first)
        last = self.read_log().strip().splitlines()[-1]
        self.assertIn('add=0 update=0 remove=0 tombstone=0 error=0', last)

    def test_lock_prevents_overlap_and_stale_lock_is_taken(self):
        a, b = self.folder(ACC_A), self.folder(ACC_B)
        x = uid()
        self.card(a, x)
        lock = os.path.join(self.tool, 'session-sync.lock')
        open(lock, 'wb').close()
        self.run_sync()
        self.assertFalse(os.path.exists(os.path.join(b, 'local_%s.json' % x)))
        self.assertTrue(os.path.exists(lock))  # the other run's lock is left alone
        old = time.time() - 11 * 60
        os.utime(lock, (old, old))
        self.run_sync()
        self.assertTrue(os.path.exists(os.path.join(b, 'local_%s.json' % x)))
        self.assertFalse(os.path.exists(lock))
        self.assertIn('stale lock taken', self.read_log())

    def test_dry_run_writes_nothing(self):
        a, b = self.folder(ACC_A), self.folder(ACC_B)
        x = uid()
        self.card(a, x)
        self.card(b, uid(), at=T0)
        self.tomb(a, uid(), T0)
        before = self.snapshot(self.tmp)
        out = self.run_sync('--dry-run')
        self.assertEqual(self.snapshot(self.tmp), before)
        self.assertIn('ADD', out)
        self.assertIn('TOMBSTONE', out)

    def test_backup_once_before_first_write(self):
        a, b = self.folder(ACC_A), self.folder(ACC_B)
        self.card(a, uid())
        original = self.snapshot()
        self.run_sync()
        backups = [n for n in os.listdir(os.path.dirname(self.root)) if n.startswith('claude-code-sessions-backup-sync-')]
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.snapshot(os.path.join(os.path.dirname(self.root), backups[0])), original)
        self.card(b, uid())
        self.run_sync()
        backups2 = [n for n in os.listdir(os.path.dirname(self.root)) if n.startswith('claude-code-sessions-backup-sync-')]
        self.assertEqual(backups2, backups)

    def test_failed_backup_writes_nothing(self):
        a, b = self.folder(ACC_A), self.folder(ACC_B)
        self.card(a, uid())
        before = self.snapshot()

        def boom(src, dst):
            os.makedirs(dst)
            raise OSError('disk full')
        saved, sync.shutil.copytree = sync.shutil.copytree, boom
        try:
            rc = sync.main(['--root', self.root, '--projects', self.projects, '--assume-not-running'])
        finally:
            sync.shutil.copytree = saved
        self.assertEqual(rc, 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(os.listdir(os.path.dirname(self.root)), ['claude-code-sessions'])
        self.assertFalse(os.path.exists(os.path.join(self.tool, 'backup-done.txt')))
        self.assertIn('backup failed', self.read_log())

    def test_app_running_returns_bool(self):
        self.assertIn(sync.app_running(), (True, False))

    def test_is_app_image_desktop_app_vs_bundled_cli(self):
        for path in (r'C:\Program Files\WindowsApps\Claude_2.26454.2.0_x64__pzs8sxrjxfjjc\app\claude.exe',
                     r'C:\Program Files\WindowsApps\Claude_2.26454.0.0_x64__pzs8sxrjxfjjc\app\Claude.exe'):
            self.assertTrue(sync.is_app_image(path), path)
        for path in (r'C:\Users\user\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming'
                     r'\Claude\claude-code\2.1.293\83cb0bd7fed4\claude.exe',
                     r'C:\Users\user\AppData\Roaming\Claude\claude-code\2.1.293\claude.exe',
                     r'C:\Program Files\WindowsApps\Claude_2.26454.2.0_x64__pzs8sxrjxfjjc\app\other.exe'):
            self.assertFalse(sync.is_app_image(path), path)

    def test_default_paths_use_msix_package_folder(self):
        # Outside the app's process tree (terminals, the WMI-spawned worker) %APPDATA%\Claude does not exist.
        local, roaming = os.path.join(self.tmp, 'Local'), os.path.join(self.tmp, 'Roaming')
        with mock.patch.dict(os.environ, {'LOCALAPPDATA': local, 'APPDATA': roaming}):
            self.assertIsNone(sync.claude_dir())  # no package: refuse, never guess %APPDATA%
            self.assertEqual(sync.main(['--projects', self.projects, '--dry-run']), 1)
            pkg = os.path.join(local, 'Packages', 'Claude_pzs8sxrjxfjjc', 'LocalCache', 'Roaming', 'Claude')
            self.root = os.path.join(pkg, 'claude-code-sessions')
            a, b = self.folder(ACC_A), self.folder(ACC_B)
            x = uid()
            self.card(a, x)
            with open(os.path.join(pkg, 'config.json'), 'w', encoding='utf-8') as fh:
                json.dump({'lastKnownAccountUuid': ACC_A}, fh)
            self.assertEqual(sync.claude_dir(), pkg)
            with open(os.path.join(self.tool, 'backup-done.txt'), 'w') as fh:
                fh.write('x')  # the backup copy would exceed MAX_PATH under the temp dir
            rc = sync.main(['--projects', self.projects, '--assume-running'])  # default --root/--config
        self.assertEqual(rc, 0, self.read_log())
        self.assertTrue(os.path.exists(os.path.join(b, 'local_%s.json' % x)))
        self.assertIn('active=%s' % ACC_A[:8], self.read_log())
        self.assertFalse(os.path.exists(roaming))

    def test_non_uuid_dirs_never_written(self):
        a, b = self.folder(ACC_A), self.folder(ACC_B)
        extra = [os.path.join(self.root, ACC_A, 'not-an-org-cache'), os.path.join(self.root, 'tmp-dir', 'sub')]
        for p in extra:
            os.makedirs(p)
        x = uid()
        self.card(a, x)
        self.tomb(a, uid(), T0)
        self.run_sync()
        self.assertTrue(os.path.exists(os.path.join(b, 'local_%s.json' % x)))
        for p in extra:
            self.assertEqual(os.listdir(p), [])

    def test_app_started_during_run_writes_nothing(self):
        a, b = self.folder(ACC_A), self.folder(ACC_B)
        x = uid()
        self.card(a, x)
        self.tomb(a, uid(), T0)  # two writes planned for b
        before = self.snapshot()
        argv =['--root', self.root, '--projects', self.projects, '--config', self.config]
        with mock.patch.object(sync, 'app_running', side_effect=[False, True]):
            self.assertEqual(sync.main(argv), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(os.listdir(os.path.dirname(self.root)), ['claude-code-sessions'])  # no backup
        self.assertIn('started or switched account during the run', self.read_log())
        with mock.patch.object(sync, 'app_running', side_effect=[False, False]):  # re-checked once only
            self.assertEqual(sync.main(argv), 0, self.read_log())
        self.assertTrue(os.path.exists(os.path.join(b, 'local_%s.json' % x)))


if __name__ == '__main__':
    unittest.main()
