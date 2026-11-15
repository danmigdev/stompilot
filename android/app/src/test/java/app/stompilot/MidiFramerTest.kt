package app.stompilot

import org.junit.Assert.assertEquals
import org.junit.Test

class MidiFramerTest {
    private fun frame(vararg pieces: String): List<String> {
        val messages = mutableListOf<String>()
        val framer = MidiFramer { messages.add(MidiHub.hex(it)) }
        for (piece in pieces) framer.feed(MidiHub.bytes(piece))
        return messages
    }

    @Test
    fun sysexSplitAcrossCallbacksIsDeliveredWhole() {
        // A TANK "MOD ON" frame, cut where USB packets might cut it.
        assertEquals(
            listOf("F0 00 32 09 49 00 00 00 02 0A 00 00 00 11 00 00 00 01 3E 03 F7"),
            frame("F0 00 32", "09 49 00 00 00 02 0A 00", "00 00 11 00 00 00 01 3E 03 F7"))
    }

    @Test
    fun consecutiveFramesAndRowsStaySeparate() {
        assertEquals(
            listOf("F0 01 F7", "F0 02 F7", "C0 04", "B0 02 00"),
            frame("F0 01 F7 F0 02", " F7 C0 04 B0 02 00"))
    }

    @Test
    fun runningStatusRepeatsTheLastChannelStatus() {
        // How a bank stored without F0 arrives: its bytes read as Control Changes.
        assertEquals(listOf("B0 00 32", "B0 09 49", "B0 00 00"), frame("B0 00 32 09 49 00 00"))
    }

    @Test
    fun realTimeBytesInsideSysexAreDeliveredOnTheirOwn() {
        assertEquals(listOf("F8", "F0 11 22 F7"), frame("F0 11 F8 22 F7"))
    }

    @Test
    fun dataWithoutStatusAndUnterminatedSysexAreDropped() {
        assertEquals(listOf("90 3C 40"), frame("12 34", "F0 01 02", "90 3C 40"))
    }
}
