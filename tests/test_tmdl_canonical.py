"""Tests for the canonical TMDL layout writer (Phase B).

The canonical layout is what Microsoft's ``powerbi-modeling-mcp`` TOM
parser expects: ``database.tmdl`` + per-table files + ``model.tmdl``
with ``ref table X`` declarations. nl2pbip's default writer still
produces the legacy monolithic ``model.tmdl`` for back-compat with
every shipped artifact; the canonical layout is opt-in via the env
var ``NL2PBIP_TMDL_CANONICAL=1`` or by setting
``context['canonical_tmdl_layout'] = True``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from nl2pbip.tmdl_engine import (
    CANONICAL_ENV_VAR,
    MODEL_PATH_KEY,
    TMDLColumn,
    TMDLMeasure,
    TMDLModel,
    TMDLRelationship,
    TMDLTable,
    _canonical_layout_requested,
    _load_model_canonical,
    _persist_model,
    _persist_model_canonical,
    _render_database_tmdl,
    _render_model_body,
    _render_model_refs_tmdl,
    _render_relationships_tmdl,
)


def _make_minimal_model() -> TMDLModel:
    """Build a TMDLModel with one table + one measure + one relationship."""
    sales = TMDLTable(name="Sales")
    sales.add_column(
        TMDLColumn(name="Amount", data_type="decimal", source_column="Amount")
    )
    sales.add_measure(TMDLMeasure(name="Total", expression="SUM(Sales[Amount])"))

    date = TMDLTable(name="Date")
    date.add_column(TMDLColumn(name="Date", data_type="dateTime", source_column="Date"))

    rel = TMDLRelationship(
        name="SalesToDate",
        from_table="Sales",
        from_column="Date",
        to_table="Date",
        to_column="Date",
    )

    model = TMDLModel()
    model.add_table(sales)
    model.add_table(date)
    model.add_relationship(rel)
    return model


def _seed_model_file(model_path: Path, model: TMDLModel) -> None:
    """Write a legacy-monolithic ``model.tmdl`` so :func:`load_model`
    has something to parse. The canonical pass then re-writes
    everything in the new layout on top of this seed."""
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_text(_render_model_body(model), encoding="utf-8")


@pytest.fixture
def canonical_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(CANONICAL_ENV_VAR, "1")
    yield


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    d = tmp_path / "SemanticModel" / "definition"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def seeded_model_path(model_dir: Path) -> Path:
    model_path = model_dir / "model.tmdl"
    _seed_model_file(model_path, _make_minimal_model())
    return model_path


# ---------------------------------------------------------------------------
# Pure renderer tests
# ---------------------------------------------------------------------------


def test_render_database_tmdl_has_compatibility_level() -> None:
    text = _render_database_tmdl(TMDLModel())
    assert "database" in text
    assert "compatibilityLevel: 1550" in text


def test_render_model_refs_tmdl_emits_ref_table_per_table() -> None:
    model = _make_minimal_model()
    text = _render_model_refs_tmdl(model)
    assert "ref table Sales" in text
    assert "ref table Date" in text
    assert "Total" not in text  # measures live in tables/<Name>.tmdl


def test_render_relationships_tmdl_has_refs_and_blocks() -> None:
    model = _make_minimal_model()
    text = _render_relationships_tmdl(model)
    assert "ref table Sales" in text
    assert "ref table Date" in text
    assert 'relationship "SalesToDate"' in text
    assert "fromTable" in text
    assert "toTable" in text


# ---------------------------------------------------------------------------
# Env / context flag wiring
# ---------------------------------------------------------------------------


def test_canonical_layout_default_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CANONICAL_ENV_VAR, raising=False)
    assert _canonical_layout_requested({}) is False


def test_canonical_layout_requested_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CANONICAL_ENV_VAR, "1")
    assert _canonical_layout_requested({}) is True


def test_canonical_layout_requested_via_context() -> None:
    assert _canonical_layout_requested({"canonical_tmdl_layout": True}) is True


def test_canonical_layout_env_truthy_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for value in ("1", "true", "yes", "on", "TRUE", "  Yes  "):
        monkeypatch.setenv(CANONICAL_ENV_VAR, value)
        assert _canonical_layout_requested({}) is True, value


def test_canonical_layout_env_falsy_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for value in ("0", "false", "no", "off", "", "anything-else"):
        monkeypatch.setenv(CANONICAL_ENV_VAR, value)
        assert _canonical_layout_requested({}) is False, value


# ---------------------------------------------------------------------------
# _persist_model_canonical
# ---------------------------------------------------------------------------


def test_persist_model_canonical_writes_expected_files(
    seeded_model_path: Path,
    model_dir: Path,
    canonical_env: None,
) -> None:
    context = {
        MODEL_PATH_KEY: str(seeded_model_path),
        "canonical_tmdl_layout": True,
    }
    target = _persist_model_canonical(context)
    assert target == seeded_model_path
    assert (model_dir / "database.tmdl").exists()
    assert (model_dir / "model.tmdl").exists()
    assert (model_dir / "relationships.tmdl").exists()
    assert (model_dir / "tables" / "Sales.tmdl").exists()
    assert (model_dir / "tables" / "Date.tmdl").exists()


def test_persist_model_canonical_emits_ref_table_declarations(
    seeded_model_path: Path,
    model_dir: Path,
    canonical_env: None,
) -> None:
    """The key fix: ``model.tmdl`` must contain ``ref table X`` lines,
    not inlined ``table "X" {...}`` blocks."""
    context = {
        MODEL_PATH_KEY: str(seeded_model_path),
        "canonical_tmdl_layout": True,
    }
    _persist_model_canonical(context)
    model_text = (model_dir / "model.tmdl").read_text()
    assert "ref table Sales" in model_text
    assert "ref table Date" in model_text
    # Crucially, no inlined table blocks.
    assert 'table "Sales"' not in model_text
    assert 'table "Date"' not in model_text


def test_persist_model_canonical_separates_tables(
    seeded_model_path: Path,
    model_dir: Path,
    canonical_env: None,
) -> None:
    """Each table lives in its own file under ``tables/<Name>.tmdl``."""
    context = {
        MODEL_PATH_KEY: str(seeded_model_path),
        "canonical_tmdl_layout": True,
    }
    _persist_model_canonical(context)
    sales = (model_dir / "tables" / "Sales.tmdl").read_text()
    assert 'table "Sales"' in sales
    assert "Total" in sales  # the measure
    assert 'table "Date"' not in sales  # not inlined


def test_persist_model_canonical_database_has_compatibility_level(
    seeded_model_path: Path,
    model_dir: Path,
    canonical_env: None,
) -> None:
    context = {
        MODEL_PATH_KEY: str(seeded_model_path),
        "canonical_tmdl_layout": True,
    }
    _persist_model_canonical(context)
    db_text = (model_dir / "database.tmdl").read_text()
    assert db_text.startswith("database")
    assert "compatibilityLevel" in db_text


# ---------------------------------------------------------------------------
# _load_model_canonical round-trip
# ---------------------------------------------------------------------------


def test_load_model_canonical_round_trip(
    seeded_model_path: Path,
    canonical_env: None,
) -> None:
    """Write via canonical, read back, verify all data survived."""
    context = {
        MODEL_PATH_KEY: str(seeded_model_path),
        "canonical_tmdl_layout": True,
    }
    _persist_model_canonical(context)
    loaded = _load_model_canonical(seeded_model_path)
    assert set(loaded.tables.keys()) == {"Sales", "Date"}
    assert "Total" in loaded.tables["Sales"].measures
    assert len(loaded.relationships) == 1
    rel = loaded.relationships[0]
    assert rel.name == "SalesToDate"
    assert rel.from_table == "Sales"
    assert rel.to_table == "Date"


# ---------------------------------------------------------------------------
# Legacy _persist_model behaviour unchanged when canonical not requested
# ---------------------------------------------------------------------------


def test_persist_model_default_legacy_layout_unchanged(
    seeded_model_path: Path,
    model_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default (no env var, no context flag) writes the legacy
    monolithic ``model.tmdl`` with everything inlined."""
    monkeypatch.delenv(CANONICAL_ENV_VAR, raising=False)
    context = {MODEL_PATH_KEY: str(seeded_model_path)}
    _persist_model(context)
    text = seeded_model_path.read_text()
    # Legacy: inlined table blocks present
    assert 'table "Sales"' in text
    assert 'table "Date"' in text
    # Canonical side files should NOT exist
    assert not (model_dir / "database.tmdl").exists()
    assert not (model_dir / "tables").exists()


def test_persist_model_routes_to_canonical_when_set(
    seeded_model_path: Path,
    model_dir: Path,
    canonical_env: None,
) -> None:
    """When the env var is set, _persist_model delegates to canonical."""
    context = {MODEL_PATH_KEY: str(seeded_model_path)}
    _persist_model(context)
    # Should have produced canonical side files
    assert (model_dir / "database.tmdl").exists()
    assert (model_dir / "tables" / "Sales.tmdl").exists()
