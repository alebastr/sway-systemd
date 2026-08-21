#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <stdint.h>
#include <systemd/sd-bus.h>

#define LOCALE1_SERVICE "org.freedesktop.locale1"
#define LOCALE1_PATH "/org/freedesktop/locale1"
#define LOCALE1_IFACE "org.freedesktop.locale1"
#define PROPERTIES_IFACE "org.freedesktop.DBus.Properties"

#define SWAY_IPC_MAGIC "i3-ipc"
#define SWAY_IPC_MAGIC_LEN 6
#define SWAY_IPC_RUN_COMMAND 0

static int sway_fd = -1;

static int sway_ipc_connect(void)
{
    const char* sockpath = getenv("SWAYSOCK");
    if (!sockpath) {
        fprintf(stderr, "SWAYSOCK not set\n");
        return -1;
    }

    sway_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (sway_fd < 0) {
        perror("socket");
        return -1;
    }

    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    if (strlen(sockpath) >= sizeof(addr.sun_path)) {
        fprintf(stderr, "SWAYSOCK path too long\n");
        close(sway_fd);
        sway_fd = -1;
        return -1;
    }
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", sockpath);

    if (connect(sway_fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        perror("connect to sway");
        close(sway_fd);
        sway_fd = -1;
        return -1;
    }

    return 0;
}

static void sway_ipc_disconnect(void)
{
    if (sway_fd >= 0) {
        close(sway_fd);
        sway_fd = -1;
    }
}

static int sway_ipc_send(const char* cmd)
{
    if (sway_fd < 0)
        return -1;

    uint32_t len = strlen(cmd);
    uint32_t type = SWAY_IPC_RUN_COMMAND;

    if (write(sway_fd, SWAY_IPC_MAGIC, SWAY_IPC_MAGIC_LEN) < 0 ||
        write(sway_fd, &len, sizeof(len)) < 0 ||
        write(sway_fd, &type, sizeof(type)) < 0 ||
        write(sway_fd, cmd, len) < 0) {
        perror("write to sway");
        return -1;
    }

    // Read response header
    char resp_magic[SWAY_IPC_MAGIC_LEN];
    uint32_t resp_len, resp_type;
    if (read(sway_fd, resp_magic, SWAY_IPC_MAGIC_LEN) < SWAY_IPC_MAGIC_LEN ||
        read(sway_fd, &resp_len, sizeof(resp_len)) < (ssize_t)sizeof(resp_len) ||
        read(sway_fd, &resp_type, sizeof(resp_type)) < (ssize_t)sizeof(resp_type)) {
        perror("read from sway");
        return -1;
    }

    // Drain response payload
    char buf[512];
    while (resp_len > 0) {
        ssize_t n = read(sway_fd, buf, resp_len < sizeof(buf) ? resp_len : sizeof(buf));
        if (n <= 0) break;
        resp_len -= n;
    }

    return 0;
}

static const struct { const char* prop; const char* param; } prop_map[] = {
    { "X11Layout",  "xkb_layout" },
    { "X11Model",   "xkb_model" },
    { "X11Variant", "xkb_variant" },
    { "X11Options", "xkb_options" },
};

#define PROP_MAP_LEN (sizeof(prop_map) / sizeof(prop_map[0]))

static const char* lookup_sway_param(const char* prop)
{
    for (size_t i = 0; i < PROP_MAP_LEN; i++)
        if (strcmp(prop_map[i].prop, prop) == 0)
            return prop_map[i].param;
    return NULL;
}

static void apply_xkb_param(const char* setting, const char* value)
{
    char cmd[256];
    snprintf(cmd, sizeof(cmd), "input type:keyboard %s \"%s\"", setting, value);
    printf("send: %s\n", cmd);
    sway_ipc_send(cmd);
}

static void read_and_apply_all(sd_bus* bus)
{
    sd_bus_error error = SD_BUS_ERROR_NULL;
    char* value = NULL;

    for (size_t i = 0; i < PROP_MAP_LEN; i++) {
        int result = sd_bus_get_property_string(
            bus, LOCALE1_SERVICE, LOCALE1_PATH, LOCALE1_IFACE,
            prop_map[i].prop, &error, &value
        );
        if (result < 0) {
            fprintf(stderr, "Failed to read %s: %s\n", prop_map[i].prop, error.message);
            sd_bus_error_free(&error);
            continue;
        }
        apply_xkb_param(prop_map[i].param, value);
        free(value);
        value = NULL;
    }
}

static int on_properties_changed(sd_bus_message* msg, void* userdata, sd_bus_error* error)
{
    sd_bus* bus = userdata;

    const char* iface = NULL;
    int result = sd_bus_message_read(msg, "s", &iface);
    if (result < 0 || strcmp(iface, LOCALE1_IFACE) != 0)
        return 0;

    // Parse changed_properties: a{sv}
    result = sd_bus_message_enter_container(msg, 'a', "{sv}");
    if (result < 0) return 0;

    while ((result = sd_bus_message_enter_container(msg, 'e', "sv")) > 0) {
        const char* prop = NULL;
        result = sd_bus_message_read(msg, "s", &prop);
        if (result < 0) break;

        const char* param = lookup_sway_param(prop);
        if (param) {
            const char* value = NULL;
            result = sd_bus_message_read(msg, "v", "s", &value);
            if (result >= 0)
                apply_xkb_param(param, value);
        } else {
            result = sd_bus_message_skip(msg, "v");
        }
        if (result < 0) break;

        result = sd_bus_message_exit_container(msg);
        if (result < 0) break;
    }

    sd_bus_message_exit_container(msg);

    // Parse invalidated_properties: re-read these from D-Bus
    result = sd_bus_message_enter_container(msg, 'a', "s");
    if (result >= 0) {
        sd_bus_error error = SD_BUS_ERROR_NULL;
        const char* prop = NULL;
        while ((result = sd_bus_message_read(msg, "s", &prop)) > 0) {
            const char* param = lookup_sway_param(prop);
            if (!param) continue;

            char* value = NULL;
            result = sd_bus_get_property_string(
                bus, LOCALE1_SERVICE, LOCALE1_PATH, LOCALE1_IFACE,
                prop, &error, &value
            );
            if (result < 0) {
                fprintf(stderr, "Failed to read %s: %s\n", prop, error.message);
                sd_bus_error_free(&error);
                continue;
            }
            apply_xkb_param(param, value);
            free(value);
        }
        sd_bus_message_exit_container(msg);
    }

    return 0;
}

int main(int argc, char* argv[])
{
    bool watch = false;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--watch") == 0)
            watch = true;
        else {
            fprintf(stderr, "Usage: %s [--watch]\n", argv[0]);
            return 1;
        }
    }

    sd_bus* bus = NULL;
    sd_bus_slot* slot = NULL;

    if (sway_ipc_connect() < 0)
        return 1;

    int result = sd_bus_open_system(&bus);
    if (result < 0) {
        fprintf(stderr, "Failed to connect to system bus: %s\n", strerror(-result));
        sway_ipc_disconnect();
        return result;
    }

    read_and_apply_all(bus);

    if (watch) {
        result = sd_bus_match_signal(
            bus, &slot,
            LOCALE1_SERVICE, LOCALE1_PATH, PROPERTIES_IFACE, "PropertiesChanged",
            on_properties_changed, bus
        );
        if (result < 0) {
            fprintf(stderr, "Failed to add match: %s\n", strerror(-result));
            sd_bus_unref(bus);
            return result;
        }

        printf("Watching org.freedesktop.locale1 for changes...\n");

        while (true) {
            result = sd_bus_wait(bus, UINT64_MAX);
            if (result < 0) {
                fprintf(stderr, "Wait error: %s\n", strerror(-result));
                break;
            }
            result = sd_bus_process(bus, NULL);
            if (result < 0) {
                fprintf(stderr, "Process error: %s\n", strerror(-result));
                break;
            }
        }

        sd_bus_slot_unref(slot);
    }

    sd_bus_unref(bus);
    sway_ipc_disconnect();
    return 0;
}
