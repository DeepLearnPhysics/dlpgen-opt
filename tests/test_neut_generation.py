from __future__ import annotations

from pathlib import Path

import pytest

from dlpgen_opt.config import NeutSource, load_config
from dlpgen_opt.layout import JobLayout
from dlpgen_opt.neut_cli import allocate_events, random_seeds, render_card
from dlpgen_opt.sources.neut import NeutBackend


BASE_CARD = """C test card
EVCT-NEVT 100
EVCT-IDPT 14
EVCT-MPV 1
NEUT-NUMBNDN 22
NEUT-NUMBNDP 18
NEUT-NUMFREP 0
NEUT-NUMATOM 40
NEUT-MDLQE 02
NEUT-RAND 1
NEUT-CRSPATH '../crsdat'
"""


def test_render_card_configures_flux_target_models_and_seed_file():
    card = render_card(
        BASE_CARD,
        events=10,
        pdg=-14,
        flux_filename="flux.root",
        flux_histogram="flux_m14",
        target_a=40,
        target_z=18,
        processes=["cc", "nc"],
        mdlqe=2002,
        mdl2p2h=1,
    )

    assert "EVCT-NEVT 10" in card
    assert "EVCT-IDPT -14" in card
    assert "EVCT-MPV 3" in card
    assert "EVCT-FILENM 'flux.root'" in card
    assert "EVCT-HISTNM 'flux_m14'" in card
    assert "NEUT-MDLQE 2002" in card
    assert "NEUT-MDL2P2H 1" in card
    assert "NEUT-RAND 0" in card
    assert "CNEUT-CRSPATH '../crsdat'" in card
    assert "NEUT-CRS " + " ".join(["1."] * 30) in card
    assert "NEUT-CRSB " + " ".join(["1."] * 30) in card


def test_neut_flavor_allocation_is_exact_and_deterministic():
    normalization = {
        "flavors": {
            "12": {"rate_weight": 1.0},
            "14": {"rate_weight": 9.0},
        }
    }
    first = allocate_events(normalization, 100, 17)
    second = allocate_events(normalization, 100, 17)

    assert first == second
    assert sum(first.values()) == 100
    assert first[14] > first[12]
    assert random_seeds(17, 14, "events") == random_seeds(17, 14, "events")
    assert random_seeds(17, 14, "events") != random_seeds(17, 14, "normalization")


def test_render_card_masks_neutral_current_channels():
    card = render_card(
        BASE_CARD,
        events=1,
        pdg=14,
        flux_filename="flux.root",
        flux_histogram="flux_p14",
        target_a=40,
        target_z=18,
        processes=["cc"],
        mdlqe=2002,
        mdl2p2h=1,
    )
    factors = next(line for line in card.splitlines() if line.startswith("NEUT-CRS "))
    values = factors.split()[1:]
    assert values[0] == "1."
    assert values[5] == "0."
    assert values[27] == "1."


@pytest.mark.parametrize(
    ("production", "name", "distance_m"),
    [
        ("production.neut-bnb.yaml", "neut_bnb_v001", 110.0),
        ("production.neut-bnb_icarus.yaml", "neut_bnb_icarus_v001", 600.0),
    ],
)
def test_neut_profiles_dispatch_internal_generator(production, name, distance_m):
    config = load_config(Path("configs") / production)

    assert isinstance(config.source, NeutSource)
    assert config.production.name == name
    assert config.source.flux.distance_m == distance_m
    command = NeutBackend().command(config, 0, JobLayout.for_job(config, 0))
    assert command[1:3] == ["-m", "dlpgen_opt.neut_cli"]
    assert command[command.index("--events") + 1] == "10"
    assert command[command.index("--seed") + 1] == "104741"
    assert command[command.index("--mdlqe") + 1] == "2002"
    assert command[command.index("--mdl2p2h") + 1] == "1"
