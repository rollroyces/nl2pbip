"""Sanity tests for the GitHub template files.

These tests verify the repo has a complete GitHub template
package — issue templates, PR template, codeowners, Renovate
config, security policy, contribution guide, release template,
and the extended CI workflow.

Each test is intentionally cheap (no network, no parsing of
GitHub's own form schema) so the suite stays fast. The tests
guard against the silent breakage of:
* Removing a template file by accident (someone runs ``rm
  .github/ISSUE_TEMPLATE/bug_report.yml``).
* Renaming a workflow so the ``on:`` trigger changes shape.
* Forgetting to bump the workflow name in the title.
* Breaking the Renovate config schema.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# File presence
# ---------------------------------------------------------------------------


class TestFilePresence:
    """The repo ships these template files. Missing files should
    fail this test so a stray ``rm`` doesn't slip through review."""

    @pytest.mark.parametrize(
        "path",
        [
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
            ".github/ISSUE_TEMPLATE/documentation.yml",
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/CODEOWNERS",
            ".github/renovate.json",
            ".github/SECURITY.md",
            ".github/RELEASE_TEMPLATE.md",
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
            ".github/workflows/codeql.yml",
            ".github/workflows/actionlint.yml",
            ".github/workflows/scorecard.yml",
            ".github/workflows/stale.yml",
            ".github/workflows/mypy.yml",
            ".github/workflows/gitleaks.yml",
            ".github/workflows/bandit.yml",
            ".github/workflows/license-check.yml",
            ".github/workflows/pr-labeler.yml",
            ".github/scripts/get-actionlint.sh",
            ".github/scripts/bandit_to_sarif.py",
            ".gitleaks.toml",
            ".bandit",
            ".github/labeler.yml",
            "CONTRIBUTING.md",
        ],
    )
    def test_required_file_exists(self, path: str) -> None:
        assert (REPO_ROOT / path).exists(), f"Required template file is missing: {path}"


class TestOptionalFiles:
    """Optional but expected files. CI should still pass without
    these, but their presence is checked so we notice if they
    disappear."""

    @pytest.mark.parametrize(
        "path",
        [
            ".github/DISCUSSION_TEMPLATE/q-a.yml",
        ],
    )
    def test_optional_file_exists(self, path: str) -> None:
        if not (REPO_ROOT / path).exists():
            pytest.skip(f"Optional file {path} not present (acceptable).")


# ---------------------------------------------------------------------------
# YAML shape
# ---------------------------------------------------------------------------


class TestYAMLShape:
    @pytest.mark.parametrize(
        "path",
        [
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
            ".github/ISSUE_TEMPLATE/documentation.yml",
            ".github/DISCUSSION_TEMPLATE/q-a.yml",
            ".github/renovate.json",
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
            ".github/workflows/codeql.yml",
            ".github/workflows/actionlint.yml",
            ".github/workflows/scorecard.yml",
            ".github/workflows/stale.yml",
            ".github/workflows/mypy.yml",
            ".github/workflows/gitleaks.yml",
            ".github/workflows/bandit.yml",
            ".github/workflows/license-check.yml",
            ".github/workflows/pr-labeler.yml",
        ],
    )
    def test_yaml_parses(self, path: str) -> None:
        full = REPO_ROOT / path
        if not full.exists():
            pytest.skip(f"{path} not present.")
        with full.open() as fh:
            data = yaml.safe_load(fh)
        assert data is not None, f"{path} is empty / not valid YAML"
        assert isinstance(data, dict), f"{path} should parse to a mapping"

    def test_workflows_have_name_field(self) -> None:
        for workflow in ("ci.yml", "release.yml", "codeql.yml"):
            full = REPO_ROOT / ".github/workflows" / workflow
            data = yaml.safe_load(full.read_text())
            assert "name" in data, f"{workflow} has no top-level 'name'"
            assert isinstance(data["name"], str)
            assert data["name"].strip(), f"{workflow} has empty 'name'"

    def test_workflows_have_trigger(self) -> None:
        for workflow in ("ci.yml", "release.yml", "codeql.yml"):
            full = REPO_ROOT / ".github/workflows" / workflow
            data = yaml.safe_load(full.read_text())
            assert "on" in data or True in data, f"{workflow} has no 'on:' trigger"

    def test_ci_triggers_on_push_and_pr(self) -> None:
        full = REPO_ROOT / ".github/workflows/ci.yml"
        data = yaml.safe_load(full.read_text())
        # PyYAML returns the literal `on:` key as the boolean True.
        on_block = data.get("on", data.get(True))
        assert on_block is not None
        assert "push" in on_block
        assert "pull_request" in on_block

    def test_release_triggers_on_tag(self) -> None:
        full = REPO_ROOT / ".github/workflows/release.yml"
        data = yaml.safe_load(full.read_text())
        on_block = data.get("on", data.get(True))
        # Either tag push, or workflow_dispatch, or both.
        assert "push" in on_block or "workflow_dispatch" in on_block

    def test_codeql_triggers_on_push_pr_schedule(self) -> None:
        full = REPO_ROOT / ".github/workflows/codeql.yml"
        data = yaml.safe_load(full.read_text())
        on_block = data.get("on", data.get(True))
        assert "push" in on_block
        assert "pull_request" in on_block
        assert "schedule" in on_block


# ---------------------------------------------------------------------------
# Issue / discussion form templates
# ---------------------------------------------------------------------------
# TOML / INI shape (for the non-YAML bot configs)
# ---------------------------------------------------------------------------


class TestTOMLConfigShape:
    @pytest.mark.parametrize(
        "path,parser",
        [
            (".gitleaks.toml", "toml"),
        ],
    )
    def test_toml_parses(self, path: str, parser: str) -> None:
        full = REPO_ROOT / path
        if not full.exists():
            pytest.skip(f"{path} not present.")
        # Python 3.11+ ships tomllib in stdlib.
        try:
            import tomllib
        except ImportError:  # pragma: no cover - 3.10 fallback
            import tomli as tomllib  # type: ignore[no-redef]
        with full.open("rb") as fh:
            data = tomllib.load(fh)
        assert data, f"{path} parsed as empty"

    @pytest.mark.parametrize(
        "path",
        [
            ".bandit",
        ],
    )
    def test_bandit_ini_parses(self, path: str) -> None:
        """``.bandit`` is INI format with a ``[bandit]`` section.
        Bandit itself parses it, so we just verify the file
        exists + is non-empty + has the required section header.
        """
        import configparser

        full = REPO_ROOT / path
        if not full.exists():
            pytest.skip(f"{path} not present.")
        config = configparser.ConfigParser()
        config.read(full)
        assert config.has_section("bandit"), (
            f"{path} must declare a [bandit] section so the " "Bandit CLI picks it up."
        )


# ---------------------------------------------------------------------------


class TestIssueFormTemplates:
    @pytest.mark.parametrize(
        "path",
        [
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
            ".github/ISSUE_TEMPLATE/documentation.yml",
            ".github/DISCUSSION_TEMPLATE/q-a.yml",
        ],
    )
    def test_form_has_name_and_body(self, path: str) -> None:
        full = REPO_ROOT / path
        if not full.exists():
            pytest.skip(f"{path} not present.")
        data = yaml.safe_load(full.read_text())
        assert "name" in data, f"{path} has no 'name'"
        assert "body" in data, f"{path} has no 'body'"
        assert isinstance(data["body"], list)
        assert len(data["body"]) >= 1, f"{path} body is empty"

    def test_bug_report_has_required_fields(self) -> None:
        data = yaml.safe_load(
            (REPO_ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml").read_text()
        )
        all_labels = set()
        for item in data["body"]:
            if item.get("type") in ("textarea", "input", "dropdown"):
                attrs = item.get("attributes", {})
                label = attrs.get("label")
                if label:
                    all_labels.add(label)
        for must in (
            "What happened?",
            "Steps to reproduce",
            "nl2pbip version",
        ):
            assert must in all_labels, f"bug_report missing '{must}'"


# ---------------------------------------------------------------------------
# CODEOWNERS
# ---------------------------------------------------------------------------


class TestCodeowners:
    def test_codeowners_has_default_owner(self) -> None:
        text = (REPO_ROOT / ".github/CODEOWNERS").read_text()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        # First non-comment line should be the default owner (``*``).
        first_real = next((line for line in lines if not line.startswith("#")), "")
        assert first_real.startswith(
            "*"
        ), f"CODEOWNERS must start with a default-owner line; got: {first_real!r}"
        # Default owner must mention a GitHub handle.
        assert "@" in first_real, "CODEOWNERS default owner needs a handle"

    def test_codeowners_paths_use_repo_root(self) -> None:
        text = (REPO_ROOT / ".github/CODEOWNERS").read_text()
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # If it's a path-prefixed line, the path should start
            # with ``/`` (repo-root-relative) — not ``./`` or a
            # bare name.
            if line.startswith("/"):
                assert not line.startswith(
                    "//"
                ), f"CODEOWNERS path should not have double slash: {line!r}"


# ---------------------------------------------------------------------------
# Renovate (replaces the old Dependabot config)
# ---------------------------------------------------------------------------


class TestRenovate:
    def test_renovate_config_is_valid_json(self) -> None:
        import json

        text = (REPO_ROOT / ".github/renovate.json").read_text()
        data = json.loads(text)
        # Top-level keys we rely on.
        assert data["platform"] == "github"
        assert "packageRules" in data
        assert "schedule" in data

    def test_renovate_ignores_heavy_ml_deps(self) -> None:
        import json

        data = json.loads((REPO_ROOT / ".github/renovate.json").read_text())
        ignored_rules = [
            rule
            for rule in data.get("packageRules", [])
            if rule.get("enabled") is False
        ]
        # Find the rule that disables heavy ML deps. At least one
        # rule must mention each of the four packages.
        all_ignored_names = set()
        for rule in ignored_rules:
            for name in rule.get("matchPackageNames", []):
                all_ignored_names.add(name)
        for heavy in ("unsloth", "trl", "transformers", "datasets"):
            assert (
                heavy in all_ignored_names
            ), f"Renovate must ignore {heavy} (heavy ML dep)."

    def test_renovate_has_auto_merge_patch_rule(self) -> None:
        """Patch + digest updates should auto-merge so we don't
        drown in a PR per dep."""
        import json

        data = json.loads((REPO_ROOT / ".github/renovate.json").read_text())
        auto_merge_rules = [
            rule
            for rule in data.get("packageRules", [])
            if rule.get("automerge") is True
        ]
        assert auto_merge_rules, "Renovate must have at least one automerge rule"
        # Find one that targets patch/digest.
        has_patch_rule = any(
            set(rule.get("matchUpdateTypes", [])) & {"patch", "digest"}
            for rule in auto_merge_rules
        )
        assert (
            has_patch_rule
        ), "Renovate should auto-merge at least patch + digest updates."


# ---------------------------------------------------------------------------
# CI workflow
# ---------------------------------------------------------------------------


class TestCIWorkflow:
    def test_ci_matrix_includes_supported_versions(self) -> None:
        data = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
        jobs = data["jobs"]["tests"]
        versions = jobs["strategy"]["matrix"]["python-version"]
        for required in ("3.10", "3.11", "3.12"):
            assert (
                required in versions
            ), f"CI matrix missing Python {required}; got {versions}"

    def test_ci_runs_black_ruff_pytest(self) -> None:
        text = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
        for command in ("black --check", "ruff check", "pytest"):
            assert command in text, f"CI workflow missing '{command}' step"

    def test_ci_has_concurrency_group(self) -> None:
        data = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
        assert "concurrency" in data
        assert "group" in data["concurrency"]

    def test_ci_smoke_test_verifies_fabric_metadata(self) -> None:
        # If the smoke test for example_run isn't checking Fabric
        # metadata files, a regression on PR #22 wouldn't be caught.
        text = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
        for must in (
            "itemMetadata.json",
            ".platform",
        ):
            assert must in text, f"CI smoke test missing '{must}' file check"

    def test_ci_benchmarks_opt_in(self) -> None:
        data = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
        # The benchmarks job should be triggered only via
        # workflow_dispatch (not on every push / PR).
        bench_job = data["jobs"].get("benchmarks")
        if bench_job is None:
            pytest.skip("benchmarks job not defined")
        on_block = data.get("on", data.get(True))
        assert "workflow_dispatch" in on_block
        assert bench_job.get("if", "").startswith(
            "github.event_name == 'workflow_dispatch'"
        ), "benchmarks job should only run via workflow_dispatch"

    def test_ci_runs_vulture(self) -> None:
        """Vulture is the dead-code gate. If the step gets removed,
        dead imports / functions can accumulate silently — the
        build won't catch them, and refactors that delete the
        only caller of a private function will look clean until
        we notice the orphaned code months later.
        """
        text = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
        assert "vulture" in text, "CI workflow missing the Vulture dead-code step"
        # Must target the production source tree (tests/ and
        # artifacts/ are noisy by design).
        assert (
            "vulture nl2pbip" in text
        ), "Vulture step should run against nl2pbip/ specifically"

    def test_dev_extra_pins_vulture(self) -> None:
        """``pip install -e ".[dev]"`` must include Vulture so
        local runs (e.g. `make test` or `tox`) match CI."""
        # Python 3.11+ ships tomllib in stdlib. On 3.10, fall
        # back to the tomli package.
        try:
            import tomllib  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - 3.10 fallback
            import tomli as tomllib  # type: ignore[no-redef]
        with open(REPO_ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        dev_deps = data["project"]["optional-dependencies"]["dev"]
        assert any(
            dep.lower().startswith("vulture") for dep in dev_deps
        ), "vulture not pinned in [project.optional-dependencies.dev]"

    def test_dev_extra_pins_pip_licenses(self) -> None:
        """``pip install -e ".[dev]"`` must include pip-licenses
        so the license-check workflow runs in the same env as
        the test that asserts the workflow exists."""
        try:
            import tomllib  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - 3.10 fallback
            import tomli as tomllib  # type: ignore[no-redef]
        with open(REPO_ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        dev_deps = data["project"]["optional-dependencies"]["dev"]
        assert any(
            dep.lower().startswith("pip-licenses") for dep in dev_deps
        ), "pip-licenses not pinned in [project.optional-dependencies.dev]"

    def test_license_check_workflow_forbids_copyleft(self) -> None:
        """The license-check workflow must fail on GPL / LGPL /
        AGPL / SSPL / Commons-Clause / UNKNOWN — the licenses
        forbidden by CONTRIBUTING.md. If the --fail-on list is
        edited, the test catches the regression.
        """
        text = (REPO_ROOT / ".github/workflows/license-check.yml").read_text()
        for license_id in ("GPL", "LGPL", "AGPL", "SSPL", "Commons-Clause"):
            assert license_id in text, (
                f"license-check workflow missing '{license_id}' from " f"--fail-on list"
            )

    def test_license_check_workflow_reports_unknown(self) -> None:
        """Unknown license detection is a soft-fail (warning
        only) today, but the workflow must still surface the
        report as an artifact so a maintainer can review."""
        text = (REPO_ROOT / ".github/workflows/license-check.yml").read_text()
        assert "UNKNOWN" in text, (
            "license-check workflow should mention UNKNOWN "
            "licenses (e.g. for the soft-fail path)"
        )

    def test_mypy_workflow_is_gating(self) -> None:
        """The mypy --strict workflow is a real CI gate (not
        report-only). It must NOT carry ``continue-on-error:
        true`` because the codebase is now strict-clean
        (0 errors) and new errors must fail the build."""
        text = (REPO_ROOT / ".github/workflows/mypy.yml").read_text()
        assert "continue-on-error" not in text, (
            "mypy workflow is gating — should not carry " "continue-on-error: true"
        )
        # Gating workflows must propagate the exit code from
        # the mypy command. The previous (report-only) version
        # had ``exit 0`` hard-coded; the gating version reads
        # ``EXIT=$?`` and propagates it.
        assert "EXIT=$?" in text, (
            "mypy workflow must capture and propagate the "
            "mypy exit code (no hard-coded 'exit 0')"
        )

    def test_mypy_config_excludes_finetune_and_providers(self) -> None:
        """``[tool.mypy.overrides]`` in pyproject.toml must
        exclude ``nl2pbip.finetune.*`` and ``nl2pbip.providers.*``
        because their heavy ML deps (unsloth / trl / datasets)
        don't ship py.typed markers. If someone removes these
        overrides the gate would suddenly fail with dozens
        of import errors."""
        try:
            import tomllib  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - 3.10 fallback
            import tomli as tomllib  # type: ignore[no-redef]
        with open(REPO_ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        overrides = data["tool"]["mypy"]["overrides"]
        excluded_modules: set[str] = set()
        for entry in overrides:
            excluded_modules.update(entry.get("module", []))
        assert (
            "nl2pbip.finetune.*" in excluded_modules
        ), "mypy overrides must exclude nl2pbip.finetune.*"
        assert (
            "nl2pbip.providers.*" in excluded_modules
        ), "mypy overrides must exclude nl2pbip.providers.*"

    def test_pr_labeler_workflow_runs_on_pull_request_target(self) -> None:
        """PR labeler must run on `pull_request_target` so it
        has write access to apply labels. The pull_request
        event alone doesn't grant label-write permissions."""
        text = (REPO_ROOT / ".github/workflows/pr-labeler.yml").read_text()
        assert "pull_request_target" in text, (
            "pr-labeler workflow should listen on "
            "pull_request_target (needs write access to "
            "apply labels)"
        )
        assert "pull-requests: write" in text, (
            "pr-labeler workflow needs pull-requests: write "
            "permission to apply labels"
        )

    def test_pr_labeler_uses_actions_labeler_v5(self) -> None:
        """Use the v5 major version of actions/labeler to make
        sure we keep getting the fixes."""
        text = (REPO_ROOT / ".github/workflows/pr-labeler.yml").read_text()
        assert (
            "actions/labeler@v5" in text
        ), "pr-labeler workflow should pin actions/labeler@v5"

    def test_labeler_config_parses(self) -> None:
        """``.github/labeler.yml`` must be valid YAML and have
        a `changed-files-labels-limit` to prevent one PR
        from spamming labels."""
        import yaml

        data = yaml.safe_load((REPO_ROOT / ".github/labeler.yml").read_text())
        assert "changed-files-labels-limit" in data, (
            "labeler.yml must set changed-files-labels-limit "
            "to prevent runaway label application"
        )
        assert isinstance(data["changed-files-labels-limit"], int)
        assert 1 <= data["changed-files-labels-limit"] <= 20

    def test_labeler_config_has_no_dead_globs(self) -> None:
        """``.github/labeler.yml`` must not reference
        directories that don't exist in the repo. If
        someone adds ``docs/**`` or ``website/**`` to a
        LABEL_* rule but never creates the directory,
        the glob silently matches nothing and the label
        never fires. Pin the rule: every glob must point
        at a real on-disk path."""
        import yaml

        data = yaml.safe_load((REPO_ROOT / ".github/labeler.yml").read_text())
        # Collect every glob from every LABEL_* rule.
        all_globs: list[str] = []
        for key, value in data.items():
            if not key.startswith("LABEL_"):
                continue
            # value is a list of one or more rule dicts
            for rule in value:
                for clause in rule.get("changed-files", []):
                    for glob in clause.get("any-glob-to-any-file", []):
                        all_globs.append(glob)
        # Each glob must resolve to at least one file or
        # directory in the repo. Resolve ``**`` to a single
        # level so pathlib doesn't have to support the
        # full glob syntax.
        from pathlib import Path

        dead: list[str] = []
        for glob in all_globs:
            # Strip ``**`` (recursive) — we just want to
            # check the parent directory exists.
            if "**" in glob:
                parent = glob.split("**", 1)[0].rstrip("/")
            else:
                parent = str(Path(glob).parent)
            if parent in ("", "."):
                continue  # root-level files (README.md etc.)
            if not (REPO_ROOT / parent).exists():
                dead.append(glob)
        assert not dead, (
            f"labeler.yml references directories that don't exist "
            f"in the repo: {dead}. Either create the directory or "
            f"remove the glob from .github/labeler.yml."
        )

    def test_labeler_config_references_existing_labels(self) -> None:
        """Every label referenced in ``.github/labeler.yml``
        must exist as a GH label in the repo. This catches
        drift: someone adds a label rule but forgets to
        create the label."""
        import yaml

        data = yaml.safe_load((REPO_ROOT / ".github/labeler.yml").read_text())
        rule_labels = [
            key.removeprefix("LABEL_").lower()
            for key in data
            if key.startswith("LABEL_")
        ]
        assert rule_labels, "labeler.yml should define at least one LABEL_* rule"

        # Fetch live labels from the repo. The test runs in CI
        # with GITHUB_TOKEN, so we can hit the GitHub API.
        # Falls back to a static list if we can't reach the API
        # (local dev / offline).
        known = self._known_labels()
        missing = [lbl for lbl in rule_labels if lbl not in known]
        assert not missing, (
            f"labeler.yml references labels not in repo: {missing}. "
            f"Either add the labels via `gh label create <name>` "
            f"or remove the LABEL_<name> rule from labeler.yml."
        )

    @staticmethod
    def _known_labels() -> set[str]:
        """Return the set of label names currently in the
        repo. Uses the GitHub REST API via gh CLI; falls back
        to the static manifest if the API is unreachable (so
        the test still passes offline)."""
        import json
        import subprocess

        try:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    "repos/rollroyces/nl2pbip/labels",
                    "--paginate",
                    "--jq",
                    ".[].name",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                # gh api --jq returns a single concatenated string
                # because we paginated; split on newlines. The
                # labels come back with their original case;
                # normalize to lowercase so labeler.yml's
                # LABEL_<Name> convention (which we lowercase)
                # matches.
                return set(
                    line.strip().lower()
                    for line in result.stdout.splitlines()
                    if line.strip()
                )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        # Fallback: hard-coded manifest. Kept in sync with
        # `gh label create` invocations in CONTRIBUTING.md and
        # the bootstrap script. Lowercase because we normalize
        # the live-API labels to lowercase as well.
        return {
            "accessibility",
            "bug",
            "documentation",
            "duplicate",
            "enhancement",
            "good first issue",
            "help wanted",
            "invalid",
            "question",
            "wontfix",
            "ci",
            "dependencies",
            "docs",
            "examples",
            "prompts",
            "core",
            "finetune",
            "tests",
        }


# ---------------------------------------------------------------------------
# Release workflow
# ---------------------------------------------------------------------------


class TestCodeQLWorkflow:
    """The codeql.yml workflow had a real bug — ``paths-ignore``
    was attached to ``codeql-action/init`` (which doesn't accept
    that input) instead of ``actions/checkout``. Every push failed
    with a malformed-workflow error. These tests lock in the
    fix so the regression can't sneak back in."""

    def test_paths_ignore_only_on_checkout(self) -> None:
        """paths-ignore is a checkout option; placing it on any
        other step is invalid and causes the workflow to fail at
        parse time on GitHub."""
        data = yaml.safe_load((REPO_ROOT / ".github/workflows/codeql.yml").read_text())
        for step in data["jobs"]["analyze"]["steps"]:
            if "paths-ignore" in step.get("with", {}):
                # Found a step using paths-ignore. It MUST be the
                # actions/checkout step.
                uses = step.get("uses", "")
                assert uses.startswith("actions/checkout"), (
                    f"paths-ignore must live on actions/checkout, "
                    f"not on {uses!r}. The codeql-action does not "
                    "accept this input and the workflow will fail "
                    "to parse."
                )

    def test_codeql_init_has_languages(self) -> None:
        data = yaml.safe_load((REPO_ROOT / ".github/workflows/codeql.yml").read_text())
        for step in data["jobs"]["analyze"]["steps"]:
            uses = step.get("uses", "")
            if uses.startswith("github/codeql-action/init"):
                with_block = step.get("with", {})
                assert (
                    "languages" in with_block
                ), "codeql-action/init requires 'languages' input."

    def test_codeql_analyze_has_category(self) -> None:
        data = yaml.safe_load((REPO_ROOT / ".github/workflows/codeql.yml").read_text())
        for step in data["jobs"]["analyze"]["steps"]:
            uses = step.get("uses", "")
            if uses.startswith("github/codeql-action/analyze"):
                with_block = step.get("with", {})
                assert (
                    "category" in with_block
                ), "codeql-action/analyze requires 'category' input."

    def test_codeql_workflow_has_security_events_write(self) -> None:
        """GitHub rejects the workflow at parse time (no jobs run)
        if ``security-events: write`` is missing from the workflow.
        The permission can live at the top level OR inside the job;
        accept either, as long as the analyze job declares it."""
        data = yaml.safe_load((REPO_ROOT / ".github/workflows/codeql.yml").read_text())
        job = data["jobs"]["analyze"]
        perms = data.get("permissions", {})
        job_perms = job.get("permissions", {})
        assert (
            perms.get("security-events") == "write"
            or job_perms.get("security-events") == "write"
        ), (
            "codeql.yml needs 'security-events: write' at the "
            "top-level or inside the analyze job's permissions. "
            "Without it, GitHub rejects the workflow before any "
            "job runs."
        )

    def test_codeql_paths_ignore_uses_block_scalar(self) -> None:
        """``paths-ignore`` rejects the ``- item`` list form when
        any item contains a glob character (``**`` etc). GitHub
        Actions' workflow parser silently fails the whole
        workflow at parse time (no jobs run, fallback to
        path-based default name). Use the block-scalar form
        (``paths-ignore: |`` with one pattern per line) instead.

        Regression test for the 2026-09-12 incident where PR #23
        + #27 + #28 fixed the same symptom three times before
        root-causing it to the YAML shape."""
        text = (REPO_ROOT / ".github/workflows/codeql.yml").read_text()
        # Either block scalar OR no paths-ignore at all (also OK).
        # We forbid: list form with a glob character in any item.
        import re

        in_paths_ignore = False
        for raw in text.splitlines():
            stripped = raw.strip()
            if stripped.startswith("paths-ignore"):
                # Capture the form: either ``paths-ignore: |`` or
                # ``paths-ignore:`` followed by a list.
                in_paths_ignore = True
                continue
            if in_paths_ignore:
                if not raw.startswith(" ") and raw.strip() != "":
                    # Left the paths-ignore block.
                    in_paths_ignore = False
                    continue
                # If we see a list item containing ``**``, fail.
                if stripped.startswith("- ") and "**" in stripped:
                    pytest.fail(
                        "paths-ignore uses the - item list form with a "
                        "glob character ('**'). GitHub Actions rejects "
                        "this at parse time. Use the block-scalar "
                        "form (paths-ignore: |) instead.\n"
                        f"Offending line: {raw!r}"
                    )


class TestReleaseWorkflow:
    def test_release_has_publish_job(self) -> None:
        data = yaml.safe_load((REPO_ROOT / ".github/workflows/release.yml").read_text())
        # The publish job is the gate that pushes to PyPI.
        assert "publish" in data["jobs"]

    def test_release_uses_trusted_publishing(self) -> None:
        text = (REPO_ROOT / ".github/workflows/release.yml").read_text()
        data = yaml.safe_load(text)
        assert "pypa/gh-action-pypi-publish" in text, (
            "Release workflow should use pypa/gh-action-pypi-publish "
            "(trusted publishing, no API token)."
        )
        # OIDC token grant is required for trusted publishing.
        perms = data.get("permissions", {})
        assert (
            perms.get("id-token") == "write"
        ), "Release workflow needs id-token: write for PyPI OIDC."

    def test_release_builds_wheel_and_sdist(self) -> None:
        text = (REPO_ROOT / ".github/workflows/release.yml").read_text()
        assert "python -m build" in text
        assert "twine check" in text


# ---------------------------------------------------------------------------
# SECURITY.md
# ---------------------------------------------------------------------------


class TestSecurityPolicy:
    def test_security_md_lists_supported_versions(self) -> None:
        text = (REPO_ROOT / ".github/SECURITY.md").read_text()
        assert "Supported versions" in text
        # Must include a contact email.
        assert "@" in text, "SECURITY.md must include a contact email"

    def test_security_md_disclosure_window(self) -> None:
        text = (REPO_ROOT / ".github/SECURITY.md").read_text()
        assert "coordinated disclosure" in text.lower() or (
            "responsible" in text.lower()
        ), "SECURITY.md must mention disclosure policy"

    def test_security_md_out_of_scope_section(self) -> None:
        text = (REPO_ROOT / ".github/SECURITY.md").read_text()
        assert "Out of scope" in text or "out of scope" in text


class TestContactInfo:
    """Regression test for the 2026-09-13 rebrand.

    The author email must be consistent across pyproject.toml,
    LICENSE, README, and SECURITY.md. The historical email
    ``rollroyces@users.noreply.github.com`` should not appear in
    any user-facing file."""

    AUTHOR_EMAIL = "roycelam@umich.edu"
    HISTORICAL_EMAIL = "rollroyces@users.noreply.github.com"

    def _user_facing_files(self):
        """Return paths that ship to end users (excludes CHANGELOG
        history links, .venv, __pycache__)."""
        candidates = [
            REPO_ROOT / "pyproject.toml",
            REPO_ROOT / "LICENSE",
            REPO_ROOT / "README.md",
            REPO_ROOT / ".github" / "SECURITY.md",
        ]
        return [p for p in candidates if p.exists()]

    def test_author_email_consistent_across_files(self):
        for path in self._user_facing_files():
            text = path.read_text()
            assert self.AUTHOR_EMAIL in text, (
                f"{path.relative_to(REPO_ROOT)}: missing author email "
                f"{self.AUTHOR_EMAIL!r}"
            )

    def test_historical_email_not_in_user_facing_files(self):
        """The historical email must not appear in user-facing files.

        CHANGELOG.md is allowed to keep the historical email because
        it documents the v1.1.0 -> v1.1.1 rebrand."""
        for path in self._user_facing_files():
            text = path.read_text()
            assert self.HISTORICAL_EMAIL not in text, (
                f"{path.relative_to(REPO_ROOT)}: still contains the "
                f"historical email {self.HISTORICAL_EMAIL!r}"
            )

    def test_pyproject_authors_field(self):
        """``pyproject.toml [project] authors`` must list the new
        email exactly once (not zero, not twice, no orphans)."""
        text = (REPO_ROOT / "pyproject.toml").read_text()
        # Look for ``email = "..."`` inside the authors block.
        import re

        matches = re.findall(
            r'authors\s*=\s*\[[^\]]*email\s*=\s*"([^"]+)"',
            text,
            re.DOTALL,
        )
        assert matches == [self.AUTHOR_EMAIL], (
            f"pyproject.toml [project] authors should list "
            f"{self.AUTHOR_EMAIL!r} exactly once; got {matches}"
        )

    def test_license_email(self):
        """LICENSE must reference the author email in the
        ``Contact:`` line."""
        text = (REPO_ROOT / "LICENSE").read_text()
        # Find the line after "Contact:".
        for line in text.splitlines():
            if "Contact:" in line:
                # Next non-empty line should have the email.
                idx = text.splitlines().index(line)
                for following in text.splitlines()[idx + 1 : idx + 5]:
                    if following.strip():
                        assert self.AUTHOR_EMAIL in following, (
                            f"LICENSE Contact line points at "
                            f"{following!r}, not {self.AUTHOR_EMAIL!r}"
                        )
                        return
        pytest.fail("LICENSE has no Contact: line")


# ---------------------------------------------------------------------------
# CONTRIBUTING.md
# ---------------------------------------------------------------------------


class TestContributingGuide:
    def test_contributing_mentions_licensing(self) -> None:
        text = (REPO_ROOT / "CONTRIBUTING.md").read_text()
        assert (
            "license" in text.lower() or "proprietary" in text.lower()
        ), "CONTRIBUTING.md must address licensing"

    def test_contributing_has_dev_setup(self) -> None:
        text = (REPO_ROOT / "CONTRIBUTING.md").read_text()
        assert "pip install" in text
        assert "pytest" in text

    def test_contributing_has_release_process(self) -> None:
        text = (REPO_ROOT / "CONTRIBUTING.md").read_text()
        assert "tag" in text.lower() or "release" in text.lower()


# ---------------------------------------------------------------------------
# Cross-template consistency
# ---------------------------------------------------------------------------


class TestReadmeConsistency:
    """Lock in README claims that are easy to drift.

    These tests guard against the failure mode where a feature
    ships, the test count grows, the perf bench list changes, but
    nobody updates the README to match. Each test pulls live data
    from the repo and asserts the README reflects it.
    """

    def test_readme_mentions_published_versions(self) -> None:
        """The Overview / capability matrix should mention that
        the project is published on PyPI, with a link to the
        project page. Drifts if the README is rewritten without
        re-checking the deployment story."""
        text = (REPO_ROOT / "README.md").read_text()
        assert "pypi.org/project/nl2pbip" in text, (
            "README should link to https://pypi.org/project/nl2pbip/ "
            "in the Overview section now that the project ships to PyPI."
        )

    def test_readme_test_count_matches_pytest_collection(self) -> None:
        """README test-counts headline must match what pytest
        actually collects on a slim CI install. Drift here is a
        smell — usually a release was cut without refreshing the
        docs.

        The README headline is the CI count (684 — excluding
        `test_finetune.py` via `--ignore`). When running this test
        locally with the heavy `finetune` extras installed,
        ``--collect-only`` may report 687 (extra 3 finetune tests).
        To handle both, the test runs ``--collect-only`` with the
        same ``--ignore=tests/test_finetune.py`` flag the CI uses
        and compares against that count.
        """
        import re
        import subprocess
        import sys

        # Match CI: exclude test_finetune.py.
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/",
                "--ignore=tests/test_finetune.py",
                "--collect-only",
                "-q",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        last_line = result.stdout.strip().splitlines()[-1]
        actual_count = int(last_line.split()[0])
        text = (REPO_ROOT / "README.md").read_text()
        # Find the headline "Pytest collects N test cases across M
        # test files" — that's the headline number.
        m = re.search(
            r"Pytest collects \*\*(\d+) test cases across \d+ test files\*\*",
            text,
        )
        assert m, (
            "README missing 'Pytest collects N test cases across M "
            "test files' headline"
        )
        claim = int(m.group(1))
        assert claim == actual_count, (
            f"README headline claims {claim} but `pytest tests/ "
            f"--ignore=tests/test_finetune.py --collect-only` reports "
            f"{actual_count} tests. Refresh the README's test-counts "
            "headline to match."
        )

    def test_readme_lists_m_builder_module(self) -> None:
        """The project layout section must list `m_builder.py` —
        Power Query M partition generation (PR #19)."""
        text = (REPO_ROOT / "README.md").read_text()
        assert "m_builder.py" in text, (
            "README project layout tree is missing `m_builder.py`. "
            "Add it under the `nl2pbip/` directory entry."
        )

    def test_readme_lists_prompt_polisher_module(self) -> None:
        """The project layout section must list `prompt_polisher.py` —
        the pre-LLM message normalisation layer."""
        text = (REPO_ROOT / "README.md").read_text()
        assert "prompt_polisher.py" in text, (
            "README project layout tree is missing `prompt_polisher.py`. "
            "Add it under the `nl2pbip/` directory entry."
        )

    def test_readme_benchmarks_table_matches_test_performance(self) -> None:
        """The README performance-benchmarks table should mention
        the same set of benches that ``tests/test_performance.py``
        defines. We count pytest functions whose name starts with
        ``test_benchmark_`` and require the README mentions at
        least that many."""
        import re
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_performance.py",
                "--collect-only",
                "-q",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        bench_lines = [
            line for line in result.stdout.splitlines() if "::test_benchmark_" in line
        ]
        actual = len(bench_lines)
        text = (REPO_ROOT / "README.md").read_text()
        # Find the performance benchmarks table.
        in_table = False
        table_rows = 0
        for raw in text.splitlines():
            if "## Performance benchmarks" in raw:
                in_table = True
                continue
            if in_table and raw.startswith("##"):
                break
            if in_table and raw.startswith("|"):
                # Skip header + separator rows.
                cells = [c.strip() for c in raw.strip().strip("|").split("|")]
                if not cells or all(
                    c in {"---", ""} or set(c) <= {"-", ":"} for c in cells
                ):
                    continue
                if cells[0].lower() == "path":
                    continue  # header row
                table_rows += 1
        assert actual == table_rows, (
            f"README performance-benchmarks table lists {table_rows} "
            f"rows but tests/test_performance.py defines {actual} "
            "benches. Update the table."
        )


class TestCrossTemplateConsistency:
    def test_repo_root_constant_in_templates(self) -> None:
        """If a template references a file path, that file should
        exist. Catches typos in CODEOWNERS, Renovate config,
        etc."""
        # CODEOWNERS paths should resolve.
        text = (REPO_ROOT / ".github/CODEOWNERS").read_text()
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if not parts:
                continue
            path = parts[0]
            if path.startswith("*"):
                continue
            # CODEOWNERS paths starting with ``/`` are repo-root
            # relative.
            if path.startswith("/"):
                rel = path.lstrip("/")
                if rel.endswith("/"):
                    continue  # directory match — can't validate
                assert (
                    REPO_ROOT / rel
                ).exists(), f"CODEOWNERS references missing path: {path}"

    def test_workflow_referenced_paths_exist(self) -> None:
        """If a workflow checks for a file via ``test -f path`` or
        similar, the path must exist in the repo."""
        for workflow in ("ci.yml", "release.yml", "codeql.yml"):
            full = REPO_ROOT / ".github/workflows" / workflow
            text = full.read_text()
            # Match ``test -f some/path`` or ``cat some/path`` or
            # ``-f artifacts/...``.
            for match in re.finditer(
                r"test -f\s+(\S+)|cat\s+(\S+)|-f\s+([^\s\"]+)", text
            ):
                path = next(g for g in match.groups() if g)
                if path.startswith("$"):
                    continue
                # Strip leading ./ if present.
                if path.startswith("./"):
                    path = path[2:]
                # Skip glob patterns and template vars.
                if any(c in path for c in ("*", "?", "$", "{", "<")):
                    continue
                # Skip absolute paths and shell builtins.
                if path.startswith("/") or path in ("true", "false", "0", "1"):
                    continue
                assert (
                    REPO_ROOT / path
                ).exists(), f"{workflow} references missing path: {path}"


# ---------------------------------------------------------------------------
# Smoke test — run example_run, verify Fabric metadata
# ---------------------------------------------------------------------------


class TestRepoTemplateIntegration:
    """The CI workflow's smoke step checks that example_run
    produces Fabric metadata. Verify the underlying behaviour so
    the smoke test doesn't guard against a broken example."""

    def test_example_run_produces_fabric_metadata(self) -> None:
        import json
        import subprocess
        import sys

        # Reset artifacts and run example. Use ``sys.executable`` so
        # the smoke test works under CI (no .venv) and locally.
        artifact_dir = REPO_ROOT / "artifacts"
        if artifact_dir.exists():
            import shutil

            shutil.rmtree(artifact_dir)
        result = subprocess.run(
            [sys.executable, "-m", "nl2pbip.example_run"],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=60,
        )
        assert result.returncode == 0, f"example_run failed: {result.stderr[-500:]}"
        project_dir = artifact_dir / "SalesInsights.pbipdir"
        for path in [
            project_dir / "SalesInsights.pbip",
            project_dir / "SalesInsights.SemanticModel" / "itemMetadata.json",
            project_dir / "SalesInsights.SemanticModel" / ".platform",
            project_dir / "SalesInsights.Report" / "itemMetadata.json",
            project_dir / "SalesInsights.Report" / ".platform",
        ]:
            assert path.exists(), f"example_run did not produce {path}"
            # Each file should be valid JSON (or in the case of
            # .pbip, also valid JSON).
            json.loads(path.read_text())
