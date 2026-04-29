# iCare Video Platform — Integration Guide for Base44 & Wix

**Backend URL:** `http://35.154.164.178:8000`  
**API Docs (Swagger):** `http://35.154.164.178:8000/docs`

---

## Architecture Overview

```
Base44  →  Admin panel: creates users, grants 90-day access, batch onboarding
Wix     →  Student web interface: login, watch videos, track progress
Backend →  Validates access, issues secure tokens, tracks progress
Mux     →  Streams the actual video
```

---

## PART 1 — BASE44 INTEGRATION

Base44 is the **admin panel**. It does not log in as a student. It manages users and access.

### What Base44 needs to do

---

### 1.1 Batch Onboard Caregivers (Primary Flow)

**Endpoint:** `POST /batch/upload`  
**Auth:** None required (admin action)  
**Content-Type:** `multipart/form-data`

**CSV File Format** (columns required):
```
email,name
john@example.com,John Smith
jane@example.com,Jane Doe
```

**Form fields:**
| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `file` | CSV file | Yes | — | Must be `.csv` |
| `course_id` | integer | No | `1` | Which course to grant access to |

**What the backend does:**
- Creates a User account for each row
- Auto-generates a temporary password
- Grants 90-day course access starting from today
- Skips duplicates (email already registered)

**Sample Response:**
```json
{
  "created": 45,
  "skipped": 2,
  "failed": 0,
  "users": [
    { "email": "john@example.com", "name": "John Smith", "temp_password": "abc123", "access_end": "2026-07-28" }
  ]
}
```

**Base44 Action Required:**
- Build a form with a CSV file picker and optional `course_id` input
- Call `POST /batch/upload` with the file
- Display the response — especially the `temp_password` for each user (Base44 must share these with caregivers)

---

### 1.2 Register a Single User (Optional)

**Endpoint:** `POST /auth/register`  
**Auth:** None required  
**Content-Type:** `application/json`

**Request Body:**
```json
{
  "email": "caregiver@example.com",
  "name": "Jane Doe",
  "password": "their-password"
}
```

**Response:**
```json
{
  "status": "registered",
  "user_id": 42,
  "email": "caregiver@example.com"
}
```

**Base44 Action Required:**
- Optional: build a single-user registration form for one-off onboarding
- After registering, Base44 must separately grant course access (no endpoint yet — see Gaps section)

---

### 1.3 What Base44 Does NOT Need

- Base44 does **not** need to log in with a JWT
- Base44 does **not** call the video play/progress endpoints
- Base44 does **not** interact with Mux directly

---

## PART 2 — WIX INTEGRATION

Wix is the **student-facing web interface**. Students log in on Wix and watch videos.

### JWT Flow Summary

```
Student opens Wix page
    → Wix calls POST /auth/login  (email + password)
    → Backend returns access_token (JWT, valid 24 hours)
    → Wix stores access_token in memory/session

Student clicks a video
    → Wix calls GET /videos/{vimeo_id}/play  with Authorization header
    → Backend checks: is user's 90-day window still active?
    → If yes: returns secure_stream_url + resume_from_seconds
    → Wix loads Mux player with that URL, seeks to resume point

Every 15 seconds while watching
    → Wix calls POST /videos/{vimeo_id}/progress
    → Backend saves progress, marks complete at 95%
```

---

### 2.1 Login

**Endpoint:** `POST /auth/login`  
**Auth:** None  
**Content-Type:** `application/json`

**Request Body:**
```json
{
  "email": "caregiver@example.com",
  "password": "their-password"
}
```

**Success Response (200):**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in_hours": 24,
  "user_id": 42,
  "name": "Jane Doe"
}
```

**Error Responses:**
| Status | Meaning |
|---|---|
| 401 | Wrong email or password |

**Wix Action Required:**
- Build a login form (email + password fields)
- On submit, call `POST /auth/login`
- Store `access_token` in Wix memory (e.g., `wixStorage` or a page variable)
- Store `user_id` and `name` to display a welcome message
- Token expires in 24 hours — if a video request returns 401, redirect to login

---

### 2.2 Get Secure Video URL (Before Playing)

**Endpoint:** `GET /videos/{vimeo_id}/play`  
**Auth:** Required — `Authorization: Bearer <access_token>`  
**Query Params:** `course_id` (optional, default `1`)

**Example Request:**
```
GET /videos/123456789/play
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

**Success Response (200):**
```json
{
  "status": "success",
  "vimeo_id": "123456789",
  "title": "Module 1: Introduction",
  "playback_id": "abc123",
  "secure_stream_url": "https://stream.mux.com/abc123.m3u8?token=eyJ...",
  "playback_token": "eyJ...",
  "token_expires_in_hours": 6,
  "resume_from_seconds": 240,
  "is_completed": false,
  "drm_enabled": true
}
```

**Error Responses:**
| Status | Meaning | Wix Should Do |
|---|---|---|
| 401 | JWT expired or missing | Redirect to login |
| 403 | No course access | Show "No access" message |
| 403 | 90-day window expired | Show "Access expired" message |
| 404 | Video not found | Show error |

**Wix Action Required:**
- When student clicks a video, call this endpoint first (never hardcode a Mux URL)
- Use `secure_stream_url` as the Mux player source — this URL already includes the token
- Use `resume_from_seconds` to seek the player to the right position on load
- Use `is_completed` to show a "completed" badge if true, and start from beginning if true

**Mux Player Setup (Wix Velo):**
```javascript
import { fetch } from 'wix-fetch';

// When video card is clicked:
async function loadVideo(vimeoId) {
  const token = memory.getItem('access_token'); // wherever you stored it

  const res = await fetch(`https://35.154.164.178:8000/videos/${vimeoId}/play`, {
    headers: { 'Authorization': `Bearer ${token}` }
  });

  if (res.status === 401) { // token expired
    // redirect to login
    return;
  }

  if (res.status === 403) {
    // show access denied message
    return;
  }

  const data = await res.json();

  // Set Mux player source
  $w('#muxPlayer').setAttribute('src', data.secure_stream_url);

  // Seek to resume point after player loads
  $w('#muxPlayer').onReady(() => {
    $w('#muxPlayer').currentTime = data.resume_from_seconds;
  });
}
```

---

### 2.3 Track Progress (Every 15 Seconds)

**Endpoint:** `POST /videos/{vimeo_id}/progress`  
**Auth:** Required — `Authorization: Bearer <access_token>`  
**Content-Type:** `application/json`

**Request Body:**
```json
{
  "current_time": 240,
  "total_duration": 3600,
  "device_type": "web",
  "session_id": "optional-session-identifier"
}
```

**Response:**
```json
{
  "status": "success",
  "recorded_seconds": 240,
  "is_completed": false
}
```

**Wix Action Required:**
- Start a `setInterval` when the player starts playing, fire every 15 seconds
- Send `current_time` (player's `currentTime`) and `total_duration` (player's `duration`)
- When `is_completed` returns `true` for the first time, show a completion message or badge
- Stop the interval when the player is paused or the page unloads

**Wix Velo Example:**
```javascript
let progressInterval = null;

function startProgressTracking(vimeoId, token) {
  progressInterval = setInterval(async () => {
    const currentTime = Math.floor($w('#muxPlayer').currentTime);
    const totalDuration = Math.floor($w('#muxPlayer').duration);

    if (!currentTime || !totalDuration) return;

    await fetch(`https://35.154.164.178:8000/videos/${vimeoId}/progress`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        current_time: currentTime,
        total_duration: totalDuration,
        device_type: 'web'
      })
    });
  }, 15000); // every 15 seconds
}

function stopProgressTracking() {
  if (progressInterval) {
    clearInterval(progressInterval);
    progressInterval = null;
  }
}

// Hook these to player events:
$w('#muxPlayer').onPlay(() => startProgressTracking(vimeoId, token));
$w('#muxPlayer').onPause(() => stopProgressTracking());
$w('#muxPlayer').onEnded(() => stopProgressTracking());
```

---

## PART 3 — WHAT IS NOT YET BUILT (Gaps)

These endpoints do not exist yet. They may be needed depending on how Base44 and Wix are built out.

| Missing Feature | Who Needs It | Notes |
|---|---|---|
| `GET /videos` — list all videos | Wix | Needed to build a course catalog/video list page |
| `GET /videos/{vimeo_id}/progress` — fetch a user's progress | Wix | Needed to show completion badges on the catalog page before the user clicks a video |
| Grant/revoke course access for a single user | Base44 | Currently only batch upload grants access |
| Extend a user's 90-day window | Base44 | No endpoint to update `access_end` date |
| List all users + their access status | Base44 | No admin dashboard endpoint |
| Password reset / forgot password | Wix | Not built |

**These can be built on request.**

---

## PART 4 — AUTHENTICATION HEADER REFERENCE

Every protected endpoint requires this header:

```
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

- Token is obtained from `POST /auth/login`
- Token is valid for **24 hours**
- If a request returns **401**, the token has expired — the user must log in again
- If a request returns **403**, the token is valid but the user lacks access

---

## PART 5 — ENDPOINT QUICK REFERENCE

| Endpoint | Method | Auth | Used By | Purpose |
|---|---|---|---|---|
| `/auth/register` | POST | No | Base44 | Create single user |
| `/auth/login` | POST | No | Wix / Mobile | Login, get JWT |
| `/batch/upload` | POST | No | Base44 | Bulk create users from CSV |
| `/videos/{id}/play` | GET | Yes (JWT) | Wix | Get secure stream URL + resume point |
| `/videos/{id}/progress` | POST | Yes (JWT) | Wix | Save playback progress |
| `/videos/{id}/download` | GET | Yes (JWT) | Mobile App | Get offline download URL + DRM license |
