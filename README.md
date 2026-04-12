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
