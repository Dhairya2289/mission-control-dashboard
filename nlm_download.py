#!/usr/bin/env python3
"""Artifact download helper for the NotebookLM dashboard integration.

Run with the *notebooklm* venv interpreter (it has `notebooklm-py` installed),
NOT the dashboard service venv:

    ~/.notebooklm-venv/bin/python nlm_download.py <notebook_id> <artifact_id> <type> <out_path> [format]

The CLI (v0.7.x) no longer exposes a `download` command, but the library still
does via `client.artifacts.download_<type>(notebook_id, out_path, artifact_id)`.
We map the dashboard's artifact `type` to the right method, write the file, and
print a single JSON line so the caller can parse the result:

    {"ok": true,  "path": "/abs/output.mp3"}
    {"ok": false, "error": "<message>"}

`artifact_id` may be empty ("") → the library picks the latest completed
artifact of that type. `format` is only meaningful for slide-deck (pdf|pptx)
and the interactive types (quiz|flashcards: json|md|html).
"""

import asyncio
import json
import sys

# type -> download method on client.artifacts
_TYPE_METHODS = {
    "audio": "download_audio",
    "video": "download_video",
    "cinematic-video": "download_video",
    "infographic": "download_infographic",
    "slide-deck": "download_slide_deck",
    "report": "download_report",
    "mind-map": "download_mind_map",
    "data-table": "download_data_table",
    "quiz": "download_quiz",
    "flashcards": "download_flashcards",
}


async def _main() -> None:
    if len(sys.argv) < 5:
        print(json.dumps({"ok": False, "error": "usage: nlm_download.py <nb> <aid> <type> <out> [format]"}))
        return
    notebook_id = sys.argv[1]
    artifact_id = sys.argv[2] or None
    art_type = sys.argv[3]
    out_path = sys.argv[4]
    fmt = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else None

    method = _TYPE_METHODS.get(art_type)
    if not method:
        print(json.dumps({"ok": False, "error": f"unsupported type: {art_type}"}))
        return

    try:
        from notebooklm import NotebookLMClient
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"import failed: {e}"}))
        return

    try:
        async with NotebookLMClient.from_storage() as client:
            fn = getattr(client.artifacts, method)
            # slide-deck / quiz / flashcards accept an output_format; others don't.
            # Try with the format kwarg first when supplied, fall back on TypeError.
            if fmt is not None:
                try:
                    await fn(notebook_id, out_path, artifact_id, output_format=fmt)
                except TypeError:
                    await fn(notebook_id, out_path, artifact_id)
            else:
                await fn(notebook_id, out_path, artifact_id)
        print(json.dumps({"ok": True, "path": out_path}))
    except Exception as e:  # noqa: BLE001 — surface any library/auth/network error as JSON
        print(json.dumps({"ok": False, "error": str(e)[:600]}))


if __name__ == "__main__":
    asyncio.run(_main())
