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

Dependencies: dbus-next, i3ipc, psutil, tenacity, python-xlib
"""
import argparse
import asyncio
import logging
import re
import socket
import struct
import sys
from functools import lru_cache
from typing import Optional

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError
from i3ipc import Event
from i3ipc.aio import Con, Connection
from psutil import Process
from tenacity import retry, retry_if_exception_type, stop_after_attempt

if sys.version_info[:2] >= (3, 9):
    from collections.abc import Callable
else:
    from typing import Callable


LOG = logging.getLogger("assign-cgroups")
SD_BUS_NAME = "org.freedesktop.systemd1"
SD_OBJECT_PATH = "/org/freedesktop/systemd1"
SD_SLICE_FORMAT = "app-{app_id}.slice"
SD_UNIT_FORMAT = "app-{app_id}-{unique}.scope"
# Ids of known launcher applications that are not special surfaces. When the app is
# started using one of those, it should be moved to a new cgroup.
# Launcher should only be listed here if it creates cgroup of its own.
LAUNCHER_APPS = ["nwgbar", "nwgdmenu", "nwggrid", "onagre"]

SD_UNIT_ESCAPE_RE = re.compile(r"[^\w:.\\]", re.ASCII)


def escape_app_id(app_id: str) -> str:
    """Escape app_id for systemd APIs.

    The "unit prefix" must consist of one or more valid characters (ASCII letters,
    digits, ":", "-", "_", ".", and "\"). The total length of the unit name including
    the suffix must not exceed 256 characters. [systemd.unit(5)]

    We also want to escape "-" to avoid creating extra slices.
    """

    def repl(match):
        return "".join([f"\\x{x:02x}" for x in match.group().encode()])

    return SD_UNIT_ESCAPE_RE.sub(repl, app_id)


LAUNCHER_APP_CGROUPS = [
    SD_SLICE_FORMAT.format(app_id=escape_app_id(app)) for app in LAUNCHER_APPS
]


def get_cgroup(pid: int) -> Optional[str]:
    """
    Get cgroup identifier for the process specified by pid.
    Assumes cgroups v2 unified hierarchy.
    """
    try:
        with open(f"/proc/{pid}/cgroup", "r") as file:
            cgroup = file.read()
        return cgroup.strip().split(":")[-1]
    except OSError:
        LOG.exception("Error geting cgroup info")
    return None


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


def create_x11_pid_getter() -> Callable[[int], int]:
    """Create fallback X11 PID getter.

    Sway 1.6.1/wlroots 0.14 can use XRes to get the PID for Xwayland apps from
    the server and won't ever reach that. The fallback is preserved for
    compatibility with i3 and earlier versions of Sway.
    """
    # pylint: disable=import-outside-toplevel
    # Defer Xlib import until we really need it.
    from Xlib import X
    from Xlib.display import Display

    try:
        # requires python-xlib >= 0.30
        from Xlib.ext import res as XRes
    except ImportError:
        XRes = None

    display = Display()

    def get_net_wm_pid(wid: int) -> int:
        """Get PID from _NET_WM_PID property of X11 window"""
        window = display.create_resource_object("window", wid)
        net_wm_pid = display.get_atom("_NET_WM_PID")
        pid = window.get_full_property(net_wm_pid, X.AnyPropertyType)

        if pid is None:
            raise RuntimeError("Failed to get PID from _NET_WM_PID")
        return int(pid.value.tolist()[0])

    def get_xres_client_id(wid: int) -> int:
        """Get PID from X server via X-Resource extension"""
        res = display.res_query_client_ids(
            [{"client": wid, "mask": XRes.LocalClientPIDMask}]
        )
        for cid in res.ids:
            if cid.spec.client > 0 and cid.spec.mask == XRes.LocalClientPIDMask:
                for value in cid.value:
                    return value
        raise RuntimeError("Failed to get PID via X-Resource extension")

    if XRes is None or display.query_extension(XRes.extname) is None:
        LOG.warning(
            "X-Resource extension is not supported. "
            "Process identification for X11 applications will be less reliable."
        )
        return get_net_wm_pid

    ver = display.res_query_version()
    LOG.info(
        "X-Resource version %d.%d",
        ver.server_major,
        ver.server_minor,
    )
    if (ver.server_major, ver.server_minor) < (1, 2):
        return get_net_wm_pid

    return get_xres_client_id


class CGroupHandler:
    """Main logic: handle i3/sway IPC events and start systemd transient units."""

    def __init__(self, bus: MessageBus, conn: Connection):
        self._bus = bus
        self._conn = conn

    @property
    @lru_cache(maxsize=1)
    def get_x11_window_pid(self) -> Optional[Callable[[int], int]]:
        """On-demand initialization of X11 PID getter"""
        try:
            return create_x11_pid_getter()
        # pylint: disable=broad-except
        except Exception as exc:
            LOG.warning("Failed to create X11 PID getter: %s", exc)
            return None

    async def connect(self):
        """asynchronous initialization code"""
        # pylint: disable=attribute-defined-outside-init
        introspection = await self._bus.introspect(SD_BUS_NAME, SD_OBJECT_PATH)
        self._sd_proxy = self._bus.get_proxy_object(
            SD_BUS_NAME, SD_OBJECT_PATH, introspection
        )
        self._sd_manager = self._sd_proxy.get_interface(f"{SD_BUS_NAME}.Manager")

        self._compositor_pid = get_pid_by_socket(self._conn.socket_path)
        self._compositor_cgroup = get_cgroup(self._compositor_pid)
        assert self._compositor_cgroup is not None
        LOG.info("compositor:%s %s", self._compositor_pid, self._compositor_cgroup)

        self._conn.on(Event.WINDOW_NEW, self._on_new_window)
        return self

    def get_pid(self, con: Con) -> Optional[int]:
        """Get PID from IPC response (sway), X-Resource or _NET_WM_PID (i3)"""
        if isinstance(con.pid, int) and con.pid > 0:
            return con.pid

        if con.window is not None and self.get_x11_window_pid is not None:
            return self.get_x11_window_pid(con.window)

        return None

    def cgroup_change_needed(self, cgroup: Optional[str]) -> bool:
        """Check criteria for assigning current app into an isolated cgroup"""
        if cgroup is None:
            return False
        for launcher in LAUNCHER_APP_CGROUPS:
            if launcher in cgroup:
                return True
        return cgroup == self._compositor_cgroup

    @retry(
        reraise=True,
        retry=retry_if_exception_type(DBusError),
        stop=stop_after_attempt(3),
    )
    async def assign_scope(self, app_id: str, proc: Process):
        """
        Assign process (and all unassigned children) to the
        app-{app_id}.slice/app{app_id}-{pid}.scope cgroup
        """
        app_id = escape_app_id(app_id)
        sd_slice = SD_SLICE_FORMAT.format(app_id=app_id)
        sd_unit = SD_UNIT_FORMAT.format(app_id=app_id, unique=proc.pid)
        # Collect child processes as systemd assigns a scope only to explicitly
        # specified PIDs.
        # There's a risk of race as the child processes may exit by the time dbus call
        # reaches systemd, hence the @retry decorator is applied to the method.
        pids = [proc.pid] + [
            x.pid
            for x in proc.children(recursive=True)
            if self.cgroup_change_needed(get_cgroup(x.pid))
        ]

        await self._sd_manager.call_start_transient_unit(
            sd_unit,
            "fail",
            [["PIDs", Variant("au", pids)], ["Slice", Variant("s", sd_slice)]],
            [],
        )
        LOG.debug(
            "window %s successfully assigned to cgroup %s/%s", app_id, sd_slice, sd_unit
        )

    async def _on_new_window(self, _: Connection, event: Event):
        """window:new IPC event handler"""
        con = event.container
        app_id = con.app_id if con.app_id else con.window_class
        try:
            pid = self.get_pid(con)
            if pid is None:
                LOG.warning("Failed to get pid for %s", app_id)
                return
            proc = Process(pid)
            cgroup = get_cgroup(proc.pid)
            # some X11 apps don't set WM_CLASS. fallback to process name
            if app_id is None:
                app_id = proc.name()
            LOG.debug("window %s(%s) cgroup %s", app_id, proc.pid, cgroup)
            if self.cgroup_change_needed(cgroup):
                await self.assign_scope(app_id, proc)
        # pylint: disable=broad-except
        except Exception as exc:
            LOG.error("Failed to modify cgroup for %s: %s", app_id, exc)


async def main():
    """Async entrypoint"""
    try:
        bus = await MessageBus().connect()
        conn = await Connection(auto_reconnect=False).connect()
        await CGroupHandler(bus, conn).connect()
        await conn.main()
    except DBusError as exc:
        LOG.error("DBus connection error: %s", exc)
    except (ConnectionError, EOFError) as exc:
        LOG.error("Sway IPC connection error: %s", exc)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Assign CGroups to apps in compositors with i3 IPC protocol support"
    )
    parser.add_argument(
        "-l",
        "--loglevel",
        choices=["critical", "error", "warning", "info", "debug"],
        default="info",
        dest="loglevel",
        help="set logging level",
    )
    args = parser.parse_args()
    logging.basicConfig(level=args.loglevel.upper())
    asyncio.run(main())
