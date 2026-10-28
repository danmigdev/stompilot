/* Exercise the exact linked C, with native calls stubbed on the host. */
#include "sysex_pacing.h"
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

unsigned int v13_output_callback;
static unsigned char buffer[64];
static unsigned checks, waits, status_calls;
static int status, disconnect_on_wait, null_buffer;

const unsigned char *v13_usb_buffer(unsigned int port)
{
    assert(port == 1);
    return null_buffer ? NULL : buffer;
}
int v13_usb_status(void)
{
    ++status_calls;
    return status;
}
void v13_task_delay(int ticks)
{
    assert(ticks == 3 && status_calls == 0);
    ++waits;
    if (disconnect_on_wait)
        status = 0;
}
static void ready(unsigned length, unsigned expected)
{
    unsigned char before[64];
    int result;
    memcpy(before, buffer, sizeof(buffer));
    waits = status_calls = 0;
    result = ch_v13_host_ready(1, length);
    assert(result == status && status_calls == 1 && waits == expected);
    assert(memcmp(before, buffer, sizeof(buffer)) == 0);
    ++checks;
}
static void boundary(const unsigned char *data, unsigned length, int expected)
{
    assert(ch_v13_host_boundary(data, length) == expected);
    ++checks;
}
int main(void)
{
    unsigned cin, cable, value, length, pos;
    v13_output_callback = CH_NATIVE_HOST_CALLBACK;
    status = 4;
    /* Exhaustive CIN/byte combinations, including every cable number. */
    for (cable = 0; cable < 16; ++cable)
        for (cin = 0; cin < 16; ++cin)
            for (value = 0; value < 256; ++value) {
                memset(buffer, 0, sizeof(buffer));
                buffer[0] = (unsigned char)(cable * 16 + cin);
                for (pos = 1; pos < 4; ++pos)
                    buffer[pos] = (unsigned char)value;
                boundary(buffer, 4, cin >= 5 && cin <= 7 && value == 0xf7);
                ready(4, (cin == 4 || cin == 6 || cin == 7) && value == 0xf0);
            }
    /* Start anywhere in a transfer, with at most one wait per transfer. */
    for (length = 4; length <= 64; length += 4)
        for (pos = 0; pos < length; pos += 4) {
            memset(buffer, 0, sizeof(buffer));
            buffer[pos] = 4; buffer[pos + 1] = 0xf0;
            ready(length, 1);
            buffer[0] = 6; buffer[1] = 0xf0; buffer[2] = 0xf7;
            ready(length, 1);
        }
    /* Invalid lengths must not read outside the packet. */
    for (length = 0; length <= 128; ++length) {
        if (length >= 4 && length <= 64 && !(length & 3))
            continue;
        boundary(NULL, length, 0);
        ready(length, 0);
    }
    boundary(NULL, UINT32_MAX, 0);
    ready(UINT32_MAX, 0);
    null_buffer = 1;
    ready(4, 0);
    null_buffer = 0;
    /* CC, continuation and end packets do not acquire a delay. */
    memset(buffer, 0, sizeof(buffer));
    buffer[0] = 0x0b; buffer[1] = 0xb0; buffer[2] = 0; buffer[3] = 0x7f;
    ready(4, 0);
    buffer[0] = 4; buffer[1] = 0x11;
    ready(4, 0);
    buffer[0] = 7; buffer[3] = 0xf7;
    ready(4, 0);
    boundary(buffer, 4, 1);
    /* U mode always takes the original builder continuation. */
    v13_output_callback = 0x1e10178;
    boundary(buffer, 4, 0);
    v13_output_callback = 0;
    boundary(NULL, 4, 0);
    v13_output_callback = CH_NATIVE_HOST_CALLBACK;
    /* Disconnect during the wait is observed by the stock status check. */
    buffer[0] = 4; buffer[1] = 0xf0;
    disconnect_on_wait = 1;
    ready(4, 1);
    assert(status == 0);
    printf("{\"checks\":%u,\"gap_ticks\":%d,\"usb_packet_limit\":%u,"
           "\"native_execution\":false}\n", checks, CH_SYSEX_GAP_TICKS, CH_USB_PACKET_LIMIT);
    return 0;
}
