from __future__ import annotations

import argparse
import gzip
import json
import re
from pathlib import Path

import pytest

from dlpgen_opt.config import GiBUUSource, load_config
from dlpgen_opt.gibuu_cli import (
    Candidate,
    _effective_sample_size,
    _read_cached_candidates,
    _run_cached,
    _selection_uniform,
    _shard_seed,
    _write_candidates,
    _write_rootracker_candidates,
    _write_reproducible_archive,
    resolved_jobcard,
)
from dlpgen_opt.layout import JobLayout
from dlpgen_opt.pipeline import Pipeline
from dlpgen_opt.provenance import checksum, write_yaml
from dlpgen_opt.sources.gibuu import GiBUUBackend


TEMPLATE = """
&neutrino_induced
 process_ID = 2
 flavor_ID = 2
 nuXsectionMode = 16
 nuExp = 15
 FileNameFlux = 'old.dat'
/
&target
 A = 12
 Z = 6
/
&input
 numEnsembles = 10
 num_runs_SameEnergy = 2
 num_Energies = 1
 path_to_input = '/old'
 version = 2024
/
&initRandom
 SEED = 1
/
&EventOutput
 WritePerturbativeParticles = F
 EventFormat = 1
/
"""


def _value(jobcard: str, key: str) -> str:
    match = re.search(rf"^\s*{key}\s*=\s*(.*?)\s*$", jobcard, re.MULTILINE)
    assert match
    return match.group(1)


def test_resolved_jobcard_sets_native_nuhepmc_flux_contract(tmp_path: Path):
    flux = tmp_path / "flux.dat"
    result = resolved_jobcard(
        TEMPLATE,
        pdg=-14,
        process="nc",
        flux_file=flux,
        input_tables=Path("/opt/gibuu/buuinput"),
        target_a=40,
        target_z=18,
        ensembles=100,
        runs=3,
        time_steps=150,
        seed=104741,
    )

    assert _value(result, "process_ID") == "-3"
    assert _value(result, "flavor_ID") == "2"
    assert _value(result, "nuExp") == "99"
    assert _value(result, "FileNameFlux") == f"'{flux}'"
    assert _value(result, "A") == "40"
    assert _value(result, "Z") == "18"
    assert _value(result, "numEnsembles") == "100"
    assert _value(result, "num_runs_SameEnergy") == "3"
    assert _value(result, "numTimeSteps") == "150"
    assert _value(result, "EventFormat") == "7"
    assert _value(result, "WritePerturbativeParticles") == ".true."
    assert _value(result, "WriteRealParticles") == ".false."


@pytest.mark.parametrize(
    ("production", "name", "distance_m"),
    [
        ("production.gibuu-bnb.yaml", "gibuu_bnb_v001", 110.0),
        (
            "production.gibuu-bnb_icarus.yaml",
            "gibuu_bnb_icarus_v001",
            600.0,
        ),
    ],
)
def test_native_gibuu_profile_dispatches_internal_generator(
    production, name, distance_m
):
    config = load_config(Path("configs") / production)

    assert isinstance(config.source, GiBUUSource)
    assert config.source.mode == "generate"
    assert config.source.flux is not None
    assert config.production.name == name
    assert config.source.flux.distance_m == distance_m
    assert config.source.flux.max_files == 32
    command = GiBUUBackend().command(config, 0, JobLayout.for_job(config, 0))
    assert command[1:3] == ["-m", "dlpgen_opt.gibuu_cli"]
    assert "--flux-spectra-manifest" in command
    process_start = command.index("--processes") + 1
    assert command[process_start : command.index("--vertex-cm")] == ["cc", "nc"]
    assert "--candidate-cache-dir" in command
    assert command[command.index("--total-events") + 1] == "10"
    assert command[command.index("--job-index") + 1] == "0"


def test_weighted_selection_counter_is_reproducible():
    assert _selection_uniform(17, "pdg14-cc", 3) == _selection_uniform(
        17, "pdg14-cc", 3
    )
    assert _selection_uniform(17, "pdg14-cc", 3) != _selection_uniform(
        17, "pdg14-nc", 3
    )


def test_effective_sample_size_responds_to_weight_concentration():
    def candidate(index, weight):
        return Candidate(
            component="pdg14-cc",
            source_event=index,
            process_id=2,
            native_weight=weight,
            flux_integral=1.0,
            score=0.0,
            particles=(),
            cache_id=str(index),
        )

    assert _effective_sample_size([candidate(0, 1), candidate(1, 1)]) == 2
    assert _effective_sample_size([candidate(0, 10), candidate(1, 1)]) < 1.2


def test_cache_shard_seeds_do_not_overlap():
    seeds = {
        _shard_seed("a" * 64, shard, component)
        for shard in range(20)
        for component in range(8)
    }
    assert len(seeds) == 160


def test_native_archive_is_reproducible(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "native.hepmc3").write_text("event\n", encoding="utf-8")
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    _write_reproducible_archive(source, first)
    (source / "native.hepmc3").touch()
    _write_reproducible_archive(source, second)

    assert first.read_bytes() == second.read_bytes()


def test_candidate_cache_preserves_neutrino_truth(tmp_path: Path):
    path = tmp_path / "candidates.jsonl.gz"
    candidate = Candidate(
        component="pdg14-cc",
        source_event=17,
        process_id=200,
        native_weight=0.25,
        flux_integral=2.0,
        score=0.0,
        particles=((13, 0.1, 0.2, 0.3, 0.4, 0.105),),
        cache_id="shard-00000/pdg14-cc/17",
        incoming_neutrino=(14, 0.0, 0.0, 1.25, 1.25),
        target_pdg=1000180400,
        nucleon_pdg=2112,
        reaction=(
            "nu:14;tgt:1000180400;N:2112;proc:Weak[CC],QES;"
            "generator_process:200;"
        ),
        cross_section_1e38_cm2=0.0038,
    )

    _write_candidates(path, [candidate])

    restored = _read_cached_candidates(path)[0]
    assert restored == candidate


def test_old_candidate_cache_is_rejected(tmp_path: Path):
    path = tmp_path / "candidates.jsonl.gz"
    candidate = Candidate(
        component="pdg14-cc",
        source_event=17,
        process_id=200,
        native_weight=0.25,
        flux_integral=2.0,
        score=0.0,
        particles=((13, 0.1, 0.2, 0.3, 0.4, 0.105),),
    )
    _write_candidates(path, [candidate])
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        record = json.loads(stream.read())
    for key in (
        "incoming_neutrino",
        "target_pdg",
        "nucleon_pdg",
        "reaction",
        "cross_section_1e38_cm2",
    ):
        record.pop(key)
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")

    with pytest.raises(RuntimeError, match="predates the RooTracker truth schema"):
        _read_cached_candidates(path)


def test_cached_candidate_projects_complete_rootracker_truth(
    tmp_path: Path, monkeypatch
):
    candidate = Candidate(
        component="pdg14-cc",
        source_event=17,
        process_id=200,
        native_weight=0.25,
        flux_integral=2.0,
        score=0.0,
        particles=((13, 0.1, 0.2, 0.3, 0.4, 0.105),),
        incoming_neutrino=(14, 0.0, 0.0, 1.25, 1.25),
        target_pdg=1000180400,
        nucleon_pdg=2112,
        reaction=(
            "nu:14;tgt:1000180400;N:2112;proc:Weak[CC],QES;"
            "generator_process:200;"
        ),
        cross_section_1e38_cm2=0.0038,
    )
    projected = []
    monkeypatch.setattr(
        "dlpgen_opt.gibuu_cli.write_rootracker",
        lambda events, output: projected.extend(events),
    )

    _write_rootracker_candidates([candidate], tmp_path / "events.root", (1, 2, 3))

    assert projected[0].vertex_m == (0.01, 0.02, 0.03, 0.0)
    assert projected[0].weight == 1.0
    assert projected[0].nucleon_pdg == 2112
    assert [(particle.pdg, particle.status) for particle in projected[0].particles] == [
        (14, 0),
        (1000180400, 0),
        (13, 1),
    ]


def test_cached_campaign_allocates_non_overlapping_job_ranges(
    tmp_path: Path, monkeypatch
):
    candidates = [
        Candidate(
            component="pdg14-cc",
            source_event=index,
            process_id=2,
            native_weight=1.0,
            flux_integral=1.0,
            score=0.0,
            particles=((13, 0.0, 0.0, 1.0, 1.1, 0.1),),
            cache_id=f"shard-00000/pdg14-cc/{index}",
        )
        for index in range(6)
    ]
    cache_entry = tmp_path / "cache/key"
    cache_manifest = {
        "key": "key",
        "shards": [{"index": 0, "candidate_events": len(candidates)}],
    }
    monkeypatch.setattr(
        "dlpgen_opt.gibuu_cli._ensure_candidate_cache",
        lambda args, required: (cache_entry, cache_manifest, candidates, 1.0),
    )
    monkeypatch.setattr(
        "dlpgen_opt.gibuu_cli.write_rootracker",
        lambda events, output: output.write_bytes(b"ROOT"),
    )

    selected_ids = []
    for job in range(2):
        output = tmp_path / f"job-{job}.gtrac.root"
        metadata = tmp_path / f"job-{job}.json"
        args = argparse.Namespace(
            total_events=4,
            job_index=job,
            campaign_dir=tmp_path / "campaign",
            events=2,
            seed=17,
            vertex_cm=(0.0, 0.0, 0.0),
            output=output,
            metadata_output=metadata,
            reserve_fraction=0.10,
        )
        _run_cached(args)
        record = json.loads(metadata.read_text(encoding="utf-8"))
        selected_ids.append({item["cache_id"] for item in record["selected"]})

    assert selected_ids[0].isdisjoint(selected_ids[1])
    assert len(selected_ids[0] | selected_ids[1]) == 4


def test_initialize_materializes_shared_flux_behind_pipeline(
    tmp_path: Path, monkeypatch
):
    config = load_config("configs/production.gibuu-bnb.yaml")
    jobcard = tmp_path / "template.job"
    jobcard.write_text(TEMPLATE, encoding="utf-8")
    assert isinstance(config.source, GiBUUSource)
    assert config.source.flux is not None
    source = config.source.model_copy(
        update={
            "jobcard": jobcard,
            "dk2nu_expected_commit": None,
            "flux": config.source.flux.model_copy(
                update={"file_pattern": tmp_path / "input-*.root"}
            ),
        }
    )
    config = config.model_copy(
        update={
            "production": config.production.model_copy(
                update={"output_dir": tmp_path / "run"}
            ),
            "source": source,
        }
    )
    calls = []
    (tmp_path / "input-0.root").write_text("input", encoding="utf-8")

    def fake_materialize(**kwargs):
        calls.append(kwargs)
        kwargs["output"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output"].write_text("canonical", encoding="utf-8")
        write_yaml(
            kwargs["manifest_output"],
            {
                "sampling": {"complete_catalog": True},
                "normalization": {"valid_per_selected_pot": True},
                "output": {"sha256": checksum(kwargs["output"])},
            },
        )

    def fake_spectra(**kwargs):
        spectrum = kwargs["output_manifest"].parent / "flux-14.dat"
        spectrum.write_text("0.5 1.0\n", encoding="utf-8")
        write_yaml(
            kwargs["output_manifest"],
            {
                "format": "dlpgen-opt-gibuu-flux-spectra",
                "flavors": {
                    "14": {
                        "path": spectrum.name,
                        "integral_per_cm2": 1.0,
                        "sha256": checksum(spectrum),
                    }
                },
            },
        )

    monkeypatch.setattr("dlpgen_opt.pipeline.materialize_flux", fake_materialize)
    monkeypatch.setattr(
        "dlpgen_opt.pipeline.materialize_flux_spectra", fake_spectra
    )
    Pipeline(config).initialize()

    assert len(calls) == 1
    assert calls[0]["flux_pattern"] == tmp_path / "input-*.root"
    assert (tmp_path / "run/flux/canonical.root").is_file()
    assert (tmp_path / "run/flux/spectra.yaml").is_file()
    assert (tmp_path / "run/manifest.yaml").is_file()

    second = config.model_copy(
        update={
            "production": config.production.model_copy(
                update={"output_dir": tmp_path / "run-second"}
            )
        }
    )
    Pipeline(second).initialize()

    assert len(calls) == 1
    assert (tmp_path / "run-second/flux/spectra.yaml").is_file()
