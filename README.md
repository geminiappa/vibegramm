# Vibegram PWA — iPhone, Android and Desktop

This folder contains the Vibegram web app configured as an installable Progressive Web App (PWA), including server-side Web Push delivery.

## What's included

- `index.html` — existing Vibegram UI with PWA install guidance and a Push enable/disable control.
- `manifest.webmanifest` — standalone app manifest.
- `pwa-192.png`, `pwa-512.png` — PWA icons.
- `service-worker.js` — app shell/offline fallback, push notification display and notification click routing.
- `server.py` — stores Push subscriptions separately from public user profiles, offers `/api/push/public-key`, `/api/push/subscribe`, `/api/push/unsubscribe`, and queues direct-message/channel-post push notifications.
- `scripts/generate_vapid_keys.py` — creates VAPID keys for Web Push.
- `requirements.txt` — includes `pywebpush` for encrypted Web Push delivery.

## Deploy to Render

1. Copy the contents of this directory to the root of the GitHub repository connected to Render. Keep `VibegramDesktop/` if you also use the Windows desktop app.
2. Keep the existing `DATABASE_URL` setting if the service uses PostgreSQL. Do not delete existing environment variables.
3. In Render → Environment, add:
   - `PUBLIC_BASE_URL` = `https://vibegramm.onrender.com`
   - `VAPID_PUBLIC_KEY` = generated public key
   - `VAPID_PRIVATE_KEY` = generated private key (**secret**)
   - `VAPID_SUBJECT` = `mailto:admin@vibegramm.onrender.com`
4. Commit and push the files. Render should install the updated `requirements.txt` and redeploy the service.

### Generate VAPID keys on Windows

Install Python 3, open PowerShell in the project folder, then run:

```powershell
python -m pip install -r requirements.txt
python scripts/generate_vapid_keys.py
```

Copy the three printed values to Render → Environment. Never paste `VAPID_PRIVATE_KEY` into JavaScript, HTML, GitHub issues, screenshots or public chat. Keep the same key pair once users have subscribed; changing it forces all devices to subscribe again.

## Install on iPhone

1. Open `https://vibegramm.onrender.com` in Safari.
2. Tap Share → Add to Home Screen → Add.
3. Open Vibegram from the new Home Screen icon (not from the Safari tab).
4. Sign in/open your profile, then use the bell button or Profile → App Vibegram → Enable Push Notifications and accept the iOS prompt.

Web Push on iPhone/iPad requires iOS/iPadOS 16.4 or newer and a Home Screen web app. Browser notification permission alone is not enough. Android/desktop Chromium can use the install prompt or the browser's install menu.

## Testing

- Check `GET /manifest.webmanifest` and `GET /service-worker.js` return HTTP 200.
- `GET /api/push/public-key` should return `{ "ok": true, "enabled": true, "publicKey": "..." }` after VAPID variables are configured.
- In the app, enable Push on two separate accounts/devices and send a direct message to the subscribed account. A new channel post should notify subscribers too.
- Check Render logs for `[Vibegram WebPush]` errors.

## Important security limitation

The existing Vibegram backend currently identifies clients primarily by username, without complete server-side login sessions/password/OAuth tokens. Push delivery is therefore integrated with the existing account model but is **not a substitute for authentication**. Before using this for private/public production messaging, implement real authentication and authorization for account actions and subscription management. Push notifications may show sender avatars on platforms that honor the notification `icon`; iOS can choose to present notifications using its own app/site icon presentation.

## Direct installation guide link (iPhone)

After deploying this version to Render, share this link:

`https://vibegramm.onrender.com/?install=1`

It opens a dedicated Vibegram install guide automatically. iOS does not expose an API that lets a website open Apple's native “Add to Home Screen” sheet directly; the user must tap Share in Safari, select “Add to Home Screen”, and confirm. The guide explains those exact steps. The link will only show the guide after this updated `index.html` is deployed to Render.

## Avatar synchronization and clickable profiles (this update)

This revision fixes two cross-device issues:

- New profile and channel avatars are saved as bounded image data URLs in the shared Vibegram database instead of relying on files in Render's ephemeral `uploads/` folder.
- `/api/sync` returns lightweight, versioned URLs such as `/api/avatar?username=name&v=...`; `/api/avatar` serves the image from the shared database. This keeps the sync payload smaller while the image is the same on desktop browsers, mobile browsers, the iPhone Home Screen PWA, and the Electron desktop wrapper that loads the same site.
- Existing `/uploads/...` avatars are migrated to DB storage at server startup when their legacy file is still present. If a legacy upload file was already lost during an earlier Render redeploy, that old image cannot be recovered by code; the user must upload it once again.
- In the chat list and global search, tapping an avatar or the displayed name opens that person's/channel's profile. Tapping the rest of the chat row still opens the conversation. Names and `@mentions` in messages/comments and channel posts also open profiles. Avatar/profile links support keyboard activation.
- Direct profile links can use `/?profile=username` after deployment.

### Deploy

1. Replace the deployed root `server.py` and `index.html` with these updated files. Keep `service-worker.js`, `manifest.webmanifest`, PWA icon files, `requirements.txt`, and any existing desktop files.
2. Keep Render's `DATABASE_URL` configured to the same shared PostgreSQL database used by the current service. Without shared persistent DB storage, profiles cannot reliably synchronize across devices/restarts.
3. Commit and push to GitHub, then wait for Render to deploy.
4. Test by changing an avatar on device A and opening the account on device B. A change of avatar should create a new versioned `/api/avatar` URL.
5. Search for a username. Click the avatar or display name to see the profile; click the rest of the row to open chat.

The server validates the image MIME/type signature and size before storing it. User avatars are capped at 512 KiB and channel avatars at 1 MiB. `/api/sync` no longer transfers the base64 image bytes for every user; it transfers versioned image URLs instead.
