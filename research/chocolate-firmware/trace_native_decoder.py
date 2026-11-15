"""Offline MIDI traces using V13's actual relocated decoder instructions.

The startup copies app offset 0x24954 to RAM 0x1bd60. This decoder is firmware
code, not a ROM service. Feed raw MIDI bytes through its native ring structure
instead of supplying idealized USB events. USB, scheduler and memcpy remain
stubs; no timing or hardware behavior is established by these traces.
"""
import json
import random
import re

from inspect_firmware import ENTRY_POINT, HERE, RELEASES, require, sha
from trace_sysex_pacing import Machine, STATE, OUTPUT, BUILDER, instructions, payload

DECODER, SOURCE, SIZE = 0x1BD60, 0x1E24A74, 0x234
RING, DATA, CAPACITY = 0x910000, 0x911000, 4096
TABLE, TABLE_SIZE = 0x1E188A0, 0x300


def native_instructions(patched):
    code, symbols = instructions(patched)
    app = (HERE / 'artifacts/inspection-013/flash.bin-files/app.bin').read_bytes()
    require(sha(app) == RELEASES['013']['app_sha256'], 'Unknown original firmware')
    count = 0
    for line in (HERE / 'artifacts/inspection-013/app-disassembly.txt').read_text().splitlines():
        m = re.match(r'\s*([0-9a-f]+):\s+((?:[0-9a-f]{2}\s+)+)(.*)', line)
        if not m:
            continue
        address, data = int(m[1], 16), bytes.fromhex(m[2])
        if not SOURCE <= address < SOURCE + SIZE:
            continue
        require(app[address-ENTRY_POINT:address-ENTRY_POINT+len(data)] == data,
                'Decoder disassembly differs from pinned firmware')
        op = m[3].strip()
        if op.endswith('>'):
            op = op.rsplit(' <', 1)[0]
        op = op.split(' #')[0].strip()
        code[address-SOURCE+DECODER] = len(data), op
        count += 1
    require(count > 150, 'Incomplete decoder disassembly')
    return code, symbols, app


class NativeDecoderMachine(Machine):
    def __init__(self, code, app, midi, host=True, start=0):
        self.table = app[TABLE-ENTRY_POINT:TABLE-ENTRY_POINT+TABLE_SIZE]
        super().__init__(code, host=host)
        require(0 <= start < CAPACITY and len(midi) < CAPACITY, 'Invalid ring fixture')
        self.write(STATE + 2264 + 8, RING, 4)
        self.write(STATE + 2264 + 4, start, 4)
        self.write(RING, (start+len(midi)) % CAPACITY, 4)
        self.write(RING+4, CAPACITY, 4)
        self.write(RING+8, DATA, 4)
        for i, value in enumerate(midi):
            self.write(DATA+(start+i) % CAPACITY, value, 1)

    def read(self, address, size):
        if TABLE <= address <= TABLE + TABLE_SIZE - size:
            return int.from_bytes(self.table[address-TABLE:address-TABLE+size], 'little')
        if RING <= address <= RING+16-size or DATA <= address <= DATA+CAPACITY-size:
            return sum(self.memory.get(address+i, 0) << (i*8) for i in range(size))
        return super().read(address, size)

    def write(self, address, value, size):
        require(not TABLE <= address < TABLE+TABLE_SIZE, 'Write to immutable decoder table')
        super().write(address, value, size)

    def stub(self, target):
        if target == DECODER:
            return False
        return super().stub(target)


def transfer(code, symbols, app, midi, host=True, start=0):
    cpu = NativeDecoderMachine(code, app, midi, host, start)
    transfers = []
    for _ in range(len(midi)+3):
        count = cpu.run(BUILDER, {'r0': OUTPUT})
        require(0 <= count <= 64 and not count % 4, 'Invalid native transfer length')
        if not count:
            require(cpu.read(RING, 4) == cpu.read(STATE+2268, 4), 'Native ring not drained')
            break
        data = bytes(cpu.read(OUTPUT+i, 1) for i in range(count))
        transfers.append(data)
        if symbols:
            require(cpu.run(symbols['ch_v13_ready_glue'], {'r5': 1, 'r6': count}) == 4,
                    'Changed USB status result')
    else:
        raise ValueError('Native transfer bound exceeded')
    events = [t[i:i+4] for t in transfers for i in range(0,len(t),4)]
    require(payload(events) == midi, 'Raw MIDI bytes lost, duplicated or reordered')
    return transfers, cpu


def main():
    stock, _, app = native_instructions(False)
    patched, symbols, _ = native_instructions(True)
    fx = bytes.fromhex('f0 00 32 09 49 00 00 00 02 08 00 00 00 11 00 00 00 01 42 03 f7')
    rev = bytes.fromhex('f0 00 32 09 51 00 00 00 02 46 00 00 00 21 00 00 00 0a 00 64 04 f7')
    old, _ = transfer(stock, None, app, fx+rev)
    new, cpu = transfer(patched, symbols, app, fx+rev)
    require(list(map(len,old)) == [60] and list(map(len,new)) == [28,32],
            'Native decoder changed expected B packet layout')
    require(cpu.delays == [3,3], 'Native B start delays differ')
    checks, steps = 1, cpu.steps
    rng = random.Random(1313)
    for index in range(100):
        frames = rng.randrange(1,6)
        midi = b''
        for frame in range(frames):
            if index % 3 == 0:
                midi += bytes((0xb0,frame,127))
            size = rng.randrange(2,129)
            midi += b'\xf0' + bytes(rng.randrange(128) for _ in range(size-2)) + b'\xf7'
        start = CAPACITY-rng.randrange(1,30)
        transfers, cpu = transfer(patched, symbols, app, midi, start=start)
        require(len(cpu.delays) == frames, 'Wrong native SysEx-start delay count')
        for t in transfers:
            ends = [i for i in range(0,len(t),4) if 5 <= t[i]&15 <= 7 and t[i+(t[i]&15)-4] == 247]
            require(not ends or ends == [len(t)-4], 'Native transfer continues after F7')
        original, _ = transfer(stock, None, app, midi, host=False, start=start)
        actual, _ = transfer(patched, None, app, midi, host=False, start=start)
        require(original == actual, 'Native USB device layout changed')
        checks += 1
        steps += cpu.steps
    report = dict(status='native_decoder_instruction_traces_passed', cases=checks,
                  instruction_steps=steps, original_B_bytes=list(map(len,old)),
                  patched_B_bytes=list(map(len,new)), device_io=False,
                  decoder_sha256=sha(app[SOURCE-ENTRY_POINT:SOURCE-ENTRY_POINT+SIZE]),
                  limits='USB, timing, task scheduling and memcpy are still stubs; hardware failure remains unexplained')
    (HERE/'artifacts/sysex-pacing/native-decoder-tests.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__ == '__main__':
    main()
