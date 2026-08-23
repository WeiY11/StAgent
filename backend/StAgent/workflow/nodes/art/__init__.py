"""Game-art node pack — first-class generation + asset nodes.

Packaged as a :class:`NodeExtension` (ComfyUI ``ComfyExtension`` analogue)
so the loader installs the whole bundle in one step. Every node here is
hand-authored with typed media sockets; assets travel by reference via
:class:`~StAgent.workflow.io.media.MediaRef`.
"""

from __future__ import annotations

from StAgent.workflow.nodes.base import WorkflowNode
from StAgent.workflow.nodes.extension import NodeExtension

from .control_nodes import (
    ART_CONTROL_NODES,
    CameraControlNode,
    PoseControlNode,
)
from .nodes import (
    ART_GENERATION_NODES,
    ImageGenNode,
    Mesh3DNode,
    UpscaleNode,
    VFXGenNode,
    VideoGenNode,
)
from .quality_critic import QualityCriticNode
from .storyboard import (
    STORYBOARD_NODES,
    ShotNode,
    StoryboardNode,
)

#: Art-side evaluation nodes (perceptual / LLM scoring for the gate).
ART_EVAL_NODES: list[type[WorkflowNode]] = [QualityCriticNode]

#: All node classes contributed by the art pack (generation + control + eval).
ART_NODES: list[type[WorkflowNode]] = [
    *ART_GENERATION_NODES,
    *ART_CONTROL_NODES,
    *STORYBOARD_NODES,
    *ART_EVAL_NODES,
]


class ArtNodeExtension(NodeExtension):
    """The game-art node bundle."""

    name = "StAgent-art"
    version = "1.0.0"

    async def get_node_list(self) -> list[type[WorkflowNode]]:
        return list(ART_NODES)


async def StAgent_entrypoint() -> NodeExtension:
    return ArtNodeExtension()


__all__ = [
    "ART_CONTROL_NODES",
    "ART_EVAL_NODES",
    "ART_GENERATION_NODES",
    "ART_NODES",
    "ArtNodeExtension",
    "CameraControlNode",
    "ImageGenNode",
    "Mesh3DNode",
    "PoseControlNode",
    "QualityCriticNode",
    "STORYBOARD_NODES",
    "ShotNode",
    "StoryboardNode",
    "UpscaleNode",
    "VFXGenNode",
    "VideoGenNode",
    "StAgent_entrypoint",
]
