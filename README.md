# claude-session-restore

An unofficial Claude Code skill for the Claude desktop app on Windows. When you switch accounts in the app, the Code tab's sidebar only shows the sessions of the account you are signed in to, although the conversations themselves are still on disk. Type `/claude-session-restore` in any Code session and every session from the other accounts on this PC is added to the current one. Claude then closes and reopens by itself so the sidebar shows them.

## What it does

1. **Plan.** `restore.py` checks that the app's package folder exists, finds the app's Start-menu ID, and prints how many sessions will be added to the current account.
2. **Hand-off.** It starts a hidden worker (`pythonw restore.py --worker`) through WMI, so the worker runs outside Claude's process tree and survives Claude being closed. Then it exits, and the skill gives you a short reply.
3. **Worker**, about 15 s later:
   - kills every `claude.exe` with `taskkill /F /T` and waits until none is left (a second pass after 10 s; after 30 s it gives up, logs it and carries on);
   - runs `sync.py`, which copies the sidebar session cards (`local_<id>.json`) and deletion markers (`deleted_<id>`) between every account/organisation folder under `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code-sessions`. Newer copies win, and a session deleted in one account is deleted in the others. Transcripts in `%USERPROFILE%\.claude\projects` are shared by all accounts already and are never touched;
   - reopens the app (`explorer.exe shell:AppsFolder\<app id>`).

The app reads its session list only at startup, which is why it has to be restarted.

## Requirements

- Windows 10/11
- Claude desktop app from the Microsoft Store
- Python 3.10 or newer on `PATH` (standard library only)

## Install

```powershell
git clone <this repo's URL> "$env:USERPROFILE\.claude\skills\claude-session-restore"
```

If `/claude-session-restore` does not show up in a new session, restart Claude once.

## Usage

- In any Code session: `/claude-session-restore`, or ask Claude to restore your sessions after switching account.
- Preview without changing anything: `python restore.py --dry-run` prints the plan, the exact worker command line and the processes that would be killed right now.
- `--delay N` changes the 15 s wait before Claude is closed.

## What gets killed

**Every `claude.exe` on the PC**: the desktop app, the Claude Code engine bundled with it, and any Claude Code you run in a terminal. Work in progress in any of them is interrupted.

They are killed with `taskkill /F /T`, which also kills **everything started from them**: shells, scripts, test runs, dev servers, the shells in the app's terminal pane, and background jobs left running by an idle session.

When run as a skill, Claude asks first if another desktop session is still working (or if you did not type the command yourself). It cannot see terminal sessions or background jobs, so finish or save those first.

## What is not synced

- Scheduled tasks (they stay per account)
- Sidebar groups and order
- Sessions whose transcript is not on this PC, and very old sessions without a Claude Code session id

## Logs, backup, undo

- Log: `session-sync.log` in this folder (rotated to `session-sync.log.1` at 1 MB). Each change is one `ADD` / `UPDATE` / `REMOVE` / `TOMBSTONE` line; worker steps start with `restore:`.
- Backup: before its first real write, `sync.py` copies the whole `claude-code-sessions` folder to `claude-code-sessions-backup-sync-<date-time>` next to it, and writes that path to `backup-done.txt` in this folder. A fresh clone has no `backup-done.txt`, so it makes a new backup on its first run.
- Undo: quit Claude completely, rename `claude-code-sessions`, then rename the backup folder to `claude-code-sessions`. The backup is the snapshot from the first run, so sidebar cards of sessions created after it disappear (their transcripts stay in `%USERPROFILE%\.claude\projects`).
- Log, backup marker and lock file are in `.gitignore`: they contain session titles and local paths.

## Manual fallback

If the skill is blocked or fails, run it yourself in Windows PowerShell (outside Claude):

```powershell
python "$env:USERPROFILE\.claude\skills\claude-session-restore\restore.py"
```

If Claude does not come back, start it from the Start menu. To sync without the restart, run `python sync.py --include-active` and then quit and reopen Claude yourself (also from the tray icon).

## Caveats

This relies on undocumented internals of the Claude desktop app: the folder layout, the session card format and the `lastKnownAccountUuid` key in `config.json`. An app update can break it. In the session folders it only writes `local_*.json` and `deleted_*` files (through temporary `.sync-*.tmp` files); it also makes one backup folder next to `claude-code-sessions`, and writes its log, lock and backup marker in this folder.

## Tests

```powershell
python -m unittest -v test_sync test_restore
```

The tests use synthetic folders and mocks; they never kill a process or launch the app.

## Tiếng Việt

Sau khi đổi tài khoản trong app Claude (bản Microsoft Store), gõ `/claude-session-restore` ở một session bất kỳ trong tab Code. Công cụ chép danh sách session của mọi tài khoản trên máy sang tài khoản đang dùng, rồi khoảng 15 giây sau tự tắt toàn bộ `claude.exe` (kể cả Claude Code đang chạy trong terminal, việc đang làm dở sẽ bị ngắt) cùng mọi thứ được chạy từ chúng (shell, script, test, dev server, tác vụ nền của các session) và tự mở lại app. Nếu app không tự mở lại, mở Claude từ menu Start. Không đồng bộ lịch chạy tự động và nhóm trên thanh bên. Nhật ký nằm ở `session-sync.log` trong thư mục này; bản sao lưu được tạo trước lần ghi đầu tiên (đường dẫn trong `backup-done.txt`); khôi phục từ bản này sẽ mất thẻ của các session tạo sau đó (nội dung hội thoại vẫn còn). Nếu bị chặn quyền, tự chạy lệnh trong mục "Manual fallback" bằng Windows PowerShell.
