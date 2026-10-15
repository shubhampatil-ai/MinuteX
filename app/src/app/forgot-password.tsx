// src/app/forgot-password.tsx — self-serve password reset, signed out.
//
// Two steps on one screen: (1) email -> POST /password/forgot sends a 6-digit
// code; (2) code + new password -> POST /password/reset, which also signs the
// user in. A typed code rather than an emailed link: the app is native, and a
// link opened on a laptop (or in the wrong browser) is a dead end.
//
// The backend answers step 1 identically whether or not the account exists,
// so the copy here never says "we found your account" — only "if".
import { useEffect, useMemo, useState } from "react";
import { KeyboardAvoidingView, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";
import { Icon } from "../../lib/icons";
import { CAPS, ELEV, FONT, R, S, useTheme, ColorScale } from "../../lib/theme";
import { Button, ErrorText, SuccessText } from "../../lib/ui";
import {
  ApiError, requestPasswordReset, resetPassword,
  RESET_ERR_INVALID, RESET_ERR_LOCKED, RESET_ERR_UNAVAILABLE,
} from "../../lib/api";
import { AuthField, EMAIL_RE, StrengthMeter, useFinishSignIn } from "../../lib/auth-ui";

type Step = "request" | "reset";

// Mirrors the backend's PASSWORD_RESET_RESEND_SECONDS. Within it the server
// silently skips the send, so the button must not pretend to resend.
const RESEND_SECONDS = 60;
const UNAVAILABLE_MSG =
  "Password reset by email isn't available right now. Contact support@exceller.tech from your account email and we'll help.";

function buildStyles(C: ColorScale, T: ReturnType<typeof useTheme>["T"]) {
  return StyleSheet.create({
    scroll: { flexGrow: 1, paddingHorizontal: 26, paddingTop: 64, paddingBottom: 48 },
    back: { flexDirection: "row", alignItems: "center", gap: 6, alignSelf: "flex-start", minHeight: 44 },
    backTxt: { fontFamily: FONT.semibold, fontSize: 14, color: C.textDim },
    kicker: { ...CAPS, letterSpacing: 2.6, color: C.textFaint, marginTop: S.xl },
    h1: { ...T.h2, marginTop: S.sm },
    sub: { ...T.bodyDim, marginTop: S.sm, lineHeight: 22 },
    strong: { fontFamily: FONT.semibold, color: C.text },
    fields: {
      marginTop: 28, backgroundColor: C.surface, borderRadius: R.card, borderWidth: 1,
      borderColor: C.border, padding: S.lg, gap: S.md, shadowColor: C.shadow, ...ELEV.sm,
    },
    linkRow: { flexDirection: "row", justifyContent: "space-between", marginTop: S.lg },
    link: { fontFamily: FONT.semibold, fontSize: 13.5, color: C.primary, minHeight: 44, textAlignVertical: "center" },
    linkOff: { color: C.textFaint },
    devBox: { marginTop: S.lg, borderRadius: R.md, borderWidth: 1, padding: S.md },
    devLabel: { ...CAPS, fontSize: 10, letterSpacing: 1.6, color: C.text },
    devCode: { fontFamily: FONT.bold, fontSize: 28, letterSpacing: 6, color: C.text, marginTop: 4 },
    devHint: { fontFamily: FONT.regular, fontSize: 12.5, color: C.textDim, marginTop: 4 },
  });
}

export default function ForgotPasswordScreen() {
  const params = useLocalSearchParams<{ email?: string }>();
  const router = useRouter();
  const finishSignIn = useFinishSignIn();
  const { C, T } = useTheme();
  const st = useMemo(() => buildStyles(C, T), [C, T]);

  const [step, setStep] = useState<Step>("request");
  const [email, setEmail] = useState(typeof params.email === "string" ? params.email : "");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [fieldErr, setFieldErr] = useState<{ email?: string; code?: string; password?: string; confirm?: string }>({});
  const [expiresMin, setExpiresMin] = useState(15);
  const [cooldown, setCooldown] = useState(0);
  const [devCode, setDevCode] = useState("");

  useEffect(() => {
    if (cooldown <= 0) return;
    const t = setTimeout(() => setCooldown((c) => c - 1), 1000);
    return () => clearTimeout(t);
  }, [cooldown]);

  const clearField = (k: keyof typeof fieldErr) =>
    setFieldErr((p) => (p[k] ? { ...p, [k]: undefined } : p));

  const goBack = () => (router.canGoBack() ? router.back() : router.replace("/login"));

  const sendCode = async (isResend = false) => {
    setError(""); setNotice("");
    const em = email.trim().toLowerCase();
    if (!EMAIL_RE.test(em)) {
      setFieldErr({ email: em ? "That doesn't look like a valid email." : "Enter your email." });
      return;
    }
    setBusy(true);
    try {
      const res = await requestPasswordReset(em);
      setEmail(em);
      setExpiresMin(Math.max(1, Math.round((res.expires_in || 900) / 60)));
      setCooldown(RESEND_SECONDS);
      setStep("reset");
      if (isResend) { setCode(""); setNotice("A new code is on its way. Earlier codes no longer work."); }
      // TEST MODE (backend allowlist): no email was sent — the code came back
      // in the response, so show it and fill it in.
      setDevCode(res.dev_code || "");
      if (res.dev_code) setCode(res.dev_code);
    } catch (e) {
      if (e instanceof ApiError && e.code === RESET_ERR_UNAVAILABLE) setError(UNAVAILABLE_MSG);
      else setError(e instanceof ApiError ? e.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  };

  const submitReset = async () => {
    setError(""); setNotice("");
    const errs: typeof fieldErr = {};
    if (!/^\d{6}$/.test(code)) errs.code = "Enter the 6-digit code from the email.";
    if (password.length < 8) errs.password = "Use at least 8 characters.";
    if (confirm !== password) errs.confirm = "Passwords don't match.";
    setFieldErr(errs);
    if (Object.keys(errs).length) return;
    setBusy(true);
    try {
      const res = await resetPassword(email, code, password);
      await finishSignIn(res.user_id);
    } catch (e) {
      if (e instanceof ApiError && e.code === RESET_ERR_INVALID) {
        setFieldErr({ code: "That code is wrong or has expired. Check the email, or resend." });
      } else if (e instanceof ApiError && e.code === RESET_ERR_LOCKED) {
        setCode("");
        setDevCode("");
        setCooldown(0);
        setError("Too many wrong attempts — that code no longer works. Send a new one.");
      } else if (e instanceof ApiError && e.code === RESET_ERR_UNAVAILABLE) {
        setError(UNAVAILABLE_MSG);
      } else {
        setError(e instanceof ApiError ? e.message : "Something went wrong.");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <KeyboardAvoidingView style={{ flex: 1, backgroundColor: C.bg }} behavior="padding">
      <ScrollView contentContainerStyle={st.scroll} keyboardShouldPersistTaps="handled"
        showsVerticalScrollIndicator={false}>
        <Pressable style={st.back} onPress={goBack} hitSlop={8}
          accessibilityRole="button" accessibilityLabel="Back to sign in">
          <Icon name="chevron.left" tintColor={C.textDim} size={16} />
          <Text style={st.backTxt}>Sign in</Text>
        </Pressable>

        <Text style={st.kicker}>Account recovery</Text>
        <Text style={st.h1}>{step === "request" ? "Reset your password" : "Check your email"}</Text>

        {step === "request" ? (
          <>
            <Text style={st.sub}>
              Enter the email you sign in with. We’ll send a 6-digit code to reset your password.
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
                returnKeyType="send"
                onSubmitEditing={() => sendCode()}
                value={email}
                onChangeText={(t) => { setEmail(t); clearField("email"); }}
                error={fieldErr.email}
              />
            </View>
            {error ? <ErrorText>{error}</ErrorText> : null}
            <Button label="Send code" onPress={() => sendCode()} loading={busy} style={{ marginTop: S.xl }} />
          </>
        ) : (
          <>
            <Text style={st.sub}>
              If an account exists for <Text style={st.strong}>{email}</Text>, we’ve sent it a
              6-digit code. It expires in {expiresMin} minutes. Check your spam folder if it
              doesn’t arrive.
            </Text>
            {devCode ? (
              <View style={[st.devBox, { backgroundColor: C.warnSoft, borderColor: C.warn }]}
                accessibilityRole="text" accessibilityLabel={`Test mode code ${devCode.split("").join(" ")}`}>
                <Text style={st.devLabel}>TEST MODE — no email was sent</Text>
                <Text style={st.devCode}>{devCode}</Text>
                <Text style={st.devHint}>This address is on the backend test list. The code is filled in below.</Text>
              </View>
            ) : null}
            <View style={st.fields}>
              <AuthField
                icon="number"
                placeholder="6-digit code"
                keyboardType="number-pad"
                maxLength={6}
                autoComplete="one-time-code"
                textContentType="oneTimeCode"
                value={code}
                onChangeText={(t) => { setCode(t.replace(/\D/g, "")); clearField("code"); }}
                error={fieldErr.code}
              />
              <AuthField
                icon="lock"
                placeholder="New password"
                autoCapitalize="none"
                secure
                autoComplete="password-new"
                textContentType="newPassword"
                value={password}
                onChangeText={(t) => { setPassword(t); clearField("password"); }}
                error={fieldErr.password}
              />
              {password ? <StrengthMeter password={password} /> : null}
              <AuthField
                icon="lock.shield"
                placeholder="Confirm new password"
                autoCapitalize="none"
                secure
                autoComplete="password-new"
                textContentType="newPassword"
                value={confirm}
                onChangeText={(t) => { setConfirm(t); clearField("confirm"); }}
                error={fieldErr.confirm}
              />
            </View>
            {notice ? <SuccessText>{notice}</SuccessText> : null}
            {error ? <ErrorText>{error}</ErrorText> : null}
            <Button label="Reset password" onPress={submitReset} loading={busy} style={{ marginTop: S.xl }} />
            <View style={st.linkRow}>
              <Pressable
                onPress={() => { setStep("request"); setError(""); setNotice(""); setFieldErr({}); setCode(""); setDevCode(""); }}
                disabled={busy} hitSlop={8} accessibilityRole="button">
                <Text style={st.link}>Use a different email</Text>
              </Pressable>
              <Pressable onPress={() => sendCode(true)} disabled={busy || cooldown > 0} hitSlop={8}
                accessibilityRole="button" accessibilityState={{ disabled: busy || cooldown > 0 }}>
                <Text style={[st.link, cooldown > 0 && st.linkOff]}>
                  {cooldown > 0 ? `Resend in ${cooldown}s` : "Resend code"}
                </Text>
              </Pressable>
            </View>
          </>
        )}
      </ScrollView>
    </KeyboardAvoidingView>
  );
}
