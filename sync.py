"""Keep the Claude desktop app's Code-session sidebar cards identical across
every account folder on this PC (see README.md; restore.py runs it).

Only local_*.json (cards) and deleted_* (tombstones) files are ever written
or removed, and never in the active account's folders while the desktop app
is running (unless --include-active). Standard library only, Python 3.10+.
"""
import argparse
import ctypes
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from ctypes import wintypes

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
MS = 1000000  # ns per ms
SLACK_MS = 60000
STALE_LOCK_S = 600
LOG_MAX_BYTES = 1024 * 1024
UUID = '[0-9a-fA-F-]{36}'
CARD_RE = re.compile(r'^local_(%s)\.json$' % UUID)
TOMB_RE = re.compile(r'^deleted_(%s)$' % UUID)
KINDS = ('ADD', 'UPDATE', 'REMOVE', 'TOMBSTONE', 'ERROR')
_dry = False


class StateChanged(Exception):
    pass


def claude_dir():
    """The MSIX app's %APPDATA%\\Claude is visible only inside the app's own
    process tree; processes outside it (terminals, the WMI-spawned worker)
    need the package folder."""
    local = os.environ.get('LOCALAPPDATA') or os.path.join(os.path.expanduser('~'), 'AppData', 'Local')
    pkg = sorted(glob.glob(os.path.join(glob.escape(local), 'Packages', 'Claude_*',
                                        'LocalCache', 'Roaming', 'Claude')))
    return pkg[0] if pkg else None  # never guess %APPDATA%: app detection only knows the Store app


def log(msg):
    line = time.strftime('%Y-%m-%d %H:%M:%S ') + msg
    if _dry:
        print(line)
        return
    path = os.path.join(TOOL_DIR, 'session-sync.log')
    try:
        if os.path.getsize(path) >= LOG_MAX_BYTES:
            os.replace(path, path + '.1')
    except OSError:
        pass
    with open(path, 'a', encoding='utf-8', errors='backslashreplace') as fh:
        fh.write(line + '\n')


def is_app_image(path):
    """The desktop app runs from ...\\WindowsApps\\Claude_*; the bundled CLI is
    also claude.exe but runs from the package's Roaming folder: not the app."""
    image = path.lower()
    return image.endswith('\\claude.exe') and '\\windowsapps\\claude_' in image


def app_running():
    """True if the MSIX desktop app (not the bundled CLI) has a process."""
    return any(is_app_image(image) for _, image in list_processes())


def list_processes():
    """[(pid, full image path)] of every process this user can query."""
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    n = 4096
    while True:
        pids = (wintypes.DWORD * n)()
        got = wintypes.DWORD()
        if not k32.K32EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(got)):
            raise ctypes.WinError(ctypes.get_last_error())
        if got.value < ctypes.sizeof(pids):
            break
        n *= 2
    buf = ctypes.create_unicode_buffer(32768)
    found = []
    for pid in pids[:got.value // ctypes.sizeof(wintypes.DWORD)]:
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        try:
            size = wintypes.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                found.append((pid, buf.value))
        finally:
            k32.CloseHandle(h)
    return found


def read_active(config):
    """Only the lastKnownAccountUuid key; the file also holds token caches."""
    try:
        with open(config, 'rb') as fh:
            value = json.load(fh).get('lastKnownAccountUuid')
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


def read_folder(path):
    cards, tombs = {}, {}
    for name in os.listdir(path):
        m_card, m_tomb = CARD_RE.match(name), TOMB_RE.match(name)
        if not (m_card or m_tomb):
            continue
        p = os.path.join(path, name)
        try:
            mtime = os.stat(p).st_mtime_ns
            with open(p, 'rb') as fh:
                raw = fh.read()
        except OSError:
            if m_card:  # locked or vanishing: treat like a half-written card
                cards[m_card.group(1)] = {'path': p, 'mtime': 0, 'raw': b'', 'data': None}
            continue
        if m_card:
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            cards[m_card.group(1)] = {'path': p, 'mtime': mtime, 'raw': raw,
                                      'data': data if isinstance(data, dict) else None}
        else:
            try:
                tombs[m_tomb.group(1)] = int(raw.decode('ascii').strip())
            except ValueError:
                tombs[m_tomb.group(1)] = mtime // MS
    return cards, tombs


def write_atomic(folder, name, raw, mtime_ns):
    fd, tmp = tempfile.mkstemp(prefix='.sync-', suffix='.tmp', dir=folder)
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(raw)
        os.utime(tmp, ns=(mtime_ns, mtime_ns))
        os.replace(tmp, os.path.join(folder, name))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def ensure_backup(root):
    marker = os.path.join(TOOL_DIR, 'backup-done.txt')
    if os.path.exists(marker):
        return
    dest = os.path.join(os.path.dirname(root),
                        'claude-code-sessions-backup-sync-' + time.strftime('%Y%m%d-%H%M%S'))
    try:
        shutil.copytree(root, dest)
        with open(marker, 'w', encoding='utf-8') as fh:
            fh.write(dest + '\n')
    except OSError as e:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError('backup failed, nothing written: %s' % str(e)[:300]) from None
    log('BACKUP ' + dest)


def title_of(card):
    data = (card or {}).get('data') or {}
    return str(data.get('title') or '').replace('\r', ' ').replace('\n', ' ')[:60]


def rank(card):
    la = card['data'].get('lastActivityAt')
    return card['mtime'], la if isinstance(la, (int, float)) else 0


def sync_folder(f, tomb, canon, transcripts, act):
    cards, tombs, path = f['cards'], f['tombs'], f['path']
    # a. tombstones: remove copies not changed since the deletion, then propagate
    for x, t in sorted(tomb.items()):
        card = cards.get(x)
        if card is not None and card['data'] is None:
            continue  # unreadable or half-written: never touch
        if card is not None and card['mtime'] // MS <= t + SLACK_MS:
            act('REMOVE', f, x, title_of(card), lambda: os.remove(card['path']))
            del cards[x]
            card = None
        if card is None and x not in tombs:
            act('TOMBSTONE', f, x, title_of(canon.get(x)),
                lambda: write_atomic(path, 'deleted_' + x, str(t).encode('ascii'), t * MS))
            tombs[x] = t
    # b. cards, newest first so the newest of duplicate cliSessionIds wins
    for x, src in sorted(canon.items(), key=lambda kv: (kv[1]['mtime'], kv[0]), reverse=True):
        t = tomb.get(x)
        if t is not None and t + SLACK_MS >= src['mtime'] // MS:
            continue  # deleted after its last change
        name = 'local_%s.json' % x
        mine = cards.get(x)
        if mine is not None:
            if mine['data'] is not None and mine['mtime'] < src['mtime'] and mine['raw'] != src['raw']:
                act('UPDATE', f, x, title_of(src),
                    lambda: write_atomic(path, name, src['raw'], src['mtime']))
                cards[x] = dict(src, path=os.path.join(path, name))
            continue
        cli = src['data'].get('cliSessionId')
        if not isinstance(cli, str) or not cli or cli.lower() not in transcripts:
            continue
        if x in tombs or cli in tombs:
            continue
        if any(c['data'] and c['data'].get('cliSessionId') == cli for c in cards.values()):
            continue
        act('ADD', f, x, title_of(src), lambda: write_atomic(path, name, src['raw'], src['mtime']))
        cards[x] = dict(src, path=os.path.join(path, name))


def sync(root, projects, running, active, changed=None):
    folders = []
    for acc in sorted(os.listdir(root)):
        acc_path = os.path.join(root, acc)
        if not re.fullmatch(UUID, acc) or not os.path.isdir(acc_path):
            continue
        for org in sorted(os.listdir(acc_path)):
            path = os.path.join(acc_path, org)
            if re.fullmatch(UUID, org) and os.path.isdir(path):
                cards, tombs = read_folder(path)
                folders.append({'acc': acc, 'short': acc[:8] + '/' + org[:8], 'path': path,
                                'cards': cards, 'tombs': tombs})
    writable = [f for f in folders if not running or f['acc'].lower() != active.lower()]

    tomb, canon = {}, {}
    for f in folders:
        for x, t in f['tombs'].items():
            tomb[x] = max(tomb.get(x, t), t)
        for x, c in f['cards'].items():
            if c['data'] is not None and (x not in canon or rank(c) > rank(canon[x])):
                canon[x] = c
    transcripts = {os.path.basename(p)[:-6].lower()
                   for p in glob.glob(os.path.join(glob.escape(projects), '*', '*.jsonl'))}

    counts = dict.fromkeys(KINDS, 0)

    def act(kind, f, x, title, op):
        nonlocal changed
        if not _dry:
            if changed is not None:  # app opened or switched account since the first check?
                if changed():
                    raise StateChanged()
                changed = None
            ensure_backup(root)
            op()
        counts[kind] += 1
        log('%-9s %s %s %s' % (kind, f['short'], x, title))

    for f in writable:
        try:
            sync_folder(f, tomb, canon, transcripts, act)
        except OSError as e:  # skip the rest of this folder; others still sync
            counts['ERROR'] += 1
            log('ERROR     %s %r' % (f['short'], e))
    return counts, len(folders), len(writable)


def take_lock():
    path = os.path.join(TOOL_DIR, 'session-sync.lock')
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode('ascii'))
            os.close(fd)
            return path
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) <= STALE_LOCK_S:
                    return None
                os.remove(path)
                log('stale lock taken')
            except FileNotFoundError:
                pass
    return None


def run(args):
    global _dry
    _dry = args.dry_run
    started = time.time()
    lock = None
    if not _dry:
        lock = take_lock()
        if lock is None:
            log('skip: another run holds the lock')
            return 0
    try:
        running = app_running() if args.running is None else args.running
        active = args.active or (read_active(args.config) if running else None)
        if running and not active:
            log('skip: desktop app running but active account unknown, no folder writable')
            return 0

        def changed():
            now = app_running()
            return (now, args.active or (read_active(args.config) if now else None)) != (running, active)
        try:
            counts, nfolders, nwritable = sync(os.path.abspath(args.root), args.projects, running, active,
                                               changed if args.running is None else None)
        except StateChanged:
            log('skip: desktop app started or switched account during the run, nothing written')
            return 0
        log('run dry=%d running=%d active=%s folders=%d writable=%d %s %.1fs' % (
            _dry, running, (active or '-')[:8], nfolders, nwritable,
            ' '.join('%s=%d' % (k.lower(), v) for k, v in counts.items()), time.time() - started))
        return 1 if counts['ERROR'] else 0
    finally:
        if lock:
            try:
                os.remove(lock)
            except OSError:
                pass


def main(argv=None):
    try:
        base = claude_dir()
        ap = argparse.ArgumentParser(description='Sync Claude desktop session cards across accounts.')
        ap.add_argument('--dry-run', action='store_true', help='plan only, print actions, write nothing')
        ap.add_argument('--root', default=base and os.path.join(base, 'claude-code-sessions'))
        ap.add_argument('--projects', default=os.path.join(os.path.expanduser('~'), '.claude', 'projects'))
        ap.add_argument('--config', default=base and os.path.join(base, 'config.json'))
        g = ap.add_mutually_exclusive_group()
        g.add_argument('--assume-running', dest='running', action='store_const', const=True)
        g.add_argument('--assume-not-running', dest='running', action='store_const', const=False)
        g.add_argument('--include-active', dest='running', action='store_const', const=False,
                       help='also write the signed-in account; restart the app afterwards')
        ap.add_argument('--active', help='active account uuid (overrides config.json)')
        args = ap.parse_args(argv)
        if not (args.root and args.config):
            log('FAIL: Claude Store package folder not found, nothing done')
            return 1
        if args.dry_run:
            try:
                sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass
        return run(args)
    except SystemExit:
        raise
    except BaseException:
        try:
            log('FAIL ' + traceback.format_exc())
        except Exception:
            pass
        return 1


if __name__ == '__main__':
    sys.exit(main())
