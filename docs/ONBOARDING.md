# AI_voice — ESP32-S3 Recorder Firmware: Developer Onboarding

Firmware for an ESP32-S3 voice recorder: records dual-mic WAV to SD, uploads
to S3 over HTTPS, transcribes via a Lambda pipeline (ElevenLabs + Groq),
controlled by buttons / serial console / BLE app.

**Hardware:** ESP32-S3 N16R8 (16 MB flash, 8 MB OPI PSRAM), 2× INMP441 I2S
mics, SD over SPI, WS2812 status LED, 2 buttons. Pin map: [device/src/Config.h](../device/src/Config.h).

**Cloud:** AWS `ap-south-1` (account 624734879550) — HTTP API `q87zfn5vyj`,
bucket `meeting-recorder-shubham-aps1`, 3 Python Lambdas, DynamoDB. Details:
[MIGRATION_ap-south-1.md](MIGRATION_ap-south-1.md). The old `eu-north-1` stack
still exists as rollback only — do not point anything at it.

---

## Step 0 — Get the code (currently the repo has NO remote!)

As of 2026-08-01 this project has **zero tracked files and no git remote** —
it lives on one laptop. Whoever reads this first with access to that laptop:

```powershell
cd C:\ai_voicebot_kalyan\AI_voice
git add -A
git commit -m "Initial commit: firmware v1.7.4 + tuned PIO build + backend docs"
git remote add origin <your-github-url>
git push -u origin master
```

`.gitignore` already excludes secrets (`.env`, `.env.bak-*`), build output
(`.pio/`), and local artifacts. **Note:** `device/src/Config.h` contains the live
`DEVICE_API_KEY` — repo access = device-upload access. Keep the repo private.

Teammates then just `git clone`.

---

## Two ways to build — pick based on what you're doing

| | PlatformIO (primary) | Arduino IDE (fallback) |
|---|---|---|
| lwIP TCP send buffer | **65535 → ~313 KB/s uploads** | 5744 → ~136 KB/s |
| Setup cost | ~45 min first build, ~5 GB disk | 10 min if IDE installed |
| Config | all in [platformio.ini](platformio.ini), versioned | Tools-menu clicking, unversioned |
| Known issue | FATFS false `DATA LOSS` log (see Known Issues) | none |

Firmware source is **identical** for both. The tuned network stack exists only
in the PlatformIO build (the IDE ships precompiled libraries that cannot be
tuned). The serial upload report tells you which build a device runs:
`lwIP snd buf (cfg) : 65535 (tuned libs)` vs `5744 <-- SMALL`.

### PlatformIO setup (Windows)

1. Install Python 3.10+, then: `pip install platformio`
   (or install the PlatformIO IDE extension in VS Code)
2. **Always run pio from PowerShell or cmd — NEVER Git Bash.** ESP-IDF's
   tooling detects MSYS shells and refuses (`MSys/Mingw is not supported`).
3. Nothing else to configure: `platformio.ini` pins the platform version,
   board flags (OPI PSRAM, 16 MB QIO, partition table), USB-CDC serial, the
   `custom_sdkconfig` network tuning, and a **space-free core dir**
   (`C:\ai_voicebot_kalyan\.piocore` — created automatically; needed because
   ESP-IDF cannot build under paths with spaces, e.g. `C:\Users\First Last\`).
4. First build downloads toolchains and recompiles the IDF libs: **30–45 min,
   one-time**. Every later build is seconds to ~2 min.

```powershell
cd C:\ai_voicebot_kalyan\AI_voice
python -m platformio run                 # build
python -m platformio run -t upload       # build + flash (auto-detects COM port)
python -m platformio device monitor      # serial monitor, 115200
```

### Arduino IDE setup (fallback / SD-critical work until the FATFS issue is fixed)

Board **ESP32S3 Dev Module**, core 3.3.x, with Tools set to:

| Setting | Value |
|---|---|
| PSRAM | **OPI PSRAM** (required — uploads refuse without it) |
| Flash Size | 16MB |
| Partition Scheme | 16M Flash (3MB APP/9.9MB FATFS) — `app3M_fat9M_16MB` |
| **USB CDC On Boot** | **Enabled** — without this, `Serial` output is invisible |

Open `device/src/src.ino` (a stub — all code is in `Main.cpp`, which the IDE
compiles automatically) and build/flash normally.

---

## Daily workflow: change code → device

1. Edit sources in `device/src/` (flat layout, one class per file):
   - `Config.h` — every pin, constant, endpoint and feature flag
   - `Main.cpp` — `setup()`/`loop()` wiring (the `.ino` is an empty stub)
   - `Core_*` — StateManager / EventBus / ConfigManager / TaskManager
   - `Driver_*` — I2S, SD, battery, LED (hardware primitives)
   - `Service_*` — recorder, S3 uploader, pending queue, BLE, WiFi provisioning
   - `Task_*` — the six FreeRTOS tasks (single owner per resource)
2. **Close any serial monitor before flashing** — an open monitor holds the
   COM port and the flash fails with `PermissionError ... Access is denied`.
3. `python -m platformio run -t upload`
4. Reopen the monitor. Useful serial commands: `help`, `show`, `start`,
   `stop`, `pending`, `sdinfo`, `benchsd <file>`, `wifi <ssid>|<pass>`.
5. Record a short clip and check the `UPLOAD ANALYSIS REPORT` block — it
   prints throughput, stall counts, memory state, and which libs it was built
   with. Treat it as the regression test for anything touching upload/SD/WiFi.

### Feature flags you'll actually touch ([device/src/Config.h](../device/src/Config.h))

| Flag | Meaning |
|---|---|
| `LOCAL_UPLOAD_TEST` | `1` = upload to a laptop over plain HTTP instead of S3 (benchmarking; see [benchmark_server/README.md](benchmark_server/README.md)). Keep `0`. |
| `WIFI_DISABLE_POWER_SAVE` | `1` (current) = radio never sleeps → low RTT, higher battery drain |
| `UPLOAD_CHUNK_BYTES` | SD→socket chunk (32 KB). The ONLY place chunk size lives. |
| `ENABLE_UPLOAD_PROFILING` | `1` = full upload instrumentation (keep on until perf work ends) |
| `PRESIGN_ENDPOINT` | ap-south-1 API. Changing it requires reflash. |

---

## Two people, one device

- The device is **stateful**: WiFi credentials (NVS), queued recordings
  (SD `pending.txt`), and whatever firmware the last person flashed. Always
  check `show` + the boot banner before assuming what's on it.
- Best fix: get a second ESP32-S3 N16R8 (~₹800) so you each have one. The
  BLE name (`AI-Recorder-XXXX`) is unique per chip, so both can coexist.
- If sharing: announce flashes; whoever flashes must verify the boot banner
  and one record→upload cycle before handing it back.
- COM port differs per machine — PIO auto-detects; the IDE needs it picked.

## The mobile app (MinuteX, Expo) — building it

The app lives in [app/](../app/) (Expo SDK 57, expo-router).
It is a **full AI meeting assistant on its own**: phone recording and audio
upload work with no hardware, and the MinuteX device is a third recording
source. See the "Recording sources" section in [README.md](README.md).

**JS-only changes need no build** — `npx expo start` and reload the dev client.

**Native changes need a new dev-client binary.** Anything that adds a native
module or changes `app.json` plugins qualifies. Two of those landed on
2026-08-03: `expo-document-picker` (file upload) and `enableBackgroundRecording`
on the expo-audio plugin. Symptom of running JS that needs a newer binary:
`Cannot find native module 'ExpoDocumentPicker'`.

| | EAS cloud build (works on any machine) | `npx expo run:android` (local) |
|---|---|---|
| Needs Android SDK | no | **yes** — Android Studio or cmdline-tools |
| Command | `npx eas-cli build --profile development --platform android` | `npx expo run:android` |
| Result | APK download link (install on the phone) | builds + installs over USB |

**This laptop has no Android SDK** (no `ANDROID_HOME`, no `adb`), so
`npx expo run:android` fails with *"Failed to resolve the Android SDK path"*.
Use the EAS cloud build — the project is already linked
(`shubham0111s-team/minutex`) with remote credentials, so it needs no local
toolchain. To build locally instead, install Android Studio and set
`ANDROID_HOME` to `%LOCALAPPDATA%\Android\Sdk`.

## Known issues (2026-08-01)

1. **PIO build: false `DATA LOSS` after recording.** FATFS metadata reads
   stale for ~2 s after file close (`exists()`=false, empty root listing) —
   the file is actually fine and the pending-queue retry uploads it. Root
   cause: pioarduino's generated sdkconfig differs from Arduino's stock FATFS
   config. **Open item — fix before shipping the PIO build.** Use the IDE
   build for SD-critical field recordings until then.
2. Upload DRAM dips to ~12 KB mid-transfer on the tuned build (never failed;
   monitor `[FRAG]` lines when touching upload code).
3. `transcribeRecording` Lambda has plaintext Groq/ElevenLabs API keys in env
   vars — rotation to Secrets Manager pending.
4. WiFi RSSI ~−75 dBm at the usual desk is the current throughput ceiling.

## History / context docs

- [benchmark_server/FINDINGS.md](benchmark_server/FINDINGS.md) — why uploads
  were 26 KB/s and the whole diagnosis (`throughput = snd_buf ÷ RTT`)
- [MIGRATION_ap-south-1.md](MIGRATION_ap-south-1.md) — the AWS region move
- [platformio.ini](platformio.ini) — every build gotcha, documented in place
