# Contributing to nl2pbip

Thanks for your interest in the project! This document covers the
mechanics of contributing — for project direction, see the
README's "What ships" section.

## Licensing — read this first

`nl2pbip` is **proprietary commercial software**. By submitting a
contribution (PR, patch, issue, discussion comment), you agree
that:

* You have the right to contribute the code under the project's
  Commercial License.
* Your contribution is your own original work, or you have the
  necessary rights to submit it.
* The maintainer may relicense your contribution as part of the
  proprietary codebase.

If you're contributing on behalf of an employer, confirm with
them that the contribution falls within the scope of your
employment agreement.

**Do not** submit code that itself is GPL / AGPL / SSPL /
Commons-Clause, or that imports / links to GPL / AGPL / SSPL
libraries at runtime. The project's optional dependencies
(`transformers`, `trl`, `unsloth`) are not bundled and are not
considered part of the project for licensing purposes.

## Development setup

```bash
git clone https://github.com/rollroyces/nl2pbip.git
cd nl2pbip

# Create a venv (Mac quirk: PYTHONPATH can hijack fresh venvs).
env -u PYTHONPATH python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

If you're working on the LLM client surface or finetune code,
also `pip install -e ".[anthropic,finetune]"`.

## Running tests

```bash
# Fast (no LLM calls, no GPU).
pytest tests/

# With coverage.
pytest tests/ --cov=nl2pbip --cov-report=term-missing

# Performance benchmarks (opt-in, ~2 s).
NL2PBIP_RUN_BENCHMARKS=1 pytest tests/test_performance.py -s

# Example run end-to-end (no LLM; uses the static plan in
# example_run.py).
python -m nl2pbip.example_run
```

## Linting

```bash
black --check .
ruff check .
```

Both must be clean before opening a PR.

## Branch + commit conventions

* Branch from `main`: `git checkout -b feat/<short-name>` or
  `fix/<short-name>` or `docs/<short-name>`.
* Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):
  `feat: add field parameters`, `fix: parser off-by-one`,
  `docs: README quickstart rewrite`, `perf: benchmark suite`,
  `refactor: split packager`.
* One PR per feature / fix. Squash-merge on landing.

## Pull request checklist

The PR template (`.github/PULL_REQUEST_TEMPLATE.md`) walks you
through the gate. The non-negotiables:

* [ ] `pytest tests/` passes locally.
* [ ] `black --check .` and `ruff check .` clean.
* [ ] New / updated tests for every behaviour change.
* [ ] `CHANGELOG.md` `[Unreleased]` block updated.
* [ ] `README.md` updated if the change is user-visible.
* [ ] No new third-party runtime dependencies without prior
      discussion in an issue.
* [ ] No GPL / AGPL / SSPL code or dependencies.

## Reporting bugs

Use the **bug report** issue template
(`.github/ISSUE_TEMPLATE/bug_report.yml`). Include:

* `nl2pbip.__version__`
* Python version
* OS
* A minimal reproducer
* The full traceback

## Proposing features

Use the **feature request** template. The maintainer triages
weekly; expect a response within 7 days. PRs without a prior
issue are welcome for small fixes; for new features please open
an issue first so we can agree on the API surface.

## Code style

* Type hints everywhere — `mypy --strict-clean` is the target.
* Docstrings are Google-style.
* `from __future__ import annotations` at the top of every
  module.
* No `print()` in production code (use `logging`).
* Prefer small, named dataclasses over dicts.

## Release process

Maintainer-only, but documented for transparency:

1. Update `CHANGELOG.md`: move `[Unreleased]` entries to
   `[X.Y.Z]`.
2. Bump `nl2pbip.__version__` and `pyproject.toml` `[project]
   version`.
3. `git commit -m "chore: bump version to X.Y.Z"`.
4. Tag: `git tag -a vX.Y.Z -m "vX.Y.Z"`.
5. `git push origin main --follow-tags` — the `release.yml`
   workflow builds, publishes to PyPI, and opens a GitHub
   release.
