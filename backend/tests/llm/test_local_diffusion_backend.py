"""Local diffusion generation adapter result contracts."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from leagent.llm.generation.backends.local_diffusion import LocalDiffusionBackend


def _patch_adapter(monkeypatch: pytest.MonkeyPatch, result: SimpleNamespace) -> None:
    from leagent.llm.domain_models.diffusion import adapter

    class _Adapter:
        async def invoke(self, **_params: object) -> SimpleNamespace:
            return result

    monkeypatch.setattr(adapter, "DiffusersTxt2ImgAdapter", _Adapter)


@pytest.mark.asyncio
async def test_local_diffusion_decodes_valid_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"image-bytes"
    _patch_adapter(
        monkeypatch,
        SimpleNamespace(
            success=True,
            b64_data=base64.b64encode(payload).decode("ascii"),
            url=None,
            mime="image/png",
            model="local-model",
            metadata={},
        ),
    )

    result = await LocalDiffusionBackend().generate(kind="image", prompt="test")

    assert result.success is True
    assert result.data == payload


@pytest.mark.asyncio
async def test_local_diffusion_rejects_invalid_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_adapter(
        monkeypatch,
        SimpleNamespace(
            success=True,
            b64_data="not base64!",
            url=None,
            mime="image/png",
            model="local-model",
            metadata={},
        ),
    )

    result = await LocalDiffusionBackend().generate(kind="image", prompt="test")

    assert result.success is False
    assert "invalid base64" in str(result.error)
