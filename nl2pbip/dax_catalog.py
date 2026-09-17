"""Load and manage organizational DAX templates and calculation groups."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DAX_LIBRARY_PATH = Path(__file__).with_name("dax_library.json")
PLACEHOLDER_PATTERN = re.compile(r"{{\s*([a-zA-Z0-9_]+)\s*}}")
# Default polling interval for the file watcher. Short enough that
# operator edits land in the next planner call within a couple of
# seconds; long enough to stay off the hot path when no editor is
# active.
DEFAULT_WATCH_INTERVAL_S = 5.0


@dataclass(frozen=True)
class DAXPattern:
    key: str
    description: str
    template: str
    format_string: Optional[str]
    parameters: List[str]


@dataclass(frozen=True)
class CalculationGroupConfig:
    key: str
    name: str
    table_name: str
    precedence: int
    description: Optional[str]
    items: List[Dict[str, Any]]


class DAXCatalog:
    """Central registry for organization-approved DAX assets."""

    def __init__(
        self,
        patterns: Dict[str, DAXPattern],
        calculation_groups: Dict[str, CalculationGroupConfig],
        source_path: Path,
    ):
        self._patterns = patterns
        self._calculation_groups = calculation_groups
        self.source_path = source_path

    # ------------------------------------------------------------------
    # Public read-only views
    # ------------------------------------------------------------------
    @property
    def patterns(self) -> Dict[str, "DAXPattern"]:
        """Map of pattern_key → :class:`DAXPattern` (read-only view)."""
        return self._patterns

    @property
    def calculation_groups(self) -> Dict[str, "CalculationGroupConfig"]:
        """Map of group_key → :class:`CalculationGroupConfig`."""
        return self._calculation_groups

    @classmethod
    def from_file(cls, path: Optional[str] = None) -> "DAXCatalog":
        file_path = Path(path).expanduser() if path else DEFAULT_DAX_LIBRARY_PATH
        if not file_path.exists():
            raise FileNotFoundError(f"DAX library file not found at {file_path}.")
        data = json.loads(file_path.read_text(encoding="utf-8"))
        patterns = {
            key: DAXPattern(
                key=key,
                description=value.get("description", ""),
                template=value["template"],
                format_string=value.get("format_string"),
                parameters=value.get("parameters")
                or _infer_parameters(value["template"]),
            )
            for key, value in (data.get("patterns") or {}).items()
        }
        calc_groups = {
            key: CalculationGroupConfig(
                key=key,
                name=value.get("name", key),
                table_name=value.get("table_name", value.get("name", key)),
                precedence=int(value.get("precedence", 0)),
                description=value.get("description"),
                items=value.get("items", []),
            )
            for key, value in (data.get("calculation_groups") or {}).items()
        }
        return cls(
            patterns=patterns, calculation_groups=calc_groups, source_path=file_path
        )

    # ------------------------------------------------------------------
    # Pattern helpers
    # ------------------------------------------------------------------
    def available_patterns(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": pattern.key,
                "description": pattern.description,
                "parameters": pattern.parameters,
                "format_string": pattern.format_string,
            }
            for pattern in self._patterns.values()
        ]

    def available_calculation_groups(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": group.key,
                "name": group.name,
                "table_name": group.table_name,
                "precedence": group.precedence,
                "items": [item.get("name") for item in group.items],
            }
            for group in self._calculation_groups.values()
        ]

    def render_pattern(self, pattern_key: str, **params: str) -> Dict[str, Any]:
        pattern = self._patterns.get(pattern_key)
        if not pattern:
            raise ValueError(f"Pattern '{pattern_key}' not found in DAX catalog.")
        missing = [param for param in pattern.parameters if param not in params]
        if missing:
            raise ValueError(
                f"Pattern '{pattern_key}' requires parameters: {', '.join(missing)}"
            )
        expression = _substitute_template(pattern.template, params)
        return {
            "expression": expression,
            "format_string": pattern.format_string,
        }

    def get_calculation_group(self, group_key: str) -> CalculationGroupConfig:
        group = self._calculation_groups.get(group_key)
        if not group:
            raise ValueError(
                f"Calculation group '{group_key}' not found in DAX catalog."
            )
        return group

    def prompt_payload(self) -> Dict[str, Any]:
        return {
            "patterns": self.available_patterns(),
            "calculation_groups": self.available_calculation_groups(),
        }

    def invalidate(self) -> None:
        """Release the cached content so the next read reloads from disk.

        ``DAXCatalog`` instances are otherwise immutable
        (frozen dataclasses for ``DAXPattern`` /
        ``CalculationGroupConfig``), so this method only clears
        anything attached by the cache layer — currently nothing
        on the instance itself, but the contract is that callers
        holding a reference to a stale catalog should call this
        and then re-fetch via :func:`get_default_catalog`.

        Kept on the class so the watcher and the cache share one
        invalidation surface.
        """
        # No per-instance cache yet — placeholder for future
        # memoisation (e.g. an ``available_patterns`` LRU). The
        # method exists so callers and watchers don't have to
        # branch on whether invalidation is meaningful.
        return None


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _infer_parameters(template: str) -> List[str]:
    return sorted(set(PLACEHOLDER_PATTERN.findall(template)))


def _substitute_template(template: str, params: Dict[str, str]) -> str:
    def replacer(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in params:
            raise ValueError(f"Missing parameter '{key}' for DAX template.")
        return params[key]

    return PLACEHOLDER_PATTERN.sub(replacer, template)


# ----------------------------------------------------------------------
# Module-level cache + file watcher
#
# Loading the DAX library parses JSON and builds frozen dataclasses; in
# long-running CLI / web sessions we want to avoid re-parsing on every
# planner call. The cache is invalidated automatically by
# :class:`DAXCatalogFileWatcher` whenever the JSON file's mtime moves.
# ----------------------------------------------------------------------


@dataclass
class _CachedCatalog:
    """Cache slot: holds the loaded catalog plus the mtime that produced it.

    Stored separately from ``DAXCatalog`` so the dataclass stays frozen
    and we don't have to touch the public class to manage the cache.
    """

    catalog: DAXCatalog
    mtime: float


_CATALOG_CACHE: Dict[str, _CachedCatalog] = {}
_CACHE_LOCK = threading.Lock()


def _cache_key(path: Path) -> str:
    return str(path.expanduser().resolve())


def get_default_catalog(path: Optional[str] = None) -> DAXCatalog:
    """Return the cached :class:`DAXCatalog`, loading on first use.

    Subsequent calls return the same instance until either:

    * the underlying JSON file's mtime changes (detected by
      :class:`DAXCatalogFileWatcher`), or
    * :func:`invalidate_cache` is called explicitly.

    The cache key is the resolved absolute path so two callers
    passing different relative paths to the same file still share
    one entry. ``path=None`` keys on the package-bundled
    ``dax_library.json``.
    """
    file_path = Path(path).expanduser() if path else DEFAULT_DAX_LIBRARY_PATH
    key = _cache_key(file_path)
    with _CACHE_LOCK:
        slot = _CATALOG_CACHE.get(key)
        try:
            current_mtime = file_path.stat().st_mtime
        except FileNotFoundError:
            current_mtime = -1.0
        if slot is not None and slot.mtime == current_mtime:
            return slot.catalog
        catalog = DAXCatalog.from_file(str(file_path))
        _CATALOG_CACHE[key] = _CachedCatalog(catalog=catalog, mtime=current_mtime)
        return catalog


def invalidate_cache(path: Optional[str] = None) -> bool:
    """Drop the cached catalog for ``path`` (or all entries when ``None``).

    Returns ``True`` when a cache entry actually existed and was
    removed, ``False`` when the cache was already cold. Safe to
    call from any thread.
    """
    with _CACHE_LOCK:
        if path is None:
            existed = bool(_CATALOG_CACHE)
            _CATALOG_CACHE.clear()
            return existed
        key = _cache_key(Path(path))
        return _CATALOG_CACHE.pop(key, None) is not None


class DAXCatalogFileWatcher:
    """Poll ``dax_library.json`` mtime and invalidate the cache on change.

    Uses :class:`threading.Timer` so the watcher doesn't need a
    dedicated thread of its own — the OS scheduler fires the
    callback in a worker thread after ``interval_s`` seconds. This
    keeps the implementation stdlib-only (no ``watchdog`` /
    ``inotify`` dependency) and side-steps the file-descriptor
    bookkeeping those libraries require.

    The watcher is intentionally simple — it polls, it logs, it
    invalidates. It does NOT push a fresh catalog into the cache:
    the next call to :func:`get_default_catalog` will reload on
    demand. That's the contract the planner already expects
    (cache-aside pattern) so we don't add new threading hazards.

    Example::

        watcher = DAXCatalogFileWatcher().start()
        try:
            ...  # planner runs in foreground
        finally:
            watcher.stop()
    """

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        interval_s: float = DEFAULT_WATCH_INTERVAL_S,
        on_change: Optional[Callable[[Path], None]] = None,
    ) -> None:
        if interval_s <= 0:
            raise ValueError(f"interval_s must be > 0, got {interval_s!r}")
        self.path = (
            Path(path).expanduser() if path else DEFAULT_DAX_LIBRARY_PATH
        )
        self.interval_s = interval_s
        self.on_change = on_change
        self._timer: Optional[threading.Timer] = None
        self._stopped = threading.Event()
        # Seed the mtime so we don't fire on the first tick for a
        # file that's been there the whole time the watcher was off.
        try:
            self._last_mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._last_mtime = -1.0

    def _check(self) -> None:
        if self._stopped.is_set():
            return
        try:
            current_mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            current_mtime = -1.0
        if current_mtime != self._last_mtime:
            previous_mtime = self._last_mtime
            self._last_mtime = current_mtime
            logger.warning(
                "DAX library file %s changed (mtime %.3f -> %.3f); "
                "invalidating cached catalog.",
                self.path,
                previous_mtime,
                current_mtime,
            )
            invalidate_cache(str(self.path))
            if self.on_change is not None:
                try:
                    self.on_change(self.path)
                except Exception:  # pragma: no cover - user callback
                    logger.exception(
                        "DAX library watcher on_change callback raised"
                    )
        self._schedule_next()

    def _schedule_next(self) -> None:
        if self._stopped.is_set():
            return
        self._timer = threading.Timer(self.interval_s, self._check)
        self._timer.daemon = True
        self._timer.start()

    def start(self) -> "DAXCatalogFileWatcher":
        """Begin polling. Idempotent: a second ``start`` is a no-op."""
        if self._timer is not None:
            return self
        self._stopped.clear()
        self._schedule_next()
        return self

    def stop(self, *, wait: bool = False) -> None:
        """Cancel the pending timer. Safe to call multiple times.

        When ``wait=True`` joins the currently-running tick (if any)
        before returning — useful in tests where the timer thread
        might still be holding the GIL on a flaky platform. Default
        ``False`` returns immediately.
        """
        self._stopped.set()
        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.cancel()
            if wait:
                timer.join(timeout=self.interval_s + 1.0)

    def wait_for_change(
        self, timeout_s: float, *, poll_s: float = 0.05
    ) -> bool:
        """Block until the watcher observes a mtime change or times out.

        Test helper. Returns ``True`` once a change was observed,
        ``False`` on timeout. ``poll_s`` controls how often the
        helper checks (it doesn't busy-wait the whole interval).
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            # ``_last_mtime`` is updated under the timer thread but
            # assignment is atomic on CPython, so a plain read is
            # fine for a best-effort test helper.
            try:
                current = self.path.stat().st_mtime
            except FileNotFoundError:
                current = -1.0
            if current != self._last_mtime:
                return True
            time.sleep(poll_s)
        return False


__all__ = [
    "DAXCatalog",
    "DAXCatalogFileWatcher",
    "DEFAULT_DAX_LIBRARY_PATH",
    "DEFAULT_WATCH_INTERVAL_S",
    "get_default_catalog",
    "invalidate_cache",
]
