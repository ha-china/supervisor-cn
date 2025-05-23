"""Plugin for Supervisor backend."""

import asyncio
import logging
from typing import Self

from ..coresys import CoreSys, CoreSysAttributes
from ..exceptions import HassioError
from ..resolution.const import ContextType, IssueType, SuggestionType
from ..utils.sentry import async_capture_exception
from .audio import PluginAudio
from .base import PluginBase
from .cli import PluginCli
from .dns import PluginDns
from .multicast import PluginMulticast
from .observer import PluginObserver

_LOGGER: logging.Logger = logging.getLogger(__name__)


class PluginManager(CoreSysAttributes):
    """Manage supported function for plugins."""

    def __init__(self, coresys: CoreSys):
        """Initialize plugin manager."""
        self.coresys: CoreSys = coresys

        self._cli: PluginCli = PluginCli(coresys)
        self._dns: PluginDns = PluginDns(coresys)
        self._audio: PluginAudio = PluginAudio(coresys)
        self._observer: PluginObserver = PluginObserver(coresys)
        self._multicast: PluginMulticast = PluginMulticast(coresys)

    async def load_config(self) -> Self:
        """Load config in executor."""
        await asyncio.gather(*[plugin.read_data() for plugin in self.all_plugins])
        return self

    @property
    def all_plugins(self) -> list[PluginBase]:
        """Return cli handler."""
        return [self._cli, self._dns, self._audio, self._observer, self._multicast]

    @property
    def cli(self) -> PluginCli:
        """Return cli handler."""
        return self._cli

    @property
    def dns(self) -> PluginDns:
        """Return dns handler."""
        return self._dns

    @property
    def audio(self) -> PluginAudio:
        """Return audio handler."""
        return self._audio

    @property
    def observer(self) -> PluginObserver:
        """Return observer handler."""
        return self._observer

    @property
    def multicast(self) -> PluginMulticast:
        """Return multicast handler."""
        return self._multicast

    async def load(self) -> None:
        """Load Supervisor plugins."""
        # Sequential to avoid issue on slow IO
        for plugin in self.all_plugins:
            try:
                await plugin.load()
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("Can't load plugin %s: %s", plugin.slug, err)
                self.sys_resolution.create_issue(
                    IssueType.FATAL_ERROR,
                    ContextType.PLUGIN,
                    reference=plugin.slug,
                    suggestions=[SuggestionType.EXECUTE_REPAIR],
                )
                await async_capture_exception(err)

        # Exit if supervisor out of date. Plugins can't update until then
        if self.sys_supervisor.need_update:
            return

        # Check requirements
        for plugin in self.all_plugins:
            # Check if need an update
            if not plugin.need_update:
                continue

            _LOGGER.info(
                "Plugin %s is not up-to-date, latest version %s, updating",
                plugin.slug,
                plugin.latest_version,
            )
            try:
                await plugin.update()
            except HassioError as ex:
                _LOGGER.error(
                    "Can't update %s to %s: %s",
                    plugin.slug,
                    plugin.latest_version,
                    str(ex),
                )
                self.sys_resolution.create_issue(
                    IssueType.UPDATE_FAILED,
                    ContextType.PLUGIN,
                    reference=plugin.slug,
                    suggestions=[SuggestionType.EXECUTE_UPDATE],
                )
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("Can't update plugin %s: %s", plugin.slug, err)
                await async_capture_exception(err)

    async def repair(self) -> None:
        """Repair Supervisor plugins."""
        await asyncio.wait(
            [self.sys_create_task(plugin.repair()) for plugin in self.all_plugins]
        )

    async def shutdown(self) -> None:
        """Shutdown Supervisor plugin."""
        # Sequential to avoid issue on slow IO
        for plugin in (
            plugin for plugin in self.all_plugins if plugin.slug != "observer"
        ):
            try:
                await plugin.stop()
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("Can't stop plugin %s: %s", plugin.slug, err)
                await async_capture_exception(err)
