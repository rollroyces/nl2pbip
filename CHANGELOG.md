# Changelog

All notable changes to `nl2pbip` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `nl2pbip.data_inspector` module: profile registered data sources
  and emit a JSON-serialisable summary of column types, distinct
  values, numeric/date ranges, and null rates. Loads from CSV /
  JSON / JSONL / Parquet paths, in-memory lists, or callables
  returning DataFrame-like objects.
- `suggest_relationships()` heuristic: ranks candidate
  foreign-key pairs across profiled tables by distinct-value
  overlap, with a naming-pattern boost for the common
  ``X.fk_id`` ↔ ``Y.id`` convention.
- Orchestrator data-profile context: `_planner_payload` now
  includes a `data_profile` block (per-table column profiles +
  ranked relationship suggestions) when the caller registers
  data sources via `context["data_sources"]`. The LLM sees actual
  values, distinct counts, and column ranges before designing
  relationships or visuals — eliminating the most common cause
  of bad plans: hallucinated column names that don't exist on
  the table, and visuals built on columns whose values the LLM
  has never seen.
- 28 new tests in `tests/test_data_inspector.py` covering record
  loading (CSV, JSON, JSONL, Parquet, in-memory), per-column
  type inference and statistics, distinct-example handling,
  heuristic relationship detection, naming-pattern boost,
  min-overlap threshold, redact-distinct-values mode, and the
  orchestrator's planner-payload integration.

### Tests
Total: 275 passed (was 247).

## [0.5.0] - 2026-09-10
- Relationship endpoint validation: `define_relationship_handler`
  checks that both tables and both columns exist, listing the
  available columns on each table when an endpoint is missing so
  the LLM can self-correct without consulting the schema.
- Relationship type-compatibility validation: relationships between
  incompatible column types (numeric↔text, date↔text, etc.) are
  rejected before the model is written. Compatible types across
  TMDL-native variants (e.g. `bigint` alias ↔ `wholeNumber`
  canonical) are accepted because they share a compatibility
  bucket.
- Relationship cardinality / cross-filter direction validation:
  unknown values raise `TMDLValidationError` with the list of
  valid TMDL spellings.
- Self-referential relationship guard: degenerate `A.X → A.X`
  joins are rejected; legitimate self-joins like
  `ManagerId → Id` on a single hierarchy table still work.
- Duplicate active-relationship guard: Power BI allows only one
  active relationship per from-side endpoint. The handler
  enforces this so two date dimensions (e.g. Calendar + Fiscal)
  cannot both be active against the same `[DateKey]` column.
- Orchestrator model-state snapshot: `_planner_payload` now
  includes a JSON snapshot of every table's columns and data
  types, plus the existing relationship list. The LLM sees the
  real schema before wiring relationships instead of guessing
  column names that don't exist.

### Fixed
- **Silent relationship failures.** Relationships with non-existent
  tables/columns now raise a clear error pointing at the missing
  endpoint and listing the actual columns. Previously the
  handler only checked that the tables existed; column typos
  produced a corrupted model that Power BI Desktop refused to
  load.
- **Type-incompatible relationships.** Power BI Desktop silently
  refuses to load models that join incompatible column types
  (e.g. `int64 → string`). The new validation surfaces a clear
  error at handler-call time so the LLM can pick a compatible
  endpoint before the model is persisted.

### Tests
- 23 new tests in `tests/test_relationship_validation.py` covering
  duplicate column names, missing/typo'd relationship endpoints,
  type compatibility buckets, alias canonicalisation,
  self-referential guards, cardinality / cross-filter validation,
  duplicate active relationships, and the orchestrator's
  model-state snapshot.

Total: 247 passed (was 224).

## [0.4.0] - 2026-09-10

### Changed
- `PBIPExporter.export_as_pbit_zip` now uses the OPC-compliant
  builder and emits the canonical Power BI Desktop manifest parts
  (`Version`, `Metadata`, `Settings`, `SecurityBindings`,
  `DiagramLayout`) plus `[Content_Types].xml` with a UTF-8 BOM and
  forward-slash archive paths.
- The fallback exporter collapses the PBIP folder structure into the
  monolithic `Report/Layout` JSON part Power BI expects, and carries
  `Report/StaticResources/...` and `Report/CustomVisuals/...`
  across.
- Template name is now read from the `*.pbip` opener file's
  `name` field rather than the folder name.

### Fixed
- **OPC compliance of the fallback `.pbit` archive.** The previous
  implementation produced archives that Power BI Desktop rejected on
  open because they were missing `[Content_Types].xml` entirely.
  Archives now include the BOM-prefixed XML at the archive root,
  register every Power BI part via Override entries, use
  forward-slash paths, and emit the canonical manifest parts
  Power BI Desktop looks up by name.
- **Windows-zip backslash paths.** `Compress-Archive` /
  `ZipFile.CreateFromDirectory` on Windows produce `Report\Layout`
  paths which Power BI rejects; the new builder normalises all paths
  to forward slashes before writing.

## [0.3.0] - 2026-09-10

### Changed
- `PBIRValidator._SUPPORTED_VISUALS` is now an alias for
  `CANONICAL_VISUAL_TYPES` in `nl2pbip.visual_types`. The canonical
  set now includes `pieChart`, `donutChart`, `funnelChart`,
  `areaChart`, `treemap`, `kpi`, `multiRowCard`, `ribbonChart`,
  `waterfallChart` (in addition to the original 8 visuals).
- `PBIRValidator.validate_visual` normalises `visualType` in place
  when an alias is supplied (both top-level and inner
  `singleVisual.visualType`). The on-disk JSON always carries the
  canonical spelling Power BI Desktop expects.
- `add_visual_handler` validates and normalises `visual_type` at
  entry. Aliases (`table`, `matrix`, `pie`, …) are silently
  rewritten; unknown types raise a `ValueError` carrying a list of
  accepted spellings so the LLM retry loop can self-correct.
- `add_report_page_handler` honours `context["page_size"]` when no
  explicit `size=` argument is supplied. Previously the canvas was
  always 1280×720 unless the caller remembered to forward the size.
- `_build_pbir_validator` now forwards the caller-supplied
  `page_size` to the validator so its bounds check uses the right
  canvas dimensions instead of falling back to 1280×720.
- `RowState.reserve` now wraps to a new row cycle when a visual
  would overflow the canvas height, not just the width. Previously
  mixing several tall tableEx visuals in the same category stacked
  them off the bottom of the page.
- `category_for_visual` and `default_size_for_visual` are thin
  shims that delegate to `nl2pbip.visual_types`. The legacy dicts
  `VISUAL_CATEGORY` and `DEFAULT_SIZES` are re-exported from
  `pbir_engine` for backwards compat.

### Fixed
- **`Unsupported visualType 'table'.`** The validator now accepts
  the legacy `table` spelling (and other common LLM-emitted names
  like `matrix`, `pie`, `donut`, `doughnut`, `funnel`, `cardVisual`)
  by rewriting them to the canonical schema names Power BI Desktop
  emits.
- Visual layout overflow when adding several detail-band visuals
  on a 720px canvas. `RowState.reserve` now wraps to a new row
  cycle on height overflow, and `add_visual_handler` downscales a
  visual that exceeds the canvas so the file Power BI Desktop
  receives always opens cleanly.
- Layout band for detail visuals was set to start at y=520 on a
  720px canvas — a 220px table put the bottom edge at 740px,
  exceeding the canvas. The band now starts at y=480 and the
  handler downscales to fit when needed.

## [0.2.0] - 2026-09-10

### Changed
- `TMDLColumn.__post_init__` now validates and normalises `data_type`
  at construction time. Unknown types raise `TMDLValidationError`
  immediately; aliases (`bigint`, `float`, `varchar`, …) are silently
  rewritten to their canonical TMDL form. The caller's original
  spelling is preserved on `original_data_type` for diagnostics.
- `TMDLColumn` auto-applies a default `formatString` when the caller
  doesn't supply one. E.g. `decimal` → `"#,0.00"`, `date` →
  `"yyyy-MM-dd"`. Caller-supplied formats take precedence.
- `_validate_columns` (the `create_table_handler` preflight) now uses
  `normalize_data_type` and writes the canonical form back to both
  `data_type` and `dataType` keys, keeping the API and writer in sync.
- `VALID_DATA_TYPES` is now an alias for the new
  `ACCEPTED_DATA_TYPES` set in `nl2pbip.data_types`. Downstream
  importers continue to work unchanged.

### Fixed
- Silent type coercion in the parser: an LLM that emits an unknown
  type (`float`, `numeric`, `floob`) no longer defaults to `string`
  without telling the operator. Unknown types now raise
  `DataTypeError`, which surfaces to the orchestrator's LLM retry
  loop with a list of accepted spellings.
- Mixed `int64` / `wholeNumber` style in the same model: aliases are
  normalised so Power BI's "Format" dropdown doesn't silently coerce
  mismatched column types.
- Reference artifact `artifacts/SalesInsights.pbipdir/.../model.tmdl`
  now includes the auto-suggested format strings for `Date`, `Year`,
  and `Amount` columns, so a fresh `pip install -e .` + `python -m
  nl2pbip.example_run` produces a Power BI Desktop-readable file with
  consistent currency / date / numeric formatting out of the box.

## [0.1.0] - 2026-09-09

### Added
- Initial public release.
- 7-step orchestrator: `create_table` → `add_measure` →
  `define_relationship` → `add_calculation_group` → `add_rls_role`
  → `add_ols_role` → package.
- TMDL parser/writer supporting the dialect emitted by Power BI
  Desktop (canonical column / measure / relationship / role syntax).
- PBIR engine for report authoring, including visual bindings
  (`queryRef` for columns, `measureRef` for measures).
- Fine-tuning module: `dataset_generator` for synthetic
  natural-language-to-plan training pairs and `train` for SFT on
  Qwen2.5 / Llama-class base models via TRL + Unsloth.
- `.pbip` / `.pbit` packaging using `pbi-tools` when available, with
  a pure-Python fallback that produces a valid template archive.
- CLI entry point `nl2pbip` (Typer-based).
- CI workflow running Black + Ruff + pytest on Python 3.10 / 3.11 /
  3.12.

[Unreleased]: https://github.com/rollroyces/nl2pbip/compare/d6f690b...HEAD
[0.1.0]: https://github.com/rollroyces/nl2pbip/releases/tag/v0.1.0
