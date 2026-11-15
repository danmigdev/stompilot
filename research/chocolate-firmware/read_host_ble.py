"""Read-only V13 HOST inspection through the proprietary BLE serial service.

Default: discover FootCtrl devices only. --snapshot reads fixed diagnostic RAM
ranges and validates the installed pacing hooks/module; --watch SECONDS also
records changes to endpoint-1 RAM while the owner presses switches. This is
not a USB wire capture: samples may miss transfers and BLE can affect timing.
No configuration-write, erase, update, reset or arbitrary-command operation is
implemented. BLE ATT writes carry only identity and bounded read requests.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import struct
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))
from midi_configurator.chocolate_persistence import PACING_BASE, PACING_SIZE, PACING_SHA256, RAM_BASE
from inspect_firmware import require, sha

# Channel UUIDs are corroborated by ../chocolate_protocol.md. Request framing
# is the existing native USB protocol before its MIDI 7-bit encoding.
SERIAL = '0000ae40-0000-1000-8000-00805f9b34fb'
WRITE = '0000ae41-0000-1000-8000-00805f9b34fb'
NOTIFY = '0000ae42-0000-1000-8000-00805f9b34fb'
SPANS = ((0x13280,128), (0x193A0,640), (0x19100,4), (RAM_BASE+510,417),
         (0x1E0FF54,4), (0x1E119CC,4), (PACING_BASE,PACING_SIZE))


def read_prefix(address, size):
    require(0 < size <= 128 and any(a <= address < address+size <= a+n for a,n in SPANS),
            'Outside fixed V13 read-only diagnostic ranges')
    return b'\x04'+((address-RAM_BASE)&0xffffffff).to_bytes(4,'little')+size.to_bytes(3,'little')


def request_frame(command, payload=b''):
    if command == 0x11:
        require(payload == b'', 'Identity request must be empty')
    else:
        require(command == 0x23 and len(payload) == 8 and payload[0] == 4,
                'Only identity and memory-type-4 reads are permitted')
        address = (int.from_bytes(payload[1:5],'little')+RAM_BASE)&0xffffffff
        require(read_prefix(address,int.from_bytes(payload[5:8],'little')) == payload,
                'Invalid fixed read prefix')
    return b'\x00\x59'+bytes([command])+len(payload).to_bytes(3,'little')+payload+bytes([~sum(payload)&255])


class Frames:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        result = []
        while len(self.buffer) >= 2:
            head = self.buffer.find(b'\x00\x59')
            if head < 0:
                self.buffer[:] = self.buffer[-1:]
                break
            del self.buffer[:head]
            if len(self.buffer) < 6:
                break
            size = int.from_bytes(self.buffer[3:6],'little')
            if size > 256:
                del self.buffer[:2]
                continue
            if len(self.buffer) < size+7:
                break
            frame = bytes(self.buffer[:size+7])
            del self.buffer[:size+7]
            body = frame[6:-1]
            if frame[-1] == (~sum(body)&255):
                result.append((frame[2],body))
        return result


class ReadOnlyLink:
    def __init__(self, client):
        self.client = client
        self.frames = Frames()
        self.queue = asyncio.Queue()
        self.lock = asyncio.Lock()

    def notified(self, _, data):
        for frame in self.frames.feed(data):
            self.queue.put_nowait(frame)

    async def request(self, command, prefix=b''):
        packet = request_frame(command,prefix)
        async with self.lock:
            while not self.queue.empty():
                self.queue.get_nowait()
            await self.client.write_gatt_char(WRITE,packet,response=False)
            deadline = time.monotonic()+6
            while True:
                remaining=deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('No matching BLE reply within six seconds')
                opcode, data = await asyncio.wait_for(self.queue.get(),remaining)
                if opcode == command and data.startswith(prefix):
                    return data[len(prefix):]

    async def read(self, address, size):
        require(any(a <= address < address+size <= a+n for a,n in SPANS), 'Read outside fixed spans')
        result = bytearray()
        while len(result) < size:
            count = min(128,size-len(result))
            chunk = await self.request(0x23,read_prefix(address+len(result),count))
            require(len(chunk)==count,'Incomplete BLE memory response')
            result.extend(chunk)
        return bytes(result)


async def main(args):
    from bleak import BleakClient, BleakScanner
    devices = await BleakScanner.discover(timeout=8,return_adv=True)
    matches = [(dev,adv) for dev,adv in devices.values()
               if (adv.local_name or dev.name or '').lower().startswith('footctrl')]
    print(json.dumps({'FootCtrl_devices':len(matches),'names':[a.local_name or d.name for d,a in matches]}),flush=True)
    if not args.snapshot and not args.watch:
        return
    require(len(matches)==1,'Expected exactly one advertised FootCtrl device')
    folder = HERE/'artifacts/host-ble'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    folder.mkdir(parents=True)
    report = dict(status='connecting',device_configuration_writes=0,firmware_writes=0,
                  commands=['0x11 identity','0x23 bounded RAM/code read'],
                  limit='Endpoint RAM observations only; BLE traffic may affect timing')
    try:
        async with BleakClient(matches[0][0],timeout=20) as client:
            require(client.services.get_service(SERIAL) is not None,'No proprietary serial service')
            link = ReadOnlyLink(client)
            await client.start_notify(NOTIFY,link.notified)
            name = (await link.request(0x11))[:24].split(b'\0')[0].decode('ascii')
            require(name=='FootCtrlPlus_013','Unsupported Chocolate firmware identity')
            report['identity']=name
            for addr,expected in ((0x1E0FF54,bytes.fromhex('c0ea88b3')),
                                  (0x1E119CC,bytes.fromhex('80ea57a6'))):
                require(await link.read(addr,4)==expected,'Installed pacing hook differs')
            require(sha(await link.read(PACING_BASE,PACING_SIZE))==PACING_SHA256,
                    'Installed pacing module differs')
            report['pacing_module_verified']=True
            for name,addr,size in (('endpoint1',0x13280,128),('route',0x193A0,640),
                                   ('save-pointer',0x19100,4),('switch-b',RAM_BASE+510,417)):
                data = await link.read(addr,size)
                (folder/(name+'.bin')).write_bytes(data)
                if name=='route':
                    report.update(output_callback=hex(struct.unpack_from('<I',data,256)[0]),host_port=data[228])
            report['status']='snapshot_verified'
            print(json.dumps(report),flush=True)
            if args.watch:
                report['status']='watching'
                print('WATCH_READY',flush=True)
                started, previous, samples, changes = time.monotonic(),None,0,0
                with (folder/'endpoint-changes.jsonl').open('x',encoding='utf-8') as log:
                    while time.monotonic()-started < args.watch:
                        before=time.monotonic()
                        packet=await link.read(0x13280,64)
                        samples+=1
                        if packet != previous:
                            changes+=1
                            entry=dict(sample=samples,seconds=before-started,
                                       read_seconds=time.monotonic()-before,data=packet.hex(' '))
                            log.write(json.dumps(entry)+'\n'); log.flush()
                            print(json.dumps(entry),flush=True)
                            previous=packet
                        await asyncio.sleep(.05)
                report.update(status='watch_completed',samples=samples,buffer_changes=changes)
            await client.stop_notify(NOTIFY)
    except BaseException as error:
        report.update(status='stopped',error=f'{type(error).__name__}: {error}')
        raise
    finally:
        (folder/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(report),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',action='store_true')
    parser.add_argument('--watch',type=int,default=0)
    args=parser.parse_args()
    require(0 <= args.watch <= 180,'Watch must be between 0 and 180 seconds')
    asyncio.run(main(args))
