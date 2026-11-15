"""Offline bounded interpreter for the two original erase-wrapper functions.

Executes only decoded instructions at 0x1E0CCF6 and 0x1E0CBEC, checking every
instruction's bytes against pinned V13. The lower erase and delay functions are
recorded stubs; this does not emulate flash hardware, interrupts or the CPU.
"""
import json
import re

from probe_sector_init import HERE, APP_BASE, ERASE_ONE, ERASE_UNIT, SECTOR, reference, require


def trace(flag):
    require(flag in (0, 1, 16), "Only reviewed erase-unit states")
    app = reference()
    decoded = {}
    for line in (HERE / "artifacts/inspection-013/app-disassembly.txt").read_text().splitlines():
        match = re.match(r"\s*([0-9a-f]+):\s+((?:[0-9a-f]{2}\s+)+)(.*)", line)
        if not match:
            continue
        address = int(match[1], 16)
        if not 0x1E0CBEC <= address < 0x1E0CD20:
            continue
        code, op = bytes.fromhex(match[2]), match[3].strip()
        require(app[address - APP_BASE:address - APP_BASE + len(code)] == code, "Disassembly bytes differ")
        decoded[address] = (len(code), op)
    registers = {f"r{i}": 0xA000 + i for i in range(16)}
    registers["r0"] = SECTOR
    before = registers.copy()
    pc, return_to, stack, events, path = ERASE_ONE, None, [], [], []

    def value(expr):
        expr = expr.strip()
        if expr in registers:
            return registers[expr]
        try:
            return int(expr, 0)
        except ValueError:
            pass
        extra = re.fullmatch(r"uextra\((r\d+), p:(\d+), l:(\d+)\)", expr)
        if extra:
            return value(extra[1]) >> int(extra[2]) & ((1 << int(extra[3])) - 1)
        memory = re.fullmatch(r"b\[(r\d+)\+(\d+)\] \(u\)", expr)
        if memory:
            require(value(memory[1]) + int(memory[2]) == ERASE_UNIT, "Unexpected data read")
            return flag
        binary = re.fullmatch(r"(r\d+|0x[0-9A-Fa-f]+|\d+) (<<|>>|\+|-|&) (r\d+|-?0x[0-9A-Fa-f]+|-?\d+)", expr)
        if binary:
            a, operator, b = value(binary[1]), binary[2], value(binary[3])
            if operator == "+": return (a + b) & 0xFFFFFFFF
            if operator == "-": return (a - b) & 0xFFFFFFFF
            if operator == "<<": return a << b & 0xFFFFFFFF
            if operator == ">>": return a >> b
            if operator == "&": return a & b
        raise ValueError(f"Unsupported audited expression: {expr}")

    def reg_list(text):
        if "-" in text:
            high, low = (int(x.strip()[1:]) for x in text.split("-"))
            return [f"r{i}" for i in range(low, high + 1)]
        return [text.strip()]

    while pc is not None:
        require(len(path) < 200 and pc in decoded, f"Unexpected program counter {pc}")
        path.append(hex(pc))
        size, op = decoded[pc]
        next_pc = pc + size
        push = re.fullmatch(r"\[--sp\] = \{rets, (.*)\}", op)
        pop = re.fullmatch(r"\{pc, (.*)\} = \[sp\+\+\]", op)
        if push:
            stack.append((return_to, {r: registers[r] for r in reg_list(push[1])}))
        elif pop:
            next_pc, saved = stack.pop()
            require(set(saved) == set(reg_list(pop[1])), "Unbalanced saved registers")
            registers.update(saved)
        elif op.startswith("call"):
            target = re.search(r": ([0-9a-f]+) >", op)
            require(target is not None, "Unresolved call")
            target = int(target[1], 16)
            if target == 0x1E0CBEC:
                return_to, next_pc = next_pc, target
            elif target in (0x1E0CBC4, 0x1E0CBC6):
                if target == 0x1E0CBC6:
                    events.append(dict(operation="erase", kind=registers["r0"], address=registers["r1"]))
                else:
                    events.append(dict(operation="delay"))
                for r in range(4):
                    registers[f"r{r}"] = 0
            else:
                raise ValueError(f"Unexpected lower-level call {target:#x}")
        elif op.startswith("goto"):
            next_pc = int(re.search(r": ([0-9a-f]+) >", op)[1], 16)
        elif op.startswith("if"):
            condition = re.match(r"if(s?) \((.*)\) (goto .*|\{)", op)
            require(condition is not None, f"Unsupported conditional {op}")
            comparison = re.fullmatch(r"(.*?) (==|!=|>=|<=|>|<) (.*)", condition[2])
            require(comparison is not None, "Unsupported comparison")
            left = comparison[1].strip()
            if left.startswith("(") and left.endswith(")"):
                left = left[1:-1]
            a, b = value(left), value(comparison[3])
            if condition[1]:
                a, b = (x if x < 0x80000000 else x - 0x100000000 for x in (a, b))
            valid = {"==": a == b, "!=": a != b, ">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[comparison[2]]
            if condition[3] == "{":
                if not valid:
                    next_pc += decoded[next_pc][0]
            elif valid:
                next_pc = int(re.search(r": ([0-9a-f]+) >", condition[3])[1], 16)
        else:
            assignment = re.fullmatch(r"(r\d+) (=|\+=) (.*)", op)
            require(assignment is not None, f"Unsupported audited instruction: {op}")
            registers[assignment[1]] = (value(assignment[3]) +
                                       (registers[assignment[1]] if assignment[2] == "+=" else 0)) & 0xFFFFFFFF
        pc = next_pc
    require(not stack and all(registers[f"r{i}"] == before[f"r{i}"] for i in range(4, 16)),
            "Callee-saved registers changed")
    return dict(flag=flag, result=registers["r0"], events=events, path=path,
                nonvolatile_registers_preserved=True, lower_erase_and_delay_are_stubs=True)


if __name__ == "__main__":
    results = [trace(flag) for flag in (0, 16)]
    require(results[0]["events"] == [] and results[0]["result"] == 0, "No-op is not empty")
    erases = [item for item in results[1]["events"] if item["operation"] == "erase"]
    require(erases == [dict(operation="erase", kind=2, address=SECTOR)] and results[1]["result"] == 4096,
            "Active path does not erase exactly the fixed sector")
    dest = HERE / "artifacts/sector-init-audit.json"
    dest.write_text(json.dumps(dict(device_io=False, results=results), indent=2) + "\n")
    print(json.dumps(dict(noop_events=results[0]["events"], active_erases=erases,
                          active_result=results[1]["result"], trace_file=str(dest)), indent=2))
