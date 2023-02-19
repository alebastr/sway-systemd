#!/usr/bin/python3
"""
Sync Sway input configuration with org.freedesktop.locale1.

Usage:
    Configure keyboard mappings with `localectl set-x11-keymap`.
    Add `exec /path/to/script` to your Sway config.

See also:
    https://www.freedesktop.org/software/systemd/man/org.freedesktop.locale1.html
"""
import asyncio
import logging
from typing import Any, Dict

from dbus_next import BusType, DBusError, Variant
from dbus_next.aio import MessageBus
from i3ipc.aio import Connection

LOG = logging.getLogger("sway.locale1")
LOCALE1_BUS_NAME = "org.freedesktop.locale1"
LOCALE1_OBJECT_PATH = "/org/freedesktop/locale1"
LOCALE1_INTERFACE = "org.freedesktop.locale1"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
PROPERTIES = {
    'X11Layout': 'layout',
    'X11Model': 'model',
    'X11Variant': 'variant',
    'X11Options': 'options'
}


class Locale1Client:
    """Handle org.freedesktop.locale1 updates and pass XKB configuration to Sway"""

    layout: str = ''
    model: str = ''
    variant: str = ''
    options: str = ''

    def __init__(self, bus: MessageBus, conn: Connection):
        self._bus = bus
        self._conn = conn
        self._proxy = None

    async def connect(self):
        """asynchronous initialization code"""
        introspection = await self._bus.introspect(LOCALE1_BUS_NAME,
                                                   LOCALE1_OBJECT_PATH)
        self._proxy = self._bus.get_proxy_object(LOCALE1_BUS_NAME,
                                                 LOCALE1_OBJECT_PATH,
                                                 introspection)
        self._proxy.get_interface(PROPERTIES_INTERFACE).on_properties_changed(
            self.on_properties_changed)

        locale1 = self._proxy.get_interface(LOCALE1_INTERFACE)
        self.layout = await locale1.get_x11_layout()
        self.model = await locale1.get_x11_model()
        self.variant = await locale1.get_x11_variant()
        self.options = await locale1.get_x11_options()

        await self.update()

    async def on_properties_changed(self,
                                    interface: str,
                                    changed: Dict[str, Any],
                                    _invalidated=None):
        """Handle updates from localed"""
        if interface != LOCALE1_INTERFACE:
            return

        apply = False

        for name, value in changed.items():
            if name not in PROPERTIES:
                continue
            if isinstance(value, Variant):
                value = value.value
            self.__dict__[PROPERTIES[name]] = value
            apply = True

        if apply:
            await self.update()

    async def update(self):
        """Pass the updated xkb configuration to Sway"""
        LOG.info("xkb: layout '%s' model '%s', variant '%s' options '%s'",
                 self.layout, self.model, self.variant, self.options)
        cmd = {
            f"input type:keyboard xkb_{name} '{self.__dict__[name]}'"
            for name in PROPERTIES.values()
        }
        replies = await self._conn.command(', '.join(cmd))
        for cmd, reply in zip(cmd, replies):
            if reply.error is not None:
                LOG.error("command '%s' failed: %s", cmd, reply.error)


async def main():
    """Async entrypoint"""
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        conn = await Connection(auto_reconnect=False).connect()
        await Locale1Client(bus, conn).connect()
        await conn.main()
    except DBusError as exc:
        LOG.error("DBus connection error: %s", exc)
    except (ConnectionError, EOFError) as exc:
        LOG.error("Sway IPC connection error: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
