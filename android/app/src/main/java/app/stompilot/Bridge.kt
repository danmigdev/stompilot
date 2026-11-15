package app.stompilot

import android.webkit.JavascriptInterface
import android.webkit.WebView
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import org.json.JSONObject
import java.io.File
import java.util.concurrent.Executors

/**
 * The interface's `request(method, payload)` channel, as on the desktop, plus
 * the live view's controls.
 *
 * Requests run on worker threads, so the interface can poll the service's
 * snapshot while a long USB transfer runs; the service serializes the rest.
 */
class Bridge(
    private val activity: MainActivity,
    private val web: WebView,
    private val documents: Documents,
    private val midi: MidiHub,
) : EventSink {
    private val workers = Executors.newCachedThreadPool()

    private val service: PyObject by lazy {
        Python.getInstance().getModule("stompilot_android")
            .callAttr("create", File(activity.filesDir, "data").absolutePath, documents, midi)
    }

    @JavascriptInterface
    fun request(id: String, method: String, payload: String) {
        workers.execute {
            var result = try {
                service.callAttr("request_json", method, payload).toString()
            } catch (error: Throwable) {
                failure(error.message ?: error.toString())
            }
            documents.sync()?.let { result = failure(it) }
            web.post {
                web.evaluateJavascript(
                    "window.__stompilotResolve && window.__stompilotResolve(${JSONObject.quote(id)}, ${JSONObject.quote(result)})",
                    null)
            }
        }
    }

    /** While the live view is open: full screen, screen kept on, back button closes it. */
    @JavascriptInterface
    fun setLiveScreen(on: Boolean) {
        activity.runOnUiThread { activity.setLiveScreen(on) }
    }

    /** Start forwarding the Chocolate Plus to the TANK; JSON with ok and the devices or a message. */
    @JavascriptInterface
    fun startLive(): String {
        val result = midi.startRelay()
        if (JSONObject(result).optBoolean("ok")) activity.runOnUiThread { activity.startLiveService() }
        return result
    }

    @JavascriptInterface
    fun stopLive() {
        midi.stopRelay()
        activity.runOnUiThread { activity.stopLiveService() }
    }

    /** Send a JSON event to the interface as a window CustomEvent. */
    override fun emit(event: String, json: String) {
        web.post {
            web.evaluateJavascript(
                "window.dispatchEvent(new CustomEvent(${JSONObject.quote(event)}, {detail: JSON.parse(${JSONObject.quote(json)})}))",
                null)
        }
    }

    private fun failure(message: String): String =
        JSONObject().put("ok", false).put("error", JSONObject().put("message", message)).toString()
}
