"""Read-only diagnosis of the worker backup mirror (receiver prune crashes on missing
support objects). Changes nothing.

  scp .go-status/diagnose-backup-mirror.py worker:/tmp/ && \
  ssh -t worker "sudo python3 /tmp/diagnose-backup-mirror.py; rm -f /tmp/diagnose-backup-mirror.py"
"""

import json
import pathlib

root = pathlib.Path("/var/lib/engineering-backup-mirror")
repo = root / "repository"
cache = root / "support-objects"
present = {p.name for p in cache.iterdir()} if cache.exists() else set()

for manifest in sorted(repo.glob("*.json")):
    try:
        value = json.loads(manifest.read_text())
    except Exception as error:  # noqa: BLE001 - diagnostic output only
        print(manifest.name, "unreadable", error)
        continue
    objects = [item["id"] for item in value.get("support_objects", [])]
    missing = [item for item in objects if item not in present]
    print(
        manifest.name,
        f"v{value.get('version')}",
        "created",
        value.get("created_at_utc"),
        "objects",
        len(objects),
        "missing",
        len(missing),
        ("first-missing " + missing[0][:16]) if missing else "",
    )

print("cache objects present:", len(present))
for name in ("incoming", "work", "failures"):
    directory = root / name
    entries = sorted(p.name for p in directory.glob("*")) if directory.exists() else None
    print(f"{name}:", "n/a" if entries is None else f"{len(entries)} entries {entries[:6]}")
for name in ("status.json", "state.json", "verified.json"):
    path = root / name
    if path.exists():
        try:
            state = json.loads(path.read_text())
            print(name, {k: state.get(k) for k in ("status", "identifier", "created_at_utc")})
        except Exception as error:  # noqa: BLE001
            print(name, "unreadable", error)
