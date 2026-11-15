#!/usr/bin/env bash
set -euo pipefail
study=$(cd -- "$(dirname -- "$0")" && pwd)
tc=${1:?Pass the JieLi toolchain root}
tc=$(cd -- "$tc" && pwd)
mkdir -p "$study/artifacts/spi-probe-build"
cd "$study/artifacts/spi-probe-build"
"$tc/pi32v2/bin/cc" -mcpu=r3 -c "$study/spi_probe_control.S" -o control.o
"$tc/common/bin/objcopy" -O binary -j .text.control control.o control.bin
"$tc/common/bin/objdump" --mcpu=r3 -h -r -d control.o > control-disassembly.txt
cat control-disassembly.txt
