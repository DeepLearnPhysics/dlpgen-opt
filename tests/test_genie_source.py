from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

import pytest
import yaml

from dlpgen_opt.artifacts import InputArtifact
from dlpgen_opt.config import load_config
from dlpgen_opt.genie_cli import flux_input, write_flux_config
from dlpgen_opt.layout import JobLayout
from dlpgen_opt.pipeline import Pipeline
from dlpgen_opt.provenance import checksum
from dlpgen_opt.sources.genie import GenieBackend


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("profile", "name", "distance_m"),
    (
        ("production.bnb_sbnd.yaml", "bnb_sbnd_v001", 110.0),
        ("production.bnb_icarus.yaml", "bnb_icarus_v001", 600.0),
    ),
)
def test_bnb_profiles_use_nominal_detector_baselines(profile, name, distance_m):
    config = load_config(ROOT / "configs" / profile)
    assert config.production.name == name
    assert config.production.output_dir == ROOT / "runs" / name
    assert config.source.type == "genie"
    assert config.source.config == ROOT / "configs" / "genie" / profile.removeprefix(
        "production."
    )
    assert config.source.executable == "dlpgen-opt-genie"
    assert config.source.expected_commit == "4a6d9e5e50ed9ae72636dd363a2f3fbf672330a6"
    assert config.source.dk2nu_expected_commit == "5b1d8c2cb72b5752a82592ea66af61d8e64a8343"
    assert config.source.flux.distance_m == distance_m
    assert config.source.flux.file_pattern.parent == ROOT
    assert config.source.flux.checksum_files is False
    assert config.source.flux.stage_to_local is True
    assert config.source.vertex_cm == (0.0, 0.0, 0.0)
    assert config.detector.geometry == ROOT / "configs" / "geometry" / "lar_vat.gdml"
    assert config.detector.supera_config == ROOT / "configs" / "supera" / "lar_sbn.yaml"


def test_flux_config_places_window_at_requested_distance(tmp_path):
    output = tmp_path / "flux.xml"
    write_flux_config(
        output,
        distance_m=110.5,
        center_m=(2.0, -3.0),
        window_size_m=(4.0, 6.0),
        max_energy_gev=20.0,
        max_weight_scan_entries=100,
    )
    root = ElementTree.parse(output)
    points = [
        [float(value) for value in point.text.split()]
        for point in root.findall("./param_set/window/point")
    ]
    assert points == [[0.0, -6.0, 110.5], [4.0, -6.0, 110.5], [0.0, 0.0, 110.5]]


def test_flux_catalog_selects_one_file_per_job_without_payload_checksum(tmp_path):
    for index in range(3):
        (tmp_path / f"beam_{index}.dk2nu.root").write_bytes(
            f"flux {index}".encode()
        )
    spline = tmp_path / "splines.xml"
    geometry = tmp_path / "geometry.gdml"
    supera = tmp_path / "supera.yaml"
    for path in (spline, geometry, supera):
        path.write_text("test\n", encoding="utf-8")
    raw = {
        "schema_version": 1,
        "production": {
            "name": "catalog_test",
            "output_dir": "runs/catalog_test",
            "jobs": 3,
            "generator_calls_per_job": 1,
            "base_seed": 17,
        },
        "source": {
            "type": "genie",
            "flux": {
                "file_pattern": "beam_*.dk2nu.root",
                "distance_m": 110.0,
                "stage_to_local": True,
            },
            "spline": spline.name,
        },
        "software": {
            "container_image": "dlpgen-opt:test",
            "edep_sim": {"executable": "edep-sim"},
            "edep2supera": {"executable": "dlpgen-opt-supera"},
            "supera_atomic": {"expected_commit": "abc"},
        },
        "detector": {
            "geometry": geometry.name,
            "supera_config": supera.name,
        },
    }
    config_path = tmp_path / "production.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    config = load_config(config_path)
    backend = GenieBackend()

    assert [backend.selected_flux(config, job).name for job in range(3)] == [
        "beam_2.dk2nu.root",
        "beam_0.dk2nu.root",
        "beam_1.dk2nu.root",
    ]
    selected = backend.inputs(config, 0)[0]
    assert isinstance(selected, InputArtifact)
    assert selected.path.name == "beam_2.dk2nu.root"
    assert selected.checksum is False
    metadata = backend.catalog_metadata(config)
    assert metadata["files"] == 3
    assert metadata["first_job_index"] == 2
    command = backend.command(config, 0, JobLayout.for_job(config, 0))
    assert str(selected.path) in command
    assert command[-1] == "--stage-flux"

    def reject_flux_checksum(path):
        if Path(path).suffix == ".root":
            raise AssertionError("flux payload must not be read during initialization")
        return checksum(Path(path))

    pipeline = Pipeline(config)
    with (
        patch.object(
            pipeline,
            "_dependency_commits",
            return_value={"SuperaAtomic": "abc"},
        ),
        patch("dlpgen_opt.validation.checksum", side_effect=reject_flux_checksum),
    ):
        pipeline.initialize()
    manifest = yaml.safe_load(
        (config.production.output_dir / "manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["genie"]["flux_catalog"]["files"] == 3
    assert "flux" not in manifest["genie"]

    # Later array tasks trust the locked catalog manifest and do not even
    # enumerate the remote directory again.
    with patch(
        "dlpgen_opt.sources.genie.flux_files",
        side_effect=AssertionError("catalog must not be rescanned"),
    ):
        Pipeline(config).initialize()


def test_flux_input_stages_only_selected_file(tmp_path):
    source = tmp_path / "selected.dk2nu.root"
    source.write_bytes(b"selected flux payload")

    with flux_input(str(source), True) as staged:
        staged_path = Path(staged)
        assert staged_path != source
        assert staged_path.read_bytes() == source.read_bytes()

    assert not staged_path.exists()


def test_genie_dry_run_uses_rootracker_and_fixed_vertex(tmp_path, capsys):
    flux = tmp_path / "beam.dk2nu.root"
    spline = tmp_path / "splines.xml"
    geometry = tmp_path / "geometry.gdml"
    supera = tmp_path / "supera.yaml"
    for path in (flux, spline, geometry):
        path.write_text("test\n", encoding="utf-8")
    supera.write_text("BBoxConfig: {Seed: -1}\n", encoding="utf-8")
    raw = {
        "schema_version": 1,
        "production": {
            "name": "genie_test",
            "output_dir": "runs/genie_test",
            "jobs": 1,
            "generator_calls_per_job": 1,
            "base_seed": 17,
        },
        "source": {
            "type": "genie",
            "flux": {"file_pattern": flux.name, "distance_m": 50.0},
            "spline": spline.name,
        },
        "software": {
            "container_image": "dlpgen-opt:test",
            "edep_sim": {"executable": "edep-sim"},
            "edep2supera": {"executable": "dlpgen-opt-supera"},
            "supera_atomic": {"expected_commit": "abc"},
        },
        "detector": {
            "geometry": geometry.name,
            "supera_config": supera.name,
        },
    }
    config_path = tmp_path / "production.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    pipeline = Pipeline(load_config(config_path))
    pipeline.run(0, dry_run=True)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert "--distance-m 50.0" in records[0]["command"]
    macro_lines = pipeline.source.edep_macro_lines(
        pipeline.config, JobLayout.for_job(pipeline.config, 0)
    )
    assert any("rooTracker/input" in line for line in macro_lines)
    assert "/generator/position/fixed/position 0.0 0.0 0.0 cm" in macro_lines
