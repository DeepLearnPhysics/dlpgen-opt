from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree

import pytest
import yaml

from dlpgen_opt.config import load_config
from dlpgen_opt.genie_cli import write_flux_config
from dlpgen_opt.layout import JobLayout
from dlpgen_opt.pipeline import Pipeline


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
