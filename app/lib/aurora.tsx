// lib/aurora.tsx — retired.
//
// The Briefing has no gradient wash. This file is kept as a no-op so the ~7
// existing `<Aurora height={...} intensity={...} />` call sites keep compiling
// while you delete them screen by screen:
//
//   src/app/(tabs)/index.tsx      src/app/record.tsx
//   src/app/login.tsx             src/app/new-recording.tsx
//   src/app/recording/[key].tsx   (x2 — header wash + <Processing>)
//
// Once they're all gone, delete this file AND `expo-linear-gradient` from
// package.json — nothing else in the app needs the native module.
import React from "react";
import type { ViewStyle } from "react-native";

export function Aurora(_props: { height?: number; intensity?: number; style?: ViewStyle }) {
  return null;
}
