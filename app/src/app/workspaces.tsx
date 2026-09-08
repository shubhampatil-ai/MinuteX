// src/app/workspaces.tsx — the workspace switcher.
//
// One list, and it answers one question: which workspace am I working in?
// Reached from the You tab, so it uses the existing screen/card/list shapes
// rather than introducing a new navigation surface.
//
// The current workspace is marked with a check and cannot be "selected"
// again. Every other row switches and returns, because a switcher that stays
// open after switching leaves the user unsure whether it took effect.
//
// WHAT THE ROLE LABEL IS FOR: telling the user what they can expect to be
// able to do. It is NOT a permission — every action it hints at is enforced
// server-side, and the app re-reads membership on every request.
import { useCallback, useMemo, useState } from "react";
import { ScrollView, StyleSheet, Text, View } from "react-native";
import { Stack, useRouter } from "expo-router";
import { Icon } from "../../lib/icons";
import { S, R, ELEV, FONT, useTheme, ColorScale } from "../../lib/theme";
import {
  Card, EmptyState, ErrorText, IconCircle, ListRow, Loading, SectionRule,
} from "../../lib/ui";
import {
  useWorkspace, workspaceIcon, workspaceLabel, roleLabel,
} from "../../lib/workspace-context";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    screen: { flex: 1, backgroundColor: C.bg },
    body: { padding: S.lg, paddingBottom: S.xxl },
    intro: {
      fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20,
      color: C.textDim, marginBottom: S.lg,
    },
    row: {
      flexDirection: "row", alignItems: "center", gap: S.md,
      paddingVertical: 14,
      borderBottomWidth: 1, borderBottomColor: C.border,
    },
    name: { fontFamily: FONT.semibold, fontSize: 15, color: C.text },
    meta: {
      fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint,
      marginTop: 3,
    },
    current: {
      fontFamily: FONT.semibold, fontSize: 10.5, letterSpacing: 0.6,
      color: C.primary, textTransform: "uppercase",
    },
    card: { marginBottom: S.lg },
  });
}

export default function WorkspacesScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { workspaces, activeId, loading, error, switchTo } = useWorkspace();
  const [busy, setBusy] = useState("");

  const pick = useCallback(async (id: string) => {
    if (id === activeId) return;
    setBusy(id);
    try {
      await switchTo(id);
      router.back();
    } finally {
      setBusy("");
    }
  }, [activeId, switchTo, router]);

  const personal = workspaces.filter((w) => w.is_personal);
  const orgs = workspaces.filter((w) => !w.is_personal);

  return (
    <View style={st.screen}>
      <Stack.Screen options={{ title: "Workspaces" }} />
      <ScrollView contentContainerStyle={st.body}>
        <Text style={st.intro}>
          Meetings, tasks and contacts belong to the workspace they were
          created in. Switching changes what you see and where new recordings
          are filed.
        </Text>

        {loading && !workspaces.length ? <Loading label="Loading workspaces" /> : null}
        {error ? <ErrorText>{error}</ErrorText> : null}

        {personal.length ? (
          <Card style={st.card}>
            <SectionRule>Personal</SectionRule>
            {personal.map((w) => (
              <WorkspaceRow
                key={w.workspace_id}
                name={workspaceLabel(w)}
                icon={workspaceIcon(w)}
                meta="Only you"
                current={w.workspace_id === activeId}
                busy={busy === w.workspace_id}
                onPress={() => pick(w.workspace_id)}
                st={st} C={C}
              />
            ))}
          </Card>
        ) : null}

        <Card style={st.card}>
          <SectionRule>Organisations</SectionRule>
          {orgs.length ? orgs.map((w) => (
            <WorkspaceRow
              key={w.workspace_id}
              name={workspaceLabel(w)}
              icon={workspaceIcon(w)}
              meta={roleLabel(w.role || "")}
              current={w.workspace_id === activeId}
              busy={busy === w.workspace_id}
              onPress={() => pick(w.workspace_id)}
              st={st} C={C}
            />
          )) : (
            <EmptyState
              icon="building.2.fill"
              title="No organisations yet"
              subtitle="Create one to share meetings, tasks and contacts with your team, or join one you've been invited to."
            />
          )}
          <ListRow
            icon="plus"
            label="Create organisation"
            sub="You'll be the owner"
            onPress={() => router.push("/organisation/create")}
          />
          <ListRow
            icon="envelope.fill"
            label="Join with an invitation"
            sub="Paste the link your admin sent you"
            onPress={() => router.push("/organisation/join")}
          />
        </Card>
      </ScrollView>
    </View>
  );
}

function WorkspaceRow({
  name, icon, meta, current, busy, onPress, st, C,
}: {
  name: string; icon: string; meta?: string;
  current: boolean; busy: boolean; onPress: () => void;
  st: ReturnType<typeof buildStyles>; C: ColorScale;
}) {
  return (
    <ListRow
      icon={icon}
      label={name}
      sub={meta || undefined}
      // The current workspace is shown as a state, not as a tappable row —
      // re-selecting it would be a no-op the user cannot distinguish from a
      // failed switch.
      onPress={current ? undefined : onPress}
      right={
        current ? <Text style={st.current}>Current</Text>
        : busy ? <Icon name="arrow.clockwise" tintColor={C.textFaint} size={16} />
        : undefined
      }
    />
  );
}
