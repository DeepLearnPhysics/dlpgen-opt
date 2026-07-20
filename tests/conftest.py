from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dlpgen_opt.config import ProductionConfig, load_config


@pytest.fixture
def production_config(tmp_path: Path) -> ProductionConfig:
    tune = tmp_path / "tune.yaml"
    geometry = tmp_path / "geometry.gdml"
    supera = tmp_path / "supera.yaml"
    tune.write_text("Generator: {}\n", encoding="utf-8")
    geometry.write_text("<gdml/>\n", encoding="utf-8")
    supera.write_text("BBoxConfig: {Seed: -1}\n", encoding="utf-8")
    config = {
        "schema_version": 1,
        "production": {
            "name": "test_v001",
            "output_dir": "runs/test_v001",
            "jobs": 2,
            "generator_calls_per_job": 2,
            "base_seed": 100,
            "seed_stride": 10,
        },
        "source": {
            "type": "dlpgen",
            "config": tune.name,
            "executable": "dlpgen",
        },
        "software": {
            "container_image": "dlpgen-opt:test",
            "edep_sim": {"executable": "edep-sim"},
            "edep2supera": {"executable": "run_edep2supera.py"},
            "supera_atomic": {
                "expected_commit": "799b2bb84d2e27aa3a2e5d90869fa453f86c68b8"
            },
        },
        "detector": {
            "geometry": geometry.name,
            "supera_config": supera.name,
        },
    }
    path = tmp_path / "production.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return load_config(path)
