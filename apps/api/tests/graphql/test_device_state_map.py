"""Every controller device state must survive the Device response model."""

from __future__ import annotations

import pytest
from unifi_api.graphql.pydantic_export import to_pydantic_model
from unifi_api.graphql.types.network.device import Device


@pytest.mark.parametrize("state", list(range(12)) + [99])
def test_every_device_state_validates_against_response_model(state: int) -> None:
    Model = to_pydantic_model(Device)
    device = Device.from_manager_output({"mac": "aa:bb:cc:dd:ee:01", "state": state})
    assert isinstance(device.state, str)
    Model.model_validate(device.to_dict())


@pytest.mark.parametrize(
    ("state", "label"),
    [(3, "firmware-mismatch"), (8, "deleting"), (10, "adoption-failed")],
)
def test_previously_unmapped_states_have_labels(state: int, label: str) -> None:
    assert Device.from_manager_output({"state": state}).state == label


def test_missing_state_stays_none() -> None:
    assert Device.from_manager_output({"mac": "aa:bb:cc:dd:ee:01"}).state is None
