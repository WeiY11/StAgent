"""CLI HTTP client configuration contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from leagent.cli.http import DEFAULT_BASE_URL, CLIHttpClient

if TYPE_CHECKING:
    import pytest


def test_cli_http_uses_default_when_environment_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEAGENT_API_URL", "")
    client = CLIHttpClient()

    assert client.base_url == DEFAULT_BASE_URL
    assert isinstance(client.client, httpx.Client)
    client.close()


def test_cli_http_explicit_base_url_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEAGENT_API_URL", "http://environment.invalid")
    client = CLIHttpClient(base_url="http://explicit.invalid")

    assert client.base_url == "http://explicit.invalid"
    assert client._build_url("api/v1/health") == "http://explicit.invalid/api/v1/health"
