# Changelog

All notable changes to `nl2pbip` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project adheres
to [Semantic Versioning](https://semver.org/).

## [1.4.0] - 2026-09-17

### Added
- **Lightweight RAG for `model_state`** (closes the 'No built-in RAG' limitation): when the model has tables AND focus hints (data_sources keys, recent lint errors) are registered, the orchestrator emits only the relevant table subset + a stable content-hash instead of dumping the full state.

## [Unreleased]

### Added
- **Multi-provider cost guardrails (`TokenBudget` + `--max-cost-usd`).**
  The orchestrator now tracks cumulative LLM spend across
  every `generate()` invocation and aborts with a new
  `BudgetExceededError` once cumulative spend crosses a
  caller-supplied cap. Plumbed three ways:

  * `Orchestrator(max_cost_usd=N)` constructor parameter
    (Python API).
  * `nl2pbip.cli --max-cost-usd N` (default 10.0; pass
    `0` or a negative value to disable).
  * `StructuredLLMClient(budget=...)` and a `set_budget`
    method for callers that want to attach a pre-built
    budget after construction.

  Cost math lives in the new `nl2pbip.pricing` module —
  a hardcoded `dict` of USD-per-token rates for OpenAI /
  Anthropic / DeepSeek / Qwen / Zhipu / Moonshot / Azure
  / custom providers, with a `$0.000003 / token` fallback
  for anything unknown. Token counting uses `tiktoken`
  (`cl100k_base` encoder) when installed and falls back
  to a 4-char heuristic when it isn't, so slim installs
  don't crash on import. Output tokens are charged at 4×
  input rate (the typical OpenAI / Anthropic ratio for
  mid-tier models).

  Spec compliance: a mocked single LLM call returning
  1000 tokens records ~$0.0006 of spend against the
  budget tracker; setting `max_cost_usd=0.0001` with
  the same call aborts with `BudgetExceededError` and
  leaves `spent_usd` unchanged (failed checks don't
  consume headroom).

  32 new tests in `tests/test_budget.py` cover the
  pricing tables, the token counter, the
  `BudgetExceededError` semantics, the LLM client
  attachment path, the CLI flag plumbing, and the
  end-to-end orchestrator abort path.
- **`Makefile` for developer ergonomics.** Six targets:
  `make test` runs the full CI-equivalent suite (pytest
  + vulture + black --check + ruff check + mypy --strict
  + bandit); `make lint` is just black + ruff; `make type`
  is mypy only; `make bench` runs the opt-in benchmark
  suite under `NL2PBIP_RUN_BENCHMARKS=1`; `make example`
  invokes the bundled end-to-end demo
  (`python -m nl2pbip.example_run`); `make clean` strips
  `__pycache__/` directories. All recipes use `.venv/bin/`
  paths so they work with the existing venv without
  requiring system-wide installs. `SHELL := /bin/bash`
  is set at the top so `find ... -exec` works on macOS
  (default /bin/sh is dash).
- **CLI smoke test** (`tests/test_cli_smoke.py`): 15
  subprocess-based assertions verifying that
  `python -m nl2pbip.cli generate --help` and
  `python -m nl2pbip.cli --help` exit 0 and surface every
  flag from `_build_generate_parser`
  (`--prompt`, `--provider`, `--model`, `--base-url`,
  `--api-key`, `--workspace`, `--output`, `--project-name`,
  `--dax-library`, `--export`, `--export-output`,
  `--api-version`). Parametrised coverage means a regression
  that drops a single flag surfaces with a precise
  "missing flag" message. Uses `sys.executable` so the test
  works under CI (no `.venv` mounted) and locally.

### Changed
- **`example_run.py`: replace ``print(...)`` with the
  standard ``logging`` machinery.** Three ``print`` calls
  (the per-step summary, the per-tool result lines, the
  final "PBIP output located at" line) now route through
  ``logger = logging.getLogger(__name__)`` + ``logger.info``.
  Default level INFO. Added a ``--log-level`` CLI flag
  (``DEBUG / INFO / WARNING / ERROR / CRITICAL``,
  case-sensitive uppercase) so callers can dial verbosity
  without editing the source. The signature is now
  ``main(argv: Optional[List[str]] = None)`` — accepts
  ``argv`` so the function is unit-testable without
  mutating ``sys.argv``.

  Regression test:
  ``tests/test_repo_templates.py::
  TestRepoTemplateIntegration::test_example_run_supports_log_level_flag``
  invokes the demo as a subprocess with
  ``--log-level INFO`` and asserts both summary lines land
  on stderr (which is where ``logging.basicConfig``
  writes by default — a regression to ``print`` would
  route them to stdout and this test would catch it).
- **mypy: tighten `[tool.mypy]` with `strict_equality` +
  `no_implicit_reexport`.** Both flags were previously
  opt-in; the codebase reached strict-clean (0 errors) so
  we can now require them. `strict_equality` prevents
  silently-true `==` comparisons between incompatible
  types, `no_implicit_reexport` forces explicit
  `__all__`/`from X import Y` for every name that flows
  from a sub-module into an unrelated `__init__`. New
  regression test
  `test_mypy_config_enables_extra_strict_flags` locks the
  settings. Verified locally with
  `mypy --strict --no-incremental --python-version 3.12 nl2pbip`
  → `Success: no issues found in 29 source files`.
- **License compliance promoted from soft-fail to gating.**
  The dep tree was audited with `pip-licenses
  --format=csv` — 123 packages, 0 with an UNKNOWN license —
  so the license-check workflow's `exit 0` soft-fail is gone.
  The `pip-licenses --fail-on` step now runs under `set -e`
  and propagates the exit code, so any future UNKNOWN or
  copyleft dep (GPL / LGPL / AGPL / SSPL / Commons-Clause)
  fails the build immediately. Added a second step that
  uses `pip-compile --generate-hashes` to emit a fully-pinned
  `requirements-hashed.txt` with SHA-256 hashes for every
  transitive dep — the supply-chain audit step. The hashed
  requirements file is uploaded alongside the license
  report as a 30-day artifact so a maintainer can diff
  against the previous known-good lock to spot churn.

## [1.3.5] - 2026-09-14

### Changed
- **mypy --strict promoted from report-only to gating.**
  The codebase reached 0 errors under mypy --strict
  (down from 77 baseline). `[tool.mypy.overrides]`
  excludes `nl2pbip.finetune.*` + `nl2pbip.providers.*`
  because their ML deps don't ship `py.typed` markers,
  and `openai` / `anthropic` because they're optional
  extras. All other errors were fixed by tightening
  generic type arguments (`set` → `set[str]`,
  `Dict[str, set]` → `Dict[str, set[str]]`), adding
  `cast(Any, ...)` for SDK overloads that mypy can't
  see through (Anthropic `temperature`), and renaming
  one loop variable (`table` → `col_table`) that
  collided with an outer-scope `TableProfile` variable.
- `mypy>=1.10` + 4 type stubs (`types-jsonschema`,
  `types-PyYAML`, `types-requests`, `types-tabulate`)
  pinned in `[dev]` extra.
- `.github/workflows/mypy.yml` now uses the
  `[tool.mypy]` config from `pyproject.toml` (no more
  inline `--exclude` flags) and exits non-zero on any
  error.

## [1.3.4] - 2026-09-14

### Added
- **PR labeler workflow** (`.github/workflows/pr-labeler.yml` +
  `.github/labeler.yml`): `actions/labeler@v5` auto-applies
  one of 8 labels (`CI`, `Dependencies`, `Docs`, `Examples`,
  `Prompts`, `Core`, `Finetune`, `Tests`) based on files
  touched in the PR. Capped at 5 changed-files labels per
  PR (`changed-files-labels-limit: 5`) and 200 files total
  (`max-files-changed: 200`) so a tree-wide refactor doesn't
  spam labels.
- 8 new GH labels (`CI`, `Dependencies`, `Docs`, `Examples`,
  `Prompts`, `Core`, `Finetune`, `Tests`) created via
  `gh label create` so the labeler has names to apply.
- 4 regression tests in `tests/test_repo_templates.py`:
  - `test_pr_labeler_workflow_runs_on_pull_request_target`
    (lock the event so it keeps write access)
  - `test_pr_labeler_uses_actions_labeler_v5` (lock the
    major version)
  - `test_labeler_config_parses` (require `changed-files-
    labels-limit` and sane bounds)
  - `test_labeler_config_references_existing_labels` (fetch
    live labels from the GH API and verify every label in
    `LABEL_*` rules exists in the repo)

## [1.3.3] - 2026-09-14

### Added
- **License compliance workflow** (`.github/workflows/license-check.yml`):
  `pip-licenses --fail-on='GPL,LGPL,AGPL,SSPL,Commons-Clause,UNKNOWN'`
  runs on every PR + push that touches `pyproject.toml` /
  `requirements*.txt` / `uv.lock`. Catches a copyleft dep the
  moment it enters the manifest. Uploads the full report as
  an artifact so a maintainer can review transitively-licensed
  packages. The ``UNKNOWN`` list entry is a soft-fail for now
  (some packages' license metadata is incomplete) but will be
  promoted to a gating failure once the codebase is UNKNOWN-free.
- **`pip-licenses>=5.0`** pinned in the `dev` extra.
- 4 regression tests in `tests/test_repo_templates.py`:
  `test_dev_extra_pins_pip_licenses` (dep wired correctly),
  `test_license_check_workflow_forbids_copyleft` (every
  forbidden license in `--fail-on`), `test_license_check_workflow_reports_unknown`
  (UNKNOWN mentioned in workflow), and the
  `TestFilePresence` / `TestYAMLShape` lists include the new
  workflow.

## [1.3.2] - 2026-09-14

### Added
- **Vulture dead-code check** in `ci.yml`: fails the build on
  unused imports / functions / variables with confidence
  >= 80% (a low threshold that catches obvious dead code
  without flagging reflection / dynamic-dispatch false
  positives). `vulture>=2.16` added to the `dev` extra so
  `pip install -e ".[dev]"` brings it in locally. Used to
  enforce that all new code paths are actually used.

### Changed
- **Bandit promoted from report-only to gating.** The codebase
  is now Bandit-clean: 5 known-justified findings carry
  ``# nosec <id> — <reason>`` markers with multi-line comments
  (Bandit's `--no-nosec` interpretation prefers the long form
  so code review catches casual `nosec` sweeps). Any new finding
  fails the build. To suppress a finding: prefer fixing the
  underlying code; only add a ``# nosec`` marker when the
  finding is genuinely a false positive.

### Fixed
- **B615 Hugging Face Hub dataset download without revision
  pin** (`nl2pbip/finetune/train.py`): real supply-chain risk.
  `load_dataset("json", data_files=...)` is a local-file load
  and `revision` is a no-op for that path, but we set it
  anyway so a future move to `dataset=...` (which does hit
  the Hub) doesn't silently regress.
- **B110 try/except/pass** (`nl2pbip/data_inspector.py:270`):
  inline comment now documents the intentional fallback to
  the polars / list paths below the `except`.
- **B404 + B603 subprocess** (`nl2pbip/exporter/exporter.py`):
  inline comments document that `cmd` is constructed from a
  hardcoded `pbi-tools` invocation and no untrusted input
  reaches the shell.
- **B311 random** (`nl2pbip/finetune/dataset_generator.py:109`):
  inline comment documents that this is a reproducibility
  seed, not a crypto use.
- **B101 assert** (`nl2pbip/tmdl_engine.py:2273`): replaced the
  `assert m_expression is not None` guard with an explicit
  `if m_expression is None: raise ValueError(...)` so the
  bytecode-stripped case still fails the build.

## [1.3.1] - 2026-09-14

### Added
- **Gitleaks secret scanning** (`.github/workflows/gitleaks.yml`
  + `.gitleaks.toml`): scans every commit + the full git history
  for committed secrets. Two layers — `protect` on every PR
  (catches new leaks before merge) and `detect` on push to
  main + weekly cron (catches historical leaks). Posts a SARIF
  to the Security tab. Configured to allowlist the test
  fixtures (which use `_fake("VENDOR", "FAKE...")` patterns)
  and the smoke-test output under `artifacts/`.
- **Bandit Python security linter** (`.github/workflows/bandit.yml`
  + `.bandit`): catches Python-specific security issues that
  CodeQL misses — B101 `assert` for input validation, B311
  `random` for crypto, B404 subprocess without `shell=False`
  validation, B615 `load_dataset` without `revision` pinning,
  B603 subprocess with untrusted input, etc. Posts a SARIF
  to the Security tab via a small JSON→SARIF converter
  (Bandit 1.9 doesn't emit SARIF natively). Report-only for
  now — the codebase has ~6 known issues, all of which need
  a `# nosec` review before this can be a gating check.

### Changed
- **Test count** bumped from 791 → 799 (+8 new tests: 2 TOML
  parse tests for `.gitleaks.toml` and `.bandit`, 6 new file
  presence tests in `tests/test_repo_templates.py` for the new
  workflow + config files).

## [1.3.0] - 2026-09-13

### Added
- **Five new bot-driven protection workflows:**
  - **Renovate** (`.github/renovate.json`) replaces Dependabot with
    grouped patch + minor updates, auto-merge on patch if CI is
    green, and per-rule config (heavy ML extras ignored).
  - **actionlint** (`.github/workflows/actionlint.yml`) lints
    `.github/workflows/*.yml` on every PR + push to main, catching
    syntax errors that escaped notice (the class of bug that hit
    us on the CodeQL `paths-ignore` config in PR #28).
  - **OpenSSF Scorecard** (`.github/workflows/scorecard.yml`) runs
    on `workflow_dispatch` (manual). Default `GITHUB_TOKEN`
    doesn't have the `admin:org:read` scope Scorecard needs,
    and without a PAT the run times out at the 10-minute mark.
    Setup instructions are inline in the workflow file: create a
    classic PAT with `repo` + `admin:org:read` scope, add as the
    `SCORE_CARD_GITHUB_TOKEN` repo secret, then re-enable the
    `schedule:` block for a weekly cron.
  - **Stale bot** (`.github/workflows/stale.yml`) auto-closes
    inactive issues after 30 days (with a 7-day warning). Honors
    the `pinned`, `security`, `in-progress`, and
    `enhancement-requested` exempt labels.
  - **mypy --strict** (`.github/workflows/mypy.yml`) type-checks
    `nl2pbip/` on every PR. Report-only for now (~77 strict
    errors remain; tracking in CHANGELOG). Will be promoted to
    a gating check once the codebase is strict-clean.

### Changed
- **Dependabot removed** in favour of Renovate. The new config
  offers better defaults: per-rule grouping, auto-merge on
  patch if CI passes, and pin-digest for GitHub Actions.
- **Test count** bumped from 782 → 791 (+9 new tests: 6 for the
  Renovate config shape, 3 for the mypy workflow shape). The
  `TestDependabot` class in `tests/test_repo_templates.py` was
  replaced with `TestRenovate`.

## [1.2.2] - 2026-09-13

### Changed
- **Docstring coverage pass.** Added Google-style docstrings to
  the public API surface of `pbir_validator.py` (`validate_page`,
  `validate_visual`), `orchestrator.py` (`ToolRegistry.register`,
  `get`, `all_specs`; `ReflectiveTrace.succeeded`),
  `data_inspector.py` (`TableProfile.to_json`,
  `DataProfile.to_json`), and `cli.py` (`parse_args`). Dataclass
  field docstrings intentionally left alone (the class-level
  docstring covers them).
- **Performance benchmarks table** gained a provenance note
  explaining that the numbers were measured on an M-series Mac
  with Python 3.11 and are reproduced verbatim in
  `tests/test_performance.py` so callers can refresh them locally.

### Added
- **Rules 32–35 (COMMON MISTAKES section) in the system prompt.**
  Adds an anti-patterns block the LLM can pattern-match on
  before producing bad output:
  - **Rule 32**: don't propose `define_relationship` for endpoints
    that don't yet exist in the same plan.
  - **Rule 33**: don't put two tools with overlapping
    responsibilities in the same plan (e.g. manual M partition +
    `csv` template partition for the same table).
  - **Rule 34**: don't emit `add_visual` until every column,
    measure, and relationship it depends on has been emitted in
    the same plan. Cites the actual error mode
    (`Projection 'X' references unknown field`).
  - **Rule 35**: don't re-emit duplicate measures — use
    `add_pattern_measure` when `dax_catalog` already provides a
    match.
- **`PROMPT_CHANGELOG` v7 entry** documenting rules 32–35.
- **`TestAntiPatternsGuidance`** (5 tests in `tests/test_prompts.py`)
  asserting the COMMON MISTAKES section header is present and that
  rules 32–35 each contain the expected keywords.

## [1.2.1] - 2026-09-13

### Changed
- **Documentation audit pass.** Corrected the README, SECURITY.md,
  prompts module docstring, and ontology module docstring to
  match the actual codebase:
  - README "What ships" header bumped from v1.1.1 to v1.2.0.
  - README rule count corrected from "30 rules" to "35 rules"
    (rules 1–31 + sub-rules 21a / 23a / 23b / 23c).
  - README Fabric type count corrected from "20 canonical" to
    "21 canonical" (`FABRIC_ITEM_TYPES` is a frozenset of 21).
  - README Architecture table handler count corrected from
    "11 handlers" to "13 handlers" (added `add_pattern_measure`
    and `set_page_layout`); same fix in the project-layout tree.
  - README Ontology counts corrected (41 / 72 / 71 / 9 instead
    of "~39 / ~72 / ~71 / 9"). Module docstring in
    `nl2pbip/ontology.py` brought in sync (was "~30 / ~50 /
    ~10" — now 41 / 72 / 9).
  - README test-counts table corrected: `test_field_parameter.py`
    → `test_field_parameters.py` (the actual filename),
    `test_repo_templates.py` 62 → 67 (post-rebrand), added
    `test_llm_client.py` (2) and `test_prompts.py` 38 → 43
    (new rule-31 coverage tests).
  - README headline test count bumped 772 → 777 to match
    `pytest tests/ --ignore=tests/test_finetune.py --collect-only`.
  - README Limitations header bumped "as of v1.0.0" → "as of
    v1.2.0".
  - `SECURITY.md` Supported-versions table expanded from
    "1.0.x ✅ / < 1.0 ❌" to "1.2.x / 1.1.x ✅ / < 1.1 ❌" with
    a note about best-effort patches.
  - `nl2pbip/prompts.py` module docstring corrected: the
    report-generation prompt is the **default**, not the opt-in
    — `report_focus_enabled=False` opts OUT to the legacy
    generic prompt.
  - `nl2pbip/prompts.py` `PROMPT_CHANGELOG` updated; the v5 entry's
    misleading "OLS + Fabric" claim replaced with a reference to
    the new v6 entry (rule 31).

### Added
- **Rule 31 (PRE-FLIGHT SCRUBBING) in the system prompt.** The
  planner prompt now tells the LLM that
  `nl2pbip.prompt_polisher.DefaultPromptPolisher` runs every
  message through a deterministic scrubber before it leaves the
  Python process. The rule lists the redaction markers
  (`[REDACTED:PII]`, `[REDACTED:SECRET]`, `[INJECTION_SCRUBBED]`)
  and the recognised secret prefixes (`sk-`, `AKIA`, `ghp_`,
  `xoxb-`, `ya29.`, `AIza`, `Bearer`). It also instructs the LLM
  to **prefer placeholder patterns** (`example.com`,
  `sk-FAKEPLACEHOLDER...`) rather than realistic-looking PII or
  secrets in examples — otherwise the scrubber strips the LLM's
  own output and the user has to redo the request.
- **`PROMPT_CHANGELOG` v6 entry** documenting rule 31 and the
  module-docstring fix.
- **New regression test class `TestPreFlightScrubbingGuidance`**
  in `tests/test_prompts.py` (5 tests) asserting that rule 31
  appears in the prompt, that the polisher's marker tokens are
  documented, that the secret-prefix list is included, that the
  placeholder-pattern recommendation is present, and that rule
  31 appears in the numbered-rules list. Future drift in this
  area fails CI.
- **Updated `TestReadmeConsistency` claim count** to keep the
  headline test count in sync with the actual collection.

## [1.2.0] - 2026-09-13

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
