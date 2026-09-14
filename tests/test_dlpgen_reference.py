from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "configs" / "dlpgen" / "baseline_dune.yaml"


def test_dune_baseline_selects_exactly_one_cc_or_nc_interaction():
    with PROFILE.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)

    assert config["SEED"] == -1
    assert set(config) == {"SEED", "InteractionSelection", "CC", "NC"}
    assert config["InteractionSelection"] == {
        "Mode": "weighted_random",
    }
    assert config["CC"]["SelectionWeight"] == 1
    assert config["NC"]["SelectionWeight"] == 1
    assert config["CC"]["NumEvent"] == [1, 1]
    assert config["NC"]["NumEvent"] == [1, 1]
    assert config["CC"]["NumParticle"] == [1, 10]
    assert config["NC"]["NumParticle"] == [1, 9]
    assert config["CC"]["Particles"][0] == {
        "PDG": [11, -11, 13, -13],
        "NumRange": [1, 1],
        "KERange": [0.01, 15.0],
        "UseMom": False,
        "Weight": 1,
    }

    for name in ("CC", "NC"):
        block = config[name]
        assert block["XRange"] == [-3700, 3700]
        assert block["YRange"] == [-2368.713, 1031.287]
        assert block["ZRange"] == [3957.559, 9357.559]
        assert block["TRange"] == [0, 10000]
        assert block["AddParent"] is True
        assert all(particle["UseMom"] is False for particle in block["Particles"])
