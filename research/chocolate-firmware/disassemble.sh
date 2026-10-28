#!/usr/bin/env bash
# Offline only; requires the already available JieLi Linux toolchain and Python 3.
set -euo pipefail
study=$(cd -- "$(dirname -- "$0")" && pwd)
toolchain=${1:?Pass the JieLi toolchain root containing pi32v2/ and common/}
toolchain=$(cd -- "$toolchain" && pwd)
version=${2:-013}
case "$version" in 012|013) ;; *) printf 'Expected version 012 or 013\n' >&2; exit 1;; esac
cd "$study/artifacts/inspection-$version"
cat > app-wrap.S <<'ASM'
.section .fw,"ax",@progbits
.global _fw
_fw:
.incbin "flash.bin-files/app.bin"
ASM
"$toolchain/pi32v2/bin/cc" -mcpu=r3 -c app-wrap.S -o app-wrap.o
python3 - <<'PY'
from pathlib import Path
import struct
p = Path('app-wrap.o')
data = bytearray(p.read_bytes())
assert data[:7] == b'\x7fELF\x01\x01\x01'
assert struct.unpack_from('<H', data, 18)[0] == 241
table = struct.unpack_from('<I', data, 32)[0]
size, count, string_index = struct.unpack_from('<HHH', data, 46)
start, length = struct.unpack_from('<II', data, table + string_index * size + 16)
names = data[start:start + length]
found = 0
for index in range(count):
    pos = table + index * size
    name_offset = struct.unpack_from('<I', data, pos)[0]
    if names[name_offset:].split(b'\0')[0] == b'.fw':
        struct.pack_into('<I', data, pos + 12, 0x01e00120)
        found += 1
assert found == 1
p.write_bytes(data)
PY
# Mask branch destinations to the processor address space, including ROM calls.
"$toolchain/common/bin/objdump" --mcpu=r3 --address-mask=0x1ffffff \
    -d --section=.fw app-wrap.o > app-disassembly.txt
printf 'Disassembly: %s/artifacts/inspection-%s/app-disassembly.txt\n' "$study" "$version"
