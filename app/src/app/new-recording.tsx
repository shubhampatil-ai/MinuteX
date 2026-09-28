// src/app/new-recording.tsx — "What are we listening to?" source chooser.
//
// The single entry point for creating a recording: phone mic, imported audio
// file, or the MinuteX device. Three rows in one grouped surface (lib/kit.tsx).
// The rows keep their icons — here, unlike a settings list, the glyph is what
// tells a phone from a file from a device at a glance. The device option is
// live only when one is paired (backend) or connected (BLE); otherwise it
// degrades to a Pair action — the hardware is an *additional* source, never a
// prerequisite.
import { useEffect, useMemo, useState } from "react";
import { StyleSheet, Text, View } from "react-native";
import { Stack, useRouter, type Href } from "expo-router";
import { Icon } from "../../lib/icons";
import { S, R, FONT, GUTTER, useTheme, ColorScale } from "../../lib/theme";
import { SettingGroup, SettingRow, StatusBadge } from "../../lib/kit";
import { useDevice } from "../../lib/device-context";
import { getDevicesList } from "../../lib/api";
import { useWorkspace, workspaceLabel } from "../../lib/workspace-context";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: GUTTER },
    title: { fontFamily: FONT.bold, fontSize: 28, lineHeight: 34, color: C.text, letterSpacing: -0.4, marginTop: S.sm },
    sub: { fontFamily: FONT.regular, fontSize: 14.5, lineHeight: 21, color: C.textDim, marginTop: S.sm },
    // "Recording in <workspace>" — a quiet strip rather than a card, so it
    // reads as context for what follows and not as a fourth option.
    wsBanner: {
      flexDirection: "row", alignItems: "center", gap: S.sm,
      marginTop: S.lg, paddingVertical: S.sm, paddingHorizontal: S.md,
      backgroundColor: C.primarySoft, borderRadius: R.sm,
    },
    wsLabel: { fontFamily: FONT.regular, fontSize: 13, color: C.textDim },
    wsName: { fontFamily: FONT.semibold, fontSize: 13, color: C.primary, flexShrink: 1 },
    note: { fontFamily: FONT.regular, fontSize: 13, lineHeight: 19, color: C.textDim, marginTop: S.xl, marginHorizontal: S.xs },
  });
}

export default function NewRecordingScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
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
  // the switcher the user just used.
  //
  // Display only: the backend re-resolves the workspace from the membership
  // behind the request and is the sole authority on what the row is stamped
  // with. A tampered value produces a 404, not a misfiled meeting.
  const { active: workspace } = useWorkspace();

  const connected = connState === "connected" && !!device;
  const deviceAvailable = paired === true || connected;

  // Swap the chooser out for the destination so "back" from the recorder
  // returns to where the user actually was, not to this sheet.
  const go = (href: Href) => router.replace(href);

  return (
    <View style={[st.container, { paddingTop: S.lg }]}>
      <Stack.Screen options={{ title: "New recording" }} />

      <Text style={st.title} accessibilityRole="header">What are we listening to?</Text>
      <Text style={st.sub}>However it&apos;s captured, you get the same brief at the end.</Text>

      {/* WHERE this recording will land. Shown before any source is chosen,
          because a recording must never be started in an ambiguous context —
          filing a client meeting into a personal workspace by accident is not
          something the user can easily undo. Personal renders too: "no
          banner" would be indistinguishable from "still loading". */}
      {workspace ? (
        <View style={st.wsBanner} accessible accessibilityLabel={`Recording in ${workspaceLabel(workspace)}`}>
          <Icon
            name={workspace.is_personal ? "person.fill" : "building.2.fill"}
            tintColor={C.primary}
            size={15}
          />
          <Text style={st.wsLabel}>Recording in</Text>
          <Text style={st.wsName} numberOfLines={1}>{workspaceLabel(workspace)}</Text>
        </View>
      ) : null}

      <SettingGroup style={{ marginTop: S.xl }}>
        <SettingRow
          icon="mic.fill"
          title="This phone"
          description="Best on a table, in a quiet room"
          onPress={() => go("/record-phone")}
        />
        <SettingRow
          icon="doc.text.fill"
          title="An audio file"
          description="wav, mp3, m4a, flac — up to 2 GB"
          onPress={() => go("/upload")}
        />
        {deviceAvailable ? (
          <SettingRow
            icon="dot.radiowaves.left.and.right"
            title="Your MinuteX device"
            description={
              connected && status
                ? `Connected · ${status.batteryLevel ?? "--"}% · handles rooms and corridors`
                : connected ? "Connected · handles rooms and corridors"
                  : "Paired — connect it to start"
            }
            trailing={connected ? <StatusBadge label="Connected" tone="success" dot /> : undefined}
            chevron
            onPress={() => go("/record")}
          />
        ) : (
          <SettingRow
            icon="dot.radiowaves.left.and.right"
            title="Your MinuteX device"
            description={paired === null ? "Checking…" : "Not paired yet — tap to pair one"}
            disabled={paired === null}
            onPress={() => go("/pair")}
          />
        )}
      </SettingGroup>

      <Text style={st.note}>
        Tell the room you&apos;re recording. It&apos;s the law in some places and good
        manners everywhere.
      </Text>
    </View>
  );
}
