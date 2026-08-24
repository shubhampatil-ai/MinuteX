// plugins/withAndroidAppQueries.js — declares Android 11+ package visibility
// for apps MinuteX hands off notifications to via Linking.openURL/canOpenURL
// (see lib/contacts.ts). Without a <queries> entry, Linking.canOpenURL()
// returns false for WhatsApp even when it's installed — Android's package
// visibility rules hide other apps from a query unless explicitly declared
// here, since app.json has no built-in field for this (it must be injected
// into AndroidManifest.xml at prebuild time).
const { withAndroidManifest } = require("@expo/config-plugins");

const PACKAGES = ["com.whatsapp", "com.whatsapp.w4b"]; // consumer + Business app

function withAndroidAppQueries(config) {
  return withAndroidManifest(config, (config) => {
    const manifest = config.modResults.manifest;
    if (!manifest.queries) manifest.queries = [{}];
    const queries = manifest.queries[0];
    if (!queries.package) queries.package = [];

    for (const pkg of PACKAGES) {
      const already = queries.package.some((p) => p.$["android:name"] === pkg);
      if (!already) queries.package.push({ $: { "android:name": pkg } });
    }

    // Intent-based visibility for the generic "send SMS" / "dial" / "send
    // email" actions that Linking/expo-sms use under the hood on Android.
    if (!queries.intent) queries.intent = [];
    const intents = [
      { action: "android.intent.action.SENDTO", data: { scheme: "smsto" } },
      { action: "android.intent.action.SENDTO", data: { scheme: "mailto" } },
      { action: "android.intent.action.DIAL", data: { scheme: "tel" } },
    ];
    for (const i of intents) {
      const already = queries.intent.some(
        (q) => q.action?.[0]?.$?.["android:name"] === i.action &&
               q.data?.[0]?.$?.["android:scheme"] === i.data.scheme
      );
      if (!already) {
        queries.intent.push({
          action: [{ $: { "android:name": i.action } }],
          data: [{ $: { "android:scheme": i.data.scheme } }],
        });
      }
    }

    return config;
  });
}

module.exports = withAndroidAppQueries;
