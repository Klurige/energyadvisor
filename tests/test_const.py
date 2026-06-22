"""Tests for const module dev-config loading."""

from __future__ import annotations

import importlib
import sys
import types


def test_const_loads_dev_defaults_without_ha_secrets():
    """DEV_DEFAULTS should still load when HA_URL/TOKEN are absent."""
    import custom_components.electricitypricelevels.const as const_mod

    module_name = "custom_components.electricitypricelevels.dev_config"
    original_dev_module = sys.modules.get(module_name)

    fake_dev_module = types.ModuleType(module_name)
    fake_dev_module.DEV_DEFAULTS_ENABLED = True
    fake_dev_module.DEV_DEFAULTS = {
        "nordpool_prices_sensor": "sensor.nord_pool_se4_current_price"
    }

    try:
        sys.modules[module_name] = fake_dev_module
        const_mod = importlib.reload(const_mod)

        assert const_mod.DEV_DEFAULTS_ENABLED is True
        assert const_mod.DEV_DEFAULTS["nordpool_prices_sensor"] == (
            "sensor.nord_pool_se4_current_price"
        )
        assert const_mod.HA_URL == ""
        assert const_mod.HA_TOKEN == ""
    finally:
        if original_dev_module is not None:
            sys.modules[module_name] = original_dev_module
        else:
            sys.modules.pop(module_name, None)
        importlib.reload(const_mod)
