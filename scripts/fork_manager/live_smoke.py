import argparse
import atexit
import fcntl
import json
import os
import pathlib
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import time

import pyte

parser = argparse.ArgumentParser()
parser.add_argument("binary")
parser.add_argument("--model", required=True)
parser.add_argument("--output", type=pathlib.Path, required=True)
args = parser.parse_args()
binary = str(pathlib.Path(args.binary).resolve())
server_binary = binary
args.output.mkdir(parents=True, exist_ok=True)
root = pathlib.Path(tempfile.mkdtemp(prefix="hb-smoke-", dir="/tmp"))
home = root / "home"
home.mkdir(mode=0o700)
atexit.register(lambda: (home / "auth.json").unlink(missing_ok=True))
source_home = pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex"))
if not (source_home / "auth.json").is_file():
    raise RuntimeError(
        "Live test requires a file-backed auth.json in CODEX_HOME; no login was changed"
    )
work = root / "work"
work.mkdir()
for name in ("auth.json", "models_cache.json"):
    source = (
        pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex"))
        / name
    )
    if source.exists():
        shutil.copyfile(source, home / name)
        (home / name).chmod(0o600)
(home / "config.toml").write_text(f'''model = {json.dumps(args.model)}
model_reasoning_effort = "low"
cli_auth_credentials_store = "file"
approval_policy = "never"
[projects."{work}"]
trust_level = "trusted"
''')
env = {**os.environ, "CODEX_HOME": str(home), "TERM": "xterm-256color"}
for name in ("HERDR_PANE_ID", "HERDR_ENV", "CODEX_THREAD_ID", "NO_COLOR"):
    env.pop(name, None)
print(json.dumps({"smoke_root": str(root)}), flush=True)
serverlog = (root / "server.log").open("wb")
server = subprocess.Popen(
    [server_binary, "app-server", "--listen", "unix://"],
    env=env,
    cwd=work,
    stdin=subprocess.DEVNULL,
    stdout=serverlog,
    stderr=subprocess.STDOUT,
)
pid = None
master = None
log = (root / "terminal.log").open("ab")


def start(args):
    global pid, master
    pid, master = pty.fork()
    if pid == 0:
        os.chdir(work)
        os.execve(binary, [binary, "--no-alt-screen", *args], env)
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))


def stop():
    global pid, master
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5
        while True:
            try:
                finished, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if finished:
                break
            if time.monotonic() > deadline:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                break
            time.sleep(0.05)
        os.close(master)
        pid = None
        master = None


def events():
    result = []
    for p in home.glob("sessions/**/*.jsonl"):
        for line in p.read_text().splitlines():
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return result


try:
    deadline = time.monotonic() + 30
    while not (home / "app-server-control/app-server-control.sock").exists():
        if server.poll() is not None:
            raise RuntimeError("isolated server exited")
        if time.monotonic() > deadline:
            raise RuntimeError("isolated server startup timed out")
        time.sleep(0.1)
    prompt = "This is a heartbeat smoke test. Use the available codex_tui automation_update tool to create a one-shot heartbeat in this thread: mode=create, kind=heartbeat, destination=thread, name=Heartbeat smoke, rrule=FREQ=MINUTELY;INTERVAL=1, repeat=false, prompt=Reply with exactly HEARTBEAT_LIVE_OK and do not call any tools. Discover the tool if necessary. Do not simulate the schedule using sleep or shell commands. After creation reply exactly SCHEDULE_CREATED_OK and end your turn. Do not create any other schedules or modify files."
    start([prompt])
    deadline = time.monotonic() + 300
    report = 0
    restarted = False
    success = False
    restart_offset = 0
    initial_footer = False
    resumed_footer = False
    while time.monotonic() < deadline:
        if select.select([master], [], [], 0.2)[0]:
            try:
                data = os.read(master, 65536)
            except OSError:
                data = b""
            if not data:
                raise RuntimeError("TUI exited before verification")
            log.write(data)
            log.flush()
            if b"\x1b[6n" in data:
                os.write(master, b"\x1b[1;1R")
            if b"\x1b[c" in data:
                os.write(master, b"\x1b[?1;2c")
            if b"\x1b[?u" in data:
                os.write(master, b"\x1b[?0u")
        if time.monotonic() < report:
            continue
        report = time.monotonic() + 5
        screen = pyte.Screen(140, 40)
        pyte.Stream(screen).feed(
            (root / "terminal.log").read_bytes().decode(errors="replace")
        )
        visible = False
        for y, line in enumerate(screen.display):
            if "● Heartbeat · one-shot" in line:
                x = line.index("●")
                visible = (
                    screen.buffer[y][x].fg == "00ffff" and screen.buffer[y][x].bold
                )
        if visible:
            if restarted:
                resumed_footer = True
                (root / "resumed-screen.txt").write_text("\n".join(screen.display))
            else:
                initial_footer = True
                (root / "initial-screen.txt").write_text("\n".join(screen.display))
        ev = events()
        completed = [
            e["payload"]
            for e in ev
            if e.get("type") == "event_msg"
            and e["payload"].get("type") == "task_complete"
        ]
        schedules = list(home.glob("cli-heartbeats/*.json"))
        print(
            json.dumps(
                {
                    "completed_turns": len(completed),
                    "schedules": len(schedules),
                    "restarted": restarted,
                    "last": completed[-1].get("last_agent_message")
                    if completed
                    else None,
                }
            ),
            flush=True,
        )
        if any(e.get("error") for e in completed):
            raise RuntimeError(
                json.dumps([e["error"] for e in completed if e.get("error")])
            )
        if schedules and completed and not restarted and initial_footer:
            schedule = json.loads(schedules[0].read_text())
            assert schedule["repeat"] is False and schedule["status"] == "ACTIVE", (
                schedule
            )
            thread = schedule["threadId"]
            print(json.dumps({"verified_schedule": schedule}), flush=True)
            restart_offset = log.tell()
            stop()
            start(["resume", thread])
            restarted = True
        if (
            restarted
            and any(
                e.get("last_agent_message", "") == "HEARTBEAT_LIVE_OK"
                for e in completed
            )
            and not schedules
        ):
            success = True
            break
    if not success:
        raise RuntimeError("heartbeat smoke test timed out")
    raw = (root / "terminal.log").read_bytes()

    def plain(data):
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", data.decode(errors="replace"))

    assert initial_footer and resumed_footer, (
        "missing cyan heartbeat footer before or after resume"
    )
    screen = pyte.Screen(140, 40)
    stream = pyte.Stream(screen)
    stream.feed(raw.decode(errors="replace"))
    (root / "final-screen.txt").write_text("\n".join(screen.display))
    assert "Heartbeat · one-shot" not in "\n".join(screen.display[-6:]), (
        "deleted heartbeat still in footer"
    )
    result = {
        "tui_binary": binary,
        "server_binary": server_binary,
        "footer_initial_and_resumed": True,
        "footer_bright_cyan": True,
        "footer_removed": True,
        "success": True,
        "thread": thread,
        "root": str(root),
        "tui_restarted": restarted,
        "one_shot_removed": True,
        "completed_turns": completed,
    }
    (root / "result.json").write_text(json.dumps(result, indent=2))
    for name in (
        "result.json",
        "initial-screen.txt",
        "resumed-screen.txt",
        "final-screen.txt",
    ):
        shutil.copyfile(root / name, args.output / name)
    print(json.dumps(result), flush=True)
finally:
    stop()
    server.terminate()
    try:
        server.wait(timeout=15)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()
    (home / "auth.json").unlink(missing_ok=True)
    log.close()
    serverlog.close()
    for name in ("server.log", "terminal.log"):
        shutil.copyfile(root / name, args.output / name)
