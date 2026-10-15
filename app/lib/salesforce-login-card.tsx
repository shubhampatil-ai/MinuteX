// lib/salesforce-login-card.tsx — "Connect with Salesforce login": the
// admin signs in to Salesforce AS THE INTEGRATION USER and approves MinuteX.
// The backend keeps that user's refresh token (KMS-encrypted) and never
// shows it. This is how the MinuteX package will be reached; until the
// package ships it sits beside the address + signing-secret connection.
//
// BEHIND A FLAG: off unless the build sets EXPO_PUBLIC_SALESFORCE_OAUTH=1.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import * as WebBrowser from "expo-web-browser";
import * as Linking from "expo-linking";
import { copyText } from "./clipboard";
import {
  SalesforceOrgConnection, disconnectSalesforceOrg, getSalesforceOrgConnection,
  setupSalesforceReceiver, startSalesforceOrgConnect,
} from "./api";
import { integrationReturnUrl } from "./integrations";
import { FONT, R, S, useTheme, ColorScale } from "./theme";
import { Button, Card, ErrorText, SuccessText, TextField } from "./ui";
import { webhookError } from "./webhook-ui";

// Defined in webhook-ui (the Integrations card reads it too); re-exported so
// the screens keep importing it from here.
export { SALESFORCE_OAUTH_ENABLED } from "./webhook-ui";

export type SalesforceEnvironment = "production" | "sandbox" | "my_domain";

const ENVIRONMENTS: { key: SalesforceEnvironment; label: string; hint: string }[] = [
  { key: "production", label: "Production", hint: "login.salesforce.com" },
  { key: "sandbox", label: "Sandbox", hint: "test.salesforce.com" },
  { key: "my_domain", label: "My Domain", hint: "your-company.my.salesforce.com" },
];

// The backend's ?reason= codes (salesforce_oauth_callback) as a person reads them.
const REASON_COPY: Record<string, string> = {
  denied: "Access was declined in Salesforce.",
  missing_params: "Salesforce didn't return a valid response. Try again.",
  expired: "That sign-in took too long. Try again.",
  not_allowed: "Only an owner or manager of this workspace can connect Salesforce.",
  no_refresh_token: "Salesforce didn't allow a lasting connection. Check that the MinuteX app in Salesforce allows refresh tokens.",
  exchange_failed: "Couldn't finish connecting to Salesforce. Try again.",
  failed: "Couldn't finish connecting to Salesforce. Try again.",
  reused: "That sign-in was already used. If you're connected, there's nothing to do; otherwise tap Connect again.",
};

// One-click setup's outcome (after the login, or "Finish setup").
export const SETUP_COPY: Record<string, string> = {
  ready: "Salesforce is connected and ready. Meetings can be linked and sent.",
  no_receiver: "The MinuteX package isn't installed in this Salesforce org yet. Install it, then tap Finish setup.",
  not_allowed: "The Salesforce user you signed in with needs the MinuteX Integration permission set. Assign it in Salesforce, then tap Finish setup.",
  manual: "This org has an older MinuteX receiver. Update the MinuteX package in Salesforce, then tap Finish setup.",
  failed: "MinuteX couldn't finish setting up Salesforce. Tap Finish setup to try again.",
};

/** The outcome in the app's return link, as a person reads it. */
export function connectOutcome(url: string): { ok: boolean; message: string } {
  const { queryParams } = Linking.parse(url);
  if (queryParams?.connected === "1") {
    const setup = typeof queryParams?.setup === "string" ? queryParams.setup : "";
    if (!setup || setup === "ready") return { ok: true, message: setup ? SETUP_COPY.ready : "Salesforce connected." };
    return { ok: false, message: `Signed in to Salesforce. ${SETUP_COPY[setup] ?? SETUP_COPY.failed}` };
  }
  const reason = typeof queryParams?.reason === "string" ? queryParams.reason : "";
  return { ok: false, message: REASON_COPY[reason] ?? REASON_COPY.failed };
}

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    heading: { fontFamily: FONT.bold, fontSize: 15, color: C.text },
    help: { fontFamily: FONT.regular, fontSize: 12.5, lineHeight: 18, color: C.textDim, marginTop: S.xs },
    row: { flexDirection: "row", gap: S.sm, flexWrap: "wrap", marginTop: S.md },
    install: { paddingBottom: S.lg, marginBottom: S.lg, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: C.border },
    pill: { borderWidth: 1, borderColor: C.border, borderRadius: R.pill, paddingHorizontal: S.md,
            paddingVertical: 8, minHeight: 40, justifyContent: "center" },
    pillOn: { borderColor: C.primary, backgroundColor: C.primarySoft },
    pillTxt: { fontFamily: FONT.semibold, fontSize: 13, color: C.text },
    kv: { fontFamily: FONT.regular, fontSize: 13, color: C.textDim, marginTop: 2 },
    ok: { fontFamily: FONT.bold, fontSize: 14, color: C.success, marginTop: S.md },
    warn: { fontFamily: FONT.bold, fontSize: 14, color: C.warn, marginTop: S.md },
  });
}

export function SalesforceLoginCard({ workspaceId, admin, receiverReady, onChanged }: {
  workspaceId: string;
  admin: boolean;
  /** Is the MinuteX receiver (address + signing secret) set up? undefined =
   *  not known yet. The login alone can't link or send anything. */
  receiverReady?: boolean;
  /** After connect, disconnect or setup: the screen re-reads the receiver. */
  onChanged?: () => void;
}) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const [conn, setConn] = useState<SalesforceOrgConnection | null>(null);
  const [env, setEnv] = useState<SalesforceEnvironment>("production");
  const [myDomain, setMyDomain] = useState("");
  const [busy, setBusy] = useState<"" | "load" | "connect" | "disconnect" | "setup">("load");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    try {
      setConn(await getSalesforceOrgConnection(workspaceId));
      setError("");
    } catch (e) {
      setError(webhookError(e));
    } finally {
      setBusy("");
    }
  }, [workspaceId]);

  useEffect(() => { void load(); }, [load]);

  async function connect() {
    setBusy("connect");
    setError("");
    setNotice("");
    try {
      const url = await startSalesforceOrgConnect(workspaceId, env, env === "my_domain" ? myDomain : "");
      const result = await WebBrowser.openAuthSessionAsync(url, integrationReturnUrl());
      if (result.type === "success") {
        const out = connectOutcome(result.url);
        if (out.ok) setNotice(out.message); else setError(out.message);
      }
      await load();
      onChanged?.();
    } catch (e) {
      setError(webhookError(e));
      setBusy("");
    }
  }

  async function finishSetup() {
    setBusy("setup");
    setError("");
    setNotice("");
    try {
      const r = await setupSalesforceReceiver(workspaceId);
      if (r.status === "ready") setNotice(SETUP_COPY.ready);
      else setError(SETUP_COPY[r.status] ?? SETUP_COPY.failed);
      onChanged?.();
    } catch (e) {
      setError(webhookError(e));
    } finally {
      setBusy("");
    }
  }

  async function disconnect() {
    setBusy("disconnect");
    try {
      await disconnectSalesforceOrg(workspaceId);
      setNotice("Salesforce disconnected.");
      await load();
    } catch (e) {
      setError(webhookError(e));
      setBusy("");
    }
  }

  const connected = !!conn?.connected;
  const reconnect = conn?.status === "reconnect_needed";

  const install = conn?.install ?? null;

  return (
    <Card>
      {/* Step 1 for a new org: the MinuteX package, from Salesforce's own
          install page. Owners and managers only; hidden when no version is
          configured on the server. */}
      {admin && install ? (
        <View style={st.install}>
          <Text style={st.heading}>Install MinuteX in Salesforce</Text>
          <Text style={st.help}>
            A Salesforce admin installs the MinuteX package once, choosing "Install for Admins
            Only", then creates the MinuteX integration user.
          </Text>
          {!install.released ? (
            <Text style={st.help}>
              This version is a beta: it installs in sandboxes and Developer Edition orgs only.
            </Text>
          ) : null}
          {/* The phone's own browser, NOT the in-app one: on Android the
              in-app browser shares the Salesforce sign-in tab and re-showed
              the earlier "Allow Access?" page instead of the install page. */}
          <View style={st.row}>
            {install.released ? (
              <Button label="Install in production" variant="secondary"
                onPress={() => { void Linking.openURL(install.production_url); }} />
            ) : null}
            <Button label="Install in a sandbox" variant="secondary"
              onPress={() => { void Linking.openURL(install.sandbox_url); }} />
            {!install.released ? (
              <Button label="Install in a Developer Edition org" variant="secondary"
                onPress={() => { void Linking.openURL(install.production_url); }} />
            ) : null}
            <Button label="Copy install link" variant="ghost"
              onPress={() => {
                void copyText(install.production_url).then((r) => {
                  if (r === "copied") setNotice("Install link copied. Open it in a browser on any device.");
                });
              }} />
          </View>
        </View>
      ) : null}
      <Text style={st.heading}>Salesforce login</Text>
      <Text style={st.help}>
        Sign in as your company&apos;s MinuteX integration user. MinuteX keeps a secure, lasting
        connection and never shows the password or tokens.
      </Text>
      {error ? <ErrorText>{error}</ErrorText> : null}
      {notice ? <SuccessText>{notice}</SuccessText> : null}

      {connected ? (
        <>
          <Text style={st.ok}>✓ Connected</Text>
          <Text style={st.kv}>{conn?.instance_host}</Text>
          {conn?.connected_at ? <Text style={st.kv}>Since {conn.connected_at.slice(0, 10)}</Text> : null}
          {receiverReady === false ? (
            <>
              <Text style={st.warn}>
                Setup isn&apos;t finished, so meetings can&apos;t be linked or sent yet.
              </Text>
              {admin ? (
                <Button label="Finish setup" onPress={() => { void finishSetup(); }}
                  loading={busy === "setup"} disabled={!!busy} style={{ marginTop: S.md }} />
              ) : null}
            </>
          ) : null}
        </>
      ) : reconnect ? (
        <>
          <Text style={st.warn}>Connect again</Text>
          <Text style={st.kv}>{conn?.status_message || "Salesforce no longer accepts MinuteX's sign-in."}</Text>
        </>
      ) : null}

      {admin ? (
        connected ? (
          <Button label="Disconnect Salesforce login" variant="secondary" onPress={() => { void disconnect(); }}
            loading={busy === "disconnect"} disabled={!!busy} style={{ marginTop: S.md }} />
        ) : (
          <>
            <View style={st.row}>
              {ENVIRONMENTS.map((e) => (
                <Pressable key={e.key} style={[st.pill, env === e.key && st.pillOn]}
                  onPress={() => setEnv(e.key)} accessibilityRole="radio"
                  accessibilityState={{ selected: env === e.key }}
                  accessibilityLabel={`${e.label}, ${e.hint}`}>
                  <Text style={st.pillTxt}>{e.label}</Text>
                </Pressable>
              ))}
            </View>
            {env === "my_domain" ? (
              <TextField value={myDomain} onChangeText={setMyDomain} accessibilityLabel="My Domain"
                placeholder="your-company.my.salesforce.com" autoCapitalize="none"
                autoCorrect={false} keyboardType="url" style={{ marginTop: S.md }} />
            ) : null}
            <Button label={reconnect ? "Connect again" : "Connect with Salesforce login"}
              onPress={() => { void connect(); }} loading={busy === "connect"}
              disabled={!!busy || (env === "my_domain" && !myDomain.trim())} style={{ marginTop: S.md }} />
          </>
        )
      ) : !connected ? (
        <Text style={st.help}>An owner or manager of this workspace connects Salesforce.</Text>
      ) : null}
    </Card>
  );
}
