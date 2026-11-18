# AI Butler

AI Butler is a macOS CLI for disposable scratch owned by T3 jobs. It does not
manage source worktrees, shared build caches, device state, or existing artifacts.

Requires macOS, a logged-in user session, Python 3.9 or later, and a local T3
installation. No Python packages are required.

~~~sh
brew tap ipavlidakis/tap
brew trust --formula ipavlidakis/tap/ai-butler
brew install ipavlidakis/tap/ai-butler
ai-butler install
ai-butler status
~~~

Only use the trust command on Homebrew versions that require formula trust.
`ai-butler install` registers the per-user LaunchAgent. Run
`ai-butler uninstall` before removing the formula with Homebrew.

For a source checkout, use `./ai-butler` and keep the checkout at a stable path
while its LaunchAgent is installed.

## Commands

| Command | Behavior |
| --- | --- |
| `list` | Show configured absolute paths, effective timing settings, and the config file location. An optional `--json` serves scripts. |
| `add <path>` | Register a new or empty pool under the dedicated disposable root. Never adopt an arbitrary path or existing files. |
| `remove <path>` | Stop allocating and sweeping that pool. Leave the directory and its contents on disk. |
| `install` | Create and load the user's LaunchAgent if needed. Running it twice is safe. |
| `uninstall` | Unload and remove the LaunchAgent. Keep the configuration and disk data. |
| `setInterval <minutes>` | Change the sweep interval. `0` restores the default of 60 minutes. Reload the installed service to apply it. |
| `setSettlementGracePeriod <minutes>` | Change the minimum time after both the foreground command finishes and T3 settles its thread. Keep the prototype's five-minute default. If work resumes, recalculate eligibility from the new settlement. |
| `setLogRetention <days>` | Keep that many calendar days of audit logs, including today (UTC). Default: seven. Expired logs are removed on the next scheduled sweep. |
| `run [--thread <id>] [--pool <name>] -- <command>` | From the T3 checkout, opt in one foreground command. Uses `T3CODE_THREAD_ID` by default. |
| `preview` | List what a sweep would keep or remove, with reasons, without deleting data. |
| `status` | Show whether the LaunchAgent is loaded and the time and outcome of its last sweep. |

## Local settings and data

- Settings: `~/.config/ai-butler/config.json`. The CLI creates it locally on
  install or the first settings change. Use the CLI to make changes so a
  running sweeper sees them safely and an interval change reloads launchd.
- Scratch pool: a folder AI Butler scans for disposable jobs. The default is
  `~/Library/Developer/AgentArtifacts/disposable-scratch/ai-butler/tmp/`.
  You can use it without adding any paths. Each `ai-butler run` creates a
  fresh job folder there. The sweeper only removes jobs it registered, even
  if something else is stored in the pool.
- LaunchAgent: `~/Library/LaunchAgents/com.local.ai-butler.plist`.
- Latest sweep: `~/Library/Developer/AgentArtifacts/admin/ai-butler/latest.json`.
- Audit history: `~/Library/Developer/AgentArtifacts/admin/ai-butler/audit/`.
  Each UTC day gets a separate `YYYY-MM-DD.jsonl` file. Use
  `ai-butler setLogRetention 14` to keep two weeks, for example.
  It records every scheduled sweep and the path, owner, reason, and outcome
  for each deletion. AI Butler writes a durable intent before removing a job.
  To view recent events, run
  `tail -n 20 ~/Library/Developer/AgentArtifacts/admin/ai-butler/audit/"$(date -u +%F).jsonl"`.
  An intent without an outcome means there was an interruption; inspect that
  path to see whether it still exists. The audit trail is local and does not
  store a backup of deleted files.

Add another pool only if you want to group jobs separately. For example,
`ai-butler add "$HOME/Library/Developer/AgentArtifacts/disposable-scratch/ai-butler/checks"`
registers a new or empty folder; use `ai-butler run --pool checks -- <command>`
to allocate jobs there. Names start with a lowercase letter and may contain
lowercase letters, digits, and hyphens. `remove` stops watching that folder
and leaves its contents in place.

The configuration, plist, T3 database, and reports are local runtime data. They
are not part of the repository. The earlier prototype uses a separate label,
config, and pool. This tool does not migrate that installation.

## Cleanup rules

`run` supplies `TMPDIR` and `AGENT_TMP_DIR` to the foreground command. Don't
store deliverables there, hand paths to detached jobs, or reuse the paths in
later commands. AI Butler only removes paths it allocated in a registered
pool. It checks read-only local T3 state, waits for settlement and command
completion plus the grace period, and preserves active, unfinished, open, or
unrecognized jobs. If it cannot read the T3 schema or match a thread, it keeps
the data. Missing configuration disables scheduled cleanup. A command
interrupted before its completion can be recorded is also kept. T3 remains
responsible for source worktree removal.

## Checks

Run `python3 check-agent-scratch.py /absolute/path/to/test-scratch` for the
scratch lifecycle fixtures. Run `python3 check-ai-butler.py /absolute/path/to/test-scratch`
for the configuration and LaunchAgent fixture.
Pass `--live-lsof` to the scratch check to test actual open-file detection during
deletion. If lsof warns that a filesystem cannot be inspected, the sweeper
keeps the job.
