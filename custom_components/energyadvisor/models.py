"""Runtime models for the Energy Advisor integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sensor.compactlevels import CompactLevelsSensor
    from .sensor.price import PriceSensor
    from .sensor.nordpool_coordinator import NordpoolDataCoordinator
    from .sensor.solarforecastsensor import SolarForecastSensor
    from .solar_forecast_coordinator import SolarForecastCoordinator


@dataclass(slots=True)
class EnergyAdvisorRuntimeData:
    """Per-config-entry runtime data."""

    levels_sensor: PriceSensor
    compact_sensor: CompactLevelsSensor
    coordinator: NordpoolDataCoordinator
    solar_sensor: SolarForecastSensor | None = None
    solar_coordinator: SolarForecastCoordinator | None = None
