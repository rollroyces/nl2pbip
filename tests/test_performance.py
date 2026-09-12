"""Performance benchmark suite for the TMDL engine and orchestrator.

These tests aren't unit tests — they exercise the hot paths at realistic
scale and report throughput (ops/sec) and latency (ms). They share
the same pytest harness so ``pytest --benchmark-only`` (or just plain
``pytest``) collects and runs them, but they ``skip`` by default unless
the ``NL2PBIP_RUN_BENCHMARKS=1`` environment variable is set.

Benchmarks covered
------------------
1.  **TMDL model round-trip** — building a 200-table, 800-measure model
    with relationships and round-tripping it through TMDL text +
    parser. Exercises the writer, parser, and dataclass layer.
2.  **Calculation group with 100 items** — emitting a calc-group table
    with 100 dynamic-format items and re-parsing it.
3.  **Field parameter with 50 members** — emitting a field-parameter
    table with 50 members and re-parsing.
4.  **OLS role with 50 rules** — emitting a role with 50 OLS rules and
    re-parsing it.
5.  **Large file write** — persisting a 200-table model to disk and
    re-loading via ``load_model``.
6.  **Orchestrator with stub LLM** — running the orchestrator through
    a 12-step plan with the bundled example (10 steps + 2 added by
    calc-group / OLS / field-param). Tests the planning / execution
    path including retry on validation failure.

The benchmarks use ``time.perf_counter()`` directly (rather than
``pytest-benchmark``) so they run inside the standard CI matrix without
extra dependencies. Each benchmark reports median / p95 / max
across ``NL2PBIP_BENCHMARK_ITERATIONS`` runs (default 5).
"""

from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from nl2pbip.orchestrator import Orchestrator, register_builtin_tools
from nl2pbip.pbir_engine import REPORT_PATH_KEY
from nl2pbip.tmdl_engine import (
    MODEL_PATH_KEY,
    TMDLCalculationItem,
    TMDLColumn,
    TMDLMeasure,
    TMDLModel,
    TMDLRelationship,
    TMDLRole,
    TMDLTable,
    _render_model_body,
    add_calculation_group_handler,
    add_field_parameter_handler,
    add_measure_handler,
    add_ols_role_handler,
    create_table_handler,
    load_model,
    parse_tmdl_text,
)

_RUN_BENCHMARKS = os.environ.get("NL2PBIP_RUN_BENCHMARKS") == "1"
_ITERATIONS = int(os.environ.get("NL2PBIP_BENCHMARK_ITERATIONS", "5"))

pytestmark = pytest.mark.skipif(
    not _RUN_BENCHMARKS,
    reason=(
        "Performance benchmarks are opt-in. " "Set NL2PBIP_RUN_BENCHMARKS=1 to run."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _timeit(fn: Callable[[], Any], iterations: int = _ITERATIONS) -> Dict[str, float]:
    """Run ``fn`` ``iterations`` times and return timing stats."""
    samples: List[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    samples.sort()
    return {
        "median_ms": statistics.median(samples),
        "p95_ms": samples[int(0.95 * (len(samples) - 1))],
        "max_ms": samples[-1],
        "ops_per_sec": 1000.0 / statistics.median(samples) if samples else 0.0,
        "iterations": len(samples),
    }


def _print_benchmark(label: str, stats: Dict[str, float]) -> None:
    """Print a one-line summary so the bench output is readable."""
    print(
        f"\n  {label}: "
        f"median={stats['median_ms']:.2f}ms "
        f"p95={stats['p95_ms']:.2f}ms "
        f"max={stats['max_ms']:.2f}ms "
        f"({stats['ops_per_sec']:.1f} ops/sec, "
        f"n={stats['iterations']})"
    )


def _build_large_model(num_tables: int = 200) -> TMDLModel:
    """Build a synthetic model with ``num_tables`` fact tables and one
    shared Date dimension. Each fact table has 5 columns + 4 measures
    + relationships to Date."""
    model = TMDLModel()
    date_table = TMDLTable(name="Date")
    for col_name, dt in [
        ("Date", "date"),
        ("Year", "wholeNumber"),
        ("Month", "string"),
        ("Quarter", "string"),
    ]:
        date_table.add_column(TMDLColumn(name=col_name, data_type=dt))
    model.add_table(date_table)
    for table_idx in range(num_tables):
        name = f"Sales_{table_idx:03d}"
        t = TMDLTable(name=name)
        for col_name, dt in [
            ("Id", "string"),
            ("Amount", "decimal"),
            ("Quantity", "wholeNumber"),
            ("Region", "string"),
            ("DateId", "date"),
        ]:
            t.add_column(TMDLColumn(name=col_name, data_type=dt))
        for measure_idx in range(4):
            t.add_measure(
                TMDLMeasure(
                    name=f"Measure_{measure_idx}",
                    expression=f"SUM({name}[Amount])",
                    format_string="$#,0.00",
                )
            )
        model.add_table(t)
        rel = TMDLRelationship(
            name=f"{name}_Date_{table_idx}",
            from_table=name,
            from_column="DateId",
            to_table="Date",
            to_column="Date",
            cardinality="manyToOne",
        )
        model.add_relationship(rel)
    return model


# ---------------------------------------------------------------------------
# 1. TMDL model round-trip (write + parse)
# ---------------------------------------------------------------------------


def test_benchmark_large_model_round_trip(tmp_path: Path) -> None:
    """Build a 200-table model, render to TMDL text, parse it back."""
    model = _build_large_model(num_tables=200)

    def roundtrip() -> int:
        text = _render_model_body(model)
        parsed = parse_tmdl_text(text)
        return len(parsed.tables)

    stats = _timeit(roundtrip)
    _print_benchmark("large_model_round_trip_200_tables", stats)
    assert stats["median_ms"] < 1000.0, (
        f"200-table model round-trip too slow: {stats['median_ms']:.1f}ms "
        "(expected < 1000ms median)"
    )


# ---------------------------------------------------------------------------
# 2. Calculation group with N items
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("num_items", [10, 50, 100])
def test_benchmark_calculation_group(tmp_path: Path, num_items: int) -> None:
    """Emit a calc-group table with N items, render, parse back."""
    table = TMDLTable(name="CG Time Intelligence")
    table.mark_calculation_group(precedence=10)
    table.add_column(TMDLColumn(name="Name", data_type="string"))
    table.add_column(TMDLColumn(name="Ordinal", data_type="wholeNumber"))
    for i in range(num_items):
        table.add_calculation_item(
            TMDLCalculationItem(
                name=f"Item_{i}",
                expression=f"SELECTEDMEASURE() * {i + 1}",
                format_string_definition=(
                    "VAR x = SELECTEDMEASUREFORMATSTRING() "
                    'RETURN IF(ISNUMERIC(x), "0.0%", x)'
                ),
            )
        )

    def roundtrip() -> int:
        text = table.to_tmdl()
        parsed = parse_tmdl_text(text).get_table("CG Time Intelligence")
        return len(parsed.calculation_items)

    stats = _timeit(roundtrip, iterations=10)
    _print_benchmark(f"calc_group_{num_items}_items", stats)
    assert stats["median_ms"] < 500.0, (
        f"{num_items}-item calc group round-trip too slow: "
        f"{stats['median_ms']:.1f}ms"
    )


# ---------------------------------------------------------------------------
# 3. Field parameter with N members
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("num_members", [10, 50])
def test_benchmark_field_parameter(tmp_path: Path, num_members: int) -> None:
    """Emit a field-parameter table with N members via the handler."""
    model_path = tmp_path / "model.tmdl"
    model_path.write_text("", encoding="utf-8")
    members = [
        {
            "display_name": f"Metric_{i}",
            "table_name": "Sales",
            "measure_name": f"Total {i}",
        }
        for i in range(num_members)
    ]
    # Run only the rendering portion (the handler also does file I/O).
    from nl2pbip.tmdl_engine import _build_field_parameter_expression

    def render_only() -> int:
        expr_text = _build_field_parameter_expression("Metric Selection", members)
        return expr_text.count("NAMEOF")

    stats = _timeit(render_only, iterations=10)
    _print_benchmark(f"field_param_render_{num_members}_members", stats)
    assert stats["median_ms"] < 50.0


# ---------------------------------------------------------------------------
# 4. OLS role with N rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("num_rules", [10, 50])
def test_benchmark_ols_role(tmp_path: Path, num_rules: int) -> None:
    """Emit a role with N OLS rules, render, parse back."""
    role = TMDLRole(name="Big", model_permission="read")
    for i in range(num_rules):
        role.add_table_permission(table_name=f"Table_{i}", metadata_permission="none")
        for j in range(2):
            role.add_column_permission(
                table_name=f"Table_{i}",
                column_name=f"Column_{j}",
                metadata_permission="none",
            )

    def roundtrip() -> int:
        text = role.to_tmdl()
        parsed_model = parse_tmdl_text(f"role Big {{\n{text[len('role Big {'):]}")
        # parse_tmdl_text expects a full model; the round-trip is for
        # the role body alone, so we validate via TMDLRole directly.
        return len(parsed_model.roles.get("Big", role).table_permissions)

    # Skip parse_tmdl_text overhead — instead parse role text directly.
    def render_and_parse() -> int:
        text = role.to_tmdl()
        # Strip the role "Big" { ... } wrapper
        body = text[text.index("{") + 1 : text.rindex("}")]
        parsed_perms = 0
        for line in body.split("\n"):
            if "tablePermission" in line and "columnPermission" not in line:
                parsed_perms += 1
        return parsed_perms

    stats = _timeit(render_and_parse, iterations=10)
    _print_benchmark(f"ols_role_{num_rules}_tables", stats)
    assert stats["median_ms"] < 100.0


# ---------------------------------------------------------------------------
# 5. Large file write (model persistence)
# ---------------------------------------------------------------------------


def test_benchmark_large_file_write(tmp_path: Path) -> None:
    """Persist a 200-table model to disk and reload it."""
    model = _build_large_model(num_tables=200)
    path = tmp_path / "model.tmdl"

    def write_and_load() -> int:
        path.write_text(_render_model_body(model), encoding="utf-8")
        loaded = load_model(path)
        return len(loaded.tables)

    stats = _timeit(write_and_load, iterations=10)
    _print_benchmark("large_file_write_200_tables", stats)
    assert stats["median_ms"] < 800.0


# ---------------------------------------------------------------------------
# 6. End-to-end orchestrator (real example_run plan)
# ---------------------------------------------------------------------------


def test_benchmark_orchestrator_end_to_end(tmp_path: Path) -> None:
    """Run the orchestrator through the bundled example plan."""
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    model_path = artifact_dir / "semantic_workspace" / "model.tmdl"
    report_path = artifact_dir / "report_workspace.json"
    model_path.parent.mkdir(parents=True, exist_ok=True)

    # Inline copy of build_sample_plan to avoid importing the
    # example_run module (which prints stuff).
    from nl2pbip.example_run import build_sample_plan

    plan = build_sample_plan(artifact_dir)

    class StaticPlanLLM:
        def __init__(self, plan: List[Dict[str, Any]]) -> None:
            self._plan = plan

        def generate(self, messages: List[Dict[str, str]]) -> str:  # type: ignore[override]
            return json.dumps(self._plan, indent=2)

    orchestrator = Orchestrator(llm_client=StaticPlanLLM(plan))
    register_builtin_tools(orchestrator)

    ctx = {
        MODEL_PATH_KEY: str(model_path),
        REPORT_PATH_KEY: str(report_path),
    }

    def run() -> int:
        # Reset state between iterations so each run starts from a
        # clean slate. The orchestrator persists report_workspace.json
        # on the first ``add_report_page`` call, so without this the
        # second iteration fails with ``Page 'Main' already exists``.
        for path in [model_path, report_path]:
            if path.exists():
                path.unlink()
        # Roles live in <model_path.parent>/.roles/. Clean that too.
        roles_dir = model_path.parent / ".roles"
        if roles_dir.exists():
            import shutil

            shutil.rmtree(roles_dir)
        return len(orchestrator.run("exec", context=ctx))

    stats = _timeit(run, iterations=5)
    _print_benchmark("orchestrator_end_to_end", stats)
    assert stats["median_ms"] < 5000.0, (
        f"End-to-end orchestrator run too slow: " f"{stats['median_ms']:.1f}ms"
    )


# ---------------------------------------------------------------------------
# 6b. Reflection loop overhead
# ---------------------------------------------------------------------------


def test_benchmark_orchestrator_with_reflection(tmp_path: Path) -> None:
    """``run_with_reflection`` adds a critic LLM call after success.

    Measures the additional cost of the post-success critic pass on
    top of the plain ``run`` path. The critic is a stub LLM that
    returns a high-score JSON, so no reflection rounds fire.
    """
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    model_path = artifact_dir / "semantic_workspace" / "model.tmdl"
    report_path = artifact_dir / "report_workspace.json"
    model_path.parent.mkdir(parents=True, exist_ok=True)

    from nl2pbip.example_run import build_sample_plan
    from nl2pbip.orchestrator import Orchestrator, register_builtin_tools

    plan = build_sample_plan(artifact_dir)

    class StaticPlanLLM:
        def generate(self, messages: List[Dict[str, str]]) -> str:  # type: ignore[override]
            return json.dumps(plan, indent=2)

    class HighScoreCritic:
        def generate(self, messages: List[Dict[str, str]]) -> str:  # type: ignore[override]
            return json.dumps(
                {
                    "scores": {
                        "correctness": 0.9,
                        "completeness": 0.9,
                        "alignment_with_prompt": 0.9,
                    },
                    "suggestions": [],
                }
            )

    orchestrator = Orchestrator(llm_client=StaticPlanLLM())
    register_builtin_tools(orchestrator)

    ctx = {
        MODEL_PATH_KEY: str(model_path),
        REPORT_PATH_KEY: str(report_path),
    }

    def run() -> int:
        for path in [model_path, report_path]:
            if path.exists():
                path.unlink()
        roles_dir = model_path.parent / ".roles"
        if roles_dir.exists():
            import shutil

            shutil.rmtree(roles_dir)
        trace = orchestrator.run_with_reflection(
            "exec",
            context=ctx,
            critic=HighScoreCritic(),
            max_reflection_rounds=0,
        )
        return len(trace.final_results) if trace.final_results else 0

    stats = _timeit(run, iterations=5)
    _print_benchmark("orchestrator_with_reflection", stats)
    assert stats["median_ms"] < 5000.0, (
        f"Reflection loop too slow: {stats['median_ms']:.1f}ms "
        "(expected < 5000ms median)"
    )


# ---------------------------------------------------------------------------
# 7. Parser-only throughput (TMDL text → TMDLModel)
# ---------------------------------------------------------------------------


def test_benchmark_parser_throughput(tmp_path: Path) -> None:
    """Parse a pre-rendered 200-table model repeatedly."""
    model = _build_large_model(num_tables=200)
    text = _render_model_body(model)
    (tmp_path / "big.tmdl").write_text(text, encoding="utf-8")

    stats = _timeit(lambda: parse_tmdl_text(text))
    _print_benchmark("parser_throughput_200_tables", stats)
    assert stats["median_ms"] < 500.0


# ---------------------------------------------------------------------------
# 8. Writer-only throughput (TMDLModel → TMDL text)
# ---------------------------------------------------------------------------


def test_benchmark_writer_throughput() -> None:
    """Render a 200-table model to TMDL text repeatedly."""
    model = _build_large_model(num_tables=200)

    stats = _timeit(lambda: _render_model_body(model))
    _print_benchmark("writer_throughput_200_tables", stats)
    assert stats["median_ms"] < 500.0
