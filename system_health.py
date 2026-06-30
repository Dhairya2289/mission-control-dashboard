"""
Mission Control — system health router (additive module).

One screen to answer "is everything alive?". Aggregates, read-only and fast,
the live state of this single-user box:

  - services   : systemctl --user is-active / is-enabled for the known units
  - listeners  : a quick TCP connect to each service port
  - backup     : last restic snapshot time + total count
  - data       : size + a cheap row-count for the known SQLite DBs
  - host       : disk free on / and RAM used/total

Everything degrades gracefully: a missing unit, an unreachable port, a broken
restic repo, a 0-byte DB, or an empty table never raises out of an endpoint —
the field just reports the failure shape (null / "down" / "not-found" / size
only). The whole payload is cached for ~5s so a dashboard that polls doesn't
fork a dozen `systemctl`/`restic` processes every second.

Self-contained APIRouter, mounted by main.py with a single include line.
Routes:
  GET /api/system/health          -> full aggregate (see SHAPE below)
  GET /api/system/health/summary  -> {"ok": int, "warn": int, "down": int}

SHAPE of /api/system/health:
  {
    "ts": 1718.., "cached": bool,
    "services":  [{"unit","active","enabled"}...],
    "listeners": [{"name","host","port","status"}...],     # status: up|down
    "backup":    {"ok":bool,"count":int,"last":iso|None,"last_age_sec":int|None}
                  | {"ok":false,"error":"..."},
    "data":      [{"name","path","exists","size_bytes","table","rows"|null}...],
    "host":      {"disk":{...},"ram":{...}},
    "summary":   {"ok":int,"warn":int,"down":int}
  }
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import config

router = APIRouter()

# --------------------------------------------------------------------------- #
# Static topology — what we expect to be running on this box.
# --------------------------------------------------------------------------- #

HOME = os.path.expanduser("~")
HERMES = os.path.join(HOME, ".hermes")
DASHBOARD = os.path.join(HOME, "dashboard")

# systemd --user units to probe. kokoro-tts may be "not-found" on this box —
# that is handled the same as any missing unit (active="inactive",
# enabled="not-found"), never an error.
SERVICE_UNITS = [
    "mission-control",
    "whisper-stt",
    "kokoro-tts",
    "syncthing",
    "hermes-backup.timer",
    "hypr-autopomodoro",
    "vault-reindex",
    "hermes-gateway",
    "hermes-gateway-dhairya",
]

# TCP listeners to ping. The dashboard binds MC_HOST (loopback by default, a
# Tailscale IP for the author) — connect to that same address for its port.
TAILSCALE_IP = config.BIND_HOST
LISTENERS = [
    {"name": "dashboard", "host": TAILSCALE_IP, "port": config.BIND_PORT},
    {"name": "whisper", "host": "127.0.0.1", "port": 51764},
    {"name": "kokoro", "host": "127.0.0.1", "port": 51765},
    {"name": "ollama", "host": "127.0.0.1", "port": 11434},
    {"name": "syncthing", "host": "127.0.0.1", "port": 8384},
]
LISTENER_TIMEOUT = 0.4  # seconds per port

# Known SQLite DBs -> (label, path, "main" table for a cheap count).
# Most live read-only under ~/.hermes; memory_core + codegraph live under
# ~/dashboard. We open every DB with mode=ro so we can never mutate hermes
# state (and the write-guard is never tripped).
DB_TARGETS = [
    ("quiz", os.path.join(HERMES, "quiz.db"), "quiz_attempts"),
    ("flashcards", os.path.join(HERMES, "flashcards.db"), "flashcard_decks"),
    ("productivity", os.path.join(HERMES, "productivity.db"), "pomodoro"),
    ("tracker", os.path.join(HERMES, "tracker.db"), "tracker_days"),
    ("agent-logs", os.path.join(HERMES, "agent-logs.db"), "agent_logs"),
    ("memory_core", os.path.join(DASHBOARD, "memory_core.db"), "knowledge_notes"),
    ("codegraph", os.path.join(DASHBOARD, ".codegraph", "codegraph.db"), "nodes"),
]

# restic — point at the hermes backup repo via env, run the bundled binary.
RESTIC_BIN = os.path.join(HOME, "bin", "restic")
RESTIC_REPO = os.path.join(HOME, "backups", "restic")
RESTIC_PASS_FILE = os.path.join(HOME, ".config", "hermes-backup", "restic-pass")
RESTIC_TIMEOUT = 12  # seconds — snapshots is cheap, but never hang the page

# Cache the whole aggregate briefly so polling stays cheap.
CACHE_TTL = 5.0
_cache: dict[str, Any] = {"ts": 0.0, "payload": None}


# --------------------------------------------------------------------------- #
# Probes — each is total: it returns data or a degraded shape, never raises.
# --------------------------------------------------------------------------- #

def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
    """Run a command, return (returncode, stdout). Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return 124, ""
    except Exception:  # noqa: BLE001 — missing binary / OS error == unknown
        return 127, ""


def _systemctl_state(unit: str) -> dict[str, str]:
    """Return {unit, active, enabled, status, detail} for a --user unit.

    One `systemctl show` call yields ActiveState/SubState/Result/Type/
    LoadState/UnitFileState; from those we derive a clean, frontend-ready
    `status` so the dashboard never paints a bare "unknown":

      up       — active and running (or a oneshot mid-run)
      idle     — a oneshot / .timer unit that ran and exited cleanly (normal rest)
      starting — activating / reloading / deactivating
      down     — failed, or a long-running service that has stopped
      missing  — unit file not-found / masked

    `active`/`enabled` are still returned verbatim for the detail line.
    """
    rc, out = _run(
        ["systemctl", "--user", "show", unit,
         "-p", "LoadState", "-p", "ActiveState", "-p", "SubState",
         "-p", "Result", "-p", "Type", "-p", "UnitFileState"],
        3.0,
    )
    props: dict[str, str] = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        props[k.strip()] = v.strip()

    load = props.get("LoadState", "")
    active = props.get("ActiveState", "") or "unknown"
    sub = props.get("SubState", "")
    result = props.get("Result", "")
    utype = props.get("Type", "")
    enabled = props.get("UnitFileState", "") or "unknown"

    if load in ("not-found", "masked", "error") or (rc not in (0,) and active == "unknown"):
        status, detail = "missing", (load or "no unit")
    elif active == "active":
        status, detail = "up", (sub or "running")
    elif active in ("activating", "reloading", "deactivating"):
        status, detail = "starting", (sub or active)
    elif active == "failed" or (result not in ("", "success")):
        status, detail = "down", (result or sub or "failed")
    elif active == "inactive":
        # A clean stop: oneshot / timer units legitimately rest here — but ONLY
        # when they are actually wired in (enabled/static). A disabled, never-run
        # timer must NOT masquerade as a healthy "idle"; flag it as down.
        oneshot_like = utype == "oneshot" or unit.endswith(".timer")
        enabled_ok = enabled in ("enabled", "enabled-runtime", "static", "indirect", "generated", "alias")
        if oneshot_like and enabled_ok:
            status, detail = "idle", "ran · exited"
        elif oneshot_like:
            status, detail = "down", "disabled"
        else:
            status, detail = "down", "stopped"
    else:
        status, detail = active, sub

    return {
        "unit": unit,
        "active": active,
        "enabled": enabled,
        "status": status,
        "detail": detail,
    }


def _probe_services() -> list[dict[str, str]]:
    return [_systemctl_state(u) for u in SERVICE_UNITS]


def _probe_listener(host: str, port: int) -> str:
    """TCP connect with a short timeout. 'up' if the port accepts, else 'down'."""
    try:
        with socket.create_connection((host, port), timeout=LISTENER_TIMEOUT):
            return "up"
    except Exception:  # noqa: BLE001 — refused / filtered / timeout == down
        return "down"


def _probe_listeners() -> list[dict[str, Any]]:
    out = []
    for spec in LISTENERS:
        out.append(
            {
                "name": spec["name"],
                "host": spec["host"],
                "port": spec["port"],
                "status": _probe_listener(spec["host"], spec["port"]),
            }
        )
    return out


def _probe_backup() -> dict[str, Any]:
    """Last restic snapshot time + count. Degrade to {ok:false,...} on error."""
    if not os.path.exists(RESTIC_BIN):
        return {"ok": False, "error": "restic binary not found"}
    env = dict(os.environ)
    env["RESTIC_REPOSITORY"] = RESTIC_REPO
    env["RESTIC_PASSWORD_FILE"] = RESTIC_PASS_FILE
    try:
        proc = subprocess.run(
            [RESTIC_BIN, "snapshots", "--json"],
            capture_output=True,
            text=True,
            timeout=RESTIC_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "restic timed out"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"restic failed: {e.__class__.__name__}"}

    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        return {"ok": False, "error": (msg[-1] if msg else f"exit {proc.returncode}")[:160]}

    import json

    try:
        snaps = json.loads(proc.stdout or "[]")
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "unparseable restic output"}
    if not isinstance(snaps, list):
        return {"ok": False, "error": "unexpected restic shape"}

    count = len(snaps)
    last_iso: Optional[str] = None
    last_age: Optional[int] = None
    if count:
        # snapshots come oldest->newest; take the last "time".
        last_iso = str(snaps[-1].get("time") or "") or None
        if last_iso:
            age = _iso_age_seconds(last_iso)
            if age is not None:
                last_age = age
    return {"ok": True, "count": count, "last": last_iso, "last_age_sec": last_age}


def _iso_age_seconds(iso: str) -> Optional[int]:
    """Seconds since an ISO-8601 timestamp (restic uses RFC3339 w/ offset)."""
    from datetime import datetime, timezone

    s = iso.strip()
    # Python <3.11 fromisoformat chokes on nanoseconds & 'Z'; normalise.
    s = s.replace("Z", "+00:00")
    # trim fractional seconds to 6 digits (microseconds) if longer
    if "." in s:
        head, _, tail = s.partition(".")
        frac = ""
        rest = ""
        for i, ch in enumerate(tail):
            if ch.isdigit():
                frac += ch
            else:
                rest = tail[i:]
                break
        s = f"{head}.{frac[:6]}{rest}"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:  # noqa: BLE001
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))


def _probe_db(label: str, path: str, table: str) -> dict[str, Any]:
    """Size + a cheap row-count for one DB. Degrade to size-only on any error."""
    import sqlite3

    rec: dict[str, Any] = {
        "name": label,
        "path": path,
        "exists": False,
        "size_bytes": 0,
        "table": table,
        "rows": None,
    }
    try:
        st = os.stat(path)
        rec["exists"] = True
        rec["size_bytes"] = st.st_size
    except OSError:
        return rec  # missing file -> exists:false, size 0

    if rec["size_bytes"] == 0:
        return rec  # 0-byte placeholder (e.g. dashboard/memory_core.db) -> rows null

    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.5)
        cur = conn.execute(f'SELECT count(*) FROM "{table}"')
        row = cur.fetchone()
        rec["rows"] = int(row[0]) if row else 0
    except Exception:  # noqa: BLE001 — locked / no such table / corrupt -> size only
        rec["rows"] = None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return rec


def _probe_data() -> list[dict[str, Any]]:
    return [_probe_db(label, path, table) for (label, path, table) in DB_TARGETS]


def _probe_host() -> dict[str, Any]:
    """Disk free on / and RAM used/total. Never raises."""
    disk: dict[str, Any] = {"ok": False}
    try:
        total, used, free = shutil.disk_usage("/")
        disk = {
            "ok": True,
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "pct_used": round(used / total * 100, 1) if total else None,
        }
    except Exception:  # noqa: BLE001
        disk = {"ok": False}

    ram: dict[str, Any] = {"ok": False}
    try:
        meminfo: dict[str, int] = {}
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                kb = rest.strip().split()
                if kb and kb[0].isdigit():
                    meminfo[key.strip()] = int(kb[0]) * 1024  # kB -> bytes
        total = meminfo.get("MemTotal", 0)
        # "available" is the honest free figure (free + reclaimable cache).
        avail = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
        used = max(0, total - avail)
        ram = {
            "ok": True,
            "total_bytes": total,
            "used_bytes": used,
            "available_bytes": avail,
            "pct_used": round(used / total * 100, 1) if total else None,
        }
    except Exception:  # noqa: BLE001
        ram = {"ok": False}

    return {"disk": disk, "ram": ram}


# --------------------------------------------------------------------------- #
# Rollup — turn the probes into an ok / warn / down badge.
# --------------------------------------------------------------------------- #

# Units that are expected to exist on this box. Anything here being inactive
# counts as "down"; units NOT here (e.g. kokoro-tts, which is not-found) only
# warn, since they may simply not be installed.
EXPECTED_ACTIVE = {
    "mission-control",
    "whisper-stt",
    "syncthing",
    "hermes-gateway",
}


def _summarize(payload: dict[str, Any]) -> dict[str, int]:
    """Roll the aggregate into {ok, warn, down} counts for a status badge."""
    ok = warn = down = 0

    for svc in payload.get("services", []):
        status = svc.get("status")
        active = svc.get("active")
        unit = svc.get("unit", "")
        if status in ("up", "idle") or active == "active":
            ok += 1  # running, or a oneshot/timer resting after a clean run
        elif status == "starting":
            warn += 1
        elif unit in EXPECTED_ACTIVE:
            down += 1  # a unit we rely on is not running
        else:
            warn += 1  # optional/uninstalled unit (e.g. kokoro-tts not-found)

    for lis in payload.get("listeners", []):
        if lis.get("status") == "up":
            ok += 1
        else:
            # dashboard listener down would be alarming, but if you're reading
            # this you're served by it; treat all listener-down as 'down'.
            down += 1

    backup = payload.get("backup") or {}
    if backup.get("ok"):
        # stale backup (>36h) is a warning, fresh is ok.
        age = backup.get("last_age_sec")
        if age is not None and age > 36 * 3600:
            warn += 1
        else:
            ok += 1
    else:
        down += 1

    host = payload.get("host") or {}
    disk = host.get("disk") or {}
    ram = host.get("ram") or {}
    if disk.get("ok"):
        pct = disk.get("pct_used") or 0
        if pct >= 95:
            down += 1
        elif pct >= 85:
            warn += 1
        else:
            ok += 1
    if ram.get("ok"):
        pct = ram.get("pct_used") or 0
        if pct >= 95:
            down += 1
        elif pct >= 90:
            warn += 1
        else:
            ok += 1

    return {"ok": ok, "warn": warn, "down": down}


# --------------------------------------------------------------------------- #
# Aggregate + cache.
# --------------------------------------------------------------------------- #

def _build_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ts": time.time(),
        "services": _probe_services(),
        "listeners": _probe_listeners(),
        "backup": _probe_backup(),
        "data": _probe_data(),
        "host": _probe_host(),
    }
    payload["summary"] = _summarize(payload)
    return payload


def _get_payload() -> tuple[dict[str, Any], bool]:
    """Return (payload, cached?). Rebuilds at most every CACHE_TTL seconds."""
    now = time.time()
    cached_payload = _cache.get("payload")
    if cached_payload is not None and (now - _cache["ts"]) < CACHE_TTL:
        return cached_payload, True
    payload = _build_payload()
    _cache["payload"] = payload
    _cache["ts"] = now
    return payload, False


# --------------------------------------------------------------------------- #
# Routes.
# --------------------------------------------------------------------------- #

@router.get("/api/system/health")
async def system_health() -> JSONResponse:
    """Full 'is everything alive' aggregate (cached ~5s). Never 500s."""
    import asyncio

    try:
        payload, cached = await asyncio.to_thread(_get_payload)
        out = dict(payload)
        out["cached"] = cached
        return JSONResponse(out)
    except Exception as e:  # noqa: BLE001 — last-ditch: a health endpoint must answer
        return JSONResponse(
            {
                "ts": time.time(),
                "cached": False,
                "error": f"health probe failed: {e.__class__.__name__}",
                "summary": {"ok": 0, "warn": 0, "down": 1},
            },
            status_code=200,
        )


@router.get("/api/system/health/summary")
async def system_health_summary() -> JSONResponse:
    """Just the {ok, warn, down} rollup for a status badge."""
    import asyncio

    try:
        payload, _ = await asyncio.to_thread(_get_payload)
        return JSONResponse(payload.get("summary", {"ok": 0, "warn": 0, "down": 0}))
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": 0, "warn": 0, "down": 1}, status_code=200)
