# MedReminder

A medication reminder app built with Expo (React Native + TypeScript).

## Quick Start

```bash
npm install
npm start
```

## Development

```bash
npm start           # Start Expo dev server (default: http://localhost:8081)
npm start -- --localhost  # Force localhost connection
npx expo start --android  # Start with Android target
```

## Typecheck

```bash
npm run typecheck
```

## Building

### Expo Go (development)

```bash
npm start
# Scan QR code with Expo Go app on your device
```

### Android Preview Build (APK)

Requires `eas login` (free account at expo.dev).

```bash
eas build --platform android --profile preview --local
# Output: android/app/build/outputs/apk/debug/app-debug.apk
```

### Android Production Build (AAB)

Requires EAS account and a signing keystore.

```bash
eas build --platform android --profile production
# Output: .aab bundle for Google Play Store submission
```

## Project Structure

```
App.tsx          # Main app (all screens: Today, Medications, History, Family, Settings)
src/i18n.ts      # Translations and weekday keys
app.json         # Expo configuration
eas.json         # EAS Build profiles
package.json     # Dependencies and scripts
```

## App Identity

- Android package: `com.medreminder.app`
- iOS bundle: `com.medreminder.app`
- Version: `1.0.0`

## Notes

- Family features are local/demo only (no real device sync)
- Notifications require Android notification permission
- Local data stored via AsyncStorage

## AI Source Monitor Prototype

This workspace now includes a lightweight Python watcher at [ai_monitor/monitor.py](/C:/Users/RYO/new_project/ai_monitor/monitor.py) that can poll selected AI sources, compare snapshots, and write change logs without using an LLM by default.

### What it does

- Polls configured sources from [ai_monitor/sources.json](/C:/Users/RYO/new_project/ai_monitor/sources.json)
- Normalizes the important page content
- Computes hashes and diffs against the last snapshot
- Writes structured change events to `ai_monitor/logs/events.jsonl`
- Writes cycle status lines to `ai_monitor/logs/status.log`

### Run once

```bash
python ai_monitor/monitor.py
```

### Run every 3 minutes

```bash
python ai_monitor/monitor.py --loop --interval 180
```

### Optional AI reaction stage

When the monitor has already logged changes, you can review only the new events with AI instead of sending every fetch through a model:

```bash
python ai_monitor/reactor.py --dry-run
```

To enable a live AI call, set these environment variables for any OpenAI-compatible endpoint and then run:

```bash
python ai_monitor/reactor.py
```

- `LLM_API_BASE`
- `LLM_API_KEY`
- `LLM_MODEL`

### Next step

Once you like the raw event logs, we can wire the reactor to a scheduler so it checks only new events every 10 minutes for classification and summarization.
