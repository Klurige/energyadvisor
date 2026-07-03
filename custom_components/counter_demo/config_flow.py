"""Config flow for the Counter Demo integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN


class CounterDemoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create a single Counter Demo config entry."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Create the integration entry immediately."""
        return self.async_create_entry(title="Counter Demo", data={})
