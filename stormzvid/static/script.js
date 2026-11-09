/* StormzVid – front-end logic (shared by every page)
 *
 * Downloader pages:  paste link -> POST /api/info (metadata) -> choose format ->
 *                    POST /api/prepare (server downloads + processes) -> GET /api/file/<token> (browser saves it)
 * Explore page:      live search filter over the platform cards
 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const root = document.documentElement;

  // ---------- Theme (dark by default, remembered in localStorage) ----------
  function initTheme() {
    const btn = $("#themeToggle");
    if (!btn) return;
    const label = () => root.getAttribute("data-theme") === "dark" ? "Switch to light mode" : "Switch to dark mode";
    btn.setAttribute("aria-label", label());
    btn.addEventListener("click", () => {
      const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      btn.setAttribute("aria-label", label());
      try { localStorage.setItem("stormzvid-theme", next); } catch (_) { /* storage may be blocked */ }
    });
  }

  // ---------- Explore page: filter cards as you type ----------
  function initExplore() {
    const search = $("#exploreSearch");
    if (!search) return;
    const cards = Array.from(document.querySelectorAll("#exploreGrid .pcard"));
    const empty = $("#exploreEmpty");
    search.addEventListener("input", () => {
      const q = search.value.trim().toLowerCase();
      let shown = 0;
      cards.forEach((card) => {
        const match = !q || card.dataset.search.includes(q);
        card.hidden = !match;
        if (match) shown++;
      });
      empty.hidden = shown > 0;
    });
  }

  // ---------- Downloader pages ----------
  function initDownloader() {
    const form = $("#urlForm");
    const configEl = $("#stormz-config");
    if (!form || !configEl) return;

    const config = JSON.parse(configEl.textContent);  // { platform, name, platforms: {slug: {name, domains, url}} }
    const input = $("#urlInput");
    const fetchBtn = $("#fetchBtn");
    const errorBox = $("#errorBox");
    const errorText = $("#errorText");
    const mismatchBox = $("#mismatchBox");
    const mismatchText = $("#mismatchText");
    const mismatchLink = $("#mismatchLink");
    const loadingBox = $("#loadingBox");
    const loadingText = $("#loadingText");
    const resultCard = $("#resultCard");
    const thumb = $("#thumb");
    const durationEl = $("#duration");
    const platformPill = $("#platformPill");
    const titleEl = $("#videoTitle");
    const metaEl = $("#videoMeta");
    const formatList = $("#formatList");
    const downloadBtn = $("#downloadBtn");
    const downloadStatus = $("#downloadStatus");

    // ----- Platform detection (mirrors the server's rules; the server is the real gatekeeper) -----
    function normalizeUrl(raw) {
      const value = raw.trim();
      if (!value) return "";
      return /^https?:\/\//i.test(value) ? value : "https://" + value;
    }

    function detectPlatform(raw) {
      try {
        const host = new URL(normalizeUrl(raw)).hostname.toLowerCase();
        for (const [slug, p] of Object.entries(config.platforms)) {
          if (p.domains.some((d) => host === d || host.endsWith("." + d))) return slug;
        }
      } catch (_) { /* not a URL yet */ }
      return null;
    }

    // If the pasted link belongs to another platform, return that platform's info.
    function wrongPlatform(raw) {
      const slug = detectPlatform(raw);
      return slug && slug !== config.platform ? config.platforms[slug] : null;
    }

    // ----- UI helpers -----
    function showMismatch(message, other) {
      mismatchText.textContent = message;
      mismatchLink.textContent = `Open the ${other.name} downloader →`;
      mismatchLink.href = other.url;
      mismatchBox.hidden = false;
    }
    function hideMismatch() { mismatchBox.hidden = true; }

    function showError(message) {
      errorText.textContent = message;
      errorBox.hidden = false;
    }
    function clearMessages() {
      errorBox.hidden = true;
      errorText.textContent = "";
      hideMismatch();
    }

    function setLoading(text) {
      if (text) { loadingText.textContent = text; loadingBox.hidden = false; }
      else { loadingBox.hidden = true; }
    }

    function setButtonLoading(btn, loading) {
      btn.classList.toggle("is-loading", loading);
      btn.disabled = loading;
    }

    function setStatus(text, kind) {
      downloadStatus.textContent = text || "";
      downloadStatus.classList.toggle("is-error", kind === "error");
      downloadStatus.classList.toggle("is-ok", kind === "ok");
    }

    function formatDuration(seconds) {
      if (!seconds || seconds < 0) return "";
      const s = Math.round(seconds);
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      const sec = String(s % 60).padStart(2, "0");
      return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
    }

    function formatSize(bytes) {
      if (!bytes) return "";
      const mb = bytes / (1024 * 1024);
      return mb >= 1024 ? `~${(mb / 1024).toFixed(1)} GB` : `~${Math.max(1, Math.round(mb))} MB`;
    }

    async function postJSON(path, body) {
      let res;
      try {
        res = await fetch(path, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...body, platform: config.platform }),
        });
      } catch (_) {
        throw new Error("Can't reach the server. Check that the app is running and try again.");
      }
      let data = {};
      try { data = await res.json(); } catch (_) { /* non-JSON error page */ }
      if (!res.ok) {
        const err = new Error(data.error || "Something went wrong. Please try again.");
        err.suggest = data.suggest || null;  // set when the link belongs on another platform's page
        throw err;
      }
      return data;
    }

    function reportError(err) {
      if (err.suggest) showMismatch(err.message, err.suggest);
      else showError(err.message);
    }

    // ----- State -----
    let current = null;   // { url, quality }
    let busy = false;

    // Warn as soon as a link from a different platform is pasted.
    input.addEventListener("input", () => {
      const other = wrongPlatform(input.value);
      if (other) showMismatch(`This is a ${other.name} link, but this page only accepts ${config.name} links.`, other);
      else hideMismatch();
    });

    // ----- Step 1: fetch video info -----
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (busy) return;

      const url = normalizeUrl(input.value);
      clearMessages();
      resultCard.hidden = true;
      current = null;

      if (!url) { showError("Please paste a video link first."); input.focus(); return; }

      const other = wrongPlatform(url);
      if (other) {
        showMismatch(`This is a ${other.name} link, but this page only accepts ${config.name} links.`, other);
        return;
      }

      busy = true;
      setButtonLoading(fetchBtn, true);
      setLoading("Reading the link…");

      try {
        renderResult(await postJSON("/api/info", { url }));
      } catch (err) {
        reportError(err);
      } finally {
        busy = false;
        setButtonLoading(fetchBtn, false);
        setLoading(null);
      }
    });

    function renderResult(info) {
      titleEl.textContent = info.title;
      platformPill.textContent = info.platform_name;
      metaEl.textContent = info.uploader || "";
      metaEl.hidden = !info.uploader;

      // Thumbnail (hidden gracefully if the CDN refuses to serve it)
      thumb.onerror = () => { thumb.hidden = true; };
      thumb.hidden = !info.thumbnail;
      if (info.thumbnail) thumb.src = info.thumbnail; else thumb.removeAttribute("src");
      thumb.alt = info.title;

      const duration = formatDuration(info.duration);
      durationEl.textContent = duration;
      durationEl.hidden = !duration;

      // Format chips. Default: highest quality up to 1080p, otherwise the first option.
      formatList.replaceChildren();
      const options = info.formats || [];
      const preferred =
        options.find((f) => /^\d+$/.test(f.quality) && Number(f.quality) <= 1080) || options[0];
      current = { url: info.url, quality: preferred ? preferred.quality : "best" };

      options.forEach((opt) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "chip";
        chip.setAttribute("role", "radio");
        chip.dataset.quality = opt.quality;
        chip.setAttribute("aria-checked", String(opt.quality === current.quality));
        chip.append(opt.label);
        const size = formatSize(opt.size);
        if (size) {
          const small = document.createElement("small");
          small.textContent = size;
          chip.append(small);
        }
        chip.addEventListener("click", () => {
          current.quality = opt.quality;
          formatList.querySelectorAll(".chip").forEach((c) =>
            c.setAttribute("aria-checked", String(c === chip)));
          setStatus("");
        });
        formatList.append(chip);
      });

      setStatus("");
      resultCard.hidden = false;
      resultCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    // ----- Step 2 + 3: prepare on the server, then let the browser save the file -----
    downloadBtn.addEventListener("click", async () => {
      if (busy || !current) return;
      busy = true;
      setButtonLoading(downloadBtn, true);
      setStatus("Processing on the server – longer videos can take a minute…");
      clearMessages();

      try {
        const job = await postJSON("/api/prepare", { url: current.url, quality: current.quality });

        const link = document.createElement("a");
        link.href = "/api/file/" + encodeURIComponent(job.token);
        link.download = job.filename || "";
        document.body.appendChild(link);
        link.click();
        link.remove();

        setStatus("Done – your download should start now.", "ok");
      } catch (err) {
        setStatus(err.message, "error");
      } finally {
        busy = false;
        setButtonLoading(downloadBtn, false);
      }
    });
  }

  initTheme();
  initExplore();
  initDownloader();
})();
