"""Vendor HTTP clients for media generation (image providers)."""

from StAgent.llm.generation.providers.base import ImageGenResult, ProviderResult
from StAgent.llm.generation.providers.dashscope import DashScopeWanxProvider
from StAgent.llm.generation.providers.openai import OpenAIImageGenProvider
from StAgent.llm.generation.providers.siliconflow import (
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    SILICONFLOW_SIZE_CATALOG,
    SiliconFlowImageProvider,
    SiliconFlowModelFamily,
    build_payload,
    match_model_family,
    snap_image_size,
)

__all__ = [
    "DEFAULT_ENDPOINT",
    "DEFAULT_MODEL",
    "SILICONFLOW_SIZE_CATALOG",
    "DashScopeWanxProvider",
    "ImageGenResult",
    "OpenAIImageGenProvider",
    "ProviderResult",
    "SiliconFlowImageProvider",
    "SiliconFlowModelFamily",
    "build_payload",
    "match_model_family",
    "snap_image_size",
]
