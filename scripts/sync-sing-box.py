#!/usr/bin/env python3
"""Synchronize the vendored sing-box source with an exact stable release."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
VENDOR_DIR = ROOT / "singbox-go"
VERSION_FILE = ROOT / "UPSTREAM_VERSION"
COMMIT_FILE = ROOT / "UPSTREAM_COMMIT"
GO_VERSION_FILE = ROOT / ".go-version"
UPSTREAM_REPOSITORY = "SagerNet/sing-box"
UPSTREAM_URL = f"https://github.com/{UPSTREAM_REPOSITORY}.git"
PROJECT_REPOSITORY = "LorenEteval/sing-box-python"
PYPI_PROJECT = "sing-box-python"
PROJECT_ADDITIONS = frozenset(
    {
        "adapter/binding_traffic.go",
        "binding/cronet_purego.go",
        "binding/main.go",
        "experimental/clashapi/binding_traffic.go",
    }
)
LEGACY_NORMALIZED_MODE_COMMIT = "45ca32dcb966f07f97fc888fe8586e359dbe8405"
LEGACY_NORMALIZED_EXECUTABLES = frozenset(
    {
        ".github/build_alpine_apk.sh",
        ".github/build_openwrt_apk.sh",
        ".github/deb2ipk.sh",
        ".github/detect_track.sh",
        ".github/setup_go_for_macos1013.sh",
        ".github/setup_go_for_windows7.sh",
        ".github/update_clients.sh",
        ".github/update_cronet.sh",
        ".github/update_cronet_dev.sh",
        ".github/update_dependencies.sh",
        "docs/installation/tools/install.sh",
        "release/config/openwrt.init",
        "release/config/openwrt.prerm",
        "release/config/sing-box.initd",
        "release/local/common.sh",
        "release/local/debug.sh",
        "release/local/enable.sh",
        "release/local/install.sh",
        "release/local/install_go.sh",
        "release/local/reinstall.sh",
        "release/local/uninstall.sh",
        "release/local/update.sh",
    }
)
VERSION_PATTERN = re.compile(r"v(?P<version>\d+\.\d+\.\d+)\Z")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
GO_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+\Z")
UPSTREAM_GO_VERSION_PATTERN = re.compile(
    r"^[ \t]*go-version:[ \t]*['\"]?(?P<version>\d+\.\d+\.\d+)['\"]?"
    r"[ \t]*(?:#.*)?$",
    re.MULTILINE,
)


class SyncError(RuntimeError):
    """A safe, user-facing synchronization failure."""


@dataclass(frozen=True)
class UpstreamCheckout:
    repository: pathlib.Path
    treeish: str
    commit: str


def run(
    command: Sequence[str],
    *,
    cwd: pathlib.Path = ROOT,
    input_text: str | None = None,
    text: bool = True,
) -> str | bytes:
    result = subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=text,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() if text else result.stderr.decode().strip()

        raise SyncError(f"Command failed: {' '.join(command)}\n{stderr}")

    return result.stdout


def parse_version(tag: str) -> tuple[int, int, int]:
    match = VERSION_PATTERN.fullmatch(tag)

    if match is None:
        raise SyncError(f"Expected a stable vX.Y.Z tag, got {tag!r}")

    return tuple(int(part) for part in match.group("version").split("."))


def package_version(tag: str) -> str:
    parse_version(tag)

    return tag[1:]


def current_tag() -> str:
    try:
        tag = VERSION_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError as error:
        raise SyncError(f"Missing version file: {VERSION_FILE}") from error

    parse_version(tag)

    return tag


def current_commit() -> str:
    try:
        commit = COMMIT_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError as error:
        raise SyncError(f"Missing commit file: {COMMIT_FILE}") from error

    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise SyncError(f"Invalid upstream commit: {commit!r}")

    return commit


def parse_go_version(version: str) -> tuple[int, int, int]:
    if GO_VERSION_PATTERN.fullmatch(version) is None:
        raise SyncError(f"Expected an exact X.Y.Z Go version, got {version!r}")

    return tuple(int(part) for part in version.split("."))


def current_go_version() -> str:
    try:
        version = GO_VERSION_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError as error:
        raise SyncError(f"Missing Go toolchain file: {GO_VERSION_FILE}") from error

    parse_go_version(version)

    return version


def request_json(url: str, *, missing_ok: bool = False) -> Any | None:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "sing-box-python-upstream-sync",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if missing_ok and error.code == 404:
            return None

        raise SyncError(f"HTTP {error.code} while requesting {url}") from error
    except urllib.error.URLError as error:
        raise SyncError(f"Unable to request {url}: {error.reason}") from error


def stable_release(requested_tag: str | None) -> dict[str, Any]:
    if requested_tag is None:
        url = f"https://api.github.com/repos/{UPSTREAM_REPOSITORY}/releases/latest"
    else:
        parse_version(requested_tag)

        encoded_tag = urllib.parse.quote(requested_tag, safe="")
        url = (
            f"https://api.github.com/repos/{UPSTREAM_REPOSITORY}"
            f"/releases/tags/{encoded_tag}"
        )
    release = request_json(url)

    if not isinstance(release, dict):
        raise SyncError("GitHub returned an invalid upstream release response")

    tag = release.get("tag_name")

    if not isinstance(tag, str):
        raise SyncError("The upstream release does not contain a tag name")

    parse_version(tag)

    if release.get("draft") or release.get("prerelease"):
        raise SyncError(f"Refusing non-stable upstream release {tag}")

    if requested_tag is not None and tag != requested_tag:
        raise SyncError(f"Requested {requested_tag}, but GitHub returned {tag}")

    return release


def github_resource_exists(endpoint: str) -> bool:
    url = f"https://api.github.com/repos/{PROJECT_REPOSITORY}/{endpoint}"

    return request_json(url, missing_ok=True) is not None


def pypi_version_exists(version: str) -> bool:
    encoded_version = urllib.parse.quote(version, safe="")
    url = f"https://pypi.org/pypi/{PYPI_PROJECT}/{encoded_version}/json"

    return request_json(url, missing_ok=True) is not None


def published_state(tag: str) -> dict[str, bool]:
    encoded_tag = urllib.parse.quote(tag, safe="")

    return {
        "tag": github_resource_exists(f"git/ref/tags/{encoded_tag}"),
        "release": github_resource_exists(f"releases/tags/{encoded_tag}"),
        "pypi": pypi_version_exists(package_version(tag)),
    }


def write_github_output(path: pathlib.Path | None, values: dict[str, str]) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8", newline="\n") as output:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise SyncError(f"GitHub output {key!r} contains a newline")

            output.write(f"{key}={value}\n")


def check_release(args: argparse.Namespace) -> None:
    current = current_tag()
    release = stable_release(args.tag)
    tag = release["tag_name"]
    current_version = parse_version(current)
    target_version = parse_version(tag)

    if target_version < current_version:
        raise SyncError(f"Upstream target {tag} is older than current {current}")

    update_required = target_version > current_version
    state = published_state(tag)

    if update_required and any(state.values()):
        occupied = ", ".join(name for name, exists in state.items() if exists)

        raise SyncError(f"Release target {tag} is already occupied by: {occupied}")

    if not update_required and any(state.values()) and not all(state.values()):
        present = ", ".join(name for name, exists in state.items() if exists)
        missing = ", ".join(name for name, exists in state.items() if not exists)

        raise SyncError(
            f"Release {tag} is inconsistent; present: {present}; missing: {missing}"
        )

    release_required = update_required or not all(state.values())
    values = {
        "current_tag": current,
        "tag": tag,
        "version": package_version(tag),
        "release_required": str(release_required).lower(),
        "update_required": str(update_required).lower(),
    }

    write_github_output(args.github_output, values)
    print(json.dumps(values, indent=2, sort_keys=True))


@contextlib.contextmanager
def upstream_checkout(
    tag: str, supplied_repository: pathlib.Path | None = None
) -> Iterator[UpstreamCheckout]:
    parse_version(tag)

    if supplied_repository is not None:
        repository = supplied_repository.resolve()
        commit = str(
            run(["git", "rev-parse", f"{tag}^{{commit}}"], cwd=repository)
        ).strip()

        yield UpstreamCheckout(repository, tag, commit)

        return

    (ROOT / "build").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="sing-box-upstream-", dir=ROOT / "build"
    ) as raw:
        repository = pathlib.Path(raw)
        run(["git", "init", "--quiet"], cwd=repository)
        run(["git", "remote", "add", "origin", UPSTREAM_URL], cwd=repository)
        run(
            ["git", "fetch", "--quiet", "--depth=1", "origin", f"refs/tags/{tag}"],
            cwd=repository,
        )
        commit = str(
            run(["git", "rev-parse", "FETCH_HEAD^{commit}"], cwd=repository)
        ).strip()

        yield UpstreamCheckout(repository, "FETCH_HEAD", commit)


def upstream_tree(
    checkout: UpstreamCheckout,
) -> tuple[dict[str, tuple[str, str]], set[str]]:
    output = run(
        ["git", "ls-tree", "-rz", checkout.treeish],
        cwd=checkout.repository,
        text=False,
    )

    assert isinstance(output, bytes)

    blobs: dict[str, tuple[str, str]] = {}
    gitlinks: set[str] = set()

    for raw_entry in output.split(b"\0"):
        if not raw_entry:
            continue

        metadata, raw_path = raw_entry.split(b"\t", 1)
        mode, object_type, object_hash = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8")

        if object_type == "blob":
            blobs[path] = (mode, object_hash)
        elif mode == "160000" and object_type == "commit":
            gitlinks.add(path)
        else:
            raise SyncError(
                f"Unsupported upstream tree entry {mode} {object_type} at {path}"
            )

    return blobs, gitlinks


def upstream_file(checkout: UpstreamCheckout, path: str) -> str:
    output = run(
        ["git", "show", f"{checkout.treeish}:{path}"],
        cwd=checkout.repository,
    )

    assert isinstance(output, str)

    return output


def go_version_from_workflow(workflow: str) -> str:
    versions = {
        match.group("version")
        for match in UPSTREAM_GO_VERSION_PATTERN.finditer(workflow)
    }

    if not versions:
        raise SyncError(
            "Upstream build workflow contains no exact go-version toolchain pin"
        )
    if len(versions) != 1:
        raise SyncError(
            "Upstream build workflow contains ambiguous exact Go toolchain pins: "
            + ", ".join(sorted(versions, key=parse_go_version))
        )

    version = versions.pop()
    parse_go_version(version)

    return version


def official_go_version(checkout: UpstreamCheckout) -> str:
    workflow = upstream_file(checkout, ".github/workflows/build.yml")

    return go_version_from_workflow(workflow)


def verify_go_version(checkout: UpstreamCheckout) -> None:
    expected = official_go_version(checkout)
    actual = current_go_version()

    if actual != expected:
        raise SyncError(
            f"Project Go toolchain is {actual}, but upstream {checkout.treeish} "
            f"builds with {expected}"
        )

    print(f"Verified Go {actual} against the upstream build workflow")


def vendor_files() -> list[str]:
    return sorted(
        path.relative_to(VENDOR_DIR).as_posix()
        for path in VENDOR_DIR.rglob("*")
        if path.is_file() or path.is_symlink()
    )


def working_blob_hashes(paths: Sequence[str]) -> dict[str, str]:
    repository_paths = [f"singbox-go/{path}" for path in paths]
    output = str(
        run(
            ["git", "hash-object", "--stdin-paths"],
            input_text="\n".join(repository_paths) + "\n",
        )
    ).splitlines()

    if len(output) != len(paths):
        raise SyncError("git hash-object returned an unexpected number of hashes")

    return dict(zip(paths, output))


def verify_file_modes(
    checkout: UpstreamCheckout, expected: dict[str, tuple[str, str]]
) -> None:
    if checkout.commit == LEGACY_NORMALIZED_MODE_COMMIT:
        upstream_executables = {
            path for path, (mode, _) in expected.items() if mode == "100755"
        }

        if upstream_executables != LEGACY_NORMALIZED_EXECUTABLES:
            raise SyncError("The audited legacy executable-mode exception changed")
    if os.name == "nt":
        print("Skipping executable-mode verification on Windows")

        return

    changed = []

    for path, (expected_mode, _) in expected.items():
        source = VENDOR_DIR / path

        if source.is_symlink():
            actual_mode = "120000"
        elif source.stat().st_mode & stat.S_IXUSR:
            actual_mode = "100755"
        else:
            actual_mode = "100644"

        legacy_normalization = (
            checkout.commit == LEGACY_NORMALIZED_MODE_COMMIT
            and path in LEGACY_NORMALIZED_EXECUTABLES
            and expected_mode == "100755"
            and actual_mode == "100644"
        )

        if actual_mode != expected_mode and not legacy_normalization:
            changed.append(f"{path} ({actual_mode}, expected {expected_mode})")

    if changed:
        raise SyncError("Modified upstream file modes: " + ", ".join(changed))


def project_addition_gitlink_collisions(gitlinks: set[str]) -> list[str]:
    return sorted(
        addition
        for addition in PROJECT_ADDITIONS
        if any(
            addition == gitlink
            or addition.startswith(f"{gitlink}/")
            or gitlink.startswith(f"{addition}/")
            for gitlink in gitlinks
        )
    )


def verify_vendor(checkout: UpstreamCheckout) -> None:
    expected, gitlinks = upstream_tree(checkout)
    gitlink_collisions = project_addition_gitlink_collisions(gitlinks)

    if gitlink_collisions:
        raise SyncError(
            "Upstream gitlinks overlap project addition paths: "
            + ", ".join(gitlink_collisions)
        )

    collisions = PROJECT_ADDITIONS.intersection(expected)

    if collisions:
        raise SyncError(
            "Upstream now owns project addition paths: " + ", ".join(sorted(collisions))
        )

    actual_paths = set(vendor_files())
    expected_paths = set(expected).union(PROJECT_ADDITIONS)
    missing = sorted(expected_paths - actual_paths)
    unexpected = sorted(actual_paths - expected_paths)

    if missing or unexpected:
        details = []

        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        raise SyncError("Vendor path mismatch; " + "; ".join(details))

    upstream_paths = sorted(expected)
    actual_hashes = working_blob_hashes(upstream_paths)
    changed = [
        path for path in upstream_paths if actual_hashes[path] != expected[path][1]
    ]

    if changed:
        raise SyncError("Modified upstream files: " + ", ".join(changed))

    verify_file_modes(checkout, expected)

    print(
        f"Verified {len(upstream_paths)} upstream files against {checkout.commit}; "
        f"allowed additions: {', '.join(sorted(PROJECT_ADDITIONS))}"
    )
    if gitlinks:
        print("Omitted upstream gitlinks: " + ", ".join(sorted(gitlinks)))


def verify_command(args: argparse.Namespace) -> None:
    with upstream_checkout(args.tag, args.upstream_dir) as checkout:
        if args.tag == current_tag() and checkout.commit != current_commit():
            raise SyncError(
                f"Upstream tag {args.tag} resolved to {checkout.commit}, "
                f"not pinned commit {current_commit()}"
            )

        verify_vendor(checkout)
        verify_go_version(checkout)


def safe_extract(archive: pathlib.Path, destination: pathlib.Path) -> None:
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            target = (destination / member.name).resolve()

            if (
                destination.resolve() not in target.parents
                and target != destination.resolve()
            ):
                raise SyncError(f"Unsafe path in upstream archive: {member.name}")

        if hasattr(tarfile, "data_filter"):
            source.extractall(destination, filter="data")
        else:
            source.extractall(destination)


def export_upstream(checkout: UpstreamCheckout, destination: pathlib.Path) -> None:
    archive = destination.parent / "upstream.tar"

    run(
        [
            "git",
            "archive",
            "--format=tar",
            f"--output={archive}",
            checkout.treeish,
        ],
        cwd=checkout.repository,
    )

    destination.mkdir()
    safe_extract(archive, destination)
    archive.unlink()


def ensure_clean_worktree() -> None:
    status = str(run(["git", "status", "--porcelain", "--untracked-files=all"])).strip()

    if status:
        raise SyncError("Synchronization requires a clean Git worktree")


def sync_command(args: argparse.Namespace) -> None:
    ensure_clean_worktree()

    current = current_tag()
    current_go = current_go_version()
    current_version = parse_version(current)
    target_version = parse_version(args.tag)

    if target_version < current_version:
        raise SyncError(f"Refusing to downgrade from {current} to {args.tag}")
    with upstream_checkout(current, args.current_upstream_dir) as current_checkout:
        if current_checkout.commit != current_commit():
            raise SyncError(
                f"Upstream tag {current} resolved to {current_checkout.commit}, "
                f"not pinned commit {current_commit()}"
            )

        verify_vendor(current_checkout)
        verify_go_version(current_checkout)
    if target_version == current_version:
        print(f"{current} is already synchronized")

        return

    (ROOT / "build").mkdir(exist_ok=True)

    with upstream_checkout(args.tag, args.upstream_dir) as target_checkout:
        target_go = official_go_version(target_checkout)

        with tempfile.TemporaryDirectory(
            prefix="sing-box-import-", dir=ROOT / "build"
        ) as raw:
            temporary = pathlib.Path(raw)
            staged_vendor = temporary / "singbox-go"

            export_upstream(target_checkout, staged_vendor)

            for relative in PROJECT_ADDITIONS:
                source = VENDOR_DIR / relative
                destination = staged_vendor / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

            backup = temporary / "previous-singbox-go"
            VENDOR_DIR.rename(backup)

            try:
                staged_vendor.rename(VENDOR_DIR)
                VERSION_FILE.write_text(
                    f"{args.tag}\n", encoding="utf-8", newline="\n"
                )
                COMMIT_FILE.write_text(
                    f"{target_checkout.commit}\n", encoding="utf-8", newline="\n"
                )
                GO_VERSION_FILE.write_text(
                    f"{target_go}\n", encoding="utf-8", newline="\n"
                )
                verify_vendor(target_checkout)
                verify_go_version(target_checkout)
            except BaseException:
                if VENDOR_DIR.exists():
                    shutil.rmtree(VENDOR_DIR)

                backup.rename(VENDOR_DIR)
                VERSION_FILE.write_text(
                    f"{current}\n", encoding="utf-8", newline="\n"
                )
                COMMIT_FILE.write_text(
                    f"{current_checkout.commit}\n", encoding="utf-8", newline="\n"
                )
                GO_VERSION_FILE.write_text(
                    f"{current_go}\n", encoding="utf-8", newline="\n"
                )

                raise

    print(f"Synchronized sing-box {args.tag} ({target_checkout.commit})")


def guard_release(args: argparse.Namespace) -> None:
    current_go_version()

    if current_tag() != args.tag:
        raise SyncError(
            f"Release tag {args.tag} does not match {VERSION_FILE.name} "
            f"({current_tag()})"
        )

    encoded_tag = urllib.parse.quote(args.tag, safe="")
    tag_exists = github_resource_exists(f"git/ref/tags/{encoded_tag}")

    if tag_exists and not args.allow_existing_tag:
        raise SyncError(f"Project tag {args.tag} already exists unexpectedly")
    if not tag_exists and args.allow_existing_tag:
        raise SyncError(f"Expected project tag {args.tag} does not exist")
    if github_resource_exists(f"releases/tags/{encoded_tag}"):
        raise SyncError(
            f"Project GitHub Release {args.tag} already exists unexpectedly"
        )

    version = package_version(args.tag)

    if pypi_version_exists(version):
        raise SyncError(f"PyPI version {version} already exists unexpectedly")

    print(f"Release target {args.tag} is available")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="check for a new stable release")
    check.add_argument("--tag", help="check one explicit stable release tag")
    check.add_argument("--github-output", type=pathlib.Path)
    check.set_defaults(handler=check_release)

    verify = commands.add_parser("verify", help="verify the current vendor tree")
    verify.add_argument("--tag", required=True)
    verify.add_argument("--upstream-dir", type=pathlib.Path)
    verify.set_defaults(handler=verify_command)

    sync = commands.add_parser("sync", help="synchronize an exact upstream tag")
    sync.add_argument("--tag", required=True)
    sync.add_argument("--upstream-dir", type=pathlib.Path)
    sync.add_argument("--current-upstream-dir", type=pathlib.Path)
    sync.set_defaults(handler=sync_command)

    guard = commands.add_parser(
        "guard-release", help="fail if a release target is inconsistent or occupied"
    )
    guard.add_argument("--tag", required=True)
    guard.add_argument("--allow-existing-tag", action="store_true")
    guard.set_defaults(handler=guard_release)

    return result


def main() -> int:
    args = parser().parse_args()

    try:
        args.handler(args)
    except SyncError as error:
        print(f"error: {error}", file=sys.stderr)

        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
