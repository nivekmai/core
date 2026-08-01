"""Coordinate commands executed by a registered mobile app."""

import asyncio
from dataclasses import dataclass
from typing import Any

from homeassistant.components import notify
from homeassistant.const import ATTR_DEVICE_ID, CONF_DOMAIN, CONF_TYPE
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.ulid import ulid_now

from . import device_action
from .const import ATTR_HASS_COMMAND_ID, DOMAIN

DEVICE_COMMAND_TIMEOUT = 30


@dataclass(frozen=True, slots=True)
class DeviceCommandResult:
    """Result reported by a mobile app."""

    success: bool


@dataclass(slots=True)
class _PendingCommand:
    """Command waiting for a result from one mobile app registration."""

    webhook_id: str
    future: asyncio.Future[DeviceCommandResult]


class DeviceCommandManager:
    """Dispatch mobile app commands and correlate their results."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the manager."""
        self._hass = hass
        self._pending: dict[str, _PendingCommand] = {}

    async def async_send(
        self,
        *,
        webhook_id: str,
        device_id: str,
        command: str,
        data: dict[str, Any],
        context: Context,
    ) -> DeviceCommandResult:
        """Send a command and wait for the target registration to report its result."""
        command_id = ulid_now()
        while command_id in self._pending:
            command_id = ulid_now()

        future: asyncio.Future[DeviceCommandResult] = self._hass.loop.create_future()
        self._pending[command_id] = _PendingCommand(webhook_id, future)

        try:
            try:
                await device_action.async_call_action_from_config(
                    self._hass,
                    device_action.ACTION_SCHEMA(
                        {
                            ATTR_DEVICE_ID: device_id,
                            CONF_DOMAIN: DOMAIN,
                            CONF_TYPE: "notify",
                            notify.ATTR_MESSAGE: command,
                            notify.ATTR_DATA: {
                                **data,
                                ATTR_HASS_COMMAND_ID: command_id,
                            },
                        }
                    ),
                    variables={},
                    context=context,
                )
            except HomeAssistantError as err:
                raise HomeAssistantError(
                    "Unable to route the command to the requesting mobile app"
                ) from err

            try:
                return await asyncio.wait_for(
                    asyncio.shield(future), timeout=DEVICE_COMMAND_TIMEOUT
                )
            except TimeoutError as err:
                raise HomeAssistantError(
                    "The requesting mobile app did not report the command result"
                ) from err
        finally:
            pending = self._pending.get(command_id)
            if pending is not None and pending.future is future:
                self._pending.pop(command_id)
            if not future.done():
                future.cancel()

    @callback
    def async_handle_result(
        self,
        webhook_id: str,
        command_id: str,
        success: bool,
    ) -> bool:
        """Resolve a command result from its target registration."""
        pending = self._pending.get(command_id)
        if pending is None or pending.webhook_id != webhook_id or pending.future.done():
            return False

        pending.future.set_result(DeviceCommandResult(success))
        return True

    @callback
    def async_cancel_for_webhook(self, webhook_id: str) -> None:
        """Cancel commands targeting an unloaded registration."""
        for command_id, pending in list(self._pending.items()):
            if pending.webhook_id != webhook_id:
                continue
            if not pending.future.done():
                pending.future.set_exception(
                    HomeAssistantError("The requesting mobile app became unavailable")
                )
            self._pending.pop(command_id)

    @callback
    def async_shutdown(self, _event: Event) -> None:
        """Cancel all pending commands when Home Assistant stops."""
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.cancel()
        self._pending.clear()
