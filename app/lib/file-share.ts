// lib/file-share.ts — write raw bytes to a file and hand it to the OS share
// sheet. The one piece lib/export-doc.ts didn't need (its PDF path gets a URI
// straight from expo-print) but lib/docx-export.ts does, since fflate returns
// bytes with nowhere to go until they're written to disk.
//
// Same defensive require() as the rest of this codebase (see lib/clipboard.ts,
// lib/audio-player.tsx, lib/export-doc.ts): a dev client built before
// expo-file-system/expo-sharing were added must degrade, not crash.
import type { ExportResult } from "./export-doc";

type FsModule = {
  File: new (...parts: any[]) => {
    create: (opts?: { overwrite?: boolean }) => void;
    write: (contents: string | Uint8Array) => void;
    uri: string;
  };
  Paths: { cache: any };
};

type SharingModule = {
  isAvailableAsync: () => Promise<boolean>;
  shareAsync: (uri: string, opts?: Record<string, unknown>) => Promise<void>;
};

let fs: FsModule | null = null;
let sharing: SharingModule | null = null;

try {
  const mod = require("expo-file-system") as any;
  if (mod && typeof mod.File === "function") fs = mod;
} catch {
  // expo-file-system not in this build
}

try {
  const mod = require("expo-sharing") as any;
  if (mod && typeof mod.shareAsync === "function") sharing = mod;
} catch {
  // expo-sharing not in this build
}

export function canShareFiles(): boolean {
  return !!fs;
}

/** Write bytes to a cache file and share it. "saved" when no share sheet is available. */
export async function shareFileBytes(
  bytes: Uint8Array, filename: string, mimeType: string
): Promise<ExportResult> {
  if (!fs) return "unsupported";
  try {
    const file = new fs.File(fs.Paths.cache, filename);
    file.create({ overwrite: true });
    file.write(bytes);
    if (sharing) {
      try {
        if (await sharing.isAvailableAsync()) {
          await sharing.shareAsync(file.uri, { mimeType, dialogTitle: filename });
          return "shared";
        }
      } catch {
        // fall through — the file still exists in the cache directory
      }
    }
    return "saved";
  } catch {
    return "failed";
  }
}
