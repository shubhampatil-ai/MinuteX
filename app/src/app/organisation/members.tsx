// src/app/organisation/members.tsx — the organisation's people.
//
// Built entirely on the Phase 2B APIs (GET/POST/PATCH/DELETE members). None
// of the authorization rules are re-implemented here — the backend enforces
// all of them and this screen only decides which controls to draw:
//
//   * OWNER cannot be invited          (backend 403)
//   * OWNER's role cannot be changed   (backend 403)
//   * OWNER cannot be removed          (backend 403)
//   * MANAGER cannot self-escalate     (backend 403, since OWNER is ungrantable)
//
// So the row for the owner simply has no actions, and the role picker offers
// Manager and Member only. If a client bypassed that, the server refuses.
//
// THE INVITE TOKEN IS SHOWN ONCE. The backend returns the raw token exactly
// once and stores only its hash, so this screen surfaces the link for
// copying and never tries to read it back — there is nowhere to read it from.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, ScrollView, StyleSheet, Text, View } from "react-native";
import { Stack, useRouter } from "expo-router";
import { S, FONT, R, useTheme, ColorScale } from "../../../lib/theme";
import {
  Button, Card, Chip, EmptyState, ErrorText, ListRow, Loading, SectionRule,
  TextField, scrollFormProps,
} from "../../../lib/ui";
import { copyText } from "../../../lib/clipboard";
import {
  ApiError, ApiMember, WorkspaceRole,
  getWorkspaceMembers, inviteMember, removeMember, updateMemberRole,
} from "../../../lib/api";
import { useWorkspace, roleLabel } from "../../../lib/workspace-context";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    screen: { flex: 1, backgroundColor: C.bg },
    body: { padding: S.lg, paddingBottom: S.xxl },
    card: { marginBottom: S.lg },
    label: {
      fontFamily: FONT.semibold, fontSize: 12, color: C.textDim,
      marginTop: S.md, marginBottom: 6,
    },
    roleRow: { flexDirection: "row", gap: S.sm, marginBottom: S.sm },
    // The one-time invitation link.
    tokenBox: {
      backgroundColor: C.primarySoft, borderRadius: R.md,
      padding: 14, marginTop: S.md,
    },
    tokenTitle: {
      fontFamily: FONT.semibold, fontSize: 13, color: C.text, marginBottom: 6,
    },
    tokenBody: {
      fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 18,
      color: C.textDim, marginBottom: S.md,
    },
    tokenValue: {
      fontFamily: FONT.regular, fontSize: 11.5, color: C.text,
      backgroundColor: C.surface, borderRadius: 8, padding: 10,
      marginBottom: S.md,
    },
  });
}

const ROLE_CHOICES: WorkspaceRole[] = ["MEMBER", "MANAGER"];

export default function MembersScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { active, activeId, isOrganisation } = useWorkspace();

  const [members, setMembers] = useState<ApiMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [email, setEmail] = useState("");
  const [role, setRole] = useState<WorkspaceRole>("MEMBER");
  const [inviting, setInviting] = useState(false);
  const [issued, setIssued] = useState<{ token: string; email: string } | null>(null);

  const caps = active?.capabilities || {};
  const canManage = !!caps.manage_members;

  const load = useCallback(async () => {
    if (!activeId || !isOrganisation) return;
    setError("");
    try {
      const { members: rows } = await getWorkspaceMembers(activeId);
      setMembers(rows);
    } catch (e: any) {
      setError(e?.message || "Could not load members.");
    } finally {
      setLoading(false);
    }
  }, [activeId, isOrganisation]);

  useEffect(() => { void load(); }, [load]);

  const invite = useCallback(async () => {
    setError("");
    const address = email.trim().toLowerCase();
    if (!address) {
      setError("Enter the person's work email address.");
      return;
    }
    setInviting(true);
    try {
      const res = await inviteMember(activeId, address, role);
      // Shown ONCE — the server keeps only the hash.
      setIssued({ token: res.invite_token, email: address });
      setEmail("");
      await load();
    } catch (e: any) {
      setError(e?.message || "Could not send the invitation.");
    } finally {
      setInviting(false);
    }
  }, [activeId, email, role, load]);

  const changeRole = useCallback((m: ApiMember) => {
    const next: WorkspaceRole = m.role === "MANAGER" ? "MEMBER" : "MANAGER";
    Alert.alert(
      `Make ${m.name || m.email} a ${roleLabel(next)}?`,
      next === "MANAGER"
        ? "Managers can invite people, manage members, and edit organisation contacts."
        : "Members can record, create tasks and add contacts, but cannot manage the organisation.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: `Make ${roleLabel(next)}`,
          onPress: async () => {
            try {
              await updateMemberRole(activeId, m.user_id, next);
              await load();
            } catch (e: any) {
              setError(e?.message || "Could not change the role.");
            }
          },
        },
      ]);
  }, [activeId, load]);

  const remove = useCallback((m: ApiMember) => {
    Alert.alert(
      `Remove ${m.name || m.email}?`,
      "They lose access to this organisation immediately. Meetings, tasks "
      + "and contacts they created stay with the organisation.",
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Remove",
          style: "destructive",
          onPress: async () => {
            try {
              await removeMember(activeId, m.user_id);
              await load();
            } catch (e: any) {
              setError(e?.message || "Could not remove the member.");
            }
          },
        },
      ]);
  }, [activeId, load]);

  if (!isOrganisation) {
    return (
      <View style={st.screen}>
        <Stack.Screen options={{ title: "Members" }} />
        <ScrollView contentContainerStyle={st.body}>
          <EmptyState
            icon="person.2.fill"
            title="Personal workspaces have no members"
            subtitle="Switch to an organisation to manage its people."
          />
          <Card style={{ marginTop: S.lg }}>
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

  return (
    <View style={st.screen}>
      <Stack.Screen options={{ title: "Members" }} />
      <ScrollView contentContainerStyle={st.body} {...scrollFormProps}>
        {loading ? <Loading label="Loading members" /> : null}
        {error ? <ErrorText>{error}</ErrorText> : null}

        <Card style={st.card}>
          <SectionRule>
            {members.length === 1 ? "1 person" : `${members.length} people`}
          </SectionRule>
          {members.map((m) => {
            const isOwner = m.role === "OWNER";
            return (
              <ListRow
                key={m.user_id}
                icon={isOwner ? "crown.fill" : "person.fill"}
                // Their actual face. `avatar_view_url` is the presigned GET the
                // API derives on every read; Avatar falls back to coloured
                // initials when it is empty or expired, so a member is never
                // an anonymous glyph.
                photoUri={m.avatar_view_url}
                photoName={m.name || m.email || ""}
                label={m.name || m.email || m.user_id}
                sub={[m.email, roleLabel(m.role)].filter(Boolean).join(" · ")}
                // The OWNER row has no actions: the backend refuses a role
                // change and a removal for them, so offering either would be
                // a control that always fails.
                onPress={canManage && !isOwner ? () => changeRole(m) : undefined}
                right={
                  canManage && !isOwner ? (
                    <Chip label="Remove" onPress={() => remove(m)} />
                  ) : undefined
                }
              />
            );
          })}
          {members.length === 1 && canManage ? (
            <Text style={[st.tokenBody, { marginTop: S.md, marginBottom: 0 }]}>
              It's just you so far. Invite the people you meet with below.
            </Text>
          ) : null}
        </Card>

        {canManage ? (
          <Card style={st.card}>
            <SectionRule>Add member</SectionRule>
            <Text style={st.label}>Work email</Text>
            <TextField
              value={email}
              onChangeText={setEmail}
              placeholder="name@company.com"
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="email-address"
              editable={!inviting}
            />

            <Text style={st.label}>Role</Text>
            <View style={st.roleRow}>
              {ROLE_CHOICES.map((r) => (
                <Chip
                  key={r}
                  label={roleLabel(r)}
                  active={role === r}
                  onPress={() => setRole(r)}
                />
              ))}
            </View>

            <View style={{ marginTop: S.sm }}>
              <Button
                label={inviting ? "Sending…" : "Send invitation"}
                onPress={invite}
                disabled={inviting || !email.trim()}
              />
            </View>

            {issued ? (
              <View style={st.tokenBox}>
                <Text style={st.tokenTitle}>Invitation for {issued.email}</Text>
                <Text style={st.tokenBody}>
                  Send them this code. It works once, expires in a few days,
                  and only the invited address can use it. You won't be able
                  to see it again.
                </Text>
                <Text style={st.tokenValue} selectable>{issued.token}</Text>
                <Button
                  label="Copy code"
                  onPress={() => { void copyText(issued.token); }}
                />
              </View>
            ) : null}
          </Card>
        ) : null}
      </ScrollView>
    </View>
  );
}
