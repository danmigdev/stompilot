#ifndef CH_SYSEX_PACING_H
#define CH_SYSEX_PACING_H
#define CH_USB_PACKET_LIMIT 64u
#define CH_SYSEX_GAP_TICKS 3
#define CH_NATIVE_HOST_CALLBACK 0x1e119aeu
int ch_v13_host_boundary(const unsigned char *, unsigned int);
int ch_v13_host_ready(unsigned int port, unsigned int length);
#endif
