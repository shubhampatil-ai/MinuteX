// src/app/(tabs)/profile.tsx — You: profile, plan, account, workspace, app, support.
//
// Hierarchy by typography and grouping, not icons: an identity header, the two
// real usage figures, then short titled groups of rows on single surfaces
// (lib/kit.tsx SettingGroup). No row carries a decorative icon — each is
// legible from its title, and the only colour is the destructive Sign out.
//
// Account fields are backed by the userApi /me endpoints; plan tiers are
// honest "Soon" placeholders (no billing backend yet) — visible but clearly
// not live. The experimental WAV engine toggle moved to Settings › Recording,
// where the other capture preferences are; this screen links there.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { S, R, FONT, GUTTER, COMPANY, useTheme, ColorScale } from "../../../lib/theme";
import {
  Avatar, Button, ErrorBlock, Loading, TextField, Toast, KeyboardAware, scrollFormProps,
} from "../../../lib/ui";
import { SettingGroup, SettingRow, StatusBadge } from "../../../lib/kit";
import { Icon } from "../../../lib/icons";
import { PhotoPicker } from "../../../lib/photo-picker";
import { canPickImage } from "../../../lib/avatars";
import { APP_VERSION } from "../../../lib/app-info";
import { unregisterThisDevice } from "../../../lib/push";
import { useDevice } from "../../../lib/device-context";
import { useWorkspace, workspaceLabel, roleLabel } from "../../../lib/workspace-context";
import {
  getMe, updateMe, changePassword, clearToken, getRecordings,
  RecordingSummary, UserProfile, ApiError,
} from "../../../lib/api";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: GUTTER },
    identity: { flexDirection: "row", alignItems: "center", gap: S.lg, marginTop: S.lg },
    name: { fontFamily: FONT.bold, fontSize: 24, lineHeight: 30, color: C.text, letterSpacing: -0.3 },
    email: { fontFamily: FONT.regular, fontSize: 14, lineHeight: 19, color: C.textDim, marginTop: 2 },
    // The small edit affordance on the avatar. Bordered in the page
    // background so it reads as sitting ON the disc at any avatar colour.
    avatarBadge: {
      position: "absolute", right: -2, bottom: -2,
      width: 24, height: 24, borderRadius: 12,
      backgroundColor: C.primary, borderWidth: 2, borderColor: C.bg,
      alignItems: "center", justifyContent: "center",
    },
    editRow: { flexDirection: "row", gap: S.sm, marginTop: S.md },
    pwMsg: { fontFamily: FONT.regular, fontSize: 13, color: C.primary, marginTop: S.sm },
    // Two real figures, one hairline surface. Not a dashboard: it is the only
    // usage data the API exposes, and it is hidden when that call fails.
    stats: {
      flexDirection: "row", marginTop: S.xl, backgroundColor: C.surface,
      borderRadius: R.card, borderWidth: StyleSheet.hairlineWidth, borderColor: C.border,
    },
    stat: { flex: 1, paddingVertical: S.lg, paddingHorizontal: S.lg },
    statNum: { fontFamily: FONT.bold, fontSize: 24, lineHeight: 28, color: C.text, fontVariant: ["tabular-nums"] },
    statLabel: { fontFamily: FONT.regular, fontSize: 12.5, color: C.textDim, marginTop: 2 },
    statDivider: { width: StyleSheet.hairlineWidth, backgroundColor: C.border, marginVertical: S.md },
    form: {
      marginTop: S.md, backgroundColor: C.surface, borderRadius: R.card, padding: S.lg,
      borderWidth: StyleSheet.hairlineWidth, borderColor: C.border,
    },
    formTitle: { fontFamily: FONT.semibold, fontSize: 15, color: C.text, marginBottom: S.md },
  });
}

export default function ProfileScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const insets = useSafeAreaInsets();
  const st = useMemo(() => buildStyles(C), [C]);
  const { disconnect } = useDevice();
  // Display only. The backend re-resolves membership and role on every
  // request, so nothing shown here is trusted for authorization.
  const { active: activeWorkspace, role: workspaceRole } = useWorkspace();
  const [me, setMe] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Edit-name state
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);

  // Profile photo. The picker uploads and hands back an S3 key; saving it is
  // this screen's job (see savePhoto), which is what keeps an abandoned sheet
  // from half-writing the profile.
  const [photoOpen, setPhotoOpen] = useState(false);

  // Change-password state
  const [pwOpen, setPwOpen] = useState(false);
  const [curPw, setCurPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [pwBusy, setPwBusy] = useState(false);
  const [pwMsg, setPwMsg] = useState("");

  // "Since you started" — derived from the recordings list, the only source
  // of usage data the API exposes. Failing to load them must not block the
  // profile itself, so the strip just hides if this call fails.
  const [items, setItems] = useState<RecordingSummary[]>([]);

  const load = useCallback(async () => {
    setError("");
    try {
      const u = await getMe();
      setMe(u);
      setName(u.name);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) { await clearToken(); router.replace("/login"); return; }
      setError(e instanceof ApiError ? e.message : "Could not load profile.");
    } finally {
      setLoading(false);
    }
    try {
      setItems(await getRecordings());
    } catch { /* the figures are optional — leave them hidden */ }
  }, [router]);

  const stats = useMemo(() => {
    // duration arrives as number | string | null (DynamoDB Decimals) — coerce.
    const secs = items.reduce((acc, r) => {
      const d = Number(r.duration);
      return acc + (isFinite(d) ? d : 0);
    }, 0);
    const hours = Math.round(secs / 3600);
    return {
      listened: hours >= 1 ? `${hours}h` : `${Math.round(secs / 60)}m`,
      filed: String(items.length),
    };
  }, [items]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => () => { if (toastTimer.current) clearTimeout(toastTimer.current); }, []);

  const showToast = (msg: string) => {
    setToast(msg);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(""), 1800);
  };

  const saveName = async () => {
    setSaving(true);
    try {
      const u = await updateMe({ name: name.trim() });
      setMe(u); setEditing(false);
      showToast("Profile updated");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not save.");
    } finally { setSaving(false); }
  };

  const savePhoto = async (avatarKey: string) => {
    // Errors are re-thrown, not swallowed: the sheet is still open and shows
    // the message, which is better than closing on a save that did not happen.
    const u = await updateMe({ avatar_url: avatarKey });
    setMe(u);
    showToast(avatarKey ? "Photo updated" : "Photo removed");
  };

  const submitPw = async () => {
    setPwMsg("");
    if (newPw.length < 8) { setPwMsg("New password must be at least 8 characters."); return; }
    setPwBusy(true);
    try {
      await changePassword(curPw, newPw);
      setPwMsg("Password changed ✓"); setCurPw(""); setNewPw("");
      setTimeout(() => { setPwOpen(false); setPwMsg(""); }, 1200);
    } catch (e) {
      setPwMsg(e instanceof ApiError ? e.message : "Could not change password.");
    } finally { setPwBusy(false); }
  };

  const logout = () => {
    Alert.alert("Sign out", "Sign out of MinuteX?", [
      { text: "Cancel", style: "cancel" },
      { text: "Sign out", style: "destructive", onPress: async () => {
        // BEFORE clearToken: removing this device's push token needs the
        // session. Otherwise the next person to pick up this phone would see
        // this account's "Brief ready" notifications. Best-effort by design.
        await unregisterThisDevice();
        disconnect(); await clearToken(); router.replace("/login");
      } },
    ]);
  };

  if (loading) return <Loading label="Loading profile…" />;

  const displayName = me?.name || "Add your name";
  const isOrg = !!activeWorkspace && !activeWorkspace.is_personal;

  return (
    <KeyboardAware>
      <ScrollView
        style={[st.container, { paddingTop: insets.top }]}
        // Derived from the real tab-bar height (60 + max(inset, 8)) plus the
        // record button's overhang.
        contentContainerStyle={{ paddingBottom: 60 + Math.max(insets.bottom, 8) + 40 }}
        {...scrollFormProps}
      >
        {/* ---- Identity ---- */}
        <View style={st.identity}>
          <Pressable
            onPress={() => setPhotoOpen(true)}
            // A build without the native picker cannot open the sheet, so the
            // disc stays a plain avatar rather than a dead tap target.
            disabled={!canPickImage()}
            accessibilityRole="button"
            accessibilityLabel={me?.avatar_view_url ? "Change your profile photo" : "Add a profile photo"}
            hitSlop={8}
          >
            <View>
              <Avatar name={me?.name || me?.email || ""} photoUri={me?.avatar_view_url} size={72} fontSize={26} />
              {canPickImage() ? (
                <View style={st.avatarBadge}>
                  <Icon name={me?.avatar_view_url ? "pencil" : "plus"} size={12} tintColor="#fff" />
                </View>
              ) : null}
            </View>
          </Pressable>
          <View style={{ flex: 1, minWidth: 0 }}>
            <Text
              style={[st.name, !me?.name && { color: C.textDim }]}
              numberOfLines={2}
              accessibilityRole="header"
            >
              {displayName}
            </Text>
            {me?.email ? <Text style={st.email} numberOfLines={1}>{me.email}</Text> : null}
          </View>
        </View>

        {/* A failed profile load states the problem AND offers the way out —
            on a primary tab there is otherwise no retry. */}
        {error ? <View style={{ marginTop: S.lg }}><ErrorBlock message={error} onRetry={load} /></View> : null}

        {editing ? (
          <View style={st.form}>
            <Text style={st.formTitle}>Your name</Text>
            <TextField value={name} onChangeText={setName} placeholder="Your name" autoCapitalize="words" />
            <View style={st.editRow}>
              <Button label="Cancel" variant="secondary" style={{ flex: 1 }} onPress={() => { setEditing(false); setName(me?.name ?? ""); }} />
              <Button label="Save" style={{ flex: 1 }} loading={saving} onPress={saveName} />
            </View>
          </View>
        ) : null}

        {/* ---- The two real usage figures ---- */}
        {items.length ? (
          <View style={st.stats}>
            <View style={st.stat} accessible accessibilityLabel={`${stats.listened} listened for you`}>
              <Text style={st.statNum}>{stats.listened}</Text>
              <Text style={st.statLabel}>Listened for you</Text>
            </View>
            <View style={st.statDivider} />
            <View style={st.stat} accessible accessibilityLabel={`${stats.filed} briefs filed`}>
              <Text style={st.statNum}>{stats.filed}</Text>
              <Text style={st.statLabel}>Briefs filed</Text>
            </View>
          </View>
        ) : null}

        {/* ---- Plan — presentational, no billing backend yet ---- */}
        <SettingGroup title="Plan">
          <SettingRow
            title="Free"
            description="Transcripts and briefs, unlimited"
            trailing={<StatusBadge label="Current" tone="info" />}
          />
          <SettingRow title="Pro" description="CRM sync, 30 languages, ask-anything" soon />
        </SettingGroup>

        {/* ---- Account ---- */}
        <SettingGroup title="Account">
          <SettingRow title="Email" value={me?.email ?? "—"} />
          {!editing ? <SettingRow title="Edit your name" onPress={() => setEditing(true)} /> : null}
          {!pwOpen ? <SettingRow title="Change password" onPress={() => setPwOpen(true)} /> : null}
        </SettingGroup>
        {pwOpen ? (
          <View style={st.form}>
            <Text style={st.formTitle}>Change password</Text>
            <TextField value={curPw} onChangeText={setCurPw} secureTextEntry placeholder="Current password" />
            <TextField style={{ marginTop: S.sm }} value={newPw} onChangeText={setNewPw} secureTextEntry
              placeholder="New password (min 8)" />
            {pwMsg ? <Text style={[st.pwMsg, pwMsg.includes("✓") && { color: C.success }]}>{pwMsg}</Text> : null}
            <View style={st.editRow}>
              <Button label="Cancel" variant="secondary" style={{ flex: 1 }} onPress={() => { setPwOpen(false); setPwMsg(""); }} />
              <Button label="Update" style={{ flex: 1 }} loading={pwBusy} onPress={submitPw} />
            </View>
          </View>
        ) : null}

        {/* ---- Workspace — context, not a preference, so it sits above App.
            The current workspace is named in the row so "where am I
            working?" is answered without a tap. ---- */}
        <SettingGroup title="Workspace">
          <SettingRow
            title={workspaceLabel(activeWorkspace)}
            description={isOrg ? `${roleLabel(workspaceRole)} · Switch workspace` : "Only you · Switch workspace"}
            onPress={() => router.push("/workspaces")}
          />
          {isOrg ? (
            <SettingRow
              title="Organisation"
              description="Members, contacts and settings"
              onPress={() => router.push("/organisation")}
            />
          ) : null}
          <SettingRow title="Contacts" description="The people you meet with" onPress={() => router.push("/contacts")} />
        </SettingGroup>

        {/* ---- App ---- */}
        <SettingGroup title="App">
          <SettingRow
            title="Settings"
            description="Appearance, recording, notifications, privacy"
            onPress={() => router.push("/settings")}
          />
          <SettingRow title="Your device" description="Battery, Wi-Fi, pairing" onPress={() => router.push("/devices")} />
        </SettingGroup>

        {/* ---- Support & about ---- */}
        <SettingGroup title="Support">
          <SettingRow title="Help & support" soon />
          <SettingRow title="About MinuteX" value={`Version ${APP_VERSION} · ${COMPANY}`} />
        </SettingGroup>

        <SettingGroup>
          <SettingRow title="Sign out" destructive onPress={logout} chevron={false} />
        </SettingGroup>
      </ScrollView>

      <PhotoPicker
        visible={photoOpen}
        onClose={() => setPhotoOpen(false)}
        name={me?.name || me?.email || ""}
        currentPhotoUri={me?.avatar_view_url}
        scope="user"
        onPicked={savePhoto}
        canRemove={!!me?.avatar_url}
      />

      <Toast visible={!!toast} label={toast} />
    </KeyboardAware>
  );
}
