# nl2pbip

Natural Language to Power BI Project (.pbip) Engine

[![PyPI](https://img.shields.io/pypi/v/nl2pbip.svg)](https://pypi.org/project/nl2pbip/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](#installation)
[![License: Commercial](https://img.shields.io/badge/license-Commercial-orange.svg)](#license)
[![CI](https://github.com/rollroyces/nl2pbip/actions/workflows/ci.yml/badge.svg)](https://github.com/rollroyces/nl2pbip/actions/workflows/ci.yml)
[![CodeQL](https://github.com/rollroyces/nl2pbip/actions/workflows/codeql.yml/badge.svg)](https://github.com/rollroyces/nl2pbip/actions/workflows/codeql.yml)
[![Tests](https://img.shields.io/badge/tests-687%20collected%2C%20674%20passing-brightgreen.svg)](#test-counts)
[![Benchmarks](https://img.shields.io/badge/benchmarks-13-blue.svg)](#performance-benchmarks)

## Overview

`nl2pbip` converts free-form analytics requirements into Git-friendly Power BI Project (`.pbip`) directories. It orchestrates a large language model (LLM) planner, DAX/TMDL tooling, PBIR layout builders, deterministic data inspection, curated ontology grounding, and an OPC-compliant `.pbit` exporter — so that every natural-language prompt becomes:

- A **semantic model** expressed in Tabular Model Definition Language (TMDL)
- A **report surface** captured as PBIR JSON (pages, visuals, bindings)
- A **packaged `.pbip` workspace** that can be exported to `.pbix` or `.pbit` and committed to a Git repo connected to a Microsoft Fabric workspace

### What ships in v1.1.1

| Capability | What it gives you |
|---|---|
| **TMDL feature expansion** | Calculation groups + dynamic format strings (Microsoft's `formatStringDefinition`), Object-Level Security (Sept 2025 grammar — nested `tablePermission` / `columnPermission`), field parameters for dynamic measure / column / table switching via slicer, and Power Query (M) partition generation with six source templates (`csv` / `sql` / `json` / `sharepoint` / `odata` / `web`) plus `promotedTable` / `merge` / `append` / `group` transformations. |
| **Fabric Git integration** | `package_pbip` writes `itemMetadata.json` + `.platform` per Fabric item so the PBIP folder is ready for a Git repo connected to a Fabric workspace. Validated against the 20 canonical Fabric item types. |
| **Agentic self-reflection loop** | `Orchestrator.run_with_reflection` records every attempt in a `ReflectiveTrace`, then runs a post-success critic pass (`correctness` / `completeness` / `alignment_with_prompt` scores) and re-invokes the planner with the critic's suggestions when the score is below threshold. New `PlannerClarification` exception lets the LLM ask the user clarifying questions instead of guessing. |
| **Performance benchmarks** | 13 opt-in benchmarks covering TMDL writer / parser round-trip on a 200-table model, calc-group / field-param / OLS rendering at multiple sizes, end-to-end orchestrator latency, and the reflection-loop overhead. See [Performance benchmarks](#performance-benchmarks). |
| **GitHub repo templates** | Issue templates, PR template, CODEOWNERS, Dependabot (pip + GitHub Actions, weekly), CodeQL workflow, release workflow (PyPI trusted publishing via OIDC + GitHub release), `SECURITY.md`, `CONTRIBUTING.md`. CI workflow hardened with concurrency groups, permissions hardening, and a smoke test that catches `package_pbip` regressions. |
| **PyPI published releases** | Tag-push driven release workflow. `v1.1.0` and `v1.1.1` are live at https://pypi.org/project/nl2pbip/. Trusted publishing via OIDC — no API token stored in the repo. |

Beyond model generation, `nl2pbip` ships an **LLM context layer** that profiles your data, validates your relationships against Power BI Desktop's actual constraints, anchors column names to a curated `schema.org`/`PROV-O` subset, and asks the LLM for column roles / measures / visuals with concrete numbers rather than guesses. The system prompt that guides the LLM is **tuned for report generation** — 30 rules covering narrative flow, visual selection by data shape, layout, and measure–visual pairing — so plans produce layouts a senior Power BI designer would approve of rather than a pile of charts.

## Architecture at a glance

| Layer | Module | What it does |
|---|---|---|
| Planner | `StructuredLLMClient` | Normalizes responses from OpenAI, Azure OpenAI, Anthropic, DeepSeek, Qwen, Zhipu, Moonshot, or any OpenAI-compatible endpoint |
| **Prompts** | `prompts` | Versioned system + user messages tuned for report generation. 30 numbered rules covering narrative flow, visual selection by data shape, layout, measure–visual pairing, filters, and TMDL/relationship/RLS/OLS plumbing. Legacy prompt preserved for opt-out. |
| Agent runtime | `Orchestrator` | Validates the plan, dispatches domain tools, retries after lint feedback. Six orthogonal context blocks feed the LLM (see [LLM context layer](#llm-context-layer)). The planner payload exposes a `prompt_meta` block (`version`, `name`, `focused_on_report_generation`) so callers can audit which prompt produced a given plan. |
| TMDL engine | `tmdl_engine` | Parser, writer, 11 handlers — `create_table`, `add_measure`, `define_relationship`, `add_calculation_group` (with `formatStringDefinition`), `add_ols_role` (Microsoft Sept 2025 grammar), `add_field_parameter`, `add_power_query_partition`, `add_visual`, `add_report_page`, `package_pbip`. Data-type and visual-type aliases normalised. Relationships validated for endpoint existence, type compatibility, self-refs, and duplicate-active guards. |
| Visual engine | `pbir_engine` | PBIR layout, OPC-compliant `.pbit` archive builder with `[Content_Types].xml` + manifest parts |
| Power Query M | `m_builder` | Safe M-string quoting (`""` escape), six source templates (`csv` / `sql` / `json` / `sharepoint` / `odata` / `web`), and four transformation builders (`promotedTable` / `merge` / `append` / `group`) |
| Packager | `packager` | Writes the PBIP folder, Fabric `itemMetadata.json` + `.platform` per item, validated against 20 canonical Fabric item types |
| Data context | `data_inspector` | Deterministic per-column profile: type inference, distinct counts (top-N by frequency), numeric stats, date ranges, sample-bounded. |
| Cross-table analysis | `data_understanding` | PK detection, FK coverage with orphan counts, cardinality hints, numeric quantiles, time ranges |
| Ontology grounding | `ontology` | Curated ~39 schema.org Types + ~72 Properties + ~71 alias entries + 9 PROV-O terms (~50 KB, no external deps) |
| AI schema advisor | `schema_advisor` | LLM-driven column-role / measure / visual suggestions with result caching and tolerant JSON parsing |
| Agent runtime | `Orchestrator` | Validates the plan, dispatches domain tools, retries after lint feedback. `run()` does validation-aware self-correction; `run_with_reflection()` adds a persistent trace + post-success critic pass + reflection loop. New `PlannerClarification` exception lets the LLM ask clarifying questions instead of guessing. Six orthogonal context blocks feed the LLM (see [LLM context layer](#llm-context-layer)). The planner payload exposes a `prompt_meta` block (`version`, `name`, `focused_on_report_generation`) so callers can audit which prompt produced a given plan. |
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

Every string the orchestrator sends to the LLM lives in `nl2pbip.prompts`. Current version is **v5** (incremented in v1.1.0 with the power-query, OLS, field-param, fabric, and agentic-reflection rules):

| Symbol | Purpose |
|---|---|
| `REPORT_GENERATION_SYSTEM_PROMPT` | The 30-rule system prompt tuned for report generation. Output contract, narrative composition (overview → breakdown → detail), visual selection (data shape → visual type), layout (no overlap, slicer strip), measure–visual pairing, filters, and TMDL/security rules. v5 adds rules 21a (field parameter switching), 23a/23b/23c (Power Query M partition authoring), and metadata about OLS + Fabric item types. |
| `LEGACY_GENERIC_SYSTEM_PROMPT` | The original 9-rule generic prompt, preserved verbatim for callers that opt out. |
| `select_system_prompt(context)` | Returns the focused prompt by default, or the legacy prompt if `context["report_focus_enabled"] = False`. The returned string is prefixed with `[nl2pbip prompt vN (name)]` so callers can log / pin the version they received. |
| `build_user_message(user_prompt, payload)` | Assembles the user-side message: `user_prompt + indented JSON payload`. |
| `build_feedback_message(error)` | Retry-feedback message with type-specific guidance for `TMDLValidationError` and `PBIRValidationError`. |
| `CRITIC_SYSTEM_PROMPT` + `build_critic_user_message(...)` | The reflection-loop critic prompt + payload builder. `run_with_reflection` invokes the critic after a successful run and scores the plan on `correctness` / `completeness` / `alignment_with_prompt`. |
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

Verify the install and see the bundled example PBIP folder:

```bash
python -c "import nl2pbip; print('nl2pbip', nl2pbip.__version__)"
python -m nl2pbip.example_run
ls artifacts/SalesInsights.pbipdir/SalesInsights.pbip
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

## 5-minute demo (no LLM required)

The fastest way to see what `nl2pbip` produces is `python -m nl2pbip.example_run` — a deterministic 7-step pipeline that uses a built-in mock LLM, runs the full TMDL + PBIR + package flow, and writes to `artifacts/SalesInsights.pbipdir`. No API key, no network, ~1 second:

```bash
python -m nl2pbip.example_run
```

Output (abridged):

```text
Executed plan containing 7 steps.
- add_report_page:     {'status': 'success', 'page': 'Main'}
- create_table:        {'status': 'success', 'table': 'Date',  'columns': ['Date', 'Month', 'Year']}
- create_table:        {'status': 'success', 'table': 'Sales', 'columns': ['SaleId', 'Region', 'Amount', 'Date']}
- add_measure:         {'status': 'success', 'measure': 'Total Revenue'}
- define_relationship: {'status': 'success', 'relationship': 'Sales_Date_Date_Date'}
- add_visual:          {'status': 'success', 'visual_id': 'visual_ec932f70'}
- package_pbip:        {'status': 'success', 'project': 'SalesInsights'}
PBIP output located at: artifacts/SalesInsights.pbipdir
```

Open the result in Power BI Desktop (`File → Open → Browse → artifacts/SalesInsights.pbipdir/SalesInsights.pbip`) and you'll see a working report with a bar chart and a Date / Sales star schema. The directory is git-friendly — every file is plain JSON or plain text.

## Worked example: prompt → plan → TMDL

**Prompt:**

> "Create a sales insights dashboard with date intelligence and region breakdowns."

**LLM plan** (excerpt of the `{"plan": [...]}` array):

```json
[
  {"tool": "add_report_page", "args": {"page": "Main", "display_name": "Sales Insights"}},
  {"tool": "create_table", "args": {"table_name": "Date", "columns": [
    {"name": "Date",  "dataType": "date"},
    {"name": "Month", "dataType": "string"},
    {"name": "Year",  "dataType": "wholeNumber"}
  ]}},
  {"tool": "create_table", "args": {"table_name": "Sales", "columns": [
    {"name": "SaleId", "dataType": "string"},
    {"name": "Region", "dataType": "string"},
    {"name": "Amount", "dataType": "decimal"},
    {"name": "Date",   "dataType": "date"}
  ]}},
  {"tool": "add_measure", "args": {"table_name": "Sales", "measure_name": "Total Revenue",
    "expression": "SUM(Sales[Amount])", "format_string": "$#,0.00"}},
  {"tool": "define_relationship", "args": {"from_table": "Sales", "from_column": "Date",
    "to_table": "Date", "to_column": "Date",
    "cardinality": "oneToMany", "cross_filter_direction": "singleDirection"}},
  {"tool": "add_visual", "args": {"page": "Main", "visual_type": "clusteredColumnChart",
    "bindings": {"Category": {"expr": {"Column": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Region"}}},
                 "Y": {"expr": {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Total Revenue"}}}}}},
  {"tool": "package_pbip", "args": {"output_path": "artifacts/SalesInsights.pbipdir",
    "project_name": "SalesInsights"}}
]
```

**Resulting `model.tmdl`:**

```text
table Date
  column Date  dataType = date       formatString = "yyyy-MM-dd"
  column Month dataType = string
  column Year  dataType = wholeNumber formatString = "#,0"

table Sales
  column SaleId dataType = string
  column Region dataType = string
  column Amount dataType = decimal    formatString = "#,0.00"
  column Date   dataType = date       formatString = "yyyy-MM-dd"
  measure "Total Revenue"
    expression = SUM(Sales[Amount])
    formatString = "$#,0.00"
```

The orchestrator wrote 7 step results, 9 JSON / TMDL files (model, relationships, 2 tables, report, page, visual, plus the `.pbip` / `.pbism` / `.pbir` manifests), and ran the full validation chain (TMDL column-name uniqueness, relationship endpoint existence, type compatibility, visual-type canonicalisation) before persisting any file. Failed validations roll the whole step back and retry with a feedback message that names the missing field or wrong type.

## What you get (output structure)

```text
artifacts/SalesInsights.pbipdir/
├── SalesInsights.pbip                              # entry point manifest
├── SalesInsights.SemanticModel/
│   ├── definition.pbism
│   └── definition/
│       ├── model.tmdl                              # human-readable TMDL
│       ├── relationships.tmdl                      # all relationships
│       └── tables/
│           ├── Date.tmdl                           # one file per table
│           └── Sales.tmdl
└── SalesInsights.Report/
    ├── definition.pbir
    └── definition/
        ├── report.json                             # report metadata
        └── pages/
            └── Main/
                ├── page.json                       # canvas / theme
                └── visuals/
                    └── visual_ec932f70.json        # one file per visual
```

Everything is plain text or JSON — diff-friendly in git, reviewable in pull requests, parseable by other tools. To package as a single binary, run `python -m nl2pbip.cli export --input … --format pbix --output …` (requires `pbi-tools`) or `--format pbit` for the in-process fallback.

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

## Troubleshooting

Common failures and how to recover:

| Symptom | Likely cause | Fix |
|---|---|---|
| `Planner must return valid JSON.` | The LLM wrapped the JSON in prose or markdown code fences | Switch to a model with reliable JSON output (gpt-4o-mini, claude-3-5-sonnet, qwen2.5-coder); or use `custom` provider with a fine-tuned local model |
| `TMDLValidationError: Unsupported dataType: …` | LLM invented a non-canonical TMDL spelling | Aliases are auto-rewritten (`bigint` → `int64`, `money` → `decimal`); unknown spellings fail. Either fix the prompt, or extend `nl2pbip.data_types.DATA_TYPE_ALIASES` |
| `TMDLValidationError: Duplicate column name …` | LLM created two columns with the same name in one table | All duplicates are reported together; the LLM should rename in the retry loop. If persistent, fix the prompt to use distinct names |
| `PBIRValidationError: Unsupported visualType '…'` | LLM invented a non-canonical visual type | Aliases are auto-rewritten (`table` → `tableEx`, `matrix` → `pivotTable`); unknown types fail. Extend `nl2pbip.visual_types.VISUAL_TYPE_ALIASES` if needed |
| `Package_pbip result missing project_path` | Context dict didn't include `package_path` | Pass `package_path` in the context (CLI does this automatically from `--output`) |
| `Packaging requires 'model_path' within context.` | Same as above but for `model_path` | Pass `MODEL_PATH_KEY` ("model_path") in context, or run via CLI |
| `pbi-tools: command not found` | Trying to export `.pbix` without pbi-tools installed | `dotnet tool install --global TabularEditor.Tools.PBITools`, or use `--format pbit` for the in-process fallback |
| `Planner output must be a JSON array` | LLM returned an object but no `plan` field | Some models return `{"steps": [...]}`. The orchestrator accepts `{plan: [...]}` and bare arrays; other shapes fail |
| Retry loop exhausts `max_attempts` | LLM consistently produces bad output | Lower temperature, switch to a stronger model, or use `example_run.py` to see what a working plan looks like |
| All FK suggestions have `coverage_ratio < 0.5` | The data has heavy orphans, or the column name doesn't match the FK column | Inspect `data_understanding.relationship_coverage` for the actual orphan count; either fix upstream data or hand-write the relationships |

Inspecting the orchestrator's intermediate state is straightforward — `results` is a list of `ToolResult` objects with the full input/output for each tool call, and the planner payload carries the `prompt_meta` block so you can pin the prompt version.

## Limitations

What `nl2pbip` doesn't do well, as of v1.0.0:

| Limitation | Why | Workaround |
|---|---|---|
| **LLM can still invent bad column names.** Even with `model_state` and `data_profile` in the planner payload, models sometimes ignore them. | The orchestrator exposes the schema but doesn't force the LLM to use it. | Pin a known-good prompt version via `prompt_meta.version`; verify model state was sent by checking the planner payload dump |
| **Foreign-key detection is heuristic.** `data_inspector.suggest_relationships()` ranks by overlap of top-5 distinct examples. When a column has many more distinct values than examples, the ratio is computed against the sample, not the full distinct count. | Sampling rather than full scan keeps the inspector fast. | For critical FKs, provide more rows via `data_sources` or hand-write the relationship |
| **No built-in RAG / retrieval.** Every plan re-emits the full payload, even for tables that already exist in the model. | The orchestrator is stateless across runs by design — PBIP is git-versioned. | Use the persisted `model_state` block to ground the LLM on existing schema (already wired) |
| **Numeric distribution quantiles require ≥5 values.** A column with 3 rows returns no quantiles in `data_understanding`. | Quantiles on <5 values are statistically meaningless. | Provide more rows, or accept the empty `numeric_distributions` entry |
| **`dax_library.json` is loaded once per CLI run.** Library changes require re-invocation. | The catalog is a static file; no hot reload. | Re-run the CLI / orchestrator with the updated library |
| **`.pbix` export requires `pbi-tools`.** The in-process fallback only emits `.pbit`. | `pbi-tools` ships its own C# compiler we don't want to fork. | Use `--format pbit` when `pbi-tools` is unavailable |
| **Custom visuals not in the LLM context.** Power BI custom visuals (e.g., bespoke chart libraries) need their visualType registered manually; the prompt doesn't know about them. | The visual-type list is curated, not exhaustive. | Extend `nl2pbip.pbir_validator._SUPPORTED_VISUALS` and `nl2pbip.visual_types.CANONICAL_VISUAL_TYPES` |
| **No streaming plan execution.** The orchestrator waits for the entire plan before executing. | Streaming requires partial model writes that complicate rollback. | For very large models, batch prompts and call the orchestrator per batch |

These are honest engineering limits, not aspirational gaps. Filing issues for any of them is welcome.

## Performance & cost

The orchestrator emits one LLM call per `run()` invocation (plus optional `ai_schema_hints` and `ontology_hints` enrichment calls). Rough budget for a single mid-complexity report (3 fact tables, 6 dimensions, 8 visuals, 4 measures, 4 relationships):

| Component | Tokens (in) | Tokens (out) | Cost @ gpt-4o-mini |
|---|---|---|---|
| System prompt | ~1,800 | — | ~$0.0003 |
| Planner payload (model_state + data_profile + data_understanding + ontology_hints + tools) | ~3,500 | — | ~$0.0005 |
| Plan output (7 steps, ~150 tokens each) | — | ~1,000 | ~$0.0006 |
| **Total per run** | **~5,300** | **~1,000** | **~$0.0014** |

Runtimes observed locally (M-series Mac, mock LLM): ~0.8 s for `example_run`. With a real LLM call over local network (Ollama): ~3–6 s. With OpenAI gpt-4o-mini over the public API: ~2–4 s depending on the planner payload size.

To reduce token spend:

* Skip `ai_schema_hints` (the deterministic profile already gives you column types and FK coverage): `context["ai_schema_hints_enabled"] = False`
* Skip `ontology_hints` if your column names are obvious: `context["ontology_hints_enabled"] = False`
* Skip `data_understanding` for tiny datasets (< 10 rows): `context["data_understanding_enabled"] = False`
* Pin the prompt version so retries don't re-load changelog data: read `prompt_meta.version` and pass it back via `context["prompt_version"]` (when supported)

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

The repo ships a complete GitHub Actions setup under `.github/workflows/`:

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | push / PR / `workflow_dispatch` | Pytest on Python 3.10 / 3.11 / 3.12, `black --check .`, `ruff check .`, example-run smoke test, Codecov upload, opt-in `benchmarks` job |
| `codeql.yml` | push / PR / weekly cron | Python security scanning (CodeQL); uploads findings to the Security tab |
| `release.yml` | tag push (`vX.Y.Z`) | Build sdist + wheel, `twine check`, publish to PyPI via trusted publishing (OIDC — no API token), create GitHub release |
| Dependabot | weekly | Pip + GitHub Actions dependency updates (patch + minor grouped; heavy ML extras ignored) |

Minimal `ci.yml` reference (see `.github/workflows/ci.yml` for the full version):

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:  # manual trigger for the benchmarks job

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

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
          pip install -e ".[dev]"
      - name: Verify the example runner produces a valid PBIP
        # Smoke test for the top-level entry point — runs the
        # bundled example and confirms the output directory
        # exists with the expected files.
        run: |
          python -m nl2pbip.example_run
          test -f artifacts/SalesInsights.pbipdir/SalesInsights.pbip
          test -f artifacts/SalesInsights.pbipdir/SalesInsights.SemanticModel/definition/model.tmdl
          test -f artifacts/SalesInsights.pbipdir/SalesInsights.SemanticModel/itemMetadata.json
          test -f artifacts/SalesInsights.pbipdir/SalesInsights.SemanticModel/.platform
          test -f artifacts/SalesInsights.pbipdir/SalesInsights.Report/itemMetadata.json
          test -f artifacts/SalesInsights.pbipdir/SalesInsights.Report/.platform
      - name: Check formatting with Black
        run: black --check .
      - name: Lint with Ruff
        run: ruff check .
      - name: Run tests
        # Skip test_finetune.py because it requires the heavy
        # finetune extra (datasets, transformers, trl) which
        # isn't installed in CI. Run those tests locally with
        # pip install -e ".[finetune]".
        run: pytest -v --cov=nl2pbip --cov-report=term-missing --cov-report=xml --ignore=tests/test_finetune.py
      - name: Upload coverage to Codecov
        if: matrix.python-version == '3.12'
        uses: codecov/codecov-action@v4
        with:
          file: coverage.xml
          fail_ci_if_error: false

  benchmarks:
    name: Performance benchmarks
    # manual trigger only — benchmarks are opt-in to keep the
    # default CI run fast.
    runs-on: ubuntu-latest
    if: github.event_name == 'workflow_dispatch'
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev]"
      - name: Run benchmarks
        env:
          NL2PBIP_RUN_BENCHMARKS: "1"
        run: pytest tests/test_performance.py -v -s
```

For PyPI publishing via OIDC trusted publishing, configure the project at https://pypi.org/manage/account/publishing/ pointing at the `rollroyces/nl2pbip` repo, the `release.yml` workflow, and the `pypi` environment — no API token is stored in the repo.

> **Tip:** To cut a release locally without OIDC: tag a version (`git tag -a v1.1.1 -m "..." && git push --follow-tags`), or run `python -m build && twine upload dist/*` from a clone that has your PyPI token configured.

## Project layout

```
nl2pbip/
├── nl2pbip/
│   ├── orchestrator.py          # planner payload assembly + retry loop
│   │                           # + run_with_reflection (post-success critic pass)
│   ├── llm_client.py            # OpenAI-compatible client + structured output
│   ├── tmdl_engine.py           # parser, writer, 10 handlers (calc-group, OLS,
│   │                           # field-param, power-query, …)
│   ├── tmdl_linter.py           # TMDLValidationError
│   ├── pbir_engine.py           # PBIR layout, OPC-compliant .pbit archive
│   ├── pbir_validator.py        # PBIR schema + semantic checks
│   ├── packager.py              # package_pbip_handler + Fabric metadata
│   │                           # (itemMetadata.json + .platform per item)
│   ├── m_builder.py             # Power Query M expression builders
│   │                           # (csv / sql / json / sharepoint / odata / web
│   │                           # + promoted / merge / append / group)
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
│   ├── prompts.py               # versioned LLM planner prompts (system + user +
│   │                           # feedback + critic) — v5 with field-param,
│   │                           # power-query, OLS, fabric rules
│   ├── cli.py                   # argparse CLI (generate / export subcommands)
│   ├── py.typed                 # PEP 561 marker
│   └── __init__.py
├── tests/                       # 684 pytest cases across 21 test files
├── artifacts/                   # example Run output (SalesInsights.pbipdir)
├── .github/                     # workflows, issue templates, CODEOWNERS,
│                                # dependabot.yml, SECURITY.md, etc.
├── pyproject.toml
├── LICENSE                      # Commercial + Apache carve-out for pre-0af050c commits
├── CHANGELOG.md
└── README.md
```

## Test counts

Pytest collects **684 test cases across 21 test files** (Python 3.10 / 3.11 / 3.12). Of those, **671 pass** on every supported Python version in CI; the remaining 13 are skipped because the benchmarks in `tests/test_performance.py` are opt-in via `NL2PBIP_RUN_BENCHMARKS=1`. 3 additional tests in `tests/test_finetune.py` (not in the headline count) require the heavy `finetune` extra (`datasets` / `transformers` / `trl`) — install locally with `pip install ".[finetune]"` to run those 3. Run `pytest tests/ --no-header -q` to confirm locally.

| Module | Collected tests |
|---|---:|
| `tests/test_repo_templates.py` | 62 |
| `tests/test_power_query.py` | 65 |
| `tests/test_opc_export.py` | 49 |
| `tests/test_ontology.py` | 42 |
| `tests/test_prompts.py` | 38 |
| `tests/test_object_level_security.py` | 32 |
| `tests/test_data_inspector.py` | 31 |
| `tests/test_fabric_metadata.py` | 30 |
| `tests/test_agentic_reflection.py` | 29 |
| `tests/test_calculation_groups.py` | 27 |
| `tests/test_field_parameter.py` | 27 |
| `tests/test_data_types.py` | 90 |
| `tests/test_data_understanding.py` | 24 |
| `tests/test_relationship_validation.py` | 23 |
| `tests/test_schema_advisor.py` | 16 |
| `tests/test_performance.py` | 13 (opt-in via `NL2PBIP_RUN_BENCHMARKS=1`) |
| `tests/test_visual_types.py` | 73 |
| `tests/test_orchestrator.py` | 4 |
| `tests/test_exporter.py` | 3 |
| `tests/test_llm_client.py` | 2 |
| `tests/test_finetune.py` | 3 (excluded from CI; needs `[finetune]` extra) |

Many tests are parametrised, which is why the function count is much lower than the case count. Run `pytest tests/ --collect-only -q` to see the breakdown locally.

The benchmark suite is opt-in to keep the default CI run fast:

```bash
NL2PBIP_RUN_BENCHMARKS=1 pytest tests/test_performance.py -s
```

## Next steps

- **Install from PyPI:** `pip install nl2pbip` (or `pip install --upgrade nl2pbip` to get the latest). Live releases at https://pypi.org/project/nl2pbip/.
- **Verify your install:** `python -m nl2pbip.example_run` produces a working PBIP from the bundled mock LLM in ~1 second. Outputs to `artifacts/SalesInsights.pbipdir/` with the full set of TMDL tables, PBIR pages, and Fabric `itemMetadata.json` + `.platform` files.
- **Browse `tests/test_prompts.py`** for the report-generation system prompt's content rules — every section keyword has a test that catches silent drift.
- **Browse `tests/test_data_understanding.py`** for a realistic end-to-end scenario with FK orphans and cardinality hints.
- **Browse `tests/test_repo_templates.py`** for the GitHub Actions templates — issue templates, PR template, dependabot config, etc.
- If you hit a runtime error, see the [Troubleshooting](#troubleshooting) and [Limitations](#limitations) tables above for common causes and workarounds.
- For token / cost budgeting across report-generation runs, see [Performance & cost](#performance--cost).
- For raw throughput numbers (200-table round-trip, calc-group / OLS / field-param rendering rates, end-to-end orchestrator latency, reflection-loop overhead), see [Performance benchmarks](#performance-benchmarks).
- Extend `dax_library.json` with your own calculation groups and measure templates so planners lean on approved logic.
- Register your raw data via `context["data_sources"]` so the LLM gets FK coverage, P50 of numeric columns, and schema.org vocabulary anchors rather than guessing.
- Pin a specific prompt version via the planner payload's `prompt_meta.version` block for reproducible plan generation.
- Use `Orchestrator.run_with_reflection(...)` instead of `Orchestrator.run(...)` for production calls — you get a `ReflectiveTrace` for debugging + a critic pass that catches plans where the LLM technically ran but missed the user's intent.

## Performance benchmarks

Opt-in benchmark suite (`NL2PBIP_RUN_BENCHMARKS=1`):

```bash
NL2PBIP_RUN_BENCHMARKS=1 pytest tests/test_performance.py -s
```

| Path | Median | Throughput |
|---|---:|---:|
| Writer (200-table model → TMDL text) | 0.9 ms | 1,148 ops/sec |
| Parser (200-table TMDL → model) | 62 ms | 16 ops/sec |
| Writer + parser round-trip | 60 ms | 17 ops/sec |
| Large file write (200 tables, disk I/O) | 63 ms | 16 ops/sec |
| Calc-group, 10 items | 0.4 ms | 2,596 ops/sec |
| Calc-group, 50 items | 1.75 ms | 573 ops/sec |
| Calc-group, 100 items | 3.5 ms | 288 ops/sec |
| Field-param render, 10 members | <0.01 ms | 338k ops/sec |
| Field-param render, 50 members | 0.01 ms | 89,718 ops/sec |
| OLS role render, 10 tables | 0.01 ms | 143k ops/sec |
| OLS role render, 50 tables | 0.03 ms | 30,769 ops/sec |
| Orchestrator end-to-end (10-step plan) | 7 ms | 140 ops/sec |
| Orchestrator with reflection loop | 7 ms | 143 ops/sec |

**Parser is ~70× slower than the writer** for the 200-table model. The bottleneck is column extraction and the `add_column` validation checks inside `_parse_table`. If you have models bigger than ~200 tables and need faster round-trip, profile with `cProfile` before optimising.

The reflection loop overhead is in the noise vs plain `run` (stub critic call is negligible vs planner + tool execution). See `tests/test_performance.py` for the full bench definitions.
- Wire `python -m nl2pbip.cli generate` into deployment automation (e.g., GitHub Actions + `pbi-tools push`) to continuously ship fully reproducible Power BI apps from natural-language specs.

## License

`nl2pbip` is **commercial proprietary software**, © 2026 Royce. All rights reserved.

This repository is published for visibility and collaboration under the terms of a **Commercial License Agreement** that you must sign with the Licensor before using the Software. **No open-source license** (MIT, Apache-2.0, GPL, AGPL, BSD, MPL, etc.) is granted by the public repository — viewing the source does not grant you any right to use, copy, modify, or distribute it.

Files committed prior to [`0af050c`](https://github.com/rollroyces/nl2pbip/commit/0af050c23393b3a4605f71ea1c8f49ccdb9cc1fb) retain their original Apache License 2.0 grant as a continuing authorization (see `LICENSE` §5). New contributions from that commit onward are governed by the Commercial License terms in [`LICENSE`](./LICENSE).

To obtain a Commercial License Agreement, contact:

> Royce &lt;roycelam@umich.edu&gt;

Your agreement will define scope, duration, fees, support, confidentiality, warranties, and termination.

THE SOFTWARE IS PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND.


noop
