// src/app/new-recording.tsx — "What are we listening to?" source chooser.
//
// "Workspace" edition. Three ways in, presented as rounded, shadowed list rows
// inside a single Card — matching the app-native card system, not hairline
// rules. The single entry point for creating a recording: phone mic, imported
// audio file, or the MinuteX device. The device option is live only when one
// is paired (backend) or connected (BLE); otherwise it degrades to a Pair
// action — the hardware is an *additional* source, never a prerequisite.
import { useEffect, useMemo, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import { Stack, useRouter, type Href } from "expo-router";
import { Icon } from "../../lib/icons";
import { S, R, CAPS, FONT, ELEV, useTheme, ColorScale } from "../../lib/theme";
import { SoonBadge, IconCircle } from "../../lib/ui";
import { useDevice } from "../../lib/device-context";
import { getDevicesList } from "../../lib/api";
import { useWorkspace, workspaceLabel } from "../../lib/workspace-context";

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    title: { ...T.headline, fontSize: 30, lineHeight: 34, marginTop: 10 },
    sub: { ...T.bodyDim, marginTop: 10 },
    // "Recording in <workspace>" — a quiet tinted strip rather than a card,
    // so it reads as context for what follows and not as a fourth option.
    wsBanner: {
      flexDirection: "row", alignItems: "center", gap: 7,
      marginTop: 18, paddingVertical: 9, paddingHorizontal: 12,
      backgroundColor: C.primarySoft, borderRadius: R.sm,
    },
    wsLabel: { ...T.caption, color: C.textDim },
    wsName: { ...T.caption, color: C.primary, fontWeight: "700", flexShrink: 1 },
    // A single elevated card holding all three source rows.
    card: {
      backgroundColor: C.surface, borderRadius: R.card,
      borderWidth: 1, borderColor: C.border,
      shadowColor: C.shadow, ...ELEV.sm,
      paddingHorizontal: S.lg,
    },
    option: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      paddingVertical: 16, borderTopWidth: 1, borderTopColor: C.border,
    },
    optionFirst: { borderTopWidth: 0 },
    optionTitle: { fontFamily: FONT.semibold, fontSize: 15.5, color: C.text },
    optionSub: { fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 17, color: C.textFaint, marginTop: 3 },
    liveDot: { width: 6, height: 6, borderRadius: 3, backgroundColor: C.success },
    disabled: { opacity: 0.55 },
    // "Worth knowing" — a soft tinted aside.
    aside: {
      backgroundColor: C.primarySoft, borderRadius: R.card, padding: 16, marginTop: 22,
    },
    asideLabel: { ...CAPS, fontSize: 10, color: C.primaryStrong },
    asideBody: { ...T.bodyDim, marginTop: 8, color: C.text },
  });
}

function Option({ n, icon, title, subtitle, onPress, disabled, soon, live, first, st, C }: {
  n: number; icon: string; title: string; subtitle: string;
  onPress?: () => void; disabled?: boolean; soon?: boolean; live?: boolean; first?: boolean;
  st: ReturnType<typeof buildStyles>; C: ColorScale;
}) {
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      style={({ pressed }) => [
        st.option, first && st.optionFirst, disabled && st.disabled, pressed && { opacity: 0.62 },
      ]}
      accessibilityLabel={title}
    >
      <IconCircle name={icon} tint={C.primary} bg={C.primarySoft} size={40} />
      <View style={{ flex: 1 }}>
        <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
          <Text style={st.optionTitle}>{title}</Text>
          {live ? <View style={st.liveDot} /> : null}
          {soon ? <SoonBadge /> : null}
        </View>
        <Text style={st.optionSub}>{subtitle}</Text>
      </View>
      <Icon name="chevron.right" tintColor={C.textFaint} size={18} />
    </Pressable>
  );
}

export default function NewRecordingScreen() {
  const router = useRouter();
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const { device, status, connState } = useDevice();

  // "Paired" is a backend fact (the account owns a device), independent of
  // whether it happens to be BLE-connected right now. null = still loading;
  // on error assume unpaired — the option degrades to Pair Device, which is
  // the right call to action anyway.
  const [paired, setPaired] = useState<boolean | null>(null);
  useEffect(() => {
    let cancelled = false;
    getDevicesList()
      .then((ids) => { if (!cancelled) setPaired(ids.length > 0); })
      .catch(() => { if (!cancelled) setPaired(false); });
    return () => { cancelled = true; };
  }, []);

  // The workspace this recording will belong to. Read from the SHARED
  // provider rather than fetched here, so this banner cannot disagree with
  // the switcher the user just used — a per-screen fetch is exactly how they
  // drift apart.
  //
  // Display only: the backend re-resolves the workspace from the membership
  // behind the request and is the sole authority on what the row is stamped
  // with. A tampered value produces a 404, not a misfiled meeting.
  const { active: workspace } = useWorkspace();

  const connected = connState === "connected" && !!device;
  const deviceAvailable = paired === true || connected;

  const go = (href: Href) => {
    // Swap the chooser out for the destination so "back" from the recorder
    // returns to where the user actually was, not to this sheet.
    //
    router.replace(href);
  };

  return (
    <View style={[st.container, { paddingTop: S.lg }]}>
      <Stack.Screen options={{ title: "New recording" }} />

      <Text style={st.title}>What are we{"\n"}listening to?</Text>
      <Text style={st.sub}>However it's captured, you get the same brief at the end.</Text>

      {/* WHERE this recording will land. Shown before any source is chosen,
          because a recording must never be started in an ambiguous context —
          filing a client meeting into a personal workspace by accident is not
          something the user can easily undo. Personal renders too, not just
          organisations: "no banner" would be indistinguishable from "still
          loading" and would train people to ignore it. */}
      {workspace ? (
        <View style={st.wsBanner}>
          <Icon
            name={workspace.is_personal ? "person.fill" : "building.2.fill"}
            tintColor={C.primary}
            size={15}
          />
          <Text style={st.wsLabel}>Recording in</Text>
          <Text style={st.wsName} numberOfLines={1}>
            {workspaceLabel(workspace)}
          </Text>
        </View>
      ) : null}

      <View style={[st.card, { marginTop: 22 }]}>
        <Option
          n={1}
          icon="mic.fill"
          title="This phone"
          subtitle="Best on a table, in a quiet room"
          onPress={() => go("/record-phone")}
          first
          st={st} C={C}
        />
        <Option
          n={2}
          icon="doc.text.fill"
          title="A file"
          subtitle="wav, mp3, m4a, flac — up to 2 GB"
          onPress={() => go("/upload")}
          st={st} C={C}
        />
        {deviceAvailable ? (
          <Option
            n={3}
            icon="dot.radiowaves.left.and.right"
            title="Your device"
            subtitle={
              connected && status
                ? `Connected · ${status.batteryLevel ?? "--"}% · handles rooms and corridors`
                : connected ? "Connected · handles rooms and corridors"
                : "Paired — connect it to start"
            }
            live={connected}
            onPress={() => go("/record")}
            st={st} C={C}
          />
        ) : (
          <Option
            n={3}
            icon="dot.radiowaves.left.and.right"
            title="Your device"
            subtitle={paired === null ? "Checking…" : "Not paired yet"}
            soon={paired !== null}
            disabled={paired === null}
            onPress={() => go("/pair")}
            st={st} C={C}
          />
        )}
      </View>

      <View style={st.aside}>
        <Text style={st.asideLabel}>Worth knowing</Text>
        <Text style={st.asideBody}>
          Tell the room you're recording. It's the law in some places and good
          manners everywhere.
        </Text>
      </View>
    </View>
  );
}
