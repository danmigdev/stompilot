/* Temporary RAM callback, never an update payload.
 * r1 == 0: restore the stock USRFLASH pointer, keeping the read callback.
 * r1 != 0: restore the pointer, unregister memory type 5 and clear all scratch.
 * The final jump MUST be a tail jump into ROM: it clears this running code.
 * Caller registers r4..r15 and rets are untouched.
 */
.section .text.control,"ax",@progbits
.global spi_probe_control
spi_probe_control:
    r0 = 0x00039000
    r3 = 0x00019100
    [r3] = r0
    if (r1 == 0) goto done
    r0 = 0
    r3 = 0x00019774
    [r3] = r0
    r0 = 0x00016810
    r1 = 0
    r2 = 96
    r3 = 0x00117f80
    goto r3
done:
    rts
