// src/app/organisation/join.tsx — accept an organisation invitation.
//
// Uses the EXISTING invitation system (Phase 2B): the token is validated
// server-side against its stored hash, its expiry, its single-use status and
// the invited email. Nothing about that is re-implemented here — this screen
// only carries the token to the API and renders what comes back.
//
// THE TOKEN IS NEVER PERSISTED. It lives in component state for the length of
// one attempt and goes nowhere else.
//
// The identity rule surfaces here as `organisation_email_conflict`: the
// invited address belongs to a personal account, so it cannot join. That is
// guidance with a real next step (sign up with the work address), not a
// failure, and it is detected by CODE — never by matching message text.
import { useCallback, useMemo, useState } from "react";
import { ScrollView, StyleSheet, Text, View } from "react-native";
import { Stack, useLocalSearchParams, useRouter } from "expo-router";
import { S, FONT, useTheme, ColorScale } from "../../../lib/theme";
import {
  Button, Card, ErrorText, SectionRule, TextField, scrollFormProps,
} from "../../../lib/ui";
import {
  ApiError, acceptInvitation, isOrganisationEmailConflict,
} from "../../../lib/api";
import { useWorkspace } from "../../../lib/workspace-context";

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    screen: { flex: 1, backgroundColor: C.bg },
    body: { padding: S.lg, paddingBottom: S.xxl },
    intro: {
      fontFamily: FONT.regular, fontSize: 13.5, lineHeight: 20,
      color: C.textDim, marginBottom: S.lg,
    },
    label: {
      fontFamily: FONT.semibold, fontSize: 12, color: C.textDim,
      marginTop: S.md, marginBottom: 6,
    },
    notice: {
      backgroundColor: C.primarySoft, borderRadius: 10,
      padding: 14, marginTop: S.md,
    },
    noticeText: {
      fontFamily: FONT.regular, fontSize: 13, lineHeight: 19, color: C.text,
    },
  });
}

/** The token out of a pasted invitation link, or the raw value if it is
 *  already a bare token. Accepting a full URL is a courtesy: people paste
 *  what their mail client gave them. */
function extractToken(raw: string): string {
  const value = String(raw || "").trim();
  if (!value) return "";
  const match = value.match(/workspace-invitations\/([A-Za-z0-9_-]+)/);
  if (match) return match[1];
  // A pasted link with the token in a query parameter.
  const qs = value.match(/[?&](?:token|invite)=([A-Za-z0-9_-]+)/);
  if (qs) return qs[1];
  return value;
}

export default function JoinOrganisationScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { refresh, switchTo } = useWorkspace();

  // A deep link may bring the token in with it.
  const { token: tokenParam } = useLocalSearchParams<{ token?: string }>();
  const [raw, setRaw] = useState(String(tokenParam || ""));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const submit = useCallback(async () => {
    setError("");
    setNotice("");
    const token = extractToken(raw);
    if (!token) {
      setError("Paste the invitation link or code.");
      return;
    }
    setBusy(true);
    try {
      const res = await acceptInvitation(token);
      await refresh();
      await switchTo(res.workspace.workspace_id);
      router.replace("/organisation");
    } catch (e: any) {
      if (isOrganisationEmailConflict(e)) {
        // The invited address is a personal account. Explain, don't fail.
        setNotice(e instanceof ApiError ? e.message : "");
      } else if (e instanceof ApiError && e.status === 403) {
        // Signed in as somebody other than the invited person.
        setError(e.message);
      } else if (e instanceof ApiError && e.status === 404) {
        // One answer for unknown / expired / cancelled / already-used, which
        // is what the backend deliberately returns — restating it here is
        // honest, and guessing which one it was would not be.
        setError("This invitation is no longer valid. Ask your "
                 + "administrator to send a new one.");
      } else {
        setError(e?.message || "Could not accept the invitation.");
      }
    } finally {
      setBusy(false);
    }
  }, [raw, refresh, switchTo, router]);

  return (
    <View style={st.screen}>
      <Stack.Screen options={{ title: "Join organisation" }} />
      <ScrollView contentContainerStyle={st.body} {...scrollFormProps}>
        <Text style={st.intro}>
          Paste the invitation link your administrator sent you. You must be
          signed in as the invited email address.
        </Text>

        <Card>
          <SectionRule>Invitation</SectionRule>
          <Text style={st.label}>Link or code</Text>
          <TextField
            value={raw}
            onChangeText={setRaw}
            placeholder="https://… or the code itself"
            autoCapitalize="none"
            autoCorrect={false}
            editable={!busy}
          />
        </Card>

        {notice ? (
          <View style={st.notice}>
            <Text style={st.noticeText}>{notice}</Text>
          </View>
        ) : null}
        {error ? <ErrorText>{error}</ErrorText> : null}

        <View style={{ marginTop: S.xl }}>
          <Button
            label={busy ? "Joining…" : "Join organisation"}
            onPress={submit}
            disabled={busy || !raw.trim()}
          />
        </View>
      </ScrollView>
    </View>
  );
}
