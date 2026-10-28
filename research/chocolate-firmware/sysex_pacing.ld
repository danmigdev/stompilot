/* Pinned FootCtrlPlus_013 bindings; no added static RAM or flash geometry. */
ENTRY(ch_v13_boundary_glue)
v13_output_callback = 0x194a0;
v13_usb_buffer = 0x1e11932;
v13_usb_status = 0x1e10a24;
v13_task_delay = 0x2117d78;
v13_packet_return = 0x1e1004c;
v13_packet_continue = 0x1e0ff58;
SECTIONS {
  .patch 0x1e265c0 : ALIGN(4) { *(.text*) *(.rodata*) }
  .hook_boundary 0x1e0ff54 : { *(.hook_boundary) }
  .hook_ready 0x1e119cc : { *(.hook_ready) }
  ASSERT(SIZEOF(.hook_boundary) == 4, "Boundary hook changed size")
  ASSERT(SIZEOF(.hook_ready) == 4, "Ready hook changed size")
  ASSERT(SIZEOF(.patch) <= 0x120, "Patch exceeds the reserved candidate extent")
  .unexpected : { *(.data*) *(.bss*) *(COMMON) }
  ASSERT(SIZEOF(.unexpected) == 0, "Patch must not add static RAM")
  /DISCARD/ : { *(.comment) *(.note*) *(.debug*) *(.llvm*) }
}
