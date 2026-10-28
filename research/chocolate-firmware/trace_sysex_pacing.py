"""Bounded offline trace of the stock USB builder plus linked pacing code.

Every disassembled instruction is byte-checked. The ROM MIDI decoder, memcpy,
USB buffer lookup, status and scheduler delay are stubs. This exercises real
control flow and register preservation, not the chip, USB timing or bootloader.
"""
from __future__ import annotations

import ast
from collections import deque
import json
import random
import re

from inspect_firmware import ENTRY_POINT, HERE, RELEASES, require, sha
from package_sysex_pacing import HOOKS, PATCH_ADDRESS, read_build

STATE, OUTPUT, BUILDER = 0x193A0, 0x900000, 0x1E0FE4C
HOST = 0x1E119AE
SIZES = {4: 3, 5: 1, 6: 2, 7: 3, 8: 3, 9: 3, 10: 3, 11: 3,
         12: 2, 13: 2, 14: 3, 15: 1}


def instructions(patched):
    app = (HERE / "artifacts/inspection-013/flash.bin-files/app.bin").read_bytes()
    require(sha(app) == RELEASES["013"]["app_sha256"], "Stock application mismatch")
    blob, hooks, build = read_build(HERE / "artifacts/sysex-pacing")
    result = {}

    def read(path, spans):
        for line in path.read_text().splitlines():
            match = re.match(r"\s*([0-9a-f]+):\s+((?:[0-9a-f]{2}\s+)+)(.*)", line)
            if not match:
                continue
            address, code = int(match[1], 16), bytes.fromhex(match[2])
            for base, data in spans:
                if base <= address < base + len(data):
                    require(data[address-base:address-base+len(code)] == code,
                            f"Disassembly bytes differ at {address:#x}")
                    op = match[3].strip()
                    if op.endswith(">"):
                        op = op.rsplit(" <", 1)[0]
                    result[address] = (len(code), op.split(" #")[0].strip())
                    break

    read(HERE / "artifacts/inspection-013/app-disassembly.txt",
         [(BUILDER, app[BUILDER-ENTRY_POINT:0x1E10050-ENTRY_POINT])])
    if patched:
        spans = [(PATCH_ADDRESS, blob)] + [(HOOKS[n][0], data) for n, data in hooks.items()]
        read(HERE / "artifacts/sysex-pacing/disassembly.txt", spans)
        result.pop(0x1E0FF56, None)  # second overwritten 16-bit instruction
    return result, {n: int(v, 16) for n, v in build["symbols"].items()}


class Machine:
    def __init__(self, code, events=(), host=True):
        self.code = code
        self.memory = {}
        self.events = deque(events)
        self.delays = []
        self.status = 4
        self.steps = 0
        self.write(STATE + 256, HOST if host else 0x1E10178, 4)
        self.write(STATE + 632, 1, 1)  # one selected input stream
        self.write(STATE + 633, 255, 1)  # no incomplete stream lock

    def read(self, address, size):
        require(STATE <= address <= STATE + 0x2000 - size or
                OUTPUT <= address <= OUTPUT + 64 - size, f"Unexpected memory read {address:#x}/{size}")
        return sum(self.memory.get(address+i, 0) << (i*8) for i in range(size))

    def write(self, address, value, size):
        self.read(address, size)
        for i in range(size):
            self.memory[address+i] = value >> (8*i) & 255

    def memory_operand(self, expr):
        m = re.fullmatch(r"(b|h)?\[(.*?)\](?: \(u\))?", expr)
        require(m is not None, f"Unsupported memory operand: {expr}")
        size = {None: 4, "b": 1, "h": 2}[m[1]]
        target = m[2]
        pre = re.fullmatch(r"\+\+(r\d+)=(-?\d+)", target)
        post = re.fullmatch(r"(r\d+)\+\+=(-?\d+)", target)
        if pre:
            self.reg[pre[1]] = (self.reg[pre[1]] + int(pre[2])) & 0xffffffff
            address = self.reg[pre[1]]
        elif post:
            address = self.reg[post[1]]
            self.reg[post[1]] = (address + int(post[2])) & 0xffffffff
        else:
            address = self.value(target)
        return address, size

    def value(self, expr):
        expr = expr.strip()
        extra = re.fullmatch(r"(u|s)extra\((r\d+), p:(\d+), l:(\d+)\)", expr)
        if extra:
            bits = int(extra[4])
            v = self.reg[extra[2]] >> int(extra[3]) & ((1 << bits) - 1)
            return (v - (1 << bits) if extra[1] == "s" and v & (1 << (bits-1)) else v) & 0xffffffff
        cast = re.fullmatch(r"(r\d+)\.b0 \(u\)", expr)
        if cast:
            return self.reg[cast[1]] & 255
        if "[" in expr:
            return self.read(*self.memory_operand(expr))

        def evaluate(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, int):
                return node.value
            if isinstance(node, ast.Name) and node.id in self.reg:
                return self.reg[node.id]
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
                return -evaluate(node.operand)
            if isinstance(node, ast.BinOp):
                a, b = evaluate(node.left), evaluate(node.right)
                if isinstance(node.op, ast.Add): return a + b
                if isinstance(node.op, ast.Sub): return a - b
                if isinstance(node.op, ast.Mult): return a * b
                if isinstance(node.op, ast.BitAnd): return a & b
                if isinstance(node.op, ast.BitOr): return a | b
                if isinstance(node.op, ast.LShift): return a << b
                if isinstance(node.op, ast.RShift): return a >> b
            raise ValueError(f"Unsupported expression: {expr}")
        return evaluate(ast.parse(expr, mode="eval").body) & 0xffffffff

    def stub(self, target):
        r = self.reg
        if target == 0x1BD60:  # ROM MIDI decoder: queued legal USB event fixtures
            require(r["r1"] == STATE + 2264, "Unexpected decoder stream")
            if self.events:
                event = self.events.popleft()
                for i, v in enumerate(event[1:]):
                    self.write(r["r0"] + i, v, 1)
                result = event[0] & 15
            else:
                result = 255
        elif target == 0x117F74:
            data = [self.read(r["r1"] + i, 1) for i in range(r["r2"])]
            for i, v in enumerate(data):
                self.write(r["r0"] + i, v, 1)
            result = r["r0"]
        elif target == 0x1E11932:
            require(r["r0"] == 1, "Unexpected USB port")
            result = OUTPUT
        elif target == 0x117D78:
            require(r["r0"] == 3, "Unexpected delay")
            self.delays.append(3)
            result = 0
        elif target == 0x1E10A24:
            result = self.status
        else:
            return False
        for i in range(4):
            r[f"r{i}"] = 0xDEAD0000 + i
        r["r0"] = result
        return True

    @staticmethod
    def registers(spec):
        result = []
        for part in spec.split(", "):
            if "-" in part:
                hi, lo = (int(x[1:]) for x in part.split("-"))
                result.extend(f"r{i}" for i in range(lo, hi+1))
            elif part not in ("rets", "pc"):
                result.append(part)
        return result

    def run(self, start, overrides=None):
        self.reg = {f"r{i}": 0xA000 + i for i in range(16)}
        self.reg.update(overrides or {})
        before = self.reg.copy()
        rets, pc, stack, loop = None, start, [], None
        for step in range(10000):
            if pc is None:
                break
            require(pc in self.code, f"Unexpected PC {pc:#x}")
            size, op = self.code[pc]
            nxt = pc + size
            push = re.fullmatch(r"\[--sp\] = (rets|\{.*\})", op)
            pop = re.fullmatch(r"(pc|\{.*\}) = \[sp\+\+\]", op)
            branch = re.fullmatch(r"(call|goto) (-?\d+)", op)
            condition = re.fullmatch(r"if(s?) \((.*?)\) (goto -?\d+|\{)", op)
            repeat = re.fullmatch(r"rep (\d+) (\d+) \{", op)
            if push:
                names = self.registers(push[1].strip("{}"))
                stack.append((rets, {n: self.reg[n] for n in names}))
            elif pop:
                saved_ret, saved = stack.pop()
                names = self.registers(pop[1].strip("{}"))
                require(set(names) == set(saved), "Unbalanced register frame")
                self.reg.update(saved)
                if "pc" in pop[1]: nxt = saved_ret
                else: rets = saved_ret
            elif op == "rts":
                nxt = rets
            elif branch:
                target = (nxt + int(branch[2])) & 0x1ffffff
                if self.stub(target):
                    if branch[1] == "goto": nxt = rets
                else:
                    if branch[1] == "call": rets = nxt
                    nxt = target
            elif condition:
                compare = re.fullmatch(r"(.*?) (==|!=|>=|<=|>|<) (.*)", condition[2])
                require(compare is not None, f"Unsupported condition {op}")
                a, b = self.value(compare[1]), self.value(compare[3])
                if condition[1]:
                    a, b = (v if v < 0x80000000 else v-0x100000000 for v in (a, b))
                valid = {"==":a==b, "!=":a!=b, ">=":a>=b, "<=":a<=b, ">":a>b, "<":a<b}[compare[2]]
                if condition[3] == "{":
                    if not valid: nxt += self.code[nxt][0]
                elif valid:
                    nxt = (nxt + int(condition[3].split()[1])) & 0x1ffffff
            elif repeat:
                require(loop is None, "Nested loop unsupported")
                loop = [nxt, nxt + int(repeat[2]), int(repeat[1])]
            else:
                assign = re.fullmatch(r"(.*?) (=|\+=|&=|\|=) (.*)", op)
                require(assign is not None, f"Unsupported instruction at {pc:#x}: {op}")
                target, operation, expr = assign.groups()
                value = self.value(expr)
                if "[" in target:
                    require(operation == "=", "Unsupported memory modification")
                    address, width = self.memory_operand(target)
                    self.write(address, value, width)
                else:
                    if operation == "+=": value += self.reg[target]
                    elif operation == "&=": value &= self.reg[target]
                    elif operation == "|=": value |= self.reg[target]
                    self.reg[target] = value & 0xffffffff
            if loop and nxt == loop[1]:
                loop[2] -= 1
                if loop[2]: nxt = loop[0]
                else: loop = None
            pc = nxt
        else:
            raise ValueError("Instruction bound exceeded")
        require(not stack and all(self.reg[f"r{i}"] == before[f"r{i}"] for i in range(4,16)),
                "Stack or callee-saved registers not restored")
        self.steps += step
        return self.reg["r0"]


def sysex_events(data):
    require(data[:1] == b"\xf0" and data[-1:] == b"\xf7", "Expected whole SysEx")
    events = []
    for pos in range(0, len(data), 3):
        part = data[pos:pos+3]
        cin = 4 + len(part) if pos + 3 >= len(data) else 4
        events.append(bytes([cin]) + part + bytes(3-len(part)))
    return events


def payload(events):
    return b"".join(e[1:1+SIZES[e[0]&15]] for e in events)


def transmissions(code, symbols, events, host=True):
    cpu = Machine(code, events, host)
    transfers = []
    for _ in range(len(events) + 3):
        count = cpu.run(BUILDER, {"r0": OUTPUT})
        require(count % 4 == 0 and count <= 64, "Invalid USB transfer size")
        if not count:
            require(not cpu.events, "Decoder was not drained")
            break
        data = bytes(cpu.read(OUTPUT+i, 1) for i in range(count))
        transfers.append(data)
        if symbols:
            require(cpu.run(symbols["ch_v13_ready_glue"], {"r5":1, "r6":count}) == 4,
                    "USB status return changed")
    else:
        raise ValueError("Transfer bound exceeded")
    actual = [t[p:p+4] for t in transfers for p in range(0,len(t),4)]
    require(payload(actual) == payload(events), "MIDI bytes lost, duplicated or reordered")
    return transfers, cpu


def main():
    stock, _ = instructions(False)
    patched, symbols = instructions(True)
    fx = bytes.fromhex("f0 00 32 09 49 00 00 00 02 08 00 00 00 11 00 00 00 01 42 03 f7")
    rev = bytes.fromhex("f0 00 32 09 51 00 00 00 02 46 00 00 00 21 00 00 00 0a 00 64 04 f7")
    pair = sysex_events(fx) + sysex_events(rev)
    old, _ = transmissions(stock, None, pair)
    new, cpu = transmissions(patched, symbols, pair)
    require(list(map(len, old)) == [60] and list(map(len, new)) == [28,32], "Captured B layout mismatch")
    require(cpu.delays == [3,3], "Expected one task delay before each SysEx")
    checks, steps = 1, cpu.steps
    rng = random.Random(13)
    for index in range(250):
        frames = rng.randrange(1,7)
        events = []
        for frame in range(frames):
            if index % 3 == 0:
                events.append(bytes([0x0b,0xb0,frame,127]))
            size = rng.randrange(2,128)
            events += sysex_events(b"\xf0" + bytes(rng.randrange(128) for _ in range(size-2)) + b"\xf7")
        transfers, cpu = transmissions(patched, symbols, events)
        require(len(cpu.delays) == frames, "Wrong number of SysEx-start waits")
        for t in transfers:
            ends = [p for p in range(0,len(t),4) if 5 <= t[p]&15 <= 7 and t[p+(t[p]&15)-4] == 247]
            require(not ends or ends == [len(t)-4], "Transfer continues beyond SysEx end")
        unchanged, _ = transmissions(stock, None, events, host=False)
        device, _ = transmissions(patched, None, events, host=False)
        require(unchanged == device, "USB device mode differs from stock")
        checks += 1
        steps += cpu.steps
    report = dict(status="bounded_instruction_trace_passed", cases=checks, instruction_steps=steps,
                  captured_B_original_usb_bytes=list(map(len,old)), captured_B_patched_usb_bytes=list(map(len,new)),
                  gap_ticks=3, device_io=False, native_execution=False,
                  limitations="ROM decoder, memcpy, buffer lookup, status and delay are stubs; no interrupt/USB/CPU emulation")
    blob, hooks, _ = read_build(HERE / "artifacts/sysex-pacing")
    report.update(blob_sha256=sha(blob), hooks={name:data.hex() for name,data in hooks.items()})
    (HERE / "artifacts/sysex-pacing/instruction-tests.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report,indent=2))


if __name__ == "__main__":
    main()
