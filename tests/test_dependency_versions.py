import re
from pathlib import Path
import subprocess

import yaml


REPOSITORY = Path(__file__).resolve().parents[1]


def _docker_args() -> dict[str, str]:
    text = (REPOSITORY / "Dockerfile").read_text(encoding="utf-8")
    return dict(re.findall(r"^ARG ([A-Z0-9_]+)=([^\s]+)$", text, re.MULTILINE))


def _versions() -> dict[str, object]:
    with (REPOSITORY / "dependencies" / "versions.yaml").open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def test_source_submodule_pins_match_manifest() -> None:
    versions = _versions()
    for name in (
        "DLPGenerator",
        "GENIE",
        "dk2nu",
        "NuWro",
        "ROOTEGPythia6",
        "edep-sim",
        "SuperaAtomic",
        "edep2supera",
    ):
        entry = subprocess.check_output(
            ["git", "-C", str(REPOSITORY), "ls-tree", "HEAD", f"dependencies/{name}"],
            text=True,
        ).strip()
        mode, object_type, commit, path = entry.split()
        assert (mode, object_type, path) == ("160000", "commit", f"dependencies/{name}")
        assert versions[name] == commit


def test_docker_artifact_pins_match_manifest() -> None:
    versions = _versions()
    args = _docker_args()
    expected = {
        "GEANT4_VERSION": "Geant4",
        "CMAKE_VERSION": "CMake",
        "GENIE_VERSION": "GENIEVersion",
        "PYTHIA8_SOURCE_SHA256": "Pythia8SourceSHA256",
        "GENIE_AR23_XSEC_SHA256": "GENIESplineSHA256",
        "GENIE_N24_XSEC_SHA256": "GENIEN24SplineSHA256",
        "GENIE_G18_10A_XSEC_SHA256": "GENIEG18SplineSHA256",
        "GIBUU_RELEASE": "GiBUURelease",
        "GIBUU_SOURCE_SHA256": "GiBUUSourceSHA256",
        "GIBUU_INPUT_SHA256": "GiBUUInputSHA256",
        "NUWRO_VERSION": "NuWroVersion",
    }
    for docker_key, manifest_key in expected.items():
        assert str(versions[manifest_key]) == args[docker_key]

    assert int(args["PYTHIA8_VERSION"]) / 1000 == versions["Pythia8"]


def test_base_image_pin_matches_manifest() -> None:
    versions = _versions()
    first_line = next(
        line
        for line in (REPOSITORY / "Dockerfile").read_text(encoding="utf-8").splitlines()
        if line.startswith("FROM ")
    )
    image, digest = first_line.removeprefix("FROM ").split("@", maxsplit=1)
    digest = digest.split()[0]
    assert versions["LArCV2Image"] == image
    assert versions["LArCV2ImageDigest"] == digest


def test_neut_runtime_pin_matches_manifest() -> None:
    versions = _versions()
    dockerfile = (REPOSITORY / "Dockerfile").read_text(encoding="utf-8")
    neut_line = next(
        line
        for line in dockerfile.splitlines()
        if line.startswith("FROM ") and line.endswith(" AS neut_upstream")
    )
    image, digest = neut_line.removeprefix("FROM ").split("@", maxsplit=1)
    digest = digest.split()[0]
    expected_image = str(versions["NEUTQuickstartImage"]).rsplit(":", maxsplit=1)[0]
    assert image == expected_image
    assert digest == versions["NEUTQuickstartImageDigest"]
    assert str(versions["NEUTSourceCommit"]) in dockerfile


def test_public_release_does_not_embed_neut_runtime() -> None:
    dockerfile = (REPOSITORY / "Dockerfile").read_text(encoding="utf-8")
    workflow = (
        REPOSITORY / ".github" / "workflows" / "release-image.yml"
    ).read_text(encoding="utf-8")

    assert "FROM runtime_base AS runtime-neut" in dockerfile
    assert "FROM runtime_base AS runtime" in dockerfile
    assert "target: runtime" in workflow

    public_target = dockerfile.split("FROM runtime_base AS runtime\n", maxsplit=1)[1]
    assert "COPY --from=neut_upstream" not in public_target
