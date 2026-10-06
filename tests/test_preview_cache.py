"""Short-lived previews are bounded in count and age, and no longer hold whole packages."""
import pytest

from tidoc.adapters.cache import PreviewCache
from tests.test_adapter_storage import storage, write_package  # noqa: F401  (fixture)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_oldest_previews_are_dropped_when_the_cache_is_full():
    cache = PreviewCache(max_items=3)
    for key in 'abcd':
        cache[key] = key.upper()
    assert 'a' not in cache and cache.get('a') is None
    assert [cache['b'], cache['c'], cache['d']] == ['B', 'C', 'D']
    assert len(cache) == 3
    cache['b'] = 'again'          # storing again makes it the newest, so 'c' goes next
    cache['e'] = 'E'
    assert 'c' not in cache and cache['b'] == 'again'


def test_previews_expire_after_their_lifetime():
    clock = Clock()
    cache = PreviewCache(ttl_seconds=60, clock=clock)
    cache['plan'] = {'big': 'context'}
    clock.now = 59
    assert 'plan' in cache
    clock.now = 61
    assert 'plan' not in cache and cache.get('plan', 'gone') == 'gone'
    with pytest.raises(KeyError):
        cache['plan']
    assert len(cache) == 0


def test_pop_returns_and_forgets_the_preview():
    cache = PreviewCache()
    cache['x'] = 1
    assert cache.pop('x') == 1
    assert cache.pop('x', 'missing') == 'missing'


def test_invalid_limits_are_rejected():
    with pytest.raises(ValueError):
        PreviewCache(max_items=0)
    with pytest.raises(ValueError):
        PreviewCache(ttl_seconds=0)


def test_package_inspection_previews_keep_only_a_hash_and_are_bounded(storage, tmp_path):
    _, _, service, _, _ = storage
    first = service.inspect_adapter(write_package(tmp_path / 'p0', '2.0.0'))
    saved = service._previews[first['preview_id']]
    assert 'package' not in saved and len(saved['content_hash']) == 64
    for index in range(1, 12):
        service.inspect_adapter(write_package(tmp_path / f'p{index}', f'2.0.{index}'))
    assert len(service._previews) <= service._previews.max_items
    # The first preview was evicted: installing it asks for a new preview instead of failing obscurely.
    with pytest.raises(ValueError, match='失效'):
        service.install_adapter(first['preview_id'])
