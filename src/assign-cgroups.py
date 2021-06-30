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
import socket
import struct
from typing import Optional

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError
from i3ipc import Event
from i3ipc.aio import Con, Connection
from psutil import Process
from tenacity import retry, retry_if_exception_type, stop_after_attempt
from Xlib import X
from Xlib.display import Display

try:
    # requires python-xlib >= 0.30
    from Xlib.ext import res as XRes
except ImportError:
    XRes = None


LOG = logging.getLogger("assign-cgroups")
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


class XlibHelper:
    def __init__(self):
        self.display = Display()
        self.use_xres = self._try_init_xres()

    def _try_init_xres(self) -> bool:
        if XRes is None or self.display.query_extension(XRes.extname) is None:
            LOG.warning(
                "X-Resource extension is not supported. "
                + "Process identification for X11 applications will be less reliable."
            )
            return False
        ver = self.display.res_query_version()
        LOG.info(
            "X-Resource version %d.%d",
            ver.server_major,
            ver.server_minor,
        )
        return (ver.server_major, ver.server_minor) >= (1, 2)

    def _get_net_wm_pid(self, wid: int) -> int:
        """Get PID from _NET_WM_PID property of X11 window"""
        window = self.display.create_resource_object("window", wid)
        net_wm_pid = self.display.get_atom("_NET_WM_PID")
        pid = window.get_full_property(net_wm_pid, X.AnyPropertyType)

        if pid is None:
            raise Exception("Failed to get PID from _NET_WM_PID")
        return int(pid.value.tolist()[0])

    def _get_xres_client_id(self, wid: int) -> int:
        """Get PID from X server via X-Resource extension"""
        r = self.display.res_query_client_ids(
            [{"client": wid, "mask": XRes.LocalClientPIDMask}]
        )
        for id in r.ids:
            if id.spec.client > 0 and id.spec.mask == XRes.LocalClientPIDMask:
                for value in id.value:
                    return value
        raise Exception("Failed to get PID via X-Resource extension")

    def get_window_pid(self, wid: int) -> Optional[int]:
        if self.use_xres:
            return self._get_xres_client_id(wid)
        else:
            return self._get_net_wm_pid(wid)


class CGroupHandler:
    def __init__(self, bus: MessageBus, conn: Connection):
        self._bus = bus
        self._conn = conn
        self._xhelper: Optional[XlibHelper] = None
        try:
            self._xhelper = XlibHelper()
        except Exception as exc:
            LOG.warning("Failed to connect to X11 display: %s", exc)

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
        LOG.info("compositor:%s %s", self._compositor_pid, self._compositor_cgroup)

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
            LOG.exception("Error geting cgroup info")
        return None

    def get_app_id(self, con: Con) -> str:
        """Get Application ID"""
        return con.app_id if con.app_id is not None else con.window_class

    def get_pid(self, con: Con) -> Optional[int]:
        """Get PID from IPC response (sway), X-Resource or _NET_WM_PID (i3)"""
        if isinstance(con.pid, int) and con.pid > 0:
            return con.pid

        if con.window is not None and self._xhelper is not None:
            return self._xhelper.get_window_pid(con.window)

        return None

    def cgroup_change_needed(self, cgroup: Optional[str]) -> bool:
        """Check criteria for assigning current app into an isolated cgroup"""
        # TODO: check for known launchers
        return cgroup == self._compositor_cgroup

    @retry(
        reraise=True,
        retry=retry_if_exception_type(DBusError),
        stop=stop_after_attempt(3),
    )
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
            if self.cgroup_change_needed(self.get_cgroup(x.pid))
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
            if pid is None:
                LOG.warning("Failed to get pid for %s", app_id)
                return
            cgroup = self.get_cgroup(pid)
            LOG.debug("window %s(%s) cgroup %s", app_id, pid, cgroup)
            if self.cgroup_change_needed(cgroup):
                await self.assign_scope(app_id, pid)
        except Exception:
            LOG.exception("Failed to modify cgroup for %s", app_id)


async def main():
    """Async entrypoint"""
    bus = await MessageBus().connect()
    conn = await Connection(auto_reconnect=False).connect()
    await CGroupHandler(bus, conn).connect()
    try:
        await conn.main()
    except (ConnectionError, EOFError):
        logging.exception("Connection to the Sway IPC was lost")


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
