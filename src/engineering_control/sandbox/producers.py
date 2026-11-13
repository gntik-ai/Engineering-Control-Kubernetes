"""The flows that produce backlog items: an audit of the code, a search for enhancements.

Both read a checkout and change nothing in it. One subscription proposes, the other
judges, and what holds up becomes proposed items of the backlog, where the administrator
decides. Nothing here reaches GitHub. What each agent is asked is in ``audit`` and
``discover``.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Final
from uuid import UUID

from engineering_control.dashboard.backlog_items import BacklogItems
from engineering_control.dashboard.project_records import ProjectNotFound
from engineering_control.github_app import GitHubApp
from engineering_control.sandbox import audit, deliver, discover
from engineering_control.sandbox.flows import (
    Checkouts,
    End,
    Follow,
    Plan,
    Result,
    account_trouble,
    why_no_answer,
)
from engineering_control.sandbox.runs import Lease, Runs, StepWork
from engineering_control.sandbox.steps import Verdict

# The original gave an audit and a discovery to Codex first, with Claude as the fallback.
FIRST: Final = "chatgpt,claude"


def _other(provider: str) -> str:
    return next(name for name in deliver.PROVIDERS if name != provider)


def _failed(outcome: str, detail: str) -> Result:
    return Result(Verdict(False, outcome, detail))


class _Producer:
    """What the two flows share: the checkout, an agent that only reads, an answer that
    is asked for once more when it does not come."""

    def __init__(self, runs: Runs, github: GitHubApp | None, backlog: BacklogItems) -> None:
        self.runs = runs
        self.backlog = backlog
        self.checkouts = Checkouts(github)

    async def _prepare(self, item: StepWork, name: str) -> Plan:
        toolchain = deliver.Toolchain.from_dict(item.run_data.get("toolchain"))
        # What the project needs is installed, so that a reproduction can build and test;
        # its tests are not run beforehand, because nothing is going to be changed.
        installed = deliver.Toolchain(prepare=toolchain.prepare, tests=())
        branch = f"engineering-control/{name}-{item.run_id.hex[:8]}"
        return await self.checkouts.plan(item, branch=branch, toolchain=installed)

    @staticmethod
    def _reader(lease: Lease | None, prompt: str, schema: dict[str, Any]) -> Plan:
        if lease is None:
            raise ValueError("the step needs an account")
        return Plan(
            deliver.review_job(lease.provider, lease.generation, prompt, schema),
            workspace=True,
            memory="6Gi",
            deadline_seconds=3600,
        )

    async def _known(self, item: StepWork) -> list[str]:
        try:
            return await asyncio.to_thread(self.backlog.open_titles, item.run_data["project"])
        except ProjectNotFound:
            return []

    @staticmethod
    def _answer(
        item: StepWork, commands: list[dict[str, Any]], *, who: str, retry: str | None
    ) -> tuple[dict[str, Any] | None, Result | None]:
        """The agent's answer, or what to do without one: try another account, ask once
        more, or give up."""
        trouble = account_trouble(item, commands, retry_provider=retry)
        if trouble is not None:
            return None, trouble
        provider = item.provider or ""
        answer = deliver.agent_answer(provider, commands)
        if answer is not None:
            return answer, None
        why = why_no_answer(provider, commands)
        asked = int(item.input.get("asked_again") or 0)
        if asked >= 1:
            return None, _failed("no_answer", f"{who} gave no answer twice: {why}")
        return None, Result(
            Verdict(True, "no_answer", f"{who} gave no answer ({why}). It is asked again."),
            follow=(Follow(item.name, item.provider, {**item.input, "asked_again": asked + 1}),),
        )

    async def ended(self, item: StepWork) -> None:
        await self.checkouts.ended(item)

    async def _close_request(self, run_id: UUID, facts: dict[str, Any]) -> None:
        """A run that an item asked for has done what the item asked."""
        item_id = await asyncio.to_thread(self.runs.item_of, run_id)
        if item_id is not None:
            await asyncio.to_thread(lambda: self.backlog.note_facts(item_id, facts, state="done"))


class AuditCode(_Producer):
    """Check out, audit, verify twice; confirmed findings become proposed items. Without
    a scope: plan the areas of the repository first, then audit and verify each in turn."""

    @staticmethod
    def _whole(data: dict[str, Any]) -> bool:
        scope = data.get("scope") or {}
        return (
            not str(scope.get("title") or "").strip() and not str(scope.get("text") or "").strip()
        )

    @staticmethod
    def _area_of(item: StepWork) -> int | None:
        """The area a step belongs to, in a whole-repository audit."""
        area = item.input.get("area")
        return int(area) if isinstance(area, int) else None

    def _after_area(self, item: StepWork) -> tuple[Follow, ...]:
        """What follows once an area has been audited and verified: the next area."""
        area = self._area_of(item)
        planned = item.run_data.get("areas") or []
        if area is None or area + 1 >= len(planned):
            return ()
        return (Follow("audit", FIRST, {"area": area + 1}),)

    async def plan(self, item: StepWork, lease: Lease | None) -> Plan:
        data = item.run_data
        if item.name == "prepare":
            return await self._prepare(item, "audit")
        if item.name == "plan":
            prompt = audit.planner_prompt(repository=data["repository"], base_sha=data["base_sha"])
            return self._reader(lease, prompt, audit.PLAN_SCHEMA)
        area = self._area_of(item)
        planned = data.get("areas") or []
        if item.name == "audit":
            prompt = audit.auditor_prompt(
                repository=data["repository"],
                base_sha=data["base_sha"],
                scope=data.get("scope") or {},
                area=(planned[area], area, len(planned))
                if area is not None and area < len(planned)
                else None,
            )
            return self._reader(lease, prompt, audit.AUDIT_SCHEMA)
        if item.name == "verify":
            found = await asyncio.to_thread(self.runs.step_outputs, item.run_id, "audit")
            mine = [output for output in found if output.get("area") == area]
            toolchain = deliver.Toolchain.from_dict(data.get("toolchain"))
            prompt = audit.verifier_prompt(
                repository=data["repository"],
                base_sha=data["base_sha"],
                findings=(mine[-1] if mine else {}).get("candidates") or [],
                known=await self._known(item),
                ordinal=int(item.input.get("round") or 1),
                tests=[command.text for command in toolchain.tests],
            )
            return self._reader(lease, prompt, audit.VERIFY_SCHEMA)
        raise ValueError(f"an audit has no step {item.name}")

    async def conclude(
        self, item: StepWork, commands: list[dict[str, Any]] | None, log: str
    ) -> Result:
        if not commands:
            return _failed("no_result", "The sandbox ended without a result.")
        if item.name == "prepare":
            base_sha, failure = Checkouts.read(commands)
            if failure is not None or base_sha is None:
                return failure or _failed("checkout_failed", "The checkout gave no commit.")
            first = "plan" if self._whole(item.run_data) else "audit"
            return Result(
                Verdict(True, "prepared", "Checked out and installed."),
                output={"base_sha": base_sha},
                follow=(Follow(first, FIRST, None),),
                data={"base_sha": base_sha},
            )
        if item.name == "plan":
            answer, instead = self._answer(item, commands, who="The planner", retry=FIRST)
            if answer is None:
                return instead or _failed("no_answer", "The planner gave no answer.")
            planned = audit.areas(answer)
            summary = " ".join(str(answer.get("summary") or "").split())[:500]
            output = {
                "summary": str(answer.get("summary") or "")[:20_000],
                "areas": planned,
                "left_out": [str(entry)[:300] for entry in (answer.get("left_out") or [])[:60]],
            }
            if not planned:
                # Nothing to split: the repository is audited as one scope after all.
                return Result(
                    Verdict(True, "one_area", f"No areas were named: {summary}"),
                    output=output,
                    follow=(Follow("audit", FIRST, None),),
                )
            return Result(
                Verdict(True, "planned", f"{len(planned)} area(s): {summary}"),
                output=output,
                follow=(Follow("audit", FIRST, {"area": 0}),),
                data={"areas": planned},
            )
        area = self._area_of(item)
        if item.name == "audit":
            answer, instead = self._answer(item, commands, who="The auditor", retry=FIRST)
            if answer is None:
                return instead or _failed("no_answer", "The auditor gave no answer.")
            summary = " ".join(str(answer.get("summary") or "").split())[:500]
            blocked = answer.get("decision") == "BLOCKED"
            if blocked and area is None:
                return Result(
                    Verdict(False, "blocked", f"The auditor could not audit the scope: {summary}"),
                    output=answer,
                )
            candidates = [] if blocked else audit.candidates(answer)
            audited: dict[str, Any] = {
                "area": area,
                "blocked": blocked,
                "summary": str(answer.get("summary") or "")[:20_000],
                "covered": [str(entry)[:300] for entry in (answer.get("covered") or [])[:60]],
                "not_covered": [
                    str(entry)[:300] for entry in (answer.get("not_covered") or [])[:60]
                ],
                "open_questions": [
                    str(entry)[:1_000] for entry in (answer.get("open_questions") or [])[:30]
                ],
                "candidates": candidates,
            }
            if blocked:
                # One area that could not be audited does not end an audit of the whole.
                return Result(
                    Verdict(True, "area_blocked", f"This area could not be audited: {summary}"),
                    output=audited,
                    follow=self._after_area(item),
                )
            if not candidates:
                return Result(
                    Verdict(True, "no_findings", summary),
                    output=audited,
                    follow=self._after_area(item),
                )
            given: dict[str, Any] = {"round": 1}
            if area is not None:
                given["area"] = area
            return Result(
                Verdict(True, "candidates", f"{len(candidates)} candidate finding(s): {summary}"),
                output=audited,
                follow=(Follow("verify", _other(item.provider or ""), given),),
                data={"auditor": item.provider},
            )
        if item.name == "verify":
            answer, instead = self._answer(item, commands, who="The verifier", retry=item.provider)
            if answer is None:
                return instead or _failed("no_answer", "The verifier gave no answer.")
            ordinal = int(item.input.get("round") or 1)
            summary = " ".join(str(answer.get("summary") or "").split())[:500]
            if ordinal < len(audit.ROUNDS):
                again: dict[str, Any] = {"round": ordinal + 1}
                if area is not None:
                    again["area"] = area
                follow: tuple[Follow, ...] = (Follow("verify", item.provider, again),)
            else:
                follow = self._after_area(item)
            return Result(
                Verdict(True, "verified", f"Round {ordinal}: {summary}"),
                output={"round": ordinal, "area": area, **answer},
                follow=follow,
            )
        return _failed("unknown", f"An audit has no step {item.name}.")

    async def done(self, item: StepWork) -> End:
        data = item.run_data
        audits = await asyncio.to_thread(self.runs.step_outputs, item.run_id, "audit")
        if not audits:
            return End("failed", "unfinished", "The run ended without an audit.")
        plans = await asyncio.to_thread(self.runs.step_outputs, item.run_id, "plan")
        planned = data.get("areas") or []
        rounds = await asyncio.to_thread(self.runs.step_outputs, item.run_id, "verify")
        # The latest audit of each area: a step given again to another account answers
        # once more for the same area.
        by_area: dict[int | None, dict[str, Any]] = {}
        for output in audits:
            by_area[output.get("area")] = output
        findings: list[dict[str, Any]] = []
        areas: list[dict[str, Any]] = []
        counts = {"CONFIRMED": 0, "REJECTED": 0, "FLAKY": 0, "BLOCKED": 0, "DUPLICATE": 0}
        number = 0
        total = 0
        for area, audited in sorted(by_area.items(), key=lambda entry: entry[0] or 0):
            by_round = {
                int(answer.get("round") or 0): answer
                for answer in rounds
                if answer.get("area") == area
            }
            answers = [by_round[ordinal] for ordinal in audit.ROUNDS if ordinal in by_round]
            name = planned[area]["name"] if area is not None and area < len(planned) else None
            confirmed = 0
            for local, finding in enumerate(audited.get("candidates") or [], start=1):
                number += 1
                total += 1
                tried = audit.attempts(answers, local)
                verdict = audit.disposition(tried)
                entry: dict[str, Any] = {
                    "number": number,
                    "title": finding["title"],
                    "severity": finding["severity"],
                    "lens": finding["lens"],
                    "area": name,
                    "disposition": verdict,
                    "notes": [
                        f"Round {index}: " + " ".join(str(t.get("note") or "").split())[:600]
                        for index, t in enumerate(tried, start=1)
                    ],
                    "duplicate_of": next(
                        (str(t["duplicate_of"])[:300] for t in tried if t.get("duplicate_of")),
                        None,
                    ),
                    "item_id": None,
                }
                if verdict == "CONFIRMED":
                    made = await asyncio.to_thread(
                        functools.partial(
                            self.backlog.produce,
                            data["project"],
                            title=finding["title"],
                            section=data.get("section"),
                            description=audit.description(finding, tried, data["base_sha"]),
                            facts={
                                "kind": "finding",
                                "severity": finding["severity"],
                                "lens": finding["lens"],
                                "confidence": finding["confidence"],
                                "component": finding["affected_component"],
                                "files": finding["files"][:20],
                                "commit": data["base_sha"],
                            },
                            proposed_by="audit",
                            origin_ref=f"run:{item.run_id}:finding:{number}",
                            reason=f"Found and verified by the audit of {data['base_sha'][:12]}.",
                            run_id=item.run_id,
                        )
                    )
                    if made is None:
                        # The backlog already has it, from this run or under the same title.
                        verdict = entry["disposition"] = "DUPLICATE"
                    else:
                        entry["item_id"] = made["id"]
                        confirmed += 1
                counts[verdict] += 1
                findings.append(entry)
            if area is not None:
                areas.append(
                    {
                        "name": name,
                        "paths": (planned[area]["paths"] if area < len(planned) else [])[:12],
                        "summary": " ".join(str(audited.get("summary") or "").split())[:600],
                        "blocked": bool(audited.get("blocked")),
                        "candidates": len(audited.get("candidates") or []),
                        "confirmed": confirmed,
                    }
                )

        def gathered(key: str, limit: int) -> list[str]:
            seen: list[str] = []
            for output in by_area.values():
                for value in output.get(key) or []:
                    if value not in seen:
                        seen.append(value)
            return seen[:limit]

        planned_areas = [str(entry.get("name")) for entry in planned]
        missed = [name for name in planned_areas if name not in {a["name"] for a in areas}]
        plan = plans[-1] if plans else {}
        result = {
            "summary": plan.get("summary") if planned else next(iter(by_area.values()))["summary"],
            "covered": gathered("covered", 80),
            "not_covered": (
                gathered("not_covered", 60)
                + [str(entry) for entry in plan.get("left_out") or []]
                + [f"The area {name}: not reached" for name in missed]
            )[:100],
            "open_questions": gathered("open_questions", 40),
            "findings": findings,
            "counts": counts,
            "areas": areas,
        }
        said = _counted(
            counts,
            {
                "CONFIRMED": "confirmed",
                "REJECTED": "not reproduced",
                "FLAKY": "reproduced once only",
                "BLOCKED": "could not be verified",
                "DUPLICATE": "already known",
            },
        )
        await self._close_request(
            item.run_id, {"audit_run": str(item.run_id), "confirmed": counts["CONFIRMED"]}
        )
        over = f" in {len(areas)} area(s)" if areas else ""
        if not total:
            return End(
                "succeeded",
                "no_findings",
                f"The audit found nothing{over}.",
                data={"result": result},
            )
        return End(
            "succeeded",
            "findings" if counts["CONFIRMED"] else "nothing_confirmed",
            f"{total} candidate finding(s){over}: {said}.",
            data={"result": result},
        )


class DiscoverEnhancements(_Producer):
    """Check out, discover, review; approved proposals become proposed items."""

    async def plan(self, item: StepWork, lease: Lease | None) -> Plan:
        data = item.run_data
        if item.name == "prepare":
            return await self._prepare(item, "discovery")
        if item.name == "discover":
            prompt = discover.discoverer_prompt(
                repository=data["repository"],
                base_sha=data["base_sha"],
                scope=data.get("scope") or {},
                known=await self._known(item),
            )
            return self._reader(lease, prompt, discover.DISCOVER_SCHEMA)
        if item.name == "review":
            found = await asyncio.to_thread(self.runs.step_outputs, item.run_id, "discover")
            prompt = discover.reviewer_prompt(
                repository=data["repository"],
                base_sha=data["base_sha"],
                proposals=(found[-1] if found else {}).get("proposals") or [],
                known=await self._known(item),
            )
            return self._reader(lease, prompt, discover.REVIEW_SCHEMA)
        raise ValueError(f"a search for enhancements has no step {item.name}")

    async def conclude(
        self, item: StepWork, commands: list[dict[str, Any]] | None, log: str
    ) -> Result:
        if not commands:
            return _failed("no_result", "The sandbox ended without a result.")
        if item.name == "prepare":
            base_sha, failure = Checkouts.read(commands)
            if failure is not None or base_sha is None:
                return failure or _failed("checkout_failed", "The checkout gave no commit.")
            return Result(
                Verdict(True, "prepared", "Checked out and installed."),
                output={"base_sha": base_sha},
                follow=(Follow("discover", FIRST, None),),
                data={"base_sha": base_sha},
            )
        if item.name == "discover":
            answer, instead = self._answer(item, commands, who="The discoverer", retry=FIRST)
            if answer is None:
                return instead or _failed("no_answer", "The discoverer gave no answer.")
            proposals = discover.proposals(answer)
            summary = " ".join(str(answer.get("summary") or "").split())[:500]
            output = {"summary": str(answer.get("summary") or "")[:20_000], "proposals": proposals}
            if not proposals:
                return Result(Verdict(True, "nothing_proposed", summary), output=output)
            return Result(
                Verdict(True, "proposed", f"{len(proposals)} proposal(s): {summary}"),
                output=output,
                follow=(Follow("review", _other(item.provider or ""), None),),
                data={"discoverer": item.provider},
            )
        if item.name == "review":
            answer, instead = self._answer(item, commands, who="The reviewer", retry=item.provider)
            if answer is None:
                return instead or _failed("no_answer", "The reviewer gave no answer.")
            summary = " ".join(str(answer.get("summary") or "").split())[:500]
            return Result(Verdict(True, "reviewed", summary), output=answer)
        return _failed("unknown", f"A search for enhancements has no step {item.name}.")

    async def done(self, item: StepWork) -> End:
        data = item.run_data
        found = await asyncio.to_thread(self.runs.step_outputs, item.run_id, "discover")
        if not found:
            return End("failed", "unfinished", "The run ended without a search.")
        proposals = found[-1].get("proposals") or []
        reviews = await asyncio.to_thread(self.runs.step_outputs, item.run_id, "review")
        review = reviews[-1] if reviews else {}
        listed: list[dict[str, Any]] = []
        counts = {
            "APPROVE": 0,
            "REQUEST_CHANGES": 0,
            "BLOCKED": 0,
            "DUPLICATE": 0,
            "NOT_REVIEWED": 0,
        }
        for number, proposal in enumerate(proposals, start=1):
            judged = discover.review_of(review, number)
            verdict = discover.verdict(judged)
            entry: dict[str, Any] = {
                "number": number,
                "title": proposal["title"],
                "risk": proposal["risk"],
                "persona": proposal["persona"],
                "disposition": verdict,
                "notes": [" ".join(str((judged or {}).get("summary") or "").split())[:600]],
                "duplicate_of": str((judged or {}).get("duplicate_of") or "")[:300] or None,
                "item_id": None,
            }
            if verdict == "APPROVE" and judged is not None:
                made = await asyncio.to_thread(
                    functools.partial(
                        self.backlog.produce,
                        data["project"],
                        title=proposal["title"],
                        section=data.get("section"),
                        description=discover.description(proposal, judged, data["base_sha"]),
                        facts={
                            "kind": "enhancement",
                            "risk": proposal["risk"],
                            "persona": proposal["persona"],
                            "commit": data["base_sha"],
                        },
                        proposed_by="discovery",
                        origin_ref=f"run:{item.run_id}:enhancement:{number}",
                        reason="Proposed by a search for enhancements and approved in review.",
                        run_id=item.run_id,
                    )
                )
                if made is None:
                    verdict = entry["disposition"] = "DUPLICATE"
                else:
                    entry["item_id"] = made["id"]
            counts[verdict] += 1
            listed.append(entry)
        result = {"summary": found[-1].get("summary"), "proposals": listed, "counts": counts}
        if not proposals:
            return End(
                "succeeded",
                "nothing_proposed",
                "The search proposed nothing.",
                data={"result": result},
            )
        said = _counted(
            counts,
            {
                "APPROVE": "wait for your approval",
                "REQUEST_CHANGES": "would need changes",
                "BLOCKED": "stopped in review",
                "DUPLICATE": "already known",
                "NOT_REVIEWED": "not reviewed",
            },
        )
        return End(
            "succeeded",
            "proposed" if counts["APPROVE"] else "nothing_approved",
            f"{len(proposals)} proposal(s): {said}.",
            data={"result": result},
        )


def _counted(counts: dict[str, int], words: dict[str, str]) -> str:
    return ", ".join(f"{counts[key]} {word}" for key, word in words.items() if counts[key])
