// src/app/organisation/create.tsx — create an Organisation workspace.
//
// The creator becomes OWNER immediately (backend does this atomically with
// the workspace row), and the app switches into the new organisation on
// success so the next thing they do lands in the right place.
//
// THE IDENTITY RULE surfaces here. An account that already has personal
// meetings, tasks or contacts cannot also become an organisation owner —
// converting a personal account is exactly what the identity model forbids.
// The backend answers 409 with code `personal_workspace_in_use`, and this
// screen renders that as guidance rather than as a failure, because the user
// has a real next step: sign up with a work address.
import { useCallback, useMemo, useState } from "react";
import { ScrollView, StyleSheet, Text, View } from "react-native";
import { Stack, useRouter } from "expo-router";
import { S, FONT, useTheme, ColorScale } from "../../../lib/theme";
import {
  Button, Card, ErrorText, SectionRule, TextField, scrollFormProps,
} from "../../../lib/ui";
import {
  ApiError, createOrganisation, isPersonalWorkspaceInUse,
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
    optional: {
      fontFamily: FONT.regular, fontSize: 11.5, color: C.textFaint,
      marginTop: 4,
    },
    // The identity-conflict explanation: guidance, not an error banner.
    conflict: {
      backgroundColor: C.primarySoft, borderRadius: 10,
      padding: 14, marginTop: S.md,
    },
    conflictText: {
      fontFamily: FONT.regular, fontSize: 13, lineHeight: 19, color: C.text,
    },
  });
}

export default function CreateOrganisationScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const { refresh, switchTo } = useWorkspace();

  const [name, setName] = useState("");
  const [companyEmail, setCompanyEmail] = useState("");
  const [domain, setDomain] = useState("");
  const [phone, setPhone] = useState("");
  const [address, setAddress] = useState("");
  const [industry, setIndustry] = useState("");

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState("");

  const submit = useCallback(async () => {
    setError("");
    setConflict("");
    if (!name.trim()) {
      setError("Give the organisation a name.");
      return;
    }
    setBusy(true);
    try {
      const { workspace } = await createOrganisation({
        name: name.trim(),
        company_email: companyEmail.trim() || undefined,
        domain: domain.trim() || undefined,
        phone: phone.trim() || undefined,
        address: address.trim() || undefined,
        industry: industry.trim() || undefined,
      });
      // Re-read the list, then switch into it: the user's next action almost
      // always belongs to the organisation they just made.
      await refresh();
      await switchTo(workspace.workspace_id);
      router.replace("/organisation");
    } catch (e: any) {
      // Branch on the CODE, never on the message text.
      if (isPersonalWorkspaceInUse(e)) {
        setConflict(e instanceof ApiError ? e.message : "");
      } else {
        setError(e?.message || "Could not create the organisation.");
      }
    } finally {
      setBusy(false);
    }
  }, [name, companyEmail, domain, phone, address, industry,
      refresh, switchTo, router]);

  return (
    <View style={st.screen}>
      <Stack.Screen options={{ title: "Create organisation" }} />
      <ScrollView contentContainerStyle={st.body} {...scrollFormProps}>
        <Text style={st.intro}>
          An organisation shares meetings, tasks and contacts with your team.
          You'll be its owner and can invite people once it exists.
        </Text>

        <Card>
          <SectionRule>Organisation</SectionRule>
          <Text style={st.label}>Name</Text>
          <TextField
            value={name}
            onChangeText={setName}
            placeholder="ABC Realty"
            autoCapitalize="words"
            editable={!busy}
          />

          <Text style={st.label}>Work email</Text>
          <TextField
            value={companyEmail}
            onChangeText={setCompanyEmail}
            placeholder="hello@abcrealty.com"
            autoCapitalize="none"
            keyboardType="email-address"
            editable={!busy}
          />

          <Text style={st.label}>Domain</Text>
          <TextField
            value={domain}
            onChangeText={setDomain}
            placeholder="abcrealty.com"
            autoCapitalize="none"
            editable={!busy}
          />
          <Text style={st.optional}>
            Shown on the organisation profile. It does not grant anyone access.
          </Text>

          <Text style={st.label}>Industry</Text>
          <TextField
            value={industry}
            onChangeText={setIndustry}
            placeholder="Real estate"
            editable={!busy}
          />

          <Text style={st.label}>Phone</Text>
          <TextField
            value={phone}
            onChangeText={setPhone}
            placeholder="+91 98765 43210"
            keyboardType="phone-pad"
            editable={!busy}
          />

          <Text style={st.label}>Address</Text>
          <TextField
            value={address}
            onChangeText={setAddress}
            placeholder="Optional"
            multiline
            editable={!busy}
          />
        </Card>

        {conflict ? (
          <View style={st.conflict}>
            <Text style={st.conflictText}>{conflict}</Text>
          </View>
        ) : null}
        {error ? <ErrorText>{error}</ErrorText> : null}

        <View style={{ marginTop: S.xl }}>
          <Button
            label={busy ? "Creating…" : "Create organisation"}
            onPress={submit}
            disabled={busy || !name.trim()}
          />
        </View>
      </ScrollView>
    </View>
  );
}
