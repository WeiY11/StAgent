from __future__ import annotations

from types import SimpleNamespace

from tests.conftest import _live_tests_requested


class _ConfigStub:
    def __init__(self, *, markexpr: str = "", run_live: bool = False) -> None:
        self.option = SimpleNamespace(markexpr=markexpr)
        self._run_live = run_live

    def getoption(self, name: str, default: object = None) -> object:
        if name == "--run-live":
            return self._run_live
        return default


def test_live_tests_are_not_requested_by_default() -> None:
    assert _live_tests_requested(_ConfigStub()) is False  # type: ignore[arg-type]


def test_live_tests_are_requested_by_live_mark_expression() -> None:
    assert _live_tests_requested(_ConfigStub(markexpr="live")) is True  # type: ignore[arg-type]


def test_live_tests_are_not_requested_by_negative_live_mark_expression() -> None:
    assert _live_tests_requested(_ConfigStub(markexpr="not live")) is False  # type: ignore[arg-type]


def test_live_tests_are_requested_by_run_live_option() -> None:
    assert _live_tests_requested(_ConfigStub(run_live=True)) is True  # type: ignore[arg-type]
