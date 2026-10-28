.section .text.glue,"ax",@progbits
.global ch_v13_boundary_glue
.global ch_v13_ready_glue

/* Replaces r8 += r7; r5 += r7 AFTER memcpy and both staging-counter clears.
 * The parent builder already saved rets and r4-r15. The C call preserves its
 * live callee-saved registers; r0-r3 are dead at the original continuation.
 */
ch_v13_boundary_glue:
    r0 = r8
    r1 = r7
    r8 += r7
    r5 += r7
    call ch_v13_host_boundary
    if (r0 == 0) goto boundary_continue
    goto v13_packet_return
boundary_continue:
    goto v13_packet_continue

/* Replaces the original call to usb_status. r5=port and r6=packet length.
 * This point precedes the original IRQ-disable call at 0x1e119d4.
 */
ch_v13_ready_glue:
    [--sp] = rets
    r0 = r5
    r1 = r6
    call ch_v13_host_ready
    pc = [sp++]

.section .hook_boundary,"ax",@progbits
    goto ch_v13_boundary_glue
.section .hook_ready,"ax",@progbits
    call ch_v13_ready_glue
