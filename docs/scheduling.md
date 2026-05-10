# Scheduling YAAMS ingestion

YAAMS ingestion is run nightly via a launchd agent. The agent invokes a single
`yaams ingest` command (no `--source` flag), which iterates every enabled source
in one pass. All sources share one schedule by design - alignment is not a
matter of multiple jobs but of one job that runs every adapter sequentially.

## launchd plist

The plist lives at `~/Library/LaunchAgents/local.yaams.ingest.plist` and
runs `yaams ingest` at 02:00 daily. Logs:

- stdout: `~/Library/Logs/yaams/ingest.log`
- stderr: `~/Library/Logs/yaams/ingest.err`

Reload after edits:

```bash
launchctl unload ~/Library/LaunchAgents/local.yaams.ingest.plist
launchctl load ~/Library/LaunchAgents/local.yaams.ingest.plist
```

## iMessage and Full Disk Access (TCC)

The `imessage` adapter reads `~/Library/Messages/chat.db`, which is gated by
macOS TCC. A logged-in shell session inherits Full Disk Access (FDA) from
Terminal/iTerm if those have been granted, but a launchd LaunchAgent does not.
Without FDA the nightly run fails with:

```
imessage: FAILED - PermissionError: [Errno 1] Operation not permitted:
  '/Users/<user>/Library/Messages/chat.db'
```

The fix is to grant FDA to the binary that launchd actually executes (the
`yaams` entry point). One-time setup:

1. Open **System Settings - Privacy & Security - Full Disk Access**.
2. Click the `+` button, press `Cmd+Shift+G`, and paste the resolved binary
   path. Resolve it first with:

   ```bash
   readlink -f "$(which yaams)"
   ```

   For pipx-managed installs this is something like
   `~/.local/pipx/venvs/yaams/bin/yaams`. Grant FDA to that exact file (not
   the `~/.local/bin/yaams` symlink - TCC follows the resolved path).

3. While you're there, also grant FDA to `/bin/zsh` and `/bin/bash` if you
   want to be able to run other launchd-spawned shell scripts that touch
   protected paths.

4. Reload the agent (see above) and confirm it works:

   ```bash
   launchctl kickstart -k gui/$(id -u)/local.yaams.ingest
   tail -f ~/Library/Logs/yaams/ingest.log
   ```

   The next run should show `imessage: <N> items (<N> new)` instead of the
   PermissionError.

If you reinstall yaams via pipx, the venv path may change and FDA will need to
be re-granted to the new binary.

## Verifying alignment

The `ingest_runs` table records one row per (run_id, source) and exposes timing
and status across the whole nightly run:

```sql
SELECT
  source,
  started_at,
  printf('%.1fs', duration_ms / 1000.0) AS duration,
  items_seen,
  items_new,
  status
FROM ingest_runs
WHERE run_id = (SELECT run_id FROM ingest_runs ORDER BY id DESC LIMIT 1)
ORDER BY started_at;
```

A healthy nightly run has every enabled source with `status = 'success'` and a
`started_at` close to 02:00 local. Persistent `failed` rows for a single source
point to either a TCC issue (imessage), a token/auth refresh problem
(`owa-piggy` for teams/calendar), or a network timing race
(`github` DNS at boot). Treat the failure pattern as the diagnostic, not the
fact that it ran.
