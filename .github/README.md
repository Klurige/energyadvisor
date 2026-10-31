# GitHub Actions

This directory contains the repository's CI and release workflows.

## Pull requests

`workflows/pull.yml` runs for pull requests. It checks the integration with
HACS and Hassfest, verifies formatting with Black (`black --check .`), and
runs the unit tests with pytest.

## Branch pushes

`workflows/push.yml` runs when commits are pushed to `release`,
`development`, or `master`. It runs the same HACS and Hassfest validation,
Black formatting check, and pytest suite as the pull request workflow.

## Local pre-commit hook

The tracked hook is `.githooks/pre-commit`. Enable it in a clone with:

```bash
git config --local core.hooksPath .githooks
```

On each commit, it checks that Black and pytest are available, runs
`black --check .` and `pytest --maxfail=1 --disable-warnings -q`, then updates
and stages the integration manifest version to one patch above the nearest
reachable numeric release tag. Fetch tags first with `git fetch --tags` if
they are missing from the clone.

## Releases

`workflows/release.yml` is started manually with **Actions → Release → Run
workflow**. Choose a branch; running it from a tag is rejected. The workflow
reads the committed `version` from every `custom_components/*/manifest.json`,
requires each version to be valid `major.minor.patch` format and all versions
to match, and uses that version as the release tag.

If that tag already exists on another commit, the workflow stops. If it does
not exist, the workflow creates and pushes an annotated tag. It then creates
a GitHub release with generated release notes, or leaves an existing release
in place. The workflow does not build or upload release artifacts.
