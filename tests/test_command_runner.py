from __future__ import annotations

import hashlib
import sys

from engineering_control.adapters.command import SafeCommandRunner


def test_yaml_summary_parses_raw_stream_without_returning_rendered_values(tmp_path) -> None:
    rendered = """apiVersion: v1
kind: ConfigMap
metadata:
  name: bounded-fixture
data:
  password: fixture-value-never-returned
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: bounded-deployment
"""
    runner = SafeCommandRunner({"python": tmp_path / "python"})
    (tmp_path / "python").symlink_to(sys.executable)

    summary = runner.run_yaml_summary(
        "python",
        ["-c", f"import sys; sys.stdout.write({rendered!r})"],
        cwd=tmp_path,
    )

    assert summary.stdout_sha256 == hashlib.sha256(rendered.encode()).hexdigest()
    assert summary.document_count == 2
    assert dict(summary.kinds) == {"ConfigMap": 1, "Deployment": 1}
    assert "fixture-value-never-returned" not in repr(summary)
