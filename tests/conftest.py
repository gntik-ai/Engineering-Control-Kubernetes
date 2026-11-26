"""What every test has: the maker of the tests' stand-in accounts is one a project's
texts may be sent to by default, as Anthropic, OpenAI and Google are for real accounts."""

from typing import Any

import pytest

from engineering_control.dashboard import review_settings


@pytest.fixture(autouse=True)
def _texts_go_to_the_test_maker(monkeypatch: Any) -> None:
    monkeypatch.setattr(review_settings, "MAKERS", review_settings.MAKERS | {"fixture"})
