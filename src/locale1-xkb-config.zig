const std = @import("std");
const os = std.os;
const log = std.log;
const posix = std.posix;
const sd = @cImport({
    @cInclude("systemd/sd-bus.h");
});

const locale1_service = "org.freedesktop.locale1";
const locale1_path = "/org/freedesktop/locale1";
const locale1_iface = "org.freedesktop.locale1";
const properties_iface = "org.freedesktop.DBus.Properties";

const sway_ipc_magic = "i3-ipc";
const sway_ipc_run_command: u32 = 0;

const prop_map = std.StaticStringMap([:0]const u8).initComptime(.{
    .{ "X11Layout", "xkb_layout" },
    .{ "X11Model", "xkb_model" },
    .{ "X11Variant", "xkb_variant" },
    .{ "X11Options", "xkb_options" },
});

fn lookupSwayParam(prop: [*:0]const u8) ?[:0]const u8 {
    return prop_map.get(std.mem.sliceTo(prop, 0));
}

// --- Sway IPC ---

const SwayIpc = struct {
    fd: posix.fd_t,

    fn connect() !SwayIpc {
        const sockpath = std.posix.getenv("SWAYSOCK") orelse {
            std.log.debug("SWAYSOCK not set\n", .{});
            return error.NoSwaysock;
        };

        const fd = try posix.socket(posix.AF.UNIX, posix.SOCK.STREAM, 0);
        errdefer posix.close(fd);

        var addr: posix.sockaddr.un = .{ .family = posix.AF.UNIX, .path = undefined };
        @memset(&addr.path, 0);

        if (sockpath.len >= addr.path.len) {
            log.debug("SWAYSOCK path too long\n", .{});
            return error.PathTooLong;
        }
        @memcpy(addr.path[0..sockpath.len], sockpath);

        try posix.connect(fd, @ptrCast(&addr), @sizeOf(posix.sockaddr.un));

        return .{ .fd = fd };
    }

    fn disconnect(self: *SwayIpc) void {
        posix.close(self.fd);
        self.fd = -1;
    }

    fn send(self: SwayIpc, cmd: []const u8) !void {
        const len: u32 = @intCast(cmd.len);
        const msg_type: u32 = sway_ipc_run_command;

        _ = try posix.write(self.fd, sway_ipc_magic);
        _ = try posix.write(self.fd, std.mem.asBytes(&len));
        _ = try posix.write(self.fd, std.mem.asBytes(&msg_type));
        _ = try posix.write(self.fd, cmd);

        // Read response header
        var resp_magic: [6]u8 = undefined;
        _ = try readFull(self.fd, &resp_magic);
        var resp_len_buf: [4]u8 = undefined;
        _ = try readFull(self.fd, &resp_len_buf);
        var resp_type_buf: [4]u8 = undefined;
        _ = try readFull(self.fd, &resp_type_buf);

        // Drain response payload
        var remaining = std.mem.bytesToValue(u32, &resp_len_buf);
        var buf: [512]u8 = undefined;
        while (remaining > 0) {
            const to_read = @min(remaining, buf.len);
            const n = posix.read(self.fd, buf[0..to_read]) catch break;
            if (n == 0) break;
            remaining -= @intCast(n);
        }
    }
};

fn readFull(fd: posix.fd_t, buf: []u8) !void {
    var total: usize = 0;
    while (total < buf.len) {
        const n = try posix.read(fd, buf[total..]);
        if (n == 0) return error.UnexpectedEof;
        total += n;
    }
}

// --- D-Bus helpers ---

fn applyXkbParam(ipc: *SwayIpc, setting: [:0]const u8, value: [*:0]const u8) void {
    var cmd_buf: [256]u8 = undefined;
    const cmd = std.fmt.bufPrint(&cmd_buf, "input type:keyboard {s} \"{s}\"", .{
        setting, std.mem.sliceTo(value, 0),
    }) catch return;

    log.info("send: {s}\n", .{cmd});

    ipc.send(cmd) catch |err| {
        log.debug("sway ipc send failed: {}\n", .{err});
    };
}

fn readAndApplyAll(bus: *sd.sd_bus, ipc: *SwayIpc) void {
    const keys = prop_map.keys();
    const values = prop_map.values();
    for (keys, values) |prop, param| {
        var err = std.mem.zeroes(sd.sd_bus_error);
        var value: [*c]u8 = null;

        const result = sd.sd_bus_get_property_string(
            bus,
            locale1_service, locale1_path, locale1_iface,
            prop.ptr, &err, &value,
        );
        if (result < 0) {
            log.debug("Failed to read {s}: {s}\n", .{
                prop,
                if (err.message) |msg| std.mem.sliceTo(msg, 0) else "(unknown)",
            });
            sd.sd_bus_error_free(&err);
            continue;
        }
        defer std.c.free(@ptrCast(value));

        applyXkbParam(ipc, param, @ptrCast(value));
    }
}

fn onPropertiesChanged(msg: ?*sd.sd_bus_message, userdata: ?*anyopaque, _: ?*sd.sd_bus_error) callconv(.c) c_int {
    const ipc: *SwayIpc = @ptrCast(@alignCast(userdata));
    const bus = sd.sd_bus_message_get_bus(msg);

    var iface: [*c]const u8 = null;
    var result = sd.sd_bus_message_read(msg, "s", &iface);
    if (result < 0) return 0;
    if (iface == null) return 0;
    if (std.mem.orderZ(u8, iface.?, locale1_iface) != .eq) return 0;

    // Parse changed_properties: a{sv}
    result = sd.sd_bus_message_enter_container(msg, 'a', "{sv}");
    if (result < 0) return 0;

    while (true) {
        result = sd.sd_bus_message_enter_container(msg, 'e', "sv");
        if (result <= 0) break;

        var prop: [*c]const u8 = null;
        result = sd.sd_bus_message_read(msg, "s", &prop);
        if (result < 0) break;

        if (prop != null) {
            if (lookupSwayParam(prop.?)) |param| {
                var value: [*c]const u8 = null;
                result = sd.sd_bus_message_read(msg, "v", "s", &value);
                if (result >= 0 and value != null)
                    applyXkbParam(ipc, param, value.?);
            } else {
                result = sd.sd_bus_message_skip(msg, "v");
            }
        } else {
            result = sd.sd_bus_message_skip(msg, "v");
        }
        if (result < 0) break;

        result = sd.sd_bus_message_exit_container(msg);
        if (result < 0) break;
    }

    _ = sd.sd_bus_message_exit_container(msg);

    // Parse invalidated_properties: re-read from D-Bus
    result = sd.sd_bus_message_enter_container(msg, 'a', "s");
    if (result >= 0) {
        while (true) {
            var prop: [*c]const u8 = null;
            result = sd.sd_bus_message_read(msg, "s", &prop);
            if (result <= 0) break;
            if (prop == null) continue;

            const param = lookupSwayParam(prop.?) orelse continue;

            var err = std.mem.zeroes(sd.sd_bus_error);
            var value: [*c]u8 = null;
            result = sd.sd_bus_get_property_string(
                bus,
                locale1_service, locale1_path, locale1_iface,
                prop, &err, &value,
            );
            if (result < 0) {
                log.debug("Failed to read {s}: {s}\n", .{
                    std.mem.sliceTo(prop.?, 0),
                    if (err.message) |m| std.mem.sliceTo(m, 0) else "(unknown)",
                });
                sd.sd_bus_error_free(&err);
                continue;
            }
            defer std.c.free(@ptrCast(value));

            applyXkbParam(ipc, param, @ptrCast(value));
        }
        _ = sd.sd_bus_message_exit_container(msg);
    }

    return 0;
}

pub fn main() !u8 {
    var watch = false;
    var args = std.process.args();
    const prog_name = args.next() orelse "locale1-xkb-config";
    while (args.next()) |arg| {
        if (std.mem.eql(u8, arg, "--watch")) {
            watch = true;
        } else {
            log.debug("Usage: {s} [--watch]\n", .{prog_name});
            return 1;
        }
    }

    var sway_ipc = SwayIpc.connect() catch return 1;
    defer sway_ipc.disconnect();

    var bus: ?*sd.sd_bus = null;
    var result = sd.sd_bus_open_system(&bus);
    if (result < 0) {
        log.debug("Failed to connect to system bus: {s}\n", .{
            std.mem.sliceTo(sd.strerror(-result), 0),
        });
        return 1;
    }
    defer _ = sd.sd_bus_unref(bus);

    readAndApplyAll(bus.?, &sway_ipc);

    if (watch) {
        var slot: ?*sd.sd_bus_slot = null;
        result = sd.sd_bus_match_signal(
            bus, &slot,
            locale1_service, locale1_path, properties_iface, "PropertiesChanged",
            onPropertiesChanged, @ptrCast(&sway_ipc),
        );
        if (result < 0) {
            log.debug("Failed to add match: {s}\n", .{
                std.mem.sliceTo(sd.strerror(-result), 0),
            });
            return 1;
        }
        defer _ = sd.sd_bus_slot_unref(slot);

        log.info("Watching org.freedesktop.locale1 for changes...\n", .{});

        while (true) {
            result = sd.sd_bus_wait(bus, std.math.maxInt(u64));
            if (result < 0) {
                log.debug("Wait error: {s}\n", .{
                    std.mem.sliceTo(sd.strerror(-result), 0),
                });
                break;
            }
            result = sd.sd_bus_process(bus, null);
            if (result < 0) {
                log.debug("Process error: {s}\n", .{
                    std.mem.sliceTo(sd.strerror(-result), 0),
                });
                break;
            }
        }
    }

    return 0;
}
