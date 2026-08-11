"""Test mobile app timers."""

from http import HTTPStatus
from unittest.mock import ANY, AsyncMock, patch

from aiohttp.test_utils import TestClient
import pytest

from homeassistant.components.mobile_app import (
    DATA_DEVICE_COMMAND_MANAGER,
    DATA_DEVICES,
    DOMAIN,
)
from homeassistant.components.mobile_app.const import COMMAND_TIMER
from homeassistant.components.mobile_app.device_commands import DeviceCommandResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent as intent_helper

from .const import REGISTER


@pytest.mark.parametrize(
    ("intent_args", "message"),
    [
        (
            {},
            "0:02:00 timer finished",
        ),
        (
            {"name": {"value": "pizza"}},
            "pizza finished",
        ),
    ],
)
async def test_timer_events(
    hass: HomeAssistant, push_registration, intent_args: dict, message: str
) -> None:
    """Test for timer events."""
    webhook_id = push_registration["webhook_id"]
    device_id = hass.data[DOMAIN][DATA_DEVICES][webhook_id].id

    await intent_helper.async_handle(
        hass,
        "test",
        intent_helper.INTENT_START_TIMER,
        {
            "minutes": {"value": 2},
        }
        | intent_args,
        device_id=device_id,
    )

    with patch(
        "homeassistant.components.mobile_app.notify.MobileAppNotificationService.async_send_message"
    ) as mock_send_message:
        await intent_helper.async_handle(
            hass,
            "test",
            intent_helper.INTENT_DECREASE_TIMER,
            {
                "minutes": {"value": 2},
            },
            device_id=device_id,
        )
        await hass.async_block_till_done()

    assert mock_send_message.mock_calls[0][2] == {
        "target": [webhook_id],
        "message": message,
        "data": {
            "channel": "Timers",
            "group": "timers",
            "importance": "high",
            "ttl": 0,
            "priority": "high",
            "push": {
                "interruption-level": "time-sensitive",
            },
        },
    }


async def test_timer_started_on_native_mobile_device(
    hass: HomeAssistant, webhook_client: TestClient
) -> None:
    """Test that timer start events create a native mobile timer."""
    registration_response = await webhook_client.post(
        "/api/mobile_app/registrations",
        json={
            **REGISTER,
            "app_data": {
                "push_url": "http://localhost/mock-push",
                "push_token": "abcd",
                "supported_device_commands": [COMMAND_TIMER],
            },
        },
    )
    assert registration_response.status == HTTPStatus.CREATED
    registration = await registration_response.json()
    await hass.async_block_till_done()

    webhook_id = registration["webhook_id"]
    device_id = hass.data[DOMAIN][DATA_DEVICES][webhook_id].id
    manager = hass.data[DOMAIN][DATA_DEVICE_COMMAND_MANAGER]
    manager.async_send = AsyncMock(return_value=DeviceCommandResult(success=True))

    await intent_helper.async_handle(
        hass,
        "test",
        intent_helper.INTENT_START_TIMER,
        {"minutes": {"value": 2}, "name": {"value": "pizza"}},
        device_id=device_id,
    )
    await hass.async_block_till_done()

    manager.async_send.assert_awaited_once_with(
        webhook_id=webhook_id,
        device_id=device_id,
        command=COMMAND_TIMER,
        data={
            "timer_seconds": 120,
            "timer_skip_ui": True,
            "timer_message": "pizza",
        },
        context=ANY,
    )
    assert manager.async_send.await_args.kwargs["context"].user_id is not None

    with patch(
        "homeassistant.components.mobile_app.notify.MobileAppNotificationService.async_send_message"
    ) as mock_send_message:
        await intent_helper.async_handle(
            hass,
            "test",
            intent_helper.INTENT_DECREASE_TIMER,
            {"minutes": {"value": 2}},
            device_id=device_id,
        )
        await hass.async_block_till_done()

    mock_send_message.assert_not_called()
