from __future__ import annotations

import json

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
