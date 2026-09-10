# nl2pbip

Natural Language to Power BI Project (.pbip) Engine

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg) ![License: Commercial](https://img.shields.io/badge/license-Commercial-orange.svg) ![CI](https://github.com/rollroyces/nl2pbip/actions/workflows/ci.yml/badge.svg)

## Overview

`nl2pbip` converts free-form analytics requirements into Git-friendly Power BI Project (`.pbip`) directories. It orchestrates a large language model (LLM) planner, DAX/TMDL tooling, PBIR layout builders, deterministic data inspection, curated ontology grounding, and an OPC-compliant `.pbit` exporter — so that every natural-language prompt becomes:

- A **semantic model** expressed in Tabular Model Definition Language (TMDL)
- A **report surface** captured as PBIR JSON (pages, visuals, bindings)
- A **packaged `.pbip` workspace** that can be exported to `.pbix` or `.pbit`

Beyond model generation, `nl2pbip` ships an **LLM context layer** that profiles your data, validates your relationships against Power BI Desktop's actual constraints, anchors column names to a curated `schema.org`/`PROV-O` subset, and asks the LLM for column roles / measures / visuals with concrete numbers rather than guesses. The system prompt that guides the LLM is **tuned for report generation** — 30 rules covering narrative flow, visual selection by data shape, layout, and measure–visual pairing — so plans produce layouts a senior Power BI designer would approve of rather than a pile of charts.

## Architecture at a glance

| Layer | Module | What it does |
|---|---|---|
| Planner | `StructuredLLMClient` | Normalizes responses from OpenAI, Azure OpenAI, Anthropic, DeepSeek, Qwen, Zhipu, Moonshot, or any OpenAI-compatible endpoint |
| **Prompts** | `prompts` | Versioned system + user messages tuned for report generation. 30 numbered rules covering narrative flow, visual selection by data shape, layout, measure–visual pairing, filters, and TMDL/relationship/RLS/OLS plumbing. Legacy prompt preserved for opt-out. |
| Agent runtime | `Orchestrator` | Validates the plan, dispatches domain tools, retries after lint feedback. Six orthogonal context blocks feed the LLM (see [LLM context layer](#llm-context-layer)). The planner payload exposes a `prompt_meta` block (`version`, `name`, `focused_on_report_generation`) so callers can audit which prompt produced a given plan. |
| TMDL engine | `tmdl_engine` | Parser, writer, 7 handlers (create_table, add_measure, define_relationship, …). Data-type and visual-type aliases normalised. Relationships validated for endpoint existence, type compatibility, self-refs, and duplicate-active guards. |
| Visual engine | `pbir_engine` | PBIR layout, OPC-compliant `.pbit` archive builder with `[Content_Types].xml` + manifest parts |
| Data context | `data_inspector` | Deterministic per-column profile: type inference, distinct counts (top-N by frequency), numeric stats, date ranges, sample-bounded. |
| Cross-table analysis | `data_understanding` | PK detection, FK coverage with orphan counts, cardinality hints, numeric quantiles, time ranges |
| Ontology grounding | `ontology` | Curated ~39 schema.org Types + ~72 Properties + ~71 alias entries + 9 PROV-O terms (~50 KB, no external deps) |
| AI schema advisor | `schema_advisor` | LLM-driven column-role / measure / visual suggestions with result caching and tolerant JSON parsing |
| Exporter | `PBIPExporter` | Calls `pbi-tools compile` for `.pbix`; falls back to in-process ZIP for `.pbit` |
| Fine-tune suite | `nl2pbip.finetune` | Synthetic dataset generation, QLoRA training with Unsloth, GGUF export for Ollama / vLLM |

## LLM context layer

The orchestrator assembles a planner payload that gives the LLM enough structure to design relationships and visuals with concrete numbers rather than guessing. Six orthogonal blocks:

| Block | Source | What it tells the LLM |
|---|---|---|
| `model_state` | TMDL engine | Existing tables, columns, types, relationships |
| `data_profile` | `data_inspector` | Per-column stats: distinct counts, top examples, numeric range, null rate |
| `ontology_hints` | `ontology` | schema.org vocabulary anchor — `customerEmail` → `schema.org/email` |
| `data_understanding` | `data_understanding` | PKs, FK coverage (orphan counts), cardinality hints, numeric quantiles, time ranges |
| `ai_schema_hints` | `schema_advisor` | LLM's own assessment of column roles + measure + visual suggestions |
| `dax_catalog` | user-supplied | Organisation-specific DAX patterns the planner should prefer |

Example payload shape with 3 tables (Customer, Order, Product) and 8 Order rows:

```json
{
  "model_state":      { "tables": {...}, "relationships": [...] },
  "data_profile":     { "tables": [{ "name": "Order", "columns": [...] }] },
  "ontology_hints":   { "columns": { "customerEmail": [{"iri": "schema.org/email", "score": 1.0}] }},
  "data_understanding": {
    "primary_keys":        [{ "table": "Order", "column": "orderId", "confidence": "strong" }],
    "relationship_coverage": [{
      "from_table": "Order", "from_column": "customerEmail",
      "to_table": "Customer", "to_column": "customerEmail",
      "matching_rows": 7, "orphan_rows": 1, "coverage_ratio": 0.875,
      "cardinality_hint": "manyToOne"
    }],
    "numeric_distributions": [{
      "column": "total", "p25": 88.7, "p50": 134.8, "p75": 281.3,
      "skew": "right", "likely_outliers": 1
    }],
    "time_ranges": [{ "column": "orderDate", "min_date": "2024-01-15", "max_date": "2024-05-30" }]
  },
  "ai_schema_hints":  { "column_semantics": [...], "measure_suggestions": [...], "visual_suggestions": [...] },
  "dax_catalog":      { "patterns": [...] }
}
```

Every block is **optional** (set the corresponding context flag to `False` to opt out) and computed **deterministically** — no LLM call required unless `ai_schema_hints` is enabled and an LLM client is wired up.

## Planner prompts

Every string the orchestrator sends to the LLM lives in `nl2pbip.prompts`:

| Symbol | Purpose |
|---|---|
| `REPORT_GENERATION_SYSTEM_PROMPT` | The 30-rule system prompt tuned for report generation. Output contract, narrative composition (overview → breakdown → detail), visual selection (data shape → visual type), layout (no overlap, slicer strip), measure–visual pairing, filters, and TMDL/security rules. |
| `LEGACY_GENERIC_SYSTEM_PROMPT` | The original 9-rule generic prompt, preserved verbatim for callers that opt out. |
| `select_system_prompt(context)` | Returns the focused prompt by default, or the legacy prompt if `context["report_focus_enabled"] = False`. The returned string is prefixed with `[nl2pbip prompt vN (name)]` so callers can log / pin the version they received. |
| `build_user_message(user_prompt, payload)` | Assembles the user-side message: `user_prompt + indented JSON payload`. |
| `build_feedback_message(error)` | Retry-feedback message with type-specific guidance for `TMDLValidationError` and `PBIRValidationError`. |
| `prompt_metadata(context)` | Returns `{version, name, focused_on_report_generation}` for the planner payload's `prompt_meta` block. |
| `PROMPT_VERSION` / `PROMPT_CHANGELOG` | Numeric version and append-only changelog. Tests assert the current version has an entry so silent drift is caught. |

Visual-selection rules (a sample of what's in the prompt):

| Data shape | Visual type |
|---|---|
| Single scalar | `card` or `kpi` (with comparison target if YoY) |
| Categorical comparison | `barChart` (horizontal if labels long) or `columnChart` |
| Trend over time | `lineChart` / `areaChart` / `ribbonChart` |
| Two-variable distribution | `scatterChart` |
| Geographic | `map` |
| Hierarchical breakdown | `treemap` or `decompositionTree` |
| Parts of whole (≤6 slices) | `pieChart`; ≤8 → `donutChart`; otherwise sorted `barChart` |
| Multi-dimensional grid | `pivotTable`; flat row × column → `tableEx` |
| Process funnel | `funnel` |
| Single value vs target | `gauge` or `kpi` |

## Installation

```bash
pip install nl2pbip
```

Optional extras provide heavyweight dependencies only when you need them:

- **Fine-tuning** (datasets + Unsloth + transformers):
  ```bash
  pip install "nl2pbip[finetune]"
  ```

- **Anthropic provider** (Claude API client):
  ```bash
  pip install "nl2pbip[anthropic]"
  ```

> **Tip:** Install `pbi-tools` separately (`dotnet tool install --global TabularEditor.Tools.PBITools`) so `python -m nl2pbip.cli export` can emit `.pbix`. Without it, the exporter automatically creates an OPC-compliant `.pbit` template.

## Quickstart & Core Usage

### CLI: cloud-hosted LLMs

`nl2pbip` ships with an `argparse`-based CLI in `nl2pbip.cli`. Two commands: `generate` and `export`. Available flags include `--prompt`, `--provider`, `--model`, `--base-url`, `--api-key`, `--workspace`, `--output`, `--project-name`, `--dax-library`, `--export`, `--export-output`, and `--api-version`. Run `python -m nl2pbip.cli generate --help` for the full list.

### CLI: local inference endpoints (Ollama, vLLM, LM Studio)

```bash
python -m nl2pbip.cli generate \
  --prompt "Marketing attribution model with creative/channel filters" \
  --provider custom \
  --base-url http://localhost:11434/v1 \
  --api-key «redacted:sk-…» \
  --model mistral-openorca \
  --workspace artifacts/workspace-local \
  --output artifacts/pbip-local
```

`StructuredLLMClient` just needs an OpenAI-compatible HTTP surface; the `custom` provider lets you point at self-hosted gateways with dummy API keys.

### Python API: programmatic generation with LLM context

The orchestrator picks up `data_sources`, `ontology_hints_enabled`, and other flags from the caller's context. Register your data and let the LLM design the model:

```python
from pathlib import Path
from nl2pbip.orchestrator import Orchestrator, register_builtin_tools
from nl2pbip.tmdl_engine import MODEL_PATH_KEY
from nl2pbip.pbir_engine import REPORT_PATH_KEY
from nl2pbip.llm_client import StructuredLLMClient

ctx = {
    MODEL_PATH_KEY: "artifacts/workspace/model.tmdl",
    REPORT_PATH_KEY: "artifacts/workspace/report.json",
    "project_name": "SalesInsights",
    "package_path": "artifacts/pbip-sales",
    "data_sources": {
        "Customer": [
            {"customerEmail": "alice@bigco.com", "firstName": "Alice",
             "customerSince": "2022-03-15", "countryCode": "US"},
            {"customerEmail": "bob@bigco.com", "firstName": "Bob",
             "customerSince": "2023-01-10", "countryCode": "GB"},
        ],
        "Order": [
            {"orderId": "A1", "orderDate": "2024-01-15", "total": 99.99,
             "customerEmail": "alice@bigco.com"},
            {"orderId": "A2", "orderDate": "2024-02-20", "total": 149.50,
             "customerEmail": "alice@bigco.com"},
        ],
    },
    # Optional flags (all default to True):
    # "ai_schema_hints_enabled": True,
    # "ontology_hints_enabled": True,
    # "data_understanding_enabled": True,
}

llm = StructuredLLMClient(provider="openai", model="gpt-4o-mini")
orch = Orchestrator(llm_client=llm)
register_builtin_tools(orch)

results = orch.run("Build a sales report with revenue per customer", context=ctx)
print(results[-1].output["project_path"])
```

The orchestrator returns structured `ToolResult` objects so you can inspect intermediate tool outputs, log them, or enforce custom compliance checks before persisting artifacts.

### Type & visual handling

The TMDL handler accepts both schema.org-native and SQL-flavored spellings — aliases resolve to the canonical form:

```python
TMDLColumn(name="id", data_type="bigint")  # → int64
TMDLColumn(name="price", data_type="money")  # → decimal
TMDLColumn(name="region", data_type="varchar")  # → string
```

Visual-type aliases work the same way:

```python
add_visual_handler(page="Main", visual_type="table", ...)   # → tableEx
add_visual_handler(page="Main", visual_type="matrix", ...)  # → pivotTable
add_visual_handler(page="Main", visual_type="pie", ...)     # → pieChart
```

`define_relationship_handler` validates the endpoint columns exist, the data types are compatible, the cardinality is valid, and there's no duplicate active relationship on the same from-side endpoint. Errors surface with enough detail for the LLM retry loop to self-correct.

## LLM Fine-Tuning Module (`nl2pbip.finetune`)

Fine-tune open-weight coders such as **Qwen 2.5 Coder 7B** on curated TMDL + PBIR schemas to run `nl2pbip` entirely offline. The module ships two scripts that you run as Python modules — there is no separate CLI:

1. **Synthetic dataset generation** – uses `instructor` + `openai` to capture validated ChatML records:

   ```bash
   python -m nl2pbip.finetune.dataset_generator \
     --model gpt-4o-mini \
     --prompt-file prompts/nl_requirements.txt \
     --train-output finetune/train.jsonl \
     --val-output finetune/val.jsonl
   ```

2. **QLoRA training with Unsloth** – wraps `unsloth.FastLanguageModel`, `trl.SFTTrainer`, and Hugging Face datasets:

   ```bash
   python -m nl2pbip.finetune.train \
     --train-file finetune/train.jsonl \
     --val-file finetune/val.jsonl \
     --output-dir finetune/output \
     --model-name unsloth/Qwen2.5-Coder-7B-Instruct \
     --epochs 2 \
     --batch-size 1 \
     --gradient-accumulation 4 \
     --export-gguf
   ```

3. **Export GGUF for Ollama / llama.cpp runtimes** – uses `FastLanguageModel.export_gguf` with quantization presets. The `--export-gguf` flag above does this automatically, writing adapters + GGUF artifacts under `finetune/output/gguf/`:

   ```text
   finetune/output/
   ├── adapter/           # LoRA adapter (PEFT)
   ├── checkpoints/       # intermediate epochs
   └── gguf/              # GGUF export for Ollama / llama.cpp
   ```

Artifacts created in `finetune/output/gguf` can be served through Ollama (`ollama create nl2pbip -f Modelfile`) or vLLM. Point `python -m nl2pbip.cli generate --provider custom --base-url http://localhost:8000/v1` at that endpoint for private inference.

## Export Engine (`.pbip` → `.pbix` / `.pbit`)

`PBIPExporter` stitches semantic + report directories into binary Power BI files. You can call it directly or via CLI:

### CLI exports

```bash
# Convert an existing PBIP directory to PBIX
python -m nl2pbip.cli export \
  --input artifacts/pbip-20240925/SalesInsights.pbipdir \
  --format pbix \
  --output dist/SalesInsights.pbix

# Generate a template (PBIT) during plan execution
python -m nl2pbip.cli generate \
  --prompt "Finance P&L with departmental RLS" \
  --export-format pbit \
  --export-output dist/FinanceTemplate.pbit
```

- `.pbix` exports require `pbi-tools` to be on `PATH`. The exporter streams `pbi-tools compile` and surfaces the log output.
- `.pbit` exports fall back to an **OPC-compliant** in-process ZIP that writes the canonical Power BI Desktop manifest parts:
  - `[Content_Types].xml` at the archive root with UTF-8 BOM
  - `Version`, `Metadata`, `Settings`, `SecurityBindings`, `DiagramLayout` at the root
  - `DataModelSchema` (TMSL JSON), `DataMashup` (8-byte magic header + embedded ZIP)
  - `Report/Layout` (collapsed JSON), optional `Report/StaticResources/...` and `Report/CustomVisuals/...`
  - Forward-slash paths everywhere (Windows `Compress-Archive` would break Power BI)

### Python API exports

```python
from nl2pbip.exporter import PBIPExporter

exporter = PBIPExporter()
pbip_dir = "artifacts/pbip-sales/SalesInsights.pbipdir"

if not exporter.export_with_pbi_tools(pbip_dir, "pbix", "dist/SalesInsights.pbix"):
    exporter.export_as_pbit_zip(pbip_dir, "dist/SalesInsights.pbit")
```

This dual-path approach lets CI or air-gapped build agents still emit templates even when `pbi-tools` is unavailable.

## CI/CD Integration

A ready-to-use workflow lives in `.github/workflows/ci.yml` and executes formatting, linting, and tests against Python 3.10–3.12. Minimal template:

```yaml
name: CI
on:
  push:
    branches: [main, master]
  pull_request:
    branches: [main, master]

jobs:
  tests:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: pip
      - name: Install deps
        run: |
          python -m pip install --upgrade pip
          if [ -f requirements.txt ]; then
            pip install -r requirements.txt
          elif [ -f pyproject.toml ]; then
            pip install .
          fi
          # Test-only extras: pytest, lint, formatter. The finetune + anthropic
          # extras require heavy ML libs and are not needed for unit tests.
          pip install ".[dev]" ".[anthropic]"
          # finetune.train does top-level imports of transformers/trl/datasets,
          # so we need them installed for the test_orchestrator/test_finetune
          # test discovery even though no GPU-backed training is exercised.
          pip install datasets pydantic "transformers>=4.40" trl
      - name: Lint
        run: |
          black --check .
          ruff check .
      - name: Test
        run: pytest -v --cov=. --cov-report=term-missing
```

> **Note:** The workflow installs `transformers>=4.40` + `trl` so `test_finetune` can import `TrainingArguments` and `SFTTrainer` at module-load time. The full test set runs without those weights — only the import matters.

## Project layout

```
nl2pbip/
├── nl2pbip/
│   ├── orchestrator.py          # planner payload assembly + retry loop
│   ├── llm_client.py            # OpenAI-compatible client + structured output
│   ├── tmdl_engine.py           # parser, writer, 7 handlers (validate + persist)
│   ├── tmdl_linter.py           # TMDLValidationError
│   ├── pbir_engine.py           # PBIR layout, OPC-compliant .pbit archive
│   ├── pbir_validator.py        # PBIR schema + semantic checks
│   ├── packager.py              # package_pbip_handler
│   ├── dax_catalog.py           # organisation-specific DAX patterns
│   ├── dax_library.json         # default DAX pattern library
│   ├── data_inspector.py        # per-column profiling + heuristic FK
│   ├── data_understanding.py    # PK, FK coverage, cardinality, quantiles, time
│   ├── ontology.py              # curated schema.org + PROV-O subset
│   ├── schema_advisor.py        # LLM-driven column-role / measure / visual hints
│   ├── data_types.py            # TMDL data-type aliases + format-string defaults
│   ├── visual_types.py          # PBIR visual-type aliases + default size
│   ├── exporter/
│   │   ├── exporter.py          # pbi-tools + .pbit fallback
│   │   ├── opc.py               # OPC primitives (Content_Types, manifest parts)
│   │   └── pbit_builder.py      # high-level PbitArchiveBuilder
│   ├── providers/
│   │   └── local_finetuned.py   # in-process fine-tuned model provider
│   ├── finetune/
│   │   ├── dataset_generator.py # instructor + OpenAI synthetic data
│   │   └── train.py             # Unsloth + trl SFT + GGUF export
│   ├── example_run.py           # python -m nl2pbip.example_run (idempotent demo)
│   ├── prompts.py               # versioned LLM planner prompts (system + user + feedback)
│   ├── cli.py                   # argparse CLI (generate / export subcommands)
│   ├── py.typed                 # PEP 561 marker
│   └── __init__.py
├── tests/                       # 398 pytest cases across 13 test files
├── artifacts/                   # example Run output (SalesInsights.pbipdir)
├── .github/workflows/ci.yml
├── pyproject.toml
├── LICENSE                      # Commercial + Apache carve-out for pre-0af050c commits
├── CHANGELOG.md
└── README.md
```

## Test counts

Pytest's collection reports 398 test cases across Python 3.10 / 3.11 / 3.12. The breakdown by file:

| Module | Test functions |
|---|---|
| `tests/test_opc_export.py` | 41 |
| `tests/test_data_types.py` | 36 |
| `tests/test_data_inspector.py` | 31 |
| `tests/test_ontology.py` | 30 |
| `tests/test_visual_types.py` | 25 |
| `tests/test_data_understanding.py` | 24 |
| `tests/test_relationship_validation.py` | 23 |
| `tests/test_prompts.py` | 38 |
| `tests/test_schema_advisor.py` | 16 |
| `tests/test_orchestrator.py` | 4 |
| `tests/test_exporter.py` | 3 |
| `tests/test_finetune.py` | 3 |
| `tests/test_llm_client.py` | 2 |

Many tests are parametrised, which is why `pytest --collect-only` reports 398 cases from 276 functions. Run `pytest tests/ --no-header -q` to confirm locally — all 398 cases pass.

## Next steps

- Browse `tests/test_prompts.py` for the report-generation system prompt's content rules — every section keyword has a test that catches silent drift.
- Browse `tests/test_data_understanding.py` for a realistic end-to-end scenario with FK orphans and cardinality hints.
- Extend `dax_library.json` with your own calculation groups and measure templates so planners lean on approved logic.
- Register your raw data via `context["data_sources"]` so the LLM gets FK coverage, P50 of numeric columns, and schema.org vocabulary anchors rather than guessing.
- Pin a specific prompt version via the planner payload's `prompt_meta.version` block for reproducible plan generation.
- Wire `python -m nl2pbip.cli generate` into deployment automation (e.g., GitHub Actions + `pbi-tools push`) to continuously ship fully reproducible Power BI apps from natural-language specs.

## License

`nl2pbip` is **commercial proprietary software**, © 2026 Royce. All rights reserved.

This repository is published for visibility and collaboration under the terms of a **Commercial License Agreement** that you must sign with the Licensor before using the Software. **No open-source license** (MIT, Apache-2.0, GPL, AGPL, BSD, MPL, etc.) is granted by the public repository — viewing the source does not grant you any right to use, copy, modify, or distribute it.

Files committed prior to [`0af050c`](https://github.com/rollroyces/nl2pbip/commit/0af050c23393b3a4605f71ea1c8f49ccdb9cc1fb) retain their original Apache License 2.0 grant as a continuing authorization (see `LICENSE` §5). New contributions from that commit onward are governed by the Commercial License terms in [`LICENSE`](./LICENSE).

To obtain a Commercial License Agreement, contact:

> Royce &lt;rollroyces@users.noreply.github.com&gt;

Your agreement will define scope, duration, fees, support, confidentiality, warranties, and termination.

THE SOFTWARE IS PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND.
