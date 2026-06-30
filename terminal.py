"""
Mission Control — interactive terminal router (additive module).

Exposes a WebSocket at /ws/terminal that spawns a real PTY running the user's
login shell (prefer /bin/fish, fallback /bin/bash) and bridges raw bytes both
directions: keystrokes from the xterm.js frontend -> the pty master fd, and
program output from the pty -> the browser. A small JSON control channel handles
window-resize so curses/full-screen TUIs lay out correctly.

The box is Tailscale-only and single-user, so an interactive shell over the
existing dashboard app is acceptable; we don't open a new port. The child is
reaped and the master fd is closed on disconnect so we never leak ptys.

Self-contained APIRouter, mounted by main.py with a single include line.
Routes:
  WS  /ws/terminal   <- bidirectional shell I/O
                        text frames may be control JSON: {"type":"resize","cols":C,"rows":R}
                        binary frames are raw stdin bytes for the shell
"""
from __future__ import annotations

import asyncio
import json
import os
import pty
import shutil
import signal
import struct
import fcntl
import termios
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


def _origin_allowed(websocket: WebSocket) -> bool:
    """Same-origin guard against cross-site WebSocket hijacking (CSWSH).

    This socket spawns a real login shell, so it is the single most dangerous
    endpoint in the app. A legitimate xterm.js client is our own frontend page,
    which the browser always stamps with an Origin header equal to the Host it
    was reached on. We accept only that case:

      * cross-site page (attacker)  -> Origin is its own site, != Host -> refuse
      * origin-less client (curl/wscat, non-browser) -> no Origin       -> refuse

    Refusing origin-less handshakes is intentional: the browser frontend is the
    only thing that should ever open this socket, and browsers cannot suppress
    the Origin header on a WebSocket. This blocks the CSWSH remote-shell vector
    without affecting the user's own same-origin terminal tab.
    """
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if not origin or not host:
        return False
    return urlparse(origin).netloc == host

# How much we try to read off the pty master in one go. xterm.js handles any
# chunk size; this just bounds a single read() syscall.
READ_CHUNK = 65536


def _pick_shell() -> list[str]:
    """Prefer fish, fall back to bash, then /bin/sh. Returns argv for the shell.

    We launch it as a login-ish interactive shell so the user's normal prompt,
    aliases and PATH are present (the dashboard env is initialized from the
    user's profile already, but -i keeps job control / prompt behaviour sane).
    """
    for candidate in ("/bin/fish", "/usr/bin/fish", "/bin/bash", "/usr/bin/bash"):
        if os.path.exists(candidate):
            return [candidate, "-i"]
    # Last resort: whatever sh resolves to.
    sh = shutil.which("sh") or "/bin/sh"
    return [sh, "-i"]


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    """Apply a terminal window size to the pty via TIOCSWINSZ ioctl.

    struct winsize is { ws_row, ws_col, ws_xpixel, ws_ypixel } (4x unsigned
    short). Pixel fields are left 0 — xterm.js works purely in cell units.
    """
    rows = max(1, min(int(rows), 5000))
    cols = max(1, min(int(cols), 5000))
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _spawn_pty() -> tuple[int, int]:
    """Fork a child running the shell attached to a new pty.

    Returns (pid, master_fd). The child execs the shell; the parent keeps the
    master fd to read/write the shell's terminal.
    """
    argv = _pick_shell()
    pid, master_fd = pty.fork()
    if pid == 0:
        # ---- child ----
        # pty.fork() has already made the slave our controlling terminal and
        # wired stdin/stdout/stderr to it. Just fix up the environment and exec.
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        # Drop anything that would make the shell think it's non-interactive.
        env.pop("PROMPT_COMMAND", None)
        try:
            os.execvpe(argv[0], argv, env)
        except Exception:
            # If exec fails there's nothing sensible to do in the child but die.
            os._exit(127)
    # ---- parent ----
    # Non-blocking master so our asyncio reader loop never wedges.
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    return pid, master_fd


def _reap(pid: int, master_fd: int) -> None:
    """Best-effort teardown: kill the shell process group and close the fd.

    Idempotent — safe to call from multiple finally paths. We SIGHUP then
    SIGKILL the child, waitpid to avoid a zombie, and close the master fd.
    """
    try:
        os.kill(pid, signal.SIGHUP)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.waitpid(pid, 0)
    except (ChildProcessError, OSError):
        pass
    try:
        os.close(master_fd)
    except OSError:
        pass


@router.websocket("/ws/terminal")
async def terminal_ws(websocket: WebSocket) -> None:
    """Bridge a browser xterm.js session to a real PTY-backed shell.

    Protocol from the client:
      * binary frame  -> raw bytes written straight to the shell's stdin
      * text frame    -> if it parses as {"type":"resize","cols":C,"rows":R}
                         we resize the pty; if {"type":"input","data":"..."}
                         we treat data as stdin; any other text is sent as-is.
    Server -> client: binary frames carrying raw shell output.
    """
    # Refuse cross-site / origin-less handshakes BEFORE spawning a shell.
    if not _origin_allowed(websocket):
        await websocket.close(code=1008)  # 1008 = policy violation
        return
    await websocket.accept()

    loop = asyncio.get_running_loop()
    pid, master_fd = _spawn_pty()

    # An asyncio.Event the reader uses to signal the shell exited / fd closed,
    # so the writer side and the outer handler can tear down promptly.
    closed = asyncio.Event()

    async def pty_to_ws() -> None:
        """Pump shell output -> browser using the event loop's fd readiness.

        We register the master fd with the loop; when it's readable we drain it
        and forward bytes. EOF (empty read) or EIO means the shell exited.
        """
        data_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def _on_readable() -> None:
            try:
                chunk = os.read(master_fd, READ_CHUNK)
            except BlockingIOError:
                return
            except OSError:
                # EIO on a master fd == slave/shell went away.
                data_queue.put_nowait(None)
                return
            if not chunk:
                data_queue.put_nowait(None)
            else:
                data_queue.put_nowait(chunk)

        try:
            loop.add_reader(master_fd, _on_readable)
        except (ValueError, OSError):
            closed.set()
            return

        try:
            while True:
                chunk = await data_queue.get()
                if chunk is None:
                    break
                await websocket.send_bytes(chunk)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            try:
                loop.remove_reader(master_fd)
            except (ValueError, OSError):
                pass
            closed.set()

    async def ws_to_pty() -> None:
        """Pump browser input -> shell stdin, and handle resize control frames."""
        try:
            while not closed.is_set():
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break

                # Binary frame: raw stdin bytes.
                raw = message.get("bytes")
                if raw is not None:
                    try:
                        os.write(master_fd, raw)
                    except OSError:
                        break
                    continue

                # Text frame: control JSON or plain stdin text.
                text = message.get("text")
                if text is None:
                    continue
                try:
                    payload = json.loads(text)
                except (ValueError, TypeError):
                    # Not JSON -> treat as literal keystrokes.
                    try:
                        os.write(master_fd, text.encode("utf-8", "replace"))
                    except OSError:
                        break
                    continue

                if isinstance(payload, dict):
                    mtype = payload.get("type")
                    if mtype == "resize":
                        try:
                            _set_winsize(
                                master_fd,
                                int(payload.get("cols", 80)),
                                int(payload.get("rows", 24)),
                            )
                        except (OSError, ValueError, TypeError):
                            pass
                        continue
                    if mtype == "input":
                        data = payload.get("data", "")
                        try:
                            os.write(master_fd, str(data).encode("utf-8", "replace"))
                        except OSError:
                            break
                        continue
                    # Unknown control type -> ignore quietly.
                    continue

                # JSON that isn't a control object -> send the raw text through.
                try:
                    os.write(master_fd, text.encode("utf-8", "replace"))
                except OSError:
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            closed.set()

    reader_task = asyncio.create_task(pty_to_ws())
    writer_task = asyncio.create_task(ws_to_pty())

    try:
        # Finish as soon as either direction ends (shell exit or socket close).
        await asyncio.wait(
            {reader_task, writer_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        closed.set()
        for task in (reader_task, writer_task):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        _reap(pid, master_fd)
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass
