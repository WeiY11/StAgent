import pytest
from pydantic import ValidationError

from StAgent.config.settings import CacheSettings
from StAgent.services.cache.keys import build_scoped_cache_key


def test_scoped_key_is_stable_for_canonical_vary_order() -> None:
    left = build_scoped_cache_key(
        "messages", owner_id="u1", resource_id="s1", generation=7,
        vary={"limit": 50, "before": None},
    )
    right = build_scoped_cache_key(
        "messages", owner_id="u1", resource_id="s1", generation=7,
        vary={"before": None, "limit": 50},
    )
    assert left == right


def test_scoped_key_isolates_owner_resource_and_generation() -> None:
    base = build_scoped_cache_key("messages", owner_id="u1", resource_id="s1")
    assert base != build_scoped_cache_key("messages", owner_id="u2", resource_id="s1")
    assert base != build_scoped_cache_key("messages", owner_id="u1", resource_id="s2")
    assert base != build_scoped_cache_key(
        "messages", owner_id="u1", resource_id="s1", generation=1
    )


def test_cache_settings_reject_more_shards_than_items() -> None:
    with pytest.raises(ValidationError):
        CacheSettings(max_items=2, shard_count=4)
