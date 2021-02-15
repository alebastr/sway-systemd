#!/usr/bin/python3
"""
Automatically assign a dedicated systemd scope to the GUI applications
launched in the same cgroup as the compositor. This could be helpful for
implementing cgroup-based resource management and would be necessary when
`systemd-oomd` is in use.

Limitations: The script is using i3ipc window:new event to detect application
launches and would fail to detect background apps or special surfaces.
Therefore it's recommended to supplement the script with use of systemd user
services for such background apps.

Dependencies: dbus-next, i3ipc, psutil, python-xlib
"""
import asyncio
import logging
import socket
import struct

from functools import wraps
from typing import Optional

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError
from i3ipc import Event
from i3ipc.aio import Con, Connection
from psutil import Process
from Xlib import X
from Xlib.display import Display

SD_BUS_NAME = "org.freedesktop.systemd1"
SD_OBJECT_PATH = "/org/freedesktop/systemd1"


def get_pid_by_socket(sockpath: str) -> int:
    """
    getsockopt (..., SO_PEERCRED, ...) returns the following structure
    struct ucred
    {
      pid_t pid; /* s32: PID of sending process.  */
      uid_t uid; /* u32: UID of sending process.  */
      gid_t gid; /* u32: GID of sending process.  */
    };
    See also: socket(7), unix(7)
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(sockpath)
        ucred = sock.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII")
        )
    pid, _, _ = struct.unpack("iII", ucred)
    return pid


def escape_app_id(app_id: str) -> str:
    """Escape app_id for systemd APIs"""
    return app_id.replace("-", "\\x2d")


def retry_async(exception=Exception, tries=3):
    def retry_async_decorator(func):
        @wraps(func)
        async def decorated(*args, **kwargs):
            err = None
            for i in range(tries):
                try:
                    return await func(*args, **kwargs)
                except exception as exc:
                    err = exc
                    logging.warning(
                        "retry: %s failed (%d/%d): %s", func.__name__, i + 1, tries, err
                    )
            raise err

        return decorated

    return retry_async_decorator


class CGroupHandler:
    log = logging.getLogger("CGroupHandler")

    def __init__(self, bus: MessageBus, conn: Connection):
        self._bus = bus
        self._conn = conn
        self._display: Optional[Display] = None

    async def connect(self):
        """asynchronous initialization code"""
        # pylint: disable=attribute-defined-outside-init
        introspection = await self._bus.introspect(SD_BUS_NAME, SD_OBJECT_PATH)
        self._sd_proxy = self._bus.get_proxy_object(
            SD_BUS_NAME, SD_OBJECT_PATH, introspection
        )
        self._sd_manager = self._sd_proxy.get_interface(f"{SD_BUS_NAME}.Manager")

        self._compositor_pid = get_pid_by_socket(self._conn.socket_path)
        self._compositor_cgroup = self.get_cgroup(self._compositor_pid)
        assert self._compositor_cgroup is not None
        self.log.info("compositor:%s %s", self._compositor_pid, self._compositor_cgroup)

        self._conn.on(Event.WINDOW_NEW, self._on_new_window)
        return self

    def get_cgroup(self, pid: int) -> Optional[str]:
        """
        Get cgroup identifier for the process specified by pid.
        Assumes cgroups v2 unified hierarchy.
        """
        try:
            with open(f"/proc/{pid}/cgroup", "r") as file:
                cgroup = file.read()
            return cgroup.strip().split(":")[-1]
        except OSError:
            self.log.exception("Error geting cgroup info")
        return None

    def get_app_id(self, con: Con) -> str:
        """Get Application ID"""
        return con.app_id if con.app_id is not None else con.window_class

    def get_net_wm_pid(self, window_id: int) -> int:
        """Get PID from _NET_WM_PID property of X11 window"""
        if self._display is None:
            self._display = Display()

        window = self._display.create_resource_object("window", window_id)
        _net_wm_pid = self._display.get_atom("_NET_WM_PID")
        pid = window.get_full_property(_net_wm_pid, X.AnyPropertyType)

        if pid is None:
            raise Exception("Failed to get PID from _NET_WM_PID")
        return int(pid.value.tolist()[0])

    def get_pid(self, con: Con) -> int:
        """Get PID from IPC response (sway) or _NET_WM_PID (i3)"""
        if con.pid is not None:
            return con.pid

        if con.window is None:
            raise Exception("Neither PID nor WindowID are present in IPC response")

        return self.get_net_wm_pid(con.window)

    @retry_async(exception=DBusError, tries=3)
    async def assign_scope(self, app_id: str, pid: int):
        """
        Assign process (and all unassigned children) to the
        app-{app_id}.slice/app{app_id}-{pid}.scope cgroup
        """
        app_id = escape_app_id(app_id)
        sd_unit = f"app-{app_id}-{pid}.scope"
        sd_slice = f"app-{app_id}.slice"
        proc = Process(pid)
        # Collect child processes as systemd assigns a scope only to explicitly
        # specified PIDs.
        # There's a risk of race as the child processes may exit by the time dbus call
        # reaches systemd, hence the @retry_async decorator is applied to the method.
        pids = [pid] + [
            x.pid
            for x in proc.children(recursive=True)
            if self.get_cgroup(x.pid) == self._compositor_cgroup
        ]

        await self._sd_manager.call_start_transient_unit(
            sd_unit,
            "fail",
            [["PIDs", Variant("au", pids)], ["Slice", Variant("s", sd_slice)]],
            [],
        )

    async def _on_new_window(self, _: Connection, event: Event):
        """window:new IPC event handler"""
        con = event.container
        app_id = self.get_app_id(con)
        try:
            pid = self.get_pid(con)
            cgroup = self.get_cgroup(pid)
            self.log.debug("window %s:%s %s", app_id, pid, cgroup)
            if cgroup == self._compositor_cgroup:
                await self.assign_scope(app_id, pid)
        except Exception:
            self.log.exception("Failed to modify cgroup for %s", app_id)


async def main():
    """Async entrypoint"""
    bus = await MessageBus().connect()
    conn = await Connection(auto_reconnect=False).connect()
    await CGroupHandler(bus, conn).connect()
    await conn.main()


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(main())
