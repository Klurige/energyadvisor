"""Reject release-branch pushes whose committed version is already released."""

import json
from pathlib import PurePosixPath
import re
import subprocess
import sys


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def check_version(commit, remote):
    paths = git("ls-tree", "-r", "--name-only", commit, "--", "custom_components")
    versions = []
    for path in paths.splitlines():
        parts = PurePosixPath(path).parts
        if len(parts) != 3 or parts[-1] != "manifest.json":
            continue
        try:
            data = json.loads(git("show", f"{commit}:{path}"))
        except json.JSONDecodeError as error:
            raise SystemExit(f"pre-push: invalid JSON in {path}: {error}")
        version = data.get("version") if isinstance(data, dict) else None
        if not isinstance(version, str) or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+", version
        ):
            raise SystemExit(f"pre-push: invalid version in {path}: {version}")
        versions.append(version)
    if not versions:
        raise SystemExit("pre-push: no integration manifests found in pushed commit.")
    if len(set(versions)) != 1:
        raise SystemExit("pre-push: manifest versions must match in pushed commit.")

    version = versions[0]
    ref = f"refs/tags/{version}"
    tags = git("ls-remote", "--tags", remote, ref, f"{ref}^{{}}")
    refs = {}
    for line in tags.splitlines():
        sha, tag_ref = line.split()
        refs[tag_ref] = sha
    target = refs.get(f"{ref}^{{}}", refs.get(ref))
    if target is not None and target != commit:
        raise SystemExit(
            f"pre-push: tag {version} already exists on a different commit on {remote}. "
            "Fetch release tags, bump the manifest version, and commit it before "
            "pushing to master."
        )


def main():
    remote = sys.argv[2]
    for line in sys.stdin:
        _, local_sha, remote_ref, _ = line.split()
        if remote_ref == "refs/heads/master" and local_sha.strip("0"):
            check_version(local_sha, remote)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(
            f"pre-push: could not verify release version (git exited {error.returncode})."
        )
