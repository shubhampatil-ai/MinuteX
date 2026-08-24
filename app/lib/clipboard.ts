// lib/clipboard.ts — copy-to-clipboard with graceful degradation.
// expo-clipboard is a NATIVE module (same story as expo-audio /
// expo-linear-gradient): loaded defensively so a dev client built before it
// was added doesn't crash — we fall back to the OS Share sheet, which is a
// core RN API and always available.
import { Share } from "react-native";

type ClipboardModule = { setStringAsync: (text: string) => Promise<boolean> };

function loadClipboard(): ClipboardModule | null {
  try {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const mod = require("expo-clipboard");
    if (mod && typeof mod.setStringAsync === "function") return mod as ClipboardModule;
  } catch {
    // fall through — native module not present in this build
  }
  return null;
}

const clip = loadClipboard();

export type CopyResult = "copied" | "shared" | "failed";

export async function copyText(text: string): Promise<CopyResult> {
  if (clip) {
    try {
      await clip.setStringAsync(text);
      return "copied";
    } catch {
      // fall through to Share
    }
  }
  try {
    await Share.share({ message: text });
    return "shared";
  } catch {
    return "failed";
  }
}
