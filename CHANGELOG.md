# Changelog

All notable changes to `nl2pbip` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `nl2pbip.data_types` module: canonical TMDL type spellings, alias
  resolution (SQL/JSON spellings → canonical TMDL form), and a default
  `formatString` suggester for numeric, monetary, and temporal types.
- 90 unit tests in `tests/test_data_types.py` covering aliases,
  variants, validation errors, auto-format suggestions, parser
  round-trips, and `TMDLColumn` construction-time checks.

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
