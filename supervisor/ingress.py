"""Fetch last versions from webserver."""

from datetime import timedelta
import logging
import random
import secrets

from .addons.addon import Addon
from .const import (
    ATTR_PORTS,
    ATTR_SESSION,
    ATTR_SESSION_DATA,
    FILE_HASSIO_INGRESS,
    IngressSessionData,
    IngressSessionDataDict,
)
from .coresys import CoreSys, CoreSysAttributes
from .utils import check_port
from .utils.common import FileConfiguration
from .utils.dt import utc_from_timestamp, utcnow
from .validate import SCHEMA_INGRESS_CONFIG

_LOGGER: logging.Logger = logging.getLogger(__name__)


class Ingress(FileConfiguration, CoreSysAttributes):
    """Fetch last versions from version.json."""

    def __init__(self, coresys: CoreSys):
        """Initialize updater."""
        super().__init__(FILE_HASSIO_INGRESS, SCHEMA_INGRESS_CONFIG)
        self.coresys: CoreSys = coresys
        self.tokens: dict[str, str] = {}

    def get(self, token: str) -> Addon | None:
        """Return addon they have this ingress token."""
        if token not in self.tokens:
            return None
        return self.sys_addons.get_local_only(self.tokens[token])

    def get_session_data(self, session_id: str) -> IngressSessionData | None:
        """Return complementary data of current session or None."""
        if data := self.sessions_data.get(session_id):
            return IngressSessionData.from_dict(data)
        return None

    @property
    def sessions(self) -> dict[str, float]:
        """Return sessions."""
        return self._data[ATTR_SESSION]

    @property
    def sessions_data(self) -> dict[str, IngressSessionDataDict]:
        """Return sessions_data."""
        return self._data[ATTR_SESSION_DATA]

    @property
    def ports(self) -> dict[str, int]:
        """Return list of dynamic ports."""
        return self._data[ATTR_PORTS]

    @property
    def addons(self) -> list[Addon]:
        """Return list of ingress Add-ons."""
        addons = []
        for addon in self.sys_addons.installed:
            if not addon.with_ingress:
                continue
            addons.append(addon)
        return addons

    async def load(self) -> None:
        """Update internal data."""
        self._update_token_list()
        self._cleanup_sessions()

        _LOGGER.info("Loaded %d ingress sessions", len(self.sessions))

    async def reload(self) -> None:
        """Reload/Validate sessions."""
        self._cleanup_sessions()
        self._update_token_list()

    async def unload(self) -> None:
        """Shutdown sessions."""
        await self.save_data()

    def _cleanup_sessions(self) -> None:
        """Remove not used sessions."""
        now = utcnow()

        sessions = {}
        sessions_data: dict[str, IngressSessionDataDict] = {}
        for session, valid in self.sessions.items():
            # check if timestamp valid, to avoid crash on malformed timestamp
            try:
                valid_dt = utc_from_timestamp(valid)
            except OverflowError:
                _LOGGER.warning("Session timestamp %f is invalid!", valid)
                continue

            if valid_dt < now:
                continue

            # Is valid
            sessions[session] = valid
            if session_data := self.sessions_data.get(session):
                sessions_data[session] = session_data

        # Write back
        self.sessions.clear()
        self.sessions.update(sessions)
        self.sessions_data.clear()
        self.sessions_data.update(sessions_data)

    def _update_token_list(self) -> None:
        """Regenerate token <-> Add-on map."""
        self.tokens.clear()

        # Read all ingress token and build a map
        for addon in self.addons:
            if addon.ingress_token:
                self.tokens[addon.ingress_token] = addon.slug

    def create_session(self, data: IngressSessionData | None = None) -> str:
        """Create new session."""
        session = secrets.token_hex(64)
        valid = utcnow() + timedelta(minutes=15)

        self.sessions[session] = valid.timestamp()
        if data is not None:
            self.sessions_data[session] = data.to_dict()

        return session

    def validate_session(self, session: str) -> bool:
        """Return True if session valid and make it longer valid."""
        if session not in self.sessions:
            _LOGGER.debug("Session %s is not known", session)
            return False

        # check if timestamp valid, to avoid crash on malformed timestamp
        try:
            valid_until = utc_from_timestamp(self.sessions[session])
        except OverflowError:
            self.sessions[session] = (utcnow() + timedelta(minutes=15)).timestamp()
            return True

        # Is still valid?
        if valid_until < utcnow():
            _LOGGER.debug("Session is no longer valid (%f/%f)", valid_until, utcnow())
            return False

        # Update time
        valid_until = valid_until + timedelta(minutes=15)
        self.sessions[session] = valid_until.timestamp()

        return True

    async def get_dynamic_port(self, addon_slug: str) -> int:
        """Get/Create a dynamic port from range."""
        if addon_slug in self.ports:
            return self.ports[addon_slug]

        port = None
        while (
            port is None
            or port in self.ports.values()
            or await check_port(self.sys_docker.network.gateway, port)
        ):
            port = random.randint(62000, 65500)

        # Save port for next time
        self.ports[addon_slug] = port
        await self.save_data()
        return port

    async def del_dynamic_port(self, addon_slug: str) -> None:
        """Remove a previously assigned dynamic port."""
        if addon_slug not in self.ports:
            return

        del self.ports[addon_slug]
        await self.save_data()

    async def update_hass_panel(self, addon: Addon):
        """Return True if Home Assistant up and running."""
        if not await self.sys_homeassistant.core.is_running():
            _LOGGER.debug("Ignoring panel update on Core")
            return

        # Update UI
        method = "post" if addon.ingress_panel else "delete"
        async with self.sys_homeassistant.api.make_request(
            method, f"api/hassio_push/panel/{addon.slug}"
        ) as resp:
            if resp.status in (200, 201):
                _LOGGER.info("Update Ingress as panel for %s", addon.slug)
            else:
                _LOGGER.warning(
                    "Fails Ingress panel for %s with %i", addon.slug, resp.status
                )
