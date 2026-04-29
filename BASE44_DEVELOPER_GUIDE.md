# iCare Video Platform — Base44 Frontend Developer Guide

**Version:** 1.0  
**Backend Base URL:** `http://35.154.164.178:8000`  
**Swagger / API Docs:** `http://35.154.164.178:8000/docs`

---

## Overview

The Base44 mobile app (Android + iOS) is responsible for:
- Student login and session management (handled entirely by Base44)
- Displaying the video course catalog
- Streaming videos online
- Downloading videos for offline playback with DRM protection
- Enforcing 90-day access windows (handled entirely by Base44)

The backend's only job is to receive a request with the API key and return a secure Mux URL. All user management, access control, and session logic stays inside Base44.

---

## Authentication — API Key

Every single request to the backend **must** include this header:

```
X-API-Key: sk_icare_1b75de18308eb135e2df9ef29aef825266eea22041f8e4a9
```

- Store this key securely in your app (environment config, not hardcoded in source)
- If the key is missing or wrong, the backend returns `403 Forbidden`
- This key does not expire but treat it as a secret — do not expose it in logs or client-side JS

---

## The Video Identifier — `vimeo_id`

All backend endpoints use `vimeo_id` to identify a video. This is the numeric Vimeo ID (e.g., `123456789`).

You will receive a **full mapping spreadsheet** (Excel) from us with every video's:
- `vimeo_id` — use this in all API calls
- Title
- Folder / module grouping

Build your in-app course catalog from this spreadsheet. The backend does not have a "list videos" endpoint — the catalog is managed on your side.

---

## Endpoints

---

### 1. Get Secure Stream URL (Online Watching)

Call this when a student taps a video to watch it online.

**Request:**
```
GET /videos/{vimeo_id}/play
X-API-Key: sk_icare_1b75de18308eb135e2df9ef29aef825266eea22041f8e4a9
```

**Example:**
```
GET /videos/123456789/play
```

**Success Response (200):**
```json
{
  "status": "success",
  "vimeo_id": "123456789",
  "title": "Module 1: Introduction to Care",
  "playback_id": "abc123xyz",
  "secure_stream_url": "https://stream.mux.com/abc123xyz.m3u8?token=eyJ...",
  "playback_token": "eyJ...",
  "token_expires_in_hours": 6,
  "drm_enabled": true,
  "drm_license_token": "eyJ...",
  "drm_license_url": "https://license.mux.com/license/widevine/abc123xyz?token=eyJ..."
}
```

**Field Reference:**
| Field | Use |
|---|---|
| `secure_stream_url` | Feed this directly into the Mux player as the video source |
| `token_expires_in_hours` | Token is valid for 6 hours — if student watches past expiry, call this endpoint again |
| `drm_enabled` | `true` in production — means DRM is active |
| `drm_license_token` | Pass to the Mux player's DRM config for key decryption |
| `drm_license_url` | Widevine license server URL — use for Android DRM config |

**Error Responses:**
| Status | Meaning |
|---|---|
| 403 | API key missing or wrong |
| 404 | `vimeo_id` not found in the database |
| 400 | Video exists but is still processing in Mux |

---

### 2. Get Offline Download URL (Offline Watching)

Call this when a student taps "Download" on a video. The download URL expires in **1 hour** — the app must start the download immediately after receiving it.

**Request:**
```
GET /videos/{vimeo_id}/download
X-API-Key: sk_icare_1b75de18308eb135e2df9ef29aef825266eea22041f8e4a9
```

**Example:**
```
GET /videos/123456789/download
```

**Success Response (200):**
```json
{
  "status": "success",
  "vimeo_id": "123456789",
  "title": "Module 1: Introduction to Care",
  "download_url": "https://stream.mux.com/xyz789.high.mp4?token=eyJ...",
  "token_expires_in_hours": 1,
  "drm_enabled": true,
  "drm_offline_license_token": "eyJ...",
  "drm_widevine_license_url": "https://license.mux.com/license/widevine/xyz789?token=eyJ...",
  "drm_fairplay_license_url": "https://license.mux.com/license/fairplay/xyz789?token=eyJ...",
  "drm_fairplay_cert_url": "https://license.mux.com/fairplay/cert"
}
```

**Field Reference:**
| Field | Platform | Use |
|---|---|---|
| `download_url` | Both | The signed MP4 to download — expires in 1 hour, download immediately |
| `drm_offline_license_token` | Both | Present this to the license server to get a persistent offline DRM license |
| `drm_widevine_license_url` | Android | Widevine license server URL — use in ExoPlayer's offline license request |
| `drm_fairplay_license_url` | iOS | FairPlay license server URL — use in AVAssetDownloadTask DRM config |
| `drm_fairplay_cert_url` | iOS | FairPlay certificate URL — fetch this first before requesting the license |
| `token_expires_in_hours` | Both | Download URL expires in 1 hour — do not cache this URL |

**Error Responses:**
| Status | Meaning |
|---|---|
| 403 | API key missing or wrong |
| 404 | Video not found |
| 400 | Video has no signed/DRM playback ID yet |

---

## DRM Integration

DRM is **active in production**. Videos are Widevine (Android) + FairPlay (iOS) protected.

---

### Android — Widevine (ExoPlayer)

**Online streaming:**
```kotlin
val mediaItem = MediaItem.Builder()
    .setUri(secureStreamUrl) // from /play response
    .setDrmConfiguration(
        MediaItem.DrmConfiguration.Builder(C.WIDEVINE_UUID)
            .setLicenseUri(drmLicenseUrl) // from /play response: drm_license_url
            .build()
    )
    .build()

player.setMediaItem(mediaItem)
```

**Offline download + license:**
```kotlin
// Step 1: Download the MP4
// Use download_url from /download response — start immediately, expires in 1 hour

// Step 2: Fetch offline Widevine license
val drmSessionManager = DefaultDrmSessionManager.Builder()
    .build(FrameworkMediaDrm.DEFAULT_PROVIDER)

// Make a license request to drm_widevine_license_url
// with drm_offline_license_token in the request header:
// "Authorization: Bearer <drm_offline_license_token>"
// License is valid for 48 hours — enough time to complete download
```

---

### iOS — FairPlay (AVPlayer / AVAssetDownloadTask)

**Online streaming:**
```swift
// Step 1: Fetch the FairPlay certificate
// GET drm_fairplay_cert_url (from /play response)

// Step 2: Configure AVPlayer with FairPlay
let asset = AVURLAsset(url: URL(string: secureStreamUrl)!)
// Implement AVContentKeySessionDelegate
// Use drm_fairplay_license_url as the license server
// Present drm_license_token when the key server requests it
```

**Offline download + license:**
```swift
// Step 1: Download using AVAssetDownloadTask
// Use download_url from /download response — start immediately

// Step 2: Request persistent FairPlay license
// Use drm_fairplay_license_url with drm_offline_license_token
// Use drm_fairplay_cert_url to fetch the FairPlay cert first
// License is valid for 48 hours
```

---

## Recommended App Flow

```
App launches
    → Base44 handles login (your own auth system)
    → Student is authenticated in Base44

Student opens course catalog
    → Load video list from your Base44 database (vimeo_id + title per video)
    → Show list with title, thumbnail, and module grouping

Student taps a video to WATCH
    → Call GET /videos/{vimeo_id}/play  with X-API-Key header
    → Use secure_stream_url as the player source
    → Configure DRM with drm_license_url + drm_license_token
    → Play video

Student taps DOWNLOAD
    → Call GET /videos/{vimeo_id}/download  with X-API-Key header
    → Immediately start downloading download_url (expires in 1 hour)
    → Fetch offline DRM license from drm_widevine_license_url (Android)
      or drm_fairplay_license_url (iOS) using drm_offline_license_token
    → Store encrypted video + license on device

Student watches OFFLINE
    → Load downloaded video from local storage
    → DRM license on device decrypts and plays it
    → No backend call needed

90-day access expiry
    → Enforced entirely by Base44
    → When access expires, stop showing the Watch/Download buttons
    → No backend involvement needed
```

---

## Important Notes

1. **Never cache the stream URL or download URL** — they expire (6 hours for streaming, 1 hour for download). Always call the backend fresh when a student wants to watch or download.

2. **Download URL expires in 1 hour** — call `/download` only when the student actually taps the download button, then start the download immediately.

3. **DRM license for offline is valid 48 hours** — fetch the license right after starting the download, not later.

4. **The `vimeo_id` is just a number** (e.g., `123456789`) — it comes from the mapping spreadsheet we provide. Build your catalog from that spreadsheet.

5. **90-day access control is 100% Base44's responsibility** — the backend does not check user access. Your app must not show Watch/Download options to students whose window has expired.

6. **API key is a shared secret** — store it in your app's secure config (e.g., environment variables or a secrets manager), never in plain source code or logs.

---

## Quick Reference

| What | Endpoint | Method | Auth |
|---|---|---|---|
| Watch a video online | `/videos/{vimeo_id}/play` | GET | X-API-Key header |
| Download a video offline | `/videos/{vimeo_id}/download` | GET | X-API-Key header |

**API Key:**
```
sk_icare_1b75de18308eb135e2df9ef29aef825266eea22041f8e4a9
```

**Backend URL:**
```
http://35.154.164.178:8000
```

**Swagger (test all endpoints live):**
```
http://35.154.164.178:8000/docs
```
