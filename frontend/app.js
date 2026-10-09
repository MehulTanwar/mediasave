(() => {
  "use strict";
  const API = (window.MEDIASAVE_API || "").replace(/\/$/, "");
  const $ = (id) => document.getElementById(id);
  const form = $("dl-form"), urlInput = $("url"), consent = $("consent"), go = $("go");
  const urlErr = $("url-error"), consentErr = $("consent-error");
  const statusEl = $("status"), errorEl = $("error"), result = $("result");
  const thumbSlot = $("thumb-slot"), rTitle = $("r-title"), rMeta = $("r-meta");
  const save = $("save"), ready = $("ready"), fine = $("r-fine");

  class ApiError extends Error {
    constructor(code, message) { super(message); this.code = code; }
  }

  // Short, specific next steps for known error codes.
  const HINTS = {
    AUTH_REQUIRED: "Only public media can be downloaded. Choose media that does not need a login.",
    DRM_PROTECTED: "DRM-protected media can't be downloaded here.",
    GEO_RESTRICTED: "This tool doesn't bypass regional restrictions.",
    TOO_LARGE: "Try a shorter item or choose Audio only.",
    FORMAT_UNAVAILABLE: "Try Audio only, or a shorter item.",
    TOO_LONG: "Try a shorter item.",
    UNSUPPORTED_PLATFORM: "See the supported platforms section for the full list.",
    UPSTREAM_BLOCKED: "Some platforms block requests from servers. This usually clears up later.",
    BUSY: "Wait a minute, then press Download again.",
    RATE_LIMITED: "Wait a minute, then press Download again.",
    NETWORK: "The server may be waking up. Wait a moment and try again.",
  };

  async function api(path, { method = "GET", body, timeout = 30000 } = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    try {
      const res = await fetch(API + path, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
        signal: ctrl.signal,
      });
      let data = null;
      try { data = await res.json(); } catch { /* non-JSON response */ }
      if (!res.ok) {
        throw new ApiError(data?.error?.code || `HTTP_${res.status}`,
          data?.error?.message || `The server returned an error (${res.status}).`);
      }
      return data;
    } catch (e) {
      if (e instanceof ApiError) throw e;
      if (e.name === "AbortError") throw new ApiError("TIMEOUT", "The request took too long. Try again, or try a shorter item.");
      throw new ApiError("NETWORK", "Can't reach the MediaSave server. Check your connection and try again.");
    } finally {
      clearTimeout(timer);
    }
  }

  const fmtDuration = (s) => {
    if (!s) return "";
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
  };
  const fmtSize = (b) => (b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`);

  function showField(el, msg) { el.textContent = msg; el.hidden = !msg; }
  function clearAll() {
    showField(urlErr, ""); showField(consentErr, "");
    errorEl.hidden = true; errorEl.textContent = "";
    result.hidden = true; save.hidden = true; ready.hidden = true; fine.hidden = true;
    statusEl.textContent = "";
  }
  function showError(err) {
    statusEl.textContent = "";
    errorEl.replaceChildren();
    const strong = document.createElement("strong");
    strong.textContent = err.message;
    errorEl.append(strong);
    if (HINTS[err.code]) errorEl.append(HINTS[err.code]);
    errorEl.hidden = false;
  }
  function setBusy(busy) {
    go.disabled = busy;
    go.textContent = busy ? "Working…" : "Download";
  }
  function renderPreview(info) {
    thumbSlot.replaceChildren();
    if (info.thumbnail) {
      const img = document.createElement("img");
      img.src = info.thumbnail; img.alt = ""; img.loading = "lazy"; img.referrerPolicy = "no-referrer";
      img.onerror = () => { thumbSlot.replaceChildren(Object.assign(document.createElement("div"), { className: "noimg" })); };
      thumbSlot.append(img);
    } else {
      thumbSlot.append(Object.assign(document.createElement("div"), { className: "noimg" }));
    }
    rTitle.textContent = info.title;
    rMeta.textContent = [info.platform, info.uploader, fmtDuration(info.duration)].filter(Boolean).join(" · ");
    result.hidden = false;
  }

  let running = false;
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (running) return;
    clearAll();

    const raw = urlInput.value.trim();
    let parsed;
    try { parsed = new URL(raw); } catch { /* handled below */ }
    if (!parsed || !/^https?:$/.test(parsed.protocol)) {
      showField(urlErr, "Paste a full link that starts with http:// or https://.");
      urlInput.focus(); return;
    }
    if (!consent.checked) {
      showField(consentErr, "Confirm that you own this media or have permission to download it.");
      consent.focus(); return;
    }

    const mode = form.elements.mode.value;
    const payload = { url: raw, confirm_rights: true };
    running = true; setBusy(true);
    const wake = setTimeout(() => { statusEl.textContent = "Still working. The free server may be waking up, which can take up to a minute."; }, 6000);

    try {
      statusEl.textContent = "Checking the link…";
      const info = await api("/api/resolve", { method: "POST", body: payload });
      if ((mode === "audio" && !info.has_audio) || (mode === "video" && !info.has_video)) {
        throw new ApiError("FORMAT_UNAVAILABLE", `This media has no ${mode} to save.`);
      }
      renderPreview(info);

      statusEl.textContent = "Preparing your file. Larger files take longer…";
      const out = await api("/api/prepare", { method: "POST", body: { ...payload, mode }, timeout: 170000 });

      statusEl.textContent = "";
      save.href = API + out.download_url;
      save.setAttribute("download", out.filename);
      save.textContent = `Save file (${fmtSize(out.size_bytes)})`;
      save.hidden = false; ready.hidden = false;
      fine.textContent = `This link expires in ${Math.round(out.expires_in / 60)} minutes.`;
      fine.hidden = false;
      save.focus();
    } catch (err) {
      result.hidden = true;
      showError(err instanceof ApiError ? err : new ApiError("UNKNOWN", "Something unexpected happened. Try again."));
    } finally {
      clearTimeout(wake); running = false; setBusy(false);
    }
  });

  urlInput.addEventListener("input", () => { showField(urlErr, ""); });
  consent.addEventListener("change", () => { showField(consentErr, ""); });
})();
