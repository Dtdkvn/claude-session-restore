---
name: claude-session-restore
description: After switching accounts in the Claude desktop app on Windows, bring every Code session from the other accounts on this PC into the current account, then close and reopen Claude by itself so the sessions show. Use when the user types /claude-session-restore or asks to restore, sync or bring back sessions after changing Claude account.
---

# Restore sessions after switching account

`restore.py` in this skill's base directory does all the work: it plans the sync, starts a hidden worker outside Claude's process tree, and exits. About 15 seconds later the worker kills every `claude.exe` (this session included) together with everything started from them, syncs the session cards, and reopens the app.

1. Check the other desktop sessions with `mcp__ccd_session_mgmt__list_sessions`. If it is listed only as a deferred tool, load it first with ToolSearch `select:mcp__ccd_session_mgmt__list_sessions`; skip this check only if the tool does not exist at all. The list already leaves out the current session.
   Ask the user ONE question, and continue only on a yes, if either is true: (a) another session has `isRunning: true` (name them: they will be killed mid-turn), or (b) the user did not type `/claude-session-restore` and the skill was picked from a plain request. The question says that Claude will close and reopen by itself, and that every Claude Code session (desktop and terminal) and everything started from them (shells, scripts, test runs, dev servers, background jobs) will be killed. Otherwise do not ask: the user typing the command is the go-ahead.
2. Run, with a 120 s timeout (use the real base directory, forward slashes are fine):
   `python "<base directory>/restore.py"`
   It prints a `Plan: N session(s) will be added to the current account ...` line, the worker pid, and `Claude will close in 15 seconds and reopen by itself.`
3. Right away send ONE short reply in the user's language: N sessions will be added to this account (from the Plan line), Claude will close and reopen by itself in about 15 seconds, and if it does not reopen they should start Claude from the Start menu. Do nothing after that: no more tool calls.
4. If the command exits non-zero, does not print `Worker started`, or is blocked by a permission check: do not retry; tell the user to run the same command themselves in Windows PowerShell (outside Claude):
   `python "$env:USERPROFILE\.claude\skills\claude-session-restore\restore.py"`
   Quote the error line if there was one.

Notes: every `claude.exe` is closed with `taskkill /F /T`, including Claude Code in terminals, and so is every process started from any Claude session: shells, scripts, test runs, dev servers, terminal-pane shells, background jobs of idle sessions. Scheduled tasks and sidebar groups are not synced. The log is `session-sync.log` in this folder; see README.md for backup and undo.
