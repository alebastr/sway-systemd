//! Automatically assign a dedicated systemd scope to the GUI applications
//! launched in the same cgroup as the compositor. This could be helpful for
//! implementing cgroup-based resource management and would be necessary when
//! `systemd-oomd` is in use.
//!
//! Limitations: The script is using i3ipc window:new event to detect application
//! launches and would fail to detect background apps or special surfaces.
//! Therefore it's recommended to supplement the script with use of systemd user
//! services for such background apps.

use clap::{ArgEnum, Parser};
use dbus::{
    arg::{RefArg, Variant},
    blocking::Connection as DbusConnection,
};
use futures_util::StreamExt;
use log::{debug, error, info, warn};
use std::sync::Arc;
use std::{
    fs, io,
    os::unix::{io::AsRawFd, net::UnixStream},
    time::Duration,
};
use swayipc_async::{Connection as SwayConnection, Event, EventType, WindowChange, WindowEvent};
use tokio::sync::Mutex;

const SD_BUS_NAME: &str = "org.freedesktop.systemd1";
const SD_OBJECT_PATH: &str = "/org/freedesktop/systemd1";
const SD_MANAGER_IFACE: &str = "org.freedesktop.systemd1.Manager";

const SD_SLICE_FORMAT: &str = "app-{app_id}.slice";
const SD_UNIT_FORMAT: &str = "app-{app_id}-{unique}.scope";

/// Launcher apps that create their own cgroup; windows spawned from them
/// should still be moved to a new scope.
const LAUNCHER_APPS: &[&str] = &["nwgbar", "nwgdmenu", "nwggrid", "onagre"];

#[repr(C)]
struct UCred {
    pid: i32,
    uid: u32,
    gid: u32,
}

fn get_pid_by_socket(path: &str) -> io::Result<u32> {
    let sock = UnixStream::connect(path)?;
    let mut ucred = UCred {
        pid: 0,
        uid: 0,
        gid: 0,
    };
    let mut len = std::mem::size_of::<UCred>() as libc::socklen_t;
    let ret = unsafe {
        libc::getsockopt(
            sock.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            &mut ucred as *mut UCred as *mut libc::c_void,
            &mut len,
        )
    };
    if ret != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(ucred.pid as u32)
}

// cgroup helpers

fn get_cgroup(pid: u32) -> Option<String> {
    let text = fs::read_to_string(format!("/proc/{pid}/cgroup")).ok()?;
    // cgroupv2: single line "0::<path>"; v1 has multiple lines — take last field
    text.lines()
        .next()
        .and_then(|l| l.split(':').last())
        .map(|s| s.trim().to_string())
}

/// Walk /proc/<pid>/task/<tid>/children recursively.
fn collect_child_pids(root: u32) -> Vec<u32> {
    let mut result = Vec::new();
    let mut stack = vec![root];
    while let Some(pid) = stack.pop() {
        let task_dir = format!("/proc/{pid}/task");
        let Ok(entries) = fs::read_dir(&task_dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let children_path = entry.path().join("children");
            let Ok(data) = fs::read_to_string(&children_path) else {
                continue;
            };
            for token in data.split_whitespace() {
                if let Ok(child) = token.parse::<u32>() {
                    result.push(child);
                    stack.push(child);
                }
            }
        }
    }
    result
}

fn escape_app_id(app_id: &str) -> String {
    let mut out = String::with_capacity(app_id.len() * 2);
    for b in app_id.bytes() {
        match b {
            b'a'..=b'z' | b'A'..=b'Z' | b'0'..=b'9' | b':' | b'.' | b'\\' | b'_' => {
                out.push(b as char);
            }
            other => {
                out.push_str(&format!("\\x{:02x}", other));
            }
        }
    }
    out
}

struct CGroupHandler {
    compositor_cgroup: String,
    launcher_cgroups: Vec<String>,
    dbus_conn: Arc<Mutex<DbusConnection>>,
}

impl CGroupHandler {
    fn new(compositor_cgroup: String, dbus_conn: Arc<Mutex<DbusConnection>>) -> Self {
        let launcher_cgroups = LAUNCHER_APPS
            .iter()
            .map(|app| SD_SLICE_FORMAT.replace("{app_id}", &escape_app_id(app)))
            .collect();
        Self {
            compositor_cgroup,
            launcher_cgroups,
            dbus_conn,
        }
    }

    fn cgroup_change_needed(&self, cgroup: Option<&str>) -> bool {
        let Some(cg) = cgroup else { return false };
        if cg == self.compositor_cgroup {
            return true;
        }
        for launcher in &self.launcher_cgroups {
            if cg.contains(launcher.as_str()) {
                return true;
            }
        }
        false
    }

    /// Assign `pid` and unassigned children to a transient systemd scope.
    /// Retries up to 3 times on D-Bus error
    async fn assign_scope(
        &self,
        app_id: &str,
        pid: u32,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        let escaped = escape_app_id(app_id);
        let sd_slice = SD_SLICE_FORMAT.replace("{app_id}", &escaped);
        let sd_unit = SD_UNIT_FORMAT
            .replace("{app_id}", &escaped)
            .replace("{unique}", &pid.to_string());

        let mut pids: Vec<u32> = Vec::new();
        if self.cgroup_change_needed(get_cgroup(pid).as_deref()) {
            pids.push(pid);
        }
        for child in collect_child_pids(pid) {
            if self.cgroup_change_needed(get_cgroup(child).as_deref()) {
                pids.push(child);
            }
        }

        let sd_unit_c = sd_unit.clone();
        let sd_slice_c = sd_slice.clone();
        let dbus_conn = Arc::clone(&self.dbus_conn);

        let mut last_err: Option<Box<dyn std::error::Error + Send + Sync>> = None;
        for attempt in 1..=3u32 {
            let result =
                call_start_transient_unit(&dbus_conn, &sd_unit_c, &sd_slice_c, pids.clone()).await;

            match result {
                Ok(_) => {
                    debug!(
                        "window {} successfully assigned to cgroup {}/{}",
                        app_id, sd_slice, sd_unit
                    );
                    return Ok(());
                }
                Err(e) => {
                    warn!("assign_scope attempt {attempt}/3 failed: {e}");
                    last_err = Some(e);
                    tokio::time::sleep(Duration::from_millis(100 * attempt as u64)).await;
                }
            }
        }

        Err(last_err.unwrap())
    }

    async fn on_new_window(&self, event: WindowEvent) {
        let con = &event.container;
        let app_id = con.app_id.clone().or_else(|| {
            con.window_properties
                .as_ref()
                .and_then(|wp| wp.class.clone())
        });

        let pid = match con.pid {
            Some(p) if p > 0 => p as u32,
            _ => {
                warn!("Failed to get pid for {:?}", app_id);
                return;
            }
        };

        // Fallback: use process name if app_id is still unknown
        let app_id = app_id.unwrap_or_else(|| {
            fs::read_to_string(format!("/proc/{pid}/comm"))
                .unwrap_or_else(|_| pid.to_string())
                .trim()
                .to_string()
        });

        let cgroup = get_cgroup(pid);
        debug!("window {}({}) cgroup {:?}", app_id, pid, cgroup);

        if self.cgroup_change_needed(cgroup.as_deref()) {
            if let Err(e) = self.assign_scope(&app_id, pid).await {
                error!("Failed to modify cgroup for {}: {}", app_id, e);
            }
        }
    }
}

async fn call_start_transient_unit(
    conn: &Arc<Mutex<DbusConnection>>,
    unit: &str,
    slice: &str,
    pids: Vec<u32>,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let unit = unit.to_string();
    let slice = slice.to_string();
    let conn = Arc::clone(conn);

    tokio::task::spawn_blocking(move || {
        let conn = conn.blocking_lock();
        let proxy = conn.with_proxy(SD_BUS_NAME, SD_OBJECT_PATH, Duration::from_secs(15));

        let properties: Vec<(&str, Variant<Box<dyn RefArg>>)> = vec![
            ("PIDs", Variant(Box::new(pids) as Box<dyn RefArg>)),
            ("Slice", Variant(Box::new(slice) as Box<dyn RefArg>)),
        ];
        let aux: Vec<(&str, Vec<(&str, Variant<Box<dyn RefArg>>)>)> = vec![];

        let (_job,): (dbus::Path,) = proxy.method_call(
            SD_MANAGER_IFACE,
            "StartTransientUnit",
            (unit.as_str(), "fail", properties, aux),
        )?;
        Ok::<(), dbus::Error>(())
    })
    .await
    .map_err(|e| Box::new(e) as Box<dyn std::error::Error + Send + Sync>)?
    .map_err(|e| Box::new(e) as Box<dyn std::error::Error + Send + Sync>)
}

#[derive(Debug, Clone, ArgEnum)]
enum LogLevel {
    Critical,
    Error,
    Warning,
    Info,
    Debug,
}

impl LogLevel {
    fn to_filter(&self) -> log::LevelFilter {
        match self {
            LogLevel::Critical => log::LevelFilter::Error,
            LogLevel::Error => log::LevelFilter::Error,
            LogLevel::Warning => log::LevelFilter::Warn,
            LogLevel::Info => log::LevelFilter::Info,
            LogLevel::Debug => log::LevelFilter::Debug,
        }
    }
}

#[derive(Parser, Debug)]
#[clap(
    name = "assign-cgroups",
    about = "Assign CGroups to apps in compositors with i3/Sway IPC protocol support"
)]
struct Cli {
    #[clap(
        short,
        long,
        arg_enum,
        default_value = "info",
        help = "Set logging level"
    )]
    loglevel: LogLevel,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();

    env_logger::builder()
        .filter_level(cli.loglevel.to_filter())
        .init();

    // D-Bus session connection
    let dbus_conn = Arc::new(Mutex::new(
        DbusConnection::new_session().map_err(|e| format!("DBus connection error: {e}"))?,
    ));

    // Locate Sway socket (for compositor PID via SO_PEERCRED)
    let socket_path = std::env::var("SWAYSOCK")
        .or_else(|_| std::env::var("I3SOCK"))
        .map_err(|_| "SWAYSOCK / I3SOCK not set — is Sway/i3 running?")?;

    // Compositor PID & cgroup
    let compositor_pid = get_pid_by_socket(&socket_path)?;
    let compositor_cgroup = get_cgroup(compositor_pid).ok_or("Could not read compositor cgroup")?;
    info!("compositor:{} {}", compositor_pid, compositor_cgroup);

    let handler = Arc::new(CGroupHandler::new(compositor_cgroup, dbus_conn));

    // Subscribe to window events via swayipc-async
    let mut events = SwayConnection::new()
        .await
        .map_err(|e| format!("Sway IPC connection error: {e}"))?
        .subscribe([EventType::Window])
        .await
        .map_err(|e| format!("Sway IPC subscribe error: {e}"))?;

    info!("Subscribed to window events on {}", socket_path);

    // Event loop
    while let Some(event) = events.next().await {
        let ev = match event {
            Ok(e) => e,
            Err(e) => {
                error!("Sway IPC error: {}", e);
                break;
            }
        };

        if let Event::Window(ev) = ev {
            if ev.change == WindowChange::New {
                let handler = Arc::clone(&handler);
                tokio::spawn(async move {
                    handler.on_new_window(*ev).await;
                });
            }
        }
    }

    Ok(())
}
