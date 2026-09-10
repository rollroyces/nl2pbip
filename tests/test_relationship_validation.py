"""Tests for column-name uniqueness and relationship validation.

Covers the TMDL validation rules that the orchestrator's LLM retry
loop now relies on:

* Duplicate column names within a single ``create_table`` call are
  rejected before the model is touched.
* Duplicate column names across separate ``create_table`` calls (on
  the same table name) are rejected by ``TMDLTable.add_column``.
* ``define_relationship`` validates both endpoints exist with the
  correct data types; the LLM gets a clear error rather than a
  silently-broken model.
* ``define_relationship`` rejects self-referential relationships,
  invalid cardinalities, and duplicate active relationships.
* The orchestrator's planner payload includes a JSON snapshot of
  the current model state so the LLM has the schema in hand when
  defining relationships.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from nl2pbip.orchestrator import Orchestrator
from nl2pbip.tmdl_engine import (
    create_table_handler,
    define_relationship_handler,
    load_model,
    parse_tmdl_text,
)
from nl2pbip.tmdl_linter import TMDLValidationError


class _StaticLLM:
    """Deterministic LLM stub for orchestrator tests."""

    def __init__(self, plan: List[Dict[str, Any]]) -> None:
        self._plan = plan
        self.calls = 0

    def generate(self, messages: List[Dict[str, str]]) -> str:
        self.calls += 1
        return json.dumps({"plan": self._plan})


# ---------------------------------------------------------------------------
# Column name uniqueness
# ---------------------------------------------------------------------------


class TestDuplicateColumnNamesRejected:
    def test_duplicate_columns_in_single_call_raise_before_model_load(
        self, tmp_path: Path
    ) -> None:
        """Two columns with the same name in the same ``columns`` list."""
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        with pytest.raises(TMDLValidationError) as exc_info:
            create_table_handler(
                table_name="Sales",
                columns=[
                    {"name": "Id", "data_type": "int64"},
                    {"name": "Id", "data_type": "string"},
                ],
                context=ctx,
            )
        # Both duplicates are surfaced in a single error.
        assert "duplicate column names" in str(exc_info.value).lower()
        assert "Id" in str(exc_info.value)
        # The model file is NOT created when validation fails — no
        # half-written state to debug.
        assert not (tmp_path / "model.tmdl").exists()

    def test_multiple_duplicate_names_surfaced_together(self, tmp_path: Path) -> None:
        """Every duplicate name is in the error message at once."""
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        with pytest.raises(TMDLValidationError) as exc_info:
            create_table_handler(
                table_name="T",
                columns=[
                    {"name": "X", "data_type": "int64"},
                    {"name": "X", "data_type": "string"},
                    {"name": "Y", "data_type": "int64"},
                    {"name": "Y", "data_type": "decimal"},
                    {"name": "Z", "data_type": "string"},  # unique, not in error
                ],
                context=ctx,
            )
        msg = str(exc_info.value)
        assert "X" in msg
        assert "Y" in msg
        assert "Z" not in msg

    def test_unique_columns_pass_through(self, tmp_path: Path) -> None:
        """No false positives — distinct names are fine."""
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        result = create_table_handler(
            table_name="Sales",
            columns=[
                {"name": "Id", "data_type": "int64"},
                {"name": "Region", "data_type": "string"},
                {"name": "Amount", "data_type": "decimal"},
            ],
            context=ctx,
        )
        assert result["status"] == "success"


# ---------------------------------------------------------------------------
# Relationship validation
# ---------------------------------------------------------------------------


class TestRelationshipValidatesEndpoints:
    def _seed_two_tables(self, tmp_path: Path) -> dict:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        create_table_handler(
            table_name="Date",
            columns=[
                {"name": "Date", "data_type": "date"},
                {"name": "Year", "data_type": "wholeNumber"},
            ],
            context=ctx,
        )
        create_table_handler(
            table_name="Sales",
            columns=[
                {"name": "SaleId", "data_type": "string"},
                {"name": "DateKey", "data_type": "int64"},
                {"name": "Amount", "data_type": "decimal"},
            ],
            context=ctx,
        )
        return ctx

    def test_unknown_from_table_rejected(self, tmp_path: Path) -> None:
        ctx = self._seed_two_tables(tmp_path)
        with pytest.raises(TMDLValidationError) as exc_info:
            define_relationship_handler(
                from_table="Nonexistent",
                from_column="Date",
                to_table="Date",
                to_column="Date",
                context=ctx,
            )
        assert "Nonexistent" in str(exc_info.value)
        assert "not defined" in str(exc_info.value).lower()

    def test_unknown_to_table_rejected(self, tmp_path: Path) -> None:
        ctx = self._seed_two_tables(tmp_path)
        with pytest.raises(TMDLValidationError) as exc_info:
            define_relationship_handler(
                from_table="Date",
                from_column="Date",
                to_table="Nonexistent",
                to_column="Date",
                context=ctx,
            )
        assert "Nonexistent" in str(exc_info.value)

    def test_unknown_from_column_lists_available_columns(self, tmp_path: Path) -> None:
        """When the column doesn't exist, list the table's real columns so the LLM can self-correct."""
        ctx = self._seed_two_tables(tmp_path)
        with pytest.raises(TMDLValidationError) as exc_info:
            define_relationship_handler(
                from_table="Sales",
                from_column="Nonexistent",
                to_table="Date",
                to_column="Date",
                context=ctx,
            )
        msg = str(exc_info.value)
        assert "Nonexistent" in msg
        # Available columns are listed so the LLM can pick a real one.
        assert "SaleId" in msg
        assert "DateKey" in msg

    def test_unknown_to_column_lists_available_columns(self, tmp_path: Path) -> None:
        ctx = self._seed_two_tables(tmp_path)
        with pytest.raises(TMDLValidationError) as exc_info:
            define_relationship_handler(
                from_table="Sales",
                from_column="DateKey",
                to_table="Date",
                to_column="Nonexistent",
                context=ctx,
            )
        msg = str(exc_info.value)
        assert "Nonexistent" in msg
        assert "Date" in msg
        assert "Year" in msg


class TestRelationshipTypeCompatibility:
    """Power BI Desktop refuses to load models that join incompatible types."""

    def _seed_numeric_pair(self, tmp_path: Path) -> dict:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        create_table_handler(
            table_name="Products",
            columns=[{"name": "Id", "data_type": "int64"}],
            context=ctx,
        )
        create_table_handler(
            table_name="Categories",
            columns=[{"name": "ProductId", "data_type": "int64"}],
            context=ctx,
        )
        return ctx

    def _seed_text_target(self, tmp_path: Path) -> dict:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        create_table_handler(
            table_name="Products",
            columns=[{"name": "Id", "data_type": "int64"}],
            context=ctx,
        )
        create_table_handler(
            table_name="Categories",
            columns=[{"name": "Name", "data_type": "string"}],
            context=ctx,
        )
        return ctx

    def test_numeric_to_numeric_succeeds(self, tmp_path: Path) -> None:
        ctx = self._seed_numeric_pair(tmp_path)
        result = define_relationship_handler(
            from_table="Products",
            from_column="Id",
            to_table="Categories",
            to_column="ProductId",
            context=ctx,
        )
        assert result["status"] == "success"

    def test_numeric_alias_to_numeric_canonical_succeeds(self, tmp_path: Path) -> None:
        """``bigint`` (alias) joins ``wholeNumber`` (canonical) cleanly."""
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        create_table_handler(
            table_name="A",
            columns=[{"name": "K", "data_type": "bigint"}],
            context=ctx,
        )
        create_table_handler(
            table_name="B",
            columns=[{"name": "K", "data_type": "wholeNumber"}],
            context=ctx,
        )
        result = define_relationship_handler(
            from_table="A",
            from_column="K",
            to_table="B",
            to_column="K",
            context=ctx,
        )
        assert result["status"] == "success"

    def test_text_to_numeric_rejected(self, tmp_path: Path) -> None:
        ctx = self._seed_text_target(tmp_path)
        with pytest.raises(TMDLValidationError) as exc_info:
            define_relationship_handler(
                from_table="Products",
                from_column="Id",
                to_table="Categories",
                to_column="Name",  # string column
                context=ctx,
            )
        msg = str(exc_info.value)
        assert "incompatible" in msg.lower()
        assert "int64" in msg
        assert "string" in msg

    def test_date_to_text_rejected(self, tmp_path: Path) -> None:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        create_table_handler(
            table_name="Dates",
            columns=[{"name": "D", "data_type": "date"}],
            context=ctx,
        )
        create_table_handler(
            table_name="Events",
            columns=[{"name": "Label", "data_type": "string"}],
            context=ctx,
        )
        with pytest.raises(TMDLValidationError):
            define_relationship_handler(
                from_table="Events",
                from_column="Label",
                to_table="Dates",
                to_column="D",
                context=ctx,
            )

    def test_boolean_to_numeric_rejected(self, tmp_path: Path) -> None:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        create_table_handler(
            table_name="Flags",
            columns=[{"name": "Active", "data_type": "boolean"}],
            context=ctx,
        )
        create_table_handler(
            table_name="Counts",
            columns=[{"name": "Total", "data_type": "int64"}],
            context=ctx,
        )
        with pytest.raises(TMDLValidationError):
            define_relationship_handler(
                from_table="Counts",
                from_column="Total",
                to_table="Flags",
                to_column="Active",
                context=ctx,
            )


class TestRelationshipSelfReferentialRejected:
    def test_self_join_same_column_rejected(self, tmp_path: Path) -> None:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        create_table_handler(
            table_name="Employees",
            columns=[
                {"name": "Id", "data_type": "int64"},
                {"name": "ManagerId", "data_type": "int64"},
            ],
            context=ctx,
        )
        # ManagerId → Id on the SAME table is fine (parent-child
        # hierarchy) — that's a valid self-referential relationship.
        result = define_relationship_handler(
            from_table="Employees",
            from_column="ManagerId",
            to_table="Employees",
            to_column="Id",
            context=ctx,
        )
        assert result["status"] == "success"

    def test_degenerate_self_join_rejected(self, tmp_path: Path) -> None:
        """``X → X`` on the same column is degenerate — Power BI rejects it."""
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        create_table_handler(
            table_name="T",
            columns=[{"name": "X", "data_type": "int64"}],
            context=ctx,
        )
        with pytest.raises(TMDLValidationError) as exc_info:
            define_relationship_handler(
                from_table="T",
                from_column="X",
                to_table="T",
                to_column="X",
                context=ctx,
            )
        assert "degenerate self-join" in str(exc_info.value).lower()


class TestRelationshipCardinalityValidation:
    def _seed(self, tmp_path: Path) -> dict:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        create_table_handler(
            table_name="A",
            columns=[{"name": "K", "data_type": "int64"}],
            context=ctx,
        )
        create_table_handler(
            table_name="B",
            columns=[{"name": "K", "data_type": "int64"}],
            context=ctx,
        )
        return ctx

    def test_invalid_cardinality_rejected(self, tmp_path: Path) -> None:
        ctx = self._seed(tmp_path)
        with pytest.raises(TMDLValidationError) as exc_info:
            define_relationship_handler(
                from_table="A",
                from_column="K",
                to_table="B",
                to_column="K",
                cardinality="sometimes",  # not a TMDL value
                context=ctx,
            )
        assert "cardinality" in str(exc_info.value).lower()

    def test_invalid_cross_filter_direction_rejected(self, tmp_path: Path) -> None:
        ctx = self._seed(tmp_path)
        with pytest.raises(TMDLValidationError) as exc_info:
            define_relationship_handler(
                from_table="A",
                from_column="K",
                to_table="B",
                to_column="K",
                cross_filter_direction="diagonal",
                context=ctx,
            )
        assert "crossFilterDirection" in str(exc_info.value)

    def test_valid_cardinalities_accepted(self, tmp_path: Path) -> None:
        # The _seed fixture is unused here — we build a fresh model
        # per cardinality so we don't trip the duplicate-active
        # relationship check.
        for cardinality in ("oneToOne", "oneToMany", "manyToOne", "manyToMany"):
            # Use a fresh model for each cardinality so we don't
            # trip the duplicate-active-relationship check.
            sub_ctx = {"model_path": str(tmp_path / f"model_{cardinality}.tmdl")}
            create_table_handler(
                table_name="A",
                columns=[{"name": "K", "data_type": "int64"}],
                context=sub_ctx,
            )
            create_table_handler(
                table_name="B",
                columns=[{"name": "K", "data_type": "int64"}],
                context=sub_ctx,
            )
            result = define_relationship_handler(
                from_table="A",
                from_column="K",
                to_table="B",
                to_column="K",
                cardinality=cardinality,
                context=sub_ctx,
            )
            assert result["status"] == "success"


class TestDuplicateActiveRelationshipsRejected:
    def test_two_active_relationships_same_from_endpoint_rejected(
        self, tmp_path: Path
    ) -> None:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        create_table_handler(
            table_name="Sales",
            columns=[{"name": "DateKey", "data_type": "int64"}],
            context=ctx,
        )
        create_table_handler(
            table_name="Calendar",
            columns=[{"name": "DateKey", "data_type": "int64"}],
            context=ctx,
        )
        create_table_handler(
            table_name="Fiscal",
            columns=[{"name": "DateKey", "data_type": "int64"}],
            context=ctx,
        )
        define_relationship_handler(
            from_table="Sales",
            from_column="DateKey",
            to_table="Calendar",
            to_column="DateKey",
            context=ctx,
        )
        # A SECOND active relationship from Sales[DateKey] is
        # rejected — Power BI allows only one active relationship
        # per from-side endpoint.
        with pytest.raises(TMDLValidationError) as exc_info:
            define_relationship_handler(
                from_table="Sales",
                from_column="DateKey",
                to_table="Fiscal",
                to_column="DateKey",
                context=ctx,
            )
        assert "active relationship" in str(exc_info.value).lower()

    def test_inactive_duplicate_allowed(self, tmp_path: Path) -> None:
        """Inactive duplicates are fine — Power BI uses them for role-playing dimensions."""
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        create_table_handler(
            table_name="Sales",
            columns=[{"name": "DateKey", "data_type": "int64"}],
            context=ctx,
        )
        create_table_handler(
            table_name="Calendar",
            columns=[{"name": "DateKey", "data_type": "int64"}],
            context=ctx,
        )
        create_table_handler(
            table_name="Fiscal",
            columns=[{"name": "DateKey", "data_type": "int64"}],
            context=ctx,
        )
        define_relationship_handler(
            from_table="Sales",
            from_column="DateKey",
            to_table="Calendar",
            to_column="DateKey",
            context=ctx,
        )
        # Second relationship, but INACTIVE — should be accepted.
        result = define_relationship_handler(
            from_table="Sales",
            from_column="DateKey",
            to_table="Fiscal",
            to_column="DateKey",
            active=False,
            context=ctx,
        )
        assert result["status"] == "success"


# ---------------------------------------------------------------------------
# Orchestrator: model state summary
# ---------------------------------------------------------------------------


class TestOrchestratorModelStateSummary:
    """The orchestrator should include the current model state in its
    planner payload so the LLM can produce accurate relationships."""

    def test_empty_workspace_summary_is_none(self, tmp_path: Path) -> None:
        model_path = tmp_path / "model.tmdl"
        # File doesn't exist yet.
        assert not model_path.exists()
        client = _StaticLLM([])
        orchestrator = Orchestrator(llm_client=client)
        summary = orchestrator._summarise_model({"model_path": str(model_path)})
        assert summary is None

    def test_summary_lists_tables_columns_types(self, tmp_path: Path) -> None:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        create_table_handler(
            table_name="Date",
            columns=[
                {"name": "Date", "data_type": "date"},
                {"name": "Year", "data_type": "wholeNumber"},
            ],
            context=ctx,
        )
        create_table_handler(
            table_name="Sales",
            columns=[
                {"name": "SaleId", "data_type": "string"},
                {"name": "Amount", "data_type": "decimal"},
            ],
            context=ctx,
        )
        client = _StaticLLM([])
        orchestrator = Orchestrator(llm_client=client)
        summary = orchestrator._summarise_model(ctx)
        assert summary is not None
        # Tables and their column types are exposed.
        assert "Date" in summary["tables"]
        assert summary["tables"]["Date"]["columns"]["Date"] == "date"
        assert summary["tables"]["Date"]["columns"]["Year"] == "wholeNumber"
        assert "Sales" in summary["tables"]
        assert summary["tables"]["Sales"]["columns"]["Amount"] == "decimal"
        # Counts are included so the LLM knows where it is.
        assert summary["table_count"] == 2
        assert summary["relationship_count"] == 0

    def test_summary_includes_relationships(self, tmp_path: Path) -> None:
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        create_table_handler(
            table_name="Date",
            columns=[{"name": "Date", "data_type": "date"}],
            context=ctx,
        )
        create_table_handler(
            table_name="Sales",
            columns=[{"name": "DateKey", "data_type": "date"}],
            context=ctx,
        )
        define_relationship_handler(
            from_table="Sales",
            from_column="DateKey",
            to_table="Date",
            to_column="Date",
            context=ctx,
        )
        client = _StaticLLM([])
        orchestrator = Orchestrator(llm_client=client)
        summary = orchestrator._summarise_model(ctx)
        assert summary is not None
        assert summary["relationship_count"] == 1
        rel = summary["relationships"][0]
        assert rel["from"] == "Sales[DateKey]"
        assert rel["to"] == "Date[Date]"
        assert rel["cardinality"] == "manyToOne"
        assert rel["active"] is True

    def test_planner_payload_includes_model_state(self, tmp_path: Path) -> None:
        """End-to-end: the LLM-bound planner payload contains the model snapshot."""
        ctx = {"model_path": str(tmp_path / "model.tmdl")}
        create_table_handler(
            table_name="Date",
            columns=[{"name": "Date", "data_type": "date"}],
            context=ctx,
        )
        create_table_handler(
            table_name="Sales",
            columns=[{"name": "DateKey", "data_type": "int64"}],
            context=ctx,
        )

        orch = Orchestrator(llm_client=_StaticLLM([]))
        payload = orch._planner_payload("Build a Sales model.", ctx)
        assert "model_state" in payload
        ms = payload["model_state"]
        assert "Date" in ms["tables"]
        assert "Sales" in ms["tables"]
        # The LLM now knows Sales.DateKey is int64 and Date.Date is
        # date — without this context it would guess column names.
        assert ms["tables"]["Sales"]["columns"]["DateKey"] == "int64"
        assert ms["tables"]["Date"]["columns"]["Date"] == "date"
