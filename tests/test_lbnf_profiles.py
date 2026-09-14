from pathlib import Path

import pytest
import yaml

from dlpgen_opt.config import GenieSource, GiBUUSource, NeutSource, NuWroSource


ROOT = Path(__file__).resolve().parents[1]
FHC = Path(
    "/cvmfs/dune.osgstorage.org/pnfs/fnal.gov/usr/dune/persistent/stash/Flux/"
    "g4lbne/v3r5p10/QGSP_BERT/OfficialEngDesignSept2021_OnAxis/neutrino/"
    "flux/*.dk2nu.root"
)
RHC = Path(
    "/cvmfs/dune.osgstorage.org/pnfs/fnal.gov/usr/dune/persistent/stash/Flux/"
    "g4lbne/v3r5p10/QGSP_BERT/OfficialEngDesignSept2021_OnAxis/antineutrino/"
    "flux/*.dk2nu.root"
)


@pytest.mark.parametrize(
    ("generator", "model"),
    (
        ("genie", GenieSource),
        ("gibuu", GiBUUSource),
        ("nuwro", NuWroSource),
        ("neut", NeutSource),
    ),
)
@pytest.mark.parametrize(("horn", "pattern"), (("fhc", FHC), ("rhc", RHC)))
@pytest.mark.parametrize(
    ("detector", "distance_m", "window_size_m"),
    (("nd", 574.0, (7.0, 5.0)), ("fd", 1_297_000.0, (12.0, 14.0))),
)
def test_lbnf_source_profiles(
    generator, model, horn, pattern, detector, distance_m, window_size_m
):
    path = ROOT / "configs" / generator / f"lbnf_{horn}_{detector}.yaml"
    with path.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    source = model.model_validate({"type": generator, **raw})

    assert source.flux.file_pattern == pattern
    assert source.flux.distance_m == distance_m
    assert source.flux.center_m == (0.0, 0.0)
    assert source.flux.window_size_m == window_size_m
    assert source.flux.max_energy_gev == 120.0
    assert source.flux.checksum_files is False
    assert source.flux.stage_to_local is False
    if generator == "genie":
        assert source.target_pdg == 1_000_180_400
        assert source.flux.max_files is None
    else:
        assert source.target_a == 40
        assert source.target_z == 18
        assert source.energy_min_gev == 0.0
        assert source.energy_max_gev == 120.0
        assert source.energy_bins == 1200
        assert source.flux.max_files == 1
