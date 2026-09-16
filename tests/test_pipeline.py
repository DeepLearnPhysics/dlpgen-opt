from __future__ import annotations

import json

from dlpgen_opt.config import SpineStageSoftware
from dlpgen_opt.pipeline import Pipeline


def test_dry_run_is_read_only_and_prints_all_commands(production_config, capsys):
    pipeline = Pipeline(production_config)
    pipeline.run(0, dry_run=True)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [record["stage"] for record in records] == ["generate", "edep-sim", "supera"]
    assert not production_config.production.output_dir.exists()
    assert "--seed 100" in records[0]["command"]
    assert "/edep-sim/edep.root" in records[1]["output"]


def test_initialize_records_resolved_configuration(production_config):
    pipeline = Pipeline(production_config)
    pipeline.initialize()
    root = production_config.production.output_dir
    assert (root / "resolved_config.yaml").is_file()
    assert (root / "manifest.yaml").is_file()


def test_spine_stage_is_selected_and_planned(production_config, capsys):
    production_config.software.spine = SpineStageSoftware(executable="spine")
    pipeline = Pipeline(production_config)
    pipeline.run(0, dry_run=True)

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [record["stage"] for record in records] == [
        "generate",
        "edep-sim",
        "supera",
        "spine",
    ]
    assert "configs/spine/dlpgen.yaml" in records[-1]["command"]
    assert records[-1]["output"].endswith("/spine/spine.h5")
    assert [path.name for path in pipeline._spine_config_inputs()] == [
        "dlpgen.yaml",
        "base.yaml",
    ]


def test_spine_event_cap_is_production_wide(production_config, capsys):
    production_config.production.jobs = 3
    production_config.software.spine = SpineStageSoftware(
        executable="spine",
        max_events=3,
    )
    pipeline = Pipeline(production_config)

    for job in range(3):
        pipeline.spine(job, dry_run=True)

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert pipeline._spine_event_count(0) == 2
    assert pipeline._spine_event_count(1) == 1
    assert pipeline._spine_event_count(2) == 0
    assert "--num-entries" not in records[0]["command"]
    assert "--num-entries 1" in records[1]["command"]
    assert records[2]["status"] == "skipped"
