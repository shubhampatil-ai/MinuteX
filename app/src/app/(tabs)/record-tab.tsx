// src/app/(tabs)/record-tab.tsx — placeholder for the center Record tab slot.
// The tab bar's custom RecordButton navigates to the full-screen /record, so
// this route is never actually shown. It exists only so expo-router has a
// screen to register for the center slot. If ever reached directly, redirect.
import { Redirect } from "expo-router";

export default function RecordTabPlaceholder() {
  return <Redirect href="/record" />;
}