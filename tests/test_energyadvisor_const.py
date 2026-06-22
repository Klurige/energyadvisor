"""Tests for Energy Advisor const module dev-config loading."""

from __future__ import annotations

import importlib
import sys
import types


def test_energyadvisor_const_loads_local_dev_defaults_without_ha_secrets():
    """Energy Advisor should load local dev defaults without HA credentials."""
    import custom_components.energyadvisor.const as const_mod

    local_module_name = "custom_components.energyadvisor.dev_config"
    original_local_module = sys.modules.get(local_module_name)

    fake_local_module = types.ModuleType(local_module_name)
    fake_local_module.DEV_DEFAULTS_ENABLED = True
    fake_local_module.DEV_DEFAULTS = {
        "nordpool_prices_sensor": "sensor.nord_pool_se4_current_price"
    }

    try:
        sys.modules[local_module_name] = fake_local_module
        const_mod = importlib.reload(const_mod)

        assert const_mod.DEV_DEFAULTS_ENABLED is True
        assert const_mod.DEV_DEFAULTS["nordpool_prices_sensor"] == (
            "sensor.nord_pool_se4_current_price"
        )
        assert const_mod.HA_URL == ""
        assert const_mod.HA_TOKEN == ""
    finally:
        if original_local_module is not None:
            sys.modules[local_module_name] = original_local_module
        else:
            sys.modules.pop(local_module_name, None)
        importlib.reload(const_mod)
