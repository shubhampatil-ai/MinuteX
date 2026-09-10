// src/app/_layout.tsx — root navigator + LOGIN GATE.
// Login gates EVERYTHING: on launch we check for a stored JWT. No token →
// redirect to /login. Only authenticated users reach the tabs / device control.
import { useEffect, useRef, useState } from "react";
import { Stack, useRouter, useSegments, type ErrorBoundaryProps } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { Pressable, Text, View } from "react-native";
import { useFonts } from "expo-font";
import {
  PlusJakartaSans_400Regular, PlusJakartaSans_500Medium,
  PlusJakartaSans_600SemiBold, PlusJakartaSans_700Bold, PlusJakartaSans_800ExtraBold,
} from "@expo-google-fonts/plus-jakarta-sans";
import { JetBrainsMono_500Medium } from "@expo-google-fonts/jetbrains-mono";
import { FONT, ThemeProvider, useTheme } from "../../lib/theme";
import { Splash } from "../../lib/splash";
import { getMe, getToken } from "../../lib/api";
import { DeviceProvider } from "../../lib/device-context";
import { WorkspaceProvider } from "../../lib/workspace-context";
import { onProfileNameSaved } from "../../lib/profile-gate";
import { IntegrationsProvider } from "../../lib/integrations";
import { NotificationsProvider } from "../../lib/notification-center";
import { recoverUploads } from "../../lib/uploads";

// Root error boundary — a crash anywhere in the tree lands here instead of a
// red screen (prod: a white screen). Styled with static colors on purpose:
// if the ThemeProvider itself crashed, useTheme() would throw again.
// Colors and fonts are static on purpose: if ThemeProvider or the font load is
// what crashed, useTheme()/FONT would fail again here. Values mirror the LIGHT
// scale in lib/theme.tsx. No custom font families — the system face always
// renders, and this screen must never be the thing that fails.
export function ErrorBoundary({ error, retry }: ErrorBoundaryProps) {
  return (
    <View style={{ flex: 1, backgroundColor: "#F6F7FB", justifyContent: "center", padding: 26 }}>
      <Text style={{ color: "#E5484D", fontSize: 11, fontWeight: "700", letterSpacing: 0.6, textTransform: "uppercase" }}>
        Couldn&apos;t finish
      </Text>
      <Text style={{ color: "#12131A", fontSize: 26, lineHeight: 32, marginTop: 8, fontWeight: "800" }}>
        Something went wrong
      </Text>
      <Text style={{ color: "#5B6072", fontSize: 13.5, lineHeight: 20, marginTop: 12 }}>
        MinuteX hit an unexpected error. Your recordings are safe in the cloud.
      </Text>
      {__DEV__ ? (
        <View style={{ borderLeftWidth: 3, borderLeftColor: "#E5484D", paddingLeft: 13, marginTop: 16 }}>
          <Text style={{ color: "#5B6072", fontSize: 12, lineHeight: 18 }} numberOfLines={4}>
            {error.message}
          </Text>
        </View>
      ) : null}
      <Pressable
        onPress={retry}
        style={({ pressed }) => ({
          marginTop: 22, backgroundColor: "#3E6BFF", borderRadius: 12,
          paddingVertical: 15, alignItems: "center", opacity: pressed ? 0.78 : 1,
        })}
      >
        <Text style={{ color: "#FFFFFF", fontSize: 14, fontWeight: "700" }}>Try again</Text>
      </Pressable>
    </View>
  );
}

// Minimum time the branded splash stays up. The token read usually resolves
// in a few ms — without a hold, the splash animation is a one-frame flash.
const SPLASH_MIN_MS = 1200;

// Route groups that do NOT require auth.
const PUBLIC_SEGMENTS = ["login", "signup"];

// The profile gate's OWN route. Requires auth (so it is not public) but must
// be exempt from the gate below, or the redirect would target the screen it is
// already on and loop.
const ONBOARDING_SEGMENT = "onboarding";

// ThemeProvider must wrap everything BELOW it that calls useTheme() — so the
// actual root content lives in a child component, with this default export
// only responsible for mounting the provider above it.
export default function RootLayout() {
  // The Briefing names an exact font family per weight — React Native custom
  // fonts do NOT synthesize weights, so every family referenced by FONT in
  // lib/theme.tsx must be registered here or that text renders in the system
  // face. Gate the tree on the load: a swap mid-render would reflow every
  // screen, and the serif/sans metrics differ enough to be visible.
  const [fontsLoaded] = useFonts({
    PlusJakartaSans_400Regular,
    PlusJakartaSans_500Medium,
    PlusJakartaSans_600SemiBold,
    PlusJakartaSans_700Bold,
    PlusJakartaSans_800ExtraBold,
    JetBrainsMono_500Medium,
  });

  // Splash is deliberately NOT themed here — it renders above ThemeProvider,
  // so useTheme() isn't available yet.
  if (!fontsLoaded) return <Splash />;

  return (
    <ThemeProvider>
      <RootContent />
    </ThemeProvider>
  );
}

function RootContent() {
  const { C, mode } = useTheme();
  const router = useRouter();
  const segments = useSegments();
  const [checked, setChecked] = useState(false);
  const [authed, setAuthed] = useState(false);
  // Whether `authed` is an answer about the CURRENT route, or a leftover from
  // the previous one. Both effects below re-run on a `segments` change, but
  // the token read is async — so without this the gate would fire first, on
  // the stale `authed`, and bounce the user straight back to where they came
  // from. Concretely: an expired-token screen clears the token and redirects
  // to /login; the gate runs with authed still `true`, sees "authed user on a
  // public route", and sends them back to the tab that just 401'd. Kept
  // separate from `checked` on purpose — `checked` drives the full-screen
  // "Starting…" state, and flipping that on every navigation would flash the
  // loading screen between every tab.
  const [authFresh, setAuthFresh] = useState(false);
  // Splash hold — starts at mount, flips after SPLASH_MIN_MS. The splash
  // hides when BOTH the auth check and the hold are done.
  const [splashHeld, setSplashHeld] = useState(true);
  useEffect(() => {
    const t = setTimeout(() => setSplashHeld(false), SPLASH_MIN_MS);
    return () => clearTimeout(t);
  }, []);

  // Re-check the stored token on every navigation, not just on mount. Login
  // saves the token then calls router.replace("/") — that changes `segments`,
  // which re-runs this effect. Without re-reading the token here, `authed`
  // would stay stuck at its stale initial value (false), and the gate below
  // would immediately bounce the just-logged-in user back to /login (looks
  // like "tapping Sign In does nothing").
  useEffect(() => {
    let cancelled = false;
    setAuthFresh(false);
    (async () => {
      const has = !!(await getToken());
      if (cancelled) return;   // a newer navigation already superseded this read
      setAuthed(has);
      setAuthFresh(true);
      setChecked(true);
    })();
    return () => { cancelled = true; };
  }, [segments]);

  // PROFILE GATE — does this signed-in user have a name yet?
  //
  // `null` means "not known yet", kept distinct from `false` for the same
  // reason `authFresh` exists above: acting on an unknown would bounce the
  // user on the very first frame after login, before the answer arrives.
  //
  // Read ONCE per session rather than on every navigation (unlike the token,
  // which is a cheap local read). This is a network call, so putting it on
  // `segments` would fire a /me request on every tab switch. The gate only
  // ever needs to open, and the one thing that opens it — saving a name — is
  // followed by router.replace("/"), so the local flip below is enough.
  const [needsName, setNeedsName] = useState<boolean | null>(null);
  // Whether the /me read has been STARTED for the current sign-in. A ref, not
  // state, and deliberately not in the dep array: it latches the one-shot
  // without re-running this effect, which is what keeps a single /me call a
  // single /me call.
  const nameChecked = useRef(false);
  // The gate is ONE-WAY once satisfied. The /me read below is async and can
  // still be in flight while the user is already filling in the onboarding
  // form, so its response carries the PRE-SAVE profile. Without this latch
  // that stale answer lands after the save and flips needsName back to true,
  // and the gate bounces the user straight back to the screen they just
  // completed — the exact "Continue does nothing until I restart the app"
  // symptom, and the reason the notify-only fix was not enough on its own.
  const nameSatisfied = useRef(false);
  useEffect(() => {
    if (!authed) {
      // Signed out: reset the latch so the NEXT sign-in re-asks. Guarded on
      // the ref rather than set unconditionally — an unconditional
      // setNeedsName(null) here would re-render, re-run this effect, and set
      // it again on every pass.
      if (nameChecked.current) {
        nameChecked.current = false;
        nameSatisfied.current = false;   // next sign-in re-asks from scratch
        setNeedsName(null);
      }
      return;
    }
    if (nameChecked.current) return;   // already asked for this sign-in
    nameChecked.current = true;
    let cancelled = false;
    (async () => {
      try {
        const me = await getMe();
        if (cancelled || nameSatisfied.current) return;
        // name_set is authoritative: `name` on /me is the RAW stored value, so
        // an older server that predates the flag still resolves correctly via
        // the name fallback rather than gating everyone.
        setNeedsName(me.name_set === undefined
          ? !String(me.name || "").trim()
          : !me.name_set);
      } catch {
        // A failed /me must NEVER trap a signed-in user behind the gate —
        // that would make a transient network error look like a lockout.
        // Fail OPEN: the worst case is a derived name shown for one session.
        // The latch stays set, so this does not retry in a loop.
        if (!cancelled && !nameSatisfied.current) setNeedsName(false);
      }
    })();
    return () => { cancelled = true; };
  }, [authed]);

  // The one thing that OPENS the gate, wired to the screen that does it.
  //
  // The /me read above is one-shot per sign-in, so saving a name cannot be
  // observed by re-reading it — and `needsName` is private to this component,
  // so the onboarding screen could not flip it either. The result was a gate
  // that stayed shut after being satisfied: Continue saved the name, then the
  // effect below bounced the user back to the gate, and only a full app
  // restart (a fresh /me) let them in.
  //
  // Trusting this ping is safe because it can only ever OPEN the gate, and
  // only for a session that already authenticated. A cold start still asks
  // the server.
  useEffect(() => onProfileNameSaved(() => {
    // Latch FIRST: an in-flight /me that resolves after this must not undo it.
    nameSatisfied.current = true;
    setNeedsName(false);
  }), []);

  // Gate: once we know auth state FOR THIS ROUTE, keep the user on the right side.
  useEffect(() => {
    if (!checked || !authFresh) return;
    const inPublic = PUBLIC_SEGMENTS.includes(segments[0] as string);
    const inOnboarding = segments[0] === ONBOARDING_SEGMENT;
    if (!authed && !inPublic) {
      router.replace("/login");
    } else if (authed && inPublic) {
      router.replace("/");
    } else if (authed && needsName === true && !inOnboarding) {
      // Signed in but nameless. Everything downstream — members lists, task
      // assignees, "recorded by" — needs a human label, so collect it before
      // the app proper. `replace` keeps it out of the back stack.
      router.replace("/onboarding/name");
    } else if (authed && needsName === false && inOnboarding) {
      // The gate is satisfied (or was never needed) but we are still sitting
      // on it — e.g. a name saved on another device. Let them through.
      router.replace("/");
    }
  }, [checked, authFresh, authed, needsName, segments, router]);

  // RECORDING RECOVERY — finish what a previous run of the app started.
  //
  // Runs once, as soon as we know the user is signed in (the upload calls need
  // the JWT). It re-queues every recording that is still on this device
  // unconfirmed, and finalizes any recording the app died in the middle of, so
  // audio that survived a crash or a background kill finds its way to the
  // server instead of sitting invisibly in the filesystem. Deliberately not
  // awaited and never allowed to throw: recovery is best-effort and must not
  // be able to keep the user out of the app.
  const recovered = useRef(false);
  useEffect(() => {
    if (!checked || !authed || recovered.current) return;
    recovered.current = true;
    void recoverUploads().catch(() => { /* the upload banners report per-job */ });
  }, [checked, authed]);

  // Status-bar icons follow the live theme (light icons on dark surfaces).
  const statusBar = <StatusBar style={mode === "dark" ? "light" : "dark"} />;

  if (!checked || splashHeld) {
    return (
      <>
        {statusBar}
        <Splash />
      </>
    );
  }

  return (
    <DeviceProvider>
      {/* ONE integration status for the whole app. Mounted here, above the
          navigator, so connecting or disconnecting Gmail on the Manage screen
          immediately changes what every OTHER screen offers — see
          lib/integrations.tsx for why a per-screen fetch would let them
          disagree. */}
      <IntegrationsProvider>
      {/* ONE unread count for the whole app, for the same reason: the bell in
          the masthead and the Notification Centre are the same data, and a
          per-screen fetch would let the badge disagree with the list the
          moment either marked something read. */}
      <NotificationsProvider>
      {/* ONE active workspace for the whole app, for the same reason as the
          two above: Desk, Record, Tasks and Contacts must all agree about
          WHICH workspace they are showing, and a per-screen read would let
          the recording banner disagree with the list it was started from.
          UI state only — the backend re-resolves membership every request. */}
      <WorkspaceProvider>
      {statusBar}
      <Stack
        screenOptions={{
          headerStyle: { backgroundColor: C.bg },
          headerTintColor: C.text,
          // fontFamily, never fontWeight — RN custom fonts don't synthesize
          // weights, so a bare fontWeight here would silently fall back.
          headerTitleStyle: { color: C.text, fontFamily: FONT.semibold, fontSize: 16 },
          headerShadowVisible: false,
          contentStyle: { backgroundColor: C.bg },
        }}
      >
        <Stack.Screen name="login" options={{ headerShown: false }} />
        <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
        {/* The profile gate. No header and no back gesture — the only way
            out is saving a name (see the gate in RootContent). */}
        <Stack.Screen
          name="onboarding/name"
          options={{ headerShown: false, gestureEnabled: false }}
        />
        {/* Recording sources — one chooser, three entry points, one pipeline */}
        <Stack.Screen name="new-recording" options={{ title: "New recording", presentation: "modal" }} />
        <Stack.Screen name="record" options={{ headerShown: false, presentation: "fullScreenModal" }} />
        <Stack.Screen name="record-phone" options={{ headerShown: false, presentation: "fullScreenModal" }} />
        <Stack.Screen name="upload" options={{ title: "Upload audio", presentation: "modal" }} />
        <Stack.Screen name="settings" options={{ title: "Settings" }} />
        <Stack.Screen name="trash" options={{ title: "Trash" }} />
        <Stack.Screen name="salesforce" options={{ title: "Salesforce" }} />
        {/* The OAuth redirect landing route. headerShown:false because it is a
            transient hop, not a place — see crm-connected.tsx for why a route
            must exist at all (the OS delivers the deep link to the app, so
            expo-router tries to route it). */}
        <Stack.Screen name="crm-connected" options={{ headerShown: false }} />
        <Stack.Screen name="salesforce-config" options={{ title: "Salesforce mapping" }} />
        {/* Integrations — the central place external applications are
            connected. index is the card list; one screen per provider that
            has something to manage (Gmail today). */}
        <Stack.Screen name="integrations/index" options={{ title: "" }} />
        <Stack.Screen name="integrations/gmail" options={{ title: "" }} />
        {/* The generic OAuth redirect landing route, for every provider —
            same transient-hop reasoning as crm-connected above. */}
        <Stack.Screen name="integrations-connected" options={{ headerShown: false }} />
        {/* Your device — reached from MinuteX's top-left status pill (and
            from You › Your device). It was a bottom tab until the bar went
            to three; the page keeps its own Masthead, which carries the live
            connection state, so the stack header is title-less and exists
            only to hand back the back button. */}
        <Stack.Screen name="devices" options={{ title: "" }} />
        <Stack.Screen name="pair" options={{ title: "Pair device" }} />
        <Stack.Screen name="claim" options={{ title: "Link device" }} />
        <Stack.Screen name="recording/[key]" options={{ headerShown: false }} />
        {/* The READ-ONLY meeting a task assignee opens from their task. A
            sibling of recording/[key] rather than a screen inside it: that
            directory's _layout mounts MeetingProvider, which fetches the
            owner-only recording route and would 404 for exactly the users
            this screen serves. See the file header for why read-only is a
            separate screen instead of a flag. */}
        <Stack.Screen name="meeting/[key]/shared" options={{ title: "Meeting" }} />
        {/* Organization layer — contacts and the cross-meeting task tracker.
            Both are top-level destinations rather than tabs: the bottom bar is
            deliberately three items, and these are places you go to from
            You / a meeting, not places you live in. Each screen sets its own
            title via its own Stack.Screen (contact titles are data, not
            constants). */}
        {/* Workspaces. Destinations reached from You, like Devices and
            Integrations — not a new navigation layer. */}
        <Stack.Screen name="workspaces" options={{ title: "Workspaces" }} />
        <Stack.Screen name="organisation/index" options={{ title: "Organisation" }} />
        <Stack.Screen name="organisation/create" options={{ title: "Create organisation" }} />
        <Stack.Screen name="organisation/join" options={{ title: "Join organisation" }} />
        <Stack.Screen name="organisation/members" options={{ title: "Members" }} />
        <Stack.Screen name="contacts" options={{ title: "Contacts" }} />
        <Stack.Screen name="contact/[id]" options={{ title: "Contact" }} />
        <Stack.Screen name="tasks" options={{ title: "Tasks" }} />
        <Stack.Screen name="task/[id]" options={{ title: "Task" }} />
        {/* The workspace Task AI. A normal pushed screen rather than a modal:
            it sets its own title (workspace vs one task) and, unlike the
            meeting assistant, a source tap pushes the transcript ON TOP of
            it — which a modal presentation would trap underneath. */}
        <Stack.Screen name="assistant" options={{ title: "MinuteX AI" }} />
        <Stack.Screen name="calendar" options={{ title: "Calendar" }} />
        <Stack.Screen name="notifications" options={{ title: "Notifications" }} />
      </Stack>
      </WorkspaceProvider>
      </NotificationsProvider>
      </IntegrationsProvider>
    </DeviceProvider>
  );
}