"""Exercise version bumps against real temporary Git repositories."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bump_version.py"
MANIFEST = "custom_components/energyadvisor/manifest.json"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit(repo):
    git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Test commit")


@pytest.fixture
def version_repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    path = tmp_path / MANIFEST
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"domain": "energyadvisor", "version": "0.0.17"}))
    git(tmp_path, "add", ".")
    commit(tmp_path)
    git(tmp_path, "tag", "0.0.17")
    return tmp_path


def bump(repo):
    return subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=repo, capture_output=True, text=True
    )


def test_version_advances_only_after_tagging(version_repo):
    repo = version_repo
    # A numerically higher historical tag must not override the nearest release.
    git(repo, "tag", "1.0.11")
    (repo / "change.txt").write_text("First change")
    git(repo, "add", "change.txt")
    commit(repo)
    git(repo, "tag", "0.0.18")

    result = bump(repo)
    assert result.returncode == 0, result.stderr
    assert json.loads(git(repo, "show", f":{MANIFEST}"))["version"] == "0.0.19"
    assert json.loads((repo / MANIFEST).read_text())["version"] == "0.0.19"
    commit(repo)

    result = bump(repo)
    assert result.returncode == 0, result.stderr
    assert git(repo, "diff", "--cached", "--name-only") == ""
    (repo / "change.txt").write_text("Second change")
    git(repo, "add", "change.txt")
    commit(repo)
    assert bump(repo).returncode == 0
    assert json.loads(git(repo, "show", f":{MANIFEST}"))["version"] == "0.0.19"

    git(repo, "tag", "0.0.19")
    assert bump(repo).returncode == 0
    assert json.loads(git(repo, "show", f":{MANIFEST}"))["version"] == "0.0.20"


def test_bump_preserves_staged_manifest_edits(version_repo):
    path = version_repo / MANIFEST
    path.write_text(json.dumps({"version": "0.0.17", "name": "Changed name"}))
    git(version_repo, "add", MANIFEST)

    result = bump(version_repo)

    assert result.returncode == 0, result.stderr
    assert json.loads(git(version_repo, "show", f":{MANIFEST}")) == {
        "version": "0.0.18",
        "name": "Changed name",
    }


def test_bump_rejects_unstaged_manifest_edits(version_repo):
    path = version_repo / MANIFEST
    content = json.dumps({"version": "0.0.17", "name": "Unstaged name"})
    path.write_text(content)

    result = bump(version_repo)

    assert result.returncode != 0
    assert "unstaged changes" in result.stderr
    assert path.read_text() == content
    assert json.loads(git(version_repo, "show", f":{MANIFEST}"))["version"] == "0.0.17"


def test_bump_requires_reachable_release_tag(version_repo):
    git(version_repo, "tag", "-d", "0.0.17")

    result = bump(version_repo)

    assert result.returncode != 0
    assert "Fetch the release tags first" in result.stderr
    assert git(version_repo, "diff", "--cached", "--name-only") == ""


@pytest.fixture
def release_repo(version_repo, tmp_path):
    repo = version_repo
    remote = tmp_path / "origin.git"
    git(repo, "init", "--bare", "-q", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "HEAD", "--tags")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'if [ "$2" = "view" ]; then\n'
        '  test -f "$RELEASE_CREATED"\n'
        "else\n"
        '  printf "%s\\n" "$*" >> "$RELEASE_CREATED"\n'
        "fi\n"
    )
    gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_REF_TYPE": "branch",
        "GITHUB_ENV": str(tmp_path / "github_env"),
        "RELEASE_CREATED": str(tmp_path / "release_created"),
    }
    return repo, env


def release(repo, env):
    workflow = yaml.safe_load(
        (SCRIPT.parents[1] / ".github/workflows/release.yml").read_text()
    )
    for step in workflow["jobs"]["release"]["steps"]:
        if "run" not in step:
            continue
        result = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            return result
        for line in Path(env["GITHUB_ENV"]).read_text().splitlines():
            key, value = line.split("=", 1)
            env[key] = value
    return result


def test_release_tags_committed_version_and_can_be_retried(release_repo):
    repo, env = release_repo
    assert bump(repo).returncode == 0
    commit(repo)
    head = git(repo, "rev-parse", "HEAD")

    result = release(repo, env)

    assert result.returncode == 0, result.stderr
    assert git(repo, "rev-parse", "0.0.18^{commit}") == head
    assert git(repo, "ls-remote", "--tags", "origin", "refs/tags/0.0.18")
    assert Path(env["RELEASE_CREATED"]).read_text().splitlines() == [
        "release create 0.0.18 --generate-notes --verify-tag"
    ]
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "diff", "--exit-code") == ""

    result = release(repo, env)
    assert result.returncode == 0, result.stderr
    assert len(Path(env["RELEASE_CREATED"]).read_text().splitlines()) == 1


def test_release_rejects_tag_on_different_commit(release_repo):
    repo, env = release_repo
    (repo / "change.txt").write_text("Unbumped commit")
    git(repo, "add", "change.txt")
    commit(repo)

    result = release(repo, env)

    assert result.returncode != 0
    assert "already exists on a different commit" in result.stdout
    assert not Path(env["RELEASE_CREATED"]).exists()


@pytest.mark.parametrize("version", ["invalid", "0.0.18"])
def test_release_rejects_invalid_or_mismatched_manifests(release_repo, version):
    repo, env = release_repo
    path = repo / "custom_components/other/manifest.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"version": version}))
    git(repo, "add", str(path))
    commit(repo)

    result = release(repo, env)

    assert result.returncode != 0
    assert not Path(env["RELEASE_CREATED"]).exists()
