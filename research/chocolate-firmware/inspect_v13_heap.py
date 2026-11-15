"""Read two fixed V13 allocator snapshots. No allocation, writes or reset.

The stock native failure path returns NULL after walking its free list. The
generic SDK's source-level hook must not be assumed to survive stock LTO.
Aggregate free bytes do not prove a contiguous block or future U/H headroom.
"""
from datetime import datetime, timezone
import json
import struct

from capture_persistence import APP_BASE, HERE, RAM_BASE, audit
from inspect_firmware import RELEASES, require, sha
from midi_configurator.chocolate_transport import UsbChocolate, list_usb_devices


def main():
    reference = (HERE / "artifacts/inspection-013/flash.bin-files/app.bin").read_bytes()
    require(sha(reference) == RELEASES["013"]["app_sha256"], "Unexpected stock app")
    # Actual stock failure path: unlock IRQ, walk list, r4=0, return r0=r4.
    expected_failure = bytes.fromhex(
        "90eab49fd1ec600701e100620481116180e8fd11442061ff03404e0040165b04")
    start = 0x1E0860A - APP_BASE
    require(reference[start:start + len(expected_failure)] == expected_failure,
            "Native allocator failure path changed")
    ports = list_usb_devices()
    require(len(ports) == 1, "Expected one stock Chocolate V13 in U")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    dest = HERE / "artifacts/heap-captures" / stamp
    with UsbChocolate(ports[0].input_name, ports[0].output_name) as device:
        identity = audit(device)

        def read(address, size):
            require((address, size) in ((0x1E084FE, 0x1D6), (0x19510, 12), (0x195A0, 8)),
                    "Outside fixed allocator inspection extents")
            prefix = b"\x04" + (address - RAM_BASE).to_bytes(4, "little") + size.to_bytes(3, "little")
            data = device._request(0x23, prefix, 0x23, prefix)["payload"][8:]
            require(len(data) == size, "Incomplete heap inspection read")
            return data

        native = read(0x1E084FE, 0x1D6)
        offset = 0x1E084FE - APP_BASE
        require(native == reference[offset:offset + len(native)], "Installed allocator differs")
        observations = []
        for _ in range(2):
            counters = read(0x19510, 12)
            head = read(0x195A0, 8)
            end, minimum, available = struct.unpack("<3I", counters)
            size, first = struct.unpack("<2I", head)
            observations.append(dict(counters_hex=counters.hex(), head_hex=head.hex(),
                end_pointer=hex(end), minimum_free_bytes=minimum, current_free_bytes=available,
                head_size=size, first_free_pointer=hex(first),
                candidate_workspace_bytes=4352, allocator_request_with_overhead=4360))
    result = dict(timestamp_utc=stamp, identity=identity["identity"], writes=0, allocations=0,
                  allocator_sha256=sha(native), native_failure_path_hex=expected_failure.hex(),
                  snapshots=observations, stable=observations[0] == observations[1],
                  limit="Aggregate counters only: contiguous capacity, MMU allocation and future U/H headroom unverified")
    dest.mkdir(parents=True)
    (dest / "heap.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (dest / "allocator.bin").write_bytes(native)
    print(json.dumps(result, indent=2))
    print(dest)


if __name__ == "__main__":
    main()
