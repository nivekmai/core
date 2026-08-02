"""Tests for mobile app LLM tools."""

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components import llm as llm_component
from homeassistant.components.mobile_app import llm as mobile_app_llm
from homeassistant.components.mobile_app.const import (
    ATTR_ALARM_HOUR,
    ATTR_ALARM_MESSAGE,
    ATTR_ALARM_MINUTE,
    ATTR_ALARM_SKIP_UI,
    ATTR_APP_DATA,
    ATTR_COMMAND_SUCCESS,
    ATTR_HASS_COMMAND_ID,
    ATTR_PUSH_TOKEN,
    ATTR_PUSH_URL,
    ATTR_PUSH_WEBSOCKET_CHANNEL,
    ATTR_SUPPORTED_DEVICE_COMMANDS,
    ATTR_TIMER_MESSAGE,
    ATTR_TIMER_SECONDS,
    ATTR_TIMER_SKIP_UI,
    COMMAND_ALARM,
    COMMAND_TIMER,
    DATA_DEVICES,
    DOMAIN,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component

from tests.common import MockConfigEntry, MockUser
from tests.typing import MockHAClientWebSocket, WebSocketGenerator

WEBHOOK_ID = "alarm-webhook-id"


@dataclass(frozen=True, slots=True)
class AlarmRegistration:
    """Test mobile app registration supporting alarm commands."""

    entry: MockConfigEntry
    device_id: str
    user_id: str
    webhook_id: str


@pytest.fixture
async def alarm_registration(
    hass: HomeAssistant,
    hass_admin_user: MockUser,
) -> AlarmRegistration:
    """Set up a mobile app registration supporting alarm commands."""
    entry = MockConfigEntry(
        data={
            ATTR_APP_DATA: {
                ATTR_PUSH_WEBSOCKET_CHANNEL: True,
                ATTR_SUPPORTED_DEVICE_COMMANDS: [COMMAND_ALARM],
            },
            "app_id": "io.homeassistant.companion.android",
            "app_name": "Home Assistant",
            "app_version": "1.0",
            "device_id": "alarm-registration-device-id",
            "device_name": "Alarm phone",
            "manufacturer": "Test",
            "model": "Phone",
            "os_name": "Android",
            "os_version": "1.0",
            "secret": "secret",
            "supports_encryption": False,
            "user_id": hass_admin_user.id,
            "webhook_id": WEBHOOK_ID,
        },
        domain=DOMAIN,
        source="registration",
        title="Alarm phone",
        version=1,
    )
    entry.add_to_hass(hass)

    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {}})
    assert await async_setup_component(hass, "llm", {})
    await hass.async_block_till_done()

    return AlarmRegistration(
        entry=entry,
        device_id=hass.data[DOMAIN][DATA_DEVICES][WEBHOOK_ID].id,
        user_id=hass_admin_user.id,
        webhook_id=WEBHOOK_ID,
    )


def _llm_context(registration: AlarmRegistration) -> llm.LLMContext:
    """Return an LLM context for the registered mobile app."""
    return llm.LLMContext(
        platform="test_platform",
        context=Context(user_id=registration.user_id),
        language="*",
        assistant="conversation",
        device_id=registration.device_id,
    )


def _set_capabilities(
    hass: HomeAssistant,
    registration: AlarmRegistration,
    commands: list[str],
) -> None:
    """Set the device command capabilities for a registration."""
    hass.config_entries.async_update_entry(
        registration.entry,
        data={
            **registration.entry.data,
            ATTR_APP_DATA: {
                **registration.entry.data[ATTR_APP_DATA],
                ATTR_SUPPORTED_DEVICE_COMMANDS: commands,
            },
        },
    )


async def _subscribe_to_push(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    webhook_id: str = WEBHOOK_ID,
    *,
    support_confirm: bool = False,
) -> MockHAClientWebSocket:
    """Subscribe to a registration's local push channel."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "mobile_app/push_notification_channel",
            "webhook_id": webhook_id,
            "support_confirm": support_confirm,
        }
    )
    assert (await client.receive_json())["success"]
    return client


async def test_alarm_tool_capability_gating(
    hass: HomeAssistant, alarm_registration: AlarmRegistration
) -> None:
    """Test alarm tool is only offered to its capable originating registration."""
    llm_context = _llm_context(alarm_registration)

    tools = mobile_app_llm.async_get_tools(hass, llm_context, "assist")
    assert tools is not None
    assert [tool.name for tool in tools.tools] == ["mobile_app_set_alarm"]
    assert tools.prompt is not None
    assert "mobile_app_set_alarm" in tools.prompt

    combined_tools = await llm_component.async_get_tools(hass, llm_context, "assist")
    assert "mobile_app_set_alarm" in {tool.name for tool in combined_tools.tools}

    assert mobile_app_llm.async_get_tools(hass, llm_context, "other") is None
    assert (
        mobile_app_llm.async_get_tools(
            hass,
            llm.LLMContext(
                platform="test_platform",
                context=Context(user_id="another-user"),
                language="*",
                assistant="conversation",
                device_id=alarm_registration.device_id,
            ),
            "assist",
        )
        is None
    )

    hass.config_entries.async_update_entry(
        alarm_registration.entry,
        data={
            **alarm_registration.entry.data,
            ATTR_APP_DATA: {ATTR_PUSH_WEBSOCKET_CHANNEL: True},
        },
    )
    assert mobile_app_llm.async_get_tools(hass, llm_context, "assist") is None

    hass.config_entries.async_update_entry(
        alarm_registration.entry,
        data={
            **alarm_registration.entry.data,
            ATTR_APP_DATA: {
                ATTR_PUSH_WEBSOCKET_CHANNEL: False,
                ATTR_SUPPORTED_DEVICE_COMMANDS: [COMMAND_ALARM],
            },
        },
    )
    assert mobile_app_llm.async_get_tools(hass, llm_context, "assist") is None


@pytest.mark.parametrize(
    ("commands", "expected_tools"),
    [
        pytest.param([COMMAND_ALARM], ["mobile_app_set_alarm"], id="alarm-only"),
        pytest.param([COMMAND_TIMER], ["mobile_app_set_timer"], id="timer-only"),
        pytest.param(
            [COMMAND_ALARM, COMMAND_TIMER],
            ["mobile_app_set_alarm", "mobile_app_set_timer"],
            id="alarm-and-timer",
        ),
        pytest.param([], [], id="neither"),
        pytest.param(["unknown_command"], [], id="unknown-command"),
    ],
)
async def test_clock_tools_are_independently_capability_gated(
    hass: HomeAssistant,
    alarm_registration: AlarmRegistration,
    commands: list[str],
    expected_tools: list[str],
) -> None:
    """Test alarm and native timer capabilities expose only their own tools."""
    _set_capabilities(hass, alarm_registration, commands)

    result = mobile_app_llm.async_get_tools(
        hass, _llm_context(alarm_registration), "assist"
    )

    assert ([] if result is None else [tool.name for tool in result.tools]) == (
        expected_tools
    )


async def test_native_timer_prompt_disambiguates_home_assistant_timer(
    hass: HomeAssistant, alarm_registration: AlarmRegistration
) -> None:
    """Test native timer guidance deterministically selects the phone tool."""
    assert await async_setup_component(hass, "homeassistant", {})
    _set_capabilities(hass, alarm_registration, [COMMAND_TIMER])
    llm_context = _llm_context(alarm_registration)

    result = await llm_component.async_get_tools(hass, llm_context, "assist")
    tool_names = {tool.name for tool in result.tools}

    assert "mobile_app_set_timer" in tool_names
    assert "HassStartTimer" in tool_names
    assert result.prompt is not None
    assert (
        "call mobile_app_set_timer instead of HassStartTimer, and never call both"
        in result.prompt
    )
    assert "Home Assistant-managed timer" in result.prompt


async def test_registration_without_native_timer_keeps_home_assistant_timer(
    hass: HomeAssistant, alarm_registration: AlarmRegistration
) -> None:
    """Test existing registrations keep the Home Assistant timer behavior."""
    assert await async_setup_component(hass, "homeassistant", {})
    llm_context = _llm_context(alarm_registration)

    result = await llm_component.async_get_tools(hass, llm_context, "assist")
    tool_names = {tool.name for tool in result.tools}

    assert "mobile_app_set_timer" not in tool_names
    assert "HassStartTimer" in tool_names


async def test_set_alarm_success(
    hass: HomeAssistant,
    alarm_registration: AlarmRegistration,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test setting an alarm and receiving its execution result."""
    client = await _subscribe_to_push(hass, hass_ws_client)
    llm_context = _llm_context(alarm_registration)
    platform = mobile_app_llm.async_get_tools(hass, llm_context, "assist")
    assert platform is not None
    tool = platform.tools[0]

    task = hass.async_create_task(
        tool.async_call(
            hass,
            llm.ToolInput(
                tool_name="mobile_app_set_alarm",
                tool_args={"hour": 20, "minute": 41, "label": "Wake up"},
            ),
            llm_context,
        )
    )

    notification = (await client.receive_json())["event"]
    assert notification["message"] == COMMAND_ALARM
    command_id = notification["data"].pop(ATTR_HASS_COMMAND_ID)
    assert notification["data"] == {
        ATTR_ALARM_HOUR: 20,
        ATTR_ALARM_MINUTE: 41,
        ATTR_ALARM_SKIP_UI: True,
        ATTR_ALARM_MESSAGE: "Wake up",
    }

    await client.send_json_auto_id(
        {
            "type": "mobile_app/command_result",
            "webhook_id": WEBHOOK_ID,
            ATTR_HASS_COMMAND_ID: command_id,
            ATTR_COMMAND_SUCCESS: True,
        }
    )
    assert (await client.receive_json())["success"]
    assert await task == {"success": True}


async def test_set_alarm_survives_cloud_fallback(
    hass: HomeAssistant,
    alarm_registration: AlarmRegistration,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test cloud fallback leaves enough time to report command execution."""
    push_url = "https://mobile-push.home-assistant.dev/push"
    hass.config_entries.async_update_entry(
        alarm_registration.entry,
        data={
            **alarm_registration.entry.data,
            ATTR_APP_DATA: {
                ATTR_PUSH_TOKEN: "PUSH_TOKEN",
                ATTR_PUSH_URL: push_url,
                ATTR_PUSH_WEBSOCKET_CHANNEL: True,
                ATTR_SUPPORTED_DEVICE_COMMANDS: [COMMAND_ALARM],
            },
        },
    )
    client = await _subscribe_to_push(hass, hass_ws_client, support_confirm=True)
    llm_context = _llm_context(alarm_registration)
    platform = mobile_app_llm.async_get_tools(hass, llm_context, "assist")
    assert platform is not None

    with (
        patch(
            "homeassistant.components.mobile_app.push_notification."
            "PUSH_CONFIRM_TIMEOUT",
            0,
        ),
        patch(
            "homeassistant.components.mobile_app.notify._send_message",
            new_callable=AsyncMock,
        ) as send_remote,
    ):
        task = asyncio.create_task(
            platform.tools[0].async_call(
                hass,
                llm.ToolInput(
                    tool_name="mobile_app_set_alarm",
                    tool_args={"hour": 20, "minute": 41},
                ),
                llm_context,
            )
        )
        notification = (await client.receive_json())["event"]
        await hass.async_block_till_done()
        await hass.async_block_till_done()

    command_id = notification["data"][ATTR_HASS_COMMAND_ID]
    send_remote.assert_awaited_once()
    assert send_remote.await_args.args[2]["data"][ATTR_HASS_COMMAND_ID] == command_id

    await client.send_json_auto_id(
        {
            "type": "mobile_app/command_result",
            "webhook_id": WEBHOOK_ID,
            ATTR_HASS_COMMAND_ID: command_id,
            ATTR_COMMAND_SUCCESS: True,
        }
    )
    assert (await client.receive_json())["success"]
    assert await task == {"success": True}


async def test_set_alarm_failure(
    hass: HomeAssistant,
    alarm_registration: AlarmRegistration,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test a failure reported by the mobile app."""
    client = await _subscribe_to_push(hass, hass_ws_client)
    llm_context = _llm_context(alarm_registration)
    platform = mobile_app_llm.async_get_tools(hass, llm_context, "assist")
    assert platform is not None

    task = hass.async_create_task(
        platform.tools[0].async_call(
            hass,
            llm.ToolInput(
                tool_name="mobile_app_set_alarm",
                tool_args={"hour": 8, "minute": 15},
            ),
            llm_context,
        )
    )
    notification = (await client.receive_json())["event"]

    await client.send_json_auto_id(
        {
            "type": "mobile_app/command_result",
            "webhook_id": WEBHOOK_ID,
            ATTR_HASS_COMMAND_ID: notification["data"][ATTR_HASS_COMMAND_ID],
            ATTR_COMMAND_SUCCESS: False,
        }
    )
    assert (await client.receive_json())["success"]
    with pytest.raises(HomeAssistantError) as err:
        await task
    assert str(err.value) == "The requesting mobile app could not set the alarm"


async def test_set_native_timer_success(
    hass: HomeAssistant,
    alarm_registration: AlarmRegistration,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test setting a native timer and receiving its execution result."""
    _set_capabilities(hass, alarm_registration, [COMMAND_TIMER])
    client = await _subscribe_to_push(hass, hass_ws_client)
    llm_context = _llm_context(alarm_registration)
    platform = mobile_app_llm.async_get_tools(hass, llm_context, "assist")
    assert platform is not None
    tool = platform.tools[0]
    assert tool.name == "mobile_app_set_timer"

    task = hass.async_create_task(
        tool.async_call(
            hass,
            llm.ToolInput(
                tool_name="mobile_app_set_timer",
                tool_args={"duration_seconds": 300, "label": "Tea"},
            ),
            llm_context,
        )
    )

    notification = (await client.receive_json())["event"]
    assert notification["message"] == COMMAND_TIMER
    command_id = notification["data"].pop(ATTR_HASS_COMMAND_ID)
    assert notification["data"] == {
        ATTR_TIMER_SECONDS: 300,
        ATTR_TIMER_SKIP_UI: True,
        ATTR_TIMER_MESSAGE: "Tea",
    }

    await client.send_json_auto_id(
        {
            "type": "mobile_app/command_result",
            "webhook_id": WEBHOOK_ID,
            ATTR_HASS_COMMAND_ID: command_id,
            ATTR_COMMAND_SUCCESS: True,
        }
    )
    assert (await client.receive_json())["success"]
    assert await task == {"success": True}


async def test_set_native_timer_failure(
    hass: HomeAssistant,
    alarm_registration: AlarmRegistration,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test a native timer failure reported by the mobile app."""
    _set_capabilities(hass, alarm_registration, [COMMAND_TIMER])
    client = await _subscribe_to_push(hass, hass_ws_client)
    llm_context = _llm_context(alarm_registration)
    platform = mobile_app_llm.async_get_tools(hass, llm_context, "assist")
    assert platform is not None

    task = hass.async_create_task(
        platform.tools[0].async_call(
            hass,
            llm.ToolInput(
                tool_name="mobile_app_set_timer",
                tool_args={"duration_seconds": 90},
            ),
            llm_context,
        )
    )
    notification = (await client.receive_json())["event"]

    await client.send_json_auto_id(
        {
            "type": "mobile_app/command_result",
            "webhook_id": WEBHOOK_ID,
            ATTR_HASS_COMMAND_ID: notification["data"][ATTR_HASS_COMMAND_ID],
            ATTR_COMMAND_SUCCESS: False,
        }
    )
    assert (await client.receive_json())["success"]
    with pytest.raises(HomeAssistantError) as err:
        await task
    assert str(err.value) == "The requesting mobile app could not set the timer"


async def test_native_timer_tool_revalidates_capability(
    hass: HomeAssistant, alarm_registration: AlarmRegistration
) -> None:
    """Test a timer capability is checked again when the tool is called."""
    _set_capabilities(hass, alarm_registration, [COMMAND_TIMER])
    llm_context = _llm_context(alarm_registration)
    platform = mobile_app_llm.async_get_tools(hass, llm_context, "assist")
    assert platform is not None
    tool = platform.tools[0]

    _set_capabilities(hass, alarm_registration, [])

    with pytest.raises(
        HomeAssistantError,
        match="does not support setting timers",
    ):
        await tool.async_call(
            hass,
            llm.ToolInput(
                tool_name="mobile_app_set_timer",
                tool_args={"duration_seconds": 90},
            ),
            llm_context,
        )


async def test_set_alarm_route_failure_is_sanitized(
    hass: HomeAssistant, alarm_registration: AlarmRegistration
) -> None:
    """Test a notification routing failure does not expose registration details."""
    llm_context = _llm_context(alarm_registration)
    platform = mobile_app_llm.async_get_tools(hass, llm_context, "assist")
    assert platform is not None

    with pytest.raises(HomeAssistantError) as err:
        await platform.tools[0].async_call(
            hass,
            llm.ToolInput(
                tool_name="mobile_app_set_alarm",
                tool_args={"hour": 8, "minute": 15},
            ),
            llm_context,
        )

    assert str(err.value) == "Unable to route the command to the requesting mobile app"


async def test_set_alarm_registration_unloaded(
    hass: HomeAssistant,
    alarm_registration: AlarmRegistration,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test unloading the target registration returns a normal tool error."""
    client = await _subscribe_to_push(hass, hass_ws_client)
    llm_context = _llm_context(alarm_registration)
    platform = mobile_app_llm.async_get_tools(hass, llm_context, "assist")
    assert platform is not None

    task = hass.async_create_task(
        platform.tools[0].async_call(
            hass,
            llm.ToolInput(
                tool_name="mobile_app_set_alarm",
                tool_args={"hour": 8, "minute": 15},
            ),
            llm_context,
        )
    )
    await client.receive_json()

    assert await hass.config_entries.async_unload(alarm_registration.entry.entry_id)

    with pytest.raises(
        HomeAssistantError,
        match="The requesting mobile app became unavailable",
    ):
        await task


async def test_command_result_requires_target_registration(
    hass: HomeAssistant,
    alarm_registration: AlarmRegistration,
    hass_admin_user: MockUser,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test a command result is accepted only from its target registration."""
    other_webhook_id = "other-alarm-webhook-id"
    other_entry = MockConfigEntry(
        data={
            ATTR_APP_DATA: {ATTR_PUSH_WEBSOCKET_CHANNEL: True},
            "app_id": "io.homeassistant.companion.android",
            "app_name": "Home Assistant",
            "app_version": "1.0",
            "device_id": "other-registration-device-id",
            "device_name": "Other phone",
            "manufacturer": "Test",
            "model": "Phone",
            "os_name": "Android",
            "os_version": "1.0",
            "secret": "other-secret",
            "supports_encryption": False,
            "user_id": hass_admin_user.id,
            "webhook_id": other_webhook_id,
        },
        domain=DOMAIN,
        source="registration",
        title="Other phone",
        version=1,
    )
    other_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(other_entry.entry_id)
    await hass.async_block_till_done()

    client = await _subscribe_to_push(hass, hass_ws_client)
    llm_context = _llm_context(alarm_registration)
    platform = mobile_app_llm.async_get_tools(hass, llm_context, "assist")
    assert platform is not None
    task = hass.async_create_task(
        platform.tools[0].async_call(
            hass,
            llm.ToolInput(
                tool_name="mobile_app_set_alarm",
                tool_args={"hour": 6, "minute": 30},
            ),
            llm_context,
        )
    )
    notification = (await client.receive_json())["event"]
    command_id = notification["data"][ATTR_HASS_COMMAND_ID]

    await client.send_json_auto_id(
        {
            "type": "mobile_app/command_result",
            "webhook_id": other_webhook_id,
            ATTR_HASS_COMMAND_ID: command_id,
            ATTR_COMMAND_SUCCESS: True,
        }
    )
    result = await client.receive_json()
    assert not result["success"]
    assert result["error"]["code"] == "not_found"
    assert not task.done()

    await client.send_json_auto_id(
        {
            "type": "mobile_app/command_result",
            "webhook_id": WEBHOOK_ID,
            ATTR_HASS_COMMAND_ID: command_id,
            ATTR_COMMAND_SUCCESS: True,
        }
    )
    assert (await client.receive_json())["success"]
    assert await task == {"success": True}


async def test_set_alarm_timeout_and_late_result(
    hass: HomeAssistant,
    alarm_registration: AlarmRegistration,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test command timeout cleanup and rejection of a late result."""
    client = await _subscribe_to_push(hass, hass_ws_client)
    llm_context = _llm_context(alarm_registration)
    platform = mobile_app_llm.async_get_tools(hass, llm_context, "assist")
    assert platform is not None

    with patch(
        "homeassistant.components.mobile_app.device_commands.DEVICE_COMMAND_TIMEOUT",
        0,
    ):
        task = hass.async_create_task(
            platform.tools[0].async_call(
                hass,
                llm.ToolInput(
                    tool_name="mobile_app_set_alarm",
                    tool_args={"hour": 7, "minute": 0},
                ),
                llm_context,
            )
        )
        notification = (await client.receive_json())["event"]
        with pytest.raises(
            HomeAssistantError,
            match="did not report the command result",
        ):
            await task

    await client.send_json_auto_id(
        {
            "type": "mobile_app/command_result",
            "webhook_id": WEBHOOK_ID,
            ATTR_HASS_COMMAND_ID: notification["data"][ATTR_HASS_COMMAND_ID],
            ATTR_COMMAND_SUCCESS: True,
        }
    )
    result = await client.receive_json()
    assert not result["success"]
    assert result["error"]["code"] == "not_found"
