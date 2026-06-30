#!/usr/bin/env python3
"""Self-healing asserter for a "config-not-code" LLM default on Hermes.

WHY THIS EXISTS
---------------
The kimchi / Cast AI gateway's WAF only serves requests whose ``User-Agent`` is
the official kimchi CLI (``kimchi/0.1.13``). Any other UA (the OpenAI Python
SDK's ``OpenAI/Python ...``, curl, urllib) gets a *fake* ``402 "exhausted
credits"`` soft-block — even when the key has credit. The real fix is simply to
send that UA.

An earlier fix patched this UA into the hermes-agent **code** (on a
``local-customizations`` git branch). ``hermes update`` checks out ``main`` and
reverted it, silently breaking the provider everywhere except the official CLI.

The durable fix is **config, not code**: ``model.default_headers`` in
``config.yaml``, which lives in the ``~/.hermes`` data dir — OUTSIDE the
hermes-agent git clone — so ``hermes update`` cannot revert it. This script
re-asserts that config. It is idempotent and non-destructive:

  * a config that is already correct is NOT rewritten (comments preserved);
  * only a config that has drifted (e.g. after a fresh ``hermes setup``
    regenerated it) is rewritten — at which point comments were already lost.

    heal_kimchi.py            # assert + heal
    heal_kimchi.py --check    # report only, change nothing

This is a sanitized excerpt of a personal Hermes customization, published as a
showcase of the "config over code so it survives upstream updates" pattern.
Tune UA / endpoint / model names to your own provider.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except Exception as exc:  # pragma: no cover
    print(f"heal_kimchi: PyYAML unavailable ({exc})", file=sys.stderr)
    raise SystemExit(0)

HERMES = Path.home() / ".hermes"
UA = "kimchi/0.1.13"
KIMCHI_API = "https://llm.kimchi.dev/openai/v1"
KIMCHI_MODEL = "kimi-k2.7"
KIMCHI_MODELS = ["kimi-k2.7", "kimi-k2.6", "minimax-m2.7", "minimax-m3",
                 "nemotron-3-ultra-fp4", "glm-5.2-fp8", "deepseek-v4-flash"]

# Each config gets the WAF User-Agent. `True` = also make kimchi the DEFAULT
# provider/model in that config; `False` = UA only, keep its own default.
# Adjust the profile paths/names to match your install.
TARGETS = {
    HERMES / "config.yaml": True,
    HERMES / "profiles" / "main" / "config.yaml": True,
    HERMES / "profiles" / "dev" / "config.yaml": False,
}


def _ensure(cfg: dict, full_default: bool) -> list[str]:
    """Mutate cfg in place to the desired state. Return list of changes."""
    changes: list[str] = []
    model = cfg.setdefault("model", {})

    # 1) The mandatory WAF User-Agent (every target, even UA-only ones).
    dh = model.get("default_headers")
    if not isinstance(dh, dict):
        dh = {}
        model["default_headers"] = dh
    if dh.get("User-Agent") != UA:
        dh["User-Agent"] = UA
        changes.append("set model.default_headers.User-Agent")

    if full_default:
        # 2) Make kimchi the default provider + model.
        if model.get("provider") != "custom:kimchi":
            model["provider"] = "custom:kimchi"
            changes.append("set model.provider=custom:kimchi")
        if model.get("default") != KIMCHI_MODEL:
            model["default"] = KIMCHI_MODEL
            changes.append(f"set model.default={KIMCHI_MODEL}")
        if model.get("base_url") != KIMCHI_API:
            model["base_url"] = KIMCHI_API
            changes.append("set model.base_url=kimchi")

        # 3) Keep the kimchi provider entry's default_model live (not a retired one).
        prov = cfg.get("providers")
        if isinstance(prov, dict) and isinstance(prov.get("custom:kimchi"), dict):
            k = prov["custom:kimchi"]
            if k.get("default_model") != KIMCHI_MODEL:
                k["default_model"] = KIMCHI_MODEL
                changes.append("set providers.custom:kimchi.default_model")
            if k.get("models") != KIMCHI_MODELS:
                k["models"] = list(KIMCHI_MODELS)
                changes.append("refresh providers.custom:kimchi.models")

        # 4) Put kimchi first in the fallback chain.
        fb = cfg.get("fallback_providers")
        if isinstance(fb, list) and fb and fb[0] != "custom:kimchi":
            fb = ["custom:kimchi"] + [p for p in fb if p != "custom:kimchi"]
            cfg["fallback_providers"] = fb
            changes.append("reorder fallback_providers (kimchi first)")
    return changes


def main() -> int:
    check = "--check" in sys.argv
    any_change = False
    for path, full_default in TARGETS.items():
        if not path.is_file():
            print(f"  - {path}: MISSING (skipped)")
            continue
        try:
            cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            print(f"  - {path}: parse error ({exc}) - skipped", file=sys.stderr)
            continue
        changes = _ensure(cfg, full_default)
        rel = path.relative_to(HERMES)
        if not changes:
            print(f"  [ok] {rel}: kimchi config OK")
            continue
        any_change = True
        if check:
            print(f"  [drift] {rel}: would heal -> {', '.join(changes)}")
        else:
            tmp = path.with_suffix(".kimchiheal.tmp")
            tmp.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
                           encoding="utf-8")
            tmp.replace(path)
            print(f"  [healed] {rel}: {', '.join(changes)}")
    if check and any_change:
        return 1
    print("heal_kimchi: done" + ("" if any_change else " (all configs already correct)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
