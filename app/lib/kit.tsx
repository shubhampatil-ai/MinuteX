// lib/kit.tsx — MinuteX composite components (the redesign's layer).
//
// lib/ui.tsx holds the primitives every screen already uses (Button, Avatar,
// SearchBar, TextField, …) and keeps their props stable. This file holds the
// COMPOSITES the redesigned screens are built from. They exist so that a
// settings group, a brief row or a bottom sheet is drawn by exactly one piece
// of code, rather than hand-assembled per screen with slightly different
// paddings, radii and separators each time.
//
// Rules this file enforces by construction:
//
//   * Grouped, not floating. A run of related rows is ONE surface with inset
//     hairlines (SettingGroup), not a stack of separately shadowed cards.
//   * Icons are optional everywhere. A row is legible from its typography; an
//     icon is added only where it aids recognition or carries state.
//   * Every pressable meets the 44pt touch floor and has an accessibility
//     label and role.
//   * Long text wraps or ellipsizes inside its row — every text column is
//     flex:1 + minWidth:0 so a trailing control can never be pushed off.
//
// No text inputs live in this file. The BottomSheet is for actions; a sheet
// that needs an input should use KeyboardAwareSheet from lib/ui.tsx (see the
// keyboard-avoidance test).
import React, { useEffect, useRef, useState } from "react";
import {
  ActivityIndicator, Modal, Platform, Pressable, StyleSheet, Switch, Text, View,
  type StyleProp, type ViewStyle,
} from "react-native";
import Animated, {
  Easing, runOnJS, useAnimatedStyle, useSharedValue, withTiming,
} from "react-native-reanimated";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { Icon, type IconName } from "./icons";
import { MOTION, PressSpring } from "./motion";
import { ELEV, FONT, GUTTER, HIT, R, S, useTheme, type ColorScale } from "./theme";

// ===========================================================================
// AppHeader — the title block for a tab root or a full-screen surface.
//
// Stack screens keep the NATIVE header (platform back gesture, large-title
// behaviour, correct Android elevation); this is for the screens that set
// headerShown:false — Desk, You — where the page itself is the header.
// ===========================================================================
export function AppHeader({
  title, subtitle, onBack, backIcon = "back", right, style, safeTop = true,
}: {
  title: string;
  subtitle?: string;
  /** Renders a leading back/close control when provided. */
  onBack?: () => void;
  backIcon?: "back" | "close";
  /** Contextual actions (IconButtons, an avatar). */
  right?: React.ReactNode;
  style?: StyleProp<ViewStyle>;
  /** Pad for the status bar. Off when a parent already handles the inset. */
  safeTop?: boolean;
}) {
  const { C, T } = useTheme();
  const insets = useSafeAreaInsets();
  return (
    <View
      style={[{
        paddingTop: (safeTop ? insets.top : 0) + S.sm,
        paddingHorizontal: GUTTER,
        paddingBottom: S.md,
      }, style]}
    >
      {onBack ? (
        <IconButton
          icon={backIcon === "close" ? "xmark" : "chevron.left"}
          onPress={onBack}
          accessibilityLabel={backIcon === "close" ? "Close" : "Back"}
          style={{ marginLeft: -S.md, marginBottom: S.xs }}
          tint={C.text}
        />
      ) : null}
      <View style={{ flexDirection: "row", alignItems: "center", gap: S.md }}>
        <View style={{ flex: 1, minWidth: 0 }}>
          <Text style={T.title} numberOfLines={2} accessibilityRole="header">{title}</Text>
          {subtitle ? (
            <Text style={[T.bodySmall, { marginTop: 2 }]} numberOfLines={2}>{subtitle}</Text>
          ) : null}
        </View>
        {right ? (
          <View style={{ flexDirection: "row", alignItems: "center", gap: S.xs, flexShrink: 0 }}>
            {right}
          </View>
        ) : null}
      </View>
    </View>
  );
}

// ===========================================================================
// SectionHeader — "Recent briefs", "Account". Optional trailing action.
// ===========================================================================
export function SectionHeader({
  title, detail, action, style,
}: {
  title: string;
  /** Quiet trailing text — a count, a date. Ignored when `action` is set. */
  detail?: string;
  action?: { label: string; onPress: () => void };
  style?: StyleProp<ViewStyle>;
}) {
  const { C, T } = useTheme();
  return (
    <View style={[st.sectionHeader, style]}>
      <Text style={[T.section, { flexShrink: 1 }]} numberOfLines={1} accessibilityRole="header">
        {title}
      </Text>
      {action ? (
        <Pressable
          onPress={action.onPress}
          hitSlop={{ top: 12, bottom: 12, left: 12, right: 12 }}
          accessibilityRole="button"
          accessibilityLabel={action.label}
          style={({ pressed }) => [{ flexDirection: "row", alignItems: "center", gap: 2 }, pressed && { opacity: 0.6 }]}
        >
          <Text style={{ fontFamily: FONT.semibold, fontSize: 13, color: C.primary }}>{action.label}</Text>
          <Icon name="chevron.right" tintColor={C.primary} size={14} />
        </Pressable>
      ) : detail ? (
        <Text style={[T.meta, { flexShrink: 0, marginLeft: S.md }]} numberOfLines={1}>{detail}</Text>
      ) : null}
    </View>
  );
}

// ===========================================================================
// IconButton — a glyph with a real hit area.
// ===========================================================================
export function IconButton({
  icon, onPress, accessibilityLabel, size = HIT, iconSize = 20, tint,
  variant = "plain", disabled, badge, style,
}: {
  icon: IconName;
  onPress?: () => void;
  /** Required: an icon alone announces nothing to a screen reader. */
  accessibilityLabel: string;
  size?: number;
  iconSize?: number;
  tint?: string;
  /** plain = glyph only; tonal = soft surface disc; filled = primary disc. */
  variant?: "plain" | "tonal" | "filled";
  disabled?: boolean;
  /** A small count or dot ("" renders a dot). */
  badge?: string;
  style?: StyleProp<ViewStyle>;
}) {
  const { C } = useTheme();
  const bg = variant === "filled" ? C.primary : variant === "tonal" ? C.surface2 : "transparent";
  const fg = tint ?? (variant === "filled" ? C.textOnPrimary : C.textDim);
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityLabel={badge ? `${accessibilityLabel}, ${badge}` : accessibilityLabel}
      accessibilityState={{ disabled: !!disabled }}
      style={({ pressed }) => [{
        width: size, height: size, borderRadius: size / 2,
        alignItems: "center", justifyContent: "center",
        backgroundColor: pressed && variant === "plain" ? C.surface2 : bg,
        opacity: disabled ? 0.4 : pressed && variant !== "plain" ? 0.8 : 1,
      }, style]}
    >
      <Icon name={icon} tintColor={fg} size={iconSize} />
      {badge != null ? (
        <View style={[st.badge, { backgroundColor: C.primary, borderColor: C.bg }, badge === "" && st.badgeDot]}>
          {badge ? <Text style={[st.badgeTxt, { color: C.textOnPrimary }]}>{badge}</Text> : null}
        </View>
      ) : null}
    </Pressable>
  );
}

// ===========================================================================
// StatusBadge — one small pill, semantic tone.
// ===========================================================================
export type Tone = "neutral" | "info" | "success" | "warning" | "danger" | "recording";

export function toneColors(tone: Tone, C: ColorScale): { fg: string; bg: string } {
  switch (tone) {
    case "info": return { fg: C.primary, bg: C.primarySoft };
    case "success": return { fg: C.success, bg: C.successSoft };
    case "warning": return { fg: C.warn, bg: C.warnSoft };
    case "danger": return { fg: C.danger, bg: C.dangerSoft };
    case "recording": return { fg: C.recording, bg: C.recordingSoft };
    default: return { fg: C.textDim, bg: C.surface2 };
  }
}

export function StatusBadge({
  label, tone = "neutral", dot, style,
}: { label: string; tone?: Tone; dot?: boolean; style?: StyleProp<ViewStyle> }) {
  const { C } = useTheme();
  const { fg, bg } = toneColors(tone, C);
  return (
    <View
      style={[st.statusBadge, { backgroundColor: bg }, style]}
      accessibilityLabel={`Status: ${label}`}
    >
      {dot ? <View style={{ width: 6, height: 6, borderRadius: 3, backgroundColor: fg }} /> : null}
      <Text style={[st.statusBadgeTxt, { color: fg }]} numberOfLines={1}>{label}</Text>
    </View>
  );
}

// ===========================================================================
// SettingGroup / SettingRow / ToggleRow — grouped native-feeling lists.
// ===========================================================================
type RowSeparator = { __separator?: boolean };

/**
 * A titled run of related rows on ONE surface. Rows get an inset hairline
 * between them automatically; conditional rows (`{x ? <Row/> : null}`) are
 * dropped before separators are assigned, so a hidden row never leaves a
 * doubled line or a gap.
 */
export function SettingGroup({
  title, footer, children, style,
}: {
  title?: string;
  /** Explanatory copy under the group — what a setting does, why one is off. */
  footer?: string;
  children: React.ReactNode;
  style?: StyleProp<ViewStyle>;
}) {
  const { C, T } = useTheme();
  const rows = React.Children.toArray(children).filter(React.isValidElement);
  if (!rows.length) return null;
  return (
    <View style={[{ marginTop: S.xl }, style]}>
      {title ? (
        <Text
          style={[T.meta, st.groupTitle]}
          accessibilityRole="header"
          numberOfLines={1}
        >
          {title}
        </Text>
      ) : null}
      <View style={[st.group, { backgroundColor: C.surface, borderColor: C.border }]}>
        {rows.map((row, i) =>
          React.cloneElement(row as React.ReactElement<RowSeparator>, { __separator: i > 0 })
        )}
      </View>
      {footer ? <Text style={[T.caption, st.groupFooter]}>{footer}</Text> : null}
    </View>
  );
}

function RowShell({
  children, onPress, disabled, separator, accessibilityLabel, accessibilityHint, accessibilityRole,
  checked, groupForA11y = true,
}: {
  children: React.ReactNode;
  onPress?: () => void;
  disabled?: boolean;
  separator?: boolean;
  accessibilityLabel?: string;
  accessibilityHint?: string;
  accessibilityRole?: "button" | "switch" | "text" | "link";
  /** For switch rows: the on/off state a screen reader announces. */
  checked?: boolean;
  /**
   * Merge a static row into ONE accessibility element. Must be false when the
   * row holds its own interactive control (a segmented picker), or iOS would
   * hide that control from VoiceOver inside the merged element.
   */
  groupForA11y?: boolean;
}) {
  const { C } = useTheme();
  const body = (pressed: boolean) => (
    <View style={[st.rowOuter, pressed && { backgroundColor: C.surface2 }]}>
      {/* The hairline is INSET from the leading edge, as on both platforms'
          native grouped lists — it separates rows without boxing each one. */}
      {separator ? <View style={[st.rowSep, { backgroundColor: C.border }]} /> : null}
      <View style={[st.row, disabled && { opacity: 0.45 }]}>{children}</View>
    </View>
  );
  if (!onPress) {
    if (!groupForA11y) return body(false);
    return (
      <View accessible accessibilityLabel={accessibilityLabel} accessibilityRole={accessibilityRole}>
        {body(false)}
      </View>
    );
  }
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole={accessibilityRole ?? "button"}
      accessibilityLabel={accessibilityLabel}
      accessibilityHint={accessibilityHint}
      accessibilityState={checked == null ? { disabled: !!disabled } : { disabled: !!disabled, checked }}
    >
      {({ pressed }) => body(pressed)}
    </Pressable>
  );
}

export function SettingRow({
  title, description, value, trailing, onPress, chevron, disabled, destructive,
  icon, soon, accessibilityHint, __separator,
}: {
  title: string;
  description?: string;
  /** Short trailing text answer ("Free", "Light", "2 connected"). */
  value?: string;
  /** Arbitrary trailing control; replaces `value`. */
  trailing?: React.ReactNode;
  onPress?: () => void;
  /** Defaults to true when the row navigates (onPress, no custom trailing). */
  chevron?: boolean;
  disabled?: boolean;
  destructive?: boolean;
  /** Optional. Most rows should NOT have one. */
  icon?: IconName;
  /** Marks a feature that is planned but not built. Forces the row inert. */
  soon?: boolean;
  accessibilityHint?: string;
} & RowSeparator) {
  const { C, T } = useTheme();
  const inert = soon || disabled;
  const showChevron = !soon && (chevron ?? (!!onPress && !trailing));
  const a11y = [title, value, description, soon ? "Coming soon" : undefined].filter(Boolean).join(", ");
  return (
    <RowShell
      onPress={inert ? undefined : onPress}
      disabled={disabled}
      separator={__separator}
      accessibilityLabel={a11y}
      accessibilityHint={accessibilityHint}
      // A custom trailing control (e.g. a segmented picker) must stay
      // individually reachable, so the row is not merged in that case.
      groupForA11y={!trailing}
    >
      {icon ? (
        <Icon name={icon} tintColor={destructive ? C.danger : C.textDim} size={19} />
      ) : null}
      <View style={{ flex: 1, minWidth: 0 }}>
        <Text
          style={[T.rowTitle, destructive && { color: C.danger }, soon && { color: C.textDim }]}
          numberOfLines={2}
        >
          {title}
        </Text>
        {description ? (
          <Text style={[T.bodySmall, { marginTop: 2 }]} numberOfLines={3}>{description}</Text>
        ) : null}
      </View>
      {soon ? (
        <StatusBadge label="Soon" />
      ) : trailing ? (
        <View style={{ flexShrink: 0, maxWidth: "50%", alignItems: "flex-end" }}>{trailing}</View>
      ) : value ? (
        <Text style={[T.bodySmall, { flexShrink: 1, maxWidth: "45%", textAlign: "right" }]} numberOfLines={1}>
          {value}
        </Text>
      ) : null}
      {showChevron ? <Icon name="chevron.right" tintColor={C.textFaint} size={16} /> : null}
    </RowShell>
  );
}

export function ToggleRow({
  title, description, value, onValueChange, disabled, __separator,
}: {
  title: string;
  description?: string;
  value: boolean;
  onValueChange: (v: boolean) => void;
  disabled?: boolean;
} & RowSeparator) {
  const { C, T } = useTheme();
  return (
    // The whole row toggles, not just the switch: a 51pt switch is a small
    // target on a row that is visibly the full width of the screen.
    <RowShell
      onPress={disabled ? undefined : () => onValueChange(!value)}
      disabled={disabled}
      separator={__separator}
      accessibilityRole="switch"
      accessibilityLabel={[title, description].filter(Boolean).join(", ")}
      checked={value}
    >
      <View style={{ flex: 1, minWidth: 0 }}>
        <Text style={T.rowTitle} numberOfLines={2}>{title}</Text>
        {description ? (
          <Text style={[T.bodySmall, { marginTop: 2 }]} numberOfLines={3}>{description}</Text>
        ) : null}
      </View>
      <Switch
        value={value}
        onValueChange={onValueChange}
        disabled={disabled}
        // iOS draws its own white thumb; Android needs an explicit thumb so
        // the ON state is not a primary thumb on a primary track.
        trackColor={{ true: C.primary, false: C.borderStrong }}
        thumbColor={Platform.OS === "android" ? "#FFFFFF" : undefined}
        ios_backgroundColor={C.borderStrong}
        accessibilityElementsHidden
        importantForAccessibility="no"
      />
    </RowShell>
  );
}

// ===========================================================================
// SegmentedControl — a small set of mutually exclusive filters.
// ===========================================================================
export function SegmentedControl<K extends string>({
  options, value, onChange, style,
}: {
  options: { key: K; label: string }[];
  value: K;
  onChange: (key: K) => void;
  style?: StyleProp<ViewStyle>;
}) {
  const { C } = useTheme();
  const [w, setW] = useState(0);
  const index = Math.max(0, options.findIndex((o) => o.key === value));
  const seg = options.length ? (w - 4) / options.length : 0;
  const x = useSharedValue(0);

  useEffect(() => {
    x.set(withTiming(index * seg, { duration: MOTION.standard, easing: Easing.out(Easing.cubic) }));
  }, [index, seg, x]);

  const thumb = useAnimatedStyle(() => ({ transform: [{ translateX: x.get() }] }));

  return (
    <View
      accessibilityRole="tablist"
      onLayout={(e) => setW(e.nativeEvent.layout.width)}
      style={[st.segTrack, { backgroundColor: C.surface2 }, style]}
    >
      {seg > 0 ? (
        <Animated.View
          pointerEvents="none"
          style={[st.segThumb, { width: seg, backgroundColor: C.text }, thumb]}
        />
      ) : null}
      {options.map((o) => {
        const active = o.key === value;
        return (
          <Pressable
            key={o.key}
            onPress={() => onChange(o.key)}
            accessibilityRole="tab"
            accessibilityLabel={o.label}
            accessibilityState={{ selected: active }}
            style={st.segItem}
          >
            <Text
              style={{
                fontFamily: active ? FONT.semibold : FONT.medium, fontSize: 13.5,
                color: active ? C.bg : C.textDim,
              }}
              numberOfLines={1}
            >
              {o.label}
            </Text>
          </Pressable>
        );
      })}
    </View>
  );
}

// ===========================================================================
// BriefCard — one brief in a list. Only what helps you pick the right one.
// ===========================================================================
export type GroupPosition = "single" | "first" | "middle" | "last";

export function BriefCard({
  title, meta, preview, status, byline, onPress, onLongPress, accessibilityHint,
  position = "single", dimmed, footer,
}: {
  title: string;
  /** "Today · 14:32", "45 min", "Phone" — joined with a middle dot. */
  meta: string[];
  /** A two-line summary. Omitted, not faked, when there isn't one. */
  preview?: string;
  status?: { label: string; tone: Tone };
  /** Who recorded it (organisation lists only). */
  byline?: React.ReactNode;
  onPress?: () => void;
  onLongPress?: () => void;
  accessibilityHint?: string;
  /** Where this row sits in a run of rows — rounds only the outer corners. */
  position?: GroupPosition;
  dimmed?: boolean;
  /** An inline secondary action (e.g. "Move to Trash" on a failed brief). */
  footer?: React.ReactNode;
}) {
  const { C, T } = useTheme();
  const top = position === "single" || position === "first";
  const bottom = position === "single" || position === "last";
  const metaLine = meta.filter(Boolean).join("  ·  ");
  return (
    <Pressable
      onPress={onPress}
      onLongPress={onLongPress}
      delayLongPress={400}
      accessibilityRole="button"
      accessibilityLabel={[title, metaLine, status?.label].filter(Boolean).join(". ")}
      accessibilityHint={accessibilityHint}
      style={({ pressed }) => [
        st.brief,
        {
          backgroundColor: pressed ? C.surface2 : C.surface,
          borderColor: C.border,
          borderTopWidth: top ? 1 : 0,
          borderBottomWidth: bottom ? 1 : 0,
          borderTopLeftRadius: top ? R.card : 0,
          borderTopRightRadius: top ? R.card : 0,
          borderBottomLeftRadius: bottom ? R.card : 0,
          borderBottomRightRadius: bottom ? R.card : 0,
          marginBottom: bottom ? S.md : 0,
        },
        dimmed && { opacity: 0.45 },
      ]}
    >
      {!top ? <View style={[st.briefSep, { backgroundColor: C.border }]} /> : null}
      <View style={{ flexDirection: "row", alignItems: "flex-start", gap: S.md }}>
        {/* minWidth:0 is what lets a long AI title ellipsize instead of
            widening the column and pushing the status badge off the card. */}
        <View style={{ flex: 1, minWidth: 0 }}>
          <Text style={T.rowTitle} numberOfLines={2}>{title}</Text>
          {metaLine ? (
            <Text style={[T.meta, { marginTop: 4 }]} numberOfLines={1}>{metaLine}</Text>
          ) : null}
        </View>
        {status ? <StatusBadge label={status.label} tone={status.tone} style={{ flexShrink: 0, marginTop: 1 }} /> : null}
      </View>
      {byline ? <View style={{ marginTop: S.sm }}>{byline}</View> : null}
      {preview ? (
        <Text style={[T.bodySmall, { marginTop: S.sm }]} numberOfLines={2}>{preview}</Text>
      ) : null}
      {footer ? <View style={{ marginTop: S.md }}>{footer}</View> : null}
    </Pressable>
  );
}

// ===========================================================================
// MeetingCard — compact meeting context ("this came from…").
//
// Smaller than a BriefCard: no preview and no status, because it appears
// INSIDE another object's screen (a day on the calendar, a task's source) where
// the meeting is context rather than the thing being chosen.
// ===========================================================================
export function MeetingCard({
  title, meta, onPress, style,
}: {
  title: string;
  meta: string[];
  onPress?: () => void;
  style?: StyleProp<ViewStyle>;
}) {
  const { C, T } = useTheme();
  const metaLine = meta.filter(Boolean).join("  ·  ");
  const inner = (
    <>
      <View style={{ flex: 1, minWidth: 0 }}>
        <Text style={T.rowTitle} numberOfLines={2}>{title}</Text>
        {metaLine ? <Text style={[T.meta, { marginTop: 3 }]} numberOfLines={1}>{metaLine}</Text> : null}
      </View>
      {onPress ? <Icon name="chevron.right" tintColor={C.textFaint} size={16} /> : null}
    </>
  );
  const box = [st.meeting, { backgroundColor: C.surface, borderColor: C.border }, style];
  if (!onPress) return <View style={box}>{inner}</View>;
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={[title, metaLine].filter(Boolean).join(". ")}
      style={({ pressed }) => [box, pressed && { backgroundColor: C.surface2 }]}
    >
      {inner}
    </Pressable>
  );
}

// ===========================================================================
// BottomSheet — contextual actions over the current screen.
//
// Built on RN <Modal> (transparent) with a Reanimated enter/exit, because the
// app never mounts a GestureHandlerRootView — a drag-to-dismiss sheet would
// mean introducing a gesture stack for one interaction. Dismissal is the
// scrim, the Android back button, or an explicit action.
// ===========================================================================
export function BottomSheet({
  visible, onClose, onDismissed, title, subtitle, children, dark,
}: {
  visible: boolean;
  onClose: () => void;
  /**
   * Fires once the exit animation has finished and the Modal is gone. An
   * action that opens ANOTHER modal (a share sheet, a rename dialog) must
   * wait for this: on iOS, presenting a modal while this one is still
   * dismissing can take the new one down with it.
   */
  onDismissed?: () => void;
  title?: string;
  subtitle?: string;
  children: React.ReactNode;
  /** Draw on the recorder's studio palette instead of the theme. */
  dark?: { bg: string; text: string; textDim: string; handle: string };
}) {
  const { C, T } = useTheme();
  const insets = useSafeAreaInsets();
  // Stay mounted through the exit animation, then let the Modal go. Opening
  // is a render-phase derived update (not an effect) so the sheet mounts in
  // the same commit that asked for it.
  const [mounted, setMounted] = useState(visible);
  if (visible && !mounted) setMounted(true);
  const p = useSharedValue(0);
  const dismissed = useRef(onDismissed);
  useEffect(() => { dismissed.current = onDismissed; }, [onDismissed]);
  // Only a sheet that was actually shown can be "dismissed" — without this the
  // first render (visible=false) would report a dismissal that never happened.
  const shown = useRef(false);

  useEffect(() => {
    const finish = () => {
      setMounted(false);
      dismissed.current?.();
    };
    if (visible) {
      shown.current = true;
      p.set(withTiming(1, { duration: MOTION.standard, easing: Easing.out(Easing.cubic) }));
    } else if (shown.current) {
      shown.current = false;
      p.set(withTiming(0, { duration: MOTION.fast + 60, easing: Easing.in(Easing.quad) }, (done) => {
        if (done) runOnJS(finish)();
      }));
    }
  }, [visible, p]);

  const scrim = useAnimatedStyle(() => ({ opacity: p.get() }));
  const sheet = useAnimatedStyle(() => ({
    transform: [{ translateY: (1 - p.get()) * 320 }],
  }));

  if (!mounted) return null;
  const bg = dark?.bg ?? C.surface;
  return (
    <Modal visible transparent animationType="none" onRequestClose={onClose} statusBarTranslucent>
      <View style={{ flex: 1, justifyContent: "flex-end" }}>
        <Animated.View style={[StyleSheet.absoluteFill, { backgroundColor: C.overlay }, scrim]}>
          <Pressable
            style={{ flex: 1 }}
            onPress={onClose}
            accessibilityRole="button"
            accessibilityLabel="Close"
          />
        </Animated.View>
        <Animated.View
          accessibilityViewIsModal
          style={[st.sheet, { backgroundColor: bg, paddingBottom: Math.max(insets.bottom, S.lg) + S.sm }, sheet]}
        >
          <View style={[st.sheetHandle, { backgroundColor: dark?.handle ?? C.borderStrong }]} />
          {title ? (
            <Text style={[T.section, { fontSize: 17, lineHeight: 22 }, dark && { color: dark.text }]} accessibilityRole="header">
              {title}
            </Text>
          ) : null}
          {subtitle ? (
            <Text style={[T.bodySmall, { marginTop: 4 }, dark && { color: dark.textDim }]}>{subtitle}</Text>
          ) : null}
          <View style={{ marginTop: title || subtitle ? S.md : 0 }}>{children}</View>
        </Animated.View>
      </View>
    </Modal>
  );
}

/** One action inside a BottomSheet. */
export function SheetAction({
  label, description, icon, onPress, destructive, disabled, selected, color, dimColor,
}: {
  label: string;
  description?: string;
  icon?: IconName;
  onPress: () => void;
  destructive?: boolean;
  disabled?: boolean;
  /** Draws a trailing check — for choosing one of several (device, input). */
  selected?: boolean;
  /** Override text colours (studio sheets). */
  color?: string;
  dimColor?: string;
}) {
  const { C, T } = useTheme();
  const fg = destructive ? C.danger : color ?? C.text;
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityLabel={[label, description].filter(Boolean).join(", ")}
      accessibilityState={{ disabled: !!disabled, selected: !!selected }}
      style={({ pressed }) => [st.sheetAction, pressed && { opacity: 0.6 }, disabled && { opacity: 0.4 }]}
    >
      {icon ? <Icon name={icon} tintColor={destructive ? C.danger : dimColor ?? C.textDim} size={20} /> : null}
      <View style={{ flex: 1, minWidth: 0 }}>
        <Text style={[T.rowTitle, { color: fg }]} numberOfLines={2}>{label}</Text>
        {description ? (
          <Text style={[T.bodySmall, { marginTop: 2 }, dimColor ? { color: dimColor } : null]} numberOfLines={2}>
            {description}
          </Text>
        ) : null}
      </View>
      {selected ? <Icon name="checkmark" tintColor={C.primary} size={18} /> : null}
    </Pressable>
  );
}

// ===========================================================================
// ProgressSteps — where a recording is in the pipeline.
//
// Stage, never percentage: the backend reports WHICH step it is on, not how
// far through it, so a bar would be an invented number.
// ===========================================================================
export type StepState = "pending" | "active" | "done" | "failed";
export type ProgressStep = { key: string; label: string; state: StepState; detail?: string };

export function ProgressSteps({ steps, style }: { steps: ProgressStep[]; style?: StyleProp<ViewStyle> }) {
  const { C, T } = useTheme();
  return (
    <View style={style} accessibilityRole="list">
      {steps.map((s, i) => {
        const last = i === steps.length - 1;
        const stateWord =
          s.state === "done" ? "done" : s.state === "active" ? "in progress"
            : s.state === "failed" ? "failed" : "not started";
        return (
          <View
            key={s.key}
            style={{ flexDirection: "row", gap: S.md }}
            accessible
            accessibilityLabel={`${s.label}, ${stateWord}${s.detail ? `. ${s.detail}` : ""}`}
          >
            <View style={{ alignItems: "center", width: 24 }}>
              <StepMarker state={s.state} C={C} />
              {!last ? (
                <View style={{
                  width: 2, flex: 1, minHeight: 18, marginVertical: 4, borderRadius: 1,
                  backgroundColor: s.state === "done" ? C.success : C.border,
                }} />
              ) : null}
            </View>
            <View style={{ flex: 1, minWidth: 0, paddingBottom: last ? 0 : S.lg, paddingTop: 2 }}>
              <Text
                style={[
                  T.rowTitle,
                  s.state === "pending" && { color: C.textFaint },
                  s.state === "failed" && { color: C.danger },
                ]}
              >
                {s.label}
              </Text>
              {s.detail ? <Text style={[T.bodySmall, { marginTop: 2 }]}>{s.detail}</Text> : null}
            </View>
          </View>
        );
      })}
    </View>
  );
}

function StepMarker({ state, C }: { state: StepState; C: ColorScale }) {
  if (state === "done") {
    return (
      <View style={[st.marker, { backgroundColor: C.success }]}>
        <Icon name="checkmark" tintColor="#FFFFFF" size={13} />
      </View>
    );
  }
  if (state === "failed") {
    return (
      <View style={[st.marker, { backgroundColor: C.danger }]}>
        <Icon name="xmark" tintColor="#FFFFFF" size={12} />
      </View>
    );
  }
  if (state === "active") {
    // A spinner, because the one thing the user needs to know about the active
    // step is that it is genuinely moving — and a spinner claims only that.
    return (
      <View style={[st.marker, { borderWidth: 2, borderColor: C.primary, backgroundColor: C.primarySoft }]}>
        <ActivityIndicator size="small" color={C.primary} style={{ transform: [{ scale: 0.7 }] }} />
      </View>
    );
  }
  return <View style={[st.marker, { borderWidth: 2, borderColor: C.border }]} />;
}

// ===========================================================================
// RoundControl — a large circular transport control with a caption.
// Used by the recorders; lives here so both screens draw it identically.
// ===========================================================================
export function RoundControl({
  onPress, accessibilityLabel, children, size = 64, bg, border, caption, captionColor, disabled,
}: {
  onPress?: () => void;
  accessibilityLabel: string;
  children: React.ReactNode;
  size?: number;
  bg: string;
  border?: string;
  caption?: string;
  captionColor: string;
  disabled?: boolean;
}) {
  return (
    <View style={{ alignItems: "center", width: Math.max(size, 76) }}>
      <PressSpring
        onPress={onPress}
        disabled={disabled}
        accessibilityLabel={accessibilityLabel}
        pressedScale={MOTION.pressScale}
        style={{
          width: size, height: size, borderRadius: size / 2,
          backgroundColor: bg,
          borderWidth: border ? 1 : 0, borderColor: border,
          alignItems: "center", justifyContent: "center",
          opacity: disabled ? 0.4 : 1,
        }}
      >
        {children}
      </PressSpring>
      {caption ? (
        <Text style={{ fontFamily: FONT.medium, fontSize: 12.5, color: captionColor, marginTop: S.sm }} numberOfLines={1}>
          {caption}
        </Text>
      ) : null}
    </View>
  );
}

const st = StyleSheet.create({
  sectionHeader: {
    flexDirection: "row", alignItems: "center", justifyContent: "space-between",
    minHeight: 32, marginBottom: S.sm,
  },
  badge: {
    position: "absolute", top: 6, right: 4, minWidth: 16, height: 16, borderRadius: 8,
    paddingHorizontal: 4, alignItems: "center", justifyContent: "center", borderWidth: 1.5,
  },
  badgeDot: { minWidth: 10, width: 10, height: 10, borderRadius: 5, top: 10, right: 10, paddingHorizontal: 0 },
  badgeTxt: { fontFamily: FONT.bold, fontSize: 9, fontVariant: ["tabular-nums"] },
  statusBadge: {
    flexDirection: "row", alignItems: "center", gap: 5, alignSelf: "flex-start",
    borderRadius: R.pill, paddingHorizontal: 8, paddingVertical: 3,
  },
  statusBadgeTxt: { fontFamily: FONT.semibold, fontSize: 11.5, lineHeight: 15 },
  groupTitle: { marginBottom: S.sm, marginLeft: S.xs },
  group: { borderRadius: R.card, borderWidth: StyleSheet.hairlineWidth, overflow: "hidden" },
  groupFooter: { marginTop: S.sm, marginHorizontal: S.xs, lineHeight: 17 },
  rowOuter: { minHeight: HIT + 8 },
  rowSep: { position: "absolute", top: 0, right: 0, left: S.lg, height: StyleSheet.hairlineWidth },
  row: {
    flexDirection: "row", alignItems: "center", gap: S.md,
    paddingHorizontal: S.lg, paddingVertical: S.md, minHeight: HIT + 8,
  },
  segTrack: {
    flexDirection: "row", borderRadius: R.pill, padding: 2, minHeight: 40, position: "relative",
  },
  segThumb: { position: "absolute", top: 2, bottom: 2, left: 2, borderRadius: R.pill },
  segItem: { flex: 1, alignItems: "center", justifyContent: "center", minHeight: 36, paddingHorizontal: S.xs },
  brief: { paddingHorizontal: S.lg, paddingVertical: S.md + 2, borderLeftWidth: 1, borderRightWidth: 1 },
  briefSep: { position: "absolute", top: 0, left: S.lg, right: 0, height: StyleSheet.hairlineWidth },
  meeting: {
    flexDirection: "row", alignItems: "center", gap: S.md,
    borderRadius: R.md, borderWidth: StyleSheet.hairlineWidth, padding: S.md, minHeight: HIT + 8,
  },
  sheet: {
    borderTopLeftRadius: R.xl, borderTopRightRadius: R.xl,
    paddingHorizontal: S.xl, paddingTop: S.md, shadowColor: "#000", ...ELEV.lg,
  },
  sheetHandle: { alignSelf: "center", width: 36, height: 4, borderRadius: 2, marginBottom: S.lg },
  sheetAction: { flexDirection: "row", alignItems: "center", gap: S.md, minHeight: HIT + 8, paddingVertical: S.sm },
  marker: { width: 24, height: 24, borderRadius: 12, alignItems: "center", justifyContent: "center" },
});
