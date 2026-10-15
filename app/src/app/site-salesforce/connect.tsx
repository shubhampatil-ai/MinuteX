// src/app/site-salesforce/connect.tsx — "Install MinuteX in Salesforce",
// opened from the Salesforce card in Settings -> Integrations (and from the
// org's MinuteX Setup tab: its "Open MinuteX" button links here).
//
//   Install MinuteX in Salesforce
//     -> the Salesforce install page opens; the admin installs MinuteX
//     -> in Salesforce, the MinuteX Setup tab asks for the SETUP CODE shown
//        here, and sends it straight to MinuteX (which checks the org)
//     -> back here: "✓ MinuteX installed" -> Connect Salesforce -> Ready
//
// Nothing is copied by hand but the short setup code: no link, package id,
// Site address or secret passes through the person or this app. The screen
// re-checks whenever the person comes back from the browser or the app
// returns to the foreground, and every few seconds while waiting.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, AppState, ScrollView, StyleSheet, Switch, Text, View } from "react-native";
import { Stack, useFocusEffect, useRouter } from "expo-router";
import * as WebBrowser from "expo-web-browser";
import { FONT, R, S, useTheme, ColorScale } from "../../../lib/theme";
import { Button, ErrorText, Loading } from "../../../lib/ui";
import {
  ApiError, SiteSfInstall, finishSiteSalesforceInstall, getSiteSalesforceInstall,
  startSiteSalesforceInstall,
} from "../../../lib/api";
import { useWorkspace } from "../../../lib/workspace-context";
import { siteSfError, usePolling } from "../../../lib/site-salesforce-ui";
import { invalidateSiteSalesforce } from "../../../lib/site-salesforce-events";
import { orgDisplayName } from "../../../lib/site-salesforce-flow";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    screen: { flex: 1, backgroundColor: C.bg },
    body: { padding: S.lg, paddingBottom: S.x4 },
    title: { fontFamily: FONT.bold, fontSize: 20, color: C.text },
    intro: { fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20, color: C.textDim, marginTop: S.sm },
    help: { fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 18, color: C.textFaint, marginTop: S.md },
    row: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", marginTop: S.xl },
    rowLabel: { fontFamily: FONT.medium, fontSize: 14, color: C.text, flex: 1, paddingRight: S.md },
    codeBox: { borderWidth: 1, borderColor: C.border, borderRadius: R.card, padding: S.lg,
               marginTop: S.xl, alignItems: "center", backgroundColor: C.surface },
    codeLabel: { fontFamily: FONT.medium, fontSize: 12.5, color: C.textDim },
    code: { fontFamily: FONT.bold, fontSize: 30, letterSpacing: 3, color: C.text, marginTop: S.sm },
    step: { fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20, color: C.text, marginTop: S.sm },
    waiting: { fontFamily: FONT.medium, fontSize: 13, color: C.textDim, marginTop: S.lg, textAlign: "center" },
    ok: { fontFamily: FONT.bold, fontSize: 20, color: C.success },
    org: { fontFamily: FONT.medium, fontSize: 14, color: C.textDim, marginTop: 4 },
  });
}

export default function InstallSalesforce() {
  const router = useRouter();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { activeId, can } = useWorkspace();
  const admin = can("manage_integrations");
  const [install, setInstall] = useState<SiteSfInstall | null>(null);
  const [sandbox, setSandbox] = useState(false);
  const [busy, setBusy] = useState<"" | "start" | "open" | "connect">("");
  const [error, setError] = useState("");
  // Set once this screen has opened the install page, so returning to the
  // app without finishing reads "not finished yet", not "nothing happened".
  const openedRef = useRef(false);

  const load = useCallback(async () => {
    if (!activeId) return;
    try {
      setInstall(await getSiteSalesforceInstall(activeId));
      setError("");
    } catch (e) {
      setError(siteSfError(e));
      setInstall((i) => i ?? { status: "none" });
    }
  }, [activeId]);
  useFocusEffect(useCallback(() => { void load(); }, [load]));

  // Back from the browser / Salesforce: check again at once.
  useEffect(() => {
    const sub = AppState.addEventListener("change", (s) => { if (s === "active") void load(); });
    return () => sub.remove();
  }, [load]);
  usePolling(install?.status === "waiting", () => { void load(); }, 4000);

  const openInstallPage = async (url: string | undefined) => {
    if (!url) return;
    setBusy("open"); setError("");
    try {
      openedRef.current = true;
      await WebBrowser.openBrowserAsync(url);   // resolves when they come back
    } catch {
      setError("Couldn't open Salesforce. Try again.");
    } finally {
      setBusy("");
      void load();
    }
  };

  const start = async () => {
    setBusy("start"); setError("");
    try {
      const next = await startSiteSalesforceInstall(activeId, sandbox);
      setInstall(next);
      setBusy("");
      await openInstallPage(next.install_url);
    } catch (e) {
      setError(siteSfError(e));
      setBusy("");
    }
  };

  const connect = async (allowOrgChange = false) => {
    setBusy("connect"); setError("");
    try {
      await finishSiteSalesforceInstall(activeId, allowOrgChange);
      invalidateSiteSalesforce("connection", "objects", "people", "relation");
      router.replace("/site-salesforce" as never);
    } catch (e) {
      if (e instanceof ApiError && e.code === "org_mismatch" && !allowOrgChange) {
        Alert.alert("Use a different Salesforce org?",
          "This workspace is already connected to another Salesforce org. Switching clears what MinuteX learned about it.",
          [{ text: "Cancel", style: "cancel" },
           { text: "Switch", style: "destructive", onPress: () => { void connect(true); } }]);
      } else if (e instanceof ApiError && e.code === "install_expired") {
        setError(siteSfError(e));
        void load();
      } else {
        setError(siteSfError(e));
      }
    } finally {
      setBusy("");
    }
  };

  if (!install) return <Loading />;
  const status = install.status;

  return (
    <ScrollView style={st.screen} contentContainerStyle={st.body}>
      <Stack.Screen options={{ title: "Salesforce" }} />
      {!admin ? (
        <>
          <Text style={st.title}>Install MinuteX in Salesforce</Text>
          <Text style={st.intro}>
            An owner or manager of this workspace installs MinuteX in Salesforce. Ask them to open
            Settings → Integrations → Salesforce.
          </Text>
        </>
      ) : status === "installed" ? (
        <>
          <Text style={st.ok}>✓ MinuteX installed</Text>
          {orgDisplayName(install.org_name) ? <Text style={st.org}>in {orgDisplayName(install.org_name)}</Text> : null}
          <Text style={st.intro}>Connect Salesforce to sync your people and push meetings.</Text>
          {error ? <ErrorText>{error}</ErrorText> : null}
          <Button label="Connect Salesforce" onPress={() => { void connect(); }} loading={busy === "connect"}
            disabled={busy !== ""} style={{ marginTop: S.xl }} />
        </>
      ) : status === "waiting" ? (
        <>
          <Text style={st.title}>Finish in Salesforce</Text>
          <View style={st.codeBox}>
            <Text style={st.codeLabel}>Your setup code</Text>
            <Text style={st.code} selectable accessibilityLabel={`Setup code ${install.setup_code || ""}`}>
              {install.setup_code}
            </Text>
          </View>
          <Text style={st.step}>1. Install MinuteX on the Salesforce page that opened.</Text>
          <Text style={st.step}>2. In Salesforce, open MinuteX Setup and follow its steps.</Text>
          <Text style={st.step}>3. Enter this setup code there and tap Connect.</Text>
          {error ? <ErrorText>{error}</ErrorText> : null}
          <Text style={st.waiting}>Waiting for Salesforce…</Text>
          <Button label="Open Salesforce again" variant="secondary" onPress={() => { void openInstallPage(install.install_url); }}
            loading={busy === "open"} disabled={busy !== ""} style={{ marginTop: S.lg }} />
          <Button label="Start over" variant="ghost" onPress={() => { void start(); }}
            disabled={busy !== ""} style={{ marginTop: S.sm }} />
          {openedRef.current ? (
            <Text style={st.help}>
              Installation didn&apos;t finish? Open Salesforce again. If Salesforce shows an error, sign in
              as a Salesforce admin and try once more.
            </Text>
          ) : null}
        </>
      ) : (
        <>
          <Text style={st.title}>Install MinuteX in Salesforce</Text>
          <Text style={st.intro}>
            Sync your Salesforce people and push meetings straight to your Salesforce records. You&apos;ll
            install MinuteX in your Salesforce org, then come back here.
          </Text>
          {status === "expired" ? <ErrorText>Your last setup code expired. Start again.</ErrorText> : null}
          {error ? <ErrorText>{error}</ErrorText> : null}
          <View style={st.row}>
            <Text style={st.rowLabel}>My Salesforce org is a sandbox</Text>
            <Switch value={sandbox} onValueChange={setSandbox} disabled={busy !== ""}
              accessibilityLabel="My Salesforce org is a sandbox" />
          </View>
          <Button label="Install MinuteX in Salesforce" onPress={() => { void start(); }}
            loading={busy === "start" || busy === "open"} disabled={busy !== ""} style={{ marginTop: S.xl }} />
          <Text style={st.help}>You&apos;ll need to sign in to Salesforce as an admin.</Text>
        </>
      )}
    </ScrollView>
  );
}
