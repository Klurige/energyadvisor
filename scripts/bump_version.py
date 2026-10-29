"""Stage the next patch version relative to the nearest reachable release tag."""

import json
from pathlib import Path
import re
import subprocess
import sys


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def main():
    root = Path(git("rev-parse", "--show-toplevel"))
    try:
        tag = git(
            "describe",
            "--tags",
            "--abbrev=0",
            "--match",
            "[0-9]*.[0-9]*.[0-9]*",
            "--exclude",
            "*[!0-9.]*",
        )
    except subprocess.CalledProcessError:
        raise SystemExit(
            "pre-commit: no reachable version tag found. Fetch the release tags first."
        )
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise SystemExit(f"pre-commit: invalid version tag: {tag}")
    major, minor, patch = map(int, tag.split("."))
    version = f"{major}.{minor}.{patch + 1}"

    manifests = git("ls-files", "custom_components/*/manifest.json").splitlines()
    if not manifests:
        raise SystemExit("pre-commit: no tracked integration manifests found.")

    updates = []
    for manifest in manifests:
        staged = git("show", f":{manifest}")
        data = json.loads(staged)
        if data.get("version") == version:
            continue
        path = root / manifest
        if path.read_text().strip() != staged:
            raise SystemExit(
                f"pre-commit: {manifest} has unstaged changes. "
                "Stage or stash them before bumping the version."
            )
        data["version"] = version
        updates.append((manifest, json.dumps(data, indent=2) + "\n"))

    for manifest, content in updates:
        (root / manifest).write_text(content)
        subprocess.run(["git", "add", "--", manifest], check=True)
        print(f"pre-commit: staged {manifest} at version {version}")


if __name__ == "__main__":
    main()
