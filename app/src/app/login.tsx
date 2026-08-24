// src/app/login.tsx — MinuteX auth (login + signup), "Workspace" edition.
//
// The entry moment is a clean masthead: a caps company kicker, the wordmark
// in bold sans, then the promise stated in a confident sentence. Fields are
// rounded boxes inside a soft card, not underlined rules. Email/password
// against the userApi backend; JWT stored via lib/api. Per-field validation,
// show/hide password, signup confirm + strength meter, remember-me (prefills
// the email — the session token itself is always secure-stored), and a
// Google button that is honestly labeled Coming Soon (no backend yet).
import { useEffect, useMemo, useRef, useState } from "react";
import {
  Animated, Easing, KeyboardAvoidingView, Modal, Platform, Pressable,
  ScrollView, StyleSheet, Text, TextInput, TextInputProps, View,
} from "react-native";
import { Icon } from "../../lib/icons";
import { useRouter } from "expo-router";
import { APP_NAME, COMPANY, TAGLINE, R, S, CAPS, FONT, ELEV, useTheme, ColorScale } from "../../lib/theme";
import { Button, ErrorText, GoogleButton } from "../../lib/ui";
import { login, signup, ApiError } from "../../lib/api";
import { store } from "../../lib/storage";

type Mode = "login" | "signup";

const REMEMBER_KEY = "minutex.remember_email";
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// ---- password strength: 0..4 (length, case mix, digit, symbol) ----
function strengthOf(pw: string): number {
  if (!pw) return 0;
  let s = 0;
  if (pw.length >= 8) s++;
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) s++;
  if (/\d/.test(pw)) s++;
  if (/[^a-zA-Z0-9]/.test(pw)) s++;
  return s;
}
const STRENGTH_LABEL = ["", "Weak", "Fair", "Good", "Strong"];

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    scroll: { flexGrow: 1, justifyContent: "center" as const, paddingHorizontal: 26, paddingVertical: 48 },
    // Masthead — plain kicker + bold wordmark, no rule.
    brandWrap: { paddingBottom: 4 },
    kicker: { ...CAPS, letterSpacing: 2.6, color: C.textFaint },
    brand: { fontFamily: FONT.extrabold, fontSize: 40, lineHeight: 44, color: C.text, marginTop: 6 },
    // The promise — the one line the product actually delivers on.
    promise: { ...T.headlineSm, marginTop: 24 },
    tagline: { ...T.bodyDim, marginTop: 10 },
    h1: { ...T.h2, marginTop: 28 },
    sub: { ...T.bodyDim, marginTop: S.sm },
    // Fields live inside a soft card, boxed not ruled.
    fields: {
      marginTop: 28, backgroundColor: C.surface, borderRadius: R.card, borderWidth: 1,
      borderColor: C.border, padding: S.lg, gap: S.md, shadowColor: C.shadow, ...ELEV.sm,
    },
    fieldWrap: {
      flexDirection: "row" as const, alignItems: "center" as const, gap: 10,
      backgroundColor: C.surface2, borderRadius: R.md, borderWidth: 1, borderColor: C.border,
      paddingVertical: 13, paddingHorizontal: 14,
    },
    fieldWrapError: { borderColor: C.danger },
    fieldInput: { flex: 1, color: C.text, fontFamily: FONT.regular, fontSize: 15.5, paddingVertical: 0 },
    fieldError: { fontFamily: FONT.regular, fontSize: 12.5, color: C.danger, marginTop: 5, marginLeft: 2 },
    // strength
    strengthRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: S.sm, marginTop: 2, marginLeft: 2 },
    strengthTrack: { flexDirection: "row" as const, gap: 4, flex: 1 },
    strengthSeg: { flex: 1, height: 3, borderRadius: 2, backgroundColor: C.border },
    strengthLabel: { fontFamily: FONT.bold, fontSize: 12, width: 48, textAlign: "right" as const },
    // remember / forgot row
    metaRow: { flexDirection: "row" as const, alignItems: "center" as const, justifyContent: "space-between" as const, marginTop: 16 },
    remember: { flexDirection: "row" as const, alignItems: "center" as const, gap: 8 },
    checkbox: {
      width: 18, height: 18, borderRadius: 5, borderWidth: 1.5, borderColor: C.border,
      alignItems: "center" as const, justifyContent: "center" as const,
    },
    checkboxOn: { backgroundColor: C.primary, borderColor: C.primary },
    rememberTxt: { fontFamily: FONT.regular, fontSize: 12.5, color: C.textDim },
    forgot: { fontFamily: FONT.semibold, fontSize: 12.5, color: C.primary },
    // divider
    orRow: { flexDirection: "row" as const, alignItems: "center" as const, gap: S.md, marginVertical: S.lg },
    orLine: { flex: 1, height: 1, backgroundColor: C.border },
    orTxt: { ...CAPS, fontSize: 10, letterSpacing: 1.8, color: C.textFaint },
    link: { fontFamily: FONT.regular, fontSize: 13.5, color: C.textDim, textAlign: "center" as const },
    linkStrong: { fontFamily: FONT.bold, color: C.primary },
    footer: { alignItems: "center" as const, marginTop: S.xxl },
    company: { ...CAPS, fontSize: 10, letterSpacing: 1.8, color: C.textFaint },
    // forgot-password sheet
    sheetBackdrop: { flex: 1, backgroundColor: "rgba(0,0,0,0.45)", justifyContent: "flex-end" as const },
    sheet: {
      backgroundColor: C.surface, borderTopLeftRadius: R.xl, borderTopRightRadius: R.xl,
      padding: S.xl, paddingBottom: S.xxl, gap: S.md,
    },
    sheetHandle: { alignSelf: "center" as const, width: 40, height: 4, borderRadius: 2, backgroundColor: C.border, marginBottom: S.sm },
    sheetTitle: { ...T.h3 },
    sheetBody: { ...T.bodyDim, lineHeight: 22 },
  });
}

// ---- AuthField: input with leading icon, optional eye toggle, inline error ----
function AuthField({
  icon, error, secure, ...rest
}: TextInputProps & { icon: string; error?: string; secure?: boolean }) {
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
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

export default function LoginScreen() {
  const router = useRouter();
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [fieldErr, setFieldErr] = useState<{ email?: string; password?: string; confirm?: string }>({});
  const [forgotOpen, setForgotOpen] = useState(false);

  // Cross-fade the form when switching login <-> signup.
  const fade = useRef(new Animated.Value(1)).current;
  const switchMode = (m: Mode) => {
    if (m === mode) return;
    Animated.timing(fade, { toValue: 0, duration: 120, easing: Easing.out(Easing.ease), useNativeDriver: true }).start(() => {
      setMode(m); setError(""); setFieldErr({}); setConfirm("");
      Animated.timing(fade, { toValue: 1, duration: 180, easing: Easing.in(Easing.ease), useNativeDriver: true }).start();
    });
  };

  // Remember-me: prefill the last email if the user opted in.
  useEffect(() => {
    (async () => {
      const saved = await store.getItemAsync(REMEMBER_KEY);
      if (saved) { setEmail(saved); setRemember(true); }
    })();
  }, []);

  const strength = strengthOf(password);
  // Weak -> danger, Fair -> warn, Good -> primary, Strong -> success.
  const strengthColor = [C.danger, C.danger, C.warn, C.primary, C.success][strength] ?? C.danger;

  const validate = (): boolean => {
    const errs: typeof fieldErr = {};
    const em = email.trim().toLowerCase();
    if (!em) errs.email = "Enter your email.";
    else if (!EMAIL_RE.test(em)) errs.email = "That doesn't look like a valid email.";
    if (!password) errs.password = "Enter your password.";
    else if (mode === "signup" && password.length < 8) errs.password = "Use at least 8 characters.";
    if (mode === "signup" && confirm !== password) errs.confirm = "Passwords don't match.";
    setFieldErr(errs);
    return Object.keys(errs).length === 0;
  };

  const submit = async () => {
    setError("");
    if (!validate()) return;
    const em = email.trim().toLowerCase();
    setBusy(true);
    try {
      if (mode === "login") await login(em, password);
      else await signup(em, password);
      if (remember) await store.setItemAsync(REMEMBER_KEY, em);
      else await store.deleteItemAsync(REMEMBER_KEY);
      // Replace so the back button doesn't return to login. Land on the tabs.
      router.replace("/");
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : "Something went wrong.";
      setError(msg);
    } finally {
      setBusy(false);
    }
  };

  return (
    <KeyboardAvoidingView
      style={{ flex: 1, backgroundColor: C.bg }}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
    >
      <ScrollView
        contentContainerStyle={st.scroll}
        keyboardShouldPersistTaps="handled"
        showsVerticalScrollIndicator={false}
      >
        {/* Masthead */}
        <View style={st.brandWrap}>
          <Text style={st.kicker}>{COMPANY}</Text>
          <Text style={st.brand}>{APP_NAME}</Text>
        </View>

        <Animated.View style={{ opacity: fade }}>
          {/* The promise, in serif — what the product actually does for you */}
          <Text style={st.promise}>
            {mode === "login"
              ? "You went to the meeting. We'll write it up."
              : "Every conversation, written up for you."}
          </Text>
          <Text style={st.tagline}>
            {mode === "login"
              ? "Every conversation comes back as a one-page brief — what was decided, what you owe, who said it."
              : "Record anything. Get back a one-page brief with the decisions, the owners, and the dates."}
          </Text>

          <View style={st.fields}>
            <AuthField
              icon="envelope"
              placeholder="Email"
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="email-address"
              autoComplete="email"
              textContentType="emailAddress"
              value={email}
              onChangeText={(t) => { setEmail(t); if (fieldErr.email) setFieldErr((p) => ({ ...p, email: undefined })); }}
              error={fieldErr.email}
            />
            <AuthField
              icon="lock"
              placeholder="Password"
              autoCapitalize="none"
              secure
              autoComplete={mode === "login" ? "password" : "password-new"}
              textContentType={mode === "login" ? "password" : "newPassword"}
              value={password}
              onChangeText={(t) => { setPassword(t); if (fieldErr.password) setFieldErr((p) => ({ ...p, password: undefined })); }}
              error={fieldErr.password}
            />

            {/* Signup: strength meter + confirm password */}
            {mode === "signup" && password ? (
              <View style={st.strengthRow}>
                <View style={st.strengthTrack}>
                  {[1, 2, 3, 4].map((i) => (
                    <View key={i} style={[st.strengthSeg, i <= strength && { backgroundColor: strengthColor }]} />
                  ))}
                </View>
                <Text style={[st.strengthLabel, { color: strengthColor }]}>{STRENGTH_LABEL[strength]}</Text>
              </View>
            ) : null}
            {mode === "signup" ? (
              <AuthField
                icon="lock.shield"
                placeholder="Confirm password"
                autoCapitalize="none"
                secure
                value={confirm}
                onChangeText={(t) => { setConfirm(t); if (fieldErr.confirm) setFieldErr((p) => ({ ...p, confirm: undefined })); }}
                error={fieldErr.confirm}
              />
            ) : null}
          </View>

          {/* Remember me / forgot password */}
          <View style={st.metaRow}>
            <Pressable style={st.remember} onPress={() => setRemember(!remember)}
              accessibilityRole="checkbox" accessibilityState={{ checked: remember }}>
              <View style={[st.checkbox, remember && st.checkboxOn]}>
                {remember ? <Icon name="checkmark" tintColor={C.bg} size={11} /> : null}
              </View>
              <Text style={st.rememberTxt}>Keep me signed in</Text>
            </Pressable>
            {mode === "login" ? (
              <Pressable onPress={() => setForgotOpen(true)} hitSlop={8}>
                <Text style={st.forgot}>Forgot password</Text>
              </Pressable>
            ) : null}
          </View>

          {error ? <ErrorText>{error}</ErrorText> : null}

          <Button
            label={mode === "login" ? "Sign in" : "Create account"}
            onPress={submit}
            loading={busy}
            style={{ marginTop: S.xl }}
          />

          <View style={st.orRow}>
            <View style={st.orLine} />
            <Text style={st.orTxt}>OR</Text>
            <View style={st.orLine} />
          </View>

          {/* Google sign-in — UI ready, backend not wired yet. Kept honest. */}
          <View>
            <GoogleButton
              label={mode === "login" ? "Continue with Google" : "Sign up with Google"}
              onPress={() => setError("Google sign-in is coming soon — use email for now.")}
            />
          </View>

          <Pressable
            onPress={() => switchMode(mode === "login" ? "signup" : "login")}
            disabled={busy}
            style={{ marginTop: S.xl }}
          >
            <Text style={st.link}>
              {mode === "login" ? "New here? " : "Already have an account? "}
              <Text style={st.linkStrong}>{mode === "login" ? "Open an account" : "Sign in"}</Text>
            </Text>
          </Pressable>
        </Animated.View>

        {/* The masthead already carries the company name — close on the
            promise instead of repeating it. */}
        <View style={st.footer}>
          <Text style={st.company}>{TAGLINE.replace(/\.$/, "")}</Text>
        </View>
      </ScrollView>

      {/* Forgot password — no reset endpoint exists yet, so this stays an
          honest explanation instead of a fake email flow. */}
      <Modal visible={forgotOpen} transparent animationType="fade" onRequestClose={() => setForgotOpen(false)}>
        <Pressable style={st.sheetBackdrop} onPress={() => setForgotOpen(false)}>
          <Pressable style={st.sheet} onPress={() => {}}>
            <View style={st.sheetHandle} />
            <Text style={st.sheetTitle}>Reset your password</Text>
            <Text style={st.sheetBody}>
              Self-serve password reset is coming soon. For now, contact us at
              {" "}support@exceller.tech from your account email and we'll reset
              it for you.
            </Text>
            <Button label="Got it" variant="secondary" onPress={() => setForgotOpen(false)} style={{ marginTop: S.sm }} />
          </Pressable>
        </Pressable>
      </Modal>
    </KeyboardAvoidingView>
  );
}
