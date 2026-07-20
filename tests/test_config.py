from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from dlpgen_opt.config import load_config


def test_paths_and_seeds_are_resolved(production_config):
    assert production_config.production.output_dir.is_absolute()
    assert production_config.source.config.is_absolute()
    assert production_config.seed(0, 0) == 100
    assert production_config.seed(1, 2) == 112


def test_latest_container_is_rejected(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "production": {
                    "name": "bad",
                    "output_dir": "out",
                    "jobs": 1,
                    "generator_calls_per_job": 1,
                    "base_seed": 1,
                },
                "source": {"type": "dlpgen", "config": "tune.yaml"},
                "software": {
                    "container_image": "image:latest",
                    "edep_sim": {"executable": "edep-sim"},
                    "edep2supera": {"executable": "run_edep2supera.py"},
                    "supera_atomic": {"expected_commit": "abc"},
                },
                "detector": {"geometry": "g.gdml", "supera_config": "s.yaml"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="latest"):
        load_config(path)
