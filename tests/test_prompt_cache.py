"""Prompt caching: what the questions of a run share comes first, a provider that keeps
what it read is asked to where that pays, and what was kept and read again is counted."""

# ruff: noqa: S105

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest

from engineering_control.engine import asking
from engineering_control.engine.catalogue import TASKS
from engineering_control.models import anthropic_api, openai_style
from engineering_control.models.providers import BY_KEY, cost_of
from engineering_control.models.service import (
    KEEP_CHARACTERS,
    TRIAL_LINES,
    ModelError,
    Question,
    marks,
)
from engineering_control.models.wire import Wire
from tests.test_ask_a_model import (  # noqa: F401 - ``provider`` is a fixture
    PASSED,
    _answered,
    _api_account,
    _collected,
    _console,
    _judging,
    _post,
    _step,
    provider,
)
from tests.test_dashboard_accounts import FakeCluster
from tests.test_models_api import (  # noqa: F401 - ``asked`` is a fixture
    ANTHROPIC_KEY,
    OTHER_KEY,
    SHAPE,
    Anthropic,
    Asked,
    Provider,
    asked,
    question,
    said,
    streamed,
)
from tests.test_sandbox_delivery import world  # noqa: F401 - a fixture

# --- what a provider is sent, and what it says it kept ---------------------------------------


def test_a_prompt_is_cut_into_pieces_only_where_something_is_to_be_kept() -> None:
    prompt = "about the run\n\nfile one\n\nfile two\n\nthe question"
    cuts = (15, 25, 35)
    # Nothing to keep: one text, as ever.
    assert anthropic_api.content(prompt) == prompt
    assert anthropic_api.content(prompt, cuts) == prompt
    assert anthropic_api.content(prompt, cuts, keep=(0, 999)) == prompt
    blocks = anthropic_api.content(prompt, cuts, keep=(15, 35))
    assert isinstance(blocks, list)
    assert "".join(block["text"] for block in blocks) == prompt, "not a character is changed"
    assert [block["text"] for block in blocks] == [
        "about the run\n\n",
        "file one\n\n",
        "file two\n\n",
        "the question",
    ]
    kept = [block.get("cache_control") for block in blocks]
    assert kept == [{"type": "ephemeral"}, None, {"type": "ephemeral"}, None]
    hour = anthropic_api.content(prompt, cuts, keep=(35,), hour=True)
    assert isinstance(hour, list)
    assert [block.get("cache_control") for block in hour] == [
        None,
        None,
        {"type": "ephemeral", "ttl": "1h"},
        None,
    ]
    # A mark where no cut was named still ends a piece there.
    odd = anthropic_api.content(prompt, (), keep=(15,))
    assert isinstance(odd, list) and [len(block["text"]) for block in odd] == [15, len(prompt) - 15]


@pytest.mark.asyncio
async def test_anthropic_is_sent_the_marks_and_says_what_it_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anthropic_api, "RETRIES", 0)
    hub = Anthropic()
    prompt = "the material\n\nthe question"
    wire = await anthropic_api.complete(
        key=ANTHROPIC_KEY,
        base_url="https://api.anthropic.com",
        model="claude-haiku-5-5",
        system="The rules.",
        prompt=prompt,
        schema=SHAPE,
        max_tokens=4000,
        seconds=60,
        http=hub.factory,
        cuts=(14,),
        keep=(14,),
    )
    body = hub.asked[-1][2]
    assert body is not None
    assert body["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "the material\n\n",
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": "the question"},
            ],
        }
    ]
    assert body["system"] == "The rules."
    # Read new 900 and kept 100 of what was read; 300 came from the cache.
    assert (wire.read, wire.cache_written, wire.cached, wire.written) == (1000, 100, 300, 60)


def test_a_chat_completion_says_what_was_kept_under_either_name() -> None:
    def answer(details: dict[str, int]) -> dict[str, Any]:
        return {
            "model": "m",
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 10,
                "prompt_tokens_details": details,
            },
        }

    plain = openai_style.read("X", answer({"cached_tokens": 600}), "m", 100)
    assert (plain.read, plain.cached, plain.cache_written) == (400, 600, 0)
    openai = openai_style.read(
        "X", answer({"cached_tokens": 0, "cache_write_tokens": 900}), "m", 100
    )
    assert (openai.read, openai.cached, openai.cache_written) == (1000, 0, 900)
    qwen = openai_style.read(
        "X", answer({"cached_tokens": 200, "cache_creation_input_tokens": 700}), "m", 100
    )
    assert (qwen.read, qwen.cached, qwen.cache_written) == (800, 200, 700)
    # Never more than was read new.
    odd = openai_style.read(
        "X", answer({"cached_tokens": 900, "cache_write_tokens": 500}), "m", 100
    )
    assert (odd.read, odd.cache_written) == (100, 100)


def test_what_was_kept_costs_more_where_the_provider_says_so_and_a_batch_costs_a_part() -> None:
    prices = {"m": {"input": 4, "output": 20, "cached": 0.2}}
    used = {"input": 1000, "output": 100, "cached": 5000, "cache_write": 800}
    plain = (1000 * 4 + 100 * 20 + 5000 * 0.2) / 1e6
    assert cost_of(prices, "m", used) == round(plain, 6)
    kept = plain + 800 * 4 * 0.25 / 1e6
    assert cost_of(prices, "m", used, write=1.25) == round(kept, 6)
    assert cost_of(prices, "m", used, write=1.25, part=0.5) == round(kept / 2, 6)
    assert cost_of(prices, "other", used) is None
    assert cost_of(prices, "m", {"input": 10, "output": 1}) == round((40 + 20) / 1e6, 6)
    # Who caches how, and what a batch costs there.
    assert BY_KEY["anthropic"].cache == "marked" and BY_KEY["anthropic"].cache_write == (1.25, 2.0)
    for key in ("openai", "google", "deepseek", "qwen", "glm", "kimi"):
        assert BY_KEY[key].cache == "automatic" and BY_KEY[key].cache_write == (1.0, 1.0), key
    shown = BY_KEY["anthropic"].as_dict()
    assert shown["cache"] == "marked" and "batch" in shown


def test_only_a_provider_that_keeps_what_is_marked_is_sent_marks_and_never_for_little() -> None:
    long = KEEP_CHARACTERS + 500
    asks = Question(
        choices=(("anthropic", "m"),),
        system="s",
        prompt="x" * (long + 50),
        schema=SHAPE,
        cuts=(100, long),
        keep=(100, long),
        shared=long,
        cache="short",
    )
    # What is too short to be kept by any provider is not marked: it would only cost.
    assert marks(BY_KEY["anthropic"], asks) == (long,)
    assert marks(BY_KEY["openai"], asks) == (), "it keeps by itself what it reads"
    assert marks(BY_KEY["anthropic"], Question(**{**asks.__dict__, "cache": "none"})) == ()
    assert marks(BY_KEY["anthropic"], Question(**{**asks.__dict__, "cache": "hour"})) == (long,)


# --- the service -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_stays_with_the_account_it_asked_before(asked: Asked) -> None:  # noqa: F811
    first = asked.account("DeepSeek one", "deepseek")
    second = asked.account("DeepSeek two", "deepseek", key="sk-" + "z8" * 24)
    # Left to itself, the account unused longest: the first, then the second.
    assert (await asked.models.ask(question())).account_id == first
    assert (await asked.models.ask(question())).account_id == second
    # A provider's cache belongs to one account: a question that names the account
    # the run asked before goes there, though the other was unused longer.
    stays = Question(**{**question().__dict__, "prefer": second})
    for _ in range(3):
        reply = await asked.models.ask(stays)
        assert reply.account_id == second and reply.usage["account_id"] == str(second)
    # An account that cannot ask is not waited for: the other one answers.
    asked.accounts.update(second, paused=True)
    assert (await asked.models.ask(stays)).account_id == first


@pytest.mark.asyncio
async def test_what_a_question_kept_is_counted_and_costed(asked: Asked) -> None:  # noqa: F811
    account = asked.account("Anthropic one", "anthropic", key=ANTHROPIC_KEY)
    asked.accounts.update(
        account, prices={"claude-opus-5-5": {"input": 4, "output": 20, "cached": 0.2}}
    )
    long = KEEP_CHARACTERS + 100
    asks = Question(
        choices=(("anthropic", "claude-opus-5-5"),),
        system="The rules.",
        prompt="m" * long + "The question.",
        schema=SHAPE,
        cuts=(long,),
        keep=(long,),
        shared=long,
        cache="hour",
    )
    reply = await asked.models.ask(asks)
    body = asked.anthropic.asked[-1][2]
    assert body is not None
    blocks = body["messages"][0]["content"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # The shape of the answer is said after the question, where nothing is kept.
    assert blocks[1]["text"].startswith("The question.") and "JSON object" in blocks[1]["text"]
    # 1,000 read of which 100 kept, at twice the price for an hour; 300 from the cache.
    assert reply.usage["cache_write"] == 100 and reply.usage["cached"] == 300
    assert reply.usage["cost"] == round((1000 * 4 + 100 * 4 * 1.0 + 60 * 20 + 300 * 0.2) / 1e6, 6)
    # Asked to keep nothing, nothing is marked.
    await asked.models.ask(Question(**{**asks.__dict__, "cache": "none"}))
    plain = asked.anthropic.asked[-1][2]
    assert plain is not None and isinstance(plain["messages"][0]["content"], str)


def _kept(read: int, cached: int, name: str = "entries") -> httpx.Response:
    return said(
        {name: TRIAL_LINES},
        usage={
            "prompt_tokens": read + cached,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": cached},
        },
    )


@pytest.mark.asyncio
async def test_trying_a_cache_asks_until_the_beginning_is_read_from_it(asked: Asked) -> None:  # noqa: F811
    account = asked.account("DeepSeek one", "deepseek")
    asked.provider.answers = [
        _kept(9000, 0),
        _kept(9000, 0),
        _kept(300, 8700),
        _kept(300, 8700, "count"),
    ]
    trial = await asked.models.try_cache(account, "model-a")
    assert (trial["works"], trial["with_another_shape"], trial["how"]) == (True, True, "automatic")
    assert [entry["cached"] for entry in trial["asked"]] == [0, 0, 8700, 8700]
    assert trial["asked"][-1]["what"] == "the same beginning, another shape of answer"
    assert asked.waited == [3.0, 3.0], "a moment between two tries, none after the one that read"
    sent = [entry[2]["messages"][1]["content"] for entry in asked.provider.asked[-4:]]
    # The same text each time, and a beginning no question had before this trial.
    assert sent[0] == sent[1] == sent[2] and sent[3] != sent[0]
    assert sent[0].count("\n") > TRIAL_LINES and len(sent[0]) > 30_000
    again = await asked.models.try_cache(account, "model-a")
    assert (
        again["asked"]
        and asked.provider.asked[-1][2]["messages"][1]["content"][:200] != sent[3][:200]
    )

    # A provider that keeps nothing is said to keep nothing, and nothing is hidden.
    asked.provider.answers = [_kept(9000, 0)] * 4
    nothing = await asked.models.try_cache(account, "model-a")
    assert (nothing["works"], nothing["with_another_shape"]) == (False, False)
    assert len(nothing["asked"]) == 4

    # A key that is refused is said, and the account waits for a new one.
    asked.provider.answers = [httpx.Response(401, json={"error": {"message": "Invalid key"}})]
    with pytest.raises(ModelError, match="does not accept the key") as refused:
        await asked.models.try_cache(account, "model-a")
    assert refused.value.label == "refused"
    assert asked.state(account)["state"] == "sign_in_required"


def test_the_console_tries_the_cache_of_an_account_with_a_key(tmp_path: Path) -> None:
    stand_in, fake = Provider(), FakeCluster()
    client = _console(tmp_path, stand_in, fake)
    try:
        account = _post(client, "", name="DeepSeek one", provider="deepseek").json()
        assert _post(client, f"/{account['id']}/sign-in", credential=OTHER_KEY).status_code == 200
        stand_in.answers = [_kept(9000, 0), _kept(200, 8800), _kept(9000, 0, "count")]
        tried = _post(client, f"/{account['id']}/cache-trial", model="model-a")
        assert tried.status_code == 200, tried.text
        shown = tried.json()
        assert (shown["account"], shown["model"], shown["works"]) == (
            "DeepSeek one",
            "model-a",
            True,
        )
        assert shown["with_another_shape"] is False and len(shown["asked"]) == 3
        assert OTHER_KEY not in tried.text
        # What is no model's name is not asked, and a subscription is not asked this way.
        assert _post(client, f"/{account['id']}/cache-trial", model="two words").status_code == 422
        claude = _post(client, "", name="Claude one", provider="claude").json()
        assert _post(client, f"/{claude['id']}/cache-trial", model="m").status_code == 422
    finally:
        client.__exit__(None, None, None)


# --- the questions of a run ------------------------------------------------------------------


def _asking(world: Any) -> Question:  # noqa: F811
    [waiting] = world.launcher._questions.values()
    found: Question = waiting
    return found


@pytest.mark.asyncio
async def test_what_a_run_shares_comes_first_and_the_question_last(
    world: Any,  # noqa: F811
    provider: Provider,  # noqa: F811
) -> None:
    first = _api_account(world)
    second = _api_account(world, name="DeepSeek two")
    provider.answers = [said(PASSED)]
    run = await _collected(world, _judging())
    await world.launcher.tick()
    asks = _asking(world)
    prompt = asks.prompt
    order = [
        prompt.index("The repository is gntik-ai/falcone-workflow."),
        prompt.index("What the run is about:"),
        prompt.index("The files the change touches:"),
        prompt.index("The change as a diff."),
        prompt.index("Judge what is asked below."),
        prompt.index("What you are asked:\nIs the change complete?"),
    ]
    assert order == sorted(order) and order[0] == 0
    # Where the question itself begins, and where each piece of what is shared does.
    assert prompt[asks.shared :].startswith("Judge what is asked below.")
    assert asks.cuts[-1] == asks.shared and list(asks.cuts) == sorted(asks.cuts)
    for cut in asks.cuts:
        assert prompt[:cut].endswith("\n\n"), "a piece ends where a part ends"
    # What the run is about never changes: it is kept apart from what does.
    steady = asks.keep[0]
    assert asks.keep == (steady, asks.shared)
    assert prompt[:steady].rstrip().endswith("material>>>")
    assert "The files the change touches" not in prompt[:steady]
    # One question in the flow: nothing is asked to be kept, whoever is asked.
    assert asks.cache == "none" and asks.prefer is None
    await _answered(world)
    judged = _step(world, run["id"], "judge")
    assert judged["usage"]["account_id"] == str(first)
    assert world.runs.asked_with(UUID(run["id"])) == {"deepseek": first}

    # Asked again in the same run, the question names the account it asked before.
    for wanted, kept in (("hour", "hour"), ("off", "none"), ("automatic", "none")):
        provider.answers = [said(PASSED)]
        run = await _collected(world, _judging(cache=wanted))
        await world.launcher.tick()
        assert _asking(world).cache == kept, wanted
        await _answered(world)
    assert second != first


@pytest.mark.asyncio
async def test_a_flow_that_asks_more_than_once_asks_for_what_it_read_to_be_kept(
    world: Any,  # noqa: F811
    provider: Provider,  # noqa: F811
) -> None:
    first = _api_account(world)
    _api_account(world, name="DeepSeek two")
    raw = _judging()
    by_id = {task["id"]: task for task in raw["tasks"]}
    # A second question after the first: a report, before the administrator decides.
    by_id["judge"]["next"] = {"pass": "tell", "changes": "implement"}
    raw["tasks"].append(
        {
            "id": "tell",
            "task": "model.report",
            "with": {
                "provider": "deepseek",
                "model": "model-a",
                "instruction": "Say what changed.",
            },
            "next": {"done": "approve"},
        }
    )
    provider.answers = [said(PASSED), said({"summary": "s", "report": "The change, told."})]
    run = await _collected(world, raw)
    await world.launcher.tick()
    judge = _asking(world)
    assert judge.cache == "short" and judge.prefer is None
    await _answered(world)
    if not world.launcher._questions:
        await world.launcher.tick()
    tell = _asking(world)
    # The run stays with the account that answered: its cache is there.
    assert tell.cache == "short" and tell.prefer == first
    # And the second question begins as the first did, up to the change.
    assert tell.prompt[: judge.shared] == judge.prompt[: judge.shared]
    assert "Notes of the earlier task" not in tell.prompt[: judge.shared]
    await _answered(world)
    assert _step(world, run["id"], "tell")["usage"]["account_id"] == str(first)
    sent = provider.asked[-1][2]
    assert sent is not None
    # A provider that keeps what it reads by itself is sent one text, with no marks.
    assert (
        sent["messages"][1]["content"]
        == tell.prompt + sent["messages"][1]["content"][len(tell.prompt) :]
    )
    assert "cache_control" not in json.dumps(sent)


def test_every_task_that_asks_a_model_says_whether_what_it_read_is_kept() -> None:
    for task in TASKS.values():
        named = [param for param in task.params if param.name == "cache"]
        # Summaries are of different files each time: there is nothing to read again.
        assert bool(named) == (task.where == "model" and task.key != "model.summarise"), task.key
        if named:
            assert named[0].choices == ("automatic", "off", "hour")
            assert named[0].default == "automatic"


def test_what_happened_in_a_loop_only_grows_and_is_cut_a_lot_at_a_time() -> None:
    ctx: Any = SimpleNamespace(node=SimpleNamespace(id="decide"))

    def steps(count: int) -> list[dict[str, Any]]:
        return [
            {
                "name": "decide" if number % 2 == 0 else "look",
                "outcome": "ok",
                "detail": f"step {number}",
            }
            for number in range(count)
        ]

    assert asking._so_far(ctx, [{"name": "prepare"}]) == "Nothing yet: this is the first step."
    whole = asking._so_far(ctx, steps(80))
    assert len(whole.splitlines()) == 80 and whole.startswith("1. decide chose step 0")
    # Each step adds a line and changes none before it.
    for count in range(2, 80):
        assert asking._so_far(ctx, steps(count + 1)).startswith(asking._so_far(ctx, steps(count)))
    # Past eighty lines the oldest forty go at once, and then it only grows again.
    cut = asking._so_far(ctx, steps(81))
    assert cut.splitlines()[0] == "(40 earlier steps are not shown.)"
    assert cut.splitlines()[1] == "41. decide chose step 40" and len(cut.splitlines()) == 42
    for count in range(81, 120):
        assert asking._so_far(ctx, steps(count + 1)).startswith(asking._so_far(ctx, steps(count)))
    assert asking._so_far(ctx, steps(121)).splitlines()[0] == "(80 earlier steps are not shown.)"
    assert Wire("t", "m", 1, 1, 0).cache_written == 0
    assert streamed("{}") is not None
