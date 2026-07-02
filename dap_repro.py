#!/usr/bin/env python3
"""Headless DAP driver to reproduce the coroutine frame-filter 'level' error.

Spawns OpenDebugAD7, launches the fib binary with the GDB coro frame filter and
frame-filters enabled, sets a breakpoint inside fib(), then requests a
stackTrace. Prints all responses/events, and in particular any OutputEvent that
carries the 'Unrecognized format of field "level"' error.
"""
import json
import os
import subprocess
import sys
import threading
import time

AD7 = os.environ.get("AD7_OVERRIDE", "/home/tzcnt/github/MIEngine/localAD7/OpenDebugAD7")
WS = "/home/tzcnt/github/tmc-examples"
PROGRAM = WS + "/build/clang-linux-debug/fib"
GDB_SCRIPT = WS + "/coro_backtrace_gdb.py"
SRC = WS + "/examples/fib.cpp"
BP_LINE = 43

proc = subprocess.Popen(
    [AD7],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    bufsize=0,
)

_seq = 0
_lock = threading.Lock()


def send(msg):
    global _seq
    with _lock:
        _seq += 1
        msg["seq"] = _seq
    data = json.dumps(msg).encode("utf-8")
    header = f"Content-Length: {len(data)}\r\n\r\n".encode("utf-8")
    proc.stdin.write(header + data)
    proc.stdin.flush()


def request(command, arguments=None):
    send({"type": "request", "command": command, "arguments": arguments or {}})


events = []
responses = []
_done = threading.Event()


def reader():
    buf = b""
    while True:
        chunk = proc.stdout.read(1)
        if not chunk:
            break
        buf += chunk
        if buf.endswith(b"\r\n\r\n"):
            headers = buf.decode("utf-8")
            length = 0
            for line in headers.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":")[1].strip())
            body = b""
            while len(body) < length:
                body += proc.stdout.read(length - len(body))
            msg = json.loads(body.decode("utf-8"))
            handle(msg)
            buf = b""


def handle(msg):
    t = msg.get("type")
    if t == "event":
        events.append(msg)
        ev = msg.get("event")
        body = msg.get("body", {})
        if ev == "output":
            cat = body.get("category", "")
            out = body.get("output", "")
            print(f"[OUTPUT/{cat}] {out.rstrip()}")
        elif ev == "stopped":
            print(f"[STOPPED] reason={body.get('reason')} threadId={body.get('threadId')}")
            threading.Thread(target=on_stopped, args=(body.get("threadId"),)).start()
        elif ev == "initialized":
            print("[EVENT] initialized")
            _initialized.set()
        elif ev == "terminated" or ev == "exited":
            print(f"[EVENT] {ev}")
            _done.set()
        else:
            print(f"[EVENT] {ev}")
    elif t == "response":
        responses.append(msg)
        ok = msg.get("success")
        cmd = msg.get("command")
        print(f"[RESP] {cmd} success={ok}" + ("" if ok else f" message={msg.get('message')}"))
        if cmd == "stackTrace":
            if ok:
                frames = msg.get("body", {}).get("stackFrames", [])
                print(f"       -> {len(frames)} frames:")
                for f in frames:
                    print(f"          #{f.get('id')} {f.get('name')}  {f.get('source',{}).get('name')}:{f.get('line')}")
                # Probe scopes on the async synthetic frames (#1001 top_fib, #1002 main::$_0,
                # #1003 client_main_awaiter) and a real frame (#1000).
                for fid in (1001, 1002, 1003, 1000):
                    request("scopes", {"frameId": fid})
            else:
                _done.set()
        if cmd == "scopes" and ok:
            # Only expand the Locals scope (skip Registers) to keep output focused.
            for s in msg.get("body", {}).get("scopes", []):
                ref = s.get("variablesReference")
                if ref and s.get("name") == "Locals":
                    request("variables", {"variablesReference": ref})
        if cmd == "variables":
            vs = msg.get("body", {}).get("variables", []) if ok else []
            print("       Locals -> " +
                  ", ".join(f"{v.get('name')}={v.get('value')}" for v in vs[:8]))
            _probes_seen[0] += 1
            if _probes_seen[0] >= 4:  # Locals for the 4 probed frames
                _done.set()


_initialized = threading.Event()
_probes_seen = [0]
_probes_expected = [0]


def on_stopped(tid):
    time.sleep(0.2)
    request("stackTrace", {"threadId": tid, "startFrame": 0, "levels": 100})


threading.Thread(target=reader, daemon=True).start()

# 1. initialize
request("initialize", {
    "clientID": "repro", "adapterID": "cppdbg", "linesStartAt1": True,
    "columnsStartAt1": True, "pathFormat": "path",
    "supportsRunInTerminalRequest": False,
})
time.sleep(0.3)

# 2. launch (mirrors launch.json cppdbg config)
request("launch", {
    "type": "cppdbg", "request": "launch", "name": "repro",
    "program": PROGRAM, "args": [], "stopAtEntry": False, "cwd": WS,
    "MIMode": "gdb",
    "miDebuggerArgs": f"-x {GDB_SCRIPT}",
    "syntheticFrameLocalsExpression": "*$__coro_frame_at({address})",
    "syntheticFrameLocalNamesExpression": "$__coro_local_names({address})",
    "setupCommands": [
        {"text": "-enable-pretty-printing", "ignoreFailures": True},
        {"text": "-enable-frame-filters", "ignoreFailures": True},
    ],
    "logging": {"engineLogging": True, "trace": True, "traceResponse": True},
})

# 3. after 'initialized', set breakpoints + configurationDone
if not _initialized.wait(timeout=20):
    print("!! never got initialized event")
    proc.kill(); sys.exit(1)

request("setBreakpoints", {
    "source": {"path": SRC, "name": "fib.cpp"},
    "breakpoints": [{"line": BP_LINE}],
})
time.sleep(0.5)
request("configurationDone", {})

# 4. wait for the stackTrace round-trip (or terminate)
if not _done.wait(timeout=40):
    print("!! timed out waiting for stackTrace/terminate")

time.sleep(0.5)
# dump any stderr
try:
    err = proc.stderr.read1(65536).decode("utf-8", "replace") if proc.stderr else ""
    if err.strip():
        print("=== ADAPTER STDERR ===")
        print(err)
except Exception:
    pass
request("disconnect", {"terminateDebuggee": True})
time.sleep(0.5)
proc.kill()
