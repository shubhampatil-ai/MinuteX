// src/app/(tabs)/profile.tsx — You: account, plan, preferences.
//
// "Workspace" edition. The masthead carries the email as its kicker and the
// name as the bold title, with the initial set in a filled, fully rounded
// avatar circle to its right. Stats live in a rounded, shadowed Card strip.
// Account fields are backed by the userApi /me endpoints; plan tiers are
// honest Coming Soon placeholders (no billing backend yet) — visible but
// clearly not live.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, ScrollView, StyleSheet, Text, View } from "react-native";
import { useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { S, R, ELEV, CAPS, FONT, TABULAR, useTheme, ColorScale } from "../../../lib/theme";
import {
  Button, ComingSoonRow, ErrorText, ListRow, Loading, Masthead, SectionRule,
  SoonBadge, SwitchRow, TextField, Toast, KeyboardAware, scrollFormProps,
} from "../../../lib/ui";
import {
  canUseWavEngine, getRecEngine, loadRecEngine, setRecEngine,
} from "../../../lib/rec-engine";
import { useDevice } from "../../../lib/device-context";
import { getRecordings, RecordingSummary } from "../../../lib/api";
import {
  getMe, updateMe, changePassword, clearToken, UserProfile, ApiError,
} from "../../../lib/api";

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: C.bg, paddingHorizontal: 20 },
    // A filled, fully-rounded avatar disc in the soft primary tint.
    avatar: {
      width: 52, height: 52, borderRadius: R.pill,
      backgroundColor: C.primarySoft,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    avatarTxt: { fontFamily: FONT.extrabold, fontSize: 22, color: C.primary },
    editRow: { flexDirection: "row" as const, gap: S.sm, marginTop: S.md },
    pwMsg: { fontFamily: FONT.regular, fontSize: 13, color: C.primary, marginTop: S.sm },
    // Stat strip — a rounded, softly-shadowed Card split into two cells.
    statStrip: {
      flexDirection: "row" as const, marginTop: 12,
      backgroundColor: C.surface, borderRadius: R.card, overflow: "hidden" as const,
      shadowColor: C.shadow, ...ELEV.sm,
    },
    statCell: { flex: 1, padding: S.lg },
    statNum: { ...TABULAR, fontFamily: FONT.extrabold, fontSize: 28, lineHeight: 32, color: C.text },
    statLabel: { ...CAPS, fontSize: 10, letterSpacing: 1.2, color: C.textFaint, marginTop: 5 },
    statDivider: { width: 1, backgroundColor: C.border },
    // Plan rows
    planRow: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: S.md,
      paddingVertical: 15, borderBottomWidth: 1, borderBottomColor: C.border,
    },
    planName: { fontFamily: FONT.semibold, fontSize: 14.5, color: C.text },
    planSub: { ...T.caption, marginTop: 2 },
    planBadge: {
      backgroundColor: C.primarySoft, borderRadius: R.pill,
      paddingHorizontal: 10, paddingVertical: 4,
    },
    planBadgeTxt: { ...CAPS, fontFamily: FONT.bold, fontSize: 10, letterSpacing: 0.8, color: C.primary },
  });
}

export default function ProfileScreen() {
  // Experimental recording engine. Android-only and only in a build that
  // bundled the native module, so the row is hidden entirely elsewhere rather
  // than shown disabled — a toggle that cannot do anything is worse than none.
  const [wavEngine, setWavEngine] = useState(false);
  useEffect(() => {
    void loadRecEngine().then(() => setWavEngine(getRecEngine() === "wav"));
  }, []);
  const router = useRouter();
  const { C, T } = useTheme();
  const insets = useSafeAreaInsets();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const { disconnect } = useDevice();
  const [me, setMe] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Edit-name state
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);

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
    } catch { /* the stat strip is optional — leave it empty */ }
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
        disconnect(); await clearToken(); router.replace("/login");
      } },
    ]);
  };

  if (loading) return <Loading label="Loading profile…" />;

  const initial = (me?.name || me?.email || "?").trim().charAt(0).toUpperCase();

  return (
    <KeyboardAware>
      <ScrollView style={[st.container, { paddingTop: insets.top + S.lg }]}
        contentContainerStyle={{ paddingBottom: 100 }} {...scrollFormProps}>
        <Masthead
          kicker={me?.email}
          title={me?.name || "Add your name"}
          right={<View style={st.avatar}><Text style={st.avatarTxt}>{initial}</Text></View>}
        />
        {error ? <ErrorText>{error}</ErrorText> : null}

        {editing ? (
          <View style={{ marginTop: S.lg }}>
            <TextField value={name} onChangeText={setName} placeholder="Your name" autoCapitalize="words" />
            <View style={st.editRow}>
              <Button label="Cancel" variant="secondary" style={{ flex: 1 }} onPress={() => { setEditing(false); setName(me?.name ?? ""); }} />
              <Button label="Save" style={{ flex: 1 }} loading={saving} onPress={saveName} />
            </View>
          </View>
        ) : null}

        {/* Since you started — real numbers from the recordings list */}
        {items.length ? (
          <>
            <SectionRule>Since you started</SectionRule>
            <View style={st.statStrip}>
              <View style={st.statCell}>
                <Text style={st.statNum}>{stats.listened}</Text>
                <Text style={st.statLabel}>Listened for you</Text>
              </View>
              <View style={st.statDivider} />
              <View style={st.statCell}>
                <Text style={st.statNum}>{stats.filed}</Text>
                <Text style={st.statLabel}>Briefs filed</Text>
              </View>
            </View>
          </>
        ) : null}

        {/* Plan — presentational placeholders, no billing backend yet */}
        <SectionRule>Your plan</SectionRule>
        <View style={st.planRow}>
          <View style={{ flex: 1 }}>
            <Text style={st.planName}>Free</Text>
            <Text style={st.planSub}>Transcripts and briefs, unlimited</Text>
          </View>
          <View style={st.planBadge}><Text style={st.planBadgeTxt}>Current</Text></View>
        </View>
        <ComingSoonRow icon="crown.fill" label="Pro" sub="CRM sync, 30 languages, ask-anything" />

        {/* Account */}
        <SectionRule>Account</SectionRule>
        <ListRow icon="envelope" label="Email" sub={me?.email ?? "—"} />
        {pwOpen ? (
          <View style={{ paddingVertical: S.md }}>
            <TextField value={curPw} onChangeText={setCurPw} secureTextEntry placeholder="Current password" />
            <TextField style={{ marginTop: S.sm }} value={newPw} onChangeText={setNewPw} secureTextEntry
              placeholder="New password (min 8)" />
            {pwMsg ? <Text style={[st.pwMsg, pwMsg.includes("✓") && { color: C.success }]}>{pwMsg}</Text> : null}
            <View style={st.editRow}>
              <Button label="Cancel" variant="secondary" style={{ flex: 1 }} onPress={() => { setPwOpen(false); setPwMsg(""); }} />
              <Button label="Update" style={{ flex: 1 }} loading={pwBusy} onPress={submitPw} />
            </View>
          </View>
        ) : (
          <ListRow icon="lock.fill" label="Change password" onPress={() => setPwOpen(true)} />
        )}
        {!editing ? (
          <ListRow icon="pencil" label="Edit your name" onPress={() => setEditing(true)} />
        ) : null}

        {/* Tasks, Folders and People are NOT listed here. They live on the
            Desk's Workspace row, because they are daily destinations rather
            than settings — putting them in this list was the wrong call and
            cost a tap on the most-used screens in the app. Only Contacts keeps
            a secondary entry point below, next to the account it belongs to. */}
        {/* Settings */}
        <SectionRule>Settings</SectionRule>
        <ListRow icon="person.2.fill" label="Contacts"
          sub="The people you meet with" onPress={() => router.push("/contacts")} />
        <ListRow icon="gearshape.fill" label="Appearance" sub="Theme, sync, notifications" onPress={() => router.push("/settings")} />
        {canUseWavEngine() ? (
          <SwitchRow
            label="High-quality WAV recording"
            sub="Experimental · 16 kHz mono PCM, ~1.9 MB/min (AAC is ~1 MB/min). Applies to the next recording."
            value={wavEngine}
            onValueChange={(v) => {
              setWavEngine(v);
              void setRecEngine(v ? "wav" : "aac");
              showToast(v ? "Next recording will use WAV" : "Next recording will use AAC");
            }}
          />
        ) : null}
        <ListRow icon="cpu.fill" label="Your device" sub="Battery, Wi-Fi, pairing" onPress={() => router.push("/devices")} />
        <ListRow icon="questionmark.circle" label="Help & support"
          right={<SoonBadge />} onPress={() => showToast("Help centre is coming soon")} />
        <ListRow icon="info.circle" label="About MinuteX" sub="Version 1.0.0 · by Exceller Tech" />
        <ListRow icon="rectangle.portrait.and.arrow.right" label="Sign out" destructive onPress={logout} />
      </ScrollView>

      <Toast visible={!!toast} label={toast} />
    </KeyboardAware>
  );
}
