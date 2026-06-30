"""
Mission Control — orchestrator router (additive, read-only).

Surfaces the Hermes self-knowledge manifest + gap audit in the dashboard. It
shells into the orchestrator package (~/.hermes/orchestrator) which already
assembles everything fail-safe; this module just exposes it over HTTP. Never
writes, never mutates the system — degrades to an error JSON if the package or
its files are missing.

Routes:
  GET /api/orchestrator/manifest  -> the full self-knowledge dict (cached file)
  GET /api/orchestrator/gaps      -> [{severity, area, issue, fix}]
  GET /api/orchestrator/status    -> compact counts + tier health + gap tally
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()

ORCH_DIR = Path.home() / ".hermes" / "orchestrator"
MANIFEST_JSON = ORCH_DIR / "manifest.json"


def _import_orch():
    """Make the orchestrator package importable (+ its proactive sibling)."""
    proactive = Path.home() / ".hermes" / "proactive"
    for p in (str(ORCH_DIR), str(proactive)):
        if p not in sys.path:
            sys.path.insert(0, p)


def _load_manifest(refresh: bool = False) -> dict:
    """Prefer the cached manifest.json (fast); rebuild only on demand."""
    if not refresh and MANIFEST_JSON.is_file():
        try:
            return json.loads(MANIFEST_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass
    try:
        _import_orch()
        import manifest  # type: ignore
        return manifest.build(refresh_probe=False)
    except Exception as e:
        return {"error": f"manifest unavailable: {e}"}


@router.get("/api/orchestrator/manifest")
def get_manifest(refresh: bool = False):
    return JSONResponse(_load_manifest(refresh=refresh))


@router.get("/api/orchestrator/gaps")
def get_gaps():
    try:
        _import_orch()
        import gaps as gaps_mod  # type: ignore
        return JSONResponse({"gaps": gaps_mod.audit()})
    except Exception as e:
        return JSONResponse({"gaps": [], "error": str(e)})


@router.get("/api/orchestrator/status")
def get_status():
    m = _load_manifest()
    if "error" in m:
        return JSONResponse(m)
    try:
        _import_orch()
        import gaps as gaps_mod  # type: ignore
        g = gaps_mod.audit(m)
    except Exception:
        g = []
    sev: dict[str, int] = {}
    for it in g:
        sev[it["severity"]] = sev.get(it["severity"], 0) + 1
    mdl = m.get("model") or {}
    probe = (mdl.get("probe") or {}).get("tiers", {})
    return JSONResponse({
        "generated_at": m.get("generated_at"),
        "config_version": m.get("config_version"),
        "days_to_iat": m.get("days_to_iat"),
        "counts": m.get("counts", {}),
        "model_default": mdl.get("default"),
        "strong_enabled": (mdl.get("policy") or {}).get("strong_tier_enabled"),
        "tiers": {k: bool(v.get("ok")) for k, v in probe.items()},
        "gap_total": len(g),
        "gap_by_severity": sev,
    })
