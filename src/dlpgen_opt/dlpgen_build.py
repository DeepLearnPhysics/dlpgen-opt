from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .provenance import git_commit, read_yaml, write_yaml


IGNORED_PARTS = {".git", ".pytest_cache", "__pycache__", "build"}
IGNORED_NAMES = {".depend", ".DS_Store"}


@dataclass(frozen=True)
class CheckoutSnapshot:
    path: Path
    commit: str | None
    fingerprint: str
    dirty: bool | None
    files: tuple[str, ...]

    def metadata(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "commit": self.commit,
            "fingerprint": self.fingerprint,
            "dirty": self.dirty,
        }


@dataclass(frozen=True)
class DLPGeneratorRuntime:
    executable: Path
    environment: dict[str, str]
    build_manifest: Path


def _git_file_list(path: Path) -> tuple[str, ...] | None:
    try:
        output = subprocess.check_output(
            [
                "git",
                "-C",
                str(path),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    files = tuple(
        sorted(
            os.fsdecode(value)
            for value in output.split(b"\0")
            if value and not Path(os.fsdecode(value)).is_absolute()
        )
    )
    return files


def _filesystem_file_list(path: Path) -> tuple[str, ...]:
    files = []
    for candidate in path.rglob("*"):
        relative = candidate.relative_to(path)
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        if candidate.name in IGNORED_NAMES:
            continue
        if candidate.is_file() or candidate.is_symlink():
            files.append(relative.as_posix())
    return tuple(sorted(files))


def _fingerprint(path: Path, files: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative_name in files:
        relative = Path(relative_name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid path in DLPGenerator checkout: {}".format(relative))
        candidate = path / relative
        digest.update(relative.as_posix().encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        if not candidate.exists() and not candidate.is_symlink():
            digest.update(b"deleted\0")
            continue
        mode = candidate.lstat().st_mode
        digest.update(b"x" if mode & stat.S_IXUSR else b"-")
        if candidate.is_symlink():
            digest.update(b"link\0")
            digest.update(os.fsencode(os.readlink(candidate)))
        else:
            digest.update(b"file\0")
            with candidate.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


def _git_dirty(path: Path) -> bool | None:
    try:
        output = subprocess.check_output(
            ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=normal"],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return bool(output.strip())


def inspect_checkout(path: Path) -> CheckoutSnapshot:
    checkout = path.expanduser().resolve()
    if not checkout.is_dir():
        raise ValueError("DLPGenerator checkout does not exist: {}".format(checkout))
    required = (
        checkout / "GNUmakefile",
        checkout / "bin" / "dlpgen",
        checkout / "python" / "dlp_generator",
    )
    missing = [str(value) for value in required if not value.exists()]
    if missing:
        raise ValueError(
            "invalid DLPGenerator checkout; missing: {}".format(", ".join(missing))
        )
    files = _git_file_list(checkout) or _filesystem_file_list(checkout)
    if not files:
        raise ValueError("DLPGenerator checkout contains no source files: {}".format(checkout))
    return CheckoutSnapshot(
        path=checkout,
        commit=git_commit(checkout),
        fingerprint=_fingerprint(checkout, files),
        dirty=_git_dirty(checkout),
        files=files,
    )


def _copy_snapshot(snapshot: CheckoutSnapshot, destination: Path) -> None:
    for relative_name in snapshot.files:
        source = snapshot.path / relative_name
        if not source.exists() and not source.is_symlink():
            continue
        target = destination / relative_name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        else:
            shutil.copy2(source, target)


def _prepend(path: Path, current: str | None) -> str:
    return str(path) + (os.pathsep + current if current else "")


def _runtime(cache: Path) -> DLPGeneratorRuntime:
    build = cache / "build"
    library = build / "lib"
    include = build / "include"
    executable = cache / "bin" / "dlpgen"
    environment = {
        "DLPGENERATOR_DIR": str(cache),
        "DLPGENERATOR_BINDIR": str(cache / "bin"),
        "DLPGENERATOR_BUILDDIR": str(build),
        "DLPGENERATOR_LIBDIR": str(library),
        "DLPGENERATOR_INCDIR": str(include),
        "DLPGENERATOR_CXX": "g++",
        "DLPGENERATOR_CXXSTDFLAG": "-std=c++17",
        "DLPGENERATOR_NUMPY": "1",
        "PATH": _prepend(cache / "bin", os.environ.get("PATH")),
        "PYTHONPATH": _prepend(cache / "python", os.environ.get("PYTHONPATH")),
        "LD_LIBRARY_PATH": _prepend(library, os.environ.get("LD_LIBRARY_PATH")),
        "ROOT_INCLUDE_PATH": _prepend(
            include / "DLPGenerator" / "ParticleBomb",
            os.environ.get("ROOT_INCLUDE_PATH"),
        ),
    }
    return DLPGeneratorRuntime(
        executable=executable,
        environment=environment,
        build_manifest=cache / "build.yaml",
    )


def build_cache_path(production_dir: Path, snapshot: CheckoutSnapshot) -> Path:
    return production_dir / ".dlpgen-cache" / snapshot.fingerprint


def _run_build(command: list[str], log, environment: dict[str, str]):
    return subprocess.run(
        command,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        env={**os.environ, **environment},
    )


def ensure_custom_build(
    production_dir: Path, snapshot: CheckoutSnapshot
) -> DLPGeneratorRuntime:
    cache_root = production_dir / ".dlpgen-cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    cache = build_cache_path(production_dir, snapshot)
    runtime = _runtime(cache)
    lock_path = cache_root / "{}.lock".format(snapshot.fingerprint)
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        ready = read_yaml(runtime.build_manifest) if runtime.build_manifest.exists() else {}
        if (
            ready.get("fingerprint") == snapshot.fingerprint
            and runtime.executable.is_file()
            and (cache / "build" / "lib" / "libLiteFMWK_ParticleBomb.so").is_file()
        ):
            return runtime

        current = inspect_checkout(snapshot.path)
        if current.fingerprint != snapshot.fingerprint:
            raise RuntimeError(
                "DLPGenerator checkout changed after production initialization; "
                "use a new production directory"
            )
        if cache.exists():
            shutil.rmtree(cache)
        cache.mkdir(parents=True)
        _copy_snapshot(snapshot, cache)
        if _fingerprint(cache, snapshot.files) != snapshot.fingerprint:
            shutil.rmtree(cache)
            raise RuntimeError("DLPGenerator checkout changed while it was being copied")

        cpus = max(1, int(os.environ.get("SLURM_CPUS_PER_TASK", "1")))
        command = ["make", "-C", str(cache), "-j{}".format(cpus)]
        log_path = cache / "build.log"
        with log_path.open("w", encoding="utf-8") as log:
            completed = _run_build(command, log, runtime.environment)
        if completed.returncode:
            raise RuntimeError(
                "custom DLPGenerator build failed with exit code {}; see {}".format(
                    completed.returncode, log_path
                )
            )
        library = cache / "build" / "lib" / "libLiteFMWK_ParticleBomb.so"
        if not runtime.executable.is_file() or not library.is_file():
            raise RuntimeError(
                "custom DLPGenerator build did not produce {} and {}".format(
                    runtime.executable, library
                )
            )
        runtime.executable.chmod(runtime.executable.stat().st_mode | stat.S_IXUSR)
        write_yaml(
            runtime.build_manifest,
            {
                **snapshot.metadata(),
                "cache": str(cache),
                "command": command,
                "log": str(log_path),
            },
        )
        return runtime
