# Systemd integration for Sway

## Goals and requirements

The goal of this project is to provide a minimal set of configuration files and scripts required for running [Sway](https://swaywm.org/) in a systemd environment.
This includes several areas of integration:

- Propagate required variables to the systemd user session environment.
- Define sway-session.target for starting user services.
- Place GUI applications into systemd scopes for systemd-oomd compatibility.

## Non-goals

- Running the compositor itself as a user service. [sway-services](https://github.com/xdbob/sway-services/) already exists and does exactly that.

- Managing sway environment. It's hard, opinionated and depends on the way user starts sway, so I don't have a solution that works for everyone and is acceptable for default configuration. See also [#6](https://github.com/alebastr/sway-systemd/issues/6).\
  The common solutions are `~/.profile` (if your display manager supports that), `~/.pam_environment`, or a wrapper script that sets the variables before starting sway.

- Supporting multiple concurrent sway sessions for the same user. It's uncommon and doing so would cause problems for which there are no easy solutions: To make this work, we set `WAYLAND_DISPLAY` with a systemd user session. This environment variable only accurate per-session, while systemd user sessions are per-user. If the user starts a second Sway instance on the same machine, the new instance would overwrite this value, which could cause some services to break in the first session if they were to read the variable after the second session starts.

## Components

### Session target

Systemd forbids starting the `graphical-session.target` directly and encourages use of an environment-specific target units. Thus, the package here defines [`sway-session.target`](./sway-session.target) that binds to `graphical-session.target` and starts user services enabled for a graphical session. `sway-session.target` should be started when the compositor is ready and the user-session environment is set, and stopped before the compositor exits.

A systemd user service may depend on or reference `sway-session.target` only if it is specific for sway. Otherwise, it's recommended to use `graphical-session.target`.

### Session script

The [`session.sh`](./src/session.sh) script is responsible for importing variables into systemd and dbus activation environments and starting session target. When the `--with-cleanup` argument is specified, it also waits in the background until the compositor exits, stops the session target and unsets variables for systemd user session.

The script itself does not set any variables except `XDG_CURRENT_DESKTOP`; it simply passes the values received from sway. The list of variables and the name of the session target are currently hardcoded and could be changed by editing the script.

For a better description see [comments in the code](./src/session.sh).

### Cgroups assignment script

The [`assign-cgroups.py`](./src/assign-cgroups.py) script subscribes to a new window i3 ipc event and automatically creates a transient scope unit (with path `app.slice/app-${app_id}.slice/app-${app_id}-${pid}.scope`) for each GUI application launched in the same cgroup as the compositor. Existing child processes of the application are assigned to the same scope.

The script is necessary to overcome a limitation of `systemd-oomd`: it only tracks resource usage by cgroups and kills the whole group when a single application misbehaves and exceeds resource usage limits. By placing individual apps into isolated cgroups we are decreasing the chance that oomd killer would target the group with the compositor and accidentally terminate the session.

It can also be used to impose resource usage limits on a specific application, because transient units are still loading override configs.\
For example, by creating `$XDG_CONFIG_HOME/systemd/user/app-firefox.slice.d/override.conf` with content

```ini
[Slice]
MemoryHigh=2G
```

you can tell systemd that all Firefox processes combined are not allowed to use more than 2 Gb of memory.
See [`systemd.resource-control(5)`](https://www.freedesktop.org/software/systemd/man/systemd.resource-control.html) for other available resource control options.

### Keyboard layout configuration

The [`locale1-xkb-config`](./src/locale1-xkb-config) script reads the system-wide input configuration from
[`org.freedesktop.locale1`](https://www.freedesktop.org/software/systemd/man/org.freedesktop.locale1.html)
systemd interface, translates it into a Sway configuration and applies to all devices with type:keyboard.

The main motivation for this component was an ability to apply system-wide keyboard mappings configured in the OS installer
to a greetd or SDDM greeter running with Sway as a display server.

The component is not enabled by default, use `-Dlocale1=true` to install the helper script and check
[`95-system-keyboard-config.conf`](./config.d/95-system-keyboard-config.conf.in) for necessary configuration.

### XDG Desktop autostart target

The `sway-xdg-autostart.target` wraps systemd bultin [`xdg-desktop-autostart.target`](https://www.freedesktop.org/software/systemd/man/systemd.special.html#xdg-desktop-autostart.target) to allow delayed start from a script.

The `xdg-desktop-autostart.target` contains units generated by [`systemd-xdg-autostart-generator(8)`](https://www.freedesktop.org/software/systemd/man/systemd-xdg-autostart-generator.html) from XDG desktop files in autostart directories.
The recommended way to start it is a `Wants=xdg-desktop-autostart.target` in a Desktop Environment session target (`sway-session.target` in our case), but there are some issues with that approach.

Most notably, there's a race between the autostarted applications and the panel with StatusNotifierHost implementation.
SNI specification is very clear on that point; if the `StatusNotifierWatcher` is unavailable or `IsStatusNotifierHostRegistered` is not set, the application should fallback to XEmbed tray. There are even known implementations that follow this requirement (Qt...) and will fail to create a status icon if started before the panel.

The component is not enabled by default, use `-Dautostart=true` to install the target and the helper script
and check [`95-xdg-desktop-autostart.conf`](./config.d/95-xdg-desktop-autostart.conf.in) for necessary configuration.

## Installation

<a href="https://repology.org/project/sway-systemd/versions">
    <img src="https://repology.org/badge/vertical-allrepos/sway-systemd.svg" alt="Packaging status" align="right">
</a>

### Dependencies

Session script calls these commands: `swaymsg`, `systemctl`, `dbus-update-activation-environment`.

Cgroups script uses following python packages:
[`dbus-next`](https://pypi.org/project/dbus-next/),
[`i3ipc`](https://pypi.org/project/i3ipc/),
[`psutil`](https://pypi.org/project/psutil/),
[`tenacity`](https://pypi.org/project/tenacity/),
[`python-xlib`](https://pypi.org/project/python-xlib/)

### Installing with meson

```
meson build
sudo ninja -C build install
```

Only the session part is installed by default. Pass `-Dcgroups=enabled` to the `meson build` command to install cgroups assignment script as well.

The command will install configuration files from [`config.d`](./config.d/) to the `/etc/sway/config.d/` directory which is included from the default sway config. If you are using custom sway configuration file and already removed the `include /etc/sway/config.d/*` line you may need to edit your config and include the installed files.

### Installing manually/using directly from git checkout

1. Clone repository.
2. Copy `sway-session.target` to the systemd user unit directory (`/usr/lib/systemd/user/`, `$XDG_CONFIG_HOME/systemd/user/` or `~/.config/systemd/user` are common locations).
3. Run `systemctl --user daemon-reload` to make systemd rescan the service files.
4. Add `exec /path/to/cloned/repo/src/session.sh --with-cleanup` to your sway config for environment and session configuration.
5. Add `exec /path/to/cloned/repo/src/assign-cgroups.py` to your sway config to enable cgroup assignment script.
6. Restart your sway session or run `swaymsg` with the commands above. Simple config reload is insufficient as it does not execute `exec` commands.
