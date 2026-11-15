package app.stompilot

import java.io.ByteArrayOutputStream

/**
 * Turns a MIDI byte stream into complete messages.
 *
 * Android delivers USB MIDI in arbitrary pieces, so a SysEx message can span
 * several callbacks. Channel messages may use running status; real-time bytes
 * may appear anywhere, even inside SysEx, and are delivered on their own.
 */
class MidiFramer(private val deliver: (ByteArray) -> Unit) {
    private val sysex = ByteArrayOutputStream()
    private var inSysex = false
    private var status = 0
    private val data = IntArray(2)
    private var count = 0
    private var needed = 0

    fun feed(bytes: ByteArray, offset: Int = 0, length: Int = bytes.size) {
        for (index in offset until offset + length) {
            val byte = bytes[index].toInt() and 0xFF
            when {
                byte >= 0xF8 -> deliver(byteArrayOf(byte.toByte()))
                byte == 0xF0 -> {
                    inSysex = true
                    sysex.reset()
                    sysex.write(byte)
                    status = 0
                }
                byte == 0xF7 -> {
                    if (inSysex) {
                        sysex.write(byte)
                        deliver(sysex.toByteArray())
                        inSysex = false
                    }
                }
                byte >= 0x80 -> {
                    inSysex = false
                    count = 0
                    needed = dataLength(byte)
                    // System common messages (F1-F6) do not set running status.
                    status = byte
                    if (needed == 0) {
                        deliver(byteArrayOf(byte.toByte()))
                        status = 0
                    }
                }
                inSysex -> sysex.write(byte)
                status != 0 -> {
                    data[count++] = byte
                    if (count == needed) {
                        val message = ByteArray(needed + 1)
                        message[0] = status.toByte()
                        for (i in 0 until needed) message[i + 1] = data[i].toByte()
                        deliver(message)
                        count = 0
                        if (status >= 0xF0) status = 0
                    }
                }
            }
        }
    }

    private fun dataLength(status: Int): Int = when {
        status in 0xC0..0xDF -> 1
        status < 0xF0 -> 2
        status == 0xF1 || status == 0xF3 -> 1
        status == 0xF2 -> 2
        else -> 0
    }
}
