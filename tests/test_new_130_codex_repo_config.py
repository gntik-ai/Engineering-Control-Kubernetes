"""NEW-130: Codex runs never load configuration from the repository they work on."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from changeset_worktree import link_changeset_worktree

from engineering_control.activities.scan import ScanActivityHandler
from engineering_control.adapters.codex import client as codex_module
from engineering_control.adapters.codex.client import CodexCLIAdapter
from engineering_control.adapters.process import ProcessResult
from engineering_control.config import Settings
from engineering_control.delivery_notifications import explain_blocker
from engineering_control.models.core import AgentRequest
from engineering_control.models.enums import ModelClass, Project, Provider

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git") or "/usr/bin/git"


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(  # noqa: S603 - fixed Git executable in the test harness
        [GIT, *arguments], cwd=repo, check=True, capture_output=True
    )


def _override_paths(value: str) -> dict[str, Any]:
    key, _, table = value.partition("=")
    assert key == "projects"
    parsed: dict[str, Any] = tomllib.loads(f"projects = {table}")["projects"]
    return parsed


async def _command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, working_directory: Path, **extra: Any
) -> list[str]:
    observed: dict[str, list[str]] = {}

    async def fake_run(command: list[str], **kwargs: Any) -> ProcessResult:
        observed["command"] = command
        final_path = Path(command[command.index("--output-last-message") + 1])
        final_path.write_text('{"summary":"bounded"}', encoding="utf-8")  # noqa: ASYNC240
        return ProcessResult(exit_code=0, events=(), stderr="")

    monkeypatch.setattr(codex_module, "run_jsonl_process", fake_run)
    request = AgentRequest(
        project=Project.MUSEMATIC,
        role="implementer",
        provider=Provider.CODEX,
        model_class=ModelClass.WORKER,
        prompt="perform a bounded synthetic task",
        working_directory=str(working_directory),
        output_schema={"type": "object"},
        **extra,
    )
    await CodexCLIAdapter(temp_root=str(tmp_path / "codex-temp"), default_model="m").execute(
        request
    )
    return observed["command"]


@pytest.mark.asyncio
async def test_adapter_marks_cwd_and_worktree_root_untrusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real" / "worktree"
    (real / "pkg" / "sub").mkdir(parents=True)
    _git(real, "init", "-q")
    link = tmp_path / "link"
    link.symlink_to(real)
    cwd = link / "pkg" / "sub"

    command = await _command(tmp_path, monkeypatch, cwd)

    overrides = [command[i + 1] for i, part in enumerate(command) if part == "--config"]
    project_overrides = [value for value in overrides if value.startswith("projects=")]
    assert len(project_overrides) == 1
    paths = _override_paths(project_overrides[0])
    assert set(paths) == {
        str(cwd),
        str(real / "pkg" / "sub"),
        str(link),
        str(real),
    }
    assert all(entry == {"trust_level": "untrusted"} for entry in paths.values())
    assert command[-1] == "-"


@pytest.mark.asyncio
async def test_adapter_without_git_root_still_untrusts_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "plain"
    cwd.mkdir()

    command = await _command(tmp_path, monkeypatch, cwd, skip_git_repository_check=True)

    assert _override_paths(command[-2]) == {str(cwd.resolve()): {"trust_level": "untrusted"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("writable", [True, False])
async def test_adapter_other_flags_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writable: bool
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _git(worktree, "init", "-q")

    command = await _command(
        tmp_path, monkeypatch, worktree, writable=writable, model="gpt-x", effort="high"
    )

    schema = command[command.index("--output-schema") + 1]
    final = command[command.index("--output-last-message") + 1]
    assert command == [
        "codex",
        "exec",
        "--json",
        "--color",
        "never",
        "--ephemeral",
        "--strict-config",
        "--cd",
        str(worktree),
        "--sandbox",
        "workspace-write" if writable else "read-only",
        "--output-schema",
        schema,
        "--output-last-message",
        final,
        "--model",
        "gpt-x",
        "--config",
        'model_reasoning_effort="high"',
        "--config",
        f'projects={{"{worktree.resolve()}"={{trust_level="untrusted"}}}}',
        "-",
    ]


def _codex_executable() -> str | None:
    found = shutil.which("codex")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "codex"
    return str(local) if local.exists() else None


def test_real_codex_cli_ignores_the_repository_mcp_server(tmp_path: Path) -> None:
    """Inert: temporary CODEX_HOME, no credentials, network routed to a closed port, and
    only `codex mcp list` (config listing) on a stdio /bin/true server."""
    codex = _codex_executable()
    if codex is None:
        pytest.skip("codex CLI is not installed")
    home = tmp_path / "codex-home"
    home.mkdir()
    repo = (tmp_path / "worktrees" / "repo" / "change").resolve()
    (repo / ".codex" / "agents").mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / ".codex" / "config.toml").write_text(
        'model = "repo-model"\nsandbox_mode = "danger-full-access"\n'
        '[mcp_servers.repo_server]\ncommand = "/bin/true"\n'
        '[agents.repo_agent]\ndescription = "repo agent"\nconfig_file = "agents/a.toml"\n',
        encoding="utf-8",
    )
    (repo / ".codex" / "agents" / "a.toml").write_text('model = "x"\n', encoding="utf-8")
    (home / "config.toml").write_text(
        'model = "hermes-model"\ncheck_for_update_on_startup = false\n'
        f'[analytics]\nenabled = false\n[projects."{repo}"]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OPENAI", "CODEX", "HTTP", "HTTPS", "ALL_PROXY"))
    }
    environment.update(
        CODEX_HOME=str(home),
        HTTP_PROXY="http://127.0.0.1:9",
        HTTPS_PROXY="http://127.0.0.1:9",
        ALL_PROXY="http://127.0.0.1:9",
        NO_PROXY="",
    )

    def servers(*overrides: str) -> list[str]:
        arguments = [codex]
        for value in overrides:
            arguments += ["--config", value]
        result = subprocess.run(  # noqa: S603 - local CLI, temporary home, inert listing
            [*arguments, "mcp", "list", "--json"],
            cwd=repo,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return [server["name"] for server in json.loads(result.stdout)]

    # Sanity: the trusted project's server is loaded without the override.
    assert servers() == ["repo_server"]
    assert servers(CodexCLIAdapter._untrusted_project_override(str(repo))) == []


class _Manager:
    def __init__(self, changed: dict[str, list[str]]) -> None:
        self.changed = changed
        self.granted = False

    def whitespace_check_failed(self, path: Path, *, base_sha: str) -> bool:
        return False

    def change_evidence(self, path: Path, *, base_sha: str) -> tuple[dict[str, Any], str]:
        paths = self.changed[path.name]
        return {"changed_file_count": len(paths), "changed_paths": paths}, "+x\n"

    def grant_delivery_read(self, repository: str, path: Path) -> None:
        self.granted = True

    def grant_release_read(self, repository: str, path: Path) -> None:
        self.granted = True


class _Scanner:
    """Scans only the sides before the refused one (the source side is checked first)."""

    def __init__(self, refused_side: str) -> None:
        self.refused_side = refused_side
        self.scanned: list[str] = []

    def _record(self, worktree: Path, output: Path) -> None:
        assert worktree.name != self.refused_side, "a refused side must never be scanned"
        self.scanned.append(worktree.name)
        output.mkdir(parents=True, exist_ok=True)

    def secret_scan(self, worktree: Path, output: Path) -> dict[str, Any]:
        self._record(worktree, output)
        return {"status": "PASS", "findings": []}

    def vulnerability_scan(self, worktree: Path, output: Path) -> dict[str, Any]:
        return {"Results": []}

    def semgrep(self, worktree: Path, output: Path) -> dict[str, Any]:
        return {"results": []}

    def sbom(self, worktree: Path, output: Path) -> dict[str, Any]:
        return {}


class _Publisher:
    def publish(self, path: Path, **_: Any) -> dict[str, Any]:
        return {"id": str(uuid4()), "filename": path.name}


def _validate(
    tmp_path: Path, changed: dict[str, list[str]], refused_side: str
) -> tuple[dict[str, Any], _Manager]:
    manager = _Manager(changed)
    (tmp_path / "projects").mkdir()
    handler = ScanActivityHandler(
        Settings(
            worktree_root=tmp_path / "worktrees",
            evidence_root=tmp_path / "evidence",
            project_config_root=tmp_path / "projects",
        ),
        manager,  # type: ignore[arg-type]
        scanner=_Scanner(refused_side),  # type: ignore[arg-type]
        publisher=_Publisher(),  # type: ignore[arg-type]
    )
    passed: dict[str, Any] = {"structured_result": {"tests": []}}
    payload = {
        "project": "llmwiki",
        "change_set_id": str(uuid4()),
        "source_worktree": str(tmp_path / "worktrees" / "source"),
        "deployment_worktree": str(tmp_path / "worktrees" / "deployment"),
        "source_repository": "llmwiki",
        "deployment_repository": "llmwiki-deploy",
        "source_base_sha": "a" * 40,
        "deployment_base_sha": "b" * 40,
        "agent_result": {"structured_result": {"decision": "PASS"}},
        "source_maker_result": passed,
        "deployment_maker_result": passed,
    }
    return handler._validate_changeset(payload), manager


@pytest.mark.parametrize(
    ("side", "path"),
    [
        ("source", ".codex/config.toml"),
        ("source", "services/api/.codex/agents/new.toml"),
        ("source", ".mcp.json"),
        ("source", ".claude/settings.json"),
        ("deployment", "charts/.claude/hooks/x.sh"),
    ],
)
def test_validation_refuses_agent_configuration_changes(
    tmp_path: Path, side: str, path: str
) -> None:
    changed = {"source": ["app.py"], "deployment": ["values.yaml"]}
    changed[side] = [*changed[side], path]

    validated, manager = _validate(tmp_path, changed, side)

    assert validated["status"] == "BLOCKED"
    assert validated["blocker"] == "AGENT_CONFIG_CHANGE_REFUSED"
    assert validated["agent_config_paths"] == [f"{side}: {path}"]
    assert path in validated["operator_message"]
    assert manager.granted is False
    assert ".codex/" in explain_blocker("AGENT_CONFIG_CHANGE_REFUSED")


def test_agent_config_paths_ignores_lookalikes() -> None:
    assert (
        ScanActivityHandler._agent_config_paths(
            ["docs/codex/config.toml", "src/.codex_notes.md", "x.mcp.json.bak", ".codexrc"]
        )
        == []
    )


_SAMPLE = """\
model = "gpt-6.1-sol"
# operator note
[features]
x = true

[projects."/srv/engineering/worktrees/musematic/abc"]
trust_level = "trusted"

[projects."/home/codex-runner/work"]
trust_level = "trusted"

[projects.'/srv/engineering/worktrees/llmwiki/def']
trust_level = "trusted"
[mcp_servers.ok]
command = "/bin/true"
[projects."/srv/engineering/worktrees-other/x"]
trust_level = "trusted"
"""


def _base_repo(tmp_path: Path, files: dict[str, str], links: dict[str, str]) -> tuple[Path, str]:
    repo = tmp_path / "worktrees" / "llmwiki" / str(uuid4())
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    for name, content in {"app.py": "x = 1\n", **files}.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content, encoding="utf-8")
    for name, target in links.items():
        (repo / name).symlink_to(target)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    base = subprocess.run(  # noqa: S603 - fixed Git executable in the test harness
        [GIT, "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    link_changeset_worktree(repo, tmp_path / "metadata")
    return repo, base


def _commit(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "change")


def _agent_paths(tmp_path: Path, repo: Path, base: str) -> list[str]:
    from engineering_control.worktrees.manager import GitWorktreeManager

    manager = GitWorktreeManager(
        mirror_root=tmp_path / "mirrors",
        worktree_root=tmp_path / "worktrees",
        lease_root=tmp_path / "leases",
        metadata_root=tmp_path / "metadata",
    )
    summary, _ = manager.change_evidence(repo, base_sha=base)
    reported = summary["agent_config_paths"]
    assert isinstance(reported, list)
    return [str(item) for item in reported]


def test_git_non_ascii_agent_config_name_is_refused(tmp_path: Path) -> None:
    repo, base = _base_repo(tmp_path, {}, {})
    (repo / ".codex" / "agents").mkdir(parents=True)
    (repo / ".codex" / "agents" / "é.toml").write_text('model = "x"\n', encoding="utf-8")
    _commit(repo)

    assert _agent_paths(tmp_path, repo, base) == [".codex/agents/é.toml"]


def test_git_rename_out_of_codex_is_refused(tmp_path: Path) -> None:
    repo, base = _base_repo(tmp_path, {".codex/config.toml": 'model = "m"\n'}, {})
    (repo / ".codex" / "config.toml").rename(repo / "agent-settings.toml")
    _commit(repo)

    assert _agent_paths(tmp_path, repo, base) == [".codex/config.toml"]


def test_git_deleting_mcp_json_is_refused(tmp_path: Path) -> None:
    repo, base = _base_repo(tmp_path, {"sub/.mcp.json": "{}\n"}, {})
    (repo / "sub" / ".mcp.json").unlink()
    _commit(repo)

    assert _agent_paths(tmp_path, repo, base) == ["sub/.mcp.json"]


def test_git_symlink_named_codex_is_refused(tmp_path: Path) -> None:
    repo, base = _base_repo(tmp_path, {}, {})
    (repo / "agent-settings").mkdir()
    (repo / "agent-settings" / "config.toml").write_text('model = "x"\n', encoding="utf-8")
    (repo / ".codex").symlink_to("agent-settings")
    _commit(repo)

    assert ".codex" in _agent_paths(tmp_path, repo, base)


@pytest.mark.parametrize("commit", [True, False])
def test_git_symlink_alias_to_agent_config_is_refused(tmp_path: Path, commit: bool) -> None:
    repo, base = _base_repo(tmp_path, {".claude/settings.json": "{}\n"}, {})
    (repo / "docs").mkdir()
    (repo / "docs" / "alias").symlink_to("../.claude")
    if commit:
        _commit(repo)

    assert _agent_paths(tmp_path, repo, base) == [
        "docs/alias -> ../.claude",
        "docs/alias -> .claude",
    ]


def test_git_existing_symlink_onto_changed_file_is_refused(tmp_path: Path) -> None:
    repo, base = _base_repo(tmp_path, {"conf/config.toml": 'model = "m"\n'}, {".codex": "conf"})
    (repo / "conf" / "config.toml").write_text('model = "evil"\n', encoding="utf-8")
    _commit(repo)

    assert _agent_paths(tmp_path, repo, base) == [".codex -> conf"]


def test_git_ordinary_change_beside_shipped_agent_config_passes(tmp_path: Path) -> None:
    # musematic ships .codex/; changing other files must still deliver.
    repo, base = _base_repo(
        tmp_path, {".codex/config.toml": 'model = "m"\n', ".codex/agents/a.toml": "x = 1\n"}, {}
    )
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "é.py").write_text("y = 1\n", encoding="utf-8")
    _commit(repo)

    assert _agent_paths(tmp_path, repo, base) == []


def test_validation_uses_the_manager_git_agent_config_report(tmp_path: Path) -> None:
    class Reporting(_Manager):
        def whitespace_check_failed(self, path: Path, *, base_sha: str) -> bool:
            return False

        def change_evidence(self, path: Path, *, base_sha: str) -> tuple[dict[str, Any], str]:
            summary, diff = super().change_evidence(path, base_sha=base_sha)
            # git C-quotes this name in changed_paths; the manager reports it raw.
            if path.name == "source":
                summary["agent_config_paths"] = [".codex/agents/é.toml"]
            return summary, diff

    changed = {"source": ['".codex/agents/\\303\\251.toml"'], "deployment": ["values.yaml"]}
    manager = Reporting(changed)
    (tmp_path / "projects").mkdir()
    handler = ScanActivityHandler(
        Settings(
            worktree_root=tmp_path / "worktrees",
            evidence_root=tmp_path / "evidence",
            project_config_root=tmp_path / "projects",
        ),
        manager,  # type: ignore[arg-type]
        scanner=_Scanner("source"),  # type: ignore[arg-type]
        publisher=_Publisher(),  # type: ignore[arg-type]
    )
    passed: dict[str, Any] = {"structured_result": {"tests": []}}
    validated = handler._validate_changeset(
        {
            "project": "musematic",
            "change_set_id": str(uuid4()),
            "source_worktree": str(tmp_path / "worktrees" / "source"),
            "deployment_worktree": str(tmp_path / "worktrees" / "deployment"),
            "source_repository": "musematic",
            "deployment_repository": "musematic-deploy",
            "source_base_sha": "a" * 40,
            "deployment_base_sha": "b" * 40,
            "agent_result": {"structured_result": {"decision": "PASS"}},
            "source_maker_result": passed,
            "deployment_maker_result": passed,
        }
    )

    assert validated["blocker"] == "AGENT_CONFIG_CHANGE_REFUSED"
    assert validated["agent_config_paths"] == ["source: .codex/agents/é.toml"]


def test_git_changed_intermediate_symlink_in_the_chain_is_refused(tmp_path: Path) -> None:
    # NEW-130 review 2: existing `.mcp.json -> settings.json`; the ChangeSet replaces
    # `settings.json` with a symlink to an existing `payload.json` (unchanged). Only the
    # middle of the chain changed, and that alone must be refused.
    repo, base = _base_repo(
        tmp_path,
        {"settings.json": "{}\n", "payload.json": '{"mcpServers": {}}\n'},
        {".mcp.json": "settings.json"},
    )
    (repo / "settings.json").unlink()
    (repo / "settings.json").symlink_to("payload.json")
    _commit(repo)

    refused = _agent_paths(tmp_path, repo, base)
    assert any(item.startswith(".mcp.json") for item in refused), refused


def test_git_non_utf8_intermediate_symlink_is_refused(tmp_path: Path) -> None:
    # NEW-130 review 3: an intermediate filename that is not valid UTF-8.
    import os as _os

    odd = _os.fsdecode(b"settings-\xff.json")
    repo, base = _base_repo(
        tmp_path,
        {odd: "{}\n", "payload.json": '{"mcpServers": {}}\n'},
        {".mcp.json": odd},
    )
    (repo / odd).unlink()
    (repo / odd).symlink_to("payload.json")
    _commit(repo)

    refused = _agent_paths(tmp_path, repo, base)
    assert any(item.startswith(".mcp.json") for item in refused), refused
