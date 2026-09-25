"""Read-only: what makes the Hermes backup large? Summarises the quarantined manifest.

  scp .go-status/backup-manifest-sizes.py worker:/tmp/ && \
  ssh -t worker "sudo python3 /tmp/backup-manifest-sizes.py; rm -f /tmp/backup-manifest-sizes.py"
"""

import json
import pathlib

manifest = pathlib.Path(
    "/var/lib/engineering-backup-mirror/quarantine-20260925/20260925T220356Z-3b4d46172bb8.json"
)
value = json.loads(manifest.read_text())
print("top-level keys:", sorted(value)[:30])
for key in ("archive_bytes", "source_bytes", "database_bytes", "support_bytes"):
    if key in value:
        print(key, value[key])
objects = value.get("support_objects", [])
print("support objects:", len(objects))
print("support_object_inline_bytes:", value.get("support_object_inline_bytes"))
print("provider_and_gh_auth_stores_included:", value.get("provider_and_gh_auth_stores_included"))


def shape(item, depth=0):
    """Print keys with sizes/counts only; never string values (no paths or secrets)."""
    if isinstance(item, dict):
        for key, sub in item.items():
            if isinstance(sub, (int, float, bool)) or sub is None:
                print("  " * depth + f"{key}: {sub}")
            elif isinstance(sub, str):
                print("  " * depth + f"{key}: <str {len(sub)} chars>")
            else:
                print("  " * depth + f"{key}:")
                shape(sub, depth + 1)
    elif isinstance(item, list):
        print("  " * depth + f"<list of {len(item)}>")
        if item:
            shape(item[0], depth + 1)


for section in ("databases", "gateway_snapshot"):
    print(f"== {section}")
    shape(value.get(section))
if objects:
    print("object fields:", sorted(objects[0]))


def size_of(item: dict) -> int:
    for key in ("plaintext_bytes", "ciphertext_bytes", "bytes", "size"):
        if isinstance(item.get(key), int):
            return item[key]
    return 0


def label_of(item: dict) -> str:
    for key in ("path", "name", "member", "source", "archive_name", "kind"):
        if isinstance(item.get(key), str):
            return item[key]
    return item.get("id", "?")[:16]


total = sum(size_of(item) for item in objects)
print(f"support objects total: {total / 1024**3:.2f} GiB")
for item in sorted(objects, key=size_of, reverse=True)[:15]:
    print(f"{size_of(item) / 1024**2:10.1f} MiB  {label_of(item)}")
