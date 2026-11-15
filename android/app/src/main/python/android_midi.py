"""A mido-shaped MIDI backend over the app's MidiHub (android.media.midi).

UsbChocolate and UsbTank use only this part of mido: port names, opening a
port, iter_pending(), send(), close() and Message.from_bytes()/bytes()/type.
Messages cross to Kotlin as hex text, which avoids signed Java bytes.
"""


class Message:
    def __init__(self, data):
        self._data = bytes(data)
        self.type = "sysex" if self._data[:1] == b"\xf0" else "other"

    @classmethod
    def from_bytes(cls, data):
        return cls(bytes(data))

    def bytes(self):
        return list(self._data)


class _Input:
    def __init__(self, hub, name):
        self.name = name
        self._subscription = hub.subscribe(name)

    def iter_pending(self):
        while True:
            message = self._subscription.poll()
            if message is None:
                return
            yield Message(bytes.fromhex(str(message)))

    def close(self):
        self._subscription.close()


class _Output:
    def __init__(self, hub, name):
        self.name = name
        self._hub = hub
        hub.connection(name)  # Fails now, rather than at the first send, if the device is gone.

    def send(self, message):
        self._hub.send(self.name, bytes(message.bytes()).hex())

    def close(self):
        pass  # The hub keeps the device open for the other users of it.


class Backend:
    Message = Message

    def __init__(self, hub):
        self._hub = hub

    def get_input_names(self):
        return [str(name) for name in self._hub.inputNames()]

    def get_output_names(self):
        return [str(name) for name in self._hub.outputNames()]

    def open_input(self, name):
        return _Input(self._hub, name)

    def open_output(self, name):
        return _Output(self._hub, name)
