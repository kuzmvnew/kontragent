#!/usr/bin/env python3
"""Build and verify the immutable NEXT Company runtime artifact.

The artifact contains the exact Git tree and its frozen runtime environment.
Staging and production consume this archive by SHA-256; neither environment is
allowed to resolve dependencies again.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "release-manifest.json"
CONTRACT_VERSION = 2
LEGACY_CONTRACT_VERSION = 1
BUILDER_VERSION = "release-artifact-v2"
EXPECTED_PYTHON_MINOR = "3.14"
EXPECTED_POSTGRESQL_VERSION = "18.6"


class ArtifactError(RuntimeError):
    """The candidate is not a valid immutable runtime artifact."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_sha256(
    root: Path,
    *,
    excluded: set[str] | None = None,
    excluded_prefixes: tuple[str, ...] = (),
) -> str:
    """Hash path, type, executable mode and content in a stable order."""
    excluded = excluded or set()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative in excluded or any(
            relative == prefix or relative.startswith(prefix + "/")
            for prefix in excluded_prefixes
        ):
            continue
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            kind = "symlink"
            content = os.readlink(path).encode("utf-8")
        elif stat.S_ISDIR(metadata.st_mode):
            kind = "directory"
            content = b""
        elif stat.S_ISREG(metadata.st_mode):
            kind = "file"
            content = path.read_bytes()
        else:
            raise ArtifactError(f"unsupported artifact entry: {relative}")
        header = canonical_json(
            {
                "path": relative,
                "kind": kind,
                "mode": stat.S_IMODE(metadata.st_mode),
                "size": len(content),
            }
        )
        digest.update(len(header).to_bytes(8, "big"))
        digest.update(header)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def systemd_hashes(root: Path) -> dict[str, str]:
    unit_root = root / "deploy" / "systemd"
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(unit_root.glob("*"))
        if path.is_file() and path.suffix in {".service", ".timer"}
    }


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    normalized_names: set[str] = set()
    symlink_names: set[str] = set()
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise ArtifactError(f"unsafe archive path: {member.name}")
        if not path.parts or path.parts[0] != "release":
            raise ArtifactError("artifact must contain one release/ root")
        normalized_name = path.as_posix()
        if normalized_name in normalized_names:
            raise ArtifactError(f"duplicate archive path: {member.name}")
        normalized_names.add(normalized_name)
        if member.isdev() or member.isfifo():
            raise ArtifactError(f"unsupported archive member: {member.name}")
        declared_mode = stat.S_IMODE(member.mode)
        if member.issym():
            # POSIX symlink permissions cannot be restored portably.  Canonical
            # artifacts always declare the platform value used by lstat().
            if declared_mode != 0o777:
                raise ArtifactError(
                    f"unsupported symlink mode {declared_mode:#o}: {member.name}"
                )
            symlink_names.add(normalized_name)
        elif member.islnk():
            # Canonical builds do not emit hard links.  Rejecting them avoids
            # aliasing one archive member's mode changes onto another.
            raise ArtifactError(f"unsupported archive hard link: {member.name}")
        elif member.isdir() or member.isreg():
            if declared_mode & ~0o755:
                raise ArtifactError(
                    f"unsafe archive mode {declared_mode:#o}: {member.name}"
                )
        else:
            raise ArtifactError(f"unsupported archive member: {member.name}")
        if member.issym():
            target = PurePosixPath(member.linkname)
            resolved = PurePosixPath(
                posixpath.normpath(str(path.parent / target))
            )
            if (
                target.is_absolute()
                or not resolved.parts
                or resolved.parts[0] != "release"
            ):
                raise ArtifactError(f"unsafe archive link: {member.name}")
    for name in normalized_names:
        path = PurePosixPath(name)
        if any(parent.as_posix() in symlink_names for parent in path.parents):
            raise ArtifactError(f"archive member descends through a symlink: {name}")
    return members


def _artifact_member_filter(
    member: tarfile.TarInfo,
    destination: str,
) -> tarfile.TarInfo:
    """Apply tarfile's data safety checks while retaining declared safe modes."""

    filtered = tarfile.data_filter(member, destination)
    if member.isdir() or member.isreg():
        return filtered.replace(mode=stat.S_IMODE(member.mode), deep=False)
    return filtered


def _restore_declared_modes(
    destination: Path,
    members: list[tarfile.TarInfo],
) -> None:
    """Restore safe artifact modes after extraction, independent of umask."""

    destination_real = destination.resolve()
    files = [member for member in members if member.isreg()]
    directories = sorted(
        (member for member in members if member.isdir()),
        key=lambda member: len(PurePosixPath(member.name).parts),
        reverse=True,
    )
    for member in [*files, *directories]:
        target = destination / PurePosixPath(member.name)
        parent_real = target.parent.resolve(strict=True)
        if os.path.commonpath((str(parent_real), str(destination_real))) != str(
            destination_real
        ):
            raise ArtifactError(f"unsafe extracted path: {member.name}")
        metadata = target.lstat()
        if member.isdir() and not stat.S_ISDIR(metadata.st_mode):
            raise ArtifactError(f"extracted entry type mismatch: {member.name}")
        if member.isreg() and not stat.S_ISREG(metadata.st_mode):
            raise ArtifactError(f"extracted entry type mismatch: {member.name}")
        os.chmod(target, stat.S_IMODE(member.mode), follow_symlinks=False)
    for member in (item for item in members if item.issym()):
        target = destination / PurePosixPath(member.name)
        metadata = target.lstat()
        if not stat.S_ISLNK(metadata.st_mode):
            raise ArtifactError(f"extracted entry type mismatch: {member.name}")
        if stat.S_IMODE(metadata.st_mode) != stat.S_IMODE(member.mode):
            try:
                os.chmod(
                    target,
                    stat.S_IMODE(member.mode),
                    follow_symlinks=False,
                )
            except (NotImplementedError, OSError) as error:
                raise ArtifactError(
                    f"cannot restore extracted symlink mode: {member.name}"
                ) from error
            if stat.S_IMODE(target.lstat().st_mode) != stat.S_IMODE(member.mode):
                raise ArtifactError(f"extracted symlink mode mismatch: {member.name}")


def extract_artifact(artifact: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(artifact, "r:gz") as archive:
        members = _safe_members(archive)
        try:
            archive.extractall(
                destination,
                members=members,
                filter=_artifact_member_filter,
            )
        except tarfile.TarError as error:
            raise ArtifactError(f"unsafe archive extraction: {error}") from error
    _restore_declared_modes(destination, members)
    release = destination / "release"
    if not release.is_dir():
        raise ArtifactError("release root is missing")
    return release


def _read_expected_checksum(artifact: Path) -> str:
    checksum_path = artifact.with_name(artifact.name + ".sha256")
    if not checksum_path.is_file():
        raise ArtifactError(f"checksum sidecar is missing: {checksum_path}")
    fields = checksum_path.read_text(encoding="utf-8").strip().split()
    if len(fields) != 2 or fields[1].lstrip("*") != artifact.name:
        raise ArtifactError("invalid checksum sidecar")
    expected = fields[0].lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise ArtifactError("invalid artifact SHA-256")
    return expected


def verify_release_tree(release: Path) -> dict[str, Any]:
    manifest_path = release / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ArtifactError("release manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    required = {
        "contract_version",
        "source_git_sha",
        "source_tree_sha256",
        "runtime_tree_sha256",
        "lock_sha256",
        "python_version",
        "python_minor",
        "python_implementation",
        "runtime_platform",
        "runtime_machine",
        "postgresql_version",
        "systemd_units",
    }
    missing = sorted(required - set(manifest))
    if missing:
        raise ArtifactError(f"release manifest fields missing: {', '.join(missing)}")
    if manifest["contract_version"] not in {
        LEGACY_CONTRACT_VERSION,
        CONTRACT_VERSION,
    }:
        raise ArtifactError("unsupported release artifact contract")
    if manifest["contract_version"] == CONTRACT_VERSION:
        builder = manifest.get("builder")
        if not isinstance(builder, dict) or set(builder) != {
            "git_sha",
            "script_sha256",
            "version",
        }:
            raise ArtifactError("builder provenance is missing or invalid")
        if (
            not re.fullmatch(r"[0-9a-f]{40}", str(builder["git_sha"]))
            or not re.fullmatch(r"[0-9a-f]{64}", str(builder["script_sha256"]))
            or builder["version"] != BUILDER_VERSION
        ):
            raise ArtifactError("builder provenance is missing or invalid")
        build_kind = manifest.get("build_kind")
        expected_kind = (
            "source_builder_same_commit"
            if manifest["source_git_sha"] == builder["git_sha"]
            else "historical_reacceptance"
        )
        if build_kind != expected_kind:
            raise ArtifactError("builder/source provenance relationship is invalid")
    if manifest["python_minor"] != EXPECTED_PYTHON_MINOR:
        raise ArtifactError(
            f"Python divergence: expected {EXPECTED_PYTHON_MINOR}, "
            f"observed {manifest['python_minor']}"
        )
    if manifest["postgresql_version"] != EXPECTED_POSTGRESQL_VERSION:
        raise ArtifactError(
            f"PostgreSQL divergence: expected {EXPECTED_POSTGRESQL_VERSION}, "
            f"observed {manifest['postgresql_version']}"
        )
    if sha256_file(release / "uv.lock") != manifest["lock_sha256"]:
        raise ArtifactError("uv.lock hash does not match the release manifest")
    observed_source_tree = tree_sha256(
        release,
        excluded={MANIFEST_NAME},
        excluded_prefixes=(".runtime", ".venv"),
    )
    if observed_source_tree != manifest["source_tree_sha256"]:
        raise ArtifactError("source tree hash does not match the release manifest")
    observed_units = systemd_hashes(release)
    if observed_units != manifest["systemd_units"]:
        raise ArtifactError("canonical systemd definitions do not match the manifest")
    observed_tree = tree_sha256(release, excluded={MANIFEST_NAME})
    if observed_tree != manifest["runtime_tree_sha256"]:
        raise ArtifactError("runtime tree hash does not match the release manifest")
    python = release / ".venv" / "bin" / "python"
    if not python.is_file() and not python.is_symlink():
        raise ArtifactError("artifact runtime interpreter is missing")
    try:
        runtime_identity = json.loads(
            subprocess.check_output(
                [
                    str(python),
                    "-c",
                    (
                        "import json,platform;"
                        "print(json.dumps({'version':platform.python_version(),"
                        "'implementation':platform.python_implementation(),"
                        "'platform':platform.system().lower(),"
                        "'machine':platform.machine()}))"
                    ),
                ],
                text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ArtifactError("artifact runtime interpreter is not executable") from error
    if runtime_identity["version"] != manifest["python_version"]:
        raise ArtifactError(
            f"runtime Python mismatch: manifest={manifest['python_version']} "
            f"runtime={runtime_identity['version']}"
        )
    identity_pairs = {
        "python_implementation": "implementation",
        "runtime_platform": "platform",
        "runtime_machine": "machine",
    }
    for manifest_key, identity_key in identity_pairs.items():
        if runtime_identity[identity_key] != manifest[manifest_key]:
            raise ArtifactError(
                f"runtime identity mismatch for {manifest_key}: "
                f"manifest={manifest[manifest_key]} "
                f"runtime={runtime_identity[identity_key]}"
            )
    return manifest


def verify_artifact(artifact: Path, *, extract_to: Path | None = None) -> dict[str, Any]:
    artifact = artifact.resolve()
    observed_sha = sha256_file(artifact)
    expected_sha = _read_expected_checksum(artifact)
    if observed_sha != expected_sha:
        raise ArtifactError(
            f"artifact SHA-256 mismatch: expected {expected_sha}, observed {observed_sha}"
        )
    if extract_to is not None:
        release = extract_artifact(artifact, extract_to)
        manifest = verify_release_tree(release)
    else:
        with tempfile.TemporaryDirectory(prefix="nextcompany-artifact-") as directory:
            release = extract_artifact(artifact, Path(directory))
            manifest = verify_release_tree(release)
    return {
        "status": "PASS",
        "artifact": artifact.name,
        "artifact_sha256": observed_sha,
        "source_git_sha": manifest["source_git_sha"],
        "lock_sha256": manifest["lock_sha256"],
        "runtime_tree_sha256": manifest["runtime_tree_sha256"],
        "python_version": manifest["python_version"],
        "postgresql_version": manifest["postgresql_version"],
        "manifest": manifest,
    }


def _git(source: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(source), *arguments], text=True
    ).strip()


def _copy_git_tree(source: Path, release: Path) -> None:
    archive_path = release.parent / "source.tar"
    with archive_path.open("wb") as stream:
        subprocess.run(
            ["git", "-C", str(source), "archive", "--format=tar", "HEAD"],
            stdout=stream,
            check=True,
        )
    with tarfile.open(archive_path, "r:") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ArtifactError(f"unsafe Git archive entry: {member.name}")
        archive.extractall(release, filter="data")


def _remove_generated_bytecode(root: Path) -> None:
    """Remove path/time-bearing caches from the runtime copied into a release."""

    if not root.exists():
        return
    cache_directories = sorted(
        (
            path
            for path in root.rglob("__pycache__")
            if path.is_dir() and not path.is_symlink()
        ),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in cache_directories:
        shutil.rmtree(path)
    for suffix in ("*.pyc", "*.pyo"):
        for path in root.rglob(suffix):
            if path.is_file() and not path.is_symlink():
                path.unlink()


def _normalize_tree_modes(root: Path) -> None:
    """Apply deterministic safe modes while retaining executable semantics."""

    for path in [root, *sorted(root.rglob("*"))]:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            if stat.S_IMODE(metadata.st_mode) != 0o777:
                try:
                    os.chmod(path, 0o777, follow_symlinks=False)
                except (NotImplementedError, OSError) as error:
                    raise ArtifactError(
                        f"cannot canonicalize symlink mode: {path}"
                    ) from error
            continue
        if stat.S_ISDIR(metadata.st_mode):
            mode = 0o755
        elif stat.S_ISREG(metadata.st_mode):
            mode = 0o755 if stat.S_IMODE(metadata.st_mode) & 0o111 else 0o644
        else:
            raise ArtifactError(f"unsupported artifact entry: {path}")
        os.chmod(path, mode, follow_symlinks=False)


def _normalized_tar(source: Path, output: Path, epoch: int) -> None:
    temporary_tar = output.with_suffix("")
    with tarfile.open(temporary_tar, "w", dereference=False) as archive:
        for path in [source, *sorted(source.rglob("*"), key=lambda item: item.relative_to(source.parent).as_posix())]:
            name = path.relative_to(source.parent).as_posix()
            info = archive.gettarinfo(str(path), arcname=name)
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            info.mtime = epoch
            if info.isfile():
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
            else:
                archive.addfile(info)
    with temporary_tar.open("rb") as source_stream, output.open("wb") as output_stream:
        with gzip.GzipFile(fileobj=output_stream, mode="wb", filename="", mtime=0) as compressor:
            shutil.copyfileobj(source_stream, compressor)
    temporary_tar.unlink()


def _embed_runtime(release: Path, venv_python: Path) -> dict[str, str]:
    identity = json.loads(
        subprocess.check_output(
            [
                str(venv_python),
                "-c",
                (
                    "import json,platform,sys,sysconfig;"
                    "print(json.dumps({"
                    "'version':platform.python_version(),"
                    "'implementation':platform.python_implementation(),"
                    "'platform':platform.system().lower(),"
                    "'machine':platform.machine(),"
                    "'base_prefix':sys.base_prefix,"
                    "'base_executable':getattr(sys,'_base_executable',sys.executable),"
                    "'site_packages':sysconfig.get_path('purelib')"
                    "}))"
                ),
            ],
            text=True,
        )
    )
    base_prefix = Path(identity["base_prefix"]).resolve()
    base_executable = Path(identity["base_executable"]).resolve()
    site_packages = Path(identity["site_packages"]).resolve()
    try:
        executable_relative = base_executable.relative_to(base_prefix)
    except ValueError as error:
        raise ArtifactError("base Python executable is outside its runtime") from error
    runtime_python = release / ".runtime" / "python"
    shutil.copytree(base_prefix, runtime_python, symlinks=False)
    version_minor = ".".join(identity["version"].split(".")[:2])
    runtime_site_packages = (
        runtime_python / "lib" / f"python{version_minor}" / "site-packages"
    )
    runtime_site_packages.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        site_packages,
        runtime_site_packages,
        symlinks=False,
        dirs_exist_ok=True,
    )
    shutil.rmtree(release / ".venv")
    bin_dir = release / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    python_wrapper = bin_dir / "python"
    python_wrapper.write_text(
        "#!/bin/sh\n"
        'release_root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"\n'
        'export PYTHONHOME="$release_root/.runtime/python"\n'
        "export PYTHONDONTWRITEBYTECODE=1\n"
        f'exec "$release_root/.runtime/python/{executable_relative.as_posix()}" "$@"\n',
        encoding="utf-8",
    )
    python_wrapper.chmod(0o755)
    for alias in ("python3", f"python{version_minor}"):
        (bin_dir / alias).symlink_to("python")
    for module in ("alembic", "uvicorn"):
        wrapper = bin_dir / module
        wrapper.write_text(
            "#!/bin/sh\n"
            'exec "$(dirname -- "$0")/python" -m '
            + module
            + ' "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
    return {
        "python_version": identity["version"],
        "python_implementation": identity["implementation"],
        "runtime_platform": identity["platform"],
        "runtime_machine": identity["machine"],
    }


def build_artifact(
    source: Path,
    output_dir: Path,
    *,
    python_minor: str = EXPECTED_PYTHON_MINOR,
    postgresql_version: str = EXPECTED_POSTGRESQL_VERSION,
) -> dict[str, Any]:
    source = source.resolve()
    if _git(source, "status", "--porcelain"):
        raise ArtifactError("refusing to build from a dirty Git tree")
    if _git(ROOT, "status", "--porcelain"):
        raise ArtifactError("refusing to build with a dirty artifact builder")
    source_sha = _git(source, "rev-parse", "HEAD")
    builder_sha = _git(ROOT, "rev-parse", "HEAD")
    builder = {
        "git_sha": builder_sha,
        "script_sha256": sha256_file(Path(__file__).resolve()),
        "version": BUILDER_VERSION,
    }
    commit_epoch = int(_git(source, "show", "-s", "--format=%ct", "HEAD"))
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nextcompany-build-") as directory:
        work = Path(directory)
        release = work / "release"
        release.mkdir()
        _copy_git_tree(source, release)
        _normalize_tree_modes(release)
        source_tree_hash = tree_sha256(release)
        environment = {
            **os.environ,
            "UV_PROJECT_ENVIRONMENT": str(release / ".venv"),
            "SOURCE_DATE_EPOCH": str(commit_epoch),
        }
        subprocess.run(
            ["uv", "venv", "--python", python_minor, "--relocatable", str(release / ".venv")],
            env=environment,
            check=True,
        )
        subprocess.run(
            ["uv", "sync", "--project", str(release), "--frozen", "--no-dev", "--active"],
            env={**environment, "VIRTUAL_ENV": str(release / ".venv")},
            check=True,
        )
        python = release / ".venv" / "bin" / "python"
        runtime_identity = _embed_runtime(release, python)
        _remove_generated_bytecode(release / ".runtime")
        _remove_generated_bytecode(release / ".venv")
        _normalize_tree_modes(release)
        python_version = runtime_identity["python_version"]
        if ".".join(python_version.split(".")[:2]) != python_minor:
            raise ArtifactError(
                f"Python divergence: expected {python_minor}, observed {python_version}"
            )
        manifest = {
            "contract_version": CONTRACT_VERSION,
            "source_git_sha": source_sha,
            "builder": builder,
            "build_kind": (
                "source_builder_same_commit"
                if source_sha == builder_sha
                else "historical_reacceptance"
            ),
            "source_tree_sha256": source_tree_hash,
            "runtime_tree_sha256": tree_sha256(release),
            "lock_sha256": sha256_file(release / "uv.lock"),
            "python_version": python_version,
            "python_minor": python_minor,
            "python_implementation": runtime_identity["python_implementation"],
            "runtime_platform": runtime_identity["runtime_platform"],
            "runtime_machine": runtime_identity["runtime_machine"],
            "postgresql_version": postgresql_version,
            "systemd_units": systemd_hashes(release),
        }
        manifest_path = release / MANIFEST_NAME
        manifest_path.write_bytes(canonical_json(manifest) + b"\n")
        manifest_path.chmod(0o644)
        artifact = output_dir / f"nextcompany-{source_sha}.tar.gz"
        _normalized_tar(release, artifact, commit_epoch)
    artifact_sha = sha256_file(artifact)
    artifact.with_name(artifact.name + ".sha256").write_text(
        f"{artifact_sha}  {artifact.name}\n", encoding="utf-8"
    )
    evidence = {
        "status": "PASS",
        "artifact": str(artifact),
        "artifact_sha256": artifact_sha,
        **manifest,
    }
    artifact.with_name(artifact.name + ".json").write_bytes(
        canonical_json(evidence) + b"\n"
    )
    return evidence


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--source", type=Path, default=ROOT)
    build.add_argument("--output-dir", type=Path, required=True)
    build.add_argument("--python-minor", default=EXPECTED_PYTHON_MINOR)
    build.add_argument("--postgresql-version", default=EXPECTED_POSTGRESQL_VERSION)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--artifact", type=Path, required=True)
    verify.add_argument("--extract-to", type=Path)
    verify.add_argument("--output", type=Path)
    verify_tree = subparsers.add_parser("verify-tree")
    verify_tree.add_argument("--release", type=Path, required=True)
    verify_tree.add_argument("--output", type=Path)
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    args = parse_args(arguments)
    try:
        if args.command == "build":
            result = build_artifact(
                args.source,
                args.output_dir,
                python_minor=args.python_minor,
                postgresql_version=args.postgresql_version,
            )
        elif args.command == "verify":
            result = verify_artifact(args.artifact, extract_to=args.extract_to)
        else:
            manifest = verify_release_tree(args.release)
            result = {
                "status": "PASS",
                "runtime_tree_sha256": manifest["runtime_tree_sha256"],
                "source_git_sha": manifest["source_git_sha"],
                "lock_sha256": manifest["lock_sha256"],
            }
    except (ArtifactError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}, ensure_ascii=False))
        return 1
    if getattr(args, "output", None):
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(canonical_json(result) + b"\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
