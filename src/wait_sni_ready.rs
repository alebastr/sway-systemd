use std::time::Duration;

use dbus::blocking::Connection;
use dbus::message::MatchRule;
use dbus::Message;

const SERVICE: &str = "org.kde.StatusNotifierWatcher";
const OBJECT_PATH: &str = "/StatusNotifierWatcher";
const INTERFACE: &str = SERVICE;
const SIGNAL_NAME: &str = "StatusNotifierHostRegistered";
const PROPERTY: &str = "IsStatusNotifierHostRegistered";

fn check_host_registered(conn: &Connection) -> bool {
    let proxy = conn.with_proxy(SERVICE, OBJECT_PATH, Duration::from_secs(5));
    use dbus::blocking::stdintf::org_freedesktop_dbus::Properties;
    proxy.get::<bool>(INTERFACE, PROPERTY).unwrap_or_else(|_| false)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let conn = Connection::new_session()?;

    if check_host_registered(&conn) {
        println!("Status notifier host already registered.");
        return Ok(());
    }

    let rule = MatchRule::new_signal(INTERFACE, SIGNAL_NAME)
        .with_sender(SERVICE)
        .with_path(OBJECT_PATH);

    let done = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let done_clone = done.clone();

    conn.add_match(rule, move |_: (), _conn: &Connection, _msg: &Message| {
        println!("Signal received, exiting.");
        done_clone.store(true, std::sync::atomic::Ordering::Relaxed);
        true
    })?;

    println!("Waiting for signal {SIGNAL_NAME} on interface {INTERFACE}...");

    while !done.load(std::sync::atomic::Ordering::Relaxed) {
        conn.process(Duration::from_millis(1000))?;
    }

    Ok(())
}
