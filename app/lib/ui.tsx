// lib/ui.tsx — MinuteX shared UI kit, "Workspace" edition.
//
// Every export from the previous kit is preserved with the same props, so no
// screen needs editing to adopt this file. What changed is the treatment:
//
//   • Cards are white, rounded (16px), softly shadowed — never hairline paper.
//   • Blue is the single primary accent for every CTA and active state.
//   • Radii are generous. Buttons and chips are pill-shaped where it reads
//     as an action; cards use 16px.
//   • StatusPill is a small tinted pill (colour + background), not bare text.
//   • SegmentedTabs is an animated two-tab underline, matching the reference.
//   • <Gradient> draws the real brand gradient (blue -> violet) again.
import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator, Animated, Easing, KeyboardAvoidingView, Platform,
  Pressable, StyleSheet, Switch, Text, TextInput, TextInputProps, View,
  ViewStyle, TextStyle,
} from "react-native";
import { LinearGradient } from "expo-linear-gradient";
import { Image } from "expo-image";
import { Icon } from "./icons";
import { avatarColorFor, initialsOf } from "./task-model";
import { CAPS, ELEV, FONT, R, S, TABULAR, useTheme, ColorScale } from "./theme";

// ---- Gradient / RawGradient -------------------------------------------------
export function Gradient({ style, children }: { style?: ViewStyle; children?: React.ReactNode }) {
  const { C } = useTheme();
  return (
    <LinearGradient colors={C.gradient} start={{ x: 0, y: 0 }} end={{ x: 1, y: 1 }} style={style}>
      {children}
    </LinearGradient>
  );
}

export function RawGradient({
  colors, start, end, style, children,
}: {
  colors: readonly [string, string, ...string[]];
  start?: { x: number; y: number };
  end?: { x: number; y: number };
  style?: ViewStyle;
  children?: React.ReactNode;
}) {
  return (
    <LinearGradient
      colors={colors as any}
      start={start ?? { x: 0, y: 0 }}
      end={end ?? { x: 1, y: 1 }}
      style={style}
    >
      {children}
    </LinearGradient>
  );
}

// ---- PressableScale --------------------------------------------------------
export function PressableScale({
  onPress, children, style, disabled, accessibilityLabel,
}: {
  onPress?: () => void; children: React.ReactNode; style?: ViewStyle;
  disabled?: boolean; accessibilityLabel?: string;
}) {
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityLabel={accessibilityLabel}
      style={({ pressed }) => [style, pressed && { opacity: 0.7 }]}
    >
      {children}
    </Pressable>
  );
}

// ---- Card: rounded, shadowed surface ---------------------------------------
export function Card({
  children, style, onPress,
}: { children: React.ReactNode; style?: ViewStyle; onPress?: () => void }) {
  const { C } = useTheme();
  const card = useMemo(() => StyleSheet.create({
    card: {
      backgroundColor: C.surface,
      borderRadius: R.card,
      padding: S.lg,
      borderWidth: 1,
      borderColor: C.border,
      shadowColor: C.shadow,
      ...ELEV.sm,
    },
  }), [C]);
  const body = <View style={[card.card, style]}>{children}</View>;
  if (!onPress) return body;
  return <PressableScale onPress={onPress}>{body}</PressableScale>;
}

// ---- RuledItem --------------------------------------------------------------
// Kept for compatibility: a hairline-separated row inside a Card or list.
export function RuledItem({
  children, onPress, last, style,
}: { children: React.ReactNode; onPress?: () => void; last?: boolean; style?: ViewStyle }) {
  const { C } = useTheme();
  const body = (
    <View style={[{
      paddingBottom: S.md,
      marginBottom: S.md,
      borderBottomWidth: last ? 0 : 1,
      borderBottomColor: C.border,
    }, style]}>
      {children}
    </View>
  );
  if (!onPress) return body;
  return <PressableScale onPress={onPress}>{body}</PressableScale>;
}

// ---- Masthead ---------------------------------------------------------------
export function Masthead({
  kicker, title, right,
}: { kicker?: string; title: string; right?: React.ReactNode }) {
  const { T } = useTheme();
  return (
    <View style={{
      flexDirection: "row", alignItems: "flex-end", justifyContent: "space-between",
      paddingBottom: S.md,
    }}>
      <View style={{ flex: 1 }}>
        {kicker ? <Text style={T.labelDim}>{kicker}</Text> : null}
        <Text style={[T.display, { marginTop: 4 }]}>{title}</Text>
      </View>
      {right}
    </View>
  );
}

// ---- SectionRule: section heading, no rule (cards carry their own edge) ----
export function SectionRule({
  children, right, style,
}: { children: React.ReactNode; right?: React.ReactNode; style?: ViewStyle }) {
  const { T } = useTheme();
  return (
    <View style={[{
      flexDirection: "row", alignItems: "center", justifyContent: "space-between",
      marginTop: S.xl, marginBottom: S.md,
    }, style]}>
      <Text style={[T.h3, { textTransform: "none", letterSpacing: 0 }]}>{children}</Text>
      {right}
    </View>
  );
}

// ---- Button ------------------------------------------------------------------
export function Button({
  label, onPress, variant = "primary", loading, disabled, style, icon,
}: {
  label: string;
  onPress?: () => void;
  variant?: "primary" | "secondary" | "danger" | "ghost";
  loading?: boolean;
  disabled?: boolean;
  style?: ViewStyle;
  icon?: React.ReactNode;
}) {
  const { C } = useTheme();
  const isOff = disabled || loading;

  const bg =
    variant === "primary" ? C.primary :
    variant === "danger" ? C.danger :
    variant === "secondary" ? C.surface2 : "transparent";
  const txtColor =
    variant === "primary" || variant === "danger" ? C.textOnPrimary :
    variant === "ghost" ? C.primary : C.text;
  const border =
    variant === "secondary" ? { borderWidth: 1, borderColor: C.border } : null;

  const content = loading ? (
    <ActivityIndicator color={txtColor} />
  ) : (
    <View style={btn.inner}>
      {icon}
      <Text style={[btn.txt, { color: txtColor }]}>{label}</Text>
    </View>
  );

  return (
    <Pressable
      onPress={onPress}
      disabled={isOff}
      style={({ pressed }) => [
        btn.btn,
        { backgroundColor: bg },
        border,
        variant === "ghost" && { paddingVertical: S.sm },
        { opacity: isOff ? 0.45 : pressed ? 0.85 : 1 },
        style,
      ]}
    >
      {content}
    </Pressable>
  );
}
const btn = StyleSheet.create({
  btn: { borderRadius: R.pill, paddingVertical: 15, paddingHorizontal: S.lg, alignItems: "center", justifyContent: "center" },
  inner: { flexDirection: "row", alignItems: "center", justifyContent: "center", gap: S.sm },
  txt: { fontFamily: FONT.bold, fontSize: 14.5 },
});

// ---- StatusPill --------------------------------------------------------------
// A small tinted pill: background = color at low opacity feel via soft token,
// text = the color itself. Callers pass a solid color; we render it on a
// matching soft chip so it always reads as a pill, not bare text.
export function StatusPill({ label, color }: { label: string; color: string }) {
  return (
    <View style={{
      alignSelf: "flex-start", borderRadius: R.pill,
      paddingHorizontal: 9, paddingVertical: 3.5,
      backgroundColor: withAlpha(color, 0.12),
    }}>
      <Text style={{ fontFamily: FONT.bold, fontSize: 10.5, letterSpacing: 0.4, color }}>
        {label}
      </Text>
    </View>
  );
}

function withAlpha(hex: string, alpha: number): string {
  if (hex.startsWith("rgba") || hex.startsWith("rgb")) return hex;
  const h = hex.replace("#", "");
  const bigint = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
  const r = (bigint >> 16) & 255, g = (bigint >> 8) & 255, b = bigint & 255;
  return `rgba(${r},${g},${b},${alpha})`;
}

// ---- Row: label + value line ------------------------------------------------
export function Row({ label, value, numeric }: { label: string; value: React.ReactNode; numeric?: boolean }) {
  const { C, T } = useTheme();
  return (
    <View style={{
      flexDirection: "row", justifyContent: "space-between", alignItems: "center",
      paddingVertical: 13, borderBottomWidth: 1, borderBottomColor: C.border,
    }}>
      <Text style={T.bodyDim}>{label}</Text>
      {typeof value === "string" ? (
        <Text style={numeric
          ? { ...TABULAR, fontSize: 12.5, color: C.text }
          : { fontFamily: FONT.semibold, fontSize: 13.5, color: C.text }}>{value}</Text>
      ) : value}
    </View>
  );
}

// ---- Screen -------------------------------------------------------------------
export function Screen({ children, style }: { children: React.ReactNode; style?: ViewStyle }) {
  const { C } = useTheme();
  return <View style={[{ flex: 1, backgroundColor: C.bg, paddingHorizontal: 20, paddingTop: 60 }, style]}>{children}</View>;
}

// ---- SectionTitle (kept for compatibility) -----------------------------------
export function SectionTitle({ children, style }: { children: React.ReactNode; style?: TextStyle }) {
  return <SectionRule style={style as ViewStyle}>{children}</SectionRule>;
}

// ---- EmptyState ---------------------------------------------------------------
export function EmptyState({
  title, subtitle, action, icon,
}: { title: string; subtitle?: string; action?: React.ReactNode; icon?: string }) {
  const { C, T } = useTheme();
  return (
    <View style={{
      backgroundColor: C.surface, borderRadius: R.card, borderWidth: 1, borderColor: C.border,
      padding: 28, marginTop: S.xl, alignItems: "center", shadowColor: C.shadow, ...ELEV.sm,
    }}>
      {icon ? (
        <View style={{
          width: 52, height: 52, borderRadius: R.lg, backgroundColor: C.primarySoft,
          alignItems: "center", justifyContent: "center", marginBottom: S.md,
        }}>
          <Icon name={icon as any} tintColor={C.primary} size={24} />
        </View>
      ) : null}
      <Text style={[T.headlineSm, { textAlign: "center" }]}>{title}</Text>
      {subtitle ? (
        <Text style={[T.bodyDim, { textAlign: "center", marginTop: 8 }]}>{subtitle}</Text>
      ) : null}
      {action ? <View style={{ marginTop: S.lg, alignSelf: "stretch" }}>{action}</View> : null}
    </View>
  );
}

// ---- Loading -------------------------------------------------------------------
export function Loading({ label }: { label?: string }) {
  const { C, T } = useTheme();
  return (
    <View style={{ flex: 1, alignItems: "center", justifyContent: "center", gap: S.md, backgroundColor: C.bg }}>
      <ActivityIndicator color={C.primary} />
      {label ? <Text style={T.bodyDim}>{label}</Text> : null}
    </View>
  );
}

// ---- ErrorText / SuccessText ---------------------------------------------------
export function ErrorText({ children }: { children: React.ReactNode }) {
  const { C } = useTheme();
  return (
    <View style={{
      backgroundColor: C.dangerSoft, borderRadius: R.md, padding: S.md, marginTop: S.md,
    }}>
      <Text style={{ fontFamily: FONT.medium, fontSize: 13.5, lineHeight: 20, color: C.danger }}>{children}</Text>
    </View>
  );
}

export function SuccessText({ children }: { children: React.ReactNode }) {
  const { C } = useTheme();
  return (
    <View style={{
      backgroundColor: C.successSoft, borderRadius: R.md, padding: S.md, marginTop: S.md,
    }}>
      <Text style={{ fontFamily: FONT.medium, fontSize: 13.5, lineHeight: 20, color: C.success }}>{children}</Text>
    </View>
  );
}

// ---- TextField: boxed, rounded --------------------------------------------------
export function TextField(props: TextInputProps & { style?: ViewStyle }) {
  const { style, ...rest } = props;
  const { C } = useTheme();
  return (
    <TextInput
      style={[{
        backgroundColor: C.surface2, borderRadius: R.md, borderWidth: 1, borderColor: C.border,
        color: C.text, fontFamily: FONT.regular, fontSize: 15.5,
        paddingVertical: 14, paddingHorizontal: 14,
      }, style]}
      placeholderTextColor={C.textFaint}
      {...rest}
    />
  );
}

// ---- Keyboard avoidance ----------------------------------------------------
//
// ONE correct implementation, shared, because getting this right needs three
// things to agree and hand-repeating them across twenty screens is how they
// drift apart:
//
//   1. android.softwareKeyboardLayoutMode should be "resize" (app.json) so a
//      normal screen is resized by the OS. NOTE: Expo's template also writes
//      adjustResize onto MainActivity itself, so this is belt-and-braces rather
//      than the thing that was broken — verified by decoding the built APK's
//      manifest, which showed adjustResize even when app.json said "pan".
//   2. iOS needs an explicit behavior; "padding" is right for a full screen and
//      is what login/claim/pair-device already use.
//   3. Android needs behavior UNDEFINED on a normal SCREEN (the OS already
//      resized the window; adding a behavior double-counts the inset), but
//      "height" inside a MODAL, which is its own window and is never resized.
//      Getting this backwards leaves every sheet's input under the keyboard —
//      see KeyboardAwareSheet.
//
// `KeyboardAware` wraps a whole screen. `KeyboardAwareSheet` wraps a
// bottom-anchored Modal, which is the worse case: a sheet pinned to the bottom
// is exactly where the keyboard appears, so without this the input is not just
// clipped but completely covered.
export function KeyboardAware({
  children, style,
}: { children: React.ReactNode; style?: ViewStyle }) {
  const { C } = useTheme();
  return (
    <KeyboardAvoidingView
      style={[{ flex: 1, backgroundColor: C.bg }, style]}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
    >
      {children}
    </KeyboardAvoidingView>
  );
}

/**
 * Keyboard-aware wrapper for a bottom-sheet Modal.
 *
 * Put this INSIDE <Modal> and around the backdrop.
 *
 * NOTE THE ANDROID BEHAVIOR, which differs from KeyboardAware above and is the
 * whole reason this is a separate component:
 *
 *   A React Native Modal on Android is its OWN WINDOW. It does not inherit
 *   MainActivity's windowSoftInputMode, so the activity being adjustResize buys
 *   a modal nothing — the keyboard simply overlaps it, and a sheet anchored to
 *   the bottom is precisely where the keyboard appears. `behavior={undefined}`
 *   (correct for a normal screen, which DOES get resized by the OS) therefore
 *   does nothing here and leaves the input covered.
 *
 *   "height" makes KeyboardAvoidingView shrink its own frame by the keyboard
 *   height instead of waiting for a window resize that never comes. This is
 *   what the meeting screen's speaker-rename sheet has always used, and it is
 *   the pattern that actually works on a handset.
 *
 * `flex: 1` (not the screen background) because the backdrop it contains
 * supplies its own scrim — giving this a background would paint over the dimmed
 * content behind the sheet.
 */
export function KeyboardAwareSheet({
  children,
}: { children: React.ReactNode }) {
  return (
    <KeyboardAvoidingView
      style={{ flex: 1 }}
      behavior={Platform.OS === "ios" ? "padding" : "height"}
    >
      {children}
    </KeyboardAvoidingView>
  );
}

/**
 * The props every scrollable form should spread onto its ScrollView.
 *
 * Two of these matter as much as the wrapper itself:
 *   keyboardShouldPersistTaps="handled" — without it the first tap on a button
 *     while the keyboard is up only dismisses the keyboard, so the user has to
 *     tap Save twice and the first tap looks broken.
 *   keyboardDismissMode="on-drag" — scrolling away from a field puts the
 *     keyboard away, which is what makes a long form feel navigable.
 */
export const scrollFormProps = {
  keyboardShouldPersistTaps: "handled" as const,
  keyboardDismissMode: "on-drag" as const,
  showsVerticalScrollIndicator: false,
};

// ---- Divider ---------------------------------------------------------------
export function Divider({ style }: { style?: ViewStyle }) {
  const { C } = useTheme();
  return <View style={[{ height: 1, backgroundColor: C.border }, style]} />;
}

// ---- IconCircle: soft tinted bubble -----------------------------------------
export function IconCircle({
  name, size = 36, iconSize, tint, bg, style,
}: { name: string; size?: number; iconSize?: number; tint?: string; bg?: string; style?: ViewStyle }) {
  const { C } = useTheme();
  return (
    <View
      style={[{
        width: size, height: size, borderRadius: size / 2.6,
        backgroundColor: bg ?? C.primarySoft,
        alignItems: "center", justifyContent: "center",
      }, style]}
    >
      <Icon name={name as any} tintColor={tint ?? C.primary} size={iconSize ?? Math.round(size * 0.5)} />
    </View>
  );
}

// ---- Avatar: one person's photo, or their coloured initials ----------------
//
// The SINGLE place a person is drawn as a circle. Four screens previously each
// built this by hand (contacts list, contact detail, the picker's three lists,
// the profile masthead), which is why adding photos would otherwise have meant
// four different fallback behaviours.
//
// `photoUri` is a presigned, EXPIRING URL (`avatar_view_url` from the API) or a
// device-local file:// path for a phone contact not yet imported. Either way it
// can fail to load — the URL may have expired while the list sat on screen, or
// the OS may have revoked the local path — so a load error falls back to
// initials rather than leaving a blank disc. That fallback is the reason this
// holds state at all.
export function Avatar({
  name, photoUri, size = 42, fontSize, style,
}: {
  name: string;
  /** "" / undefined means "no photo" — draw initials. */
  photoUri?: string;
  size?: number;
  /** Defaults to a proportion of `size`; override to match a specific spec. */
  fontSize?: number;
  style?: ViewStyle;
}) {
  // WHICH uri failed, not merely "one did". A new URL deserves a fresh attempt
  // — re-signing produces a different URL for the same image, and a boolean
  // flag would keep the photo hidden for as long as the component stayed
  // mounted after one expired load. Comparing against the current uri resets
  // that for free, with no effect and no cascading render.
  const [failedUri, setFailedUri] = useState<string | undefined>(undefined);

  const showPhoto = !!photoUri && failedUri !== photoUri;
  return (
    <View
      style={[{
        width: size, height: size, borderRadius: size / 2,
        backgroundColor: avatarColorFor(name),
        alignItems: "center", justifyContent: "center",
        overflow: "hidden",
      }, style]}
    >
      {showPhoto ? (
        <Image
          source={{ uri: photoUri }}
          style={{ width: size, height: size }}
          contentFit="cover"
          // The initials stay mounted underneath, so a slow image reveals them
          // first rather than an empty circle — and a failed one needs no
          // separate placeholder.
          transition={150}
          onError={() => setFailedUri(photoUri)}
          accessibilityIgnoresInvertColors
        />
      ) : (
        <Text
          style={{
            fontFamily: FONT.bold,
            fontSize: fontSize ?? Math.round(size * 0.36),
            color: "#fff",
          }}
        >
          {initialsOf(name)}
        </Text>
      )}
    </View>
  );
}

// ---- SegmentedTabs: animated pill underline, matching the reference --------
export function SegmentedTabs({
  tabs, value, onChange, style,
}: {
  tabs: { key: string; label: string; icon?: string }[];
  value: string;
  onChange: (key: string) => void;
  style?: ViewStyle;
}) {
  const { C } = useTheme();
  const activeIndex = Math.max(0, tabs.findIndex((t) => t.key === value));
  const anim = useRef(new Animated.Value(activeIndex)).current;

  useEffect(() => {
    Animated.spring(anim, {
      toValue: activeIndex, useNativeDriver: false, friction: 9, tension: 90,
    }).start();
  }, [activeIndex, anim]);

  const [widths, setWidths] = useState<number[]>(() => tabs.map(() => 0));
  const offsets = useMemo(() => {
    const out: number[] = [];
    let acc = 0;
    for (const w of widths) { out.push(acc); acc += w; }
    return out;
  }, [widths]);

  const left = anim.interpolate({
    inputRange: tabs.map((_, i) => i),
    outputRange: tabs.map((_, i) => offsets[i] ?? 0),
  });
  const width = anim.interpolate({
    inputRange: tabs.map((_, i) => i),
    outputRange: tabs.map((_, i) => widths[i] ?? 0),
  });

  return (
    <View style={[{
      flexDirection: "row", position: "relative",
      borderBottomWidth: 1.5, borderBottomColor: C.border,
    }, style]}>
      <Animated.View style={{
        position: "absolute", left, width, bottom: -1.5, height: 2.5,
        backgroundColor: C.primary, borderRadius: 2,
      }} />
      {tabs.map((t, i) => {
        const active = t.key === value;
        return (
          <Pressable
            key={t.key}
            onPress={() => onChange(t.key)}
            onLayout={(e) => {
              const w = e.nativeEvent.layout.width;
              setWidths((prev) => { const next = prev.slice(); next[i] = w; return next; });
            }}
            accessibilityRole="tab"
            accessibilityState={{ selected: active }}
            style={{ paddingBottom: 11, paddingHorizontal: S.lg }}
          >
            <Text style={{
              fontFamily: active ? FONT.bold : FONT.semibold, fontSize: 14.5,
              color: active ? C.primary : C.textFaint,
            }}>{t.label}</Text>
          </Pressable>
        );
      })}
    </View>
  );
}

// ---- Skeleton ----------------------------------------------------------------
export function Skeleton({ style }: { style?: ViewStyle }) {
  const { C } = useTheme();
  const glow = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(glow, { toValue: 1, duration: 700, easing: Easing.inOut(Easing.ease), useNativeDriver: true }),
        Animated.timing(glow, { toValue: 0, duration: 700, easing: Easing.inOut(Easing.ease), useNativeDriver: true }),
      ])
    );
    loop.start();
    return () => loop.stop();
  }, [glow]);
  const opacity = glow.interpolate({ inputRange: [0, 1], outputRange: [0.5, 1] });
  return <Animated.View style={[{ height: 9, borderRadius: R.sm, backgroundColor: C.surface2, opacity }, style]} />;
}

// ---- SkeletonCard --------------------------------------------------------------
export function SkeletonCard({ style }: { style?: ViewStyle }) {
  const { C } = useTheme();
  return (
    <View style={[{
      backgroundColor: C.surface, borderRadius: R.card, borderWidth: 1, borderColor: C.border,
      padding: S.lg, marginBottom: S.md,
    }, style]}>
      <Skeleton style={{ width: 88 }} />
      <Skeleton style={{ width: "100%", height: 20, marginTop: 10 }} />
      <Skeleton style={{ width: "64%", height: 20, marginTop: 6 }} />
      <Skeleton style={{ width: "100%", marginTop: 12 }} />
      <Skeleton style={{ width: "82%", marginTop: 6 }} />
    </View>
  );
}

// ---- SearchBar: rounded pill search field --------------------------------------
export function SearchBar({
  value, onChangeText, placeholder = "Search", style,
}: { value: string; onChangeText: (t: string) => void; placeholder?: string; style?: ViewStyle }) {
  const { C } = useTheme();
  return (
    <View style={[{
      flexDirection: "row", alignItems: "center", gap: S.sm,
      backgroundColor: C.surface2, borderRadius: R.pill,
      paddingHorizontal: S.md, paddingVertical: 11,
    }, style]}>
      <Icon name="magnifyingglass" tintColor={C.textFaint} size={18} />
      <TextInput
        style={{ flex: 1, fontFamily: FONT.regular, fontSize: 15, color: C.text, paddingVertical: 0 }}
        placeholder={placeholder}
        placeholderTextColor={C.textFaint}
        value={value}
        onChangeText={onChangeText}
        autoCapitalize="none"
        autoCorrect={false}
        returnKeyType="search"
      />
      {value ? (
        <Pressable onPress={() => onChangeText("")} hitSlop={8} accessibilityLabel="Clear search">
          <Icon name="xmark.circle.fill" tintColor={C.textFaint} size={18} />
        </Pressable>
      ) : null}
    </View>
  );
}

// ---- Chip: pill ------------------------------------------------------------
export function Chip({
  label, active, onPress, icon, style,
}: { label: string; active?: boolean; onPress?: () => void; icon?: string; style?: ViewStyle }) {
  const { C } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [{
        flexDirection: "row", alignItems: "center", gap: 5,
        borderRadius: R.pill, borderWidth: 1,
        borderColor: active ? C.primary : C.border,
        backgroundColor: active ? C.primarySoft : C.surface,
        paddingHorizontal: 12, paddingVertical: 7,
      }, pressed && { opacity: 0.75 }, style]}
    >
      {icon ? <Icon name={icon as any} tintColor={active ? C.primary : C.textDim} size={13} /> : null}
      <Text style={{
        fontFamily: FONT.semibold, fontSize: 12.5,
        color: active ? C.primary : C.textDim,
      }}>{label}</Text>
    </Pressable>
  );
}

// ---- SoonBadge + ComingSoonRow ---------------------------------------------
export function SoonBadge() {
  const { C } = useTheme();
  return (
    <View style={{ backgroundColor: C.surface2, borderRadius: R.pill, paddingHorizontal: 8, paddingVertical: 3 }}>
      <Text style={{ ...CAPS, fontSize: 9.5, letterSpacing: 0.6, color: C.textFaint }}>Soon</Text>
    </View>
  );
}

export function ComingSoonRow({
  icon, label, sub,
}: { icon: string; label: string; sub?: string }) {
  const { C, T } = useTheme();
  return (
    <View style={{
      flexDirection: "row", alignItems: "center", gap: S.md,
      paddingVertical: 14, borderBottomWidth: 1, borderBottomColor: C.border, opacity: 0.55,
    }}>
      <IconCircle name={icon} bg={C.surface2} tint={C.textDim} size={34} />
      <View style={{ flex: 1 }}>
        <Text style={{ fontFamily: FONT.medium, fontSize: 13.5, color: C.text }}>{label}</Text>
        {sub ? <Text style={[T.caption, { marginTop: 2 }]}>{sub}</Text> : null}
      </View>
      <SoonBadge />
    </View>
  );
}

// ---- ListRow -----------------------------------------------------------------
export function ListRow({
  icon, label, sub, right, onPress, tint, destructive,
}: {
  icon: string; label: string; sub?: string; right?: React.ReactNode;
  onPress?: () => void; tint?: string; destructive?: boolean;
}) {
  const { C, T } = useTheme();
  const color = destructive ? C.danger : C.text;
  return (
    <Pressable
      onPress={onPress}
      disabled={!onPress}
      style={({ pressed }) => [{
        flexDirection: "row", alignItems: "center", gap: S.md,
        paddingVertical: 14, borderBottomWidth: 1, borderBottomColor: C.border,
      }, pressed && { opacity: 0.6 }]}
    >
      <IconCircle name={icon} tint={destructive ? C.danger : (tint ?? C.primary)} bg={destructive ? C.dangerSoft : C.primarySoft} size={34} />
      <View style={{ flex: 1 }}>
        <Text style={{ fontFamily: FONT.semibold, fontSize: 14.5, color }}>{label}</Text>
        {sub ? <Text style={[T.caption, { marginTop: 3 }]}>{sub}</Text> : null}
      </View>
      {right ?? (onPress ? <Icon name="chevron.right" tintColor={C.textFaint} size={16} /> : null)}
    </Pressable>
  );
}

// ---- SwitchRow -------------------------------------------------------------
export function SwitchRow({
  label, sub, value, onValueChange, disabled,
}: { label: string; sub?: string; value: boolean; onValueChange: (v: boolean) => void; disabled?: boolean }) {
  const { C, T } = useTheme();
  return (
    <View style={{
      flexDirection: "row", alignItems: "center", justifyContent: "space-between",
      paddingVertical: 13, gap: S.md, borderBottomWidth: 1, borderBottomColor: C.border,
      opacity: disabled ? 0.45 : 1,
    }}>
      <View style={{ flex: 1 }}>
        <Text style={{ fontFamily: FONT.medium, fontSize: 13.5, color: C.text }}>{label}</Text>
        {sub ? <Text style={[T.caption, { marginTop: 2 }]}>{sub}</Text> : null}
      </View>
      <Switch
        value={value}
        onValueChange={onValueChange}
        disabled={disabled}
        trackColor={{ true: C.primary, false: C.border }}
        thumbColor="#FFFFFF"
      />
    </View>
  );
}

// ---- StepDots ----------------------------------------------------------------
export function StepDots({ total, index, style }: { total: number; index: number; style?: ViewStyle }) {
  const { C } = useTheme();
  return (
    <View style={[{ flexDirection: "row", gap: 6, alignItems: "center" }, style]}>
      {Array.from({ length: total }, (_, i) => (
        <View key={i} style={{
          width: i <= index ? 20 : 6, height: 6, borderRadius: 3,
          backgroundColor: i <= index ? C.primary : C.border,
        }} />
      ))}
    </View>
  );
}

// ---- Toast ---------------------------------------------------------------------
export function Toast({ visible, label, icon = "checkmark" }: {
  visible: boolean; label: string; icon?: string;
}) {
  const { C } = useTheme();
  const anim = useRef(new Animated.Value(0)).current;
  const [mounted, setMounted] = useState(visible);

  useEffect(() => {
    if (visible) {
      setMounted(true);
      Animated.timing(anim, { toValue: 1, duration: 180, easing: Easing.out(Easing.ease), useNativeDriver: true }).start();
    } else {
      Animated.timing(anim, { toValue: 0, duration: 150, useNativeDriver: true }).start(
        ({ finished }) => finished && setMounted(false)
      );
    }
  }, [visible, anim]);

  if (!mounted) return null;
  return (
    <Animated.View
      pointerEvents="none"
      style={{
        position: "absolute", bottom: 40, left: 26, right: 26,
        flexDirection: "row", alignItems: "center", gap: S.sm,
        backgroundColor: C.text, borderRadius: R.pill,
        paddingHorizontal: S.lg, paddingVertical: 13,
        opacity: anim, shadowColor: C.shadow, ...ELEV.md,
        transform: [{ translateY: anim.interpolate({ inputRange: [0, 1], outputRange: [10, 0] }) }],
      }}
    >
      <Icon name={icon as any} tintColor={C.bg} size={15} />
      <Text style={{ fontFamily: FONT.semibold, fontSize: 13, color: C.bg }}>{label}</Text>
    </Animated.View>
  );
}

// ---- GoogleButton ----------------------------------------------------------
export function GoogleButton({ label, onPress, disabled }: {
  label: string; onPress?: () => void; disabled?: boolean;
}) {
  const { C } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      style={({ pressed }) => [{
        flexDirection: "row", alignItems: "center", justifyContent: "center", gap: S.sm,
        borderWidth: 1, borderColor: C.border, backgroundColor: C.surface,
        borderRadius: R.pill, paddingVertical: 14,
        opacity: disabled ? 0.4 : pressed ? 0.8 : 1,
      }]}
    >
      <Text style={{ fontFamily: FONT.bold, fontSize: 15, color: "#4285F4" }}>G</Text>
      <Text style={{ fontFamily: FONT.bold, fontSize: 14, color: C.text }}>{label}</Text>
    </Pressable>
  );
}

export type { ColorScale };
