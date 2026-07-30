const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const PAGE_META = {
  grids: ["GRID LIBRARY", "宮格庫"],
  capture: ["CAPTURE 01", "建立宮格"],
  profiles: ["FOLDER PROFILES", "處理資料夾"],
  jobs: ["PIPELINE JOBS", "下載工作"],
  library: ["VIDEO LIBRARY", "影片庫"],
  settings: ["LOCAL SETTINGS", "設定"],
};

function readLocalState() {
  try {
    const value = JSON.parse(localStorage.getItem("muse-ui") || "{}");
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}

const saved = readLocalState();
const state = {
  route: "grids",
  theme: saved.theme === "light" ? "light" : "dark",
  blur: Boolean(saved.blur),
  rememberProgress: saved.rememberProgress !== false,
  autoSubtitle: saved.autoSubtitle !== false,
  denseGrids: Boolean(saved.denseGrids),
  settings: null,
  profiles: [],
  summary: null,
  grids: [],
  gridMap: new Map(),
  gridPage: 1,
  gridHasMore: false,
  gridSelection: new Set(),
  currentGrid: null,
  videos: [],
  videoMap: new Map(),
  videoPage: 1,
  videoHasMore: false,
  currentVideo: null,
  jobs: [],
  jobTimer: null,
  searchTimer: null,
};

function persistLocal() {
  try {
    localStorage.setItem("muse-ui", JSON.stringify({
      theme: state.theme,
      blur: state.blur,
      rememberProgress: state.rememberProgress,
      autoSubtitle: state.autoSubtitle,
      denseGrids: state.denseGrids,
    }));
  } catch {}
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>"']/g, character => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[character]);
}

function formatDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return "未知片長";
  const rounded = Math.round(value);
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const remain = rounded % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remain).padStart(2, "0")}`
    : `${minutes}:${String(remain).padStart(2, "0")}`;
}

function formatSize(bytes) {
  const value = Number(bytes || 0);
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(1)} GB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

function formatTime(timestamp) {
  if (!timestamp) return "";
  return new Intl.DateTimeFormat("zh-TW", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(new Date(Number(timestamp) * 1000));
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function showToast(message, type = "success", action = null) {
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  const text = document.createElement("span");
  text.textContent = message;
  toast.append(text);
  if (action) {
    const button = document.createElement("button");
    button.textContent = action.label;
    button.addEventListener("click", async () => {
      try {
        await action.run();
        toast.remove();
      } catch (error) {
        showToast(error.message, "error");
      }
    });
    toast.append(button);
  }
  $("#toastRegion").append(toast);
  setTimeout(() => toast.remove(), action ? 9000 : 4500);
}

function applyUiPreferences() {
  document.documentElement.dataset.theme = state.theme;
  document.body.classList.toggle("thumbnails-blurred", state.blur);
  document.body.classList.toggle("dense-grids", state.denseGrids);
}

function currentRoute() {
  const route = location.hash.replace(/^#/, "").split("?")[0];
  return PAGE_META[route] ? route : "grids";
}

function setRoute() {
  state.route = currentRoute();
  $$(".page").forEach(page => { page.hidden = page.dataset.page !== state.route; });
  $$("[data-route-link]").forEach(link => {
    link.classList.toggle("active", link.dataset.routeLink === state.route);
  });
  const [eyebrow, title] = PAGE_META[state.route];
  $("#pageEyebrow").textContent = eyebrow;
  $("#pageTitle").textContent = title;
  window.scrollTo({ top: 0, behavior: "instant" });
  if (state.route === "grids" && !state.grids.length) loadGrids(true);
  if (state.route === "profiles") loadProfiles();
  if (state.route === "jobs") loadJobs();
  if (state.route === "library" && !state.videos.length) loadVideos(true);
  if (state.route === "settings") renderSettings();
  updateBulkBar();
}

async function loadBaseData() {
  const requests = [
    api("/api/settings"),
    api("/api/summary"),
    api("/api/profiles"),
    api("/api/tasks"),
  ];
  const [settingsResult, summaryResult, profilesResult, jobsResult] =
    await Promise.allSettled(requests);
  if (settingsResult.status === "fulfilled") {
    state.settings = settingsResult.value.settings;
    state.blur = state.settings.privacy?.blur_thumbnails ?? state.blur;
    state.rememberProgress = state.settings.privacy?.remember_progress ?? state.rememberProgress;
    state.autoSubtitle = state.settings.privacy?.auto_subtitles ?? state.autoSubtitle;
    applyUiPreferences();
    applyCaptureDefaults();
  }
  if (summaryResult.status === "fulfilled") {
    state.summary = summaryResult.value;
    renderSummary();
  }
  if (profilesResult.status === "fulfilled") {
    state.profiles = profilesResult.value.profiles;
    renderProfileSelects();
  }
  if (jobsResult.status === "fulfilled") {
    state.jobs = jobsResult.value.tasks;
    updateJobBadge();
  }
  for (const result of [settingsResult, summaryResult, profilesResult, jobsResult]) {
    if (result.status === "rejected") showToast(result.reason.message, "error");
  }
  renderSettings();
}

function renderSummary() {
  if (!state.summary) return;
  $("#metricGridLibrary").textContent = state.summary.gridLibrary.toLocaleString("zh-TW");
  $("#metricGridArchive").textContent = state.summary.gridArchive.toLocaleString("zh-TW");
  $("#navGridCount").textContent = state.summary.gridLibrary.toLocaleString("zh-TW");
  $("#outputRootLabel").textContent = state.summary.outputRoot;
}

function renderProfileSelects() {
  const enabled = state.profiles.filter(profile => profile.enabled);
  const options = enabled.map(profile =>
    `<option value="${escapeHtml(profile.id)}">${escapeHtml(profile.name)}</option>`
  ).join("");
  ["#bulkProfile", "#gridDialogProfile"].forEach(selector => {
    const select = $(selector);
    const previous = select.value;
    select.innerHTML = options || `<option value="">尚無可用 Profile</option>`;
    if ([...select.options].some(option => option.value === previous)) select.value = previous;
  });
  renderFavoriteTargets();
}

function renderFavoriteTargets() {
  if (!state.settings) return;
  const folders = state.settings.favorite_folders || [];
  const options = folders.map(folder =>
    `<option value="${escapeHtml(folder.id)}">${escapeHtml(folder.name)}</option>`
  ).join("");
  ["#favoriteTarget", "#playerFavoriteTarget"].forEach(selector => {
    const select = $(selector);
    const previous = select.value;
    select.innerHTML = options || `<option value="">請先建立收藏資料夾</option>`;
    if ([...select.options].some(option => option.value === previous)) select.value = previous;
  });
}

function gridParams() {
  return new URLSearchParams({
    q: $("#gridQuery").value.trim(),
    location: $("#gridLocation").value || "all",
    includeTags: $("#gridIncludeTags").value.trim(),
    excludeTags: $("#gridExcludeTags").value.trim(),
    page: String(state.gridPage),
    pageSize: state.denseGrids ? "60" : "36",
  });
}

async function loadGrids(reset = false) {
  if (reset) {
    state.gridPage = 1;
    state.grids = [];
    state.gridMap.clear();
    $("#gridCards").innerHTML = "";
  }
  $("#gridLoading").hidden = false;
  $("#gridLoadMore").hidden = true;
  try {
    const payload = await api(`/api/grids?${gridParams()}`);
    const existing = new Set(state.grids.map(item => item.id));
    payload.items.forEach(item => {
      state.gridMap.set(item.id, item);
      if (!existing.has(item.id)) state.grids.push(item);
    });
    state.gridHasMore = payload.hasMore;
    renderGridLocationOptions(payload.locations);
    renderGrids();
    $("#gridResultText").textContent = payload.indexing
      ? `先顯示 ${payload.count.toLocaleString("zh-TW")} 個宮格，背景正在補齊 Metadata 與 TAG`
      : `共 ${payload.count.toLocaleString("zh-TW")} 個符合條件的宮格`;
    $("#gridLoadMore").hidden = !payload.hasMore;
  } catch (error) {
    showToast(`宮格索引讀取失敗：${error.message}`, "error");
    $("#gridResultText").textContent = "宮格索引暫時無法讀取";
  } finally {
    $("#gridLoading").hidden = true;
  }
}

function renderGridLocationOptions(locations) {
  const select = $("#gridLocation");
  const previous = select.value;
  select.innerHTML = locations.map(item =>
    `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`
  ).join("");
  if ([...select.options].some(option => option.value === previous)) select.value = previous;
}

function gridCard(item) {
  const selected = state.gridSelection.has(item.id);
  const route = item.routes?.at(-1);
  const tags = (item.tags || []).slice(0, 4);
  return `
    <article class="grid-card${selected ? " selected" : ""}" data-grid-id="${escapeHtml(item.id)}">
      <div class="grid-image-wrap">
        <img src="${escapeHtml(item.thumbnailUrl)}" alt="${escapeHtml(item.title)} 的 5×5 宮格" loading="lazy">
        <button class="select-button" data-action="toggle-grid" aria-label="選取宮格">${selected ? "✓" : ""}</button>
        <span class="location-pill">${escapeHtml(item.locationName)}</span>
        <span class="duration-pill">${formatDuration(item.duration)}</span>
      </div>
      <div class="grid-card-body">
        <h3 title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</h3>
        <div class="meta-row">
          <span>${escapeHtml(item.source || "未知來源")}</span>
          <span>${item.frameCount || 25} 格</span>
          ${route ? `<span class="routed-state">已送 ${escapeHtml(route.profileName)}</span>` : ""}
        </div>
        <div class="tag-list small">${tags.map(tag => `<button data-action="filter-tag" data-tag="${escapeHtml(tag)}">${escapeHtml(tag)}</button>`).join("")}</div>
      </div>
    </article>
  `;
}

function renderGrids() {
  const cards = $("#gridCards");
  cards.classList.toggle("dense", state.denseGrids);
  cards.innerHTML = state.grids.length
    ? state.grids.map(gridCard).join("")
    : `<div class="empty-panel inline-empty"><span>▦</span><h3>沒有符合條件的宮格</h3><p>調整篩選，或先建立新的宮格備份。</p></div>`;
  cards.querySelectorAll("img").forEach(image => {
    image.addEventListener("error", () => image.closest(".grid-image-wrap")?.classList.add("image-error"), { once: true });
  });
  updateBulkBar();
}

function updateBulkBar() {
  $("#bulkBar").hidden = state.route !== "grids" || state.gridSelection.size === 0;
  $("#bulkCount").textContent = state.gridSelection.size;
}

function toggleGrid(itemId) {
  if (state.gridSelection.has(itemId)) state.gridSelection.delete(itemId);
  else state.gridSelection.add(itemId);
  renderGrids();
}

function openGridDialog(item) {
  state.currentGrid = item;
  $("#gridDialogTitle").textContent = item.title;
  $("#gridDialogImage").src = item.assetUrl;
  $("#gridDialogSource").href = item.url || "#";
  $("#gridDialogSource").hidden = !item.url;
  const meta = [
    ["位置", item.locationName],
    ["來源", item.source || "未知"],
    ["片長", formatDuration(item.duration)],
    ["檔案", item.filename],
    ["大小", formatSize(item.size)],
    ["建立時間", formatTime(item.modified)],
    ["畫面數", `${item.frameCount || 25} 格`],
  ];
  $("#gridDialogMeta").innerHTML = meta.map(([key, value]) =>
    `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>`
  ).join("");
  $("#gridDialogTags").innerHTML = (item.tags || []).slice(0, 24)
    .map(tag => `<button data-action="filter-dialog-tag" data-tag="${escapeHtml(tag)}">${escapeHtml(tag)}</button>`)
    .join("");
  $("#gridDialog").showModal();
}

async function routeGridIds(itemIds, profileId, mode = "copy") {
  if (!itemIds.length) return;
  if (!profileId) {
    showToast("請先建立並選擇處理 Profile", "error");
    return;
  }
  const payload = await api("/api/grids/route", {
    method: "POST",
    body: JSON.stringify({ itemIds, profileId, mode }),
  });
  if (payload.errors?.length) {
    showToast(`已送出 ${payload.count} 個，另有 ${payload.errors.length} 個失敗`, "error");
  } else {
    showToast(`已複製 ${payload.count} 個宮格到處理資料夾`);
  }
  state.gridSelection.clear();
  await Promise.all([loadGrids(true), loadProfiles(), loadSummary()]);
}

function applyCaptureDefaults() {
  if (!state.settings) return;
  const capture = state.settings.capture || {};
  $("#capturePages").value = capture.pages ?? 1;
  $("#captureMaxVideos").value = capture.max_videos ?? 20;
  $("#captureQuality").value = capture.quality || "480p";
}

async function submitCapture(event) {
  event.preventDefault();
  const target = $("#captureTarget").value.trim();
  if (!target) return;
  const button = $("#captureForm button[type=submit]");
  button.disabled = true;
  try {
    await api("/api/capture", {
      method: "POST",
      body: JSON.stringify({
        target,
        pages: Number($("#capturePages").value),
        maxVideos: Number($("#captureMaxVideos").value),
        quality: $("#captureQuality").value,
      }),
    });
    showToast("宮格建立工作已加入佇列");
    location.hash = "jobs";
    loadJobs();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function loadProfiles() {
  $("#profileLoading").hidden = false;
  try {
    const payload = await api("/api/profiles");
    state.profiles = payload.profiles;
    renderProfiles();
    renderProfileSelects();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    $("#profileLoading").hidden = true;
  }
}

function profileModeName(mode) {
  return {
    preview: "Preview",
    shorts: "Shorts",
    video: "Video",
    chosen: "Chosen",
  }[mode] || mode;
}

function profileSummary(profile) {
  const options = profile.options || {};
  return [
    options.asr ? options.asr_backend?.toUpperCase() || "ASR" : "無 ASR",
    options.translation ? "翻譯" : "原文",
    profile.mode === "preview" ? `${Math.round(options.preview_seconds / 60 * 10) / 10} 分鐘／段` :
      (profile.mode === "shorts" ? "來源最高畫質" :
        `${profile.mode === "chosen" ? options.chosen_height : options.video_height}P`),
    options.enhance ? "Enhance" : "原音",
  ];
}

function renderProfiles() {
  $("#profileCards").innerHTML = state.profiles.map(profile => `
    <article class="profile-card" data-profile-id="${escapeHtml(profile.id)}" style="--profile-color:${escapeHtml(profile.color || "#d6ff3f")}">
      <div class="profile-card-head">
        <span class="profile-color"></span>
        <div><small>${profile.system ? "預設 PROFILE" : "自訂 PROFILE"}</small><h3>${escapeHtml(profile.name)}</h3></div>
        <span class="mode-pill">${profileModeName(profile.mode)}</span>
      </div>
      <div class="profile-counts">
        <div><strong>${Number(profile.pendingCount || 0).toLocaleString("zh-TW")}</strong><span>Inbox 待處理</span></div>
        <div><strong>${Number(profile.videoCount || 0).toLocaleString("zh-TW")}</strong><span>成品影片</span></div>
      </div>
      <div class="profile-paths">
        <p><span>IN</span><code title="${escapeHtml(profile.inbox_dir)}">${escapeHtml(profile.inbox_dir)}</code></p>
        <p><span>OUT</span><code title="${escapeHtml(profile.output_dir)}">${escapeHtml(profile.output_dir)}</code></p>
      </div>
      <div class="profile-chips">${profileSummary(profile).map(item => `<span>${escapeHtml(item)}</span>`).join("")}</div>
      <div class="profile-actions">
        <button class="button subtle" data-action="view-profile-grids">查看 Inbox</button>
        <button class="button subtle" data-action="clone-profile">複製</button>
        <button class="button subtle" data-action="edit-profile">設定</button>
        <button class="button primary" data-action="run-profile" ${profile.enabled ? "" : "disabled"}>執行</button>
      </div>
    </article>
  `).join("");
}

function emptyProfile() {
  const root = state.settings?.output_root || "output";
  const id = `custom-${Date.now().toString(36)}`;
  return {
    id,
    name: "新的處理資料夾",
    mode: "video",
    enabled: true,
    system: false,
    color: "#d6ff3f",
    inbox_dir: `${root}\\${id}\\inbox`,
    output_dir: `${root}\\${id}\\videos`,
    grid_backup_dir: `${root}\\04_downloaded`,
    route_mode: "copy",
    auto_run: false,
    options: {
      asr: true, demucs_asr: true, asr_stream: false, subtitles: true,
      translation: true, dialogue_trim: true, selective_download: true,
      three_phase_selection: true, edge_padding: false, enhance: true,
      metadata: true, archive: true, keep_work: false, reuse_cache: true,
      force: false, preview_seconds: 180, video_height: 480,
      chosen_height: 1080, asr_backend: "moss",
      translation_model: "x-ai/grok-4.3", reasoning_effort: "minimal",
      trim_threshold: 30, segment_gap: 1.5, asr_chunk_seconds: 180,
      asr_batch_size: 3,
    },
  };
}

function openProfileDialog(profile) {
  const value = structuredClone(profile);
  $("#profileId").value = value.id;
  $("#profileSystem").value = value.system ? "1" : "0";
  $("#profileDialogTitle").textContent = value.system ? `設定｜${value.name}` : "編輯自訂 Profile";
  $("#profileName").value = value.name;
  $("#profileMode").value = value.mode;
  $("#profileInbox").value = value.inbox_dir;
  $("#profileOutput").value = value.output_dir;
  $("#profileArchive").value = value.grid_backup_dir;
  $("#profileRouteMode").value = value.route_mode || "copy";
  $("#profileEnabled").checked = value.enabled !== false;
  $$("[data-option]", $("#profileForm")).forEach(input => {
    const option = value.options?.[input.dataset.option];
    if (input.type === "checkbox") input.checked = Boolean(option);
    else input.value = option ?? "";
  });
  $("#deleteProfileButton").hidden = Boolean(value.system);
  $("#profileValidation").hidden = true;
  updateProfileModeFields();
  $("#profileDialog").showModal();
}

function collectProfileForm() {
  const options = {};
  $$("[data-option]", $("#profileForm")).forEach(input => {
    options[input.dataset.option] = input.type === "checkbox"
      ? input.checked
      : (input.type === "number" ? Number(input.value) : input.value.trim());
  });
  return {
    id: $("#profileId").value,
    system: $("#profileSystem").value === "1",
    name: $("#profileName").value.trim(),
    mode: $("#profileMode").value,
    enabled: $("#profileEnabled").checked,
    color: state.profiles.find(item => item.id === $("#profileId").value)?.color || "#d6ff3f",
    inbox_dir: $("#profileInbox").value.trim(),
    output_dir: $("#profileOutput").value.trim(),
    grid_backup_dir: $("#profileArchive").value.trim(),
    route_mode: $("#profileRouteMode").value,
    auto_run: false,
    options,
  };
}

function validateProfile(profile) {
  const options = profile.options;
  if (!profile.name || !profile.inbox_dir || !profile.output_dir || !profile.grid_backup_dir) {
    return "名稱與三個資料夾路徑都不可留空。";
  }
  if (options.selective_download && !options.translation) {
    return "精選下載需要翻譯；請開啟翻譯，或關閉精選下載。";
  }
  if (!options.asr && !options.reuse_cache &&
      (options.translation || options.dialogue_trim || options.selective_download)) {
    return "ASR 與 Cache 同時關閉時，翻譯、對白剪片與精選下載沒有時間軸來源。";
  }
  return "";
}

function updateProfileModeFields() {
  const mode = $("#profileMode").value;
  const threePhase = $('[data-option="three_phase_selection"]');
  threePhase.disabled = !["video", "chosen"].includes(mode);
  if (threePhase.disabled) threePhase.checked = false;
}

async function saveProfile(event) {
  event.preventDefault();
  if (!state.settings) return;
  const profile = collectProfileForm();
  const error = validateProfile(profile);
  $("#profileValidation").hidden = !error;
  $("#profileValidation").textContent = error;
  if (error) return;
  const profiles = [...state.settings.profiles];
  const index = profiles.findIndex(item => item.id === profile.id);
  if (index >= 0) profiles[index] = profile;
  else profiles.push(profile);
  try {
    const payload = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ settings: { ...state.settings, profiles } }),
    });
    state.settings = payload.settings;
    $("#profileDialog").close();
    showToast("處理 Profile 已儲存");
    await Promise.all([loadProfiles(), loadSummary()]);
  } catch (saveError) {
    showToast(saveError.message, "error");
  }
}

async function deleteCurrentProfile() {
  const id = $("#profileId").value;
  if ($("#profileSystem").value === "1") return;
  if (!confirm("刪除這個 Profile 設定？資料夾與檔案不會被刪除。")) return;
  const profiles = state.settings.profiles.filter(profile => profile.id !== id);
  const payload = await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({ settings: { ...state.settings, profiles } }),
  });
  state.settings = payload.settings;
  $("#profileDialog").close();
  showToast("Profile 設定已刪除，原資料夾未變更");
  await loadProfiles();
}

async function runProfile(profile) {
  const summary = profileSummary(profile).join(" · ");
  if (!confirm(`執行「${profile.name}」？\n${summary}\n\n只會掃描這個 Profile 的 Inbox。`)) return;
  try {
    await api("/api/profiles/run", {
      method: "POST",
      body: JSON.stringify({ profileId: profile.id }),
    });
    showToast(`${profile.name} 已加入下載工作`);
    location.hash = "jobs";
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function loadJobs() {
  clearTimeout(state.jobTimer);
  try {
    const payload = await api("/api/tasks");
    state.jobs = payload.tasks;
    renderJobs();
    updateJobBadge();
    if (state.jobs.some(job => ["queued", "running"].includes(job.state))) {
      state.jobTimer = setTimeout(loadJobs, 2500);
    }
  } catch (error) {
    showToast(error.message, "error");
  }
}

function updateJobBadge() {
  const active = state.jobs.filter(job => ["queued", "running"].includes(job.state)).length;
  $("#navJobCount").textContent = active || "";
}

function jobStateLabel(stateName) {
  return {
    queued: "等待中", running: "執行中", done: "已完成", ready: "可處理",
    failed: "失敗", cancelled: "已取消", interrupted: "已中斷",
  }[stateName] || stateName;
}

function renderJobs() {
  $("#jobEmpty").hidden = state.jobs.length > 0;
  $("#jobList").innerHTML = state.jobs.map(job => `
    <article class="job-card ${escapeHtml(job.state)}" data-task-id="${escapeHtml(job.id)}">
      <div class="job-main">
        <span class="job-state">${jobStateLabel(job.state)}</span>
        <div class="job-title"><h3>${escapeHtml(job.label)}</h3><p>${escapeHtml(job.message || "等待工作")}</p></div>
        <time>${formatTime(job.updatedAt)}</time>
      </div>
      <div class="progress-track"><i style="width:${Math.max(0, Math.min(100, job.progress || 0))}%"></i></div>
      <div class="job-footer">
        <span>${job.progress || 0}%</span>
        <span class="bulk-spacer"></span>
        ${job.log?.length ? `<details><summary>最近 Log</summary><pre>${escapeHtml(job.log.join("\n"))}</pre></details>` : ""}
        ${job.cancellable ? `<button class="button subtle small-button" data-action="cancel-job">取消</button>` : ""}
        ${["failed", "cancelled", "interrupted"].includes(job.state) ? `<button class="button subtle small-button" data-action="retry-job">重試</button>` : ""}
      </div>
    </article>
  `).join("");
}

async function taskAction(taskId, action) {
  try {
    await api(`/api/tasks/${action}`, {
      method: "POST",
      body: JSON.stringify({ taskId }),
    });
    showToast(action === "cancel" ? "已送出取消要求" : "工作已重新加入佇列");
    loadJobs();
  } catch (error) {
    showToast(error.message, "error");
  }
}

function videoParams() {
  return new URLSearchParams({
    q: $("#videoQuery").value.trim(),
    location: $("#videoLocation").value || "all",
    page: String(state.videoPage),
    pageSize: "36",
  });
}

async function loadVideos(reset = false) {
  if (reset) {
    state.videoPage = 1;
    state.videos = [];
    state.videoMap.clear();
    $("#videoCards").innerHTML = "";
  }
  $("#videoLoading").hidden = false;
  $("#videoLoadMore").hidden = true;
  try {
    const payload = await api(`/api/videos?${videoParams()}`);
    const existing = new Set(state.videos.map(item => item.id));
    payload.items.forEach(item => {
      state.videoMap.set(item.id, item);
      if (!existing.has(item.id)) state.videos.push(item);
    });
    state.videoHasMore = payload.hasMore;
    renderVideoLocations(payload.locations);
    renderVideos();
    $("#videoResultText").textContent = payload.indexing
      ? `先顯示 ${payload.count.toLocaleString("zh-TW")} 部影片，背景正在補齊 Metadata`
      : `共 ${payload.count.toLocaleString("zh-TW")} 部符合條件的影片`;
    $("#videoLoadMore").hidden = !payload.hasMore;
  } catch (error) {
    showToast(`影片索引讀取失敗：${error.message}`, "error");
  } finally {
    $("#videoLoading").hidden = true;
  }
}

function renderVideoLocations(locations) {
  const select = $("#videoLocation");
  const previous = select.value;
  select.innerHTML = locations.map(item =>
    `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`
  ).join("");
  if ([...select.options].some(option => option.value === previous)) select.value = previous;
}

function videoCard(item) {
  return `
    <article class="video-card" data-video-id="${escapeHtml(item.id)}">
      <div class="video-poster">
        <img src="${escapeHtml(item.thumbnailUrl)}" alt="" loading="lazy">
        <button class="play-overlay" data-action="play-video" aria-label="播放 ${escapeHtml(item.title)}">▶</button>
        <span class="location-pill">${escapeHtml(item.locationName)}</span>
        <span class="duration-pill">${formatDuration(item.duration)}</span>
      </div>
      <div class="video-card-body">
        <h3 title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</h3>
        <div class="meta-row"><span>${formatSize(item.size)}</span><span>${item.hasSubtitle ? "有字幕" : "無字幕"}</span><span>${escapeHtml(item.publishedStage || "")}</span></div>
        <div class="video-actions">
          <button data-action="play-video">播放</button>
          <button data-action="favorite-video">移到收藏</button>
          <button class="danger-text" data-action="trash-video">回收</button>
        </div>
      </div>
    </article>
  `;
}

function renderVideos() {
  $("#videoCards").innerHTML = state.videos.length
    ? state.videos.map(videoCard).join("")
    : `<div class="empty-panel inline-empty"><span>▶</span><h3>沒有符合條件的影片</h3><p>切換資料夾或清除搜尋條件。</p></div>`;
}

function openPlayer(item) {
  state.currentVideo = item;
  $("#playerTitle").textContent = item.title;
  $("#playerLocation").textContent = item.locationName || "VIDEO";
  $("#playerSubtitleState").textContent = item.hasSubtitle ? "繁體中文字幕可用" : "沒有同名 SRT";
  const video = $("#videoPlayer");
  const track = $("#subtitleTrack");
  video.src = item.mediaUrl;
  if (item.subtitleUrl) {
    track.src = item.subtitleUrl;
    track.default = state.autoSubtitle;
    track.addEventListener("load", () => {
      if (video.textTracks[0]) video.textTracks[0].mode = state.autoSubtitle ? "showing" : "hidden";
    }, { once: true });
  } else {
    track.removeAttribute("src");
  }
  const progressKey = `muse-progress:${item.id}`;
  video.onloadedmetadata = () => {
    if (!state.rememberProgress) return;
    const progress = Number(localStorage.getItem(progressKey) || 0);
    if (progress > 2 && progress < video.duration - 5) video.currentTime = progress;
  };
  let lastSaved = -1;
  video.ontimeupdate = () => {
    if (!state.rememberProgress) return;
    const second = Math.floor(video.currentTime);
    if (second % 5 === 0 && second !== lastSaved) {
      lastSaved = second;
      try { localStorage.setItem(progressKey, String(video.currentTime)); } catch {}
    }
  };
  $("#playerDialog").showModal();
  video.play().catch(() => {});
}

function closePlayer() {
  const video = $("#videoPlayer");
  video.pause();
  video.removeAttribute("src");
  video.load();
  $("#playerDialog").close();
}

async function favoriteVideo(item, targetId = $("#favoriteTarget").value) {
  if (!targetId) {
    showToast("請先在設定建立收藏資料夾", "error");
    return;
  }
  try {
    await api("/api/media/move", {
      method: "POST",
      body: JSON.stringify({ itemId: item.id, targetId }),
    });
    showToast("影片與字幕已一起移到收藏資料夾");
    if ($("#playerDialog").open) closePlayer();
    await loadVideos(true);
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function trashVideo(item) {
  if (!confirm(`將「${item.title}」與同名字幕移到可復原回收區？`)) return;
  try {
    const payload = await api("/api/media/trash", {
      method: "POST",
      body: JSON.stringify({ itemId: item.id }),
    });
    if ($("#playerDialog").open) closePlayer();
    await loadVideos(true);
    showToast("影片已移到可復原回收區", "success", {
      label: "復原",
      run: async () => {
        await api("/api/media/restore", {
          method: "POST",
          body: JSON.stringify({ token: payload.token }),
        });
        showToast("影片與字幕已復原");
        await loadVideos(true);
      },
    });
  } catch (error) {
    showToast(error.message, "error");
  }
}

function renderSettings() {
  if (!state.settings) return;
  $("#settingsOutputRoot").value = state.settings.output_root || "";
  $("#settingsTrashDir").value = state.settings.trash_dir || "";
  $("#settingsCaptureQuality").value = state.settings.capture?.quality || "480p";
  $("#settingsCapturePages").value = state.settings.capture?.pages ?? 1;
  $("#settingsCaptureMax").value = state.settings.capture?.max_videos ?? 20;
  $("#settingsBlur").checked = state.blur;
  $("#settingsProgress").checked = state.rememberProgress;
  $("#settingsSubtitle").checked = state.autoSubtitle;
  renderFavoriteFolderEditor();
}

function renderFavoriteFolderEditor() {
  const folders = state.settings?.favorite_folders || [];
  $("#favoriteFolderList").innerHTML = folders.map((folder, index) => `
    <div class="folder-editor-row" data-favorite-index="${index}">
      <input data-field="name" value="${escapeHtml(folder.name)}" aria-label="收藏名稱">
      <input data-field="path" value="${escapeHtml(folder.path)}" aria-label="收藏路徑">
      <button class="icon-button" data-action="remove-favorite-folder" aria-label="移除收藏資料夾">×</button>
    </div>
  `).join("");
}

function collectFavoriteFolders() {
  return $$(".folder-editor-row").map((row, index) => ({
    id: state.settings.favorite_folders[index]?.id || `favorite-${Date.now().toString(36)}-${index}`,
    name: $('[data-field="name"]', row).value.trim() || "收藏資料夾",
    path: $('[data-field="path"]', row).value.trim(),
  })).filter(folder => folder.path);
}

async function saveGlobalSettings() {
  if (!state.settings) return;
  state.blur = $("#settingsBlur").checked;
  state.rememberProgress = $("#settingsProgress").checked;
  state.autoSubtitle = $("#settingsSubtitle").checked;
  const next = {
    ...state.settings,
    trash_dir: $("#settingsTrashDir").value.trim(),
    favorite_folders: collectFavoriteFolders(),
    capture: {
      quality: $("#settingsCaptureQuality").value,
      pages: Number($("#settingsCapturePages").value),
      max_videos: Number($("#settingsCaptureMax").value),
    },
    privacy: {
      blur_thumbnails: state.blur,
      remember_progress: state.rememberProgress,
      auto_subtitles: state.autoSubtitle,
    },
  };
  try {
    const payload = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ settings: next }),
    });
    state.settings = payload.settings;
    persistLocal();
    applyUiPreferences();
    renderSettings();
    renderFavoriteTargets();
    showToast("本機工作區設定已儲存");
  } catch (error) {
    showToast(error.message, "error");
  }
}

function handleGridClick(event) {
  const card = event.target.closest("[data-grid-id]");
  if (!card) return false;
  const item = state.gridMap.get(card.dataset.gridId);
  if (!item) return true;
  const action = event.target.closest("[data-action]")?.dataset.action;
  if (action === "toggle-grid") toggleGrid(item.id);
  else if (action === "filter-tag") {
    $("#gridIncludeTags").value = event.target.dataset.tag;
    loadGrids(true);
  } else openGridDialog(item);
  return true;
}

function handleVideoClick(event) {
  const card = event.target.closest("[data-video-id]");
  if (!card) return false;
  const item = state.videoMap.get(card.dataset.videoId);
  if (!item) return true;
  const action = event.target.closest("[data-action]")?.dataset.action;
  if (action === "favorite-video") favoriteVideo(item);
  else if (action === "trash-video") trashVideo(item);
  else openPlayer(item);
  return true;
}

function handleProfileClick(event) {
  const card = event.target.closest("[data-profile-id]");
  if (!card) return false;
  const profile = state.profiles.find(item => item.id === card.dataset.profileId);
  if (!profile) return true;
  const action = event.target.closest("[data-action]")?.dataset.action;
  if (action === "edit-profile") openProfileDialog(profile);
  if (action === "clone-profile") {
    const clone = structuredClone(profile);
    clone.id = `custom-${Date.now().toString(36)}`;
    clone.system = false;
    clone.name = `${clone.name} 複本`;
    openProfileDialog(clone);
  }
  if (action === "run-profile") runProfile(profile);
  if (action === "view-profile-grids") {
    location.hash = "grids";
    setTimeout(() => {
      $("#gridLocation").value = profile.id;
      loadGrids(true);
    }, 0);
  }
  return true;
}

function bindEvents() {
  window.addEventListener("hashchange", setRoute);
  $("#captureForm").addEventListener("submit", submitCapture);
  $("#profileForm").addEventListener("submit", saveProfile);
  $("#profileMode").addEventListener("change", updateProfileModeFields);
  $("#gridDialog").addEventListener("cancel", event => {
    event.preventDefault();
    $("#gridDialog").close();
  });
  $("#playerDialog").addEventListener("cancel", event => {
    event.preventDefault();
    closePlayer();
  });
  $("#profileDialog").addEventListener("cancel", event => {
    event.preventDefault();
    $("#profileDialog").close();
  });
  $("#gridQuery").addEventListener("input", () => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => loadGrids(true), 350);
  });
  $("#videoQuery").addEventListener("input", () => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => loadVideos(true), 350);
  });
  document.addEventListener("keydown", event => {
    if ((event.key === "p" || event.key === "P") &&
        !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) {
      $("#privacyScreen").hidden = !$("#privacyScreen").hidden;
    }
    if (event.key === "Escape" && !$("#privacyScreen").hidden) $("#privacyScreen").hidden = true;
  });
  document.addEventListener("click", async event => {
    if (handleGridClick(event) || handleVideoClick(event) || handleProfileClick(event)) return;
    const actionNode = event.target.closest("[data-action]");
    if (!actionNode) return;
    const action = actionNode.dataset.action;
    const actions = {
      "privacy-off": () => { $("#privacyScreen").hidden = true; },
      "toggle-blur": () => {
        state.blur = !state.blur; persistLocal(); applyUiPreferences();
      },
      theme: () => {
        state.theme = state.theme === "dark" ? "light" : "dark";
        persistLocal(); applyUiPreferences();
      },
      "open-settings": () => { location.hash = "settings"; },
      "refresh-page": () => refreshCurrentPage(),
      "apply-grid-filter": () => loadGrids(true),
      "grid-density": () => {
        state.denseGrids = !state.denseGrids;
        persistLocal(); applyUiPreferences(); loadGrids(true);
      },
      "select-visible": () => {
        state.grids.forEach(item => state.gridSelection.add(item.id)); renderGrids();
      },
      "clear-grid-selection": () => { state.gridSelection.clear(); renderGrids(); },
      "route-selected-grids": () => routeGridIds(
        [...state.gridSelection], $("#bulkProfile").value, $("#bulkRouteMode").value
      ),
      "route-dialog-grid": async () => {
        if (!state.currentGrid) return;
        await routeGridIds([state.currentGrid.id], $("#gridDialogProfile").value, "copy");
        $("#gridDialog").close();
      },
      "close-grid-dialog": () => $("#gridDialog").close(),
      "filter-dialog-tag": () => {
        $("#gridIncludeTags").value = actionNode.dataset.tag;
        $("#gridDialog").close();
        loadGrids(true);
      },
      "load-more-grids": () => { state.gridPage += 1; loadGrids(false); },
      "add-profile": () => openProfileDialog(emptyProfile()),
      "close-profile-dialog": () => $("#profileDialog").close(),
      "delete-profile": () => deleteCurrentProfile(),
      "refresh-jobs": () => loadJobs(),
      "cancel-job": () => taskAction(actionNode.closest("[data-task-id]").dataset.taskId, "cancel"),
      "retry-job": () => taskAction(actionNode.closest("[data-task-id]").dataset.taskId, "retry"),
      "apply-video-filter": () => loadVideos(true),
      "load-more-videos": () => { state.videoPage += 1; loadVideos(false); },
      "close-player": () => closePlayer(),
      "favorite-current-video": () => {
        if (state.currentVideo) favoriteVideo(state.currentVideo, $("#playerFavoriteTarget").value);
      },
      "trash-current-video": () => {
        if (state.currentVideo) trashVideo(state.currentVideo);
      },
      "save-settings": () => saveGlobalSettings(),
      "add-favorite-folder": () => {
        state.settings.favorite_folders.push({
          id: `favorite-${Date.now().toString(36)}`,
          name: "新的收藏資料夾",
          path: `${state.settings.output_root}\\07_favorites`,
        });
        renderFavoriteFolderEditor();
      },
      "remove-favorite-folder": () => {
        const row = actionNode.closest("[data-favorite-index]");
        state.settings.favorite_folders.splice(Number(row.dataset.favoriteIndex), 1);
        renderFavoriteFolderEditor();
      },
    };
    try {
      await actions[action]?.();
    } catch (error) {
      showToast(error.message, "error");
    }
  });
}

function refreshCurrentPage() {
  if (state.route === "grids") return Promise.all([loadGrids(true), loadSummary()]);
  if (state.route === "profiles") return loadProfiles();
  if (state.route === "jobs") return loadJobs();
  if (state.route === "library") return loadVideos(true);
  if (state.route === "settings") return loadBaseData();
  return Promise.resolve();
}

async function initialize() {
  applyUiPreferences();
  bindEvents();
  if (!location.hash) history.replaceState(null, "", "#grids");
  setRoute();
  try {
    await loadBaseData();
  } catch (error) {
    showToast(error.message, "error");
  }
}

initialize();
