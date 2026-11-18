const KPW6_CSRF_TOKEN = (typeof window !== "undefined" && window.__KPW6_CSRF_TOKEN__) ? window.__KPW6_CSRF_TOKEN__ : "__KPW6_CSRF_TOKEN__";
let currentFileId = "demo_academic_2col";
let currentMode = "academic_2col";
let currentPage = 0;
let totalPages = 1;
let currentScreens = [];
let currentScreenIdx = 0;
let currentSlicesMeta = [];
let currentHighlights = [];
let rightViewMode = "single"; // "single" or "grid"
let currentDpiPreset = 450;
let currentStrokeBoldness = 0;
let currentCoverMode = "smart_card";
let currentCustomCoverId = "";
let currentHasCoverScreen = false;
let currentExportFormat = "epub_layout";
let userManuallyChoseFormat = false;
let currentSplitVolumesEvery = 0;
let activeExportJobId = "";
let pageOverridesByFile = {};
let previewRequestSerial = 0;
let fileSelectionSerial = 0;
let coverUploadSerial = 0;
let lightboxRequestSerial = 0;

const ALL_MODES = ["academic_2col", "tech_book", "scanned_book", "comic_manga", "pure_text"];

function getCurrentFileOverrides() {
  if (!pageOverridesByFile[currentFileId]) {
    pageOverridesByFile[currentFileId] = {};
  }
  return pageOverridesByFile[currentFileId];
}

function setPageOverride(rule) {
  const map = getCurrentFileOverrides();
  if (!rule || rule === "auto") {
    delete map[String(currentPage)];
  } else {
    map[String(currentPage)] = rule;
  }
  renderPageOverrideToolbar();
  refreshPreview(0);
}

function clearAllPageOverrides() {
  pageOverridesByFile[currentFileId] = {};
  renderPageOverrideToolbar();
  refreshPreview(0);
}

function renderPageOverrideToolbar() {
  const lbl = document.getElementById("overridePageNumLabel");
  if (lbl) lbl.innerText = currentPage + 1;
  const map = getCurrentFileOverrides();
  const activeRule = map[String(currentPage)] || "auto";
  ["auto", "whole_portrait", "whole_landscape", "skip"].forEach(r => {
    const el = document.getElementById("ovr-" + r);
    if (el) el.classList.toggle("active", r === activeRule);
  });

  const wrap = document.getElementById("activeOverridesWrap");
  const list = document.getElementById("activeOverridesPills");
  if (!wrap || !list) return;
  list.innerHTML = "";
  const entries = Object.entries(map).filter(([_, v]) => v && v !== "auto").sort((a, b) => parseInt(a[0]) - parseInt(b[0]));
  if (!entries.length) {
    wrap.style.display = "none";
    return;
  }
  wrap.style.display = "flex";
  const tagMap = {
    "whole_portrait": "🔒整页",
    "whole_landscape": "🔄横置90°",
    "skip": "🚫跳过"
  };
  entries.forEach(([pStr, rule]) => {
    const p0 = parseInt(pStr);
    const b = document.createElement("button");
    b.type = "button";
    b.className = "pill" + (p0 === currentPage ? " active" : "");
    b.style.padding = "2px 7px";
    b.style.fontSize = "10.5px";
    b.innerText = `P${p0 + 1}:${tagMap[rule] || rule}`;
    b.title = `点击跳转到第 ${p0 + 1} 页`;
    b.onclick = () => jumpToPage(p0);
    list.appendChild(b);
  });
}

function setPageRangeAll() {
  document.getElementById("optStartPage").value = 1;
  document.getElementById("optEndPage").value = totalPages || "";
}

function setPageRangeCurrent() {
  document.getElementById("optStartPage").value = currentPage + 1;
  document.getElementById("optEndPage").value = currentPage + 1;
}

function setSplitVolumes(everyPages) {
  currentSplitVolumesEvery = everyPages;
  [0, 50, 100, 150].forEach(v => {
    const el = document.getElementById("vol-" + v);
    if (el) el.classList.toggle("active", v === everyPages);
  });
  const labels = {
    0: "整本不拆卷",
    50: "每 50 页拆一卷",
    100: "每 100 页拆一卷 (推荐)",
    150: "每 150 页拆一卷"
  };
  document.getElementById("valSplitVol").innerText = labels[everyPages] || `每 ${everyPages} 页一卷`;
}

const dropzone = document.getElementById("dropzone");
dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  if (e.dataTransfer.files && e.dataTransfer.files[0]) {
    handleFileUpload(e.dataTransfer.files[0]);
  }
});

// Keyboard shortcuts: Left/Right flips KPW6 screens (or pages in grid mode), Up/Down flips PDF pages, Esc closes lightbox
window.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") return;
  const lb = document.getElementById("lightboxModal");
  if (lb.style.display === "flex") {
    if (e.key === "Escape") closeLightbox();
    return;
  }
  if (e.key === "ArrowRight") {
    e.preventDefault();
    if (rightViewMode === "single") stepKpw6Screen(1);
    else changePage(1);
  } else if (e.key === "ArrowLeft") {
    e.preventDefault();
    if (rightViewMode === "single") stepKpw6Screen(-1);
    else changePage(-1);
  } else if (e.key === "ArrowDown" || e.key === "PageDown") {
    e.preventDefault();
    changePage(1);
  } else if (e.key === "ArrowUp" || e.key === "PageUp") {
    e.preventDefault();
    changePage(-1);
  }
});

function setRightViewMode(mode) {
  rightViewMode = mode;
  document.getElementById("tabSingleView").classList.toggle("active", mode === "single");
  document.getElementById("tabGridView").classList.toggle("active", mode === "grid");
  document.getElementById("singleSimStage").style.display = (mode === "single") ? "flex" : "none";
  document.getElementById("kpw6ScreensContainer").style.display = (mode === "grid") ? "grid" : "none";
}

function selectMode(mode, skipRefresh = false) {
  currentMode = mode;
  ALL_MODES.forEach(m => {
    document.getElementById("card-" + m).classList.toggle("active", m === mode);
  });

  document.getElementById("optEnhance").checked = (mode === "scanned_book" || mode === "comic_manga");
  document.getElementById("ctrlComic").style.display = (mode === "comic_manga") ? "block" : "none";
  document.getElementById("ctrlPureText").style.display = (mode === "pure_text") ? "block" : "none";
  if (!userManuallyChoseFormat) {
    setExportFormat(mode === "pure_text" ? "epub_reflow" : "epub_layout", true);
  }

  if (!skipRefresh) {
    refreshPreview();
  }
}

async function fetchUploadedFilesAndInit() {
  const selectionId = fileSelectionSerial;
  const res = await fetch("/api/files");
  const data = await res.json();
  if (selectionId !== fileSelectionSerial) return;
  renderUploadedPills(data.uploaded || []);

  // Auto-open history only while no newer user selection has taken place.
  if (data.uploaded && data.uploaded.length > 0) {
    const latest = data.uploaded[0];
    await switchToUploadedFile(latest.file_id, latest.name);
  } else {
    loadDemo("demo_academic_2col", "academic_2col");
  }
}

function renderUploadedPills(uploadedList) {
  const wrap = document.getElementById("uploadedFilesWrap");
  const container = document.getElementById("uploadedFilesPills");
  container.innerHTML = "";
  if (!uploadedList || uploadedList.length === 0) {
    wrap.style.display = "none";
    return;
  }
  wrap.style.display = "flex";
  uploadedList.forEach(item => {
    const group = document.createElement("span");
    group.style.display = "inline-flex";
    group.style.alignItems = "center";
    group.style.gap = "2px";
    const btn = document.createElement("button");
    btn.className = "pill" + (item.file_id === currentFileId ? " active" : "");
    btn.id = "pill-" + item.file_id;
    btn.innerText = "📘 " + item.name;
    btn.onclick = () => switchToUploadedFile(item.file_id, item.name);
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "pill";
    delBtn.style.padding = "4px 6px";
    delBtn.style.color = "#fca5a5";
    delBtn.innerText = "✕";
    delBtn.title = "从上传记录中移除此 PDF";
    delBtn.onclick = (e) => {
      e.stopPropagation();
      deleteUploadedFile(item.file_id);
    };
    group.appendChild(btn);
    group.appendChild(delBtn);
    container.appendChild(group);
  });
}

async function deleteUploadedFile(fileId) {
  try {
    const res = await fetch("/api/delete_file", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-KPW6-Token": KPW6_CSRF_TOKEN },
      body: JSON.stringify({ file_id: fileId })
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || "无法移除文件");
    delete pageOverridesByFile[fileId];
    renderUploadedPills(data.uploaded || []);
    if (currentFileId === fileId) {
      if (data.uploaded && data.uploaded.length > 0) {
        await switchToUploadedFile(data.uploaded[0].file_id, data.uploaded[0].name);
      } else {
        await loadDemo("demo_academic_2col", "academic_2col");
      }
    }
  } catch (err) {
    alert("移除文件失败：" + (err.message || err));
  }
}

function highlightActiveFilePill() {
  document.querySelectorAll(".demo-pills .pill").forEach(el => {
    el.classList.toggle("active", el.id === "pill-" + currentFileId);
  });
}

async function switchToUploadedFile(fileId, fileName) {
  const selectionId = ++fileSelectionSerial;
  ++previewRequestSerial;
  currentFileId = fileId;
  currentPage = 0;
  currentScreenIdx = 0;
  document.getElementById("optStartPage").value = 1;
  document.getElementById("optEndPage").value = "";
  document.getElementById("currentFileName").innerText = "📘 当前文档：" + fileName;
  highlightActiveFilePill();

  const res = await fetch("/api/doc_info?file_id=" + encodeURIComponent(fileId));
  const info = await res.json();
  if (selectionId !== fileSelectionSerial || fileId !== currentFileId) return;
  if (info.detected) {
    const banner = document.getElementById("detectBanner");
    banner.style.display = "block";
    banner.innerText = "🤖 智能识别：" + info.detected.reason;
    selectMode(info.detected.recommended_mode, true);
  }
  renderHighlightsBar(info.highlights || []);
  await refreshPreview();
}

async function loadDemo(fileId, mode) {
  const selectionId = ++fileSelectionSerial;
  ++previewRequestSerial;
  currentFileId = fileId;
  currentPage = 0;
  currentScreenIdx = 0;
  document.getElementById("optStartPage").value = 1;
  document.getElementById("optEndPage").value = "";
  document.getElementById("detectBanner").style.display = "none";
  const names = {
    "demo_academic_2col": "📄 内置样张：demo_双栏学术论文.pdf",
    "demo_tech_book": "📊 内置样张：demo_带图表专业书.pdf",
    "demo_scanned_book": "🔍 内置样张：demo_泛黄扫描版书籍.pdf",
    "demo_comic_manga": "🎨 内置样张：demo_PDF漫画双页跨页.pdf",
    "demo_pure_text": "📖 内置样张：demo_纯文字小说书籍.pdf"
  };
  document.getElementById("currentFileName").innerText = names[fileId] || fileId;
  highlightActiveFilePill();
  const res = await fetch("/api/doc_info?file_id=" + encodeURIComponent(fileId));
  const info = await res.json();
  if (selectionId !== fileSelectionSerial || fileId !== currentFileId) return;
  renderHighlightsBar(info.highlights || []);
  selectMode(mode);
}

async function handleFileUpload(file) {
  if (!file) return;
  const selectionId = ++fileSelectionSerial;
  // Keep the current document's pending preview valid until a new PDF is
  // accepted. Successful uploads change file_id, which rejects older images.
  const previousName = document.getElementById("currentFileName").innerText;
  document.getElementById("currentFileName").innerText = "⏳ 正在上传并建立图表索引：" + file.name + " ...";
  const form = new FormData();
  form.append("file", file);
  let data;
  try {
    const res = await fetch("/api/upload", {
      method: "POST",
      headers: { "X-KPW6-Token": KPW6_CSRF_TOKEN },
      body: form
    });
    data = await res.json();
    if (selectionId !== fileSelectionSerial) return;
    if (!res.ok || data.error) throw new Error(data.error || "无法上传 PDF");
  } catch (err) {
    if (selectionId !== fileSelectionSerial) return;
    document.getElementById("currentFileName").innerText = previousName;
    document.getElementById("loadingBadge").style.display = "none";
    alert("上传失败：" + (err.message || err));
    return;
  }
  if (data.file_id) {
    currentFileId = data.file_id;
    currentPage = 0;
    currentScreenIdx = 0;
    document.getElementById("optStartPage").value = 1;
    document.getElementById("optEndPage").value = "";
    document.getElementById("currentFileName").innerText = "📘 当前文档：" + data.filename;
    if (data.uploaded) {
      renderUploadedPills(data.uploaded);
    }
    highlightActiveFilePill();
    renderHighlightsBar(data.highlights || []);
    if (data.detected) {
      const banner = document.getElementById("detectBanner");
      banner.style.display = "block";
      banner.innerText = "🤖 智能识别：" + data.detected.reason;
      selectMode(data.detected.recommended_mode);
    } else {
      refreshPreview();
    }
  }
}

function renderHighlightsBar(highlights) {
  currentHighlights = highlights || [];
  const bar = document.getElementById("highlightsBar");
  const list = document.getElementById("highlightsList");
  list.innerHTML = "";
  if (!currentHighlights.length) {
    bar.style.display = "none";
    return;
  }
  bar.style.display = "flex";
  currentHighlights.forEach(h => {
    const btn = document.createElement("button");
    const isTbl = h.kind === "table";
    btn.className = "pill " + (isTbl ? "pill-table" : "pill-figure");
    btn.dataset.pageStart = h.page_num;
    btn.dataset.pageEnd = h.end_page;
    btn.innerText = (isTbl ? "📊 " : "📈 ") + h.label;
    btn.onclick = () => jumpToPage(h.page_num);
    list.appendChild(btn);
  });
  updateHighlightPillsActiveState();
}

function updateHighlightPillsActiveState() {
  document.querySelectorAll("#highlightsList .pill").forEach(btn => {
    const s = parseInt(btn.dataset.pageStart);
    const e = parseInt(btn.dataset.pageEnd);
    btn.classList.toggle("active", currentPage >= s && currentPage <= e);
  });
}

function setDpiPreset(dpi) {
  currentDpiPreset = dpi;
  [300, 450, 600].forEach(d => {
    document.getElementById("dpi-" + d).classList.toggle("active", d === dpi);
  });
  const labels = {
    300: "300 PPI (标准轻巧)",
    450: "450 PPI (1.5x 超清)",
    600: "600 PPI (2.0x 超采样)"
  };
  document.getElementById("valDpiPreset").innerText = labels[dpi] || (dpi + " PPI");
  updateQualityBadgeText();
  refreshPreview(currentScreenIdx);
}

function setStrokeBoldness(level) {
  currentStrokeBoldness = level;
  [0, 1, 2, 3].forEach(lv => {
    document.getElementById("bold-" + lv).classList.toggle("active-emerald", lv === level);
  });
  const labels = {
    0: "0: 原细度",
    1: "+1: 微加粗 (修断笔)",
    2: "+2: 中度黑体化",
    3: "+3: 强力加粗 (细图纸)"
  };
  document.getElementById("valStrokeBold").innerText = labels[level] || ("+" + level);
  updateQualityBadgeText();
  refreshPreview(currentScreenIdx);
}

function updateQualityBadgeText() {
  const vec = document.getElementById("optVectorLossless").checked;
  const enh = document.getElementById("optEnhance").checked;
  const rmWm = document.getElementById("optRemoveWatermark").checked;
  const deskew = document.getElementById("optAutoDeskew").checked;
  const boldText = currentStrokeBoldness > 0 ? ` · 加粗+${currentStrokeBoldness}` : "";
  const extraTags = [rmWm ? "去水印" : "", deskew ? "自动纠偏" : ""].filter(Boolean).join("·");
  let modeDesc = `${currentDpiPreset} PPI`;
  if (currentMode === "scanned_book" || currentMode === "comic_manga" || enh || currentStrokeBoldness > 0 || rmWm || deskew || !vec) {
    modeDesc += ` 超清精修${boldText}${extraTags ? "·" + extraTags : ""}`;
  } else {
    modeDesc += ` · 矢量优先`;
  }
  const el = document.getElementById("statQualityMode");
  if (el) el.innerText = modeDesc;
}

function setCoverMode(mode) {
  currentCoverMode = mode;
  ["smart_card", "first_page", "custom_image", "none"].forEach(m => {
    document.getElementById("cov-" + m).classList.toggle("active", m === mode);
  });
  const labels = {
    "smart_card": "精装书封卡",
    "first_page": "原书首页全幅",
    "custom_image": "自定义封面图",
    "none": "不加独立封面"
  };
  document.getElementById("valCoverMode").innerText = labels[mode] || mode;
  document.getElementById("coverTextInputs").style.display = (mode === "smart_card") ? "flex" : "none";
  document.getElementById("coverUploadWrap").style.display = (mode === "custom_image") ? "block" : "none";
  if (currentPage !== 0 && mode !== "none") {
    jumpToPage(0);
  } else {
    refreshPreview(0);
  }
}

async function handleCoverUpload(file) {
  if (!file) return;
  const uploadId = ++coverUploadSerial;
  const statusEl = document.getElementById("coverUploadStatus");
  statusEl.innerText = "⏳ 正在上传封面图...";
  const form = new FormData();
  form.append("file", file);
  try {
    const res = await fetch("/api/upload_cover", {
      method: "POST",
      headers: { "X-KPW6-Token": KPW6_CSRF_TOKEN },
      body: form
    });
    const data = await res.json();
    if (uploadId !== coverUploadSerial) return;
    if (!res.ok || data.error || !data.cover_id) {
      throw new Error(data.error || "无法读取封面图片，请选择完整的图片文件。");
    }
    currentCustomCoverId = data.cover_id;
    statusEl.innerText = "✅ 已加载自定义封面：" + data.filename;
    if (currentPage !== 0) jumpToPage(0);
    else refreshPreview(0);
  } catch (err) {
    if (uploadId !== coverUploadSerial) return;
    statusEl.innerText = "❌ 上传失败：" + (err.message || "请重试");
  } finally {
    if (uploadId === coverUploadSerial) document.getElementById("coverFileInput").value = "";
  }
}

function getOptionsPayload() {
  updateQualityBadgeText();
  const rawStart = parseInt(document.getElementById("optStartPage").value || "1");
  const rawEndStr = (document.getElementById("optEndPage").value || "").trim();
  const rawEnd = rawEndStr ? parseInt(rawEndStr) : null;
  return {
    file_id: currentFileId,
    page_num: currentPage,
    mode: currentMode,
    split_pages: document.getElementById("optSplitPages").checked,
    landscape_2x: document.getElementById("optLandscape").checked,
    trim_header_footer: document.getElementById("optTrimHeader").checked,
    enhance_contrast: document.getElementById("optEnhance").checked,
    clean_spine_shadow: document.getElementById("optCleanSpine").checked,
    remove_watermark: document.getElementById("optRemoveWatermark").checked,
    auto_deskew: document.getElementById("optAutoDeskew").checked,
    gamma_strength: parseFloat(document.getElementById("optGamma").value),
    margin_Percent: parseFloat(document.getElementById("optMargin").value),
    comic_split_spread: document.getElementById("optComicSplit").checked,
    manga_rtl: document.getElementById("optMangaRtl").checked,
    comic_full_bleed: document.getElementById("optComicBleed").checked,
    reflow_font_size: parseFloat(document.getElementById("optFontSize").value),
    line_spacing: parseFloat(document.getElementById("optLineSpacing").value),
    dpi_preset: currentDpiPreset,
    vector_lossless: document.getElementById("optVectorLossless").checked,
    stroke_boldness: currentStrokeBoldness,
    sharpen_strength: parseFloat(document.getElementById("optSharpen").value),
    cover_mode: currentCoverMode,
    custom_cover_title: document.getElementById("optCoverTitle").value || "",
    custom_cover_author: document.getElementById("optCoverAuthor").value || "",
    custom_cover_id: currentCustomCoverId,
    start_page: isNaN(rawStart) ? 1 : rawStart,
    end_page: (rawEnd !== null && !isNaN(rawEnd)) ? rawEnd : null,
    split_volumes_every: currentSplitVolumesEvery,
    page_overrides: getCurrentFileOverrides(),
  };
}

function clearPreviewAfterError(message) {
  currentScreens = [];
  currentSlicesMeta = [];
  currentScreenIdx = 0;
  currentHasCoverScreen = false;
  document.getElementById("sourcePreviewImg").src = "";
  document.getElementById("singleKpw6Img").src = "";
  document.getElementById("sliceHotspotsLayer").innerHTML = "";
  document.getElementById("screenSelectorPills").innerHTML = "";
  document.getElementById("kpw6ScreensContainer").innerHTML = "";
  document.getElementById("singleKpw6Bezel").style.display = "none";
  document.getElementById("skipPageNotice").style.display = "none";
  ["statSlices", "statScreens", "statZoom"].forEach(id => {
    document.getElementById(id).innerText = "-";
  });
  const notice = document.getElementById("previewErrorNotice");
  notice.innerText = "预览失败：" + message;
  notice.style.display = "block";
}

async function refreshPreview(targetScreenIdx = 0) {
  const requestId = ++previewRequestSerial;
  const requestedFileId = currentFileId;
  const badge = document.getElementById("loadingBadge");
  badge.style.display = "block";
  badge.innerText = `⏳ 正在渲染第 ${currentPage + 1} 页预览...`;
  document.getElementById("previewErrorNotice").style.display = "none";

  try {
    const payload = getOptionsPayload();
    const res = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-KPW6-Token": KPW6_CSRF_TOKEN },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (requestId !== previewRequestSerial || payload.file_id !== currentFileId) return;
    if (!res.ok || data.error) throw new Error(data.error || "无法生成预览，请重试。");

    currentPage = data.page_num;
    totalPages = data.total_pages;
    currentScreens = data.kpw6_pages || [];
    currentSlicesMeta = data.slices_meta || [];
    currentHasCoverScreen = !!data.has_cover_screen;

    if (data.detected_title) {
      document.getElementById("optCoverTitle").placeholder = "封面标题：" + data.detected_title.slice(0, 36);
    }
    if (data.detected_author) {
      document.getElementById("optCoverAuthor").placeholder = "副标题：" + data.detected_author.slice(0, 36);
    }

    document.getElementById("pageInputBox").value = currentPage + 1;
    document.getElementById("pageInputBox").max = totalPages;
    document.getElementById("totalPagesNum").innerText = totalPages;
    document.getElementById("optEndPage").placeholder = `末页(${totalPages})`;

    const slider = document.getElementById("pageSlider");
    slider.max = totalPages;
    slider.value = currentPage + 1;

    document.getElementById("statSlices").innerText = data.slice_count;
    document.getElementById("statScreens").innerText = data.kpw6_screens_count;
    document.getElementById("statZoom").innerText = data.magnification + "x";
    document.getElementById("sourcePreviewImg").src = data.source_annotated;

    renderPageOverrideToolbar();
    updateHighlightPillsActiveState();
    renderSliceHotspots();

    const isSkipped = (data.page_override === "skip");
    const hasRenderableScreens = currentScreens.length > 0;
    document.getElementById("skipPageNotice").style.display = isSkipped ? "block" : "none";
    document.getElementById("singleKpw6Bezel").style.display = hasRenderableScreens ? "flex" : "none";
    if (isSkipped) {
      document.getElementById("skipPageNumText").innerText = currentPage + 1;
    }
    if (!hasRenderableScreens) {
      document.getElementById("screenSelectorPills").innerHTML = "";
    }

    // Render Grid View
    const container = document.getElementById("kpw6ScreensContainer");
    container.innerHTML = "";
    if (!hasRenderableScreens) {
      container.innerHTML = `<div style="grid-column:1/-1;text-align:center;color:#fca5a5;padding:36px 20px;background:rgba(239,68,68,0.12);border:2px dashed #ef4444;border-radius:14px;">🚫 <strong>第 ${currentPage + 1} 页已标记为【跳过此页】</strong><div style="font-size:12px;color:#94a3b8;margin-top:6px;">导出电子书或 PDF 时将自动跳过本页</div></div>`;
    }
    currentScreens.forEach((b64, i) => {
      const div = document.createElement("div");
      div.className = "kpw6-frame";
      div.id = "grid-frame-" + i;
      div.innerHTML = `
        <div class="screen-label">
          <strong>KPW6 第 ${i + 1} / ${currentScreens.length} 屏</strong>
          <span style="font-size:11px;color:#38bdf8;cursor:pointer;" onclick="openLightbox(currentScreens[${i}], '第 ${currentPage+1} 页 · KPW6 第 ${i+1} 屏', 'kpw6', ${i})">🔍 高清放大</span>
        </div>
        <img src="${b64}" alt="KPW6 Screen ${i+1}" onclick="openLightbox(currentScreens[${i}], '第 ${currentPage+1} 页 · KPW6 第 ${i+1} 屏', 'kpw6', ${i})">
      `;
      container.appendChild(div);
    });

    // Render Single 1:1 View
    if (hasRenderableScreens) {
      const safeIdx = (targetScreenIdx === -1)
        ? Math.max(0, currentScreens.length - 1)
        : Math.max(0, Math.min(currentScreens.length - 1, targetScreenIdx));
      selectKpw6Screen(safeIdx, false);
    }
  } catch (err) {
    if (requestId !== previewRequestSerial || requestedFileId !== currentFileId) return;
    clearPreviewAfterError(err.message || "无法生成预览，请重试。");
  } finally {
    if (requestId === previewRequestSerial) badge.style.display = "none";
  }
}

function renderSliceHotspots() {
  const layer = document.getElementById("sliceHotspotsLayer");
  layer.innerHTML = "";
  currentSlicesMeta.forEach(s => {
    const spot = document.createElement("div");
    spot.className = "slice-hotspot";
    spot.dataset.outPageIdx = s.out_page_idx;
    spot.style.left = s.left_pct + "%";
    spot.style.top = s.top_pct + "%";
    spot.style.width = s.width_pct + "%";
    spot.style.height = s.height_pct + "%";
    spot.title = `点击查看区块 #${s.index} 对应的【KPW6 第 ${s.out_page_idx + 1} 屏】`;
    spot.onclick = (e) => {
      e.stopPropagation();
      selectKpw6Screen(s.out_page_idx, true);
    };
    layer.appendChild(spot);
  });
}

function selectKpw6Screen(idx, flash = false) {
  if (!currentScreens.length) return;
  currentScreenIdx = Math.max(0, Math.min(currentScreens.length - 1, idx));
  document.getElementById("singleKpw6Img").src = currentScreens[currentScreenIdx];

  const pillsWrap = document.getElementById("screenSelectorPills");
  pillsWrap.innerHTML = "";
  currentScreens.forEach((_, i) => {
    const b = document.createElement("button");
    b.className = "screen-pill" + (i === currentScreenIdx ? " active" : "");
    b.innerText = (currentHasCoverScreen && i === 0) ? `🖼️ 第 1 屏 (专属封面)` : `第 ${i + 1} 屏`;
    b.onclick = () => selectKpw6Screen(i, false);
    pillsWrap.appendChild(b);
  });

  // Highlight matching hotspots on the left Source PDF
  document.querySelectorAll(".slice-hotspot").forEach(el => {
    el.classList.toggle("active-slice", parseInt(el.dataset.outPageIdx) === currentScreenIdx);
  });

  // Highlight matching card in Grid View
  document.querySelectorAll(".kpw6-frame").forEach((el, i) => {
    el.classList.toggle("active-frame", i === currentScreenIdx);
  });

  if (flash) {
    if (rightViewMode === "single") {
      const bezel = document.getElementById("singleKpw6Bezel");
      bezel.classList.add("flash-highlight");
      setTimeout(() => bezel.classList.remove("flash-highlight"), 500);
    } else {
      const card = document.getElementById("grid-frame-" + currentScreenIdx);
      if (card) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }
}

function stepKpw6Screen(delta) {
  const nextScreen = currentScreenIdx + delta;
  if (nextScreen >= 0 && nextScreen < currentScreens.length) {
    selectKpw6Screen(nextScreen, false);
  } else if (nextScreen >= currentScreens.length && currentPage + 1 < totalPages) {
    // Auto-advance to next PDF page's Screen 1!
    currentPage += 1;
    refreshPreview(0);
  } else if (nextScreen < 0 && currentPage > 0) {
    // Auto-step back to previous PDF page's last screen!
    currentPage -= 1;
    refreshPreview(-1);
  }
}

function jumpToPage(targetPage0) {
  const clamped = Math.max(0, Math.min(totalPages - 1, targetPage0));
  if (isNaN(clamped)) return;
  currentPage = clamped;
  refreshPreview(0);
}

function changePage(delta) {
  const next = Math.max(0, Math.min(totalPages - 1, currentPage + delta));
  if (next !== currentPage) {
    currentPage = next;
    refreshPreview(0);
  }
}

function zoomCurrentKpw6Screen() {
  if (!currentScreens.length) return;
  openLightbox(
    currentScreens[currentScreenIdx],
    `第 ${currentPage + 1} 页 · KPW6 第 ${currentScreenIdx + 1} / ${currentScreens.length} 屏`,
    "kpw6",
    currentScreenIdx
  );
}

async function openLightbox(src, title, targetType = "kpw6", screenIdx = 0) {
  if (!src) return;
  const requestId = ++lightboxRequestSerial;
  const imgEl = document.getElementById("lightboxImg");
  const badgeEl = document.getElementById("lightboxHdBadge");
  imgEl.src = src;
  document.getElementById("lightboxTitle").innerText = "🔍 " + (title || "高清细节放大镜");
  badgeEl.innerText = "⏳ 正在加载高清图层...";
  badgeEl.style.background = "rgba(59,130,246,0.2)";
  badgeEl.style.color = "#93c5fd";
  badgeEl.style.borderColor = "#3b82f6";
  document.getElementById("lightboxModal").style.display = "flex";

  try {
    const payload = {
      ...getOptionsPayload(),
      target_type: targetType,
      screen_idx: screenIdx,
    };
    const res = await fetch("/api/preview_hd", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-KPW6-Token": KPW6_CSRF_TOKEN },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (requestId !== lightboxRequestSerial || document.getElementById("lightboxModal").style.display !== "flex") return;
    if (!res.ok || data.error) throw new Error(data.error || "高清图层暂不可用");
    if (data.hd_image) {
      imgEl.src = data.hd_image;
      badgeEl.innerText = `✨ 已加载高清图层 (${data.width}×${data.height} px)`;
      badgeEl.style.background = "rgba(16,185,129,0.2)";
      badgeEl.style.color = "#6ee7b7";
      badgeEl.style.borderColor = "#10b981";
    }
  } catch (err) {
    if (requestId !== lightboxRequestSerial || document.getElementById("lightboxModal").style.display !== "flex") return;
    badgeEl.innerText = "高清图层暂不可用，当前显示预览图";
  }
}

function closeLightbox() {
  ++lightboxRequestSerial;
  document.getElementById("lightboxModal").style.display = "none";
}

function setLightboxZoom(pxWidth) {
  document.getElementById("lightboxImg").style.width = pxWidth + "px";
}

const FORMAT_META = {
  "epub_layout": {
    short: ".EPUB (版面精排+封面)",
    btn: "🚀 一键导出【.EPUB 满屏精排版】(推荐传 Send to Kindle)",
    hint: "✨ <strong>图像版 EPUB：</strong>保留生成的分屏图像，内置封面与图表目录。可尝试通过 Send to Kindle 传书；设备上的版式与封面显示请实际确认。"
  },
  "pdf": {
    short: ".PDF (矢量优先版)",
    btn: "🚀 一键导出【.PDF 重排版】(封面由当前设置决定)",
    hint: "📄 <strong>PDF 格式：</strong>在适用页面上保留矢量文字与公式，启用图像增强时可能栅格化。文件大小取决于输入与处理选项，独立封面由当前设置决定。"
  },
  "azw3": {
    short: ".AZW3 (Kindle KF8 原生)",
    btn: "🚀 一键导出【.AZW3 Kindle 原生格式】(USB 数据线直传首选)",
    hint: "🔌 <strong>USB 数据线直传首选：</strong>自动调用本机 Calibre 引擎编译为 Kindle 原生 KF8 (<code>.azw3</code>) 格式，内置封面与目录，适合超大型扫描书免云端限制直拷 <code>documents/</code> 目录。"
  },
  "epub_reflow": {
    short: ".EPUB (流式可调字号)",
    btn: "🚀 一键导出【.EPUB 流式可调字号版】(纯文字小说/长文推荐)",
    hint: "📖 <strong>纯文字重排电子书：</strong>提取正文、合并断句并生成章节目录。流式文字可在支持的阅读器中调整字号，字体、词典和封面显示取决于阅读器。"
  },
  "mobi": {
    short: ".MOBI (经典 Kindle 格式)",
    btn: "🚀 一键导出【.MOBI 经典 Kindle 格式】(含封面与目录)",
    hint: "📚 <strong>经典 .MOBI 格式：</strong>调用本机 Calibre 引擎打包为经典 MOBI 电子书格式，兼容旧款 Kindle 与各类本地阅读器管理软件。"
  },
  "cbz": {
    short: ".CBZ (1264×1680 PNG 漫画包)",
    btn: "🚀 一键导出【.CBZ 1264×1680 PNG 漫画/图集包】",
    hint: "🎨 <strong>标准漫画归档包 (.cbz)：</strong>将生成的 1264×1680 封面与所有重排屏幕以无损 PNG 封装并附带 <code>ComicInfo.xml</code>，适合漫画阅读器或 KCC 二次处理。"
  }
};

function setExportFormat(fmt, isAuto = false) {
  if (!isAuto) userManuallyChoseFormat = true;
  currentExportFormat = fmt;
  ["epub_layout", "pdf", "azw3", "epub_reflow", "mobi", "cbz"].forEach(k => {
    const el = document.getElementById("fmt-" + k);
    if (el) el.classList.toggle("active-emerald", k === fmt);
  });
  const m = FORMAT_META[fmt] || FORMAT_META["epub_layout"];
  document.getElementById("valExportFormat").innerText = m.short;
  document.getElementById("mainExportBtn").innerText = m.btn;
  document.getElementById("formatHintText").innerHTML = m.hint;
}

async function exportSelectedFormat(overrideFormat = null) {
  const fmt = overrideFormat || currentExportFormat;
  const isBundle = (fmt === "bundle_both");
  const btn = document.getElementById(isBundle ? "bundleExportBtn" : "mainExportBtn");
  const oldText = btn.innerText;
  btn.innerText = `⏳ 正在为 KPW6 生成 ${fmt.toUpperCase()} 文件（请查看上方进度条）...`;
  btn.disabled = true;

  const jobId = "job_" + Date.now() + "_" + Math.random().toString(36).slice(2, 7);
  activeExportJobId = jobId;
  const progPanel = document.getElementById("exportProgressPanel");
  const progTitle = document.getElementById("exportProgressTitle");
  const progEta = document.getElementById("exportProgressEta");
  const progPct = document.getElementById("exportProgressPct");
  const progFill = document.getElementById("exportProgressBarFill");
  const cancelBtn = document.getElementById("cancelExportBtn");
  if (cancelBtn) cancelBtn.disabled = false;

  progPanel.style.display = "flex";
  progTitle.innerText = "正在启动 KPW6 排版与封面引擎...";
  progEta.innerText = "已用时 0.0s · 正在计算预计耗时...";
  progPct.innerText = "1%";
  progFill.style.width = "2%";

  const pollTimer = setInterval(async () => {
    try {
      const r = await fetch("/api/export_progress?job_id=" + encodeURIComponent(jobId));
      const p = await r.json();
      if (p && p.status) {
        const pct = Math.max(1, Math.min(100, p.percent || 1));
        progTitle.innerText = p.stage_text || "正在精排页面...";
        progPct.innerText = pct + "%";
        progFill.style.width = pct + "%";
        const elapsed = (p.elapsed_sec || 0).toFixed(1);
        const etaStr = (p.eta_sec !== null && p.eta_sec !== undefined && p.eta_sec > 0.2)
          ? `预计剩余 ~${p.eta_sec.toFixed(1)}s`
          : "即将完成";
        progEta.innerText = `已用时 ${elapsed}s · ${etaStr}`;
      }
    } catch (e) {}
  }, 260);

  try {
    const payload = {
      ...getOptionsPayload(),
      export_format: fmt,
      job_id: jobId,
    };
    const res = await fetch("/api/export_unified", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-KPW6-Token": KPW6_CSRF_TOKEN },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    clearInterval(pollTimer);
    if (data.cancelled) {
      progPanel.style.display = "none";
      return;
    }
    if (data.error) {
      progPanel.style.display = "none";
      alert("导出失败：" + data.error);
      return;
    }

    progFill.style.width = "100%";
    progPct.innerText = "100%";
    progTitle.innerText = "✅ 导出完成！";
    setTimeout(() => { progPanel.style.display = "none"; }, 1800);

    const toast = document.getElementById("exportToast");
    toast.style.display = "flex";
    document.getElementById("toastText").innerText = data.message || `✅ 已生成：${data.output_filename}`;

    const dl1 = document.getElementById("downloadLink");
    const dl2 = document.getElementById("downloadSecondLink");
    const volWrap = document.getElementById("volumeDownloadsContainer");
    volWrap.innerHTML = "";

    if (data.volumes && data.volumes.length > 1) {
      dl1.style.display = "none";
      dl2.style.display = "none";
      volWrap.style.display = "flex";
      data.volumes.forEach((v, idx) => {
        const a = document.createElement("a");
        a.className = idx === 0 ? "btn btn-emerald" : "btn btn-primary";
        a.href = v.download_url;
        a.setAttribute("download", "");
        a.innerText = v.download_label || `💾 下载第 ${idx + 1} 卷`;
        volWrap.appendChild(a);
        if (v.second_download_url) {
          const a2 = document.createElement("a");
          a2.className = "btn btn-purple";
          a2.href = v.second_download_url;
          a2.setAttribute("download", "");
          a2.innerText = v.second_download_label || `💾 下载第 ${idx + 1} 卷配套 .PDF`;
          volWrap.appendChild(a2);
        }
      });
    } else {
      volWrap.style.display = "none";
      dl1.style.display = "inline-flex";
      dl1.href = data.download_url;
      dl1.innerText = data.download_label || "💾 下载文件到本地";

      if (data.second_download_url) {
        dl2.href = data.second_download_url;
        dl2.innerText = data.second_download_label || "💾 下载配套 EPUB";
        dl2.style.display = "inline-flex";
      } else {
        dl2.style.display = "none";
      }
    }

    const viewBtn = document.getElementById("viewPdfOnlineLink");
    if (data.view_url) {
      viewBtn.href = data.view_url;
      viewBtn.style.display = "inline-flex";
    } else {
      viewBtn.style.display = "none";
    }
  } catch (err) {
    clearInterval(pollTimer);
    progPanel.style.display = "none";
    alert("导出异常：" + (err && err.message ? err.message : err));
  } finally {
    if (activeExportJobId === jobId) activeExportJobId = "";
    clearInterval(pollTimer);
    btn.innerText = oldText;
    btn.disabled = false;
  }
}

async function cancelActiveExport() {
  if (!activeExportJobId) return;
  const cancelBtn = document.getElementById("cancelExportBtn");
  if (cancelBtn) cancelBtn.disabled = true;
  document.getElementById("exportProgressTitle").innerText = "⏹ 正在中止导出任务...";
  try {
    await fetch("/api/cancel_export", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-KPW6-Token": KPW6_CSRF_TOKEN },
      body: JSON.stringify({ job_id: activeExportJobId })
    });
  } catch (e) {}
}

async function openOutputFolder() {
  await fetch("/api/open_folder", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-KPW6-Token": KPW6_CSRF_TOKEN },
    body: "{}"
  });
}

window.addEventListener("DOMContentLoaded", () => {
  fetchUploadedFilesAndInit();
});
