#!/bin/sh
#
# Address several issues with DBus activation and systemd user sessions
#
# 1. DBus-activated and systemd services do not share the environment with user
#    login session. In order to make the applications that have GUI or interact
#    with the compositor work as a systemd user service, certain variables must
#    be propagated to the systemd and dbus.
#    Possible (but not exhaustive) list of variables:
#    - DISPLAY - for X11 applications that are started as user session services
#    - WAYLAND_DISPLAY - similarly, this is needed for wayland-native services
#    - I3SOCK/SWAYSOCK - allow services to talk with sway using i3 IPC protocol
#
# 2. `xdg-desktop-portal` requires XDG_CURRENT_DESKTOP to be set in order to
#    select the right implementation for screenshot and screencast portals.
#    With all the numerous ways to start sway, it's not possible to rely on the
#    right value of the XDG_CURRENT_DESKTOP variable within the login session,
#    therefore the script will ensure that it is always set to `sway`.
#
# 3. GUI applications started as a systemd service (or via xdg-autostart-generator)
#    may rely on the XDG_SESSION_TYPE variable to select the backend.
#    Ensure that it is always set to `wayland`.
#
# 4. The common way to autostart a systemd service along with the desktop
#    environment is to add it to a `graphical-session.target`. However, systemd
#    forbids starting the graphical session target directly and encourages use
#    of an environment-specific target units. Therefore, the integration
#    package here provides and uses `sway-session.target` which would bind to
#    the `graphical-session.target`.
#
# 5. Optionally, stop the target and unset the variables when the compositor
#    exits.
#
# Arguments:
#  --with-cleanup   Run optional cleanup code at compositor exit.
#
# References:
#  - https://github.com/swaywm/sway/wiki#gtk-applications-take-20-seconds-to-start
#  - https://github.com/emersion/xdg-desktop-portal-wlr/wiki/systemd-user-services,-pam,-and-environment-variables
#  - https://www.freedesktop.org/software/systemd/man/systemd.special.html#graphical-session.target
#  - https://systemd.io/DESKTOP_ENVIRONMENTS/
#
export XDG_CURRENT_DESKTOP=sway
export XDG_SESSION_TYPE=wayland
VARIABLES="DISPLAY I3SOCK SWAYSOCK WAYLAND_DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_TYPE"
SESSION_TARGET="sway-session.target"

# DBus activation environment is independent from systemd. While most of
# dbus-activated services are already using `SystemdService` directive, some
# still don't and thus we should set the dbus environment with a separate
# command.
if hash dbus-update-activation-environment 2>/dev/null; then
    # shellcheck disable=SC2086
    dbus-update-activation-environment --systemd ${VARIABLES:- --all}
fi

# shellcheck disable=SC2086
systemctl --user import-environment $VARIABLES
systemctl --user start "$SESSION_TARGET"

# Optionally, wait until the compositor exits and cleanup variables and services.
if [ "$1" != "--with-cleanup" ] ||
    [ -z "$SWAYSOCK" ] ||
    ! hash swaymsg 2>/dev/null
then
    exit 0;
fi

# wait until the compositor exits
swaymsg -t subscribe '["shutdown"]'

# stop the session target and unset the variables
systemctl --user stop "$SESSION_TARGET"
if [ -n "$VARIABLES" ]; then
    # shellcheck disable=SC2086
    systemctl --user unset-environment $VARIABLES
fi
