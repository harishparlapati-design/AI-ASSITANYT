# Android app (Capacitor) — build configuration

This directory holds the configuration to wrap the hosted Harry site as an
Android app. **No APK has been built on this machine**: the Android SDK and a
JDK are not installed here, so claiming a built or installed APK would be false.

What *is* verified: the website is an installable PWA (`manifest.webmanifest`,
service worker, generated icons, offline shell), which is the zero-friction way
to get Harry on the phone today — open the hosted URL in Chrome and choose
**Install app** / **Add to home screen**.

## Prerequisites for building the APK

- Node 18+ (Node 22 is present here)
- JDK 17 (`java -version`) — **not installed here**
- Android SDK with build-tools 34+ and `ANDROID_HOME` set — **not installed here**
- A deployed HTTPS origin for Harry (see `../deploy/README.md`)

## Steps

```bash
npm init -y
npm install @capacitor/core @capacitor/cli @capacitor/android
npx cap init "Harry AI" com.harry.assistant --web-dir=www
npx cap add android
cp mobile/capacitor.config.json capacitor.config.json
npx cap sync android
cd android && ./gradlew assembleDebug
# output: android/app/build/outputs/apk/debug/app-debug.apk
```

`capacitor.config.json` points `server.url` at the hosted origin, so the app
loads the same authenticated site (cookies handled by the WebView) instead of a
bundled copy of the UI. Set `HARRY_APP_URL` before building, or edit the file.

## Production notes

- Use `assembleRelease` with a signed keystore; never commit the keystore.
- `android:usesCleartextTraffic` must stay **false**; only HTTPS origins.
- Microphone permission (`RECORD_AUDIO`) is requested by the WebView when the
  user taps the mic; screen capture stays desktop-only.
- File downloads go through the WebView's download listener; grant
  `WRITE_EXTERNAL_STORAGE` only on API < 29.
- Verification that is still outstanding: install on a physical device, test
  login, voice permission, document download and reconnect after airplane mode.
