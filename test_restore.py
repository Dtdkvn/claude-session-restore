"""Tests for restore.py, mocks only (never kills or launches anything):
python -m unittest -v test_restore"""
import base64
import contextlib
import ctypes
import fnmatch
import getpass
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import uuid
from ctypes import wintypes
from unittest import mock

import restore
import sync

HERE = os.path.dirname(os.path.abspath(__file__))
APP = r'C:\Program Files\WindowsApps\Claude_2.37337.1.0_x64__pzs8sxrjxfjjc\app\claude.exe'
CLI = (r'C:\Users\user\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming'
       r'\Claude\claude-code\2.1.295\d2e29494ec83\claude.exe')
STANDALONE = r'C:\Users\user\.local\bin\CLAUDE.EXE'
OTHERS = [r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
          r'C:\Users\user\AppData\Local\Programs\Python\Python310\pythonw.exe',
          r'C:\tools\claude-helper.exe', r'C:\tools\notclaude.exe', r'C:\tools\claude.exe.bak']


def argv_of(cmdline):
    """How Windows splits a command line (CommandLineToArgvW)."""
    shell32 = ctypes.WinDLL('shell32')
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    n = ctypes.c_int()
    arr = shell32.CommandLineToArgvW(cmdline, ctypes.byref(n))
    try:
        return [arr[i] for i in range(n.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(arr)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += s


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = sync.TOOL_DIR, sync._dry
        sync.TOOL_DIR, sync._dry = self.tmp, False

    def tearDown(self):
        sync.TOOL_DIR, sync._dry = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read_log(self):
        try:
            with open(os.path.join(self.tmp, 'session-sync.log'), encoding='utf-8') as fh:
                return fh.read()
        except OSError:
            return ''


class PureTest(Base):
    def test_kill_targets(self):
        procs = [(10, APP), (11, APP.upper()), (12, CLI), (13, STANDALONE), (99, CLI)]
        procs += [(20 + i, p) for i, p in enumerate(OTHERS)]
        self.assertEqual(restore.kill_targets(procs, own_pid=99),
                         [(10, APP), (11, APP.upper()), (12, CLI), (13, STANDALONE)])
        self.assertEqual(restore.kill_targets([(5, p) for p in OTHERS], own_pid=1), [])

    def test_encoded_command_round_trip_and_quoting(self):
        python = 'C:\\Program Files\\Py th\u1ec3n\\pythonw.exe'
        args = [python, os.path.abspath(restore.__file__), '--worker', '--delay', '15', '--aumid', 'Claude_x!Claude']
        cmd = restore.worker_cmdline(python, 15, 'Claude_x!Claude')
        self.assertEqual(argv_of(cmd), args)
        odd = ['a b', 'quote"in', 'trail\\', 'space trail\\', '\u0110\u1ed3ng b\u1ed9', '']
        self.assertEqual(argv_of(subprocess.list2cmdline(['x.exe'] + odd))[1:], odd)
        script = restore.wmi_script(cmd)
        self.assertEqual(base64.b64decode(restore.encode_ps(script)).decode('utf-16-le'), script)
        self.assertIn('Invoke-CimMethod -ClassName Win32_Process -MethodName Create', script)
        b64 = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script).group(1)
        self.assertEqual(base64.b64decode(b64).decode('utf-16-le'), cmd)
        self.assertTrue(script.isascii())

    @unittest.skipUnless(os.path.exists(restore.POWERSHELL), 'no Windows PowerShell')
    def test_powershell_decodes_command_line_exactly(self):
        # Runs PowerShell only to decode a string; nothing is created or killed.
        cmd = subprocess.list2cmdline(["C:\\D\u1ea7u t\u01b0 'x' $y\\pythonw.exe", 'a "b"', '\u2019q\u2018'])
        script = '$c = %s\n[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($c))' % (
            restore.PS_DECODE % restore.encode_ps(cmd))
        for _ in range(3):  # PowerShell 5.1 now and then exits 0 with empty stdout when run back to back
            res = subprocess.run([restore.POWERSHELL, '-NoProfile', '-NonInteractive', '-EncodedCommand',
                                  restore.encode_ps(script)], capture_output=True, text=True, timeout=60,
                                 creationflags=restore.NO_WINDOW)
            if res.stdout.strip():
                break
        self.assertEqual(base64.b64decode(res.stdout.strip()).decode('utf-16-le'), cmd,
                         'rc=%s stderr=%r' % (res.returncode, res.stderr[-500:]))

    def test_aumid_parsing_and_fallback(self):
        pkg = 'Claude_pzs8sxrjxfjjc'
        self.assertEqual(restore.parse_aumid('\r\nClaude_pzs8sxrjxfjjc!Claude\r\n', pkg), 'Claude_pzs8sxrjxfjjc!Claude')
        self.assertEqual(restore.parse_aumid('Claude_other!App\nClaude_pzs8sxrjxfjjc!Claude2\n', pkg),
                         'Claude_pzs8sxrjxfjjc!Claude2')
        self.assertEqual(restore.parse_aumid('Claude_other!App\n', pkg), 'Claude_other!App')
        self.assertEqual(restore.parse_aumid('', pkg), pkg + '!Claude')
        self.assertEqual(restore.parse_aumid('Get-StartApps : error\nClaude_x\n', pkg), pkg + '!Claude')
        base = os.path.join(r'C:\Users\user\AppData\Local\Packages', pkg, 'LocalCache', 'Roaming', 'Claude')
        with mock.patch.object(restore.subprocess, 'run', side_effect=OSError('no powershell')):
            self.assertEqual(restore.resolve_aumid(base), pkg + '!Claude')
        done = subprocess.CompletedProcess([], 0, stdout='Claude_pzs8sxrjxfjjc!Claude\n')
        with mock.patch.object(restore.subprocess, 'run', return_value=done) as run:
            self.assertEqual(restore.resolve_aumid(base), 'Claude_pzs8sxrjxfjjc!Claude')
        self.assertIn('Get-StartApps', run.call_args[0][0][-1])


class WorkerTest(Base):
    """Worker order: sleep -> kill -> wait -> sync -> launch."""

    def run_worker(self, alive, sync_rc=0, still_running=False, sync_exc=None, dry=False, kill_exc=None):
        clock, events = Clock(), []

        def listing():
            events.append(('list', clock.now))
            return [(p, img) for p, img in [(os.getpid(), APP)] + alive(clock.now)]

        def fake_run(argv, **kw):
            self.assertEqual(argv[0], restore.TASKKILL)
            self.assertEqual(argv[1:4], ['/F', '/T', '/PID'])
            self.assertNotEqual(int(argv[4]), os.getpid())
            events.append(('kill', int(argv[4]), clock.now))
            if kill_exc and int(argv[4]) in kill_exc:
                raise kill_exc[int(argv[4])]
            return subprocess.CompletedProcess(argv, 0)

        def fake_sync(argv):
            events.append(('sync', argv, clock.now))
            if sync_exc:
                raise sync_exc
            return sync_rc

        def fake_popen(argv, **kw):
            events.append(('launch', argv, clock.now))
            return mock.Mock(pid=1)

        def fake_sleep(s):
            events.append(('sleep', s)) if s >= 1 else None
            clock.sleep(s)

        with mock.patch.object(restore.time, 'monotonic', clock.monotonic), \
                mock.patch.object(restore.time, 'sleep', fake_sleep), \
                mock.patch.object(sync, 'list_processes', listing), \
                mock.patch.object(sync, 'app_running', return_value=still_running), \
                mock.patch.object(sync, 'main', fake_sync), \
                mock.patch.object(restore.subprocess, 'run', fake_run), \
                mock.patch.object(restore.subprocess, 'Popen', fake_popen), \
                mock.patch.object(restore, 'in_job', return_value=False):
            argv = ['--worker', '--delay', '15', '--aumid', 'Claude_pzs8sxrjxfjjc!Claude'] + (['--dry-run'] if dry else [])
            rc = restore.main(argv)
        return rc, [e for e in events if e[0] != 'list']

    def kinds(self, events):
        return [e[0] for e in events]

    def test_order_kill_wait_sync_launch(self):
        rc, ev = self.run_worker(lambda t: [(101, APP), (102, CLI)] if t < 16 else [])
        self.assertEqual(rc, 0, self.read_log())
        self.assertEqual(self.kinds(ev), ['sleep', 'kill', 'kill', 'sync', 'launch'])
        self.assertEqual(ev[0], ('sleep', 15))
        self.assertEqual([e[1] for e in ev[1:3]], [101, 102])
        self.assertEqual(ev[3][1], [])  # real sync, no flags
        self.assertGreaterEqual(ev[3][2], 16)  # only after every claude.exe is gone
        self.assertEqual(ev[4][1], [restore.EXPLORER, r'shell:AppsFolder\Claude_pzs8sxrjxfjjc!Claude'])
        log = self.read_log()
        self.assertIn('all claude.exe gone', log)
        self.assertIn('app running before sync=0', log)

    def test_second_kill_pass_after_10s(self):
        rc, ev = self.run_worker(lambda t: [(101, APP)] if t < 15 + 12 else [])
        self.assertEqual(rc, 0, self.read_log())
        self.assertEqual(self.kinds(ev), ['sleep', 'kill', 'kill', 'sync', 'launch'])
        self.assertEqual(ev[2][2] - ev[1][2], 10)
        self.assertIn('second kill pass', self.read_log())

    def test_give_up_after_30s_still_syncs_and_relaunches(self):
        rc, ev = self.run_worker(lambda t: [(101, APP)], still_running=True)
        self.assertEqual(rc, 1)
        self.assertEqual(self.kinds(ev), ['sleep', 'kill', 'kill', 'sync', 'launch'])
        self.assertEqual(ev[3][2] - ev[1][2], 30)
        log = self.read_log()
        self.assertIn('GAVE UP after 30s', log)
        self.assertIn('app running before sync=1 - sync will skip the signed-in account', log)

    def test_sync_failure_still_relaunches(self):
        rc, ev = self.run_worker(lambda t: [], sync_exc=RuntimeError('boom'))
        self.assertEqual(rc, 1)
        self.assertEqual(self.kinds(ev), ['sleep', 'sync', 'launch'])
        self.assertIn('boom', self.read_log())
        rc, ev = self.run_worker(lambda t: [], sync_rc=1)
        self.assertEqual((rc, self.kinds(ev)), (1, ['sleep', 'sync', 'launch']))

    def test_taskkill_raising_keeps_killing_and_syncs(self):
        # regression: one taskkill raising used to skip the other kills and the sync
        exc = {101: subprocess.TimeoutExpired('taskkill', 30), 103: OSError('taskkill blocked')}
        rc, ev = self.run_worker(lambda t: [(101, APP), (102, CLI), (103, STANDALONE)] if t < 16 else [],
                                 kill_exc=exc)
        self.assertEqual(rc, 0, self.read_log())
        self.assertEqual(self.kinds(ev), ['sleep', 'kill', 'kill', 'kill', 'sync', 'launch'])
        self.assertEqual([e[1] for e in ev[1:4]], [101, 102, 103])
        log = self.read_log()
        self.assertIn('taskkill pid=101 FAIL', log)
        self.assertIn('taskkill pid=103 FAIL', log)
        self.assertIn('all claude.exe gone', log)

    def test_kill_step_crash_still_syncs_and_relaunches(self):
        with mock.patch.object(restore, 'kill_all', side_effect=RuntimeError('enum failed')):
            rc, ev = self.run_worker(lambda t: [])
        self.assertEqual((rc, self.kinds(ev)), (1, ['sleep', 'sync', 'launch']))
        self.assertIn('FAIL kill', self.read_log())

    def test_worker_dry_run_does_nothing(self):
        rc, ev = self.run_worker(lambda t: [(101, APP), (102, CLI)], dry=True)
        self.assertEqual((rc, ev), (0, []))
        log = self.read_log()
        self.assertIn('would kill pid=101', log)
        self.assertIn('would kill pid=102', log)
        self.assertNotIn('would kill pid=%d ' % os.getpid(), log)
        self.assertIn(r'shell:AppsFolder\Claude_pzs8sxrjxfjjc!Claude', log)


class DefaultModeTest(Base):
    def setUp(self):
        super().setUp()
        local, self.home = os.path.join(self.tmp, 'Local'), os.path.join(self.tmp, 'home')
        self.pkg = os.path.join(local, 'Packages', 'Claude_pzs8sxrjxfjjc', 'LocalCache', 'Roaming', 'Claude')
        self.root = os.path.join(self.pkg, 'claude-code-sessions')
        acc_a, acc_b, acc_c, org = (str(uuid.UUID(int=i << 96)) for i in (0xA, 0xB, 0xC, 0xF))
        for acc in (acc_a, acc_b, acc_c):
            os.makedirs(os.path.join(self.root, acc, org))
        with open(os.path.join(self.pkg, 'config.json'), 'w', encoding='utf-8') as fh:
            json.dump({'lastKnownAccountUuid': acc_a}, fh)
        for i in range(2):  # B has both, A (active) only the first: 1 ADD to A + 2 to C
            x = str(uuid.uuid4())
            for acc in (acc_b, acc_a) if i == 0 else (acc_b,):
                with open(os.path.join(self.root, acc, org, 'local_%s.json' % x), 'w', encoding='utf-8') as fh:
                    json.dump({'cliSessionId': x, 'title': 'Ph\u00e2n t\u00edch %d' % i, 'lastActivityAt': 1}, fh)
            os.makedirs(os.path.join(self.home, '.claude', 'projects', 'D--x'), exist_ok=True)
            open(os.path.join(self.home, '.claude', 'projects', 'D--x', x + '.jsonl'), 'w').close()
        self.env = mock.patch.dict(os.environ, {'LOCALAPPDATA': local, 'USERPROFILE': self.home})
        self.env.start()
        self.calls = []

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def fake_run(self, wmi_out='0 4242\r\n'):
        def run(argv, **kw):
            self.calls.append(argv)
            self.assertNotEqual(argv[0], restore.TASKKILL)
            if 'Get-StartApps' in argv[-1]:
                return subprocess.CompletedProcess(argv, 0, stdout='Claude_pzs8sxrjxfjjc!Claude\n')
            self.assertEqual(argv[:4], [restore.POWERSHELL, '-NoProfile', '-NonInteractive', '-EncodedCommand'])
            self.assertEqual(kw.get('creationflags'), restore.NO_WINDOW)
            return subprocess.CompletedProcess(argv, 0, stdout=wmi_out, stderr='')
        return run

    def run_main(self, *argv, wmi_out='0 4242\r\n'):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(restore.subprocess, 'run', self.fake_run(wmi_out)), \
                mock.patch.object(restore.subprocess, 'Popen', return_value=mock.Mock(pid=777)) as popen, \
                mock.patch.object(sync, 'list_processes', return_value=[(101, APP), (102, CLI), (103, OTHERS[0])]):
            rc = restore.main(list(argv))
        return rc, out.getvalue(), popen

    def test_default_spawns_one_worker_via_wmi_and_kills_nothing(self):
        before = sorted(os.walk(self.root))
        rc, out, popen = self.run_main()
        self.assertEqual(rc, 0, out)
        self.assertIn('Plan: 1 session(s) will be added to the current account (all accounts: add=3 ', out)
        self.assertIn('Worker started (WMI), pid 4242.', out)
        self.assertIn('Claude will close in 15 seconds and reopen by itself.', out)
        popen.assert_not_called()
        spawns = [a for a in self.calls if '-EncodedCommand' in a]
        self.assertEqual(len(spawns), 1)
        script = base64.b64decode(spawns[0][-1]).decode('utf-16-le')
        b64 = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script).group(1)
        cmd = argv_of(base64.b64decode(b64).decode('utf-16-le'))
        self.assertEqual(cmd[1:], [os.path.abspath(restore.__file__), '--worker', '--delay', '15',
                                   '--aumid', 'Claude_pzs8sxrjxfjjc!Claude'])
        self.assertEqual(sorted(os.walk(self.root)), before)  # the plan wrote nothing
        self.assertEqual(self.read_log(), '')

    def test_fail_in_card_title_does_not_block_plan(self):
        # regression: a title containing FAIL made the plan step fail for good
        for folder, _, names in os.walk(self.root):
            for name in names:
                path = os.path.join(folder, name)
                with open(path, encoding='utf-8') as fh:
                    card = json.load(fh)
                card['title'] = 'Fix FAILing CI tests'
                with open(path, 'w', encoding='utf-8') as fh:
                    json.dump(card, fh)
        for argv in ((), ('--dry-run',)):
            rc, out, popen = self.run_main(*argv)
            self.assertEqual(rc, 0, out)
            self.assertIn('Plan: 1 session(s) will be added to the current account', out)
            self.assertNotIn('FAIL', out)

    def test_wmi_failure_falls_back_to_detached(self):
        rc, out, popen = self.run_main('--delay', '20', wmi_out='9 \r\n')
        self.assertEqual(rc, 0, out)
        self.assertIn('WMI unavailable', out)
        self.assertIn('Worker started (detached process), pid 777.', out)
        self.assertIn('Claude will close in 20 seconds', out)
        popen.assert_called_once()
        flags = popen.call_args[1]['creationflags']
        for f in (subprocess.DETACHED_PROCESS, subprocess.CREATE_NEW_PROCESS_GROUP, subprocess.CREATE_BREAKAWAY_FROM_JOB):
            self.assertTrue(flags & f)
        self.assertIn('--delay 20', popen.call_args[0][0])

    def test_dry_run_spawns_nothing(self):
        rc, out, popen = self.run_main('--dry-run')
        self.assertEqual(rc, 0, out)
        popen.assert_not_called()
        self.assertFalse([a for a in self.calls if '-EncodedCommand' in a])
        self.assertIn('Plan: 1 session(s) will be added', out)
        self.assertIn('Worker command line: ', out)
        self.assertIn('--worker --delay 15 --aumid Claude_pzs8sxrjxfjjc!Claude', out)
        self.assertIn('Would kill 2 process(es) now:', out)
        self.assertIn('101 ' + APP, out)
        self.assertNotIn(OTHERS[0], out)

    def test_no_package_folder_fails_with_2(self):
        with mock.patch.object(sync, 'claude_dir', return_value=None):
            rc, out, popen = self.run_main()
        self.assertEqual(rc, 2)
        self.assertIn('FAIL', out)
        self.assertEqual(self.calls, [])
        popen.assert_not_called()

    def test_failed_plan_kills_nothing(self):
        with mock.patch.object(sync, 'main', return_value=1):
            rc, out, popen = self.run_main()
        self.assertEqual(rc, 1)
        self.assertIn('plan step failed', out)
        self.assertFalse([a for a in self.calls if '-EncodedCommand' in a])
        popen.assert_not_called()


class RepoTest(unittest.TestCase):
    """The folder is the published git repo."""

    def test_gitignore_covers_files_sync_writes(self):
        with open(os.path.join(HERE, '.gitignore'), encoding='utf-8') as fh:
            pats = [p.strip().rstrip('/') for p in fh if p.strip() and not p.startswith('#')]
        for name in ('session-sync.log', 'session-sync.log.1', 'backup-done.txt', 'session-sync.lock', '__pycache__'):
            self.assertTrue(any(fnmatch.fnmatch(name, p) for p in pats), name)

    def test_skill_md_frontmatter(self):
        with open(os.path.join(HERE, 'SKILL.md'), encoding='utf-8') as fh:
            text = fh.read()
        head = text.split('---')[1]
        self.assertIn('name: claude-session-restore', head)
        self.assertIn('/claude-session-restore', head)
        self.assertIn('restore.py', text)

    def test_no_personal_data(self):
        user = getpass.getuser().lower()
        for name in ('sync.py', 'test_sync.py', 'restore.py', 'test_restore.py', 'SKILL.md', 'README.md', '.gitignore'):
            with open(os.path.join(HERE, name), encoding='utf-8') as fh:
                text = fh.read()
            if user not in ('user', 'claude'):
                self.assertNotIn('\\' + user, text.lower(), name)
            self.assertIsNone(re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', text), name)
            self.assertIsNone(re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', text, re.I), name)
        with open(os.path.join(HERE, 'restore.py'), 'rb') as fh:
            self.assertTrue(fh.read().isascii())


if __name__ == '__main__':
    unittest.main()
