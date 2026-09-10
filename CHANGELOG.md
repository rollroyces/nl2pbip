# Changelog

All notable changes to `nl2pbip` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `nl2pbip.visual_types` module: canonical Power BI visual-type
  spellings, alias resolution (`table` → `tableEx`,
  `matrix` → `pivotTable`, `pie` → `pieChart`, `donut` /
  `doughnut` → `donutChart`, etc.), default layout category, and
  default on-screen size for each visual type.
- 73 unit tests in `tests/test_visual_types.py` covering aliases,
  variants, validation errors, layout lookups, validator
  integration, and end-to-end `add_visual_handler` with aliases.

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
