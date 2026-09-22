#!/usr/bin/env python3
"""Migrate a single SemanticModel directory from the legacy monolithic
TMDL layout to the canonical multi-file layout.

v2.0.0 flips the default TMDL writer to canonical. Existing artifacts
shipped with the monolithic ``model.tmdl`` still load (the loader
auto-detects), but you should run this script to canonicalise them so
the new artifacts match the new default.

Usage::

    python scripts/legacy_to_canonical.py path/to/SemanticModel
    python scripts/legacy_to_canonical.py path/to/SemanticModel --commit
    python scripts/legacy_to_canonical.py path/to/SemanticModel --diff

Default mode is DRY-RUN: the script reports what it would change and
exits non-zero if the artifact is already canonical (nothing to do).
Pass ``--commit`` to actually rewrite the files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make ``nl2pbip`` importable when invoked from a checkout.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from nl2pbip.tmdl_engine import (  # noqa: E402  (path-mutated import)
    MODEL_PATH_KEY,
    _persist_model_canonical,
    load_model,
)


def _looks_like_canonical(model_dir: Path) -> bool:
    return (model_dir / "database.tmdl").exists() or (
        (model_dir / "tables").exists() and any((model_dir / "tables").glob("*.tmdl"))
    )


def _looks_like_legacy(model_dir: Path) -> bool:
    return (
        (model_dir / "model.tmdl").exists()
        and (
            "ref table"
            in (model_dir / "model.tmdl")
            .read_text(encoding="utf-8")
            .splitlines()[0:1][0]
            if (model_dir / "model.tmdl").read_text(encoding="utf-8").strip()
            else False
        )
        is False
        and _looks_like_canonical(model_dir) is False
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate a SemanticModel directory to the canonical TMDL layout."
    )
    parser.add_argument(
        "path",
        type=Path,
        help="Path to the SemanticModel directory (containing model.tmdl).",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually rewrite the model files. Default is dry-run.",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Show before/after byte counts (dry-run mode only).",
    )
    args = parser.parse_args(argv)

    model_dir = args.path
    if not model_dir.exists():
        print(f"error: {model_dir} does not exist", file=sys.stderr)
        return 2
    if not model_dir.is_dir():
        print(f"error: {model_dir} is not a directory", file=sys.stderr)
        return 2

    model_tmdl = model_dir / "model.tmdl"
    if not model_tmdl.exists():
        print(f"error: {model_tmdl} not found — not a TMDL model?", file=sys.stderr)
        return 2

    if _looks_like_canonical(model_dir):
        print(f"info: {model_dir} already canonical — nothing to do")
        return 0

    # Sanity-load to make sure we can parse the legacy body.
    try:
        load_model(model_tmdl, prefer_canonical=False)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"error: failed to parse legacy model.tmdl: {exc}", file=sys.stderr)
        return 3

    if args.diff:
        before = sum(p.stat().st_size for p in model_dir.rglob("*.tmdl"))
        print(f"info: current bytes across all .tmdl files: {before}")

    if not args.commit:
        print(
            f"dry-run: would canonicalise {model_dir} "
            "(database.tmdl + per-table files + model.tmdl refs + relationships.tmdl)"
        )
        print("dry-run: pass --commit to apply the migration")
        return 0

    _persist_model_canonical({MODEL_PATH_KEY: str(model_tmdl)})
    print(f"canonicalised: {model_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
