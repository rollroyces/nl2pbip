# Changelog

All notable changes to `nl2pbip` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **`nl2pbip.prompt_polisher` — pre-LLM message normalisation layer.**
  New module that scrubs every message the orchestrator is about to
  send to the LLM. Six ordered steps: encoding normalise (Unicode
  NFC, BOM strip, smart-quote replacement), whitespace normalise
  (CRLF / CR → LF, control-char strip, run-of-spaces collapse,
  paragraph-preserving blank-line dedup), **secret redact**
  (OpenAI / Anthropic / Stripe / AWS / GitHub / Slack / Google /
  Bearer tokens → `[REDACTED:SECRET]`), **PII redact** (emails /
  phones / IPv4 / IPv6 → `[REDACTED:PII]`), **injection scrub**
  (prompt-injection phrases wrapped in `[INJECTION_SCRUBBED]...
  [/INJECTION_SCRUBBED]` markers), and length budget (over-budget
  payloads truncated with a `[TRUNCATED]` marker). All regexes use
  bounded quantifiers so adversarial input stays linear-time. The
  default is opt-in: pass `prompt_polisher=DefaultPromptPolisher()`
  to `Orchestrator(...)`. The polish report is captured on each
  `AttemptRecord` so `ReflectiveTrace.attempts[i].polish_steps`
  shows what changed before the call. `tests/test_prompt_polisher.py`
  (76 tests) + `tests/test_polisher_integration.py` (11 tests)
  lock the behaviour in.

## [1.1.2] - 2026-09-13

### Changed
- **README refreshed for the v1.1.1 release.** Test-counts table
  rewritten with the actual per-file parametrised case counts
  (21 files / 680 cases collected; `test_finetune.py` excluded
  from CI). Performance benchmarks table expanded to all 13
  benches including the reflection-loop overhead (previously
  only 9 entries). Project layout tree updated with
  `nl2pbip/m_builder.py` (Power Query M builder, missing
  since PR #19) and accurate test-file count.
- CI/CD section rewritten to describe the full workflow set
  (`ci.yml` + `codeql.yml` + `release.yml` + Dependabot) with
  the actual YAML from the v1.1.x rewrite (concurrency group,
  permissions hardening, example-run smoke test, opt-in
  benchmarks job). Removed the stale "install transformers
  + trl" note from the previous CI workflow.
- Planner prompts section now mentions `CRITIC_SYSTEM_PROMPT`
  + `build_critic_user_message` (added in PR #20) and notes
  the current prompt version is v5.
- Overview / capability matrix updated to add a row for
  PyPI-published releases (with link to the project page)
  and to link the Performance benchmarks row to its section.

### Added
- Installation section now includes a "Verify your install"
  snippet (`python -c "import nl2pbip; print(...)"` +
  `python -m nl2pbip.example_run` + `ls artifacts/...`).
- CI/CD section now documents the PyPI trusted-publishing
  setup flow + how to cut a release locally without OIDC.
- Next steps list mentions `pip install nl2pbip` from PyPI
  (with the project URL) and recommends
  `Orchestrator.run_with_reflection(...)` over plain `run(...)`
  for production use.

## [1.1.1] - 2026-09-13

### Changed
- **Author / contact email swapped.** All references to
  `rollroyces@users.noreply.github.com` replaced with
  `roycelam@umich.edu` across `pyproject.toml`, `LICENSE`, `README.md`,
  and `.github/SECURITY.md`. GitHub username (`rollroyces`) and
  commit-hash references in README/CHANGELOG are unchanged —
  those point at historical artefacts that exist under the
  `rollroyces` GitHub namespace.
- Per-repo git identity updated to `Royce <roycelam@umich.edu>`
  so future commits carry the right author email.

### Notes
- v1.1.0 was already published to PyPI under the previous
  email. PyPI's filename-collision protection prevents
  re-uploading the same version, so v1.1.0's package metadata
  retains the old email as a historical record. Anyone who
  needs the corrected metadata should `pip install
  --upgrade nl2pbip` to pull v1.1.1+.

## [1.1.0] - 2026-09-12

### Added
- **TMDL feature expansion** — five new capabilities covering
  the Power BI desktop authoring surface:
  - **Calculation Groups** + dynamic format strings
    (`add_calculation_group` tool + `TMDLCalculationItem` /
    `isCalculationGroup` / `formatStringDefinition` TMDL grammar).
  - **Object-Level Security (OLS)** with the Microsoft
    Sept 2025 TMDL grammar (nested
    `tablePermission Tbl { columnPermission Col { ... } }`
    blocks; legacy RLS aggregator still parsed for back-compat).
  - **Field Parameters** for dynamic measure / column / table
    switching via slicer (`add_field_parameter` tool, canonical
    `isParameterTable` + `partition = calculated expression` TMDL).
  - **Power Query (M) partition generation**
    (`add_power_query_partition` tool + `nl2pbip.m_builder` module
    with six source templates and four transformation builders).
  - **Fabric Git Integration metadata** — `itemMetadata.json` +
    `.platform` per Fabric item so PBIP folders are ready for a
    Git repo connected to a Fabric workspace. Validated against
    `FABRIC_ITEM_TYPES` (20 canonical Fabric types).
  - 247 new tests across the five feature files.

- **Agentic self-reflection loop**
  (`Orchestrator.run_with_reflection`): persistent
  `ReflectiveTrace` + cumulative feedback across retries +
  post-success critic pass via the planner LLM (or a separate
  `critic` client) + reflection loop that re-invokes the planner
  with the critic's suggestions when the score is below threshold.
  New `PlannerClarification` exception for LLM-asked questions.
  `PlanQualityScore` dataclass with `correctness` / `completeness`
  / `alignment_with_prompt` + `is_acceptable()` predicate.
  System prompt v5 (rules 23a / 23b / 23c). 29 new tests.

- **Performance benchmark suite**
  (`tests/test_performance.py`): 13 opt-in benchmarks covering
  TMDL writer / parser round-trip, calc-group / field-param / OLS
  rendering at multiple sizes, large file write, end-to-end
  orchestrator latency, and the reflection-loop overhead.

- **GitHub repo template files** (`.github/` + `CONTRIBUTING.md`):
  issue templates (`bug_report` / `feature_request` /
  `documentation`), PR template, CODEOWNERS, Dependabot,
  `SECURITY.md`, `CONTRIBUTING.md`, Discussion template, release
  notes template, CodeQL workflow, release workflow (PyPI
  trusted publishing + GitHub release). CI workflow upgraded
  with concurrency group, permissions hardening, an example
  smoke test, opt-in benchmarks job, and `--ignore` for the
  finetune test suite. 53 new tests in
  `tests/test_repo_templates.py`.

### Changed
- CI workflow simplified — dropped the heavy ML stack from the
  default CI install (only required for `test_finetune.py`,
  which is now `--ignore`d). `pyyaml>=6.0` and `twine>=5.0`
  added to `[dev]` extras.
- CodeQL workflow hardened — `paths-ignore` correctly lives
  on `actions/checkout` (not `codeql-action/init`), `security-events:
  write` declared, and `paths-ignore` uses the block-scalar form
  to work around the parser rejecting glob characters in the
  list form.
- README rewritten with a 5-minute demo, worked example,
  troubleshooting, limitations, performance & cost sections;
  performance benchmarks section documents throughput at the
  current version.

### Tests
- **666 tests pass** across 16 test files (was 484 at v1.0.0;
  +182 across this release).
- 13 benchmarks opt-in via `NL2PBIP_RUN_BENCHMARKS=1`.
- `black` + `ruff` clean.

Nothing yet — release notes for the next version land here.

## [1.0.0] - 2026-09-10

### Added
- `nl2pbip.prompts` module: extracted every string the orchestrator
  sends to the LLM into a single importable module with explicit
  versioning.
  - `REPORT_GENERATION_SYSTEM_PROMPT` — the new system prompt
    tuned for report generation. 30 numbered rules organised in
    7 sections: output contract, report composition (narrative
    flow: overview → breakdown → detail), visual selection (data
    shape → visual type mapping), layout (no overlap, slicer
    strip, consistent margins), measure–visual pairing, filters,
    and TMDL / security / final-package rules.
  - `LEGACY_GENERIC_SYSTEM_PROMPT` — the original 9-rule generic
    prompt preserved verbatim for callers that opt out.
  - `select_system_prompt(context)` — picks focused vs. legacy
    based on `context["report_focus_enabled"]`. Defaults to the
    tuned prompt for new callers.
  - `build_user_message(user_prompt, payload)` — assembles the
    user-side message (prompt + indented JSON payload).
  - `build_feedback_message(error)` — retry-feedback message
    with type-specific guidance for `TMDLValidationError` and
    `PBIRValidationError`.
  - `prompt_metadata(context)` — exposes `{version, name,
    focused_on_report_generation}` in the planner payload's new
    `prompt_meta` block.
  - `PROMPT_CHANGELOG` — append-only changelog with one entry
    per version bump.
- Orchestrator integration: the inline 9-rule prompt is replaced
  by `select_system_prompt(context)`. The planner payload now
  includes a `prompt_meta` block so callers can audit which
  prompt version produced a given plan. The retry-feedback
  builder routes through `build_feedback_message` for consistent
  format and richer guidance.
- 38 tests in `tests/test_prompts.py` covering versioning
  (positive version, monotonic changelog, current entry has
  metadata), prompt content (every section keyword present),
  legacy prompt availability, user-message assembly (prompt
  prepended, payload JSON indented, payload not mutated),
  feedback builder (TMDL/PBIR reminders + generic fallback),
  prompt selection (default focused, opt-out legacy, version
  header present), prompt metadata (focused vs. legacy, stable
  keys), and orchestrator integration (system prompt reflects
  opt-out, payload exposes `prompt_meta`, user message starts
  with user text).

### Tests
Total: 398 passed (was 360).

## [0.9.0] - 2026-09-10

### Added
- `nl2pbip.data_understanding` module: cross-table data analysis
  that fills the gaps between the deterministic inspector
  (per-column stats) and the AI advisor (LLM-inferred column
  roles). Four deterministic analyses:
  - **Primary-key detection** — flags columns with
    `distinct_count == row_count` as strong PK candidates, with
    weaker confidence tiers for high-but-not-perfect ratios.
  - **FK coverage** — for every suggested FK pair, computes
    actual matching / orphan counts against the data. Replaces
    the inspector's heuristic overlap with a concrete number
    the LLM can trust.
  - **Cardinality hints** — `oneToOne` / `manyToOne` /
    `manyToMany` / `oneToMany` derived from the ratio of
    distinct values + coverage.
  - **Numeric quantiles** — P25/P50/P75/P95 + skewness hint +
    outlier count for every numeric column.
  - **Time range** — min/max dates for every date column, so
    the LLM doesn't suggest year-over-year measures on data
    that only spans a month.
- `data_understanding` block in the planner payload: emitted
  alongside `data_profile` and `ontology_hints` when data
  sources are registered. Disabled via
  `context["data_understanding_enabled"] = False`.
- 24 tests in `tests/test_data_understanding.py` covering
  primary-key detection (strong/likely/weak), FK coverage with
  orphans, cardinality hints (oneToOne/manyToOne/manyToMany/
  oneToMany), numeric quantiles + skew, time range for ISO
  dates, top-level analyzer with realistic data, and orchestrator
  integration (with/without data sources, opt-out).

### Tests
Total: 360 passed (was 336).

## [0.8.0] - 2026-09-10

### Added
- `nl2pbip.ontology` module: a curated subset of public
  ontologies (schema.org + PROV-O) with token-overlap scoring,
  alias hit-precedence, and bounded planner-summary output.
  - `lookup_type(iri)`, `lookup_property(iri)` — direct IRI
    fragment lookup.
  - `suggest_matches(column_name)` — fuzzy match column
    names to ontology terms using token-overlap scoring +
    camelCase splitting. Alias hits (e.g. `customerEmail` →
    `schema.org/email`) score 1.0 and take precedence.
  - `build_planner_summary(column_names)` — bounded JSON
    shape for the orchestrator's planner payload.
- `ontology_hints` block in the planner payload: emitted
  alongside `data_profile` when data sources are registered.
  Each column name gets up to 2 ontology candidates (with
  IRI, label, comment, score, source). Disabled via
  `context["ontology_hints_enabled"] = False`.
- Fix: `ColumnSemantics` now carries `table` and `ontology_match`
  fields. The earlier `table` field was dropped during JSON
  parsing, leaving the LLM with column names but no table
  context.
- 42 tests in `tests/test_ontology.py` covering tokenisation,
  token-overlap scoring, lookup, curated-subset integrity,
  suggest_matches for common business column names, planner
  summary shape, and orchestrator integration.

### Tests
Total: 336 passed (was 294).

## [0.7.0] - 2026-09-10

### Added
- `nl2pbip.schema_advisor` module: an LLM-driven schema
  enrichment layer for the LLM planner payload. Wraps the
  orchestrator's LLM client and asks it to enrich the
  deterministic data profile with column-semantics,
  measure-suggestions, and visual-suggestions. Caches results
  by profile fingerprint to avoid repeat LLM calls on retries.
  Tolerant response parsing (code-fenced JSON, leading prose,
  missing fields are dropped rather than crashing). Graceful
  fallback: returns `None` on LLM failure or unparseable
  output, so the deterministic profile keeps working unchanged.
- `ai_schema_hints` block in the planner payload: emitted when
  data sources are registered and an LLM client is available.
  Opt-out via `context["data_inspector_ai_enabled"] = False`
  for callers under strict data-residency.
- 16 tests in `tests/test_schema_advisor.py` covering: prompt
  building, response parsing (including code fences and malformed
  JSON), invalid-role normalisation, missing-field dropping,
  cache hits, empty-profile guard, LLM-failure fallback,
  parse-failure fallback, and orchestrator integration.

### Tests
Total: 294 passed (was 278).

## [0.6.0] - 2026-09-10

### Added
- `nl2pbip.data_inspector` module: profile data sources at runtime
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
