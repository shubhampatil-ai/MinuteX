// lib/auth-ui.tsx — pieces shared by the signed-out screens (login, and the
// forgot-password reset): the boxed field, the password strength meter, and
// the one sequence every successful sign-in must run before landing.
import { useCallback, useMemo, useState } from "react";
import { Pressable, StyleSheet, Text, TextInput, TextInputProps, View } from "react-native";
import { useRouter } from "expo-router";
import { Icon } from "./icons";
import { FONT, R, S, useTheme, ColorScale } from "./theme";
import { restoreWorkspaceFor } from "./api";
import { useWorkspace } from "./workspace-context";

export const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// ---- password strength: 0..4 (length, case mix, digit, symbol) ----
export function strengthOf(pw: string): number {
  if (!pw) return 0;
  let s = 0;
  if (pw.length >= 8) s++;
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) s++;
  if (/\d/.test(pw)) s++;
  if (/[^a-zA-Z0-9]/.test(pw)) s++;
  return s;
}
const STRENGTH_LABEL = ["", "Weak", "Fair", "Good", "Strong"];

function buildStyles(C: ColorScale) {
  return StyleSheet.create({
    fieldWrap: {
      flexDirection: "row", alignItems: "center", gap: 10,
      backgroundColor: C.surface2, borderRadius: R.md, borderWidth: 1, borderColor: C.border,
      paddingVertical: 13, paddingHorizontal: 14,
    },
    fieldWrapError: { borderColor: C.danger },
    fieldInput: { flex: 1, color: C.text, fontFamily: FONT.regular, fontSize: 15.5, paddingVertical: 0 },
    fieldError: { fontFamily: FONT.regular, fontSize: 12.5, color: C.danger, marginTop: 5, marginLeft: 2 },
    strengthRow: { flexDirection: "row", alignItems: "center", gap: S.sm, marginTop: 2, marginLeft: 2 },
    strengthTrack: { flexDirection: "row", gap: 4, flex: 1 },
    strengthSeg: { flex: 1, height: 3, borderRadius: 2, backgroundColor: C.border },
    strengthLabel: { fontFamily: FONT.bold, fontSize: 12, width: 48, textAlign: "right" },
  });
}

// ---- AuthField: input with leading icon, optional eye toggle, inline error ----
export function AuthField({
  icon, error, secure, ...rest
}: TextInputProps & { icon: string; error?: string; secure?: boolean }) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const [hidden, setHidden] = useState(!!secure);
  return (
    <View>
      <View style={[st.fieldWrap, !!error && st.fieldWrapError]}>
        <Icon name={icon as any} tintColor={error ? C.danger : C.textFaint} size={18} />
        <TextInput
          style={st.fieldInput}
          placeholderTextColor={C.textFaint}
          secureTextEntry={hidden}
          {...rest}
        />
        {secure ? (
          <Pressable onPress={() => setHidden(!hidden)} hitSlop={10}
            accessibilityLabel={hidden ? "Show password" : "Hide password"}>
            <Icon name={hidden ? "eye.slash" : "eye"} tintColor={C.textFaint} size={18} />
          </Pressable>
        ) : null}
      </View>
      {error ? <Text style={st.fieldError}>{error}</Text> : null}
    </View>
  );
}

export function StrengthMeter({ password }: { password: string }) {
  const { C } = useTheme();
  const st = useMemo(() => buildStyles(C), [C]);
  const strength = strengthOf(password);
  // Weak -> danger, Fair -> warn, Good -> primary, Strong -> success.
  const color = [C.danger, C.danger, C.warn, C.primary, C.success][strength] ?? C.danger;
  return (
    <View style={st.strengthRow}>
      <View style={st.strengthTrack}>
        {[1, 2, 3, 4].map((i) => (
          <View key={i} style={[st.strengthSeg, i <= strength && { backgroundColor: color }]} />
        ))}
      </View>
      <Text style={[st.strengthLabel, { color }]}>{STRENGTH_LABEL[strength]}</Text>
    </View>
  );
}

/** What every successful sign-in runs once the token is saved.
 *
 * WorkspaceProvider sits ABOVE the auth gate, so its one-shot mount fetch
 * already ran while signed out and found no token. Nothing re-runs it on its
 * own, so the list has to be pulled again HERE — otherwise the user lands on
 * the tabs with an empty workspace list and their organisation missing.
 */
export function useFinishSignIn() {
  const { refresh: refreshWorkspaces } = useWorkspace();
  const router = useRouter();
  return useCallback(async (userId: string) => {
    // Put them back in the workspace they signed out of. Done BEFORE the
    // list is fetched so the very first request is already scoped there,
    // and it is only a hint — the provider re-validates it against the
    // fresh list and falls back to Personal if the membership is gone.
    await restoreWorkspaceFor(userId);
    // Load the workspaces BEFORE navigating: the tabs render against this
    // list, and arriving with a stale empty one is what made an owner's
    // organisation disappear. Non-fatal — refresh() swallows its own errors.
    await refreshWorkspaces();
    // Replace so the back button doesn't return to a signed-out screen.
    router.replace("/");
  }, [refreshWorkspaces, router]);
}
