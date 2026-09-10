// src/app/organisation/index.tsx — the Organisation hub.
//
// One screen listing the organisation's sections, using the existing
// Card/ListRow/SectionRule shapes. It is not a new navigation paradigm — it
// is a destination reached from You, like Devices or Integrations.
//
// ROLE GATING HERE IS PRESENTATION ONLY. Hiding Members from a MEMBER saves
// them a pointless tap; it is not what stops them managing members. Every one
// of these destinations calls an API that re-resolves membership and role
// server-side, so a member who navigated here by hand still gets 403/404.
//
// Organisation Integrations (Phase 2D) is a real destination, not a
// placeholder: it routes to the SAME /integrations route Settings ->
// Connected apps links to, but that screen renders a deliberately
// different, organisation-scoped view once the active workspace is an
// organisation (see src/app/integrations/index.tsx's own module comment) —
// only the Organisation Salesforce connection and the current user's Gmail,
// never the rest of the personal catalog. This hub row only needs to get
// the user to that screen, not duplicate its logic. Sections that
// genuinely belong to a later phase (Settings) still use the existing
// Coming Soon treatment, so the shape of the product stays honest — the
// same choice the profile screen already makes.
import { useMemo } from "react";
import { ScrollView, StyleSheet, Text, View } from "react-native";
import { Stack, useRouter } from "expo-router";
import { S, FONT, useTheme, ColorScale } from "../../../lib/theme";
import {
  Card, ComingSoonRow, EmptyState, ListRow, Loading, SectionRule,
} from "../../../lib/ui";
import {
  useWorkspace, roleLabel, workspaceLabel,
} from "../../../lib/workspace-context";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    screen: { flex: 1, backgroundColor: C.bg },
    body: { padding: S.lg, paddingBottom: S.xxl },
    header: { marginBottom: S.lg },
    name: { fontFamily: FONT.bold, fontSize: 22, color: C.text },
    role: {
      fontFamily: FONT.regular, fontSize: 12.5, color: C.textDim,
      marginTop: 4,
    },
    card: { marginBottom: S.lg },
  });
}

export default function OrganisationScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { active, isOrganisation, role, loading, workspaces } = useWorkspace();

  if (loading && !workspaces.length) {
    return (
      <View style={st.screen}>
        <Stack.Screen options={{ title: "Organisation" }} />
        <Loading label="Loading" />
      </View>
    );
  }

  // Reached while working personally — offer the two ways in rather than an
  // error, because "no organisation" is a normal state, not a failure.
  if (!isOrganisation || !active) {
    return (
      <View style={st.screen}>
        <Stack.Screen options={{ title: "Organisation" }} />
        <ScrollView contentContainerStyle={st.body}>
          <EmptyState
            icon="building.2.fill"
            title="You're in your personal workspace"
            subtitle="Create an organisation to share meetings, tasks and contacts with your team, or join one you've been invited to."
          />
          <Card style={{ marginTop: S.lg }}>
            <ListRow
              icon="plus"
              label="Create organisation"
              sub="You'll be the owner"
              onPress={() => router.push("/organisation/create")}
            />
            <ListRow
              icon="envelope.fill"
              label="Join with an invitation"
              onPress={() => router.push("/organisation/join")}
            />
            <ListRow
              icon="arrow.left.arrow.right"
              label="Switch workspace"
              onPress={() => router.push("/workspaces")}
            />
          </Card>
        </ScrollView>
      </View>
    );
  }

  // Presentation gating — see the header note. `capabilities` comes from the
  // server alongside the role, so the UI and the backend agree by
  // construction instead of the app re-deriving the rule.
  const caps = active.capabilities || {};
  const canManageMembers = !!caps.manage_members;
  const canManageSettings = !!caps.manage_settings;

  return (
    <View style={st.screen}>
      <Stack.Screen options={{ title: "Organisation" }} />
      <ScrollView contentContainerStyle={st.body}>
        <View style={st.header}>
          <Text style={st.name}>{workspaceLabel(active)}</Text>
          <Text style={st.role}>
            {roleLabel(role)}
            {active.industry ? ` · ${active.industry}` : ""}
          </Text>
        </View>

        <Card style={st.card}>
          <SectionRule>Work</SectionRule>
          <ListRow
            icon="newspaper.fill"
            label="Meetings"
            sub="Everything recorded in this organisation"
            onPress={() => router.push("/")}
          />
          <ListRow
            icon="checkmark.circle.fill"
            label="Tasks"
            sub="Organisation action items"
            onPress={() => router.push("/tasks")}
          />
          <ListRow
            icon="person.2.fill"
            label="Contacts"
            sub="Shared clients, prospects and vendors"
            onPress={() => router.push("/contacts")}
          />
        </Card>

        <Card style={st.card}>
          <SectionRule>Manage</SectionRule>
          {canManageMembers ? (
            <ListRow
              icon="person.badge.plus"
              label="Members"
              sub="Invite people and set roles"
              onPress={() => router.push("/organisation/members")}
            />
          ) : (
            <ListRow
              icon="person.2.fill"
              label="Members"
              sub="See who's in this organisation"
              onPress={() => router.push("/organisation/members")}
            />
          )}
          <ListRow
            icon="puzzlepiece.extension.fill"
            label="Organisation Integrations"
            sub="Apps and services used with this organisation"
            onPress={() => router.push("/integrations")}
          />
          {canManageSettings ? (
            <ComingSoonRow
              icon="gearshape.fill"
              label="Settings"
              sub="Organisation profile and preferences"
            />
          ) : null}
        </Card>

        <Card style={st.card}>
          <SectionRule>Workspace</SectionRule>
          <ListRow
            icon="arrow.left.arrow.right"
            label="Switch workspace"
            sub="Back to personal, or another organisation"
            onPress={() => router.push("/workspaces")}
          />
        </Card>
      </ScrollView>
    </View>
  );
}
