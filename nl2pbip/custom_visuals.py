"""Custom visual registry — organisation-defined Power BI visual types.

Power BI Desktop supports far more visual types than the small
canonical set bundled in :mod:`nl2pbip.visual_types` (AppSource
visuals, custom visuals shipped with Power BI, organisation-specific
visual plugins). Operators want to register their organisation's
visual names so the LLM planner can pick them up without having to
hard-code them in the prompt or remember an exact spelling.

This module loads a TOML registry from ``~/.nl2pbip/custom_visuals.toml``
on import. The file uses one ``[[visual]]`` entry per registered
visual::

    # ~/.nl2pbip/custom_visuals.toml
    [[visual]]
    name = "KPI Tile"
    visualType = "kpiTile"
    description = "Internal KPI tile with traffic-light status icon."

    [[visual]]
    name = "Revenue Waterfall"
    visualType = "revenueWaterfall"
    description = "Finance team's waterfall with variance annotations."

The visual's ``visualType`` is what gets merged into
:data:`nl2pbip.visual_types.CANONICAL_VISUAL_TYPES` so existing
validation/normalisation code picks it up. ``name`` and
``description`` are surfaced to the planner prompt so the LLM
can match user requests to the registered visuals.

If the TOML file is missing the module degrades gracefully —
:func:`get_custom_visual_specs` returns ``[]`` and nothing is
merged into the registry. That keeps imports cheap for the
common case (no custom visuals registered).

The registry is loaded exactly once per process; :func:`reload` is
exposed for tests and for an explicit ``--reload-custom-visuals``
style admin operation.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib

logger = logging.getLogger(__name__)

DEFAULT_CUSTOM_VISUALS_PATH = Path("~/.nl2pbip/custom_visuals.toml").expanduser()


@dataclass(frozen=True)
class CustomVisualSpec:
    """A single registered custom visual.

    Attributes
    ----------
    name:
        Human-readable name surfaced to the LLM in the planner
        prompt (e.g. ``"Revenue Waterfall"``).
    visual_type:
        The Power BI visual-type spelling that goes into the
        ``.pbir`` file (e.g. ``"revenueWaterfall"``). This is the
        value merged into
        :data:`nl2pbip.visual_types.CANONICAL_VISUAL_TYPES`.
    description:
        Optional one-line description shown alongside ``name`` in
        the planner prompt. Defaults to ``""``.
    """

    name: str
    visual_type: str
    description: str = ""

    def to_registry_payload(self) -> Dict[str, str]:
        """Serialise for inclusion in the planner payload."""
        return {
            "name": self.name,
            "visualType": self.visual_type,
            "description": self.description,
        }


def _parse_visual_table(table: Dict[str, Any]) -> Optional[CustomVisualSpec]:
    """Build a :class:`CustomVisualSpec` from one TOML ``[[visual]]`` table.

    Returns ``None`` when the entry is missing the required fields
    (``name`` or ``visualType``) so a partially-filled registry
    file still parses the good entries — only the malformed rows
    are skipped, with a warning so the operator can fix the file.
    """
    name = table.get("name")
    visual_type = table.get("visualType")
    if not isinstance(name, str) or not name.strip():
        logger.warning("custom_visuals.toml entry missing 'name'; skipping: %r", table)
        return None
    if not isinstance(visual_type, str) or not visual_type.strip():
        logger.warning(
            "custom_visuals.toml entry %r missing 'visualType'; skipping",
            name,
        )
        return None
    description_raw = table.get("description", "")
    description = description_raw if isinstance(description_raw, str) else ""
    return CustomVisualSpec(
        name=name.strip(),
        visual_type=visual_type.strip(),
        description=description.strip(),
    )


def load_custom_visual_specs(path: Optional[Path] = None) -> List[CustomVisualSpec]:
    """Read ``path`` (default: ``~/.nl2pbip/custom_visuals.toml``) and return specs.

    Returns an empty list when the file is missing. Raises
    :class:`tomllib.TOMLDecodeError` when the file exists but is
    malformed — that's a hard failure (operator should fix the
    file) rather than a silent skip.

    The ``[[visual]]`` array-of-tables form is the only supported
    structure; a top-level scalar or a flat ``[visual]`` table is
    treated as no visuals registered (with a warning so the
    operator knows the file was read but ignored).
    """
    target = (
        Path(path).expanduser() if path is not None else DEFAULT_CUSTOM_VISUALS_PATH
    )
    if not target.exists():
        return []
    try:
        with target.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError:
        logger.exception("custom_visuals.toml at %s is malformed", target)
        raise
    raw_entries = data.get("visual")
    if raw_entries is None:
        # Common mistake: a single [visual] table instead of
        # [[visual]] — surface it explicitly.
        logger.warning(
            "custom_visuals.toml at %s has no [[visual]] array of tables; "
            "got keys %s",
            target,
            sorted(data.keys()),
        )
        return []
    if not isinstance(raw_entries, list):
        logger.warning(
            "custom_visuals.toml at %s: 'visual' must be an array of tables "
            "([[visual]]), got %s",
            target,
            type(raw_entries).__name__,
        )
        return []
    specs: List[CustomVisualSpec] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            logger.warning(
                "custom_visuals.toml at %s: skipping non-table entry %r",
                target,
                entry,
            )
            continue
        spec = _parse_visual_table(entry)
        if spec is not None:
            specs.append(spec)
    return specs


# ----------------------------------------------------------------------
# Module-level cache + registry merge
# ----------------------------------------------------------------------


_CUSTOM_VISUAL_CACHE: Optional[List[CustomVisualSpec]] = None
_MERGE_APPLIED = False


def get_custom_visual_specs() -> List[CustomVisualSpec]:
    """Return the cached list of custom visual specs.

    Loads the TOML registry the first time, then returns the same
    list on subsequent calls. Use :func:`reload` to force a
    re-read (e.g. after the operator edits the file).
    """
    global _CUSTOM_VISUAL_CACHE
    if _CUSTOM_VISUAL_CACHE is None:
        _CUSTOM_VISUAL_CACHE = load_custom_visual_specs()
    return list(_CUSTOM_VISUAL_CACHE)


def reload(path: Optional[Path] = None) -> List[CustomVisualSpec]:
    """Force a re-read of the registry. Returns the fresh specs.

    ``path`` overrides the default location for this load only —
    subsequent calls without ``path`` revert to the default.
    """
    global _CUSTOM_VISUAL_CACHE
    if path is not None:
        specs = load_custom_visual_specs(Path(path))
    else:
        specs = load_custom_visual_specs()
    _CUSTOM_VISUAL_CACHE = list(specs)
    return list(_CUSTOM_VISUAL_CACHE)


def register_custom_visuals_into_registry(
    *, rebuild_canonical: bool = True
) -> List[str]:
    """Merge registered visuals into ``visual_types.CANONICAL_VISUAL_TYPES``.

    Returns the list of ``visualType`` strings that were newly
    added. Already-present types are not duplicated.

    Parameters
    ----------
    rebuild_canonical:
        ``True`` (default) rebuilds
        :data:`nl2pbip.visual_types.CANONICAL_VISUAL_TYPES` as a
        fresh frozenset that contains the union of the original
        canonical types and the custom visual types. ``False``
        skips the rebuild — useful in tests where the module has
        already been mutated and you don't want to lose your
        edits.
    """
    # Lazy import keeps the dependency direction clean:
    # ``visual_types`` does NOT import this module, so any
    # installation that uses the canonical visuals alone never
    # loads the TOML parser.
    from nl2pbip import visual_types

    specs = get_custom_visual_specs()
    existing = set(visual_types.CANONICAL_VISUAL_TYPES)
    new_types = sorted({s.visual_type for s in specs} - existing)
    if new_types and rebuild_canonical:
        merged = frozenset(existing | set(new_types))
        # The frozenset is a module-level binding; rebind it.
        visual_types.CANONICAL_VISUAL_TYPES = merged
        # ``validate_visual_type`` / ``normalize_visual_type``
        # both read from the module attribute, so picking up the
        # new frozenset is automatic — no further patching needed.
        # ``_resolve_case_insensitive`` iterates ``CANONICAL_VISUAL_TYPES``
        # at call time, so it sees the merged set without any
        # extra plumbing.
    if not _MERGE_APPLIED and specs:
        # Log once per process so a quiet test suite doesn't get
        # spammed when many subagents call this in sequence.
        logger.info(
            "Registered %d custom visual(s): %s",
            len(specs),
            ", ".join(s.visual_type for s in specs),
        )
    return new_types


def planner_payload_section() -> Dict[str, Any]:
    """Return the ``custom_visuals`` section for the planner prompt.

    The orchestrator's prompt-builder can drop this dict straight
    into the planner payload alongside the canonical
    ``visual_types`` block. Empty registry → empty list (so the
    caller can decide whether to include the section at all).
    """
    return {
        "custom_visuals": [s.to_registry_payload() for s in get_custom_visual_specs()],
    }


__all__ = [
    "DEFAULT_CUSTOM_VISUALS_PATH",
    "CustomVisualSpec",
    "get_custom_visual_specs",
    "load_custom_visual_specs",
    "planner_payload_section",
    "register_custom_visuals_into_registry",
    "reload",
]
