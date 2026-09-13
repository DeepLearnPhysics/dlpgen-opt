from __future__ import annotations

from pathlib import Path

import pytest

from dlpgen_opt.config import NuWroSource, load_config
from dlpgen_opt.layout import JobLayout
from dlpgen_opt.nuwro_cli import _reaction, render_params
from dlpgen_opt.sources.nuwro import NuWroBackend


def _spectrum(path: Path, values: list[float]) -> None:
    path.write_text(
        "# energy_GeV flux_per_GeV_cm2\n"
        + "\n".join(f"{index + 0.5} {value}" for index, value in enumerate(values))
        + "\n",
        encoding="utf-8",
    )


def test_render_params_encodes_mixed_flavor_histograms(tmp_path: Path):
    numu = tmp_path / "numu.dat"
    nue = tmp_path / "nue.dat"
    _spectrum(numu, [1.0, 3.0])
    _spectrum(nue, [2.0, 4.0])
    params = render_params(
        {
            14: {"path": numu, "integral_per_cm2": 12.0},
            12: {"path": nue, "integral_per_cm2": 2.0},
        },
        events=10,
        test_events=1000,
        seed=17,
        target_a=40,
        target_z=18,
        processes=["cc", "nc"],
        energy_min_gev=0.0,
        energy_max_gev=2.0,
        energy_bins=2,
    )

    assert "number_of_events = 10" in params
    assert "number_of_test_events = 1000" in params
    assert "nucleus_p = 18" in params
    assert "nucleus_n = 22" in params
    assert "beam_content = 12 2 % 0 2000 2 4" in params
    assert "beam_content += 14 12 % 0 2000 1 3" in params
    assert "dyn_dis_nc = 1" in params
    assert "FSI_on = 1" in params


def test_render_params_rejects_wrong_bin_count(tmp_path: Path):
    spectrum = tmp_path / "numu.dat"
    _spectrum(spectrum, [1.0])
    with pytest.raises(RuntimeError, match="has 1 bins, expected 2"):
        render_params(
            {14: {"path": spectrum, "integral_per_cm2": 1.0}},
            events=1,
            test_events=1,
            seed=1,
            target_a=40,
            target_z=18,
            processes=["cc"],
            energy_min_gev=0.0,
            energy_max_gev=2.0,
            energy_bins=2,
        )


def test_nuwro_mec_current_is_inferred_from_final_lepton():
    assert _reaction(2, [14, 1000180400, 13], [0, 0, 1]) == ("CC", "MEC")
    assert _reaction(2, [14, 1000180400, 14], [0, 0, 1]) == ("NC", "MEC")
    assert _reaction(100, [-14, 1000180400, -13], [0, 0, 1]) == ("CC", "HYP")


@pytest.mark.parametrize(
    ("production", "name", "distance_m"),
    [
        ("production.nuwro-bnb.yaml", "nuwro_bnb_v001", 110.0),
        ("production.nuwro-bnb_icarus.yaml", "nuwro_bnb_icarus_v001", 600.0),
    ],
)
def test_nuwro_profiles_dispatch_internal_generator(production, name, distance_m):
    config = load_config(Path("configs") / production)

    assert isinstance(config.source, NuWroSource)
    assert config.production.name == name
    assert config.source.flux.distance_m == distance_m
    assert config.source.flux.max_files == 32
    command = NuWroBackend().command(config, 0, JobLayout.for_job(config, 0))
    assert command[1:3] == ["-m", "dlpgen_opt.nuwro_cli"]
    assert command[command.index("--events") + 1] == "10"
    assert command[command.index("--test-events") + 1] == "100000"
    assert command[command.index("--seed") + 1] == "104741"
    process_start = command.index("--processes") + 1
    assert command[process_start : command.index("--vertex-cm")] == ["cc", "nc"]
