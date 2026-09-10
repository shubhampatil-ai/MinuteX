// src/app/onboarding/name.tsx — the one-time "who are you?" gate.
//
// WHY THIS EXISTS. `name` was optional on every account created before this
// screen, so most Users rows carry name: "". A members list, a task assignee
// and a meeting's "recorded by" all need a human label, and with no name the
// only thing left to render is the user_id — a uuid, which is an identifier,
// not a name. The server now DERIVES a display name from the email local-part
// so a uuid never reaches the UI (workspace_schema.display_name), but a guess
// is a fallback, not an answer: "jsmith2@..." derives nothing useful, and only
// the person themselves knows how their name should read.
//
// So this screen is a GATE, not a suggestion. It has no skip and no back:
// _layout routes here whenever the signed-in user has no name set, and the
// only way out is to save one. The AVATAR is genuinely optional — a photo has
// an initials fallback that works, a name does not.
//
// Everything here is existing machinery: PhotoPicker + uploadAvatar for the
// image, updateMe for the write. Nothing about the profile flow is duplicated.
import { useCallback, useEffect, useMemo, useState } from "react";
import { ScrollView, StyleSheet, Text, View } from "react-native";
import { Stack, useRouter } from "expo-router";
import { S, FONT, useTheme, ColorScale } from "../../../lib/theme";
import {
  Avatar, Button, ErrorText, KeyboardAware, TextField, scrollFormProps,
} from "../../../lib/ui";
import { PhotoPicker } from "../../../lib/photo-picker";
import { canPickImage } from "../../../lib/avatars";
import { getMe, updateMe, UserProfile } from "../../../lib/api";
import { notifyProfileNameSaved } from "../../../lib/profile-gate";

// Matches the server's own clamp in patch_me ([:100]), so the field cannot
// accept text the backend would silently truncate.
const MAX_NAME = 100;

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    screen: { flex: 1, backgroundColor: C.bg },
    body: { padding: S.lg, paddingTop: S.xxl, paddingBottom: S.xxl },
    title: {
      fontFamily: FONT.semibold, fontSize: 26, color: C.text,
      marginBottom: S.sm,
    },
    blurb: {
      fontFamily: FONT.regular, fontSize: 14.5, lineHeight: 21,
      color: C.textDim, marginBottom: S.xl,
    },
    photoWrap: { alignItems: "center", marginBottom: S.xl },
    photoHint: {
      fontFamily: FONT.regular, fontSize: 12.5, color: C.textDim,
      marginTop: S.sm,
    },
    label: {
      fontFamily: FONT.semibold, fontSize: 12, color: C.textDim,
      marginBottom: 6,
    },
    actions: { marginTop: S.xl },
  });
}

export default function OnboardingNameScreen() {
  const router = useRouter();
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);

  const [me, setMe] = useState<UserProfile | null>(null);
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [photoOpen, setPhotoOpen] = useState(false);

  // Seed the field with the server's DERIVED name rather than an empty box.
  // For "shubham.patil@..." that is already "Shubham Patil", so the common
  // case is a single tap on Continue — a gate the user can clear instantly is
  // a gate they don't resent. It is still a real edit: nothing was written to
  // their row, so whatever they leave here is what actually gets saved.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const u = await getMe();
        if (cancelled) return;
        setMe(u);
        setName((u.name || u.suggested_name || "").trim());
      } catch {
        // A profile read failure must not trap the user on a screen with
        // nothing in it — they can still type a name and save.
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const savePhoto = useCallback(async (avatarKey: string) => {
    setPhotoOpen(false);
    try {
      const u = await updateMe({ avatar_url: avatarKey });
      setMe(u);
    } catch (e: any) {
      setError(e?.message || "Could not save the photo.");
    }
  }, []);

  const submit = useCallback(async () => {
    const clean = name.trim();
    if (clean.length < 2) {
      setError("Enter your name so your team can recognise you.");
      return;
    }
    setError("");
    setSaving(true);
    try {
      await updateMe({ name: clean });
      // Tell the gate BEFORE navigating. The gate asks /me once per sign-in,
      // so without this it still believes this user is nameless and redirects
      // straight back here — which is why saving used to appear to do nothing
      // until the app was killed and relaunched.
      notifyProfileNameSaved();
      // replace, not push: this screen must not sit in the back stack, or the
      // gesture back from Home would land on a gate that is already satisfied.
      router.replace("/");
    } catch (e: any) {
      setError(e?.message || "Could not save your name.");
      setSaving(false);
    }
  }, [name, router]);

  return (
    <KeyboardAware style={st.screen}>
      {/* No back button and no gesture: the only exit is saving a name. */}
      <Stack.Screen
        options={{
          title: "",
          headerShown: false,
          gestureEnabled: false,
        }}
      />
      <ScrollView {...scrollFormProps} contentContainerStyle={st.body}>
        <Text style={st.title}>What should we call you?</Text>
        <Text style={st.blurb}>
          Your name appears on the meetings you record, the tasks you're
          assigned, and to everyone in your organisation. You can change it
          any time from your profile.
        </Text>

        <View style={st.photoWrap}>
          <Avatar
            name={name || me?.email || ""}
            photoUri={me?.avatar_view_url}
            size={88}
          />
          {canPickImage() ? (
            <>
              <Button
                label={me?.avatar_url ? "Change photo" : "Add a photo"}
                variant="secondary"
                onPress={() => setPhotoOpen(true)}
              />
              <Text style={st.photoHint}>Optional</Text>
            </>
          ) : null}
        </View>

        <Text style={st.label}>Your name</Text>
        <TextField
          value={name}
          onChangeText={(t) => setName(t.slice(0, MAX_NAME))}
          placeholder="e.g. Priya Sharma"
          autoCapitalize="words"
          autoCorrect={false}
          editable={!saving}
          onSubmitEditing={submit}
          returnKeyType="done"
          autoFocus
        />

        {error ? <ErrorText>{error}</ErrorText> : null}

        <View style={st.actions}>
          <Button
            label={saving ? "Saving…" : "Continue"}
            onPress={submit}
            disabled={saving || name.trim().length < 2}
          />
        </View>
      </ScrollView>

      <PhotoPicker
        visible={photoOpen}
        onClose={() => setPhotoOpen(false)}
        name={name || me?.email || ""}
        currentPhotoUri={me?.avatar_view_url}
        scope="user"
        onPicked={savePhoto}
        canRemove={!!me?.avatar_url}
      />
    </KeyboardAware>
  );
}
