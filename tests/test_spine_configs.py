from pathlib import Path

import yaml


REPOSITORY = Path(__file__).resolve().parents[1]


def test_generator_spine_configs_share_one_base_and_select_scheme():
    expected = {
        "dlpgen": "larsoft",
        "genie": "genie",
        "gibuu": "gibuu",
        "nuwro": "nuwro",
        "neut": "neut",
    }
    directory = REPOSITORY / "configs" / "spine"
    assert (directory / "base.yaml").is_file()

    for generator, scheme in expected.items():
        with (directory / f"{generator}.yaml").open(encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
        assert config["include"] == ["base.yaml"]
        assert (
            config["override"][
                "io.dataset.schema.neutrinos.interaction_scheme"
            ]
            == scheme
        )


def test_all_production_configs_enable_capped_spine_diagnostics():
    for path in sorted((REPOSITORY / "configs").glob("production*.yaml")):
        with path.open(encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
        assert config["software"]["spine"] == {
            "executable": "spine",
            "expected_commit": "b69f954eaadab1fa849400b082b7ca49c49aea25",
            "max_events": 100,
        }, path
