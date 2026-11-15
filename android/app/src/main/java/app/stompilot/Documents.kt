package app.stompilot

import android.content.Intent
import android.net.Uri
import android.provider.OpenableColumns
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ConcurrentHashMap

/**
 * Android file pickers for the Python service.
 *
 * The service reads and writes ordinary files, so a picked document is copied
 * to a private local file whose path the service receives. After each request,
 * [sync] copies every changed local file back to the document it came from.
 */
class Documents(private val activity: ComponentActivity) {
    private class Link(val uri: Uri, var digest: String)

    private val root = File(activity.filesDir, "documents")
    private val links = ConcurrentHashMap<String, Link>()
    private var pending: CompletableFuture<Uri?>? = null

    private val open = activity.registerForActivityResult(ActivityResultContracts.OpenDocument()) { complete(it) }
    private val createJson = activity.registerForActivityResult(ActivityResultContracts.CreateDocument("application/json")) { complete(it) }
    private val createText = activity.registerForActivityResult(ActivityResultContracts.CreateDocument("text/plain")) { complete(it) }

    private fun complete(uri: Uri?) {
        synchronized(this) {
            pending?.complete(uri)
            pending = null
        }
    }

    /** Called by Python on a worker thread; waits for the picker and returns a local path, or null if cancelled. */
    fun pick(kind: String, suggestedName: String): String? {
        val future = CompletableFuture<Uri?>()
        synchronized(this) {
            if (pending != null) return null
            pending = future
        }
        val reading = kind == "open_project" || kind == "import_profile"
        activity.runOnUiThread {
            try {
                when {
                    // JSON files often have no JSON type on Android, so every file can be chosen.
                    reading -> open.launch(arrayOf("*/*"))
                    kind == "export_messages" -> createText.launch(suggestedName)
                    else -> createJson.launch(suggestedName)
                }
            } catch (error: Exception) {
                complete(null)
            }
        }
        val uri = future.get() ?: return null
        if (reading) {
            try {
                activity.contentResolver.takePersistableUriPermission(
                    uri, Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            } catch (_: SecurityException) {
                // Read access for this session is enough to open the file.
            }
        }
        val folder = File(root, System.nanoTime().toString()).apply { mkdirs() }
        val file = File(folder, displayName(uri) ?: suggestedName.ifBlank { "stompilot.json" })
        if (reading) {
            val input = activity.contentResolver.openInputStream(uri) ?: return null
            input.use { source -> file.outputStream().use { source.copyTo(it) } }
        }
        links[file.absolutePath] = Link(uri, digest(file))
        return file.absolutePath
    }

    /** Copy changed local files back to their documents; returns an error message or null. */
    fun sync(): String? {
        for ((path, link) in links) {
            val file = File(path)
            if (!file.isFile) continue
            val current = digest(file)
            if (current == link.digest) continue
            try {
                val output = activity.contentResolver.openOutputStream(link.uri, "wt")
                    ?: return "Could not write ${file.name}. Use Save as to choose another file."
                output.use { target -> file.inputStream().use { it.copyTo(target) } }
                link.digest = current
            } catch (error: Exception) {
                return "Could not write ${file.name}: ${error.message}. Use Save as to choose another file."
            }
        }
        return null
    }

    private fun displayName(uri: Uri): String? =
        activity.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
            if (cursor.moveToFirst()) cursor.getString(0)?.takeIf { it.isNotBlank() } else null
        }

    private fun digest(file: File): String {
        if (!file.isFile) return ""
        val sha = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(8192)
            while (true) {
                val read = input.read(buffer)
                if (read < 0) break
                sha.update(buffer, 0, read)
            }
        }
        return sha.digest().joinToString("") { "%02x".format(it) }
    }
}
