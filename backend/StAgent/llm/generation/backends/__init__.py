"""Generation backends — Strategy implementations for media production."""

from StAgent.llm.generation.backends.configured import ConfiguredGenerationBackend
from StAgent.llm.generation.backends.dashscope_image import DashScopeImageBackend
from StAgent.llm.generation.backends.elevenlabs import ElevenLabsBackend
from StAgent.llm.generation.backends.http import (
    HttpMesh3DBackend,
    HttpUpscaleBackend,
    HttpVfxBackend,
    HttpVideoBackend,
)
from StAgent.llm.generation.backends.local_diffusion import LocalDiffusionBackend
from StAgent.llm.generation.backends.offline import OfflineGenerationBackend
from StAgent.llm.generation.backends.openai_image import ImageProviderBackend, OpenAIImageBackend
from StAgent.llm.generation.backends.replicate import ReplicateBackend
from StAgent.llm.generation.backends.siliconflow import SiliconFlowImageBackend

__all__ = [
    "ConfiguredGenerationBackend",
    "DashScopeImageBackend",
    "ElevenLabsBackend",
    "HttpMesh3DBackend",
    "HttpUpscaleBackend",
    "HttpVfxBackend",
    "HttpVideoBackend",
    "ImageProviderBackend",
    "LocalDiffusionBackend",
    "OfflineGenerationBackend",
    "OpenAIImageBackend",
    "ReplicateBackend",
    "SiliconFlowImageBackend",
]
