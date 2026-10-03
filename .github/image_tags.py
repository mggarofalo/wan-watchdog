"""Validate release tags and decide whether a release promotes the stable image."""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import health


def promotes_stable(ref: str, version: str) -> bool:
    if not ref.startswith("refs/tags/"):
        return False
    tag = ref.removeprefix("refs/tags/")
    match = re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
                         r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?", tag)
    if not match or tag != f"v{version}":
        raise ValueError(f"Release tag {tag!r} must be valid semver and match health.VERSION {version!r}")
    prerelease = match.group(4)
    if prerelease and any(part.isdigit() and len(part) > 1 and part.startswith("0")
                          for part in prerelease.split(".")):
        raise ValueError("Numeric prerelease identifiers must not have leading zeroes")
    return match.group(3) == "0" and prerelease is None


if __name__ == "__main__":
    print(f"stable={str(promotes_stable(os.environ.get('GITHUB_REF', ''), health.VERSION)).lower()}")
