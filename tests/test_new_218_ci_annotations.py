"""NEW-218: the CI-repair maker sees check-run annotations, not only job log lines."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_staging_gitops import HEAD_SHA, _baseline_handler, _check, _plan

from engineering_control.activities.gitops import _annotation_block, _with_annotations

JOB_URL = "https://github.com/gntik-ai/musematic/actions/runs/5/job/{}"
CODEQL = {
    "path": "apps/api/src/handler.mjs",
    "start_line": 41,
    "end_line": 41,
    "annotation_level": "failure",
    "title": "Uncontrolled data used in path expression",
    "message": "This path depends on a user-provided value.",
}


def _serve_annotations(fake, runs: list[dict], annotations: dict[int, list[dict]]) -> None:
    answer = fake._answer

    def serve(args: list[str]) -> str:
        joined = " ".join(args)
        if args[0] == "api" and f"/commits/{HEAD_SHA}/check-runs" in args[1]:
            return json.dumps(runs)
        if args[0] == "api" and "/check-runs/" in args[1] and "/annotations" in args[1]:
            run = int(args[1].split("/check-runs/", 1)[1].split("/", 1)[0])
            return json.dumps(annotations.get(run, []))
        assert "check-runs" not in joined, joined
        return answer(args)

    fake._answer = serve  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_an_annotation_only_failure_reaches_the_repair_maker(tmp_path: Path) -> None:
    checks = [
        {**_check("CodeQL", "FAILURE"), "detailsUrl": "https://github.com/x/y/runs/900"},
        {**_check("quality", "FAILURE"), "detailsUrl": JOB_URL.format(11)},
    ]
    handler, fake = _baseline_handler(tmp_path, [".github/workflows/ci.yml"], checks)
    fake.job_logs = {11: "not ok 1 - quality\n"}
    _serve_annotations(
        fake,
        [
            {"id": 900, "name": "CodeQL", "conclusion": "failure", "annotations": 1},
            {"id": 11, "name": "quality", "conclusion": "failure", "annotations": 0},
            {"id": 12, "name": "lint", "conclusion": "success", "annotations": 4},
        ],
        {900: [CODEQL]},
    )

    blocked = await handler.handle("gitops.pr_checks", {"plan": _plan()})

    assert blocked["blocker"] == "SOURCE_PR_CI_REGRESSION"
    codeql = blocked["failure_excerpts"]["CodeQL"]
    assert codeql.startswith("Check-run annotations (1):")
    assert "apps/api/src/handler.mjs:41 [failure] Uncontrolled data" in codeql
    assert "Log excerpt" not in blocked["failure_excerpts"]["quality"]
    # Only annotated failing runs are read; a run without annotations is never fetched.
    reads = [" ".join(call) for call in fake.calls if "/annotations" in " ".join(call)]
    assert len(reads) == 1 and "/check-runs/900/annotations" in reads[0]


@pytest.mark.asyncio
async def test_annotations_come_first_and_the_log_keeps_the_rest(tmp_path: Path) -> None:
    checks = [{**_check("quality", "FAILURE"), "detailsUrl": JOB_URL.format(11)}]
    handler, fake = _baseline_handler(tmp_path, [".github/workflows/ci.yml"], checks)
    fake.job_logs = {11: "not ok 1 - quality\n" + "##[error]exit 1\n" * 400}
    _serve_annotations(
        fake,
        [{"id": 11, "name": "quality", "conclusion": "failure", "annotations": 30}],
        {11: [dict(CODEQL, start_line=index, end_line=index) for index in range(30)]},
    )

    blocked = await handler.handle("gitops.pr_checks", {"plan": _plan()})

    excerpt = blocked["failure_excerpts"]["quality"]
    assert excerpt.startswith("Check-run annotations (")
    assert "\nLog excerpt:\n" in excerpt
    assert "not ok 1 - quality" in excerpt
    assert len(excerpt) <= 4000


def test_line_anchored_credentials_are_redacted_before_formatting() -> None:
    # NEW-218 review: redacting the joined, prefixed line missed patterns anchored to a
    # whole value (e.g. password=...), so each raw field is redacted first.
    block = _annotation_block(
        [
            dict(CODEQL, message="password=DemoPlaintext123"),
            dict(CODEQL, title="password=DemoPlaintext123", message="see title"),
        ]
    )
    assert "DemoPlaintext123" not in block
    assert block.startswith("Check-run annotations (2):")


def test_annotation_text_is_bounded_and_redacted() -> None:
    token = "ghp_" + "A" * 36
    block = _annotation_block([dict(CODEQL, message=f"token {token} leaked " + "x" * 2000)] * 40)
    assert token not in block
    assert block.startswith("Check-run annotations (40):")
    assert len(block) <= 4000
    assert all(len(line) <= 500 for line in block.splitlines())
    assert _annotation_block([]) == ""
    combined = _with_annotations(block, "not ok 1 - quality")
    assert len(combined) <= 4000 and combined.endswith("not ok 1 - quality")
