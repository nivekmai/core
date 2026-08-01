"""LLM tools for registered mobile apps."""
# pylint: disable=home-assistant-use-runtime-data  # Uses legacy hass.data[DOMAIN] pattern

from typing import Any, override

import voluptuous as vol

from homeassistant.components.llm import LLMTools
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.llm import LLM_API_ASSIST, LLMContext, Tool, ToolInput
from homeassistant.util.json import JsonObjectType

from .const import (
    ATTR_ALARM_HOUR,
    ATTR_ALARM_MESSAGE,
    ATTR_ALARM_MINUTE,
    ATTR_ALARM_SKIP_UI,
    ATTR_APP_DATA,
    ATTR_PUSH_TOKEN,
    ATTR_PUSH_URL,
    ATTR_PUSH_WEBSOCKET_CHANNEL,
    ATTR_SUPPORTED_DEVICE_COMMANDS,
    COMMAND_ALARM,
    CONF_USER_ID,
    DATA_CONFIG_ENTRIES,
    DATA_DEVICE_COMMAND_MANAGER,
    DOMAIN,
)
from .device_commands import DeviceCommandManager
from .util import webhook_id_from_device_id


def _supports_push(app_data: dict[str, Any]) -> bool:
    """Return whether registration data describes a usable push path."""
    return bool(
        app_data.get(ATTR_PUSH_WEBSOCKET_CHANNEL)
        or (app_data.get(ATTR_PUSH_TOKEN) and app_data.get(ATTR_PUSH_URL))
    )


@callback
def _get_capable_device(
    hass: HomeAssistant, llm_context: LLMContext, command: str
) -> tuple[str, str, Context] | None:
    """Return the originating registration when it can execute a command."""
    if (
        llm_context.device_id is None
        or llm_context.context is None
        or llm_context.context.user_id is None
        or (webhook_id := webhook_id_from_device_id(hass, llm_context.device_id))
        is None
    ):
        return None

    config_entry = hass.data[DOMAIN][DATA_CONFIG_ENTRIES].get(webhook_id)
    if (
        config_entry is None
        or config_entry.data[CONF_USER_ID] != llm_context.context.user_id
    ):
        return None

    app_data = config_entry.data[ATTR_APP_DATA]
    supported_device_commands = app_data.get(ATTR_SUPPORTED_DEVICE_COMMANDS)
    if (
        not isinstance(supported_device_commands, list)
        or command not in supported_device_commands
        or not _supports_push(app_data)
    ):
        return None

    return webhook_id, llm_context.device_id, llm_context.context


class SetPhoneAlarmTool(Tool):
    """Set an alarm on the mobile device that initiated Assist."""

    name = "mobile_app_set_alarm"
    description = (
        "Set an alarm at a specific local clock time on the phone that initiated "
        "this Assist request."
    )
    parameters = vol.Schema(
        {
            vol.Required("hour", description="Hour in 24-hour time"): vol.All(
                int, vol.Range(min=0, max=23)
            ),
            vol.Required("minute", description="Minute of the hour"): vol.All(
                int, vol.Range(min=0, max=59)
            ),
            vol.Optional("label", description="Optional alarm label"): vol.All(
                cv.string, vol.Length(max=256)
            ),
        }
    )

    @override
    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: ToolInput,
        llm_context: LLMContext,
    ) -> JsonObjectType:
        """Set an alarm and wait for the phone to report execution."""
        if (
            capable_device := _get_capable_device(hass, llm_context, COMMAND_ALARM)
        ) is None:
            raise HomeAssistantError(
                "The requesting mobile app does not support setting alarms"
            )

        args = self.parameters(tool_input.tool_args)
        webhook_id, device_id, context = capable_device
        command_data: dict[str, int | str | bool] = {
            ATTR_ALARM_HOUR: args["hour"],
            ATTR_ALARM_MINUTE: args["minute"],
            ATTR_ALARM_SKIP_UI: True,
        }
        if label := args.get("label"):
            command_data[ATTR_ALARM_MESSAGE] = label

        manager: DeviceCommandManager = hass.data[DOMAIN][DATA_DEVICE_COMMAND_MANAGER]
        result = await manager.async_send(
            webhook_id=webhook_id,
            device_id=device_id,
            command=COMMAND_ALARM,
            data=command_data,
            context=context,
        )
        if not result.success:
            raise HomeAssistantError(
                "The requesting mobile app could not set the alarm"
            )

        return {"success": True}


@callback
def async_get_tools(
    hass: HomeAssistant, llm_context: LLMContext, api_id: str
) -> LLMTools | None:
    """Return tools supported by the mobile app that initiated Assist."""
    if (
        api_id != LLM_API_ASSIST
        or _get_capable_device(hass, llm_context, COMMAND_ALARM) is None
    ):
        return None

    return LLMTools(
        tools=[SetPhoneAlarmTool()],
        prompt=(
            "When the user asks to set an alarm at a specific clock time on this "
            "phone, call mobile_app_set_alarm. The alarm is created by the Clock app on "
            "the device that initiated Assist."
        ),
    )
