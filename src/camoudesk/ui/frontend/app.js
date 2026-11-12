let allProfiles = [];
let currentOsFilter = "all";
let pendingDeleteId = null;
// Exit IP of the last successful proxy test, saved with the profile so launches can geo-match
// (timezone, locale, coordinates, WebRTC IP) without a network round-trip.
let lastProxyTest = null;

function showToast(msg, kind = "info") {
  const toast = document.getElementById("toast");
  toast.textContent = msg;
  toast.className = `toast show ${kind}`;
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => {
    toast.className = "toast";
  }, 4000);
}

function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

async function refreshProfiles() {
  if (!window.pywebview || !window.pywebview.api) return;
  try {
    const res = await window.pywebview.api.list_profiles();
    if (!res.ok) {
      showToast(res.error || "Failed to load profiles", "error");
      return;
    }
    allProfiles = res.data || [];
    renderProfiles();
  } catch (err) {
    console.error("Failed to fetch profiles:", err);
  }
}

function setOsFilter(filterVal, btn) {
  currentOsFilter = filterVal;
  document.querySelectorAll(".filter-pills .pill").forEach((el) => el.classList.remove("active"));
  if (btn) btn.classList.add("active");
  renderProfiles();
}

function formatFingerprintChip(p) {
  const fp = p.fingerprint_summary || {};
  const parts = [];
  if (fp.screen) parts.push(fp.screen);
  if (fp.cores) parts.push(`${fp.cores}c`);
  if (fp.gpu) parts.push(fp.gpu.replace(/^ANGLE\s*\(|\)$/g, ""));
  return parts.length > 0 ? parts.join(" · ") : "Deterministic Identity Ready";
}

function renderProfiles() {
  const tbody = document.getElementById("profiles-body");
  const empty = document.getElementById("empty-state");
  const query = (document.getElementById("search-input").value || "").trim().toLowerCase();

  let activeCount = 0;
  allProfiles.forEach((p) => {
    if (p.status === "running") activeCount++;
  });
  document.getElementById("stat-total").textContent = String(allProfiles.length);
  document.getElementById("stat-active").textContent = String(activeCount);

  const filtered = allProfiles.filter((p) => {
    if (currentOsFilter !== "all" && p.os !== currentOsFilter) return false;
    if (!query) return true;
    const hay = `${p.name} ${p.notes} ${p.proxy_masked} ${p.timezone} ${p.os}`.toLowerCase();
    return hay.includes(query);
  });

  tbody.innerHTML = "";
  if (filtered.length === 0) {
    empty.style.display = "block";
    return;
  }
  empty.style.display = "none";

  const osIcons = { macos: "🍏 macOS", windows: "🪟 Windows", linux: "🐧 Linux" };

  filtered.forEach((p) => {
    const running = p.status === "running";
    const fpChip = formatFingerprintChip(p);
    const subParts = [];
    if (p.last_used_at) subParts.push(`Last used ${p.last_used_at}`);
    else subParts.push(`Created ${p.created_at || "recently"}`);
    if (p.notes) subParts.push(p.notes);
    if (running && p.pid) subParts.push(`PID ${p.pid}${p.mode ? ` · ${p.mode}` : ""}`);

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>
        <div class="profile-cell">
          <span class="dot ${running ? "dot-running" : "dot-stopped"}"></span>
          <div>
            <div class="profile-title">${escapeHtml(p.name)}</div>
            <div class="profile-sub">${escapeHtml(subParts.join(" · "))}</div>
            ${p.last_error ? `<div class="error-line">⚠️ ${escapeHtml(p.last_error.split("\n").slice(-1)[0])}</div>` : ""}
          </div>
        </div>
      </td>
      <td>
        <div style="display:flex; flex-direction:column; gap:5px; align-items:flex-start;">
          <span class="badge">${osIcons[p.os] || escapeHtml(p.os)}</span>
          <span class="badge badge-fp" title="${escapeHtml(fpChip)}">${escapeHtml(fpChip)}</span>
        </div>
      </td>
      <td>
        <span class="badge ${p.proxy ? "badge-proxy" : "badge-direct"}" title="${escapeHtml(p.proxy_masked)}">
          ${p.proxy ? "🌐 " : ""}${escapeHtml(p.proxy_masked)}
        </span>
      </td>
      <td><span style="color: var(--text-secondary); font-size: 12px;">${escapeHtml(p.timezone)}</span></td>
      <td>
        <div class="actions">
          ${running
            ? `<button class="btn btn-stop" onclick="stopProfile('${escapeHtml(p.id)}')">⏹ Stop</button>`
            : `<button class="btn btn-start" onclick="launchProfile('${escapeHtml(p.id)}')">▶ Start</button>`
          }
          <button class="btn btn-ghost" onclick="openEditModal('${escapeHtml(p.id)}')" title="Edit Profile">✎</button>
          <button class="btn btn-ghost" onclick="openLogsModal('${escapeHtml(p.id)}')" title="View Logs">📋</button>
          <button class="btn btn-danger-ghost" onclick="askDeleteProfile('${escapeHtml(p.id)}')" title="Delete Profile">🗑</button>
        </div>
      </td>
    `;
    tbody.appendChild(tr);
  });
}

async function launchProfile(id) {
  showToast("Launching Camoufox persistent session...", "info");
  const res = await window.pywebview.api.launch_profile(id);
  if (!res.ok) {
    showToast(res.error || "Launch failed", "error");
    return;
  }
  await refreshProfiles();
}

async function stopProfile(id) {
  const res = await window.pywebview.api.stop_profile(id);
  if (!res.ok) {
    showToast(res.error || "Failed to stop browser", "error");
  }
  await refreshProfiles();
}

function openCreateModal() {
  document.getElementById("modal-title").textContent = "Create Anti-Detect Profile";
  document.getElementById("edit-profile-id").value = "";
  document.getElementById("f-name").value = "";
  document.getElementById("f-os").value = "macos";
  document.getElementById("f-locale").value = "en-US";
  document.getElementById("f-proxy").value = "";
  document.getElementById("f-tz").value = "Asia/Jakarta";
  document.getElementById("f-url").value = "https://browserleaks.com/canvas";
  document.getElementById("f-notes").value = "";
  document.getElementById("f-webrtc").checked = true;
  document.getElementById("f-humanize").checked = true;
  document.getElementById("proxy-status").innerHTML = "";
  lastProxyTest = null;
  document.getElementById("btn-regen-fp").style.display = "none";
  document.getElementById("profile-modal").classList.add("open");
}

function openEditModal(id) {
  const prof = allProfiles.find((item) => item.id === id);
  if (!prof) return;
  document.getElementById("modal-title").textContent = `Edit: ${prof.name}`;
  document.getElementById("edit-profile-id").value = prof.id;
  document.getElementById("f-name").value = prof.name;
  document.getElementById("f-os").value = prof.os || "macos";
  document.getElementById("f-locale").value = prof.locale || "en-US";
  document.getElementById("f-proxy").value = prof.proxy || "";
  ensureTimezoneOption(prof.timezone || "America/New_York");
  document.getElementById("f-tz").value = prof.timezone || "America/New_York";
  document.getElementById("f-url").value = prof.startup_url || "https://browserleaks.com/canvas";
  document.getElementById("f-notes").value = prof.notes || "";
  document.getElementById("f-webrtc").checked = prof.block_webrtc !== false;
  document.getElementById("f-humanize").checked = prof.humanize !== false;
  document.getElementById("proxy-status").innerHTML = "";
  lastProxyTest = null;
  document.getElementById("btn-regen-fp").style.display = "inline-flex";
  document.getElementById("profile-modal").classList.add("open");
}

function ensureTimezoneOption(tz) {
  const select = document.getElementById("f-tz");
  const exists = Array.from(select.options).some((o) => o.value === tz);
  if (!exists && tz) {
    const opt = document.createElement("option");
    opt.value = tz;
    opt.textContent = tz;
    select.appendChild(opt);
  }
}

function closeProfileModal() {
  document.getElementById("profile-modal").classList.remove("open");
}

async function saveProfileForm() {
  const id = document.getElementById("edit-profile-id").value.trim();
  const name = document.getElementById("f-name").value.trim();
  if (!name) {
    showToast("Profile name is required", "error");
    return;
  }

  const payload = {
    name,
    os: document.getElementById("f-os").value,
    locale: document.getElementById("f-locale").value,
    proxy: document.getElementById("f-proxy").value.trim(),
    timezone: document.getElementById("f-tz").value,
    startup_url: document.getElementById("f-url").value.trim(),
    notes: document.getElementById("f-notes").value.trim(),
    block_webrtc: document.getElementById("f-webrtc").checked,
    humanize: document.getElementById("f-humanize").checked,
  };
  if (lastProxyTest && lastProxyTest.proxy === payload.proxy) {
    payload.proxy_exit_ip = lastProxyTest.ip;
  }

  const res = id
    ? await window.pywebview.api.update_profile(id, payload)
    : await window.pywebview.api.create_profile(payload);

  if (!res.ok) {
    showToast(res.error || "Failed to save profile", "error");
    return;
  }
  closeProfileModal();
  showToast(id ? "Profile updated" : "Profile created with unique hardware identity", "success");
  await refreshProfiles();
}

async function regenerateCurrentFingerprint() {
  const id = document.getElementById("edit-profile-id").value.trim();
  if (!id) return;
  const res = await window.pywebview.api.regenerate_fingerprint(id);
  if (!res.ok) {
    showToast(res.error || "Could not regenerate fingerprint", "error");
    return;
  }
  showToast("Generated new hardware identity for this profile", "success");
  await refreshProfiles();
}

async function runProxyTest() {
  const proxyVal = document.getElementById("f-proxy").value.trim();
  const statusEl = document.getElementById("proxy-status");
  if (!proxyVal) {
    statusEl.innerHTML = '<span style="color: #f59e0b;">Enter a proxy URL first.</span>';
    return;
  }
  statusEl.innerHTML = '<span style="color: #60a5fa;">Testing proxy connection...</span>';
  const res = await window.pywebview.api.test_proxy(proxyVal);
  lastProxyTest = res.ok ? { proxy: proxyVal, ip: res.ip } : null;
  if (!res.ok) {
    statusEl.innerHTML = `<span style="color: #ef4444;">Failed: ${escapeHtml(res.error)}</span>`;
    return;
  }
  const loc = [res.city, res.country].filter(Boolean).join(", ");
  statusEl.innerHTML = `<span style="color: #10b981;">Connected! IP: <strong>${escapeHtml(res.ip)}</strong>${loc ? ` (${escapeHtml(loc)})` : ""} · ${res.latency_ms}ms</span>`;
  if (res.timezone) {
    ensureTimezoneOption(res.timezone);
    document.getElementById("f-tz").value = res.timezone;
    showToast(`Auto-matched timezone: ${res.timezone}`, "success");
  }
}

async function openLogsModal(id) {
  const prof = allProfiles.find((p) => p.id === id);
  document.getElementById("logs-title").textContent = `Logs — ${prof ? prof.name : id}`;
  const res = await window.pywebview.api.get_profile_logs(id);
  const content = res.ok && res.data ? res.data : "(No worker logs recorded yet for this profile.)";
  document.getElementById("logs-content").textContent = content;
  document.getElementById("logs-modal").classList.add("open");
}

function closeLogsModal() {
  document.getElementById("logs-modal").classList.remove("open");
}

function askDeleteProfile(id) {
  const prof = allProfiles.find((p) => p.id === id);
  pendingDeleteId = id;
  document.getElementById("confirm-text").textContent =
    `Delete "${prof ? prof.name : id}" and remove its saved cookies, localStorage, and fingerprint identity from disk?`;
  document.getElementById("confirm-modal").classList.add("open");
}

function closeConfirmModal() {
  pendingDeleteId = null;
  document.getElementById("confirm-modal").classList.remove("open");
}

document.getElementById("confirm-ok-btn").addEventListener("click", async () => {
  if (!pendingDeleteId) return;
  const target = pendingDeleteId;
  closeConfirmModal();
  const res = await window.pywebview.api.delete_profile(target);
  if (!res.ok) {
    showToast(res.error || "Delete failed", "error");
    return;
  }
  showToast("Profile removed", "info");
  await refreshProfiles();
});

window.addEventListener("pywebviewready", async () => {
  try {
    const info = await window.pywebview.api.get_app_info();
    if (info.ok && info.data) {
      document.getElementById("app-subtitle").textContent =
        `v${info.data.version} · Open-Source Anti-Detect Manager · Apple Silicon`;
    }
  } catch (err) {
    console.warn(err);
  }
  await refreshProfiles();
  setInterval(refreshProfiles, 2000);
});
