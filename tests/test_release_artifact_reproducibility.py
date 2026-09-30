from __future__ import annotations

import filecmp
import os
from pathlib import Path
import sys
import tarfile
import tempfile

import pytest

from scripts.release_artifact import build_artifact, sha256_file


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux runtime artifact regression")
@pytest.mark.skipif(
    os.getenv("STAGING_ARTIFACT_DOUBLE_BUILD") != "1",
    reason="run by the staging parity artifact reproducibility CI gate",
)
def test_same_source_builds_byte_identically_across_paths_and_umasks(tmp_path):
    outputs = []
    manifests = []
    results = []
    original_tempdir = tempfile.tempdir
    original_umask = os.umask(0o022)
    try:
        for index, mask in enumerate((0o022, 0o077), start=1):
            build_temp = tmp_path / f"temporary-build-root-{index}"
            output = tmp_path / f"separate-output-root-{index}"
            build_temp.mkdir()
            tempfile.tempdir = str(build_temp)
            os.umask(mask)
            result = build_artifact(ROOT, output)
            artifact = Path(result["artifact"])
            outputs.append(artifact)
            results.append(result)
            with tarfile.open(artifact, "r:gz") as archive:
                manifests.append(
                    archive.extractfile("release/release-manifest.json").read()
                )
                assert not any(
                    member.name.endswith((".pyc", ".pyo"))
                    or "/__pycache__/" in member.name
                    for member in archive.getmembers()
                )
    finally:
        tempfile.tempdir = original_tempdir
        os.umask(original_umask)

    assert filecmp.cmp(outputs[0], outputs[1], shallow=False)
    assert sha256_file(outputs[0]) == sha256_file(outputs[1])
    assert manifests[0] == manifests[1]
    assert results[0]["runtime_tree_sha256"] == results[1]["runtime_tree_sha256"]
    assert results[0]["builder"] == results[1]["builder"]
    assert results[0]["source_git_sha"] == results[0]["builder"]["git_sha"]
    assert results[0]["build_kind"] == "source_builder_same_commit"
