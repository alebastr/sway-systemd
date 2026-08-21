use std::env;
use std::time::Duration;

use dbus::arg::{PropMap, RefArg, Variant};
use dbus::blocking::Connection;
use dbus::message::MatchRule;
use dbus::Message;
use swayipc::Connection as SwayConnection;

const LOCALE1_SERVICE: &str = "org.freedesktop.locale1";
const LOCALE1_PATH: &str = "/org/freedesktop/locale1";
const LOCALE1_IFACE: &str = "org.freedesktop.locale1";
const PROPERTIES_IFACE: &str = "org.freedesktop.DBus.Properties";

const PROP_MAP: &[(&str, &str)] = &[
    ("X11Layout", "xkb_layout"),
    ("X11Model", "xkb_model"),
    ("X11Variant", "xkb_variant"),
    ("X11Options", "xkb_options"),
];

fn lookup_sway_param(prop: &str) -> Option<&'static str> {
    PROP_MAP
        .iter()
        .find(|(p, _)| *p == prop)
        .map(|(_, param)| *param)
}

fn apply_xkb_param(sway: &mut SwayConnection, setting: &str, value: &str) {
    let cmd = format!("input type:keyboard {setting} \"{value}\"");
    println!("send: {cmd}");
    if let Err(e) = sway.run_command(&cmd) {
        eprintln!("Failed to send command: {e}");
    }
}

fn read_and_apply_all(conn: &Connection, sway: &mut SwayConnection) {
    let proxy = conn.with_proxy(LOCALE1_SERVICE, LOCALE1_PATH, Duration::from_secs(5));
    use dbus::blocking::stdintf::org_freedesktop_dbus::Properties;

    for &(prop, param) in PROP_MAP {
        match proxy.get::<String>(LOCALE1_IFACE, prop) {
            Ok(value) => apply_xkb_param(sway, param, &value),
            Err(e) => eprintln!("Failed to read {prop}: {e}"),
        }
    }
}

fn handle_properties_changed(conn: &Connection, sway: &mut SwayConnection, msg: &Message) {
    let Ok((iface, changed, invalidated)): Result<(String, PropMap, Vec<String>), _> =
        msg.read3()
    else {
        return;
    };

    if iface != LOCALE1_IFACE {
        return;
    }

    // Apply changed properties directly from the signal
    for (prop, value) in &changed {
        if let Some(param) = lookup_sway_param(prop) {
            if let Some(s) = variant_as_str(value) {
                apply_xkb_param(sway, param, s);
            }
        }
    }

    // Re-read invalidated properties from D-Bus
    let proxy = conn.with_proxy(LOCALE1_SERVICE, LOCALE1_PATH, Duration::from_secs(5));
    use dbus::blocking::stdintf::org_freedesktop_dbus::Properties;

    for prop in &invalidated {
        if let Some(param) = lookup_sway_param(prop) {
            match proxy.get::<String>(LOCALE1_IFACE, prop) {
                Ok(value) => apply_xkb_param(sway, param, &value),
                Err(e) => eprintln!("Failed to read {prop}: {e}"),
            }
        }
    }
}

fn variant_as_str(v: &Variant<Box<dyn RefArg>>) -> Option<&str> {
    v.0.as_str()
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let watch = match env::args().nth(1).as_deref() {
        None => false,
        Some("--watch") => true,
        Some(_) => {
            eprintln!("Usage: locale1-xkb-config [--watch]");
            std::process::exit(1);
        }
    };

    let mut sway = SwayConnection::new()?;
    let conn = Connection::new_system()?;

    read_and_apply_all(&conn, &mut sway);

    if watch {
        let rule = MatchRule::new_signal(PROPERTIES_IFACE, "PropertiesChanged")
            .with_sender(LOCALE1_SERVICE)
            .with_path(LOCALE1_PATH);

        conn.add_match(rule, move |_: (), conn: &Connection, msg: &Message| {
            handle_properties_changed(conn, &mut sway, msg);
            true
        })?;

        println!("Watching org.freedesktop.locale1 for changes...");

        loop {
            conn.process(Duration::from_millis(1000))?;
        }
    }

    Ok(())
}
