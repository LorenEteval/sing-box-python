# sing-box-python

[![Build and publish Python distributions](https://github.com/LorenEteval/sing-box-python/actions/workflows/build-and-publish.yml/badge.svg?branch=main)](https://github.com/LorenEteval/sing-box-python/actions/workflows/build-and-publish.yml)

Python bindings for [sing-box](https://github.com/SagerNet/sing-box).

## Install

Prebuilt binary wheels include the native Go and C++ binding, so a supported installation does not require Go, CMake,
or a C++ compiler.

```
pip install sing-box-python
```

Binary wheels are published for Linux x86-64 and ARM64, Windows x86-64 and ARM64, and macOS Intel and Apple Silicon.

### Build from Source

A source distribution is also published as a fallback. If pip cannot find a compatible wheel, it may build the native
binding from source. A source build requires:

* The [Go toolchain selected by `singbox-go/go.mod`](https://go.dev/doc/install) in `PATH` (`toolchain` when
  present, otherwise the `go` directive).
* A working C and C++ compiler toolchain.
* MinGW-w64 on Windows x86-64, or LLVM-MinGW on Windows ARM64, with `gcc` and `g++` available in `PATH`.

The isolated Python build environment installs CMake, pybind11, setuptools, and wheel automatically. To build directly
from a repository checkout:

```
pip install .
```

The build downloads sing-box's checksum-pinned, platform-specific Cronet Go module automatically. Wheels contain an
isolated Cronet runtime (`libcronet.dylib`, `libcronet.so`, or `libcronet.dll`), loaded from an absolute package path
during import. On macOS the build converts the pinned static archive into a separate dynamic library so Chromium's
private C++ runtime cannot conflict with pybind11.

## API

```pycon
>>> import singbox
>>> help(singbox)
Help on package singbox:

NAME
    singbox - Native Python bindings for starting and managing sing-box.

PACKAGE CONTENTS
    _native

CLASSES
    builtins.RuntimeError(builtins.Exception)
        singbox._native.SingBoxError
    pybind11_builtins.pybind11_object(builtins.object)
        singbox._native.SingBox

    class SingBox(pybind11_builtins.pybind11_object)
     |  An explicitly managed, non-blocking in-process sing-box instance.
     |
     |  Unlike the module-level startFromJSON function, this class reports startup
     |  errors as Python exceptions and does not wait for operating-system signals.
     |  Call stop explicitly or use the instance as a context manager.
     |
     |  Methods defined here:
     |
     |  __enter__(...)
     |      __enter__(self: singbox._native.SingBox) -> singbox._native.SingBox
     |
     |      Return this instance for use as a context manager.
     |
     |  __exit__(...)
     |      __exit__(self: singbox._native.SingBox, arg0: object, arg1: object, arg2: object) -> None
     |
     |      Stop the instance when leaving a context-manager block.
     |
     |  __init__(...)
     |      __init__(self: singbox._native.SingBox) -> None
     |
     |      Create a stopped sing-box instance.
     |
     |  queryStats(...)
     |      queryStats(self: singbox._native.SingBox, patterns: collections.abc.Sequence[str] = [], reset: bool = False, regexp: bool = False) -> dict
     |
     |      Return available runtime, Clash, and V2Ray statistics as a dictionary.
     |
     |      patterns filters V2Ray counters. reset clears matched V2Ray counters after
     |      reading, and regexp interprets patterns as regular expressions. Clash and
     |      V2Ray sections are None when their corresponding sing-box services are not
     |      enabled by the configuration. Runtime memory counters are always present.
     |
     |  startFromJSON(...)
     |      startFromJSON(self: singbox._native.SingBox, json: str) -> None
     |
     |      Start this instance from a UTF-8 JSON configuration string.
     |
     |      The call returns after the service starts. Invalid configuration, construction,
     |      or startup errors raise SingBoxError. Starting an already-running instance
     |      raises RuntimeError.
     |
     |  stop(...)
     |      stop(self: singbox._native.SingBox) -> None
     |
     |      Stop the instance and release its native resources; repeated calls are safe.
     |
     |  Readonly properties defined here:
     |
     |  handle
     |      Opaque numeric instance identifier, or 0 while stopped.
     |
     |  running
     |      Whether this instance currently owns a running native service.

    class SingBoxError(builtins.RuntimeError)
     |  An error reported by the managed sing-box native lifecycle API.

FUNCTIONS
    startFromJSON(...) method of pybind11_builtins.pybind11_detail_function_record instance
        startFromJSON(json: str) -> None

        Start sing-box from JSON and block until SIGINT or SIGTERM is received.

        This process-oriented entry point is intended to be the target of a
        multiprocessing.Process. Configuration decoding or construction failures exit
        the process with status 23. Service startup failures call os.Exit(-1), whose
        observed status is platform-dependent. Use SingBox for exception-based,
        non-blocking in-process lifecycle management.

DATA
    __all__ = ['SingBox', 'SingBoxError', '__version__', 'startFromJSON']

VERSION
    vX.Y.Z
```

## Vendored Upstream Source

This repository and its PyPI source distribution contain a vendored copy of
[sing-box](https://github.com/SagerNet/sing-box). Vendoring keeps source installs independent of Git submodule support.
All regular files under `singbox-go` come directly from the upstream release except these explicit additions:

* `singbox-go/binding/main.go`
* `singbox-go/binding/cronet_purego.go`

Upstream's `clients/android` and `clients/apple` Git submodule links are intentionally not vendored because they are not
used to build the Python package. The initial Windows import normalized executable bits on upstream shell scripts; file
paths and blob contents remain identical to upstream. Synchronization runs on Linux and preserves upstream modes.

### Upstream Release Synchronization

`.github/workflows/sync-upstream.yml` checks the latest stable upstream release daily and can also be started manually
from the Actions page. Drafts and prereleases are rejected. `UPSTREAM_VERSION` is the single version source for both the
Python distribution version and the sing-box version embedded in the native library, while `UPSTREAM_COMMIT` pins the
commit to which that release tag resolved during import.

The synchronization script checks the current vendor tree against its exact upstream tag, fetches the requested tag,
replaces only upstream-owned files, restores the two binding additions, updates `UPSTREAM_VERSION`, and verifies every
vendored upstream path and Git blob. Repeated checks are no-ops when the stable release is already synchronized.

Run the same operations locally from the repository root:

```shell
python scripts/sync-sing-box.py check
python scripts/sync-sing-box.py verify --tag v1.13.18
python scripts/sync-sing-box.py sync --tag vX.Y.Z
```

For an automated update, the sync workflow commits `chore: sync sing-box vX.Y.Z` to `main`, then explicitly dispatches
the existing build-and-publish workflow at that commit. The latter builds and tests every distribution before creating
the `vX.Y.Z` tag, publishing `X.Y.Z` to PyPI through Trusted Publishing, and creating the GitHub Release. Explicit
dispatch is required because pushes made with GitHub's `GITHUB_TOKEN` do not trigger new push workflows.

## Binary Wheel Platforms

The distributions are built and tested in [GitHub Actions](https://github.com/LorenEteval/sing-box-python/actions).

| Platform | Architecture | CPython |
|----------|--------------|---------|
| Linux | x86-64 | 3.8-3.14, 3.13t, 3.14t |
| Linux | ARM64 | 3.8-3.14, 3.13t, 3.14t |
| Windows | x86-64 | 3.8-3.14, 3.13t, 3.14t |
| Windows | ARM64 | 3.9-3.14, 3.13t, 3.14t |
| macOS | Intel | 3.8-3.14, 3.13t, 3.14t |
| macOS | Apple Silicon | 3.8-3.14, 3.13t, 3.14t |

Naive outbound is included on all wheel platforms in the table. Cronet is bundled as an isolated shared runtime on
macOS, Linux, and Windows. The macOS runtime is produced from the pinned static archive during the wheel build. The
Cronet artifact comes from the platform module and exact version already pinned by the vendored sing-box `go.mod` and
verified by `go.sum`; the workflow does not build an unrelated Cronet revision.
Custom source builds can override the complete sing-box tag list with the `SINGBOX_BUILD_TAGS` environment variable.

## License

The license for this project follows its original Go repository [sing-box](https://github.com/SagerNet/sing-box)
and is under [GNU GPL v3 or later](https://github.com/LorenEteval/sing-box-python/blob/main/LICENSE).
