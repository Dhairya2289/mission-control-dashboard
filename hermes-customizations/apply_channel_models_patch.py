#!/usr/bin/env python3
"""Idempotently (re)apply a per-channel-model code patch to gateway/run.py.

WHY
---
Hermes has no native per-channel model: one gateway/bot serves every Discord
channel with a single configured model. This adds a tiny resolver
(``GatewayRunner._resolve_channel_default_model``) plus one application hook in
``_resolve_session_agent_runtime`` so a ``discord.channel_models`` map in
config.yaml gives each channel its own default model (e.g. Planner/Dev ->
kimi-k2.7, Scholar/Quizmaster -> minimax-m3, utility bots -> glm-5.2-fp8).

config.yaml lives in the ``~/.hermes`` data dir (survives ``hermes update``),
but this CODE lives in the hermes-agent git clone, which ``hermes update`` resets
to upstream ``main``. This script re-inserts the patch after an update. It is:

  * idempotent  — a no-op if the resolver is already present;
  * anchor-based — inserts at stable in-method anchors, tolerant of line drift;
  * fail-safe   — verifies the result COMPILES; on any failure it restores the
    original file untouched and prints a MANUAL-REAPPLY warning, so it can never
    leave the gateway with a broken run.py.

Invoked by restore_after_update.sh (before the gateway restart). Run by hand:
    python3 apply_channel_models_patch.py [run.py-path]
(an explicit path is only for testing against a scratch copy).

Sanitized excerpt of a personal Hermes customization, published to show a
safe pattern for carrying a small code patch across upstream resets: stable
anchors + compile-verify + automatic rollback, never a brittle full-file
overwrite.
"""
from __future__ import annotations

import py_compile
import sys
from pathlib import Path

RUN_PY = Path.home() / ".hermes" / "hermes-agent" / "gateway" / "run.py"
MARKER = "_resolve_channel_default_model"

# Anchor 1: the resolver method definition — insert the helper just before it.
HELPER_ANCHOR = "    def _resolve_session_agent_runtime(\n"
HELPER_CODE = '''    def _resolve_channel_default_model(
        self,
        source: Optional[SessionSource] = None,
        user_config: Optional[dict] = None,
    ) -> Optional[str]:
        """[local-customizations] Per-Discord-channel default model.

        Reads an optional ``discord.channel_models`` map
        ({channel_or_thread_id: model_name}) and returns the model bound to
        ``source``'s channel (thread -> channel -> parent fallback, mirroring
        ``resolve_channel_prompt``). None when unset, so behaviour is identical
        to upstream unless configured. The caller applies this only when there is
        no explicit ``/model`` session override, so a manual /model still wins.
        Provider/credentials are unchanged (targets share the gateway provider,
        e.g. custom:kimchi) -- only the model NAME differs. Re-applied after
        ``hermes update`` by hermes-customizations/apply_channel_models_patch.py.
        """
        try:
            if source is None or getattr(source, "platform", None) != Platform.DISCORD:
                return None
            disc = None
            if isinstance(user_config, dict):
                d = user_config.get("discord")
                if isinstance(d, dict) and isinstance(d.get("channel_models"), dict):
                    disc = d
            if disc is None:
                disc = (_load_gateway_config() or {}).get("discord") or {}
            cmap = disc.get("channel_models") if isinstance(disc, dict) else None
            if not isinstance(cmap, dict) or not cmap:
                return None
            for key in (getattr(source, "thread_id", None),
                        getattr(source, "chat_id", None),
                        getattr(source, "parent_chat_id", None)):
                if key and str(key) in cmap:
                    val = cmap.get(str(key))
                    if isinstance(val, str) and val.strip():
                        return val.strip()
        except Exception:
            logger.debug("channel_models resolution failed", exc_info=True)
        return None

'''

# Anchor 2: the /model session-override application block — insert our per-channel
# default model right AFTER it (so explicit /model overrides still win).
APP_ANCHOR = (
    "        if override and resolved_session_key:\n"
    "            model, runtime_kwargs = self._apply_session_model_override(\n"
    "                resolved_session_key, model, runtime_kwargs\n"
    "            )\n"
)
APP_CODE = '''
        # [local-customizations] discord.channel_models -- per-channel default
        # model. Applied after runtime resolution, ONLY when there is no explicit
        # /model session override (so a manual /model still wins). Same
        # provider/credentials; only the model NAME changes.
        if not override:
            _chan_model = self._resolve_channel_default_model(source, user_config)
            if _chan_model:
                model = _chan_model
'''


def main() -> int:
    target = RUN_PY
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        target = Path(sys.argv[1])
    if not target.is_file():
        print(f"channel_models: {target} not found", file=sys.stderr)
        return 0
    src = target.read_text(encoding="utf-8")
    if MARKER in src:
        print("channel_models: already applied (no-op)")
        return 0
    if HELPER_ANCHOR not in src or APP_ANCHOR not in src:
        print("channel_models: ANCHORS NOT FOUND — upstream run.py changed shape; "
              "MANUAL RE-APPLY NEEDED (see README.md).", file=sys.stderr)
        return 2
    patched = src.replace(HELPER_ANCHOR, HELPER_CODE + HELPER_ANCHOR, 1)
    patched = patched.replace(APP_ANCHOR, APP_ANCHOR + APP_CODE, 1)
    tmp = target.with_suffix(".chanmodels.tmp")
    tmp.write_text(patched, encoding="utf-8")
    try:
        py_compile.compile(str(tmp), doraise=True)
    except Exception as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        print(f"channel_models: patched file failed to compile ({exc}); NOT applied. "
              "MANUAL RE-APPLY NEEDED.", file=sys.stderr)
        return 3
    tmp.replace(target)
    print(f"channel_models: re-applied to {target} ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
