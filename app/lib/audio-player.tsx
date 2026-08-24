// lib/audio-player.tsx — recording playback, "Workspace" edition.
//
// An Apple-Music-style hero player: a large tap-to-seek waveform, a big
// circular play button, mono elapsed/remaining timecodes, and skip ±15s /
// speed controls as soft pill buttons beneath. `variant` is retained for
// call-site compatibility — "hero" is the full player used in the meeting
// header, "card" is a compact docked bar used elsewhere.
//
// Built on expo-audio. Kept separate from ui.tsx (pure presentational kit)
// since this owns real player state via expo-audio's hooks, not just styling.
//
// SEEK FROM ELSEWHERE ON THE PAGE
// The transcript needs "tap a timestamp to jump the audio there", which means
// something outside this component has to drive the player. That's what
// <AudioPlayerProvider> + useAudioSeek() are for: the provider owns the player
// and publishes a seek function, so the transcript can call seekTo(seconds)
// without knowing anything about expo-audio. Progress is published too, so a
// transcript row can highlight itself as the audio passes through it.
//
// expo-audio is a NATIVE module — it only works in a dev client / build that
// was actually compiled with it. Importing it statically throws
// "Cannot find native module 'ExpoAudio'" at module-load time in Expo Go or
// an older dev client, which crashes the whole screen before anything
// renders. Loaded defensively here so the rest of the meeting detail screen
// keeps working — playback UI just falls back to a clear "rebuild needed"
// notice instead of taking the app down.
import { createContext, useCallback, useContext, useMemo, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import { Icon } from "./icons";
import { S, R, ELEV, TABULAR, useTheme, ColorScale } from "./theme";
import { Waveform } from "./waveform";

type ExpoAudioModule = {
  useAudioPlayer: (url: string) => any;
  useAudioPlayerStatus: (player: any) => any;
};

function loadExpoAudio(): ExpoAudioModule | null {
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const mod = require("expo-audio");
    if (mod && typeof mod.useAudioPlayer === "function") return mod as ExpoAudioModule;
  } catch {
    // fall through — native module not present in this build
  }
  if (__DEV__) {
    console.warn(
      "[audio-player] expo-audio native module unavailable — playback UI " +
      "will show a rebuild notice. Rebuild the dev client (npx expo run:android) " +
      "to enable in-app playback."
    );
  }
  return null;
}

const expoAudio = loadExpoAudio();

// Playback speeds. Meetings are the classic 1.5x listen, so the ladder is
// weighted toward faster-than-real-time rather than symmetric around 1x.
const SPEEDS = [1, 1.25, 1.5, 2, 0.75] as const;

function fmt(sec: number): string {
  if (!isFinite(sec) || sec < 0) return "0:00";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

// ---------------------------------------------------------------------------
// Context — lets the transcript seek the audio and follow along.
//
// `seekTo` is null when there is no player mounted (no audio_url, or a build
// without expo-audio), so callers can hide their affordance instead of
// offering a tap that would do nothing.
// ---------------------------------------------------------------------------
type AudioSeekValue = {
  seekTo: ((seconds: number) => void) | null;
  currentTime: number;
  playing: boolean;
};

const AudioSeekCtx = createContext<AudioSeekValue>({
  seekTo: null, currentTime: 0, playing: false,
});

export function useAudioSeek(): AudioSeekValue {
  return useContext(AudioSeekCtx);
}

/**
 * Owns the player for a screen and publishes seek/progress to its subtree.
 *
 * Renders no UI itself — place <AudioPlayer /> anywhere inside it (typically
 * docked at the bottom) and the two will be wired together automatically.
 * Without a url, children still render and useAudioSeek() reports seekTo:null.
 */
export function AudioPlayerProvider({
  url, children,
}: { url: string | null | undefined; children: React.ReactNode }) {
  if (!expoAudio || !url) {
    return (
      <AudioSeekCtx.Provider value={{ seekTo: null, currentTime: 0, playing: false }}>
        {children}
      </AudioSeekCtx.Provider>
    );
  }
  return <RealProvider url={url} audio={expoAudio}>{children}</RealProvider>;
}

// The player instance lives here so both the provider's context and the
// docked <AudioPlayer/> read the SAME expo-audio player. Sharing it through
// context is what makes a transcript tap and the scrubber agree; two
// useAudioPlayer() calls on one url would be two independent players.
type PlayerHandle = {
  player: any;
  status: any;
  setSpeed: (rate: number) => void;
  speed: number;
};

const PlayerCtx = createContext<PlayerHandle | null>(null);

function RealProvider({
  url, audio, children,
}: { url: string; audio: ExpoAudioModule; children: React.ReactNode }) {
  const player = audio.useAudioPlayer(url);
  const status = audio.useAudioPlayerStatus(player);
  const [speed, setSpeedState] = useState(1);

  const setSpeed = useCallback((rate: number) => {
    setSpeedState(rate);
    try {
      // setPlaybackRate is the expo-audio API; guard because older builds of
      // the native module may not expose it and a throw here would take the
      // whole screen down.
      player.setPlaybackRate?.(rate);
    } catch {
      // Speed is a convenience — losing it must not break playback.
    }
  }, [player]);

  const seekTo = useCallback((seconds: number) => {
    const duration = status?.duration ?? 0;
    if (!duration) return;
    player.seekTo(Math.max(0, Math.min(duration, seconds)));
    // Tapping a transcript timestamp means "play from here" — seeking while
    // paused and staying paused would look like nothing happened.
    if (!status?.playing) player.play();
  }, [player, status?.duration, status?.playing]);

  const seekValue = useMemo<AudioSeekValue>(() => ({
    seekTo,
    currentTime: status?.currentTime ?? 0,
    playing: !!status?.playing,
  }), [seekTo, status?.currentTime, status?.playing]);

  const handle = useMemo<PlayerHandle>(
    () => ({ player, status, setSpeed, speed }),
    [player, status, setSpeed, speed]
  );

  return (
    <PlayerCtx.Provider value={handle}>
      <AudioSeekCtx.Provider value={seekValue}>{children}</AudioSeekCtx.Provider>
    </PlayerCtx.Provider>
  );
}

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    // Hero: a soft rounded card holding the big play button, waveform and
    // timecodes — the single most prominent object under the meeting header.
    heroCard: {
      backgroundColor: C.surface, borderRadius: R.card,
      borderWidth: 1, borderColor: C.border,
      padding: S.lg, shadowColor: C.shadow, ...ELEV.sm,
    },
    heroRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: S.md },
    playCircle: {
      width: 52, height: 52, borderRadius: 26, backgroundColor: C.primary,
      alignItems: "center" as const, justifyContent: "center" as const,
      shadowColor: C.primary, shadowOpacity: 0.3, shadowRadius: 10, shadowOffset: { width: 0, height: 4 },
    },
    timeRow: { flexDirection: "row" as const, justifyContent: "space-between" as const, marginTop: 8 },
    time: { ...TABULAR, fontSize: 12, color: C.textFaint },
    speedPill: { fontSize: 12, color: C.textFaint },
    // Compact "card" bar for non-hero placements.
    barCard: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 12,
      backgroundColor: C.surface2, borderRadius: R.pill,
      paddingHorizontal: 14, paddingVertical: 10,
    },
    skipRow: {
      flexDirection: "row" as const, alignItems: "center" as const,
      justifyContent: "center" as const, gap: S.xl, marginTop: 14,
    },
    skipBtn: {
      width: 44, height: 44, borderRadius: 22, backgroundColor: C.surface2,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    speedBtn: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 4,
      paddingHorizontal: 12, paddingVertical: 8,
      backgroundColor: C.surface2, borderRadius: R.pill,
    },
    unavailable: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.sm,
      backgroundColor: C.surface, borderWidth: 1, borderColor: C.border,
      borderRadius: R.card, padding: S.md,
    },
  });
}

export function AudioPlayer({
  url, seed, variant = "hero",
}: { url: string; seed?: string; variant?: "hero" | "card" }) {
  const shared = useContext(PlayerCtx);
  if (!expoAudio) return <AudioUnavailable />;
  // Inside an <AudioPlayerProvider> the player is shared (so transcript seeks
  // and the scrubber stay in sync). Standalone, it owns its own — which keeps
  // every existing call site working untouched.
  if (shared) {
    return <PlayerBar seed={seed ?? url} variant={variant} handle={shared} />;
  }
  return <StandalonePlayer url={url} seed={seed ?? url} audio={expoAudio} variant={variant} />;
}

function AudioUnavailable() {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  return (
    <View style={st.unavailable}>
      <Icon name="exclamationmark.triangle.fill" tintColor={C.warn} size={18} />
      <Text style={[T.caption, { flex: 1 }]}>
        Playback needs a rebuilt app (expo-audio isn&apos;t in this build yet).
      </Text>
    </View>
  );
}

function StandalonePlayer({
  url, seed, audio, variant,
}: { url: string; seed: string; audio: ExpoAudioModule; variant: "hero" | "card" }) {
  const player = audio.useAudioPlayer(url);
  const status = audio.useAudioPlayerStatus(player);
  const [speed, setSpeedState] = useState(1);
  const setSpeed = useCallback((rate: number) => {
    setSpeedState(rate);
    try { player.setPlaybackRate?.(rate); } catch { /* non-fatal */ }
  }, [player]);
  const handle = useMemo<PlayerHandle>(
    () => ({ player, status, setSpeed, speed }),
    [player, status, setSpeed, speed]
  );
  return <PlayerBar seed={seed} variant={variant} handle={handle} />;
}

function PlayerBar({
  seed, variant, handle,
}: { seed: string; variant: "hero" | "card"; handle: PlayerHandle }) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { player, status, setSpeed, speed } = handle;

  const toggle = () => {
    if (status.playing) player.pause();
    else {
      // Restart from the top once playback has run to the end — otherwise
      // tapping play again after finishing does nothing (currentTime is
      // already at duration).
      if (status.didJustFinish || (status.duration > 0 && status.currentTime >= status.duration)) {
        player.seekTo(0);
      }
      player.play();
    }
  };

  const seekToRatio = (ratio: number) => {
    if (!status.duration) return;
    player.seekTo(Math.max(0, Math.min(1, ratio)) * status.duration);
  };

  const skipBy = (delta: number) => {
    if (!status.duration) return;
    player.seekTo(Math.max(0, Math.min(status.duration, status.currentTime + delta)));
  };

  const cycleSpeed = () => {
    const i = SPEEDS.indexOf(speed as (typeof SPEEDS)[number]);
    setSpeed(SPEEDS[(i + 1) % SPEEDS.length]);
  };

  const progress = status.duration > 0 ? status.currentTime / status.duration : 0;
  const remaining = Math.max(0, (status.duration || 0) - (status.currentTime || 0));

  if (variant === "card") {
    return (
      <View style={st.barCard}>
        <Pressable onPress={toggle} hitSlop={8} accessibilityLabel={status.playing ? "Pause" : "Play"}>
          <Icon name={status.playing ? "pause.circle.fill" : "play.circle.fill"} tintColor={C.primary} size={34} />
        </Pressable>
        <View style={{ flex: 1 }}>
          <Waveform
            seed={seed} bars={40} height={22} barWidth={2.5} progress={progress}
            color={C.primary} dimColor={C.border} onSeek={seekToRatio}
          />
        </View>
        <Text style={st.time}>{fmt(remaining)}</Text>
      </View>
    );
  }

  return (
    <View style={st.heroCard}>
      <View style={st.heroRow}>
        <Pressable
          onPress={toggle}
          hitSlop={8}
          accessibilityLabel={status.playing ? "Pause" : "Play"}
          style={({ pressed }) => [st.playCircle, pressed && { opacity: 0.85 }]}
        >
          {status.isBuffering && !status.isLoaded ? (
            <Icon name="hourglass" tintColor={C.textOnPrimary} size={22} />
          ) : (
            <Icon name={status.playing ? "pause.fill" : "play.fill"} tintColor={C.textOnPrimary} size={22} />
          )}
        </Pressable>

        {/* Large tap-to-seek waveform — the hero visual for the whole player. */}
        <View style={{ flex: 1 }}>
          <Waveform
            seed={seed}
            bars={48}
            height={40}
            barWidth={3}
            progress={progress}
            color={C.primary}
            dimColor={C.border}
            onSeek={seekToRatio}
          />
          <View style={st.timeRow}>
            <Text style={st.time}>{fmt(status.currentTime || 0)}</Text>
            <Text style={st.time}>-{fmt(remaining)}</Text>
          </View>
        </View>
      </View>

      {/* Skip ±15s / speed — soft pill controls beneath the waveform. */}
      <View style={st.skipRow}>
        <Pressable onPress={() => skipBy(-15)} hitSlop={8}
          style={({ pressed }) => [st.skipBtn, pressed && { opacity: 0.7 }]}
          accessibilityLabel="Back 15 seconds">
          <Icon name="gobackward.15" tintColor={C.text} size={19} />
        </Pressable>

        <Pressable onPress={cycleSpeed} hitSlop={8}
          style={({ pressed }) => [st.speedBtn, pressed && { opacity: 0.7 }]}
          accessibilityLabel={`Playback speed ${speed}x. Tap to change.`}>
          <Text style={{
            ...TABULAR, fontSize: 13, color: speed === 1 ? C.textDim : C.primary,
          }}>
            {speed}×
          </Text>
        </Pressable>

        <Pressable onPress={() => skipBy(15)} hitSlop={8}
          style={({ pressed }) => [st.skipBtn, pressed && { opacity: 0.7 }]}
          accessibilityLabel="Forward 15 seconds">
          <Icon name="goforward.15" tintColor={C.text} size={19} />
        </Pressable>
      </View>
    </View>
  );
}
