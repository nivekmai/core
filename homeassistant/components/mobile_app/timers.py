"""Timers for the mobile app."""

from datetime import timedelta
import logging

from homeassistant.components import notify
from homeassistant.components.intent import TimerEventType, TimerInfo
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE_ID, CONF_WEBHOOK_ID
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from . import device_action
from .const import (
    ATTR_APP_DATA,
    ATTR_SUPPORTED_DEVICE_COMMANDS,
    ATTR_TIMER_MESSAGE,
    ATTR_TIMER_SECONDS,
    ATTR_TIMER_SKIP_UI,
    COMMAND_TIMER,
    CONF_USER_ID,
    DATA_DEVICE_COMMAND_MANAGER,
    DOMAIN,
)
from .device_commands import DeviceCommandManager

_LOGGER = logging.getLogger(__name__)


@callback
def _supports_native_timer(entry: ConfigEntry) -> bool:
    """Return whether a mobile app registration supports native timers."""
    app_data = entry.data[ATTR_APP_DATA]
    commands = app_data.get(ATTR_SUPPORTED_DEVICE_COMMANDS)
    return isinstance(commands, list) and COMMAND_TIMER in commands


async def _async_start_native_timer(
    hass: HomeAssistant,
    entry: ConfigEntry,
    timer_info: TimerInfo,
    native_timer_ids: set[str],
) -> None:
    """Start a native timer on the originating mobile device."""
    if timer_info.device_id is None:
        return

    data: dict[str, int | str | bool] = {
        ATTR_TIMER_SECONDS: timer_info.created_seconds,
        ATTR_TIMER_SKIP_UI: True,
    }
    if timer_info.name:
        data[ATTR_TIMER_MESSAGE] = timer_info.name

    manager: DeviceCommandManager = hass.data[DOMAIN][DATA_DEVICE_COMMAND_MANAGER]
    try:
        result = await manager.async_send(
            webhook_id=entry.data[CONF_WEBHOOK_ID],
            device_id=timer_info.device_id,
            command=COMMAND_TIMER,
            data=data,
            context=Context(user_id=entry.data[CONF_USER_ID]),
        )
    except HomeAssistantError:
        _LOGGER.warning("Unable to start native timer on mobile device", exc_info=True)
        return

    if result.success:
        native_timer_ids.add(timer_info.id)
    else:
        _LOGGER.warning("Mobile device failed to start native timer")


@callback
def async_handle_timer_event(
    hass: HomeAssistant,
    entry: ConfigEntry,
    native_timer_ids: set[str],
    event_type: TimerEventType,
    timer_info: TimerInfo,
) -> None:
    """Handle timer events."""
    if event_type == TimerEventType.STARTED and _supports_native_timer(entry):
        entry.async_create_task(
            hass,
            _async_start_native_timer(hass, entry, timer_info, native_timer_ids),
            "mobile_app_native_timer",
        )
        return

    if event_type != TimerEventType.FINISHED:
        if event_type == TimerEventType.CANCELLED:
            native_timer_ids.discard(timer_info.id)
        return

    if timer_info.id in native_timer_ids:
        native_timer_ids.discard(timer_info.id)
        return

    if timer_info.name:
        message = f"{timer_info.name} finished"
    else:
        message = f"{timedelta(seconds=timer_info.created_seconds)} timer finished"

    entry.async_create_task(
        hass,
        device_action.async_call_action_from_config(
            hass,
            {
                CONF_DEVICE_ID: timer_info.device_id,
                notify.ATTR_MESSAGE: message,
                notify.ATTR_DATA: {
                    "group": "timers",
                    # Android
                    "channel": "Timers",
                    "importance": "high",
                    "ttl": 0,
                    "priority": "high",
                    # iOS
                    "push": {
                        "interruption-level": "time-sensitive",
                    },
                },
            },
            {},
            None,
        ),
        "mobile_app_timer_notification",
    )
