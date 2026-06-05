"""Print every HTTP/WS route registered on the FastAPI app.

Useful as a quick sanity check after merging a new feature branch:

    .venv/bin/python scripts/list_routes.py

Or grouped by tag prefix:

    .venv/bin/python scripts/list_routes.py --group

Output is plain text — no terminal colors — so it stays usable in CI logs
and `make verify` failure messages.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("OMNI_SKIP_EMBED_BACKFILL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402
from starlette.routing import Route, WebSocketRoute  # noqa: E402


def _prefix(path: str) -> str:
    """Bucket by the second path segment so `/api/insights/summary` and
    `/api/insights/categories` group together."""
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "api":
        return f"/api/{parts[1]}"
    return f"/{parts[0]}" if parts else "/"


def main() -> None:
    grouped = "--group" in sys.argv

    rows: list[tuple[str, str, str]] = []
    for r in app.routes:
        if isinstance(r, Route):
            methods = ",".join(sorted(r.methods or [])) or "GET"
            rows.append((methods, r.path, r.name or ""))
        elif isinstance(r, WebSocketRoute):
            rows.append(("WS", r.path, r.name or ""))
        else:
            # Mount, Static, etc.
            rows.append(("MOUNT", getattr(r, "path", str(r)), ""))

    rows.sort(key=lambda x: (x[1], x[0]))

    if grouped:
        by_prefix: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        for methods, path, name in rows:
            by_prefix[_prefix(path)].append((methods, path, name))
        for prefix in sorted(by_prefix):
            print(f"\n{prefix}")
            print("-" * 60)
            for methods, path, name in by_prefix[prefix]:
                print(f"  {methods:8s}  {path:40s}  {name}")
        print()
        print(f"Total: {len(rows)} routes across {len(by_prefix)} prefixes.")
        return

    print(f"{'METHODS':10s}{'PATH':45s}NAME")
    print("-" * 80)
    for methods, path, name in rows:
        print(f"{methods:10s}{path:45s}{name}")
    print()
    print(f"Total: {len(rows)} routes.")


if __name__ == "__main__":
    main()
