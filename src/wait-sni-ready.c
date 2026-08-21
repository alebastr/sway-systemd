#include <stdio.h>
#include <systemd/sd-bus.h>

#define SERVICE "org.kde.StatusNotifierWatcher"
#define OBJECT_PATH "/StatusNotifierWatcher"
#define INTERFACE SERVICE // interface name is the same as the service name
#define SIGNAL_NAME "StatusNotifierHostRegistered"
#define PROPERTY "Is" SIGNAL_NAME

static int check_host_registered(sd_bus* bus)
{
    sd_bus_error error = SD_BUS_ERROR_NULL;
    int value = 0;

    int result = sd_bus_get_property_trivial(
        bus,
        SERVICE, OBJECT_PATH, INTERFACE, PROPERTY,
        &error, 'b', &value
    );
    sd_bus_error_free(&error);
    if (result < 0) return 0;
    return value;
}

static int signal_handler(sd_bus_message* msg, void* userdata, sd_bus_error* error)
{
    *(int*)userdata = 1;
    printf("Signal received, exiting.\n");
    return 0;
}

int main(void)
{
    sd_bus* bus = NULL;
    sd_bus_slot* slot = NULL;
    int done = 0;

    int result = sd_bus_open_user(&bus);
    if (result < 0)
    {
        fprintf(stderr, "Connection error: %s\n", strerror(-result));
        return 1;
    }

    // Check if the host is already registered
    if (check_host_registered(bus))
    {
        printf("Status notifier host already registered.\n");
        sd_bus_unref(bus);
        return 0;
    }

    // Add a match rule for the signal we're interested in
    result = sd_bus_match_signal(
        bus, &slot,
        SERVICE, OBJECT_PATH, INTERFACE, SIGNAL_NAME,
        signal_handler, &done
    );
    if (result < 0)
    {
        fprintf(stderr, "Match error: %s\n", strerror(-result));
        sd_bus_unref(bus);
        return 1;
    }

    printf("Waiting for signal %s on interface %s...\n", SIGNAL_NAME, INTERFACE);

    // Wait for the signal (blocking)
    while (!done)
    {
        result = sd_bus_wait(bus, UINT64_MAX);
        if (result < 0)
        {
            fprintf(stderr, "Wait error: %s\n", strerror(-result));
            break;
        }
        result = sd_bus_process(bus, NULL);
        if (result < 0)
        {
            fprintf(stderr, "Process error: %s\n", strerror(-result));
            break;
        }
    }

    sd_bus_slot_unref(slot);
    sd_bus_unref(bus);
    return done ? 0 : 1;
}
