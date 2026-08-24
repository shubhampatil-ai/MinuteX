package expo.modules.audiofocus

// Why this module exists at all.
//
// expo-audio DOES install an OnAudioFocusChangeListener (AudioModule.kt), but
// every branch of it acts on `allPlayables` — players. AudioRecorder is never
// consulted. So on Android nothing in expo-audio reacts to the recorder losing
// audio focus.
//
// That matters because of how MediaRecorder behaves when the telephony stack
// takes the microphone: it does NOT error and it does NOT stop. It keeps
// encoding *silence*. `AudioRecorder.isRecording` is a plain Kotlin boolean
// flipped only by our own record()/pause()/stop() calls, and durationMillis is
// wall-clock arithmetic, so both keep insisting all is well while the file
// fills with nothing. A JS-side poll of getStatus() therefore cannot detect a
// call — which is exactly the bug this module fixes.
//
// The only signal Android gives us without READ_PHONE_STATE is audio focus.
// AUDIOFOCUS_LOSS_TRANSIENT is what a phone call raises; a permanent
// AUDIOFOCUS_LOSS is another app taking the mic for good. Neither tells us
// *what* took it, and we deliberately do not request READ_PHONE_STATE to find
// out — the required behaviour (pause, preserve, resume if the user hadn't
// paused) is identical either way.
//
// DESIGN: this module reports facts and owns no policy. It does not touch the
// recorder, does not pause anything, and does not decide what a given focus
// change means for the recording. All of that lives in lib/rec-controller.ts,
// so the state machine stays in one readable place and this file stays small
// enough to audit.

import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Build
import android.provider.Settings
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition

// Mirrors the string union in lib/audio-focus.ts. Keep the two in step.
private const val EVENT_FOCUS_CHANGE = "onAudioFocusChange"

class AudioFocusModule : Module() {
  private val context: Context
    get() = appContext.reactContext ?: throw IllegalStateException("No react context")

  private val audioManager: AudioManager
    get() = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager

  private val notificationManager: NotificationManager
    get() = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

  private var focusRequest: AudioFocusRequest? = null
  private var listening = false

  // The ringer mode as it was BEFORE we silenced it, so stop() can put the
  // phone back exactly as the user had it. Null means "we have not silenced
  // anything", which is what makes restore idempotent and safe to call on a
  // path where silencing never happened (permission refused, already silent).
  private var previousRingerMode: Int? = null

  // Held so the pre-O abandonAudioFocus() path can pass the same instance it
  // registered — abandoning with a different object is a silent no-op.
  private val listener = AudioManager.OnAudioFocusChangeListener { focusChange ->
    val kind = when (focusChange) {
      AudioManager.AUDIOFOCUS_LOSS -> "loss"
      AudioManager.AUDIOFOCUS_LOSS_TRANSIENT -> "loss_transient"
      AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK -> "loss_transient_can_duck"
      AudioManager.AUDIOFOCUS_GAIN -> "gain"
      else -> "unknown"
    }
    // Emitted for every change including ducking, which the JS side ignores
    // for recording. Reporting it rather than filtering here keeps the policy
    // decision in one place.
    sendEvent(EVENT_FOCUS_CHANGE, mapOf("change" to kind, "raw" to focusChange))
  }

  override fun definition() = ModuleDefinition {
    Name("AudioFocus")

    Events(EVENT_FOCUS_CHANGE)

    // Start reporting focus changes.
    //
    // We request focus with AUDIOFOCUS_GAIN so that the OS has a registered
    // listener to notify. expo-audio's own request is tied to playback and is
    // released whenever nothing is playing (shouldReleaseFocus() checks
    // `allPlayables`), so during a recording there may be no active request at
    // all — and with no request there are no callbacks. This gives the
    // recording its own.
    AsyncFunction("startListening") {
      if (listening) return@AsyncFunction true

      val result = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        val attrs = AudioAttributes.Builder()
          // Recording is not media playback. VOICE_COMMUNICATION +
          // CONTENT_TYPE_SPEECH describes what we are actually doing and makes
          // the OS treat a call as a genuine conflict, which is the signal we
          // want.
          .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
          .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
          .build()
        val req = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
          .setAudioAttributes(attrs)
          .setOnAudioFocusChangeListener(listener)
          // We want to be told about transient losses, not silently ducked.
          .setWillPauseWhenDucked(false)
          // Never block: if focus is refused we still record (the mic may well
          // be available anyway) and rely on the JS-side silence detection.
          .setAcceptsDelayedFocusGain(false)
          .build()
        focusRequest = req
        audioManager.requestAudioFocus(req)
      } else {
        @Suppress("DEPRECATION")
        audioManager.requestAudioFocus(
          listener,
          AudioManager.STREAM_MUSIC,
          AudioManager.AUDIOFOCUS_GAIN
        )
      }

      listening = true
      // Report whether focus was granted, but do NOT treat refusal as fatal.
      result == AudioManager.AUDIOFOCUS_REQUEST_GRANTED
    }

    AsyncFunction("stopListening") {
      if (!listening) return@AsyncFunction true
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        focusRequest?.let { audioManager.abandonAudioFocusRequest(it) }
        focusRequest = null
      } else {
        @Suppress("DEPRECATION")
        audioManager.abandonAudioFocus(listener)
      }
      listening = false
      true
    }

    // Whether a phone call is in progress, WITHOUT READ_PHONE_STATE.
    //
    // AudioManager.mode is readable by any app and the telephony stack sets it
    // to IN_CALL (cellular) or IN_COMMUNICATION (VoIP — WhatsApp, Meet) for
    // the duration of a call. That is enough to label the pause reason
    // honestly as "call" rather than the generic "audio_interruption", which
    // is the difference between the UI saying something true and something
    // vague. It is a best-effort hint: some OEMs and some VoIP apps do not set
    // it reliably, so JS must not depend on it being correct.
    Function("isInCall") {
      val mode = audioManager.mode
      mode == AudioManager.MODE_IN_CALL || mode == AudioManager.MODE_IN_COMMUNICATION
    }

    // The call phase, as far as AudioManager.mode can tell us. This is what
    // separates "the phone is ringing" from "the user answered".
    //
    //   "idle"    no call activity
    //   "ringing" RINGTONE mode — a call is coming in but has NOT been answered
    //   "in_call" IN_CALL / IN_COMMUNICATION — answered, the mic is in use
    //
    // Why this matters for recording: an incoming ring raises
    // AUDIOFOCUS_LOSS_TRANSIENT exactly like an answered call does, so focus
    // alone cannot tell them apart. Pausing on the ring is wrong — most rings
    // are ignored or rejected, and pausing a meeting because someone's phone
    // rang loses the seconds around it for nothing. Only an ANSWERED call
    // actually takes the microphone, and only then must we pause.
    //
    // Still best-effort, same caveat as isInCall(): some OEMs and VoIP apps do
    // not set mode faithfully. The JS side treats an unreliable answer as
    // "keep recording and let the silence detector catch a truly dead mic",
    // which fails safe — a slightly noisy recording beats a silently truncated
    // one.
    Function("getCallPhase") {
      when (audioManager.mode) {
        AudioManager.MODE_RINGTONE -> "ringing"
        AudioManager.MODE_IN_CALL, AudioManager.MODE_IN_COMMUNICATION -> "in_call"
        else -> "idle"
      }
    }

    // Whether we may change the ringer mode.
    //
    // Silencing the phone requires Do Not Disturb access on Android 6+
    // (ACCESS_NOTIFICATION_POLICY). It is a special-access permission: it can
    // only be granted from a system Settings screen, never from a runtime
    // dialog, which is why this is a query plus an intent rather than a
    // request() call.
    Function("canSilenceRinger") {
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
        notificationManager.isNotificationPolicyAccessGranted
      } else {
        // Pre-M needs no grant — setRingerMode was freely available.
        true
      }
    }

    // Open the DND-access Settings screen so the user can grant it. Returns
    // false if the screen cannot be opened (some OEM ROMs omit it), so the
    // caller can say something honest instead of appearing to do nothing.
    AsyncFunction("openSilenceRingerSettings") {
      try {
        val intent = Intent(Settings.ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS)
          .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(intent)
        true
      } catch (e: Exception) {
        false
      }
    }

    // Silence the ringer for the duration of a recording, remembering what it
    // was so it can be put back.
    //
    // RINGER_MODE_SILENT, not VIBRATE: a vibrating phone sitting on the same
    // table as the microphone is clearly audible in the recording — which is
    // the exact artifact this exists to remove.
    //
    // Returns true only if the ringer is now silent. A refusal is not an
    // error: the recording proceeds either way, and ring-aware pausing already
    // keeps an unanswered call from truncating the audio.
    AsyncFunction("silenceRinger") {
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M &&
        !notificationManager.isNotificationPolicyAccessGranted
      ) {
        return@AsyncFunction false
      }
      try {
        val current = audioManager.ringerMode
        if (current != AudioManager.RINGER_MODE_SILENT) {
          // Only remember a mode we are actually replacing. Overwriting this
          // on a second call would lose the user's original setting.
          if (previousRingerMode == null) previousRingerMode = current
          audioManager.ringerMode = AudioManager.RINGER_MODE_SILENT
        }
        true
      } catch (e: SecurityException) {
        // Some ROMs throw despite reporting access. Treated as "could not
        // silence" rather than crashing a recording that is about to start.
        false
      }
    }

    // Put the ringer back exactly as it was. Idempotent, and a no-op when we
    // never silenced anything — so it is safe on every stop path, including
    // the ones that run after a crash-recovery where no silencing happened.
    AsyncFunction("restoreRinger") {
      val prev = previousRingerMode ?: return@AsyncFunction true
      try {
        audioManager.ringerMode = prev
        previousRingerMode = null
        true
      } catch (e: SecurityException) {
        // The user may have revoked DND access mid-recording. Clear our record
        // of it anyway: retrying forever cannot succeed, and leaving the field
        // set would make a later restore attempt use a stale value.
        previousRingerMode = null
        false
      }
    }

    OnDestroy {
      // Never leave a phone silenced because the app went away. This is the
      // last line of defence for the case where stop() never ran.
      previousRingerMode?.let {
        try {
          audioManager.ringerMode = it
        } catch (e: SecurityException) {
          // Nothing further we can do from a destroyed module.
        }
        previousRingerMode = null
      }

      if (listening) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
          focusRequest?.let { audioManager.abandonAudioFocusRequest(it) }
        } else {
          @Suppress("DEPRECATION")
          audioManager.abandonAudioFocus(listener)
        }
        listening = false
      }
    }
  }
}
