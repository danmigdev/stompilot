#!/usr/bin/env bash
# Build and test offline. No device access or installation.
set -euo pipefail
study=$(cd -- "$(dirname -- "$0")" && pwd)
tc=${1:?Pass the JieLi toolchain root}
tc=$(cd -- "$tc" && pwd)
cd "$study"
build=artifacts/sysex-pacing
mkdir -p "$build"
cc -std=c99 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined \
  -fno-omit-frame-pointer sysex_pacing.c test_sysex_pacing.c \
  -o "$build/test-sysex-pacing"
"$build/test-sysex-pacing" | tee "$build/host-tests.json"
"$tc/pi32v2/bin/cc" -mcpu=r3 -std=c99 -Oz -Wall -Wextra -Werror \
  -ffreestanding -fno-builtin -c sysex_pacing.c -o "$build/pacing.o"
"$tc/pi32v2/bin/cc" -mcpu=r3 -c sysex_pacing_glue.S -o "$build/glue.o"
"$tc/pi32v2/bin/cc" -mcpu=r3 -nostdlib -Wl,-T,sysex_pacing.ld \
  "$build/pacing.o" "$build/glue.o" -o "$build/sysex-pacing.elf"
"$tc/common/bin/objcopy" -O binary -j .patch "$build/sysex-pacing.elf" "$build/sysex-pacing.bin"
for hook in boundary ready; do
  "$tc/common/bin/objcopy" -O binary -j ".hook_$hook" \
    "$build/sysex-pacing.elf" "$build/hook-$hook.bin"
done
"$tc/common/bin/objdump" --mcpu=r3 --address-mask=0x1ffffff -h -t -d \
  "$build/sysex-pacing.elf" > "$build/disassembly.txt"
"$tc/common/bin/objdump" --mcpu=r3 -h "$build/sysex-pacing.elf"
