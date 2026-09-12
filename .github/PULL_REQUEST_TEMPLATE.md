<!--
Thanks for opening a PR! Please fill in this template so reviewers
can move quickly. Sections marked *required* must be answered for
the PR to be merged.
-->

## Summary

<!-- One-paragraph description of what this PR does and why. -->

## Related issue

<!-- Link any issue this PR closes: `Closes #123` or `Fixes #456`. -->

## Type of change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds capability)
- [ ] Breaking change (existing behaviour changes; call out migration)
- [ ] Documentation / examples only
- [ ] Performance / refactor (no behaviour change)

## What changed

<!-- Bullet list of the substantive changes. Group by module. -->

- `nl2pbip/<module>.py`: short description
- `tests/test_<module>.py`: N new tests
- `README.md` / `CHANGELOG.md`: docs / changelog updates

## How was this tested?

<!-- Run `pytest tests/ -v` and paste a short summary. -->

```
pytest tests/  →  NNN passed, NN skipped in X.Xs
black --check .  →  clean
ruff check .     →  clean
```

## Checklist

- [ ] *required* I ran `pytest tests/` and all tests pass locally.
- [ ] *required* `black --check .` and `ruff check .` are clean.
- [ ] *required* I added or updated tests for the change.
- [ ] *required* I updated the `CHANGELOG.md` `[Unreleased]` block.
- [ ] *required* The PR title is `<scope>: <imperative summary>` (e.g. `feat: field parameters`, `fix: parser off-by-one`).
- [ ] I ran `git pull --rebase origin main` and resolved any conflicts.
- [ ] This PR adds **no** new third-party runtime dependencies.
- [ ] This PR adds **no** GPL / AGPL dependencies (project is proprietary).
- [ ] If the change is user-visible, I updated the `README.md`.

## Breaking changes

<!-- If checked above, call out every API / behaviour change. Migration steps? -->

## Screenshots / PBIP snippets

<!-- Optional: paste a snippet of the new TMDL output, a screenshot of
the report, or the relevant JSON plan. -->
