package app.stompilot

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.result.contract.ActivityResultContracts
import android.view.WindowManager
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.widget.FrameLayout
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.webkit.WebViewAssetLoader
import androidx.webkit.WebViewClientCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/** Hosts the desktop interface in a WebView, served from the app's assets. */
class MainActivity : ComponentActivity() {
    lateinit var bridge: Bridge
        private set
    lateinit var midi: MidiHub
        private set

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))

        val web = WebView(this)
        val frame = FrameLayout(this).apply { addView(web) }
        setContentView(frame)
        // Keep the interface clear of the status bar, navigation bar and cutouts.
        ViewCompat.setOnApplyWindowInsetsListener(frame) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            WindowInsetsCompat.CONSUMED
        }

        val assets = WebViewAssetLoader.Builder()
            .addPathHandler("/assets/", WebViewAssetLoader.AssetsPathHandler(this))
            .build()
        web.webViewClient = object : WebViewClientCompat() {
            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? =
                assets.shouldInterceptRequest(request.url)
        }
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true

        midi = MidiHub(this)
        bridge = Bridge(this, web, Documents(this), midi)
        midi.listener = bridge
        web.addJavascriptInterface(bridge, "StompilotAndroid")
        onBackPressedDispatcher.addCallback(this, closeLive)
        web.loadUrl("https://appassets.androidplatform.net/assets/web/index.html")
    }

    private val notifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) {
        // Forwarding works either way; without the permission its notification is hidden.
    }

    fun startLiveService() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            notifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        startForegroundService(Intent(this, LiveService::class.java))
    }

    fun stopLiveService() {
        stopService(Intent(this, LiveService::class.java))
    }

    /** The back button closes the live view instead of the app while it is open. */
    private val closeLive = object : OnBackPressedCallback(false) {
        override fun handleOnBackPressed() = bridge.emit("stompilot-back", "{}")
    }

    fun setLiveScreen(on: Boolean) {
        if (on) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        val bars = WindowCompat.getInsetsController(window, window.decorView)
        if (on) {
            bars.systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            bars.hide(WindowInsetsCompat.Type.systemBars())
        } else {
            bars.show(WindowInsetsCompat.Type.systemBars())
        }
        closeLive.isEnabled = on
    }

    override fun onDestroy() {
        stopLiveService()
        midi.close()
        super.onDestroy()
    }
}
