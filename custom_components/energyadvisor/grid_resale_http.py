"""Authenticated access to explicitly generated CSV snapshots."""

import re
from pathlib import Path

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant


class GridResaleExportView(HomeAssistantView):
    """Do not publish tax records in HA's unauthenticated www directory."""

    url = "/api/energyadvisor/grid_resale/{filename}"
    name = "api:energyadvisor:grid_resale"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, filename: str) -> web.StreamResponse:
        if re.fullmatch(r"[a-f0-9]{32}\.csv", filename) is None:
            raise web.HTTPNotFound()
        path = Path(self.hass.config.path("energyadvisor", "exports", filename))
        if not await self.hass.async_add_executor_job(path.is_file):
            raise web.HTTPNotFound()
        return web.FileResponse(
            path,
            headers={
                "Content-Disposition": 'attachment; filename="grid_resale.csv"',
                "Cache-Control": "no-store",
            },
        )
