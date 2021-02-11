# Systemd integration for Sway

## Goals and requirements

The goal of this project is to provide a minimal set of configs and scripts required for running [Sway](https://swaywm.org/) in a systemd environment.
This includes several areas of integration:

### Environment

- Certain variables should be propagated from the compositor to a systemd user session and DBus activation environment.
  Examples of such variables: `DISPLAY`, `WAYLAND_DISPLAY`, `SWAYSOCK`, ...
- Unfiltered import of the whole environment could be ignored by `systemd` if even a single value does not pass the validity check.
- `XDG_CURRENT_DESKTOP` should _preferably_ be initialized with something that allows `xdg-desktop-portal` service to pick the right backend.
- It would be nice to clear or restore the previous values of the variables listed above at the compositor exit.

### Session target

- systemd forbids starting the `graphical-session.target` directly and encourages use of an environment-specific target units. There should be a `sway-session.target` that binds to `graphical-session.target`.
- `sway-session.target` should be started when the compositor is ready and the user-session environment is set.
- It would be nice to stop the `sway-session.target` when the compositor exits.

### Control groups and systemd-oomd

- `systemd-oomd` would terminate the whole cgroup of a process exceeding the memory usage limits. Yes, including the compositor.
- We would like to avoid that and place resource-consuming applications into dedicated cgroups or transient units.
- This could be done before or after starting the application. In the latter case it's our responsibility to assign all child processes into the same group.

## Non-goals

Running the compositor itself as an user service. Besides, [sway-services](https://github.com/xdbob/sway-services/) already exists and does exactly that.
