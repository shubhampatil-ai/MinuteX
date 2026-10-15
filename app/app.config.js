// app.config.js — dynamic additions on top of app.json (which stays the
// source of truth for everything static).
//
// ONE job: point Android at google-services.json WITHOUT committing it.
//
// Expo Push on Android is delivered through FCM, and the native FCM client
// refuses to initialise without google-services.json — getExpoPushTokenAsync
// then fails with "Default FirebaseApp is not initialized". The file is
// gitignored (see .gitignore), so it reaches EAS Build as a FILE environment
// variable instead:
//
//   eas env:create --scope project --environment development \
//     --name GOOGLE_SERVICES_JSON --type file --value ./google-services.json
//   (repeat for preview / production)
//
// EAS materialises the file on the build worker and sets GOOGLE_SERVICES_JSON
// to its path. Locally, a google-services.json dropped next to this file is
// picked up for `expo run:android`. With neither, the build still succeeds and
// push is simply unavailable on that binary — the app degrades (lib/push.ts
// reports "unavailable") rather than failing to build.
//
// This is NOT the FCM V1 service-account key. That one is a real secret and
// is uploaded to EAS credentials (`eas credentials`), never to the repo or to
// this config. See docs/PUSH_NOTIFICATIONS.md.
const fs = require("fs");
const path = require("path");

module.exports = ({ config }) => {
  // eslint-disable-next-line no-undef -- a Node config file, evaluated by Expo CLI
  const local = path.join(__dirname, "google-services.json");
  const googleServicesFile =
    process.env.GOOGLE_SERVICES_JSON || (fs.existsSync(local) ? "./google-services.json" : undefined);

  // APNs environment. The expo-notifications plugin defaults aps-environment
  // to "development", which only matches a development-signed build. Every
  // other EAS profile here (preview, sideload, production, production-apk) is
  // distribution-signed and needs "production", or iOS registers against the
  // wrong APNs gateway and pushes never arrive. EAS sets EAS_BUILD_PROFILE on
  // the build worker; a local `expo run:ios` has none and stays development.
  const profile = process.env.EAS_BUILD_PROFILE || "";
  const apnsMode = !profile || profile === "development" ? "development" : "production";
  const plugins = (config.plugins || []).map((p) =>
    Array.isArray(p) && p[0] === "expo-notifications"
      ? ["expo-notifications", { ...(p[1] || {}), mode: apnsMode }]
      : p
  );

  // Continue with Google (lib/google-signin.ts). The plugin's only native job
  // without Firebase is registering the iOS URL scheme — the REVERSED iOS
  // client id — and it throws when given none. So it is added only when that
  // id is configured; Android needs no plugin (its OAuth client is matched by
  // package name + signing SHA-1 in the Google Cloud console), and an iOS
  // build without the id simply reports Google sign-in as unavailable.
  const iosClientId = process.env.EXPO_PUBLIC_GOOGLE_IOS_CLIENT_ID || "";
  if (iosClientId) {
    const suffix = ".apps.googleusercontent.com";
    const iosUrlScheme = "com.googleusercontent.apps." + iosClientId.replace(suffix, "");
    plugins.push(["@react-native-google-signin/google-signin", { iosUrlScheme }]);
  }

  return {
    ...config,
    plugins,
    android: {
      ...config.android,
      ...(googleServicesFile ? { googleServicesFile } : {}),
    },
  };
};
