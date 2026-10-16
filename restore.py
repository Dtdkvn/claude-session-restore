"""Bring every Code session from the other Claude accounts on this PC into the
current one, then close every claude.exe and reopen the desktop app so the
sidebar shows them (the app reads its session cards only at startup).

Default mode: preflight, print the plan, start a hidden worker outside
Claude's process tree (WMI), exit. The worker waits --delay seconds, kills
every claude.exe, runs sync.py for real, relaunches the app and logs every
step to session-sync.log. Standard library only, Python 3.10+, Windows.
"""
import argparse
import base64
import contextlib
import ctypes
import io
import os
import re
import subprocess
import sys
import time
import traceback
from ctypes import wintypes

import sync

SYSTEM_ROOT = os.environ.get('SystemRoot') or r'C:\Windows'
POWERSHELL = os.path.join(SYSTEM_ROOT, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
TASKKILL = os.path.join(SYSTEM_ROOT, 'System32', 'taskkill.exe')
EXPLORER = os.path.join(SYSTEM_ROOT, 'explorer.exe')
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
POLL_S, SECOND_PASS_S, GIVE_UP_S = 0.5, 10, 30
AUMID_RE = re.compile(r'Claude_[A-Za-z0-9]+![A-Za-z0-9._-]+')
PLAN_RE = re.compile(r'^\S+ \S+ (ADD|UPDATE|REMOVE|TOMBSTONE|ERROR)\s+([0-9a-fA-F-]{8})/\S+ (\S+)')
# PowerShell decodes the command line itself, so no quoting survives into PS.
PS_DECODE = "[Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('%s'))"


def log(msg):
    try:  # a locked log file must never stop the kill -> sync -> relaunch chain
        sync.log('restore: ' + msg)
    except Exception:
        pass


def kill_targets(procs, own_pid):
    """Every claude.exe (desktop app, bundled CLI, standalone CLI) but us."""
    return [(pid, image) for pid, image in procs
            if pid != own_pid and os.path.basename(image).lower() == 'claude.exe']


def parse_aumid(text, pkg):
    ids = [line.strip() for line in text.splitlines() if AUMID_RE.fullmatch(line.strip())]
    for aumid in ids:
        if aumid.lower().startswith(pkg.lower() + '!'):
            return aumid
    return ids[0] if ids else pkg + '!Claude'


def resolve_aumid(base):
    """base = ...\\Packages\\<family>\\LocalCache\\Roaming\\Claude."""
    pkg = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(base))))
    try:
        out = subprocess.run(
            [POWERSHELL, '-NoProfile', '-NonInteractive', '-Command',
             "Get-StartApps | Where-Object AppID -like 'Claude_*!*' | ForEach-Object AppID"],
            capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        out = ''
    return parse_aumid(out or '', pkg)


def pythonw():
    path = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    return path if os.path.isfile(path) else sys.executable


def worker_cmdline(python, delay, aumid):
    return subprocess.list2cmdline([python, os.path.abspath(__file__), '--worker',
                                    '--delay', str(delay), '--aumid', aumid])


def encode_ps(script):
    return base64.b64encode(script.encode('utf-16-le')).decode('ascii')


def wmi_script(cmd):
    return ("$c = %s\n"
            "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$c}\n"
            "'{0} {1}' -f $r.ReturnValue, $r.ProcessId\n" % (PS_DECODE % encode_ps(cmd)))


def spawn_wmi(cmd):
    """Parent becomes WmiPrvSE: outside the claude.exe tree that taskkill /T walks."""
    res = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-EncodedCommand', encode_ps(wmi_script(cmd))],
                         capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
    m = re.search(r'^(\d+) (\d+)\s*$', res.stdout or '', re.M)
    if not m or m.group(1) != '0':
        raise RuntimeError('rc=%s %s' % (res.returncode, ((res.stdout or '') + (res.stderr or '')).strip()[-300:]))
    return int(m.group(2))


def spawn_detached(cmd):
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB
    return subprocess.Popen(cmd, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid


def plan(base):
    """sync.py --dry-run --assume-not-running, reduced to one line."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = sync.main(['--dry-run', '--assume-not-running',
                        '--root', os.path.join(base, 'claude-code-sessions'),
                        '--config', os.path.join(base, 'config.json')])
    text = out.getvalue()
    # sync.main returns 1 on every FAIL/ERROR path; no text match, card titles are in the output
    if rc != 0 or 'run dry=1' not in text:
        raise RuntimeError('plan step failed, nothing killed:\n' + text[-2000:])
    active = (sync.read_active(os.path.join(base, 'config.json')) or '')[:8].lower()
    counts, mine = dict.fromkeys(sync.KINDS, 0), set()
    for line in text.splitlines():
        m = PLAN_RE.match(line)
        if m:
            counts[m.group(1)] += 1
            if m.group(1) == 'ADD' and active and m.group(2).lower() == active:
                mine.add(m.group(3))
    total = ' '.join('%s=%d' % (k.lower(), v) for k, v in counts.items())
    if not active:
        return 'Plan: current account unknown (all accounts: %s).' % total
    return 'Plan: %d session(s) will be added to the current account (all accounts: %s).' % (len(mine), total)


def default(args):
    base = sync.claude_dir()
    if not base:
        print('FAIL: Claude Store package folder not found (%LOCALAPPDATA%\\Packages\\Claude_*), nothing done.')
        return 2
    aumid = resolve_aumid(base)
    print(plan(base))
    cmd = worker_cmdline(pythonw(), args.delay, aumid)
    if args.dry_run:
        print('Worker command line: ' + cmd)
        targets = kill_targets(sync.list_processes(), os.getpid())
        print('Would kill %d process(es) now:' % len(targets))
        for pid, image in targets:
            print('  %6d %s' % (pid, image))
        return 0
    try:
        pid, how = spawn_wmi(cmd), 'WMI'
    except Exception as e:
        print('WMI unavailable (%s); starting the worker as a detached process instead.' % e)
        pid, how = spawn_detached(cmd), 'detached process'
    print('Worker started (%s), pid %d.' % (how, pid))
    print('Claude will close in %d seconds and reopen by itself.' % args.delay)
    return 0


def in_job():
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    res = wintypes.BOOL()
    return bool(k32.IsProcessInJob(k32.GetCurrentProcess(), None, ctypes.byref(res)) and res.value)


def taskkill(pid, image):
    """Never raises: the poll, second pass and give-up decide what happens next."""
    try:
        res = subprocess.run([TASKKILL, '/F', '/T', '/PID', str(pid)], capture_output=True,
                             timeout=30, creationflags=NO_WINDOW)
        log('taskkill pid=%d rc=%d %s' % (pid, res.returncode, image))
    except Exception as e:
        log('taskkill pid=%d FAIL %r %s' % (pid, e, image))


def kill_all(own_pid):
    """Kill every claude.exe, poll until none is left; False if we gave up."""
    start = time.monotonic()
    targets = kill_targets(sync.list_processes(), own_pid)
    log('killing %d claude.exe' % len(targets))
    for pid, image in targets:
        taskkill(pid, image)
    second = False
    while True:
        left = kill_targets(sync.list_processes(), own_pid)
        elapsed = time.monotonic() - start
        if not left:
            log('all claude.exe gone after %.1fs' % elapsed)
            return True
        if elapsed >= GIVE_UP_S:
            log('GAVE UP after %ds, still running: %s' % (GIVE_UP_S, ' '.join(str(p) for p, _ in left)))
            return False
        if elapsed >= SECOND_PASS_S and not second:
            second = True
            log('second kill pass, %d left' % len(left))
            for pid, image in left:
                taskkill(pid, image)
        time.sleep(POLL_S)


def launch(aumid):
    try:
        subprocess.Popen([EXPLORER, 'shell:AppsFolder\\' + aumid], creationflags=NO_WINDOW, close_fds=True)
        log('relaunched shell:AppsFolder\\' + aumid)
        return True
    except Exception:
        log('FAIL relaunch ' + traceback.format_exc())
        return False


def worker(args):
    me, parent = os.getpid(), os.getppid()
    procs = sync.list_processes()
    aumid = args.aumid or resolve_aumid(sync.claude_dir() or '')
    log('worker pid=%d parent=%d %s in_job=%d delay=%ds dry=%d' % (
        me, parent, dict(procs).get(parent, '?'), in_job(), args.delay, args.dry_run))
    if args.dry_run:
        for pid, image in kill_targets(procs, me):
            log('would kill pid=%d %s' % (pid, image))
        log('would run sync (root=%s), then launch shell:AppsFolder\\%s' % (sync.claude_dir(), aumid))
        return 0
    try:
        time.sleep(args.delay)
        ok = kill_all(me)
    except Exception:
        ok = False
        log('FAIL kill ' + traceback.format_exc())
    try:  # always sync: if the app survived, sync leaves the signed-in account alone
        still = sync.app_running()
        log('app running before sync=%d%s' % (still, ' - sync will skip the signed-in account' if still else ''))
        rc = sync.main([])
        log('sync rc=%d' % rc)
        ok = ok and rc == 0
    except Exception:
        ok = False
        log('FAIL ' + traceback.format_exc())
    ok = launch(aumid) and ok
    log('worker done ok=%d' % ok)
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description='Sync Claude sessions across accounts, then restart the app.')
    ap.add_argument('--dry-run', action='store_true', help='show the plan and what would be killed; do nothing')
    ap.add_argument('--delay', type=int, default=15, help='seconds before Claude is closed (default 15)')
    ap.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--aumid', help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.worker:
        try:
            return worker(args)
        except BaseException:
            try:
                log('FAIL ' + traceback.format_exc())
            except Exception:
                pass
            return 1
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    try:
        return default(args)
    except Exception as e:
        print('FAIL: %s' % e)
        return 1


if __name__ == '__main__':
    sys.exit(main())
