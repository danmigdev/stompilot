/* V13 USB HOST pacing. Called only by the stock MIDI routing task.
 * No delay in the switch scanner, an ISR, or an IRQ-disabled section.
 */
#include "sysex_pacing.h"

extern unsigned int v13_output_callback;
extern const unsigned char *v13_usb_buffer(unsigned int port);
extern int v13_usb_status(void);
extern void v13_task_delay(int ticks);

static int valid_length(unsigned int length)
{
    return length >= 4 && length <= CH_USB_PACKET_LIMIT && !(length & 3);
}

/* Stop the USB builder at a SysEx end after copying its staging chunk.
 * Its ring reader, running status, counters and epilogue retain ownership
 * of state. In device mode the builder follows its original control flow.
 */
int ch_v13_host_boundary(const unsigned char *chunk, unsigned int length)
{
    unsigned int cin;
    if (v13_output_callback != CH_NATIVE_HOST_CALLBACK || !valid_length(length))
        return 0;
    chunk += length - 4;
    cin = chunk[0] & 15;
    return cin >= 5 && cin <= 7 && chunk[cin - 4] == 0xf7;
}

/* BEFORE the stock status check and critical section. A completion event
 * (or idle-output event) reaches this sender through midi_route. Waiting
 * before a SysEx start covers slow previous transfers without guessing
 * when they finish. CC/PC and SysEx continuation packets are not delayed.
 */
int ch_v13_host_ready(unsigned int port, unsigned int length)
{
    const unsigned char *packet;
    unsigned int pos, cin;
    if (valid_length(length)) {
        packet = v13_usb_buffer(port);
        if (packet) {
            for (pos = 0; pos < length; pos += 4) {
                cin = packet[pos] & 15;
                if ((cin == 4 || cin == 6 || cin == 7) && packet[pos + 1] == 0xf0) {
                    v13_task_delay(CH_SYSEX_GAP_TICKS);
                    break;
                }
            }
        }
    }
    /* Recheck connection status after yielding, as in the original sender. */
    return v13_usb_status();
}
