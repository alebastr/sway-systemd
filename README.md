# Systemd integration for Sway

## Goals and requirements

The goal of this project is to provide a minimal set of configuration files and
scripts required for running [Sway] in a systemd environment.
This includes several areas of integration:

- Propagate required variables to the systemd user session environment.
- Define sway-session.target for starting user services.
- Place GUI applications into systemd scopes for systemd-oomd compatibility.

## Non-goals

- Running the compositor itself as a user service.
  [sway-services] already exists and does exactly that.

- Managing Sway environment.
  It's hard, opinionated and depends on the way user starts Sway, so I don't
  have a solution that works for everyone and is acceptable for default
  configuration.  See also [#6].

  The common solutions are `~/.profile` (if your display manager supports that)
  or a wrapper script that sets the variables before starting Sway.

- Supporting multiple concurrent Sway sessions for the same user.
  It's uncommon and doing so would cause problems for which there are no easy
  solutions:

  As a part of the integration, we set `WAYLAND_DISPLAY` and `DISPLAY` for a
  systemd user session.
  The variables are only accurate per-session, while the systemd user sessions
  are per-user.
  So if the user starts a second Sway instance on the same machine, the new
  instance would overwrite the variables, potentially causing some services to
  break for the first session.

## Components

### Session targets

Systemd forbids starting the `graphical-session.target` directly and encourages
use of an environment-specific target units.  Thus, the package here defines
[`sway-session.target`] that binds to the `graphical-session.target` and starts
user services enabled for a graphical session.
`sway-session.target` should be started when the compositor is ready and the
user session environment is set, and stopped before the compositor exits.

An user service may depend on or reference `sway-session.target` only if it is
specific for Sway. Otherwise, it's recommended to use `graphical-session.target`.

A special `sway-session-shutdown.target` can be used to stop the
`graphical-session.target` and the `sway-session.target` with all the contained
services.\
`systemctl start sway-session-shutdown.target` will apply the `Conflicts=`
statements in the unit file and ensure that everything is exited, something that
`systemctl stop sway-session.target` is unable to guarantee.

### Session script

The [`session.sh`](./src/session.sh) script is responsible for importing
variables into systemd and dbus activation environments and starting session
target.  It also stays in the background until the compositor exits, stops
the session target and unsets variables for systemd user session
(this can be disabled by passing `--no-cleanup`).

The script itself does not set any variables except `XDG_CURRENT_DESKTOP`/
`XDG_SESSION_TYPE`; it simply passes the values received from Sway.
The list of variables and the name of the session target are currently
hardcoded and could be changed by editing the script.

For a better description see [comments in the code](./src/session.sh).

### Cgroups assignment script

The [`assign-cgroups.py`](./src/assign-cgroups.py) script subscribes to a new
window i3 ipc event and automatically creates a transient scope unit
(with path `app.slice/app-${app_id}.slice/app-${app_id}-${pid}.scope`) for each
GUI application launched in the same cgroup as the compositor.
Existing child processes of the application are assigned to the same scope.

The script is necessary to overcome a limitation of `systemd-oomd`:
it only tracks resource usage by cgroups and kills the whole group when
a single application misbehaves and exceeds resource usage limits.
By placing individual apps into isolated cgroups we are decreasing the chance
that the oomd killer would target the group with the compositor and accidentally
terminate the session.

It can also be used to impose resource usage limits on a specific application,
because transient units are still loading override configs.  For example,
by creating `$XDG_CONFIG_HOME/systemd/user/app-firefox.slice.d/override.conf`
with content

```ini
[Slice]
MemoryHigh=2G
```

you can tell systemd that all the Firefox processes combined are not allowed to
exceed 2 Gb of memory.  See [`systemd.resource-control(5)`] for other available
resource control options.

### Keyboard layout configuration

The [`locale1-xkb-config`] script reads the system-wide input configuration
from [`org.freedesktop.locale1`] systemd interface, translates it into a Sway
configuration and applies to all devices with type:keyboard.

The main motivation for this component was an ability to apply system-wide
keyboard mappings configured in the OS installer to a greetd or SDDM greeter
running with Sway as a display server.

The component is not enabled by default. Use `-Dautoload-configs=locale1,...`
to install the configuration file to Sway's default config drop-in directory or
check [`95-system-keyboard-config.conf`] for necessary configuration.

### XDG Desktop autostart target

The `sway-xdg-autostart.target` wraps systemd bultin
[`xdg-desktop-autostart.target`] to allow delayed start from a script.

The `xdg-desktop-autostart.target` contains units generated by
[`systemd-xdg-autostart-generator(8)`] from XDG desktop files in autostart
directories.
The recommended way to start it is a `Wants=xdg-desktop-autostart.target`
in a Desktop Environment session target (`sway-session.target` in our case),
but there are some issues with that approach.

Most notably, there's a race between the autostarted applications and the panel
with StatusNotifierHost implementation.
SNI specification is very clear on that point; if the `StatusNotifierWatcher`
is unavailable or `IsStatusNotifierHostRegistered` is not set, the application
should fallback to XEmbed tray.
There are even known implementations that follow this requirement (Qt...) and
will fail to create a status icon if started before the panel.

The component is not enabled by default. Use `-Dautoload-configs=autostart,...`
to install the configuration file to Sway's default config drop-in directory or
check [`95-xdg-desktop-autostart.conf`] for necessary configuration.

## Installation

<a href="https://repology.org/project/sway-systemd/versions">
    <img src="https://repology.org/badge/vertical-allrepos/sway-systemd.svg?exclude_unsupported=1"
        alt="Packaging status" align="right">
</a>

### Dependencies

Session script calls these commands:
`swaymsg`, `systemctl`, `dbus-update-activation-environment`.

Cgroups script uses following python packages:
[`dbus-next`](https://pypi.org/project/dbus-next/),
[`i3ipc`](https://pypi.org/project/i3ipc/),
[`psutil`](https://pypi.org/project/psutil/),
[`tenacity`](https://pypi.org/project/tenacity/),
[`python-xlib`](https://pypi.org/project/python-xlib/)

### Installing with meson

```
meson setup --sysconfidir=/etc [-Dautoload-configs=...,...] build
sudo meson install -C build
```

The command will install configuration files from [`config.d`](./config.d/)
to the `/etc/sway/config.d/` directory included from the default Sway config.
The `autoload-config` option allows you to specify the configuration files that
are loaded by default, with the rest being installed to
`$PREFIX/share/sway-systemd`.

If you are using a custom Sway configuration file and already removed the
`include /etc/sway/config.d/*` line, you will need to edit your config and
include the installed files.

> [!NOTE]
> It's not advised to enable everything system-wide, as behavior of certain
> integration components can be unexpected and confusing for the users.
> E.g. `locale1` can overwrite the keyboard options set in Sway config ([#21]),
> and `autostart` can conflict with existing autostart configuration.

### Installing manually/using directly from git checkout

1. Clone repository.
2. Copy `units/*.target` to the systemd user unit directory
   (`/usr/lib/systemd/user/`, `$XDG_CONFIG_HOME/systemd/user/` or
   `~/.config/systemd/user` are common locations).
3. Run `systemctl --user daemon-reload` to make systemd rescan the service files.
4. Add `exec /path/to/cloned/repo/src/session.sh` to your Sway config for
   environment and session configuration.
5. Add `exec /path/to/cloned/repo/src/assign-cgroups.py` to your Sway config
   to enable cgroup assignment script.
6. Restart your Sway session or run `swaymsg` with the commands above.
   Simple config reload is insufficient as it does not execute `exec` commands.

[Sway]: https://swaywm.org
[sway-services]: https://github.com/xdbob/sway-services/

[`systemd.resource-control(5)`]: https://www.freedesktop.org/software/systemd/man/systemd.resource-control.html
[`org.freedesktop.locale1`]: https://www.freedesktop.org/software/systemd/man/org.freedesktop.locale1.html
[`xdg-desktop-autostart.target`]: https://www.freedesktop.org/software/systemd/man/systemd.special.html#xdg-desktop-autostart.target
[`systemd-xdg-autostart-generator(8)`]: https://www.freedesktop.org/software/systemd/man/systemd-xdg-autostart-generator.html

[`95-system-keyboard-config.conf`]: ./config.d/95-system-keyboard-config.conf.in
[`95-xdg-desktop-autostart.conf`]: ./config.d/95-xdg-desktop-autostart.conf.in
[`locale1-xkb-config`]: ./src/locale1-xkb-config
[`sway-session.target`]: ./units/sway-session.target

[#6]: https://github.com/alebastr/sway-systemd/issues/6
[#21]: https://github.com/alebastr/sway-systemd/issues/21
