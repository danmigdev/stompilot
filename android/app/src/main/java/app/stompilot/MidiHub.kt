package app.stompilot

import android.content.Context
import android.media.midi.MidiDevice
import android.media.midi.MidiDeviceInfo
import android.media.midi.MidiInputPort
import android.media.midi.MidiManager
import android.media.midi.MidiOutputPort
import android.media.midi.MidiReceiver
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/** Receives events for the interface, such as forwarded presses. */
fun interface EventSink {
    fun emit(event: String, json: String)
}

/**
 * USB MIDI devices through android.media.midi, shared by the Python service
 * and the forwarding of footswitch presses.
 *
 * Each device is opened once. Its messages are framed and copied to every
 * subscriber, so the editor and the live view can listen at the same time.
 * Port names follow the Windows ones: "<name> (<id>) 0" receives from the
 * device and "<name> (<id>) 1" sends to it, so discovery code is unchanged.
 */
class MidiHub(context: Context) {
    private val manager = context.getSystemService(Context.MIDI_SERVICE) as MidiManager
    private val thread = HandlerThread("stompilot-midi").apply { start() }
    private val handler = Handler(thread.looper)
    private val open = ConcurrentHashMap<Int, Connection>()

    @Volatile
    var listener: EventSink? = null

    /** Forward every message from [relaySource] to [relayTarget] (device ids), unless paused. */
    @Volatile
    var relaySource: Int = -1
    @Volatile
    var relayTarget: Int = -1

    inner class Subscription(private val connection: Connection) {
        val queue = LinkedBlockingQueue<ByteArray>()

        /** The next complete message as hex, or null when none is waiting. */
        fun poll(): String? = queue.poll()?.let(::hex)

        fun close() {
            connection.subscribers.remove(this)
        }
    }

    inner class Connection(val info: MidiDeviceInfo, val device: MidiDevice) {
        val subscribers = CopyOnWriteArrayList<Subscription>()
        private val output: MidiOutputPort? = device.openOutputPort(0)
        private val input: MidiInputPort? = device.openInputPort(0)
        private val framer = MidiFramer { message -> received(this, message) }

        init {
            output?.connect(object : MidiReceiver() {
                override fun onSend(data: ByteArray, offset: Int, count: Int, timestamp: Long) {
                    synchronized(framer) { framer.feed(data, offset, count) }
                }
            })
        }

        fun send(message: ByteArray) {
            val port = input ?: throw IllegalStateException("${name(info)} accepts no MIDI input.")
            synchronized(port) {
                val size = port.maxMessageSize.coerceAtLeast(3)
                var start = 0
                while (start < message.size) {
                    val length = minOf(size, message.size - start)
                    port.send(message, start, length)
                    start += length
                }
            }
        }

        fun close() {
            try { output?.close() } catch (_: Exception) {}
            try { input?.close() } catch (_: Exception) {}
            try { device.close() } catch (_: Exception) {}
        }
    }

    init {
        val callback = object : MidiManager.DeviceCallback() {
            override fun onDeviceAdded(device: MidiDeviceInfo) = changed()
            override fun onDeviceRemoved(device: MidiDeviceInfo) {
                open.remove(device.id)?.close()
                changed()
            }
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            manager.registerDeviceCallback(MidiManager.TRANSPORT_MIDI_BYTE_STREAM, { handler.post(it) }, callback)
        } else {
            @Suppress("DEPRECATION")
            manager.registerDeviceCallback(callback, handler)
        }
    }

    private fun changed() {
        listener?.emit("stompilot-midi-devices", devicesJson())
    }

    @Suppress("DEPRECATION")
    private fun infos(): List<MidiDeviceInfo> =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU)
            manager.getDevicesForTransport(MidiManager.TRANSPORT_MIDI_BYTE_STREAM).toList()
        else manager.devices.toList()

    private fun name(info: MidiDeviceInfo): String {
        val properties = info.properties
        return (properties.getString(MidiDeviceInfo.PROPERTY_PRODUCT)
            ?: properties.getString(MidiDeviceInfo.PROPERTY_NAME)
            ?: "MIDI device").trim()
    }

    /** "<name> (<id>)": the Windows-style port names add " 0" (receive) or " 1" (send). */
    private fun label(info: MidiDeviceInfo) = "${name(info)} (${info.id})"

    /** Port names that receive from devices, as mido.get_input_names() returns them. */
    fun inputNames(): Array<String> =
        infos().filter { it.outputPortCount > 0 }.map { "${label(it)} 0" }.toTypedArray()

    /** Port names that send to devices, as mido.get_output_names() returns them. */
    fun outputNames(): Array<String> =
        infos().filter { it.inputPortCount > 0 }.map { "${label(it)} 1" }.toTypedArray()

    fun devicesJson(): String {
        val list = JSONArray()
        for (info in infos()) {
            list.put(JSONObject().put("id", info.id).put("name", name(info))
                .put("manufacturer", info.properties.getString(MidiDeviceInfo.PROPERTY_MANUFACTURER) ?: "")
                .put("usb", info.type == MidiDeviceInfo.TYPE_USB))
        }
        return list.toString()
    }

    private fun idOf(portName: String): Int {
        val match = Regex("""\((\d+)\) [01]$""").find(portName)
            ?: throw IllegalArgumentException("Unknown MIDI port: $portName")
        return match.groupValues[1].toInt()
    }

    /** Open (once) the device behind a port name; waits up to 5 seconds. */
    fun connection(portName: String): Connection = connectionFor(idOf(portName))

    fun connectionFor(id: Int): Connection {
        open[id]?.let { return it }
        synchronized(this) {
            open[id]?.let { return it }
            val info = infos().firstOrNull { it.id == id }
                ?: throw IllegalStateException("The MIDI device is no longer connected.")
            val future = CompletableFuture<MidiDevice?>()
            manager.openDevice(info, { future.complete(it) }, handler)
            val device = future.get(5, TimeUnit.SECONDS)
                ?: throw IllegalStateException("Android could not open ${name(info)}.")
            return Connection(info, device).also { open[id] = it }
        }
    }

    fun subscribe(portName: String): Subscription {
        val connection = connection(portName)
        return Subscription(connection).also { connection.subscribers.add(it) }
    }

    /** Send hex-encoded MIDI bytes to the device behind a port name. */
    fun send(portName: String, hexMessage: String) {
        connection(portName).send(bytes(hexMessage))
    }

    private fun received(connection: Connection, message: ByteArray) {
        for (subscriber in connection.subscribers) subscriber.queue.offer(message)
        val target = relayTarget
        if (connection.info.id != relaySource || target < 0) return
        // The editor talks to the controller with SysEx replies; never forward those.
        if (connection.subscribers.isNotEmpty()) return
        var forwarded = false
        try {
            open[target]?.send(message)
            forwarded = open[target] != null
        } catch (_: Exception) {
            forwarded = false
        }
        listener?.emit("stompilot-live-message", JSONObject()
            .put("hex", hex(message)).put("forwarded", forwarded).put("time", System.currentTimeMillis()).toString())
    }

    /**
     * Forward the Chocolate Plus to the TANK; returns JSON with ok and either
     * the two device names or a message saying what is missing.
     */
    fun startRelay(): String {
        val devices = infos()
        val chocolate = devices.filter { "footctrlplus" in name(it).lowercase() }
        val tank = devices.filter { name(it).lowercase().let { n -> "usb-midi" in n || "tank" in n } }
        val problem = when {
            chocolate.size != 1 -> "Connect one Chocolate Plus with its switch on U (found ${chocolate.size})."
            tank.size != 1 -> "Connect one TANK Mini (found ${tank.size})."
            else -> null
        }
        if (problem != null) return JSONObject().put("ok", false).put("message", problem).toString()
        return try {
            connectionFor(chocolate[0].id)
            connectionFor(tank[0].id)
            relaySource = chocolate[0].id
            relayTarget = tank[0].id
            JSONObject().put("ok", true).put("chocolate", name(chocolate[0])).put("tank", name(tank[0])).toString()
        } catch (error: Exception) {
            JSONObject().put("ok", false).put("message", error.message ?: error.toString()).toString()
        }
    }

    fun stopRelay() {
        relaySource = -1
        relayTarget = -1
    }

    fun close() {
        stopRelay()
        for (connection in open.values) connection.close()
        open.clear()
        thread.quitSafely()
    }

    companion object {
        fun hex(bytes: ByteArray): String = bytes.joinToString(" ") { "%02X".format(it.toInt() and 0xFF) }

        fun bytes(hex: String): ByteArray {
            val clean = hex.replace(" ", "")
            require(clean.length % 2 == 0) { "Odd number of hex digits" }
            return ByteArray(clean.length / 2) { clean.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
        }
    }
}
