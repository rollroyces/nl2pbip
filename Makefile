# nl2pbip developer Makefile.
#
# Targets:
#
#   make test   — full CI-equivalent suite: pytest + vulture
#                 (dead-code) + black --check (format) + ruff
#                 (lint) + mypy --strict (types) + bandit
#                 (security). Run this before pushing a PR.
#
#   make lint   — quick style pass: black --check + ruff.
#                 Use this in a tight edit loop when you only
#                 want format / lint feedback.
#
#   make type   — mypy --strict only. Useful when triaging a
#                 type error without re-running the full test
#                 suite.
#
#   make bench  — opt-in benchmark suite. Sets the
#                 ``NL2PBIP_RUN_BENCHMARKS=1`` env var that
#                 ``tests/test_performance.py`` looks for; the
#                 suite is skipped under the default
#                 ``make test`` because the benchmarks are
#                 slow and not unit-grade.
#
#   make example — runs ``python -m nl2pbip.example_run``,
#                 the bundled end-to-end demo (mock LLM,
#                 writes to ``artifacts/``).
#
#   make clean  — delete ``__pycache__/`` directories + stale
#                 ``.pyc`` files. Use this when you've
#                 switched branches and want to be sure stale
#                 bytecode isn't masking a regression.
#
# All targets use ``.venv/bin/`` paths so they work with the
# project's existing venv without requiring system-wide
# installs of black / mypy / ruff / bandit. If you prefer the
# system tools, prepend ``PATH=$(pwd)/.venv/bin:$$PATH`` to
# the ``make`` invocation and drop the ``.venv/bin/`` prefix
# in the recipes below.

.PHONY: test lint type bench example clean

# Use bash for ``find ... -exec`` portability + ``set -e``
# semantics. Without this, ``make`` defaults to /bin/sh on
# macOS, which is dash and chokes on some GNU-isms.
SHELL := /bin/bash

test:
	.venv/bin/python -m pytest tests/ --no-header -q
	.venv/bin/vulture nl2pbip
	.venv/bin/black --check .
	.venv/bin/ruff check .
	.venv/bin/mypy --strict --no-incremental --python-version 3.12 nl2pbip
	.venv/bin/bandit -c .bandit -r nl2pbip

lint:
	.venv/bin/black --check .
	.venv/bin/ruff check .

type:
	.venv/bin/mypy --strict --no-incremental --python-version 3.12 nl2pbip

bench:
	NL2PBIP_RUN_BENCHMARKS=1 .venv/bin/python -m pytest tests/test_performance.py -v -s

example:
	.venv/bin/python -m nl2pbip.example_run

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
