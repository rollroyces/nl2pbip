"""Tests for the DAX-catalog module-level cache and file watcher.

The cache lets long-running sessions (CLI ``run`` mode, web hooks)
avoid re-parsing ``dax_library.json`` on every planner call. The
watcher invalidates the cache automatically when the JSON's mtime
changes, so editor edits land on the next call without a process
restart.

These tests use a tmp copy of the bundled library so they don't
race other tests that load the real file (and so a 1-second mtime
granularity on some filesystems doesn't cause flakes).
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from nl2pbip.dax_catalog import (
    _CATALOG_CACHE,
    DEFAULT_DAX_LIBRARY_PATH,
    DAXCatalog,
    DAXCatalogFileWatcher,
    _cache_key,
    get_default_catalog,
    invalidate_cache,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bundled_library_copy(tmp_path: Path) -> Path:
    """Copy the bundled dax_library.json into a tmp file.

    The watcher test mutates this copy. Returning a real file under
    ``tmp_path`` keeps the bundled library pristine for the rest of
    the suite and lets us set explicit mtimes.
    """
    dest = tmp_path / "dax_library.json"
    shutil.copyfile(DEFAULT_DAX_LIBRARY_PATH, dest)
    return dest


def _mutate_library(path: Path, extra_pattern_key: str) -> None:
    """Append a sentinel pattern to the library on disk.

    The new key is unique per-call so the cache (keyed on the
    catalog contents via mtime) is guaranteed to be different.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("patterns", {})[extra_pattern_key] = {
        "description": f"sentinel pattern {extra_pattern_key}",
        "template": "/* {{slot}} */",
        "parameters": ["slot"],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    # Some filesystems have 1s mtime resolution; bump the mtime
    # explicitly so the watcher's stat() sees the change.
    future = time.time() + 1.0
    import os

    os.utime(path, (future, future))


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class TestDAXCatalogCache:
    def setup_method(self) -> None:
        invalidate_cache()

    def teardown_method(self) -> None:
        invalidate_cache()

    def test_get_default_catalog_returns_cached_instance(
        self, bundled_library_copy: Path
    ) -> None:
        first = get_default_catalog(str(bundled_library_copy))
        second = get_default_catalog(str(bundled_library_copy))
        assert first is second  # cache hit, same object

    def test_explicit_invalidate_reloads(self, bundled_library_copy: Path) -> None:
        first = get_default_catalog(str(bundled_library_copy))
        assert invalidate_cache(str(bundled_library_copy)) is True
        second = get_default_catalog(str(bundled_library_copy))
        assert first is not second

    def test_invalidate_unknown_key_returns_false(
        self, bundled_library_copy: Path
    ) -> None:
        # Cold cache: nothing to invalidate.
        assert invalidate_cache(str(bundled_library_copy)) is False

    def test_invalidate_all_clears_everything(
        self, bundled_library_copy: Path, tmp_path: Path
    ) -> None:
        other = tmp_path / "other.json"
        shutil.copyfile(DEFAULT_DAX_LIBRARY_PATH, other)
        get_default_catalog(str(bundled_library_copy))
        get_default_catalog(str(other))
        assert len(_CATALOG_CACHE) >= 2
        assert invalidate_cache() is True
        assert _CATALOG_CACHE == {}

    def test_mtime_change_forces_reload(self, bundled_library_copy: Path) -> None:
        first = get_default_catalog(str(bundled_library_copy))
        assert first.patterns == first.patterns  # sanity
        # Mutate the file and bump mtime explicitly.
        _mutate_library(bundled_library_copy, "sentinel_one")
        # The cache slot's stored mtime now disagrees with disk,
        # so the next call must rebuild the catalog.
        second = get_default_catalog(str(bundled_library_copy))
        assert first is not second
        assert "sentinel_one" in second.patterns

    def test_cache_key_normalises_paths(self, bundled_library_copy: Path) -> None:
        # Same file via two different relative spellings → same key.
        assert _cache_key(bundled_library_copy) == _cache_key(
            bundled_library_copy.resolve()
        )


# ---------------------------------------------------------------------------
# File watcher
# ---------------------------------------------------------------------------


class TestDAXCatalogFileWatcher:
    def teardown_method(self) -> None:
        invalidate_cache()

    def test_interval_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            DAXCatalogFileWatcher(interval_s=0)
        with pytest.raises(ValueError):
            DAXCatalogFileWatcher(interval_s=-1.0)

    def test_watcher_invalidates_cache_within_interval(
        self, bundled_library_copy: Path
    ) -> None:
        """Mutate the file → watcher's next tick clears the cache.

        Polling interval is 0.5s for the test (faster than the
        production 5s default). The test asserts the cache is gone
        well within the 6-second budget the task spec asks for.
        """
        # Prime the cache so the watcher has something to invalidate.
        get_default_catalog(str(bundled_library_copy))
        assert _cache_key(bundled_library_copy) in _CATALOG_CACHE

        watcher = DAXCatalogFileWatcher(str(bundled_library_copy), interval_s=0.5)
        callback_seen: Dict[str, Any] = {}
        event = threading.Event()

        def on_change(p: Path) -> None:
            callback_seen["path"] = p
            event.set()

        watcher.on_change = on_change
        watcher.start()
        try:
            # Mutate the file in a separate step so the mtime
            # change is observable to the polling timer.
            _mutate_library(bundled_library_copy, "sentinel_watch")

            # The watcher polls every 0.5s. The cache must be gone
            # within 6s — that's the contract the task spec sets
            # for the production 5s interval.
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                if _cache_key(bundled_library_copy) not in _CATALOG_CACHE:
                    break
                time.sleep(0.1)
            else:
                pytest.fail(
                    "DAX catalog watcher did not invalidate the cache "
                    "within 6 seconds"
                )

            # The user-supplied on_change callback also fires.
            assert event.wait(timeout=1.0), "watcher on_change callback did not fire"
            assert callback_seen["path"] == bundled_library_copy
        finally:
            watcher.stop(wait=True)

    def test_watcher_reload_picks_up_new_pattern(
        self, bundled_library_copy: Path
    ) -> None:
        """After invalidation, the next get_default_catalog reloads.

        Closes the loop: watcher detects → cache cleared → next
        consumer reloads with the new pattern visible.
        """
        get_default_catalog(str(bundled_library_copy))
        assert (
            "sentinel_reload"
            not in get_default_catalog(str(bundled_library_copy)).patterns
        )

        watcher = DAXCatalogFileWatcher(str(bundled_library_copy), interval_s=0.5)
        watcher.start()
        try:
            _mutate_library(bundled_library_copy, "sentinel_reload")
            # Wait for the cache to clear.
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                if _cache_key(bundled_library_copy) not in _CATALOG_CACHE:
                    break
                time.sleep(0.1)
            else:
                pytest.fail("watcher did not invalidate cache in time")

            catalog = get_default_catalog(str(bundled_library_copy))
            assert "sentinel_reload" in catalog.patterns
        finally:
            watcher.stop(wait=True)

    def test_watcher_stop_is_idempotent(self, bundled_library_copy: Path) -> None:
        watcher = DAXCatalogFileWatcher(str(bundled_library_copy), interval_s=10.0)
        watcher.start()
        watcher.stop()
        watcher.stop()  # must not raise
        watcher.stop(wait=True)

    def test_watcher_start_is_idempotent(self, bundled_library_copy: Path) -> None:
        watcher = DAXCatalogFileWatcher(str(bundled_library_copy), interval_s=10.0)
        watcher.start()
        first_timer = watcher._timer
        watcher.start()  # must not schedule a second timer
        assert watcher._timer is first_timer
        watcher.stop()


# ---------------------------------------------------------------------------
# Bundled catalog stays loadable through the cache layer
# ---------------------------------------------------------------------------


def test_bundled_catalog_loads_via_cache() -> None:
    """The shipped library must still load through the public cache API."""
    invalidate_cache()
    catalog = get_default_catalog()
    # Same shape as the eager ``DAXCatalog.from_file`` path.
    assert isinstance(catalog, DAXCatalog)
    assert "yoy_growth" in catalog.patterns
