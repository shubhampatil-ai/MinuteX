// lib/app-info.ts — the running app's version, read from the build.
//
// Profile and Settings both used to print a hard-coded "1.0.0", which would
// silently go stale on the first version bump. expo-constants reads the value
// app.json was built with, so the About rows say what is actually installed.
import Constants from "expo-constants";

export const APP_VERSION: string = Constants.expoConfig?.version ?? "—";
