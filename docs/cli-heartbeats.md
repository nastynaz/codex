# CLI heartbeats

The local daemon-backed TUI can schedule a recurring prompt in its current thread.
The timer runs inside the TUI process. It adds no process, service or OS job.
Closing all local TUIs stops dispatch; reopening a compatible TUI resumes saved
active schedules. The machine must be awake. `--no-daemon`, remote connections,
untrusted projects and managed configurations disabling local automation do not
advertise the tool.

Ask for a loop in ordinary language, or use the existing `$loop` skill. For example:
“Check this task every five minutes until the build finishes, then stop the loop.”
The agent uses `codex_tui.automation_update`; the calling thread is supplied by the
host. There is one schedule per thread. Supported intervals range from one minute
to 24 hours, expressed as minute/hour recurrence rules. No calendar/timezone rules
are accepted. Creation waits one complete interval before the first activation. Set `repeat=false`
for a one-shot; the default is `repeat=true`.

## Operation and recovery

- `create` requires `kind=heartbeat`, `destination=thread`, `name`, `prompt` and
  `rrule`. `status` defaults to `ACTIVE`; `repeat` defaults to `true`, including
  for existing saved schedules. Repeating an identical creation is a no-op;
  changing an existing schedule requires `update`.
- `view` returns the complete saved schedule, next due time, last accepted turn ID,
  last dispatch time and any failure. Times are Unix seconds in UTC.
- `update` preserves omitted fields. Set `status=PAUSED` to pause or `ACTIVE` to
  resume. `repeat` can change a schedule between repeating and one-shot. An update
  schedules the next activation one interval from now.
- `delete` removes this thread's schedule. It is safe to repeat without an ID and
  can also recover an invalid schedule file.
- On completion, the agent should delete the schedule and rename the thread
  `done: <name>`. The scheduler also removes a due repeating schedule if that title
  is found. One-shots still deliver in a thread with an old `done:` title.

When a regular turn is active, a heartbeat joins its pending-input queue. The
agent sees it at its next processing opportunity, including after an in-flight
model response or tool call. The message says the heartbeat activated and includes
its name, activation time, interval, repeat setting and saved instructions. Every
live recurring tick submits a message; it does not wait for the previous turn to
finish. The active turn keeps its original trigger and permissions.

When idle, a heartbeat starts a new turn with the target thread's model,
permissions, environments and instructions. Plan mode, review, compaction or a
concurrent idle reservation defer delivery for 30 seconds. Missed recurring ticks
while clients are closed do not form a backlog. An overdue one-shot stays due.
A one-shot deletes its schedule only after the server accepts its message, whether
into an active turn or a new one. Acceptance does not guarantee task completion or
recovery of that input after an immediate server crash.

Scheduled input uses Codex's existing heartbeat history format and does not count
as a fresh human grant of authority. Existing tool and command approval rules
remain in force, including pauses for approval or user input.

Schedules are private JSON files under `$CODEX_HOME/cli-heartbeats`, independent
of the main Codex database. All compatible local clients using that home share
those schedules. Per-thread OS locks prevent duplicate dispatch by concurrent
clients. A schedule is marked paused on disk before submission and reactivated
only after a confirmed result. A crash, timeout, unsupported server or uncertain
reply therefore leaves it paused. A retained TUI warning identifies the thread;
inspect its history and schedule before asking the agent to resume. No external
alert service is installed. A failed model turn after accepted dispatch is subject
to the normal thread error/approval handling; `lastRunAt` records acceptance, not
successful completion of the task.

## Building and using the fork

Both the TUI and the app-server must come from this fork: the scheduler requires
`turn/heartbeat`, which older servers reject safely. This feature is in the
terminal client; it does not add a desktop-app interface.

From the repository root, assemble a complete package in a new directory:

```sh
just assemble-codex-package --package-dir /absolute/path/to/heartbeat-package
```

The upstream package builder builds the CLI and its existing companion binaries.
Building alone does not replace or restart the installed daemon. When ready to
switch, between active tasks, use the new package's CLI to pin that package:

```sh
/absolute/path/to/heartbeat-package/bin/codex app-server daemon update --from-cli
/absolute/path/to/heartbeat-package/bin/codex
```

The update asks for confirmation and can interrupt running daemon tasks. Keep
using the fork's CLI while using heartbeats; an upstream CLI does not own this
timer. Returning to an upstream daemon leaves saved schedule files intact but
cannot dispatch them. To return to an upstream package, use that package's CLI
with the same `app-server daemon update --from-cli` command.

## Maintaining the fork

The source extension points are:

- `tui/src/heartbeat/`: tool contract, file storage, scheduling and dispatch.
- TUI tool bridge/startup: register the tool and own the timer's lifetime.
- App-server `turn/heartbeat`: narrow scheduled input, with an optional accepted turn.
- Core heartbeat delivery: queue onto an active regular turn or reserve an idle turn.
  The earlier `turn/startIfIdle` endpoint remains available for compatible callers.
- Core heartbeat admission: scheduled input obeys automatic Plan-mode rules.
- Loaded-thread resume: replace an existing local client MCP endpoint while
  retaining its tool policy and session layer, including across MCP reloads.

Keep these as a small feature branch rebased onto upstream `main`. No upstream
schema tables or third-party dependencies are changed. Regenerate the stable and
experimental protocol fixtures and Python SDK after changes to the request type.
The MCP bridge, thread-resume path and core admission path are the most likely
places to need adaptation.

After rebasing, run these focused checks from the repository root, then assemble
a fresh package:

```sh
just test -p codex-tui -p codex-core --lib \
  -E 'test(heartbeat) | test(steer_preserves_request_origin) | test(client_mcp_rebinding) | test(local_daemon_registers_approval_gated)'
just test -p codex-app-server -p codex-core --test all \
  -E 'test(turn_heartbeat) | test(turn_start_if_idle) | test(automatic_plan_rejections) | test(legacy_permission_profile_requests_fail_closed)'
```

These cover persistence, concurrent clients, crash/timeout recovery, the MCP tool
contract, endpoint replacement across reloads, busy-turn message delivery, idle admission and
Plan-mode deferral. The app-server test uses a mock model; it does not establish
live model behaviour. After installation, exercise a disposable thread with a
one-minute schedule, check that a heartbeat appears in its history, then delete
the schedule. Repeat after closing and reopening the TUI to check the installed
package and daemon together.
