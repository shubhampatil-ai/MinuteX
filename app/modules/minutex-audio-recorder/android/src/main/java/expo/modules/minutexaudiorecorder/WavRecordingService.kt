package expo.modules.minutexaudiorecorder

// Microphone foreground service for the experimental WAV recorder.
//
// WHY THIS IS REQUIRED, not optional. On Android 9+ an app that loses
// foreground status also loses microphone access: AudioRecord keeps returning
// successfully but every frame is ZEROS. That is the same class of failure the
// AAC path already fights (see modules/audio-focus) — silent, undetectable from
// the return value, and it destroys the recording rather than erroring. From
// Android 14 the microphone FGS type is enforced explicitly and startForeground
// throws without it.
//
// expo-audio solves this for its own recorder with AudioRecordingService (see
// enableBackgroundRecording in app.json). That service is bound to expo-audio
// AudioRecorder instances and cannot be reused for ours, so the WAV engine
// needs its own — with its own channel, so the two notifications never collide.
//
// SCOPE: this keeps the process foregrounded while capture runs. It does not
// defend against the user force-stopping the app or the OEM battery manager
// killing it; nothing can. Segments already on disk survive both, which is why
// the header is written up front (see WavRecorder.start).

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat

class WavRecordingService : Service() {

  override fun onBind(intent: Intent?): IBinder? = null

  override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
    if (intent?.action == ACTION_STOP) {
      stopSelfSafely()
      return START_NOT_STICKY
    }

    createChannel()
    try {
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
        startForeground(
          NOTIFICATION_ID,
          buildNotification(),
          ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
        )
      } else {
        startForeground(NOTIFICATION_ID, buildNotification())
      }
    } catch (e: Exception) {
      // Android 14+ throws if the mic FGS permission or notification permission
      // is missing. Stop cleanly rather than leave a half-started service; the
      // module reports the failure so the caller can decide whether to record
      // foreground-only.
      stopSelfSafely()
      return START_NOT_STICKY
    }

    // NOT sticky: if the process dies the recording is over, and a restarted
    // service with no recorder behind it would show a notification for a
    // recording that is not happening.
    return START_NOT_STICKY
  }

  private fun stopSelfSafely() {
    try {
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
        stopForeground(STOP_FOREGROUND_REMOVE)
      } else {
        @Suppress("DEPRECATION")
        stopForeground(true)
      }
    } catch (e: Exception) {
      // Already stopped.
    }
    stopSelf()
  }

  private fun createChannel() {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
    val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
    if (manager.getNotificationChannel(CHANNEL_ID) != null) return
    val channel = NotificationChannel(
      CHANNEL_ID,
      "Recording (experimental WAV)",
      // LOW: an ongoing recording indicator must be visible but must never
      // make a sound — a notification chime would be captured by the mic it is
      // announcing.
      NotificationManager.IMPORTANCE_LOW
    ).apply {
      description = "Shown while MinuteX is recording in high-quality WAV mode."
      setShowBadge(false)
      setSound(null, null)
      enableVibration(false)
    }
    manager.createNotificationChannel(channel)
  }

  private fun buildNotification(): Notification =
    NotificationCompat.Builder(this, CHANNEL_ID)
      .setContentTitle("MinuteX is recording")
      .setContentText("Recording in WAV (experimental)")
      // The app launcher icon: a local drawable would have to ship with the
      // module and would not match the host app branding.
      .setSmallIcon(applicationInfo.icon)
      .setOngoing(true)
      .setSilent(true)
      .setCategory(NotificationCompat.CATEGORY_SERVICE)
      .setPriority(NotificationCompat.PRIORITY_LOW)
      .build()

  companion object {
    private const val CHANNEL_ID = "minutex_wav_recording_channel"
    private const val NOTIFICATION_ID = 0xA10D
    private const val ACTION_STOP = "expo.modules.minutexaudiorecorder.STOP"

    /** Returns false if the service could not be started (caller records anyway). */
    fun start(context: Context): Boolean = try {
      val intent = Intent(context, WavRecordingService::class.java)
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        context.startForegroundService(intent)
      } else {
        context.startService(intent)
      }
      true
    } catch (e: Exception) {
      // ForegroundServiceStartNotAllowedException on 12+, or a missing
      // permission on 14+. Not fatal: foreground-only recording still works.
      false
    }

    fun stop(context: Context) {
      try {
        context.stopService(Intent(context, WavRecordingService::class.java))
      } catch (e: Exception) {
        // Never started, or already gone.
      }
    }
  }
}
