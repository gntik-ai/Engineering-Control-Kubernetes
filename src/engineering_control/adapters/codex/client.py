from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from engineering_control.adapters.auth_probe import AuthProbeError, run_auth_probe
from engineering_control.adapters.process import (
    EventCallback,
    SandboxToolchain,
    classify_failure,
    run_jsonl_process,
)
from engineering_control.models.core import AgentRequest, AgentResult, ProviderUsage
from engineering_control.models.enums import Provider, RunStatus
from engineering_control.provider_error_kinds import provider_error_kind
from engineering_control.security.redaction import SecretRedactor


class CodexCLIAdapter:
    provider = Provider.CODEX

    def __init__(
        self,
        executable: str = "codex",
        temp_root: str | None = None,
        default_model: str | None = None,
    ) -> None:
        self.executable = executable
        self.temp_root = temp_root
        self.default_model = default_model

    async def authenticated(self) -> bool:
        result = await run_auth_probe(
            [self.executable, "login", "status"], provider=self.provider.value
        )
        if result.failure is not None:
            raise AuthProbeError(result.failure)
        return result.exit_code == 0

    @staticmethod
    def _git_metadata_roots(worktree: Path) -> list[str]:
        """NEW-37: a linked worktree keeps its index, HEAD and objects outside the
        worktree (the per-worktree gitdir and the shared commondir). The workspace-write
        sandbox only allows writes under the working directory, so the maker could edit
        and test but never `git commit` ("index.lock" read-only). Return those two git
        directories -- the maker's ACL already grants them -- as extra writable roots."""
        marker = worktree / ".git"
        if not marker.is_file():
            return []
        text = marker.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            return []
        gitdir = Path(text.removeprefix("gitdir:").strip())
        if not gitdir.is_absolute():
            gitdir = worktree / gitdir
        gitdir = gitdir.resolve()
        roots = [gitdir]
        commondir_file = gitdir / "commondir"
        if commondir_file.is_file():
            common = Path(commondir_file.read_text(encoding="utf-8").strip())
            common = (common if common.is_absolute() else gitdir / common).resolve()
            if common not in roots:
                roots.append(common)
        worktree_root = worktree.resolve()
        return [str(root) for root in roots if not root.is_relative_to(worktree_root)]

    @staticmethod
    def _project_trust_paths(working_directory: str) -> list[str]:
        """NEW-130: the directories whose Codex project trust must be forced off.

        Codex 0.159.3 loads a project's `.codex/config.toml` (MCP servers, `[agents]`,
        model, sandbox and approval settings, hooks, exec policies) for every `.codex/`
        folder between the cwd and the project root (the nearest `.git`) when the user
        config marks the cwd, the project root, or (for a linked worktree) the main
        checkout as trusted, and Codex wrote such entries for every past worktree. Trust
        is looked up for the cwd first and then the project root; a session `-c` entry
        for the same key beats the user config. Return the cwd and its project root,
        each absolute and resolved, deduplicated in a stable order.
        """
        absolute = Path(os.path.abspath(working_directory))
        candidates: list[Path] = []
        for cwd in (absolute, absolute.resolve()):
            candidates.append(cwd)
            for directory in (cwd, *cwd.parents):
                if (directory / ".git").exists():
                    candidates.append(directory)
                    break
        unique: list[str] = []
        for candidate in candidates:
            for form in (str(candidate), str(candidate.resolve())):
                if form not in unique:
                    unique.append(form)
        return unique

    @classmethod
    def _untrusted_project_override(cls, working_directory: str) -> str:
        """NEW-130: one `--config` value that marks every trust path untrusted.

        Verified on codex-cli 0.159.3 (temporary CODEX_HOME + repo, `app-server`
        `config/read` and a real `exec` whose repo MCP server was a marker command):
        - The dotted form `-c 'projects."<path>".trust_level="untrusted"'` does NOT work:
          the `-c` key parser keeps the quotes, so it adds a key named `"<path>"` and
          the trusted entry still applies.
        - An inline table `-c 'projects={"<path>"={trust_level="untrusted"}}'` is merged
          into `projects` and wins: the project layer gets a disabledReason, so its
          `mcp_servers` (never started), `[agents]`, model, sandbox_mode and
          approval_policy are dropped; the user config and CLI flags stay in force.
        - The cwd entry beats the project-root entry, so both are overridden. An entry
          on the main checkout of a linked worktree is beaten by the worktree root's.
        - There is no option that disables project config entirely (only
          `--ignore-user-config`, which also drops the runner's own config).
        Skills and AGENTS.md still load from untrusted folders; they are instructions.
        """
        entries = ",".join(
            f'{json.dumps(path, ensure_ascii=False)}={{trust_level="untrusted"}}'
            for path in cls._project_trust_paths(working_directory)
        )
        return f"projects={{{entries}}}"

    async def execute(
        self,
        request: AgentRequest,
        on_event: EventCallback | None = None,
        *,
        toolchain: SandboxToolchain | None = None,
    ) -> AgentResult:
        if request.provider is not self.provider:
            raise ValueError("Codex adapter received a non-Codex request")
        started_at = datetime.now(UTC)
        temp_parent = self._run_temp_parent(request.run_id) if self.temp_root else None
        with tempfile.TemporaryDirectory(prefix="attempt-", dir=temp_parent) as raw:
            temp = Path(raw)
            os.chmod(temp, 0o700)
            schema_path = temp / "output-schema.json"
            final_path = temp / "final.json"
            schema_path.write_text(
                json.dumps(request.output_schema, sort_keys=True), encoding="utf-8"
            )
            os.chmod(schema_path, 0o600)

            command = [
                self.executable,
                "exec",
                "--json",
                "--color",
                "never",
                "--ephemeral",
                "--strict-config",
                "--cd",
                request.working_directory,
                "--sandbox",
                "workspace-write" if request.writable else "read-only",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(final_path),
            ]
            if request.skip_git_repository_check:
                command.append("--skip-git-repo-check")
            if request.writable:
                for root in self._git_metadata_roots(Path(request.working_directory)):
                    command.extend(("--add-dir", root))
            model = request.model or self.default_model
            if model:
                command.extend(("--model", model))
            if request.effort:
                command.extend(("--config", f'model_reasoning_effort="{request.effort}"'))
            # NEW-130: never load configuration from the repository being worked on.
            command.extend(
                ("--config", self._untrusted_project_override(request.working_directory))
            )
            command.append("-")

            process_result = await run_jsonl_process(
                command,
                prompt=request.prompt,
                cwd=request.working_directory,
                timeout_seconds=request.timeout_seconds,
                provider=self.provider.value,
                on_event=on_event,
                toolchain=toolchain,
            )
            structured_result: dict[str, Any] = {}
            summary = ""
            if final_path.exists():
                safe = SecretRedactor().redact_text(final_path.read_text(encoding="utf-8"))
                summary = str(safe.value).strip()
                try:
                    parsed = json.loads(summary)
                    if isinstance(parsed, dict):
                        structured_result = SecretRedactor().redact(parsed).value
                except json.JSONDecodeError:
                    pass

            usage = self._usage(process_result.events)
            session_id = self._session_id(process_result.events)
            if process_result.exit_code == 0 and not process_result.timed_out:
                status = RunStatus.SUCCESS
                error_code = None
                error_kind = None
            else:
                raw_status, error_code = classify_failure(process_result)
                error_kind = provider_error_kind(
                    process_result.events, timed_out=process_result.timed_out
                )
                status = RunStatus(raw_status)
                if not summary:
                    summary = self._failure_summary(process_result.events, process_result.stderr)
            return AgentResult(
                run_id=request.run_id,
                provider=self.provider,
                role=request.role,
                status=status,
                summary=summary,
                usage=usage,
                structured_result=structured_result,
                provider_session_id=session_id,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                exit_code=process_result.exit_code,
                error_code=error_code,
                provider_error_kind=error_kind,
            )

    def _run_temp_parent(self, run_id: UUID) -> Path:
        configured = Path(str(self.temp_root))
        configured.mkdir(parents=True, exist_ok=True, mode=0o700)
        if configured.is_symlink() or not configured.is_dir():
            raise ValueError("Codex temporary root must be a real directory")
        root = configured.resolve(strict=True)
        run_root = root / str(run_id)
        if run_root.exists() or run_root.is_symlink():
            if run_root.is_symlink() or not run_root.is_dir():
                raise ValueError("Codex run temporary root must be a real directory")
            if stat.S_IMODE(run_root.stat().st_mode) != 0o700:
                raise ValueError("Codex run temporary root has unsafe permissions")
        else:
            run_root.mkdir(mode=0o700)
        if run_root.resolve(strict=True).parent != root:
            raise ValueError("Codex run temporary root escaped its provider root")
        return run_root

    @staticmethod
    def _session_id(events: tuple[dict[str, Any], ...]) -> str | None:
        for event in events:
            if event.get("type") in {"thread.started", "session.started"}:
                candidate = event.get("thread_id") or event.get("session_id")
                if isinstance(candidate, str):
                    return candidate
        return None

    @staticmethod
    def _usage(events: tuple[dict[str, Any], ...]) -> ProviderUsage:
        values: dict[str, int] = {}
        for event in events:
            usage = event.get("usage")
            if isinstance(usage, dict):
                for target, alternatives in {
                    "input_tokens": ("input_tokens", "inputTokens"),
                    "output_tokens": ("output_tokens", "outputTokens"),
                    "cached_input_tokens": ("cached_input_tokens", "cachedInputTokens"),
                }.items():
                    for source in alternatives:
                        if isinstance(usage.get(source), int):
                            values[target] = usage[source]
                            break
        return ProviderUsage(
            input_tokens=values.get("input_tokens"),
            output_tokens=values.get("output_tokens"),
            cached_input_tokens=values.get("cached_input_tokens"),
        )

    @staticmethod
    def _failure_summary(events: tuple[dict[str, Any], ...], stderr: str) -> str:
        for event in reversed(events):
            event_type = str(event.get("type", ""))
            if "error" not in event and not event_type.endswith(".failed"):
                continue
            payload = event.get("error") or event.get("message") or event.get("detail")
            if payload is None or payload == "" or payload == {}:
                continue
            rendered = (
                payload
                if isinstance(payload, str)
                else json.dumps(payload, separators=(",", ":"), sort_keys=True)
            )
            safe = SecretRedactor().redact_text(f"{event_type}: {rendered}")
            return str(safe.value)[-4000:]
        return str(SecretRedactor().redact_text(stderr[-4000:]).value)
