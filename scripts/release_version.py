"""Check and set the versions of fxtr and behaviors, which are released independently.

Each package's version lives in `[project].version` of its `pyproject.toml`. behaviors declares the
fxtr versions it works with as a bounded range, `fxtr>=L,<U`, in its dependencies, and `check`
holds that range to these rules:

- The workspace's fxtr satisfies it, since that is the fxtr behaviors' tests run against.
- U is no later than the next breaking fxtr release after the workspace's fxtr (the next minor
  version before 1.0, the next major after), since behaviors can't vouch for a breaking release
  that doesn't exist yet.

CI separately tests behaviors against fxtr L from PyPI, so the lower bound can't go stale.

    uv run python scripts/release_version.py check
        Fail unless the versions and the range follow the rules above. Pre-commit runs it, and so
        does every check of a release.

    uv run python scripts/release_version.py version {fxtr,behaviors,fxtr-lower-bound}
        Print a package's version, or the lowest fxtr behaviors allows.

    uv run python scripts/release_version.py check-index {fxtr,behaviors} --index URL
        Check a release of the package against what the index (https://pypi.org or
        https://test.pypi.org) already has: fail if the version is already there, or, for
        behaviors, if fxtr L isn't; for fxtr, warn if the newest behaviors there doesn't allow the
        new fxtr. The publish workflow's dry run runs it before uploading; the checks of a release
        pull request run it too, also counting the other package the release publishes.

    uv run python scripts/release_version.py set [--fxtr X] [--behaviors X] [--fxtr-range SPEC]
        Write new versions and behaviors' fxtr range (e.g. '>=0.2.0,<0.3'), fail without changing
        anything unless the result passes `check`, then relock every uv project in the repository,
        each of which records the workspace packages' versions in its lockfile.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Collection
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
PYPROJECTS = {
    "fxtr": ROOT / "fxtr" / "pyproject.toml",
    "behaviors": ROOT / "behaviors" / "pyproject.toml",
}

# A PEP 440 public version without an epoch or local part, e.g. 0.1.0, 0.1.0a1, 0.1.0rc2.
VERSION = re.compile(r"\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?")
# behaviors' dependency on fxtr, as it is written in its pyproject.toml.
FXTR_REQUIREMENT = re.compile(r'"fxtr(?P<spec>[^"]*)"')


def project_version(package: str) -> str:
    return tomllib.loads(PYPROJECTS[package].read_text())["project"]["version"]


def behaviors_fxtr_spec() -> str:
    dependencies = tomllib.loads(PYPROJECTS["behaviors"].read_text())["project"]["dependencies"]
    requirements = (Requirement(d) for d in dependencies)
    return next((str(r.specifier) for r in requirements if r.name == "fxtr"), "")


def range_bounds(spec: str) -> tuple[Version, Version] | None:
    """`(L, U)` if `spec` is exactly `>=L,<U` with plain versions, else None."""
    try:
        specifiers = {s.operator: s.version for s in SpecifierSet(spec)}
    except InvalidSpecifier:
        return None
    if specifiers.keys() != {">=", "<"} or not all(map(VERSION.fullmatch, specifiers.values())):
        return None
    return Version(specifiers[">="]), Version(specifiers["<"])


def fxtr_lower_bound() -> str:
    if (bounds := range_bounds(spec := behaviors_fxtr_spec())) is None:
        sys.exit(f"behaviors' fxtr requirement {spec!r} is not '>=L,<U'; run `check`")
    return str(bounds[0])


def next_breaking(version: Version) -> Version:
    major, minor = (version.release + (0,))[:2]
    return Version(f"0.{minor + 1}") if major == 0 else Version(f"{major + 1}")


def check() -> list[str]:
    errors = []
    for package in PYPROJECTS:
        if not VERSION.fullmatch(version := project_version(package)):
            errors.append(f"{package}'s version {version!r} is not a plain PEP 440 version")
    if errors:
        return errors
    fxtr = Version(project_version("fxtr"))
    spec = behaviors_fxtr_spec()
    if (bounds := range_bounds(spec)) is None:
        errors.append(f"behaviors must require fxtr as '>=L,<U' with plain versions, not {spec!r}")
    else:
        lower, upper = bounds
        if not lower <= fxtr < upper:
            errors.append(
                f"behaviors requires fxtr{spec}, which excludes the workspace's fxtr {fxtr}"
            )
        if upper > next_breaking(fxtr):
            errors.append(
                f"behaviors requires fxtr{spec}, but its upper bound may be at most "
                f"{next_breaking(fxtr)}, fxtr's next breaking release"
            )
    return errors


def index_json(index: str, path: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{index.rstrip('/')}/pypi/{path}/json") as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def check_index(
    package: str, index: str, releasing: Collection[str] = ()
) -> tuple[list[str], list[str]]:
    """Errors and warnings for releasing the workspace's `package` to `index`, alongside the
    workspace's versions of the packages in `releasing`, which are published first or after it."""
    errors, warnings = [], []
    version = project_version(package)
    if index_json(index, f"{package}/{version}") is not None:
        errors.append(f"{package} {version} is already on {index}")
    if package == "behaviors":
        lower = fxtr_lower_bound()
        released_with = "fxtr" in releasing and lower == project_version("fxtr")
        if not released_with and index_json(index, f"fxtr/{lower}") is None:
            errors.append(f"behaviors requires fxtr>={lower}, but fxtr {lower} is not on {index}")
    # A behaviors released alongside allows the new fxtr, since `check` holds its range to it.
    elif "behaviors" not in releasing and (behaviors := index_json(index, "behaviors")) is not None:
        released = [
            v
            for v, files in behaviors["releases"].items()
            if files and not all(f["yanked"] for f in files)
        ]
        if released:
            newest = max(released, key=Version)
            release = index_json(index, f"behaviors/{newest}") or {"info": {}}
            requires = release["info"].get("requires_dist") or []
            spec = next(
                (Requirement(r).specifier for r in requires if Requirement(r).name == "fxtr"),
                SpecifierSet(),
            )
            if not spec.contains(version, prereleases=True):
                warnings.append(
                    f"the newest behaviors on {index}, {newest}, requires fxtr{spec}, which "
                    f"excludes fxtr {version}: behaviors users won't get it until a behaviors "
                    "release allows it"
                )
    return errors, warnings


def set_versions(fxtr: str | None, behaviors: str | None, fxtr_range: str | None) -> None:
    originals = {path: path.read_text() for path in PYPROJECTS.values()}
    for package, new in (("fxtr", fxtr), ("behaviors", behaviors)):
        if new is not None:
            replace_once(
                PYPROJECTS[package], f'version = "{project_version(package)}"', f'version = "{new}"'
            )
    if fxtr_range is not None:
        text = PYPROJECTS["behaviors"].read_text()
        if len(FXTR_REQUIREMENT.findall(text)) != 1:
            sys.exit("expected exactly one fxtr requirement in behaviors/pyproject.toml")
        PYPROJECTS["behaviors"].write_text(FXTR_REQUIREMENT.sub(f'"fxtr{fxtr_range}"', text))
    if errors := check():
        for path, text in originals.items():
            path.write_text(text)
        sys.exit("not changed, since the result would fail the check:\n" + "\n".join(errors))
    lockfiles = subprocess.run(
        ["git", "ls-files", "*uv.lock"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    for lockfile in lockfiles:
        project = (ROOT / lockfile).parent
        print(f"relocking {project.relative_to(ROOT)}")
        subprocess.run(["uv", "lock", "--project", str(project)], cwd=ROOT, check=True)


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    if text.count(old) != 1:
        sys.exit(f"expected exactly one {old!r} in {path.relative_to(ROOT)}")
    path.write_text(text.replace(old, new))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    commands.add_parser("version").add_argument("of", choices=[*PYPROJECTS, "fxtr-lower-bound"])
    index_parser = commands.add_parser("check-index")
    index_parser.add_argument("package", choices=PYPROJECTS)
    index_parser.add_argument("--index", required=True)
    set_parser = commands.add_parser("set")
    set_parser.add_argument("--fxtr")
    set_parser.add_argument("--behaviors")
    set_parser.add_argument("--fxtr-range")
    args = parser.parse_args()
    if args.command == "check":
        if errors := check():
            sys.exit("\n".join(errors))
    elif args.command == "version":
        if args.of == "fxtr-lower-bound":
            print(fxtr_lower_bound())
        else:
            print(project_version(args.of))
    elif args.command == "check-index":
        errors, warnings = check_index(args.package, args.index)
        for warning in warnings:
            # A workflow annotation, and a summary line for whoever approves the upload.
            print(f"::warning::{warning}")
            if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
                with open(summary, "a") as file:
                    file.write(f"- :warning: {warning}\n")
        if errors:
            sys.exit("\n".join(errors))
    elif not (args.fxtr or args.behaviors or args.fxtr_range):
        parser.error("set needs at least one of --fxtr, --behaviors, --fxtr-range")
    else:
        set_versions(args.fxtr, args.behaviors, args.fxtr_range)


if __name__ == "__main__":
    main()
