from __future__ import annotations

import math
from pathlib import Path

import pytest

from dlpgen_opt.flux_cli import (
    RAY_DISK_RADIUS_CM,
    _direction,
    _flux_weights,
    _invalid_throw_fields,
    _uniform,
    _window_point_cm,
    catalog_digest,
    materialize,
    select_flux_files,
)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize(
    "field",
    ["energy_gev", "raw_ray_weight", "importance_weight", "ray_weight_per_cm2", "flux_weight_per_cm2"],
)
def test_invalid_throw_identifies_bad_field(field, value):
    values = dict(
        energy_gev=1.0,
        importance_weight=1.0,
        ray_weight_per_cm2=0.01,
        flux_weight_per_cm2=0.01,
    )
    values[field] = value
    assert _invalid_throw_fields(**values) == [field]


def test_signed_importance_weight_preserves_reported_bnb_throw():
    ray, flux = _flux_weights(
        6.165717877114254e-05, -0.005037239702252192, 0.999881, 1
    )
    assert ray == pytest.approx(-9.88613175167597e-12, rel=1e-12, abs=0)
    assert flux < 0
    assert _invalid_throw_fields(
        energy_gev=0.03193825112865286,
        raw_ray_weight=6.165717877114254e-05,
        importance_weight=-0.005037239702252192,
        ray_weight_per_cm2=ray,
        flux_weight_per_cm2=flux,
    ) == []
    assert _invalid_throw_fields(energy_gev=-1.0, raw_ray_weight=-1.0) == [
        "energy_gev", "raw_ray_weight"
    ]


def test_zero_energy_is_invalid_but_zero_weights_are_valid():
    assert _invalid_throw_fields(
        energy_gev=0.0,
        importance_weight=0.0,
        ray_weight_per_cm2=0.0,
        flux_weight_per_cm2=0.0,
    ) == ["energy_gev"]
    assert _invalid_throw_fields(
        energy_gev=1.0,
        importance_weight=0.0,
        ray_weight_per_cm2=0.0,
        flux_weight_per_cm2=0.0,
    ) == []


def test_flux_file_sampling_is_deterministic_bounded_and_catalog_identified():
    paths = [Path(f"beam-{index}.root") for index in range(20)]

    first = select_flux_files(paths, 104741, 4, sample=True)
    second = select_flux_files(list(reversed(paths)), 104741, 4, sample=True)

    assert first == second
    assert len(first) == 4
    assert catalog_digest(paths) == catalog_digest(list(reversed(paths)))
    assert first != select_flux_files(paths, 104742, 4, sample=True)


def test_counter_based_window_sampling_is_deterministic_and_bounded():
    first = _window_point_cm(
        seed=104729,
        file_index=2,
        entry=17,
        replica=0,
        distance_m=110.0,
        center_m=(2.0, -3.0),
        window_size_m=(4.0, 6.0),
    )
    second = _window_point_cm(
        seed=104729,
        file_index=2,
        entry=17,
        replica=0,
        distance_m=110.0,
        center_m=(2.0, -3.0),
        window_size_m=(4.0, 6.0),
    )

    assert first == second
    assert _uniform(104729, 2, 17, 0, 0) == 0.44747524981721476
    assert _uniform(104729, 2, 17, 0, 1) == 0.4230166096258904
    assert 0.0 <= first[0] < 400.0
    assert -600.0 <= first[1] < 0.0
    assert first[2] == 11000.0
    assert _uniform(1, 2, 3, 4, 0) != _uniform(1, 2, 3, 4, 1)


def test_direction_is_unit_vector_from_decay_to_window():
    direction = _direction((3.0, 4.0, 12.0), (0.0, 0.0, 0.0))

    assert direction == pytest.approx((3.0 / 13.0, 4.0 / 13.0, 12.0 / 13.0))
    assert math.sqrt(sum(value * value for value in direction)) == pytest.approx(1.0)


def test_flux_weight_has_importance_tilt_area_and_replica_factors():
    ray_density, plane_density = _flux_weights(
        raw_ray_weight=math.pi,
        importance_weight=2.0,
        direction_z=0.5,
        throws_per_decay=4,
    )

    assert ray_density == pytest.approx(2.0 / RAY_DISK_RADIUS_CM**2)
    assert plane_density == pytest.approx(ray_density / 8.0)


@pytest.mark.parametrize(
    ("point", "decay"),
    [((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)), ((math.inf, 0.0, 0.0), (0, 0, 0))],
)
def test_direction_rejects_degenerate_geometry(point, decay):
    with pytest.raises(RuntimeError):
        _direction(point, decay)


@pytest.mark.parametrize(
    ("flavors", "seed", "message"),
    [([13], 1, "neutrino PDG"), ([14], -1, "non-negative")],
)
def test_materialize_rejects_noncanonical_identity_inputs(flavors, seed, message):
    with pytest.raises(ValueError, match=message):
        materialize(
            flux_pattern=Path("unused.root"),
            output=Path("unused-output.root"),
            manifest_output=Path("unused-output.yaml"),
            distance_m=110.0,
            center_m=(0.0, 0.0),
            window_size_m=(1.0, 1.0),
            flavors=flavors,
            seed=seed,
        )
