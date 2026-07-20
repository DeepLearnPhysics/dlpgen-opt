from __future__ import annotations

import csv

import pytest

from dlpgen_opt.layout import JobLayout
from dlpgen_opt.sources.dlpgen import DLPGeneratorBackend


FIELDS = [
    "call_id", "interaction_id", "particle_id", "particle_in_interaction",
    "status_code", "pdg_code", "parent0", "parent1", "child_first",
    "child_last", "px", "py", "pz", "energy", "mass", "x", "y", "z", "t",
]


def _write_csv(path, interactions=(0,)):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        for call in range(2):
            for interaction in interactions:
                writer.writerow(
                    dict(
                        call_id=call, interaction_id=interaction, particle_id=0,
                        particle_in_interaction=0, status_code=1.0, pdg_code=13.0,
                        parent0=0, parent1=0, child_first=0, child_last=0,
                        px=0.1, py=0.2, pz=0.3, energy=0.4, mass=0.105,
                        x=10, y=-20, z=30, t=4,
                    )
                )


def test_csv_is_converted_to_pbomb_with_cm_vertices(production_config):
    layout = JobLayout.for_job(production_config, 0)
    layout.create()
    _write_csv(layout.source_csv)
    result = DLPGeneratorBackend().finalize(production_config, layout)
    lines = layout.hepevt.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "0 0 1 1 -2 3 4"
    assert lines[1].split() == ["1", "13", "0", "0", "0", "0", "0.1", "0.2", "0.3", "0.4", "0.105"]
    assert result["format"] == "edep-sim-pbomb"


def test_multiple_interactions_per_call_fail_loudly(production_config):
    layout = JobLayout.for_job(production_config, 0)
    layout.create()
    _write_csv(layout.source_csv, interactions=(0, 1))
    with pytest.raises(RuntimeError, match="NumEvent"):
        DLPGeneratorBackend().finalize(production_config, layout)
