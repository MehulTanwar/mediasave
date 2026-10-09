# MediaSave

Paste a link, confirm you have the rights, save the file. Public media only.

```
mediasave/
  frontend/   static site (HTML/CSS/JS, no build step)
  backend/    FastAPI + yt-dlp API (Docker)
  render.yaml optional one-click blueprint for Render
```

## Run locally

```bash
# Backend (Python 3.10+)
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --port 8000

# Frontend (new terminal)
cd frontend
python -m http.server 8080      # open http://localhost:8080
```
`frontend/config.js` points at `http://localhost:8000` by default.

## Deploy for free

**1. Backend on Render (free web service)**
1. Push this repo to GitHub.
2. Render → New → Blueprint (uses `render.yaml`), or New → Web Service → Docker with root `backend/`.
3. Set `ALLOWED_ORIGINS` to your frontend URL (no trailing slash).
4. Check `https://<your-service>.onrender.com/api/health`.

Fly.io, Koyeb, or any Docker host also works. Keep **one worker/instance**: the rate limiter and temp-file registry live in memory.

**2. Frontend on Cloudflare Pages / Netlify / GitHub Pages**
1. Edit `frontend/config.js` → `window.MEDIASAVE_API = "https://<your-service>.onrender.com"`.
2. Publish the `frontend/` folder (no build command). The `_headers` file adds security headers on Cloudflare Pages and Netlify.
3. Update the contact line in `about.html`.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Liveness check |
| POST | `/api/resolve` | `{url, confirm_rights}` → metadata, nothing downloaded |
| POST | `/api/prepare` | `{url, mode: "video"\|"audio", confirm_rights}` → `{download_url, filename, size_bytes, expires_in}` |
| GET | `/api/files/{token}` | Fetch the prepared file (expires after `FILE_TTL_SECONDS`) |

Errors always look like `{"error": {"code": "DRM_PROTECTED", "message": "..."}}`. Codes: `INVALID_URL`, `UNSUPPORTED_PLATFORM`, `CONSENT_REQUIRED`, `AUTH_REQUIRED`, `DRM_PROTECTED`, `GEO_RESTRICTED`, `UNAVAILABLE`, `TOO_LARGE`, `TOO_LONG`, `FORMAT_UNAVAILABLE`, `PLAYLIST_UNSUPPORTED`, `LIVE_UNSUPPORTED`, `UPSTREAM_BLOCKED`, `UPSTREAM_RATE_LIMITED`, `UPSTREAM_TIMEOUT`, `UPSTREAM_ERROR`, `RATE_LIMITED`, `BUSY`, `INVALID_REQUEST`, `FILE_EXPIRED`, `INTERNAL`.

## Built-in guardrails

- No cookies, logins or tokens accepted; yt-dlp user config is ignored.
- DRM streams refused (`allow_unplayable_formats` off); geo-bypass off.
- Domain allowlist, http/https only, no credentials or odd ports, and DNS must resolve to public IPs (SSRF defence).
- Ownership confirmation required by the API, not just the UI.
- Limits: size, duration, per-IP rate limit, concurrent jobs, single items only.
- Files live in a private temp dir and are deleted after the TTL; logs record host and outcome only.

## Honest limitations

- **Platforms may block cloud servers.** YouTube, Instagram, TikTok and others often reject datacenter IPs. Those requests return `UPSTREAM_BLOCKED` or `AUTH_REQUIRED`; the app does not try to evade this. Vimeo, Internet Archive, SoundCloud and similar tend to work more reliably.
- **Free tiers sleep** (Render ~15 min idle), so the first request can take about a minute. The UI tells users this.
- **Ephemeral disk and small RAM/CPU**: keep `MAX_FILESIZE_MB` modest.
- **No ffmpeg**, so no MP3 conversion or merging separate video+audio streams. Add `ffmpeg` to the Dockerfile if you want that.
- **yt-dlp breaks often** as sites change. Redeploy periodically to pick up the latest version.
- Platform terms (e.g. YouTube's) generally forbid downloading without permission. Consider restricting `ALLOWED_DOMAINS` to platforms where you're comfortable operating, and add a real takedown contact.
