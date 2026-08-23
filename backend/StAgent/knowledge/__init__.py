"""Knowledge layer — document chunk storage, retrieval, and vector indexing."""

from StAgent.knowledge.gc import GCReport, KnowledgeGarbageCollector
from StAgent.knowledge.vector_index import (
    KnowledgeVectorIndex,
    get_knowledge_vector_index,
    set_knowledge_vector_index,
)

__all__ = [
    "KnowledgeVectorIndex",
    "get_knowledge_vector_index",
    "set_knowledge_vector_index",
    "KnowledgeGarbageCollector",
    "GCReport",
]
