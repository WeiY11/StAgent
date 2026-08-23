from __future__ import annotations

from StAgent.context.cache import SourceCache
from StAgent.context.types import ContextBlock, ContextScope, RenderTarget


def _block(sid="test"):
    return ContextBlock(
        source_id=sid,
        kind="identity",
        render_target=RenderTarget.SYSTEM,
        body="hello",
        tokens=2,
        cost=5,
        signature="sig",
        priority=500,
        weight=1.0,
    )


def test_put_get_session():
    cache = SourceCache()
    cache.put("k1", _block(), ContextScope.SESSION)
    assert cache.get("k1", ContextScope.SESSION) is not None
    assert cache.stats["hits"] == 1


def test_turn_scope_never_cached():
    cache = SourceCache()
    cache.put("k1", _block(), ContextScope.TURN)
    assert cache.get("k1", ContextScope.TURN) is None


def test_source_cache_is_bounded() -> None:
    cache = SourceCache(max_size=2)
    cache.put("a", _block("a"), ContextScope.SESSION)
    cache.put("b", _block("b"), ContextScope.SESSION)
    cache.put("c", _block("c"), ContextScope.SESSION)
    assert cache.stats["size"] == 2
    assert cache.get("a", ContextScope.SESSION) is None


def test_turn_scope_is_never_inserted() -> None:
    cache = SourceCache()
    cache.put("k", _block(), ContextScope.TURN)
    assert cache.stats["size"] == 0
