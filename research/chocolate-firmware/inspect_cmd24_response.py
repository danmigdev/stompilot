"""Offline audit of the V13 command-24 response checksum length.

Checks exact original instructions and models this short response basic block.
This is not a CPU emulator, runtime exception trace or device-access tool.
"""
from __future__ import annotations

import json

from capture_persistence import HERE, APP_BASE, RELEASES, require, sha


def inspect():
    app = (HERE / "artifacts/inspection-013/flash.bin-files/app.bin").read_bytes()
    require(sha(app) == RELEASES["013"]["app_sha256"], "Wrong original V13 application")
    # Decoded with the already installed official PI32v2 r3 objdump. Paired
    # instructions marked # read the pre-bundle values of their source registers.
    response = bytes.fromhex("412b61d6c941c84d80a8a0d6c84ebfeafbfe")
    checksum = bytes.fromhex("740482140485d8ee004342182217c32181edf931783f90e103025404")
    argument_decode = bytes.fromhex("484a4e49a6f12004494ba6f12018494ca6f1201c")
    for address, expected in ((0x1E13DF0, response), (0x1E13BF8, checksum),
                              (0x1E13DC4, argument_decode)):
        require(app[address - APP_BASE:address - APP_BASE + len(expected)] == expected,
                f"Reviewed instruction sequence differs at {address:#x}")

    # r6 was the incoming payload length before this command branch, but the
    # branch overwrites it with the second little-endian uint32 argument.
    # The erase callback preserves r6 in its r8-r4 callee-saved register set.
    second_argument = 0x3C000
    r6 = second_argument
    r1 = 11                                # 0x1E13DF0: response payload length
    header_length, r1 = r1, r6              # 0x1E13DF2/4: parallel move/store
    checksum_length = r1                   # call at 0x1E13DFE, no intervening r1 write
    require(header_length == 11 and checksum_length == 245760, "Response model differs")
    return dict(application_sha256=sha(app), device_io=False, model="audited basic block, not a full CPU emulator",
                callback_address="0x1D114", second_argument=second_argument,
                declared_response_payload_bytes=header_length,
                actual_checksum_read_bytes=checksum_length,
                reads_beyond_declared_payload=checksum_length - header_length,
                vulnerable_move_address="0x1E13DF2", checksum_call_address="0x1E13DFE",
                checksum_function="0x1E13BF8",
                observation="The second callback argument becomes the response checksum length.",
                implication="The diagnostic's absolute sector address causes an out-of-payload read after erase.",
                limitation="No runtime exception trace proves this was the exact USB disconnection cause.",
                device_execution="suspended; do not repeat this command-24 diagnostic")


if __name__ == "__main__":
    result = inspect()
    dest = HERE / "artifacts/sector-erase-probes/20261027T223637.066882Z/cmd24-response-audit.json"
    dest.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
