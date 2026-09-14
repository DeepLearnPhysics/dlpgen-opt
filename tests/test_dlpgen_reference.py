from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "configs" / "dlpgen" / "mpvmpr_dune.yaml"


def test_dune_mpvmpr_reference_profile_preserves_current_distribution():
    with PROFILE.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)

    assert config["SEED"] == -1
    assert set(config) == {"SEED", "Generator1", "Generator2", "Singles"}
    assert config["Generator1"]["NumEvent"] == [5, 15]
    assert config["Generator1"]["NumParticle"] == [1, 10]
    assert config["Generator1"]["Particles"][0] == {
        "PDG": [11, -11, 13, -13],
        "NumRange": [1, 1],
        "KERange": [0.01, 15.0],
        "UseMom": False,
        "Weight": 1,
    }
    assert config["Generator2"]["NumEvent"] == [3, 7]
    assert config["Generator2"]["NumParticle"] == [1, 9]
    assert config["Singles"]["NumEvent"] == [30, 50]
    assert config["Singles"]["NumParticle"] == [1, 1]

    for name in ("Generator1", "Generator2", "Singles"):
        block = config[name]
        assert block["XRange"] == [-3700, 3700]
        assert block["YRange"] == [-2368.713, 1031.287]
        assert block["ZRange"] == [3957.559, 9357.559]
        assert block["TRange"] == [0, 10000]
        assert all(particle["UseMom"] is False for particle in block["Particles"])

    assert config["Generator1"]["AddParent"] is True
    assert config["Generator2"]["AddParent"] is True
    assert "AddParent" not in config["Singles"]
    assert config["Singles"]["Particles"][1]["PDG"] == [-13]
    assert config["Singles"]["Particles"][1]["KERange"] == [0.1, 0.5]
