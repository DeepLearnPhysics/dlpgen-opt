from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pyhepmc
import yaml

from dlpgen_opt.config import GiBUUSource, load_config
from dlpgen_opt.layout import JobLayout
from dlpgen_opt.nuhepmc_cli import (
    GIBUU_NUCLEAR_REMNANT_PDG,
    NUCLEAR_REMNANT_PDG,
    convert,
)
from dlpgen_opt.nuhepmc_rootracker import _reaction_mode
from dlpgen_opt.pipeline import Pipeline
from dlpgen_opt.sources.gibuu import GiBUUBackend


def test_nuhepmc_process_names_map_to_normalized_modes():
    expected = {
        "CC_QE_nu": ("Weak[CC]", "QES"),
        "NC_elastic_n_nubar": ("Weak[NC]", "QES"),
        "CC_2p2h_nu": ("Weak[CC]", "MEC"),
        "NC_RES_ppi0_nu": ("Weak[NC]", "RES"),
        "CC_eta_nubar": ("Weak[CC]", "RES"),
        "CC_kaon_nu": ("Weak[CC]", "RES"),
        "NC_1gamma_p_nu": ("Weak[NC]", "RES"),
        "CC_multi_pi_nu": ("Weak[CC]", "RES"),
        "NC_DIF_nu": ("Weak[NC]", "DFR"),
        "CC_COH_nu": ("Weak[CC]", "COH"),
        "NC_DIS_nubar": ("Weak[NC]", "DIS"),
        "CC Bkgd-p": ("Weak[CC]", "1Pion"),
    }
    for name, result in expected.items():
        assert _reaction_mode(name) == result


def _write_nuhepmc(
    path: Path,
    count: int = 3,
    version: int = 1,
    remnant_pdg: int = NUCLEAR_REMNANT_PDG,
) -> None:
    run_info = pyhepmc.GenRunInfo()
    run_info.attributes["NuHepMC.Version.Major"] = version
    run_info.attributes["NuHepMC.Version.Minor"] = 9 if version == 0 else 0
    run_info.attributes["NuHepMC.Version.Patch"] = 0
    run_info.weight_names = ["CV"]
    run_info.attributes["NuHepMC.ProcessInfo[100].Name"] = "CCQE"
    run_info.tools = [
        pyhepmc.GenRunInfo.ToolInfo("GiBUU", "2025", "test event generator")
    ]
    with pyhepmc.open(str(path), "w") as output:
        for index in range(count):
            event = pyhepmc.GenEvent(pyhepmc.Units.GEV, pyhepmc.Units.CM)
            event.run_info = run_info
            event.event_number = 10 + index
            event.attributes["ProcID" if version == 0 else "signal_process_id"] = 100
            event.attributes["LabPos" if version == 0 else "lab_pos"] = [
                0.0,
                0.0,
                0.0,
            ]
            event.weights = [1.0 + index]
            vertex = pyhepmc.GenVertex(pyhepmc.FourVector(0.0, 0.0, 0.0, 0.0))
            vertex.status = 1
            vertex.add_particle_in(
                pyhepmc.GenParticle(pyhepmc.FourVector(0.0, 0.0, 1.0, 1.0), 14, 4)
            )
            vertex.add_particle_in(
                pyhepmc.GenParticle(
                    pyhepmc.FourVector(0.0, 0.0, 0.0, 37.2), 1_000_180_400, 20
                )
            )
            vertex.add_particle_out(
                pyhepmc.GenParticle(
                    pyhepmc.FourVector(0.1, 0.0, 0.6, 0.62), 13, 1
                )
            )
            vertex.add_particle_out(
                pyhepmc.GenParticle(
                    pyhepmc.FourVector(-0.1, 0.0, 0.4, 1.02), 2212, 1
                )
            )
            vertex.add_particle_out(
                pyhepmc.GenParticle(
                    pyhepmc.FourVector(0.0, 0.0, 0.0, 36.0),
                    remnant_pdg,
                    1,
                )
            )
            event.add_vertex(vertex)
            output.write(event)


def _write_config(tmp_path: Path, input_path: Path) -> Path:
    geometry = tmp_path / "geometry.gdml"
    supera = tmp_path / "supera.yaml"
    jobcard = tmp_path / "gibuu.job"
    geometry.write_text("<gdml/>\n", encoding="utf-8")
    supera.write_text("BBoxConfig: {Seed: -1}\n", encoding="utf-8")
    jobcard.write_text("&neutrino_induced /\n", encoding="utf-8")
    raw = {
        "schema_version": 1,
        "production": {
            "name": "gibuu_test",
            "output_dir": "runs/gibuu_test",
            "jobs": 2,
            "generator_calls_per_job": 1,
            "base_seed": 17,
        },
        "source": {
            "type": "gibuu",
            "input": input_path.name,
            "jobcard": jobcard.name,
            "generator_version": "2025",
            "vertex_cm": [1.0, -2.0, 3.0],
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
    path = tmp_path / "production.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_nuhepmc_conversion_selects_final_state_and_event_range(tmp_path: Path):
    source = tmp_path / "events.hepmc3"
    output = tmp_path / "events.gtrac.root"
    metadata = tmp_path / "metadata.json"
    _write_nuhepmc(source)

    projected = []

    def capture(events, path):
        projected.extend(events)
        path.write_bytes(b"ROOT")

    with patch("dlpgen_opt.nuhepmc_cli.write_rootracker", side_effect=capture):
        result = convert(
            source,
            output,
            metadata,
            events=2,
            skip=1,
            output_event_offset=7,
            vertex_cm=(1.0, -2.0, 3.0),
        )

    assert [event.event_number for event in projected] == [7, 8]
    assert projected[0].vertex_m == (0.01, -0.02, 0.03, 0.0)
    assert [particle.pdg for particle in projected[0].particles] == [
        14,
        1000180400,
        13,
        2212,
    ]
    assert [particle.status for particle in projected[0].particles] == [0, 0, 1, 1]
    assert projected[0].reaction.startswith(
        "nu:14;tgt:1000180400;N:-1;proc:Weak[CC],QES;"
    )
    assert result["source_event_numbers"] == [11, 12]
    assert result["output_event_offset"] == 7
    assert result["process_ids"] == [100, 100]
    assert result["lab_positions"] == [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
    assert result["weight_names"] == ["CV"]
    assert result["event_weights"] == [[2.0], [3.0]]
    assert result["generator_tools"][0]["name"] == "GiBUU"
    assert result["particles_written"] == 4
    assert result["nuclear_remnants_skipped"] == 2
    assert json.loads(metadata.read_text(encoding="utf-8")) == result


def test_gibuu_2025_nuhepmc_09_fixed_width_particle_fields(tmp_path: Path):
    source = tmp_path / "gibuu.hepmc3"
    output = tmp_path / "events.gtrac.root"
    metadata = tmp_path / "metadata.json"
    _write_nuhepmc(
        source,
        count=1,
        version=0,
        remnant_pdg=GIBUU_NUCLEAR_REMNANT_PDG,
    )

    # GiBUU's Fortran writer leaves leading blanks on positive numeric fields.
    lines = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.startswith("P "):
            fields = line.split()
            line = " ".join(fields[:4]) + "  " + "  ".join(fields[4:])
        lines.append(line)
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")

    projected = []
    with patch(
        "dlpgen_opt.nuhepmc_cli.write_rootracker",
        side_effect=lambda events, path: (
            projected.extend(events),
            path.write_bytes(b"ROOT"),
        ),
    ):
        result = convert(source, output, metadata, events=1)

    assert [particle.pdg for particle in projected[0].particles] == [
        14,
        1000180400,
        13,
        2212,
    ]
    assert result["process_ids"] == [100]
    assert result["nuclear_remnants_skipped"] == 1


def test_gibuu_backend_uses_nonoverlapping_event_ranges(tmp_path: Path):
    source = tmp_path / "events.hepmc3"
    _write_nuhepmc(source)
    config = load_config(_write_config(tmp_path, source))
    assert isinstance(config.source, GiBUUSource)
    layout = JobLayout.for_job(config, 1)
    backend = GiBUUBackend()

    command = backend.command(config, 1, layout)
    assert command[:4] == [
        sys.executable,
        "-m",
        "dlpgen_opt.nuhepmc_cli",
        str(source),
    ]
    assert command[command.index("--skip") + 1] == "1"
    assert command[-3:] == ["1.0", "-2.0", "3.0"]
    assert backend.edep_macro_lines(config, layout) == [
        "/generator/kinematics/rooTracker/input " + str(layout.rootracker),
        "/generator/kinematics/rooTracker/generator GiBUU",
        "/generator/kinematics/set rooTracker",
    ]

    pipeline = Pipeline(config)
    with patch.object(
        pipeline,
        "_dependency_commits",
        return_value={"SuperaAtomic": "abc"},
    ):
        pipeline.initialize()
    manifest = yaml.safe_load(
        (config.production.output_dir / "manifest.yaml").read_text(encoding="utf-8")
    )
    assert manifest["gibuu"]["generator_version"] == "2025"
    assert manifest["gibuu"]["native_format"] == "NuHepMC"
    assert manifest["gibuu"]["native_input"]["sha256"]

    assert command[command.index("--output") + 1] == str(layout.rootracker)


def test_gibuu_source_profile_merges_with_production_inputs(tmp_path: Path):
    source = tmp_path / "events.hepmc3"
    _write_nuhepmc(source, count=1)
    config_path = _write_config(tmp_path, source)
    profile_dir = tmp_path / "gibuu"
    profile_dir.mkdir()
    profile = profile_dir / "nuhepmc-import.yaml"
    profile.write_text(
        "generator_version: '2025-p5'\n"
        "checksum_input: false\n"
        "vertex_cm: [4.0, 5.0, 6.0]\n",
        encoding="utf-8",
    )
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["source"] = {
        "type": "gibuu",
        "config": "gibuu/nuhepmc-import.yaml",
        "input": source.name,
        "jobcard": "gibuu.job",
    }
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    config = load_config(config_path)

    assert isinstance(config.source, GiBUUSource)
    assert config.source.config == profile
    assert config.source.input == source
    assert config.source.jobcard == tmp_path / "gibuu.job"
    assert config.source.generator_version == "2025-p5"
    assert config.source.checksum_input is False
    assert config.source.vertex_cm == (4.0, 5.0, 6.0)
