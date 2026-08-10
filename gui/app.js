const AUTO_REFRESH_INTERVAL_MS = 5 * 60 * 1000;
const LIVE_RESEARCH_REFRESH_INTERVAL_MS = 3 * 60 * 1000;
const MODEL_LIVE_REFRESH_INTERVAL_MS = 3 * 60 * 1000;
const DATA_LIVE_REFRESH_INTERVAL_MS = 30 * 1000;
const FLOATING_X_SCROLL_SELECTOR = ".table-shell, .factor-table-scroll";
const MODEL_API_PREFIX = "/model";
const THEME_STORAGE_KEY = "fxalpha.theme";
const GUI_BUILD_ID = "20260810-overview-v128";

document.documentElement.dataset.guiBuild = GUI_BUILD_ID;

const API_ORIGIN = window.location.protocol === "file:"
  ? "http://127.0.0.1:18081"
  : window.location.hostname === "localhost"
    ? `${window.location.protocol}//127.0.0.1:${window.location.port || "18081"}`
    : "";

const state = {
  health: null,
  platformRuntime: null,
  automationStatus: null,
  automationActionResult: null,
  automationControlBusy: false,
  evaluationProfile: null,
  evaluationModeSwitching: false,
  codexUsageSnapshot: null,
  deepseekUsageSnapshot: null,
  data: null,
  dataLiveStatus: null,
  dataLivePreflightResult: null,
  dataQueryFields: null,
  dataQueryResult: null,
  dataQuerySelectedFields: null,
  dataQueryExpandedGroups: null,
  dataQueryLoading: false,
  dataLiveRefreshTimer: null,
  factorStatus: null,
  factorConsole: null,
  factorRunView: null,
  orchestratorEvents: null,
  orchestratorTraces: null,
  modelOrchestratorEvents: null,
  modelOrchestratorTraces: null,
  factorResearchPreflight: null,
  factorResearchControl: null,
  factorOverviewSnapshot: null,
  factorLibraryRaw: null,
  duplicateAudit: null,
  factorAudit: null,
  factorAuditRunStatus: null,
  fullResearchConsole: null,
  factorMap: null,
  factorMapLoading: false,
  modelStatus: null,
  modelPreflight: null,
  modelOrchestratorStatus: null,
  modelCurrentContext: null,
  modelResearchCurrent: null,
  modelResearchJournal: null,
  modelResearchOrchTraces: null,
  modelResearchMcpTraces: null,
  modelFeatureSets: null,
  modelRuns: null,
  modelRegistry: null,
  modelProduction: null,
  modelBacktest: null,
  predictionStatus: null,
  tradingStatus: null,
  riskPolicyStatus: null,
  dailyOpsStatus: null,
  paperFleetStatus: null,
  paperBenchmark: null,
  paperReplayBusy: false,
  latestTradingResult: null,
  latestDataAction: null,
  pipelineStatus: null,
  maintenanceStatus: null,
  latestMaintenanceAction: null,
  lastRunId: null,
  refreshTimer: null,
  liveRefreshTimer: null,
  factorAuditRunPollTimer: null,
  modelBacktestRequest: null,
  modelBacktestLastUrl: "",
  modelBacktestLastLoadedAt: 0,
  modelBacktestLoading: false,
  modelResultsRefreshInFlight: false,
  modelBacktestRenderSignature: "",
  modelBacktestHoverFrame: 0,
  modelBacktestHoverPendingDate: "",
  refreshInFlight: false,
  liveRefreshInFlight: false,
  pendingRefreshReason: null,
  lastRefreshAt: null,
  nextAutoRefreshAt: null,
  activePanel: "overview",
  selectedPaperAccountId: localStorage.getItem("fxalpha.paperAccountId") || "",
  paperTradingTab: localStorage.getItem("fxalpha.paperTradingTabV2") || "overview",
  paperConsoleTab: localStorage.getItem("fxalpha.paperConsoleTabV1") || "status",
  paperLedgerQueryDate: "",
  paperTargetFilter: localStorage.getItem("fxalpha.paperTargetFilter") || "all",
  activeModelWorkspace: localStorage.getItem("fxalpha.activeModelWorkspace") || "command",
  activeModelLogView: localStorage.getItem("fxalpha.activeModelLogView") || "interaction",
  activeModelLogSessionId: localStorage.getItem("fxalpha.activeModelLogSessionId") || "",
  modelCommandMode: localStorage.getItem("fxalpha.modelCommandMode") === "production" ? "production" : "research",
  modelFeatureSource: localStorage.getItem("fxalpha.modelFeatureSource") === "custom" ? "custom" : "catalog",
  modelSelectedFeatureSetId: "",
  modelCustomFeatureSetId: "",
  modelSelectedFactorIds: new Set(),
  modelFactorQuery: "",
  modelFactorCategory: "all",
  activeModelAssetLane: localStorage.getItem("fxalpha.activeModelAssetLane") || "research",
  dataFoundationTab: localStorage.getItem("fxalpha-data-foundation-tab") || "status",
  activeWorkspace: null,
  activeLibraryTab: "registry",
  selectedFeatureSetName: "",
  backendMode: "unknown",
  inspector: null,
  candidateSort: "time",
  researchStepFilter: "all",
  activeResearchNavTarget: "research-progress-board",
  activeOrchestratorTraceId: "",
  libraryFilter: {
    query: "",
    status: "all",
    category: "all",
    holdingPeriod: "all",
  },
  modelBacktestSelection: {
    selector: "latest",
    modelId: "",
    modelRunId: "",
    label: "最新",
    rollingDaily: false,
  },
  modelBacktestCategory: "",
  modelBacktestSort: "time",
  modelBacktestSortDirection: "desc",
};

const panelButtons = document.querySelectorAll(".nav-item");
const panels = document.querySelectorAll(".panel");
const workspaceSections = document.querySelectorAll(".workspace-section");
const modelWorkspaceTabs = document.querySelectorAll(".model-workspace-tab");
const modelWorkspaceSections = document.querySelectorAll(".model-workspace-section");
const metricTemplate = document.getElementById("metric-card-template");
const researchWorkspaceNav = document.getElementById("research-workspace-nav");
const rail = document.querySelector(".rail");
const navToggle = document.getElementById("nav-toggle");
let floatingXScrollbarSeq = 0;
let floatingXScrollbarRaf = 0;
let paperPlanResizeObserver = null;
const guidancePresets = [
  "这个方向偏量价，请加入估值或质量锚点，并避免只做窗口微调。",
  "请优先降低慢变量持久性带来的过拟合风险，换一组更快的输入信号，不要继续增强慢变量。",
  "请改变信号源、条件变量或算子几何，避免重复同一类因子值模式。",
  "请提高可交易性，降低换手，并解释为什么这个信号能在目标持仓周期内兑现。",
];

function apiUrl(url) {
  return API_ORIGIN && url.startsWith("/") ? `${API_ORIGIN}${url}` : url;
}

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function syncThemeToggle() {
  const button = document.getElementById("theme-toggle");
  const label = document.getElementById("theme-toggle-label");
  if (!button) return;
  const light = currentTheme() === "light";
  button.setAttribute("aria-pressed", light ? "true" : "false");
  button.setAttribute("aria-label", light ? "切换为深色模式" : "切换为浅色模式");
  if (label) label.textContent = light ? "浅色模式" : "深色模式";
}

function setTheme(theme, { persist = true } = {}) {
  const normalized = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = normalized;
  document.documentElement.style.colorScheme = normalized;
  if (persist) {
    try {
      window.localStorage?.setItem(THEME_STORAGE_KEY, normalized);
    } catch (_error) {
      // Theme still applies for the current session when storage is restricted.
    }
  }
  syncThemeToggle();
  queueFloatingXScrollbarRefresh();
}

function setPanelBusy(panelName, busy) {
  const panel = document.getElementById(`panel-${panelName}`);
  if (!panel) return;
  panel.classList.toggle("is-refreshing", Boolean(busy));
  panel.setAttribute("aria-busy", busy ? "true" : "false");
}

function applyManagedDefault(field, nextValue) {
  if (!field || nextValue === undefined || nextValue === null || nextValue === "") return;
  const value = String(nextValue);
  if (field.tagName === "SELECT" && !Array.from(field.options || []).some((option) => option.value === value)) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = `${value} (runtime)`;
    field.appendChild(option);
  }
  const previousAuto = field.dataset.runtimeDefaultValue || "";
  if (!field.value || field.value === previousAuto) {
    field.value = value;
  }
  field.dataset.runtimeDefaultValue = value;
}

function formField(form, name) {
  return form?.elements?.namedItem ? form.elements.namedItem(name) : null;
}

function floatingXScrollContainers(root = document) {
  return [...new Set(
    [...root.querySelectorAll(FLOATING_X_SCROLL_SELECTOR)]
      .filter((container) => container && !container.classList.contains("floating-x-scrollbar"))
  )];
}

function floatingXScrollbarContentWidth(container) {
  const primary = container.firstElementChild;
  return Math.max(
    Number(container.scrollWidth || 0),
    Number(primary?.scrollWidth || 0),
    Number(container.clientWidth || 0),
  );
}

function ensureFloatingXScrollbar(container) {
  if (!container?.parentElement) return null;
  let barId = container.dataset.floatingXScrollbarId || "";
  let bar = barId ? document.getElementById(barId) : null;
  if (!bar) {
    barId = `floating-x-scrollbar-${++floatingXScrollbarSeq}`;
    bar = document.createElement("div");
    bar.id = barId;
    bar.className = "floating-x-scrollbar";
    bar.setAttribute("aria-hidden", "true");
    bar.innerHTML = `<div class="floating-x-scrollbar__spacer"></div>`;
    container.dataset.floatingXScrollbarId = barId;
    container.parentElement.insertBefore(bar, container.nextSibling);
    const sync = { active: false };
    bar.addEventListener("scroll", () => {
      if (sync.active) return;
      sync.active = true;
      container.scrollLeft = bar.scrollLeft;
      sync.active = false;
    });
    container.addEventListener("scroll", () => {
      if (sync.active) return;
      sync.active = true;
      bar.scrollLeft = container.scrollLeft;
      sync.active = false;
    });
  }
  return bar;
}

function refreshFloatingXScrollbars(root = document) {
  floatingXScrollContainers(root).forEach((container) => {
    const bar = ensureFloatingXScrollbar(container);
    if (!bar) return;
    const spacer = bar.querySelector(".floating-x-scrollbar__spacer");
    const contentWidth = floatingXScrollbarContentWidth(container);
    const clientWidth = Number(container.clientWidth || 0);
    const shouldShow = clientWidth > 0 && contentWidth - clientWidth > 6;
    if (spacer) {
      spacer.style.width = `${Math.ceil(contentWidth)}px`;
    }
    bar.hidden = !shouldShow;
    container.classList.toggle("has-floating-x-scrollbar", shouldShow);
    if (shouldShow && Math.abs(bar.scrollLeft - container.scrollLeft) > 1) {
      bar.scrollLeft = container.scrollLeft;
    }
  });
}

function queueFloatingXScrollbarRefresh(root = document) {
  if (floatingXScrollbarRaf) {
    window.cancelAnimationFrame(floatingXScrollbarRaf);
  }
  floatingXScrollbarRaf = window.requestAnimationFrame(() => {
    floatingXScrollbarRaf = 0;
    refreshFloatingXScrollbars(root);
  });
}

function applyResearchRuntimeDefaults() {
  const form = document.getElementById("research-form");
  const commandForm = document.getElementById("orchestrator-command-form");
  const defaults = serviceOutputs(state.factorStatus).runtime_defaults
    || serviceOutputs(state.factorConsole).runtime_defaults
    || {};
  if (!form || Object.keys(defaults).length === 0) return;
  applyManagedDefault(formField(form, "universe"), defaults.universe);
  applyManagedDefault(formField(form, "start_date"), defaults.selection_start_date);
  applyManagedDefault(formField(form, "end_date"), defaults.selection_end_date);
  applyManagedDefault(formField(form, "benchmark"), defaults.benchmark);
  applyManagedDefault(formField(form, "holding_period"), defaults.holding_period);
  applyManagedDefault(formField(form, "target_adopted"), defaults.target_adopted);
  applyManagedDefault(formField(form, "n_candidates"), defaults.n_candidates);
  applyManagedDefault(formField(form, "n_rounds"), defaults.n_rounds);
  applyManagedDefault(formField(form, "seed_count"), defaults.seed_count);
  applyManagedDefault(formField(form, "seed_max_concurrent"), defaults.seed_max_concurrent);
  applyManagedDefault(formField(form, "max_direction_attempts"), defaults.max_direction_attempts);
  applyManagedDefault(formField(form, "max_stagnation_rounds"), defaults.max_stagnation_rounds);
  applyManagedDefault(formField(form, "top_frac"), defaults.top_frac);
  applyManagedDefault(formField(form, "cost_rate"), defaults.cost_rate);
  applyManagedDefault(formField(form, "rebalance_anchor"), defaults.rebalance_anchor);
  applyManagedDefault(formField(form, "universe_date"), defaults.universe_date);
  if (commandForm) {
    applyManagedDefault(formField(commandForm, "universe"), defaults.universe);
    applyManagedDefault(formField(commandForm, "start_date"), defaults.selection_start_date);
    applyManagedDefault(formField(commandForm, "end_date"), defaults.selection_end_date);
    applyManagedDefault(formField(commandForm, "benchmark"), defaults.benchmark);
    applyManagedDefault(formField(commandForm, "holding_period"), defaults.holding_period);
    applyManagedDefault(formField(commandForm, "target_adopted"), defaults.target_adopted);
    applyManagedDefault(formField(commandForm, "n_candidates"), defaults.n_candidates);
    applyManagedDefault(formField(commandForm, "n_rounds"), defaults.n_rounds);
    applyManagedDefault(formField(commandForm, "top_frac"), defaults.top_frac);
    applyManagedDefault(formField(commandForm, "cost_rate"), defaults.cost_rate);
    const llmModelField = formField(commandForm, "llm_model");
    if (llmModelField && llmModelField.dataset.userSelected !== "true") {
      llmModelField.value = normalizeCommandLlmModel(defaults.llm_model);
      llmModelField.dataset.runtimeDefaultValue = llmModelField.value;
    }
  }
  renderCommandConsole();
}

function modelRuntimeDefaults() {
  return serviceOutputs(state.modelStatus).runtime_defaults || {};
}

function modelCommandPreflightOutputs() {
  return serviceOutputs(state.modelPreflight);
}

function modelCommandOrchestratorOutputs() {
  return serviceOutputs(state.modelOrchestratorStatus);
}

function modelCommandMode() {
  return state.modelCommandMode === "production" ? "production" : "research";
}

function syncModelCommandModeUI() {
  const mode = modelCommandMode();
  const form = document.getElementById("model-command-form");
  const modeField = formField(form, "evaluation_mode");
  if (modeField) modeField.value = mode;
  document.querySelectorAll("[data-model-evaluation-mode]").forEach((button) => {
    const active = button.dataset.modelEvaluationMode === mode;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  document.querySelectorAll("[data-model-mode-pane]").forEach((pane) => {
    pane.hidden = pane.dataset.modelModePane !== mode;
  });
  const title = document.getElementById("model-command-mode-title");
  const note = document.getElementById("model-command-mode-note");
  if (title) title.textContent = mode === "production" ? "生产模式" : "研究模式";
  if (note) note.textContent = mode === "production"
    ? "选择已通过研究确认的来源轮次，执行固定参数的 Production Rolling。"
    : "选择研究因子、研究方案与 Qlib 基线参数，再执行预检和训练。";
  const startButton = document.getElementById("start-model-orch");
  if (startButton) startButton.textContent = mode === "production" ? "启动 Production Rolling" : "启动新研究";
}

function setModelCommandMode(mode) {
  state.modelCommandMode = mode === "production" ? "production" : "research";
  try {
    window.localStorage?.setItem("fxalpha.modelCommandMode", state.modelCommandMode);
  } catch (error) {
    // Ignore storage failures in restricted browser contexts.
  }
  syncModelCommandModeUI();
  renderModelCommandConsole();
}

function modelCommandParamDefaults(preset = "fxalpha") {
  const contract = serviceOutputs(state.modelStatus).contract || {};
  const calibrated = contract.r1_default_lgbm_params || {};
  if (preset !== "qlib") return calibrated;
  return { ...calibrated, ...(contract.qlib_official_alpha158_lgbm_params || {}) };
}

function applyModelCommandParamPreset(preset = "fxalpha") {
  const params = modelCommandParamDefaults(preset);
  document.querySelectorAll("#model-command-form [data-model-param]").forEach((field) => {
    const key = field.dataset.modelParam;
    if (params[key] !== undefined && params[key] !== null) field.value = String(params[key]);
  });
  document.querySelectorAll("[data-model-param-preset]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.modelParamPreset === preset);
  });
}

function modelCommandBaselineOverrides() {
  const defaults = modelCommandParamDefaults("fxalpha");
  const overrides = {};
  document.querySelectorAll("#model-command-form [data-model-param]").forEach((field) => {
    const key = field.dataset.modelParam;
    const value = Number(field.value);
    if (Number.isFinite(value) && Number(defaults[key]) !== value) overrides[key] = value;
  });
  return overrides;
}

function modelFeatureSetCatalogItems() {
  const outputs = serviceOutputs(state.modelFeatureSets);
  const items = Array.isArray(outputs.items)
    ? outputs.items
    : (Array.isArray(outputs.feature_sets) ? outputs.feature_sets : []);
  return items.filter((item) => item && item.feature_set_id && item.trainable !== false);
}

function modelActiveFeatureSetSource() {
  const items = modelFeatureSetCatalogItems();
  return items.find((item) => item.is_active_pointer)
    || items.find((item) => item.updates_active_feature_pointer && Number(item.factor_count) >= Number(serviceOutputs(state.factorLibraryRaw).total || 0))
    || items.find((item) => item.source_type === "all_active")
    || null;
}

function modelFeatureSetSourceLabel(item = {}) {
  if (item.source_type === "all_active") return "全量 Active";
  if (item.recommendation_family || /family[-_ ]?top/i.test(text(item.feature_set_id, ""))) return "Family 审计组合";
  if (item.source_type === "audit_recommended") return "审计推荐";
  if (item.source_type === "diagnostic_ablation" || item.source_type === "diagnostic") return "诊断组合";
  return "历史研究快照";
}

function modelFeatureSetFriendlyName(item = {}) {
  const featureSetId = text(item.feature_set_id, "");
  if (item.source_type === "all_active" || /active50/i.test(featureSetId)) return "Active 全量";
  const familyMatch = featureSetId.match(/family[-_]?top([123])/i);
  if (familyMatch) return `Family Top${familyMatch[1]}`;
  if (/quality[-_]?top12/i.test(featureSetId)) return "Quality Top12";
  if (/custom/i.test(featureSetId)) return "自定义组合";
  return displayModelIdentifier(featureSetId, "Feature Set");
}

function modelSelectedFeatureSet() {
  const featureSetId = state.modelFeatureSource === "custom"
    ? state.modelCustomFeatureSetId
    : state.modelSelectedFeatureSetId;
  return modelFeatureSetCatalogItems().find((item) => item.feature_set_id === featureSetId) || null;
}

function modelResearchFeatureSetId() {
  return state.modelFeatureSource === "custom"
    ? String(state.modelCustomFeatureSetId || "")
    : String(state.modelSelectedFeatureSetId || "");
}

function syncModelFeatureSourceUI() {
  const source = state.modelFeatureSource === "custom" ? "custom" : "catalog";
  document.querySelectorAll("[data-model-feature-source]").forEach((button) => {
    const active = button.dataset.modelFeatureSource === source;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  document.querySelectorAll("[data-model-feature-source-pane]").forEach((pane) => {
    pane.hidden = pane.dataset.modelFeatureSourcePane !== source;
  });
  const form = document.getElementById("model-command-form");
  const hidden = formField(form, "feature_set_id");
  if (hidden) hidden.value = modelResearchFeatureSetId();
}

function modelAuditFeatureRecommendations() {
  const audit = serviceOutputs(state.factorAudit);
  return Array.isArray(audit.feature_set_recommendations) ? audit.feature_set_recommendations : [];
}

function modelFactorLibraryItems() {
  const outputs = serviceOutputs(state.factorLibraryRaw);
  return Array.isArray(outputs.items) ? outputs.items : [];
}

function filteredModelFactorItems() {
  const query = state.modelFactorQuery.trim().toLowerCase();
  return modelFactorLibraryItems().filter((item) => {
    const category = text(item.category, "Other");
    if (state.modelFactorCategory !== "all" && category !== state.modelFactorCategory) return false;
    if (!query) return true;
    return [item.factor_id, item.name, item.expression, category]
      .map((value) => text(value, "").toLowerCase())
      .join(" ")
      .includes(query);
  });
}

function renderModelFactorPicker() {
  const picker = document.getElementById("model-factor-picker");
  if (!picker) return;
  const library = modelFactorLibraryItems();
  const categories = [...new Set(library.map((item) => text(item.category, "Other")))].sort();
  const categorySelect = document.getElementById("model-factor-category");
  if (categorySelect) {
    categorySelect.innerHTML = `<option value="all">全部分类 (${library.length})</option>${categories.map((category) => `<option value="${escapeHtml(category)}">${escapeHtml(category)} (${library.filter((item) => text(item.category, "Other") === category).length})</option>`).join("")}`;
    categorySelect.value = categories.includes(state.modelFactorCategory) ? state.modelFactorCategory : "all";
  }
  const search = document.getElementById("model-factor-search");
  if (search && search.value !== state.modelFactorQuery) search.value = state.modelFactorQuery;
  const visible = filteredModelFactorItems();
  picker.innerHTML = visible.length ? visible.map((item) => {
    const metadata = parseMetadata(item);
    const deepScore = libraryMetric(item, "deep_score");
    const selected = state.modelSelectedFactorIds.has(String(item.factor_id));
    return `
      <label class="model-factor-option ${selected ? "is-selected" : ""}">
        <input type="checkbox" data-model-factor-id="${escapeHtml(text(item.factor_id, ""))}" ${selected ? "checked" : ""} />
        <span class="model-factor-option-main"><strong>${escapeHtml(text(item.name, item.factor_id))}</strong><small>${escapeHtml(text(item.factor_id, ""))} · ${escapeHtml(text(item.category, "Other"))}</small></span>
        <span class="model-factor-option-metrics"><b>ICIR ${escapeHtml(shortNumber(item.icir, 2))}</b><small>Deep ${escapeHtml(shortNumber(deepScore ?? metadata.metrics?.deep_score, 1))}</small></span>
      </label>
    `;
  }).join("") : `<div class="empty-state">当前筛选没有匹配因子。</div>`;

  const count = document.getElementById("model-factor-selection-count");
  if (count) count.textContent = `已选 ${state.modelSelectedFactorIds.size} 个 · 当前显示 ${visible.length} 个`;
  const freeze = document.getElementById("freeze-model-feature-set");
  if (freeze) freeze.disabled = state.modelSelectedFactorIds.size < 1;

  const recommendationsNode = document.getElementById("model-factor-recommendations");
  const recommendations = modelAuditFeatureRecommendations();
  if (recommendationsNode) {
    recommendationsNode.innerHTML = recommendations.length ? `
      <span>审计组合</span>
      ${recommendations.map((item) => `<button class="tiny-button" type="button" data-model-audit-recommendation="${escapeHtml(text(item.name, ""))}">${escapeHtml(text(item.name, "组合"))} · ${escapeHtml(text(item.count, item.factor_ids?.length || 0))}</button>`).join("")}
    ` : `<span>暂无因子审计推荐组合</span>`;
  }
  const auditNote = document.getElementById("model-factor-audit-note");
  const auditSummary = serviceOutputs(state.factorAudit).summary || {};
  if (auditNote) auditNote.textContent = auditSummary.stale
    ? "审计摘要提示版本变化；自定义组合按当前 50 个 Active 因子校验，创建过程不会改写因子库。"
    : "基于当前因子审计目录创建，不会改写 Active 因子库。";
}

function renderModelFeatureSetChooser() {
  const select = document.getElementById("model-feature-set-select");
  if (!select) return;
  const items = modelFeatureSetCatalogItems();
  const activeSource = modelActiveFeatureSetSource();
  if (!state.modelSelectedFeatureSetId || !items.some((item) => item.feature_set_id === state.modelSelectedFeatureSetId)) {
    state.modelSelectedFeatureSetId = activeSource?.feature_set_id || items[0]?.feature_set_id || "";
  }
  select.innerHTML = items.length ? items.map((item) => `
    <option value="${escapeHtml(item.feature_set_id)}">${escapeHtml(displayModelIdentifier(item.feature_set_id))} · ${escapeHtml(text(item.factor_count, 0))} 因子 · ${escapeHtml(modelFeatureSetSourceLabel(item))}</option>
  `).join("") : `<option value="">暂无可训练 Feature Set</option>`;
  select.value = state.modelSelectedFeatureSetId;

  const latestByPattern = (pattern) => items.find((item) => pattern.test(text(item.feature_set_id, "")));
  const presets = [
    { label: "Active 全量", item: activeSource || latestByPattern(/active/i) },
    { label: "Family Top1", item: latestByPattern(/family[-_]?top1/i) },
    { label: "Family Top2", item: latestByPattern(/family[-_]?top2/i) },
    { label: "Family Top3", item: latestByPattern(/family[-_]?top3/i) },
  ].filter((entry) => entry.item);
  const presetsNode = document.getElementById("model-feature-set-presets");
  if (presetsNode) presetsNode.innerHTML = presets.map((entry) => `<button class="model-feature-preset ${entry.item.feature_set_id === state.modelSelectedFeatureSetId ? "is-active" : ""}" type="button" data-model-feature-set-id="${escapeHtml(entry.item.feature_set_id)}"><strong>${escapeHtml(entry.label)}</strong><span>${escapeHtml(text(entry.item.factor_count, 0))} 因子</span></button>`).join("");

  const selected = items.find((item) => item.feature_set_id === state.modelSelectedFeatureSetId) || null;
  const summary = document.getElementById("model-feature-set-summary");
  if (summary) summary.innerHTML = selected ? `
    <div class="model-feature-summary-primary">
      <span>当前研究组合</span>
      <strong>${escapeHtml(modelFeatureSetFriendlyName(selected))}</strong>
      <small title="${escapeHtml(text(selected.feature_set_id, ""))}">${escapeHtml(displayModelIdentifier(selected.feature_set_id))}</small>
    </div>
    <div class="model-feature-summary-stats">
      <span><b>${escapeHtml(text(selected.factor_count, 0))}</b><small>因子 / 特征</small></span>
      <span><b>${escapeHtml(modelFeatureSetSourceLabel(selected))}</b><small>组合来源</small></span>
      <span><b>${escapeHtml(compactDateTime(selected.generated_at))}</b><small>冻结时间</small></span>
    </div>
  ` : `<div class="model-feature-summary-empty">请选择一个可训练的 Feature Set。</div>`;
  renderModelFactorAuditBridge();
  syncModelFeatureSourceUI();
  renderModelFactorPicker();
}

function renderModelFactorAuditBridge() {
  const node = document.getElementById("model-feature-audit-status");
  if (!node) return;
  const audit = serviceOutputs(state.factorAudit);
  const summary = audit.summary || {};
  const run = serviceOutputs(state.factorAuditRunStatus);
  const running = ["queued", "running"].includes(text(run.status, ""));
  const stale = Boolean(summary.stale);
  const generatedAt = audit.generated_at || summary.generated_at || summary.last_audit_at;
  const tone = running ? "is-running" : stale ? "is-stale" : summary.status === "completed" ? "is-fresh" : "is-pending";
  const title = running
    ? `信息簇审计${run.status === "queued" ? "排队中" : "运行中"}`
    : stale
      ? "因子库审计需要刷新"
      : summary.status === "completed" ? "因子库审计已同步" : "尚无可用审计";
  const detail = running
    ? "完成后会自动更新审计组合与当前 Active 因子目录。"
    : stale
      ? `${text(summary.current_active_count, modelFactorLibraryItems().length)} 个 Active 因子 · 旧组合仅作历史参考`
      : `${text(summary.current_active_count || summary.active_count, modelFactorLibraryItems().length)} 个 Active 因子 · ${generatedAt ? `${compactDateTime(generatedAt)} 更新` : "等待审计时间"}`;
  node.className = `model-feature-audit-status ${tone}`;
  node.innerHTML = `<span class="status-dot"></span><div><small>Factor Library Audit</small><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span></div>`;
  const button = document.getElementById("model-refresh-factor-audit");
  if (button) {
    button.disabled = running;
    button.textContent = running ? "审计运行中…" : "刷新信息簇审计";
  }
}

function applyModelProtocolPreset(preset = "complete") {
  const form = document.getElementById("model-command-form");
  const configs = {
    complete: { rounds: 3, stage: "round_synthesis", execute: true, registry: true },
    screen: { rounds: 1, stage: "research_score", execute: true, registry: false },
    plan: { rounds: 0, stage: "experiment_plan", execute: false, registry: false },
  };
  const config = configs[preset] || configs.complete;
  formField(form, "model_orch_rounds").value = String(config.rounds);
  formField(form, "max_stage").value = config.stage;
  formField(form, "execute_qlib").checked = config.execute;
  formField(form, "write_registry").checked = config.registry;
  document.querySelectorAll("[data-model-protocol-preset]").forEach((button) => button.classList.toggle("is-active", button.dataset.modelProtocolPreset === preset));
  renderModelLaunchReview();
}

function renderModelLaunchReview() {
  const form = document.getElementById("model-command-form");
  if (!form) return;
  const data = new FormData(form);
  const featureSet = modelSelectedFeatureSet();
  const featureSetId = modelResearchFeatureSetId();
  const preflight = modelCommandPreflightOutputs();
  const preflightMatches = Boolean(featureSetId && featureSet && preflight.passed === true && preflight.feature_set_id === featureSetId);
  const overrides = modelCommandBaselineOverrides();
  const stages = {
    round_synthesis: "完整研究",
    research_score: "研究评分",
    train_backtest_seed42: "Seed42 训练回测",
    experiment_plan: "仅生成实验计划",
  };
  const stage = text(data.get("max_stage"), "");
  const rounds = text(data.get("model_orch_rounds"), 0);
  const planButton = document.querySelector("[data-model-protocol-preset].is-active strong");
  const planName = text(planButton?.textContent, "自定义研究方案");
  const featureName = featureSet
    ? modelFeatureSetFriendlyName(featureSet)
    : (state.modelFeatureSource === "custom" ? "自定义组合尚未冻结" : "尚未选择研究组合");
  const featureDetail = featureSet
    ? `${text(featureSet.factor_count, "--")} 因子 · ${displayModelIdentifier(featureSetId)}`
    : `${state.modelSelectedFactorIds.size} 个因子待冻结`;
  const runTraining = data.get("execute_qlib") === "on";
  const writeRegistry = data.get("write_registry") === "on";
  const node = document.getElementById("model-launch-review");
  if (node) node.innerHTML = `
    <div class="model-launch-review-hero">
      <div><span>研究对象</span><strong>${escapeHtml(featureName)}</strong><small>${escapeHtml(featureDetail)}</small></div>
      <span class="badge ${preflightMatches ? "success" : "warn"}">${preflightMatches ? "Ready" : "待处理"}</span>
    </div>
    <div class="model-launch-review-grid">
      <div><span>研究方案</span><strong>${escapeHtml(planName)}</strong><small>Round 0 + ${escapeHtml(rounds)} 轮</small></div>
      <div><span>执行终点</span><strong>${escapeHtml(stages[stage] || stage || "--")}</strong><small>${runTraining ? "真实 Qlib 训练" : "不执行训练"}</small></div>
      <div><span>研究证据</span><strong>${writeRegistry ? "通过 Gate 后入库" : "不写共享 Registry"}</strong><small>${runTraining ? "训练已开启" : "仅规划"}</small></div>
      <div><span>模型参数</span><strong>${Object.keys(overrides).length ? `${Object.keys(overrides).length} 项自定义` : "FXAlpha 默认"}</strong><small>LightGBM 有界校验</small></div>
    </div>
    <div class="model-launch-gate ${preflightMatches ? "is-ready" : "is-blocked"}"><span>启动预检</span><strong>${preflightMatches ? "配置完整，可以启动" : (featureSetId ? "等待刷新或处理阻断" : "请先完成研究对象")}</strong></div>
  `;

  const productionNode = document.getElementById("model-production-launch-review");
  const sourceRound = String(document.getElementById("model-production-source-round")?.value || "");
  if (productionNode) productionNode.innerHTML = `
    <div class="model-launch-review-grid">
      <div><span>来源轮次</span><strong>${escapeHtml(sourceRound || "尚未选择")}</strong><small>已通过研究确认</small></div>
      <div><span>验证协议</span><strong>4 folds</strong><small>Seed 42 → 17/83</small></div>
      <div><span>参数策略</span><strong>不可变继承</strong><small>不读取研究页临时参数</small></div>
    </div>
    <div class="model-launch-gate ${sourceRound ? "is-ready" : "is-blocked"}"><span>启动条件</span><strong>${sourceRound ? "来源已确认" : "必须选择来源轮次"}</strong></div>
  `;
}

function validateModelCommandBaselineParams() {
  const form = document.getElementById("model-command-form");
  if (form && !form.reportValidity()) return "请先修正超出允许范围的参数。";
  const params = { ...modelCommandParamDefaults("fxalpha"), ...modelCommandBaselineOverrides() };
  if (Number(params.num_leaves) > (2 ** Number(params.max_depth))) {
    return "num_leaves 不能超过 2^max_depth。";
  }
  if (Number(params.early_stopping_rounds) >= Number(params.n_estimators)) {
    return "early_stopping_rounds 必须小于 n_estimators。";
  }
  if (Number(params.bagging_fraction) < 1 && Number(params.bagging_freq) <= 0) {
    return "bagging_fraction 小于 1 时，bagging_freq 必须大于 0。";
  }
  return "";
}

function modelProductionSourceRounds() {
  const rounds = serviceOutputs(state.modelRuns).rounds || [];
  return rounds.filter((round) => {
    const confirmation = round.experiment?.research_metadata?.research_confirmation || {};
    return round.stage === "research_confirmation" && confirmation.status === "passed";
  });
}

function populateModelProductionSources() {
  const select = document.getElementById("model-production-source-round");
  if (!select) return;
  const selected = select.value;
  const options = modelProductionSourceRounds().map((round) => {
    const metadata = round.experiment?.research_metadata || {};
    const confirmation = metadata.research_confirmation || {};
    const score = confirmation.confirmed_research_score ?? metadata.confirmed_research_score;
    const label = `${text(round.round_group_id, "unknown")} · 确认分 ${text(score, "--")}`;
    return `<option value="${escapeHtml(text(round.round_group_id, ""))}">${escapeHtml(label)}</option>`;
  }).join("");
  select.innerHTML = `<option value="">请选择已通过 Seed17/83 确认的研究轮次</option>${options}`;
  if (selected && Array.from(select.options).some((option) => option.value === selected)) select.value = selected;
}

function setModelActionMessage(message, tone = "subtle") {
  const el = document.getElementById("model-action-result");
  if (!el) return;
  el.className = `command-message ${tone}`;
  el.textContent = message || "";
  el.hidden = !message;
}

function renderModelCommandConsole() {
  const strip = document.getElementById("model-command-status-strip");
  if (!strip) return;
  syncModelCommandModeUI();
  populateModelProductionSources();
  renderModelFeatureSetChooser();
  const mode = modelCommandMode();
  const model = serviceOutputs(state.modelStatus);
  const preflight = modelCommandPreflightOutputs();
  const orchestrator = modelCommandOrchestratorOutputs();
  const contract = model.contract || {};
  const labelContract = preflight.label0_contract || contract.label_contract || {};
  const seedPolicy = contract.research_seed_policy || {};
  const portfolio = contract.portfolio || contract.production_rolling?.portfolio || {};
  const activeValues = preflight.active_values_readiness || model.active_values_readiness || {};
  const orchestratorKnown = Object.keys(orchestrator).length > 0;
  const activeJob = orchestratorKnown ? (orchestrator.active_job || null) : (model.orchestrator?.active_job || null);
  const latestJob = orchestrator.latest_job || model.latest_job || model.orchestrator?.latest_job || {};
  const preflightKnown = Object.keys(preflight).length > 0;
  const sourceRoundId = String(document.getElementById("model-production-source-round")?.value || "");
  const selectedFeatureSetId = modelResearchFeatureSetId();
  const preflightMatchesSelection = !selectedFeatureSetId || preflight.feature_set_id === selectedFeatureSetId;
  const selectionReady = mode === "production"
    ? Boolean(sourceRoundId)
    : Boolean(selectedFeatureSetId && modelSelectedFeatureSet());
  const preflightPassed = preflight.passed === true && preflightMatchesSelection && selectionReady;
  const activeValuesReady = preflight.safe_to_freeze_feature_set === true || activeValues.safe_to_freeze_feature_set === true;
  const statusTone = activeJob ? "is-running" : mode === "production"
    ? (sourceRoundId ? "is-ready" : "is-blocked")
    : (preflightPassed ? "is-ready" : preflightKnown ? "is-blocked" : "is-pending");
  const statusTitle = activeJob ? "研究任务运行中" : mode === "production"
    ? (sourceRoundId ? "Production Rolling 可启动" : "请选择来源研究轮次")
    : (preflightPassed ? "研究配置已就绪" : preflightKnown ? "研究配置存在阻断" : "等待研究预检");
  const featureSet = modelSelectedFeatureSet();
  strip.className = `model-command-readiness ${statusTone}`;
  strip.innerHTML = `
    <div class="model-readiness-title"><span class="status-dot"></span><div><small>Launch Readiness</small><strong>${escapeHtml(statusTitle)}</strong></div></div>
    <div class="model-readiness-facts">
      <span><small>${mode === "production" ? "来源" : "研究对象"}</small><b>${escapeHtml(mode === "production" ? (sourceRoundId || "未选择") : (featureSet ? `${text(featureSet.factor_count, "--")} 因子` : state.modelFeatureSource === "custom" ? `${state.modelSelectedFactorIds.size} 个待冻结` : "未选择"))}</b></span>
      <span><small>${mode === "production" ? "验证" : "特征数据"}</small><b>${escapeHtml(mode === "production" ? "4 folds" : (activeValuesReady ? "Ready" : "需处理"))}</b></span>
      <span><small>任务队列</small><b>${escapeHtml(activeJob ? text(activeJob.status, "运行中") : "空闲")}</b></span>
    </div>
  `;

  const controlNote = document.getElementById("model-command-control-note");
  if (controlNote) {
    if (activeJob) {
      controlNote.textContent = `模型研究正在运行：${text(activeJob.job_id, "unknown job")}；停止会在当前安全轮次或 Seed 边界生效。`;
      controlNote.dataset.state = "running";
    } else if (mode === "production" && !sourceRoundId) {
      controlNote.textContent = "生产模式必须选择已通过 Seed17/83 研究确认的来源轮次；Rolling 会固定继承该轮参数。";
      controlNote.dataset.state = "blocked";
    } else if (mode === "research" && !preflightPassed && preflightKnown) {
      controlNote.textContent = preflight.blocker?.human_message || preflight.stale_reason || (preflight.errors || []).join("；") || "模型研究预检未通过。";
      controlNote.dataset.state = "blocked";
    } else {
      controlNote.textContent = "当前没有运行中的模型研究；请核对参数与预检状态后启动。";
      controlNote.dataset.state = "idle";
    }
  }

  const contractNode = document.getElementById("model-command-contract");
  if (contractNode) {
    const screeningSeed = seedPolicy.screening_seed || 42;
    const confirmationSeeds = seedPolicy.confirmation_seeds || [17, 83];
    contractNode.innerHTML = `
      <div><span>训练引擎</span><strong>${escapeHtml(text(contract.model_class, "FXAlphaWeightedLGBModel"))}</strong></div>
      <div><span>基准轮</span><strong>Round 0 自动执行</strong></div>
      <div><span>Seed 协议</span><strong>${escapeHtml(`${screeningSeed} → ${confirmationSeeds.join("/")}`)}</strong></div>
      <div><span>标签合同</span><strong>${escapeHtml(`${text(labelContract.label_name, "LABEL0")} · ${text(labelContract.label_forward_period, 5)}日`)}</strong></div>
      <div><span>组合协议</span><strong>${escapeHtml(`Top${text(portfolio.topk, 20)} / Drop${text(portfolio.n_drop, 2)} / Hold${text(portfolio.hold_thresh, 5)}`)}</strong></div>
    `;
  }

  const productionContractNode = document.getElementById("model-production-contract");
  if (productionContractNode) {
    const rolling = contract.production_rolling || {};
    productionContractNode.innerHTML = `
      <div><span>Rolling Profile</span><strong>${escapeHtml(text(rolling.profile, "four_fold_expanding_6m_v1"))}</strong></div>
      <div><span>折数</span><strong>${escapeHtml(text(rolling.fold_count, 4))} folds</strong></div>
      <div><span>窗口</span><strong>Valid ${escapeHtml(text(rolling.valid_months, 6))}m / Test ${escapeHtml(text(rolling.test_months, 6))}m</strong></div>
      <div><span>Seed 协议</span><strong>42 → 17/83</strong></div>
      <div><span>Candidate 门槛</span><strong>${escapeHtml(text(rolling.candidate_score_threshold, 70))}</strong></div>
    `;
  }

  const startButton = document.getElementById("start-model-orch");
  const stopButton = document.getElementById("stop-model-orch");
  const resumeButton = document.getElementById("resume-model-orch");
  if (startButton) {
    const ready = mode === "production" ? Boolean(sourceRoundId) : preflightPassed;
    startButton.disabled = Boolean(activeJob) || !ready;
    startButton.title = activeJob ? "已有模型任务正在运行" : ready ? (mode === "production" ? "启动正式 Production Rolling" : "启动新的模型研究") : (mode === "production" ? "请先选择已通过确认的来源轮次" : "请先处理预检阻断");
  }
  const productionStartButton = document.getElementById("start-model-production-orch");
  if (productionStartButton) {
    productionStartButton.disabled = Boolean(activeJob) || !sourceRoundId;
    productionStartButton.title = activeJob ? "已有模型任务正在运行" : sourceRoundId ? "启动正式 Production Rolling" : "请先选择已通过确认的来源轮次";
  }
  if (stopButton) stopButton.disabled = !activeJob;
  if (resumeButton) resumeButton.disabled = !["interrupted", "failed"].includes(text(latestJob.status, ""));
  renderModelLaunchReview();
}

async function refreshModelCommandPreflight() {
  const form = document.getElementById("model-command-form");
  const featureSetId = form ? modelResearchFeatureSetId() : "";
  const query = featureSetId ? `?feature_set_id=${encodeURIComponent(featureSetId)}` : "";
  const [preflight, orchestratorStatus, runs] = await Promise.all([
    getJsonSafe(`${MODEL_API_PREFIX}/preflight${query}`),
    getJsonSafe(`${MODEL_API_PREFIX}/orchestrator/status`),
    getJsonSafe(`${MODEL_API_PREFIX}/runs?limit=100`),
  ]);
  state.modelPreflight = preflight;
  state.modelOrchestratorStatus = orchestratorStatus;
  if (!runs?._failed) state.modelRuns = runs;
  renderModelCommandConsole();
  return modelCommandPreflightOutputs();
}
const STANDARD_FACTOR_CATEGORIES = [
  "Price Volume",
  "Fundamental",
  "Analyst",
  "Sentiment",
  "Options",
  "Model",
  "Insider Transactions",
  "Short Interest",
  "Ownership",
  "Composite",
  "Other",
];

function setNavOpen(open) {
  rail?.classList.toggle("nav-open", open);
  navToggle?.setAttribute("aria-expanded", open ? "true" : "false");
  navToggle?.setAttribute("aria-label", open ? "关闭平台模块菜单" : "打开平台模块菜单");
  if (!open) {
    navToggle?.blur();
  }
}

document.addEventListener("click", (event) => {
  const toggle = event.target.closest?.("#nav-toggle");
  if (toggle) {
    event.preventDefault();
    setNavOpen(!rail?.classList.contains("nav-open"));
    return;
  }
  if (!rail?.classList.contains("nav-open")) return;
  if (rail.contains(event.target)) return;
  setNavOpen(false);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    setNavOpen(false);
  }
});

window.addEventListener("resize", () => {
  if (window.innerWidth > 1120) {
    setNavOpen(false);
  }
  queueFloatingXScrollbarRefresh();
  queueOverviewModuleMasonry();
});

function setPanel(name, workspaceName = null) {
  state.activePanel = name;
  try {
    window.localStorage?.setItem("fxalpha.activePanel", name);
  } catch (error) {
    // Ignore storage failures in restricted browser contexts.
  }
  panels.forEach((panel) => panel.classList.toggle("active", panel.id === `panel-${name}`));
  panelButtons.forEach((button) => {
    const samePanel = button.dataset.panel === name;
    const sameWorkspace = workspaceName
      ? button.dataset.workspace === workspaceName
      : !button.dataset.workspace;
    button.classList.toggle("active", samePanel && sameWorkspace);
  });
}

function setWorkspace(name) {
  state.activeWorkspace = name;
  try {
    window.localStorage?.setItem("fxalpha.activeWorkspace", name);
  } catch (error) {
    // Ignore storage failures in restricted browser contexts.
  }
  workspaceSections.forEach((section) => {
    section.classList.toggle("active", section.id === `workspace-${name}`);
  });
  renderResearchWorkspaceNav();
  if (name === "orch-trace" && researchPanelIsVisible()) {
    window.setTimeout(() => {
      refreshResearchLive({ force: true, includeTracePayload: true }).catch((error) => {
        console.error("GUI orch trace refresh failed", error);
      });
    }, 0);
  }
  if (name === "knowledge" && researchPanelIsVisible()) {
    window.setTimeout(() => loadFactorMap().catch((error) => {
      console.error("GUI factor map load failed", error);
    }), 0);
  }
}

function researchWorkspaceNavItems() {
  return [
    { kind: "workspace", workspace: "command", label: "研究指令台", detail: "标准化启动 Orchestrator" },
    { kind: "workspace", workspace: "run", label: "研究现场", detail: "进度、等级分布、LLM 研究记录" },
    { kind: "workspace", workspace: "candidates", label: "候选因子榜", detail: "候选评分、质量门与详情" },
    { kind: "workspace", workspace: "knowledge", label: "因子地图", detail: "信息区域、关系与研究轨迹" },
    { kind: "workspace", workspace: "orch-trace", label: "Orch Trace", detail: "Orchestrator LLM trace" },
  ];
}

function renderResearchWorkspaceNav() {
  if (!researchWorkspaceNav) return;
  const items = researchWorkspaceNavItems();
  researchWorkspaceNav.innerHTML = items.map((item) => {
    const isActive = item.kind === "jump"
      ? state.activeWorkspace === "run" && state.activeResearchNavTarget === item.target
      : state.activeWorkspace === item.workspace;
    const attrs = item.kind === "jump"
      ? `data-jump-target="${item.target}"`
      : `data-workspace="${item.workspace}"`;
    const extraClass = item.kind === "jump"
      ? " is-section-jump"
      : ` is-workspace-link${item.workspace === "knowledge" ? " is-workspace-group-start" : ""}`;
    return `
      <button class="workspace-tab${extraClass}${isActive ? " active" : ""}" type="button" ${attrs} title="${escapeHtml(item.detail)}">
        <span>${escapeHtml(item.label)}</span>
      </button>
    `;
  }).join("");
}

function jumpToResearchSection(targetId) {
  if (!targetId) return;
  state.activeResearchNavTarget = targetId;
  const jump = () => {
    const target = document.getElementById(targetId);
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  if (state.activeWorkspace !== "run") {
    setWorkspace("run");
    window.requestAnimationFrame(() => window.requestAnimationFrame(jump));
    return;
  }
  renderResearchWorkspaceNav();
  jump();
}

function restoreInitialNavigation() {
  let savedPanel = "";
  let savedWorkspace = "";
  let requestedWorkspace = "";
  let requestedPanel = "";
  let requestedLibraryTab = "";
  let hasRequestedWorkspace = false;
  try {
    const params = new URLSearchParams(window.location.search || "");
    const hashWorkspace = String(window.location.hash || "").replace(/^#workspace-?/, "");
    requestedPanel = params.get("panel") || "";
    requestedLibraryTab = params.get("library_tab") || "";
    requestedWorkspace = params.get("workspace") || hashWorkspace;
    hasRequestedWorkspace = params.has("workspace") || Boolean(hashWorkspace);
  } catch (error) {
    requestedWorkspace = "";
    requestedPanel = "";
    requestedLibraryTab = "";
    hasRequestedWorkspace = false;
  }
  if (requestedPanel && document.getElementById(`panel-${requestedPanel}`)) {
    const panelButton = document.querySelector(`.nav-item[data-panel="${requestedPanel}"]`);
    setPanel(requestedPanel, panelButton?.dataset.workspace || null);
    if (requestedPanel === "research" && requestedWorkspace && document.getElementById(`workspace-${requestedWorkspace}`)) {
      setWorkspace(requestedWorkspace);
    }
    if (requestedPanel === "model-research") {
      const requestedModelWorkspace = requestedWorkspace && document.getElementById(`model-workspace-${requestedWorkspace}`)
        ? requestedWorkspace
        : state.activeModelWorkspace;
      setModelWorkspace(requestedModelWorkspace || "live");
    }
    if (requestedPanel === "library" && requestedLibraryTab) {
      window.requestAnimationFrame(() => setLibraryTab(requestedLibraryTab));
    }
    return;
  }
  if (hasRequestedWorkspace && requestedWorkspace && document.getElementById(`workspace-${requestedWorkspace}`)) {
    setPanel("research", "run");
    setWorkspace(requestedWorkspace);
    return;
  }
  try {
    savedPanel = window.localStorage?.getItem("fxalpha.activePanel") || "";
    savedWorkspace = window.localStorage?.getItem("fxalpha.activeWorkspace") || "";
  } catch (error) {
    savedPanel = "";
    savedWorkspace = "";
  }
  if (savedPanel && document.getElementById(`panel-${savedPanel}`)) {
    const panelButton = document.querySelector(`.nav-item[data-panel="${savedPanel}"]`);
    setPanel(savedPanel, panelButton?.dataset.workspace || null);
    if (savedPanel === "model-research") {
      const savedModelWorkspace = state.activeModelWorkspace && document.getElementById(`model-workspace-${state.activeModelWorkspace}`)
        ? state.activeModelWorkspace
        : "live";
      setModelWorkspace(savedModelWorkspace);
    }
  }
  if ((savedPanel || state.activePanel) === "research") {
    state.activeResearchNavTarget = "research-progress-board";
    setWorkspace("run");
    return;
  }
  if (savedWorkspace && document.getElementById(`workspace-${savedWorkspace}`)) {
    setWorkspace(savedWorkspace);
  } else if (!document.querySelector(".workspace-section.active")) {
    setWorkspace("run");
  }
}

panelButtons.forEach((button) => {
  button.addEventListener("click", () => {
    setPanel(button.dataset.panel, button.dataset.workspace || null);
    if (button.dataset.workspace) {
      setWorkspace(button.dataset.workspace);
    }
    setNavOpen(false);
    if (window.innerWidth <= 1120) {
      document.querySelector(".main")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    if (button.dataset.panel === "research") {
      setTimeout(() => refreshResearchLive({ force: true }), 0);
    }
    setTimeout(() => refreshState({ reason: "panel_switch" }), 0);
  });
});

researchWorkspaceNav?.addEventListener("click", (event) => {
  const button = event.target.closest(".workspace-tab");
  if (!button) return;
  const target = button.dataset.jumpTarget;
  if (target) {
    jumpToResearchSection(target);
    return;
  }
  if (button.dataset.workspace) {
    setWorkspace(button.dataset.workspace);
    if (["knowledge", "orch-trace"].includes(button.dataset.workspace)) {
      document.getElementById(`workspace-${button.dataset.workspace}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }
});

document.addEventListener("click", (event) => {
  const modelAssetLaneButton = event.target.closest?.("[data-model-asset-lane]");
  if (modelAssetLaneButton) {
    const lane = modelAssetLaneButton.dataset.modelAssetLane === "rolling" ? "rolling" : "research";
    state.activeModelAssetLane = lane;
    try {
      window.localStorage?.setItem("fxalpha.activeModelAssetLane", lane);
    } catch (error) {
      // Ignore storage failures in restricted browser contexts.
    }
    renderModelResearch();
    return;
  }
  if (event.target.closest("[data-factor-map-refresh]")) {
    loadFactorMap().catch((error) => console.error("GUI factor map refresh failed", error));
    return;
  }
  const factorMapRegion = event.target.closest("[data-factor-map-region]");
  if (factorMapRegion) {
    const regionUid = factorMapRegion.dataset.factorMapRegion;
    const detail = document.querySelector(`.factor-map-region-card[data-region-uid="${regionUid}"]`);
    if (!detail) return;
    detail.open = true;
    detail.scrollIntoView({ behavior: "smooth", block: "center" });
  }
});

function setModelWorkspace(name) {
  const nextName = name || "live";
  state.activeModelWorkspace = nextName;
  try {
    window.localStorage?.setItem("fxalpha.activeModelWorkspace", nextName);
  } catch (error) {
    // Ignore storage failures in restricted browser contexts.
  }
  modelWorkspaceSections.forEach((section) => {
    section.classList.toggle("active", section.id === `model-workspace-${nextName}`);
  });
  modelWorkspaceTabs.forEach((button) => {
    button.classList.toggle("active", button.dataset.modelWorkspace === nextName);
  });
  if (nextName === "backtest" && modelResearchPanelIsVisible()) {
    window.setTimeout(() => {
      loadSelectedModelBacktest({ force: false }).catch((error) => {
        console.error("GUI model backtest load failed", error);
      });
    }, 0);
  }
}

modelWorkspaceTabs.forEach((button) => {
  button.addEventListener("click", () => setModelWorkspace(button.dataset.modelWorkspace));
});

function setLibraryTab(name) {
  state.activeLibraryTab = name === "relations" ? "feature-sets" : (name || "registry");
  document.querySelectorAll(".library-tab-panel").forEach((section) => {
    section.classList.toggle("active", section.id === `library-tab-${state.activeLibraryTab}`);
  });
  document.querySelectorAll(".library-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.libraryTab === state.activeLibraryTab);
  });
}

document.querySelectorAll(".library-tab").forEach((button) => {
  button.addEventListener("click", () => setLibraryTab(button.dataset.libraryTab));
});

const modelFeatureSetCatalogDetails = document.querySelector(".model-library-secondary-catalog");
modelFeatureSetCatalogDetails?.addEventListener("toggle", async () => {
  if (!modelFeatureSetCatalogDetails.open) return;
  const container = document.getElementById("model-library-feature-sets");
  if (!container) return;
  if (!serviceOutputs(state.modelFeatureSets).items?.length) {
    container.innerHTML = `<div class="empty-state">正在读取 Feature Set 目录...</div>`;
    state.modelFeatureSets = await getJsonSafe(`${MODEL_API_PREFIX}/feature-sets`, { timeoutMs: 30000 });
  }
  container.innerHTML = renderModelFeatureSetCatalogPanel(serviceOutputs(state.modelFeatureSets));
});

function modelBacktestActiveRunId(modelLike = state.modelStatus) {
  const model = serviceOutputs(modelLike);
  const researchCurrent = serviceOutputs(state.modelResearchCurrent);
  const researchCurrentState = researchCurrent.state || model.research_current?.state || (model.gui_projection || {}).research_current?.state || {};
  const projection = model.gui_projection || {};
  const candidateRounds = projection.candidate_rounds || {};
  const projectedActiveRound = candidateRounds.current_candidate_round || projection.active_round_view || {};
  const liveSession = model.live_session || {};
  const activeRound = model.active_round || {};
  const recentRounds = Array.isArray(model.recent_rounds) ? model.recent_rounds : Array.isArray(model.rounds) ? model.rounds : [];
  const recentSeedRuns = Array.isArray(model.recent_seed_runs) ? model.recent_seed_runs : [];
  const recentRound = recentRounds.find((round) => text(round.model_run_id, ""));
  const recentSeedRun = recentSeedRuns.find((row) => text(row.model_run_id, ""));
  return text(
    researchCurrentState.model_run_id
      || liveSession.latest_model_run_id
      || projectedActiveRound.model_run_id
      || activeRound.model_run_id
      || recentRound?.model_run_id
      || recentSeedRun?.model_run_id,
    ""
  );
}

function modelBacktestSelectionUsesActiveRun() {
  return !state.modelBacktestSelection.modelRunId
    && !state.modelBacktestSelection.modelId
    && text(state.modelBacktestSelection.selector, "latest") === "active";
}

function modelBacktestUrl(options = {}) {
  const rollingSelection = state.modelBacktestSelection.role === "rolling_campaign"
    || state.modelBacktestSelection.selector === "rolling";
  const includeDaily = options.includeDaily !== false
    && (!rollingSelection || state.modelBacktestSelection.rollingDaily === true);
  const params = new URLSearchParams({
    max_points: "260",
    include_daily: includeDaily ? "true" : "false",
    max_daily_holdings: includeDaily ? "30" : "0",
  });
  const forcedModelRunId = text(options.modelRunId, "");
  const activeModelRunId = modelBacktestSelectionUsesActiveRun() ? modelBacktestActiveRunId() : "";
  const selectedModelRunId = forcedModelRunId || state.modelBacktestSelection.modelRunId || activeModelRunId;
  if (selectedModelRunId && state.modelBacktestSelection.role === "rolling_campaign") {
    params.set("rolling_campaign_id", selectedModelRunId);
    params.set("selector", "rolling");
  } else if (selectedModelRunId) {
    params.set("model_run_id", selectedModelRunId);
  } else if (state.modelBacktestSelection.modelId) {
    params.set("model_id", state.modelBacktestSelection.modelId);
  } else {
    const selector = state.modelBacktestSelection.selector || "latest";
    if (selector === "active" || selector === "model") {
      const fallbackActiveRunId = modelBacktestActiveRunId();
      if (fallbackActiveRunId) {
        params.set("model_run_id", fallbackActiveRunId);
      } else {
        params.set("selector", "latest");
      }
    } else {
      params.set("selector", selector);
    }
  }
  return `${MODEL_API_PREFIX}/backtest?${params.toString()}`;
}

function modelBacktestWorkspaceIsVisible() {
  return modelResearchPanelIsVisible() && state.activeModelWorkspace === "backtest";
}

async function loadSelectedModelBacktest({ force = true, render = true, includeDaily = true } = {}) {
  const url = modelBacktestUrl({ includeDaily });
  if (!force && state.modelBacktestLastUrl === url && state.modelBacktest && !state.modelBacktest._failed && !state.modelBacktest.error) {
    return state.modelBacktest;
  }
  if (state.modelBacktestRequest?.url === url) {
    return await state.modelBacktestRequest.promise;
  }
  state.modelBacktestLoading = true;
  if (render && modelBacktestWorkspaceIsVisible()) {
    renderModelResearch();
  }
  const request = { url, promise: null };
  const promise = (async () => {
    let result = await getJsonSafe(url, { timeoutMs: includeDaily ? 90000 : 12000 });
    if (includeDaily && (result?._failed || result?.error) && isAbortLikeErrorMessage(result?.error || result?.err)) {
      const fallbackUrl = modelBacktestUrl({ includeDaily: false });
      const fallback = await getJsonSafe(fallbackUrl, { timeoutMs: 15000 });
      if (fallback && !fallback._failed && !fallback.error) {
        result = annotateBacktestDailyFallback(fallback, result.error || result.err || "");
      }
    }
    return result;
  })()
    .then((result) => {
      if (state.modelBacktestRequest !== request) {
        return state.modelBacktest;
      }
      state.modelBacktest = keepPreviousOnReadFailure(result, state.modelBacktest);
      if (result && !result._failed && !result.error) {
        state.modelBacktestLastUrl = url;
        state.modelBacktestLastLoadedAt = Date.now();
      }
      return state.modelBacktest;
    })
    .finally(() => {
      if (state.modelBacktestRequest === request) {
        state.modelBacktestRequest = null;
        state.modelBacktestLoading = false;
      }
    });
  request.promise = promise;
  state.modelBacktestRequest = request;
  const result = await promise;
  if (render) {
    renderModelResearch();
    renderModelLibrary();
  }
  return result;
}

function closeBacktestModelMenus(exceptMenu = null) {
  document.querySelectorAll(".backtest-model-menu.is-open").forEach((menu) => {
    if (menu === exceptMenu) return;
    menu.classList.remove("is-open");
    menu.querySelector(".backtest-model-menu-trigger")?.setAttribute("aria-expanded", "false");
  });
}

function modelBacktestDisplayBase(item = {}) {
  const canonical = canonicalModelDisplayName(item, { kind: text(item.role, "") === "rolling_campaign" ? "rolling" : "model" });
  if (canonical) return canonical;
  const raw = text(item.display_model_id || item.model_id || "", "");
  const match = raw.match(/^(m_\d{8}_\d{6})/);
  if (match) return match[1];
  const display = text(item.display_name || "", "");
  const displayMatch = display.match(/(m_\d{8}_\d{6})/);
  if (displayMatch) return displayMatch[1];
  const created = text(item.created_at || item.train_end || item.finished_at || "", "");
  const createdMatch = created.match(/(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/);
  if (createdMatch) return `m_${createdMatch[1]}${createdMatch[2]}${createdMatch[3]}_${createdMatch[4]}${createdMatch[5]}${createdMatch[6]}`;
  return text(item.short_model_run_id ? `m_${item.short_model_run_id}` : compactModelRunId(item.model_run_id), "m_unknown");
}

function modelBacktestDisplaySeed(item = {}) {
  const explicit = item.seed ?? item.resolved_training_params?.seed ?? item.metadata?.seed;
  const raw = text(explicit, "42");
  const number = Number(raw);
  if (Number.isFinite(number)) {
    return String(Math.abs(Math.trunc(number)) % 100).padStart(2, "0");
  }
  return raw.slice(-2).padStart(2, "0");
}

function modelBacktestRoundLabel(item = {}, roundNoByRun = {}) {
  const runId = text(item.model_run_id || item.modelRunId || "", "");
  const fromMap = roundNoByRun instanceof Map ? roundNoByRun.get(runId) : roundNoByRun?.[runId];
  const value = item.round_no ?? item.candidate_round_no ?? item.flow_round_no ?? fromMap;
  return value !== undefined && value !== null && value !== "" ? `R${text(value)}` : "";
}

function modelBacktestFeatureChip(item = {}) {
  const raw = displayModelIdentifier(item.feature_set_label || item.feature_set_id, "").replace(/^fs-/, "");
  if (!raw) return "";
  const trimmed = raw.replace(/-\d{8}(?:-\d{4,6})?$/i, "");
  return trimmed.toUpperCase();
}

function modelBacktestStatusChip(item = {}, role = "") {
  const status = text(item.status, "").toLowerCase();
  if (status === "candidate") return "入库候选";
  if (status === "production") return "生产";
  if (status === "archived") return "已归档";
  return text(item.status, "");
}

function modelBacktestOptionFromRow(item = {}, roundNoByRun = {}, role = "") {
  const runId = text(item.model_run_id, "");
  const fullSeed = text(item.seed ?? item.resolved_training_params?.seed ?? item.metadata?.seed, "42");
  const roundLabel = modelBacktestRoundLabel(item, roundNoByRun);
  const baseName = modelBacktestDisplayBase(item);
  const label = roundLabel && !baseName.endsWith(` · ${roundLabel}`)
    ? `${baseName} · ${roundLabel}`
    : baseName;
  const metrics = [
    `年化 ${pct(item.excess_annualized_ret_with_cost ?? item.annualized_ret, 1)}`,
    `IR ${shortNumber(item.excess_information_ratio_with_cost ?? item.sharpe, 3)}`,
    `DD ${pct(item.max_drawdown, 1)}`,
  ].join(" · ");
  const chips = [
    roundLabel,
    modelBacktestFeatureChip(item),
    modelBacktestStatusChip(item, role),
  ].filter(Boolean);
  const rawStatus = text(item.status, "");
  const statusNote = modelBacktestStatusChip(item, role) || rawStatus;
  return {
    modelId: text(item.model_id, ""),
    modelRunId: runId,
    label,
    seed: "42",
    roundLabel,
    chips,
    metrics,
    note: "",
    sortTs: modelBacktestSortTimestamp(item),
    sortScore: Number(item.research_score ?? item.metadata?.research_score ?? item.confirmed_research_score ?? item.metadata?.confirmed_research_score),
    sortAnnualized: Number(item.excess_annualized_ret_with_cost ?? item.annualized_ret),
    sortIr: Number(item.excess_information_ratio_with_cost ?? item.sharpe),
    completedAt: text(item.finished_at || item.created_at || item.started_at || item.updated_at || item.train_end || "", ""),
    title: `${label} / ${statusNote}${rawStatus && rawStatus !== statusNote ? ` (${rawStatus})` : ""} / 正式 Seed42${fullSeed !== "42" ? "（历史审计记录已折叠）" : ""} / ${runId}`,
    role: role || text(item.selector_role || item.role || item.status, "candidate"),
    status: text(item.status, ""),
  };
}

function modelBacktestTimestampFromId(value = "") {
  const raw = text(value, "");
  const match = raw.match(/(?:m_|-)(\d{8})[_-](\d{6})/);
  if (!match) return 0;
  const [, day, time] = match;
  const iso = `${day.slice(0, 4)}-${day.slice(4, 6)}-${day.slice(6, 8)}T${time.slice(0, 2)}:${time.slice(2, 4)}:${time.slice(4, 6)}Z`;
  return parseIso(iso)?.getTime() || 0;
}

function modelBacktestSortTimestamp(item = {}) {
  const direct = parseIso(item.finished_at || item.created_at || item.started_at || item.updated_at || item.train_end);
  if (direct) return direct.getTime();
  return Math.max(
    modelBacktestTimestampFromId(item.model_run_id),
    modelBacktestTimestampFromId(item.model_id),
    modelBacktestTimestampFromId(item.display_name),
  );
}

function modelBacktestDropdownOptions(models = [], runs = [], seedDiagnostics = [], roundNoByRun = {}) {
  const byRun = new Map();
  const visibleStatuses = new Set(["research", "candidate", "production"]);
  const upsertOption = (option) => {
    const runId = text(option.modelRunId || "", "");
    if (!runId) return;
    const existing = byRun.get(runId);
    if (!existing) {
      byRun.set(runId, option);
      return;
    }
    existing.sortTs = Math.max(Number(existing.sortTs || 0), Number(option.sortTs || 0));
    if (!existing.modelId && option.modelId) existing.modelId = option.modelId;
    if (!existing.completedAt && option.completedAt) existing.completedAt = option.completedAt;
  };
  // Seed17/83 are audit evidence, never top-level selectable models.
  (models || []).forEach((item) => {
    const runId = text(item.model_run_id, "");
    if (!visibleStatuses.has(text(item.status, "").toLowerCase())) return;
    if (!runId) return;
    upsertOption(modelBacktestOptionFromRow(item, roundNoByRun));
  });
  return [...byRun.values()].sort((a, b) =>
    (Number(b.sortTs || 0) - Number(a.sortTs || 0))
    || text(b.modelRunId, "").localeCompare(text(a.modelRunId, ""))
  ).slice(0, 30);
}

function modelRollingBacktestOptions(campaigns = []) {
  return (campaigns || []).map((campaign) => {
    const seed42 = (campaign.seeds || []).find((item) => Number(item.seed) === 42) || (campaign.seeds || [])[0] || {};
    const metrics = seed42.rolling_metrics || {};
    const score = campaign.final?.available ? campaign.final?.rolling_score : campaign.preliminary?.score;
    return {
      modelId: `rolling:${text(campaign.campaign_id, "")}`,
      modelRunId: text(campaign.campaign_id, ""),
      label: canonicalModelDisplayName(campaign, { kind: "rolling" }),
      role: "rolling_campaign",
      status: text(campaign.status, "research"),
      chips: [
        `${campaign.final?.available ? "准入分" : "初筛分"} ${shortNumber(score, 2)}`,
        "正式 Seed42",
        text(campaign.decision, campaign.status),
      ],
      metrics: [
        `IR ${shortNumber(metrics.excess_information_ratio_with_cost, 3)}`,
        `年化 ${pct(metrics.excess_annualized_ret_with_cost, 1)}`,
        `DD ${pct(metrics.max_drawdown, 1)}`,
      ].join(" · "),
      sortTs: parseIso(campaign.completed_at || campaign.started_at)?.getTime() || 0,
      sortScore: Number(score),
      sortAnnualized: Number(metrics.excess_annualized_ret_with_cost ?? metrics.annualized_ret),
      sortIr: Number(metrics.excess_information_ratio_with_cost ?? metrics.ir),
      completedAt: text(campaign.completed_at || campaign.started_at, ""),
      title: `${text(campaign.display_name, "ROLLING")} / ${text(campaign.campaign_id)} / ${text(campaign.decision, campaign.status)} / four-fold rolling`,
    };
  }).filter((item) => item.modelRunId);
}

function mergeRollingCampaignCatalog(...sources) {
  const byCampaignId = new Map();
  sources.forEach((source) => {
    (Array.isArray(source) ? source : []).forEach((campaign) => {
      const campaignId = text(campaign?.campaign_id, "");
      if (!campaignId) return;
      const existing = byCampaignId.get(campaignId) || {};
      byCampaignId.set(campaignId, { ...existing, ...campaign });
    });
  });
  return [...byCampaignId.values()].sort((left, right) => {
    const leftTime = parseIso(left.completed_at || left.started_at)?.getTime() || 0;
    const rightTime = parseIso(right.completed_at || right.started_at)?.getTime() || 0;
    return (rightTime - leftTime)
      || text(right.campaign_id, "").localeCompare(text(left.campaign_id, ""));
  });
}

function sortModelBacktestOptions(options = [], sortKey = "time", direction = "desc") {
  const keyMap = {
    time: "sortTs",
    score: "sortScore",
    annualized: "sortAnnualized",
    ir: "sortIr",
  };
  const key = keyMap[sortKey] || keyMap.time;
  const multiplier = direction === "asc" ? 1 : -1;
  return [...options].sort((a, b) => {
    const aValue = Number(a[key]);
    const bValue = Number(b[key]);
    const aFinite = Number.isFinite(aValue);
    const bFinite = Number.isFinite(bValue);
    if (aFinite !== bFinite) return aFinite ? -1 : 1;
    const primary = aFinite && bFinite ? (aValue - bValue) * multiplier : 0;
    const timestampTieBreak = (Number(a.sortTs || 0) - Number(b.sortTs || 0)) * multiplier;
    return primary
      || timestampTieBreak
      || text(a.modelRunId, "").localeCompare(text(b.modelRunId, "")) * multiplier;
  });
}

function compactModelRunId(runId) {
  const raw = text(runId, "");
  if (!raw) return "";
  if (raw.length <= 24) return raw;
  const parts = raw.split("-");
  return parts[parts.length - 1] || raw.slice(-24);
}

function displayModelIdentifier(value, fallback = "") {
  return text(value, fallback)
    .replace(/model0703/gi, "model")
    .replace(/^m0703_/i, "mrun_")
    .replace(/^mr0703_/i, "mround_")
    .replace(/^ms0703_/i, "msession_")
    .replace(/^roll0703_/i, "model_roll_");
}

const MODEL_DISPLAY_NAMING_VERSION = "model_display_v1";

function modelFeatureSetDisplayLabel(value) {
  return displayModelIdentifier(value, "UNSPECIFIED")
    .replace(/^fs-/i, "")
    .replace(/^model-/i, "")
    .replace(/[-_](?:19|20)\d{6}(?:[-_]\d{4,6})?$/i, "")
    .replace(/[-_]+/g, "-")
    .replace(/^-|-$/g, "")
    .toUpperCase() || "UNSPECIFIED";
}

function modelDisplayTimestamp(item = {}) {
  const identifiers = [item.model_run_id, item.campaign_id, item.round_group_id].map((value) => displayModelIdentifier(value, ""));
  for (const raw of identifiers) {
    const match = raw.match(/model_prod_.*_(\d{8})T(\d{6})/i)
      || raw.match(/mround_(\d{8})_(\d{6})/i)
      || raw.match(/model_roll_(\d{8})T(\d{6})/i)
      || raw.match(/(?:^|_)(\d{8})_(\d{6})(?:_|$)/);
    if (!match) continue;
    const day = match[1];
    const timeValue = match[2];
    return compactDateTime(`${day.slice(0, 4)}-${day.slice(4, 6)}-${day.slice(6, 8)}T${timeValue.slice(0, 2)}:${timeValue.slice(2, 4)}:${timeValue.slice(4, 6)}Z`);
  }
  return compactDateTime(item.completed_at || item.created_at || item.started_at || item.updated_at) || "时间未知";
}

function canonicalModelDisplayName(item = {}, { kind = "model", roundNo = null } = {}) {
  let base = text(item.display_naming_version, "") === MODEL_DISPLAY_NAMING_VERSION
    ? text(item.display_name, "")
    : "";
  if (!base) {
    const status = text(item.status, "research").toLowerCase();
    const role = kind === "rolling" || text(item.role, "") === "rolling_campaign"
      ? "ROLLING"
      : ({ research: "研究", candidate: "候选", production: "生产", archived: "归档" }[status] || "模型");
    base = `${role} · ${modelFeatureSetDisplayLabel(item.feature_set_id)} · ${modelDisplayTimestamp(item)}`;
  }
  const hasRound = / · R\d+$/i.test(base);
  return roundNo !== null && roundNo !== undefined && roundNo !== "" && !hasRound
    ? `${base} · R${text(roundNo)}`
    : base;
}

function compactResearchRunLabel(runId) {
  const raw = text(runId, "");
  if (!raw) return "--";
  const match = raw.match(/^fr_(\d{8})_(\d{6})_([a-z0-9]+)/i);
  if (match) return `${match[1].slice(4)}-${match[2].slice(0, 4)} · ${match[3].slice(0, 6)}`;
  return clip(raw, 22);
}

function modelBacktestMetricLine(item = {}) {
  return [
    `IR ${shortNumber(item.excess_information_ratio_with_cost ?? item.sharpe, 3)}`,
    `年化 ${pct(item.excess_annualized_ret_with_cost ?? item.annualized_ret, 1)}`,
    `DD ${pct(item.max_drawdown, 1)}`,
  ].join(" · ");
}

function renderModelFeatureSetCatalogPanel(featureSetCatalog = {}) {
  const featureSetCatalogItems = Array.isArray(featureSetCatalog.items)
    ? featureSetCatalog.items
    : (Array.isArray(featureSetCatalog.feature_sets) ? featureSetCatalog.feature_sets : []);
  const featureSetCatalogSummary = featureSetCatalog.summary || {
    total: featureSetCatalog.count || featureSetCatalogItems.length,
    trainable: featureSetCatalogItems.filter((item) => item.trainable !== false).length,
  };
  const readiness = featureSetCatalog.active_values_readiness || {};
  const readinessReady = readiness.safe_to_freeze_feature_set === true;
  const readinessText = readinessReady ? "active values ready" : "active values blocked";
  const readinessJob = readiness.active_values_job || {};
  const readinessJobLine = readinessJob.job_id
    ? `job ${text(readinessJob.status, "--")} · ${text(readinessJob.job_id, "--")}`
    : (readiness.resume_available ? `resume · ${text(readiness.resume_action, "")}` : "");
  const readinessDetail = readinessReady
    ? `默认从 parquet/已落库因子值组装；model 不计算因子值。`
    : `${text(readiness.feature_snapshot_block_reason || readiness.required_action, "active values not ready")} · 默认 source_mode=${text(readiness.refresh_source_mode_default, "parquet")}`;
  const sourceLabel = (source) => ({
    all_active: "全量 Active",
    audit_recommended: "审计推荐",
    diagnostic: "复核快照",
    manual_or_historical: "历史 / 手工",
  }[source] || text(source, "未知"));
  const featureSetCatalogRows = featureSetCatalogItems.slice(0, 12).map((item) => {
    const warnings = item.warnings || [];
    const rec = item.recommendation || {};
    return `
      <tr class="${item.is_active_pointer ? "active-row" : ""}">
        <td><strong>${escapeHtml(displayModelIdentifier(item.feature_set_id))}</strong><small>${escapeHtml(text(item.manifest_file_rel || item.updated_at, ""))}</small></td>
        <td><span class="badge subtle">${escapeHtml(sourceLabel(item.source_type))}</span><small>${escapeHtml(text(rec.name || item.factor_selection_mode, ""))}</small></td>
        <td>${escapeHtml(text(item.factor_count, "0"))}<small>${escapeHtml(text(item.feature_count, "0"))} features</small></td>
        <td>${escapeHtml(text(item.label_forward_period, "--"))}D<small>${escapeHtml(text(item.feature_missing_strategy, ""))}</small></td>
        <td><span class="badge ${item.trainable ? "success" : "danger"}">${escapeHtml(item.trainable ? "可训练" : "需复核")}</span><small>${escapeHtml(warnings.slice(0, 2).join("、") || (item.is_active_pointer ? "active pointer" : text(item.updated_at, "")))}</small></td>
      </tr>
    `;
  }).join("");
  return `
    <div class="section-head">
      <div>
        <p class="eyebrow">Feature Sets</p>
        <h3>Feature Set 目录 <span class="source-pill">manifest + factor_audit</span></h3>
      </div>
      <span class="section-hint">${escapeHtml(text(featureSetCatalogSummary.total, "0"))} 个快照 · ${escapeHtml(text(featureSetCatalogSummary.trainable, "0"))} 个可训练 · 模型库只读展示</span>
    </div>
    <div class="warning-strip ${readinessReady ? "success-strip" : ""}">
      <strong>${escapeHtml(readinessText)}</strong>
      <span>${escapeHtml(readinessDetail)}</span>
      ${readinessJobLine ? `<small>${escapeHtml(readinessJobLine)}</small>` : ""}
      <small>registry ${escapeHtml(text(readiness.registry_fingerprint, "--"))} · manifest ${escapeHtml(text(readiness.manifest_registry_fingerprint, "--"))}</small>
    </div>
    <div class="table-shell compact-table model-feature-set-table-shell">
      <table class="data-table">
        <thead><tr><th>Feature Set</th><th>来源</th><th>因子</th><th>Label / Missing</th><th>状态</th></tr></thead>
        <tbody>${featureSetCatalogRows || `<tr><td colspan="5">暂无 feature set catalog；冻结快照后会自动出现。</td></tr>`}</tbody>
      </table>
    </div>
  `;
}

function compactEvidenceRef(ref) {
  if (ref === undefined || ref === null || ref === "") return "";
  if (typeof ref === "string" || typeof ref === "number" || typeof ref === "boolean") return text(ref, "");
  if (Array.isArray(ref)) return ref.map(compactEvidenceRef).filter(Boolean).join(" / ");
  if (typeof ref === "object") {
    return text(
      ref.path
      || ref.file
      || ref.artifact
      || ref.ref
      || ref.id
      || ref.name
      || ref.event_type
      || ref.stage
      || "",
      ""
    ) || clip(JSON.stringify(ref), 120);
  }
  return text(ref, "");
}

document.addEventListener("click", async (event) => {
  const menuTrigger = event.target.closest?.(".backtest-model-menu-trigger");
  if (menuTrigger) {
    const menu = menuTrigger.closest(".backtest-model-menu");
    const nextOpen = !menu?.classList.contains("is-open");
    closeBacktestModelMenus(menu);
    menu?.classList.toggle("is-open", nextOpen);
    menuTrigger.setAttribute("aria-expanded", nextOpen ? "true" : "false");
    if (nextOpen) {
      const popover = menu?.querySelector(".backtest-menu-popover");
      if (popover) popover.scrollTop = 0;
    }
    return;
  }
  if (!event.target.closest?.(".backtest-model-menu")) {
    closeBacktestModelMenus();
  }

  const panelTarget = event.target.closest?.("[data-panel-target]");
  if (panelTarget) {
    setPanel(panelTarget.dataset.panelTarget);
    return;
  }

  const preset = event.target.closest?.("[data-backtest-selector]");
  if (preset) {
    if (preset.dataset.backtestSelector === "model") {
      const wasModelMode = state.modelBacktestSelection.selector === "model"
        && state.modelBacktestSelection.role !== "rolling_campaign";
      const wasOpen = Boolean(document.querySelector(".backtest-model-menu.is-open"));
      const shouldOpen = !(wasModelMode && wasOpen);
      state.modelBacktestSelection = {
        selector: "model",
        modelId: "",
        modelRunId: "",
        label: "选择模型或 Rolling",
        role: "",
        rollingDaily: false,
      };
      setPanel("model-research");
      setModelWorkspace("backtest");
      renderModelResearch();
      window.requestAnimationFrame(() => {
        const visiblePanel = document.querySelector(".backtest-select-panel:not(.is-hidden)");
        const menu = visiblePanel?.querySelector(".backtest-model-menu");
        const trigger = visiblePanel?.querySelector(".backtest-model-menu-trigger");
        menu?.classList.toggle("is-open", shouldOpen);
        trigger?.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
      });
      return;
    }
    state.modelBacktestSelection = {
      selector: preset.dataset.backtestSelector,
      modelId: "",
      modelRunId: "",
      label: preset.dataset.backtestLabel || preset.textContent.trim(),
      role: "",
      rollingDaily: false,
    };
    setPanel("model-research");
    setModelWorkspace("backtest");
    await loadSelectedModelBacktest();
    return;
  }

  const categoryButton = event.target.closest?.("[data-backtest-category]");
  if (categoryButton) {
    const category = ["research", "rolling", "production"].includes(categoryButton.dataset.backtestCategory)
      ? categoryButton.dataset.backtestCategory
      : "research";
    state.modelBacktestCategory = category;
    state.modelBacktestSelection = {
      selector: "model",
      modelId: "",
      modelRunId: "",
      label: category === "research" ? "研究模型" : (category === "rolling" ? "Rolling 模型" : "生产模型"),
      role: category === "rolling" ? "rolling_campaign" : "",
      rollingDaily: false,
    };
    setPanel("model-research");
    setModelWorkspace("backtest");
    renderModelResearch();
    return;
  }

  const sortButton = event.target.closest?.("[data-backtest-sort]");
  if (sortButton) {
    event.preventDefault();
    event.stopPropagation();
    const sortKey = ["time", "score", "annualized", "ir"].includes(sortButton.dataset.backtestSort)
      ? sortButton.dataset.backtestSort
      : "time";
    const currentSort = ["time", "score", "annualized", "ir"].includes(state.modelBacktestSort)
      ? state.modelBacktestSort
      : "time";
    state.modelBacktestSortDirection = currentSort === sortKey
      ? (state.modelBacktestSortDirection === "asc" ? "desc" : "asc")
      : "desc";
    state.modelBacktestSort = sortKey;
    renderModelResearch();
    window.requestAnimationFrame(() => {
      const menu = document.querySelector(".backtest-select-panel:not(.is-hidden) .backtest-model-menu");
      const trigger = menu?.querySelector(".backtest-model-menu-trigger");
      menu?.classList.add("is-open");
      trigger?.setAttribute("aria-expanded", "true");
      menu?.querySelector(`[data-backtest-sort="${sortKey}"]`)?.focus({ preventScroll: true });
    });
    return;
  }

  const rollingDailyButton = event.target.closest?.("[data-rolling-daily-load]");
  if (rollingDailyButton) {
    state.modelBacktestSelection = {
      ...state.modelBacktestSelection,
      rollingDaily: true,
    };
    await loadSelectedModelBacktest({ includeDaily: true });
    return;
  }

  const modelButton = event.target.closest?.("[data-model-backtest-id]");
  if (modelButton) {
    const selectedCategory = ["research", "rolling", "production"].includes(modelButton.dataset.modelBacktestCategory)
      ? modelButton.dataset.modelBacktestCategory
      : (modelButton.dataset.modelBacktestRole === "rolling_campaign" ? "rolling" : state.modelBacktestCategory || "research");
    state.modelBacktestCategory = selectedCategory;
    state.modelBacktestSelection = {
      selector: "model",
      modelId: modelButton.dataset.modelBacktestId,
      modelRunId: modelButton.dataset.modelBacktestRunId || "",
      label: modelButton.dataset.modelBacktestLabel || "选中模型",
      role: modelButton.dataset.modelBacktestRole || (selectedCategory === "rolling" ? "rolling_campaign" : ""),
      rollingDaily: false,
    };
    setPanel("model-research");
    setModelWorkspace("backtest");
    closeBacktestModelMenus();
    await loadSelectedModelBacktest();
  }
});

document.addEventListener("keydown", (event) => {
  if (!["Enter", " "].includes(event.key)) return;
  const candidateRow = event.target.closest?.(".model-candidate-row[data-model-backtest-run-id]");
  if (!candidateRow) return;
  event.preventDefault();
  candidateRow.click();
});

document.addEventListener("mouseover", (event) => {
  const hoverTarget = event.target.closest?.("[data-backtest-hover-date]");
  if (hoverTarget) scheduleBacktestHoverPanelUpdate(hoverTarget.dataset.backtestHoverDate);
  const paperTarget = event.target.closest?.("[data-paper-curve-date]");
  if (paperTarget) updatePaperAccountHoverPanel(paperTarget.dataset.paperCurveDate);
});

document.addEventListener("focusin", (event) => {
  const hoverTarget = event.target.closest?.("[data-backtest-hover-date]");
  if (hoverTarget) scheduleBacktestHoverPanelUpdate(hoverTarget.dataset.backtestHoverDate);
  const paperTarget = event.target.closest?.("[data-paper-curve-date]");
  if (paperTarget) updatePaperAccountHoverPanel(paperTarget.dataset.paperCurveDate);
});

function text(value, fallback = "--") {
  if (value === undefined || value === null || value === "") return fallback;
  return String(value);
}

function displayStepText(step, key, fallback = "") {
  const displayKey = `display_${key}`;
  return text(modelRunErrorSummary(step?.[displayKey] || step?.[key]) || step?.[displayKey] || step?.[key], fallback);
}

function modelRunErrorSummary(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const lower = raw.toLowerCase();
  if (lower.includes("metric 'rank ic' is malformed") || lower.includes("metric 'rank icir' is malformed")) {
    return "Qlib 训练和 pred.pkl 已产出，但 MLflow/Qlib artifact 阶段读取 Rank IC 指标失败；当前 round 已进入 blocker。";
  }
  if (lower.includes("failed to download artifacts from path 'report_normal_1day.pkl'")) {
    return "Qlib portfolio_analysis/report_normal_1day.pkl 缺失，回测曲线未生成。";
  }
  if (lower.includes("post_run_artifact_portfolio_config_mismatch")) {
    return "运行后的 portfolio artifact 审计未通过：缺少或未读取到 PortAnaRecord/strategy/backtest 配置。";
  }
  if (lower.includes("run_model_failed:") || lower.includes("codererror(") || lower.includes("traceback")) {
    const first = raw.split(/\n+/).find(Boolean) || raw;
    return clip(first.replace(/^run_model_failed:/, ""), 260);
  }
  return "";
}

function shortNumber(value, digits = 2) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return "--";
  return Number(value).toFixed(digits);
}

function moneyNumber(value) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return "--";
  const number = Number(value);
  const abs = Math.abs(number);
  if (abs >= 1e8) return `${(number / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${(number / 1e4).toFixed(2)}万`;
  return number.toFixed(0);
}

function pct(value, digits = 2) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return "--";
  return `${(Number(value) * 100).toFixed(digits)}%`;
}

function signedPercent(value, digits = 2) {
  const numeric = Number(value);
  if (value === undefined || value === null || !Number.isFinite(numeric)) return "--";
  return `${numeric >= 0 ? "+" : ""}${pct(numeric, digits)}`;
}

function percentagePoints(value, digits = 2) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return "--";
  return `${(Number(value) * 100).toFixed(digits)} pp`;
}

function renderRiskLineChart(rows, series, options = {}) {
  const validRows = (rows || []).filter((row) => series.some((item) => Number.isFinite(Number(row[item.key]))));
  if (validRows.length < 2) return `<div class="paper-risk-chart-empty">历史序列积累中</div>`;
  const width = 720;
  const height = 250;
  const margin = { left: 56, right: 20, top: 22, bottom: 34 };
  const values = validRows.flatMap((row) => series.map((item) => Number(row[item.key])).filter(Number.isFinite));
  const thresholdValues = (options.thresholds || []).map((item) => Number(item.value)).filter(Number.isFinite);
  let minValue = Number.isFinite(options.minValue) ? Number(options.minValue) : Math.min(...values, ...thresholdValues);
  let maxValue = Number.isFinite(options.maxValue) ? Number(options.maxValue) : Math.max(...values, ...thresholdValues);
  if (Math.abs(maxValue - minValue) < 1e-9) maxValue = minValue + 1;
  if (!Number.isFinite(options.minValue)) minValue -= (maxValue - minValue) * 0.08;
  if (!Number.isFinite(options.maxValue)) maxValue += (maxValue - minValue) * 0.08;
  const xFor = (index) => margin.left + index * (width - margin.left - margin.right) / Math.max(validRows.length - 1, 1);
  const yFor = (value) => margin.top + (maxValue - Number(value)) * (height - margin.top - margin.bottom) / (maxValue - minValue);
  const format = options.format || ((value) => pct(value, 1));
  const bands = options.showStress ? validRows.map((row, index) => {
    if (!row.market_stress) return "";
    const step = (width - margin.left - margin.right) / Math.max(validRows.length - 1, 1);
    return `<rect class="paper-risk-stress-band" x="${Math.max(margin.left, xFor(index) - step / 2).toFixed(1)}" y="${margin.top}" width="${Math.max(step, 2).toFixed(1)}" height="${height - margin.top - margin.bottom}"><title>${escapeHtml(row.date)} · 市场压力</title></rect>`;
  }).join("") : "";
  const grid = [0, 0.5, 1].map((ratio) => {
    const value = maxValue - (maxValue - minValue) * ratio;
    const y = yFor(value);
    return `<line class="paper-risk-chart-grid" x1="${margin.left}" y1="${y.toFixed(1)}" x2="${width - margin.right}" y2="${y.toFixed(1)}"></line><text class="paper-risk-chart-axis" x="${margin.left - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end">${escapeHtml(format(value))}</text>`;
  }).join("");
  const thresholds = (options.thresholds || []).map((item) => {
    const y = yFor(item.value);
    return `<line class="paper-risk-threshold" x1="${margin.left}" y1="${y.toFixed(1)}" x2="${width - margin.right}" y2="${y.toFixed(1)}"></line><text class="paper-risk-threshold-label" x="${width - margin.right - 4}" y="${(y - 5).toFixed(1)}" text-anchor="end">${escapeHtml(item.label)}</text>`;
  }).join("");
  const lines = series.map((item) => {
    const points = validRows.map((row, index) => {
      const value = Number(row[item.key]);
      return Number.isFinite(value) ? `${xFor(index).toFixed(1)},${yFor(value).toFixed(1)}` : "";
    }).filter(Boolean).join(" ");
    return `<polyline class="paper-risk-chart-line" points="${points}" style="--risk-series:${item.color}"></polyline>`;
  }).join("");
  const sampleEvery = Math.max(1, Math.ceil(validRows.length / 18));
  const hitPoints = validRows.map((row, index) => index % sampleEvery === 0 || index === validRows.length - 1 ? series.map((item) => {
    const value = Number(row[item.key]);
    if (!Number.isFinite(value)) return "";
    return `<circle class="paper-risk-chart-hit" cx="${xFor(index).toFixed(1)}" cy="${yFor(value).toFixed(1)}" r="5"><title>${escapeHtml(row.date)} · ${escapeHtml(item.label)} ${escapeHtml(format(value))}</title></circle>`;
  }).join("") : "").join("");
  const legend = series.map((item) => `<span><i style="--risk-series:${item.color}"></i>${escapeHtml(item.label)}</span>`).join("");
  return `<div class="paper-risk-chart"><div class="paper-risk-chart-legend">${legend}</div><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(options.label || "风控历史走势")}">${bands}${grid}${thresholds}${lines}${hitPoints}<text class="paper-risk-chart-axis" x="${margin.left}" y="${height - 8}">${escapeHtml(validRows[0].date)}</text><text class="paper-risk-chart-axis" x="${width - margin.right}" y="${height - 8}" text-anchor="end">${escapeHtml(validRows[validRows.length - 1].date)}</text></svg></div>`;
}

function validationBadgeClass(status) {
  if (status === "blocked") return "danger";
  if (status === "review_required") return "warn";
  if (status === "clean") return "ok";
  return "subtle";
}

function backtestDailyItem(date) {
  const backtest = serviceOutputs(state.modelBacktest);
  const daily = backtest.daily_breakdown || {};
  return (daily.by_date || {})[date] || (daily.items || []).find((item) => item.date === date) || null;
}

function renderBacktestHoverContent(date) {
  const item = backtestDailyItem(date);
  if (!item) {
    const rollingSelection = state.modelBacktestSelection.role === "rolling_campaign"
      || state.modelBacktestSelection.selector === "rolling";
    if (rollingSelection && state.modelBacktestSelection.rollingDaily !== true) {
      return `<div class="empty-state compact">当前仅加载 Rolling 拼接净值；点击曲线下方按钮后，可查看所选 Seed 的逐日持仓、交易和贡献。</div>`;
    }
    return `<div class="empty-state compact">悬停曲线上的交易日，可查看当天收益、持仓、交易和贡献。</div>`;
  }
  const holdings = (item.holdings || []).slice(0, 8);
  const trades = (item.trades || []).slice(0, 6);
  const contributors = (item.top_contributors || []).slice(0, 6);
  const dailyReturn = Number(item.daily_return);
  const dailyBenchmarkReturn = Number(item.daily_benchmark_return);
  const dailyExcessReturn = Number.isFinite(dailyReturn) && Number.isFinite(dailyBenchmarkReturn)
    ? dailyReturn - dailyBenchmarkReturn
    : Number(item.daily_excess_return);
  const rowList = (rows, renderer, emptyText) => rows.length
    ? rows.map(renderer).join("")
    : `<li class="muted">${escapeHtml(emptyText)}</li>`;
  return `
    <div class="backtest-hover-head">
      <strong>${escapeHtml(item.date)}</strong>
      <span>账户 ${shortNumber(item.account, 0)}</span>
    </div>
    <div class="backtest-hover-metrics">
      <span><b>当日净收益</b>${pct(item.daily_return, 2)}</span>
      <span><b>当日成本后超额</b>${pct(dailyExcessReturn, 2)}</span>
      <span><b>累计净收益</b>${pct(item.strategy_cumulative_return, 1)}</span>
      <span><b>相对基准累计</b>${pct(item.relative_cumulative_return ?? item.excess_cumulative_return, 1)}</span>
      <span><b>换手</b>${pct(item.turnover, 2)}</span>
      <span><b>成本</b>${pct(item.cost, 4)}${item.cost_value !== undefined && item.cost_value !== null ? ` · ${moneyNumber(item.cost_value)}` : ""}</span>
    </div>
    <div class="backtest-hover-columns">
      <div>
        <h4>持仓 Top</h4>
        <ul>${rowList(holdings, (row) => `<li><span>${escapeHtml(row.symbol)} ${escapeHtml(row.security_name || "")}</span><b>${pct(row.weight, 2)}</b></li>`, "无持仓明细")}</ul>
      </div>
      <div>
        <h4>交易</h4>
        <ul>${rowList(trades, (row) => `<li><span>${escapeHtml(row.side)} ${escapeHtml(row.symbol)} ${escapeHtml(row.security_name || "")}${row.trade_cost !== undefined && row.trade_cost !== null ? `<small>成本 ${moneyNumber(row.trade_cost)}</small>` : ""}</span><b>${moneyNumber(row.trade_value)}</b></li>`, "无交易事件")}</ul>
      </div>
      <div>
        <h4>贡献</h4>
        <ul>${rowList(contributors, (row) => `<li><span>${escapeHtml(row.symbol)} ${escapeHtml(row.security_name || "")}</span><b>${moneyNumber(row.contribution)}</b></li>`, "无贡献拆解")}</ul>
      </div>
    </div>
  `;
}

function updateBacktestHoverPanel(date) {
  const panel = document.getElementById("backtest-hover-panel");
  const nextDate = text(date, "");
  if (!panel || !nextDate || panel.dataset.hoverDate === nextDate) return;
  panel.innerHTML = renderBacktestHoverContent(nextDate);
  panel.dataset.hoverDate = nextDate;
}

function scheduleBacktestHoverPanelUpdate(date) {
  const nextDate = text(date, "");
  if (!nextDate) return;
  const panel = document.getElementById("backtest-hover-panel");
  if (panel?.dataset.hoverDate === nextDate || state.modelBacktestHoverPendingDate === nextDate) return;
  state.modelBacktestHoverPendingDate = nextDate;
  if (state.modelBacktestHoverFrame) return;
  state.modelBacktestHoverFrame = window.requestAnimationFrame(() => {
    const pendingDate = state.modelBacktestHoverPendingDate;
    state.modelBacktestHoverFrame = 0;
    state.modelBacktestHoverPendingDate = "";
    updateBacktestHoverPanel(pendingDate);
  });
}

function renderBacktestCurveChart(curve, options = {}) {
  const points = (curve || []).filter((item) => item && item.date && item.model_return !== null && item.model_return !== undefined);
  if (points.length < 2) {
    return `<div class="empty-state">暂无 Qlib 回测曲线数据。</div>`;
  }
  const width = 760;
  const height = 280;
  const pad = { top: 18, right: 22, bottom: 34, left: 54 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const values = points.flatMap((item) => [
    Number(item.model_return),
    item.benchmark_return === null || item.benchmark_return === undefined ? 0 : Number(item.benchmark_return),
    item.excess_return === null || item.excess_return === undefined ? 0 : Number(item.excess_return),
  ]).filter((value) => Number.isFinite(value));
  const yMinRaw = Math.min(0, ...values);
  const yMaxRaw = Math.max(0, ...values);
  const span = yMaxRaw - yMinRaw || 1;
  const yMin = yMinRaw - span * 0.06;
  const yMax = yMaxRaw + span * 0.08;
  const x = (idx) => pad.left + (points.length === 1 ? 0 : (idx / (points.length - 1)) * innerW);
  const y = (value) => pad.top + (1 - ((Number(value) - yMin) / (yMax - yMin))) * innerH;
  const pathFor = (key) => points.map((item, idx) => {
    const raw = item[key] === null || item[key] === undefined ? 0 : Number(item[key]);
    return `${idx === 0 ? "M" : "L"} ${x(idx).toFixed(1)} ${y(raw).toFixed(1)}`;
  }).join(" ");
  const zeroY = y(0).toFixed(1);
  const last = points[points.length - 1];
  let peakNav = -Infinity;
  let peakIdx = 0;
  let drawdownPeakIdx = 0;
  let maxDrawdownIdx = 0;
  let maxDrawdown = 0;
  let bestDailyIdx = 0;
  let bestDaily = -Infinity;
  points.forEach((item, idx) => {
    const cumulative = Number(item.model_return || 0);
    const nav = 1 + cumulative;
    const daily = Number(item.daily_model_return || 0);
    if (daily > bestDaily) {
      bestDaily = daily;
      bestDailyIdx = idx;
    }
    if (nav > peakNav) {
      peakNav = nav;
      peakIdx = idx;
    }
    const drawdown = peakNav > 0 ? nav / peakNav - 1 : 0;
    if (drawdown < maxDrawdown) {
      maxDrawdown = drawdown;
      maxDrawdownIdx = idx;
      drawdownPeakIdx = peakIdx;
    }
  });
  const bestDailyPoint = points[bestDailyIdx];
  const drawdownPeakPoint = points[drawdownPeakIdx];
  const maxDrawdownPoint = points[maxDrawdownIdx];
  const labelX = (idx) => Math.min(width - 170, Math.max(pad.left + 8, x(idx) + 10));
  const labelY = (value) => Math.max(pad.top + 16, y(value) - 14);
  const drawdownPeakReturn = Number(drawdownPeakPoint.model_return || 0);
  const drawdownTroughReturn = Number(maxDrawdownPoint.model_return || 0);
  const bestDailyReturn = Number(bestDailyPoint.model_return || 0);
  const hitWidth = Math.max(7, innerW / Math.max(1, points.length));
  const hoverTargets = points.map((item, idx) => `
    <rect
      class="chart-hover-hit"
      x="${(x(idx) - hitWidth / 2).toFixed(1)}"
      y="${pad.top}"
      width="${hitWidth.toFixed(1)}"
      height="${innerH}"
      tabindex="0"
      data-backtest-hover-date="${escapeHtml(item.date)}"
    >
      <title>${escapeHtml(item.date)} 当日净收益 ${pct(item.daily_model_return, 2)} 累计净收益 ${pct(item.model_return, 1)}</title>
    </rect>
  `).join("");
  const foldRows = Array.isArray(options.folds) ? options.folds : [];
  const foldBoundaries = foldRows.slice(1).map((fold, foldIndex) => {
    const boundaryDate = text(fold.signal_start || fold.segments?.test?.[0], "");
    const boundaryIndex = points.findIndex((item) => item.date >= boundaryDate);
    if (!boundaryDate || boundaryIndex < 0) return "";
    const boundaryX = x(boundaryIndex).toFixed(1);
    return `
      <line class="chart-fold-boundary" x1="${boundaryX}" y1="${pad.top}" x2="${boundaryX}" y2="${height - pad.bottom}"></line>
      <text class="chart-fold-label" x="${(Number(boundaryX) + 5).toFixed(1)}" y="${pad.top + 13}">第 ${foldIndex + 2} 折</text>
    `;
  }).join("");
  return `
    <div class="backtest-chart-shell">
      <div class="backtest-chart-main">
        <div class="chart-legend">
          <span><i class="legend-dot model"></i>成本后策略累计 ${pct(last.model_return, 1)}</span>
          <span><i class="legend-dot benchmark"></i>基准累计 ${pct(last.benchmark_return, 1)}</span>
          <span><i class="legend-dot excess"></i>相对基准累计 ${pct(last.relative_cumulative_return ?? last.excess_return, 1)}</span>
        </div>
        <div class="chart-basis-note">复利净值口径 · 策略已扣交易成本 · 相对基准 = 策略净值 ÷ 基准净值 − 1</div>
        <svg class="backtest-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="成本后策略、基准和相对基准累计收益曲线">
          <line class="chart-axis" x1="${pad.left}" y1="${zeroY}" x2="${width - pad.right}" y2="${zeroY}"></line>
          <line class="chart-grid" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}"></line>
          <line class="chart-grid" x1="${pad.left}" y1="${pad.top}" x2="${width - pad.right}" y2="${pad.top}"></line>
          <line class="chart-grid" x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}"></line>
          <path class="chart-line benchmark" d="${pathFor("benchmark_return")}"></path>
          <path class="chart-line excess" d="${pathFor("excess_return")}"></path>
          <path class="chart-line model" d="${pathFor("model_return")}"></path>
          ${foldBoundaries}
          <line class="chart-dd-span" x1="${x(drawdownPeakIdx).toFixed(1)}" y1="${y(drawdownPeakReturn).toFixed(1)}" x2="${x(maxDrawdownIdx).toFixed(1)}" y2="${y(drawdownTroughReturn).toFixed(1)}"></line>
          <g class="chart-marker marker-peak">
            <circle cx="${x(drawdownPeakIdx).toFixed(1)}" cy="${y(drawdownPeakReturn).toFixed(1)}" r="4"></circle>
            <text x="${labelX(drawdownPeakIdx).toFixed(1)}" y="${labelY(drawdownPeakReturn).toFixed(1)}">回撤起点</text>
            <text x="${labelX(drawdownPeakIdx).toFixed(1)}" y="${(labelY(drawdownPeakReturn) + 15).toFixed(1)}">${escapeHtml(drawdownPeakPoint.date)}</text>
          </g>
          <g class="chart-marker marker-drawdown">
            <circle cx="${x(maxDrawdownIdx).toFixed(1)}" cy="${y(drawdownTroughReturn).toFixed(1)}" r="5"></circle>
            <text x="${labelX(maxDrawdownIdx).toFixed(1)}" y="${labelY(drawdownTroughReturn).toFixed(1)}">最大回撤 ${pct(maxDrawdown, 1)}</text>
            <text x="${labelX(maxDrawdownIdx).toFixed(1)}" y="${(labelY(drawdownTroughReturn) + 15).toFixed(1)}">${escapeHtml(maxDrawdownPoint.date)}</text>
          </g>
          <g class="chart-marker marker-best-day">
            <circle cx="${x(bestDailyIdx).toFixed(1)}" cy="${y(bestDailyReturn).toFixed(1)}" r="4"></circle>
            <text x="${labelX(bestDailyIdx).toFixed(1)}" y="${labelY(bestDailyReturn).toFixed(1)}">最大单日 ${pct(bestDaily, 1)}</text>
            <text x="${labelX(bestDailyIdx).toFixed(1)}" y="${(labelY(bestDailyReturn) + 15).toFixed(1)}">${escapeHtml(bestDailyPoint.date)}</text>
          </g>
          <text class="chart-label" x="${pad.left}" y="${height - 10}">${escapeHtml(points[0].date)}</text>
          <text class="chart-label end" x="${width - pad.right}" y="${height - 10}">${escapeHtml(last.date)}</text>
          <text class="chart-label y-axis" x="8" y="${y(yMaxRaw).toFixed(1)}">${pct(yMaxRaw, 0)}</text>
          <text class="chart-label y-axis" x="8" y="${zeroY}">0%</text>
          <text class="chart-label y-axis" x="8" y="${y(yMinRaw).toFixed(1)}">${pct(yMinRaw, 0)}</text>
          ${hoverTargets}
        </svg>
      </div>
      <div id="backtest-hover-panel" class="backtest-hover-panel" data-hover-date="${escapeHtml(last.date)}">
        ${renderBacktestHoverContent(last.date)}
      </div>
    </div>
  `;
}

function clip(value, max = 220) {
  if (!value) return "";
  const clean = String(value).trim();
  return clean.length > max ? `${clean.slice(0, max)}...` : clean;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function clearNode(node) {
  node.innerHTML = "";
}

function serviceOutputs(payload) {
  return payload?.outputs || payload || {};
}

function firstNonEmptyArray(...values) {
  return values.find((value) => Array.isArray(value) && value.length) || [];
}

function parseIso(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function parseIsoDay(value) {
  if (!value) return null;
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) return parseIso(value);
  const [, year, month, day] = match;
  const date = new Date(Number(year), Number(month) - 1, Number(day));
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatDateInputValue(value) {
  const date = value instanceof Date ? value : parseIsoDay(value);
  if (!date) return "";
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function shiftDateInputValue(value, deltaDays) {
  const base = parseIsoDay(value);
  if (!base) return "";
  const next = new Date(base.getFullYear(), base.getMonth(), base.getDate() + deltaDays);
  return formatDateInputValue(next);
}

function secondsSince(value) {
  const date = parseIso(value);
  if (!date) return Infinity;
  return (Date.now() - date.getTime()) / 1000;
}

function compactDateTime(value) {
  const date = parseIso(value);
  if (!date) return "--";
  const pad = (number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function compactClockTime(value) {
  const date = parseIso(value);
  if (!date) return "--";
  const pad = (number) => String(number).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function parseJsonMaybe(value) {
  if (!value) return null;
  try {
    return JSON.parse(String(value));
  } catch (_error) {
    return null;
  }
}

function toolPayloadFromPreview(event) {
  const parsed = parseJsonMaybe(event?.result_preview);
  if (!parsed) return {};
  return parsed.outputs || parsed;
}

function normalizeFactorConsole(responseLike) {
  const wrapper = responseLike || { ok: true, outputs: {} };
  const outputs = serviceOutputs(wrapper);
  delete outputs.active_job;
  delete outputs.recent_jobs;
  delete outputs.latest_run;
  delete outputs.latest_research;
  return { ...wrapper, outputs };
}

async function getJson(url, options = {}) {
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? (url.startsWith(`${MODEL_API_PREFIX}/status`) ? 70000 : 15000);
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  const resp = await fetch(apiUrl(url), { signal: controller.signal, cache: "no-store" }).finally(() => window.clearTimeout(timer));
  if (!resp.ok) {
    throw new Error(`${url} -> ${resp.status}`);
  }
  return await resp.json();
}

async function postJson(url, body, options = {}) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), options.timeoutMs ?? 30000);
  try {
    const resp = await fetch(apiUrl(url), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const payload = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      return { ...payload, ok: false, error: payload.error || payload.err || `${url} -> ${resp.status}`, http_status: resp.status };
    }
    return payload;
  } catch (error) {
    return { ok: false, error: String(error), _failed: true };
  } finally {
    window.clearTimeout(timer);
  }
}

async function getJsonSafe(url, options = {}) {
  try {
    return await getJson(url, options);
  } catch (error) {
    return { ok: false, error: String(error), _failed: true };
  }
}

function isAbortLikeErrorMessage(value) {
  const message = String(value || "").toLowerCase();
  return message.includes("aborterror")
    || message.includes("signal is aborted")
    || message.includes("operation was aborted")
    || message.includes("the user aborted a request");
}

function annotateBacktestDailyFallback(result, fallbackReason = "") {
  if (!result || result._failed || result.error) return result;
  const outputs = serviceOutputs(result);
  outputs.daily_breakdown = {
    available: false,
    reason: "daily breakdown timeout fallback",
    items: [],
    by_date: {},
  };
  outputs.stock_contribution = {
    available: false,
    reason: "每日持仓和个股贡献明细加载超时，当前先展示基础回测曲线。",
  };
  outputs.load_warning = `每日明细加载超时，已自动降级为基础回测视图。${fallbackReason ? ` 原始错误：${fallbackReason}` : ""}`;
  result.outputs = outputs;
  return result;
}

function keepPreviousOnReadFailure(nextValue, previousValue) {
  if (nextValue && !nextValue._failed && !nextValue.error) return nextValue;
  return previousValue || nextValue;
}

async function getFactorConsoleSafe(options = {}) {
  return await getJsonSafe("/factor/console/live", options);
}

function offlineResearchConsoleFromSnapshot(snapshotPayload) {
  const snapshotOutputs = serviceOutputs(snapshotPayload);
  const snapshotDigest = snapshotOutputs.live_research_digest || {};
  const hasSnapshot = Boolean(
    snapshotOutputs.runtime_view
    || snapshotOutputs.decision_view
    || Array.isArray(snapshotOutputs.research_steps)
    || Array.isArray(snapshotDigest.research_steps)
  );
  if (!hasSnapshot) {
    return normalizeFactorConsole({
      ok: false,
      error: "factor_console_live_unavailable",
      outputs: {
        source: "empty",
        is_live: false,
        status: "unavailable",
        pipeline: { overall_status: "unavailable", ok: false, error: "factor_console_live_unavailable" },
        runtime_view: {},
        decision_view: {},
        research_steps: [],
        live_research_digest: { source: "empty", is_live: false, research_steps: [] },
      },
    });
  }
  return normalizeFactorConsole({
    ok: true,
    outputs: {
      ...snapshotOutputs,
      source: "offline_snapshot",
      is_live: false,
      snapshot_generated_at: snapshotPayload?.generated_at || snapshotOutputs.generated_at,
      live_research_digest: {
        ...snapshotDigest,
        source: "offline_snapshot",
        is_live: false,
        snapshot_generated_at: snapshotPayload?.generated_at || snapshotOutputs.generated_at,
      },
    },
  });
}

async function refreshDataLive() {
  if (document.hidden || !dataFoundationPanelIsVisible() || state.dataFoundationTab !== "live") return;
  state.dataLiveStatus = await getJsonSafe("/data/live-status");
  renderDataFoundation();
  scheduleDataLivePolling();
}

function dataLiveTargetDate() {
  const formEl = document.getElementById("data-live-control-form");
  if (!formEl) return "auto";
  const form = new FormData(formEl);
  return form.get("target_date") || "auto";
}

async function refreshDataQueryFields() {
  state.dataQueryFields = await getJsonSafe("/data/query/fields");
  state.dataQueryExpandedGroups = [];
  renderDataFoundation();
}

function dataFoundationLatestTradeDate() {
  const data = serviceOutputs(state.data);
  return formatDateInputValue(
    data.current_dataset?.latest_trade_date
    || data.current_dataset?.latest_dates?.hdf5
    || data.snapshot?.latest_hdf5_trade_date
    || data.data_quality_summary?.latest_trade_date
    || ""
  );
}

function ensureDataQueryDateDefaults(formEl) {
  if (!formEl) return;
  const startInput = formEl.querySelector('input[name="start"]');
  const endInput = formEl.querySelector('input[name="end"]');
  if (!startInput || !endInput) return;
  const fallbackEnd = dataFoundationLatestTradeDate() || formatDateInputValue(new Date());
  if (!endInput.value) {
    endInput.value = fallbackEnd;
  }
  if (!startInput.value) {
    startInput.value = shiftDateInputValue(endInput.value || fallbackEnd, -DATA_QUERY_DEFAULT_WINDOW_DAYS);
  }
}

async function runDataQueryFromForm() {
  const formEl = document.getElementById("data-query-form");
  if (!formEl) return;
  if (!state.dataQueryFields) {
    await refreshDataQueryFields();
  }
  ensureDataQueryDateDefaults(formEl);
  state.dataQueryLoading = true;
  renderDataFoundation();
  const form = new FormData(formEl);
  const params = new URLSearchParams();
  params.set("code", form.get("code") || "");
  if (form.get("start")) params.set("start", form.get("start"));
  if (form.get("end")) params.set("end", form.get("end"));
  if (form.get("benchmark")) params.set("benchmark", form.get("benchmark"));
  params.set("transform", form.get("transform") || "index100");
  params.set("fields", selectedDataQueryFields().join(","));
  try {
    state.dataQueryResult = await getJsonSafe(`/data/query?${params.toString()}`);
  } finally {
    state.dataQueryLoading = false;
    renderDataFoundation();
  }
}

function dataLiveIsRunning() {
  const live = serviceOutputs(state.dataLiveStatus);
  const jobStatus = String(live.active_job?.status || live.latest_job?.status || live.status || "").toLowerCase();
  return /queued|running|started|stage_in_progress|in_progress/.test(jobStatus);
}

function scheduleDataLivePolling() {
  if (state.dataLiveRefreshTimer) {
    window.clearTimeout(state.dataLiveRefreshTimer);
    state.dataLiveRefreshTimer = null;
  }
  if (!dataFoundationPanelIsVisible() || state.dataFoundationTab !== "live") return;
  const delay = DATA_LIVE_REFRESH_INTERVAL_MS;
  state.dataLiveRefreshTimer = window.setTimeout(() => {
    refreshDataLive().catch((error) => {
      console.error("GUI data live refresh failed", error);
      scheduleDataLivePolling();
    });
  }, delay);
}

async function startDataUpdateFromGui({ mode = "daily", dryRun = true } = {}) {
  const targetDate = dataLiveTargetDate();
  state.latestDataAction = await postJson("/data/update/start", {
    mode,
    target_date: targetDate,
    timeout_minutes: 180,
    dry_run: dryRun,
    confirm: !dryRun,
  });
  state.dataFoundationTab = "live";
  localStorage.setItem("fxalpha-data-foundation-tab", state.dataFoundationTab);
  await refreshState({ reason: "data-update-start" });
  await refreshDataLive();
}

function appendMetricCards(container, items) {
  clearNode(container);
  items.forEach((item) => {
    const frag = metricTemplate.content.cloneNode(true);
    frag.querySelector(".metric-label").textContent = item.label;
    frag.querySelector(".metric-value").textContent = item.value;
    frag.querySelector(".metric-note").textContent = item.note || "";
    container.appendChild(frag);
  });
}

function buildGuidancePresets() {
  const container = document.getElementById("guidance-presets");
  if (!container) return;
  container.innerHTML = guidancePresets.map((item) => `
    <button class="preset-chip" type="button" data-guidance="${escapeHtml(item)}">${escapeHtml(item)}</button>
  `).join("");
  container.querySelectorAll(".preset-chip").forEach((button) => {
    button.addEventListener("click", () => {
      document.getElementById("guidance-input").value = button.dataset.guidance;
    });
  });
}

function renderApiChip() {
  const chip = document.getElementById("api-chip");
  const note = document.getElementById("backend-note");
  const ready = Boolean(state.health?.ok);
  chip.textContent = ready ? "API 在线 · 5分钟" : "API 离线";
  chip.classList.toggle("is-live", ready);
  note.textContent = "";
  note.hidden = true;
}

function renderMiniMetrics() {
  const container = document.getElementById("mini-metrics");
  const factorConsole = serviceOutputs(state.factorConsole);
  const factorStatus = serviceOutputs(state.factorStatus);
  const digest = liveResearchDigest();
  const qgpt = digest.quantgpt_task_summary
    || factorConsole.quantgpt_task_summary
    || factorStatus.quantgpt_task_summary
    || {};
  const runningTask = (qgpt.running_tasks || [])[0];
  const registry = factorConsole.registry_summary || factorStatus.registry_summary || {};
  const runtime = factorConsole.runtime_view || factorStatus.runtime_view || digest.runtime_view || {};
  const runView = factorConsole.run_view || factorStatus.run_view || serviceOutputs(state.factorRunView);
  const toolIntent = (runView.tool_intents || [])[0] || {};
  const shortTaskType = {
    score: "Score",
    backtest: "Backtest",
    anti_overfit: "Anti",
    adversarial_validation: "Adv",
    novelty_check: "Novelty",
    quality_gate: "Gate",
  }[runningTask?.task_type] || runningTask?.task_type;
  const phaseText = runningTask
    ? shortTaskType || "Running"
    : runtime.current_phase || digest.current_phase || "研究记录";
  container.innerHTML = `
    <div class="mini-metric">
      <span>因子</span>
      <strong>${text(registry.active, "0")}</strong>
    </div>
    <div class="mini-metric">
      <span>ICIR</span>
      <strong>${shortNumber(registry.avg_icir, 3)}</strong>
    </div>
    <div class="mini-metric metric-wide">
      <span>阶段</span>
      <strong title="${escapeHtml(phaseText)}">${escapeHtml(clip(phaseText, 28))}</strong>
    </div>
  `;
}

function renderResearchSummary() {
  const factorConsole = serviceOutputs(state.factorConsole);
  const registry = factorConsole.registry_summary || {};
  const readiness = factorConsole.readiness || {};
  const digest = liveResearchDigest();
  const runtime = factorConsole.runtime_view || digest.runtime_view || {};
  const counts = runtime.progress_counts || {};
  const llmUsage = factorConsole.llm_usage_summary || digest.llm_usage_summary || {};
  const runView = factorConsole.run_view || serviceOutputs(state.factorRunView);
  const staleTasks = factorConsole.stale_quantgpt_tasks || runView.stale_quantgpt_tasks || {};
  const runMode = digest.run_mode || factorConsole.selected_run?.contract?.run_mode || "orchestrator";
  const controllerLabel = runMode === "codex_mcp"
    ? "MCP 调试"
    : runMode === "production"
      ? "生产 ORCH"
      : "生产 ORCH";
  const effectiveStatus = `${text(factorConsole.status, "idle")} / ${text(runtime.current_phase, "Research")}`;
  const effectiveNote = runtime.current_action || digest.latest_llm_step?.summary || "等待研究启动";

  appendMetricCards(document.getElementById("research-summary"), [
    {
      label: "研究状态",
      value: effectiveStatus,
      note: `${controllerLabel} · ${effectiveNote}`,
    },
    {
      label: "活跃因子",
      value: text(registry.active, "0"),
      note: `平台共 ${text(registry.total, "0")} 个因子，平均 ICIR ${shortNumber(registry.avg_icir, 3)}`,
    },
    {
      label: "QuantGPT 执行网关",
      value: readiness.quantgpt_api?.execution_ready ? "可执行" : "阻断",
      note: readiness.quantgpt_api?.reachable
        ? text(readiness.quantgpt_api?.url, "")
        : "代码/MCP 网关独立检查；HTTP 仅用于调试观测",
    },
    {
      label: "最新研究结果",
      value: `${text(counts.imported, "0")} / ${text(counts.candidates, "0")}`,
      note: `本次 research_steps ${text(counts.research_steps, "0")}，QuantGPT 任务 ${text(counts.qgpt_task_store_total, "0")}`,
    },
    {
      label: "工具进度",
      value: (runView.tool_intents || [])[0]?.tool || "等待工具",
      note: ((runView.tool_intents || [])[0] || {}).task_id
        ? `${text(((runView.tool_intents || [])[0] || {}).candidate_id || "候选", "候选")} · task ${text(((runView.tool_intents || [])[0] || {}).task_id, "")}`
        : "由现有 research_steps 与 QuantGPT task store 投影",
    },
    {
      label: "任务陈旧提示",
      value: Number(staleTasks.stale_count || 0) > 0 ? `发现 ${text(staleTasks.stale_count, "0")}` : "无",
      note: Number(staleTasks.stale_count || 0) > 0
        ? `超过 ${text(staleTasks.stale_threshold_seconds, "") } 秒未活动；仅提示，不会自动改写 QuantGPT 任务状态`
        : "只读检查 QuantGPT task DB，不做自动回填或重置",
    },
    {
      label: "LLM 决策用量",
      value: `${text(llmUsage.request_count, "0")} 次`,
      note: llmUsage.total_tokens
        ? `${text(llmUsage.total_tokens, "0")} tokens · ${text(llmUsage.payload_chars, "0")} payload chars`
        : `${text(llmUsage.payload_chars, "0")} payload chars · ${text(llmUsage.error_count, "0")} errors`,
    },
  ]);
}

function latestEventByName(events, name) {
  const reversed = [...(events || [])].reverse();
  return reversed.find((item) => item.event === name) || null;
}

function recordRoundId(record) {
  if (!record || typeof record !== "object") return "";
  if (record.round_id) return String(record.round_id);
  const stageId = String(record.stage_id || record.trace_id || "");
  const match = stageId.match(/(.+?:r\d{4})/);
  return match ? match[1] : "";
}

function currentResearchRoundId() {
  const latest = researchSteps()[0] || {};
  return recordRoundId(latest);
}

function currentRoundQualityGate(gate, digest = {}) {
  if (!gate?.counts) return {};
  const gateRound = recordRoundId(gate);
  const currentRound = currentResearchRoundId();
  if (gateRound && currentRound) return gateRound === currentRound ? gate : {};
  const phase = String(digest.current_phase || digest.stage || "").toLowerCase();
  const latestStage = String((researchSteps()[0] || {}).stage || "").toLowerCase();
  return /quality_gate|import_gate|gate|import/.test(`${phase} ${latestStage}`) ? gate : {};
}

function latestToolEvent(events, toolName) {
  const reversed = [...(events || [])].reverse();
  return reversed.find((item) => item.event === "tool_call_completed" && item.tool === toolName) || null;
}

function researchProjection() {
  const factorConsole = serviceOutputs(state.factorConsole);
  const digest = liveResearchDigest();
  const runtime = factorConsole.runtime_view || digest.runtime_view || {};
  const steps = researchSteps();
  const latest = steps[0] || {};
  const earliest = steps.length ? steps[steps.length - 1] : {};
  const guidanceHistory = steps.filter((step) => step.stage === "human_guidance");
  const researchState = latest?.extra?.research_state || {};
  return {
    run_id: runtime.run_id || latest.run_id || state.lastRunId || "",
    status: runtime.status || factorConsole.status || "idle",
    stage: latest.stage || runtime.current_phase || "idle",
    started_at: earliest.ts || earliest.created_at || "",
    latest_event: {
      ts: runtime.updated_at || latest.ts || latest.created_at || "",
      event: latest.stage || runtime.current_phase || "idle",
      message: latest.summary || runtime.current_action || "",
      extra: { research_state: researchState },
    },
    event_count: steps.length,
    events: steps.map((step) => {
      const transition = researchStepTransition(step).transition || {};
      return {
        ts: step.ts || step.created_at,
        event: step.stage || "research_step",
        message: step.summary || step.decision || "",
        next: transition.next_action || "",
        stage_id: step.stage_id,
        round_id: step.round_id,
      };
    }),
    inputs: {
      target_adopted: researchState.target_valid_imports || researchState.target_adopted || "",
      runtime_contract: latest?.extra?.runtime_contract || "",
      orchestration_mode: latest?.extra?.source || "research_steps",
    },
    summary: {
      adopted: runtime.progress_counts?.quality_gate_adopted || 0,
      valid_imports: runtime.progress_counts?.imported || 0,
    },
    guidance_history: guidanceHistory,
  };
}

function allResearchEvents(activeJob, latestResearch) {
  return activeJob?.events || [];
}

function researchConsoleIsLive() {
  const payload = state.factorConsole || {};
  const outputs = serviceOutputs(payload);
  if (!outputs || outputs.source === "offline_snapshot" || outputs.is_live === false) return false;
  return Boolean(
    !payload._failed
    && !payload.error
    && (
      outputs.runtime_view
      || outputs.decision_view
      || Array.isArray(outputs.research_steps)
      || outputs.live_research_digest
    )
  );
}

function offlineResearchSnapshotDigest() {
  const factorSnapshot = serviceOutputs(state.factorOverviewSnapshot);
  const rawDigest = factorSnapshot.live_research_digest || {};
  const runtime = factorSnapshot.runtime_view || rawDigest.runtime_view || {};
  const decisionView = factorSnapshot.decision_view || rawDigest.decision_view || {};
  const research_steps = firstNonEmptyArray(rawDigest.research_steps, factorSnapshot.research_steps);
  const candidateTaskView = firstNonEmptyArray(factorSnapshot.candidate_task_view, rawDigest.candidate_task_view);
  const candidateRecords = firstNonEmptyArray(factorSnapshot.candidate_records, rawDigest.candidate_records);
  const digest = {
    ...rawDigest,
    ...runtime,
    source: "offline_snapshot",
    is_live: false,
    snapshot_generated_at: state.factorOverviewSnapshot?.generated_at || factorSnapshot.generated_at || rawDigest.generated_at,
    status: runtime.status || rawDigest.status || factorSnapshot.status || "offline_snapshot",
    run_id: runtime.run_id || rawDigest.run_id || "",
    current_phase: runtime.current_phase || researchStepTitle(decisionView) || rawDigest.current_phase || "Offline Snapshot",
    current_action: runtime.current_action || runtime.next_action || decisionView.next || decisionView.decision || decisionView.summary || rawDigest.current_action || "",
    updated_at: runtime.updated_at || decisionView.updated_at || decisionView.ts || rawDigest.updated_at || state.factorOverviewSnapshot?.generated_at,
    runtime_view: runtime,
    decision_view: decisionView,
    research_steps,
    candidate_task_view: candidateTaskView,
    candidate_records: candidateRecords,
    recent_candidates: candidateTaskView.length ? candidateTaskView : [...candidateRecords, ...(rawDigest.recent_candidates || [])],
    tool_timeline: firstNonEmptyArray(factorSnapshot.tool_timeline, rawDigest.tool_timeline),
  };
  digest.latest_llm_step = decisionView && Object.keys(decisionView).length
    ? decisionView
    : runtime.latest_step || rawDigest.latest_llm_step || research_steps[0] || {};
  return digest;
}

function liveResearchDigest() {
  const factorConsole = serviceOutputs(state.factorConsole);
  if (!researchConsoleIsLive()) return offlineResearchSnapshotDigest();
  const rawDigest = factorConsole.live_research_digest || {};
  const runView = factorConsole.run_view || serviceOutputs(state.factorRunView);
  const runtime = factorConsole.runtime_view || rawDigest.runtime_view || {};
  const decisionView = factorConsole.decision_view || rawDigest.decision_view || {};
  const candidateTaskView = firstNonEmptyArray(factorConsole.candidate_task_view, rawDigest.candidate_task_view);
  const toolTimeline = firstNonEmptyArray(factorConsole.tool_timeline, rawDigest.tool_timeline);
  const applyQuantgptRunningOverride = (digest) => {
    const qgpt = digest.quantgpt_task_summary || factorConsole.quantgpt_task_summary || {};
    const staleIndicator = factorConsole.stale_quantgpt_tasks || runView.stale_quantgpt_tasks || {};
    const staleTaskIds = new Set((staleIndicator.tasks || []).map((task) => text(task.task_id || task.id, "")).filter(Boolean));
    const runningTasks = (qgpt.running_tasks || []).filter((task) => !staleTaskIds.has(text(task.task_id || task.id, "")));
    const runningCount = runningTasks.length;
    if (runningCount <= 0) return digest;
    const task = runningTasks[0] || qgpt.latest_task || {};
    const taskTs = parseIso(task.created_at || task.updated_at || task.completed_at);
    const latestStep = researchSteps()[0] || {};
    const latestStepTs = parseIso(latestStep.ts || latestStep.created_at);
    if (latestStepTs && taskTs && latestStepTs.getTime() >= taskTs.getTime()) {
      return digest;
    }
    const taskType = task.task_type || "mcp_tool";
    const phaseMap = {
      score: "Quick Score",
      backtest: "Deep Validation",
      anti_overfit: "Anti-overfit",
      adversarial_validation: "Adversarial Validation",
      novelty_check: "Novelty Check",
      quality_gate: "Quality Gate",
    };
    return {
      ...digest,
      status: "running_mcp_tools",
      current_phase: phaseMap[taskType] || "QuantGPT MCP",
      current_action: `QuantGPT 正在运行 ${taskType}${task.expression ? `：${task.expression}` : ""}`,
      active_task_count: Math.max(Number(digest.active_task_count || 0), runningCount),
      quantgpt_task_summary: qgpt,
      updated_at: task.created_at || task.updated_at || digest.updated_at,
    };
  };
  const digest = {
    ...rawDigest,
    ...runtime,
    source: "live_console",
    is_live: true,
    runtime_view: runtime,
    decision_view: decisionView,
    run_view: runView,
    candidate_task_view: candidateTaskView,
    tool_timeline: toolTimeline,
  };
  digest.run_id = runtime.run_id || rawDigest.run_id || state.lastRunId || "";
  digest.round_id = runtime.round_id || rawDigest.round_id || decisionView.round_id || researchSteps()[0]?.round_id || "";
  digest.stage = runtime.stage || rawDigest.stage || decisionView.stage || researchSteps()[0]?.stage || "";
  digest.status = runtime.status || rawDigest.status || factorConsole.status;
  digest.current_phase = runtime.current_phase || researchStepTitle(decisionView) || rawDigest.current_phase || "Research";
  digest.current_action = runtime.current_action || runtime.next_action || decisionView.next || decisionView.decision || decisionView.summary || rawDigest.current_action || "";
  digest.updated_at = runtime.updated_at || decisionView.updated_at || decisionView.ts || rawDigest.updated_at;
  digest.latest_llm_step = decisionView && Object.keys(decisionView).length
    ? decisionView
    : runtime.latest_step || rawDigest.latest_llm_step || researchSteps()[0] || {};
  digest.candidate_records = firstNonEmptyArray(factorConsole.candidate_records, rawDigest.candidate_records);
  digest.recent_candidates = candidateTaskView.length
    ? candidateTaskView
    : [
      ...digest.candidate_records,
      ...(rawDigest.recent_candidates || []),
    ];
  const steps = researchSteps();
  const latestStep = steps[0] || {};
  const latestStepTs = parseIso(latestStep.ts || latestStep.created_at);
  const digestTs = parseIso(digest.updated_at);
  if (latestStepTs && (!digestTs || latestStepTs.getTime() > digestTs.getTime())) {
    const { transition: latestTransition } = researchStepTransition(latestStep);
    digest.current_phase = researchStepTitle(latestStep);
    digest.current_action = latestTransition.next_action || latestStep.next_action || latestStep.decision || latestStep.summary || digest.current_action;
    digest.updated_at = latestStep.ts || latestStep.created_at;
    digest.event_count = Math.max(Number(digest.event_count || 0), steps.length);
    digest.latest_llm_step = latestStep;
    digest.status = "research_step_updated";
  }
  return applyQuantgptRunningOverride(digest);
}

function researchSteps() {
  const factorConsole = serviceOutputs(state.factorConsole);
  if (researchConsoleIsLive()) {
    const digest = factorConsole.live_research_digest || {};
    return firstNonEmptyArray(digest.research_steps, factorConsole.research_steps);
  }
  return offlineResearchSnapshotDigest().research_steps || [];
}

function latestLlmOutput() {
  const digest = liveResearchDigest();
  const steps = researchSteps();
  const latest = digest.latest_llm_step && Object.keys(digest.latest_llm_step).length
    ? digest.latest_llm_step
    : steps[0] || {};
  return latest;
}

function researchModeBadge() {
  const latest = latestLlmOutput();
  const transition = latest?.stage_transition || {};
  const tags = Array.isArray(latest?.tags) ? latest.tags.map((tag) => String(tag).toLowerCase()) : [];
  const isOrchestrator = transition.mode === "orchestrator" || tags.includes("orchestrator");
  if (isOrchestrator) {
    const traceModel = orchestratorTraces().slice().reverse()
      .map((trace) => text(trace?.llm_model).trim())
      .find(Boolean);
    const rawModel = text(
      transition.llm_model
        || latest?.llm_model
        || latest?._orchestrator_llm_model
        || latest?.extra?.llm_model
        || traceModel,
    ).trim();
    const modelLabel = rawModel
      ? rawModel.replace(/^deepseek-v4-pro$/i, "DeepSeek v4 Pro").replace(/^deepseek-v4-flash$/i, "DeepSeek v4 Flash")
      : "DeepSeek · 待 trace 确认";
    return {
      label: `Orchestrator · ${modelLabel}`,
      title: `run=${text(latest.run_id || "暂无")} trace=${text(transition.llm_trace_id || "最近 trace")}${traceModel ? ` model=${traceModel}` : ""}`,
      tone: "ok",
    };
  }
  return {
    label: "Codex MCP · 调试",
    title: "显式人工调试/复核路径；默认生产路径为 ORCH",
    tone: "subtle",
  };
}

function orchestratorOutputs(payload) {
  return serviceOutputs(payload);
}

function isCurrentOrchestratorMode() {
  const factorConsole = serviceOutputs(state.factorConsole);
  const digest = liveResearchDigest();
  const runtime = factorConsole.runtime_view || digest.runtime_view || {};
  const latest = latestLlmOutput();
  const transition = latest?.stage_transition || runtime.stage_transition || {};
  const tags = Array.isArray(latest?.tags) ? latest.tags.map((tag) => String(tag).toLowerCase()) : [];
  const mode = String(
    transition.mode
      || runtime.orchestration_mode
      || runtime.mode
      || latest?.extra?.source
      || ""
  ).toLowerCase();
  return mode === "orchestrator" || tags.includes("orchestrator");
}

function currentRunId() {
  const factorConsole = serviceOutputs(state.factorConsole);
  const digest = liveResearchDigest();
  const runtime = factorConsole.runtime_view || digest.runtime_view || {};
  return runtime.run_id || digest.run_id || latestLlmOutput()?.run_id || state.lastRunId || "";
}

function orchestratorTraces() {
  const runId = currentRunId();
  const traces = orchestratorOutputs(state.orchestratorTraces).traces || [];
  return runId ? traces.filter((trace) => !trace.run_id || trace.run_id === runId) : traces;
}

function orchestratorEvents() {
  const runId = currentRunId();
  const events = orchestratorOutputs(state.orchestratorEvents).events || [];
  return runId ? events.filter((event) => !event.run_id || event.run_id === runId) : events;
}

function modelOrchestratorActiveJob() {
  const status = serviceOutputs(state.modelOrchestratorStatus);
  const model = serviceOutputs(state.modelStatus);
  return status.active_job || (model.orchestrator || {}).active_job || {};
}

function modelOrchestratorRunId() {
  const status = serviceOutputs(state.modelOrchestratorStatus);
  const job = modelOrchestratorActiveJob();
  const traces = [
    ...(serviceOutputs(state.modelResearchOrchTraces).traces || []),
    ...(serviceOutputs(state.modelResearchMcpTraces).traces || []),
    ...(serviceOutputs(state.modelOrchestratorTraces).traces || []),
  ];
  const events = [
    ...(serviceOutputs(state.modelOrchestratorEvents).events || []),
    ...(serviceOutputs(state.modelOrchestratorStatus).events_tail || []),
  ];
  const latestTrace = [...traces].sort((a, b) => traceTsValue(b) - traceTsValue(a))[0] || {};
  const latestEvent = [...events].sort((a, b) => traceTsValue(b) - traceTsValue(a))[0] || {};
  return job.job_id
    || job.run_id
    || status.latest_job?.job_id
    || status.latest_job?.run_id
    || latestTrace.job_id
    || latestTrace.run_id
    || latestEvent.job_id
    || latestEvent.run_id
    || "";
}

function modelOrchestratorSessionId() {
  const status = serviceOutputs(state.modelOrchestratorStatus);
  const model = serviceOutputs(state.modelStatus);
  const activeSession = status.active_session || (model.orchestrator || {}).active_session || model.active_session || model.live_session || {};
  return activeSession.session_id
    || modelOrchestratorActiveJob().session_id
    || status.latest_session?.session_id
    || (model.orchestrator || {}).latest_session?.session_id
    || "";
}

function modelOrchestratorTraces() {
  const runId = modelOrchestratorRunId();
  const sessionId = modelOrchestratorSessionId();
  const traces = serviceOutputs(state.modelResearchOrchTraces).traces || orchestratorOutputs(state.modelOrchestratorTraces).traces || [];
  const filtered = (runId || sessionId)
    ? traces.filter((trace) => (
      (!trace.job_id && !trace.run_id && !trace.session_id)
      || trace.job_id === runId
      || trace.run_id === runId
      || trace.session_id === sessionId
    ))
    : traces;
  return dedupeModelRows(filtered).sort((a, b) => traceTsValue(b) - traceTsValue(a));
}

function modelOrchestratorEvents() {
  const runId = modelOrchestratorRunId();
  const sessionId = modelOrchestratorSessionId();
  const events = [
    ...(orchestratorOutputs(state.modelOrchestratorEvents).events || []),
    ...(serviceOutputs(state.modelOrchestratorStatus).events_tail || []),
  ];
  const filtered = (runId || sessionId)
    ? events.filter((event) => (
      (!event.job_id && !event.run_id && !event.session_id)
      || event.job_id === runId
      || event.run_id === runId
      || event.session_id === sessionId
    ))
    : events;
  return dedupeModelRows(filtered).sort((a, b) => traceTsValue(b) - traceTsValue(a));
}

function modelMcpTraces() {
  const runId = modelOrchestratorRunId();
  const sessionId = modelOrchestratorSessionId();
  const traces = serviceOutputs(state.modelResearchMcpTraces).traces || [];
  const filtered = (runId || sessionId)
    ? traces.filter((trace) => (
      (!trace.job_id && !trace.run_id && !trace.session_id)
      || trace.job_id === runId
      || trace.run_id === runId
      || trace.session_id === sessionId
    ))
    : traces;
  return dedupeModelRows(filtered).sort((a, b) => traceTsValue(b) - traceTsValue(a));
}

function dedupeModelRows(rows) {
  const seen = new Set();
  return (rows || []).filter((row) => {
    const key = row?.event_id || row?.trace_id || [row?.ts, row?.event_type, row?.stage, row?.job_id || row?.run_id].join("|");
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function normalizeTraceForDisplay(trace) {
  const summary = trace?.result_summary || {};
  return {
    traceId: trace?.trace_id || "",
    stage: trace?.stage || summary.stage || trace?.checkpoint || "trace",
    checkpoint: trace?.checkpoint || "",
    eventType: trace?.event_type || "",
    decision: summary.decision || "",
    nextAction: summary.next_action || "",
    nextStage: summary.next_stage || "",
    judgment: summary.judgment || "",
    why: summary.why || "",
    candidateCount: summary.candidate_count || 0,
    candidateIds: summary.candidate_ids || [],
    candidateExpressions: summary.candidate_expressions || [],
  };
}

function traceTsValue(trace) {
  const date = parseIso(trace?.ts);
  return date ? date.getTime() : 0;
}

function traceGroupKey(trace) {
  return trace?.trace_id
    || [trace?.run_id, trace?.round_id, trace?.stage || trace?.checkpoint].filter(Boolean).join(":")
    || `trace-${traceTsValue(trace)}`;
}

function preferTraceForDetail(traces) {
  return [...(traces || [])].reverse().find((trace) => (
    trace?.result_summary || trace?.result || trace?.event_type === "llm_result" || trace?.error || trace?.error_type
  )) || [...(traces || [])].reverse()[0] || {};
}

function groupOrchestratorTraces(traces) {
  const groups = new Map();
  (traces || []).forEach((trace) => {
    const key = traceGroupKey(trace);
    const group = groups.get(key) || {
      groupId: key,
      traces: [],
      traceId: trace?.trace_id || key,
      runId: trace?.run_id || "",
      roundId: trace?.round_id || "",
      stage: trace?.stage || trace?.checkpoint || "trace",
      latestTs: 0,
    };
    group.traces.push(trace);
    group.traceId = group.traceId || trace?.trace_id || key;
    group.runId = group.runId || trace?.run_id || "";
    group.roundId = group.roundId || trace?.round_id || "";
    group.stage = group.stage || trace?.stage || trace?.checkpoint || "trace";
    group.latestTs = Math.max(group.latestTs, traceTsValue(trace));
    groups.set(key, group);
  });
  return [...groups.values()].map((group) => {
    const sorted = [...group.traces].sort((a, b) => traceTsValue(a) - traceTsValue(b));
    const primaryTrace = preferTraceForDetail(sorted);
    const requestTrace = sorted.find((trace) => trace?.event_type === "llm_request") || null;
    const resultTrace = [...sorted].reverse().find((trace) => trace?.event_type === "llm_result" || trace?.result_summary || trace?.result) || null;
    return {
      ...group,
      traces: sorted,
      primaryTrace,
      requestTrace,
      resultTrace,
      stage: primaryTrace?.stage || group.stage,
      roundId: primaryTrace?.round_id || group.roundId,
      runId: primaryTrace?.run_id || group.runId,
      latestTs: group.latestTs || traceTsValue(primaryTrace),
      hasError: sorted.some((trace) => trace?.error || trace?.error_type || /error|failed/i.test(String(trace?.event_type || ""))),
      eventTypes: [...new Set(sorted.map((trace) => trace?.event_type).filter(Boolean))],
    };
  }).sort((a, b) => b.latestTs - a.latestTs);
}

function nestedFirstValue(source, keys, maxDepth = 6) {
  if (!source || maxDepth < 0) return undefined;
  if (Array.isArray(source)) {
    for (const item of source) {
      const found = nestedFirstValue(item, keys, maxDepth - 1);
      if (found !== undefined) return found;
    }
    return undefined;
  }
  if (typeof source !== "object") return undefined;
  for (const key of keys) {
    if (source[key] !== undefined && source[key] !== null && source[key] !== "") {
      return source[key];
    }
  }
  for (const value of Object.values(source)) {
    const found = nestedFirstValue(value, keys, maxDepth - 1);
    if (found !== undefined) return found;
  }
  return undefined;
}

function compactTraceValue(value, maxLength = 480) {
  if (value === undefined || value === null || value === "") return "";
  if (Array.isArray(value)) {
    return value.map((item) => compactTraceValue(item, Math.max(80, Math.floor(maxLength / Math.max(1, value.length))))).filter(Boolean).join("；");
  }
  if (typeof value === "object") return clip(JSON.stringify(value, null, 2), maxLength);
  return clip(String(value), maxLength);
}

function stageHasActionGuard(stage) {
  return [
    "score_review",
    "novelty_review",
    "deep_validation_review",
    "import_gate_review",
    "import_review",
  ].includes(String(stage || "").toLowerCase());
}

function traceGuardSummary(traceOrGroup, linkedEvents = []) {
  const traces = Array.isArray(traceOrGroup?.traces) ? traceOrGroup.traces : [traceOrGroup].filter(Boolean);
  const sources = [];
  traces.forEach((trace) => {
    sources.push(trace?.result, trace?.payload, trace?.result_summary, trace);
  });
  (linkedEvents || []).forEach((event) => {
    sources.push(event?.advice, event?.stage_transition, event?.llm_result, event);
  });
  const allowed = sources.map((item) => nestedFirstValue(item, ["allowed_actions"], 5)).find((item) => item !== undefined);
  const blocked = sources.map((item) => nestedFirstValue(item, ["blocked_actions"], 5)).find((item) => item !== undefined);
  const codeAction = sources.map((item) => nestedFirstValue(item, ["code_action", "action", "next_action"], 5)).find((item) => item !== undefined);
  const gateReady = sources.map((item) => nestedFirstValue(item, ["code_gate_ready", "gate_ready"], 5)).find((item) => item !== undefined);
  const stage = traceOrGroup?.stage || traceOrGroup?.primaryTrace?.stage || traces[0]?.stage || "";
  return { allowed, blocked, codeAction, gateReady, applies: stageHasActionGuard(stage) };
}

function traceLinkedEvents(trace, events = orchestratorEvents()) {
  const traceId = trace?.trace_id || "";
  const stage = trace?.stage || "";
  const roundId = trace?.round_id || "";
  const linked = events.filter((event) => {
    const transition = event.stage_transition || {};
    return (traceId && transition.llm_trace_id === traceId)
      || (stage && event.stage === stage && (!roundId || event.round_id === roundId));
  });
  return linked.length ? linked.slice(-8) : events.slice(-8);
}

function traceLinkedResearchSteps(trace) {
  const traceId = trace?.trace_id || "";
  const stage = trace?.stage || "";
  const roundId = trace?.round_id || "";
  return researchSteps().filter((step) => {
    const { transition } = researchStepTransition(step);
    return (traceId && transition.llm_trace_id === traceId)
      || (stage && step.stage === stage && (!roundId || step.round_id === roundId));
  }).slice(0, 5);
}

function renderTraceJsonBlock(title, payload, options = {}) {
  if (payload === undefined || payload === null || payload === "") return "";
  const open = options.open ? " open" : "";
  const content = typeof payload === "string" ? payload : JSON.stringify(payload, null, 2);
  return `
    <details class="orch-trace-raw"${open}>
      <summary>${escapeHtml(title)}</summary>
      <pre>${escapeHtml(content)}</pre>
    </details>
  `;
}

function renderOrchestratorTraceWorkspace() {
  const container = document.getElementById("orch-trace-board");
  if (!container) return;
  const modeOk = isCurrentOrchestratorMode();
  const runId = currentRunId();
  const traces = orchestratorTraces();
  const events = orchestratorEvents();
  const traceOutputs = orchestratorOutputs(state.orchestratorTraces);
  const eventOutputs = orchestratorOutputs(state.orchestratorEvents);
  if (!modeOk) {
    container.innerHTML = `
      <div class="empty-state orch-trace-empty">
        当前是显式 Codex MCP 调试模式，因此没有 Orchestrator LLM trace。生产 ORCH 模式会在这里展示 DeepSeek 请求、响应、event 和 research_step 对照信息。
      </div>
    `;
    return;
  }
  if (!traces.length) {
    container.innerHTML = `
      <div class="empty-state orch-trace-empty">
        当前 run ${escapeHtml(text(runId, "暂无"))} 还没有 LLM trace。Orchestrator 完成第一个 DeepSeek checkpoint 后会自动写入。
      </div>
    `;
    return;
  }
  const traceGroups = groupOrchestratorTraces(traces);
  const defaultGroup = traceGroups.find((group) => group.resultTrace || group.primaryTrace?.result_summary || group.primaryTrace?.result) || traceGroups[0];
  const selectedGroup = traceGroups.find((group) => group.groupId === state.activeOrchestratorTraceId || group.traceId === state.activeOrchestratorTraceId) || defaultGroup;
  const selected = selectedGroup.primaryTrace || {};
  const selectedRequest = selectedGroup.requestTrace;
  const selectedResult = selectedGroup.resultTrace || selected;
  state.activeOrchestratorTraceId = selectedGroup.groupId || selected.trace_id || "";
  const detail = normalizeTraceForDisplay(selected);
  const redaction = selected.redaction_status || {};
  const errorCount = traces.filter((trace) => trace.error || trace.error_type || /error|failed/i.test(String(trace.event_type || ""))).length;
  const redactionWarnings = traces.filter((trace) => trace.redaction_status?.redaction_warning).length;
  const selectedEvents = traceLinkedEvents(selected, events);
  const guard = traceGuardSummary(selectedGroup, selectedEvents);
  const selectedSteps = traceLinkedResearchSteps(selected);
  const allowedText = compactTraceValue(guard.allowed, 320) || (guard.applies ? "等待 code advice/event 投影" : "本阶段无代码动作守卫");
  const blockedText = compactTraceValue(guard.blocked, 320) || (guard.applies ? "无阻断动作" : "本阶段无代码动作守卫");
  container.innerHTML = `
    <div class="orch-trace-kpis">
      <span>mode <b>orchestrator</b></span>
      <span>run <b>${escapeHtml(text(runId, "--"))}</b></span>
      <span>stages <b>${escapeHtml(text(traceGroups.length, "0"))}</b></span>
      <span>trace rows <b>${escapeHtml(text(traceOutputs.count ?? traces.length, "0"))}</b></span>
      <span>events <b>${escapeHtml(text(eventOutputs.count ?? events.length, "0"))}</b></span>
      <span>errors <b>${escapeHtml(text(errorCount, "0"))}</b></span>
      <span>redaction <b>${escapeHtml(redactionWarnings ? `${redactionWarnings} warnings` : "clean")}</b></span>
    </div>
    <div class="orch-trace-layout">
      <aside class="orch-trace-list" aria-label="Orchestrator LLM traces">
        ${traceGroups.map((group) => {
          const trace = group.primaryTrace || {};
          const item = normalizeTraceForDisplay(trace);
          const active = group.groupId === selectedGroup.groupId;
          const tone = group.hasError ? " danger" : group.resultTrace ? " ok" : "";
          const badges = [
            group.requestTrace ? "request" : "",
            group.resultTrace ? "result" : "",
            group.traces.length > 1 ? `${group.traces.length} rows` : "",
          ].filter(Boolean);
          return `
            <button class="orch-trace-item${active ? " active" : ""}${tone}" type="button" data-orch-trace-id="${escapeHtml(group.groupId || group.traceId || "")}">
              <span>${escapeHtml(compactRoundLabel(group.roundId || group.runId) || "--")} · ${escapeHtml(item.stage)}</span>
              <strong>${escapeHtml(clip(item.decision || item.nextAction || item.eventType, 92))}</strong>
              <small>${escapeHtml(compactDateTime(trace.ts))} · ${escapeHtml(text(trace.llm_model || trace.llm_provider, "LLM"))}</small>
              <span class="orch-trace-item-badges">${badges.map((badge) => `<i>${escapeHtml(badge)}</i>`).join("")}</span>
            </button>
          `;
        }).join("")}
      </aside>
      <section class="orch-trace-detail">
        <div class="orch-trace-detail-head">
          <div>
            <p class="eyebrow">${escapeHtml(selectedGroup.eventTypes.join(" + ") || text(detail.eventType, "LLM Trace"))}</p>
            <h3>${escapeHtml(text(detail.stage, "trace"))}</h3>
            <small>${escapeHtml(text(selectedGroup.traceId || selected.trace_id, "--"))}</small>
          </div>
          <div class="orch-trace-meta">
            <span>${escapeHtml(text(selected.llm_model || selected.llm_provider, "DeepSeek"))}</span>
            <span>${escapeHtml(text(selected.elapsed_s !== undefined ? `${shortNumber(selected.elapsed_s, 2)}s` : "", "pending"))}</span>
            <span>${escapeHtml(text(selected.payload_chars ? `${selected.payload_chars} chars` : "", "--"))}</span>
            <span>${escapeHtml(text(`${selectedGroup.traces.length} trace row${selectedGroup.traces.length > 1 ? "s" : ""}`, "--"))}</span>
          </div>
        </div>
        <div class="orch-trace-decision-grid">
          <article>
            <span>decision</span>
            <strong>${escapeHtml(text(detail.decision, "--"))}</strong>
          </article>
          <article>
            <span>next</span>
            <strong>${escapeHtml([detail.nextStage, detail.nextAction].filter(Boolean).join(" / ") || "--")}</strong>
          </article>
          <article>
            <span>guard</span>
            <strong>${escapeHtml(compactTraceValue(guard.codeAction || guard.gateReady || "code evidence required", 140))}</strong>
          </article>
          <article>
            <span>redaction</span>
            <strong>${escapeHtml(redaction.redaction_warning || `${text(redaction.policy, "policy")} · ${redaction.redacted ? "redacted" : "clean"}`)}</strong>
          </article>
        </div>
        <div class="orch-trace-copy-grid">
          <article>
            <span>judgment</span>
            <p>${escapeHtml(text(detail.judgment, selected.error || selected.raw_response_preview || "--"))}</p>
          </article>
          <article>
            <span>why</span>
            <p>${escapeHtml(text(detail.why, "--"))}</p>
          </article>
        </div>
        <div class="orch-trace-guard-grid">
          <div><span>allowed_actions</span><p>${escapeHtml(allowedText)}</p></div>
          <div><span>blocked_actions</span><p>${escapeHtml(blockedText)}</p></div>
        </div>
        ${detail.candidateCount || detail.candidateExpressions.length ? `
          <div class="orch-candidate-strip">
            <span>candidates ${escapeHtml(text(detail.candidateCount, detail.candidateExpressions.length))}</span>
            ${detail.candidateExpressions.map((expression, index) => `
              <code title="${escapeHtml(expression || "")}">${escapeHtml(text(detail.candidateIds[index], `c${index + 1}`))}: ${escapeHtml(clip(expression || "", 96))}</code>
            `).join("")}
          </div>
        ` : ""}
        <div class="orch-trace-context-grid">
          <section>
            <h4>Linked Events</h4>
            <div class="orch-event-list">
              ${selectedEvents.map((event) => `
                <article>
                  <span>${escapeHtml(compactDateTime(event.ts))}</span>
                  <strong>${escapeHtml(text(event.stage || event.event_type, "--"))} · ${escapeHtml(text(event.event_type || event.checkpoint, "--"))}</strong>
                  <p>${escapeHtml(clip(event.summary || event.decision || "", 260))}</p>
                </article>
              `).join("") || `<div class="empty-state">暂无关联 event。</div>`}
            </div>
          </section>
          <section>
            <h4>Research Steps</h4>
            <div class="orch-event-list">
              ${selectedSteps.map((step) => {
                const { transition } = researchStepTransition(step);
                return `
                  <article>
                    <span>${escapeHtml(compactDateTime(step.ts || step.created_at))}</span>
                    <strong>${escapeHtml(text(step.stage, "--"))} → ${escapeHtml(text(transition.next_stage, "pending"))}</strong>
                    <p>${escapeHtml(clip(researchStepSummary(step), 300))}</p>
                  </article>
                `;
              }).join("") || `<div class="empty-state">暂无关联 research_step 投影。</div>`}
            </div>
          </section>
        </div>
        ${renderTraceJsonBlock("LLM result JSON", selectedResult?.result || selectedResult?.result_summary, { open: true })}
        ${renderTraceJsonBlock("User prompt / context pack", selectedRequest?.user_prompt || selectedRequest?.payload || selected.user_prompt || selected.payload)}
        ${renderTraceJsonBlock("System prompt", selectedRequest?.system_prompt || selected.system_prompt)}
        ${renderTraceJsonBlock("Trace rows compact", selectedGroup.traces)}
      </section>
    </div>
  `;
  container.querySelectorAll("[data-orch-trace-id]").forEach((button) => {
    button.addEventListener("click", () => {
      state.activeOrchestratorTraceId = button.dataset.orchTraceId || "";
      renderOrchestratorTraceWorkspace();
    });
  });
}

function commandPreflightOutputs() {
  return serviceOutputs(state.factorResearchPreflight);
}

function commandControlOutputs() {
  return serviceOutputs(state.factorResearchControl);
}

function activeResearchRunIdForRequest() {
  const factorConsole = serviceOutputs(state.factorConsole);
  const runtime = factorConsole.runtime_view || {};
  const digest = factorConsole.live_research_digest || {};
  const control = commandControlOutputs();
  return text(runtime.run_id || digest.run_id || control.run_id, "").trim();
}

function orchestratorEventsUrl() {
  const runId = activeResearchRunIdForRequest();
  const runQuery = runId ? `&run_id=${encodeURIComponent(runId)}` : "";
  return `/factor/research/orchestrator-events?limit=140&include_payload=false&include_history=false${runQuery}`;
}

function orchestratorTracesUrl({ includePayload = false } = {}) {
  const runId = activeResearchRunIdForRequest();
  const runQuery = runId ? `&run_id=${encodeURIComponent(runId)}` : "";
  return `/factor/research/orchestrator-traces?limit=60&include_payload=${includePayload ? "true" : "false"}&include_history=false${runQuery}`;
}

function orchestratorTraceWorkspaceVisible() {
  return researchPanelIsVisible() && state.activeWorkspace === "orch-trace";
}

const COMMAND_ORCHESTRATOR_LLM_MODELS = Object.freeze({
  "deepseek-v4-pro": "V4 Pro",
  "deepseek-v4-flash": "V4 Flash",
});

function normalizeCommandLlmModel(value, fallback = "deepseek-v4-pro") {
  const raw = String(value || "").trim().toLowerCase();
  const normalized = raw === "deepseek-v4" ? "deepseek-v4-pro" : raw;
  return COMMAND_ORCHESTRATOR_LLM_MODELS[normalized] ? normalized : fallback;
}

function commandRunPinnedLlmModel(activeRun = {}, controlRunId = "") {
  const pinned = activeRun?.inputs?.llm_model || activeRun?.summary?.llm_model;
  if (pinned) return normalizeCommandLlmModel(pinned);
  const latest = latestLlmOutput() || {};
  const latestRunId = text(latest.run_id || "", "").trim();
  if (controlRunId && latestRunId && latestRunId !== controlRunId) return "";
  return normalizeCommandLlmModel(
    latest?.stage_transition?.llm_model
      || latest?.llm_model
      || latest?._orchestrator_llm_model,
    "",
  );
}

function commandLlmModelContractReady(defaults = {}) {
  const options = Array.isArray(defaults.llm_model_options) ? defaults.llm_model_options : [];
  return Object.keys(COMMAND_ORCHESTRATOR_LLM_MODELS).every((model) => options.includes(model));
}

function syncCommandLlmModelControl({ activeRun = {}, controlRunId = "", controlState = "idle", defaults = {} } = {}) {
  const form = document.getElementById("orchestrator-command-form");
  const field = formField(form, "llm_model");
  if (!field) return;
  const runLocked = Boolean(controlRunId) && [
    "running", "pause_requested", "paused", "resume_requested", "stop_requested",
  ].includes(controlState);
  const contractReady = commandLlmModelContractReady(defaults);
  const locked = runLocked || !contractReady;
  const defaultModel = normalizeCommandLlmModel(defaults.llm_model);
  const pinnedModel = runLocked ? commandRunPinnedLlmModel(activeRun, controlRunId) : "";
  const selectedModel = normalizeCommandLlmModel(pinnedModel || field.value || defaultModel, defaultModel);
  field.value = selectedModel;
  document.querySelectorAll("[data-command-llm-model]").forEach((button) => {
    const active = button.dataset.commandLlmModel === selectedModel;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-checked", active ? "true" : "false");
    button.disabled = locked;
    button.title = runLocked
      ? `当前 run 已固定使用 ${COMMAND_ORCHESTRATOR_LLM_MODELS[selectedModel]}`
      : !contractReady
        ? "等待因子研究 API 在当前 run 结束后安全重载模型选择契约"
        : `新建 ORCH 任务首选 ${COMMAND_ORCHESTRATOR_LLM_MODELS[button.dataset.commandLlmModel] || button.dataset.commandLlmModel}`;
  });
  const note = document.getElementById("command-llm-model-note");
  if (note) {
    note.textContent = runLocked
      ? `当前 run 已锁定 ${COMMAND_ORCHESTRATOR_LLM_MODELS[selectedModel]}；暂停或继续不会换模`
      : !contractReady
        ? "后端模型选择契约待安全重载；当前禁止启动新任务"
        : "仅影响新建 ORCH 任务；失败回退沿用平台策略";
  }
}

function setCommandLlmModel(value) {
  const form = document.getElementById("orchestrator-command-form");
  const field = formField(form, "llm_model");
  const button = document.querySelector(`[data-command-llm-model="${CSS.escape(String(value || ""))}"]`);
  if (!field || button?.disabled) return;
  field.value = normalizeCommandLlmModel(value);
  field.dataset.userSelected = "true";
  syncCommandLlmModelControl({
    controlRunId: commandControlOutputs().run_id || "",
    controlState: commandControlOutputs().state || "idle",
    defaults: serviceOutputs(state.factorStatus).runtime_defaults || {},
  });
  setCommandMessage(`新建 ORCH 任务将首选 ${COMMAND_ORCHESTRATOR_LLM_MODELS[field.value]}。`, "ok");
}

function commandFormPayload({ configOnly = false } = {}) {
  const form = document.getElementById("orchestrator-command-form");
  if (!form) return {};
  const data = new FormData(form);
  const payload = {
    universe: data.get("universe"),
    start_date: data.get("start_date"),
    end_date: data.get("end_date"),
    holding_period: Number(data.get("holding_period")),
    benchmark: data.get("benchmark"),
    target_adopted: Number(data.get("target_adopted")),
    n_candidates: Number(data.get("n_candidates")),
    n_rounds: Number(data.get("n_rounds")),
    top_frac: Number(data.get("top_frac")),
    cost_rate: Number(data.get("cost_rate")),
  };
  if (configOnly) {
    const { start_date, end_date, ...configPayload } = payload;
    return configPayload;
  }
  return {
    ...payload,
    evaluation_mode: serviceOutputs(state.evaluationProfile).active_default_mode || "production",
    direction: data.get("direction") || "auto",
    n_groups: 5,
    orchestration_mode: "orchestrator",
    llm_model: normalizeCommandLlmModel(data.get("llm_model")),
    qgpt_url: commandPreflightOutputs().qgpt_url || serviceOutputs(state.factorStatus).runtime_defaults?.qgpt_url || "http://127.0.0.1:8003",
    neutralize_cap: data.get("neutralize_cap") === "on",
    neutralize_industry: false,
    submit_wq: data.get("submit_wq") === "on",
  };
}

function commandLaunchReceiptMismatches(requested = {}, accepted = {}) {
  const keys = [
    "direction", "universe", "start_date", "end_date", "holding_period", "benchmark",
    "target_adopted", "n_candidates", "n_rounds", "top_frac", "cost_rate",
    "neutralize_cap", "submit_wq", "orchestration_mode", "evaluation_mode", "llm_model",
  ];
  return keys.filter((key) => {
    if (!(key in accepted)) return true;
    return String(requested[key] ?? "") !== String(accepted[key] ?? "");
  });
}

function setCommandMessage(message = "", tone = "subtle") {
  const node = document.getElementById("command-message");
  if (!node) return;
  node.className = `command-message ${tone ? ` ${tone}` : ""}`;
  node.textContent = message || "";
  node.hidden = !message;
}

function renderCommandConsole() {
  const strip = document.getElementById("command-preflight-strip");
  const controlNote = document.getElementById("command-control-note");
  const startButton = document.getElementById("command-start-orchestrator");
  const pauseButton = document.getElementById("command-pause-orchestrator");
  const resumeButton = document.getElementById("command-resume-orchestrator");
  const stopButton = document.getElementById("command-stop-orchestrator");
  if (!strip) return;
  const outputs = commandPreflightOutputs();
  const control = commandControlOutputs();
  const allowedActions = new Set(control.allowed_actions || []);
  const defaults = serviceOutputs(state.factorStatus).runtime_defaults || outputs.runtime_defaults || {};
  const qgpt = outputs.readiness?.quantgpt_api || {};
  const activeRun = outputs.active_orchestrator_run || control.active_job || {};
  const controlRunId = control.run_id || activeRun.run_id || "";
  const controlState = control.state || (activeRun.run_id ? "running" : "idle");
  const roundId = control.round_id || "";
  const runDateMatch = controlRunId.match(/^fr_(\d{4})(\d{2})(\d{2})_/i);
  const runLabel = runDateMatch ? `${runDateMatch[2]}${runDateMatch[3]}` : (controlRunId ? clip(controlRunId, 12) : "无");
  const roundLabel = /:stop$/i.test(roundId) ? "STOP" : (compactRoundLabel(roundId) || "--");
  const stateLabels = {
    idle: "空闲",
    running: "运行中",
    pause_requested: "暂停处理中",
    paused: "已暂停",
    resume_requested: "恢复处理中",
    stop_requested: "停止处理中",
    completed: "已结束",
    blocked: "阻断",
  };
  const stale = outputs.stale_or_interrupted || {};
  const preflightKnown = Object.keys(outputs).length > 0;
  const chip = (label, value, tone = "subtle", title = "") => `
    <span class="command-status-chip ${tone}" title="${escapeHtml(title || value || "")}">
      ${escapeHtml(label)} <b>${escapeHtml(text(value, "--"))}</b>
    </span>
  `;
  const statusRow = (label, chips) => `
    <div class="command-status-row">
      <span class="command-status-row-label">${escapeHtml(label)}</span>
      <div class="command-status-items">${chips.join("")}</div>
    </div>
  `;
  const runStatusChips = [
    chip("Run", runLabel, controlRunId ? "warn" : "ok", controlRunId),
    chip("Round", roundLabel, roundLabel === "STOP" ? "warn" : "subtle", roundId),
    chip("Stage", control.stage ? researchStageTitle(control.stage) : "--", "subtle", control.stage || ""),
    chip("状态", stateLabels[controlState] || controlState, ["running", "resume_requested"].includes(controlState) ? "ok" : ["blocked", "stop_requested"].includes(controlState) ? "danger" : "warn"),
    ...(stale.stale ? [chip("Interrupted", text(stale.reason, "stale"), "warn")] : []),
  ];
  const readinessChips = [
    chip("API", preflightKnown ? "OK" : "待检测", preflightKnown ? "ok" : "subtle"),
    chip("QuantGPT", qgpt.reachable ? "OK" : preflightKnown ? "不可达" : "待检测", qgpt.reachable ? "ok" : preflightKnown ? "danger" : "subtle", qgpt.error || outputs.doctor_hint || ""),
    chip("Preflight", outputs.can_start === false ? "阻断" : "通过", outputs.can_start === false ? "danger" : "ok", (outputs.blocking_errors || []).join(", ")),
  ];
  strip.innerHTML = [
    statusRow("当前运行", runStatusChips),
    statusRow("启动检查", readinessChips),
  ].join("");
  const canStart = allowedActions.has("start") || controlState === "idle";
  const canPause = allowedActions.has("pause");
  const canResume = allowedActions.has("resume");
  const canStop = allowedActions.has("stop");
  const llmModelContractReady = commandLlmModelContractReady(defaults);
  const stateNotes = {
    running: "研究运行中：可以暂停并保留恢复检查点，也可以停止本次 run。",
    pause_requested: "暂停请求已提交：等待当前安全检查点完成后，可继续或停止。",
    paused: "研究已暂停：可以从当前检查点继续，或停止本次 run。",
    resume_requested: "继续请求已提交：Orchestrator 正在恢复当前 run。",
    stop_requested: "停止请求已提交：等待当前安全检查点结束。",
    completed: "上一轮研究已结束：可以启动新 run；其余按钮只在运行或暂停状态启用。",
    blocked: "研究处于阻断状态：请先根据状态提示处理问题，再继续或启动新 run。",
    idle: "当前没有运行中的研究：请核对参数和预检状态后启动新 run。",
  };
  if (controlNote) {
    controlNote.textContent = stateNotes[controlState] || `当前控制状态：${controlState}。可用操作由后台状态机决定。`;
    controlNote.dataset.state = controlState;
  }
  if (startButton) {
    startButton.hidden = false;
    startButton.disabled = !canStart || !llmModelContractReady;
    startButton.textContent = "启动新研究";
    startButton.title = !llmModelContractReady
      ? "等待因子研究 API 安全重载模型选择契约"
      : canStart ? "执行预检并启动新的 ORCH run" : "已有研究正在运行或等待状态切换";
  }
  if (pauseButton) {
    pauseButton.hidden = false;
    pauseButton.disabled = !canPause;
    pauseButton.title = canPause ? "在安全检查点暂停并保存恢复状态" : "仅研究运行时可暂停";
  }
  if (resumeButton) {
    resumeButton.hidden = false;
    resumeButton.disabled = !canResume;
    resumeButton.title = canResume ? "从已保存的检查点继续同一 run" : "仅研究暂停后可继续";
  }
  if (stopButton) {
    stopButton.hidden = false;
    stopButton.disabled = !canStop;
    stopButton.title = canStop ? "停止当前 run，不再自动继续" : "当前没有可停止的运行中或暂停 run";
  }
  syncCommandLlmModelControl({ activeRun, controlRunId, controlState, defaults });
}

async function refreshCommandPreflight() {
  const [preflight, control] = await Promise.all([
    getJsonSafe("/factor/research/preflight"),
    getJsonSafe("/factor/research/control"),
  ]);
  state.factorResearchPreflight = preflight;
  state.factorResearchControl = control;
  renderCommandConsole();
  return commandPreflightOutputs();
}

async function submitCommandControl(action) {
  const control = commandControlOutputs();
  const runId = control.run_id || commandPreflightOutputs().active_orchestrator_run?.run_id || state.lastRunId || "";
  if (!runId) {
    setCommandMessage("当前没有可操作的 ORCH run。", "warn");
    return;
  }
  if (action === "stop" && !window.confirm(`确认停止当前研究 ${runId}？如果只是暂时离开，请选择“暂停研究”。`)) {
    return;
  }
  const labels = { pause: "暂停", resume: "继续", stop: "结束" };
  const button = document.getElementById(`command-${action}-orchestrator`);
  if (button) button.disabled = true;
  setCommandMessage(`正在请求${labels[action] || action}：${runId}`, "subtle");
  try {
    const result = await postJson(`/factor/research/${action}`, {
      run_id: runId,
      reason: `web_gui_operator_${action}`,
    });
    if (!result?.ok) {
      setCommandMessage(`${labels[action] || action}失败：${result?.err || "control request failed"}`, "danger");
      return;
    }
    const actual = result.outputs?.actual_state || result.outputs?.status || "accepted";
    setCommandMessage(`${labels[action] || action}请求已接收，当前状态：${actual}`, "ok");
    await refreshCommandPreflight();
    await refreshResearchLive({ force: true });
  } finally {
    renderCommandConsole();
  }
}

async function saveCommandDefaults() {
  const button = document.getElementById("command-save-defaults");
  if (button) button.disabled = true;
  setCommandMessage("正在保存默认配置...", "subtle");
  try {
    const result = await postJson("/factor/research/config-defaults", commandFormPayload({ configOnly: true }));
    if (!result?.ok) {
      setCommandMessage(`保存失败：${result?.err || "config update failed"}`, "danger");
      return;
    }
    state.factorStatus = await getJsonSafe("/factor/status");
    applyResearchRuntimeDefaults();
    setCommandMessage("默认配置已保存；后续启动会读取新的 runtime defaults。", "ok");
  } finally {
    if (button) button.disabled = false;
  }
}

async function submitCommandOrchestrator() {
  const button = document.getElementById("command-start-orchestrator");
  if (button) button.disabled = true;
  setCommandMessage("正在执行启动预检...", "subtle");
  try {
    const preflight = await refreshCommandPreflight();
    const activeRun = preflight.active_orchestrator_run || {};
    if (activeRun.run_id) {
      state.lastRunId = activeRun.run_id;
      setCommandMessage(`已接管当前 Orchestrator：${activeRun.run_id}`, "ok");
      setWorkspace("run");
      await refreshResearchLive({ force: true });
      return;
    }
    if (!preflight.can_start) {
      const reason = (preflight.blocking_errors || []).join(", ") || preflight.doctor_hint || "preflight_blocked";
      setCommandMessage(`暂不能启动：${reason}`, "danger");
      return;
    }
    const requested = commandFormPayload();
    const result = await postJson("/factor/research/start", requested);
    if (!result?.ok) {
      setCommandMessage(`启动失败：${result?.err || "research start failed"}`, "danger");
      return;
    }
    const receiptMismatches = commandLaunchReceiptMismatches(requested, result.inputs || {});
    if (receiptMismatches.length) {
      setCommandMessage(`研究已启动，但启动回执参数不一致：${receiptMismatches.join(", ")}。请停止并检查后台版本。`, "danger");
      await refreshCommandPreflight();
      return;
    }
    if (result.outputs?.run_id) {
      state.lastRunId = result.outputs.run_id;
    }
    setCommandMessage(`Orchestrator 已按本页参数启动：${result.outputs?.run_id || "running"}`, "ok");
    setWorkspace("run");
    await refreshResearchLive({ force: true });
  } finally {
    if (button) button.disabled = false;
    renderCommandConsole();
  }
}

// One display contract for every model-judgment stage. The raw stage code is
// still preserved in ids, metadata, logs, and API payloads; only presentation
// reads this catalog.
const RESEARCH_STAGE_CATALOG = Object.freeze({
  protocol_load: Object.freeze({ zh: "研究上下文加载", en: "Research Context Load" }),
  pre_batch_decision: Object.freeze({ zh: "批次启动判断", en: "Pre-batch Decision" }),
  thesis_design: Object.freeze({ zh: "研究主线设计", en: "Thesis Design" }),
  hypothesis_design: Object.freeze({ zh: "研究假设设计", en: "Hypothesis Design" }),
  expression_design: Object.freeze({ zh: "候选表达式设计", en: "Expression Design" }),
  brief: Object.freeze({ zh: "研究任务说明", en: "Research Brief" }),
  candidate_plan: Object.freeze({ zh: "候选执行规划", en: "Candidate Planning" }),
  score_review: Object.freeze({ zh: "快筛评审", en: "Quick Screening Review" }),
  candidate_decision: Object.freeze({ zh: "候选去留判断", en: "Candidate Decision" }),
  novelty_review: Object.freeze({ zh: "新颖性评审", en: "Novelty Review" }),
  deep_validation_review: Object.freeze({ zh: "深度验证评审", en: "Deep Validation Review" }),
  import_gate_review: Object.freeze({ zh: "入库门评审", en: "Import Gate Review" }),
  import_review: Object.freeze({ zh: "入库结果评审", en: "Import Result Review" }),
  round_synthesis: Object.freeze({ zh: "本轮研究总结", en: "Round Synthesis" }),
  four_step_summary: Object.freeze({ zh: "研究流程总结", en: "Research Process Summary" }),
  checkpoint_stop: Object.freeze({ zh: "检查点暂停", en: "Checkpoint Pause" }),
  human_guidance: Object.freeze({ zh: "人工研究干预", en: "Operator Guidance" }),
  blocker_review: Object.freeze({ zh: "阻塞诊断", en: "Blocker Review" }),
  blocker: Object.freeze({ zh: "研究阻塞", en: "Research Blocker" }),
  note: Object.freeze({ zh: "研究记录", en: "Research Note" }),
});

function researchStageMeta(value) {
  const raw = String(value || "").trim();
  const key = raw.toLowerCase();
  const known = RESEARCH_STAGE_CATALOG[key];
  if (known) return { key, raw, known: true, ...known };
  const fallback = raw ? raw.replaceAll("_", " ") : "研究判断";
  return { key, raw, known: false, zh: fallback, en: "" };
}

function researchStepTitle(step) {
  const stage = researchStageMeta(step?.stage);
  return stage.known ? stage.zh : step?.event || step?.tool || "研究判断";
}

function researchStepEnglishTitle(step) {
  return researchStageMeta(step?.stage).en;
}

function researchStepVariant(step) {
  const stageId = String(step?.stage_id || "").toLowerCase();
  if (stageId.includes("resume") || stageId.includes("reload") || stageId.includes("reopen")) return "resume";
  if (stageId.includes("retry")) return "retry";
  if (stageId.includes("backup")) return "backup";
  return "";
}

function isBranchResearchStep(step) {
  const variant = researchStepVariant(step);
  return variant === "backup" || variant === "retry";
}

function researchStepDisplayTitle(step) {
  const base = researchStepTitle(step);
  const variant = researchStepVariant(step);
  if (!variant) return base;
  return `${base} (${variant})`;
}

function compactStageId(step) {
  const full = String(step?.stage_id || "").trim();
  const match = full.match(/(r\d+):s(\d+)/i);
  if (match) {
    return `${match[1]}:s${String(match[2]).padStart(2, "0")}`;
  }
  const roundId = String(step?.round_id || "").trim();
  const stageSeq = Number(step?.stage_seq || 0);
  if (roundId && stageSeq) {
    return `${roundId}:s${String(stageSeq).padStart(2, "0")}`;
  }
  return full ? clip(full, 24) : "stage id missing";
}

function compactStageSeqId(step) {
  const full = String(step?.stage_id || "").trim();
  const match = full.match(/:s(\d+)/i);
  if (match) return `s${String(match[1]).padStart(2, "0")}`;
  const stageSeq = Number(step?.stage_seq || 0);
  if (stageSeq) return `s${String(stageSeq).padStart(2, "0")}`;
  return "--";
}

function researchStepTransition(step, runtime = {}) {
  const topLevel = step?.stage_transition || runtime?.stage_transition || {};
  if (topLevel && Object.keys(topLevel).length) {
    return { transition: topLevel, source: "top_level" };
  }
  const legacy = step?.extra?.stage_transition || {};
  if (legacy && Object.keys(legacy).length) {
    return { transition: legacy, source: "legacy_extra" };
  }
  return { transition: {}, source: step?.transition_missing || step?.extra?.transition_missing ? "missing" : "absent" };
}

function nextActionTitle(progress, conclusion) {
  const source = text(progress?.next || conclusion, "");
  const normalized = source.replace(/\s+/g, " ").trim();
  if (!normalized) return "待执行";
  const commandMatch = normalized.match(/\b(run_backtest|run_anti_overfit|run_adversarial_validation|submit quality_gate|quality_gate|score_factor|validate_expression|fxalpha_novelty_check|novelty_check|fxalpha_quality_gate)\b/i);
  if (commandMatch) {
    return commandMatch[1]
      .replace(/^submit\s+/i, "")
      .replace(/_/g, " ")
      .replace(/\b\w/g, (char) => char.toUpperCase());
  }
  const zhMatch = normalized.match(/^(深验|快筛|验证|提交|记录|复核|入库|改写|筛选)[^。；;,.，]{0,22}/);
  if (zhMatch) return zhMatch[0];
  return clip(normalized.split(/[。；;,.，]/)[0], 28);
}

function researchStepSummary(step) {
  if (!step || !Object.keys(step).length) return "";
  const { transition } = researchStepTransition(step);
  const parts = [
    step.summary || step.content || step.message,
    step.decision ? `决策：${step.decision}` : "",
    transition.facts ? `事实：${transitionField(transition, "facts", 360)}` : "",
    transition.judgment ? `判断：${transitionField(transition, "judgment", 260)}` : "",
    transition.next_action || step.next_action ? `下一步：${transitionField(transition, "next_action", 260) || step.next_action}` : "",
    transition.why ? `原因：${transitionField(transition, "why", 240)}` : "",
    transition.history_used ? `历史参考：${transitionField(transition, "history_used", 240)}` : "",
    transition.reason ? `阶段流转理由：${transitionField(transition, "reason", 240)}` : "",
  ].filter(Boolean);
  return parts.join("\n");
}

function researchStepChain(step, transition = {}) {
  const previous = text(step?.previous_stage, "start");
  const current = text(step?.stage, "unknown");
  const next = text(transition?.next_stage, "pending");
  return `${previous} → ${current} → ${next}`;
}

function isResumeProtocolStep(step) {
  if (!step || step.stage !== "protocol_load") return false;
  const stageId = String(step.stage_id || "").toLowerCase();
  return Boolean(
    step.previous_stage
    && step.stage_seq > 1
    && (stageId.includes("resume") || stageId.includes("reload") || stageId.includes("reopen"))
  );
}

function researchStepsForFlow() {
  return researchSteps().filter((step) => step?.schema_version === "research_step_v2" && !isResumeProtocolStep(step));
}

function latestResumeProtocolStep() {
  return researchSteps().find((step) => isResumeProtocolStep(step)) || null;
}

function compareResearchStepOrder(a, b) {
  const aSeq = Number(a?.stage_seq || 0);
  const bSeq = Number(b?.stage_seq || 0);
  if (aSeq && bSeq && aSeq !== bSeq) return aSeq - bSeq;
  const aTs = parseIso(a?.ts || a?.created_at)?.getTime() || 0;
  const bTs = parseIso(b?.ts || b?.created_at)?.getTime() || 0;
  return aTs - bTs;
}

function flowStepsForCurrentRound() {
  const steps = researchStepsForFlow();
  if (!steps.length) return [];
  const latest = steps[0] || {};
  const roundId = latest?.round_id;
  const sameRound = roundId ? steps.filter((step) => step?.round_id === roundId) : steps;
  return [...sameRound].sort(compareResearchStepOrder);
}

function mainFlowStepsForCurrentRound() {
  return flowStepsForCurrentRound().filter((step) => !isBranchResearchStep(step));
}

function branchFlowStepsForCurrentRound() {
  return flowStepsForCurrentRound().filter((step) => isBranchResearchStep(step));
}

// The backend deliberately records two events for an LLM stage: a request
// checkpoint (req_*) and the completed stage result (sNN_*).  Both records are
// useful in the trace, but the compact main-line tracker must show one card per
// stage.  Prefer the completed result when both records are available.
function researchStepDisplayPriority(step) {
  const stageId = String(step?.stage_id || "").toLowerCase();
  if (/(?:^|:)s\d+_/.test(stageId)) return 2;
  if (/(?:^|:)req_/.test(stageId)) return 1;
  return step?.decision && !/进入\s*llm\s*review|等待.*返回/i.test(String(step.decision)) ? 2 : 1;
}

function isResearchStepRequestCheckpoint(step) {
  return /(?:^|:)req_/.test(String(step?.stage_id || "").toLowerCase())
    || /llm_request/i.test(String(step?.event_type || step?.monitoring?.event_type || ""));
}

function dedupeResearchStepsForFlow(steps) {
  const selected = new Map();
  (steps || []).forEach((step, index) => {
    const roundId = String(step?.round_id || "").trim();
    const stage = String(step?.stage || "").trim();
    const stageSeq = String(step?.stage_seq ?? "").trim();
    const key = `${roundId}|${stageSeq}|${stage}`;
    const previous = selected.get(key);
    const isLaterRequest = Boolean(previous)
      && isResearchStepRequestCheckpoint(step)
      && !isResearchStepRequestCheckpoint(previous.step)
      && index > previous.index;
    if (!previous
      || isLaterRequest
      || researchStepDisplayPriority(step) > researchStepDisplayPriority(previous.step)
      || (researchStepDisplayPriority(step) === researchStepDisplayPriority(previous.step) && index > previous.index)) {
      selected.set(key, { step, index });
    }
  });
  return [...selected.values()]
    .map(({ step }) => step)
    .sort(compareResearchStepOrder);
}

function recentVisibleResearchSteps(limit = 6) {
  const steps = dedupeResearchStepsForFlow(flowStepsForCurrentRound()
    .filter((step) => !isBranchResearchStep(step))
  );
  return steps.slice(-limit);
}

function compactResearchRunRoundIdentity(latest = {}) {
  const allSteps = researchStepsForFlow();
  const latestRunId = String(latest?.run_id || recordRoundId(latest).split(":")[0] || "").trim();
  const runDateMatch = latestRunId.match(/(?:^|_)(20\d{2})(\d{2})(\d{2})(?:_|$)/);
  const runDateKey = runDateMatch ? `${runDateMatch[2]}${runDateMatch[3]}` : "----";
  const sameRunRounds = allSteps
    .filter((step) => !latestRunId || String(step?.run_id || "").trim() === latestRunId)
    .map((step) => {
      const roundId = recordRoundId(step);
      const match = roundId.match(/:r(\d+)/i);
      return match ? { roundId, roundNo: Number(match[1]) } : null;
    })
    .filter(Boolean)
    .sort((a, b) => b.roundNo - a.roundNo);
  const latestResearchRound = sameRunRounds[0] || null;
  const stageKey = compactStageSeqId(latest).toUpperCase();
  const latestStageState = [
    latest?.stage,
    latest?.event_type,
    latest?.checkpoint,
    latest?.status,
    latest?.round_id,
  ].filter(Boolean).join(" ").toLowerCase();
  const stageValue = /checkpoint[_ -]?stop|(?:^|:)stop(?:$|:)/.test(latestStageState)
    ? "STOP"
    : (stageKey === "--" ? "S--" : stageKey);
  return {
    runValue: runDateKey,
    roundValue: latestResearchRound ? String(latestResearchRound.roundNo).padStart(4, "0") : "----",
    stageValue,
    title: [latestRunId, latestResearchRound?.roundId, latest?.stage_id].filter(Boolean).join(" · "),
  };
}

function researchStageTitle(stage) {
  return researchStepTitle({ stage });
}

function evidenceRefsHtml(step) {
  const refs = Array.isArray(step?.evidence_refs) ? step.evidence_refs : [];
  if (!refs.length) return "";
  return `
    <div class="evidence-ref-list">
      ${refs.slice(0, 6).map((ref) => `
        <span title="${escapeHtml(JSON.stringify(ref || {}))}">
          ${escapeHtml(evidenceRefLabel(ref))}
        </span>
      `).join("")}
    </div>
  `;
}

function evidenceRefLabel(ref) {
  const tool = text(ref?.tool || ref?.source || ref?.type || "evidence");
  if (tool === "candidate_plan_code_precheck") {
    const parts = [
      "表达式预检",
      `fatal ${text(ref.fatal_count, "0")}`,
      `warn ${text(ref.warning_count, "0")}`,
    ];
    const ids = Array.isArray(ref.fatal_candidate_ids) ? ref.fatal_candidate_ids.filter(Boolean) : [];
    if (ids.length) parts.push(`drop ${ids.slice(0, 4).join(",")}`);
    return parts.join(" · ");
  }
  if (tool === "candidate_plan_llm_budget_triage") {
    const ids = Array.isArray(ref.skipped_candidate_ids) ? ref.skipped_candidate_ids.filter(Boolean) : [];
    return `候选规划去重 · skip ${text(ref.skipped_count, "0")}${ids.length ? ` · ${ids.slice(0, 4).join(",")}` : ""}`;
  }
  if (tool === "code_advice_keeper") {
    const parts = [
      "代码硬证据放行",
      `ready ${text(ref.code_ready_count, "0")}`,
      `LLM ${text(ref.llm_selected_count, "0")}`,
      `fallback ${text(ref.code_fallback_count, "0")}`,
    ];
    const ids = Array.isArray(ref.code_fallback_candidate_ids) ? ref.code_fallback_candidate_ids.filter(Boolean) : [];
    if (ids.length) parts.push(ids.slice(0, 4).join(","));
    return parts.join(" · ");
  }
  return [
    tool,
    ref?.task_id ? text(ref.task_id) : "",
    ref?.note ? clip(ref.note, 80) : "",
  ].filter(Boolean).join(" · ");
}

function researchValueText(value, maxLength = 260) {
  if (value === undefined || value === null || value === "") return "";
  if (typeof value === "string") return clip(value, maxLength);
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    return clip(value.map((item) => researchValueText(item, 120)).filter(Boolean).join("；"), maxLength);
  }
  if (typeof value === "object") {
    return clip(Object.entries(value)
      .map(([key, item]) => {
        const body = researchValueText(item, 120);
        return body ? `${key}: ${body}` : "";
      })
      .filter(Boolean)
      .join("；"), maxLength);
  }
  return clip(String(value), maxLength);
}

function transitionField(transition, key, maxLength = 260) {
  return researchValueText(transition?.[key], maxLength);
}

function coerceArray(value) {
  if (!value) return [];
  return Array.isArray(value) ? value : [value];
}

function thesisName(thesis) {
  if (!thesis) return "未命名经济假设";
  if (typeof thesis === "string") return clip(thesis, 72);
  return text(thesis.name || thesis.title || thesis.thesis || thesis.economic_thesis, "未命名经济假设");
}

function thesisStatusLabel(thesis) {
  const raw = String(thesis?.status || thesis?.thesis_status || thesis?.decision || "").toLowerCase();
  if (raw.includes("support") || raw.includes("adopt") || raw.includes("pass")) return "supported";
  if (raw.includes("crowd") || raw.includes("novelty")) return "crowded";
  if (raw.includes("translation")) return "translation_failed";
  if (raw.includes("weak")) return "weak_evidence";
  if (raw.includes("reject") || raw.includes("retire")) return "rejected";
  return raw || "observing";
}

function candidateThesis(candidate) {
  const value = candidate?.economic_thesis || candidate?.thesis || candidate?.metadata?.economic_thesis;
  if (!value) return "";
  return typeof value === "string" ? value : thesisName(value);
}

function candidateHypothesis(candidate) {
  return candidate?.hypothesis || candidate?.candidate_prompt || candidate?.interpretation?.hypothesis || "";
}

function candidateTargetHorizon(candidate) {
  return candidate?.target_horizon
    || candidate?.holding_period_days && `${candidate.holding_period_days}D`
    || candidate?.holding_period && `${candidate.holding_period}D`
    || "--";
}

function extractThesisCards() {
  const factorConsole = serviceOutputs(state.factorConsole);
  const digest = liveResearchDigest();
  const cards = [];
  const seen = new Set();
  const add = (payload, source, fallback = {}) => {
    if (!payload) return;
    const item = typeof payload === "string" ? { name: payload, market_mechanism: payload } : { ...payload };
    const card = { ...fallback, ...item, source: item.source || source };
    const name = thesisName(card);
    const key = `${name}::${text(card.target_horizon || card.holding_period_days, "")}`.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    cards.push(card);
  };
  coerceArray(factorConsole.thesis_cards || digest.thesis_cards).forEach((item) => add(item, item?.source || "console"));
  researchSteps().forEach((step) => {
    const extra = step.extra || {};
    const fallback = {
      source: "research_step",
      updated_at: step.ts || step.created_at,
      status: extra.thesis_status || step.thesis_status,
      target_horizon: extra.target_horizon || step.target_horizon,
    };
    coerceArray(extra.economic_theses || step.economic_theses).forEach((item) => add(item, "research_step", fallback));
    add(extra.economic_thesis || step.economic_thesis, "research_step", fallback);
  });
  liveCandidates(40).forEach((candidate) => {
    add(candidate.economic_thesis, "candidate", {
      expression: candidate.expression,
      hypothesis: candidateHypothesis(candidate),
      target_horizon: candidateTargetHorizon(candidate),
      status: candidateDecision(candidate),
      updated_at: candidate.tool_ts || candidate.ts,
    });
  });
  return cards.slice(0, 30);
}

function factorMapOutputs() {
  return serviceOutputs(state.factorMap);
}

async function loadFactorMap() {
  if (state.factorMapLoading) return;
  state.factorMapLoading = true;
  renderFactorMapWorkspace();
  try {
    const response = await getJsonSafe("/factor/map", { timeoutMs: 8000 });
    if (response && !response._failed && !response.error && response.ok) {
      state.factorMap = response;
    }
  } finally {
    state.factorMapLoading = false;
    renderFactorMapWorkspace();
  }
}

function thesisMatchTokens(thesis) {
  return [
    thesisName(thesis),
    thesis.market_mechanism,
    thesis.behavioral_or_risk_rationale,
    thesis.summary,
    thesis.hypothesis,
    thesis.current_thesis,
  ]
    .join(" ")
    .toLowerCase()
    .split(/[^a-z0-9\u4e00-\u9fa5]+/)
    .filter((token) => token.length >= 4)
    .slice(0, 24);
}

function relatedThesisCandidates(thesis) {
  const tokens = thesisMatchTokens(thesis);
  if (!tokens.length) return [];
  return liveCandidates(100)
    .map((candidate) => {
      const haystack = [
        candidate.name,
        candidate.display_name,
        candidate.expression,
        candidateHypothesis(candidate),
        candidate.quality_decision,
        candidate.screening_stage,
      ].join(" ").toLowerCase();
      const score = tokens.reduce((acc, token) => acc + (haystack.includes(token) ? 1 : 0), 0);
      return { candidate, score };
    })
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score || candidateSortValue(b.candidate, "score") - candidateSortValue(a.candidate, "score"))
    .map((item) => item.candidate)
    .slice(0, 5);
}

function timeLabel(value) {
  const date = parseIso(value);
  if (!date) return text(value, "--");
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function ageLabel(value) {
  const seconds = secondsSince(value);
  if (!Number.isFinite(seconds)) return "--";
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}秒前`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}分钟前`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}小时前`;
  return `${Math.round(seconds / 86400)}天前`;
}

function candidateRuntimeFailureInfo(candidate) {
  const stage = String(candidate?.screening_stage || candidate?.stage || candidate?.source_tool || "").toLowerCase();
  const status = String(candidate?.status || candidate?.latest_status || "").toLowerCase();
  const decision = String(candidate?.quality_decision || candidate?.single_factor_decision || candidate?.gate_decision || "").toLowerCase();
  const validation = String(candidate?.validation || "").toLowerCase();
  const error = String(candidate?.error || "").toLowerCase();
  const reasons = [
    candidate?.reject_reason,
    ...(Array.isArray(candidate?.reject_reasons) ? candidate.reject_reasons : []),
    ...(Array.isArray(candidate?.veto_reasons) ? candidate.veto_reasons : []),
    ...(Array.isArray(candidate?.task_history) ? candidate.task_history.flatMap((task) => [task?.status, task?.error]) : []),
  ].filter(Boolean).map((item) => String(item).toLowerCase());
  const textBlob = [stage, status, decision, validation, error, ...reasons].join(" ");
  const interrupted = /进程重启|任务中断|interrupted|api_boot_mismatch|interrupted_by_api_restart/.test(textBlob);
  const scoreRuntimeFailed = /score_runtime_error|score_factor_runtime_error|score_error|score_factor_worker_failed|runtime_error|worker_failed/.test(textBlob);
  const taskFailed = stage.includes("failed") || status === "failed" || decision.includes("task_failed") || interrupted || scoreRuntimeFailed;
  return { taskFailed, interrupted, scoreRuntimeFailed };
}

function activeResearchRunId() {
  const factorConsole = serviceOutputs(state.factorConsole);
  const digest = liveResearchDigest();
  return String(digest.run_id || factorConsole.runtime_view?.run_id || "").trim();
}

function activeResearchRoundId() {
  const factorConsole = serviceOutputs(state.factorConsole);
  const digest = liveResearchDigest();
  const latestStep = researchSteps()[0] || {};
  return String(digest.round_id || factorConsole.runtime_view?.round_id || latestStep.round_id || "").trim();
}

function candidateRunId(candidate) {
  return String(candidate?.run_id || candidate?.round_id || "").split(":")[0].trim();
}

function candidateRoundId(candidate) {
  return String(candidate?.round_id || candidate?.round || "").trim();
}

function candidateOriginInfo(candidate) {
  const activeRunId = activeResearchRunId();
  const activeRoundId = activeResearchRoundId();
  const runId = candidateRunId(candidate);
  const roundId = candidateRoundId(candidate);
  const sourceTool = String(candidate?.source_tool || "").toLowerCase();
  const failed = candidateRuntimeFailureInfo(candidate);
  if (failed.interrupted) return { label: "中断记录", tone: "failed" };
  if (failed.taskFailed) return { label: "任务失败", tone: "failed" };
  if (activeRoundId && roundId === activeRoundId) return { label: "当前 round", tone: "current" };
  if (activeRunId && runId === activeRunId) return { label: "同 run 历史", tone: "history" };
  if (!runId && sourceTool.includes("quantgpt_task_store")) return { label: "任务历史", tone: "history" };
  if (runId && activeRunId && runId !== activeRunId) return { label: "历史 run", tone: "history" };
  return { label: "候选记录", tone: "neutral" };
}

function candidateHasUnscoredPlanDrop(candidate) {
  const quickScore = Number(candidate?.quick_score ?? candidate?.score);
  if (Number.isFinite(quickScore)) return false;
  return (candidate?.stage_history || []).some((item) => {
    const stage = String(item?.screening_stage || item?.stage || "").toLowerCase();
    return stage.includes("candidate_plan_dropped");
  });
}

function candidateGrade(candidate) {
  if (candidateRuntimeFailureInfo(candidate).taskFailed) return "ERR";
  const stage = String(candidate?.screening_stage || candidate?.source_tool || "").toLowerCase();
  const status = String(candidate?.status || "").toLowerCase();
  if (stage.includes("running") || status === "running") return "P";
  // P is reserved for a real, in-flight score.  A candidate removed during
  // planning, blocked by precheck, or carrying an incomplete novelty
  // projection has never received a grade.
  if (candidateHasUnscoredPlanDrop(candidate) || stage.includes("candidate_plan_dropped") || stage.includes("precheck_blocked")) return "—";
  if (stage.includes("precheck_warning") || stage.includes("planned_for_score")) return "P";
  const officialGrade = candidate?.official_grade
    || candidate?.deep_validation?.score_parts?.official_grade
    || candidate?.gate_result?.official_grade;
  if (officialGrade) return officialGrade;
  const m = candidateMetrics(candidate);
  const quickScore = Number(m.quick_score ?? candidate?.quick_score ?? candidate?.score);
  if (Number.isFinite(quickScore)) return gradeFromQuickScore(quickScore, candidate);
  if (candidate?.grade) return candidate.grade;
  const score = Number(m.deep_score ?? candidate?.deep_score);
  if (Number.isFinite(score)) {
    if (score >= 85) return "A";
    if (score >= 70) return "B";
    if (score >= 55) return "C";
    return "D";
  }
  return "—";
}

function candidateQuickGradeByCurrentRules(candidate) {
  const m = candidateMetrics(candidate);
  const quickScore = Number(m.quick_score ?? candidate?.quick_score ?? candidate?.score);
  if (!Number.isFinite(quickScore)) return "";
  return gradeFromQuickScore(quickScore, candidate);
}

function quickScoreThresholds(candidate = {}) {
  const thresholds = candidate?.screening_hint?.thresholds
    || candidate?.interpretation?.screening_hint?.thresholds
    || candidate?.gate_result?.screening_hint?.thresholds
    || {};
  return {
    a: Number(thresholds.quick_score_a ?? 85),
    b: Number(thresholds.quick_score_b ?? 70),
    c: Number(thresholds.quick_score_c ?? 55),
  };
}

function gradeFromQuickScore(score, candidate = {}) {
  const numeric = Number(score);
  if (!Number.isFinite(numeric)) return "—";
  const thresholds = quickScoreThresholds(candidate);
  if (numeric >= thresholds.a) return "A";
  if (numeric >= thresholds.b) return "B";
  if (numeric >= thresholds.c) return "C";
  return "D";
}

function digestFromActiveJob(activeJob) {
  const events = activeJob.events || [];
  const session = latestEventByName(events, "session_started") || {};
  const latestLlm = [...events].reverse().find((event) => event.event === "agent_message") || {};
  const recentCandidates = extractRecentCandidatesFromEvents(events);
  return {
    run_id: activeJob.run_id,
    session_id: session.session_id,
    status: activeJob.status,
    current_phase: activeJob.stage || activeJob.status || "Running",
    current_action: describeLiveAction(activeJob),
    event_count: activeJob.event_count ?? events.length,
    active_task_count: 1,
    updated_at: activeJob.latest_event?.ts || activeJob.started_at,
    target_adopted: activeJob.inputs?.target_adopted,
    target_progress: {
      new_imported: Number(activeJob.summary?.adopted || 0),
      target_adopted: Number(activeJob.inputs?.target_adopted || 0),
    },
    latest_llm_step: latestLlm,
    recent_candidates: recentCandidates,
  };
}

function describeLiveAction(activeJob) {
  const event = activeJob?.latest_event || (activeJob?.events || []).slice(-1)[0] || {};
  if (event.event === "tool_call_started") {
    const expression = event.expression || event.arguments?.expression;
    const action = event.tool === "score_factor"
      ? "正在做单因子快筛"
      : event.tool === "run_backtest"
        ? "正在做完整分组回测"
        : event.tool === "fxalpha_quality_gate"
          ? "正在做入库门检查"
          : `正在调用 ${text(event.tool, "MCP tool")}`;
    return `${action}${expression ? `：${clip(expression, 150)}` : ""}`;
  }
  if (event.event === "tool_call_completed") {
    if (event.tool === "score_factor") return "快筛结果已返回，LLM 将挑选 B/近 B 候选进入深度验证";
    if (event.tool === "run_backtest") return "完整回测已返回，等待诊断/抗过拟合/滚动验证或入库门";
    if (event.tool === "fxalpha_quality_gate") return "入库门已返回，正在决定 import、更新研究轨迹或切换研究路径";
    return `${text(event.tool, "MCP tool")} 已返回，正在解析结果并决定下一步`;
  }
  if (event.event === "agent_message") {
    return `LLM 正在判断：${clip(event.content, 180)}`;
  }
  if (event.event === "four_step_consensus") {
    return `四步分析形成共识：${clip(JSON.stringify(event.consensus || {}), 180)}`;
  }
  if (event.event === "analysis_fact_pack_built") {
    return "已生成四步分析事实包，等待 LLM 做研究路径判断";
  }
  return `${text(event.event || activeJob?.stage || "Running")} · ${clip(event.note || event.reason || "", 160)}`;
}

function extractRecentCandidatesFromEvents(events, limit = 24) {
  const candidates = [];
  const seen = new Set();
  const pushCandidate = (item, event, extra = {}) => {
    if (!item || candidates.length >= limit) return;
    if (!(item.expression || event?.arguments?.expression || event?.expression || item.name || item.grade || item.score !== undefined)) return;
    const candidate = {
      ...item,
      ...extra,
      expression: item.expression || event?.arguments?.expression || event?.expression,
      tool_ts: event?.ts,
    };
    const key = `${candidate.screening_stage || candidate.source_tool || event?.tool || ""}::${candidate.expression || candidate.name || candidate.tool_ts}`;
    if (seen.has(key)) return;
    seen.add(key);
    candidates.push(candidate);
  };
  [...(events || [])].reverse().forEach((event) => {
    if (candidates.length >= limit) return;
    if (event.event === "tool_call_started" && ["score_factor", "run_backtest"].includes(event.tool)) {
      pushCandidate({
        expression: event.arguments?.expression || event.expression,
        name: event.tool === "score_factor" ? "快筛中" : "深度验证中",
        grade: "P",
        screening: { decision: "running", summary: "工具正在运行，结果返回后会自动补充 IC/IR/Sharpe/年化。" },
      }, event, {
        source_tool: event.tool,
        screening_stage: event.tool === "score_factor" ? "quick_score_running" : "deep_validation_running",
      });
      return;
    }
    if (event.event !== "tool_call_completed") return;
    const payload = toolPayloadFromPreview(event);
    if (event.tool === "score_factor") {
      const item = payload.candidate || payload.result || payload.factor || payload;
      pushCandidate(item, event, {
        source_tool: "score_factor",
        screening_stage: item?.screening_stage || "quick_score",
      });
      return;
    }
    if (event.tool === "run_backtest") {
      const item = payload.candidate || payload.result || payload.factor || payload;
      pushCandidate(item, event, {
        source_tool: "run_backtest",
        screening_stage: item?.screening_stage || "deep_validation",
      });
      return;
    }
    if (event.tool === "fxalpha_quality_gate") {
      [
        ["adopted", "import_gate_adopted"],
        ["screened_out", "import_gate_screened_out"],
        ["rejected", "import_gate_rejected"],
      ].forEach(([field, stage]) => {
        (payload[field] || []).forEach((item) => pushCandidate(item, event, {
          source_tool: "fxalpha_quality_gate",
          screening_stage: stage,
        }));
      });
    }
  });
  return candidates;
}

function parseCandidateString(raw) {
  const value = String(raw || "").trim();
  if (!value) return {};
  const parsed = {};
  if (value.startsWith("@{") || value.includes("; expression=") || value.includes("; score=")) {
    [
      ["name", /(?:^|[;{\s])name=([^;}]*)/i],
      ["expression", /(?:^|[;{\s])expression=([^;}]*)/i],
      ["grade", /(?:^|[;{\s])grade=([^;}]*)/i],
      ["score", /(?:^|[;{\s])score=([\-0-9.]+)/i],
      ["ic_mean", /(?:^|[;{\s])ic=([\-0-9.]+)/i],
      ["ic_ir", /(?:^|[;{\s])ic_ir=([\-0-9.]+)/i],
      ["rank_ic_mean", /(?:^|[;{\s])rank_ic=([\-0-9.]+)/i],
      ["rank_ic_ir", /(?:^|[;{\s])rank_ic_ir=([\-0-9.]+)/i],
      ["annual_return", /(?:^|[;{\s])annual_return=([\-0-9.]+)/i],
      ["sharpe", /(?:^|[;{\s])sharpe=([\-0-9.]+)/i],
    ].forEach(([key, pattern]) => {
      const match = value.match(pattern);
      if (!match) return;
      const textValue = match[1].trim();
      parsed[key] = /^-?\d+(\.\d+)?$/.test(textValue) ? Number(textValue) : textValue;
    });
    return parsed.name || parsed.expression ? parsed : { name: value };
  }
  return { name: value };
}

function coerceCandidatePayload(raw) {
  if (!raw) return {};
  if (typeof raw === "string") return parseCandidateString(raw);
  if (typeof raw === "object") return { ...raw };
  return {};
}

function researchStepExtra(step) {
  if (step?.extra && typeof step.extra === "object") return step.extra;
  const raw = step?.extra_preview;
  if (!raw || typeof raw !== "string") return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return { _raw_extra_preview: raw };
  }
}

function isBatchReference(value) {
  return /^batch\d+(?:[_-].*)?$/i.test(String(value || "").trim());
}

function candidatePayloadsFromPreview(raw) {
  const source = String(raw || "");
  const payloads = [];
  const seen = new Set();
  const pattern = /"name"\s*:\s*"([^"]+)"\s*,\s*"expression"\s*:\s*"([^"]+)"/g;
  let match = pattern.exec(source);
  while (match) {
    const [, name, expression] = match;
    const key = `${name}::${expression}`;
    if (!seen.has(key) && !isBatchReference(name)) {
      const nearby = source.slice(match.index, match.index + 700);
      const scoreMatch = nearby.match(/"(?:quick_score|score)"\s*:\s*([0-9.]+)/);
      const gradeMatch = nearby.match(/"grade"\s*:\s*"([ABCD])"/i);
      const rankIcirMatch = nearby.match(/"rank_icir?"\s*:\s*([\-0-9.]+)/i);
      const sharpeMatch = nearby.match(/"sharpe"\s*:\s*([\-0-9.]+)/i);
      payloads.push({
        name,
        expression,
        quick_score: scoreMatch ? Number(scoreMatch[1]) : undefined,
        score: scoreMatch ? Number(scoreMatch[1]) : undefined,
        grade: gradeMatch ? gradeMatch[1].toUpperCase() : undefined,
        rank_ic_ir: rankIcirMatch ? Number(rankIcirMatch[1]) : undefined,
        sharpe: sharpeMatch ? Number(sharpeMatch[1]) : undefined,
      });
      seen.add(key);
    }
    match = pattern.exec(source);
  }
  return payloads;
}

function candidatePayloadsFromStep(step) {
  const extra = researchStepExtra(step);
  const payloads = [];
  const add = (raw) => {
    const payload = coerceCandidatePayload(raw);
    if (!payload.name && !payload.factor_name && !payload.expression && payload.score === undefined && payload.quick_score === undefined && payload.deep_score === undefined) return;
    payloads.push(payload);
  };
  add(extra.candidate);
  add(extra.keeper);
  [
    "selected_for_novelty",
    "selected_candidates",
    "candidates",
    "top_candidates",
    "deep_candidates",
  ].forEach((key) => {
    const items = extra[key];
    if (Array.isArray(items)) items.forEach(add);
  });
  if (Array.isArray(step?.candidate_lanes)) {
    step.candidate_lanes.forEach(add);
  }
  (step?.evidence_refs || []).forEach((ref) => {
    if (!ref || typeof ref !== "object") return;
    if (Array.isArray(ref.items)) ref.items.forEach(add);
    if (Array.isArray(ref.candidate_lanes)) ref.candidate_lanes.forEach(add);
  });
  if (extra._raw_extra_preview) {
    candidatePayloadsFromPreview(extra._raw_extra_preview).forEach(add);
  }
  return payloads;
}

function metricFromStepText(textValue, patterns, asPercent = false) {
  const source = String(textValue || "");
  for (const pattern of patterns) {
    const match = source.match(pattern);
    if (!match) continue;
    const numeric = Number(match[1]);
    if (Number.isFinite(numeric)) return asPercent ? numeric / 100 : numeric;
  }
  return undefined;
}

function gradeFromStepText(textValue) {
  const source = String(textValue || "");
  const match = source.match(/快筛(?:强)?([ABCD])\b/i)
    || source.match(/为([ABCD])\//i)
    || source.match(/\b([ABCD])档/i);
  return match ? match[1].toUpperCase() : undefined;
}

function candidateNameFromStep(step, payload = {}) {
  if (payload.name || payload.factor_name) return payload.name || payload.factor_name;
  const refs = Array.isArray(step.refs) ? step.refs : [];
  const ref = refs.find((item) => {
    if (!/(?:score_factor|fxalpha_novelty_check|run_backtest|run_anti_overfit|run_adversarial_validation|candidate):/i.test(String(item))) return false;
    const value = String(item).split(":").slice(1).join(":").trim();
    return value && !isBatchReference(value);
  });
  if (ref) return String(ref).split(":").slice(1).join(":").trim();
  const plainRef = refs.find((item) => {
    const value = String(item || "").trim();
    return /^[A-Z][A-Za-z0-9_]{5,}$/.test(value) && !isBatchReference(value) && !/^active_\d+$/i.test(value);
  });
  if (plainRef) return String(plainRef).trim();
  const textBlob = `${step.summary || ""} ${step.decision || ""} ${step.next || ""}`;
  const knownName = textBlob.match(/\b([A-Z][A-Za-z0-9_]{5,})\b/);
  return knownName ? knownName[1] : "";
}

function candidatePayloadHasQuickScore(payload = {}) {
  const sources = [
    payload,
    payload.metrics || {},
    payload.key_metrics || {},
    payload.backtest_summary || {},
    payload.report_metrics || {},
  ];
  return sources.some((source) => source?.quick_score !== undefined || source?.score !== undefined);
}

function scoreReviewStepIsRunning(step, payload = {}) {
  if (candidatePayloadHasQuickScore(payload)) return false;
  const blob = `${step.summary || ""} ${step.decision || ""} ${step.next || ""} ${JSON.stringify(step.stage_transition || {})}`.toLowerCase();
  return /进行中|正在执行|准备对|等待 score_factor|validate_and_score_in_progress|in_progress|running/.test(blob);
}

function stageFromResearchStep(step, payload = {}) {
  const payloadStage = String(payload.screening_stage || payload.candidate_lane || payload.precheck_status || "").toLowerCase();
  if (["precheck_blocked", "precheck_warning", "planned_for_score", "candidate_plan_dropped"].includes(payloadStage)) return payloadStage;
  const stage = String(step.stage || "").toLowerCase();
  const blob = `${step.summary || ""} ${step.decision || ""} ${step.next || ""}`.toLowerCase();
  if (stage === "score_review") return scoreReviewStepIsRunning(step, payload) ? "quick_score_running" : "quick_score";
  if (stage === "import_gate_review") {
    if (/schema|missing|失败|拦截|拒绝|blocked|reject|veto/.test(blob)) return "import_gate_rejected";
    if (/adopt|import|导入|入库|通过/.test(blob)) return "import_gate_adopted";
    return "quality_gate";
  }
  if (stage === "deep_validation_review") {
    if (/novelty.*(拒绝|fail|veto)|low_information_gain|不深验|不深度|相关性.*高/.test(blob)) return "novelty_rejected";
    if (/通过novelty|novelty.*通过|进入完整深验|进入.*深验/.test(blob)) return "novelty_passed";
    return "deep_validation";
  }
  return stage || "research_step";
}

function candidateStageRankValue(value) {
  const textValue = String(value || "").toLowerCase();
  const runningPenalty = textValue.includes("running") ? -2 : 0;
  const order = [
    ["imported", 80],
    ["adopted", 70],
    ["quality_gate", 60],
    ["import_gate", 60],
    ["deep", 50],
    ["adversarial", 48],
    ["anti_overfit", 46],
    ["backtest", 44],
    ["novelty", 40],
    ["planned_for_score", 24],
    ["precheck_warning", 22],
    ["candidate_plan_dropped", 21],
    ["precheck_blocked", 20],
    ["quick", 30],
    ["score", 30],
    ["candidate", 20],
    ["expression", 18],
    ["hypothesis", 12],
    ["thesis", 10],
  ];
  const item = order.find(([token]) => textValue.includes(token));
  return item ? item[1] + runningPenalty : 0;
}

function mergeCandidateRecord(base, next) {
  const merged = { ...base };
  Object.entries(next).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    if (key === "stage_history") {
      merged.stage_history = [...(merged.stage_history || []), ...(value || [])];
      return;
    }
    if (key === "screening") {
      merged.screening = { ...(merged.screening || {}), ...value };
      return;
    }
    if (key === "novelty_guard") {
      merged.novelty_guard = { ...(merged.novelty_guard || {}), ...value };
      return;
    }
    if (key === "deep_validation") {
      merged.deep_validation = { ...(merged.deep_validation || {}), ...value };
      return;
    }
    if (key === "rolling_validation") {
      merged.rolling_validation = { ...(merged.rolling_validation || {}), ...value };
      return;
    }
    if (["screening_stage", "stage"].includes(key)) {
      const currentRank = candidateStageRankValue(merged[key] || merged.screening_stage || merged.stage);
      const nextRank = candidateStageRankValue(value);
      if (nextRank >= currentRank) merged[key] = value;
      return;
    }
    if (["quality_decision", "status"].includes(key) && String(value).toLowerCase() === "running" && merged[key]) {
      return;
    }
    merged[key] = value;
  });
  return merged;
}

function candidateRecordKey(candidate) {
  const round = String(candidate?.round_id || candidate?.round || "").trim();
  const candidateId = String(candidate?.candidate_id || candidate?.lane || "").trim();
  if (round && candidateId) return `process:${round}:${candidateId}`.toLowerCase();
  const expression = String(candidate?.expression || "").trim();
  if (expression) return `expr:${expression}`.toLowerCase();
  const name = String(candidate?.name || candidate?.factor_name || "").trim();
  if (name) return `name:${name}`.toLowerCase();
  return "";
}

function candidateExpressionKey(candidate) {
  const expression = String(candidate?.expression || "").trim();
  return expression ? `expr:${expression}`.toLowerCase() : "";
}

function candidateProcessKey(candidate) {
  const round = String(candidate?.round_id || candidate?.round || "").trim();
  const candidateId = String(candidate?.candidate_id || candidate?.lane || "").trim();
  return round && candidateId ? `process:${round}:${candidateId}`.toLowerCase() : "";
}

function candidateIdFromStageId(stageId) {
  const match = String(stageId || "").match(/:candidate_\d+_([^:]+)$/i);
  return match ? match[1] : "";
}

function candidatesFromResearchSteps(limit = 80) {
  const steps = [...researchSteps()].reverse();
  const byKey = new Map();
  steps.forEach((step) => {
    const extra = researchStepExtra(step);
    const refs = Array.isArray(step.refs) ? step.refs : [];
    const candidateRef = refs.find((item) => {
      if (!/(?:score_factor|fxalpha_novelty_check|run_backtest|run_anti_overfit|run_adversarial_validation|candidate):/i.test(String(item))) return false;
      const value = String(item).split(":").slice(1).join(":").trim();
      return value && !isBatchReference(value);
    });
    const candidateStage = /^(score_review|deep_validation_review|import_gate_review)$/i.test(String(step.stage || ""));
    const payloads = candidatePayloadsFromStep(step);
    const candidates = payloads.length ? payloads : [coerceCandidatePayload(extra.candidate)];
    candidates.forEach((payload) => {
    const expression = payload.expression || extra.expression || "";
    if (!extra.candidate && !expression && !candidateRef && !candidateStage && !payload.name && !payload.factor_name) return;
    const name = candidateNameFromStep(step, payload);
    if (!name && !expression) return;
    const stage = stageFromResearchStep(step, payload);
    const blob = `${step.summary || ""} ${step.decision || ""} ${step.next || ""}`;
    const textScore = metricFromStepText(blob, [/得分\s*([0-9.]+)/, /[ABCD]\/([0-9.]+)/i, /(?<!_)score\s*([0-9.]+)/i]);
    const textDeepScore = metricFromStepText(blob, [/deep[_\s-]*score\s*([0-9.]+)/i, /深度(?:检验|验证)?(?:得分)?\s*([0-9.]+)/i]);
    const quickScore = payload.quick_score ?? (stage === "quick_score" ? (payload.score ?? textScore) : undefined);
    const deepScore = payload.deep_score
      ?? payload.deep_validation?.deep_score
      ?? payload.gate_result?.deep_score
      ?? textDeepScore
      ?? (stage.includes("deep") || stage.includes("import_gate") ? textScore : undefined);
    const inferred = {
      ...payload,
      candidate_id: payload.candidate_id || payload.id || candidateIdFromStageId(step.stage_id),
      name: payload.name || name || payload.factor_name,
      expression,
      source_tool: step.stage,
      screening_stage: stage,
      tool_ts: step.ts || step.created_at,
      round_id: step.round_id,
      stage_id: step.stage_id,
      round: step.round ?? step.round_no,
      quality_decision: stage.includes("running") ? "running"
        : stage.includes("rejected") || stage.includes("screened") ? "blocked"
        : stage.includes("adopted") ? "imported"
          : stage.includes("novelty_passed") ? "deep_next"
            : stage.includes("deep") ? "deep_done"
              : stage.includes("quick") ? "scored"
                : "pending",
      screening: {
        decision: stage.includes("rejected") ? "blocked" : "reviewed",
        summary: step.summary || step.decision || "",
        reason: step.decision || step.next || "",
      },
      novelty_guard: extra.novelty_guard || payload.novelty_guard,
      hypothesis: payload.hypothesis || extra.hypothesis,
      economic_thesis: payload.economic_thesis || extra.economic_thesis,
      grade: payload.grade || gradeFromStepText(blob),
      score: payload.score ?? quickScore,
      quick_score: quickScore,
      deep_score: deepScore,
      ic_mean: payload.ic_mean ?? payload.ic ?? metricFromStepText(blob, [/\bIC\s*([\-0-9.]+)/i]),
      ic_ir: payload.ic_ir ?? payload.icir ?? metricFromStepText(blob, [/\bICIR\s*([\-0-9.]+)/i]),
      rank_ic_mean: payload.rank_ic_mean ?? payload.rank_ic ?? metricFromStepText(blob, [/RankIC\s*([\-0-9.]+)/i]),
      rank_ic_ir: payload.rank_ic_ir ?? payload.rank_icir ?? metricFromStepText(blob, [/RankICIR\s*([\-0-9.]+)/i]),
      annual_return: payload.annual_return ?? metricFromStepText(blob, [/年化\s*([\-0-9.]+)%/], true),
      sharpe: payload.sharpe ?? metricFromStepText(blob, [/Sharpe\s*([\-0-9.]+)/i]),
      stage_history: [{
        ts: step.ts || step.created_at,
        stage: step.stage,
        summary: step.summary,
        decision: step.decision,
        next: step.next,
        refs: step.refs,
      }],
    };
    const existingKey = candidateRecordKey(inferred);
    if (!existingKey) return;
    byKey.set(existingKey, mergeCandidateRecord(byKey.get(existingKey) || {}, inferred));
    });
  });
  return [...byKey.values()]
    .filter((item) => item.expression || item.name)
    .sort((a, b) => (parseIso(b.tool_ts)?.getTime() || 0) - (parseIso(a.tool_ts)?.getTime() || 0))
    .slice(0, limit);
}

function libraryMatchForCandidate(candidate) {
  const library = serviceOutputs(state.factorLibraryRaw);
  const items = library.items || [];
  const expression = String(candidate?.expression || "").trim();
  const name = String(candidate?.name || candidate?.factor_name || "").trim().toLowerCase();
  if (!expression && !name) return null;
  return items.find((item) => {
    const itemExpression = String(item.expression || parseMetadata(item).expression || "").trim();
    const itemName = String(item.name || item.factor_name || parseMetadata(item).factor_name || "").trim().toLowerCase();
    return (expression && itemExpression === expression) || (name && itemName === name);
  }) || null;
}

function enrichCandidateWithRegistry(candidate) {
  const match = libraryMatchForCandidate(candidate);
  if (!match) return candidate;
  const facts = candidateStageFacts(candidate);
  const factorConsole = serviceOutputs(state.factorConsole);
  const model = serviceOutputs(state.modelStatus);
  const registry = factorConsole.registry_summary || {};
  const activeValues = factorConsole.active_values_store || factorConsole.readiness?.active_factor_values || {};
  const activeValuesSynced = Boolean(activeValues.exists)
    && !activeValues.stale
    && Number(activeValues.column_count || 0) === Number(registry.active || 0);
  const modelFeatureStale = Boolean(model.feature_set_stale || model.readiness?.feature_set_stale || model.active_feature_set?.feature_set_stale);
  const modelFeatureSynced = !modelFeatureStale
    && Number(model.active_feature_set?.factor_count || model.readiness?.active_feature_manifest?.factor_count || 0) === Number(registry.active || 0);
  const registryMatch = {
    factor_id: match.factor_id,
    status: match.status || "active",
    name: match.name,
  };
  if (!facts.gatePassed) {
    return mergeCandidateRecord(candidate, {
      registry_match: registryMatch,
    });
  }
  return mergeCandidateRecord(candidate, {
    factor_id: match.factor_id,
    status: match.status || "active",
    screening_stage: activeValuesSynced && modelFeatureSynced ? "registry_imported_derived_synced" : "registry_imported_derived_stale",
    quality_decision: activeValuesSynced && modelFeatureSynced ? "imported" : "registry_imported",
    source_tool: candidate.source_tool || "factor_registry",
    expression: candidate.expression || match.expression,
    name: candidate.name || match.name,
    ic_mean: candidate.ic_mean ?? match.ic_mean,
    ic_ir: candidate.ic_ir ?? match.icir,
    rank_ic_mean: candidate.rank_ic_mean ?? libraryMetric(match, "rank_ic_mean"),
    rank_ic_ir: candidate.rank_ic_ir ?? libraryMetric(match, "rank_icir"),
    annual_return: candidate.annual_return ?? libraryMetric(match, "annual_return"),
    sharpe: candidate.sharpe ?? match.sharpe,
    deep_score: candidate.deep_score ?? libraryMetric(match, "deep_score"),
    screening: {
      decision: activeValuesSynced && modelFeatureSynced ? "imported" : "registry_imported_derived_stale",
      summary: activeValuesSynced && modelFeatureSynced ? "已在 active 因子库中找到匹配记录，派生层已同步。" : "已在 active 因子库中找到匹配记录，active-values/model 派生层待同步。",
      reason: match.factor_id,
    },
  });
}

function candidateStageLabel(candidate) {
  const canonicalLabel = text(candidate?.display_status_label, "").trim();
  const visibleAliases = {
    待深验: "待深度验证",
    深验中: "深度验证中",
    深验拦截: "深度验证拦截",
    异常深验: "异常深度验证",
  };
  if (canonicalLabel) return visibleAliases[canonicalLabel] || canonicalLabel;
  const facts = candidateStageFacts(candidate);
  const stage = facts.stage;
  const metrics = candidateMetrics(candidate);
  const hasQuickScore = metrics.quick_score !== undefined && metrics.quick_score !== null && Number.isFinite(Number(metrics.quick_score));
  const anomaly = candidateDeepEligibilityAnomaly(candidate, facts);
  const noveltyReject = candidateNoveltyRejectLabel(candidate);

  // The candidate table has a deliberately small business-state vocabulary.
  // Reasons, legacy diagnostics, and implementation details belong in the
  // reason/details column; they must not create another visible state.
  if (anomaly?.legacy || anomaly?.current) return "异常深度验证";
  if (candidate?.production_promoted === true || ["production_promoted", "production_existing"].includes(String(candidate?.shadow_import_status || ""))) return "已入库";
  if (candidate?.shadow_committed === true || String(candidate?.shadow_import_status || "") === "shadow_committed") return "入库中";
  if (stage.includes("registry_imported_derived_stale")) return "入库中";
  if (stage.includes("registry_imported_derived_synced")) return "已入库";
  if (facts.gatePassed && stage.includes("imported")) return "已入库";
  if (facts.gateRejected) return "入库拒绝";
  if (stage.includes("quality_gate") || stage.includes("import_gate") || stage.includes("gate")) {
    return facts.gatePassed ? "入库中" : "入库检查中";
  }
  if (facts.gatePassed) return "入库中";
  if (candidate?.registry_match?.factor_id) return "匹配库内因子";

  if (facts.deepRejected) return "深度验证拦截";
  if (stage.includes("deep_validation_running") || stage.includes("backtest_running") || stage.includes("anti_overfit_running") || stage.includes("adversarial_validation_running")) return "深度验证中";
  if (facts.hasDeepEvidence || stage.includes("deep") || stage === "run_backtest" || stage.includes("anti_overfit") || stage.includes("adversarial_validation")) {
    return facts.hasCompleteDeepEvidence ? "待入库检查" : "深度验证中";
  }

  if (noveltyReject) return noveltyReject;
  if (facts.noveltyRejected) return "因子库互相关拦截";
  if (stage.includes("novelty")) {
    const novelty = candidate?.novelty_metrics || candidate?.novelty_guard || candidate?.novelty_correlation || {};
    const combined = candidate?.combined_guard || {};
    const noveltyAllowed = novelty.allowed === true
      || combined.novelty_allowed === true
      || facts.decision.includes("deep_validate")
      || facts.decision.includes("advance_to_deep_validation");
    return noveltyAllowed ? "待深度验证" : "互相关检测中";
  }

  if (stage.includes("precheck_blocked")) return "表达式预检拦截";
  // Candidate Plan semantic dedup stays in the construction/precheck phase;
  // it never entered quick score and therefore must not look like a score fail.
  if (!hasQuickScore && candidateHasUnscoredPlanDrop(candidate)) return "表达式预检拦截";
  if (stage.includes("candidate_plan_dropped")) return "表达式预检拦截";
  if (facts.runtimeInterrupted || facts.scoreRuntimeFailed) return "快筛失败";
  if (facts.quickFailed) return "快筛拦截";
  if (stage.includes("planned_for_score")) return "待快筛";
  if (stage.includes("precheck_warning")) return "待快筛";
  if (stage.includes("quick_score_pending_result") || stage.includes("quick_score_running") || stage.includes("score_running")) return "快筛中";
  if (stage.includes("quick") || stage === "score_factor") return hasQuickScore ? "互相关检测中" : "快筛中";
  if (stage.includes("expression") || stage.includes("candidate") || stage.includes("thesis") || stage.includes("hypothesis") || stage.includes("plan")) return "构造表达式中";
  return "构造表达式中";
}

function candidateStatusTone(label) {
  const value = String(label || "").toLowerCase();
  if (/已入库|待深(?:度验证|验)|通过|pass/.test(value)) return "ok";
  if (/失败|异常|入库拒绝|拦截|预检|reject|veto/.test(value)) return "danger";
  if (/警告|临界|warning/.test(value)) return "warn";
  if (/待快筛|快筛中|待|中|构造|pending|running/.test(value)) return "info";
  return "info";
}

function candidateDecisionLabel(decision) {
  const value = String(decision || "pending").toLowerCase();
  if (value === "reject") return "拒绝";
  if (value === "pending") return "待处理";
  if (value === "running") return "运行中";
  if (value === "pass") return "通过";
  if (value === "adopt" || value === "imported") return "已入库";
  return String(decision || "待处理");
}

function candidateDecision(candidate) {
  const stage = String(candidate?.screening_stage || candidate?.source_tool || "").toLowerCase();
  const status = String(candidate?.status || "").toLowerCase();
  if (stage.includes("running") || status === "running") return "running";
  return candidate?.single_factor_decision
    || candidate?.quality_decision
    || candidate?.quality_gate_decision
    || candidate?.gate_decision
    || candidate?.gate_result?.decision
    || candidate?.screening?.decision
    || candidate?.screening_hint?.decision
    || candidate?.interpretation?.quality_decision
    || "pending";
}

function candidateMetrics(candidate) {
  const runtimeFailure = candidateRuntimeFailureInfo(candidate);
  const direct = candidate?.metrics || {};
  const nestedBacktest = direct.backtest_summary || {};
  const summary = {
    ...candidate,
    ...candidate?.backtest_summary,
    ...candidate?.key_metrics,
    ...direct,
    ...nestedBacktest,
    ...candidate?.gate_result,
  };
  return {
    ic_mean: summary.ic_mean ?? summary.ic ?? summary.mean_ic,
    ic_ir: summary.ic_ir ?? summary.icir ?? summary.ir,
    rank_ic_mean: summary.rank_ic_mean ?? summary.rank_ic ?? summary.rank_ic_mean_abs,
    rank_ic_ir: summary.rank_ic_ir ?? summary.rank_icir ?? summary.rank_ir,
    sharpe: summary.sharpe ?? summary.sharpe_ratio,
    annual_return: summary.annual_return ?? summary.annualized_return ?? summary.annualized_ret ?? summary.returns,
    max_drawdown: summary.max_drawdown ?? summary.max_dd ?? candidate?.best_long_only_group_metrics?.max_drawdown,
    ic_win_rate: summary.ic_win_rate ?? summary.win_rate,
    turnover: summary.turnover ?? summary.avg_turnover,
    quick_score: runtimeFailure.scoreRuntimeFailed ? undefined : summary.quick_score ?? summary.score,
    deep_score: summary.deep_score ?? candidate?.deep_validation?.deep_score ?? candidate?.gate_result?.deep_score,
    rolling_score: summary.rolling_score ?? candidate?.rolling_validation?.score ?? candidate?.deep_validation?.rolling_validation?.score,
  };
}

function isValidFactorCandidate(candidate) {
  const name = String(candidate?.name || candidate?.factor_name || "").trim();
  const expression = String(candidate?.expression || "").trim();
  const lower = name.toLowerCase();
  if (expression) return !/^backtest_report_|^run_backtest_|^fxalpha_quality_gate_|^novelty_check_/i.test(expression);
  if (!name) return false;
  if (["validated", "candidate", "candidates", "summary", "latest", "score", "quick", "deep", "backtest"].includes(lower)) return false;
  if (/^20\d{6}(?:[_-]\d{4,6})?$/.test(name)) return false;
  if (/^(?:round|batch)\d+(?:[_-].*)?$/i.test(name)) return false;
  if (/^backtest_report_|^run_backtest_|^fxalpha_quality_gate_|^novelty_check_/i.test(name)) return false;
  return candidate?.score !== undefined
    || candidate?.quick_score !== undefined
    || candidate?.deep_score !== undefined
    || Boolean(candidate?.metrics)
    || Boolean(candidate?.status);
}

function liveCandidates(limit = 50) {
  const board = currentCandidateBoard();
  if (!board || board.schema_version !== "current_candidate_board_v1") return [];
  return (board.candidates || [])
    .filter(isValidFactorCandidate)
    .slice(0, limit)
    .map(enrichCandidateWithRegistry);
}

function currentCandidateBoard() {
  const factorConsole = serviceOutputs(state.factorConsole);
  return factorConsole.current_candidate_board || {};
}

function antiOverfitLabel(candidate) {
  const anti = candidate?.anti_overfit_summary || candidate?.anti_overfit || candidate?.deep_validation?.anti_overfit || {};
  const score = candidate?.anti_overfit_score ?? anti.score;
  if (score !== undefined && score !== null) return shortNumber(score, 1);
  return "--";
}

function noveltyLabel(candidate) {
  const rejectLabel = candidateNoveltyRejectLabel(candidate);
  if (rejectLabel) return rejectLabel;
  const novelty = candidate?.novelty_metrics || candidate?.novelty_guard || candidate?.deep_validation?.novelty_correlation || candidate?.novelty_correlation || {};
  const p90 = novelty.p90_pearson ?? novelty.p90_existing_pearson;
  const r90 = novelty.p90_rank_corr ?? novelty.p90_existing_rank_corr;
  const maxP = novelty.max_existing_pearson ?? novelty.max_pearson;
  const maxR = novelty.max_existing_rank_corr ?? novelty.max_rank_corr;
  const noveltyScore = candidate?.novelty_score ?? novelty.score;
  const stage = String(candidate?.screening_stage || "").toLowerCase();
  const metrics = candidateMetrics(candidate);
  const hasQuickScore = metrics.quick_score !== undefined && metrics.quick_score !== null && Number.isFinite(Number(metrics.quick_score));
  if ([p90, r90, maxP, maxR].every((value) => value === undefined || value === null)) {
    if (noveltyScore !== undefined && noveltyScore !== null) {
      return novelty.allowed === true ? `通过 ${shortNumber(noveltyScore, 3)}` : shortNumber(noveltyScore, 3);
    }
    return stage.includes("quick") && hasQuickScore ? "待互相关检测" : "--";
  }
  return `P ${shortNumber(maxP, 3)} / R ${shortNumber(maxR, 3)} / p90 ${shortNumber(p90, 3)}/${shortNumber(r90, 3)}`;
}

function adversarialLabel(candidate) {
  const adv = candidate?.adversarial_validation || candidate?.deep_validation?.adversarial_validation || {};
  const score = candidate?.adversarial_score ?? adv.score;
  if (score !== undefined && score !== null) return shortNumber(score, 1);
  return "--";
}

function rollingValidationLabel(candidate) {
  const rolling = candidate?.rolling_validation || candidate?.deep_validation?.rolling_validation || {};
  const summary = rolling.summary || {};
  const score = candidate?.rolling_score ?? rolling.score ?? summary.score;
  if (score !== undefined && score !== null) return shortNumber(score, 1);
  return "--";
}

function isGenericCandidateName(value) {
  const raw = String(value || "").trim();
  return /^(候选|候选因子|candidate)(?:\s*[-_:]?\s*\d+)?$/i.test(raw);
}

function candidateDisplayName(candidate, fallback = "候选") {
  const processLabel = candidateProcessLabel(candidate, "");
  if (processLabel && !candidate?.factor_id) return processLabel;
  const expression = String(candidate?.expression || "").trim();
  const name = String(candidate?.display_name || candidate?.name || candidate?.factor_name || "").trim();
  if (name && name !== expression && name.length < 64 && !isGenericCandidateName(name)) return name;
  if (candidate?.factor_id) return candidate.factor_id;
  const thesis = String(candidateThesis(candidate) || "").trim();
  if (thesis) return clip(thesis, 40);
  const hypothesis = String(candidateHypothesis(candidate) || "").trim();
  if (hypothesis) return clip(hypothesis, 40);
  if (expression) {
    const compact = expression
      .replace(/\s+/g, " ")
      .replace(/^rank\(/i, "")
      .replace(/\)\s*\*\s*rank\(/gi, " × ")
      .replace(/[()]/g, "")
      .trim();
    return clip(compact || expression, 40);
  }
  return fallback;
}

function compactRoundLabel(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const match = raw.match(/(?:^|:)(r\d{3,5})(?:$|:)/i);
  if (match) return match[1].toLowerCase();
  return clip(raw.split(":").pop() || raw, 18);
}

function candidateProcessLabel(candidate, fallback = "") {
  const round = compactRoundLabel(candidate?.round_id || candidate?.round);
  const candidateId = String(candidate?.candidate_id || candidate?.lane || "").trim();
  if (round && candidateId) return `${round}:${candidateId}`;
  if (round) return `${round}:${fallback || "候选"}`;
  if (candidateId) return fallback ? `${fallback}:${candidateId}` : candidateId;
  return fallback;
}

function candidateIdentityParts(candidate, fallback = "候选") {
  const expression = String(candidate?.expression || "").trim();
  const processLabel = candidateProcessLabel(candidate, fallback);
  const officialName = String(candidate?.factor_id || candidate?.display_name || candidate?.factor_name || "").trim();
  const imported = String(candidate?.registry_status || candidate?.quality_decision || candidate?.screening_stage || "").toLowerCase().includes("import");
  const expressionLabel = expression
    ? clip(expression
      .replace(/\s+/g, " ")
      .replace(/^rank\(/i, "")
      .replace(/\)\s*\*\s*rank\(/gi, " × ")
      .replace(/[()]/g, "")
      .trim() || expression, 42)
    : "";
  if (imported && officialName) {
    return { title: officialName, subtitle: processLabel || expressionLabel, processLabel };
  }
  return {
    title: processLabel || fallback,
    subtitle: expressionLabel || candidateDisplayName(candidate, ""),
    processLabel,
  };
}

function renderPinnedBestCandidate(candidate, options = {}) {
  if (!candidate || !Object.keys(candidate).length) return "";
  const label = options.label || "最强候选";
  const m = candidateMetrics(candidate);
  const deepDisplay = candidateDeepDisplay(candidate);
  const identity = candidateIdentityParts(candidate, label);
  const pinnedTitle = identity.title === label
    ? candidateDisplayName(candidate, "候选")
    : identity.title;
  return `
    <button class="pinned-candidate" type="button" id="inspect-best-candidate">
      <div class="pinned-candidate-main">
        <div class="pinned-candidate-title">
          <span class="badge subtle">${escapeHtml(label)}</span>
          <span class="pinned-candidate-id-label">候选编号</span>
          <strong>${escapeHtml(text(pinnedTitle, "暂无候选"))}</strong>
        </div>
        <div class="pinned-candidate-expression" title="${escapeHtml(candidate.expression || "")}">
          <span class="pinned-candidate-expression-label">表达式</span>
          <code>${escapeHtml(candidate.expression || identity.subtitle || "等待表达式")}</code>
        </div>
      </div>
      <div class="pinned-metrics">
        <span>ICIR <b>${shortNumber(m.ic_ir, 3)}</b></span>
        <span>Sharpe <b>${shortNumber(m.sharpe, 3)}</b></span>
        <span>年化 <b>${pct(m.annual_return, 2)}</b></span>
        <span>Quick <b>${shortNumber(m.quick_score, 1)}</b></span>
        ${deepDisplay.value !== undefined ? `<span>Deep <b>${shortNumber(deepDisplay.value, 1)}</b></span>` : ""}
      </div>
    </button>
  `;
}

function taskParamSummary(task) {
  const p = task?.params || {};
  return [
    p.universe ? `universe=${p.universe}` : "",
    p.start_date && p.end_date ? `${p.start_date} ~ ${p.end_date}` : "",
    p.benchmark ? `benchmark=${p.benchmark}` : "",
    p.holding_period ? `holding=${p.holding_period}` : "",
    p.n_groups ? `groups=${p.n_groups}` : "",
    p.cost_rate !== undefined ? `cost=${p.cost_rate}` : "",
    p.neutralize_cap !== undefined ? `cap=${Boolean(p.neutralize_cap)}` : "",
    p.neutralize_industry !== undefined ? `industry=${Boolean(p.neutralize_industry)}` : "",
  ].filter(Boolean).join(" · ");
}

function researchConfigItems(runtimeDefaults = {}, task = null, fallbackHolding = "--") {
  const p = task?.params || {};
  const selectionStart = runtimeDefaults.selection_start_date || runtimeDefaults.default_start_date || p.start_date;
  const selectionEnd = runtimeDefaults.selection_end_date || runtimeDefaults.default_end_date || p.end_date;
  const valueStart = runtimeDefaults.value_start_date || runtimeDefaults.default_value_start_date;
  const valueEnd = runtimeDefaults.value_end_date || runtimeDefaults.default_value_end_date;
  const universe = runtimeDefaults.universe || runtimeDefaults.default_universe || p.universe || "all_market";
  const benchmark = runtimeDefaults.benchmark || runtimeDefaults.default_benchmark || p.benchmark || "hs300";
  const holdingPeriod = runtimeDefaults.holding_period || runtimeDefaults.default_holding_period || p.holding_period || fallbackHolding;
  return [
    { label: "Universe", value: universe },
    { label: "Score Window", value: selectionStart && selectionEnd ? `${selectionStart} ~ ${selectionEnd}` : "--" },
    { label: "Value Window", value: valueStart && valueEnd ? `${valueStart} ~ ${valueEnd}` : "--" },
    { label: "Benchmark", value: benchmark },
    { label: "Holding", value: `${holdingPeriod}D` },
  ].filter(Boolean);
}

function taskDuration(task) {
  if (!task) return "--";
  if (task.duration_seconds !== undefined && task.duration_seconds !== null) return `${shortNumber(task.duration_seconds, 1)}s`;
  const started = parseIso(task.created_at);
  if (!started) return "--";
  return `${shortNumber((Date.now() - started.getTime()) / 1000, 0)}s`;
}

function candidateRejectReasons(candidate) {
  const anomaly = candidateDeepEligibilityAnomaly(candidate);
  return [
    anomaly?.legacy ? "历史记录按当前阈值不满足 deep 放行条件" : "",
    anomaly?.current ? "当前 run 出现 C/D deep 放行异常" : "",
      ...(candidate?.reject_reasons || []),
      ...(candidate?.veto_reasons || []),
      ...(candidate?.interpretation?.reject_reasons || []),
      ...(candidate?.deep_validation?.veto_reasons || []),
      ...(candidate?.gate_result?.reject_reasons || []),
      ...(candidate?.screening?.reject_reasons || []),
      ...(candidate?.precheck_warnings || []),
    candidate?.novelty_reject_label,
    candidate?.novelty_reject_type,
    candidate?.status_label,
    candidate?.status_reason,
    candidate?.precheck_instruction,
    candidate?.gate_result?.reason,
    candidate?.screening?.reason,
    candidate?.screening_hint?.reason,
    candidate?.novelty_guard?.reason,
    candidate?.novelty_guard?.decision,
  ].filter(Boolean);
}

function candidateEvidenceText(candidate) {
  const parts = [
    candidate?.reject_reason,
    candidate?.quality_decision,
    candidate?.single_factor_decision,
    candidate?.screening_stage,
    candidate?.source_stage,
    candidate?.source_tool,
    candidate?.screening?.decision,
    candidate?.screening?.summary,
    candidate?.screening?.reason,
    candidate?.precheck_status,
    candidate?.precheck_instruction,
    ...(candidate?.precheck_warnings || []),
    candidate?.screening_hint?.decision,
    candidate?.screening_hint?.reason,
    candidate?.novelty_reject_type,
    candidate?.novelty_reject_label,
    candidate?.novelty_guard?.decision,
    candidate?.novelty_guard?.reason,
    candidate?.novelty_guard?.summary,
    candidate?.novelty?.decision,
    candidate?.novelty?.reason,
    candidate?.novelty?.summary,
    ...candidateRejectReasons(candidate),
  ];
  (candidate?.stage_history || []).forEach((item) => {
    parts.push(item?.stage, item?.summary, item?.decision, item?.next);
    if (Array.isArray(item?.refs)) parts.push(...item.refs);
  });
  return parts.filter(Boolean).map((part) => String(part)).join(" | ").toLowerCase();
}

function candidateNoveltyRejectLabel(candidate) {
  const textBlob = candidateEvidenceText(candidate);
  const novelty = candidate?.novelty_metrics || candidate?.novelty_guard || candidate?.novelty_correlation || {};
  const combined = candidate?.combined_guard || {};
  const decision = String(candidateDecision(candidate) || "").toLowerCase();
  const stage = String(candidate?.screening_stage || candidate?.stage || candidate?.source_tool || "").toLowerCase();
  const hasNoveltyEvidence = stage.includes("novelty")
    || novelty.allowed !== undefined
    || novelty.score !== undefined
    || candidate?.novelty_score !== undefined
    || combined.novelty_allowed !== undefined;
  if (!hasNoveltyEvidence) return "";
  const hasRejectSignal = stage.includes("novelty_rejected")
    || novelty.allowed === false
    || combined.novelty_allowed === false
    || /reject|rejected|blocked|veto|drop|screen|拒绝|剔除|拦截|不深验|不进入/.test(decision)
    || /reject|rejected|blocked|veto|drop|dropped|screened|screen_out|orthogonalize|拒绝|剔除|拦截|不深验|不进入|未进入|未保留|无增量信息|低增量|同质变体/.test(textBlob);
  if (!hasRejectSignal) return "";
  if (/batch_redundancy|same[_ -]?batch|within[_ -]?batch|intra[_ -]?batch|batch.*(?:redundan|similar|corr|correlation)|组内|同批|批内|同轮|同质变体/.test(textBlob)) {
    return "组内互相关拦截";
  }
  // The numeric novelty gate compares against the active factor pool.  Any
  // candidate-level rejection without a batch-redundancy reason is therefore
  // shown as a factor-library interception, with the precise reason retained
  // beside the row.
  return "因子库互相关拦截";
}

function candidateStageFacts(candidate) {
  const stage = String(candidate?.screening_stage || candidate?.stage || candidate?.source_tool || "").toLowerCase();
  const decision = String(candidateDecision(candidate) || "").toLowerCase();
  const status = String(candidate?.status || candidate?.latest_status || "").toLowerCase();
  const reasons = candidateRejectReasons(candidate).map((reason) => String(reason).toLowerCase());
  const m = candidateMetrics(candidate);
  const novelty = candidate?.novelty_metrics || candidate?.novelty_guard || candidate?.novelty_correlation || {};
  const gate = candidate?.gate_result || {};
  const taskTypes = new Set((candidate?.task_history || []).map((task) => String(task?.task_type || "").toLowerCase()));
  const antiStatus = String(candidate?.anti_overfit_status || candidate?.anti_overfit_summary?.status || candidate?.anti_overfit?.status || "").toLowerCase();
  const advStatus = String(candidate?.adversarial_status || candidate?.adversarial_validation?.status || "").toLowerCase();
  const hasBacktest = Boolean(candidate?.backtest_status || taskTypes.has("backtest") || stage.includes("deep_validation"));
  const hasAnti = candidate?.anti_overfit_score !== undefined
    || taskTypes.has("anti_overfit")
    || Boolean(antiStatus && !antiStatus.includes("not_run"));
  const hasAdv = candidate?.adversarial_score !== undefined
    || taskTypes.has("adversarial_validation")
    || Boolean(advStatus && !advStatus.includes("not_run"));
  const hasCompleteDeepEvidence = hasBacktest && hasAnti && hasAdv;
  const hasDeepEvidence = hasBacktest || hasAnti || hasAdv || m.deep_score !== undefined;
  const isRunning = stage.includes("running") || status === "running" || decision.includes("running");
  const quickFailed = stage.includes("quick") && (
    decision.includes("reject")
    || reasons.some((reason) => reason.includes("quick_score_below") || reason.includes("quick"))
    || (m.quick_score !== undefined && Number(m.quick_score) < 60)
  );
  const noveltyRejected = stage.includes("novelty_rejected")
    || novelty.allowed === false
    || reasons.some((reason) => reason.includes("novelty") || reason.includes("correlation"));
  const deepRejected = stage.includes("deep") && (
    decision.includes("reject")
    || decision.includes("targeted_mutation")
    || reasons.some((reason) => reason.includes("deep") || reason.includes("anti") || reason.includes("adversarial") || reason.includes("risk"))
    || (m.deep_score !== undefined && Number(m.deep_score) < 80)
  );
  const preGateBlocked = quickFailed || noveltyRejected || deepRejected;
  const gatePassed = !preGateBlocked && (
    stage.includes("adopted")
    || stage.includes("imported")
    || decision.includes("adopt")
    || decision.includes("imported")
    || gate.passed === true
  );
  const gateRejected = stage.includes("quality_gate_rejected")
    || stage.includes("quality_gate_screen")
    || stage.includes("import_gate_rejected")
    || stage.includes("import_gate_screen")
    || (stage.includes("gate") && (decision.includes("reject") || decision.includes("screen")));
  return {
    stage,
    decision,
    status,
    reasons,
    hasDeepEvidence,
    hasBacktest,
    hasAnti,
    hasAdv,
    hasCompleteDeepEvidence,
    isRunning,
    quickFailed,
    noveltyRejected,
    deepRejected,
    preGateBlocked,
    gatePassed,
    gateRejected,
  };
}

function candidateDeepEligibilityAnomaly(candidate, facts = null) {
  const quickGrade = candidateQuickGradeByCurrentRules(candidate);
  if (!["C", "D"].includes(String(quickGrade).toUpperCase())) return null;
  const metrics = candidateMetrics(candidate);
  const stageFacts = facts || {
    stage: String(candidate?.screening_stage || candidate?.stage || candidate?.source_tool || "").toLowerCase(),
    decision: String(candidateDecision(candidate) || "").toLowerCase(),
    hasDeepEvidence: metrics.deep_score !== undefined
      || Boolean(candidate?.backtest_status)
      || Boolean(candidate?.anti_overfit_status || candidate?.anti_overfit_summary || candidate?.anti_overfit)
      || Boolean(candidate?.adversarial_status || candidate?.adversarial_validation),
    quickFailed: false,
    noveltyRejected: false,
    deepRejected: false,
    gateRejected: false,
  };
  const stage = String(stageFacts.stage || "").toLowerCase();
  const decision = String(stageFacts.decision || "").toLowerCase();
  const hasAdvancedEvidence = stageFacts.hasDeepEvidence
    || stage.includes("deep")
    || stage.includes("novelty")
    || stage.includes("quality_gate")
    || decision.includes("deep_validate")
    || decision.includes("advance_to_novelty")
    || decision.includes("submit_quality_gate");
  if (!hasAdvancedEvidence || stageFacts.quickFailed || stageFacts.noveltyRejected || stageFacts.deepRejected || stageFacts.gateRejected) return null;
  const activeRunId = activeResearchRunId();
  const activeRoundId = activeResearchRoundId();
  const runId = candidateRunId(candidate);
  const roundId = candidateRoundId(candidate);
  const taskStoreHistory = !runId && String(candidate?.source_tool || "").toLowerCase().includes("quantgpt_task_store");
  const current = Boolean(activeRoundId && roundId && roundId === activeRoundId);
  const legacy = taskStoreHistory || !current || Boolean(activeRunId && runId && runId !== activeRunId);
  return {
    legacy,
    current,
    quickGrade,
  };
}

function candidateDeepDisplay(candidate) {
  const m = candidateMetrics(candidate);
  if (m.deep_score !== undefined && m.deep_score !== null && !Number.isNaN(Number(m.deep_score))) {
    return { value: m.deep_score, label: "", title: "official deep_score" };
  }
  const facts = candidateStageFacts(candidate);
  if (!facts.hasDeepEvidence) return { value: undefined, label: "", title: "尚未进入 deep validation" };
  const detail = [
    facts.hasBacktest ? "回测已回" : "回测待回",
    facts.hasAnti ? "抗过拟合已回" : "抗过拟合待回",
    facts.hasAdv ? "对抗验证已回" : "对抗验证待回",
  ].join("；");
  return {
    value: undefined,
    label: facts.hasCompleteDeepEvidence ? "待深分" : "证据中",
    title: `${detail}；official deep_score 尚未生成`,
  };
}

function latestImportBlockerSummary(events, digest) {
  const gate = currentRoundQualityGate(digest?.latest_quality_gate || {}, digest);
  const counts = gate.counts || {};
  const payloads = [];
  [...(events || [])].reverse().some((event) => {
    if (event.event !== "tool_call_completed") return false;
    if (!["fxalpha_quality_gate", "fxalpha_import_factors", "score_factor", "run_backtest"].includes(event.tool)) return false;
    const payload = toolPayloadFromPreview(event);
    if (!payload || !Object.keys(payload).length) return false;
    payloads.push({ tool: event.tool, payload });
    return payloads.length >= 3;
  });
  const importPayload = payloads.find((item) => item.tool === "fxalpha_import_factors")?.payload || {};
  const gatePayload = payloads.find((item) => item.tool === "fxalpha_quality_gate")?.payload || gate;
  const skipped = importPayload.skipped_items || importPayload.skipped || [];
  const screened = gatePayload.screened_out || [];
  const rejected = gatePayload.rejected || [];
  const adopted = gatePayload.adopted || [];
  const reasons = [
    ...(Array.isArray(skipped) ? skipped.map((item) => item.reason || item.err || item.factor_id) : []),
    ...screened.flatMap(candidateRejectReasons),
    ...rejected.flatMap(candidateRejectReasons),
  ].filter(Boolean).slice(0, 6);
  return {
    adopted: counts.adopted ?? adopted.length ?? 0,
    screened_out: counts.screened_out ?? screened.length ?? 0,
    rejected: counts.rejected ?? rejected.length ?? 0,
    imported: importPayload.imported,
    skipped_count: Array.isArray(skipped) ? skipped.length : Number(skipped || 0),
    reasons,
  };
}

function isNearThresholdCandidate(candidate) {
  const m = candidateMetrics(candidate);
  const decision = String(candidate?.quality_decision || candidate?.gate_result?.decision || "").toLowerCase();
  if (["adopt", "pass", "candidate"].some((word) => decision.includes(word))) return true;
  return Math.abs(Number(m.ic_mean || 0)) >= 0.02
    || Math.abs(Number(m.rank_ic_mean || 0)) >= 0.02
    || Math.abs(Number(m.ic_ir || 0)) >= 0.2
    || Math.abs(Number(m.rank_ic_ir || 0)) >= 0.2
    || Number(candidate?.score || 0) >= 45;
}

function candidateSortValue(candidate, mode) {
  if (mode !== "time" && candidateRuntimeFailureInfo(candidate).taskFailed) return -1;
  const m = candidateMetrics(candidate);
  switch (mode) {
    case "time": {
      const timestamp = candidate?.source_step_ts
        || candidate?.tool_ts
        || candidate?.updated_at
        || candidate?.created_at
        || candidate?.completed_at
        || "";
      const parsed = Date.parse(timestamp);
      return Number.isFinite(parsed) ? parsed : 0;
    }
    case "abs_ic":
      return Math.abs(Number(m.ic_mean || 0));
    case "rank_icir":
      return Math.abs(Number(m.rank_ic_ir || 0));
    case "sharpe":
      return Number(m.sharpe || 0);
    case "annual_return":
      return Number(m.annual_return || 0);
    case "score":
    default:
      return Number(candidate?.deep_score ?? candidate?.deep_validation?.deep_score ?? m.quick_score ?? 0);
  }
}

function scoreCell(value, label = "") {
  if (value === undefined || value === null || Number.isNaN(Number(value))) {
    return `<span class="score-empty ${label ? "has-note" : ""}">${escapeHtml(label || "--")}</span>`;
  }
  const numeric = Number(value);
  const pctWidth = Math.max(0, Math.min(100, numeric));
  const tone = numeric >= 80 ? "strong" : numeric >= 60 ? "good" : numeric >= 40 ? "weak" : "bad";
  return `
    <span class="score-cell ${tone}">
      <b>${shortNumber(numeric, 1)}</b>
      ${label ? `<em>${escapeHtml(label)}</em>` : ""}
      <i><u style="width:${pctWidth}%"></u></i>
    </span>
  `;
}

function candidateQualitySummary(candidates) {
  const rows = candidates || [];
  const summary = rows.reduce((acc, candidate) => {
    const grade = String(candidateGrade(candidate) || "--").toUpperCase();
    const decision = String(candidateDecision(candidate)).toLowerCase();
    const stage = String(candidateStageLabel(candidate));
    const facts = candidateStageFacts(candidate);
    const isPassed = facts.gatePassed;
    if (isPassed) acc.passed += 1;
    const reasons = candidateRejectReasons(candidate);
    const isBlocked = !isPassed && (facts.preGateBlocked || facts.gateRejected || reasons.length || decision.includes("reject") || decision.includes("veto") || decision.includes("screen"));
    if (stage.includes("中") || decision.includes("running")) acc.running += 1;
    else if (isBlocked) acc.blocked += 1;
    else acc.open += 1;
    acc.grades[grade] = (acc.grades[grade] || 0) + 1;
    reasons.forEach((reason) => {
      const key = String(reason).split(":")[0].slice(0, 64);
      acc.reasons[key] = (acc.reasons[key] || 0) + 1;
    });
    return acc;
  }, { total: rows.length, running: 0, open: 0, passed: 0, blocked: 0, grades: {}, reasons: {} });
  summary.best = rows
    .filter((candidate) => !String(candidateDecision(candidate)).toLowerCase().includes("running"))
    .sort((a, b) => candidateSortValue(b, "score") - candidateSortValue(a, "score"))[0] || rows[0] || {};
  summary.topReason = Object.entries(summary.reasons).sort((a, b) => b[1] - a[1])[0]?.[0] || "";
  return summary;
}

function countCandidateList(value) {
  if (Array.isArray(value)) return value.length;
  if (value && typeof value === "object") return Object.keys(value).length;
  return 0;
}

function gradeCountsFromText(raw) {
  const source = String(raw || "");
  const grades = {};
  const pattern = /(^|[^A-Za-z])([ABCD])\s*\/?\s*([0-9]{2}(?:\.\d+)?)/g;
  let match = pattern.exec(source);
  while (match) {
    const grade = match[2].toUpperCase();
    grades[grade] = (grades[grade] || 0) + 1;
    match = pattern.exec(source);
  }
  return grades;
}

function mergeGradeCounts(target, source) {
  Object.entries(source || {}).forEach(([grade, count]) => {
    const key = normalizeGradeBucket(grade);
    target[key] = (target[key] || 0) + Number(count || 0);
  });
}

function normalizeGradeBucket(grade) {
  const value = String(grade || "").trim().toUpperCase();
  return ["A", "B", "C", "D"].includes(value) ? value : "P";
}

function gradeSummaryFromResearchSteps() {
  const summary = {
    total: 0,
    running: 0,
    scored: 0,
    passed: 0,
    blocked: 0,
    grades: {},
    reasons: {},
    source_label: "research_step_v2 score_review",
  };
  const allSteps = researchSteps();
  const scoreSteps = allSteps.filter((step) => step?.stage === "score_review");
  const targetSteps = scoreSteps.length ? scoreSteps : allSteps.filter((step) => step?.stage === "candidate_plan");
  targetSteps.forEach((step) => {
    const { transition } = researchStepTransition(step);
    const facts = transition.facts || {};
    const factText = researchValueText(facts, 1200);
    const blob = [step.summary, step.decision, factText, transition.judgment].filter(Boolean).join(" ");
    const textGrades = gradeCountsFromText(blob);
    const quickScreened = Number(
      typeof facts === "object" && !Array.isArray(facts)
        ? facts.quick_screened ?? facts.validated_count ?? facts.candidate_count
        : undefined
    );
    const selected = typeof facts === "object" && !Array.isArray(facts)
      ? countCandidateList(facts.selected_for_novelty || facts.deep_candidates || facts.allowed)
      : 0;
    const rejected = typeof facts === "object" && !Array.isArray(facts)
      ? countCandidateList(facts.rejected_quick || facts.not_selected || facts.rejected)
      : 0;
    const textTotal = Object.values(textGrades).reduce((acc, value) => acc + Number(value || 0), 0);
    const stageTotal = Number.isFinite(quickScreened) && quickScreened > 0
      ? quickScreened
      : Math.max(textTotal, selected + rejected);
    if (!stageTotal) return;
    summary.total += stageTotal;
    summary.scored += step.stage === "score_review" ? stageTotal : 0;
    summary.passed += selected;
    summary.blocked += rejected;
    mergeGradeCounts(summary.grades, textGrades);
    if (selected && !textGrades.A && !textGrades.B) {
      summary.grades.B = (summary.grades.B || 0) + selected;
    }
    const known = Object.values(textGrades).reduce((acc, value) => acc + Number(value || 0), 0)
      + (selected && !textGrades.A && !textGrades.B ? selected : 0);
    const unknown = Math.max(0, stageTotal - known);
    if (unknown) summary.grades.P = (summary.grades.P || 0) + unknown;
  });
  return summary.total ? summary : null;
}

function effectiveGradeSummary(summary) {
  const hasVisibleGrades = Object.values(summary?.grades || {}).some((value) => Number(value || 0) > 0);
  if (summary?.total && hasVisibleGrades) return { ...summary, source_label: "live candidates" };
  return gradeSummaryFromResearchSteps() || summary;
}

function renderGradeDistribution(summary) {
  const total = Math.max(1, summary.total || 0);
  const normalizedGrades = {};
  Object.entries(summary.grades || {}).forEach(([grade, count]) => {
    const key = normalizeGradeBucket(grade);
    normalizedGrades[key] = (normalizedGrades[key] || 0) + Number(count || 0);
  });
  const items = ["A", "B", "C", "D", "P"].map((grade) => ({
    grade,
    count: normalizedGrades[grade] || 0,
  })).filter((item) => item.count > 0);
  if (!items.length) return `<div class="grade-distribution empty">暂无候选分布</div>`;
  return `
    <div class="grade-distribution">
      ${items.map((item) => `
        <span class="grade-dist-item grade-${item.grade.toLowerCase().replaceAll(/[^a-z0-9]+/g, "p")}">
          <b>${escapeHtml(item.grade)}</b>
          <i style="width:${Math.max(8, (item.count / total) * 100)}%"></i>
          <em>${item.count}</em>
        </span>
      `).join("")}
    </div>
  `;
}

function renderGradeDistributionModule(summary) {
  const effective = effectiveGradeSummary(summary);
  const source = effective?.source_label || "live candidates";
  return `
    <section class="grade-distribution-module" id="grade-distribution-module">
      <div>
        <p class="eyebrow">Grade Distribution · 候选评分</p>
        <h3>候选等级分布</h3>
        <small class="module-subtitle">${escapeHtml(`Quick Score 分布 · ${source === "live candidates" ? "最近 50 个表达式候选" : "来自最近研究日志"}`)}</small>
      </div>
      ${renderGradeDistribution(effective || summary)}
    </section>
  `;
}

function renderResearchPulseBoard() {
  const container = document.getElementById("research-pulse-board");
  if (!container) return;
  const digest = liveResearchDigest();
  const candidates = liveCandidates(50);
  const summary = candidateQualitySummary(candidates);
  const step = latestLlmOutput();
  const best = summary.best || {};
  const bestMetrics = candidateMetrics(best);
  const bestDeepDisplay = candidateDeepDisplay(best);
  const bestName = best.name && best.name !== best.expression
    ? best.name
    : `${candidateGrade(best)} · Quick ${shortNumber(bestMetrics.quick_score, 1)}`;
  const qgpt = digest.quantgpt_task_summary || {};
  const next = step?.next || step?.next_action || digest.current_action || "等待下一轮 Codex/MCP 推进";
  const bottleneck = digest.blocking_reason
    || summary.topReason
    || step?.decision
    || "暂无明确瓶颈；等待 score/deep/import gate 返回更多证据";
  container.innerHTML = `
    <div class="pulse-card pulse-primary">
      <span>研究进展</span>
      <strong>${escapeHtml(text(researchStepTitle(step), "等待 LLM 记录"))}</strong>
      <p>${escapeHtml(clip(step?.decision || step?.summary || digest.current_action || "暂无 LLM 摘要。", 210))}</p>
    </div>
    <div class="pulse-card">
      <span>批次态势</span>
      <strong>${summary.total} candidates · ${text(qgpt.running_count, "0")} running</strong>
      <div class="pulse-mini-grid">
        <b>Open ${summary.open}</b>
        <b>Gate passed ${summary.passed}</b>
        <b>Blocked ${summary.blocked}</b>
      </div>
      ${renderGradeDistribution(summary)}
    </div>
    <button class="pulse-card pulse-candidate" type="button" id="inspect-best-candidate">
      <span>最强候选</span>
      <strong>${escapeHtml(text(bestName, "暂无候选"))}</strong>
      <code>${escapeHtml(clip(best.expression || "score_factor 返回后显示表达式", 170))}</code>
      <div class="pulse-metrics">
        <em>Quick ${shortNumber(bestMetrics.quick_score, 1)}</em>
        <em>Deep ${bestDeepDisplay.value !== undefined ? shortNumber(bestDeepDisplay.value, 1) : escapeHtml(bestDeepDisplay.label || "--")}</em>
        <em>ICIR ${shortNumber(bestMetrics.ic_ir, 3)}</em>
        <em>Sharpe ${shortNumber(bestMetrics.sharpe, 2)}</em>
      </div>
    </button>
    <div class="pulse-card pulse-next">
      <span>瓶颈 / 下一步</span>
      <strong>${escapeHtml(clip(bottleneck, 80))}</strong>
      <p>${escapeHtml(clip(next, 220))}</p>
    </div>
  `;
  document.getElementById("inspect-best-candidate")?.addEventListener("click", () => {
    if (!best || !Object.keys(best).length) return;
    state.inspector = { kind: "candidate", payload: best };
    renderInspector();
    renderCandidateResultTable();
    document.getElementById("inspector-detail")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
}

function countByEvent(events, name) {
  return (events || []).filter((event) => event.event === name).length;
}

function sumEventField(events, name, field) {
  return (events || [])
    .filter((event) => event.event === name)
    .reduce((total, event) => total + Number(event[field] || 0), 0);
}

function compactCandidateTable(candidates, limit = 10) {
  const rows = (candidates || []).slice(0, limit);
  if (!rows.length) {
    return `<div class="empty-state compact-empty">还没有候选结果返回。score_factor 启动后会先显示“快筛中”，结果返回后会补上 IC/IR、RankICIR、Sharpe、年化和拒绝原因。</div>`;
  }
  return `
    <div class="cockpit-factor-table">
      <div class="factor-table-head">
        <strong>最近候选因子 / 快筛与回测结果</strong>
        <span>实时来自 MCP 工具：快筛、深度验证、入库门</span>
      </div>
      <div class="factor-table-scroll">
        <table>
          <thead>
            <tr>
              <th>阶段</th>
              <th>Grade</th>
              <th>Quick</th>
              <th>Deep</th>
              <th>Expression</th>
              <th>IC</th>
              <th>ICIR</th>
              <th>Rank IC</th>
              <th>Rank ICIR</th>
              <th>Sharpe</th>
              <th>年化</th>
              <th>Novelty</th>
              <th>结论 / 拒绝原因</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map((candidate) => {
              const m = candidateMetrics(candidate);
              const deepDisplay = candidateDeepDisplay(candidate);
              const reasons = candidateRejectReasons(candidate);
              const decision = candidateDecision(candidate);
              const stage = candidateStageLabel(candidate);
              const grade = candidateGrade(candidate);
              const novelty = candidate?.novelty_guard || candidate?.deep_validation?.novelty_correlation || candidate?.novelty_correlation || {};
              const rowClass = [
                decision === "adopt" || decision === "pass" ? "row-adopted" : "",
                isNearThresholdCandidate(candidate) ? "row-near-threshold" : "",
                reasons.some((reason) => /autocorr|correlation|novelty|information/i.test(String(reason))) ? "row-veto" : "",
              ].filter(Boolean).join(" ");
              return `
                <tr class="${rowClass}">
                  <td><span class="stage-chip">${escapeHtml(stage)}</span></td>
                  <td><span class="badge grade-${String(grade || "p").toLowerCase()}">${escapeHtml(text(grade, "--"))}</span></td>
                  <td>${scoreCell(m.quick_score)}</td>
                  <td title="${escapeHtml(deepDisplay.title || "")}">${scoreCell(deepDisplay.value, deepDisplay.label)}</td>
                  <td><code>${escapeHtml(text(candidate.expression, "暂无表达式"))}</code></td>
                  <td>${shortNumber(m.ic_mean, 4)}</td>
                  <td>${shortNumber(m.ic_ir, 3)}</td>
                  <td>${shortNumber(m.rank_ic_mean, 4)}</td>
                  <td>${shortNumber(m.rank_ic_ir, 3)}</td>
                  <td>${shortNumber(m.sharpe, 3)}</td>
                  <td>${pct(m.annual_return, 2)}</td>
                  <td>P ${shortNumber(novelty.max_existing_pearson, 3)} / R ${shortNumber(novelty.max_existing_rank_corr, 3)}</td>
                  <td><span class="decision-cell">${escapeHtml(text(decision, "pending"))}</span>${reasons.length ? ` · <span class="reason-text">${escapeHtml(reasons.join(", "))}</span>` : ""}</td>
                </tr>
              `;
            }).join("")}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

function renderCompactSystemStrip(activeJob) {
  const container = document.getElementById("compact-system-strip");
  const configContainer = document.getElementById("research-config-strip");
  if (!container) return;
  const factorConsole = serviceOutputs(state.factorConsole);
  const digest = liveResearchDigest();
  const runtime = factorConsole.runtime_view || digest.runtime_view || {};
  const counts = runtime.progress_counts || {};
  const latestStep = runtime.latest_step || digest.decision_view || digest.latest_llm_step || {};
  const researchState = latestStep.extra?.research_state || activeJob?.latest_event?.extra?.research_state || {};
  const candidates = liveCandidates(50);
  const holding = candidates.find((candidate) => candidate.holding_period_days || candidate.holding_period)?.holding_period_days
    || candidates.find((candidate) => candidate.holding_period_days || candidate.holding_period)?.holding_period
    || Object.keys(factorConsole.registry_summary?.holding_period_counts || {})[0]
    || "--";
  const readiness = factorConsole.readiness || {};
  const qgptOk = readiness.quantgpt_api?.reachable;
  const nativeMcp = /native_mcp|codex_native/i.test(`${activeJob?.inputs?.orchestration_mode || ""} ${activeJob?.inputs?.runtime_contract || ""}`);
  const qgptLabel = qgptOk ? "QGPT API OK" : nativeMcp ? "Native MCP" : "QGPT API OFF";
  const latestTask = digest.quantgpt_task_summary?.latest_task || {};
  const runtimeDefaults = serviceOutputs(state.factorStatus).runtime_defaults || {};
  const configItems = researchConfigItems(runtimeDefaults, latestTask, holding);
  const modeBadge = researchModeBadge();
  const deepSeekBalanceChip = researchDeepSeekBalanceChipHtml();
  const decisionView = digest.decision_view || latestStep || {};
  const activityAt = runtime.updated_at
    || decisionView.ts
    || decisionView.updated_at
    || latestStep.ts
    || latestStep.created_at
    || digest.updated_at
    || state.factorConsole?.generated_at
    || factorConsole.generated_at
    || state.lastRefreshAt;
  if (configContainer) {
    configContainer.innerHTML = configItems.length
      ? configItems.map((item) => `
        <span class="config-chip">
          <b>${escapeHtml(item.label)}</b>
          <strong>${escapeHtml(item.value)}</strong>
        </span>
      `).join("")
      : `<span class="config-chip"><b>Research Config</b><strong>等待 QuantGPT task store 参数</strong></span>`;
  }
  container.innerHTML = `
    ${digest.is_live === false ? `<span class="badge warn" title="${escapeHtml(text(digest.snapshot_generated_at, "snapshot time missing"))}">离线快照</span>` : ""}
    <span>动态 ${escapeHtml(ageLabel(activityAt))}</span>
    <span class="badge ${escapeHtml(modeBadge.tone)}" title="${escapeHtml(modeBadge.title)}">${escapeHtml(modeBadge.label)}</span>
    ${deepSeekBalanceChip}
    <span class="${qgptOk || nativeMcp ? "ok-dot" : "bad-dot"}">${escapeHtml(qgptLabel)}</span>
  `;
}

function renderLatestLlmPanel() {
  const step = latestLlmOutput();
  const digest = liveResearchDigest();
  const qgpt = digest.quantgpt_task_summary || {};
  const latestTask = qgpt.latest_task || {};
  const stepDate = parseIso(step?.ts || step?.created_at);
  const taskDate = parseIso(latestTask.created_at || latestTask.updated_at || latestTask.completed_at);
  const llmStepIsStale = Boolean(
    taskDate
    && (!stepDate || stepDate.getTime() < taskDate.getTime())
    && Number(qgpt.running_count || 0) > 0
  );
  const summary = step?.summary || step?.content || step?.message || "";
  const refs = Array.isArray(step.refs) ? step.refs : [];
  const inputHints = [
    step?.stage ? `stage=${step.stage}` : "",
    step?.run_id ? `run=${step.run_id}` : "",
    step?.round_no != null ? `round=${step.round_no}` : "",
    refs.length ? `refs=${refs.join(", ")}` : "",
  ].filter(Boolean).join(" · ");
  if (!summary) {
    return `
      <article class="live-panel llm-panel empty-live">
        <p class="eyebrow">LLM 研究记录</p>
        <h3>暂无 LLM 输出摘要</h3>
        <p>请前台 Codex/MCP 在关键研究节点调用 <code>fxalpha_record_research_step</code>。GUI 不展示内部 system prompt，也不会编造 LLM 判断。</p>
      </article>
    `;
  }
  return `
    <article class="live-panel llm-panel">
      <p class="eyebrow">LLM 研究记录</p>
      <h3>${escapeHtml(researchStepTitle(step))}</h3>
      <div class="llm-io-grid">
        <div class="llm-io-block">
          <span>输入线索</span>
          <p>${escapeHtml(inputHints || "未记录 refs；请在下一次 fxalpha_record_research_step 中补充输入来源。")}</p>
        </div>
      </div>
      <div class="live-panel-meta">
        <span>${escapeHtml(timeLabel(step.ts || step.created_at))}</span>
        <span>${escapeHtml(ageLabel(step.ts || step.created_at))}</span>
        <span>${escapeHtml(text(step.priority, "normal"))}</span>
      </div>
      <p class="progress-summary-block">${escapeHtml(summary)}</p>
      ${evidenceRefsHtml(step)}
      ${llmStepIsStale ? `
        <div class="inline-warning">
          LLM 摘要滞后于当前 QuantGPT 任务。工具仍在运行：${escapeHtml(text(latestTask.task_type, "mcp_tool"))}
          ${latestTask.expression ? ` · ${escapeHtml(clip(latestTask.expression, 140))}` : ""}
        </div>
      ` : ""}
      <details class="raw-event"><summary>展开 LLM 输出 JSON</summary><pre>${escapeHtml(JSON.stringify(step, null, 2))}</pre></details>
    </article>
  `;
}

function renderCurrentMcpTaskPanel() {
  const digest = liveResearchDigest();
  const qgpt = digest.quantgpt_task_summary || {};
  const runningTasks = qgpt.running_tasks || [];
  const hasRunning = runningTasks.length > 0 || Number(qgpt.running_count || 0) > 0;
  const task = runningTasks[0] || qgpt.latest_task || {};
  const title = hasRunning
    ? text(task.task_type || task.status, "运行中任务")
    : task.task_type
      ? `暂无运行中任务 · 最近完成 ${text(task.task_type)}`
      : "暂无运行中 MCP 任务";
  const expression = hasRunning
    ? text(task.expression, "运行中任务暂未记录表达式")
    : task.expression
      ? `最近完成：${task.expression}`
      : "当前没有 running task；等待 Codex/MCP 发起下一次工具调用。";
  const byType = qgpt.by_type || {};
  return `
    <article class="live-panel task-panel ${hasRunning ? "task-running" : "task-idle"}">
      <p class="eyebrow">当前 MCP 工具任务</p>
      <h3>${escapeHtml(title)}</h3>
      <code>${escapeHtml(expression)}</code>
      <p class="task-param-line">${escapeHtml(taskParamSummary(task) || "暂无任务参数")}</p>
      <div class="task-metrics">
        <span>Status <b>${escapeHtml(hasRunning ? text(task.status, "--") : "idle")}</b></span>
        <span>耗时 <b>${escapeHtml(taskDuration(task))}</b></span>
        <span>Score <b>${escapeHtml(text(byType.score?.completed, "0"))}</b></span>
        <span>Backtest <b>${escapeHtml(text(byType.backtest?.completed, "0"))}</b></span>
        <span>抗过拟合 <b>${escapeHtml(text(byType.anti_overfit?.completed, "0"))}</b></span>
        <span>对抗验证 <b>${escapeHtml(text(byType.adversarial_validation?.completed, "0"))}</b></span>
      </div>
      <details class="raw-event"><summary>展开当前任务 JSON</summary><pre>${escapeHtml(JSON.stringify(task, null, 2))}</pre></details>
    </article>
  `;
}

function renderKeyConclusionPanel() {
  const digest = liveResearchDigest();
  const gate = currentRoundQualityGate(digest.latest_quality_gate || {}, digest);
  const fourStep = digest.latest_four_step || {};
  const imported = digest.latest_imported_factor || {};
  const blocker = digest.blocking_reason;
  const candidate = liveCandidates(1)[0] || {};
  const reasons = candidateRejectReasons(candidate);
  const conclusion = blocker
    ? { title: "当前阻塞", body: blocker, raw: digest }
    : gate.counts
      ? { title: "最新 Import Gate", body: `adopted=${text(gate.counts.adopted, "0")} / screened=${text(gate.counts.screened_out, "0")} / rejected=${text(gate.counts.rejected, "0")}`, raw: gate }
      : fourStep.consensus
        ? { title: "四步分析共识", body: JSON.stringify(fourStep.consensus, null, 2), raw: fourStep }
        : imported.factor_id || imported.expression
          ? { title: "最近入库因子", body: `${text(imported.name || imported.factor_id)}\n${text(imported.expression)}\nDeep ${shortNumber(imported.deep_score, 1)} · ICIR ${shortNumber(imported.icir, 3)}`, raw: imported }
          : { title: "最近候选结论", body: `${text(candidateDecision(candidate))}${reasons.length ? ` · ${reasons.slice(0, 3).join("；")}` : ""}`, raw: candidate };
  return `
    <article class="live-panel conclusion-panel">
      <p class="eyebrow">最近关键结论</p>
      <h3>${escapeHtml(conclusion.title)}</h3>
      <p>${escapeHtml(clip(conclusion.body || "等待研究复盘或 import gate 更新。", 420))}</p>
      <details class="raw-event"><summary>展开结论 JSON</summary><pre>${escapeHtml(JSON.stringify(conclusion.raw || {}, null, 2))}</pre></details>
    </article>
  `;
}

function renderRecentResearchStepsPanel() {
  const steps = researchSteps().slice(0, 6);
  if (!steps.length) {
    return `
      <div class="recent-step-strip empty-live">
        <div class="recent-step-head">
          <strong>LLM 研究记录</strong>
          <span>暂无 research_steps；请 MCP 在关键节点调用 fxalpha_record_research_step。</span>
        </div>
      </div>
    `;
  }
  return `
    <div class="recent-step-strip">
      <div class="recent-step-head">
        <strong>LLM 研究记录</strong>
        <span>最近 ${steps.length} 条；完整记录在“研究笔记/飞行记录仪”查看</span>
      </div>
      <div class="recent-step-list llm-log-list">
        ${steps.map((step, index) => `
          <details class="recent-step-item llm-log-item" ${index === 0 ? "open" : ""}>
            <summary class="recent-step-summary llm-log-summary">
              <div class="llm-log-stage">
                <span>${escapeHtml(researchStepTitle(step))}</span>
                <small>${escapeHtml(text(step.stage_id || step.stage, "stage id missing"))}</small>
              </div>
              <b>${escapeHtml(clip(step.summary || step.decision || step.next || "", 160))}</b>
              <small class="llm-log-time">${escapeHtml(ageLabel(step.ts || step.created_at))}</small>
            </summary>
          </details>
        `).join("")}
      </div>
    </div>
  `;
}

function renderResearchLiveDesk(activeJob, latestResearch) {
  const container = document.getElementById("research-live-desk");
  if (!container) return;
  const steps = researchSteps().slice(0, 12);
  const latest = steps[0] || latestLlmOutput();
  if (!latest && !steps.length) {
    container.innerHTML = `
      <article class="live-panel llm-panel empty-live unified-llm-log">
        <p class="eyebrow">LLM Research Log</p>
        <h3>LLM 研究记录</h3>
        <p>等待下一条研究记录。</p>
      </article>
    `;
    return;
  }
  container.innerHTML = `
    <article class="live-panel llm-panel unified-llm-log">
      <div class="unified-log-head">
        <div>
          <p class="eyebrow">LLM Research Log</p>
          <h3>LLM 研究记录</h3>
        </div>
        <span>最近 ${steps.length} 条 research step</span>
      </div>
      <div class="recent-step-list unified-step-list llm-log-list">
        ${steps.map((step, index) => {
          const { transition } = researchStepTransition(step);
          const stepMeta = [
            step?.round_id ? `轮次 ${step.round_id}` : "",
            step?.stage_seq ? `阶段序号 ${step.stage_seq}` : "",
            step?.previous_stage ? `上一阶段 ${researchStageHumanLabel(step.previous_stage)}` : "",
          ].filter(Boolean).join(" · ");
          return `
            <details class="recent-step-item llm-log-item" ${index === 0 ? "open" : ""}>
              <summary class="recent-step-summary llm-log-summary">
                <div class="llm-log-stage">
                  <span>${escapeHtml(researchStepTitle(step))}</span>
                  <small title="${escapeHtml(text(step.stage_id || step.stage, "stage id missing"))}">${escapeHtml(compactStageId(step))}</small>
                </div>
                <b>${escapeHtml(clip(transition.judgment || step.summary || step.decision || step.next || "", 170))}</b>
                <small class="llm-log-time">${escapeHtml(ageLabel(step.ts || step.created_at))}</small>
              </summary>
              <div class="recent-step-body llm-log-body">
                ${stepMeta ? `<div class="progress-meta-strip"><span>${escapeHtml(stepMeta)}</span>${transition.next_stage ? `<span>${escapeHtml(`下一阶段 ${researchStageHumanLabel(transition.next_stage)}`)}</span>` : ""}</div>` : ""}
                <div class="llm-log-detail-grid">
                  ${step.summary ? `
                    <div class="llm-log-detail-row">
                      <span>总结</span>
                      <p>${escapeHtml(clip(step.summary, 700))}</p>
                    </div>
                  ` : ""}
                  ${step.decision ? `
                    <div class="llm-log-detail-row">
                      <span>机器决策</span>
                      <p>${escapeHtml(step.decision)}</p>
                    </div>
                  ` : ""}
                  ${transition.judgment ? `
                    <div class="llm-log-detail-row">
                      <span>研究判断</span>
                      <p>${escapeHtml(transitionField(transition, "judgment", 700))}</p>
                    </div>
                  ` : ""}
                  ${transition.why ? `
                    <div class="llm-log-detail-row">
                      <span>为什么</span>
                      <p>${escapeHtml(transitionField(transition, "why", 900))}</p>
                    </div>
                  ` : ""}
                  ${transition.history_used ? `
                    <div class="llm-log-detail-row">
                      <span>历史依据</span>
                      <p>${escapeHtml(transitionField(transition, "history_used", 900))}</p>
                    </div>
                  ` : ""}
                  ${transition.reason ? `
                    <div class="llm-log-detail-row">
                      <span>下一阶段理由</span>
                      <p>${escapeHtml(transitionField(transition, "reason", 700))}</p>
                    </div>
                  ` : ""}
                </div>
                ${evidenceRefsHtml(step)}
                <details class="raw-event"><summary>展开 JSON</summary><pre>${escapeHtml(JSON.stringify(step || {}, null, 2))}</pre></details>
              </div>
            </details>
          `;
        }).join("") || `<div class="empty-state compact-empty">暂无更多 LLM 研究记录。</div>`}
      </div>
    </article>
  `;
}

function mcpStepState({ done, running, blocked }) {
  if (blocked) return "blocked";
  if (running) return "running";
  if (done) return "done";
  return "pending";
}

function renderMcpFlowMap(activeJob, latestResearch) {
  const container = document.getElementById("mcp-flow-map");
  if (!container) return;
  const digest = liveResearchDigest();
  const steps = researchSteps();
  const stepStages = new Set(steps.map((step) => step.stage).filter(Boolean));
  const qgpt = digest.quantgpt_task_summary || {};
  const byType = qgpt.by_type || {};
  const runningTypes = new Set((qgpt.running_tasks || []).map((task) => task.task_type).filter(Boolean));
  const strictCandidates = liveCandidates(50);
  const hasCandidates = strictCandidates.length > 0;
  const gate = currentRoundQualityGate(digest.latest_quality_gate || {}, digest);
  const imported = digest.latest_imported_factor || {};
  const current = String(digest.current_phase || activeJob?.stage || "");
  const flow = [
    {
      key: "context",
      label: "Context",
      title: "读取上下文",
      metric: stepStages.has("brief") ? "brief 已记录" : "等待 brief",
      detail: "字段、活跃因子池、因子地图、研究配置",
      state: mcpStepState({ done: stepStages.has("brief") || Boolean(digest.run_id), running: /context|brief/i.test(current) }),
    },
    {
      key: "design",
      label: "Design",
      title: "候选设计",
      metric: stepStages.has("candidate_plan") ? "plan 已记录" : "Codex 生成表达式",
      detail: "由外部 Agent 写表达式，不走后端 DeepSeek",
      state: mcpStepState({ done: stepStages.has("candidate_plan") || hasCandidates, running: /design|candidate/i.test(current) }),
    },
    {
      key: "score",
      label: "Score",
      title: "快筛",
      metric: `${text(byType.score?.completed, "0")} completed`,
      detail: "quick score / ABCD / IC / ICIR",
      state: mcpStepState({ done: Number(byType.score?.completed || 0) > 0 || hasCandidates, running: runningTypes.has("score") || runningTypes.has("score_factor") }),
    },
    {
      key: "deep",
      label: "Deep",
      title: "深度验证",
      metric: `bt ${text(byType.backtest?.completed, "0")} / anti ${text(byType.anti_overfit?.completed, "0")}`,
      detail: "完整回测、诊断、抗过拟合",
      state: mcpStepState({
        done: Number(byType.backtest?.completed || 0) > 0 || Number(byType.anti_overfit?.completed || 0) > 0 || stepStages.has("deep_validation_review"),
        running: runningTypes.has("backtest") || runningTypes.has("anti_overfit"),
      }),
    },
    {
      key: "gate",
      label: "Gate",
      title: "入库检查",
      metric: gate.counts ? `adopt ${text(gate.counts.adopted, "0")} / reject ${text(gate.counts.rejected, "0")}` : "等待 gate",
      detail: "deep_score + novelty veto",
      state: mcpStepState({ done: Boolean(gate.counts) || stepStages.has("import_gate_review"), running: runningTypes.has("quality_gate") }),
    },
    {
      key: "import",
      label: "Import",
      title: "因子入库",
      metric: imported.factor_id || imported.name || "等待 import",
      detail: "registry / parquet / 模型特征衔接",
      state: mcpStepState({ done: Boolean(imported.factor_id || imported.expression), running: runningTypes.has("import") }),
    },
  ];
  container.innerHTML = `
    <div class="mcp-flow-head">
      <div>
        <p class="eyebrow">MCP Flow · 工具编排</p>
        <h3>官方研究步骤实时轨道</h3>
      </div>
      <span>${escapeHtml(text(digest.current_action || "等待 Codex/MCP 推进下一步"))}</span>
    </div>
    <div class="mcp-flow-rail">
      ${flow.map((step, index) => `
        <article class="mcp-flow-step ${step.state}">
          <div class="mcp-step-index">${step.state === "done" ? "✓" : index + 1}</div>
          <div>
            <span>${escapeHtml(step.label)}</span>
            <strong>${escapeHtml(step.title)}</strong>
            <small>${escapeHtml(clip(step.metric, 44))}</small>
            <p>${escapeHtml(step.detail)}</p>
          </div>
        </article>
      `).join("")}
    </div>
  `;
}

function buildMcpFlowItems(activeJob) {
  const digest = liveResearchDigest();
  const steps = researchSteps();
  const stepStages = new Set(steps.map((step) => step.stage).filter(Boolean));
  const qgpt = digest.quantgpt_task_summary || {};
  const byType = qgpt.by_type || {};
  const counts = (digest.runtime_view || {}).progress_counts || {};
  const scoreCompleted = Number(counts.quick_screened || byType.score?.completed || byType.score_factor?.completed || 0);
  const backtestCompleted = Number(byType.backtest?.completed || byType.run_backtest?.completed || 0);
  const antiCompleted = Number(byType.anti_overfit?.completed || byType.run_anti_overfit?.completed || 0);
  const advCompleted = Number(byType.adversarial_validation?.completed || byType.run_adversarial_validation?.completed || 0);
  const noveltyCompleted = Number(counts.novelty_checked || byType.novelty_check?.completed || byType.novelty?.completed || 0);
  const gateCompleted = Number(byType.quality_gate?.completed || byType.fxalpha_quality_gate?.completed || 0);
  const gateAdopted = Number(counts.quality_gate_adopted || 0);
  const gateRejected = Number(counts.quality_gate_rejected || 0);
  const runningTypes = new Set((qgpt.running_tasks || []).map((task) => task.task_type).filter(Boolean));
  const strictCandidates = liveCandidates(50);
  const hasCandidates = strictCandidates.length > 0;
  const gate = currentRoundQualityGate(digest.latest_quality_gate || {}, digest);
  const imported = digest.latest_imported_factor || {};
  const current = String(digest.current_phase || activeJob?.stage || "");
  return [
    {
      key: "context",
      label: "Context",
      title: "读取上下文",
      metric: stepStages.has("brief") ? "brief 已记录" : "等待 brief",
      detail: "字段、活跃因子池、因子地图、研究配置",
      state: mcpStepState({ done: stepStages.has("brief") || Boolean(digest.run_id), running: /context|brief/i.test(current) }),
    },
    {
      key: "thesis",
      label: "Thesis",
      title: "经济假设",
      metric: extractThesisCards().length ? `${extractThesisCards().length} 条 thesis` : "等待 thesis",
      detail: "先机制，再 hypothesis，再表达式",
      state: mcpStepState({ done: extractThesisCards().length > 0, running: /thesis|brief|candidate/i.test(current) }),
    },
    {
      key: "score",
      label: "Score",
      title: "快筛",
      metric: `${text(scoreCompleted, "0")} completed`,
      detail: "quick score / ABCD / IC / ICIR",
      state: mcpStepState({ done: scoreCompleted > 0 || hasCandidates, running: runningTypes.has("score") || runningTypes.has("score_factor") }),
    },
    {
      key: "deep",
      label: "Deep",
      title: "深度验证",
      metric: `bt ${text(backtestCompleted, "0")} / anti ${text(antiCompleted, "0")} / adv ${text(advCompleted, "0")}`,
      detail: "回测、抗过拟合、对抗检测",
      state: mcpStepState({
        done: backtestCompleted > 0 || antiCompleted > 0 || advCompleted > 0 || stepStages.has("deep_validation_review"),
        running: runningTypes.has("backtest") || runningTypes.has("anti_overfit") || runningTypes.has("adversarial_validation"),
      }),
    },
    {
      key: "novelty",
      label: "Novelty",
      title: "互相关检测",
      metric: noveltyCompleted ? `${text(noveltyCompleted)} completed` : hasCandidates ? "候选已汇总" : "等待 novelty",
      detail: "同持仓周期因子值相关性",
      state: mcpStepState({ done: noveltyCompleted > 0 || strictCandidates.some((item) => item.novelty_guard || item.novelty_correlation), running: runningTypes.has("novelty") || runningTypes.has("novelty_check") }),
    },
    {
      key: "gate",
      label: "Gate",
      title: "入库检查",
      metric: gateAdopted || gateRejected ? `adopt ${text(gateAdopted, "0")} / reject ${text(gateRejected, "0")}` : gateCompleted ? `${text(gateCompleted)} completed` : gate.counts ? `adopt ${text(gate.counts.adopted, "0")} / reject ${text(gate.counts.rejected, "0")}` : "等待 gate",
      detail: "quality gate 是唯一质量裁决",
      state: mcpStepState({ done: gateCompleted > 0 || gateAdopted > 0 || gateRejected > 0 || Boolean(gate.counts) || stepStages.has("import_gate_review"), running: runningTypes.has("quality_gate") }),
    },
    {
      key: "import",
      label: "Import",
      title: "因子入库",
      metric: imported.factor_id || imported.name || "等待 import",
      detail: "registry / parquet / metadata",
      state: mcpStepState({ done: Boolean(imported.factor_id || imported.expression), running: runningTypes.has("import") }),
    },
  ];
}

function renderResearchFlowTrackerHtml(activeJob, progress = currentResearchProgress()) {
  const steps = recentVisibleResearchSteps(6);
  const branchSteps = [];
  const resumeStep = latestResumeProtocolStep();
  if (steps.length) {
    const latest = steps[steps.length - 1] || {};
    const compactIdentity = compactResearchRunRoundIdentity(latest);
    const latestTransition = researchStepTransition(latest).transition || {};
    const visibleSteps = steps;
    const nextStage = latestTransition.next_stage;
    const hasNextStage = Boolean(nextStage);
    const progressHasRunning = Boolean(progress?.hasRunning);
    const loopBack = Boolean(nextStage && visibleSteps.some((step) => step?.stage === nextStage));
    const currentFlowTitle = hasNextStage
      ? researchStageTitle(nextStage)
      : researchStepDisplayTitle(latest);
    const currentFlowEnglish = hasNextStage
      ? researchStageEnglishLabel(nextStage)
      : researchStepEnglishTitle(latest);
    const currentFlowMeta = hasNextStage
      ? clip(researchActionHumanLabel(transitionField(latestTransition, "next_action", 120)) || `来自 ${compactStageId(latest)}`, 58)
      : text(latest.stage_id, "stage pending");
    const flowIsBlocked = latest.stage === "blocker"
      || String(nextStage || "").toLowerCase() === "blocker"
      || /blocker|blocked|fix_orchestrator_runtime_issue/i.test(String(currentFlowTitle || "") + " " + String(currentFlowMeta || ""));
    return `
      <section class="research-flow-tracker process-log-flow ${flowIsBlocked ? "is-blocked" : ""}" aria-label="研究流程进度">
        <div class="research-flow-head">
          <div>
            <p class="eyebrow">Flow Tracker · 主线轨道</p>
            <h3>研究流程进度</h3>
            <small class="module-subtitle">${escapeHtml(`当前 round ${text(latest.round_id, "--")} · 最近 ${visibleSteps.length} 个 stage`)}</small>
          </div>
        </div>
        ${resumeStep ? `
          <div class="research-flow-current resume-note">
            <span>恢复</span>
            <strong>${escapeHtml(clip(resumeStep.summary || resumeStep.decision || "已重新加载协议与上下文，继续原 round。", 120))}</strong>
            <small>${escapeHtml(text(resumeStep.stage_id, "resume"))}</small>
          </div>
        ` : ""}
        <div class="research-flow-current-row">
          <div class="research-flow-id-card" title="${escapeHtml(compactIdentity.title)}">
            <span class="research-flow-id-segment"><small>RUN</small><strong>${escapeHtml(compactIdentity.runValue)}</strong></span>
            <span class="research-flow-id-segment"><small>ROUND</small><strong>${escapeHtml(compactIdentity.roundValue)}</strong></span>
            <span class="research-flow-id-segment stage"><small>STAGE</small><strong>${escapeHtml(compactIdentity.stageValue)}</strong></span>
          </div>
          <div class="research-flow-current ${flowIsBlocked ? "is-blocked" : ""}">
            <span>${flowIsBlocked ? "Blocker" : "当前"}</span>
            <div class="research-flow-current-name">
              <strong>${escapeHtml(currentFlowTitle)}</strong>
              ${currentFlowEnglish ? `<small class="research-flow-stage-en">${escapeHtml(currentFlowEnglish)}</small>` : ""}
            </div>
            <small>${escapeHtml(currentFlowMeta)}</small>
          </div>
        </div>
        <div class="research-flow-track process-log-track">
          ${visibleSteps.map((step, index) => {
            const { transition } = researchStepTransition(step);
            const isLatest = step.stage_id === latest.stage_id;
            const isBlocked = step.stage === "blocker";
            const isCurrentFallback = isLatest && !hasNextStage;
            const stepStateClass = isBlocked
              ? `blocked${isCurrentFallback ? " is-current" : ""}`
              : isCurrentFallback
                ? "running is-current"
                : "done";
            const variant = researchStepVariant(step);
            return `
              <article class="research-flow-step ${stepStateClass}" title="${escapeHtml(researchStepSummary(step))}">
                <div class="research-flow-dot">${escapeHtml(compactStageSeqId(step))}</div>
                <div>
                  <span>${escapeHtml(isLatest ? "最近记录" : "已完成")}</span>
                  <strong>${escapeHtml(researchStepDisplayTitle(step))}</strong>
                  ${researchStepEnglishTitle(step) ? `<small class="flow-step-stage-en">${escapeHtml(researchStepEnglishTitle(step))}</small>` : ""}
                  <small class="flow-step-id">${escapeHtml(compactStageId(step))}</small>
                  ${variant ? `<small class="flow-step-variant">${escapeHtml(variant)}</small>` : ""}
                  <small>${escapeHtml(clip(transitionField(transition, "judgment", 90) || step.summary || step.decision || "", 58))}</small>
                </div>
              </article>
            `;
          }).join("")}
          ${nextStage ? `
            <article class="research-flow-step pending-next is-current ${progressHasRunning ? "running " : ""}${loopBack ? "loop-back" : ""}" title="${escapeHtml(transitionField(latestTransition, "next_action", 180))}">
              <div class="research-flow-dot">→</div>
              <div>
                <span>${escapeHtml(loopBack ? "返回" : "下一步")}</span>
                <strong>${escapeHtml(loopBack ? `回到 ${researchStageTitle(nextStage)}` : researchStageTitle(nextStage))}</strong>
                ${researchStageEnglishLabel(nextStage) ? `<small class="flow-step-stage-en">${escapeHtml(researchStageEnglishLabel(nextStage))}</small>` : ""}
                <small>${escapeHtml(clip(researchActionHumanLabel(transitionField(latestTransition, "next_action", 120)), 58))}</small>
              </div>
            </article>
          ` : ""}
        </div>
      </section>
    `;
  }
  const flow = buildMcpFlowItems(activeJob);
  const digest = liveResearchDigest();
  const qgpt = digest.quantgpt_task_summary || {};
  const runningTask = (qgpt.running_tasks || [])[0];
  const currentKey = researchFlowCurrentKey(flow, digest, runningTask, activeJob);
  return `
    <section class="research-flow-tracker" aria-label="研究流程进度">
      <div class="research-flow-track">
      ${flow.map((step, index) => `
        <article class="research-flow-step ${step.state} ${step.key === currentKey ? "is-current" : ""}" title="${escapeHtml(`${step.title}：${step.detail}`)}">
          <div class="research-flow-dot">${step.state === "done" ? "✓" : index + 1}</div>
          <div>
            <span>${escapeHtml(step.label)}</span>
            <strong>${escapeHtml(step.title)}</strong>
            <small>${escapeHtml(clip(step.metric, 44))}</small>
          </div>
        </article>
      `).join("")}
      </div>
    </section>
  `;
}

function researchFlowCurrentKey(flow, digest, runningTask, activeJob) {
  const taskMap = {
    score: "score",
    score_factor: "score",
    backtest: "deep",
    anti_overfit: "deep",
    adversarial_validation: "deep",
    novelty_check: "novelty",
    novelty: "novelty",
    quality_gate: "gate",
    import: "import",
  };
  if (runningTask?.task_type && taskMap[runningTask.task_type]) {
    return taskMap[runningTask.task_type];
  }
  const decision = digest.decision_view || {};
  const actionText = [
    digest.current_action,
    decision.next,
    decision.next_action,
    activeJob?.latest_event?.next,
    activeJob?.latest_event?.message,
  ].filter(Boolean).join(" ").toLowerCase();
  const actionMap = [
    ["gate", /quality[_\s-]?gate|fxalpha_quality_gate|入库门/],
    ["novelty", /novelty|fxalpha_novelty_check|相关性/],
    ["deep", /run_backtest|backtest|anti[_\s-]?overfit|adversarial|深验|深度|回测|过拟合|对抗/],
    ["score", /score_factor|quick[_\s-]?screen|quick-screen|validate_expression|快筛|评分|验证/],
    ["import", /import|registry|入库/],
  ];
  const actionMatched = actionMap.find(([, pattern]) => pattern.test(actionText));
  if (actionMatched) return actionMatched[0];
  const currentText = [
    digest.current_phase,
    activeJob?.stage,
    decision.stage,
    decision.summary,
    decision.decision,
    decision.next,
  ].filter(Boolean).join(" ").toLowerCase();
  const textMap = [
    ["context", /context|brief|上下文/],
    ["score", /score|quick|快筛|评分/],
    ["deep", /deep|backtest|anti|adversarial|深度|回测|过拟合|对抗/],
    ["novelty", /novelty|correlation|相关性/],
    ["gate", /gate|quality|import_gate|入库门|质量裁决/],
    ["import", /import|registry|入库/],
    ["thesis", /thesis|hypothesis|candidate_plan|经济假设|候选设计/],
  ];
  const matched = textMap.find(([, pattern]) => pattern.test(currentText));
  if (matched) return matched[0];
  return (flow.find((step) => step.state === "running")
    || [...flow].reverse().find((step) => step.state === "done")
    || flow[0])?.key;
}

function currentResearchProgress() {
  const digest = liveResearchDigest();
  const decision = digest.decision_view || {};
  const qgpt = digest.quantgpt_task_summary || {};
  const runningTasks = qgpt.running_tasks || [];
  const hasRunning = runningTasks.length > 0 || Number(qgpt.running_count || 0) > 0;
  const task = runningTasks[0] || qgpt.latest_task || {};
  const flowSteps = flowStepsForCurrentRound();
  const completedFlowSteps = flowSteps.filter((candidate) => !isResearchStepRequestCheckpoint(candidate));
  const requestSteps = flowSteps.filter((candidate) => isResearchStepRequestCheckpoint(candidate));
  const latestFlowStep = completedFlowSteps[completedFlowSteps.length - 1] || {};
  const latestRequestStep = requestSteps[requestSteps.length - 1] || {};
  const step = latestFlowStep && Object.keys(latestFlowStep).length
    ? latestFlowStep
    : decision && Object.keys(decision).length
      ? decision
      : latestLlmOutput();
  const taskTs = task.created_at || task.updated_at || task.completed_at;
  const stepTs = step?.ts || step?.created_at;
  const transitionState = researchStepTransition(step, digest.runtime_view || {});
  const requestIsActive = latestRequestStep && Object.keys(latestRequestStep).length
    && (!step?.stage_seq || Number(latestRequestStep.stage_seq || 0) >= Number(step.stage_seq || 0))
    && latestRequestStep.stage !== step?.stage;
  const activeStep = requestIsActive ? latestRequestStep : step;
  const activeTransitionState = requestIsActive
    ? researchStepTransition(latestRequestStep, digest.runtime_view || {})
    : transitionState;
  const transition = activeTransitionState.transition;
  let phase = text(digest.current_phase, "Idle");
  let action = text(digest.current_action, "等待下一条研究记录");
  let timestamp = digest.updated_at || stepTs || taskTs;
  if (hasRunning) {
    phase = {
      score: "Quick Score",
      backtest: "Deep Validation",
      anti_overfit: "Anti-overfit",
      adversarial_validation: "Adversarial Validation",
      novelty_check: "Novelty Check",
      quality_gate: "Quality Gate",
    }[task.task_type] || "QuantGPT MCP";
    action = `${text(task.task_type, "mcp_tool")} 正在运行${task.expression ? `：${task.expression}` : ""}`;
    timestamp = taskTs || timestamp;
  } else if (requestIsActive) {
    phase = `${researchStepDisplayTitle(activeStep)} · 等待 DeepSeek`;
    action = researchLlmWaitingNarrative(activeStep.stage);
    timestamp = activeStep.ts || activeStep.created_at || timestamp;
  } else if (step && Object.keys(step).length) {
    phase = transition.next_stage
      ? `${researchStepDisplayTitle(step)} → ${text(transition.next_stage, "next")}`
      : researchStepDisplayTitle(step);
    action = transitionField(transition, "next_action", 360) || step.next_action || step.decision || step.summary || "等待下一步动作";
    timestamp = stepTs || timestamp;
  }
  return {
    phase,
    action,
    timestamp,
    task,
    hasRunning,
    llmStep: step,
    requestStep: requestIsActive ? latestRequestStep : null,
    activeStep,
    completedTransition: transitionState.transition,
    resumeStep: latestResumeProtocolStep(),
    llmIsStale: false,
    blocker: digest.blocking_reason,
    transition,
    transitionSource: transitionState.source,
    next: transitionField(transition, "next_action", 360) || step?.next_action || digest.current_action || "等待下一轮研究推进",
    branchSteps: branchFlowStepsForCurrentRound(),
    mainFlowSteps: mainFlowStepsForCurrentRound(),
  };
}

function researchStageHumanLabel(value) {
  return researchStageMeta(value).zh;
}

function researchStageEnglishLabel(value) {
  return researchStageMeta(value).en;
}

function researchActionHumanLabel(value, nextStage = "") {
  const raw = String(value || "").trim();
  const labels = {
    llm_review_in_progress: `已提交给 LLM 评审，正在等待${nextStage ? `「${researchStageHumanLabel(nextStage)}」` : "本阶段"}返回研究判断。`,
    validate_and_score_candidates: "校验候选表达式并执行快速评分。",
    validate_and_score: "校验候选表达式并执行快速评分。",
    validate_and_score_in_progress: "正在校验候选表达式并执行快速评分。",
    run_novelty: "对快筛通过的候选执行正式新颖性检查。",
    run_novelty_and_return_expression_design: "完成新颖性复核后，带着证据返回表达式设计。",
    run_deep_validation: "对新颖性通过的候选执行完整深度验证。",
    run_deep_validation_in_progress: "正在执行回测、抗过拟合、滚动稳定性与对抗验证。",
    run_quality_gate: "把深度验证通过的候选提交质量门复核。",
    run_quality_gate_for_ready_candidates: "把深度验证通过的候选提交质量门复核。",
    import_factor: "导入质量门已通过的候选并核对入库结果。",
    auto_import_gate_adopted_candidates: "导入质量门已通过的候选并核对真实入库结果。",
    advance_to_expression_design: "进入表达式设计，把当前假设转成可计算候选。",
    advance_to_hypothesis_design: "进入假设设计，把研究主线转成可验证关系。",
    return_expression_design: "返回表达式设计，按当前证据做定向修改。",
    return_hypothesis_design: "返回假设设计，重新梳理可检验关系。",
    return_thesis_design: "返回研究主线，重新选择经济机制。",
    synthesize_deep_failures: "汇总本轮深度验证结果，并形成下一轮交接意见。",
    write_round_synthesis: "整理本轮证据、结论和下一轮研究交接。",
    start_next_round: "带着本轮结论开始下一轮研究。",
    start_next_round_at_expression_design: "保留当前主线与假设，从表达式设计开始下一轮定向修改。",
    start_next_round_at_hypothesis_design: "保留当前研究主线，从假设设计开始下一轮调整。",
    refresh_factor_library_information_context: "刷新因子库信息审计后继续研究。",
    replay_existing_candidate_plan: "恢复已有候选规划并继续执行。",
    restart_orchestrator_with_interrupted_handoff: "从中断交接点安全恢复研究。",
    stop_round: "结束本轮并整理研究结论。",
    idle: "等待下一条研究动作。",
    run_batch: "候选规划已完成，进入工具验证。",
    propose_theses: "形成新的经济研究主线。",
    propose_hypotheses: "形成可验证的研究假设。",
    propose_candidates: "形成可计算的候选表达式。",
    mutate: "保留仍有价值的机制，返回上游做定向修改。",
    adopted: "候选已通过当前复核。",
    import_success: "候选已完成真实导入。",
    stop_target_reached: "研究目标已达到，结束当前任务。",
    advance_some: "已有候选可继续推进，下一步按质量证据进入后续验证。",
    reject_batch: "本批候选未达到继续推进条件，需要回到上游重新设计。",
    continue_next_round: "继续下一轮研究，并把本轮经验写入上下文。",
    checkpoint_stop: "当前阶段暂停，等待人工确认或下一次推进。",
  };
  return labels[raw] || raw.replaceAll("_", " ");
}

function researchLlmWaitingNarrative(stage) {
  const raw = String(stage || "").trim();
  const narratives = {
    thesis_design: "DeepSeek 正在选择本轮研究主线。",
    hypothesis_design: "DeepSeek 正在形成可验证的研究假设。",
    expression_design: "DeepSeek 正在设计候选表达式。",
    candidate_plan: "DeepSeek 正在审查候选执行规划。",
    score_review: "DeepSeek 正在复核快速评分证据。",
    novelty_review: "DeepSeek 正在复核新颖性证据。",
    deep_validation_review: "DeepSeek 正在复核深度验证证据。",
    import_gate_review: "DeepSeek 正在复核质量门证据。",
    import_review: "DeepSeek 正在核对导入结果。",
    round_synthesis: "DeepSeek 正在总结本轮研究。",
    blocker_review: "DeepSeek 正在分析研究阻断。",
  };
  return narratives[raw] || `DeepSeek 正在等待「${researchStageHumanLabel(raw)}」阶段判断。`;
}

function researchRunningTaskNarrative(task = {}, fallbackStage = "") {
  const taskType = String(task?.task_type || "").trim().toLowerCase();
  const profiles = {
    validate_expression: {
      title: "表达式校验",
      english: "Expression Validation",
    },
    score: {
      title: "快速评分",
      english: "Quick Screening",
    },
    score_factor: {
      title: "快速评分",
      english: "Quick Screening",
    },
    novelty_check: {
      title: "新颖性检查",
      english: "Novelty Check",
    },
    backtest: {
      title: "深度回测",
      english: "Deep Backtest",
    },
    anti_overfit: {
      title: "抗过拟合验证",
      english: "Anti-overfit Validation",
    },
    rolling_validation: {
      title: "滚动稳定性验证",
      english: "Rolling Validation",
    },
    adversarial_validation: {
      title: "对抗验证",
      english: "Adversarial Validation",
    },
    quality_gate: {
      title: "质量门",
      english: "Quality Gate",
    },
    import: {
      title: "因子导入",
      english: "Factor Import",
    },
  };
  return profiles[taskType] || {
    title: researchStageHumanLabel(fallbackStage || taskType || "研究执行"),
    english: researchStageEnglishLabel(fallbackStage || taskType),
  };
}

function stageDetailValueText(value, limit = 420) {
  return clip(String(value || "").replace(/\s+/g, " ").trim(), limit);
}

function researchProgressIsBlocked(step = {}, transition = {}, digest = {}) {
  const structuredStates = [
    step?.stage,
    step?.event_type,
    step?.checkpoint,
    step?.monitoring?.event_type,
    transition?.next_stage,
  ].map((value) => String(value || "").trim().toLowerCase());
  const tags = Array.isArray(step?.tags)
    ? step.tags.map((tag) => String(tag || "").trim().toLowerCase())
    : [];
  const nextAction = String(transition?.next_action || "").trim().toLowerCase();
  return structuredStates.includes("blocker")
    || tags.includes("blocker")
    || tags.includes("tool_infrastructure_blocker")
    || Boolean(String(digest?.blocking_reason || "").trim())
    || /^fix_[a-z0-9_]*(?:before_restart|runtime_issue)/.test(nextAction);
}

function renderResearchProgressBoard(activeJob) {
  const container = document.getElementById("research-progress-board");
  if (!container) return;
  const digest = liveResearchDigest();
  const candidates = liveCandidates(50);
  const summary = candidateQualitySummary(candidates);
  const progress = currentResearchProgress();
  const runtime = digest.runtime_view || serviceOutputs(state.factorConsole).runtime_view || {};
  const counts = runtime.progress_counts || {};
  const step = progress.llmStep || {};
  const requestStep = progress.requestStep || {};
  const resumeStep = progress.resumeStep;
  const transitionState = researchStepTransition(step, runtime);
  const completedTransition = progress.completedTransition && Object.keys(progress.completedTransition).length
    ? progress.completedTransition
    : transitionState.transition;
  const transition = progress.transition && Object.keys(progress.transition).length ? progress.transition : completedTransition;
  const completedStageLabel = text(step?.stage, "--");
  const nextStageLabel = text(transition.next_stage, progress.hasRunning ? progress.phase : "等待下一步");
  const stageChain = [
    step?.previous_stage ? `上一阶段 ${step.previous_stage}` : "",
    step?.stage ? `当前阶段 ${step.stage}` : "",
    transition?.next_stage ? `下一阶段 ${transition.next_stage}` : "",
  ].filter(Boolean).join(" · ");
  const stageSeqLabel = Number.isFinite(Number(step?.stage_seq))
    ? `S${String(Number(step.stage_seq)).padStart(2, "0")}`
    : "";
  const roundStageLabel = [String(step?.round_id || "").toUpperCase(), stageSeqLabel].filter(Boolean).join(" · ");
  const activeRoundLabel = String(step?.round_id || "").toUpperCase();
  const stepTotal = counts.research_steps ?? researchSteps().length;
  const currentGate = currentRoundQualityGate(digest.latest_quality_gate || {}, digest);
  const conclusion = digest.blocking_reason
    || transition.why
    || currentGate.feedback
    || step.decision
    || "等待更多工具证据";
  const nextStageHuman = researchStageHumanLabel(nextStageLabel);
  const runningTaskNarrative = researchRunningTaskNarrative(progress.task, transitionField(transition, "next_stage", 160) || progress.phase);
  const completedStageEnglish = researchStepEnglishTitle(step);
  const completedStageTitle = step?.stage ? researchStepDisplayTitle(step) : "等待研究记录";
  const currentStageTitle = completedStageTitle;
  const currentStageEnglish = completedStageEnglish;
  const progressStageTitle = progress.hasRunning
    ? `${runningTaskNarrative.title} · 运行中`
    : requestStep.stage
      ? `${researchStepDisplayTitle(requestStep)} · 等待 DeepSeek`
    : (transition.next_stage ? `等待${nextStageHuman}` : completedStageTitle);
  const progressStageEnglish = progress.hasRunning
    ? `${runningTaskNarrative.english || "Tool Execution"} · Running`
    : requestStep.stage
      ? `${researchStepEnglishTitle(requestStep) || "Research Review"} · Waiting`
    : (transition.next_stage ? researchStageEnglishLabel(nextStageLabel) : completedStageEnglish);
  const taskExpression = String(progress.task?.expression || "").trim();
  const latestTags = Array.isArray(step?.tags) ? step.tags.map((tag) => String(tag || "")) : [];
  const latestIsLlmRequestProgress = latestTags.includes("llm_request_progress")
    || step?.monitoring?.event_type === "llm_request";
  const boardIsBlocked = researchProgressIsBlocked(step, transition, digest);
  container.innerHTML = `
    ${renderResearchFlowTrackerHtml(activeJob, progress)}
    <section class="progress-cockpit ${boardIsBlocked ? "is-blocked" : ""}">
      <article class="progress-main-card ${progress.hasRunning ? "is-running" : ""} ${boardIsBlocked ? "is-blocked" : ""}">
        <div class="progress-title-row">
          <p class="eyebrow">Current Stage · 当前阶段</p>
        <small>${escapeHtml(ageLabel(progress.timestamp))}</small>
        </div>
        <h3>当前阶段</h3>
        <p class="progress-value-title">${escapeHtml(progressStageTitle)}</p>
        ${progressStageEnglish ? `<small class="progress-stage-en">${escapeHtml(progressStageEnglish)}</small>` : ""}
        ${taskExpression ? `
          <div class="stage-detail-row stage-expression-row">
            <span>当前表达式</span>
            <code>${escapeHtml(taskExpression)}</code>
          </div>
        ` : ""}
        ${resumeStep ? `<small class="muted-line">${escapeHtml(`已恢复: ${clip(resumeStep.summary || resumeStep.decision || "重新加载协议与上下文", 120)}`)}</small>` : ""}
        <div class="progress-kpis">
          <span>研究步骤 <b>${escapeHtml(text(stepTotal, "0"))}</b></span>
          <span>QGPT 候选 <b>${escapeHtml(text(summary.total, "0"))}</b></span>
          <span>已入库 <b>${escapeHtml(text(counts.imported, "0"))}</b></span>
        </div>
      </article>
      <article class="progress-side-card current-stage-card ${boardIsBlocked ? "is-blocked" : ""}">
        <p class="eyebrow">Last Stage · 最近阶段</p>
        <h3>最近阶段</h3>
        <p class="progress-value-title">${escapeHtml(currentStageTitle)}</p>
        ${currentStageEnglish ? `<small class="progress-stage-en">${escapeHtml(currentStageEnglish)}</small>` : ""}
        <div class="progress-meta-strip">
          ${roundStageLabel ? `<span><b>${escapeHtml(roundStageLabel)}</b></span>` : ""}
          <span>已完成</span>
          ${completedTransition.next_stage ? `<span>下一步 ${escapeHtml(researchStageHumanLabel(completedTransition.next_stage))}</span>` : ""}
          <span>${escapeHtml(`来源 ${progress.transitionSource || transitionState.source || "research_step"}`)}</span>
        </div>
        <div class="stage-detail-list">
          ${[
            { label: "阶段摘要", value: step?.summary, emphasis: true },
            { label: "研究判断", value: completedTransition.judgment },
            { label: "判定依据", value: completedTransition.why },
            { label: "历史依据", value: completedTransition.history_used },
            { label: "下一阶段理由", value: completedTransition.reason },
          ].filter((item) => item.value).map((item) => `
            <div class="stage-detail-row ${item.emphasis ? "is-emphasis" : ""}">
              <span>${escapeHtml(item.label)}</span>
              <strong>${escapeHtml(clip(item.value, item.emphasis ? 260 : 520))}</strong>
            </div>
          `).join("")}
        </div>
        ${progress.branchSteps?.length ? `<small class="muted-line">${escapeHtml(`另有 ${progress.branchSteps.length} 条补跑分支已折叠，不计入主线顺序。`)}</small>` : ""}
      </article>
    </section>
    ${renderGradeDistributionModule(summary)}
  `;
}

function renderDiagnosticsPanel(activeJob) {
  const factorConsole = serviceOutputs(state.factorConsole);
  const diagnostics = factorConsole.diagnostics || {};
  const qgpt = diagnostics.quantgpt_task_summary || factorConsole.quantgpt_task_summary || {};
  const digest = liveResearchDigest();
  return `
    <details class="diagnostics-panel">
      <summary>诊断信息</summary>
      <div class="diagnostics-grid">
        <span>run ${escapeHtml(text(digest.run_id, "none"))}</span>
        <span>research stage ${escapeHtml(text(digest.current_phase, "none"))}</span>
        <span>task store ${escapeHtml(text(qgpt.total, "0"))} / running ${escapeHtml(text(qgpt.running_count, "0"))}</span>
      </div>
    </details>
  `;
}

function renderCandidateResultTable() {
  const container = document.getElementById("live-candidate-table");
  const bestSlot = document.getElementById("live-candidate-best");
  if (!container) return;
  const board = currentCandidateBoard();
  if (!board || board.schema_version !== "current_candidate_board_v1") {
    if (bestSlot) bestSlot.innerHTML = "";
    container.innerHTML = `
      <div class="empty-state error-state">
        候选榜缺少 current_candidate_board。当前榜已禁用旧 candidate_task_view fallback。
      </div>
    `;
    queueFloatingXScrollbarRefresh(container);
    return;
  }
  const allCandidates = liveCandidates(500);
  if (!allCandidates.length) {
    if (bestSlot) bestSlot.innerHTML = "";
    const errors = board.errors || [];
    container.innerHTML = `
      <div class="empty-state ${board.ok === false ? "error-state" : ""}">
        本次 run 暂无可展示候选。
        ${errors.length ? `<small>${escapeHtml(errors.slice(0, 3).map((item) => item.code || item.message || "schema_error").join("；"))}</small>` : ""}
      </div>
    `;
    queueFloatingXScrollbarRefresh(container);
    return;
  }
  // This is intentionally a complete run board: every round remains visible
  // so researchers can compare the actual evolution rather than only the
  // latest batch.  Per-row origin chips distinguish current versus prior
  // rounds within the same run.
  const candidates = [...allCandidates].sort((a, b) => {
    const delta = candidateSortValue(b, state.candidateSort) - candidateSortValue(a, state.candidateSort);
    if (delta) return delta;
    return String(b.source_step_ts || "").localeCompare(String(a.source_step_ts || ""));
  });
  const counts = candidates.reduce((acc, candidate) => {
    const decision = String(candidateDecision(candidate)).toLowerCase();
    const stage = String(candidateStageLabel(candidate));
    const facts = candidateStageFacts(candidate);
    if (decision.includes("running") || stage.includes("中")) acc.running += 1;
    else if (facts.gatePassed) acc.passed += 1;
    else if (facts.preGateBlocked || facts.gateRejected || candidateRejectReasons(candidate).length || decision.includes("reject") || decision.includes("veto") || decision.includes("screen")) acc.blocked += 1;
    else acc.open += 1;
    return acc;
  }, { running: 0, open: 0, passed: 0, blocked: 0 });
  const digest = liveResearchDigest();
  const runSummary = digest.run_id ? `
    <div class="candidate-run-strip">
      <span class="candidate-origin-pill current">当前 run</span>
      <strong>${escapeHtml(compactResearchRunLabel(digest.run_id))}</strong>
      <span>${escapeHtml(text(digest.current_phase || "等待研究状态"))}</span>
      <small>${escapeHtml(clip(digest.current_action || "等待下一步", 72))}</small>
    </div>
  ` : "";
  const bestCandidate = allCandidates
    .filter((candidate) => !String(candidateDecision(candidate)).toLowerCase().includes("running"))
    .sort((a, b) => candidateSortValue(b, "score") - candidateSortValue(a, "score"))[0] || candidates[0];
  const selectedCandidate = state.inspector?.kind === "candidate"
    ? allCandidates.find((candidate) => {
      const selected = state.inspector?.payload || {};
      return candidateProcessKey(candidate) && candidateProcessKey(candidate) === candidateProcessKey(selected)
        || candidate.expression && selected.expression && candidate.expression === selected.expression
        || candidate.candidate_id && selected.candidate_id && candidate.candidate_id === selected.candidate_id;
    })
    : null;
  const pinnedCandidate = selectedCandidate || bestCandidate;
  container.innerHTML = `
    <div class="candidate-result-head">
      <div class="candidate-count-strip" aria-label="候选状态汇总">
        ${counts.running ? `<span>运行 ${counts.running}</span>` : ""}
        <span>候选 ${candidates.length}</span>
        <span>通过 ${counts.passed}</span>
        <span>拦截 ${counts.blocked}</span>
      </div>
      <div class="radar-sort">
        ${[
          ["time", "时间"],
          ["score", "Score"],
          ["abs_ic", "|IC|"],
          ["rank_icir", "Rank ICIR"],
          ["sharpe", "Sharpe"],
          ["annual_return", "年化"],
        ].map(([mode, label]) => `
          <button class="tiny-button ${state.candidateSort === mode ? "active" : ""}" type="button" data-live-candidate-sort="${mode}" aria-pressed="${state.candidateSort === mode}" title="按${label}降序排列候选">${label}</button>
        `).join("")}
      </div>
    </div>
    ${board.ok === false ? `
      <div class="schema-error-strip">
        ${escapeHtml((board.errors || []).slice(0, 3).map((item) => item.code || item.message || "schema_error").join("；"))}
      </div>
    ` : ""}
    ${runSummary}
    <div class="factor-table-scroll live-factor-scroll">
      <table class="live-factor-table">
        <thead>
          <tr>
            <th>阶段</th>
            <th>候选</th>
            <th>快筛等级</th>
            <th>Quick</th>
            <th>Deep</th>
            <th>IC</th>
            <th>ICIR</th>
            <th>Rank ICIR</th>
            <th>Sharpe</th>
            <th>年化</th>
            <th>回撤</th>
            <th>抗过拟合</th>
            <th>对抗</th>
            <th>Rolling</th>
            <th>Novelty</th>
            <th>周期</th>
            <th>结论 / 原因</th>
          </tr>
        </thead>
        <tbody>
          ${candidates.map((candidate, index) => {
            const m = candidateMetrics(candidate);
            const deepDisplay = candidateDeepDisplay(candidate);
            const identity = candidateIdentityParts(candidate, `候选 ${index + 1}`);
            const reasons = candidateRejectReasons(candidate);
            const decision = candidateDecision(candidate);
            const decisionDetail = [text(decision, "pending"), ...reasons].filter(Boolean).join(" · ");
            const grade = candidateGrade(candidate);
            const origin = candidateOriginInfo(candidate);
            const rowClass = [
              origin.tone === "current" ? "row-current-run" : "",
              origin.tone === "history" ? "row-history-run" : "",
              origin.tone === "failed" ? "row-task-failed" : "",
              state.inspector?.kind === "candidate" && state.inspector?.payload?.expression === candidate.expression ? "row-selected" : "",
              decision === "adopt" || decision === "imported" || decision === "pass" ? "row-adopted" : "",
              isNearThresholdCandidate(candidate) ? "row-near-threshold" : "",
              reasons.some((reason) => /autocorr|correlation|novelty|information|veto/i.test(String(reason))) ? "row-veto" : "",
            ].filter(Boolean).join(" ");
            return `
              <tr class="${rowClass}" data-live-candidate="${index}">
                <td><span class="stage-chip tone-${candidateStatusTone(candidateStageLabel(candidate))}">${escapeHtml(candidateStageLabel(candidate))}</span></td>
                <td title="${escapeHtml(candidate.expression || identity.subtitle || identity.title)}">
                  <strong>${escapeHtml(clip(identity.title, 34))}</strong>
                  <span class="candidate-origin-pill ${escapeHtml(origin.tone)}">${escapeHtml(origin.label)}</span>
                  ${identity.subtitle ? `<small class="candidate-expression-brief">${escapeHtml(identity.subtitle)}</small>` : ""}
                </td>
                <td title="${escapeHtml(candidate?.grade_provenance === "quick_score" ? "Quick Score 映射等级，不代表最终入库结论" : "当前可用等级")}"><span class="badge grade-${String(grade || "p").toLowerCase()}">${escapeHtml(text(grade, "--"))}</span></td>
                <td>${scoreCell(m.quick_score)}</td>
                <td title="${escapeHtml(deepDisplay.title || "")}">${scoreCell(deepDisplay.value, deepDisplay.label)}</td>
                <td>${shortNumber(m.ic_mean, 4)}</td>
                <td>${shortNumber(m.ic_ir, 3)}</td>
                <td>${shortNumber(m.rank_ic_ir, 3)}</td>
                <td>${shortNumber(m.sharpe, 3)}</td>
                <td>${pct(m.annual_return, 2)}</td>
                <td>${pct(m.max_drawdown, 2)}</td>
                <td>${escapeHtml(antiOverfitLabel(candidate))}</td>
                <td>${escapeHtml(adversarialLabel(candidate))}</td>
                <td>${escapeHtml(rollingValidationLabel(candidate))}</td>
                <td>${escapeHtml(noveltyLabel(candidate))}</td>
                <td>${escapeHtml(candidateTargetHorizon(candidate))}</td>
                <td class="candidate-decision-cell" title="${escapeHtml(decisionDetail)}"><b>${escapeHtml(candidateDecisionLabel(decision))}</b></td>
              </tr>
            `;
          }).join("")}
        </tbody>
      </table>
    </div>
  `;
  if (bestSlot) {
    bestSlot.innerHTML = selectedCandidate ? "" : renderPinnedBestCandidate(pinnedCandidate, { label: "本次 run 最高分快筛候选" });
  }
  document.getElementById("inspect-best-candidate")?.addEventListener("click", () => {
    state.inspector = { kind: "candidate", payload: pinnedCandidate };
    renderInspector();
    renderCandidateResultTable();
    document.getElementById("inspector-detail")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      });
  container.querySelectorAll("[data-live-candidate]").forEach((row) => {
    row.addEventListener("click", () => {
      state.inspector = { kind: "candidate", payload: candidates[Number(row.dataset.liveCandidate)] };
      renderInspector();
      renderCandidateResultTable();
    });
  });
  queueFloatingXScrollbarRefresh(container);
  container.querySelectorAll("[data-live-candidate-sort]").forEach((button) => {
    button.addEventListener("click", () => {
      state.candidateSort = button.dataset.liveCandidateSort;
      renderCandidateResultTable();
    });
  });
}

function renderResearchBiBoard(activeJob, latestResearch) {
  const container = document.getElementById("research-bi-board");
  if (!container) return;
  const digest = liveResearchDigest();
  const factorConsole = serviceOutputs(state.factorConsole);
  const runtime = factorConsole.runtime_view || digest.runtime_view || {};
  const runtimeCounts = runtime.progress_counts || {};
  const registry = factorConsole.registry_summary || {};
  const readiness = factorConsole.readiness || {};
  const latestSummary = latestResearch?.summary || latestResearch?.result?.summary || {};
  const events = allResearchEvents(activeJob, latestResearch);
  const target = Number(digest.target_adopted || activeJob?.inputs?.target_adopted || latestSummary.target_adopted || 10);
  const activeFactorCount = Number(digest.target_progress?.active || digest.active_factor_count || registry.active || 0);
  const newImported = Number(digest.target_progress?.new_imported || latestSummary.imported || activeJob?.summary?.adopted || sumEventField(events, "round_completed", "adopted_count") || 0);
  const strictCandidates = liveCandidates(50);
  const candidateCount = strictCandidates.length;
  const currentGate = currentRoundQualityGate(digest.latest_quality_gate || {}, digest);
  const gateCounts = currentGate.counts || {};
  const screened = Number(gateCounts.screened_out || 0) || sumEventField(events, "round_completed", "screened_out_count")
    + sumEventField(events, "seed_stage_completed", "screened_out_count");
  const rejected = Number(gateCounts.rejected || 0) || sumEventField(events, "round_completed", "rejected_count");
  const pctDone = target > 0 ? Math.max(0, Math.min(100, (activeFactorCount / target) * 100)) : 0;
  const llm = readiness.llm_runtime || {};
  const notes = readiness.quantgpt_research_notes || {};
  const phase = digest.current_phase || activeJob?.stage || "Idle";
  const currentAction = digest.current_action || "等待研究启动";
  const importBlocker = latestImportBlockerSummary(events, digest);
  const qgptTasks = digest.quantgpt_task_summary || {};
  const qgptByType = qgptTasks.by_type || {};
  const latestTask = qgptTasks.latest_task || {};
  const importedFactor = digest.latest_imported_factor || {};
  const llmOutput = latestLlmOutput();
  const llmSummary = researchStepSummary(llmOutput);
  const modeBadge = researchModeBadge();
  container.innerHTML = `
    <div class="bi-head">
      <div>
        <p class="eyebrow">Research Cockpit · 研究驾驶舱</p>
        <h4>现在在干什么，为什么这么做</h4>
      </div>
      <span>${escapeHtml(text(digest.updated_at || activeJob?.started_at || "等待刷新"))}</span>
    </div>
    <div class="cockpit-status">
      <div>
        <span class="phase-chip phase-${escapeHtml(String(phase).toLowerCase().replaceAll(/[^a-z0-9]+/g, "-"))}">${escapeHtml(phase)}</span>
        <span class="badge ${escapeHtml(modeBadge.tone)}" title="${escapeHtml(modeBadge.title)}">${escapeHtml(modeBadge.label)}</span>
        <strong>${escapeHtml(text(currentAction))}</strong>
        <small>Run ${escapeHtml(text(runtime.run_id || digest.run_id || "暂无"))} · 研究记录 ${escapeHtml(text(runtimeCounts.research_steps || researchSteps().length, "0"))} · 候选 ${escapeHtml(text(runtimeCounts.candidates || liveCandidates(50).length, "0"))} · 最近 ${escapeHtml(ageLabel(runtime.updated_at || digest.updated_at))}</small>
      </div>
      ${digest.blocking_reason ? `<b class="blocking-chip">阻塞: ${escapeHtml(digest.blocking_reason)}</b>` : ""}
    </div>
    <div class="llm-now-card ${llmSummary ? "ready" : ""}">
      <div>
        <p class="eyebrow">Latest LLM Output · 最新 LLM 输出</p>
        <strong>${escapeHtml(llmSummary ? researchStepTitle(llmOutput) : "等待 Codex/MCP 写入 LLM 输出摘要")}</strong>
        <small>${escapeHtml(llmSummary ? text(llmOutput.ts || llmOutput.created_at || digest.updated_at) : "请在前台 MCP 研究关键节点调用 fxalpha_record_research_step；GUI 不会伪造 LLM 判断。")}</small>
      </div>
      <p>${escapeHtml(llmSummary || "当前已有工具层进度，但还没有 LLM 主动记录的研究判断。候选、回测和入库状态仍会继续从 QuantGPT task store 展示。")}</p>
    </div>
    <div class="bi-grid">
      <article class="bi-card accent-red">
        <span>Active 目标进度</span>
        <strong>${activeFactorCount} / ${target}</strong>
        <div class="meter"><i style="width:${pctDone}%"></i></div>
        <small>当前 active 因子 / 目标 active 因子，当前完成 ${shortNumber(pctDone, 1)}% · 本轮新增 ${newImported}</small>
      </article>
      <article class="bi-card accent-blue">
        <span>候选雷达</span>
        <strong>${candidateCount}</strong>
        <small>最近候选 ${candidateCount} · 入库门筛出 ${screened} · 拒绝 ${rejected}</small>
      </article>
      <article class="bi-card">
        <span>因子资产</span>
        <strong>${text(registry.active, "0")}</strong>
        <small>总数 ${text(registry.total, "0")} · retired ${text(registry.retired, "0")} · avg ICIR ${shortNumber(registry.avg_icir, 3)}</small>
      </article>
      <article class="bi-card">
        <span>因子地图</span>
        <strong>${escapeHtml(text(factorMapOutputs().summary?.region_count, "0"))}</strong>
        <small>信息区域 · ${escapeHtml(text(llm.model || "LLM unknown"))}</small>
      </article>
    </div>
    <div class="why-panel">
      <div>
        <p class="eyebrow">Why Not Imported · 为什么没入库</p>
        <strong>${Number(importBlocker.adopted || 0) > 0 ? `已有 ${text(importBlocker.adopted)} 个通过入库门` : "当前还没有通过入库门的候选"}</strong>
        <small>
          入库门 adopted=${text(importBlocker.adopted, "0")} · screened=${text(importBlocker.screened_out, "0")} · rejected=${text(importBlocker.rejected, "0")}
          ${importBlocker.imported !== undefined ? ` · imported=${text(importBlocker.imported, "0")} · skipped=${text(importBlocker.skipped_count, "0")}` : ""}
        </small>
      </div>
      <p>${escapeHtml(importBlocker.reasons.length ? importBlocker.reasons.join("；") : "目前仍在快筛/深度验证阶段。只有完成完整回测、诊断、抗过拟合/滚动验证后，才会进入入库门。")}</p>
    </div>
    <div class="mcp-task-strip">
      <article>
        <span>QuantGPT 工具任务</span>
        <strong>${escapeHtml(text(qgptTasks.total, "0"))}</strong>
        <small>score ${escapeHtml(text(qgptByType.score?.completed, "0"))} / backtest ${escapeHtml(text(qgptByType.backtest?.completed, "0"))} / anti ${escapeHtml(text(qgptByType.anti_overfit?.completed, "0"))} / running ${escapeHtml(text(qgptTasks.running_count, "0"))}</small>
      </article>
      <article>
        <span>最新工具</span>
        <strong>${escapeHtml(text(latestTask.task_type || latestTask.status, "暂无"))}</strong>
        <small>${escapeHtml(clip(latestTask.expression || "等待 QuantGPT task store 更新", 180))}</small>
      </article>
      <article>
        <span>最近入库因子</span>
        <strong>${escapeHtml(text(importedFactor.factor_id || importedFactor.name || "暂无"))}</strong>
        <small>${escapeHtml(clip(importedFactor.expression || "因子库暂无新增表达式", 180))}</small>
      </article>
    </div>
    ${compactCandidateTable(strictCandidates, 10)}
  `;
}

function renderProgressRail(activeJob, latestResearch) {
  const container = document.getElementById("research-progress-rail");
  if (!container) return;
  const digest = liveResearchDigest();
  const events = allResearchEvents(activeJob, latestResearch);
  const names = new Set(events.map((event) => event.event));
  const latestResearchStatus = latestResearch?.research?.status || latestResearch?.status;
  const currentPhase = digest.current_phase || "";
  const toolProgress = digest.tool_progress?.tools || {};
  const steps = [
    ["Context", names.has("agent_prompt_built") || latestResearchStatus, "载入 Prompt.md、本地配置、字段、因子地图和活跃因子池"],
    ["Validate", events.some((event) => event.tool === "validate_expression") || Number(toolProgress.validate_expression?.completed || 0) > 0, `${text(toolProgress.validate_expression?.completed, "0")} 个表达式验证完成`],
    ["Quick Score", events.some((event) => event.tool === "score_factor") || Number(toolProgress.score_factor?.completed || 0) > 0, `${text(toolProgress.score_factor?.completed, "0")} 个候选完成快筛`],
    ["Four-step", names.has("four_step_consensus"), "LLM 根据 batch 事实决定深挖、换方向或结束本轮"],
    ["Deep Validate", events.some((event) => ["run_backtest", "diagnose_factor", "run_anti_overfit", "run_rolling_validation"].includes(event.tool)) || Number(toolProgress.run_backtest?.completed || 0) > 0 || Number(toolProgress.run_anti_overfit?.completed || 0) > 0, `回测 ${text(toolProgress.run_backtest?.completed, "0")} · 抗过拟合 ${text(toolProgress.run_anti_overfit?.completed, "0")}`],
    ["Import Gate", events.some((event) => event.tool === "fxalpha_quality_gate") || Number(toolProgress.fxalpha_quality_gate?.completed || 0) > 0, "只检查已完成深度验证的候选及其 novelty 与入库资格"],
    ["Import", events.some((event) => event.tool === "fxalpha_import_factors") || Number(latestResearch?.summary?.imported || 0) > 0, "写入 factor registry，并同步模型特征层"],
    ["Done", ["Done", "completed"].includes(currentPhase) || activeJob?.status === "completed", "会话结束或等待下一轮研究"],
  ];
  container.innerHTML = `
    <div class="progress-title">
      <strong>阶段状态条</strong>
      <span>Context / Validate / Quick Score / Four-step / Deep Validate / Import Gate / Import / Done</span>
    </div>
    <div class="stepper">
      ${steps.map(([label, done, hint], index) => `
        <div class="step ${done ? "done" : ""} ${currentPhase === label ? "current" : ""}">
          <b>${done ? "✓" : index + 1}</b>
          <span>${escapeHtml(label)}</span>
          <small>${escapeHtml(hint)}</small>
        </div>
      `).join("")}
    </div>
  `;
}

function renderFourStepCards(fourStep) {
  const blocks = [
    ["事实收集", fourStep?.fact_collection || fourStep?.fact_pack, "把工具结果、候选指标、失败证据摆在桌面上"],
    ["独立判断", fourStep?.independent_judgment, "第一视角判断是否继续、换方向、深挖或入库"],
    ["交叉复审", fourStep?.cross_review, "第二视角挑战结论，避免 LLM 自嗨"],
    ["共识行动", fourStep?.consensus, "最终下一步：import、更新研究轨迹、换 hypothesis 或停止"],
  ];
  return `
    <div class="four-step-grid">
      ${blocks.map(([title, payload, hint]) => `
        <article class="four-step-card ${payload ? "ready" : ""}">
          <span>${escapeHtml(title)}</span>
          <p>${escapeHtml(clip(payload ? JSON.stringify(payload, null, 2) : hint, 360))}</p>
          ${payload ? `<details><summary>展开完整内容</summary><pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre></details>` : ""}
        </article>
      `).join("")}
    </div>
  `;
}

function renderResearchStepTimeline(steps, fallbackEvents = []) {
  const availableStages = [
    "all",
    "protocol_load",
    "pre_batch_decision",
    "thesis_design",
    "hypothesis_design",
    "expression_design",
    "candidate_plan",
    "score_review",
    "candidate_decision",
    "novelty_review",
    "deep_validation_review",
    "import_gate_review",
    "import_review",
    "round_synthesis",
    "checkpoint_stop",
    "human_guidance",
    "blocker",
    "brief",
    "four_step_summary",
    "note",
  ];
  const filteredSteps = state.researchStepFilter === "all"
    ? steps
    : steps.filter((step) => step.stage === state.researchStepFilter);
  const timeline = filteredSteps.length
    ? filteredSteps.map((step) => ({
      ts: step.ts || step.created_at,
      event: researchStepTitle(step),
      tool: "fxalpha_record_research_step",
      step: step.stage,
      summary: researchStepSummary(step),
      raw: step,
      priority: step.priority,
    }))
    : fallbackEvents;
  return `
    <div class="step-filter-bar">
      ${availableStages.map((stage) => `
        <button class="tiny-button ${state.researchStepFilter === stage ? "active" : ""}" type="button" data-step-filter="${stage}">
          ${escapeHtml(stage === "all" ? "全部" : stage)}
        </button>
      `).join("")}
    </div>
    ${!timeline.length ? `<div class="empty-state">${steps.length ? "当前筛选条件下没有 LLM 输出记录。" : "还没有 LLM 输出摘要。前台 Codex/MCP 调用 fxalpha_record_research_step 后，这里会滚动显示最近 20 条。"}</div>` : `
    <div class="ledger-list">
      ${timeline.map((item) => {
        const raw = item.raw || {};
        const transitionState = researchStepTransition(raw);
        const transition = transitionState.transition;
        const chain = researchStepChain(raw, transition);
        const stageMeta = [
          raw.round_id ? `round ${raw.round_id}` : "",
          raw.stage_id ? `stage ${raw.stage_id}` : "",
          raw.stage_seq ? `seq ${raw.stage_seq}` : "",
        ].filter(Boolean).join(" · ");
        const rawTags = Array.isArray(raw.tags) ? raw.tags.map((tag) => String(tag || "")) : [];
        const isLlmRequestProgress = rawTags.includes("llm_request_progress")
          || raw.monitoring?.event_type === "llm_request";
        const factsLabel = isLlmRequestProgress ? "上下文摘要" : "事实";
        const judgmentLabel = isLlmRequestProgress ? "请求状态" : "判断";
        return `
          <article class="ledger-card research-step-card ${item.priority === "blocker" ? "blocker-step" : ""}">
            <div class="ledger-card-head">
              <strong>${escapeHtml(text(item.event))}${item.step ? ` · ${escapeHtml(text(item.step))}` : ""}</strong>
              <span>${escapeHtml(text(item.ts))}</span>
            </div>
            <p class="transition-line">${escapeHtml(chain)}</p>
            ${stageMeta ? `<small class="muted-line">${escapeHtml(stageMeta)}</small>` : ""}
            <p>${escapeHtml(clip(item.summary || "工具/LLM 事件已记录", 720))}</p>
            ${transition.facts ? `<p><b>${escapeHtml(factsLabel)}：</b>${escapeHtml(clip(transition.facts, 360))}</p>` : ""}
            ${transition.judgment ? `<p><b>${escapeHtml(judgmentLabel)}：</b>${escapeHtml(clip(transition.judgment, 360))}</p>` : ""}
            ${transition.why ? `<p><b>为什么：</b>${escapeHtml(clip(transition.why, 520))}</p>` : ""}
            ${transition.next_action ? `<p><b>下一步：</b>${escapeHtml(clip(transition.next_action, 360))}</p>` : ""}
            ${transition.reason ? `<p><b>下一阶段理由：</b>${escapeHtml(clip(transition.reason, 360))}</p>` : ""}
            ${transition.history_used ? `<p><b>历史参考：</b>${escapeHtml(clip(transition.history_used, 260))}</p>` : ""}
            ${evidenceRefsHtml(raw)}
            <details class="raw-event">
              <summary>展开原始输入输出 / Raw JSON</summary>
              <pre>${escapeHtml(JSON.stringify(raw, null, 2))}</pre>
            </details>
          </article>
        `;
      }).join("")}
    </div>
    `}
  `;
}

function renderRoundLedger(activeJob, latestResearch) {
  const container = document.getElementById("round-ledger");
  if (!container) return;
  const digest = liveResearchDigest();
  const fourStep = digest.latest_four_step || {};
  const steps = researchSteps();
  const flightEvents = digest.recent_llm_io || [];
  const events = allResearchEvents(activeJob, latestResearch)
    .filter((event) => [
      "agent_prompt_built",
      "agent_message",
      "analysis_fact_pack_built",
      "four_step_fact_collection",
      "four_step_independent_judgment",
      "four_step_cross_review",
      "four_step_consensus",
      "four_step_protocol_blocked",
      "tool_call_completed",
      "session_started",
      "seed_prompts_built",
      "seed_stage_completed",
      "guidance_applied",
      "round_started",
      "round_completed",
      "direction_switch",
      "session_completed",
    ].includes(event.event))
    .filter((event) => event.event !== "tool_call_completed" || [
      "fxalpha_quality_gate",
      "fxalpha_import_factors",
    ].includes(event.tool));
  if (!steps.length && !flightEvents.length && !events.length) {
    container.innerHTML = `
      <div class="ledger-head">
        <div><p class="eyebrow">LLM Flight Recorder · 飞行记录仪</p><h4>LLM 输入输出与四步分析</h4></div>
      </div>
      <div class="empty-state">还没有 LLM 输出摘要。前台 Codex/MCP 调用 fxalpha_record_research_step 后，这里会滚动显示最近 20 条。</div>
    `;
    return;
  }
  const fallbackTimeline = flightEvents.length ? flightEvents : events.slice(-18).reverse().map((event) => {
    const io = event.llm_io || {};
    const input = io.input ?? event.user_prompt ?? event.direction_hint ?? event.selected_prompt ?? event.direction_prompt ?? event.prompt ?? "";
    const output = io.output ?? event.result_preview ?? event.content ?? event.fact_pack ?? event.fact_collection ?? event.independent_judgment ?? event.cross_review ?? event.consensus ?? event.top_candidates ?? event.prompts ?? event.analysis ?? event.next_prompt ?? event.updated_direction_hint ?? "";
    return {
      ts: event.ts,
      event: event.event,
      tool: event.tool,
      step: event.step,
      summary: event.quality_feedback || event.rationale || event.reason || describeEvent(event),
      raw: { input, output, event },
    };
  });
  container.innerHTML = `
    <div class="ledger-head">
      <div><p class="eyebrow">LLM Flight Recorder · 飞行记录仪</p><h4>LLM 输入输出与四步分析</h4></div>
      <span>${steps.length || fallbackTimeline.length} 条关键记录</span>
    </div>
    ${renderFourStepCards(fourStep)}
    ${renderResearchStepTimeline(steps, fallbackTimeline)}
  `;
  container.querySelectorAll("[data-step-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      state.researchStepFilter = button.dataset.stepFilter;
      renderRoundLedger(activeJob, latestResearch);
    });
  });
}

function renderRunPillbar(activeJob, latestResearch) {
  const container = document.getElementById("run-pillbar");
  const digest = liveResearchDigest();
  const latestSummary = latestResearch?.summary || latestResearch?.result?.summary || {};
  const pills = [
    { label: "当前 run", value: digest.run_id || (activeJob ? activeJob.run_id : "暂无活跃 run") },
    { label: "阶段", value: digest.current_phase || (activeJob ? activeJob.stage || activeJob.status : latestResearch?.research?.status || "空闲") },
    { label: "研究记录", value: digest.event_count || (activeJob ? activeJob.event_count : latestSummary.candidates ?? "--") },
    { label: "目标入库数", value: digest.target_adopted || activeJob?.inputs?.target_adopted || latestSummary.target_adopted || "--" },
  ];
  container.innerHTML = pills.map((pill) => `
    <div class="run-pill">
      <span>${escapeHtml(pill.label)}</span>
      <strong>${escapeHtml(text(pill.value))}</strong>
    </div>
  `).join("");
}

function renderActiveRunMeta(activeJob, latestResearch) {
  const container = document.getElementById("active-run-meta");
  const digest = liveResearchDigest();
  const events = activeJob?.events || [];
  const sessionStarted = latestEventByName(events, "session_started");
  const guidanceRecords = guidanceImpactRecords(researchSteps(), activeJob?.guidance_history || []);
  const pendingGuidance = guidanceRecords.find((item) => !item.delivery && !item.superseded) || null;
  const latestGuidanceResult = guidanceRecords.find((item) => item.delivery || item.superseded) || null;
  const latestGate = currentRoundQualityGate(digest.latest_quality_gate || {}, digest);
  const consensus = digest.latest_four_step?.consensus || {};
  const llm = digest.latest_llm_step || {};
  const directionHistory = latestResearch?.result?.direction_health?.direction_history
    || latestResearch?.direction_health?.direction_history
    || [];
  const directionLast = directionHistory[directionHistory.length - 1] || {};
  container.innerHTML = `
    <div class="meta-card">
      <span>研究主管</span>
      <strong>${escapeHtml(digest.quantgpt_task_summary?.total ? "Codex 直连 MCP 观测中" : text(consensus.action || sessionStarted?.selected_family || directionLast.family, "等待判断"))}</strong>
      <small>${escapeHtml(digest.quantgpt_task_summary?.total ? clip(digest.current_action || "已接入 QuantGPT task store，正在汇总真实工具调用。", 220) : clip(consensus.rationale || directionLast.rationale || sessionStarted?.rationale || llm.summary || "暂无方向说明", 220))}</small>
    </div>
    <div class="meta-card">
      <span>最新 Gate</span>
      <strong>${escapeHtml(text(latestGate.counts ? `${latestGate.counts.adopted}/${latestGate.counts.screened_out}/${latestGate.counts.rejected}` : "暂无"))}</strong>
      <small>adopted / screened / rejected · ${escapeHtml(clip(latestGate.feedback || "等待质量门返回", 180))}</small>
    </div>
    <div class="meta-card">
      <span>最近候选</span>
      <strong>${escapeHtml(text(liveCandidates(50).length, "0"))}</strong>
      <small>${escapeHtml(text(liveCandidates(1)[0]?.expression, "还没有 current_candidate_board 候选"))}</small>
    </div>
    <div class="meta-card">
      <span>因子地图</span>
      <strong>${escapeHtml(text(factorMapOutputs().status, "未加载"))}</strong>
      <small>${escapeHtml(text(factorMapOutputs().map_id, "切换到因子地图页面读取最新审计与研究轨迹"))}</small>
    </div>
    <div class="meta-card">
      <span>一次性干预</span>
      <strong>${escapeHtml(pendingGuidance ? "待执行" : latestGuidanceResult?.response ? "已执行" : latestGuidanceResult?.delivery ? "已送达" : "无")}</strong>
      <small>${escapeHtml(clip(
        pendingGuidance?.guidance?.summary
          || latestGuidanceResult?.response?.stage_transition?.judgment
          || latestGuidanceResult?.response?.summary
          || latestGuidanceResult?.guidance?.summary
          || "只影响下一次 LLM 判断",
        180,
      ))}</small>
    </div>
  `;
}

function renderPromptStack(activeJob, latestResearch) {
  const container = document.getElementById("prompt-stack");
  const digest = liveResearchDigest();
  const latestAgentMessage = latestLlmOutput();
  const steps = researchSteps();
  const fourStep = digest.latest_four_step || {};
  const consensus = fourStep.consensus || {};
  const task = digest.quantgpt_task_summary?.latest_task || {};

  const cards = [
    {
      title: "最新 LLM 输出摘要",
      body: researchStepSummary(latestAgentMessage) || "还没有捕获到 LLM 自然语言输出。请在 MCP 研究关键节点调用 fxalpha_record_research_step。",
      raw: latestAgentMessage,
    },
    {
      title: "四步分析共识",
      body: consensus.action
        ? `${consensus.action}\n\n${consensus.rationale || ""}`
        : "等待 batch score + quality gate 后进入四步分析",
      raw: consensus,
    },
    {
      title: "最近 20 条 research_steps",
      body: steps.length
        ? steps.map((step) => `[${text(step.ts || step.created_at)}] ${researchStepTitle(step)}\n${researchStepSummary(step)}`).join("\n\n")
        : "暂无 research_steps。这里不会展示内部 system prompt，只展示 LLM 主动记录的研究输出摘要。",
      raw: steps,
    },
    {
      title: "当前 / 最近 MCP 工具任务",
      body: task.expression
        ? `${text(task.task_type)} · ${text(task.status)} · ${taskDuration(task)}\n${task.expression}`
        : "当前没有 QuantGPT MCP 工具任务。",
      raw: task,
    },
    {
      title: "研究配置",
      body: JSON.stringify(activeJob?.inputs || {}, null, 2) || "等待本次研究配置",
      raw: activeJob?.inputs,
    },
  ];

  container.innerHTML = cards.map((card) => `
    <article class="prompt-card">
      <p>${escapeHtml(card.title)}</p>
      <pre>${escapeHtml(clip(card.body, 1200))}</pre>
      ${card.raw ? `<details class="raw-event"><summary>展开完整内容</summary><pre>${escapeHtml(typeof card.raw === "string" ? card.raw : JSON.stringify(card.raw, null, 2))}</pre></details>` : ""}
    </article>
  `).join("");
}

function candidateMetricLine(candidate) {
  const summary = candidateMetrics(candidate);
  const longOnly = candidate?.best_long_only_group_metrics || {};
  const decision = candidateDecision(candidate);
  const reasons = candidateRejectReasons(candidate);
  const auto = candidate?.autocorrelation || candidate?.metrics?.autocorrelation || {};
  const ao = candidate?.anti_overfit_summary || candidate?.anti_overfit || {};
  const novelty = candidate?.novelty_guard || candidate?.deep_validation?.novelty_correlation || candidate?.novelty_correlation || {};
  const neutral = candidate?.neutralization_applied || candidate?.metrics?.neutralization_applied || {};
  const capNeutral = neutral.cap?.applied ? `市值中性:${neutral.cap.source || "on"}` : "市值中性:skip";
  const industryNeutral = neutral.industry?.applied ? `行业中性:${neutral.industry.source || "on"}` : "行业中性:skip";
  return `
    <div class="candidate-metrics">
      <span>IC ${shortNumber(summary.ic_mean, 4)}</span>
      <span>ICIR ${shortNumber(summary.ic_ir, 4)}</span>
      <span>Rank IC ${shortNumber(summary.rank_ic_mean, 4)}</span>
      <span>Rank ICIR ${shortNumber(summary.rank_ic_ir, 4)}</span>
      <span>Sharpe ${shortNumber(summary.sharpe, 3)}</span>
      <span>年化 ${pct(summary.annual_return, 2)}</span>
      <span>Quick ${shortNumber(summary.quick_score, 1)}</span>
      <span>Deep ${shortNumber(summary.deep_score, 1)}</span>
      <span>Long-only ${shortNumber(longOnly.sharpe ?? summary.sharpe, 3)} / ${pct(longOnly.annual_return ?? summary.annual_return, 2)}</span>
      <span>换手 ${pct(summary.turnover, 2)}</span>
      <span>回撤 ${pct(summary.max_drawdown, 2)}</span>
      <span>决策 ${escapeHtml(text(decision))}</span>
      <span>Novelty P ${shortNumber(novelty.max_existing_pearson, 3)} / R ${shortNumber(novelty.max_existing_rank_corr, 3)}</span>
      <span>Persistence ${escapeHtml(text(auto.risk_flag, "normal"))}</span>
      <span>过拟合 ${escapeHtml(text(ao.recommendation || ao.score, "--"))}</span>
      <span>${escapeHtml(capNeutral)}</span>
      <span>${escapeHtml(industryNeutral)}</span>
      ${reasons.length ? `<span>否决 ${escapeHtml(reasons.join(", "))}</span>` : ""}
    </div>
  `;
}

function resolveReportUrl(reportUrl, qgptUrl) {
  if (!reportUrl) return "";
  if (/^https?:\/\//.test(reportUrl)) return reportUrl;
  if (!qgptUrl) return reportUrl;
  return `${qgptUrl.replace(/\/$/, "")}${reportUrl.startsWith("/") ? "" : "/"}${reportUrl}`;
}

function setInspector(kind, payload) {
  state.inspector = { kind, payload };
  renderInspector();
}

function paintInspector(selector, html) {
  const container = document.querySelector(selector);
  if (!container) return;
  container.hidden = false;
  container.innerHTML = html;
}

function hideInspector(selector) {
  const container = document.querySelector(selector);
  if (!container) return;
  container.innerHTML = "";
  container.hidden = true;
}

function screeningSummary(candidate) {
  const screening = candidate.screening || candidate.gate_result || {};
  const novelty = candidate.novelty_metrics || candidate.novelty_guard || screening.novelty_guard || {};
  return candidate?.quality_gate_reason
    || candidate?.novelty_reason
    || candidate?.deep_validation?.next_round_feedback
    || screening.summary
    || screening.reason
    || novelty.reason
    || "--";
}

function renderCandidateGrid(activeJob, latestResearch) {
  const container = document.getElementById("candidate-grid");
  const digest = liveResearchDigest();
  const board = currentCandidateBoard();
  let candidates = liveCandidates(24);
  if (candidates.length) {
    candidates = candidates
      .sort((a, b) => {
        const aAdopt = String(a.quality_decision || a.gate_result?.decision || "").includes("adopt") ? 1 : 0;
        const bAdopt = String(b.quality_decision || b.gate_result?.decision || "").includes("adopt") ? 1 : 0;
        if (aAdopt !== bAdopt) return bAdopt - aAdopt;
        return candidateSortValue(b, state.candidateSort) - candidateSortValue(a, state.candidateSort);
      })
      .slice(0, 24);
  }
  const boardErrors = board?.errors || [];
  if (!candidates.length) {
    container.innerHTML = `
      <div class="empty-state ${board?.ok === false || board?.schema_version !== "current_candidate_board_v1" ? "error-state" : ""}">
        ${board?.schema_version === "current_candidate_board_v1"
          ? "当前 round 没有可展示候选。"
          : "候选雷达缺少 current_candidate_board；已禁用 event / quality payload / research history fallback。"}
        ${boardErrors.length ? `<small>${escapeHtml(boardErrors.slice(0, 3).map((item) => item.code || item.message || "schema_error").join("；"))}</small>` : ""}
      </div>
    `;
    return;
  }
  const gate = currentRoundQualityGate(digest.latest_quality_gate || {}, digest);
  container.innerHTML = candidates.map((candidate, index) => {
    const metrics = candidateMetrics(candidate);
    const auto = candidate.autocorrelation || candidate.metrics?.autocorrelation || {};
    const reportUrl = resolveReportUrl(candidate.report_url, activeJob?.inputs?.qgpt_url || latestResearch?.inputs?.qgpt_url);
    const decision = candidateDecision(candidate);
    const stage = candidateStageLabel(candidate);
    const reasons = candidateRejectReasons(candidate);
    const veto = reasons.some((reason) => /corr|correlation|autocorr|novelty|information/i.test(String(reason)))
      || /high|low_information|reject|screen/i.test(String(auto.risk_flag || decision));
    const near = isNearThresholdCandidate(candidate);
    return `
      <article class="candidate-card radar-card ${decision === "adopt" ? "adopted" : ""} ${veto ? "veto" : ""} ${near ? "near-threshold" : ""}" data-candidate-index="${index}">
        <div class="candidate-head">
          <div>
            <span class="badge grade-${String(candidateGrade(candidate) || "p").toLowerCase()}">${escapeHtml(text(candidateGrade(candidate), "--"))}</span>
            <strong>${shortNumber(metrics.deep_score ?? metrics.quick_score, 1)}</strong>
          </div>
          <div class="candidate-head-tags">
            <span class="stage-chip">${escapeHtml(stage)}</span>
            <span class="badge subtle">${escapeHtml(text(decision))}</span>
          </div>
        </div>
        <div class="candidate-name">${escapeHtml(text(candidateDisplayName(candidate, `candidate-${index + 1}`)))}</div>
        <code>${escapeHtml(text(candidate.expression, "暂无表达式"))}</code>
        ${candidateMetricLine(candidate)}
        <div class="candidate-footer">
          <span>Persistence：${escapeHtml(text(auto.risk_flag || auto.max_corr || auto.pearson, "normal"))}</span>
          <span>${escapeHtml(text(reasons.join(", ") || screeningSummary(candidate), ""))}</span>
        </div>
        <div class="candidate-actions">
          <button class="tiny-button" type="button" data-inspect-candidate="${index}">查看详情</button>
          ${reportUrl ? `<a class="inline-link" href="${escapeHtml(reportUrl)}" target="_blank" rel="noreferrer">打开回测报告</a>` : ""}
        </div>
      </article>
    `;
  }).join("");
  container.insertAdjacentHTML("afterbegin", `
    <div class="radar-summary">
      <strong>候选因子雷达</strong>
      <span>候选 ${escapeHtml(text(candidates.length, "0"))}</span>
      <span>adopted ${escapeHtml(text(gate.counts?.adopted, "0"))}</span>
      <span>screened ${escapeHtml(text(gate.counts?.screened_out, "0"))}</span>
      <span>rejected ${escapeHtml(text(gate.counts?.rejected, "0"))}</span>
      <div class="radar-sort">
        ${[
          ["time", "时间"],
          ["score", "Score"],
          ["abs_ic", "|IC|"],
          ["rank_icir", "Rank ICIR"],
          ["sharpe", "Sharpe"],
          ["annual_return", "年化"],
        ].map(([mode, label]) => `
          <button class="tiny-button ${state.candidateSort === mode ? "active" : ""}" type="button" data-candidate-sort="${mode}">${label}</button>
        `).join("")}
      </div>
    </div>
  `);

  container.querySelectorAll("[data-inspect-candidate]").forEach((button) => {
    button.addEventListener("click", () => {
      const candidate = candidates[Number(button.dataset.inspectCandidate)];
      setInspector("candidate", candidate);
    });
  });

  container.querySelectorAll("[data-candidate-sort]").forEach((button) => {
    button.addEventListener("click", () => {
      state.candidateSort = button.dataset.candidateSort;
      renderCandidateGrid(activeJob, latestResearch);
    });
  });

  if (!state.inspector && candidates.length) {
    setInspector("candidate", candidates[0]);
  }
}

function describeEvent(event) {
  switch (event.event) {
    case "session_started":
      return `已选择研究机制/方向 ${text(event.selected_family)}，目标入库 ${text(event.target_adopted)} 个因子。`;
    case "seed_prompts_built":
      return `已生成 ${text(event.seed_count)} 个 Seed Prompt，用于启动第一批研究假设。`;
    case "seed_stage_completed":
      return `Seed 阶段完成，成功 ${text(event.seed_success_count)} 个，其中 ${text(event.import_ready_count)} 个达到可入库标准。`;
    case "baseline_selected":
      return `已选择新 baseline：${clip(event.expression, 120)}。来源：${text(event.source)}。`;
    case "guidance_received":
      return `已收到人工干预：${clip(event.guidance?.message, 120)}`;
    case "guidance_applied":
      return "人工干预已送达下一次模型判断，本条现已消费。";
    case "round_started":
      return `第 ${text(event.round)} 轮已启动，基于 baseline ${clip(event.baseline_expression, 90)}。`;
    case "round_completed":
      return `第 ${text(event.round)} 轮完成，共得到 ${text(event.candidate_count)} 个候选，其中 ${text(event.adopted_count)} 个达到可入库标准。`;
    case "direction_switch":
      return `研究方向已切换，原因：${text(event.reason)}。`;
    case "session_completed":
      return `本次研究会话结束，停止原因：${text(event.stop_reason)}。`;
    case "agent_prompt_built":
      return "Codex 已加载官方 PROMPT.md、FACTOR_MINING 和 FXAlpha 本地化 appendix，准备直连 MCP 研究。";
    case "agent_message":
      return `LLM 输出：${clip(event.content, 180)}`;
    case "analysis_fact_pack_built":
      return "四步分析事实包已生成：包含 batch 候选、score/gate 指标、持久性诊断、novelty 与近期研究经验约束。";
    case "four_step_fact_collection":
      return `四步分析 1/4 事实收集：${clip(JSON.stringify(event.fact_collection || {}, null, 2), 220)}`;
    case "four_step_independent_judgment":
      return `四步分析 2/4 独立判断：${clip(JSON.stringify(event.independent_judgment || {}, null, 2), 220)}`;
    case "four_step_cross_review":
      return `四步分析 3/4 第二视角复审：${clip(JSON.stringify(event.cross_review || {}, null, 2), 220)}`;
    case "four_step_consensus":
      return `四步分析 4/4 共识行动：${clip(JSON.stringify(event.consensus || {}, null, 2), 220)}`;
    case "four_step_protocol_blocked":
      return `四步分析协议拦截：${text(event.reason)}，先完成分析再继续工具调用。`;
    case "tool_call_completed":
      if (event.tool === "fxalpha_quality_gate") {
        const payload = toolPayloadFromPreview(event);
        const counts = payload.counts || {};
        return `入库门完成：adopted=${text(counts.adopted ?? (payload.adopted || []).length, "0")}，screened_out=${text(counts.screened_out ?? (payload.screened_out || []).length, "0")}。`;
      }
      if (event.tool === "score_factor") return "单因子快筛完成：已返回 IC/IR、RankICIR、Sharpe、年化和 Grade。";
      if (event.tool === "run_backtest") return "完整分组回测完成：可进入诊断、抗过拟合、滚动验证或入库门。";
      if (event.tool === "fxalpha_import_factors") {
        const payload = toolPayloadFromPreview(event);
        return `入库工具完成：imported=${text(payload.imported, "0")}，skipped=${text(payload.skipped, "0")}。`;
      }
      return `${event.tool || "tool"} 调用完成。`;
    case "job_finished":
      return event.ok ? "后台研究运行已完成。" : `后台研究运行失败：${text(event.err)}`;
    default:
      return JSON.stringify(event);
  }
}

function guidanceIdentifier(item = {}) {
  return text(item?.extra?.guidance_id || item.guidance_id || item.stage_id || "", "");
}

function guidanceDeliveryRef(step = {}, guidance = {}) {
  const guidanceId = guidanceIdentifier(guidance);
  const guidanceStageId = text(guidance.stage_id || "", "");
  return (step.evidence_refs || []).find((ref) => {
    if (!ref || ref.type !== "operator_guidance_delivery") return false;
    if (guidanceId && text(ref.guidance_id || "", "") === guidanceId) return true;
    return Boolean(guidanceStageId && text(ref.guidance_stage_id || "", "") === guidanceStageId);
  }) || null;
}

function guidanceImpactRecords(steps = [], fallbackItems = []) {
  const ordered = [...(steps || [])].sort((left, right) => (
    (parseIso(left.ts || left.created_at)?.getTime() || 0)
    - (parseIso(right.ts || right.created_at)?.getTime() || 0)
  ));
  const guidanceSteps = ordered.filter((step) => step.stage === "human_guidance");
  const sourceItems = guidanceSteps.length ? guidanceSteps : (fallbackItems || []);
  return [...sourceItems].reverse().map((guidance) => {
    const guidanceTime = parseIso(guidance.ts || guidance.created_at)?.getTime() || 0;
    const delivery = ordered.find((step) => {
      const stepTime = parseIso(step.ts || step.created_at)?.getTime() || 0;
      return stepTime >= guidanceTime && Boolean(guidanceDeliveryRef(step, guidance));
    }) || null;
    const deliveryRef = delivery ? guidanceDeliveryRef(delivery, guidance) : null;
    const traceId = text(delivery?.llm_trace_id || deliveryRef?.trace_id || "", "");
    const response = traceId
      ? ordered.find((step) => {
        const tags = (step.tags || []).map((tag) => String(tag).toLowerCase());
        return text(step.llm_trace_id || "", "") === traceId
          && tags.includes("llm_result")
          && step.stage !== "human_guidance";
      }) || null
      : null;
    const superseded = !delivery && guidanceSteps.some((item) => (
      (parseIso(item.ts || item.created_at)?.getTime() || 0) > guidanceTime
    ));
    return { guidance, delivery, deliveryRef, response, superseded };
  });
}

function guidanceImpactMarkup(record, { history = false } = {}) {
  const { guidance, delivery, deliveryRef, response, superseded } = record;
  const guidanceExtra = guidance.extra || {};
  const transition = response?.stage_transition || {};
  const deliveredStage = deliveryRef?.delivered_to_stage || delivery?.stage || "";
  const status = response ? "已执行" : delivery ? "已送达" : superseded ? "已被替代" : "待下一次判断";
  const statusClass = response ? "is-complete" : delivery ? "is-delivered" : superseded ? "is-superseded" : "is-pending";
  const responseText = transition.judgment || response?.summary || response?.decision || "";
  const nextStage = transition.next_stage || "";
  const nextAction = transition.next_action || "";
  return `
    <article class="guidance-impact-item ${statusClass}${history ? " is-history" : ""}">
      <div class="guidance-impact-meta">
        <span class="guidance-impact-status">${escapeHtml(status)}</span>
        <span>${escapeHtml(text(guidance.ts || guidance.created_at, ""))}</span>
        <span>${escapeHtml(text(guidanceExtra.author || guidance.author, "operator"))}</span>
      </div>
      <p class="guidance-impact-message">${escapeHtml(clip(guidance.summary || guidance.message || "", history ? 140 : 220))}</p>
      ${delivery ? `
        <div class="guidance-impact-receipt">
          <b>送达</b>
          <span>${escapeHtml(researchStepTitle({ stage: deliveredStage }))} · ${escapeHtml(compactRoundLabel(delivery.round_id) || text(delivery.round_id, "--"))}</span>
        </div>
      ` : `
        <div class="guidance-impact-receipt is-muted">
          <b>${superseded ? "未执行" : "下一步"}</b>
          <span>${superseded ? "提交新干预前尚未送达，本条已失效" : "只进入下一次 DeepSeek 阶段判断"}</span>
        </div>
      `}
      ${response ? `
        <div class="guidance-impact-response">
          <b>模型判断</b>
          <p>${escapeHtml(clip(responseText, history ? 160 : 240))}</p>
          <small>${escapeHtml([
            response.decision ? `decision ${response.decision}` : "",
            nextStage ? `next ${researchStepTitle({ stage: nextStage })}` : "",
            nextAction ? clip(nextAction, 80) : "",
          ].filter(Boolean).join(" · "))}</small>
        </div>
      ` : ""}
    </article>
  `;
}

function renderGuidance(activeJob) {
  const note = document.getElementById("guidance-note");
  const history = document.getElementById("guidance-history");
  const submitButton = document.querySelector("#guidance-form button[type='submit']");
  if (!note || !history || !submitButton) return;
  const steps = researchSteps();
  const guidanceSteps = steps.filter((step) => step.stage === "human_guidance");
  const control = commandControlOutputs();
  const canGuide = Boolean(control.run_id && (control.allowed_actions || []).includes("guidance"));
  if (state.backendMode !== "console") {
    note.textContent = "人工干预记录依赖实时控制台接口；当前仅显示离线快照或空态。";
    submitButton.disabled = true;
    history.innerHTML = `<div class="empty-state">当前不是 live console：研究历史可离线查看，但实时干预需要 /factor/console/live 恢复。</div>`;
    return;
  }
  submitButton.disabled = !canGuide;
  note.textContent = canGuide
    ? `当前 run：${compactResearchRunLabel(control.run_id) || control.run_id}。仅影响下一次 LLM 判断；再次提交会替代尚未送达的旧干预。单条最多 500 字。`
    : "当前没有可接收干预的运行中或暂停 run。";
  const records = guidanceImpactRecords(steps, activeJob?.guidance_history || []);
  if (!records.length) {
    history.innerHTML = `<div class="empty-state compact">没有待执行干预。发送后，这里只显示当前待执行项和最近一次结果。</div>`;
    return;
  }
  const pending = records.find((item) => !item.delivery && !item.superseded) || null;
  const latestResult = records.find((item) => item.delivery || item.superseded) || null;
  const visibleIds = new Set([pending, latestResult].filter(Boolean).map((item) => guidanceIdentifier(item.guidance)));
  const older = records.filter((item) => !visibleIds.has(guidanceIdentifier(item.guidance))).slice(0, 8);
  history.innerHTML = `
    <div class="guidance-current-grid">
      <section class="guidance-current-slot">
        <span class="guidance-slot-label">待执行干预</span>
        ${pending ? guidanceImpactMarkup(pending) : `<div class="guidance-slot-empty">无。已送达的干预不会继续影响后续阶段。</div>`}
      </section>
      <section class="guidance-current-slot">
        <span class="guidance-slot-label">最近一次结果</span>
        ${latestResult ? guidanceImpactMarkup(latestResult) : `<div class="guidance-slot-empty">尚无模型送达回执。</div>`}
      </section>
    </div>
    ${older.length ? `
      <details class="guidance-receipt-history">
        <summary>历史回执 ${older.length}</summary>
        <div class="guidance-receipt-history-list">${older.map((item) => guidanceImpactMarkup(item, { history: true })).join("")}</div>
      </details>
    ` : ""}
  `;
}

function renderRecentNotes(notes) {
  const container = document.getElementById("recent-notes");
  if (!notes || !notes.length) {
    container.innerHTML = `<div class="empty-state">当前还没有归档研究笔记。</div>`;
    return;
  }
  container.innerHTML = notes.map((note) => `
    <article class="note-card">
      <div class="note-head">
        <strong>${escapeHtml(note.name)}</strong>
        <span>${escapeHtml(text(note.updated_at))}</span>
      </div>
      <pre>${escapeHtml(clip(note.preview, 700))}</pre>
    </article>
  `).join("");
}

function renderFactorMapWorkspace() {
  const container = document.getElementById("factor-map-board");
  if (!container) return;
  const map = serviceOutputs(state.factorMap);
  const audit = map.audit || {};
  const summary = map.summary || {};
  const regions = coerceArray(map.regions);
  const observations = coerceArray(map.recent_observations).slice().reverse();
  const activeRunId = activeResearchRunId();
  const activity = map.region_activity || {};
  const relations = coerceArray(map.region_relations);
  const topPairs = coerceArray(map.top_correlated_pairs);
  const loading = state.factorMapLoading && !map.map_id;
  const status = text(map.status, loading ? "loading" : "missing");
  const importedNearRegions = Object.values(activity).reduce(
    (total, item) => total + Number(item?.imported_near_region || 0),
    0,
  );
  const familyCode = (region, fallback = "–") => {
    const clusterId = text(region?.cluster_id || region?.semantic_profile?.cluster_id, "");
    const suffix = clusterId.replace(/^information_/, "");
    return /^\d+$/.test(suffix) ? suffix.padStart(3, "0") : fallback;
  };
  const currentRunByRegion = new Map();
  observations.filter((item) => activeRunId && text(item?.run_id) === activeRunId).forEach((item) => {
    const uid = text(item?.region_uid);
    if (!uid) return;
    const entry = currentRunByRegion.get(uid) || { count: 0, imported: 0, outcomes: new Set() };
    entry.count += 1;
    entry.imported += /^imported/.test(text(item?.outcome).toLowerCase()) ? 1 : 0;
    entry.outcomes.add(text(item?.outcome));
    currentRunByRegion.set(uid, entry);
  });
  const currentRunUnmapped = coerceArray(map.unmapped_evidence)
    .filter((item) => activeRunId && text(item?.run_id) === activeRunId).length;
  const regionByUid = new Map(regions.map((region, index) => [
    text(region.region_uid),
    { region, index: familyCode(region, String(index + 1)) },
  ]));
  const relationDegree = relations.reduce((accumulator, relation) => {
    [relation?.source_region_uid, relation?.target_region_uid].forEach((uid) => {
      if (!uid) return;
      accumulator[uid] = (accumulator[uid] || 0) + 1;
    });
    return accumulator;
  }, {});
  const pct = (numerator, denominator) => {
    const base = Number(denominator || 0);
    return base > 0 ? Math.round((Number(numerator || 0) / base) * 100) : 0;
  };
  const regionRisk = (regionActivity) => {
    const level = text(regionActivity?.guidance?.level, "insufficient_evidence");
    if (level === "action") return "action";
    if (level === "observe") return "observe";
    return "quiet";
  };
  const regionNodes = regions.map((region, index) => {
    const uid = text(region.region_uid);
    const theta = (Math.PI * 2 * index / Math.max(regions.length, 1)) - Math.PI / 2;
    return {
      uid,
      index: familyCode(region, String(index + 1)),
      x: 500 + Math.cos(theta) * 382,
      y: 265 + Math.sin(theta) * 178,
      region,
      activity: activity[uid] || {},
      currentRun: currentRunByRegion.get(uid) || { count: 0, imported: 0, outcomes: new Set() },
    };
  });
  const nodeByUid = new Map(regionNodes.map((node) => [node.uid, node]));
  const currentRunRegionCount = regionNodes.filter((node) => Number(node.currentRun.count || 0) > 0).length;
  const currentRunImportedRegionCount = regionNodes.filter((node) => Number(node.currentRun.imported || 0) > 0).length;
  const currentRunActionRegionCount = regionNodes.filter((node) => (
    Number(node.currentRun.count || 0) > 0 && regionRisk(node.activity) === "action"
  )).length;
  const runOverlayByClusterId = new Map(regionNodes
    .filter((node) => Number(node.currentRun.count || 0) > 0)
    .map((node) => [text(node.region.cluster_id), {
      count: Number(node.currentRun.count || 0),
      imported: Number(node.currentRun.imported || 0),
      action: regionRisk(node.activity) === "action",
    }]));
  const globalRelationGraph = renderRelationGraphMarkup(map.relation_graph || {}, regions, topPairs, {
    runOverlayByClusterId,
  });
  const networkEdges = relations
    .filter((relation) => nodeByUid.has(relation?.source_region_uid) && nodeByUid.has(relation?.target_region_uid))
    .map((relation) => {
      const source = nodeByUid.get(relation.source_region_uid);
      const target = nodeByUid.get(relation.target_region_uid);
      const strength = Number(relation.dependency_score || 0);
      return `<line x1="${source.x.toFixed(1)}" y1="${source.y.toFixed(1)}" x2="${target.x.toFixed(1)}" y2="${target.y.toFixed(1)}" class="factor-map-network-link" style="--link-strength:${Math.max(0.2, Math.min(strength, 1)).toFixed(2)}"><title>${escapeHtml(`#${source.index} ↔ #${target.index} · 依赖强度 ${strength.toFixed(2)}`)}</title></line>`;
    }).join("");
  const networkNodes = regionNodes.map((node) => {
    const profile = node.region.semantic_profile || {};
    const regionActivity = node.activity;
    const activeCount = Number(profile.active_factor_count || node.region.size || 0);
    const radius = Math.min(29, 15 + activeCount * 2.2);
    const risk = regionRisk(regionActivity);
    const title = `#${node.index} ${text(profile.name, node.uid)}\n${activeCount} 个活跃因子 · ${relationDegree[node.uid] || 0} 条跨区关系 · ${Number(regionActivity.trajectory_count || 0)} 条研究轨迹`;
    return `<button type="button" class="factor-map-network-node is-${risk}" data-factor-map-region="${escapeHtml(node.uid)}" style="--node-x:${node.x.toFixed(1)};--node-y:${node.y.toFixed(1)};--node-size:${radius.toFixed(1)}" title="${escapeHtml(title)}"><span>${node.index}</span></button>`;
  }).join("");
  const networkKey = regionNodes.map((node) => {
    const profile = node.region.semantic_profile || {};
    const activeCount = Number(profile.active_factor_count || node.region.size || 0);
    const risk = regionRisk(node.activity);
    return `<button type="button" class="factor-map-network-key-item is-${risk}" data-factor-map-region="${escapeHtml(node.uid)}" title="查看 ${escapeHtml(text(profile.name, node.uid))} 的详细说明">
      <i>${node.index}</i><span>${escapeHtml(clip(text(profile.name, node.uid), 18))}</span><small>${activeCount} 个因子</small>
    </button>`;
  }).join("");
  const heatmap = regionNodes
    .slice()
    .sort((left, right) => Number(right.activity.trajectory_count || 0) - Number(left.activity.trajectory_count || 0))
    .map((node) => {
      const profile = node.region.semantic_profile || {};
      const regionActivity = node.activity;
      const currentRun = node.currentRun;
      const intensity = Math.min(1, Math.max(
        Number(regionActivity.trajectory_count || 0) / 32,
        Number(currentRun.count || 0) / 8,
      ));
      const deepRate = pct(regionActivity.deep_rejected, regionActivity.deep_checked);
      const risk = regionRisk(regionActivity);
      const currentRunText = Number(currentRun.count || 0)
        ? `本 run ${currentRun.count}${currentRun.imported ? ` · 入库 ${currentRun.imported}` : ""}`
        : "本 run 未覆盖";
      const runState = Number(currentRun.imported || 0)
        ? `<span class="factor-map-heat-run-state is-imported">入库</span>`
        : Number(currentRun.count || 0)
          ? `<span class="factor-map-heat-run-state">本 run</span>`
          : "";
      return `<button type="button" class="factor-map-heat-cell is-${risk}${Number(currentRun.count || 0) ? " is-current-run" : ""}${Number(currentRun.imported || 0) ? " is-current-run-imported" : ""}" data-factor-map-region="${escapeHtml(node.uid)}" style="--activity:${intensity.toFixed(2)}" title="${escapeHtml(text(profile.name, node.uid))}">
        <span class="factor-map-heat-index">${node.index}</span>
        <strong>${escapeHtml(clip(text(profile.name, node.uid), 22))}${runState}</strong>
        <small>${escapeHtml(currentRunText)} · 累计 ${escapeHtml(text(regionActivity.trajectory_count, 0))} · 深验未过 ${deepRate}%</small>
        <i class="factor-map-heat-level" aria-hidden="true"><b></b></i>
      </button>`;
    }).join("");
  const regionCards = regions.map((region) => {
    const regionUid = text(region.region_uid, "--");
    const regionIndex = regionByUid.get(regionUid)?.index || "–";
    const regionActivity = activity[regionUid] || {};
    const profile = region.semantic_profile || {};
    const fields = coerceArray(profile.core_fields);
    const structures = coerceArray(profile.core_structures);
    const guidance = regionActivity.guidance || {};
    const noveltyRate = pct(regionActivity.novelty_rejected, regionActivity.novelty_checked);
    const deepRate = pct(regionActivity.deep_rejected, regionActivity.deep_checked);
    const risk = regionRisk(regionActivity);
    return `
      <details class="factor-map-region-card is-${risk}" data-region-uid="${escapeHtml(regionUid)}">
        <summary>
          <span>
            <small><i class="factor-map-region-index">${regionIndex}</i>方向 ${regionIndex} · ${escapeHtml(text(profile.combination_form, "信息组合"))}</small>
            <strong>${escapeHtml(text(profile.name, regionUid))}</strong>
          </span>
          <span class="factor-map-region-metrics">
            <b>已有 ${escapeHtml(text(profile.active_factor_count, region.size || 0))} 个</b>
            <b>尝试 ${escapeHtml(text(regionActivity.trajectory_count, 0))} 次</b>
            <b>${escapeHtml(text(regionActivity.imported_near_region, 0))} 入库</b>
          </span>
        </summary>
        <div class="factor-map-region-detail">
          <code>${escapeHtml(regionUid)}</code>
          <div class="factor-map-member-list">
            ${fields.map((field) => `<span title="${escapeHtml(text(field.usage, field.meaning))}">${escapeHtml(text(field.meaning || field.field, "--"))} · ${escapeHtml(text(field.usage, "核心字段"))}</span>`).join("")}
          </div>
          <p>${escapeHtml(structures.length ? `主要结构：${structures.join("；")}` : "该区域尚未形成可稳定归纳的共同结构。")}</p>
          <div class="factor-map-stage-bars" aria-label="研究检查结果">
            <span><small>与已有因子太像</small><i><b style="--stage-value:${noveltyRate}%"></b></i><em>${noveltyRate}%</em></span>
            <span><small>深度检验没有通过</small><i><b style="--stage-value:${deepRate}%"></b></i><em>${deepRate}%</em></span>
          </div>
          <div class="factor-map-funnel">
            <span><small>新颖性检查</small><b>${escapeHtml(text(regionActivity.novelty_checked, 0))}</b></span>
            <span><small>新颖性拒绝</small><b>${escapeHtml(text(regionActivity.novelty_rejected, 0))}</b></span>
            <span><small>深度验证</small><b>${escapeHtml(text(regionActivity.deep_checked, 0))}</b></span>
            <span><small>深度未通过</small><b>${escapeHtml(text(regionActivity.deep_rejected, 0))}</b></span>
          </div>
          ${guidance.instruction ? `<p class="factor-map-guidance is-${escapeHtml(text(guidance.level, "observe"))}">${escapeHtml(guidance.instruction)}</p>` : ""}
        </div>
      </details>
    `;
  }).join("");
  container.innerHTML = `
    <header class="factor-map-toolbar">
      <div>
        <p class="eyebrow">Factor Map · 因子地图</p>
        <div class="factor-map-title-row"><h3>因子地图</h3></div>
        <small>活跃因子库的信息家族关系，与本轮研究在各家族的覆盖轨迹。地图仅作研究辅助，不参与评分、门禁或导入。</small>
      </div>
      <button class="ghost refresh-action factor-map-refresh-action ${state.factorMapLoading ? "is-refreshing" : ""}" type="button" data-factor-map-refresh ${state.factorMapLoading ? "disabled" : ""}>
        <span>${state.factorMapLoading ? "加载中…" : "刷新因子地图"}</span>
        <small>读取最新审计与轨迹</small>
      </button>
    </header>
    <section class="factor-map-status-strip is-${escapeHtml(status)}">
      <span><small>状态</small><b>${escapeHtml(status)}</b></span>
      <span><small>地图版本</small><b>${escapeHtml(text(map.map_id, "--"))}</b></span>
      <span><small>审计版本</small><b>${escapeHtml(text(map.audit_id, "--"))}</b></span>
      <span><small>活跃池覆盖</small><b>${audit.active_pool_coverage_complete ? "完整" : "待确认"}</b></span>
    </section>
    <section class="factor-map-overview">
      <div class="factor-map-kpi-grid" aria-label="因子地图关键指标">
        <span><small>信息区域</small><b>${escapeHtml(text(summary.region_count, regions.length))}</b></span>
        <span><small>因子节点</small><b>${escapeHtml(text(summary.factor_node_count, 0))}</b></span>
        <span><small>近期研究轨迹</small><b>${escapeHtml(text(summary.recent_observation_count, observations.length))}</b></span>
        <span><small>轨迹附近入库</small><b>${escapeHtml(text(importedNearRegions, 0))}</b></span>
      </div>
    </section>
    <section class="factor-map-visual-grid" aria-label="信息区域关系与研究热度总览">
      <article class="factor-map-network-panel factor-map-global-panel">
        <header>
          <div><small>全库信息家族 · 本 run 覆盖叠层</small><h4>因子家族关系图</h4></div>
          <div class="factor-map-run-legend" aria-label="本 run 图例">
            <span><i class="is-covered"></i><b>黄环</b><small>本 run 覆盖 ${currentRunRegionCount}</small></span>
            <span><i class="is-imported"></i><b>绿环</b><small>已有入库 ${currentRunImportedRegionCount}</small></span>
            <span><i class="is-action"></i><b>粉环</b><small>建议换机制 ${currentRunActionRegionCount}</small></span>
            ${currentRunUnmapped ? `<em>${currentRunUnmapped} 个候选未映射</em>` : ""}
          </div>
        </header>
        <div class="factor-map-global-graph">${globalRelationGraph}</div>
      </article>
      <article class="factor-map-heat-panel">
        <header><div><small>与关系图同编号</small><h4>区域研究热度</h4></div><span>编号对应上方家族代表节点。进度条表示累计尝试，本 run 覆盖的家族会单独标出。</span></header>
        <div class="factor-map-heat-grid">${heatmap || `<div class="empty-state">尚无可映射的研究活动。</div>`}</div>
        <footer><span>按累计尝试次数排列</span><small>“深验未过”只统计已经进入深度检验的候选</small></footer>
      </article>
    </section>
    <section class="factor-map-section">
      <header><div><small>家族组成与研究结果</small><h4>因子方向明细</h4></div><span>每张卡说明该方向的既有覆盖、研究记录及建议。</span></header>
      <div class="factor-map-region-grid">${regionCards || `<div class="empty-state">${loading ? "正在读取因子地图…" : "当前没有可用的信息区域。"}</div>`}</div>
    </section>
    <section class="factor-map-section">
      <header><div><small>研究过程记录</small><h4>近期候选怎么走到下一步</h4></div><span>只读记录候选经过的检查，不改变生产判断。</span></header>
      <div class="factor-map-trajectory-list">
        ${observations.slice(0, 36).map((item) => `
          <article>
            <span class="factor-map-outcome outcome-${escapeHtml(text(item.outcome, "observed"))}">${escapeHtml(text(item.outcome, "observed"))}</span>
            <div><strong>${escapeHtml(text(item.candidate_id, "--"))} · ${escapeHtml(text(item.stage, "--"))}</strong><p>${escapeHtml(clip(item.reason || item.expression, 220))}</p></div>
            <small>${escapeHtml(compactRoundLabel(item.round_id) || text(item.round_id, "--"))}</small>
          </article>
        `).join("") || `<div class="empty-state">尚无可映射的近期研究轨迹；新研究会按稳定 trajectory_id 持续写入。</div>`}
      </div>
    </section>
  `;
}

function renderOverviewSummary() {
  const data = serviceOutputs(state.data);
  const factor = serviceOutputs(state.factorStatus);
  const model = serviceOutputs(state.modelStatus);
  const modelRegistrySummary = model.registry_summary || model.readiness?.model_registry_summary || {};
  const pipeline = serviceOutputs(state.pipelineStatus);
  const items = [
    {
      label: "数据底座",
      value: text(data.status, "unknown"),
      note: `最新交易日 ${text(data.snapshot?.latest_hdf5_trade_date)}`,
      tone: data.status === "completed" ? "good" : "idle",
    },
    {
      label: "因子平台",
      value: text(factor.status, "unknown"),
      note: `活跃因子 ${text(factor.registry_summary?.active, "0")}`,
      tone: factor.status === "running" ? "warn" : "idle",
    },
    {
      label: "模型平台",
      value: text(model.status, "unknown"),
      note: `research ${text(modelRegistrySummary.research, "0")}，candidate ${text(modelRegistrySummary.candidate, "0")}，production ${text(modelRegistrySummary.production, "0")}`,
      tone: model.status === "ready" || model.status === "completed" ? "good" : "idle",
    },
    {
      label: "流程状态",
      value: text(pipeline.status, "idle"),
      note: text(pipeline.latest_run?.status || pipeline.latest_run?.pipeline?.overall_status, ""),
      tone: pipeline.status === "running" ? "warn" : "idle",
    },
  ];
  const container = document.getElementById("overview-summary");
  container.innerHTML = items.map((item) => `
    <article class="bi-kpi-card is-${escapeHtml(item.tone)}">
      <div>
        <p class="metric-label">${escapeHtml(item.label)}</p>
        <p class="metric-value">${escapeHtml(item.value)}</p>
      </div>
      <p class="metric-note">${escapeHtml(item.note)}</p>
    </article>
  `).join("");
}

function renderMaintenanceOverview() {
  const node = document.getElementById("dashboard-maintenance");
  if (!node) return;
  const maintenance = serviceOutputs(state.maintenanceStatus);
  const audit = maintenance.disk_audit || {};
  const preview = maintenance.cleanup_preview || {};
  const cleanup = preview.summary || {};
  const byKind = cleanup.by_kind || {};
  const keyPaths = audit.key_paths || [];
  const keyPathMap = Object.fromEntries(keyPaths.map((item) => [item.name, item]));
  const dataStaging = byKind.data_foundation_staging || {};
  const dataBackups = byKind.data_foundation_production_backups || {};
  const dataMiscBackups = byKind.data_foundation_misc_backups || {};
  const pickleCache = byKind.pickle_cache || {};
  const resetBackups = byKind.reset_backups || {};
  const modelFeatureSets = byKind.model_feature_sets || {};
  const tradingPredictionFeatures = byKind.trading_prediction_features || {};
  const blocked = cleanup.blocked_candidates || [];
  const dataBlocked = blocked.filter((item) => String(item.kind || "").startsWith("data_foundation_"));
  const protectedReasons = blocked.reduce((acc, item) => {
    const reason = String(item.blocked_reason || item.protected_reason || "protected");
    acc[reason] = (acc[reason] || 0) + 1;
    return acc;
  }, {});
  const protectedSummary = Object.entries(protectedReasons)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4)
    .map(([reason, count]) => `${reason}: ${count}`)
    .join(" · ");
  const previewGeneratedAt = maintenance.cleanup_preview_generated_at || preview.generated_at;
  const previewStale = maintenance.cleanup_preview_stale;
  const protectedRows = blocked.slice(0, 8);
  const topPaths = keyPaths
    .slice()
    .sort((a, b) => Number(b.bytes || 0) - Number(a.bytes || 0))
    .slice(0, 6);
  const lastAction = serviceOutputs(state.latestMaintenanceAction);
  const actionOutputs = lastAction.outputs || {};
  node.innerHTML = `
    <div class="maintenance-console">
      <div class="detail-grid">
        <div><span class="detail-label">Real Project Size</span><strong>${escapeHtml(text(audit.project_total?.human_size, "--"))}</strong><small>${escapeHtml(text(audit.project_total?.note, "deduplicated project scan"))}</small></div>
        <div><span class="detail-label">Safe 可释放</span><strong>${escapeHtml(text(cleanup.reclaimable_human, "--"))}</strong></div>
        <div><span class="detail-label">Executable / Protected</span><strong>${escapeHtml(text(cleanup.executable_count, "0"))} / ${escapeHtml(text(cleanup.blocked_count, "0"))}</strong></div>
        <div><span class="detail-label">Last Preview</span><strong>${escapeHtml(text(previewGeneratedAt, "--"))}</strong><small>${previewStale ? "stale · run preview" : "current enough"}</small></div>
        <div><span class="detail-label">Last Cleanup</span><strong>${escapeHtml(text(actionOutputs.deleted_human, "not executed"))}</strong></div>
        <div><span class="detail-label">治理入口</span><strong>fxalpha-platform MCP</strong><small>GUI 使用 HTTP API；CLI 仅人工/故障 fallback</small></div>
      </div>
      <div class="maintenance-data-cleanup">
        <div>
          <span class="detail-label">pickle_cache 占用</span>
          <strong>${escapeHtml(text(keyPathMap.pickle_cache?.human_size, "--"))}</strong>
          <small>safe 可释放 ${escapeHtml(text(pickleCache.human_size, "0 B"))}</small>
        </div>
        <div>
          <span class="detail-label">data_foundation/staging</span>
          <strong>${escapeHtml(text(keyPathMap.data_foundation_staging?.human_size, "--"))}</strong>
          <small>可释放 ${escapeHtml(text(dataStaging.human_size, "0 B"))} · 当前生产和最近包受保护</small>
        </div>
        <div>
          <span class="detail-label">production_backups</span>
          <strong>${escapeHtml(text(keyPathMap.data_foundation_production_backups?.human_size, "--"))}</strong>
          <small>可释放 ${escapeHtml(text(dataBackups.human_size, "0 B"))} · 当前 promotion 受保护</small>
        </div>
        <div>
          <span class="detail-label">data_foundation/backups</span>
          <strong>${escapeHtml(text(dataMiscBackups.human_size, "0 B"))}</strong>
          <small>修复/诊断备份 · 24h 新项和运行状态 blocked</small>
        </div>
        <div>
          <span class="detail-label">reset_backups 可释放</span>
          <strong>${escapeHtml(text(resetBackups.human_size, "0 B"))}</strong>
          <small>保留最新 1 个 reset backup</small>
        </div>
        <div>
          <span class="detail-label">model_feature_sets 可释放</span>
          <strong>${escapeHtml(text(modelFeatureSets.human_size, "0 B"))}</strong>
          <small>active / registry / 最近 5 个 / 48h 新目录 protected</small>
        </div>
        <div>
          <span class="detail-label">prediction_features 可释放</span>
          <strong>${escapeHtml(text(tradingPredictionFeatures.human_size, "0 B"))}</strong>
          <small>旧交易预测特征快照 · 保留最新 1 个</small>
        </div>
        <div>
          <span class="detail-label">受保护/blocked</span>
          <strong>${escapeHtml(text(cleanup.blocked_count, "0"))}</strong>
          <small>${escapeHtml(text(protectedSummary, "No protected summary yet"))}</small>
        </div>
      </div>
      <div class="maintenance-path-list">
        ${topPaths.map((item) => `
          <div class="maintenance-path-row">
            <span>${escapeHtml(text(item.name, "path"))}</span>
            <strong>${escapeHtml(text(item.human_size, "--"))}</strong>
          </div>
        `).join("") || `<p class="muted">No disk audit data yet.</p>`}
      </div>
      <div class="maintenance-path-list">
        ${protectedRows.map((item) => `
          <div class="maintenance-path-row">
            <span>${escapeHtml(text(item.blocked_reason || item.protected_reason, "protected"))}<small>${escapeHtml(text(String(item.path || "").split("/").pop(), ""))}</small></span>
            <strong>${escapeHtml(text(item.human_size, "--"))}</strong>
          </div>
        `).join("") || `<p class="muted">No protected package in the latest preview.</p>`}
      </div>
      <div class="maintenance-actions">
        <button class="ghost small" data-maintenance-cleanup="dry-run">预览清理</button>
        <button class="ghost small danger-soft" data-maintenance-cleanup="execute">执行 safe 清理</button>
      </div>
      <p class="muted small-note">Safe 已直接增强，覆盖 pickle_cache、旧数据底座包/修复备份、旧 reset backup、旧 model feature sets、旧模型实验 workspace 和旧 prediction feature snapshots。生产治理首选 fxalpha-platform MCP；GUI 使用 HTTP API；CLI 仅人工/故障 fallback。</p>
    </div>
  `;
}

function renderOverviewDetails() {
  const data = serviceOutputs(state.data);
  const factorConsole = serviceOutputs(state.factorConsole);
  const factorStatus = serviceOutputs(state.factorStatus);
  const factorLibrary = serviceOutputs(state.factorLibraryRaw);
  const model = serviceOutputs(state.modelStatus);
  const modelRegistry = serviceOutputs(state.modelRegistry);
  const prediction = serviceOutputs(state.predictionStatus);
  const trading = serviceOutputs(state.tradingStatus);
  const pipeline = serviceOutputs(state.pipelineStatus);
  const digest = liveResearchDigest();
  const runtime = factorConsole.runtime_view || digest.runtime_view || {};
  const latestStep = latestLlmOutput();
  const registry = factorStatus.registry_summary || factorLibrary.registry_summary || factorConsole.registry_summary || {};
  if ((registry.active === undefined || registry.active === null) && factorLibrary.total !== undefined) {
    registry.active = factorLibrary.total;
  }
  if ((registry.total === undefined || registry.total === null) && factorLibrary.total !== undefined) {
    registry.total = factorLibrary.total;
  }
  const activeValues = factorStatus.active_values_store || factorStatus.readiness?.active_factor_values || factorConsole.active_values_store || factorConsole.readiness?.active_factor_values || {};
  const registryActiveKnown = registry.active !== undefined && registry.active !== null;
  const registryActiveCount = Number(registry.active || 0);
  const activeValuesSynced = registryActiveKnown && activeValues.exists && !activeValues.stale && Number(activeValues.column_count || 0) === registryActiveCount;
  const modelFeatureStale = Boolean(model.feature_set_stale || model.readiness?.feature_set_stale || model.active_feature_set?.feature_set_stale);
  const modelFeatureSynced = !modelFeatureStale && Number(model.active_feature_set?.factor_count || model.readiness?.active_feature_manifest?.factor_count || 0) === Number(registry.active || 0);
  const models = modelRegistry.items || modelRegistry.models || [];
  const modelRegistrySummary = model.registry_summary || model.readiness?.model_registry_summary || {};
  const researchCount = Number(modelRegistrySummary.research ?? models.filter((item) => item.status === "research").length);
  const candidateCount = Number(modelRegistrySummary.candidate ?? models.filter((item) => item.status === "candidate").length);
  const productionCount = Number(modelRegistrySummary.production ?? models.filter((item) => item.status === "production").length);
  const productionStatus = serviceOutputs(state.modelProduction);
  const production = productionStatus.production_model || models.find((item) => item.status === "production") || {};
  const productionValidation = productionStatus.production_validation || {};

  document.getElementById("dashboard-factor").innerHTML = `
    <div class="detail-grid">
      <div><span class="detail-label">当前阶段</span><strong>${escapeHtml(text(runtime.current_phase || digest.current_phase, "idle"))}</strong></div>
      <div><span class="detail-label">研究记录</span><strong>${escapeHtml(text((runtime.progress_counts || {}).research_steps || researchSteps().length, "0"))}</strong></div>
      <div><span class="detail-label">候选记录</span><strong>${escapeHtml(text(liveCandidates(50).length, "0"))}</strong></div>
      <div><span class="detail-label">最近更新</span><strong>${escapeHtml(ageLabel(runtime.updated_at || digest.updated_at))}</strong></div>
    </div>
    <div class="detail-copy">
      <span class="detail-label">最新研究判断</span>
      <p>${escapeHtml(clip(latestStep?.decision || latestStep?.summary || runtime.current_action || "当前没有最新研究记录。", 420))}</p>
    </div>
  `;
  document.getElementById("dashboard-factor-library").innerHTML = `
    <div class="detail-grid">
      <div><span class="detail-label">Active 因子</span><strong>${escapeHtml(text(registry.active, "0"))}</strong></div>
      <div><span class="detail-label">总因子</span><strong>${escapeHtml(text(registry.total, "0"))}</strong></div>
      <div><span class="detail-label">平均 ICIR</span><strong>${shortNumber(registry.avg_icir, 3)}</strong></div>
      <div><span class="detail-label">Pending / Retired</span><strong>${escapeHtml(text(registry.pending, "0"))} / ${escapeHtml(text(registry.retired, "0"))}</strong></div>
    </div>
    <div class="detail-grid">
      <div><span class="detail-label">Registry Active</span><strong>${escapeHtml(text(registry.active, "0"))}</strong><small>factor_registry.db</small></div>
      <div><span class="detail-label">Active Values</span><strong>${escapeHtml(activeValuesSynced ? "synced" : text(activeValues.refresh_status || (activeValues.stale ? "stale" : "missing"), "missing"))}</strong><small>${escapeHtml(`${text(activeValues.column_count, "0")} columns`)}</small></div>
      <div><span class="detail-label">Model Features</span><strong>${escapeHtml(modelFeatureSynced ? "synced" : "stale")}</strong><small>${escapeHtml(text(model.readiness?.stale_reason || model.active_feature_set?.stale_reason || ""))}</small></div>
    </div>
  `;
  const modelProcessStatus = model.process_status || model.gui_projection?.process_status || model.status;
  document.getElementById("dashboard-model").innerHTML = `
    <div class="detail-grid">
      <div><span class="detail-label">模型状态</span><strong>${escapeHtml(text(modelProcessStatus))}</strong><small>readiness ${escapeHtml(text(model.readiness_status || model.status))}</small></div>
      <div><span class="detail-label">模型总数</span><strong>${escapeHtml(text(model.readiness?.model_registry_summary?.total || models.length, "0"))}</strong></div>
      <div><span class="detail-label">Production</span><strong>${escapeHtml(text(production.model_id, "暂无"))}</strong></div>
      <div><span class="detail-label">Production 检验</span><strong>${escapeHtml(text(productionValidation.status, "unknown"))}</strong></div>
    </div>
  `;
  const tradingWarnings = trading.warnings || state.tradingStatus?.warnings || [];
  document.getElementById("dashboard-trading").innerHTML = `
    <div class="detail-grid">
      <div><span class="detail-label">推荐交易状态</span><strong>${escapeHtml(text(trading.status || prediction.status, "unknown"))}</strong></div>
      <div><span class="detail-label">生产模型</span><strong>${escapeHtml(text(trading.prediction?.outputs?.run_context?.model_id || prediction.readiness?.run_context?.model_id || production.model_id, "暂无"))}</strong></div>
      <div><span class="detail-label">最新推荐</span><strong>${escapeHtml(text(trading.latest_recommendation?.signal_date, "暂无"))}</strong></div>
      <div><span class="detail-label">Pending</span><strong>${escapeHtml(text(trading.pending_recommendations?.length, "0"))}</strong></div>
    </div>
    <div class="detail-copy"><span class="detail-label">风险提示</span><p>${escapeHtml(text(tradingWarnings.join("；") || state.latestTradingResult?.err || "推荐模拟交易链路正常。"))}</p></div>
  `;
  document.getElementById("overview-data").innerHTML = `
    <div class="detail-grid">
      <div><span class="detail-label">生产数据最新日期</span><strong>${escapeHtml(text(data.snapshot?.latest_hdf5_trade_date))}</strong></div>
      <div><span class="detail-label">QuantGPT 覆盖率</span><strong>${pct(data.snapshot?.quantgpt_latest_coverage_ratio, 2)}</strong></div>
      <div><span class="detail-label">Parquet 数量</span><strong>${escapeHtml(text(data.snapshot?.quantgpt_stock_parquet_count))}</strong></div>
      <div><span class="detail-label">滞后股票数</span><strong>${escapeHtml(text(data.snapshot?.quantgpt_stale_stock_count, "0"))}</strong></div>
    </div>
  `;
  document.getElementById("overview-pipeline").innerHTML = `
    <div class="detail-grid">
      <div><span class="detail-label">状态</span><strong>${escapeHtml(text(pipeline.status))}</strong></div>
      <div><span class="detail-label">原因</span><strong>${escapeHtml(text(pipeline.latest_run?.status || pipeline.latest_run?.err || pipeline.latest_run?.pipeline?.error))}</strong></div>
    </div>
  `;
}

function overviewPercentValue(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  return number <= 1 ? number * 100 : number;
}

function overviewToneFromText(value, fallback = "idle") {
  const raw = String(value || "").toLowerCase();
  if (!raw) return fallback;
  if (raw.includes("error") || raw.includes("fail") || raw.includes("block") || raw.includes("stale") || raw.includes("missing")) return "danger";
  if (raw.includes("warn") || raw.includes("running") || raw.includes("pending") || raw.includes("loading")) return "warn";
  if (raw.includes("ready") || raw.includes("ok") || raw.includes("complete") || raw.includes("synced") || raw.includes("healthy")) return "good";
  return fallback;
}

function backgroundWorkflowTone(status) {
  const value = String(status || "").toLowerCase();
  if (value === "failed") return "danger";
  if (value === "running") return "warn";
  if (value === "scheduled") return "good";
  if (value === "unavailable") return "danger";
  return "idle";
}

function backgroundWorkflowLabel(status) {
  const labels = {
    running: "运行中",
    scheduled: "已排程",
    failed: "执行失败",
    idle: "空闲",
    unavailable: "状态不可用",
  };
  return labels[String(status || "").toLowerCase()] || "读取中";
}

function compactSystemdTime(value) {
  const raw = text(value, "");
  if (!raw) return "--";
  return raw.replace(/^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+/, "");
}

function backgroundServiceLabel(workflow = {}) {
  const service = workflow.service || {};
  if (service.operational_state === "completed") return "本轮已完成 · 正常退出";
  if (service.operational_state === "running") return "本轮运行中";
  if (service.operational_state === "failed") return "本轮执行失败";
  if (service.available === false) return "执行服务不可用";
  if (["active", "activating"].includes(service.active_state)) return "本轮运行中";
  if (service.result === "failed" || Number(service.exit_status || 0) !== 0) return "本轮执行失败";
  if (service.active_state === "inactive" && service.sub_state === "dead" && service.result === "success") {
    return "本轮已完成 · 正常退出";
  }
  return `${text(service.active_state, "unknown")} · ${text(service.sub_state, "unknown")}`;
}

function backgroundTimerLabel(timer = {}) {
  if (timer.operational_state === "waiting") return "已启用 · 等待触发";
  if (timer.operational_state === "failed") return "定时器失败";
  if (timer.available === false) return "定时器不可用";
  if (timer.active_state === "active" && timer.sub_state === "waiting") return "已启用 · 等待触发";
  return `${text(timer.active_state, "unknown")} · ${text(timer.sub_state, "unknown")}`;
}

function backgroundResourceLabel(service = {}, kind = "memory") {
  const recorded = kind === "swap" ? service.swap_peak_recorded : service.memory_peak_recorded;
  const value = kind === "swap" ? service.swap_peak_human : service.memory_peak_human;
  return recorded ? text(value, "未提供") : "未提供本轮统计";
}

function backgroundScheduleTime(workflow = {}) {
  const match = text(workflow.schedule, "").match(/\b([01]\d|2[0-3]):([0-5]\d)\b/);
  return match ? `${match[1]}:${match[2]}` : "";
}

function backgroundResultLabel(service = {}) {
  if (service.operational_state === "running") return "正在执行";
  if (service.operational_state === "failed" || service.result === "failed" || Number(service.exit_status || 0) !== 0) {
    return `失败 · 退出码 ${text(service.exit_status, "--")}`;
  }
  if (service.operational_state === "completed" || service.result === "success") return "成功 · 退出码 0";
  return "尚无执行结果";
}

function renderBackgroundAutomationActionResult() {
  const target = document.getElementById("background-automation-action-result");
  if (!target) return;
  const result = state.automationActionResult;
  if (!result) {
    target.hidden = true;
    target.innerHTML = "";
    return;
  }
  const outputs = serviceOutputs(result);
  const labels = {
    resume: "自动调度已启用",
    pause: "自动调度已暂停",
    run_now: "任务已提交，正在读取运行状态",
    update_schedule: "自动调度时间已更新",
  };
  const errors = {
    automation_service_already_running: "该任务已经在运行，无需重复启动。",
    automation_schedule_time_invalid: "时间格式不正确，请使用 24 小时制。",
    automation_systemd_control_failed: "systemd 操作失败，请在诊断审计中查看服务状态。",
    automation_write_confirmation_required: "该操作需要再次确认。",
  };
  const action = text(outputs.action || result.inputs?.action, "");
  target.className = `background-automation-action-result ${result.ok ? "is-ok" : "is-danger"}`;
  target.innerHTML = `<strong>${escapeHtml(result.ok ? (labels[action] || "后台自动化设置已更新") : "操作未完成")}</strong><span>${escapeHtml(result.ok ? "状态已经重新读取并记录到操作审计。" : (errors[result.err || result.error] || text(result.err || result.error, "未知错误")))}</span>`;
  target.hidden = false;
}

function backgroundWorkflowView(key) {
  const runtime = serviceOutputs(state.automationStatus);
  const fullRuntime = serviceOutputs(state.platformRuntime);
  const automations = runtime.automations || fullRuntime.automations || {};
  const data = serviceOutputs(state.data);
  const fleet = serviceOutputs(state.paperFleetStatus);
  const activeAccount = (fleet.accounts || []).find((item) => item.status === "active") || (fleet.accounts || [])[0] || {};
  const workflowMeta = {
    data_foundation: {
      title: "数据底座日更",
      eyebrow: "Data Foundation",
      primaryLabel: "生产最新日",
      primaryValue: data.snapshot?.latest_hdf5_trade_date || data.current_production_dataset?.latest_trade_date,
      secondaryLabel: "当前阶段",
      secondaryValue: data.daily_update?.stage_summary?.current_stage || data.daily_update?.current_stage || data.status,
    },
    paper_trading: {
      title: "模拟交易日切",
      eyebrow: "Paper Trading",
      primaryLabel: "最近账本日",
      primaryValue: activeAccount.latest_snapshot?.trade_date || fleet.data?.qlib_latest,
      secondaryLabel: "待执行计划",
      secondaryValue: `${(activeAccount.pending_recommendations || []).length} 条`,
    },
  };
  const workflow = automations[key] || {};
  const service = workflow.service || {};
  const timer = workflow.timer || {};
  const swapPressure = Number(service.swap_peak_bytes || 0) >= 512 * 1024 * 1024;
  const baseTone = backgroundWorkflowTone(workflow.status);
  return {
    workflow,
    service,
    timer,
    meta: workflowMeta[key] || { title: key, eyebrow: "Background Workflow" },
    tone: swapPressure && baseTone === "good" ? "warn" : baseTone,
    swapPressure,
    serviceLabel: backgroundServiceLabel(workflow),
    timerLabel: backgroundTimerLabel(timer),
    nextTrigger: timer.next_trigger || (workflow.status === "running" ? "本轮结束后重新排程" : "--"),
  };
}

function renderBackgroundWorkflowStatus(targetId, workflowKeys) {
  const target = document.getElementById(targetId);
  if (!target) return;
  const compactOverview = targetId === "overview-background-workflow-status";
  const cards = workflowKeys.map((key) => {
    const { workflow, service, timer, meta, tone, swapPressure, serviceLabel, timerLabel, nextTrigger } = backgroundWorkflowView(key);
    if (compactOverview) {
      const primary = text(meta.primaryValue, "--");
      const secondary = text(meta.secondaryValue, "--");
      const statusCopy = tone === "danger"
        ? serviceLabel
        : workflow.status === "running"
          ? "本轮正在执行"
          : `${serviceLabel}；${timerLabel}`;
      return `
        <button class="overview-workflow-card is-${escapeHtml(tone)}" data-panel-target="${key === "data_foundation" ? "data-foundation" : "trading"}" type="button">
          <span class="status-dot"></span>
          <div class="overview-workflow-identity">
            <small>${escapeHtml(meta.eyebrow)}</small>
            <strong>${escapeHtml(meta.title)}</strong>
          </div>
          <div class="overview-workflow-primary"><span>${escapeHtml(meta.primaryLabel)}</span><strong>${escapeHtml(primary)}</strong></div>
          <div class="overview-workflow-primary"><span>${escapeHtml(meta.secondaryLabel)}</span><strong>${escapeHtml(secondary)}</strong></div>
          <div class="overview-workflow-schedule"><span>${escapeHtml(statusCopy)}</span><strong>下次 ${escapeHtml(compactSystemdTime(nextTrigger))}</strong></div>
          <b class="background-workflow-badge">${escapeHtml(`${backgroundWorkflowLabel(workflow.status)}${swapPressure ? " · 交换偏高" : ""}`)}</b>
        </button>
      `;
    }
    const rawServiceState = `${text(service.active_state, "unknown")}/${text(service.sub_state, "unknown")}${Number(service.main_pid || 0) ? ` · PID ${service.main_pid}` : ""}`;
    const timerWaiting = timer.operational_state === "waiting" || (timer.active_state === "active" && timer.sub_state === "waiting");
    const serviceRunning = service.operational_state === "running" || ["active", "activating"].includes(service.active_state);
    const scheduleTime = backgroundScheduleTime(workflow);
    return `
      <article class="background-workflow-card is-${escapeHtml(tone)}">
        <header>
          <div><span>${escapeHtml(meta.eyebrow)}</span><strong>${escapeHtml(meta.title)}</strong></div>
          <b class="background-workflow-badge">${escapeHtml(`${backgroundWorkflowLabel(workflow.status)}${swapPressure ? " · 交换偏高" : ""}`)}</b>
        </header>
        <div class="background-workflow-facts">
          <div><span>${escapeHtml(meta.primaryLabel)}</span><strong>${escapeHtml(text(meta.primaryValue, "--"))}</strong></div>
          <div><span>${escapeHtml(meta.secondaryLabel)}</span><strong>${escapeHtml(text(meta.secondaryValue, "--"))}</strong></div>
          <div><span>本轮状态</span><strong>${escapeHtml(serviceLabel)}</strong></div>
          <div><span>自动调度</span><strong>${escapeHtml(timerLabel)}</strong></div>
          <div><span>下次触发</span><strong>${escapeHtml(compactSystemdTime(nextTrigger))}</strong></div>
          <div><span>最近完成</span><strong>${escapeHtml(compactSystemdTime(service.execution_finished_at || timer.last_trigger))}</strong></div>
        </div>
        <div class="background-workflow-controls" data-automation-workflow="${escapeHtml(key)}">
          <div class="background-workflow-control-copy"><strong>调度控制</strong><span>${escapeHtml(text(workflow.schedule, "等待后台状态"))}</span></div>
          <div class="background-workflow-control-actions">
            <button class="tiny-button" type="button" data-background-workflow-action="resume" data-background-workflow="${escapeHtml(key)}" ${timerWaiting ? "disabled" : ""}>启用</button>
            <button class="tiny-button" type="button" data-background-workflow-action="pause" data-background-workflow="${escapeHtml(key)}" ${timerWaiting ? "" : "disabled"}>暂停</button>
            <button class="tiny-button is-primary" type="button" data-background-workflow-action="run_now" data-background-workflow="${escapeHtml(key)}" ${serviceRunning ? "disabled" : ""}>立即执行</button>
          </div>
          <div class="background-workflow-schedule-edit">
            <label><span>执行时间</span><input type="time" value="${escapeHtml(scheduleTime)}" data-background-workflow-time="${escapeHtml(key)}" /></label>
            <button class="tiny-button" type="button" data-background-workflow-action="update_schedule" data-background-workflow="${escapeHtml(key)}">保存时间</button>
          </div>
          <small>暂停只停止未来触发，不会中止正在运行的任务。</small>
        </div>
        <details class="background-workflow-technical">
          <summary>最近一次技术明细</summary>
          <div>
            <span><small>底层运行态</small><strong>${escapeHtml(rawServiceState)} · 单次任务退出后显示 inactive/dead</strong></span>
            <span><small>执行结果</small><strong>${escapeHtml(backgroundResultLabel(service))}</strong></span>
            <span><small>CPU 时间</small><strong>${escapeHtml(`${text(service.cpu_seconds, "--")} 秒`)}</strong></span>
            <span><small>内存峰值</small><strong>${escapeHtml(backgroundResourceLabel(service, "memory"))}</strong></span>
            <span><small>交换峰值</small><strong>${escapeHtml(backgroundResourceLabel(service, "swap"))}</strong></span>
          </div>
        </details>
      </article>
    `;
  });
  target.innerHTML = cards.join("") || `<div class="empty-state compact">后台定时状态正在读取。</div>`;
}

function renderTradingBackgroundWorkflowSummary() {
  const target = document.getElementById("trading-background-workflow-summary");
  if (!target) return;
  const { workflow, service, nextTrigger: paperNextTrigger } = backgroundWorkflowView("paper_trading");
  const { nextTrigger: dataNextTrigger } = backgroundWorkflowView("data_foundation");
  const fleet = serviceOutputs(state.paperFleetStatus);
  const account = (fleet.accounts || []).find((item) => item.status === "active") || (fleet.accounts || [])[0] || {};
  const latestSnapshot = account.latest_snapshot || {};
  const latestRecommendation = account.latest_recommendation || {};
  const pendingRecommendations = account.pending_recommendations || [];
  const ledgerDate = text(latestSnapshot.trade_date, "--");
  const productionDate = text(fleet.data?.qlib_latest, "");
  const ledgerIsCurrent = Boolean(productionDate && ledgerDate !== "--" && ledgerDate >= productionDate);
  const pendingSignalDate = text(latestRecommendation.signal_date || pendingRecommendations[0]?.signal_date, "--");
  const pendingCount = pendingRecommendations.length;
  const blockedAccounts = Array.isArray(fleet.blocked_accounts) ? fleet.blocked_accounts : [];
  const integrityIssues = Array.isArray(account.integrity_issues) ? account.integrity_issues : [];
  const fleetStatus = String(fleet.status || "").toLowerCase();
  const workflowStatus = String(workflow.status || "").toLowerCase();
  const serviceFailed = service.operational_state === "failed"
    || service.result === "failed"
    || Number(service.exit_status || 0) !== 0;
  const hardBlocked = serviceFailed
    || ["failed", "unavailable"].includes(workflowStatus)
    || ["blocked", "failed", "unavailable", "stale"].some((value) => fleetStatus.includes(value))
    || blockedAccounts.length > 0
    || integrityIssues.length > 0;
  const blockDetail = text(
    integrityIssues[0]?.message
      || integrityIssues[0]?.reason
      || blockedAccounts[0]?.blocked_reason
      || blockedAccounts[0]?.reason
      || (serviceFailed ? "后台任务执行失败，请查看控制台详情" : "模拟交易链路存在阻断"),
    "模拟交易链路存在阻断",
  );
  const normalDetail = ledgerIsCurrent
    ? `账户账本 ${ledgerDate} · 已追平生产数据`
    : `账户账本 ${ledgerDate} · 等待自动追平 ${productionDate || "生产数据"}`;
  const pendingDetail = pendingCount
    ? (latestRecommendation.execution_date
      ? `计划 ${latestRecommendation.execution_date} 执行`
      : "等待下一交易日行情入库")
    : "当前无需执行调仓";
  const summaryTone = hardBlocked ? "danger" : "good";
  target.innerHTML = `
    <div class="paper-automation-summary-head">
      <div class="paper-automation-summary-title">
        <span class="status-dot is-${escapeHtml(summaryTone)}"></span>
        <div><small>Paper Trading Automation</small><strong>模拟交易自动化</strong></div>
      </div>
      <button class="ghost paper-automation-detail-action" type="button" data-paper-trading-tab="console" data-paper-console-target="automation">查看后台详情</button>
    </div>
    <div class="paper-automation-summary-facts">
      <span class="is-${escapeHtml(summaryTone)}"><small>当前状态</small><strong>${escapeHtml(hardBlocked ? "已阻断" : "运行正常")}</strong><em>${escapeHtml(hardBlocked ? blockDetail : normalDetail)}</em></span>
      <span class="${hardBlocked ? "is-danger" : (pendingCount ? "is-warn" : "is-good")}"><small>待执行计划</small><strong>${escapeHtml(`${pendingCount} 条${pendingCount ? ` · ${pendingSignalDate} 信号已生成` : ""}`)}</strong><em>${escapeHtml(pendingDetail)}</em></span>
      <span><small>下一自动调度</small><strong>${escapeHtml(compactSystemdTime(dataNextTrigger))}</strong><em>模拟交易补检 ${escapeHtml(compactSystemdTime(paperNextTrigger))}</em></span>
    </div>
  `;
}

function overviewServiceTone(service) {
  if (!service) return "idle";
  if (service.ok === true) return "good";
  if (service.ok === false) return "danger";
  return overviewToneFromText(service.status || service.state || service.health, "idle");
}

function overviewPhaseLabel(value, fallback = "空闲") {
  const raw = text(value, fallback);
  const key = String(raw).trim().toLowerCase().replace(/[\s-]+/g, "_");
  const stage = researchStageMeta(key);
  if (stage.known) return stage.zh;
  const labels = {
    unknown: "未知",
    idle: "空闲",
    ready: "就绪",
    completed: "完成",
    running: "运行中",
    blocked: "阻塞",
    llm_output: "研究输出",
  };
  return labels[key] || raw;
}

function overviewCleanMessage(value, fallback = "暂无说明") {
  const raw = text(value, fallback);
  const normalized = String(raw).toLowerCase();
  if (normalized.includes("prediction status is blocked")) return "预测状态阻塞，推荐结果可能过期";
  if (normalized.includes("production model") || normalized.includes("production_model")) return "暂无生产模型";
  if (normalized.includes("feature set stale")) return "特征快照过期";
  if (normalized.includes("active values")) return "活跃因子值需要检查";
  if (normalized.includes("research console ready")) return "研究控制台待更新";
  return raw;
}

function overviewBar(label, value, note, tone = "idle") {
  const percentValue = overviewPercentValue(value);
  const width = percentValue === null ? 0 : Math.max(0, Math.min(100, percentValue));
  const displayValue = percentValue === null ? "--" : `${shortNumber(width, 1)}%`;
  return `
    <div class="system-meter is-${escapeHtml(tone)}">
      <div class="meter-head">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(displayValue)}</strong>
      </div>
      <div class="meter-bar"><span style="width:${escapeHtml(String(width))}%"></span></div>
      <p>${escapeHtml(note || "指标暂不可用")}</p>
    </div>
  `;
}

function overviewResourceTone(value) {
  const percent = overviewPercentValue(value);
  if (percent === null) return "idle";
  if (percent > 80) return "red";
  if (percent >= 60) return "amber";
  return "green";
}

function compactInteger(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return Math.round(number).toLocaleString("en-US");
}

function compactTokenUnit(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  if (Math.abs(number) >= 100000000) return `${shortNumber(number / 100000000, 2)}亿`;
  if (Math.abs(number) >= 1000000) return `${shortNumber(number / 1000000, 2)}百万`;
  return compactInteger(number);
}

function compactUsd(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  if (number > 0 && number < 0.01) return "<$0.01";
  return `$${shortNumber(number, number >= 1 ? 2 : 4)}`;
}

function compactCny(value, { signed = false } = {}) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  const prefix = signed && number > 0 ? "+" : "";
  return `${prefix}¥${shortNumber(number, Math.abs(number) >= 1 ? 2 : 4)}`;
}

function tokenSummaryHtml(usage = {}, providerLabel = "Token") {
  const sourceLabel = usage.source_label || (usage.source === "orchestrator_llm_trace" ? "来自本地 trace" : "本机观测");
  return `
    <div class="token-summary-card">
      <div class="token-summary-head">
        <span>${escapeHtml(providerLabel)} token 消耗</span>
        <strong>${escapeHtml(sourceLabel)}</strong>
      </div>
      <div class="usage-stat-grid">
        <div><span>prompt_tokens</span><strong>${escapeHtml(compactInteger(usage.prompt_tokens))}</strong></div>
        <div><span>completion_tokens</span><strong>${escapeHtml(compactInteger(usage.completion_tokens))}</strong></div>
        <div><span>total_tokens</span><strong>${escapeHtml(compactInteger(usage.total_tokens))}</strong></div>
        <div><span>request_count</span><strong>${escapeHtml(compactInteger(usage.request_count ?? usage.requests))}</strong></div>
      </div>
      <p>${escapeHtml(text(usage.note, `${sourceLabel}，非官方余额数据`))}</p>
    </div>
  `;
}

function latestTimestamp(values) {
  let latest = null;
  values.forEach((value) => {
    const date = parseIso(value);
    if (date && (!latest || date.getTime() > latest.getTime())) latest = date;
  });
  return latest ? latest.toISOString() : "";
}

function observedDeepSeekUsage() {
  const traceOutputs = orchestratorOutputs(state.orchestratorTraces);
  const visibleTraces = orchestratorTraces();
  const modelTraceOutputs = orchestratorOutputs(state.modelOrchestratorTraces);
  const visibleModelTraces = modelOrchestratorTraces();
  const traces = [
    ...(visibleTraces.length ? visibleTraces : (traceOutputs.traces || [])),
    ...(visibleModelTraces.length ? visibleModelTraces : (modelTraceOutputs.traces || [])),
  ];
  const llmTraces = traces.filter((trace) => String(trace.trace_type || "").includes("llm") || String(trace.event_type || "").includes("llm"));
  const requests = llmTraces.filter((trace) => trace.event_type === "llm_request");
  const results = llmTraces.filter((trace) => trace.event_type === "llm_result");
  const errors = llmTraces.filter((trace) => trace.event_type === "llm_error" || trace.error || trace.error_type);
  const deepseekRequests = requests.filter((trace) => String(trace.llm_provider || "").toLowerCase().includes("deepseek") || String(trace.llm_model || "").toLowerCase().includes("deepseek"));
  const requestRows = deepseekRequests.length ? deepseekRequests : requests;
  const payloadChars = requestRows.reduce((sum, trace) => sum + Number(trace.payload_chars || 0), 0);
  const now = Date.now();
  const windows = {
    last_24h: { request_count: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
    last_7d: { request_count: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  };
  llmTraces.forEach((trace) => {
    const ts = parseIso(trace.ts || trace.created_at);
    if (!ts) return;
    const ageMs = now - ts.getTime();
    const usage = trace.usage || trace.result?.usage || {};
    const hasUsage = Boolean(usage.prompt_tokens || usage.input_tokens || usage.completion_tokens || usage.output_tokens || usage.total_tokens);
    let promptTokens = Number(usage.prompt_tokens || usage.input_tokens || 0);
    let completionTokens = Number(usage.completion_tokens || usage.output_tokens || 0);
    if (!hasUsage && trace.event_type === "llm_request") {
      promptTokens = Math.ceil(Number(trace.payload_chars || 0) / 4);
    }
    if (!hasUsage && trace.event_type === "llm_result") {
      let resultChars = 0;
      try {
        resultChars = JSON.stringify(trace.result || {}).length;
      } catch {
        resultChars = 0;
      }
      completionTokens = Math.ceil(resultChars / 4);
    }
    const totalTokens = Number(usage.total_tokens || 0) || promptTokens + completionTokens;
    [
      ["last_24h", 24 * 60 * 60 * 1000],
      ["last_7d", 7 * 24 * 60 * 60 * 1000],
    ].forEach(([key, horizonMs]) => {
      if (ageMs < 0 || ageMs > horizonMs) return;
      if (trace.event_type === "llm_request") windows[key].request_count += 1;
      windows[key].prompt_tokens += Number.isFinite(promptTokens) ? promptTokens : 0;
      windows[key].completion_tokens += Number.isFinite(completionTokens) ? completionTokens : 0;
      windows[key].total_tokens += Number.isFinite(totalTokens) ? totalTokens : 0;
    });
  });
  const elapsedRows = [...results, ...errors].map((trace) => Number(trace.elapsed_s)).filter((value) => Number.isFinite(value));
  const avgElapsed = elapsedRows.length ? elapsedRows.reduce((sum, value) => sum + value, 0) / elapsedRows.length : null;
  const models = [...new Set(llmTraces.map((trace) => trace.llm_model).filter(Boolean))];
  if (!llmTraces.length) {
    return {
      configured: false,
      source_kind: "orchestrator_trace",
      status: "no_trace",
      title: "暂无可观测调用",
      note: "当前 run 尚未暴露 Orchestrator LLM trace；不会编造消耗。",
    };
  }
  return {
    configured: true,
    source_kind: "orchestrator_trace",
    status: errors.length ? "warning" : "observed",
    requests: requestRows.length,
    results: results.length,
    errors: errors.length,
    payload_chars: payloadChars,
    estimated_prompt_tokens: Math.ceil(payloadChars / 4),
    avg_elapsed_s: avgElapsed,
    model: models[0] || "deepseek",
    model_count: models.length,
    trace_rows: llmTraces.length,
    updated_at: latestTimestamp(llmTraces.map((trace) => trace.ts || trace.created_at)),
    window: currentRunId() ? "当前研究 run" : "最近 trace 窗口",
    ...windows,
  };
}

function overviewUsageModel(rawUsage = {}) {
  const codexRaw = rawUsage.codex || {};
  const deepseekRaw = rawUsage.deepseek || {};
  const deepseekObserved = observedDeepSeekUsage();
  const codexObserved = serviceOutputs(state.codexUsageSnapshot);
  const deepseekSnapshot = serviceOutputs(state.deepseekUsageSnapshot);
  const codexOfficial = codexRaw.official || (codexRaw.source_kind === "codex_desktop_sqlite_snapshot" ? {} : codexRaw);
  const codexLocal = codexRaw.local_observed
    ? {
        ...codexObserved,
        ...codexRaw.local_observed,
        last_24h: codexRaw.local_observed.last_24h || codexObserved.last_24h,
        last_7d: codexRaw.local_observed.last_7d || codexObserved.last_7d,
      }
    : codexObserved;
  const deepseekOfficial =
    deepseekRaw.official_balance?.status === "ok"
      ? deepseekRaw.official_balance
      : deepseekSnapshot.official_balance?.status === "ok"
        ? deepseekSnapshot.official_balance
        : deepseekSnapshot.official_balance || deepseekRaw.official_balance || {};
  const deepseekSnapshotTrace = deepseekSnapshot.observed_trace || deepseekSnapshot;
  const deepseekTrace = deepseekRaw.observed_trace?.configured === true
    ? {
        ...deepseekObserved,
        ...deepseekSnapshotTrace,
        ...deepseekRaw.observed_trace,
        last_24h: deepseekRaw.observed_trace.last_24h || deepseekSnapshotTrace.last_24h || deepseekObserved.last_24h,
        last_7d: deepseekRaw.observed_trace.last_7d || deepseekSnapshotTrace.last_7d || deepseekObserved.last_7d,
      }
    : {
        ...deepseekObserved,
        ...deepseekSnapshotTrace,
        last_24h: deepseekSnapshotTrace.last_24h || deepseekObserved.last_24h,
        last_7d: deepseekSnapshotTrace.last_7d || deepseekObserved.last_7d,
      };
  return {
    codex: {
      configured: codexOfficial.configured === true || codexLocal.configured === true,
      source_kind: "codex_bundle",
      official: codexOfficial.configured === true ? codexOfficial : {
        configured: false,
        source: codexOfficial.source || "not_configured",
        status: codexOfficial.status || "not_connected",
        title: "官方额度未接入",
        note: "Codex 官方额度请接入 OpenAI usage dashboard、企业 Analytics API 或 Codex Desktop rate-limit 事件；GUI 不抓私有 dashboard。",
      },
      local_observed: codexLocal.configured === true ? {
        ...codexLocal,
        configured: true,
        source_kind: "codex_desktop_sqlite_snapshot",
      } : {
        configured: false,
        source_kind: "codex_desktop_sqlite_snapshot",
        status: "missing_snapshot",
        note: "本机 Codex token snapshot 缺失。",
      },
    },
    deepseek: {
      configured: deepseekOfficial.configured === true || deepseekTrace.configured === true,
      source_kind: "deepseek_bundle",
      official_balance: deepseekOfficial.configured === true ? deepseekOfficial : {
        configured: false,
        status: deepseekOfficial.status || "missing_api_key",
        source: "https://api.deepseek.com/user/balance",
        note: "未配置 DeepSeek API key 或当前 API 未重载到余额查询实现。",
      },
      observed_trace: deepseekTrace,
    },
  };
}

function deepSeekOfficialBalanceDisplay(balance = {}) {
  const infos = Array.isArray(balance.balance_infos) ? balance.balance_infos : [];
  const firstBalance = infos[0] || {};
  const currency = text(firstBalance.currency, "").toUpperCase();
  const statusLabel = balance.status === "ok"
    ? balance.is_available === false ? "余额不足" : "已接入"
    : balance.configured === false || balance.status === "missing_api_key"
      ? "未配置"
      : "请求失败";
  const value = balance.status === "ok"
    ? currency === "CNY"
      ? compactCny(firstBalance.total_balance)
      : `${text(firstBalance.total_balance, "--")} ${currency}`.trim()
    : statusLabel;
  return {
    currency,
    statusLabel,
    value,
    tone: balance.status === "ok" && balance.is_available !== false ? "is-ok" : "is-warn",
  };
}

function researchDeepSeekBalanceChipHtml() {
  if (!isCurrentOrchestratorMode()) return "";
  if (!state.platformRuntime) {
    return '<span class="deepseek-balance-chip is-loading"><b>DeepSeek 余额</b>读取中</span>';
  }
  if (state.platformRuntime._failed || state.platformRuntime.error) {
    return '<span class="deepseek-balance-chip is-warn" title="平台运行状态接口读取失败"><b>DeepSeek 余额</b>读取失败</span>';
  }
  const rawUsage = serviceOutputs(state.platformRuntime).usage || {};
  const balance = overviewUsageModel(rawUsage).deepseek.official_balance || {};
  const display = deepSeekOfficialBalanceDisplay(balance);
  const updatedAt = text(balance.updated_at || state.platformRuntime.generated_at, "");
  const title = [
    "DeepSeek 官方账户余额",
    updatedAt ? `更新 ${updatedAt}` : "",
    balance.source ? `来源 ${balance.source}` : "",
  ].filter(Boolean).join(" · ");
  return `
    <span class="deepseek-balance-chip ${escapeHtml(display.tone)}" title="${escapeHtml(title)}">
      <b>DeepSeek 余额</b>${escapeHtml(display.value)}
    </span>
  `;
}

function overviewRuntimeWarningText(warning) {
  const value = text(warning, "").trim();
  if (!value) return "";
  if (value === "runtime_usage_status_missing") return "";
  if (value === "deepseek_api_key_missing" || value === "deepseek_balance_key_missing") return "DeepSeek 官方余额未配置 API key";
  if (value === "codex_logs_missing") return "Codex Desktop rate-limit 日志未找到";
  if (value === "codex_rate_limit_event_missing") return "Codex rate-limit 事件暂未写入";
  return value.replace(/_/g, " ");
}

function overviewUsageHtml(label, usage, kind) {
  if (usage?.source_kind === "codex_bundle") {
    const official = usage.official || {};
    const local = usage.local_observed || {};
    const last24h = local.last_24h || {};
    const last7d = local.last_7d || {};
    const officialRate = ["codex_session_token_count", "codex_desktop_rate_limits_log"].includes(official.source) ? official : (local.official_rate_limits || {});
    const limits = officialRate.rate_limits || {};
    const rateWindows = overviewCodexRateWindows(limits);
    const last24hNote = last24h.source === "codex_session_rollout_events"
      ? `${compactInteger(last24h.sessions || last24h.threads)} sessions`
      : `${compactInteger(last24h.threads)} threads`;
    const last7dNote = last7d.source === "codex_session_rollout_events"
      ? `${compactInteger(last7d.sessions || last7d.threads)} sessions`
      : last7d ? `${compactInteger(last7d.threads)} threads` : "暂无 7D 汇总";
    return `
      <article class="usage-compact-card is-codex">
        <div class="usage-meter-head">
          <span class="detail-label">CODEX</span>
          <strong>${escapeHtml(officialRate.configured ? "Codex 客户端窗口" : "额度窗口未接入")}</strong>
        </div>
        <div class="usage-rate-stack">
          ${overviewRateWindowHtml("5H 额度", rateWindows.fiveHour)}
          ${overviewRateWindowHtml("Weekly 额度", rateWindows.weekly)}
        </div>
        <div class="usage-compact-grid usage-token-grid">
          ${usageCompactMetric("24H cached token", compactTokenUnit(last24h.cached_input_tokens), last24hNote)}
          ${usageCompactMetric("7D cached token", compactTokenUnit(last7d.cached_input_tokens), last7dNote)}
        </div>
      </article>
    `;
  }
  if (usage?.source_kind === "deepseek_bundle") {
    const balance = usage.official_balance || {};
    const trace = usage.observed_trace || {};
    const balanceDisplay = deepSeekOfficialBalanceDisplay(balance);
    const balanceCurrency = balanceDisplay.currency;
    const balanceValue = balanceDisplay.value;
    const trace24h = trace.last_24h || {};
    const trace7d = trace.last_7d || {};
    const balanceChange = balance.balance_changes?.[balanceCurrency] || {};
    const hasExactUsage = Number(trace.exact_usage_records || trace.request_count || 0) > 0;
    const balanceChangeValue = balanceCurrency === "CNY" && Number.isFinite(Number(balanceChange.delta))
      ? compactCny(balanceChange.delta, { signed: true })
      : "--";
    const tone = balance.status === "ok" && balance.is_available !== false ? "is-good" : trace.errors ? "is-warn" : "is-idle";
    return `
      <article class="usage-compact-card ${tone}">
        <div class="usage-meter-head">
          <span class="detail-label">DEEPSEEK</span>
          <strong>${escapeHtml(balance.status === "ok" && balance.is_available === false ? "已接入 · 余额不足" : balanceDisplay.statusLabel)}</strong>
        </div>
        <div class="usage-compact-grid">
          ${usageCompactMetric("余额", balanceValue)}
          ${usageCompactMetric("24H token", hasExactUsage ? compactTokenUnit(trace24h.total_tokens) : "--")}
          ${usageCompactMetric("7D token", hasExactUsage ? compactTokenUnit(trace7d.total_tokens) : "--")}
          ${usageCompactMetric("本次余额变化", balanceChangeValue)}
        </div>
      </article>
    `;
  }
  const configured = usage?.configured === true;
  if (!configured) {
    return `
      <article class="usage-meter is-idle">
        <div class="usage-meter-head">
          <span class="detail-label">${escapeHtml(label)}</span>
          <strong>${escapeHtml(text(usage?.title, "状态源未配置"))}</strong>
        </div>
        <p>${escapeHtml(text(usage?.note, "可接入 runtime_usage_status.json；当前不估算额度，也不读取密钥。"))}</p>
      </article>
    `;
  }
  if (usage.source_kind === "orchestrator_trace") {
    const tone = usage.errors ? "is-warn" : "is-good";
    return `
      <article class="usage-meter ${tone}">
        <div class="usage-meter-head">
          <span class="detail-label">${escapeHtml(label)}</span>
          <strong>${escapeHtml(`${compactInteger(usage.requests)} 次请求`)}</strong>
        </div>
        <div class="usage-stat-grid">
          <div><span>结果</span><strong>${escapeHtml(compactInteger(usage.results))}</strong></div>
          <div><span>错误</span><strong>${escapeHtml(compactInteger(usage.errors))}</strong></div>
          <div><span>估算 token</span><strong>${escapeHtml(compactInteger(usage.estimated_prompt_tokens))}</strong></div>
          <div><span>平均耗时</span><strong>${usage.avg_elapsed_s === null ? "--" : `${shortNumber(usage.avg_elapsed_s, 1)}s`}</strong></div>
        </div>
        <p>${escapeHtml(text(usage.model, "DeepSeek"))} · ${escapeHtml(text(usage.window, "最近 trace 窗口"))} · ${escapeHtml(ageLabel(usage.updated_at))} · trace 观测值，非账单</p>
      </article>
    `;
  }
  if (usage.source_kind === "codex_desktop_sqlite_snapshot") {
    const current = usage.current_thread || {};
    const project = usage.project || {};
    const last24h = usage.last_24h || {};
    return `
      <article class="usage-meter is-good">
        <div class="usage-meter-head">
          <span class="detail-label">${escapeHtml(label)}</span>
          <strong>${escapeHtml(compactInteger(current.tokens_used))} tokens</strong>
        </div>
        <div class="usage-stat-grid">
          <div><span>当前线程</span><strong>${escapeHtml(compactInteger(current.tokens_used))}</strong></div>
          <div><span>近 24h</span><strong>${escapeHtml(compactInteger(last24h.tokens_used))}</strong></div>
          <div><span>项目累计</span><strong>${escapeHtml(compactInteger(project.tokens_used))}</strong></div>
          <div><span>线程数</span><strong>${escapeHtml(compactInteger(project.threads))}</strong></div>
        </div>
        <p>${escapeHtml(text(current.model, "Codex"))} · ${escapeHtml(text(current.title, "当前线程"))} · ${escapeHtml(ageLabel(current.updated_at || usage.generated_at))} · 本机观测 token，非官方剩余额度</p>
      </article>
    `;
  }
  const usedTokens = Number(usage.used_tokens || usage.prompt_tokens || 0) + Number(usage.completion_tokens || 0);
  const limitTokens = Number(usage.limit_tokens || 0);
  const percentValue = limitTokens > 0 ? (usedTokens / limitTokens) * 100 : null;
  const width = percentValue === null ? 0 : Math.max(0, Math.min(100, percentValue));
  const primary = kind === "deepseek"
    ? `${text(usage.requests, "0")} req · ${text(usage.cost, "--")}`
    : `${text(usage.remaining, "--")} remaining`;
  const secondary = kind === "deepseek"
    ? `${text(usage.prompt_tokens, "0")} prompt / ${text(usage.completion_tokens, "0")} completion`
    : `${text(usedTokens || usage.used_tokens, "0")} / ${text(usage.limit_tokens, "--")} tokens`;
  return `
    <article class="usage-meter is-good">
      <div class="usage-meter-head">
        <span class="detail-label">${escapeHtml(label)}</span>
        <strong>${escapeHtml(primary)}</strong>
      </div>
      <div class="meter-bar"><span style="width:${escapeHtml(String(width))}%"></span></div>
      <p>${escapeHtml(secondary)} · ${escapeHtml(text(usage.window, "current window"))}</p>
    </article>
  `;
}

function rateLimitRemaining(block = {}) {
  const value = Number(block.remaining_percent);
  return Number.isFinite(value) ? `${shortNumber(value, 1)}% left` : "--";
}

function usageCompactMetric(label, value, note = "") {
  return `
    <div class="usage-compact-metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(text(value, "--"))}</strong>
      ${note ? `<small>${escapeHtml(note)}</small>` : ""}
    </div>
  `;
}

function overviewRateWindowHtml(label, block) {
  const used = overviewPercentValue(block?.used_percent);
  const remaining = block?.remaining_percent ?? (used === null ? null : Math.max(0, 100 - used));
  // This meter is explicitly labelled as remaining allowance.  Rendering the
  // consumed percentage made a fully available 5H window look empty.
  const width = remaining === null ? 0 : Math.max(0, Math.min(100, Number(remaining)));
  const remainingValue = Number(remaining);
  const level = !Number.isFinite(remainingValue)
    ? "unknown"
    : remainingValue >= 80
      ? "green"
      : remainingValue >= 50
        ? "blue"
        : remainingValue >= 25
          ? "yellow"
          : "red";
  const reset = block?.reset_after_seconds
    ? `${shortNumber(Number(block.reset_after_seconds) / 3600, 1)}h reset`
    : ageLabel(block?.reset_at_iso);
  return `
    <div class="usage-window-meter">
      <div class="meter-head"><span>${escapeHtml(label)}</span><strong>${remaining === null ? "--" : `${shortNumber(remaining, 1)}% left`}</strong></div>
      <div class="meter-bar usage-level-${escapeHtml(level)}"><span style="width:${escapeHtml(String(width))}%"></span></div>
    </div>
  `;
}

function overviewCodexRateWindows(limits = {}) {
  const windows = [limits.primary, limits.secondary].filter((item) => item && typeof item === "object");
  const minutes = (block) => Number(block?.window_minutes);
  const fiveHour = windows.find((block) => Number.isFinite(minutes(block)) && minutes(block) > 0 && minutes(block) <= 360);
  const weekly = windows.find((block) => Number.isFinite(minutes(block)) && minutes(block) >= 10080);
  return {
    // The client currently emits only the weekly window while the temporary 5H
    // allowance is fully available. Map by the reported duration, not position.
    fiveHour: fiveHour || (weekly ? { used_percent: 0, remaining_percent: 100, window_minutes: 300 } : {}),
    weekly: weekly || {},
  };
}

function overviewResearchRunRoundStage(runtime = {}, digest = {}, latestStep = {}) {
  const runId = text(runtime.run_id || digest.run_id || latestStep.run_id, "");
  const roundId = text(digest.round_id || runtime.round_id || latestStep.round_id, "");
  const stageId = text(latestStep.stage_id || runtime.stage_id || digest.stage_id || "", "");
  const runDate = runId.match(/20(\d{2})(\d{2})(\d{2})/);
  const roundMatch = roundId.match(/r(\d{3,5})/i);
  const stageMatch = stageId.match(/(?:^|[:_-])s(?:tage)?[_-]?(\d{1,3})(?:$|[:_-])/i)
    || text(digest.stage || runtime.stage || latestStep.stage, "").match(/(?:^|[:_-])s(?:tage)?[_-]?(\d{1,3})(?:$|[:_-])/i);
  return {
    run: runDate ? `RUN ${runDate[2]}${runDate[3]}` : "RUN --",
    round: roundMatch ? `ROUND ${roundMatch[1].padStart(4, "0")}` : "ROUND --",
    stage: stageMatch ? `S ${stageMatch[1].padStart(2, "0")}` : "S --",
  };
}

function overviewUsageStripHtml(codexUsage = {}, deepseekUsage = {}) {
  const codexOfficial = codexUsage.official || {};
  // Rate windows come only from the newest session token_count event.  The
  // legacy sqlite snapshot remains useful for token totals, but is stale for
  // live percentage windows and must not be used as a fallback here.
  const rateSource = codexOfficial.source === "codex_session_token_count" && codexOfficial.configured === true
    ? codexOfficial
    : {};
  const rateWindows = overviewCodexRateWindows(rateSource.rate_limits || {});
  const codexLocal = codexUsage.local_observed || {};
  const codex24h = codexLocal.last_24h || {};
  const codex7d = codexLocal.last_7d || {};
  const deepseekBalance = deepseekUsage.official_balance || {};
  const deepseekTrace = deepseekUsage.observed_trace || {};
  const deepseek24h = deepseekTrace.last_24h || {};
  const deepseek7d = deepseekTrace.last_7d || {};
  const balanceDisplay = deepSeekOfficialBalanceDisplay(deepseekBalance);
  const balanceCurrency = balanceDisplay.currency;
  const balanceValue = deepseekBalance.status === "ok" ? balanceDisplay.value : "--";
  const balance24h = (deepseekBalance.balance_24h_changes || {})[balanceCurrency || "CNY"] || {};
  const balanceDelta = Number(balance24h.delta);
  const balanceSpend = Number.isFinite(balanceDelta) && balanceDelta <= 0
    ? compactCny(Math.abs(balanceDelta))
    : "--";
  const codexTotal = (windowUsage) => compactTokenUnit(windowUsage.total_tokens ?? windowUsage.tokens_used);
  const codexCachedNote = (windowUsage) => `cached ${compactTokenUnit(windowUsage.cached_input_tokens)} · ${compactInteger(windowUsage.sessions || windowUsage.threads)} sessions`;
  return `
    <div class="usage-summary-row">
      <section class="usage-segment usage-rate-segment">
        <div class="usage-segment-head"><span>CODEX 额度</span><small>${escapeHtml(rateSource.updated_at ? `更新 ${compactClockTime(rateSource.updated_at)}` : "等待官方窗口")}</small></div>
        <div class="usage-rate-stack">
          ${overviewRateWindowHtml("5H", rateWindows.fiveHour)}
          ${overviewRateWindowHtml("WEEKLY", rateWindows.weekly)}
        </div>
      </section>
      <section class="usage-segment">
        <div class="usage-segment-head"><span>CODEX TOKEN</span></div>
        <div class="usage-inline-metrics">
          ${usageCompactMetric("24H token", codexTotal(codex24h), codexCachedNote(codex24h))}
          ${usageCompactMetric("7D token", codexTotal(codex7d), codexCachedNote(codex7d))}
        </div>
      </section>
      <section class="usage-segment">
        <div class="usage-segment-head"><span>DEEPSEEK 余额</span></div>
        <div class="usage-inline-metrics">
          ${usageCompactMetric("余额", balanceValue, deepseekBalance.status === "ok" ? "官方余额接口" : "官方余额未返回")}
          ${usageCompactMetric("24H 余额消耗", balanceSpend, balanceSpend === "--" ? "尚无完整 24H 余额窗口" : "官方余额差")}
        </div>
      </section>
      <section class="usage-segment">
        <div class="usage-segment-head"><span>DEEPSEEK TOKEN</span></div>
        <div class="usage-inline-metrics">
          ${usageCompactMetric("24H token", compactTokenUnit(deepseek24h.total_tokens), `${compactInteger(deepseek24h.request_count)} records`)}
          ${usageCompactMetric("7D token", compactTokenUnit(deepseek7d.total_tokens), `${compactInteger(deepseek7d.request_count)} records`)}
        </div>
      </section>
    </div>
  `;
}

function overviewCodexRateLimitHtml(rate) {
  const limits = rate.rate_limits || {};
  return `
    ${overviewRateWindowHtml("5h", limits.primary || {})}
    ${overviewRateWindowHtml("Weekly", limits.secondary || {})}
  `;
}

function overviewMetricValue(value, fallback = "--") {
  if (value === undefined || value === null || value === "") return fallback;
  if (typeof value === "number") return shortNumber(value, Math.abs(value) >= 10 ? 0 : 3);
  return String(value);
}

function overviewMicroMeter(label, value, note, tone = "idle") {
  const percentValue = overviewPercentValue(value);
  const width = percentValue === null ? 0 : Math.max(0, Math.min(100, percentValue));
  const display = percentValue === null ? "--" : `${shortNumber(width, 1)}%`;
  return `
    <div class="module-micro-meter is-${escapeHtml(tone)}">
      <div><span>${escapeHtml(label)}</span><strong>${escapeHtml(display)}</strong></div>
      <div class="meter-bar"><span style="width:${escapeHtml(String(width))}%"></span></div>
      <small>${escapeHtml(note || "")}</small>
    </div>
  `;
}

function overviewVisualHtml(visual = {}) {
  if (!visual || !visual.type) return "";
  if (visual.type === "paper-performance") {
    const account = Array.isArray(visual.accountSeries) ? visual.accountSeries.filter(Number.isFinite) : [];
    const benchmark = Array.isArray(visual.benchmarkSeries) ? visual.benchmarkSeries.filter(Number.isFinite) : [];
    const all = [...account, ...benchmark];
    const min = all.length ? Math.min(...all) : 0;
    const max = all.length ? Math.max(...all) : 1;
    const range = Math.max(0.0001, max - min);
    const path = (values) => values.map((value, index) => {
      const x = values.length <= 1 ? 0 : (index / (values.length - 1)) * 100;
      const y = 32 - ((value - min) / range) * 28;
      return `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    return `
      <div class="module-visual module-visual-paper-performance">
        <div class="paper-performance-copy">
          <span>账户收益曲线</span>
          <strong>${escapeHtml(visual.title || "生产模拟账户")}</strong>
          <small>${escapeHtml(visual.note || "按账户账本日结计算")}</small>
        </div>
        <svg viewBox="0 0 100 36" preserveAspectRatio="none" role="img" aria-label="账户与沪深300累计收益走势">
          <line x1="0" y1="32" x2="100" y2="32" class="paper-spark-baseline"></line>
          ${benchmark.length > 1 ? `<path d="${path(benchmark)}" class="paper-spark-benchmark"></path>` : ""}
          ${account.length > 1 ? `<path d="${path(account)}" class="paper-spark-account"></path>` : ""}
        </svg>
        <div class="paper-performance-legend"><span><i class="account"></i>模拟账户</span><span><i class="benchmark"></i>沪深 300</span></div>
      </div>
    `;
  }
  if (visual.type === "radial") {
    const percent = overviewPercentValue(visual.value);
    const sweep = percent === null ? 0 : Math.max(0, Math.min(100, percent));
    return `
      <div class="module-visual module-visual-radial" style="--module-sweep:${escapeHtml(`${sweep}%`)}">
        <div class="module-radial-ring"><strong>${percent === null ? "--" : `${shortNumber(sweep, 1)}%`}</strong></div>
        <div>
          <span>${escapeHtml(visual.label || "")}</span>
          <strong>${escapeHtml(visual.title || "")}</strong>
          <small>${escapeHtml(visual.note || "")}</small>
        </div>
      </div>
    `;
  }
  if (visual.type === "distribution") {
    const items = visual.items || [];
    const total = items.reduce((sum, item) => sum + Number(item.value || 0), 0);
    return `
      <div class="module-visual module-visual-distribution">
        <div class="distribution-track" aria-hidden="true">
          ${items.map((item) => {
            const width = total > 0 ? Math.max(4, (Number(item.value || 0) / total) * 100) : 0;
            return `<span class="is-${escapeHtml(item.tone || "idle")}" style="width:${escapeHtml(String(width))}%"></span>`;
          }).join("")}
        </div>
        <div class="distribution-legend">
          ${items.map((item) => `
            <span><i class="is-${escapeHtml(item.tone || "idle")}"></i>${escapeHtml(item.label)} <strong>${escapeHtml(compactInteger(item.value))}</strong></span>
          `).join("")}
        </div>
        <small>${escapeHtml(visual.note || "")}</small>
      </div>
    `;
  }
  if (visual.type === "candidate-board") {
    const candidates = visual.candidates || [];
    return `
      <div class="module-visual module-visual-candidate-board">
        <div class="mini-candidate-list">
          ${candidates.length ? candidates.map((candidate) => `
            <span>
              <strong class="grade-tone-${escapeHtml(String(candidate.grade || "idle").toLowerCase())}">${escapeHtml(candidate.grade || "--")}</strong>
              <em>${escapeHtml(candidate.name || "--")}</em>
              <small>${escapeHtml(candidate.score || "--")}</small>
            </span>
          `).join("") : `<small>${escapeHtml(visual.empty || "暂无候选")}</small>`}
        </div>
        ${visual.note ? `<small>${escapeHtml(visual.note)}</small>` : ""}
      </div>
    `;
  }
  if (visual.type === "factor-mining") {
    const items = visual.gradeItems || [];
    const total = items.reduce((sum, item) => sum + Number(item.value || 0), 0);
    const candidates = visual.candidates || [];
    return `
      <div class="module-visual module-visual-factor-mining">
        <div class="distribution-track" aria-label="候选等级分布">
          ${items.map((item) => {
            const width = total > 0 ? Math.max(4, (Number(item.value || 0) / total) * 100) : 0;
            return `<span class="is-${escapeHtml(item.tone || "idle")}" style="width:${escapeHtml(String(width))}%"></span>`;
          }).join("")}
        </div>
        <div class="distribution-legend">
          ${items.map((item) => `
            <span><i class="is-${escapeHtml(item.tone || "idle")}"></i>${escapeHtml(item.label)} <strong>${escapeHtml(compactInteger(item.value))}</strong></span>
          `).join("")}
        </div>
        <div class="mini-candidate-list recent-candidate-list">
          ${candidates.length ? candidates.map((candidate) => `
            <span>
              <strong class="candidate-code">${escapeHtml(candidate.code || "--")}</strong>
              <em>${escapeHtml(candidate.status || "--")}</em>
              <b class="grade-tone-${escapeHtml(String(candidate.grade || "idle").toLowerCase())}">${escapeHtml(candidate.grade || "--")}</b>
              <small>Quick ${escapeHtml(candidate.quickScore || "--")}</small>
            </span>
          `).join("") : `<small>${escapeHtml(visual.empty || "暂无候选")}</small>`}
        </div>
        ${visual.note ? `<small>${escapeHtml(visual.note)}</small>` : ""}
      </div>
    `;
  }
  if (visual.type === "bars") {
    return `
      <div class="module-visual module-visual-bars">
        ${(visual.items || []).map((item) => overviewMicroMeter(item.label, item.value, item.note, item.tone)).join("")}
      </div>
    `;
  }
  if (visual.type === "model-snapshot") {
    return `
      <div class="module-visual module-visual-model">
        <div class="model-snapshot-main">
          <span>${escapeHtml(visual.label || "Active Feature Set")}</span>
          <strong>${escapeHtml(visual.title || "--")}</strong>
          <small>${escapeHtml(visual.note || "")}</small>
        </div>
        <div class="model-snapshot-metrics">
          ${(visual.metrics || []).map((metric) => `
            <div class="is-${escapeHtml(metric.tone || "idle")}">
              <span>${escapeHtml(metric.label)}</span>
              <strong>${escapeHtml(metric.value)}</strong>
              <small>${escapeHtml(metric.note || "")}</small>
            </div>
          `).join("")}
        </div>
      </div>
    `;
  }
  if (visual.type === "gate") {
    return `
      <div class="module-visual module-visual-gate">
        ${(visual.steps || []).map((step) => `
          <span class="gate-step is-${escapeHtml(step.tone || "idle")}">
            <i></i>
            <strong>${escapeHtml(step.label)}</strong>
            <small>${escapeHtml(step.note || "")}</small>
          </span>
        `).join("")}
      </div>
    `;
  }
  return "";
}

function overviewModuleCard(item) {
  return `
    <button class="module-cockpit-card is-${escapeHtml(item.tone || "idle")} variant-${escapeHtml(item.variant || "standard")} layout-${escapeHtml(item.layout || "facts")}" data-module-kind="${escapeHtml(item.kind || item.target || "overview")}" data-panel-target="${escapeHtml(item.target || "overview")}" type="button">
      <div class="module-card-head">
        <span class="status-dot"></span>
        <div>
          <span class="detail-label">${escapeHtml(item.eyebrow || "Module")}</span>
          <strong>${escapeHtml(item.title)}</strong>
        </div>
        <em>${escapeHtml(item.statusLabel || "状态未知")}</em>
      </div>
      <div class="module-fact-grid">
        ${(item.facts || []).map((fact) => `
          <div>
            <span>${escapeHtml(fact.label)}</span>
            <strong class="${escapeHtml(fact.valueClass || "")}">${escapeHtml(fact.value)}</strong>
            <small>${escapeHtml(fact.note || "")}</small>
          </div>
        `).join("")}
      </div>
      ${overviewVisualHtml(item.visual)}
      <p>${escapeHtml(item.summary || "")}</p>
    </button>
  `;
}

function overviewResourceGauge(label, value, note, tone = "good") {
  const percentValue = overviewPercentValue(value);
  if (percentValue === null) return "";
  const width = Math.max(0, Math.min(100, percentValue));
  return `
    <div class="resource-gauge is-${escapeHtml(tone)}" style="--resource-sweep:${escapeHtml(String(width))}%">
      <div class="resource-ring" aria-label="${escapeHtml(`${label} ${shortNumber(width, 1)}%`)}">
        <strong>${escapeHtml(shortNumber(width, 1))}%</strong>
      </div>
      <div class="resource-gauge-copy">
        <span class="detail-label">${escapeHtml(label)}</span>
        <p>${escapeHtml(note || "")}</p>
      </div>
    </div>
  `;
}

function buildOverviewModules(ctx) {
  const {
    data,
    runtime,
    digest,
    latestStep,
    registry,
    registryActiveKnown,
    activeValueStatusKnown,
    activeValues,
    activeValuesSynced,
    model,
    modelRegistry,
    modelRegistryLoaded,
    production,
    productionValidation,
    trading,
    prediction,
    tradingWarnings,
    pendingTrading,
    staleStocks,
    coverage,
    modelFeatureStale,
    factorAudit,
    featureSetCatalog,
    factorControl,
    modelResearch,
    paperFleet,
    benchmarkRows,
  } = ctx;
  const models = modelRegistry.items || modelRegistry.models || [];
  const modelRegistrySummary = model.registry_summary || model.readiness?.model_registry_summary || {};
  const researchCount = Number(modelRegistrySummary.research ?? models.filter((item) => item.status === "research").length);
  const candidateCount = Number(modelRegistrySummary.candidate ?? models.filter((item) => item.status === "candidate").length);
  const candidates = liveCandidates(100);
  const grades = candidates.reduce((acc, candidate) => {
    const grade = candidateGrade(candidate);
    acc[grade] = (acc[grade] || 0) + 1;
    return acc;
  }, {});
  const gradeTone = (grade) => {
    if (grade === "A") return "good";
    if (grade === "B") return "info";
    if (grade === "C" || grade === "P") return "warn";
    if (grade === "D") return "danger";
    return "idle";
  };
  const gradeItems = ["A", "B", "C", "D", "P", "--"]
    .map((grade) => ({ label: grade, value: grades[grade] || 0, tone: gradeTone(grade) }))
    .filter((item) => item.value > 0);
  const recentCandidateRows = [...candidates]
    .sort((a, b) => {
      const aTime = parseIso(a.tool_ts || a.updated_at || a.created_at || a.completed_at)?.getTime() || 0;
      const bTime = parseIso(b.tool_ts || b.updated_at || b.created_at || b.completed_at)?.getTime() || 0;
      return bTime - aTime;
    })
    .slice(0, 3);
  const recentCandidates = recentCandidateRows.map((candidate) => {
    const metrics = candidateMetrics(candidate);
    return {
      code: candidateProcessLabel(candidate, "") || text(candidate.candidate_id || candidate.id || candidate.factor_id, "--"),
      status: candidateStageLabel(candidate),
      grade: candidateGrade(candidate),
      quickScore: overviewMetricValue(metrics.quick_score ?? candidate.quick_score ?? candidate.score),
    };
  });
  const recentCandidate = recentCandidateRows[0] || {};
  const researchStepCount = Number(digest.event_count || researchSteps().length || 0);
  const runtimeCounts = runtime.progress_counts || digest.progress_counts || {};
  const quantgptSummary = digest.quantgpt_task_summary || {};
  const historicalCandidateCount = candidates.length || Number(runtimeCounts.candidates || runtimeCounts.candidate_records || quantgptSummary.candidate_count || quantgptSummary.completed_count || 0);
  const researchHistoryCount = Math.max(historicalCandidateCount, researchStepCount);
  const recentResearchAge = ageLabel(runtime.updated_at || digest.updated_at || latestStep?.ts || latestStep?.created_at || recentCandidate.source_step_ts || recentCandidate.tool_ts);
  const stopRequested = /checkpoint[\s_-]*stop|operator_stop_requested|stop[\s_-]*handoff/i.test([
    runtime.current_phase,
    digest.current_phase,
    runtime.current_action,
    digest.current_action,
  ].join(" "));
  const nextResearchStage = stopRequested
    ? "等待下一次启动"
    : overviewPhaseLabel(
      runtime.next_phase
        || digest.next_phase
        || researchStepTransition(latestStep).transition?.next_action
        || latestStep.next_action
        || runtime.next_action
        || digest.current_action,
      "等待下一步"
    );
  const runRoundStage = overviewResearchRunRoundStage(
    {
      ...runtime,
      run_id: factorControl.run_id || runtime.run_id,
      round_id: factorControl.round_id || runtime.round_id || digest.round_id || latestStep.round_id || candidateRoundId(recentCandidate),
      stage_id: factorControl.stage_id || runtime.stage_id,
      stage: factorControl.stage || runtime.stage,
    },
    digest,
    latestStep
  );
  const bestModel = production.model_id
    ? production
    : models.find((item) => item.status === "candidate") || [...models].sort((a, b) => Number(b.metadata?.research_score ?? b.research_score ?? -999) - Number(a.metadata?.research_score ?? a.research_score ?? -999))[0] || {};
  const bestModelIcir = bestModel.icir ?? bestModel.rank_icir ?? bestModel.ic_mean ?? bestModel.rank_ic;
  const bestModelIcirLabel = bestModel.icir !== undefined && bestModel.icir !== null
    ? "最佳 ICIR"
    : bestModel.rank_icir !== undefined && bestModel.rank_icir !== null
      ? "最佳 Rank ICIR"
      : "最佳 IC";
  const auditSummary = factorAudit.summary || {};
  const auditIssueFactors = (factorAudit.factor_checks || []).filter((item) => Array.isArray(item?.issues) && item.issues.length);
  const auditedFactorCount = Number(auditSummary.factor_count || auditSummary.active_count || 0);
  const auditIssueCount = Number(auditSummary.data_issue_count || 0);
  const auditWatchCount = Number(auditSummary.watch_count || 0);
  const currentAuditUniverse = Number(auditSummary.current_active_count || registry.active || 0);
  const auditUncoveredCount = Math.max(0, currentAuditUniverse - auditedFactorCount);
  const auditModeLabel = auditSummary.scope === "all" ? "全部" : text(auditSummary.audit_type || auditSummary.scope, "因子库");
  const auditConclusion = auditIssueCount > 0 ? `发现 ${compactInteger(auditIssueCount)} 项异常` : auditWatchCount > 0 ? `${compactInteger(auditWatchCount)} 项观察` : auditedFactorCount > 0 ? "未发现异常" : "尚未审计";
  const auditIssueDetail = auditIssueFactors.length
    ? `待补齐因子值列：${auditIssueFactors.map((item) => text(item.factor_id, item.name || "--")).join("、")}`
    : "";
  const featureSets = Array.isArray(featureSetCatalog.items) ? featureSetCatalog.items : [];
  const selectedFeatureSetId = model.orchestrator?.current_context_summary?.selected_feature_set_id || model.active_session?.feature_set_id;
  const activeFeatureSet = model.active_feature_set || featureSets.find((item) => item.feature_set_id === selectedFeatureSetId) || featureSets[0] || {};
  const featureCount = activeFeatureSet.feature_count || activeFeatureSet.factor_count || model.active_values_readiness?.factor_count || model.readiness?.active_feature_manifest?.factor_count;
  const modelStatus = model.active_session?.status || model.orchestrator?.active_session?.status || model.gui_projection?.process_progress?.status || model.status;
  const modelLoaded = Boolean(modelStatus || activeFeatureSet.feature_set_id || model.active_values_readiness);
  const productionDate = data.snapshot?.latest_hdf5_trade_date || data.snapshot?.latest_quantgpt_trade_date || data.snapshot?.latest_qlib_trade_date;
  const promoteTime = data.daily_update?.promoted_at || data.daily_update?.last_successful_promotion?.promoted_at || data.snapshot?.latest_promote_at || data.generated_at;
  const dataStatus = data.update_status || data.status || "unknown";
  const factorControlState = String(factorControl.state || "").toLowerCase();
  const factorPhase = factorControlState || runtime.current_phase || digest.current_phase || "idle";
  const factorTone = factorControlState === "blocked" ? "danger" : factorControlState === "paused" ? "warn" : overviewToneFromText(factorPhase, "idle");
  const dataTone = data.status === "completed" && staleStocks === 0 ? "good" : staleStocks > 0 ? "warn" : overviewToneFromText(dataStatus, "idle");
  const modelTone = modelFeatureStale ? "warn" : overviewToneFromText(model.status, "idle");
  const tradingTone = tradingWarnings.length || pendingTrading ? "warn" : overviewToneFromText(trading.status || prediction.status, "idle");
  const activeTotal = Number(registry.total || 0);
  const activeRatio = activeTotal > 0 ? (Number(registry.active || 0) / activeTotal) * 100 : null;
  const activeValueRatio = registry.active ? (Number(activeValues.column_count || 0) / Number(registry.active || 1)) * 100 : null;
  const featureSyncRatio = registry.active ? (Number(featureCount || 0) / Number(registry.active || 1)) * 100 : null;
  const modelCurrent = modelResearch.current || {};
  const modelReview = modelResearch.llm_review_signal || {};
  const modelExecution = String(modelReview.execution_decision || "").toLowerCase();
  const modelResearchRunning = ["running", "accepted", "queued", "starting"].includes(String(modelCurrent.status || "").toLowerCase())
    || ["running", "accepted", "queued", "starting"].includes(modelExecution);
  const modelResearchPaused = !modelResearchRunning && (modelReview.active === true || ["stopped", "paused", "checkpoint_stop"].includes(modelExecution));
  const modelResearchTone = modelResearchRunning ? "warn" : modelResearchPaused ? "idle" : "idle";
  const modelResearchLabel = modelResearchRunning ? "研究中" : modelResearchPaused ? "暂停复核" : "未运行";
  const activePaperAccount = (paperFleet.accounts || []).find((account) => account.status === "active") || (paperFleet.accounts || [])[0] || {};
  const paperSnapshot = activePaperAccount.latest_snapshot || {};
  const paperHistory = Array.isArray(activePaperAccount.account_history) ? activePaperAccount.account_history : [];
  const initialCapital = Number(activePaperAccount.initial_capital || paperHistory[0]?.account_value || 0);
  const accountValue = Number(paperSnapshot.account_value || paperHistory[paperHistory.length - 1]?.account_value || 0);
  const cumulativeReturn = initialCapital > 0 && accountValue > 0 ? accountValue / initialCapital - 1 : null;
  const dailyReturn = Number(paperHistory[paperHistory.length - 1]?.daily_return);
  const benchmarkByDate = new Map((benchmarkRows || []).map((row) => [String(row.date || row.trade_date || ""), Number(row.close)]));
  const comparablePaperHistory = paperHistory.filter((row) => Number.isFinite(benchmarkByDate.get(String(row.trade_date || ""))));
  const benchmarkBase = comparablePaperHistory.length ? benchmarkByDate.get(String(comparablePaperHistory[0].trade_date || "")) : null;
  const benchmarkLast = comparablePaperHistory.length ? benchmarkByDate.get(String(comparablePaperHistory[comparablePaperHistory.length - 1].trade_date || "")) : null;
  const benchmarkReturn = Number(benchmarkBase) > 0 && Number.isFinite(benchmarkLast) ? benchmarkLast / benchmarkBase - 1 : null;
  const relativeReturn = Number.isFinite(cumulativeReturn) && Number.isFinite(benchmarkReturn)
    ? (1 + cumulativeReturn) / (1 + benchmarkReturn) - 1
    : null;
  const accountSeries = paperHistory.map((row) => initialCapital > 0 ? Number(row.account_value || 0) / initialCapital - 1 : NaN);
  const benchmarkSeries = comparablePaperHistory.map((row) => Number(benchmarkByDate.get(String(row.trade_date || ""))) / Number(benchmarkBase) - 1);
  const paperPending = Array.isArray(activePaperAccount.pending_recommendations) ? activePaperAccount.pending_recommendations.length : pendingTrading;
  const paperBlocked = (activePaperAccount.integrity_issues || []).length > 0 || (paperFleet.blocked_accounts || []).length > 0 || String(paperFleet.status || "").includes("blocked");
  const paperTone = paperBlocked ? "danger" : paperPending ? "warn" : "good";
  return [
    {
      eyebrow: "Paper Trading",
      title: "模拟交易",
      target: "trading",
      kind: "paper-trading",
      tone: paperTone,
      variant: "featured",
      layout: "performance",
      statusLabel: paperBlocked ? "已阻断" : "运行正常",
      facts: [
        { label: "账户净值", value: accountValue ? compactInteger(accountValue) : "--", note: text(activePaperAccount.display_name, "生产模拟账户") },
        { label: "累计收益", value: Number.isFinite(cumulativeReturn) ? signedPercent(cumulativeReturn, 2) : "--", valueClass: numberTone(cumulativeReturn), note: `最新账本 ${text(paperSnapshot.trade_date, "--")}` },
        { label: "相对沪深 300", value: Number.isFinite(relativeReturn) ? signedPercent(relativeReturn, 2) : "--", valueClass: numberTone(relativeReturn), note: Number.isFinite(benchmarkReturn) ? `同期基准 ${signedPercent(benchmarkReturn, 2)}` : "等待同期基准" },
        { label: "今日收益", value: Number.isFinite(dailyReturn) ? signedPercent(dailyReturn, 2) : "--", valueClass: numberTone(dailyReturn), note: `股票仓位 ${shortNumber(Number(paperSnapshot.risk_metrics?.actual_stock_exposure || 0) * 100, 1)}%` },
        { label: "待执行计划", value: `${compactInteger(paperPending)} 条`, note: paperPending ? `信号 ${text(activePaperAccount.latest_recommendation?.signal_date, "--")}` : "当前无需调仓" },
      ],
      visual: {
        type: "paper-performance",
        title: text(activePaperAccount.display_name, "生产模拟账户"),
        note: `${paperHistory.length} 个账本日 · 收益已扣除实际交易成本`,
        accountSeries,
        benchmarkSeries,
      },
      summary: paperBlocked
        ? overviewCleanMessage(activePaperAccount.integrity_issues?.[0]?.message || tradingWarnings[0] || "模拟交易链路存在阻断，请进入控制台处理。")
        : paperPending
          ? `${paperPending} 条计划已生成，等待下一交易日数据后自动执行。`
          : "账户账本已追平生产数据，当前链路运行正常。",
    },
    {
      eyebrow: "Data Foundation",
      title: "数据底座",
      target: "data-foundation",
      tone: dataTone,
      variant: "standard",
      statusLabel: overviewPhaseLabel(dataStatus, "未知"),
      facts: [
        { label: "生产日期", value: text(productionDate, "--"), note: coverage === null ? (data.snapshot?.quantgpt_contract?.ok ? "数据契约通过" : "覆盖率 --") : `覆盖率 ${shortNumber(coverage, 1)}%` },
        { label: "滞后股票", value: compactInteger(staleStocks), note: staleStocks ? "需关注" : "覆盖正常" },
        { label: "行情文件", value: compactInteger(data.snapshot?.quantgpt_stock_parquet_count || data.snapshot?.quantgpt_contract?.stock_file_count), note: ageLabel(promoteTime) },
      ],
      summary: dataStatus === "running" ? "数据更新中，进度详情见数据底座页。" : overviewCleanMessage(data.data_quality_summary?.status || `最新交易日 ${text(productionDate, "--")}`),
    },
    {
      eyebrow: "Factor Mining",
      title: "因子挖掘",
      target: "research",
      tone: factorTone,
      variant: "standard",
      statusLabel: factorControlState === "blocked" ? "已阻塞" : factorControlState === "paused" ? "已暂停" : overviewPhaseLabel(factorPhase, "空闲"),
      facts: [
        { label: "当前任务", value: runRoundStage.run, note: `${runRoundStage.round} · ${runRoundStage.stage}` },
        { label: "运行状态", value: factorControlState === "blocked" ? "阻塞" : factorControlState === "paused" ? "暂停" : overviewPhaseLabel(factorPhase, "空闲"), note: `记录 ${compactInteger(researchHistoryCount)}` },
        { label: "后续动作", value: factorControlState === "blocked" ? "先处理阻断" : clip(nextResearchStage, 18), note: stopRequested ? "等待人工启动" : "按研究状态推进" },
      ],
      summary: factorControlState === "blocked"
        ? "当前研究已阻塞；进入因子研究查看阻断原因与可恢复动作。"
        : factorControlState === "paused"
          ? "因子研究已暂停，历史结果保留，等待人工恢复。"
          : clip(overviewCleanMessage(runtime.latest_decision || latestStep?.decision || runtime.current_action || digest.current_action || "当前没有运行中的因子研究。"), 110),
    },
    {
      eyebrow: "Factor Library",
      title: "因子库",
      target: "library",
      tone: auditSummary.stale ? "warn" : auditSummary.status === "completed" ? "good" : (registryActiveKnown ? "good" : "idle"),
      variant: "standard",
      statusLabel: auditUncoveredCount > 0 ? "部分覆盖" : auditSummary.status === "completed" ? "审计正常" : (registryActiveKnown ? "已读取" : "读取中"),
      facts: [
        { label: "Active / Total", value: registryActiveKnown ? `${text(registry.active, "0")} / ${registry.total === undefined || registry.total === null ? "--" : text(registry.total, "0")}` : "--", note: "活跃生产池" },
        { label: "审计结论", value: auditConclusion, note: auditedFactorCount ? `已审计 ${compactInteger(auditedFactorCount)} 个${auditUncoveredCount ? ` · ${compactInteger(auditUncoveredCount)} 个未覆盖` : ""}` : "等待审计" },
        { label: "平均 ICIR", value: overviewMetricValue(registry.avg_icir), note: "active pool" },
      ],
      summary: auditUncoveredCount > 0
        ? `${auditIssueCount ? auditIssueDetail : `最近一次${auditModeLabel}审计未发现明显异常`}；当前新增的 ${compactInteger(auditUncoveredCount)} 个 active 因子尚未纳入。`
        : auditSummary.status === "completed" ? `最近一次审计覆盖 ${compactInteger(auditedFactorCount)} 个因子，${auditConclusion}。` : "等待因子库审计摘要。",
    },
    {
      eyebrow: "Model Research",
      title: "模型研究",
      target: "model-research",
      tone: modelResearchTone,
      variant: "standard",
      statusLabel: modelResearchLabel,
      facts: [
        { label: "当前任务", value: modelResearchRunning ? text(modelCurrent.model_run_id || modelCurrent.round_group_id, "运行中") : "无", note: modelResearchPaused ? "没有后台训练进程" : "当前未启动模型研究" },
        { label: "最近结论", value: modelResearchPaused ? "等待人工复核" : text(modelCurrent.decision, "--"), note: text(modelReview.execution_label, "暂无活跃研究") },
        { label: "最近更新时间", value: text((modelReview.ts || modelCurrent.ts || "").slice(0, 10), "--"), note: "历史证据仅作参考" },
      ],
      summary: modelResearchRunning
        ? "模型研究正在运行，进入模块查看轮次和日志。"
        : modelResearchPaused
          ? overviewCleanMessage(modelReview.reason_summary || "当前没有模型研究进程；上一轮已暂停，等待人工复核。")
          : "当前没有模型研究任务；历史 Feature Set 和研究结果不代表正在运行。",
    },
    {
      eyebrow: "Model Registry",
      title: "模型库",
      target: "model-library",
      tone: production.model_id ? "good" : "warn",
      variant: "standard",
      statusLabel: modelRegistryLoaded ? (production.model_id ? "生产模型" : "未生产") : "读取中",
      facts: [
        { label: "生产模型", value: modelRegistryLoaded ? text(production.model_id, "未设置") : "--", note: modelRegistryLoaded ? overviewPhaseLabel(productionValidation.status, "待验证") : "读取中" },
        { label: "特征数量", value: modelRegistryLoaded ? compactInteger(production.feature_count || production.factor_count) : "--", note: text(production.feature_set_id, "Feature Set") },
        { label: "生产 Rank ICIR", value: modelRegistryLoaded ? overviewMetricValue(production.rank_icir) : "--", note: "当前生产版本" },
      ],
      summary: !modelRegistryLoaded ? "模型库接口仍在读取，返回后自动刷新此卡片。" : production.model_id ? "已有生产模型，可进入模型库查看验证与版本。" : "暂无生产模型，交易链路会保持阻塞态。",
    },
  ];
}

function buildOverviewHealthModel() {
  const data = serviceOutputs(state.data);
  const factorConsole = serviceOutputs(state.factorConsole);
  const factorStatus = serviceOutputs(state.factorStatus);
  const factorLibrary = serviceOutputs(state.factorLibraryRaw);
  const factorAudit = serviceOutputs(state.factorAudit);
  const model = serviceOutputs(state.modelStatus);
  const featureSetCatalog = serviceOutputs(state.modelFeatureSets);
  const modelRegistry = serviceOutputs(state.modelRegistry);
  const productionStatus = serviceOutputs(state.modelProduction);
  const prediction = serviceOutputs(state.predictionStatus);
  const trading = serviceOutputs(state.tradingStatus);
  const embeddedProductionStatus = serviceOutputs(trading.production_model);
  const effectiveProductionStatus = Object.keys(productionStatus).length ? productionStatus : embeddedProductionStatus;
  const factorControl = serviceOutputs(state.factorResearchControl);
  const modelResearch = serviceOutputs(state.modelResearchCurrent);
  const paperFleet = serviceOutputs(state.paperFleetStatus);
  const benchmarkRows = serviceOutputs(state.paperBenchmark).rows || [];
  const pipeline = serviceOutputs(state.pipelineStatus);
  const runtimeStatus = serviceOutputs(state.platformRuntime);
  const digest = liveResearchDigest();
  const runtime = factorConsole.runtime_view || digest.runtime_view || {};
  const latestStep = latestLlmOutput();
  const registryBase = factorStatus.registry_summary || factorLibrary.registry_summary || factorConsole.registry_summary || {};
  const registry = { ...registryBase };
  if ((registry.active === undefined || registry.active === null) && factorLibrary.total !== undefined) {
    registry.active = factorLibrary.total;
  }
  if ((registry.total === undefined || registry.total === null) && factorLibrary.total !== undefined) {
    registry.total = factorLibrary.total;
  }
  if (registry.avg_icir === undefined || registry.avg_icir === null) {
    const icirs = (factorLibrary.items || []).map((item) => Number(item.icir)).filter((value) => Number.isFinite(value));
    if (icirs.length) registry.avg_icir = icirs.reduce((sum, value) => sum + value, 0) / icirs.length;
  }
  const activeValues = factorConsole.active_values_store || factorConsole.readiness?.active_factor_values || factorStatus.active_values_store || factorStatus.readiness?.active_factor_values || {};
  const activeValueStatusKnown = registryBase.active !== undefined && registryBase.active !== null;
  const registryActiveKnown = registry.active !== undefined && registry.active !== null;
  const registryActiveCount = Number(registry.active || 0);
  const activeValuesSynced = activeValueStatusKnown && activeValues.exists && !activeValues.stale && Number(activeValues.column_count || 0) === registryActiveCount;
  const modelFeatureStale = Boolean(model.feature_set_stale || model.readiness?.feature_set_stale || model.active_feature_set?.feature_set_stale);
  const models = modelRegistry.items || modelRegistry.models || [];
  const modelRegistrySummary = model.registry_summary || model.readiness?.model_registry_summary || {};
  const researchCount = Number(modelRegistrySummary.research ?? models.filter((item) => item.status === "research").length);
  const candidateCount = Number(modelRegistrySummary.candidate ?? models.filter((item) => item.status === "candidate").length);
  const productionCount = Number(modelRegistrySummary.production ?? models.filter((item) => item.status === "production").length);
  const production = effectiveProductionStatus.production_model || models.find((item) => item.status === "production") || {};
  const modelRegistryLoaded = Boolean(models.length || model.readiness?.model_registry_summary || production.model_id);
  const productionValidation = trading.production_validation_summary || effectiveProductionStatus.production_validation || {};
  const staleStocks = Number(data.snapshot?.quantgpt_stale_stock_count || 0);
  const coverage = overviewPercentValue(data.snapshot?.quantgpt_latest_coverage_ratio);
  const tradingWarnings = trading.warnings || state.tradingStatus?.warnings || [];
  const pendingTrading = trading.pending_recommendations?.length || 0;

  const actions = [];
  const addAction = (tone, title, detail, target) => {
    actions.push({ tone, title, detail, target });
  };
  if (staleStocks > 0 || data.status !== "completed") {
    addAction("warn", "数据底座需要关注", `滞后股票 ${staleStocks}，最新交易日 ${text(data.snapshot?.latest_hdf5_trade_date, "--")}`, "data-foundation");
  }
  if (String(factorControl.state || runtime.current_phase || digest.current_phase || "").toLowerCase().includes("block")) {
    addAction("danger", "因子研究存在阻塞", clip(overviewCleanMessage(runtime.current_action || latestStep?.decision || "检查研究工作区。"), 160), "research");
  }
  if (activeValueStatusKnown && !activeValuesSynced) {
    addAction("warn", "活跃因子值未同步", `${text(activeValues.column_count, "0")} 列 / ${text(registry.active, "0")} 个活跃因子`, "library");
  }
  if (modelFeatureStale) {
    addAction("warn", "模型特征快照过期", overviewCleanMessage(model.readiness?.stale_reason || model.active_feature_set?.stale_reason || "需要查看模型研究页。"), "model-research");
  }
  if (!production.model_id || productionValidation.status === "failed") {
    addAction("warn", "生产模型需要确认", overviewCleanMessage(productionValidation.status || "暂无生产模型"), "model-library");
  }
  if (pendingTrading || tradingWarnings.length) {
    addAction("warn", "交易链路有待处理项", overviewCleanMessage(tradingWarnings[0] || `${pendingTrading} 条待处理推荐`), "trading");
  }
  if (overviewToneFromText(pipeline.status, "idle") === "danger") {
    addAction("danger", "Pipeline 状态异常", text(pipeline.latest_run?.err || pipeline.latest_run?.pipeline?.error || pipeline.status), "trading");
  }
  if (!actions.length) {
    addAction("good", "当前没有高优先级阻塞", "平台主要链路可继续按研究和模型节奏推进。", "research");
  }

  const overallTone = actions.some((item) => item.tone === "danger")
    ? "danger"
    : actions.some((item) => item.tone === "warn")
      ? "warn"
      : "good";
  const overallLabel = overallTone === "danger" ? "需要处理" : overallTone === "warn" ? "有关注项" : "运行正常";

  const flowNodes = [
    {
      title: "数据底座",
      target: "data-foundation",
      tone: data.status === "completed" && staleStocks === 0 ? "good" : "warn",
      value: text(data.snapshot?.latest_hdf5_trade_date, "--"),
      note: coverage === null ? "覆盖率 --" : `覆盖率 ${shortNumber(coverage, 1)}%`,
    },
    {
      title: "因子研究",
      target: "research",
      tone: String(factorControl.state || "").toLowerCase() === "blocked" ? "danger" : overviewToneFromText(factorControl.state || runtime.current_phase || digest.current_phase, "idle"),
      value: String(factorControl.state || "").toLowerCase() === "blocked" ? "已阻塞" : overviewPhaseLabel(factorControl.state || runtime.current_phase || digest.current_phase, "空闲"),
      note: ageLabel(runtime.updated_at || digest.updated_at),
    },
    {
      title: "因子库",
      target: "library",
      tone: activeValueStatusKnown ? (activeValuesSynced ? "good" : "warn") : (registryActiveKnown ? "good" : "idle"),
      value: registryActiveKnown ? `${text(registry.active, "0")} 个活跃` : "待加载",
      note: activeValueStatusKnown ? (activeValuesSynced ? "因子值已同步" : `${text(activeValues.column_count, "0")} 列因子值`) : (registryActiveKnown ? "注册表摘要待加载" : "因子库状态读取中"),
    },
    {
      title: "模型研究",
      target: "model-research",
      tone: "idle",
      value: modelResearch.llm_review_signal?.active ? "暂停复核" : "未运行",
      note: "当前无后台模型研究任务",
    },
    {
      title: "模型库",
      target: "model-library",
      tone: production.model_id ? "good" : "warn",
      value: text(production.model_id, "未设生产"),
      note: `research ${text(researchCount, "0")} · candidate ${text(candidateCount, "0")} · production ${text(productionCount, "0")}`,
    },
    {
      title: "模拟交易",
      target: "trading",
      tone: tradingWarnings.length ? "warn" : overviewToneFromText(trading.status || prediction.status, "idle"),
      value: overviewPhaseLabel(trading.status || prediction.status, "unknown"),
      note: tradingWarnings[0] ? clip(overviewCleanMessage(tradingWarnings[0]), 64) : pendingTrading ? `${pendingTrading} 条待处理` : text(trading.latest_recommendation?.signal_date, "等待推荐"),
    },
  ];

  const moduleSignals = [
    {
      label: "数据",
      target: "data-foundation",
      value: text(data.snapshot?.latest_hdf5_trade_date, "--"),
      note: `${text(data.snapshot?.quantgpt_stock_parquet_count, "0")} 份行情文件 · ${staleStocks} 个滞后`,
      tone: staleStocks ? "warn" : "good",
    },
    {
      label: "研究",
      target: "research",
      value: overviewPhaseLabel(runtime.current_phase || digest.current_phase, "空闲"),
      note: clip(overviewCleanMessage(latestStep?.summary || runtime.current_action || "研究控制台待更新"), 120),
      tone: overviewToneFromText(runtime.current_phase || digest.current_phase, "idle"),
    },
    {
      label: "因子库",
      target: "library",
      value: registryActiveKnown ? (registry.total !== undefined && registry.total !== null ? `${text(registry.active, "0")} / ${text(registry.total, "0")}` : `${text(registry.active, "0")} 个活跃`) : "待加载",
      note: activeValueStatusKnown ? (activeValuesSynced ? "活跃因子值已对齐" : "活跃因子值需要检查") : (registryActiveKnown ? "注册表摘要待加载" : "等待因子库状态"),
      tone: activeValueStatusKnown ? (activeValuesSynced ? "good" : "warn") : (registryActiveKnown ? "good" : "idle"),
    },
    {
      label: "模型",
      target: "model-research",
      value: overviewPhaseLabel(model.status, "未知"),
      note: modelFeatureStale ? overviewCleanMessage(model.readiness?.stale_reason || "特征快照过期") : "特征快照已对齐",
      tone: modelFeatureStale ? "warn" : overviewToneFromText(model.status, "idle"),
    },
    {
      label: "交易",
      target: "trading",
      value: text(trading.latest_recommendation?.signal_date || overviewPhaseLabel(trading.status || prediction.status, "--"), "--"),
      note: overviewCleanMessage(tradingWarnings[0] || `${pendingTrading} 条待处理推荐`),
      tone: tradingWarnings.length || pendingTrading ? "warn" : overviewToneFromText(trading.status || prediction.status, "idle"),
    },
  ];
  const businessModules = buildOverviewModules({
    data,
    runtime,
    digest,
    latestStep,
    registry,
    registryActiveKnown,
    activeValueStatusKnown,
    activeValues,
    activeValuesSynced,
    model,
    modelRegistry,
    modelRegistryLoaded,
    production,
    productionValidation,
    trading,
    prediction,
    tradingWarnings,
    pendingTrading,
    staleStocks,
    coverage,
    modelFeatureStale,
    factorAudit,
    featureSetCatalog,
    factorControl,
    modelResearch,
    paperFleet,
    benchmarkRows,
  });

  return {
    overallTone,
    overallLabel,
    keyBlocker: actions[0],
    actions: actions.slice(0, 5),
    flowNodes,
    moduleSignals,
    businessModules,
    system: runtimeStatus.system || {},
    services: runtimeStatus.services || {},
    usage: overviewUsageModel(runtimeStatus.usage || {}),
    warnings: runtimeStatus.warnings || state.platformRuntime?.warnings || [],
    runtimeUnavailable: state.platformRuntime?._failed || state.platformRuntime?.ok === false || !runtimeStatus.system,
  };
}

function renderOverviewCockpit() {
  const model = buildOverviewHealthModel();
  renderBackgroundWorkflowStatus("overview-background-workflow-status", ["data_foundation", "paper_trading"]);
  const hero = document.getElementById("overview-hero-status");
  if (hero) {
    // The platform attention state belongs with the system health signals below.
    // Keeping this slot empty also avoids duplicating a warning in the page hero.
    hero.innerHTML = "";
    hero.hidden = true;
  }

  const systemNode = document.getElementById("overview-system-rail");
  if (systemNode) {
    const system = model.system;
    const services = model.services;
    const disk = system.disk || {};
    const runtimeReady = !model.runtimeUnavailable && disk.available;
    const serviceItems = [
      { label: "API", value: services.api || state.health },
      { label: "GUI", value: services.gui || { ok: true, status: "loaded" } },
      { label: "QuantGPT", value: services.quantgpt },
      {
        label: "运行时长",
        value: {
          ok: runtimeReady,
          status: text(system.process_uptime_human, runtimeReady ? "--" : "读取中"),
        },
      },
    ];
    const attentionTone = ["good", "warn", "danger"].includes(model.overallTone)
      ? model.overallTone
      : "warn";
    const attentionLabel = attentionTone === "good" ? "一切正常" : text(model.overallLabel, "需要关注");
    const attentionDetail = attentionTone === "good"
      ? "核心服务与业务链路正常"
      : text(model.keyBlocker?.title, "请查看待处理事项");
    const serviceChipsHtml = serviceItems.map((item) => `
      <span class="service-chip is-${escapeHtml(overviewServiceTone(item.value))}">
        ${escapeHtml(item.label)}
        <strong>${escapeHtml(text(item.value?.status || item.value?.ok === true && "ok" || item.value?.ok === false && "error" || "unknown"))}</strong>
      </span>
    `).join("");
    systemNode.innerHTML = `
      <div class="system-status-head">
        <div>
          <p class="eyebrow">System Status</p>
          <h3>系统状态</h3>
        </div>
        <div class="service-chip-list">${serviceChipsHtml}</div>
      </div>
      ${runtimeReady ? `
        <div class="system-resource-summary">
          <div class="resource-gauge-grid">
            ${overviewResourceGauge("Disk", disk.percent, `${text(disk.used_human, "--")} 已用 / ${text(disk.free_human, "--")} 可用 · 容量 ${text(disk.total_human, "--")}`, overviewResourceTone(disk.percent))}
          </div>
          <div class="system-attention is-${escapeHtml(attentionTone)}">
            <i></i>
            <div><span>平台状态</span><strong>${escapeHtml(attentionLabel)}</strong><small>${escapeHtml(attentionDetail)}</small></div>
          </div>
        </div>
      ` : `
        <div class="runtime-mini-grid">
          <div><span class="detail-label">运行指标</span><strong>后台读取中</strong><small>返回后自动补齐</small></div>
          <div><span class="detail-label">业务数据</span><strong>${escapeHtml(text(state.data?.outputs?.snapshot?.latest_hdf5_trade_date || serviceOutputs(state.data).snapshot?.latest_hdf5_trade_date, "--"))}</strong><small>生产最新交易日</small></div>
        </div>
        <div class="system-attention is-${escapeHtml(attentionTone)}">
          <i></i>
          <div><span>平台状态</span><strong>${escapeHtml(attentionLabel)}</strong><small>${escapeHtml(attentionDetail)}</small></div>
        </div>
      `}
    `;
  }

  const flowNode = document.getElementById("overview-flow-lane");
  if (flowNode) {
    flowNode.innerHTML = model.flowNodes.map((item, index) => `
      <button class="flow-node is-${escapeHtml(item.tone)}" data-panel-target="${escapeHtml(item.target)}" type="button">
        <span class="flow-index">${index + 1}</span>
        <strong>${escapeHtml(item.title)}</strong>
        <em>${escapeHtml(item.value)}</em>
        <small>${escapeHtml(item.note)}</small>
      </button>
    `).join("");
  }

  const matrixNode = document.getElementById("overview-module-matrix");
  const paperNode = document.getElementById("overview-paper-highlight");
  const paperModule = model.businessModules.find((item) => item.kind === "paper-trading");
  if (paperNode) {
    paperNode.innerHTML = paperModule ? overviewModuleCard(paperModule) : `<div class="empty-state compact">模拟交易状态正在读取。</div>`;
  }
  if (matrixNode) {
    matrixNode.innerHTML = model.businessModules.filter((item) => item.kind !== "paper-trading").map(overviewModuleCard).join("");
  }

  const actionNode = document.getElementById("overview-action-queue");
  if (actionNode) {
    actionNode.innerHTML = model.actions.map((item) => `
      <button class="action-item is-${escapeHtml(item.tone)}" data-panel-target="${escapeHtml(item.target)}" type="button">
        <span></span>
        <strong>${escapeHtml(item.title)}</strong>
        <small>${escapeHtml(item.detail)}</small>
      </button>
    `).join("");
  }

  const usageNode = document.getElementById("overview-usage-panel");
  if (usageNode) {
    usageNode.innerHTML = `
      ${overviewUsageStripHtml(model.usage.codex || {}, model.usage.deepseek || {})}
    `;
  }

  const signalsNode = document.getElementById("overview-module-signals");
  if (signalsNode) {
    signalsNode.innerHTML = model.moduleSignals.map((item) => `
      <button class="module-signal-row is-${escapeHtml(item.tone)}" data-panel-target="${escapeHtml(item.target)}" type="button">
        <span>${escapeHtml(item.label)}</span>
        <strong>${escapeHtml(item.value)}</strong>
        <small>${escapeHtml(item.note)}</small>
      </button>
    `).join("");
  }
}

function renderOverviewFailure(error) {
  const message = clip(text(error?.message || error, "unknown overview error"), 240);
  const hero = document.getElementById("overview-hero-status");
  if (hero) {
    hero.hidden = false;
    hero.innerHTML = `
      <span class="overview-status-badge is-danger">读取失败</span>
      <span>${escapeHtml(message)}</span>
    `;
  }
  const systemNode = document.getElementById("overview-system-rail");
  if (systemNode) {
    systemNode.innerHTML = `
      <div class="empty-state">
        平台状态已返回，但总览渲染失败：${escapeHtml(message)}
        <small>前端版本 ${escapeHtml(GUI_BUILD_ID)} · 请刷新页面后重试</small>
      </div>
    `;
  }
}

function renderModelResearch() {
  renderModelCommandConsole();
  const model = serviceOutputs(state.modelStatus);
  const researchCurrentPayload = serviceOutputs(state.modelResearchCurrent);
  const researchCurrentRecord = researchCurrentPayload.current || researchCurrentPayload;
  const researchCurrent = Object.keys(researchCurrentRecord || {}).length
    ? researchCurrentRecord
    : (model.research_current || (model.gui_projection || {}).research_current || {});
  const researchJournal = serviceOutputs(state.modelResearchJournal);
  const researchGuiBrief = researchCurrent.gui_brief || {};
  const researchState = researchCurrent.state || {};
  const researchLlmContext = researchCurrent.llm_context || {};
  const researchJournalEvents = Array.isArray(researchJournal.events)
    ? researchJournal.events
    : (Array.isArray(researchJournal.journal) ? researchJournal.journal : []);
  const researchJournalIsLatestFirst = !Array.isArray(researchJournal.events)
    && Array.isArray(researchJournal.journal);
  const factorConsole = serviceOutputs(state.factorConsole);
  const factorRegistry = factorConsole.registry_summary || {};
  const registry = serviceOutputs(state.modelRegistry);
  const models = registry.items || registry.models || [];
  const modelRunCatalog = serviceOutputs(state.modelRuns);
  const seedDiagnostics = registry.seed_models || modelRunCatalog.seed_models || [];
  const allModelRounds = Array.isArray(modelRunCatalog.rounds) ? modelRunCatalog.rounds : [];
  const allModelSeedRuns = Array.isArray(modelRunCatalog.seed_runs)
    ? modelRunCatalog.seed_runs
    : (Array.isArray(modelRunCatalog.runs) ? modelRunCatalog.runs : []);
  const backtest = serviceOutputs(state.modelBacktest);
  // /model/status is intentionally compact and only returns the latest few
  // campaigns.  The backtest response carries the full selectable catalog, so
  // merge both sources instead of letting a non-empty compact response hide
  // older Rolling results.
  const rollingCampaigns = mergeRollingCampaignCatalog(
    backtest.rolling_campaigns,
    model.rolling_campaigns,
  );
  const latestRollingCampaign = model.latest_rolling_campaign || rollingCampaigns[0] || {};
  const productionStatus = serviceOutputs(state.modelProduction);
  const productionModels = productionStatus.items || productionStatus.production_models || models.filter((item) => item.status === "production");
  const modelPreflight = serviceOutputs(state.modelPreflight);
  const modelOrchStatus = serviceOutputs(state.modelOrchestratorStatus);
  const latestSession = model.live_session || {};
  const populatedRecord = (...records) => records.find((record) => (
    record && typeof record === "object" && Object.keys(record).length > 0
  )) || {};
  // The status API intentionally clears active_* after a job exits.  Keep the
  // most recent completed session visible instead of rendering empty cells.
  const activeSession = populatedRecord(
    modelOrchStatus.active_session,
    (model.orchestrator || {}).active_session,
    model.active_session,
    latestSession,
  );
  const activeJob = populatedRecord(
    modelOrchStatus.active_job,
    (model.orchestrator || {}).active_job,
  );
  const displaySession = populatedRecord(
    activeSession,
    modelOrchStatus.latest_session,
    (model.orchestrator || {}).latest_session,
    model.latest_session,
    latestSession,
    (modelOrchStatus.sessions || [])[0],
  );
  const displayJob = populatedRecord(
    activeJob,
    modelOrchStatus.latest_job,
    (model.orchestrator || {}).latest_job,
    model.latest_job,
  );
  const roundTimeline = model.recent_rounds || [];
  const stageTimeline = model.stage_flow || [];
  const modelSteps = model.latest_research_steps || [];
  const latestModelStep = model.latest_decision || modelSteps[0] || {};
  const latestValidation = model.latest_validation || {};
  const latestImportGate = model.latest_import_gate || {};
  const latestConfigAudit = model.latest_config_audit || {};
  const latestHumanGuidance = model.latest_human_guidance || {};
  const activeRound = model.active_round || {};
  const executionDiagnostics = model.execution_diagnostics || {};
  const researchContract = model.research_contract || {};
  const canonicalExecution = executionDiagnostics.canonical !== false && executionDiagnostics.execution_mode !== "diagnostic_fallback";
  const executionModeLabel = canonicalExecution ? "正式 MCP" : "诊断 / 非正式";
  const executionWarnings = executionDiagnostics.warnings || [];
  const mcpSessions = model.recent_sessions || [];
  const mcpMonitor = { latest_import_gate: latestImportGate };
  const researchLive = {
    latest_decision: latestModelStep,
    latest_human_guidance: latestHumanGuidance,
    rounds: roundTimeline,
    stage_timeline: stageTimeline,
    encoding_warnings: model.encoding_warnings || [],
  };
  const researchSurface = { production_policy: "qlib_lgbm_canonical" };
  const snapshotDrift = {};
  const bestLiveCandidate = model.best_candidate || productionStatus.best_candidate || models.find((item) => item.status === "candidate") || {};
  const bestSessionRound = model.best_session_round || {};
  const latestCompletedRound = model.latest_completed_round || {};
  const bestSessionMetrics = bestSessionRound.metrics || bestSessionRound.metric_summary || {};
  const bestSessionValidation = bestSessionRound.validation || {};
  const nextAction = latestModelStep.next || latestModelStep.stage_transition?.next_action || latestSession.stage || "等待 Codex/MCP 推进";
  const latestRun = {
    status: latestSession.stage || activeRound.stage || roundTimeline[0]?.stage || "",
    model_run_id: latestSession.latest_model_run_id || activeRound.model_run_id || roundTimeline[0]?.model_run_id || "",
    run_error: modelRunErrorSummary(activeRound.run_summary_ref?.run_error || roundTimeline[0]?.run_summary_ref?.run_error || "")
      || activeRound.run_summary_ref?.run_error
      || roundTimeline[0]?.run_summary_ref?.run_error
      || "",
  };
  const best = models.find((item) => item.status === "candidate") || [...models].sort((a, b) => Number(b.metadata?.research_score ?? b.research_score ?? -999) - Number(a.metadata?.research_score ?? a.research_score ?? -999))[0] || {};
  const backtestModel = backtest.model || best;
  const backtestMetrics = backtest.metrics || {};
  const backtestCurve = backtest.curve || [];
  const backtestLast = backtestCurve[backtestCurve.length - 1] || {};
  const diagnostics = backtest.diagnostics || {};
  const stockContribution = backtest.stock_contribution || {};
  const featureCount = Number(model.active_feature_set?.feature_count || model.readiness?.active_feature_manifest?.feature_count || 0);
  const featurePolicy = model.active_feature_set?.feature_snapshot_policy_version || model.readiness?.active_feature_manifest?.feature_snapshot_policy_version || "legacy_feature_dropna_policy";
  const missingSummary = model.active_feature_set?.feature_missing_summary || model.readiness?.active_feature_manifest?.feature_missing_summary || {};
  const activeFactorCount = Number(
    model.readiness?.active_factor_count
    || model.active_feature_set?.factor_count
    || model.readiness?.active_feature_manifest?.factor_count
    || factorRegistry.active
    || 0
  );
  const featureSetStale = Boolean(model.feature_set_stale || model.readiness?.feature_set_stale || model.active_feature_set?.feature_set_stale);
  const activeValuesReadiness = modelPreflight.active_values_readiness || model.active_values_readiness || {};
  const activeValuesReady = activeValuesReadiness.safe_to_freeze_feature_set === true;
  const activeValuesStatusText = activeValuesReady
    ? `active values ready · source ${text(activeValuesReadiness.refresh_source_mode_default, "parquet")}`
    : `active values blocked · ${text(modelPreflight.stale_reason || activeValuesReadiness.feature_snapshot_block_reason || activeValuesReadiness.required_action, "not ready")}`;
  const activeValuesJob = activeValuesReadiness.active_values_job || {};
  const activeValuesJobText = activeValuesJob.job_id
    ? `job ${text(activeValuesJob.status, "--")} · ${text(activeValuesJob.job_id, "--")}`
    : (activeValuesReadiness.resume_available ? `resume available · ${text(activeValuesReadiness.resume_action, "")}` : "");
  const featureSyncNote = featureSetStale || (activeFactorCount && featureCount !== activeFactorCount)
    ? `模型特征集未同步：因子库 active=${activeFactorCount}，模型特征=${featureCount}，${text(model.readiness?.stale_reason || model.active_feature_set?.stale_reason || "fingerprint mismatch")}`
    : `已同步 active 因子 ${activeFactorCount || featureCount}`;
  const projection = model.gui_projection || {};
  const trustState = projection.trust_state || {};
  const processProgress = projection.process_progress || projection.progress_state || {};
  const researchProgress = projection.research_progress || {};
  const candidateRounds = projection.candidate_rounds || {};
  const stopState = projection.stop_state || {};
  const researchSubject = projection.research_subject || {};
  const displayedSessionId = text(displaySession.session_id, "");
  const scopedResearchJournalEvents = displayedSessionId
    ? researchJournalEvents.filter((step) => text(step.session_id, "") === displayedSessionId)
    : researchJournalEvents;
  const rawLlmReviewSignal = modelOrchStatus.llm_review_signal
    || projection.llm_review_signal
    || (projection.research_progress || {}).llm_review_signal
    || serviceOutputs(state.modelResearchCurrent).llm_review_signal
    || serviceOutputs(state.modelResearchJournal).llm_review_signal
    || {};
  const latestJudgment = researchProgress.latest || projection.latest_judgment || {};
  const projectedActiveRound = candidateRounds.current_candidate_round || projection.active_round_view || {};
  const qualityGateSummary = projection.quality_gate_summary || {};
  const seedStability = projection.seed_stability || qualityGateSummary.seed_stability || processProgress.latest_seed_stability || {};
  const seedStabilityAvailable = Boolean(seedStability.available || seedStability.verdict || seedStability.artifact_path);
  const roundEvolution = candidateRounds.comparison_rows || projection.round_evolution || roundTimeline || [];
  const researchTimeline = scopedResearchJournalEvents.length
    ? (researchJournalIsLatestFirst ? [...scopedResearchJournalEvents] : [...scopedResearchJournalEvents].reverse())
    : (displayedSessionId ? [] : (researchProgress.timeline || projection.research_timeline || modelSteps || []));
  const legacyResearchPattern = /forward[_\s-]*test|sota(?:[_\s-]*gate|[_\s-]*score)?|archive_below_threshold|forward_test_reject/i;
  const isLegacyResearchRecord = (record) => {
    if (!record || typeof record !== "object") return false;
    if (record.lifecycle_migration === "dual_mode_research_20260719") return true;
    const metadata = record.metadata && typeof record.metadata === "object" ? record.metadata : {};
    if (metadata.lifecycle_migration === "dual_mode_research_20260719") return true;
    const searchable = JSON.stringify({
      stage: record.stage,
      decision: record.decision,
      next: record.next,
      next_action: record.next_action,
      summary: record.summary,
      evidence_refs: record.evidence_refs,
      extra: record.extra,
    });
    return legacyResearchPattern.test(searchable);
  };
  const currentResearchTimeline = researchTimeline.filter((step) => !isLegacyResearchRecord(step));
  const researchCurrentIsLegacy = isLegacyResearchRecord(researchCurrent)
    || isLegacyResearchRecord(latestJudgment)
    || isLegacyResearchRecord(latestModelStep);
  const llmReviewSignal = isLegacyResearchRecord(rawLlmReviewSignal)
    ? { ...rawLlmReviewSignal, active: false, historical: true }
    : rawLlmReviewSignal;
  const compactDateId = (value) => {
    const raw = text(value, "");
    if (!raw || raw === "--") return "--";
    const match = raw.match(/20\d{6}[_-]\d{6}/);
    if (match) return match[0].replace(/^20\d{2}/, "");
    const fallback = raw.match(/\d{4}[_-]\d{6}/);
    if (fallback) return fallback[0];
    return clip(raw, 22);
  };
  const reviewExtra = llmReviewSignal.source_step?.extra && typeof llmReviewSignal.source_step.extra === "object"
    ? llmReviewSignal.source_step.extra
    : {};
  const isResearchConfirmationFailure = Boolean(
    llmReviewSignal.active
    && llmReviewSignal.stage === "research_confirmation"
    && llmReviewSignal.llm_decision === "failed"
  );
  const reviewTitle = isResearchConfirmationFailure
    ? "为什么没有进入 Rolling"
    : "自动调参建议人工复核";
  const reviewDecisionText = isResearchConfirmationFailure
    ? "这套参数的结果不够稳定，先继续研究"
    : `${text(llmReviewSignal.llm_decision, "review")} -> ${text(llmReviewSignal.llm_next, "human_review")}`;
  const reviewReasonText = isResearchConfirmationFailure
    ? "Seed42 看起来不错，但用同样参数跑 Seed17/83 后，结果差异太大。说明它还不够稳定，所以先不进入 Rolling。"
    : text(llmReviewSignal.reason_summary, "模型建议暂停当前研究并进行人工复核。");
  const reviewExecutionLabel = isResearchConfirmationFailure
    ? "下一步"
    : text(llmReviewSignal.execution_label || llmReviewSignal.execution_decision, "已记录");
  const reviewExecutionMeta = isResearchConfirmationFailure
    ? "继续调参数"
    : [
      llmReviewSignal.round_no ? `Round ${llmReviewSignal.round_no}` : "",
      compactDateId(llmReviewSignal.round_group_id || ""),
    ].filter(Boolean).join(" · ");
  const diagnosticWarnings = [
    ...(trustState.warnings || []),
    ...((projection.diagnostics || {}).warnings || []),
  ].filter(Boolean);
  const uniqueDiagnosticWarnings = [...new Set(diagnosticWarnings)];
  const projectionSource = (value) => Array.isArray(value) ? value.join(" + ") : text(value, "");
  const renderSourcePill = (source) => `<span class="source-pill">${escapeHtml(projectionSource(source || ""))}</span>`;
  const activeFeatureProjection = researchSubject.active_feature_set || model.active_feature_set || {};
  const stageLabel = (status) => ({
    done: "完成",
    running: "进行中",
    paused: "暂停",
    failed: "失败",
    blocked: "阻断",
    waiting: "等待",
  }[status] || text(status, "等待"));
  const metricValue = (round, key) => (round.metrics_brief || {})[key];
  const paramBriefLine = (round) => {
    const params = round.params_brief || {};
    return [
      `lr ${text(params.learning_rate, "--")}`,
      `leaves ${text(params.num_leaves, "--")}`,
      `leaf ${text(params.min_data_in_leaf, "--")}`,
      `L1 ${text(params.lambda_l1, "--")}`,
      `L2 ${text(params.lambda_l2, "--")}`,
      `top${text(params.topk, "--")}/drop${text(params.n_drop, "--")}`,
      text(params.benchmark, ""),
    ].filter(Boolean).join(" · ");
  };
  const metricScaleMax = (key) => Math.max(
    0,
    ...roundEvolution
      .map((round) => Math.abs(Number((round.metrics_brief || {})[key])))
      .filter((value) => Number.isFinite(value))
  );
  const metricScales = {
    excess_annualized_ret_with_cost: metricScaleMax("excess_annualized_ret_with_cost"),
    excess_information_ratio_with_cost: metricScaleMax("excess_information_ratio_with_cost"),
    rank_ic: metricScaleMax("rank_ic"),
    rank_icir: metricScaleMax("rank_icir"),
    max_drawdown: metricScaleMax("max_drawdown"),
  };
  const metricBar = (value, kind = "positive", scale = 0) => {
    const num = Number(value);
    const magnitude = Number.isFinite(num) ? Math.abs(num) : 0;
    const width = magnitude && scale
      ? Math.max(4, Math.min(100, (magnitude / scale) * 100))
      : 0;
    return `<span class="model-metric-bar ${kind}"><i style="width:${width}%"></i></span>`;
  };
  const roundFlag = (round) => [
    round.is_baseline_round ? "基准轮" : "",
    round.is_active ? "当前候选" : "",
    round.is_best_session_round ? "本 session 最佳" : "",
    round.is_latest_round || Number(round.round_no) === Number(candidateRounds.latest_completed_round_no) ? "最近完成" : "",
  ].filter(Boolean).join(" / ");
  const dataSources = processProgress.data_sources || {};
  const dataSourceCards = ["research_current", "research_journal", "session_record", "rounds"].map((key) => {
    const item = dataSources[key] || {};
    return `
      <span class="model-source-chip" title="${escapeHtml(`${text(item.updated_at || item.path, "等待写入")} · ${text(item.meaning, "")}`)}">
        <b>${escapeHtml(key)}</b>
        <small>${escapeHtml(text(item.records, key === "session_record" ? "1" : "0"))}</small>
      </span>
    `;
  }).join("");
  const statusSourceMap = modelOrchStatus.status_source_map || model.status_source_map || [];
  const sourceRecordCount = (count) => {
    if (count === undefined || count === null || count === "") return "--";
    if (typeof count === "object") {
      return Object.entries(count)
        .map(([key, value]) => `${key}:${value}`)
        .join(" · ");
    }
    return String(count);
  };
  const statusSourceCards = (statusSourceMap || []).map((source) => {
    const filter = source.filter && Object.keys(source.filter).length
      ? Object.entries(source.filter)
        .filter(([, value]) => value !== undefined && value !== null && value !== "" && !(Array.isArray(value) && !value.length))
        .map(([key, value]) => `${key}=${Array.isArray(value) ? value.length : value}`)
        .join(" · ")
      : "";
    const flags = [
      source.truth_level,
      source.legacy_view ? "legacy view" : "",
      source.generated_preview ? "preview" : "",
    ].filter(Boolean).join(" · ");
    return `
      <article>
        <span class="detail-label">${escapeHtml(text(source.key, "source"))}</span>
        <strong>${escapeHtml(sourceRecordCount(source.record_count))}</strong>
        <p>${escapeHtml(text(source.gui_role || source.writes, ""))}</p>
        <small>${escapeHtml(text(source.path, ""))}</small>
        ${filter ? `<small>${escapeHtml(filter)}</small>` : ""}
        ${flags ? `<small>${escapeHtml(flags)}</small>` : ""}
      </article>
    `;
  }).join("");
  const stageCardRows = ((processProgress.stage_flow || stageTimeline || []).length ? (processProgress.stage_flow || stageTimeline) : [
    { key: "snapshot", label: "snapshot", status: featureCount ? "done" : "waiting" },
    { key: "session_start", label: "session_start", status: latestSession.session_id ? "done" : "waiting" },
    { key: "hypothesis", label: "hypothesis", status: latestSession.session_id ? "done" : "waiting" },
    { key: "experiment", label: "experiment", status: projectedActiveRound.round_no != null ? "done" : "waiting" },
    { key: "develop", label: "develop", status: latestRun.status ? "done" : "waiting" },
    { key: "run", label: "run", status: latestRun.status ? "done" : "waiting" },
    { key: "validate", label: "validate", status: latestValidation.status ? "done" : "waiting" },
    { key: "feedback", label: "feedback", status: latestJudgment.stage ? "running" : "waiting" },
    { key: "gate", label: "gate", status: latestImportGate.status ? "done" : "waiting" },
    { key: "human_guidance", label: "human guidance", status: (researchProgress.human_guidance || {}).stage ? "running" : "waiting" },
  ]);
  const stageProgressPct = stageCardRows.length
    ? Math.max(0, Math.min(100, (stageCardRows.filter((stage) => {
      const status = text(stage.status, "").toLowerCase();
      return ["done", "completed", "pass", "passed"].includes(status);
    }).length / stageCardRows.length) * 100))
    : 0;
  const latestSummary = researchCurrentIsLegacy
    ? "暂无 2026-07-19 双模式新流程研究记录"
    : (researchGuiBrief.summary || researchGuiBrief.headline || researchProgress.summary || latestJudgment.summary || displayStepText(latestModelStep, "summary", "等待研究判断"));
  const latestDecision = researchCurrentIsLegacy
    ? "旧 SOTA / forward 结论已转入历史区，不作为当前判断"
    : (researchGuiBrief.decision || researchProgress.decision || latestJudgment.decision || displayStepText(latestModelStep, "decision", "暂无决策摘要"));
  const latestNext = researchCurrentIsLegacy
    ? "按研究模式启动首次真实测试"
    : (researchGuiBrief.next_action || researchProgress.next || latestJudgment.next || displayStepText(latestModelStep, "next", "") || processProgress.next_action || nextAction || "等待下一轮 prepare / submit");
  const latestResearchStep = currentResearchTimeline[0] || (researchCurrentIsLegacy ? {} : latestModelStep) || {};
  const latestResearchTransition = latestResearchStep.stage_transition || latestResearchStep.transition || {};
  const weakDecision = /^(continue|submitted|done|ok|暂无决策摘要)$/i.test(String(latestDecision || "").trim());
  const researchSituation = weakDecision ? latestSummary : latestDecision;
  const researchWhy = researchCurrentIsLegacy
    ? "历史记录原样保留，但不再参与 research score、Seed 确认、Rolling Gate 或资产状态判断。"
    : latestResearchTransition.judgment
    || latestResearchTransition.facts
    || latestResearchTransition.history_used
    || latestResearchStep.judgment
    || latestResearchStep.reason
    || latestSummary;
  const researchNextActionText = isResearchConfirmationFailure
    ? "人工决定是否以新的稳定性假设启动下一轮 Research"
    : latestNext;
  const researchEvidenceText = isResearchConfirmationFailure
    ? "最优参数已完成 Seed42 / Seed17 / Seed83 复核"
    : researchWhy;
  const researchConclusionPanel = `
    <section class="model-research-conclusion ${isResearchConfirmationFailure ? "is-review" : ""}">
      <dl>
        <div><dt>本次结果</dt><dd>${escapeHtml(isResearchConfirmationFailure ? "暂不进入 Rolling" : clip(text(researchSituation, "等待研究结论"), 80))}</dd></div>
        <div><dt>原因</dt><dd>${escapeHtml(isResearchConfirmationFailure ? "不同 Seed 的结果差异较大" : clip(text(researchWhy, "等待记录"), 80))}</dd></div>
        <div><dt>下一步</dt><dd>${escapeHtml(isResearchConfirmationFailure ? "继续调参数" : clip(text(researchNextActionText, "等待下一步"), 80))}</dd></div>
      </dl>
    </section>
  `;
  const modelResearchSummary = document.getElementById("model-research-summary");
  if (modelResearchSummary) {
    modelResearchSummary.innerHTML = "";
  }
  const stopBanner = stopState.active ? `
    <section class="model-stop-state">
      <div>
        <span class="detail-label">停止态</span>
        <strong>当前研究已停止</strong>
        <p>${escapeHtml((stopState.reasons || []).join("；") || "当前 session 不满足正式续跑条件。")}</p>
      </div>
      <aside>
        <span class="detail-label">建议动作</span>
        <strong>${escapeHtml(text(stopState.recommended_action, "新开正式 MCP session 或补齐阻断项后重跑。"))}</strong>
      </aside>
    </section>
  ` : "";
  const processParamRows = [
    { key: "session", value: processProgress.session_id || latestSession.session_id, wide: true },
    { key: "policy", value: processProgress.model_policy || researchContract.canonical_policy || "qlib_lgbm_canonical" },
    { key: "frozen", value: processProgress.frozen_feature_set_id || processProgress.feature_set_id || latestSession.feature_set_id },
    { key: "active", value: processProgress.active_feature_set_id || activeFeatureProjection.feature_set_id },
    { key: "active_values", value: activeValuesReadiness.active_values_status || "unknown", compact: true },
    { key: "factors", value: processProgress.factor_count || activeFeatureProjection.factor_count || activeFactorCount, compact: true },
    { key: "round", value: processProgress.current_round || latestSession.current_round, compact: true },
    { key: "run", value: processProgress.latest_model_run_id || projectedActiveRound.model_run_id, wide: true },
  ].map(({ key, value, wide, compact }) => `
    <span class="model-process-chip ${wide ? "is-wide" : ""} ${compact ? "is-compact" : ""}" title="${escapeHtml(`${key}: ${text(value, "暂无")}`)}">
      <b>${escapeHtml(key)}</b>
      <small>${escapeHtml(text(value, "暂无"))}</small>
    </span>
  `).join("");
  const currentRoundMetrics = projectedActiveRound.metrics_brief || {};
  const currentRoundValidation = projectedActiveRound.validation_brief || {};
  const currentRoundGate = projectedActiveRound.gate_brief || {};
  const currentRoundConfigAudit = projectedActiveRound.config_audit_brief || {};
  const currentRoundQualityItems = [
    ...(currentRoundValidation.hard_blocks || []),
    ...(currentRoundValidation.strategy_warnings || []),
    ...(currentRoundValidation.warnings || []),
    ...(currentRoundGate.veto_reasons || []),
  ].filter(Boolean);
  const currentRoundQualityChips = currentRoundQualityItems.slice(0, 5).map((item) => `<span>${escapeHtml(text(item))}</span>`).join("");
  const currentRoundTradability = currentRoundValidation.tradability_exposure || {};
  const currentRoundStRisk = currentRoundTradability.risk_flags || {};
  const currentRoundPredictionSt = currentRoundTradability.prediction || {};
  const currentRoundHoldingsSt = currentRoundTradability.primary_holdings || {};
  const currentRoundStRiskLine = currentRoundTradability.status ? [
    `pred top50 avg ${pct(currentRoundStRisk.top50_st_like_ratio ?? currentRoundPredictionSt.top50_avg_st_like_ratio, 1)}`,
    `p95 ${pct(currentRoundStRisk.top50_p95_st_like_ratio ?? currentRoundPredictionSt.top50_p95_st_like_ratio, 1)}`,
    `latest ${pct(currentRoundStRisk.top50_latest_st_like_ratio ?? currentRoundPredictionSt.top50_latest_st_like_ratio, 1)}`,
    `hold ${pct(currentRoundStRisk.primary_holdings_st_like_weight ?? currentRoundHoldingsSt.st_like_weight, 1)}`,
  ].join(" / ") : "";
  const modelStageName = (stage) => {
    const sharedStage = researchStageMeta(stage);
    if (sharedStage.known) return sharedStage.zh;
    return ({
    protocol_load: "研究协议加载",
    experiment_plan: "参数实验计划",
    train_backtest_seed42: "Seed42 训练与回测",
    round_synthesis: "本轮研究总结",
    research_confirmation: "跨 Seed 稳定性确认",
    context_review: "上下文复核",
    snapshot_review: "快照复核",
    session_start: "会话开始",
    hypothesis_review: "假设复盘",
    experiment_review: "实验复盘",
    develop_review: "开发护栏",
    run_review: "训练回测",
    validate_review: "验证复盘",
    feedback_review: "反馈复盘",
    seed_stability: "Seed 确认",
    seed_stability_diagnostic: "Seed 确认",
    }[stage] || text(stage, "未知阶段"));
  };
  const modelResearchDecisionLabel = (decision) => ({
    failed: "确认未通过",
    continue: "本轮已记录",
    submitted: "计划已提交",
    completed: "已完成",
    blocked: "已阻断",
  }[text(decision, "").toLowerCase()] || "已记录");
  const modelResearchNextLabel = (next) => ({
    human_review: "人工决定是否发起下一轮研究",
    experiment_plan: "进入下一轮参数实验",
    train_backtest_seed42: "执行 Seed42 训练与回测",
    research_confirmation: "执行优胜轮跨 Seed 确认",
  }[text(next, "").toLowerCase()] || text(next, "等待下一步"));
  const modelStageSeqLabel = (seq) => {
    const num = Number(seq);
    return Number.isFinite(num) && num > 0 ? `阶段 ${num}` : "阶段 --";
  };
  const sessionExecutionEvents = [...currentResearchTimeline];
  const sessionExecutionRows = sessionExecutionEvents.map((step, index) => {
    const legacyRecord = isLegacyResearchRecord(step);
    const stepSummary = displayStepText(step, "summary", "暂无摘要");
    const stepDecision = modelResearchDecisionLabel(displayStepText(step, "decision", ""));
    const stepNext = step.next_action || displayStepText(step, "next", "");
    return `
      <li class="${index === 0 ? "is-latest" : ""}">
        <div class="model-session-event-head">
          <time>${escapeHtml(compactDateTime(step.ts || step.created_at) || "时间未记录")}</time>
          <strong>${escapeHtml(legacyRecord ? `历史旧流程 · ${modelStageName(step.stage)}` : modelStageName(step.stage))}</strong>
          ${step.round_no != null ? `<small class="model-session-event-round">Round ${escapeHtml(text(step.round_no))}</small>` : ""}
        </div>
        <p>${escapeHtml(text(stepSummary || "暂无摘要"))}</p>
        <details class="model-session-event-details">
          <summary>处理结果</summary>
          <small class="model-session-event-outcome">${escapeHtml(stepDecision)}${stepNext ? `；下一步：${escapeHtml(modelResearchNextLabel(stepNext))}` : ""}</small>
        </details>
      </li>
    `;
  }).join("");
  const roundComparisonRows = roundEvolution.map((round) => {
    const metrics = round.metrics_brief || {};
    const validation = round.validation_brief || {};
    const gate = round.gate_brief || {};
    const audit = round.config_audit_brief || {};
    const flags = roundFlag(round);
    return `
      <tr class="${round.is_active ? "active-row" : ""} ${round.is_best_session_round ? "best-row" : ""}">
        <td><strong>${escapeHtml(text(round.round_label || `R${round.round_no ?? "--"}`))}</strong>${flags ? `<small>${escapeHtml(flags)}</small>` : ""}</td>
        <td><span class="badge subtle">${escapeHtml(text(round.stage, "waiting"))}</span></td>
        <td class="wide-cell"><span class="cell-clamp">${escapeHtml(clip(round.hypothesis || round.experiment_key || "等待假设", 160))}</span></td>
        <td class="wide-cell"><span class="cell-clamp">${escapeHtml(paramBriefLine(round))}</span></td>
        <td>${pct(metrics.excess_annualized_ret_with_cost, 1)}${metricBar(metrics.excess_annualized_ret_with_cost, "positive", metricScales.excess_annualized_ret_with_cost)}</td>
        <td>${shortNumber(metrics.excess_information_ratio_with_cost, 3)}${metricBar(metrics.excess_information_ratio_with_cost, "positive", metricScales.excess_information_ratio_with_cost)}</td>
        <td>${shortNumber(metrics.rank_ic, 4)}${metricBar(metrics.rank_ic, "positive", metricScales.rank_ic)}</td>
        <td>${shortNumber(metrics.rank_icir, 3)}${metricBar(metrics.rank_icir, "positive", metricScales.rank_icir)}</td>
        <td>${pct(metrics.max_drawdown, 1)}${metricBar(metrics.max_drawdown, "drawdown", metricScales.max_drawdown)}</td>
        <td>${escapeHtml(text(validation.status, "unknown"))}</td>
        <td>${escapeHtml(text(gate.status, "pending"))}</td>
        <td><span class="cell-clamp compact">${escapeHtml(audit.passed === true ? "Config 通过" : audit.passed === false ? "Config 失败" : clip(round.feedback_brief || "等待反馈", 120))}</span></td>
      </tr>
    `;
  }).join("");
  const candidateTrend = roundEvolution.map((round) => {
    const metrics = round.metrics_brief || {};
    return `
      <article class="${round.is_active ? "is-active" : ""} ${round.is_best_session_round ? "is-best" : ""}">
        <strong>${escapeHtml(text(round.round_label || `R${round.round_no ?? "--"}`))}</strong>
        <span>${metricBar(metrics.excess_annualized_ret_with_cost, "positive", metricScales.excess_annualized_ret_with_cost)}<small>年化 ${pct(metrics.excess_annualized_ret_with_cost, 1)}</small></span>
        <span>${metricBar(metrics.excess_information_ratio_with_cost, "positive", metricScales.excess_information_ratio_with_cost)}<small>IR ${shortNumber(metrics.excess_information_ratio_with_cost, 3)}</small></span>
        <span>${metricBar(metrics.max_drawdown, "drawdown", metricScales.max_drawdown)}<small>回撤 ${pct(metrics.max_drawdown, 1)}</small></span>
      </article>
    `;
  }).join("");
  const activeRoundEvidenceRefs = projectedActiveRound.evidence_refs || [];
  const productionModel = productionStatus.production_model || productionModels[0] || model.production_model || {};
  const encodingWarnings = researchLive.encoding_warnings || [];
  const latestRunError = researchGuiBrief.root_cause || modelRunErrorSummary(latestRun.run_error || "") || "";
  const seedComparisonList = Array.isArray(seedStability.comparison_rows) ? seedStability.comparison_rows : [];
  const modelLogSessions = Array.isArray(modelOrchStatus.sessions) ? modelOrchStatus.sessions : [];
  const modelLogStatusLabel = (status) => ({
    completed: "已完成",
    interrupted: "研究已停止",
    running: "运行中",
    queued: "排队中",
    failed: "执行失败",
  }[text(status, "").toLowerCase()] || text(status, "未知"));
  const defaultModelLogSession = modelOrchStatus.active_session || modelOrchStatus.latest_session || modelLogSessions[0] || {};
  if (!modelLogSessions.some((session) => session.session_id === state.activeModelLogSessionId)) {
    state.activeModelLogSessionId = defaultModelLogSession.session_id || "";
  }
  const modelLogSession = modelLogSessions.find((session) => session.session_id === state.activeModelLogSessionId)
    || defaultModelLogSession;
  const modelLogSessionId = modelLogSession.session_id || "";
  const modelLogRoundIds = new Set((modelLogSession.round_group_ids || []).filter(Boolean));
  const modelLogJobs = Array.isArray(modelOrchStatus.jobs) ? modelOrchStatus.jobs : [];
  const modelOrchJob = modelLogJobs.find((job) => job.job_id === modelLogSession.active_job_id)
    || modelOrchStatus.active_job
    || {};
  const modelOrchRunId = modelOrchJob.job_id || modelLogSession.active_job_id || "";
  const allModelOrchTraces = serviceOutputs(state.modelResearchOrchTraces).traces
    || serviceOutputs(state.modelOrchestratorTraces).traces
    || [];
  const allModelOrchEvents = [
    ...(serviceOutputs(state.modelOrchestratorEvents).events || []),
    ...(modelOrchStatus.events_tail || []),
  ];
  const allModelMcpTraces = serviceOutputs(state.modelResearchMcpTraces).traces || [];
  const belongsToModelLogSession = (row) => !modelLogSessionId || (
    row.session_id === modelLogSessionId
    || (row.round_group_id && modelLogRoundIds.has(row.round_group_id))
    || (row.job_id && row.job_id === modelOrchRunId)
    || (row.run_id && row.run_id === modelOrchRunId)
  );
  const modelOrchTraceRows = dedupeModelRows(allModelOrchTraces.filter(belongsToModelLogSession))
    .sort((a, b) => traceTsValue(b) - traceTsValue(a));
  const modelOrchEventRows = dedupeModelRows(allModelOrchEvents.filter(belongsToModelLogSession))
    .sort((a, b) => traceTsValue(b) - traceTsValue(a));
  const modelMcpTraceRows = dedupeModelRows(allModelMcpTraces.filter(belongsToModelLogSession))
    .sort((a, b) => traceTsValue(b) - traceTsValue(a));
  const modelContextOutputs = serviceOutputs(state.modelCurrentContext);
  const modelContextSummary = modelContextOutputs.current_context_summary
    || modelOrchStatus.current_context_summary
    || (model.orchestrator || {}).current_context_summary
    || {};
  const latestOrchEvent = modelOrchEventRows[0] || {};
  const modelOrchStartedEvent = modelOrchEventRows.find((event) => event.event_type === "orchestrator_started" && event.inputs) || {};
  const modelOrchInputs = modelOrchJob.inputs || modelOrchStartedEvent.inputs || {};
  const latestOrchResultTrace = modelOrchTraceRows.find((trace) => trace.event_type === "llm_result") || {};
  const modelOrchErrors = modelOrchEventRows.filter((event) => event.error || /error|blocker/i.test(String(event.event_type || "")));
  const modelOrchStatusLabel = modelLogSession.status || modelOrchJob.status || latestOrchEvent.status || "historical";
  const modelOrchStageLabel = modelLogSession.current_stage || modelOrchJob.stage || latestOrchResultTrace.stage || latestOrchEvent.stage || "historical";
  const modelLegacyTraceCount = modelOrchTraceRows.filter((trace) => trace.legacy_trace).length;
  const modelLogCompletedRounds = modelLogSession.payload?.completed_rounds || [];
  const modelOrchEventTableRows = modelOrchEventRows.slice(0, 120).map((event) => `
    <tr class="${event.error ? "danger-row" : ""}">
      <td><strong>${escapeHtml(text(event.stage || event.event_type, ""))}</strong><small>${escapeHtml(compactDateTime(event.ts))}</small></td>
      <td><span class="badge subtle">${escapeHtml(text(event.event_type, ""))}</span><small>${escapeHtml(text(event.tool_name || event.status || "", ""))}</small></td>
      <td class="wide-cell"><span class="cell-clamp">${escapeHtml(text(event.reason || event.error || event.trace_id || event.event_id, ""))}</span></td>
    </tr>
  `).join("");
  const latestContextTrace = [...modelMcpTraceRows, ...modelOrchTraceRows].find((trace) =>
    trace.context_pack || trace.context_snapshot || trace.event_type === "context_snapshot"
  ) || {};
  const contextSnapshotPayload = latestContextTrace.context_pack
    || latestContextTrace.context_snapshot
    || modelContextSummary
    || {};
  const modelTraceGroupKey = (trace) => [
    trace.job_id || trace.run_id || modelOrchRunId || "job",
    trace.session_id || modelOrchestratorSessionId() || "session",
    trace.round_no ?? trace.round ?? "",
    trace.round_group_id || "",
    trace.stage || "trace",
  ].join("|");
  const groupedModelTraces = (() => {
    const groups = new Map();
    modelOrchTraceRows.forEach((trace) => {
      const key = modelTraceGroupKey(trace);
      const group = groups.get(key) || {
        groupId: key,
        traces: [],
        stage: trace.stage || "trace",
        roundNo: trace.round_no ?? trace.round ?? "",
        roundGroupId: trace.round_group_id || "",
        jobId: trace.job_id || trace.run_id || "",
        sessionId: trace.session_id || "",
        latestTs: 0,
      };
      group.traces.push(trace);
      group.latestTs = Math.max(group.latestTs, traceTsValue(trace));
      group.stage = group.stage || trace.stage || "trace";
      groups.set(key, group);
    });
    return [...groups.values()].map((group) => {
      const sorted = [...group.traces].sort((a, b) => traceTsValue(a) - traceTsValue(b));
      const requestTrace = sorted.find((trace) => trace.event_type === "llm_request") || null;
      const resultTrace = [...sorted].reverse().find((trace) => trace.event_type === "llm_result" || trace.parsed_response || trace.result_summary) || null;
      const primaryTrace = resultTrace || requestTrace || sorted[sorted.length - 1] || {};
      return {
        ...group,
        traces: sorted,
        requestTrace,
        resultTrace,
        primaryTrace,
        eventTypes: [...new Set(sorted.map((trace) => trace.event_type).filter(Boolean))],
        hasError: sorted.some((trace) => trace.error || /error|failed|block/i.test(String(trace.event_type || ""))),
      };
    }).sort((a, b) => b.latestTs - a.latestTs);
  })();
  const modelLlmTraceGroups = groupedModelTraces.filter((group) => {
    const result = group.resultTrace?.result_summary || group.resultTrace?.parsed_response || {};
    return group.stage === "experiment_plan" || result.llm_call_status === "called";
  });
  const defaultModelTraceGroup = modelLlmTraceGroups.find((group) => group.resultTrace) || modelLlmTraceGroups[0] || {};
  const selectedModelTraceGroup = modelLlmTraceGroups.find((group) => group.groupId === state.activeModelOrchTraceId) || defaultModelTraceGroup;
  state.activeModelOrchTraceId = selectedModelTraceGroup.groupId || "";
  const selectedModelRequestTrace = selectedModelTraceGroup.requestTrace || {};
  const selectedModelResultTrace = selectedModelTraceGroup.resultTrace || selectedModelTraceGroup.primaryTrace || {};
  const selectedModelParsedResponse = selectedModelResultTrace.parsed_response || selectedModelResultTrace.result_summary || selectedModelResultTrace.result || {};
  const selectedModelContextPack = selectedModelRequestTrace.context_pack
    || selectedModelResultTrace.context_pack
    || latestContextTrace.context_pack
    || contextSnapshotPayload
    || {};
  const selectedModelHypothesis = text(
    selectedModelParsedResponse.experiment_hypothesis
      || selectedModelParsedResponse.hypothesis
      || selectedModelParsedResponse.summary
      || selectedModelParsedResponse.judgment
      || selectedModelResultTrace.raw_response_preview,
    "暂无返回摘要",
  );
  const selectedModelEvidence = selectedModelParsedResponse.evidence_interpretation
    || selectedModelParsedResponse.parameter_change_rationale
    || selectedModelParsedResponse.why
    || selectedModelParsedResponse.reason
    || selectedModelParsedResponse.rationale
    || "--";
  const selectedModelEvidenceText = typeof selectedModelEvidence === "string"
    ? selectedModelEvidence
    : JSON.stringify(selectedModelEvidence);
  const selectedModelStageBriefing = selectedModelRequestTrace.stage_briefing
    || selectedModelResultTrace.stage_briefing
    || selectedModelRequestTrace.user_prompt
    || "暂无任务指令";
  const selectedModelOutputContract = selectedModelRequestTrace.output_contract || selectedModelResultTrace.output_contract || {};
  const selectedModelResearchEvidence = selectedModelContextPack.research_evidence || {};
  const selectedModelParameterChanges = Array.isArray(selectedModelParsedResponse.parameter_changes)
    ? selectedModelParsedResponse.parameter_changes
    : [];
  const selectedModelRisks = Array.isArray(selectedModelParsedResponse.risks_to_watch)
    ? selectedModelParsedResponse.risks_to_watch
    : [];
  const selectedModelRoundReceipt = modelLogCompletedRounds.find((round) => (
    round.round_group_id === selectedModelTraceGroup.roundGroupId
    || Number(round.round_no) === Number(selectedModelTraceGroup.roundNo)
  )) || {};
  const modelTraceListRows = modelLlmTraceGroups.slice(0, 60).map((group) => {
    const response = group.resultTrace?.parsed_response || group.resultTrace?.result_summary || {};
    const active = group.groupId === selectedModelTraceGroup.groupId;
    const title = [
      group.roundNo ? `R${group.roundNo}` : "",
      group.stage === "experiment_plan" ? "大模型参数提案" : group.stage || "交互",
    ].filter(Boolean).join(" · ");
    const summary = response.summary || response.judgment || response.decision || group.primaryTrace?.raw_response_preview || group.eventTypes.join(" / ");
    return `
      <button class="orch-trace-item${active ? " active" : ""}${group.hasError ? " danger" : group.resultTrace ? " ok" : ""}" type="button" data-model-orch-trace-id="${escapeHtml(group.groupId)}">
        <span>${escapeHtml(title || "--")}</span>
        <strong>${escapeHtml(clip(summary || "等待返回", 96))}</strong>
        <small>${escapeHtml(compactDateTime(group.primaryTrace?.ts))} · ${escapeHtml(text(response.llm_model || group.primaryTrace?.llm_model || "DeepSeek", "DeepSeek"))}</small>
        <span class="orch-trace-item-badges">
          ${group.eventTypes.map((item) => `<i>${escapeHtml(item)}</i>`).join("")}
          ${group.traces.length > 1 ? `<i>${escapeHtml(`${group.traces.length} rows`)}</i>` : ""}
        </span>
      </button>
    `;
  }).join("");
  const supportedModelLogViews = new Set(["interaction", "receipts", "evidence"]);
  if (!supportedModelLogViews.has(state.activeModelLogView)) state.activeModelLogView = "interaction";
  const modelLogSessionOptions = modelLogSessions.map((session) => {
    const featureSet = displayModelIdentifier(session.feature_set_id || "未标注 Feature Set");
    const rounds = Number(session.n_rounds_completed || session.payload?.completed_rounds?.length || 0);
    return `<option value="${escapeHtml(session.session_id || "")}"${session.session_id === modelLogSessionId ? " selected" : ""}>${escapeHtml(`${featureSet} · ${rounds} 轮 · ${modelLogStatusLabel(session.status)} · ${compactDateTime(session.updated_at || session.created_at)}`)}</option>`;
  }).join("");
  const scopedResearchJournal = researchJournalEvents.filter((entry) => (
    entry.session_id === modelLogSessionId
    || (entry.round_group_id && modelLogRoundIds.has(entry.round_group_id))
  ));
  const modelLogJournalCards = scopedResearchJournal.slice(0, 80).map((entry) => `
    <article class="model-log-decision-card">
      <div><span>${escapeHtml(text(entry.stage, "research"))}</span><small>${escapeHtml(compactDateTime(entry.ts))}</small></div>
      <strong>${escapeHtml(text(entry.decision, "recorded"))}</strong>
      <p>${escapeHtml(text(entry.summary, "暂无摘要"))}</p>
      <footer><span>${escapeHtml(entry.round_no !== undefined ? `Round ${entry.round_no}` : "会话级")}</span><span>${escapeHtml(text(entry.round_group_id, "--"))}</span></footer>
    </article>
  `).join("");
  const modelLogViewButtons = [
    ["interaction", "大模型交互"],
    ["receipts", "执行回执"],
    ["evidence", "原始载荷"],
  ].map(([value, label]) => `<button type="button" data-model-log-view="${value}" class="${state.activeModelLogView === value ? "active" : ""}">${label}</button>`).join("");
  const selectedModelParameterChangeCards = selectedModelParameterChanges.map((change) => `
    <article class="model-log-parameter-change">
      <span>${escapeHtml(text(change.parameter, "参数"))}</span>
      <strong>${escapeHtml(text(change.from, "--"))} <i>→</i> ${escapeHtml(text(change.to, "--"))}</strong>
      <p>${escapeHtml(text(change.reason, "未记录变更理由"))}</p>
    </article>
  `).join("");
  const selectedModelRiskItems = selectedModelRisks.map((risk) => `<li>${escapeHtml(text(risk, ""))}</li>`).join("");
  const selectedModelReceiptDecision = selectedModelRoundReceipt.round_synthesis_decision
    || scopedResearchJournal.find((entry) => entry.round_group_id === selectedModelRoundReceipt.round_group_id && entry.stage === "round_synthesis")?.decision
    || "等待执行回执";
  const modelLogInteractionView = `
    <section class="model-log-view-panel model-log-interaction-view">
      <div class="model-log-view-head"><div><span class="detail-label">LLM CONVERSATION AUDIT</span><h3>大模型交互详情</h3></div><small>左侧选择一次参数提案，右侧查看完整输入、响应与采用结果</small></div>
      <div class="orch-trace-layout model-log-trace-layout model-log-conversation-layout">
        <aside class="orch-trace-list model-log-trace-list" aria-label="大模型交互记录">${modelTraceListRows || `<div class="empty-state">该会话没有真实大模型调用。</div>`}</aside>
        <section class="model-log-conversation-detail">
          <header class="model-log-conversation-head">
            <div><span class="detail-label">${escapeHtml(selectedModelTraceGroup.roundNo !== "" ? `ROUND ${selectedModelTraceGroup.roundNo}` : "MODEL CALL")}</span><h3>${escapeHtml(text(selectedModelParsedResponse.decision, "等待响应"))}</h3><small>${escapeHtml(compactDateTime(selectedModelResultTrace.ts || selectedModelRequestTrace.ts))} · ${escapeHtml(text(selectedModelParsedResponse.llm_provider_model || selectedModelParsedResponse.llm_model || "DeepSeek"))}</small></div>
            <div><span class="badge ${selectedModelResultTrace.schema_status === "current" ? "ok" : "subtle"}">${escapeHtml(text(selectedModelResultTrace.schema_status, "recorded"))}</span><small>${escapeHtml(text(selectedModelParsedResponse.next_move || selectedModelParsedResponse.next, "--"))}</small></div>
          </header>
          <section class="model-log-message model-log-message-input">
            <div class="model-log-message-role"><span>INPUT</span><strong>平台发送给大模型</strong></div>
            <article><span>本轮任务</span><p>${escapeHtml(text(selectedModelStageBriefing, "暂无任务指令"))}</p></article>
            <div class="model-log-context-facts">
              <span><b>${escapeHtml(text((selectedModelResearchEvidence.recent_rounds || []).length, "0"))}</b><small>参考轮次</small></span>
              <span><b>${escapeHtml(text((selectedModelResearchEvidence.parameter_ledger || []).length, "0"))}</b><small>参数账本</small></span>
              <span><b>${escapeHtml(text((selectedModelResearchEvidence.cross_feature_references || []).length, "0"))}</b><small>跨 Feature Set 参考</small></span>
              <span><b>${Object.keys(selectedModelContextPack.correction || {}).length ? "有" : "无"}</b><small>服务端纠错</small></span>
            </div>
            <details class="model-log-inline-raw"><summary>查看 System Prompt 与输出契约</summary><div>${renderTraceJsonBlock("System Prompt", selectedModelRequestTrace.system_prompt || selectedModelResultTrace.system_prompt, { open: true })}${renderTraceJsonBlock("Output Contract", selectedModelOutputContract, { open: true })}</div></details>
          </section>
          <section class="model-log-message model-log-message-output">
            <div class="model-log-message-role"><span>OUTPUT</span><strong>大模型返回</strong></div>
            <div class="model-log-response-grid"><article><span>可检验假设</span><p>${escapeHtml(selectedModelHypothesis)}</p></article><article><span>证据解释</span><p>${escapeHtml(selectedModelEvidenceText)}</p></article></div>
            <div class="model-log-parameter-changes">${selectedModelParameterChangeCards || `<div class="empty-state">本次响应没有参数变更。</div>`}</div>
            ${selectedModelRiskItems ? `<article class="model-log-risk-list"><span>模型提示的风险</span><ul>${selectedModelRiskItems}</ul></article>` : ""}
          </section>
          <section class="model-log-message model-log-message-receipt">
            <div class="model-log-message-role"><span>RECEIPT</span><strong>平台采用与执行</strong></div>
            <div class="model-log-receipt-row"><span><small>参数组</small><strong>${escapeHtml(text(selectedModelParsedResponse.parameter_group, "--"))}</strong></span><span><small>采用状态</small><strong>${escapeHtml(text(selectedModelParsedResponse.decision, "--"))}</strong></span><span><small>后续回执</small><strong>${escapeHtml(selectedModelReceiptDecision)}</strong></span></div>
            <small>${escapeHtml(text(selectedModelRoundReceipt.round_group_id || selectedModelTraceGroup.roundGroupId, "尚无 round_group_id"))}</small>
          </section>
        </section>
      </div>
    </section>
  `;
  const modelLogReceiptsView = `
    <section class="model-log-view-panel">
      <div class="model-log-view-head"><div><span class="detail-label">EXECUTION RECEIPTS</span><h3>模型建议后的执行记录</h3></div><small>这里只记录建议是否提交、平台如何执行和是否触发阻断</small></div>
      <div class="model-log-source-summary"><span><b>${modelLlmTraceGroups.length}</b><small>真实模型调用</small></span><span><b>${scopedResearchJournal.length}</b><small>研究回执</small></span><span><b>${modelOrchEventRows.length}</b><small>后台事件</small></span><span><b>${modelOrchErrors.length + modelLegacyTraceCount}</b><small>异常 / legacy</small></span></div>
      <div class="model-log-decision-list">${modelLogJournalCards || `<div class="empty-state">该会话没有执行回执。</div>`}</div>
      <div class="table-shell compact-table model-orch-scroll-table compact"><table class="data-table model-orch-event-table"><thead><tr><th>Stage</th><th>事件</th><th>说明</th></tr></thead><tbody>${modelOrchEventTableRows || `<tr><td colspan="3">暂无后台事件。</td></tr>`}</tbody></table></div>
    </section>
  `;
  const modelLogRawView = `
    <section class="model-log-view-panel">
      <div class="model-log-view-head"><div><span class="detail-label">RAW PAYLOAD</span><h3>所选交互的原始载荷</h3></div><small>用于复盘 Prompt、上下文裁剪、JSON 契约和模型原始响应</small></div>
      <div class="model-log-payload-grid">
        ${renderTraceJsonBlock("Stage Briefing", selectedModelStageBriefing, { open: true })}
        ${renderTraceJsonBlock("System Prompt", selectedModelRequestTrace.system_prompt || selectedModelResultTrace.system_prompt, { open: false })}
        ${renderTraceJsonBlock("Context Pack", selectedModelContextPack, { open: true })}
        ${renderTraceJsonBlock("Output Contract", selectedModelOutputContract, { open: false })}
        ${renderTraceJsonBlock("Parsed Response", selectedModelParsedResponse, { open: true })}
        ${renderTraceJsonBlock("Raw Trace Rows", selectedModelTraceGroup.traces || [], { open: false })}
      </div>
    </section>
  `;
  const modelLogActiveContent = {
    interaction: modelLogInteractionView,
    receipts: modelLogReceiptsView,
    evidence: modelLogRawView,
  }[state.activeModelLogView];
  const modelOrchTraceWorkspace = `
    <section class="model-orch-trace-page model-log-workspace-v2 model-log-audit-workspace">
      <div class="model-log-session-bar">
        <label><span>研究会话</span><select id="model-log-session-select">${modelLogSessionOptions || `<option value="">暂无研究会话</option>`}</select></label>
        <div><span class="badge ${modelOrchStatusLabel === "completed" ? "ok" : "subtle"}">${escapeHtml(modelLogStatusLabel(modelOrchStatusLabel))}</span><strong>${escapeHtml(displayModelIdentifier(modelLogSession.feature_set_id || "未选择 Feature Set"))}</strong><small>${escapeHtml(`${modelLlmTraceGroups.length} 次真实大模型调用`)} · ${escapeHtml(compactDateTime(modelLogSession.updated_at || modelLogSession.created_at))}</small></div>
      </div>
      <nav class="model-log-view-switch model-log-audit-tabs" aria-label="研究日志视图">${modelLogViewButtons}</nav>
      ${modelLogActiveContent}
    </section>
  `;
  const selectedRoundIds = new Set(Array.isArray(displaySession.round_group_ids)
    ? displaySession.round_group_ids.map((roundId) => text(roundId, "")).filter(Boolean)
    : []);
  const reportedSessionRounds = modelOrchStatus.session_rounds || model.session_rounds || [];
  const sessionRounds = [
    ...(Array.isArray(reportedSessionRounds) ? reportedSessionRounds : []),
    ...(Array.isArray(roundTimeline) ? roundTimeline.filter((round) => (
      !selectedRoundIds.size || selectedRoundIds.has(text(round.round_group_id, ""))
    )) : []),
  ].filter((round, index, rounds) => {
    const roundId = text(round.round_group_id || round.model_run_id, "");
    return !roundId || rounds.findIndex((candidate) => (
      text(candidate.round_group_id || candidate.model_run_id, "") === roundId
    )) === index;
  });
  const featureSets = serviceOutputs(state.modelFeatureSets);
  const featureSetItems = featureSets.items || featureSets.feature_sets || featureSets.catalog || [];
  const latestFeatureSet = featureSetItems[0] || {};
  const preflightErrors = modelPreflight.errors || [];
  const preflightWarnings = modelPreflight.warnings || [];
  const label0Contract = modelPreflight.label0_contract || {};
  const contractRoundPool = [
    ...(Array.isArray(sessionRounds) ? sessionRounds : []),
    projectedActiveRound,
    ...(Array.isArray(roundTimeline) ? roundTimeline : []),
  ].filter((round) => round && typeof round === "object" && Object.keys(round).length);
  const latestRoundForContract = contractRoundPool.find((round) => (
    round.experiment || round.experiment_plan || round.submitted_payload
  )) || contractRoundPool[0] || {};
  const latestExperimentPlan = latestRoundForContract.experiment
    || latestRoundForContract.experiment_plan
    || latestRoundForContract.submitted_payload
    || modelContextSummary.experiment_plan
    || {};
  const latestModelParams = latestExperimentPlan.model_params
    || latestExperimentPlan.qlib_model_kwargs
    || latestExperimentPlan.lgbm_params
    || latestExperimentPlan.params
    || latestRoundForContract.params_brief
    || {};
  const compactRange = (value) => {
    if (Array.isArray(value)) return value.map((item) => text(item, "")).filter(Boolean).join(" -> ");
    if (value && typeof value === "object") {
      return [value.start, value.begin, value.from, value.end, value.to]
        .map((item) => text(item, ""))
        .filter(Boolean)
        .join(" -> ");
    }
    return text(value, "--");
  };
  const experimentSegments = latestExperimentPlan.segments
    || latestExperimentPlan.dataset_segments
    || latestExperimentPlan.windows
    || {};
  const portfolioText = (value, fallback) => {
    if (!value) return fallback;
    if (typeof value === "string") return value;
    const topk = value.topk ?? value.top_k ?? value.top;
    const drop = value.n_drop ?? value.drop ?? value.drop_n;
    const hold = value.hold_thresh ?? value.hold ?? value.holding_period_days;
    return [`top${text(topk, "--")}`, `drop${text(drop, "--")}`, `hold${text(hold, "--")}`].join("/");
  };
  const experimentParamLine = (experiment = {}, round = {}) => {
    const params = experiment.model_params || experiment.qlib_model_kwargs || experiment.lgbm_params || experiment.params || round.params_brief || {};
    return [
      `lr ${text(params.learning_rate ?? params.lr, "--")}`,
      `leaves ${text(params.num_leaves, "--")}`,
      `leaf ${text(params.min_data_in_leaf ?? params.min_child_samples, "--")}`,
      `L1 ${text(params.lambda_l1 ?? params.reg_alpha, "--")}`,
      `L2 ${text(params.lambda_l2 ?? params.reg_lambda, "--")}`,
      text(experiment.sample_weight_policy || params.sample_weight_policy || round.sample_weight_policy, ""),
    ].filter(Boolean).join(" · ");
  };
  const modelResearchMode = (displayJob.mode || displaySession.mode || modelContextSummary.mode || "").toLowerCase() === "mcp"
    ? "Codex MCP"
    : "Orchestrator · DeepSeek v4 Flash";
  const trainingWindowText = [
    `train ${compactRange(experimentSegments.train || latestExperimentPlan.train_window)}`,
    `valid ${compactRange(experimentSegments.valid || experimentSegments.validation || latestExperimentPlan.valid_window)}`,
    `test ${compactRange(experimentSegments.test || latestExperimentPlan.test_window)}`,
  ].join(" · ");
  const label0Text = label0Contract.label_name
    ? `${text(label0Contract.label_name)} · ${text(label0Contract.forward_window || label0Contract.forward_period || "T+1 -> T+6")} · ${text(label0Contract.execution_deal_price || "open")}`
    : "LABEL0 · T+1 -> T+6 · open";
  const currentBlocker = activeSession.current_blocker || activeJob.current_blocker || (modelOrchStatus.session_blockers || [])[0] || modelPreflight.blocker || {};
  const jobStatusRaw = text(displayJob.status || modelOrchStatusLabel || displaySession.status || processProgress.status || "", "").toLowerCase();
  const activeJobStatus = text(activeJob.status, "").toLowerCase();
  const isTrainingActive = Boolean(activeJob.thread_alive || ["queued", "running"].includes(activeJobStatus));
  const isBlocked = Boolean(currentBlocker.code || currentBlocker.category || /block|failed|error/i.test(jobStatusRaw));
  const hasCompletedRunHistory = contractRoundPool.some((round) => {
    const status = text(round.status || round.stage, "").toLowerCase();
    return ["completed", "research_confirmation", "rolling_preliminary"].includes(status)
      || Boolean(round.model_run_id || round.seed_runs?.length || round.seed_models?.length);
  });
  const isTerminated = !isTrainingActive && (
    ["completed", "failed", "cancelled", "canceled", "interrupted"].includes(jobStatusRaw)
    || hasCompletedRunHistory
  );
  const runtimeState = isBlocked
    ? { label: "阻断", tone: "blocked", detail: currentBlocker.human_message || currentBlocker.repair_action || activeJob.reason || "需要人工处理后恢复。" }
    : isTrainingActive
      ? { label: "运行中", tone: "active", detail: activeJob.stage || modelOrchStageLabel || "后台 job 正在执行。" }
      : isTerminated
        ? { label: "已结束", tone: "ended", detail: "当前没有训练线程运行，页面展示的是最近一次 session 的结果。" }
        : { label: "等待训练", tone: "waiting", detail: "当前没有活动训练 job。" };
  const allSessionSeedRuns = (sessionRounds.length ? sessionRounds : roundTimeline)
    .flatMap((round) => {
      const direct = round.seed_runs || round.seed_models || round.seeds || [];
      return Array.isArray(direct) && direct.length ? direct : [round];
    });
  const completedSeedRuns = allSessionSeedRuns.filter((seed) => {
    const status = text(seed.status || seed.train_status || seed.registry_status || seed.status_in_registry || seed.metadata?.asset_status || "", "").toLowerCase();
    return Boolean(seed.model_run_id || seed.display_model_id) && !["queued", "running", "pending", "waiting"].includes(status);
  }).length;
  const requestedRoundCount = Number(displaySession.n_rounds_requested || modelOrchInputs.n_rounds || processProgress.n_rounds_requested || processProgress.target_rounds || 0);
  const completedRoundCount = Number(displaySession.n_rounds_completed || candidateRounds.latest_completed_round_no || processProgress.n_rounds_completed || processProgress.completed_rounds || 0);
  // Research is staged: each round starts with Seed 42 and only the winning
  // round receives the two additional confirmation seeds (17 and 83).
  const targetSeedRuns = requestedRoundCount > 0
    ? requestedRoundCount + 2
    : Number(displaySession.n_seed_runs_requested || modelOrchInputs.n_seed_runs || 0);
  const trainingProgressPct = targetSeedRuns > 0
    ? Math.max(0, Math.min(100, (completedSeedRuns / targetSeedRuns) * 100))
    : 0;
  const showTrainingProgress = Boolean(isTrainingActive && targetSeedRuns > 0);
  const visibleTrainingProgressPct = showTrainingProgress ? trainingProgressPct : 0;
  const progressMainText = showTrainingProgress
    ? `${completedSeedRuns} / ${targetSeedRuns}`
    : (isTerminated ? "无活动训练" : (targetSeedRuns > 0 ? `计划 ${targetSeedRuns}` : "等待启动"));
  const progressSubText = showTrainingProgress
    ? "模型训练 run"
    : (isTerminated ? "最近 session 已结束" : "当前未启动 run");
  const seedFromRunId = (value) => {
    const match = text(value, "").match(/[_-]s(\d{1,3})(?:[_-]|$)/i);
    return match ? match[1] : "";
  };
  const compactRunLabel = (value) => {
    const seed = seedFromRunId(value);
    const compact = compactDateId(value);
    return seed ? `S${seed} · ${compact}` : compact;
  };
  const compactLongId = (value) => {
    const raw = text(value, "");
    if (!raw || raw === "--") return "--";
    const date = compactDateId(raw);
    if (date !== "--" && date !== clip(raw, 22)) return date;
    return clip(raw.replace(/^legacy_job:/, ""), 24);
  };
  const liveObjectLabel = isTrainingActive ? "当前" : "最近";
  const roundProgressText = isTrainingActive
    ? `${text(completedRoundCount || "--")} / ${text(requestedRoundCount || "--")}`
    : (requestedRoundCount > 0 ? `最近 ${text(completedRoundCount || "0")} / ${text(requestedRoundCount)}` : "--");
  const liveJobId = displayJob.job_id || displaySession.active_job_id || modelContextSummary.job_id || modelOrchRunId || "--";
  const liveSessionId = displaySession.session_id || displayJob.session_id || modelContextSummary.session_id || "--";
  const currentRoundId = displaySession.current_round_group_id
    || displayJob.current_round_group_id
    || latestRoundForContract.round_group_id
    || projectedActiveRound.round_group_id
    || latestRoundForContract.model_run_id
    || projectedActiveRound.model_run_id
    || "--";
  const activeRunId = processProgress.latest_model_run_id || displaySession.latest_model_run_id || projectedActiveRound.model_run_id || latestRun.model_run_id || "--";
  const modelParam = (label, value, note = "", tone = "") => {
    const valueText = text(value, "");
    if (!valueText || valueText === "--" || valueText === "undefined") return "";
    return `
      <span class="model-exp-chip ${tone}" title="${escapeHtml(note || valueText)}">
        <b>${escapeHtml(label)}</b>
        <strong>${escapeHtml(valueText)}</strong>
        ${note ? `<small>${escapeHtml(note)}</small>` : ""}
      </span>
    `;
  };
  const statusChips = [
    modelParam("mode", modelResearchMode),
    modelParam("active values", modelPreflight.active_values_status || activeValuesReadiness.active_values_status, modelPreflight.stale_reason || activeValuesReadiness.stale_reason || activeValuesReadiness.feature_snapshot_block_reason || "", activeValuesReady ? "is-ok" : "is-warn"),
  ].filter(Boolean).join("");
  const featureSetId = text(displaySession.feature_set_id || activeFeatureProjection.feature_set_id || latestFeatureSet.feature_set_id || latestRoundForContract.feature_set_id || "未选择 feature set");
  const featureSetDisplayName = featureSetId
    .replace(/^fs-/, "")
    .replace(/[-_]\d{8}[-_]\d{4,6}$/i, "")
    .replace(/-/g, " · ");
  const selectedFeatureSetMeta = featureSetItems.find((item) => text(item.feature_set_id || item.id, "") === featureSetId) || {};
  const featureSetFactorCount = Number(selectedFeatureSetMeta.factor_count || selectedFeatureSetMeta.feature_count || activeFeatureProjection.factor_count || latestRoundForContract.factor_count || featureCount || 0);
  const featureSetFeatureCount = Number(selectedFeatureSetMeta.feature_count || activeFeatureProjection.feature_count || latestRoundForContract.feature_count || featureCount || 0);
  const featureSetCountText = [
    featureSetFactorCount ? `${featureSetFactorCount} factors` : "",
    featureSetFeatureCount ? `${featureSetFeatureCount} features` : "",
    label0Text,
  ].filter(Boolean).join(" · ");
  const rangeCardText = (value) => {
    const valueText = compactRange(value);
    return valueText && valueText !== "--" ? valueText : "--";
  };
  const trainWindowText = rangeCardText(experimentSegments.train || latestExperimentPlan.train_window);
  const validWindowText = rangeCardText(experimentSegments.valid || experimentSegments.validation || latestExperimentPlan.valid_window);
  const testWindowText = rangeCardText(experimentSegments.test || latestExperimentPlan.test_window);
  const portfolioSummary = portfolioText(latestExperimentPlan.portfolio, "top20/drop2/hold5");
  const paramKpi = (label, value, note = "", tone = "") => {
    const valueText = text(value, "");
    if (!valueText || valueText === "--" || valueText === "undefined") return "";
    return `
      <article class="model-param-kpi ${escapeHtml(tone)}" title="${escapeHtml([label, valueText, note].filter(Boolean).join(" · "))}">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(valueText)}</strong>
        ${note ? `<small>${escapeHtml(note)}</small>` : ""}
      </article>
    `;
  };
  const windowParamKpi = (label, rangeText) => {
    const dates = text(rangeText, "").match(/(\d{4}-\d{2}-\d{2}).*?(\d{4}-\d{2}-\d{2})/);
    return dates
      ? paramKpi(label, dates[1], `至 ${dates[2]}`)
      : paramKpi(label, rangeText);
  };
  const windowParamKpis = [
    windowParamKpi("Train", trainWindowText),
    windowParamKpi("Valid", validWindowText),
    windowParamKpi("Test", testWindowText),
  ].filter(Boolean).join("");
  const sampleWeightPolicy = text(latestExperimentPlan.sample_weight_policy || latestModelParams.sample_weight_policy, "");
  const sampleWeightSummary = sampleWeightPolicy === "top50_smooth2_bottom50_smooth1p5_mean_norm"
    ? "Top50 ×2.0 / Bottom50 ×1.5"
    : sampleWeightPolicy;
  const portfolioDisplay = portfolioSummary
    .replace(/top/ig, "Top")
    .replace(/drop/ig, "Drop")
    .replace(/hold/ig, "Hold")
    .replace(/\//g, " · ");
  const modelParamKpis = [
    paramKpi("学习率", latestModelParams.learning_rate ?? latestModelParams.lr),
    paramKpi("Num Leaves", latestModelParams.num_leaves),
    paramKpi("Max Depth", latestModelParams.max_depth),
    paramKpi("Min Leaf", latestModelParams.min_data_in_leaf ?? latestModelParams.min_child_samples),
    paramKpi("L1", latestModelParams.lambda_l1 ?? latestModelParams.reg_alpha),
    paramKpi("L2", latestModelParams.lambda_l2 ?? latestModelParams.reg_lambda),
    paramKpi("迭代轮数", latestModelParams.n_estimators ?? latestModelParams.num_boost_round),
    paramKpi("早停轮数", latestModelParams.early_stopping_rounds ?? latestModelParams.early_stopping_round),
    paramKpi("特征采样", latestModelParams.feature_fraction ?? latestModelParams.colsample_bytree),
    paramKpi("行采样", latestModelParams.bagging_fraction ?? latestModelParams.subsample),
    paramKpi("Boosting", latestModelParams.boosting_type ?? latestModelParams.boosting),
    paramKpi("目标函数", latestModelParams.objective),
    paramKpi("Sample Weight", sampleWeightSummary, "", "is-wide"),
  ].filter(Boolean).join("");
  const strategyParamKpis = [
    paramKpi("Portfolio", portfolioDisplay, "", "is-contract"),
    paramKpi("Benchmark", latestExperimentPlan.benchmark || modelContextSummary.benchmark || "000300sh"),
  ].filter(Boolean).join("");
  const tuningStatus = { ...(displaySession.payload || {}), ...(displayJob.payload || {}) };
  const platformBestRoundId = text(tuningStatus.best_round_group_id, "");
  const noImproveStreak = Number(tuningStatus.consecutive_no_improvement ?? 0);
  const topStatusPanel = `
    <section class="model-live-status-board ${runtimeState.tone}">
      <div class="model-live-status-main">
        <span class="detail-label">当前状态</span>
        <strong>${escapeHtml(runtimeState.label)}</strong>
        <small>${escapeHtml(runtimeState.detail)}</small>
        <div class="model-live-status-chips">${statusChips}</div>
      </div>
      <div class="model-live-status-grid">
        <span title="${escapeHtml(text(liveJobId, "--"))}"><b>${escapeHtml(compactLongId(liveJobId))}</b><small>${escapeHtml(liveObjectLabel)} job</small></span>
        <span title="${escapeHtml(text(liveSessionId, "--"))}"><b>${escapeHtml(compactLongId(liveSessionId))}</b><small>${escapeHtml(liveObjectLabel)} session</small></span>
        <span title="${escapeHtml(text(currentRoundId, "--"))}"><b>${escapeHtml(compactDateId(currentRoundId))}</b><small>${escapeHtml(liveObjectLabel)} round</small></span>
        <span title="${escapeHtml(text(activeRunId, "--"))}"><b>${escapeHtml(compactRunLabel(activeRunId))}</b><small>${escapeHtml(liveObjectLabel)} run</small></span>
        <span title="${escapeHtml(platformBestRoundId || "--")}"><b>${escapeHtml(compactDateId(platformBestRoundId || "--"))}</b><small>平台最优 round</small></span>
        <span><b>${escapeHtml(text(noImproveStreak, "0"))} / 3</b><small>连续未改善</small></span>
      </div>
      <div class="model-run-progress">
        <div>
          <strong>${escapeHtml(progressMainText)}</strong>
          <span>${escapeHtml(progressSubText)}</span>
        </div>
        ${showTrainingProgress
          ? `<meter min="0" max="100" value="${visibleTrainingProgressPct.toFixed(1)}"></meter>`
          : `<div class="model-idle-progress">${escapeHtml(runtimeState.label)}</div>`}
      </div>
    </section>
  `;
  const llmReviewBanner = llmReviewSignal.active ? `
    <section class="model-llm-review-banner ${escapeHtml(text(llmReviewSignal.severity, "warning"))}">
      <div>
        <span class="detail-label">${escapeHtml(reviewTitle)}</span>
        <strong>${escapeHtml(reviewDecisionText)}</strong>
        <p>${escapeHtml(clip(reviewReasonText, 260))}</p>
      </div>
      <aside>
        <span>${escapeHtml(reviewExecutionLabel)}</span>
        <small>${escapeHtml(reviewExecutionMeta)}</small>
      </aside>
    </section>
  ` : "";
  const modelLiveModuleHead = (eyebrow, title, hint) => `
    <div class="model-live-module-head">
      <div><p class="eyebrow">${escapeHtml(eyebrow)}</p><h3>${escapeHtml(title)}</h3></div>
      <small>${escapeHtml(hint)}</small>
    </div>
  `;
  const modelParameterCapsulesPanel = `
    <section class="model-experiment-ribbon model-live-module" id="model-live-configuration">
      ${modelLiveModuleHead("02 · Experiment Configuration", "实验配置", "本次训练使用的快照、数据窗口、模型参数与组合规则")}
      <div class="model-exp-identity">
        <div class="model-feature-set-summary">
          <span>Feature Set</span>
          <b title="${escapeHtml(featureSetId)}">${escapeHtml(featureSetDisplayName)}</b>
          <small>${escapeHtml(featureSetCountText || "等待 feature set")}</small>
        </div>
        <section class="model-param-group is-window">
          <span class="model-param-group-label">数据窗口</span>
          <div class="model-param-kpi-grid">${windowParamKpis || `<article class="model-param-kpi is-warn"><span>窗口</span><strong>等待实验计划</strong></article>`}</div>
        </section>
      </div>
      <div class="model-param-groups">
        <section class="model-param-group is-model">
          <span class="model-param-group-label">LightGBM 参数</span>
          <div class="model-param-kpi-grid">${modelParamKpis || `<article class="model-param-kpi is-warn"><span>参数</span><strong>等待实验计划</strong></article>`}</div>
        </section>
        <section class="model-param-group is-strategy">
          <span class="model-param-group-label">交易契约</span>
          <div class="model-param-kpi-grid">${strategyParamKpis}</div>
        </section>
      </div>
      ${preflightErrors.length || preflightWarnings.length ? `
        <div class="model-exp-warning">${escapeHtml((preflightErrors.concat(preflightWarnings)).slice(0, 3).join("；"))}</div>
      ` : ""}
    </section>
  `;
  const seedRunsForRound = (round) => {
    const direct = round.seed_runs || round.seed_models || round.seeds || [];
    if (Array.isArray(direct) && direct.length) return direct;
    if (seedComparisonList.length && (round.round_group_id || round.model_run_id)) return seedComparisonList;
    return [round];
  };
  const seedAnnualValue = (seed) => {
    const metrics = seed.metrics || seed.metrics_brief || {};
    return Number(metrics.excess_annualized_ret_with_cost ?? metrics.annualized_ret ?? seed.excess_annualized_ret_with_cost ?? seed.annualized_ret);
  };
  const seedIrValue = (seed) => {
    const metrics = seed.metrics || seed.metrics_brief || {};
    return Number(metrics.excess_information_ratio_with_cost ?? metrics.ir ?? seed.excess_information_ratio_with_cost ?? seed.sharpe);
  };
  const seedDdValue = (seed) => {
    const metrics = seed.metrics || seed.metrics_brief || {};
    return Number(metrics.max_drawdown ?? seed.max_drawdown);
  };
  const numberRange = (values, formatter) => {
    const finite = values.filter((value) => Number.isFinite(value));
    if (!finite.length) return "--";
    return `${formatter(Math.min(...finite))} ~ ${formatter(Math.max(...finite))}`;
  };
  const medianNumber = (values) => {
    const finite = values.filter((value) => Number.isFinite(value)).sort((a, b) => a - b);
    if (!finite.length) return Number.NaN;
    const middle = Math.floor(finite.length / 2);
    return finite.length % 2 ? finite[middle] : (finite[middle - 1] + finite[middle]) / 2;
  };
  const seedTone = (seed) => {
    const status = text(seed.registry_status || seed.status_in_registry || seed.metadata?.asset_status || seed.status, "").toLowerCase();
    if (status === "production") return "production";
    if (status === "candidate") return "candidate";
    if (status === "research") return "research";
    if (status === "archived" || seed.score_review_decision === "archive_below_threshold") return "archived";
    return "";
  };
  const roundLookup = new Map();
  [...allModelRounds, ...sessionRounds, ...roundTimeline].forEach((round) => {
    const roundId = text(round.round_group_id || round.model_run_id, "");
    if (roundId && !roundLookup.has(roundId)) roundLookup.set(roundId, round);
  });
  const registryByRunId = new Map();
  (models || []).forEach((item) => {
    const runId = text(item.model_run_id || item.display_model_id || item.model_id, "");
    if (runId) registryByRunId.set(runId, item);
  });
  const candidateSeedRowMap = new Map();
  const pushCandidateSeed = (seed, sourceRound = {}, source = "") => {
    if (!seed || typeof seed !== "object") return;
    const meta = seed.metadata && typeof seed.metadata === "object" ? seed.metadata : {};
    const runId = text(seed.model_run_id || seed.display_model_id || seed.model_id || meta.model_run_id, "");
    const roundId = text(seed.round_group_id || meta.round_group_id || sourceRound.round_group_id || sourceRound.model_run_id, "");
    const isModel = runId.startsWith("mrun_")
      || roundId.startsWith("mround_")
      || runId.startsWith("m0703_")
      || runId.includes("_mr0703_")
      || roundId.startsWith("mr0703_")
      || ["model", "model0703"].includes(meta.model_system_version)
      || ["domain.model", "domain.model0703"].includes(meta.source_module);
    if (!runId || !isModel || candidateSeedRowMap.has(runId)) return;
    const registryItem = registryByRunId.get(runId) || {};
    const registryMeta = registryItem.metadata && typeof registryItem.metadata === "object" ? registryItem.metadata : {};
    const round = roundLookup.get(roundId) || sourceRound || {};
    candidateSeedRowMap.set(runId, {
      seed: {
        ...registryItem,
        ...seed,
        model_run_id: runId,
        round_group_id: roundId || seed.round_group_id || meta.round_group_id,
        registry_status: seed.registry_status || seed.status_in_registry || registryItem.status || registryItem.registry_status,
        metadata: { ...registryMeta, ...meta },
        _source: source,
      },
      round,
      createdAt: Date.parse(seed.created_at || seed.updated_at || registryItem.created_at || registryItem.updated_at || round.created_at || "") || 0,
    });
  };
  allModelSeedRuns.forEach((seed) => pushCandidateSeed(seed, roundLookup.get(text(seed.round_group_id, "")) || {}, "runs"));
  (sessionRounds.length ? sessionRounds : roundTimeline).forEach((round) => {
    seedRunsForRound(round).forEach((seed) => pushCandidateSeed(seed, round, "session"));
  });
  seedDiagnostics.forEach((seed) => pushCandidateSeed(seed, roundLookup.get(text(seed.round_group_id || seed.metadata?.round_group_id, "")) || {}, "diagnostic"));
  models.forEach((seed) => pushCandidateSeed(seed, roundLookup.get(text(seed.round_group_id || seed.metadata?.round_group_id, "")) || {}, "registry"));
  const candidateSeedUniverse = [...candidateSeedRowMap.values()]
    .filter((row) => {
      const seed = row.seed || {};
      const metadata = seed.metadata && typeof seed.metadata === "object" ? seed.metadata : {};
      const runId = text(seed.model_run_id || seed.display_model_id || seed.model_id, "");
      const seedPolicy = text(seed.seed_policy || row.round?.seed_policy || metadata.seed_policy, "");
      return registryByRunId.has(runId)
        || seedPolicy === "staged_screening_then_confirmation"
        || text(seed.evaluation_mode || metadata.evaluation_mode, "") === "production"
        || Boolean(seed.rolling_campaign_id || metadata.rolling_campaign_id);
    })
    .sort((a, b) => b.createdAt - a.createdAt);
  const seedRowsByRound = new Map();
  candidateSeedUniverse.forEach((row) => {
    const roundId = text(row.seed.round_group_id || row.round.round_group_id || row.round.model_run_id, "");
    if (!roundId) return;
    if (!seedRowsByRound.has(roundId)) seedRowsByRound.set(roundId, []);
    seedRowsByRound.get(roundId).push(row.seed);
  });
  const candidateSeedRows = candidateSeedUniverse
    .filter((row) => {
      const metadata = row.seed.metadata && typeof row.seed.metadata === "object" ? row.seed.metadata : {};
      const evaluationMode = text(row.seed.evaluation_mode || metadata.evaluation_mode || "research").toLowerCase();
      const rollingCampaignId = text(row.seed.rolling_campaign_id || metadata.rolling_campaign_id, "");
      return Number(row.seed.seed ?? metadata.seed ?? seedFromRunId(row.seed.model_run_id)) === 42
        && evaluationMode !== "production"
        && !rollingCampaignId;
    })
    .slice(0, 30).map((row, index) => {
    const roundId = text(row.seed.round_group_id || row.round.round_group_id || row.round.model_run_id, "");
    const seedRuns = seedRowsByRound.get(roundId) || [row.seed];
    return {
      ...row,
      index,
      annRange: numberRange(seedRuns.map(seedAnnualValue), (value) => pct(value, 1)),
      irRange: numberRange(seedRuns.map(seedIrValue), (value) => shortNumber(value, 3)),
      ddRange: numberRange(seedRuns.map(seedDdValue), (value) => pct(value, 1)),
    };
  });
  const candidateModelRows = candidateSeedRows.map(({ seed, round, index, annRange, irRange, ddRange }, rowIndex) => {
    const metrics = seed.metrics || seed.metrics_brief || {};
    const training = metrics.training_diagnostics || seed.training_diagnostics || {};
    const score = seed.score || {};
    const confirmation = seed.research_confirmation || seed.metadata?.research_confirmation || {};
    const rollingGates = seed.rolling_gates || seed.metadata?.rolling_gates || {};
    const legacySeed = isLegacyResearchRecord(seed)
      || isLegacyResearchRecord(round)
      || seed.metadata?.lifecycle_migration === "dual_mode_research_20260719";
    const gate = seed.gate || seed.gate_result || {};
    const candidateModelId = text(seed.model_id || seed.registry_model_id || seed.metadata?.model_id || "", "");
    const candidateModelRunId = text(seed.model_run_id || seed.display_model_id || round.model_run_id || "", "");
    const candidateSeed = text((seed.seed ?? seed.metadata?.seed ?? seedFromRunId(candidateModelRunId)) || "--");
    const seedExperiment = seed.experiment || round.experiment || round.experiment_plan || latestExperimentPlan || {};
    const candidateFeatureSetId = text(
      seed.feature_set_id
        || seed.metadata?.feature_set_id
        || round.feature_set_id
        || seedExperiment.feature_set_id,
      "Feature Set 未记录",
    );
    const candidateLabel = canonicalModelDisplayName({
      ...seed,
      model_run_id: candidateModelRunId,
      feature_set_id: candidateFeatureSetId,
      status: seed.registry_status || seed.status_in_registry || seed.metadata?.asset_status || seed.status || "research",
    }, { roundNo: round.round_no ?? round.candidate_round_no ?? null });
    const seedParamLine = seed.params_brief
      ? paramBriefLine(seed)
      : experimentParamLine(seedExperiment, round);
    const statusText = text(seed.registry_status || seed.status_in_registry || seed.metadata?.asset_status || seed.status || "pending");
    const evaluationMode = text(seed.evaluation_mode || seed.metadata?.evaluation_mode || "research");
    const confirmationStatus = text(confirmation.status, candidateSeed === "42" ? "screening" : "pending");
    const gateText = legacySeed
      ? "historical"
      : evaluationMode === "production"
      ? (Object.keys(rollingGates).length ? (Object.values(rollingGates).every(Boolean) ? "passed" : "rejected") : "pending")
      : confirmationStatus;
    const gateShortText = ({
      pass_with_warnings: "warn",
      passed_with_warnings: "warn",
      pass: "pass",
      passed: "pass",
      reject: "reject",
      rejected: "reject",
      pending: "pending",
      historical: "历史",
    }[gateText.toLowerCase()] || gateText);
    const statusKey = text(statusText, "").toLowerCase().replace(/[^a-z0-9_-]+/g, "-") || "pending";
    const gateKey = ({
      pass_with_warnings: "warn",
      passed_with_warnings: "warn",
      warn: "warn",
      warning: "warn",
      pass: "pass",
      passed: "pass",
      reject: "reject",
      rejected: "reject",
      pending: "pending",
      historical: "historical",
    }[gateText.toLowerCase()] || gateText.toLowerCase().replace(/[^a-z0-9_-]+/g, "-") || "pending");
    const displayScore = Number(
      score.research_score ?? seed.research_score ?? seed.metadata?.research_score
      ?? seed.confirmed_research_score ?? seed.metadata?.confirmed_research_score
    );
    const scoreLabel = legacySeed ? "历史分不沿用" : "SOTA 得分";
    const scoreTone = Number.isFinite(displayScore)
      ? (displayScore < 50 ? "is-low" : (displayScore < 60 ? "is-mid" : (displayScore < 85 ? "is-good" : "is-best")))
      : "";
    const assetDisplayLabel = evaluationMode === "production"
      ? "生产模型"
      : statusText.toLowerCase() === "candidate"
        ? "候选模型"
        : "研究模型";
    const confirmationDisplayLabel = ({
      pass: "稳定性复核通过",
      passed: "稳定性复核通过",
      pass_with_warnings: "稳定性复核通过",
      passed_with_warnings: "稳定性复核通过",
      reject: "稳定性复核未通过",
      rejected: "稳定性复核未通过",
      failed: "稳定性复核未通过",
      not_run: "待稳定性复核",
      pending: "待稳定性复核",
      screening: "待稳定性复核",
      historical: "历史记录",
    }[gateText.toLowerCase()] || "待稳定性复核");
    const displayParamLine = seedParamLine.split(" · ").slice(0, 6).join(" · ");
    const stabilityText = [
      `round ann ${annRange}`,
      `IR ${irRange}`,
      `DD ${ddRange}`,
      seedStabilityAvailable && index === 0 ? `overlap ${pct(seedStability.top10_overlap_mean, 1)}` : "",
      seedStabilityAvailable && index === 0 ? `rank corr ${shortNumber(seedStability.pred_rank_corr_mean, 3)}` : "",
      training.available ? `${training.early_stopped ? "early stop" : "full budget"} · best ${text(training.best_iteration, "--")}/${text(training.configured_n_estimators, "--")}` : "",
      training.train_valid_gap_at_best != null ? `train-valid gap ${shortNumber(training.train_valid_gap_at_best, 5)}` : "",
    ].filter(Boolean).join(" · ");
    const candidateTooltip = [
      `查看 ${candidateLabel} 的模型回测详情`,
      text(candidateModelRunId || candidateModelId, ""),
      `Feature Set：${candidateFeatureSetId}`,
      seedParamLine ? `参数：${seedParamLine}` : "",
      legacySeed ? `状态：${statusText} · 历史旧流程` : `状态：${statusText} · ${evaluationMode === "production" ? "Rolling Gate" : "Seed 确认"} ${gateText}`,
      Number.isFinite(displayScore) ? `${scoreLabel}：${shortNumber(displayScore, 1)}` : "",
      `年化：${pct(metrics.excess_annualized_ret_with_cost ?? metrics.annualized_ret ?? seed.excess_annualized_ret_with_cost ?? seed.annualized_ret, 1)} · IR：${shortNumber(metrics.excess_information_ratio_with_cost ?? metrics.ir ?? seed.excess_information_ratio_with_cost ?? seed.sharpe, 3)} · DD：${pct(metrics.max_drawdown ?? seed.max_drawdown, 1)}`,
      stabilityText,
    ].filter(Boolean).join("\n");
    return `
      <article
        class="model-candidate-row ${seedTone(seed)}"
        role="button"
        tabindex="0"
        data-model-backtest-id="${escapeHtml(candidateModelId)}"
        data-model-backtest-run-id="${escapeHtml(candidateModelRunId)}"
        data-model-backtest-label="${escapeHtml(candidateLabel)}"
        data-model-backtest-role="research_model"
        title="${escapeHtml(candidateTooltip)}"
        aria-label="查看 ${escapeHtml(candidateLabel)} 的模型回测详情"
      >
        <div class="model-candidate-rank">${escapeHtml(text(rowIndex + 1, ""))}</div>
        <div class="model-candidate-main">
          <div class="model-candidate-title">
            <strong title="${escapeHtml(text(seed.model_run_id || seed.display_model_id || round.model_run_id || "等待 model_run_id"))}">${escapeHtml(candidateLabel)}</strong>
            <span title="${escapeHtml(text(round.round_group_id || seed.round_group_id || round.model_run_id || "round --"))}">Round ${escapeHtml(compactDateId(round.round_group_id || seed.round_group_id || round.model_run_id || "--"))}</span>
          </div>
          <p title="${escapeHtml(seedParamLine || "等待参数")}">${escapeHtml(displayParamLine || "等待参数")}</p>
          <div class="model-candidate-feature-set" title="Feature Set：${escapeHtml(candidateFeatureSetId)}">
            <span>Feature Set</span>
            <strong>${escapeHtml(displayModelIdentifier(candidateFeatureSetId))}</strong>
          </div>
          <div class="model-candidate-state">
            <span class="asset-status ${escapeHtml(statusKey)}" title="${escapeHtml(statusText)}">${escapeHtml(assetDisplayLabel)}</span>
            <span class="forward-status ${escapeHtml(gateKey)}" title="${escapeHtml(gateText)}">${escapeHtml(legacySeed ? "历史记录" : confirmationDisplayLabel)}</span>
          </div>
          <small>${escapeHtml(stabilityText)}</small>
        </div>
        <div class="model-candidate-metrics">
          <span class="model-candidate-metric-score ${escapeHtml(scoreTone)}"><b>${escapeHtml(shortNumber(displayScore, 1))}</b><small>${escapeHtml(scoreLabel)}</small></span>
          <span><b>${pct(metrics.excess_annualized_ret_with_cost ?? metrics.annualized_ret ?? seed.excess_annualized_ret_with_cost ?? seed.annualized_ret, 1)}</b><small>年化</small></span>
          <span><b>${shortNumber(metrics.excess_information_ratio_with_cost ?? metrics.ir ?? seed.excess_information_ratio_with_cost ?? seed.sharpe, 3)}</b><small>IR</small></span>
          <span><b>${pct(metrics.max_drawdown ?? seed.max_drawdown, 1)}</b><small>最大回撤</small></span>
        </div>
      </article>
    `;
  }).join("");
  const rollingCandidateRows = rollingCampaigns.slice(0, 10).map((campaign, index) => {
    const campaignId = text(campaign.campaign_id, "");
    const seeds = Array.isArray(campaign.seeds) ? campaign.seeds : [];
    const seedNumbers = seeds.map((seed) => Number(seed.seed)).filter(Number.isFinite);
    const seed42 = seeds.find((seed) => Number(seed.seed) === 42) || seeds[0] || {};
    const seed42Metrics = seed42.rolling_metrics || seed42.metrics || {};
    const rollingScore = Number(campaign.final?.rolling_score ?? campaign.score?.rolling_score ?? campaign.preliminary?.score);
    const annualizedRet = Number(seed42Metrics.excess_annualized_ret_with_cost ?? seed42Metrics.annualized_ret);
    const informationRatio = Number(seed42Metrics.excess_information_ratio_with_cost ?? seed42Metrics.ir);
    const maxDrawdown = Number(seed42Metrics.max_drawdown);
    const rollingFeatureSetId = text(
      campaign.feature_set_id
        || seed42.feature_set_id
        || seed42.metadata?.feature_set_id,
      "Feature Set 未记录",
    );
    const candidatePassed = campaign.candidate_created === true;
    const campaignComplete = Boolean(campaign.completed_at || campaign.decision);
    const gateKey = candidatePassed ? "pass" : (campaignComplete ? "reject" : "pending");
    const decisionLabel = candidatePassed
      ? "Candidate 已生成"
      : campaign.decision === "rolling_gate_failed"
        ? "Rolling Gate 未通过"
        : campaign.decision === "stop_after_seed42"
          ? "Seed42 初筛未通过"
          : campaignComplete
            ? text(campaign.decision, "已完成")
            : "Rolling 运行中";
    const rollingDisplayName = canonicalModelDisplayName(campaign, { kind: "rolling" });
    const scoreTone = Number.isFinite(rollingScore)
      ? (rollingScore < 50 ? "is-low" : (rollingScore < 60 ? "is-mid" : (rollingScore < 85 ? "is-good" : "is-best")))
      : "";
    const factorCount = text(seeds.find((seed) => Number(seed.seed) === 42)?.factor_count || seeds[0]?.factor_count || campaign.factor_count, "--");
    const campaignTooltip = [
      `查看正式 Rolling campaign：${campaignId}`,
      `Feature Set：${rollingFeatureSetId}`,
      `四折连续拼接 · Seed ${seedNumbers.join("/") || "--"} · ${factorCount} 因子`,
      `状态：${decisionLabel}`,
      Number.isFinite(rollingScore) ? `${campaign.final?.available ? "Rolling 准入分" : "Seed42 初筛分"}：${shortNumber(rollingScore, 1)}` : "",
      `正式 Seed42：年化 ${pct(annualizedRet, 1)} · IR ${shortNumber(informationRatio, 3)} · DD ${pct(maxDrawdown, 1)}`,
    ].filter(Boolean).join("\n");
    return `
      <article
        class="model-candidate-row is-rolling ${candidatePassed ? "candidate" : "research"}"
        role="button"
        tabindex="0"
        data-model-backtest-id="rolling:${escapeHtml(campaignId)}"
        data-model-backtest-run-id="${escapeHtml(campaignId)}"
        data-model-backtest-label="${escapeHtml(rollingDisplayName)}"
        data-model-backtest-role="rolling_campaign"
        title="${escapeHtml(campaignTooltip)}"
        aria-label="查看 ${escapeHtml(rollingDisplayName)} 的完整回测"
      >
        <div class="model-candidate-rank">R${escapeHtml(text(index + 1))}</div>
        <div class="model-candidate-main">
          <div class="model-candidate-title">
            <strong title="${escapeHtml(campaignId)}">${escapeHtml(rollingDisplayName)}</strong>
            <span>四折连续拼接 · ${escapeHtml(seedNumbers.length > 1 ? "稳定性审计已完成" : "等待稳定性审计")}</span>
          </div>
          <p>${escapeHtml(`${factorCount} 因子 · Top${text(campaign.portfolio?.topk, "--")} / Drop${text(campaign.portfolio?.n_drop, "--")} / Hold${text(campaign.portfolio?.hold_thresh, "--")}`)}</p>
          <div class="model-candidate-feature-set" title="Feature Set：${escapeHtml(rollingFeatureSetId)}">
            <span>Feature Set</span>
            <strong>${escapeHtml(displayModelIdentifier(rollingFeatureSetId))}</strong>
          </div>
          <div class="model-candidate-state">
            <span class="asset-status rolling">Rolling 模型</span>
            <span class="forward-status ${escapeHtml(gateKey)}">${escapeHtml(decisionLabel)}</span>
          </div>
        </div>
        <div class="model-candidate-metrics">
          <span class="model-candidate-metric-score ${escapeHtml(scoreTone)}"><b>${escapeHtml(shortNumber(rollingScore, 1))}</b><small>SOTA 得分</small></span>
          <span><b>${pct(annualizedRet, 1)}</b><small>年化</small></span>
          <span><b>${shortNumber(informationRatio, 3)}</b><small>IR</small></span>
          <span><b>${pct(maxDrawdown, 1)}</b><small>最大回撤</small></span>
        </div>
      </article>
    `;
  }).join("");
  const activeModelAssetLane = state.activeModelAssetLane === "rolling" ? "rolling" : "research";
  const candidateModelPanel = `
    <section class="model-console-section model-round-seed-panel" id="model-live-assets">
      <div class="live-section-title">
        <div><p class="eyebrow">04 · MODEL RESULTS</p><h3>模型分数与结果</h3></div>
        <small>在此查看研究结果与 Rolling 验证结果。</small>
      </div>
      <nav class="model-live-lane-tabs model-asset-lane-tabs" aria-label="模型结果类型">
        <button class="${activeModelAssetLane === "research" ? "active" : ""}" type="button" data-model-asset-lane="research" aria-pressed="${activeModelAssetLane === "research" ? "true" : "false"}">研究模型</button>
        <button class="${activeModelAssetLane === "rolling" ? "active" : ""}" type="button" data-model-asset-lane="rolling" aria-pressed="${activeModelAssetLane === "rolling" ? "true" : "false"}">Rolling 模型</button>
      </nav>
      <div class="model-asset-lane-head">
        <strong>${activeModelAssetLane === "rolling" ? "Rolling 模型" : "研究模型"}</strong>
        <span>${activeModelAssetLane === "rolling" ? "冻结参数后的四折验证结果" : "用于比较参数配置的研究结果"}</span>
      </div>
      <div class="model-candidate-list">${activeModelAssetLane === "rolling" ? (rollingCandidateRows || `<div class="empty-state">当前没有 Rolling 模型。</div>`) : (candidateModelRows || `<div class="empty-state">暂无研究模型。</div>`)}</div>
    </section>
  `;
  const rollingGateLabels = {
    four_folds_complete: "四折完整",
    reliability_passed: "拼接可靠性",
    preliminary_score_reached: "初筛分达标",
    at_least_two_positive_stitched_ir: "至少两个 Seed 拼接 IR 为正",
    at_least_three_positive_fold_ir: "至少三折 IR 为正",
    latest_fold_ir_positive: "最新折 IR 为正",
    ir_std_within_limit: "跨 Seed IR 稳定",
    return_std_within_limit: "跨 Seed 收益稳定",
    median_drawdown_within_limit: "跨 Seed 回撤受控",
    drawdown_within_limit: "回撤不越线",
  };
  const rollingDecisionText = (campaign) => {
    const decision = text(campaign.decision, "");
    if (decision === "stop_after_seed42") return "Seed42 初筛未通过，按规则停止；Seed17/83 未执行，不是任务缺失。";
    if (decision === "rolling_gate_failed") return "三 Seed 已完成，但正式 Rolling 门槛未全部通过，保留 research。";
    if (decision === "candidate") return "正式 Rolling 已通过，已生成 candidate。";
    if (campaign.status === "failed") return `Rolling 执行失败：${text(campaign.err, "请查看 evidence")}`;
    return decision || "等待正式 Rolling 结果。";
  };
  const rollingCampaignForPanel = backtest.rolling_campaign || latestRollingCampaign;
  const rollingSeed42 = (rollingCampaignForPanel.seeds || []).find((item) => Number(item.seed) === 42)
    || (rollingCampaignForPanel.seeds || [])[0]
    || {};
  const rollingMetrics = rollingSeed42.rolling_metrics || {};
  const rollingPreliminary = rollingCampaignForPanel.preliminary || {};
  const rollingFinal = rollingCampaignForPanel.final || {};
  const rollingCampaignScore = rollingFinal.available ? rollingFinal.rolling_score : rollingPreliminary.score;
  const campaignRollingGates = rollingFinal.available ? (rollingFinal.gates || {}) : (rollingPreliminary.gates || {});
  const rollingCampaignPassed = rollingCampaignForPanel.candidate_created === true;
  const rollingSeedAuditRows = (rollingCampaignForPanel.seeds || []).map((seed) => {
    const seedNumber = Number(seed.seed);
    const metrics = seed.rolling_metrics || {};
    return `
      <tr>
        <td><strong>Seed${escapeHtml(text(seedNumber))}</strong><small>${seedNumber === 42 ? "正式模型" : "稳定性审计"}</small></td>
        <td>${pct(metrics.excess_annualized_ret_with_cost, 2)}</td>
        <td>${shortNumber(metrics.excess_information_ratio_with_cost, 3)}</td>
        <td>${pct(metrics.max_drawdown, 2)}</td>
        <td>${shortNumber(seed.diagnostic_score, 2)}</td>
        <td><span class="badge ${seedNumber === 42 ? "ok" : "subtle"}">${seedNumber === 42 ? "正式展示" : "仅审计"}</span></td>
      </tr>
    `;
  }).join("");
  const registryResearchModels = models.filter((item) => ["research", "candidate", "production"].includes(text(item.status, "").toLowerCase()));
  const seed42ResearchAvailable = candidateSeedUniverse.some(({ seed }) => Number(seed.seed ?? seed.metadata?.seed ?? seedFromRunId(seed.model_run_id)) === 42)
    || Boolean(latestRun.model_run_id);
  const currentResearchConfirmationFailed = text(researchCurrent.stage, "") === "research_confirmation"
    && text(researchCurrent.decision, "") === "failed";
  const researchConfirmationPassed = models.some((item) => {
    const confirmation = item.research_confirmation || item.metadata?.research_confirmation || {};
    return ["pass", "passed", "confirmed"].includes(text(confirmation.status, "").toLowerCase());
  }) || Boolean(rollingCampaignForPanel.campaign_id);
  const lifecycleRollingCampaign = currentResearchConfirmationFailed ? {} : rollingCampaignForPanel;
  const rollingSeeds = lifecycleRollingCampaign.seeds || [];
  const rollingSeedNumbers = new Set(rollingSeeds.map((item) => Number(item.seed)).filter(Number.isFinite));
  const rollingCampaignComplete = Boolean(lifecycleRollingCampaign.completed_at || lifecycleRollingCampaign.decision);
  const rollingInitialStatus = !lifecycleRollingCampaign.campaign_id
    ? "waiting"
    : !rollingCampaignComplete
      ? "running"
      : (lifecycleRollingCampaign.preliminary || {}).passed
        ? "done"
        : "failed";
  const rollingConfirmStatus = rollingSeedNumbers.has(17) && rollingSeedNumbers.has(83)
    ? "done"
    : rollingInitialStatus === "failed"
      ? "skipped"
      : rollingInitialStatus === "done"
        ? "running"
        : "waiting";
  const rollingCandidateStatus = lifecycleRollingCampaign.candidate_created
    ? "done"
    : rollingCampaignComplete
      ? "failed"
      : "waiting";
  const matchingProductionModel = productionModels.find((item) => {
    const metadata = item.metadata || {};
    return text(item.rolling_campaign_id || metadata.rolling_campaign_id, "") === text(lifecycleRollingCampaign.campaign_id, "")
      || (lifecycleRollingCampaign.candidate_created && text(item.feature_set_id || metadata.feature_set_id, "") === text(lifecycleRollingCampaign.feature_set_id, ""));
  });
  const productionReleaseStatus = matchingProductionModel
    ? "done"
    : lifecycleRollingCampaign.candidate_created
      ? "waiting"
      : rollingCampaignComplete
        ? "skipped"
        : "waiting";
  const lifecycleStatusLabel = (status) => ({
    done: "已完成",
    running: "进行中",
    failed: "未通过",
    skipped: "未进入",
    waiting: "等待",
  }[status] || "等待");
  const researchLifecycleNodes = [
    {
      title: "研究准备",
      detail: "确认快照、数据窗口与训练约束",
      status: featureSetId && featureSetId !== "未选择 feature set" ? "done" : "waiting",
    },
    {
      title: "参数实验",
      detail: "在固定窗口中比较参数配置",
      status: projectedActiveRound.round_no != null || activeRunId !== "--" ? "done" : "waiting",
    },
    {
      title: "基准训练与回测",
      detail: "固定随机种子 42，生成研究评分",
      status: seed42ResearchAvailable ? "done" : (isTrainingActive ? "running" : "waiting"),
    },
    {
      title: "优胜配置稳定性复核",
      detail: "使用随机种子 17 / 83 复核稳定性",
      status: currentResearchConfirmationFailed ? "failed" : (researchConfirmationPassed ? "done" : "waiting"),
    },
    {
      title: "研究结果登记",
      detail: "保留参数、评分与复核结论",
      status: registryResearchModels.length ? "done" : "waiting",
    },
  ];
  const productionLifecycleNodes = [
    {
      title: "四折滚动初筛",
      detail: "固定随机种子 42，执行四折拼接验证",
      status: rollingInitialStatus,
    },
    {
      title: "跨种子滚动复核",
      detail: "初筛通过后，使用 17 / 83 检查稳定性",
      status: rollingConfirmStatus,
    },
    {
      title: "候选模型准入",
      detail: "满足全部 Rolling 门槛后生成候选模型",
      status: rollingCandidateStatus,
    },
    {
      title: "生产模型发布",
      detail: "固定随机种子 42 refit 并登记生产模型",
      status: productionReleaseStatus,
    },
  ];
  const researchLifecycleCurrentTitle = currentResearchConfirmationFailed
    ? "研究稳定性复核未通过"
    : researchConfirmationPassed
      ? "研究稳定性复核已通过"
      : isTrainingActive
        ? "研究训练进行中"
        : "等待研究结果";
  const researchLifecycleCurrentDetail = currentResearchConfirmationFailed
    ? "同一参数在不同随机种子下结果差异较大，暂不进入 Rolling。"
    : researchConfirmationPassed
      ? "最优配置已经完成跨种子复核，可以进入 Rolling 验证。"
      : "先完成 Seed42 基准训练与回测，再对最优配置进行跨种子复核。";
  const rollingLifecycleCurrentTitle = !lifecycleRollingCampaign.campaign_id
    ? "尚未启动 Rolling"
    : productionReleaseStatus === "done"
      ? "生产模型已发布"
      : lifecycleRollingCampaign.candidate_created
        ? "Rolling 已通过，等待生产发布"
        : rollingCampaignComplete && lifecycleRollingCampaign.decision === "rolling_gate_failed"
          ? "Rolling 稳定性复核未通过"
          : rollingCampaignComplete && lifecycleRollingCampaign.decision === "stop_after_seed42"
            ? "Rolling 初筛未通过"
            : "Rolling 验证进行中";
  const rollingLifecycleCurrentDetail = !lifecycleRollingCampaign.campaign_id
    ? (currentResearchConfirmationFailed
      ? "研究稳定性复核尚未通过，因此没有启动 Rolling。"
      : "研究确认通过后，才会使用固定参数启动四折 Rolling 验证。")
    : rollingCampaignComplete && lifecycleRollingCampaign.decision === "rolling_gate_failed"
      ? "四折验证完成，但跨种子稳定性门槛未通过；不生成候选模型。"
      : rollingCampaignComplete && lifecycleRollingCampaign.decision === "stop_after_seed42"
        ? "Seed42 四折初筛未通过；按规则不再执行 Seed17 / 83 复核。"
        : lifecycleRollingCampaign.candidate_created
          ? "Rolling 候选已生成；生产发布仍需固定 Seed42 refit。"
          : "正在按四折窗口验证；完整分数与明细请到回测结果查看。";
  const researchLifecycleSteps = researchLifecycleNodes.map((node, index) => ({ ...node, lane: "研究", sequence: index + 1 }));
  const rollingLifecycleSteps = productionLifecycleNodes.map((node, index) => ({ ...node, lane: "Rolling", sequence: index + 1 }));
  const researchLifecycleTone = currentResearchConfirmationFailed ? "failed" : (researchConfirmationPassed ? "done" : "waiting");
  const rollingLifecycleTone = !lifecycleRollingCampaign.campaign_id
    ? "waiting"
    : lifecycleRollingCampaign.candidate_created || productionReleaseStatus === "done"
      ? "done"
      : rollingCampaignComplete
        ? "failed"
        : "running";
  const combinedLifecycleSteps = [...researchLifecycleSteps, ...rollingLifecycleSteps];
  const combinedLifecycleDoneCount = combinedLifecycleSteps.filter((node) => node.status === "done").length;
  const combinedLifecycleCurrentTitle = currentResearchConfirmationFailed
    ? researchLifecycleCurrentTitle
    : lifecycleRollingCampaign.campaign_id
      ? rollingLifecycleCurrentTitle
      : researchLifecycleCurrentTitle;
  const combinedLifecycleCurrentDetail = currentResearchConfirmationFailed
    ? researchLifecycleCurrentDetail
    : lifecycleRollingCampaign.campaign_id
      ? rollingLifecycleCurrentDetail
      : researchLifecycleCurrentDetail;
  const combinedLifecycleTone = currentResearchConfirmationFailed
    ? researchLifecycleTone
    : lifecycleRollingCampaign.campaign_id
      ? rollingLifecycleTone
      : researchLifecycleTone;
  const lifecycleFocusNode = combinedLifecycleSteps.find((node) => ["running", "failed"].includes(node.status))
    || combinedLifecycleSteps.find((node) => node.status === "waiting")
    || combinedLifecycleSteps.at(-1);
  const lifecycleProgressLabel = (title) => ({
    "研究准备": "研究准备",
    "参数实验": "参数实验",
    "基准训练与回测": "基准回测",
    "优胜配置稳定性复核": "稳定性确认",
    "研究结果登记": "结果登记",
    "四折滚动初筛": "四折初筛",
    "跨种子滚动复核": "跨种子复核",
    "候选模型准入": "候选准入",
    "生产模型发布": "生产发布",
  }[title] || title);
  const renderLifecycleProgress = () => `
    <div class="model-lifecycle-progress" aria-label="研究到生产进度">
      <div class="model-lifecycle-progress-summary">
        <span>研究 ${escapeHtml(text(researchLifecycleSteps.filter((node) => node.status === "done").length))} / ${escapeHtml(text(researchLifecycleSteps.length))}</span>
        <span>Rolling ${escapeHtml(text(rollingLifecycleSteps.filter((node) => node.status === "done").length))} / ${escapeHtml(text(rollingLifecycleSteps.length))}</span>
      </div>
      <div class="model-lifecycle-progress-rail" role="progressbar" aria-valuemin="0" aria-valuemax="${escapeHtml(text(combinedLifecycleSteps.length))}" aria-valuenow="${escapeHtml(text(combinedLifecycleDoneCount))}">
        ${combinedLifecycleSteps.map((node, index) => `<span class="is-${escapeHtml(node.status)}" title="${escapeHtml(`${node.lane} · ${node.title} · ${lifecycleStatusLabel(node.status)}`)}">${escapeHtml(text(index + 1))}</span>`).join("")}
      </div>
      <div class="model-lifecycle-progress-stage-labels">
        ${combinedLifecycleSteps.map((node) => `<span title="${escapeHtml(node.title)}">${escapeHtml(lifecycleProgressLabel(node.title))}</span>`).join("")}
      </div>
      <small>当前步骤：${escapeHtml(text(lifecycleFocusNode?.title, "--"))} · ${escapeHtml(lifecycleStatusLabel(lifecycleFocusNode?.status))}</small>
    </div>
  `;
  const renderLifecycleSteps = (steps) => steps.map((node) => `
    <article class="model-lifecycle-step is-${escapeHtml(node.status)}">
      <span class="model-lifecycle-step-dot">${escapeHtml(text(node.sequence))}</span>
      <div>
        <small>${escapeHtml(node.lane)}</small>
        <strong>${escapeHtml(node.title)}</strong>
        <span>${escapeHtml(lifecycleStatusLabel(node.status))}</span>
      </div>
    </article>
  `).join("");
  const modelLifecyclePanel = `
    <div class="model-lifecycle-panel" id="model-live-lifecycle">
      <div class="model-lifecycle-head">
        <div>
          <h3>模型验证进度</h3>
        </div>
        <div class="model-lifecycle-head-actions">
          <span class="model-lifecycle-count">${escapeHtml(text(combinedLifecycleDoneCount))} / ${escapeHtml(text(combinedLifecycleSteps.length))} 已完成</span>
          ${lifecycleRollingCampaign.campaign_id ? `<button class="ghost small" type="button" data-backtest-selector="rolling" data-backtest-label="Rolling 结果">查看 Rolling 详情</button>` : ""}
        </div>
      </div>
      <div class="model-lifecycle-current is-${escapeHtml(combinedLifecycleTone)}">
        <span>当前结论</span>
        <div><strong>${escapeHtml(combinedLifecycleCurrentTitle)}</strong><small>${escapeHtml(combinedLifecycleCurrentDetail)}</small></div>
      </div>
      ${renderLifecycleProgress()}
    </div>
  `;
  const rollingFoldRows = (rollingSeed42.folds || []).map((fold, index) => {
    const healthy = Number(fold.information_ratio) > 0;
    return `
      <tr class="${healthy ? "" : "danger-row"}">
        <td><strong>${escapeHtml(`第 ${index + 1} 折`)}</strong><small>${escapeHtml(text(fold.fold_id, "--"))}</small></td>
        <td>${escapeHtml(text(fold.signal_start, "--"))}<small>至 ${escapeHtml(text(fold.signal_end, "--"))}</small></td>
        <td><strong>${pct(fold.annualized_ret, 2)}</strong></td>
        <td><strong>${shortNumber(fold.information_ratio, 3)}</strong></td>
        <td>${pct(fold.max_drawdown, 2)}</td>
        <td>${shortNumber(fold.quality_score, 2)}<small>${fold.last_signal_executed === false ? "末日信号待下一交易日" : `${text(fold.report_days, "--")} 日`}</small></td>
      </tr>
    `;
  }).join("");
  const rollingGateCards = Object.entries(campaignRollingGates).map(([key, passed]) => `
    <article class="rolling-gate-card ${passed ? "is-pass" : "is-fail"}">
      <span>${escapeHtml(rollingGateLabels[key] || key)}</span>
      <strong>${passed ? "通过" : "未通过"}</strong>
    </article>
  `).join("");
  const rollingCampaignPanel = rollingCampaignForPanel.campaign_id ? `
    <section class="model-console-section model-rolling-campaign-panel">
      <div class="live-section-title">
        <div>
          <h3>生产 Rolling · 四折拼接</h3>
          <small>${escapeHtml(text(rollingCampaignForPanel.campaign_id))} · ${escapeHtml(text(rollingCampaignForPanel.feature_set_id, "--"))}</small>
        </div>
        <span class="badge ${rollingCampaignForPanel.candidate_created ? "ok" : "warn"}">${escapeHtml(rollingCampaignForPanel.candidate_created ? "Candidate 已生成" : text(rollingCampaignForPanel.status, "research"))}</span>
      </div>
      <div class="rolling-scope-note">
        <div><span>正式模型与曲线</span><strong>固定 Seed42 · 不选择最高 Seed</strong></div>
        <div><span>Seed17/83 的用途</span><strong>只参与稳定性审计和 Candidate 准入</strong></div>
      </div>
      <div class="rolling-campaign-summary">
        <article><span>${rollingFinal.available ? "Rolling 准入分" : "Seed42 初筛分"}</span><strong>${shortNumber(rollingCampaignScore, 2)}</strong><small>${rollingFinal.available ? "三 Seed 稳定性准入" : "仅决定是否补跑 17/83"}</small></article>
        <article><span>Seed42 拼接年化</span><strong>${pct(rollingMetrics.excess_annualized_ret_with_cost, 2)}</strong><small>正式扣费超额 · 四折全区间</small></article>
        <article><span>Seed42 拼接 IR</span><strong>${shortNumber(rollingMetrics.excess_information_ratio_with_cost, 3)}</strong><small>${text(rollingSeed42.factor_count, "--")} 因子</small></article>
        <article><span>Seed42 最大回撤</span><strong>${pct(rollingMetrics.max_drawdown, 2)}</strong><small>正式连续账户拼接</small></article>
      </div>
      <div class="rolling-decision-note ${rollingCampaignPassed ? "is-pass" : "is-fail"}">
        <strong>${rollingCampaignPassed ? "正式 Rolling 通过" : "正式 Rolling 未通过"}</strong>
        <span>${escapeHtml(rollingDecisionText(rollingCampaignForPanel))}<small>${escapeHtml(compactDateTime(rollingCampaignForPanel.completed_at))}</small></span>
      </div>
      <div class="rolling-gate-grid">${rollingGateCards || `<div class="empty-state">暂无 Rolling 门槛结果。</div>`}</div>
      <details class="seed-audit-panel">
        <summary><span>Seed 稳定性审计</span><strong>${rollingFinal.available ? (rollingCampaignPassed ? "通过" : "未通过") : "等待或仅初筛"}</strong><small>Seed42 是正式模型；Seed17/83 只保留审计指标</small></summary>
        <div class="table-shell compact-table">
          <table class="data-table">
            <thead><tr><th>Seed</th><th>年化</th><th>IR</th><th>回撤</th><th>诊断分</th><th>角色</th></tr></thead>
            <tbody>${rollingSeedAuditRows || `<tr><td colspan="6">暂无 Seed 审计证据。</td></tr>`}</tbody>
          </table>
        </div>
      </details>
      <div class="table-shell rolling-fold-table">
        <table class="data-table">
          <thead><tr><th>折</th><th>信号区间</th><th>年化</th><th>IR</th><th>最大回撤</th><th>折质量分</th></tr></thead>
          <tbody>${rollingFoldRows || `<tr><td colspan="6">暂无四折明细。</td></tr>`}</tbody>
        </table>
      </div>
      <div class="rolling-campaign-foot">
        <span>已执行 Seed：${escapeHtml((rollingCampaignForPanel.seeds || []).map((item) => item.seed).join(" / ") || "--")}</span>
        <span>正式展示：Seed42 · ${escapeHtml(text((rollingSeed42.folds || [])[0]?.signal_start || (rollingSeed42.folds || [])[0]?.segments?.test?.[0], "--"))} 至 ${escapeHtml(text((rollingSeed42.folds || []).at(-1)?.signal_end || (rollingSeed42.folds || []).at(-1)?.segments?.test?.[1], "--"))}</span>
        <span>策略：top${escapeHtml(text(rollingCampaignForPanel.portfolio?.topk, "--"))} / drop${escapeHtml(text(rollingCampaignForPanel.portfolio?.n_drop, "--"))} / hold${escapeHtml(text(rollingCampaignForPanel.portfolio?.hold_thresh, "--"))}</span>
        <span title="${escapeHtml(text(rollingCampaignForPanel.evidence_path, ""))}">证据：campaign.json</span>
      </div>
    </section>
  ` : `
    <section class="model-console-section model-rolling-campaign-panel">
      <div class="live-section-title"><h3>生产 Rolling · 四折拼接</h3><small>尚无正式 Rolling campaign</small></div>
      <div class="empty-state">研究模型可以先回测；只有启动生产 Rolling 后，这里才会显示四折、门槛和停止原因。</div>
    </section>
  `;
  document.getElementById("model-runtime-detail").innerHTML = `
    <section class="model-research-scene model-research-console-v3">
      ${stopBanner}
      ${uniqueDiagnosticWarnings.length && !stopState.active ? `<div class="warning-strip">${escapeHtml(uniqueDiagnosticWarnings.join("；"))}</div>` : ""}
      ${activeValuesReady ? "" : `<div class="warning-strip"><strong>${escapeHtml(activeValuesStatusText)}</strong><span>先刷新 active values，再冻结 feature set。</span>${activeValuesJobText ? `<small>${escapeHtml(activeValuesJobText)}</small>` : ""}</div>`}
      ${latestRunError ? `<div class="warning-strip">${escapeHtml(clip(latestRunError, 360))}</div>` : ""}
      ${encodingWarnings.length ? `<div class="warning-strip">检测到 ${escapeHtml(text(encodingWarnings.length))} 条历史中文文本编码损坏，主视图已过滤 dict/乱码文本。</div>` : ""}
      <section class="model-live-module" id="model-live-overview">
        ${modelLiveModuleHead("01 · LIVE OVERVIEW", "运行总览", "本次研究的运行态、执行对象与训练进度")}
        ${topStatusPanel}
      </section>
      ${modelParameterCapsulesPanel}
      <section class="model-console-section model-research-progress model-progress-combined" id="model-live-progress">
        <div class="live-section-title">
          <div><p class="eyebrow">03 · RESEARCH PROGRESS</p><h3>研究进展</h3></div>
        </div>
        ${modelLifecyclePanel}
        <div class="model-progress-layout">
          ${researchConclusionPanel}
          <section class="model-session-execution-log">
            <div class="model-session-execution-head">
              <div><span>本次研究记录</span><small>仅展示当前研究节点，按时间倒序</small></div>
              <small>${escapeHtml(`共 ${currentResearchTimeline.length} 条 · 滚动查看`)}</small>
            </div>
            <ol>${sessionExecutionRows || `<li class="is-empty">暂无 research step；正式研究节点会在这里按时间记录。</li>`}</ol>
          </section>
          <small class="model-live-log-hint">历史 session、完整 prompt 与参数依据请到“研究日志”查看。</small>
        </div>
      </section>
      ${candidateModelPanel}
    </section>
  `;
  document.querySelectorAll(".model-lifecycle-track").forEach((track) => {
    track.scrollLeft = 0;
  });
  const modelOrchTraceDetail = document.getElementById("model-orch-trace-detail");
  if (modelOrchTraceDetail) {
    modelOrchTraceDetail.innerHTML = modelOrchTraceWorkspace;
    modelOrchTraceDetail.querySelector("#model-log-session-select")?.addEventListener("change", (event) => {
      state.activeModelLogSessionId = event.target.value || "";
      state.activeModelOrchTraceId = "";
      localStorage.setItem("fxalpha.activeModelLogSessionId", state.activeModelLogSessionId);
      renderModelResearch();
    });
    modelOrchTraceDetail.querySelectorAll("[data-model-log-view]").forEach((button) => {
      button.addEventListener("click", () => {
        state.activeModelLogView = button.dataset.modelLogView || "interaction";
        localStorage.setItem("fxalpha.activeModelLogView", state.activeModelLogView);
        renderModelResearch();
      });
    });
    modelOrchTraceDetail.querySelectorAll("[data-model-orch-trace-id]").forEach((button) => {
      button.addEventListener("click", () => {
        state.activeModelOrchTraceId = button.dataset.modelOrchTraceId || "";
        renderModelResearch();
      });
    });
  }
  const stepCards = modelSteps.slice(0, 5).map((step) => {
    const legacyRecord = isLegacyResearchRecord(step);
    return `
    <article class="mini-card ${legacyRecord ? "is-legacy" : ""}">
      <span class="badge ${legacyRecord ? "danger" : "subtle"}">${escapeHtml(legacyRecord ? `历史旧流程 · ${text(step.stage)}` : text(step.stage))}</span>
      <strong>${escapeHtml(displayStepText(step, "summary", "暂无摘要"))}</strong>
      <p>${escapeHtml(displayStepText(step, "decision", ""))}</p>
      <small>${escapeHtml(displayStepText(step, "next", text(step.ts, "")))}</small>
    </article>
  `;
  }).join("");
  const renderContributionItems = (items) => (items || []).map((item) => `
    <div class="contribution-rank-row">
      <div class="contribution-symbol"><strong>${escapeHtml(text(item.symbol))}</strong><small>${escapeHtml(text(item.security_name, ""))}</small></div>
      <div class="contribution-value"><strong>${shortNumber(item.contribution, 0)}</strong><small>单日 ${shortNumber(item.max_daily_contribution, 0)} / ${shortNumber(item.min_daily_contribution, 0)}</small></div>
      <div><strong>${escapeHtml(text(item.holding_days, "0"))}</strong><small>持仓</small></div>
      <div><strong>${pct(item.max_weight, 1)}</strong><small>权重</small></div>
    </div>
  `).join("");
  const renderDailyTradeDetails = () => "";
  const exposureWidth = (value) => {
    const number = Number(value);
    if (!Number.isFinite(number)) return "0%";
    return `${Math.max(0, Math.min(100, number * 100)).toFixed(1)}%`;
  };
  const renderExposureFamily = ({ title, tone, note, top20, top50 }) => `
    <article class="model-exposure-family ${escapeHtml(tone || "")}">
      <div class="model-exposure-family-head">
        <strong>${escapeHtml(title)}</strong>
        <span>${escapeHtml(note)}</span>
      </div>
      <div class="model-exposure-bars">
        <div class="model-exposure-bar-row">
          <span>Top20</span>
          <div class="model-exposure-track"><i style="width:${exposureWidth(top20)}"></i></div>
          <b>${pct(top20, 1)}</b>
        </div>
        <div class="model-exposure-bar-row">
          <span>Top50</span>
          <div class="model-exposure-track"><i style="width:${exposureWidth(top50)}"></i></div>
          <b>${pct(top50, 1)}</b>
        </div>
      </div>
    </article>
  `;
  const renderModelExposureAudit = (validationPayload, exposurePayload = {}) => {
    const tradability = validationPayload.tradability_exposure || {};
    const style = validationPayload.model_style_exposure || {};
    const stPrediction = tradability.prediction || {};
    const styleTop20 = style.top20_prediction || style.top10_prediction || {};
    const styleTop50 = style.top50_prediction || {};
    const styleSummary = style.summary || {};
    const exposureAvailable = Boolean(exposurePayload.available || tradability.status || style.status);
    const sourceNote = text(
      exposurePayload.note,
      exposureAvailable ? "读取 validation audit；部分风格分位需要专门风格/基本面基准。" : "当前模型没有生成暴露诊断 artifact。"
    );
    return `
      <section class="model-exposure-audit">
        <div class="model-exposure-head">
          <h3>模型暴露诊断</h3>
          <div class="model-exposure-tags">
            <span>${escapeHtml(exposureAvailable ? "validation audit" : "未生成完整暴露")}</span>
            <span>${escapeHtml(text(style.status || tradability.status || validationPayload.status, "unknown"))}</span>
          </div>
        </div>
        <div class="model-exposure-family-grid">
          ${renderExposureFamily({
            title: "ST 暴露",
            tone: "danger",
            note: stPrediction.row_count ? `pred ${shortNumber(stPrediction.row_count, 0)} 行` : "pred.pkl 检查",
            top20: stPrediction.topk_avg_st_like_ratio ?? stPrediction.top20_avg_st_like_ratio ?? stPrediction.top10_avg_st_like_ratio,
            top50: stPrediction.top50_avg_st_like_ratio,
          })}
          ${renderExposureFamily({
            title: "小市值暴露",
            tone: "warning",
            note: "市值分位 <=20%",
            top20: styleTop20.avg_small_cap_ratio,
            top50: styleTop50.avg_small_cap_ratio,
          })}
          ${renderExposureFamily({
            title: "高成长暴露",
            tone: "neutral",
            note: "成长分位 >=80%",
            top20: styleTop20.avg_high_growth_ratio,
            top50: styleTop50.avg_high_growth_ratio,
          })}
          ${renderExposureFamily({
            title: "蓝筹暴露",
            tone: "positive",
            note: "大市值 + ROE",
            top20: styleTop20.avg_blue_chip_ratio,
            top50: styleTop50.avg_blue_chip_ratio,
          })}
        </div>
        <p class="model-exposure-note">
          ${escapeHtml(sourceNote)}
          ${styleSummary.score_std !== undefined ? ` 预测分布：mean ${escapeHtml(shortNumber(styleSummary.score_mean, 4))} · std ${escapeHtml(shortNumber(styleSummary.score_std, 4))} · p99abs ${escapeHtml(shortNumber(styleSummary.score_p99_abs, 4))}` : ""}
          ${stPrediction.unique_instruments !== undefined ? ` · 覆盖股票 ${escapeHtml(text(stPrediction.unique_instruments))}` : ""}
        </p>
      </section>
    `;
  };
  const backtestReady = Boolean(!state.modelBacktestLoading && backtestCurve.length && (backtestModel.model_id || backtestModel.model_run_id));
  const roundNoByRun = new Map((roundEvolution || [])
    .map((round) => [text(round.model_run_id, ""), round.round_no])
    .filter(([runId, roundNo]) => runId && roundNo !== undefined && roundNo !== null && roundNo !== ""));
  const backtestRecentModels = Array.isArray(backtest.recent_models) ? backtest.recent_models : [];
  const registryBacktestOptions = modelBacktestDropdownOptions(
    backtestRecentModels.length ? backtestRecentModels : models,
    serviceOutputs(state.modelRuns).runs || [],
    seedDiagnostics,
    roundNoByRun,
  );
  const rollingBacktestOptions = modelRollingBacktestOptions(rollingCampaigns);
  const backtestOptions = [...rollingBacktestOptions, ...registryBacktestOptions];
  const researchBacktestOptions = registryBacktestOptions.filter((option) => text(option.status, "").toLowerCase() === "research");
  const productionBacktestOptions = registryBacktestOptions.filter((option) => text(option.status, "").toLowerCase() === "production");
  const inferredBacktestCategory = state.modelBacktestSelection.role === "rolling_campaign" || backtestModel.role === "rolling_campaign"
    ? "rolling"
    : state.modelBacktestSelection.selector === "production" || text(backtestModel.status, "").toLowerCase() === "production"
      ? "production"
      : "research";
  const selectedCategory = ["research", "rolling", "production"].includes(state.modelBacktestCategory)
    ? state.modelBacktestCategory
    : inferredBacktestCategory;
  const backtestSort = ["time", "score", "annualized", "ir"].includes(state.modelBacktestSort)
    ? state.modelBacktestSort
    : "time";
  const backtestSortDirection = state.modelBacktestSortDirection === "asc" ? "asc" : "desc";
  const backtestSortLabels = {
    time: "时间",
    score: "综合评分",
    annualized: "年化收益",
    ir: "IR",
  };
  const rankedBacktestOptions = (options, category) => sortModelBacktestOptions(options, backtestSort, backtestSortDirection)
    .map((item, index) => ({ ...item, category, sortRank: index + 1 }));
  const categoryOptions = {
    research: rankedBacktestOptions(researchBacktestOptions, "research"),
    rolling: rankedBacktestOptions(rollingBacktestOptions, "rolling"),
    production: rankedBacktestOptions(productionBacktestOptions, "production"),
  };
  const visibleBacktestOptions = categoryOptions[selectedCategory];
  const selectedModelRunId = text(state.modelBacktestSelection.modelRunId || backtest.selection?.model_run_id, "");
  const resolvedBacktestRunId = text(backtest.selection?.model_run_id || backtestModel.model_run_id || selectedModelRunId, "");
  const selectedSeedRow = seedDiagnostics.find((item) => text(item.model_run_id, "") === resolvedBacktestRunId)
    || seedDiagnostics.find((item) => text(item.model_run_id, "") === selectedModelRunId);
  const selectedSeedBaseRunId = text(
    selectedSeedRow?.base_model_run_id
      || (selectedSeedRow?.is_base_seed ? selectedSeedRow.model_run_id : "")
      || backtestModel.base_model_run_id
      || "",
    ""
  );
  const seedPeerRows = selectedSeedBaseRunId
    ? seedDiagnostics
      .filter((item) => text(item.base_model_run_id, "") === selectedSeedBaseRunId || text(item.model_run_id, "") === selectedSeedBaseRunId)
      .sort((a, b) => {
        if (Boolean(a.is_base_seed) !== Boolean(b.is_base_seed)) return a.is_base_seed ? -1 : 1;
        return Number(a.seed ?? 0) - Number(b.seed ?? 0);
      })
    : [];
  const categoryLabel = { research: "研究模型", rolling: "Rolling 模型", production: "生产模型" };
  const backtestSortValueLabel = (option) => ({
    time: compactDateTime(option.completedAt) || "时间未记录",
    score: `评分 ${shortNumber(option.sortScore, 2)}`,
    annualized: `年化 ${pct(option.sortAnnualized, 1)}`,
    ir: `IR ${shortNumber(option.sortIr, 3)}`,
  }[backtestSort] || "");
  const renderBacktestMenuOption = (option) => {
    const chips = Array.isArray(option.chips) ? option.chips : [];
    const statusChip = chips[chips.length - 1] || "";
    return `
      <button
        class="backtest-menu-option ${selectedModelRunId === option.modelRunId ? "active" : ""}"
        data-model-backtest-id="${escapeHtml(text(option.modelId, ""))}"
        data-model-backtest-run-id="${escapeHtml(text(option.modelRunId, ""))}"
        data-model-backtest-label="${escapeHtml(text(option.label, "选中模型"))}"
        data-model-backtest-role="${escapeHtml(text(option.role, ""))}"
        data-model-backtest-category="${escapeHtml(text(option.category, ""))}"
        title="${escapeHtml(text(option.title, option.modelRunId))}"
      >
        <span class="backtest-menu-option-title">
          <b><i class="backtest-menu-option-rank">${escapeHtml(text(option.sortRank, "--"))}</i><span>${escapeHtml(text(option.label, option.modelRunId))}</span></b>
          <span class="backtest-menu-option-chiprow">${statusChip ? `<em>${escapeHtml(text(statusChip, ""))}</em>` : ""}</span>
        </span>
        <span class="backtest-menu-option-meta">
          <span class="backtest-menu-option-metrics">${escapeHtml(text(option.metrics, ""))}</span>
          <span class="backtest-menu-option-sort-value">${escapeHtml(backtestSortValueLabel(option))}</span>
        </span>
      </button>
    `;
  };
  const selectedOption = backtestOptions.find((option) =>
    selectedModelRunId
    && option.modelRunId === selectedModelRunId
    && (!state.modelBacktestSelection.role || option.role === state.modelBacktestSelection.role)
  ) || backtestOptions.find((option) => selectedModelRunId && option.modelRunId === selectedModelRunId);
  const selectedOptionLabel = text(
    selectedOption?.label,
    `选择${categoryLabel[selectedCategory]}`
  );
  const renderBacktestToolbar = () => `
    <div class="backtest-toolbar">
      <div>
        <span class="detail-label">回测对象</span>
        <strong>${escapeHtml(categoryLabel[selectedCategory])}</strong>
      </div>
      <div class="backtest-control-stack">
        <div class="backtest-selector-row">
          ${["research", "rolling", "production"].map((category) => `
            <button class="backtest-selector ${selectedCategory === category ? "active" : ""}" data-backtest-category="${category}">
              <span>${categoryLabel[category]}</span>
              <small>${escapeHtml(`${categoryOptions[category].length} 个可选`)}</small>
            </button>
          `).join("")}
        </div>
        <div class="backtest-select-panel">
          <span>选择${escapeHtml(categoryLabel[selectedCategory])}</span>
          <div class="backtest-model-menu">
            <button class="backtest-model-menu-trigger" type="button" aria-expanded="false">
              ${escapeHtml(selectedOptionLabel)}
            </button>
            <div class="backtest-menu-popover" role="menu">
              <div class="backtest-menu-sort-row" aria-label="模型排序">
                <span>排序</span>
                ${[
                  ["time", "时间"],
                  ["score", "综合评分"],
                  ["annualized", "年化收益"],
                  ["ir", "IR"],
                ].map(([key, label]) => `<button class="tiny-button ${backtestSort === key ? "active" : ""}" type="button" data-backtest-sort="${key}" aria-pressed="${backtestSort === key}" title="按${label}${backtestSort === key && backtestSortDirection === "asc" ? "升序" : "降序"}排列">${label}${backtestSort === key ? `<b aria-hidden="true">${backtestSortDirection === "asc" ? "↑" : "↓"}</b>` : ""}</button>`).join("")}
                <strong class="backtest-menu-sort-status" aria-live="polite">${escapeHtml(backtestSortLabels[backtestSort])} · ${backtestSortDirection === "asc" ? "低到高" : "高到低"}</strong>
              </div>
              ${visibleBacktestOptions.map(renderBacktestMenuOption).join("") || `<div class="empty-state">暂无可选择模型。</div>`}
            </div>
          </div>
        </div>
      </div>
    </div>
  `;
  const renderSeedPeerBacktests = () => {
    const confirmationSeeds = Array.isArray(researchConfirmation.seed_results) ? researchConfirmation.seed_results : [];
    const auditRows = confirmationSeeds.length ? confirmationSeeds : seedPeerRows.map((item) => ({
      seed: item.seed,
      research_score: item.research_score ?? item.score?.research_score,
      excess_annualized_ret_with_cost: item.excess_annualized_ret_with_cost ?? item.annualized_ret,
      excess_information_ratio_with_cost: item.excess_information_ratio_with_cost ?? item.sharpe,
      abs_max_drawdown: Math.abs(Number(item.max_drawdown) || 0),
    }));
    if (!auditRows.length || auditRows.length < 2) return "";
    const rows = auditRows.map((item) => {
      const seedNumber = Number(item.seed);
      return `
        <tr>
          <td><strong>Seed${escapeHtml(text(seedNumber, "--"))}</strong><small>${seedNumber === 42 ? "正式模型" : "稳定性审计"}</small></td>
          <td>${pct(item.excess_annualized_ret_with_cost, 2)}</td>
          <td>${shortNumber(item.excess_information_ratio_with_cost, 3)}</td>
          <td>${pct(-(Number(item.abs_max_drawdown) || 0), 2)}</td>
          <td>${shortNumber(item.research_score, 2)}</td>
          <td><span class="badge ${seedNumber === 42 ? "ok" : "subtle"}">${seedNumber === 42 ? "正式展示" : "仅审计"}</span></td>
        </tr>
      `;
    }).join("");
    return `
      <details class="seed-audit-panel">
        <summary><span>Seed 稳定性审计</span><strong>${escapeHtml(text(researchConfirmation.status, "已记录"))}</strong><small>正式模型固定 Seed42；Seed17/83 不参与模型排名</small></summary>
        <div class="table-shell compact-table">
          <table class="data-table model-registry-table">
            <thead><tr><th>Seed</th><th>年化</th><th>IR</th><th>回撤</th><th>研究分</th><th>角色</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </details>
    `;
  };
  const libraryRows = models.map((item) => {
    const review = modelLibraryReviewMeta(item);
    const isSelected = text(item.model_id, "") === text(state.modelBacktestSelection.modelId, "")
      || text(item.model_run_id, "") === text(state.modelBacktestSelection.modelRunId, "");
    const libraryRoundLabel = modelBacktestRoundLabel(item, roundNoByRun);
    const displayName = canonicalModelDisplayName(item, {
      kind: text(item.role, "") === "rolling_campaign" ? "rolling" : "model",
      roundNo: libraryRoundLabel ? libraryRoundLabel.replace(/^R/, "") : null,
    });
    const displaySubtitle = text(item.display_subtitle, text(item.model_run_id, ""));
    const productionEvidence = ["candidate", "production"].includes(text(item.status, "").toLowerCase())
      || text(item.evaluation_mode || item.metadata?.evaluation_mode, "").toLowerCase() === "production";
    const primaryScore = productionEvidence
      ? (item.rolling_score ?? item.metadata?.rolling_score)
      : (item.research_score ?? item.metadata?.research_score ?? item.confirmed_research_score ?? item.metadata?.confirmed_research_score);
    return `
      <tr class="${isSelected ? "selected-row" : ""}">
        <td><strong>${escapeHtml(displayName)}</strong><small>${escapeHtml(displaySubtitle)}</small></td>
        <td><span class="badge subtle">${escapeHtml(text(item.role || item.status, "candidate"))}</span></td>
        <td>${escapeHtml(displayModelIdentifier(item.feature_set_id, ""))}<small>${escapeHtml(text(item.factor_count, "0"))} factors</small></td>
        <td>${shortNumber(primaryScore, 2)}<small>${productionEvidence ? "Rolling 准入分" : "Seed42 研究分"}</small></td>
        <td><span class="badge ${escapeHtml(validationBadgeClass(review.reviewStatus))}">${escapeHtml(review.reviewLabel)}</span><small>${escapeHtml(text((item.risk_flags || item.review_flags || []).slice(0, 2).join("、"), ""))}</small></td>
        <td>${pct(item.excess_annualized_ret_with_cost ?? item.annualized_ret, 1)}<small>IR ${shortNumber(item.excess_information_ratio_with_cost ?? item.sharpe, 3)} · DD ${pct(item.max_drawdown, 1)}</small></td>
        <td><button class="ghost small" data-model-backtest-id="${escapeHtml(text(item.model_id))}" data-model-backtest-run-id="${escapeHtml(text(item.model_run_id, ""))}" data-model-backtest-label="${escapeHtml(displayName)}">展开回测</button></td>
      </tr>
    `;
  }).join("");
  const seedDiagnosticRows = seedDiagnostics.map((item) => {
    const seedNumber = Number(item.seed ?? item.metadata?.seed);
    const seedLabel = seedNumber === 42 ? "正式模型" : "稳定性审计";
    const displayModelId = text(item.display_name || item.display_model_id || item.model_id, item.model_run_id);
    const displaySubtitle = text(item.display_subtitle, `${seedLabel} · ${text(item.model_run_id, "")}`);
    return `
      <tr>
        <td><strong>${escapeHtml(displayModelId)}</strong><small>${escapeHtml(displaySubtitle)}</small></td>
        <td><span class="badge ${seedNumber === 42 ? "ok" : "subtle"}">${escapeHtml(seedLabel)}</span><small>Seed${escapeHtml(text(seedNumber, "--"))}</small></td>
        <td>${pct(item.excess_annualized_ret_with_cost ?? item.annualized_ret, 1)}<small>IR ${shortNumber(item.excess_information_ratio_with_cost ?? item.sharpe, 3)} · DD ${pct(item.max_drawdown, 1)}</small></td>
        <td>${shortNumber(item.rank_ic, 4)}<small>ICIR ${shortNumber(item.rank_icir, 3)}</small></td>
        <td>${item.is_base_seed ? "100.0%" : pct(item.top10_overlap_mean, 1)}<small>corr ${shortNumber(item.pred_rank_corr_mean, 3)}</small></td>
      </tr>
    `;
  }).join("");
  document.getElementById("model-records-detail").innerHTML = `
    <div class="model-library-inline-head">
      <div>
        <span class="detail-label">模型库</span>
        <strong>${escapeHtml(text(registry.count || models.length, "0"))} 个 research / candidate / production</strong>
        <p>模型库只保存模型 metadata、metrics、status 和路径引用；原始因子 parquet 与 Qlib artifacts 通过 manifest 审计，不复制进 registry。</p>
      </div>
      <button class="ghost small" data-panel-target="model-library">打开完整模型库</button>
    </div>
    <div class="table-shell">
      <table class="data-table model-registry-table">
        <thead><tr><th>模型</th><th>角色</th><th>Feature Set</th><th>研究 / 准入分</th><th>Review</th><th>Seed42 核心指标</th><th>操作</th></tr></thead>
        <tbody>${libraryRows || `<tr><td colspan="7">暂无 research / candidate / production 模型。</td></tr>`}</tbody>
      </table>
    </div>
    <details class="seed-audit-panel">
      <summary><span>Seed 审计证据</span><strong>${escapeHtml(text(seedDiagnostics.length, "0"))} 条</strong><small>Seed17/83 不作为正式模型列出</small></summary>
      <div class="table-shell compact-table">
        <table class="data-table model-registry-table">
          <thead><tr><th>执行记录</th><th>角色</th><th>收益 / IR</th><th>Rank IC</th><th>一致性</th></tr></thead>
          <tbody>${seedDiagnosticRows || `<tr><td colspan="5">暂无 Seed 审计证据。</td></tr>`}</tbody>
        </table>
      </div>
    </details>
    <div class="model-research-log-split">
      <div>
        <span class="detail-label">最近 Session</span>
        <div class="table-shell compact-table">
          <table class="data-table">
            <thead><tr><th>Session</th><th>阶段</th><th>轮次</th><th>更新时间</th></tr></thead>
            <tbody>${mcpSessions.slice(0, 4).map((item) => `
              <tr><td>${escapeHtml(text(item.session_id))}</td><td>${escapeHtml(text(item.stage))}</td><td>${escapeHtml(text(item.current_round, "0"))}</td><td>${escapeHtml(text(item.updated_at))}</td></tr>
            `).join("") || `<tr><td colspan="4">暂无 session。</td></tr>`}</tbody>
          </table>
        </div>
      </div>
      <div>
        <span class="detail-label">最近研究叙事</span>
        <div class="card-grid">${stepCards || `<div class="empty-state">暂无模型 LLM 输出摘要。</div>`}</div>
      </div>
    </div>
  `;
  const backtestDetailNode = document.getElementById("model-backtest-detail");
  const backtestRenderSignature = JSON.stringify({
    loading: Boolean(state.modelBacktestLoading),
    url: state.modelBacktestLastUrl || modelBacktestUrl({ includeDaily: true }),
    category: selectedCategory,
    selectedModelId: text(state.modelBacktestSelection.modelId, ""),
    selectedModelRunId,
    rollingDaily: state.modelBacktestSelection.rollingDaily === true,
    resolvedBacktestRunId,
    backtestReady,
    curveLength: backtestCurve.length,
    curveLastDate: text(backtestLast.date || backtestLast.datetime || ""),
    validationStatus: text((backtest.validation || {}).status, ""),
    recentModelCount: Array.isArray(backtest.recent_models) ? backtest.recent_models.length : 0,
    recentModelHead: text((Array.isArray(backtest.recent_models) ? backtest.recent_models[0] : {})?.model_run_id, ""),
    registryCount: models.length,
    seedDiagnosticCount: seedDiagnostics.length,
  });
  if (
    modelBacktestWorkspaceIsVisible()
    && backtestDetailNode?.dataset.renderSignature === backtestRenderSignature
    && backtestDetailNode.innerHTML
  ) {
    return;
  }
  const setBacktestDetailHtml = (html) => {
    if (!backtestDetailNode) return;
    backtestDetailNode.innerHTML = html;
    backtestDetailNode.dataset.renderSignature = backtestRenderSignature;
  };
  if (!backtestReady) {
    const backtestError = state.modelBacktestLoading
      ? "正在读取当前选择模型的 Qlib 回测曲线。"
      : state.modelBacktest?.err || state.modelBacktest?.error || "当前选择的模型尚未加载回测曲线。";
    setBacktestDetailHtml(`
      ${renderBacktestToolbar()}
      <div class="empty-state backtest-empty-state">
        <strong>${escapeHtml(state.modelBacktestLoading ? "回测读取中" : "当前对象暂无回测结果")}</strong>
        <p>${escapeHtml(text(backtestError))}</p>
        <small>切换回测对象时才会重新读取 ret.pkl 和每日持仓明细；研究现场刷新不会重复拉取回测。</small>
      </div>
    `);
    return;
  }
  const strategyAnnualized = backtestMetrics.strategy_annualized_ret ?? backtestModel.strategy_annualized_ret;
  const excessAnnualized = backtestMetrics.excess_annualized_ret_with_cost ?? backtestMetrics.annualized_ret ?? backtestModel.excess_annualized_ret_with_cost ?? backtestModel.annualized_ret;
  const excessIr = backtestMetrics.excess_information_ratio_with_cost ?? backtestMetrics.sharpe ?? backtestModel.excess_information_ratio_with_cost ?? backtestModel.sharpe;
  const strategySharpe = backtestMetrics.strategy_sharpe ?? backtestModel.strategy_sharpe;
  const maxDrawdown = backtestMetrics.max_drawdown ?? backtestModel.max_drawdown;
  const navMaxDrawdown = backtestMetrics.nav_max_drawdown;
  const grossAnnualized = backtestMetrics.gross_strategy_annualized_ret ?? backtestModel.gross_strategy_annualized_ret;
  const benchmarkAnnualized = backtestMetrics.benchmark_annualized_ret
    ?? (Number.isFinite(Number(strategyAnnualized)) && Number.isFinite(Number(excessAnnualized))
      ? Number(strategyAnnualized) - Number(excessAnnualized)
      : null);
  const netCumulative = backtestMetrics.net_cumulative_return ?? backtestLast.net_strategy_cumulative_return ?? backtestLast.model_return;
  const grossCumulative = backtestMetrics.gross_cumulative_return ?? backtestLast.gross_strategy_cumulative_return;
  const benchmarkCumulative = backtestMetrics.benchmark_cumulative_return ?? backtestLast.benchmark_return;
  const relativeCumulative = backtestMetrics.relative_cumulative_return ?? backtestLast.relative_cumulative_return ?? backtestLast.excess_return;
  const netValueGap = backtestMetrics.net_value_gap ?? backtestLast.net_value_gap;
  const cumulativeCostDrag = backtestMetrics.cost_drag_cumulative ?? backtestLast.cost_drag_cumulative;
  const averageTurnover = backtestMetrics.turnover ?? backtestModel.turnover;
  const averageCost = backtestMetrics.avg_cost ?? backtestModel.avg_cost;
  const totalCostCumulative = backtestLast.total_cost_cumulative;
  const rankIc = backtestMetrics.rank_ic ?? backtestModel.rank_ic;
  const rankIcir = backtestMetrics.rank_icir ?? backtestModel.rank_icir;
  const backtestDisplayName = text(backtestModel.display_name, text(backtestModel.model_id, backtestModel.model_run_id));
  const backtestCompactNameBase = text(
    selectedOption?.label,
    text(backtestModel.display_label, backtestDisplayName)
  );
  const isRollingBacktest = backtestModel.role === "rolling_campaign";
  const backtestSeedLabel = isRollingBacktest ? "正式曲线：Seed42 四折拼接" : "正式 Seed42";
  const backtestCompactName = backtestCompactNameBase;
  const stockNameCache = stockContribution.diagnostics?.security_name_cache || {};
  const stockNameSource = stockContribution.diagnostics?.security_name_source === "production_stock_identity_cache"
    ? "生产证券主数据缓存"
    : text(stockContribution.diagnostics?.security_name_source, "未确认");
  const validation = backtest.validation || {};
  const exposure = backtest.exposure || {};
  const validationStatus = text(validation.status, "unknown");
  const backtestLoadWarning = text(backtest.load_warning, "");
  const validationWarning = validationStatus === "blocked"
    ? `模型检验阻断：${(validation.hard_blocks || []).join("、") || "未通过检验"}。该模型不应进入 Rolling candidate / production。`
    : validationStatus === "review_required"
      ? `模型检验提示：无硬阻断，但存在 ${((validation.warnings || []).join("、") || "待复核项")}，晋升前需要研究记录说明。`
      : "";
  const backtestMetadata = backtestModel.metadata || {};
  const researchConfirmation = backtestModel.research_confirmation || backtestMetadata.research_confirmation || {};
  const rollingGates = backtestModel.rolling_gates || backtestMetadata.rolling_gates || {};
  const productionRefit = backtest.production_refit || backtestModel.production_refit || {};
  const portfolioContract = backtest.portfolio_contract || {};
  const portfolioLabel = `top${text(portfolioContract.topk, "20")}/drop${text(portfolioContract.n_drop, "2")}/hold${text(portfolioContract.hold_thresh, "5")}`;
  const modelEvaluationScore = isRollingBacktest
    ? (backtestModel.rolling_score ?? backtestMetadata.rolling_score)
    : (backtestModel.research_score ?? backtestMetadata.research_score
      ?? backtestModel.confirmed_research_score ?? backtestMetadata.confirmed_research_score);
  const modelEvaluationLabel = isRollingBacktest
    ? (backtest.rolling_campaign?.final?.available ? "Rolling 准入分" : "Seed42 初筛分")
    : "研究分";
  const backtestFeatureCount = backtest.feature_count
    ?? backtestModel.feature_count
    ?? backtestModel.factor_count;
  const backtestAssetLabel = isRollingBacktest ? "Rolling 验证" : "研究模型";
  const selectedRollingCampaign = isRollingBacktest ? (backtest.rolling_campaign || {}) : {};
  const selectedRollingSeeds = Array.isArray(selectedRollingCampaign.seeds) ? selectedRollingCampaign.seeds : [];
  const selectedRollingSeed = selectedRollingSeeds.find((item) => Number(item.seed) === Number(backtestModel.seed ?? 42))
    || selectedRollingSeeds.find((item) => Number(item.seed) === 42)
    || selectedRollingSeeds[0]
    || {};
  const selectedRollingFolds = Array.isArray(selectedRollingSeed.folds) ? selectedRollingSeed.folds : [];
  const selectedRollingFinal = selectedRollingCampaign.final || {};
  const selectedRollingPreliminary = selectedRollingCampaign.preliminary || {};
  const selectedRollingScoreDetail = selectedRollingFinal.available
    ? selectedRollingFinal
    : (selectedRollingPreliminary.score || {});
  const selectedRollingScoreComponents = selectedRollingScoreDetail.components || {};
  const selectedRollingGates = selectedRollingFinal.available
    ? (selectedRollingFinal.gates || {})
    : (selectedRollingPreliminary.gates || rollingGates);
  const rollingExecutedSeeds = selectedRollingSeeds.map((item) => Number(item.seed)).filter(Number.isFinite);
  const rollingDecision = text(selectedRollingCampaign.decision, "pending");
  const rollingCurveScope = backtestModel.role === "rolling_campaign" ? `
    <div class="rolling-curve-scope">
      <strong>正式曲线：Seed42 四折连续拼接</strong>
      <span>${escapeHtml(text(backtest.period?.start, "--"))} 至 ${escapeHtml(text(backtest.period?.end, "--"))} · Seed17/83 只做审计</span>
    </div>
  ` : "";
  const rollingDailyAvailable = backtest.daily_breakdown?.available === true;
  const rollingDailyControl = backtestModel.role === "rolling_campaign" ? `
    <div class="rolling-daily-control ${rollingDailyAvailable ? "is-loaded" : ""}">
      <div>
        <strong>${rollingDailyAvailable ? "Seed42 逐日明细已加载" : "Seed42 逐日持仓与贡献默认不加载"}</strong>
        <span>${rollingDailyAvailable ? "悬停曲线交易日即可查看正式模型的持仓、交易与个股贡献。" : "四折净值已经完整；按需加载只读取正式 Seed42 明细。"}</span>
      </div>
      ${rollingDailyAvailable ? `<span class="badge ok">已加载</span>` : `<button type="button" class="ghost small" data-rolling-daily-load>加载 Seed42 逐日明细</button>`}
    </div>
  ` : "";
  const renderEvaluationKpis = () => {
    const rows = [];
    if (researchConfirmation.status) {
      const confirmationPassed = researchConfirmation.status === "passed";
      rows.push(`<div class="backtest-kpi forward ${confirmationPassed ? "pass" : "watch"}" title="跨随机种子复核 · 参考分 ${escapeHtml(shortNumber(researchConfirmation.confirmed_research_score, 1))}"><span>稳定性审计</span><strong>${confirmationPassed ? "通过" : "待复核"}</strong></div>`);
    }
    if (Object.keys(rollingGates).length) {
      const rollingPassed = Object.values(rollingGates).every(Boolean);
      rows.push(`<div class="backtest-kpi forward ${rollingPassed ? "pass" : "reject"}"><span>Rolling Gate</span><strong>${rollingPassed ? "passed" : "failed"}</strong><small>四折拼接 · Top20/Drop2</small></div>`);
    }
    return rows.join("");
  };
  const renderPerformanceMetricSections = () => {
    const annualizedCostDrag = Number.isFinite(Number(grossAnnualized)) && Number.isFinite(Number(strategyAnnualized))
      ? Number(grossAnnualized) - Number(strategyAnnualized)
      : null;
    return `
      <section class="backtest-metric-section">
        <div class="backtest-metric-section-head">
          <div><span class="detail-label">COMPOUNDED NAV</span><h3>真实净值表现</h3></div>
          <p>复利净值口径；默认策略收益已经扣除交易成本。</p>
        </div>
        <div class="backtest-metric-grid">
          <article class="backtest-metric-card primary"><span>测试期净累计收益</span><strong>${pct(netCumulative, 2)}</strong><small>∏(1 + 每日净收益) − 1</small></article>
          <article class="backtest-metric-card"><span>净策略年化收益</span><strong>${pct(strategyAnnualized, 2)}</strong><small>Qlib 日均净收益 × 238</small></article>
          <article class="backtest-metric-card"><span>净策略 Sharpe</span><strong>${shortNumber(strategySharpe, 3)}</strong><small>净收益 · 238 日折算</small></article>
          <article class="backtest-metric-card"><span>净值最大回撤</span><strong>${pct(navMaxDrawdown, 2)}</strong><small>复利净值峰谷回撤</small></article>
        </div>
      </section>
      <section class="backtest-metric-section">
        <div class="backtest-metric-section-head">
          <div><span class="detail-label">RELATIVE TO BENCHMARK</span><h3>相对基准表现</h3></div>
          <p>年化采用Qlib算术口径；累计采用复利净值口径，二者不可直接互相换算。</p>
        </div>
        <div class="backtest-metric-grid relative-grid">
          <article class="backtest-metric-card"><span>基准累计收益</span><strong>${pct(benchmarkCumulative, 2)}</strong><small>基准复利净值</small></article>
          <article class="backtest-metric-card"><span>基准年化收益</span><strong>${pct(benchmarkAnnualized, 2)}</strong><small>总年化 − 成本后超额年化</small></article>
          <article class="backtest-metric-card primary"><span>相对基准累计收益</span><strong>${pct(relativeCumulative, 2)}</strong><small>策略净值 ÷ 基准净值 − 1</small></article>
          <article class="backtest-metric-card"><span>净值差</span><strong>${percentagePoints(netValueGap, 2)}</strong><small>策略净累计 − 基准累计</small></article>
        </div>
        <div class="metric-relation-note">
          <strong>口径关系</strong>
          <span>净策略年化 ${pct(strategyAnnualized, 2)} = 基准年化 ${pct(benchmarkAnnualized, 2)} + 成本后超额年化 ${pct(excessAnnualized, 2)}；累计收益来自逐日复利，不用年化值倒推。</span>
        </div>
      </section>
      <section class="backtest-metric-section cost-section">
        <div class="backtest-metric-section-head">
          <div><span class="detail-label">COST &amp; TURNOVER</span><h3>交易成本与执行</h3></div>
          <p>毛收益只用于解释成本拖累，不作为默认策略曲线。</p>
        </div>
        <div class="backtest-metric-grid cost-grid">
          <article class="backtest-metric-card"><span>毛累计收益</span><strong>${pct(grossCumulative, 2)}</strong><small>扣费前复利净值</small></article>
          <article class="backtest-metric-card"><span>净累计收益</span><strong>${pct(netCumulative, 2)}</strong><small>扣费后复利净值</small></article>
          <article class="backtest-metric-card"><span>累计成本拖累</span><strong>${percentagePoints(cumulativeCostDrag, 2)}</strong><small>毛累计 − 净累计</small></article>
          <article class="backtest-metric-card"><span>年化成本拖累</span><strong>${percentagePoints(annualizedCostDrag, 2)}</strong><small>毛年化 − 净年化</small></article>
          <article class="backtest-metric-card"><span>平均换手率</span><strong>${pct(averageTurnover, 2)}</strong><small>每日 portfolio turnover</small></article>
          <article class="backtest-metric-card"><span>平均每日成本</span><strong>${pct(averageCost, 4)}</strong><small>${totalCostCumulative !== undefined && totalCostCumulative !== null ? `累计成本 ${moneyNumber(totalCostCumulative)}` : "成本比例"}</small></article>
        </div>
      </section>
      ${Number.isFinite(Number(rankIc)) || Number.isFinite(Number(rankIcir)) ? `
        <section class="backtest-metric-section compact-section">
          <div class="backtest-metric-section-head">
            <div><span class="detail-label">PREDICTION QUALITY</span><h3>预测质量</h3></div>
            <p>衡量模型排序预测能力，不与策略累计收益混算。</p>
          </div>
          <div class="backtest-metric-grid prediction-grid">
            <article class="backtest-metric-card"><span>Rank IC</span><strong>${shortNumber(rankIc, 4)}</strong><small>每日截面秩相关均值</small></article>
            <article class="backtest-metric-card"><span>Rank ICIR</span><strong>${shortNumber(rankIcir, 4)}</strong><small>Rank IC 稳定性</small></article>
          </div>
        </section>
      ` : ""}
    `;
  };
  const renderRollingBacktestAudit = () => {
    if (!isRollingBacktest) return "";
    const scoreOverall = selectedRollingScoreDetail.overall?.score ?? selectedRollingScoreComponents.overall_median;
    const scoreWorst = selectedRollingScoreDetail.worst_fold?.score ?? selectedRollingScoreComponents.worst_fold_median;
    const scoreLatest = selectedRollingScoreDetail.latest_fold?.score ?? selectedRollingScoreComponents.latest_fold_median;
    const gateEntries = Object.entries(selectedRollingGates || {});
    const gatesPassed = gateEntries.length > 0 && gateEntries.every(([, passed]) => Boolean(passed));
    const foldRows = selectedRollingFolds.map((fold, index) => `
      <tr class="${Number(fold.information_ratio) <= 0 || Number(fold.quality_score) < 50 ? "danger-row" : ""}">
        <td><strong>第 ${index + 1} 折</strong><small>${escapeHtml(text(fold.fold_id, "--"))}</small></td>
        <td>${escapeHtml(text(fold.signal_start, "--"))}<small>至 ${escapeHtml(text(fold.signal_end, "--"))}</small></td>
        <td><strong>${pct(fold.annualized_ret, 2)}</strong></td>
        <td><strong>${shortNumber(fold.information_ratio, 3)}</strong></td>
        <td>${pct(fold.max_drawdown, 2)}</td>
        <td><strong>${shortNumber(fold.quality_score, 2)}</strong><small>${text(fold.report_days, "--")} 日</small></td>
      </tr>
    `).join("");
    const gateCards = gateEntries.map(([key, passed]) => `
      <article class="rolling-gate-card ${passed ? "is-pass" : "is-fail"}">
        <span>${escapeHtml(rollingGateLabels[key] || key)}</span>
        <strong>${passed ? "通过" : "未通过"}</strong>
      </article>
    `).join("");
    const decisionLabel = selectedRollingFinal.available ? "三 Seed 正式准入" : "Seed42 四折初筛";
    return `
      <section class="backtest-metric-section rolling-backtest-audit">
        <div class="backtest-metric-section-head">
          <div><span class="detail-label">ROLLING VALIDATION</span><h3>Rolling 验证诊断</h3></div>
          <span class="badge ${gatesPassed ? "ok" : "warn"}">${escapeHtml(decisionLabel)} · ${escapeHtml(rollingDecision)}</span>
        </div>
        <div class="backtest-metric-grid rolling-score-grid">
          <article class="backtest-metric-card primary"><span>${escapeHtml(modelEvaluationLabel)}</span><strong>${shortNumber(modelEvaluationScore, 2)}</strong><small>${selectedRollingFinal.available ? "三 Seed 正式评分" : "只决定是否继续 Seed17/83"}</small></article>
          <article class="backtest-metric-card"><span>全区间质量分</span><strong>${shortNumber(scoreOverall, 2)}</strong><small>四折连续拼接</small></article>
          <article class="backtest-metric-card"><span>最差折质量分</span><strong>${shortNumber(scoreWorst, 2)}</strong><small>控制局部失效风险</small></article>
          <article class="backtest-metric-card"><span>最新折质量分</span><strong>${shortNumber(scoreLatest, 2)}</strong><small>反映近期有效性</small></article>
          <article class="backtest-metric-card"><span>折覆盖</span><strong>${selectedRollingFolds.length} / 4</strong><small>连续 Walk-forward</small></article>
          <article class="backtest-metric-card"><span>Seed 覆盖</span><strong>${rollingExecutedSeeds.length} / 3</strong><small>${escapeHtml(rollingExecutedSeeds.join(" / ") || "尚未执行")}</small></article>
        </div>
        <div class="rolling-decision-note ${gatesPassed ? "is-pass" : "is-fail"}">
          <strong>${gatesPassed ? "当前门槛通过" : "当前门槛未通过"}</strong>
          <span>${escapeHtml(rollingDecisionText(selectedRollingCampaign))}</span>
        </div>
        <div class="rolling-gate-grid">${gateCards || `<div class="empty-state">暂无 Rolling Gate 结果。</div>`}</div>
        <div class="table-shell rolling-fold-table">
          <table class="data-table">
            <thead><tr><th>折</th><th>信号区间</th><th>成本后超额年化</th><th>成本后超额 IR</th><th>Qlib 最大回撤</th><th>折质量分</th></tr></thead>
            <tbody>${foldRows || `<tr><td colspan="6">暂无四折明细。</td></tr>`}</tbody>
          </table>
        </div>
      </section>
    `;
  };
  setBacktestDetailHtml(`
    ${renderBacktestToolbar()}
    ${backtestLoadWarning ? `<div class="warning-strip">${escapeHtml(backtestLoadWarning)}</div>` : ""}
    ${validationWarning ? `<div class="warning-strip">${escapeHtml(validationWarning)}</div>` : ""}
    <div class="backtest-result-head">
      <div class="backtest-identity">
        <div class="backtest-identity-top"><span class="detail-label">模型信息</span><span class="backtest-identity-chip">${escapeHtml(backtestAssetLabel)}</span></div>
        <strong title="${escapeHtml(backtestDisplayName)}">${escapeHtml(backtestCompactName)}</strong>
        <div class="backtest-identity-meta">
          <span>${escapeHtml(text(backtest.period?.start))} 至 ${escapeHtml(text(backtest.period?.end))}</span>
          <span>${escapeHtml(portfolioLabel)} · ${escapeHtml(text(portfolioContract.benchmark, "000300sh"))}</span>
          <span title="${escapeHtml(text(backtest.feature_set_id || backtestModel.feature_set_id, ""))}">${escapeHtml(Number.isFinite(Number(backtestFeatureCount)) ? `${backtestFeatureCount} 因子快照` : "因子快照")}</span>
        </div>
      </div>
      <div class="backtest-kpi-grid">
        <div class="backtest-kpi primary"><span>成本后超额年化</span><strong>${pct(excessAnnualized, 2)}</strong><small>Qlib 日均超额 × 238</small></div>
        <div class="backtest-kpi"><span>成本后超额 IR</span><strong>${shortNumber(excessIr, 3)}</strong><small>成本后风险调整超额</small></div>
        <div class="backtest-kpi"><span>${escapeHtml(modelEvaluationLabel)}</span><strong>${shortNumber(modelEvaluationScore, 2)}</strong><small>${isRollingBacktest ? "Rolling 验证评分" : "研究筛选评分"}</small></div>
        <div class="backtest-kpi secondary"><span>Qlib 最大回撤</span><strong>${pct(maxDrawdown, 2)}</strong><small>准入评分沿用口径</small></div>
        ${isRollingBacktest ? "" : renderEvaluationKpis()}
      </div>
    </div>
    ${renderPerformanceMetricSections()}
    ${renderRollingBacktestAudit()}
    ${isRollingBacktest ? "" : renderModelExposureAudit(validation, exposure)}
    ${isRollingBacktest ? "" : renderSeedPeerBacktests()}
    ${rollingCurveScope}
    ${renderBacktestCurveChart(backtestCurve, { folds: selectedRollingFolds })}
    ${rollingDailyControl}
    ${renderDailyTradeDetails()}
    ${stockContribution.available ? `
      <section class="contribution-board">
        <div class="contribution-summary-panel">
          <div class="contribution-summary-head">
            <div>
              <span class="detail-label">个股贡献摘要</span>
              <strong>测试期持仓贡献拆解</strong>
            </div>
            <div class="contribution-source">
              <span>${escapeHtml(stockNameSource)}</span>
              <span>${escapeHtml(text(stockNameCache.record_count, "0"))} 条主数据</span>
            </div>
          </div>
          <div class="contribution-kpi-grid">
            <div class="contribution-kpi positive"><span>正贡献合计</span><strong>${shortNumber(stockContribution.concentration?.positive_total, 0)}</strong></div>
            <div class="contribution-kpi negative"><span>负贡献合计</span><strong>${shortNumber(stockContribution.concentration?.negative_total, 0)}</strong></div>
            <div class="contribution-kpi"><span>Top3 正贡献占比</span><strong>${pct(stockContribution.concentration?.top3_positive_share, 1)}</strong></div>
            <div class="contribution-kpi"><span>覆盖股票数</span><strong>${escapeHtml(text(stockContribution.concentration?.stock_count, "0"))}</strong></div>
          </div>
        </div>
        <div class="contribution-split-grid">
          <article class="contribution-rank-panel contribution-profit-panel">
            <div class="contribution-rank-head">
              <span class="detail-label">盈利贡献</span>
              <strong>Top 10</strong>
            </div>
            <div class="contribution-rank-list">
              <div class="contribution-rank-row head"><span>股票</span><span>贡献</span><span>持仓</span><span>权重</span></div>
              ${renderContributionItems(stockContribution.top_winners) || `<div class="empty-state">暂无盈利贡献。</div>`}
            </div>
          </article>
          <article class="contribution-rank-panel contribution-loss-panel">
            <div class="contribution-rank-head">
              <span class="detail-label">亏损贡献</span>
              <strong>Top 10</strong>
            </div>
            <div class="contribution-rank-list">
              <div class="contribution-rank-row head"><span>股票</span><span>贡献</span><span>持仓</span><span>权重</span></div>
              ${renderContributionItems(stockContribution.top_losers) || `<div class="empty-state">暂无亏损贡献。</div>`}
            </div>
          </article>
        </div>
      </section>
    ` : backtestModel.role === "rolling_campaign" ? "" : `<div class="empty-state">${escapeHtml(text(stockContribution.reason, "暂无个股贡献数据。"))}</div>`}
  `);
}

function modelLibraryReviewMeta(item) {
  const reviewStatus = item?.status === "archived"
    ? "archived"
    : item?.validation?.status || (item?.sota_excluded_reason ? "excluded" : "unknown");
  const reviewLabel = reviewStatus === "review_required"
    ? "需复核"
    : reviewStatus === "blocked"
      ? "阻断"
      : reviewStatus === "clean"
        ? "通过"
        : reviewStatus === "archived"
          ? "已归档"
          : text(reviewStatus);
  return { reviewStatus, reviewLabel };
}

function resolveModelLibraryFocus(models, production, rollingCandidate) {
  const byId = new Map((models || []).map((item) => [text(item.model_id, ""), item]));
  const byRun = new Map((models || []).map((item) => [text(item.model_run_id, ""), item]));
  const selectedId = text(state.modelBacktestSelection.modelId, "");
  const selectedRunId = text(state.modelBacktestSelection.modelRunId, "");
  if (selectedRunId && byRun.has(selectedRunId)) {
    return {
      model: byRun.get(selectedRunId),
      roleLabel: state.modelBacktestSelection.label || "模型库选中模型",
      reason: "当前模型已被模型库 / 模型回测联动锁定，右侧说明和表格高亮都会跟随它。",
    };
  }
  if (selectedId && byId.has(selectedId)) {
    return {
      model: byId.get(selectedId),
      roleLabel: state.modelBacktestSelection.label || "模型库选中模型",
      reason: "当前模型已被模型库 / 模型回测联动锁定，右侧说明和表格高亮都会跟随它。",
    };
  }
  const productionId = text(production?.model_id, "");
  if (productionId && byId.has(productionId)) {
    return {
      model: byId.get(productionId),
      roleLabel: "生产模型",
      reason: "当前没有显式手动选择，默认先聚焦生产模型，方便核对真实交易主链路。",
    };
  }
  const candidateId = text(rollingCandidate?.model_id, "");
  if (candidateId && byId.has(candidateId)) {
    return {
      model: byId.get(candidateId),
      roleLabel: "Rolling 候选",
      reason: "当前没有手动或 production 锚点，默认展示正式 Rolling 评分最高的候选模型。",
    };
  }
  return {
    model: models?.[0] || {},
    roleLabel: models?.[0]?.model_id ? "当前排序首项" : "暂无聚焦模型",
    reason: models?.[0]?.model_id
      ? "当前没有 production / Rolling candidate 锚点，先按模型库排序展示首项。"
      : "模型库暂无记录。",
  };
}

function renderModelLibrary() {
  const registry = serviceOutputs(state.modelRegistry);
  const models = registry.items || registry.models || [];
  const seedDiagnostics = registry.seed_models || serviceOutputs(state.modelRuns).seed_models || [];
  const featureSetCatalog = serviceOutputs(state.modelFeatureSets);
  const modelScore = (item) => {
    const productionEvidence = ["candidate", "production"].includes(text(item?.status, "").toLowerCase())
      || text(item?.evaluation_mode || item?.metadata?.evaluation_mode, "").toLowerCase() === "production";
    const value = Number(productionEvidence
      ? (item?.rolling_score ?? item?.metadata?.rolling_score ?? item?.research_score ?? item?.metadata?.research_score)
      : (item?.research_score ?? item?.metadata?.research_score ?? item?.confirmed_research_score ?? item?.metadata?.confirmed_research_score));
    return Number.isFinite(value) ? value : null;
  };
  const displayModels = [...models].sort((a, b) => {
    const statusRank = (item) => item.status === "production" ? 0 : item.status === "candidate" ? 1 : item.status === "research" ? 2 : 3;
    const byStatus = statusRank(a) - statusRank(b);
    if (byStatus !== 0) return byStatus;
    const byScore = (modelScore(b) ?? -Infinity) - (modelScore(a) ?? -Infinity);
    if (Number.isFinite(byScore) && byScore !== 0) return byScore;
    return text(b.created_at, "").localeCompare(text(a.created_at, ""));
  });
  const productionStatus = serviceOutputs(state.modelProduction);
  const productionItems = productionStatus.items || productionStatus.production_models || [];
  const production = productionStatus.production_model || productionItems[0] || models.find((item) => item.status === "production") || {};
  const productionValidation = productionStatus.production_validation || {};
  const rollingCandidate = displayModels.find((item) => item.status === "candidate") || {};
  const candidateCount = models.filter((item) => item.status === "candidate").length;
  const researchCount = models.filter((item) => item.status === "research").length;
  const productionCount = models.filter((item) => item.status === "production").length;
  const confirmedResearchRounds = [...models.reduce((groups, item) => {
    const confirmation = item.research_confirmation || item.metadata?.research_confirmation || {};
    const roundGroupId = text(item.round_group_id || item.metadata?.round_group_id, "");
    if (["passed", "failed"].includes(text(confirmation.status, "").toLowerCase()) && roundGroupId && !groups.has(roundGroupId)) {
      groups.set(roundGroupId, { roundGroupId, confirmation, featureSetId: item.feature_set_id, models: [] });
    }
    if (groups.has(roundGroupId)) groups.get(roundGroupId).models.push(item);
    return groups;
  }, new Map()).values()];
  const focus = resolveModelLibraryFocus(displayModels, production, rollingCandidate);
  const focusModel = focus.model || {};
  const focusModelId = text(focusModel.model_id, "");
  const focusDisplayName = text(focusModel.display_name, text(focusModelId, "暂无模型"));
  const focusReview = modelLibraryReviewMeta(focusModel);
  const focusTrainParams = focusModel.resolved_training_params || {};
  const focusPortfolioParams = focusModel.resolved_portfolio_params || {};
  const focusConfigAudit = focusModel.config_audit || {};
  const focusSeedStability = focusModel.seed_stability || {};
  const focusConfirmation = focusModel.research_confirmation || focusModel.metadata?.research_confirmation || {};
  const focusConfirmationSeeds = Array.isArray(focusConfirmation.seed_results) ? focusConfirmation.seed_results : [];
  const focusSeedInputs = focusModel.seed_score_inputs || focusSeedStability.seed_score_inputs || (focusConfirmationSeeds.length ? {
    ann_min: Math.min(...focusConfirmationSeeds.map((item) => Number(item.excess_annualized_ret_with_cost)).filter(Number.isFinite)),
    ir_mean: focusConfirmationSeeds.map((item) => Number(item.excess_information_ratio_with_cost)).filter(Number.isFinite).reduce((sum, value, _, rows) => sum + value / rows.length, 0),
  } : {});
  const focusRollingGates = focusModel.rolling_gates || focusModel.metadata?.rolling_gates || {};
  const focusScore = modelScore(focusModel);
  const focusScoreLabel = (focusModel.evaluation_mode || focusModel.metadata?.evaluation_mode) === "production" ? "Rolling 准入分" : "Seed42 研究分";
  const focusParamLine = [
    `leaves ${text(focusTrainParams.num_leaves, "--")}`,
    `lr ${text(focusTrainParams.lr || focusTrainParams.learning_rate, "--")}`,
    `min_leaf ${text(focusTrainParams.min_data_in_leaf, "--")}`,
    `L1 ${text(focusTrainParams.lambda_l1, "--")}`,
    `L2 ${text(focusTrainParams.lambda_l2, "--")}`,
    `top${text(focusPortfolioParams.topk, "--")}/drop${text(focusPortfolioParams.n_drop, "--")}`,
  ].join(" · ");
  const selectionContainer = document.getElementById("model-library-selection");
  const summaryContainer = document.getElementById("model-library-summary");
  const featureSetContainer = document.getElementById("model-library-feature-sets");
  if (featureSetContainer) {
    featureSetContainer.innerHTML = renderModelFeatureSetCatalogPanel(featureSetCatalog);
  }
  summaryContainer.className = "model-library-summary-strip";
  summaryContainer.innerHTML = [
    { label: "模型总数", value: text(registry.count || models.length, "0"), note: "model_registry.db" },
    { label: "Production", value: text(production.model_id, "暂无"), note: `检验 ${text(productionValidation.status, "unknown")}` },
    { label: "Rolling Candidate", value: text(rollingCandidate.model_id, "暂无"), note: `准入分 ${shortNumber(modelScore(rollingCandidate), 2)} · 正式表现看 Seed42` },
    { label: "Research", value: text(researchCount, "0"), note: "研究证据，不可直接生产" },
    { label: "Candidate / Prod", value: `${text(candidateCount, "0")} / ${text(productionCount, "0")}`, note: "Rolling 达标 / 已晋升" },
    { label: "三Seed审计轮", value: text(confirmedResearchRounds.length, "0"), note: "Seed42 正式 · 17/83 审计" },
  ].map((item) => `
    <article>
      <span class="metric-label">${escapeHtml(item.label)}</span>
      <strong class="metric-value" title="${escapeHtml(item.value)}">${escapeHtml(item.value)}</strong>
      <small class="metric-note">${escapeHtml(item.note)}</small>
    </article>
  `).join("");
  const container = document.getElementById("model-table");
  if (!models.length) {
    container.innerHTML = `<div class="empty-state">模型库暂无记录。</div>`;
    selectionContainer.innerHTML = `<div class="empty-state">当前没有可联动的模型记录；刷新 model registry 后这里会显示默认聚焦模型与跳转说明。</div>`;
    queueFloatingXScrollbarRefresh(container);
    return;
  }
  selectionContainer.innerHTML = `
    <div class="model-library-focus-card">
      <div class="model-library-focus-head">
        <div class="model-library-focus-title">
          <span class="detail-label">Current Focus</span>
          <h4>${escapeHtml(focusDisplayName)}</h4>
          <p>${escapeHtml(focus.reason)}</p>
        </div>
        <div class="inspector-badges">
          <span class="badge subtle">${escapeHtml(focus.roleLabel)}</span>
          <span class="badge subtle">${escapeHtml(text(focusModel.status, "unknown"))}</span>
          <span class="badge ${escapeHtml(validationBadgeClass(focusReview.reviewStatus))}">${escapeHtml(focusReview.reviewLabel)}</span>
        </div>
      </div>
      <div class="model-library-score-grid">
        <article><span class="metric-label">${escapeHtml(focusScoreLabel)}</span><strong class="metric-value">${shortNumber(focusScore, 2)}</strong></article>
        <article><span class="metric-label">Rank IC</span><strong class="metric-value">${shortNumber(focusModel.rank_ic ?? focusModel.ic_mean, 4)}</strong></article>
        <article><span class="metric-label">Cost Adj Ret</span><strong class="metric-value">${pct(focusModel.excess_annualized_ret_with_cost ?? focusModel.annualized_ret, 2)}</strong></article>
        <article><span class="metric-label">Cost Adj IR</span><strong class="metric-value">${shortNumber(focusModel.excess_information_ratio_with_cost ?? focusModel.sharpe, 3)}</strong></article>
        <article><span class="metric-label">Max Drawdown</span><strong class="metric-value">${pct(focusModel.max_drawdown, 2)}</strong></article>
        <article><span class="metric-label">因子数</span><strong class="metric-value">${escapeHtml(text(focusModel.factor_count, "0"))}</strong></article>
        <article><span class="metric-label">Seed Worst Ann</span><strong class="metric-value">${pct(focusSeedInputs.ann_min, 2)}</strong></article>
        <article><span class="metric-label">Seed Mean IR</span><strong class="metric-value">${shortNumber(focusSeedInputs.ir_mean, 3)}</strong></article>
      </div>
      <div class="detail-grid">
        <div><span class="detail-label">当前定位</span><strong>${escapeHtml(focus.roleLabel)}</strong><small>${escapeHtml(text(focusModel.status, "unknown"))}</small></div>
        <div><span class="detail-label">回测联动</span><strong>${focusModelId ? "已就绪" : "暂无"}</strong><small>${escapeHtml(focusModelId ? "点击“查看”会跳转到模型研究 / 回测结果，并保留这个模型上下文。" : "暂无可联动模型。")}</small></div>
        <div><span class="detail-label">Policy Version</span><strong>${escapeHtml(text(focusModel.feature_snapshot_policy_version || "legacy_feature_dropna_policy"))}</strong></div>
        <div><span class="detail-label">Created At</span><strong>${escapeHtml(text(focusModel.created_at, "--"))}</strong></div>
        <div><span class="detail-label">Model Family</span><strong>${escapeHtml(text(focusModel.model_family, "--"))}</strong></div>
        <div><span class="detail-label">Production 检验</span><strong>${escapeHtml(text(focusModel.validation?.status || productionValidation.status, "unknown"))}</strong></div>
        <div><span class="detail-label">评测模式</span><strong>${escapeHtml(text(focusModel.evaluation_mode || focusModel.metadata?.evaluation_mode, "research"))}</strong></div>
        <div><span class="detail-label">Seed 确认</span><strong>${escapeHtml(text(focusConfirmation.status, "未运行"))}</strong></div>
        <div><span class="detail-label">Baseline Kind</span><strong>${escapeHtml(text(focusModel.baseline_kind || "dynamic_playbook"))}</strong></div>
        <div><span class="detail-label">Seed Verdict</span><strong>${escapeHtml(text(focusSeedStability.verdict, "unknown"))}</strong><small>${escapeHtml(text((focusSeedStability.reasons || []).slice(0, 1).join("；"), "无 seed 风险说明"))}</small></div>
        <div><span class="detail-label">Seed Overlap</span><strong>${pct(focusSeedInputs.top10_overlap_mean, 1)}</strong><small>rank corr ${shortNumber(focusSeedInputs.pred_rank_corr_mean, 3)}</small></div>
        <div><span class="detail-label">Rolling Gate</span><strong>${Object.keys(focusRollingGates).length ? (Object.values(focusRollingGates).every(Boolean) ? "通过" : "失败") : "未运行"}</strong><small>candidate 必须通过正式四折检验</small></div>
        <div><span class="detail-label">Config Audit</span><strong>${escapeHtml(focusConfigAudit.passed === true ? "通过" : focusConfigAudit.passed === false ? "失败" : "缺失")}</strong><small>${escapeHtml(text((focusConfigAudit.violations || focusConfigAudit.warnings || []).slice(0, 1).join("；"), "无异常"))}</small></div>
      </div>
      <div class="detail-copy">
        <span class="detail-label">最终生效参数</span>
        <p>${escapeHtml(focusParamLine)}</p>
      </div>
      <div class="detail-copy">
        <span class="detail-label">状态 / 评分说明</span>
        <p>研究模式按 Seed42 评分筛选，只有会话优胜轮补 Seed17/83 做稳定性确认；生产 candidate 只能由四折 Rolling 正式评分达标产生。</p>
      </div>
    </div>
  `;
  const confirmationRows = confirmedResearchRounds.flatMap((group) => {
    const seedResults = Array.isArray(group.confirmation.seed_results) ? group.confirmation.seed_results : [];
    return seedResults.map((seedResult) => {
      const matchingModel = group.models.find((item) => Number(item.seed ?? item.metadata?.seed) === Number(seedResult.seed)) || {};
      const displayName = text(matchingModel.display_name, `${group.roundGroupId} · S${text(seedResult.seed, "--")}`);
      return `
        <tr>
          <td><strong>${escapeHtml(displayName)}</strong><small>${escapeHtml(group.roundGroupId)}</small></td>
          <td><span class="badge ${group.confirmation.status === "passed" ? "success" : "warning"}">${group.confirmation.status === "passed" ? "审计通过" : "审计未通过"}</span><small>Seed ${escapeHtml(text(seedResult.seed, "--"))}</small></td>
          <td>${pct(seedResult.excess_annualized_ret_with_cost, 2)}<small>IR ${shortNumber(seedResult.excess_information_ratio_with_cost, 3)} · DD ${pct(-(Number(seedResult.abs_max_drawdown) || 0), 1)}</small></td>
          <td>${shortNumber(seedResult.research_score, 2)}<small>审计参考 ${shortNumber(group.confirmation.confirmed_research_score, 2)}</small></td>
          <td>${escapeHtml(text(group.featureSetId, "--"))}</td>
          <td><span class="badge ${Number(seedResult.seed) === 42 ? "ok" : "subtle"}">${Number(seedResult.seed) === 42 ? "正式模型" : "仅审计"}</span></td>
        </tr>
      `;
    });
  }).join("");
  container.innerHTML = `
    <div class="table-status-line">这里显示 research / candidate / production；research 不能直接晋升，candidate 只由正式 Rolling 产生。</div>
    <table class="data-table model-registry-table">
      <thead><tr><th>模型</th><th>状态</th><th>评审</th><th>Score</th><th>因子</th><th>Rank IC</th><th>收益 / IR</th><th>回测</th></tr></thead>
      <tbody>
        ${displayModels.map((item) => {
          const { reviewStatus, reviewLabel } = modelLibraryReviewMeta(item);
          const displayName = text(item.display_name, text(item.model_id, item.model_run_id));
          const displaySubtitle = text(item.display_subtitle, text(item.feature_snapshot_policy_version || "legacy_feature_dropna_policy"));
          return `
          <tr class="${focusModelId === item.model_id || text(state.modelBacktestSelection.modelRunId, "") === text(item.model_run_id, "") ? "selected-row" : ""}">
            <td>
              <strong title="${escapeHtml(text(item.model_id))}">${escapeHtml(displayName)}</strong>
              <small class="model-policy-line">${escapeHtml(displaySubtitle)}</small>
            </td>
            <td><span class="badge subtle">${escapeHtml(text(item.status))}</span><small>${escapeHtml(text(item.model_family, "--"))}</small></td>
            <td><span class="badge ${escapeHtml(validationBadgeClass(reviewStatus))}">${escapeHtml(reviewLabel)}</span></td>
            <td>${shortNumber(modelScore(item), 2)}</td>
            <td>${escapeHtml(text(item.factor_count, "0"))}</td>
            <td>${shortNumber(item.rank_ic ?? item.ic_mean, 4)}</td>
            <td><strong>${pct(item.excess_annualized_ret_with_cost ?? item.annualized_ret, 2)}</strong><small>IR ${shortNumber(item.excess_information_ratio_with_cost ?? item.sharpe, 3)} · DD ${pct(item.max_drawdown, 1)}</small></td>
            <td><button class="table-action" data-model-backtest-id="${escapeHtml(text(item.model_id, ""))}" data-model-backtest-run-id="${escapeHtml(text(item.model_run_id, ""))}" data-model-backtest-label="${escapeHtml(displayName)}">查看</button></td>
          </tr>
        `}).join("")}
      </tbody>
    </table>
    <div class="table-status-line">Seed 稳定性审计：正式模型固定 Seed42；17/83 只用于否决不稳定结果，不参与模型排名。</div>
    <table class="data-table model-registry-table">
      <thead><tr><th>Seed Run</th><th>审计状态</th><th>收益 / IR</th><th>研究分</th><th>Feature Set</th><th>角色</th></tr></thead>
      <tbody>${confirmationRows || `<tr><td colspan="6">暂无 Seed 确认结果。</td></tr>`}</tbody>
    </table>
  `;
  queueFloatingXScrollbarRefresh(container);
}

function tradingBadgeClass(value) {
  const raw = String(value || "").toLowerCase();
  if (raw.includes("failed") || raw.includes("blocked") || raw.includes("stale")) return "danger";
  if (raw.includes("waiting") || raw.includes("pending") || raw.includes("dry_run")) return "warn";
  if (raw.includes("ready") || raw.includes("completed") || raw.includes("promoted")) return "ok";
  return "neutral";
}

function latestDailyOpsOutputs() {
  const dailyOps = serviceOutputs(state.dailyOpsStatus);
  return {
    dailyOps,
    latest: dailyOps.latest || {},
    summary: dailyOps.summary || {},
  };
}

function tradingNextAdvice({ dailyLatest, trading, prediction, warnings, productionValidation, pendingRecommendations = null, recommendation = {}, snapshot = {} }) {
  const waitingReason = dailyLatest.waiting_reason || "";
  const validationBlocks = productionValidation?.hard_blocks || [];
  const pending = Array.isArray(pendingRecommendations) ? pendingRecommendations : (trading.pending_recommendations || []);
  const signalDate = text(recommendation.signal_date || pending[0]?.signal_date, "最新");
  const ledgerDate = text(snapshot.trade_date, "当前生产日");
  if (validationBlocks.length) {
    return {
      tone: "danger",
      badge: "需要处理",
      title: "生产模型验证未通过",
      body: `当前不能运行模拟交易：${validationBlocks.join("；")}。请先修复模型验证问题，再重新晋升通过验证的模型。`,
    };
  }
  if (waitingReason === "waiting_for_next_trade_date") {
    return {
      tone: "waiting",
      badge: "自动等待",
      title: "等待下一交易日行情",
      body: `${signalDate} 信号已生成，账户账本已推进到 ${ledgerDate}；下一交易日数据进入生产后会自动执行，无需手工补跑。`,
    };
  }
  if (!prediction || prediction.status === "blocked" || trading.prediction?.ok === false) {
    return { tone: "danger", badge: "已阻断", title: "预测链路阻塞", body: "请先检查生产模型、特征缓存和预测状态，再考虑触发日切。" };
  }
  if ((trading.latest_execution || {}).status === "failed") {
    return { tone: "danger", badge: "执行失败", title: "模拟交易执行失败", body: "优先查看执行元数据、成交、持仓和账本文件，不要重算历史推荐。" };
  }
  if ((dailyLatest.blockers || []).length) {
    return { tone: "danger", badge: "已阻断", title: "完整日切被阻断", body: (dailyLatest.blockers || []).join("；") };
  }
  if (pending.length) {
    return {
      tone: "waiting",
      badge: "等待数据推进",
      title: `${pending.length} 条计划等待下一交易日`,
      body: `${signalDate} 信号已生成，账户账本已到 ${ledgerDate}；待下一交易日行情进入生产后自动执行。`,
    };
  }
  return { tone: "ok", badge: "链路正常", title: "模拟交易链路正常", body: "当前无硬性阻断，生产数据、模型、调仓计划和账户账本已衔接。" };
}

function renderPathList(paths) {
  const rows = Object.entries(paths || {}).filter(([, value]) => value);
  if (!rows.length) return `<div class="empty-state">暂无可展示文件路径。</div>`;
  return rows.map(([label, value]) => `
    <div class="path-row">
      <span>${escapeHtml(label)}</span>
      <code>${escapeHtml(value)}</code>
      <button class="tiny-button" type="button" data-copy-text="${escapeHtml(value)}">复制</button>
    </div>
  `).join("");
}

function tableFromRows(rows, columns, emptyText) {
  if (!rows || !rows.length) return `<div class="empty-state">${escapeHtml(emptyText)}</div>`;
  return `
    <div class="table-shell compact-table-shell">
      <table class="data-table compact-table">
        <thead><tr>${columns.map((col) => `<th>${escapeHtml(col.label)}</th>`).join("")}</tr></thead>
        <tbody>
          ${rows.map((row) => `
            <tr>${columns.map((col) => `<td>${col.render ? col.render(row) : escapeHtml(text(row[col.key]))}</td>`).join("")}</tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function numberTone(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || Math.abs(n) < 1e-9) return "flat";
  return n > 0 ? "positive" : "negative";
}

function terminalStat(label, value, note = "", tone = "") {
  return `
    <div class="terminal-stat ${escapeHtml(tone)}">
      <span>${escapeHtml(label)}</span>
      <strong>${value}</strong>
      ${note ? `<small>${escapeHtml(note)}</small>` : ""}
    </div>
  `;
}

function paperHoldingRows(snapshot = {}, securityNames = {}) {
  const accountValue = Number(snapshot.account_value || 0);
  return Object.entries(snapshot.positions || {}).map(([instrument, position]) => ({
    instrument,
    security_name: securityNames[instrument] || "",
    shares: position.shares ?? position.amount,
    price: position.price,
    market_value: position.market_value,
    weight: accountValue > 0 ? Number(position.market_value || 0) / accountValue : null,
    count_day: position.count_day,
  })).sort((a, b) => Number(b.market_value || 0) - Number(a.market_value || 0));
}

function paperPositionCostBasis(dailyTrades = {}) {
  const inventory = new Map();
  Object.entries(dailyTrades || {})
    .sort(([left], [right]) => text(left, "").localeCompare(text(right, "")))
    .forEach(([, rows]) => (rows || []).forEach((row) => {
      const instrument = text(row.instrument, "");
      const action = text(row.action, "").toLowerCase();
      const quantity = Math.abs(Number(row.filled_amount ?? row.amount ?? row.shares));
      if (!instrument || !Number.isFinite(quantity) || quantity <= 0 || !["buy", "sell"].includes(action)) return;
      const current = inventory.get(instrument) || { shares: 0, cost: 0 };
      if (action === "buy") {
        const tradeValue = Math.abs(Number(row.trade_value ?? (quantity * Number(row.price || 0))));
        const fee = Math.max(0, Number(row.cost || 0));
        if (!Number.isFinite(tradeValue)) return;
        current.shares += quantity;
        current.cost += tradeValue + fee;
      } else if (current.shares > 0) {
        const soldShares = Math.min(quantity, current.shares);
        const averageCost = current.cost / current.shares;
        current.shares -= soldShares;
        current.cost -= averageCost * soldShares;
        if (current.shares <= 1e-8) {
          current.shares = 0;
          current.cost = 0;
        }
      }
      inventory.set(instrument, current);
    }));
  return inventory;
}

function paperAccountDailyItem(date) {
  const accounts = serviceOutputs(state.paperFleetStatus).accounts || [];
  const account = paperSelectedAccount(accounts);
  const snapshot = (account?.account_history || []).find((row) => text(row.trade_date, "") === text(date, ""));
  if (!snapshot) return null;
  return {
    ...snapshot,
    initial_capital: account.initial_capital,
    holdings: paperHoldingRows(snapshot, account.security_names || {}),
    trades: (account.daily_trades || {})[date] || [],
  };
}

function renderPaperAccountHoverContent(date) {
  const item = paperAccountDailyItem(date);
  if (!item) return `<div class="empty-state compact">悬停曲线上的交易日，可查看当天账户、持仓和成交。</div>`;
  const allHoldings = item.holdings || [];
  const allTrades = item.trades || [];
  const holdings = allHoldings.slice(0, 8);
  const trades = allTrades.slice(0, 8);
  const risk = item.risk_metrics || {};
  const stockExposure = risk.actual_stock_exposure ?? (Number(item.account_value) > 0 ? Number(item.stock_value || 0) / Number(item.account_value) : null);
  const cashExposure = risk.actual_cash_weight ?? (Number(item.account_value) > 0 ? Number(item.cash || 0) / Number(item.account_value) : null);
  const tradeCost = allTrades.reduce((sum, row) => sum + Number(row.cost || 0), 0);
  const tradeValue = allTrades.reduce((sum, row) => sum + Math.abs(Number(row.trade_value || 0)), 0);
  const cumulativeReturn = Number(item.initial_capital) > 0 ? Number(item.account_value) / Number(item.initial_capital) - 1 : null;
  const rowList = (rows, renderer, emptyText) => rows.length ? rows.map(renderer).join("") : `<li class="muted">${escapeHtml(emptyText)}</li>`;
  return `
    <div class="backtest-hover-head">
      <strong>${escapeHtml(item.trade_date)}</strong>
      <span>账户净值 ${shortNumber(item.account_value, 2)}</span>
    </div>
    <div class="backtest-hover-metrics paper-hover-metrics">
      <span><b>当日收益</b><strong class="${numberTone(item.daily_return)}">${pct(item.daily_return, 2)}</strong></span>
      <span><b>当日盈亏</b><strong class="${numberTone(item.daily_pnl)}">${shortNumber(item.daily_pnl, 2)}</strong></span>
      <span><b>累计收益</b><strong class="${numberTone(cumulativeReturn)}">${pct(cumulativeReturn, 2)}</strong></span>
      <span><b>股票仓位</b><strong>${pct(stockExposure, 1)}</strong></span>
      <span><b>现金仓位</b><strong>${pct(cashExposure, 1)}</strong></span>
      <span><b>持仓数量</b><strong>${allHoldings.length} 只</strong></span>
      <span><b>成交额</b><strong>${moneyNumber(tradeValue)} · ${allTrades.length} 笔</strong></span>
      <span><b>交易成本</b><strong>${shortNumber(tradeCost, 2)}</strong></span>
    </div>
    <div class="backtest-hover-columns paper-hover-columns">
      <div>
        <h4>当日持仓 Top 8</h4>
        <ul>${rowList(holdings, (row) => `<li><span><strong>${escapeHtml(row.instrument)}</strong><small>${escapeHtml(row.security_name || "名称待同步")}</small></span><b>${pct(row.weight, 2)}</b></li>`, "当日无股票持仓")}</ul>
      </div>
      <div>
        <h4>当日成交</h4>
        <ul>${rowList(trades, (row) => `<li><span><strong class="action-${escapeHtml(text(row.action, ""))}">${row.action === "buy" ? "买入" : row.action === "sell" ? "卖出" : escapeHtml(text(row.action, "成交"))}</strong> ${escapeHtml(row.instrument)}<small>${escapeHtml(row.security_name || "名称待同步")} · ${shortNumber(row.filled_amount, 0)} 股 @ ${shortNumber(row.price, 2)}</small></span><b>${moneyNumber(row.trade_value)}</b></li>`, "当日无成交")}</ul>
      </div>
    </div>
  `;
}

function updatePaperAccountHoverPanel(date) {
  const panel = document.getElementById("paper-account-hover-panel");
  const nextDate = text(date, "");
  if (!panel || !nextDate || panel.dataset.hoverDate === nextDate) return;
  panel.innerHTML = renderPaperAccountHoverContent(nextDate);
  panel.dataset.hoverDate = nextDate;
}

function renderPaperAccountCurve(rows, benchmarkRows = []) {
  const clean = (rows || [])
    .map((row) => ({
      date: text(row.trade_date || row.date, ""),
      value: Number(row.ending_account_value ?? row.account_value),
      pnl: Number(row.daily_pnl ?? 0),
    }))
    .filter((row) => row.date && Number.isFinite(row.value))
    .sort((a, b) => a.date.localeCompare(b.date));
  if (!clean.length) return `<div class="empty-state">暂无账户曲线。</div>`;
  const benchmarkByDate = new Map((benchmarkRows || [])
    .map((row) => [text(row.date || row.trade_date, ""), Number(row.close ?? row.adj_close)])
    .filter(([date, value]) => date && Number.isFinite(value)));
  const firstComparable = clean.find((row) => benchmarkByDate.has(row.date));
  const accountBase = Number(clean[0].value);
  const benchmarkBase = firstComparable ? benchmarkByDate.get(firstComparable.date) : null;
  const accountSeries = clean.map((row) => ({ ...row, cumulative: accountBase ? row.value / accountBase - 1 : 0 }));
  const benchmarkSeries = benchmarkBase ? clean
    .filter((row) => benchmarkByDate.has(row.date))
    .map((row) => ({ date: row.date, cumulative: benchmarkByDate.get(row.date) / benchmarkBase - 1 })) : [];
  const width = 760;
  const height = 280;
  const pad = { left: 54, right: 22, top: 22, bottom: 34 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const allReturns = [0, ...accountSeries.map((row) => row.cumulative), ...benchmarkSeries.map((row) => row.cumulative)];
  const minRaw = Math.min(...allReturns);
  const maxRaw = Math.max(...allReturns);
  const span = Math.max(maxRaw - minRaw, 0.01);
  const yMin = minRaw - span * 0.08;
  const yMax = maxRaw + span * 0.10;
  const dateIndex = new Map(clean.map((row, idx) => [row.date, idx]));
  const x = (idx) => pad.left + (clean.length === 1 ? 0 : idx / (clean.length - 1) * innerW);
  const y = (value) => pad.top + (1 - ((Number(value) - yMin) / (yMax - yMin))) * innerH;
  const path = accountSeries.map((row, idx) => `${idx ? "L" : "M"} ${x(idx).toFixed(1)} ${y(row.cumulative).toFixed(1)}`).join(" ");
  const benchmarkPath = benchmarkSeries.map((row, idx) => `${idx ? "L" : "M"} ${x(dateIndex.get(row.date)).toFixed(1)} ${y(row.cumulative).toFixed(1)}`).join(" ");
  const last = clean[clean.length - 1];
  const accountReturn = accountSeries[accountSeries.length - 1]?.cumulative;
  const benchmarkReturn = benchmarkSeries[benchmarkSeries.length - 1]?.cumulative;
  const relativeReturn = Number.isFinite(accountReturn) && Number.isFinite(benchmarkReturn) ? (1 + accountReturn) / (1 + benchmarkReturn) - 1 : null;
  let peakNav = -Infinity;
  let peakIdx = 0;
  let drawdownPeakIdx = 0;
  let maxDrawdownIdx = 0;
  let maxDrawdown = 0;
  let bestDailyIdx = 0;
  let bestDaily = -Infinity;
  accountSeries.forEach((row, idx) => {
    const nav = 1 + row.cumulative;
    if (nav > peakNav) { peakNav = nav; peakIdx = idx; }
    const drawdown = peakNav > 0 ? nav / peakNav - 1 : 0;
    if (drawdown < maxDrawdown) { maxDrawdown = drawdown; maxDrawdownIdx = idx; drawdownPeakIdx = peakIdx; }
    if (row.pnl > bestDaily) { bestDaily = row.pnl; bestDailyIdx = idx; }
  });
  const hitWidth = Math.max(8, innerW / Math.max(1, clean.length));
  const zeroY = y(0).toFixed(1);
  const areaPath = `${path} L ${x(accountSeries.length - 1).toFixed(1)} ${zeroY} L ${x(0).toFixed(1)} ${zeroY} Z`;
  return `
    <div class="backtest-chart-shell paper-account-chart-shell">
      <div class="backtest-chart-main">
      <div class="chart-legend" aria-label="图例">
        <span><i class="legend-dot model"></i>模拟账户累计 ${pct(accountReturn, 2)}</span>
        <span><i class="legend-dot benchmark"></i>沪深 300 累计 ${benchmarkSeries.length ? pct(benchmarkReturn, 2) : "暂无同期数据"}</span>
        ${benchmarkSeries.length ? `<span><i class="legend-dot excess"></i>相对基准累计 ${pct(relativeReturn, 2)}</span>` : ""}
      </div>
      <div class="chart-basis-note">复利净值口径 · 同期首日归零 · 相对基准 = 账户净值 ÷ 沪深 300 净值 − 1 · 悬停查看逐日持仓与成交</div>
      <svg class="backtest-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="模拟账户与沪深300同期归一净值曲线">
        <defs><linearGradient id="paper-account-area-gradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#60a5fa" stop-opacity="0.22"></stop><stop offset="100%" stop-color="#60a5fa" stop-opacity="0"></stop></linearGradient></defs>
        <line class="chart-axis" x1="${pad.left}" y1="${zeroY}" x2="${width - pad.right}" y2="${zeroY}"></line>
        <line class="chart-grid" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}"></line>
        <line class="chart-grid" x1="${pad.left}" y1="${pad.top}" x2="${width - pad.right}" y2="${pad.top}"></line>
        <line class="chart-grid" x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}"></line>
        <path class="paper-account-area" d="${areaPath}"></path>
        ${benchmarkPath ? `<path class="chart-line benchmark paper-benchmark-line" d="${benchmarkPath}"></path>` : ""}
        <path class="chart-line model" d="${path}"></path>
        <line class="chart-dd-span" x1="${x(drawdownPeakIdx).toFixed(1)}" y1="${y(accountSeries[drawdownPeakIdx].cumulative).toFixed(1)}" x2="${x(maxDrawdownIdx).toFixed(1)}" y2="${y(accountSeries[maxDrawdownIdx].cumulative).toFixed(1)}"></line>
        <g class="chart-marker marker-drawdown"><circle cx="${x(maxDrawdownIdx).toFixed(1)}" cy="${y(accountSeries[maxDrawdownIdx].cumulative).toFixed(1)}" r="5"></circle><title>最大回撤 ${pct(maxDrawdown, 2)} · ${escapeHtml(accountSeries[maxDrawdownIdx].date)}</title></g>
        <g class="chart-marker marker-best-day"><circle cx="${x(bestDailyIdx).toFixed(1)}" cy="${y(accountSeries[bestDailyIdx].cumulative).toFixed(1)}" r="4"></circle><title>最佳单日盈亏 ${shortNumber(bestDaily, 2)} · ${escapeHtml(accountSeries[bestDailyIdx].date)}</title></g>
        ${accountSeries.map((row, idx) => `<rect class="chart-hover-hit" x="${(x(idx) - hitWidth / 2).toFixed(1)}" y="${pad.top}" width="${hitWidth.toFixed(1)}" height="${innerH}" tabindex="0" data-paper-curve-date="${escapeHtml(row.date)}"><title>${escapeHtml(row.date)} 账户累计 ${pct(row.cumulative, 2)} · 账户净值 ${shortNumber(row.value, 2)}</title></rect>`).join("")}
        <text class="chart-label y-axis" x="8" y="${y(maxRaw).toFixed(1)}">${pct(maxRaw, 0)}</text>
        <text class="chart-label y-axis" x="8" y="${zeroY}">0%</text>
        <text class="chart-label y-axis" x="8" y="${y(minRaw).toFixed(1)}">${pct(minRaw, 0)}</text>
        <text class="chart-label" x="${pad.left}" y="${height - 10}">${escapeHtml(clean[0].date)}</text>
        <text class="chart-label end" x="${width - pad.right}" y="${height - 10}">${escapeHtml(last.date)}</text>
      </svg>
      </div>
      <div id="paper-account-hover-panel" class="backtest-hover-panel paper-account-hover-panel" data-hover-date="${escapeHtml(last.date)}">${renderPaperAccountHoverContent(last.date)}</div>
    </div>
  `;
}

function paperBenchmarkQueryUrl() {
  const start = new Date();
  start.setUTCDate(start.getUTCDate() - 550);
  return `/data/benchmark-series?code=000300.SH&start=${start.toISOString().slice(0, 10)}`;
}

function symbolCell(row) {
  return `<strong>${escapeHtml(paperDisplayInstrument(row.instrument || row.symbol))}</strong><small>${escapeHtml(text(row.security_name, "名称待同步"))}</small>`;
}

function signedNumberCell(value, digits = 2) {
  return `<span class="num ${numberTone(value)}">${shortNumber(value, digits)}</span>`;
}

function monitorBlock(title, hint, content) {
  return `
    <section class="monitor-block">
      <div class="monitor-block-head">
        <h4>${escapeHtml(title)}</h4>
        ${hint ? `<span>${escapeHtml(hint)}</span>` : ""}
      </div>
      ${content}
    </section>
  `;
}

function legacyPaperFleetView() {
  const fleet = serviceOutputs(state.paperFleetStatus);
  const accounts = fleet.accounts || [];
  const overview = document.getElementById("paper-fleet-overview");
  const comparison = document.getElementById("paper-account-comparison");
  const replayCenter = document.getElementById("paper-replay-center");
  if (!overview || !comparison || !replayCenter) return;

  const latestFleet = fleet.latest_fleet_run || {};
  const registry = fleet.registry || {};
  const data = fleet.data || {};
  const operationLock = fleet.operation_lock || {};
  overview.innerHTML = `
    <div class="detail-grid terminal-detail-grid">
      <div><span class="detail-label">Fleet Status</span><strong>${escapeHtml(text(latestFleet.status || fleet.status, "ready"))}</strong></div>
      <div><span class="detail-label">Active Accounts</span><strong>${escapeHtml(text(fleet.active_account_count, "0"))}</strong></div>
      <div><span class="detail-label">Production Models</span><strong>${escapeHtml(text(new Set(accounts.flatMap((item) => (item.deployments || []).map((row) => row.model_run_id))).size, "0"))}</strong></div>
      <div><span class="detail-label">Data Latest</span><strong>${escapeHtml(text(data.qlib_latest, "--"))}</strong></div>
      <div><span class="detail-label">Recommendations</span><strong>${escapeHtml(text(registry.total_recommendations, "0"))}</strong></div>
      <div><span class="detail-label">Executions / Snapshots</span><strong>${escapeHtml(`${text(registry.paper_executions, "0")} / ${text(registry.paper_account_snapshots, "0")}`)}</strong></div>
      <div><span class="detail-label">Last Fleet Run</span><strong>${escapeHtml(text(latestFleet.fleet_run_id, "尚未运行"))}</strong></div>
      <div><span class="detail-label">Replay Basis</span><strong>As-Of Capped</strong></div>
      <div><span class="detail-label">Write Lock</span><strong>${escapeHtml(text(operationLock.status, "idle"))}</strong><small>${escapeHtml(text(operationLock.holder || operationLock.last_holder, ""))}</small></div>
    </div>
  `;

  const rows = accounts.map((account) => {
    const deployment = [...(account.deployments || [])].sort((a, b) => text(b.effective_from).localeCompare(text(a.effective_from)))[0] || {};
    const snapshot = account.latest_snapshot || {};
    const history = account.account_history || [];
    const initial = Number(account.initial_capital || 0);
    const value = Number(snapshot.account_value);
    const totalReturn = initial > 0 && Number.isFinite(value) ? value / initial - 1 : null;
    const gapOutputs = serviceOutputs(account.gap_plan || {});
    const gapPlan = gapOutputs.plan || gapOutputs;
    const recommendation = account.latest_recommendation || {};
    const confidence = recommendation.metrics?.confidence || {};
    const risk = snapshot.risk_metrics || {};
    return {
      account_id: account.account_id,
      display_name: account.display_name,
      account_mode: account.account_mode,
      model_run_id: deployment.model_run_id,
      model_id: deployment.model_id,
      trade_date: snapshot.trade_date,
      account_value: value,
      total_return: totalReturn,
      pending_count: (account.pending_recommendations || []).length,
      gap_count: (gapPlan.trade_dates || []).length,
      run_status: account.recent_runs?.[0]?.status || account.status,
      history_count: history.length,
      contract: account.strategy_contract_version,
      confidence_state: confidence.confidence_state,
      target_exposure: confidence.target_stock_exposure ?? recommendation.metrics?.target_stock_exposure ?? risk.target_stock_exposure,
      actual_exposure: risk.actual_stock_exposure,
      target_cash: confidence.target_cash_weight ?? recommendation.metrics?.target_cash_weight ?? risk.target_cash_weight,
      selected_count: confidence.selected_count,
      confidence_reason: (confidence.reasons || []).join("；"),
    };
  });
  comparison.innerHTML = tableFromRows(rows, [
    { key: "account_id", label: "账户", render: (row) => `<strong>${escapeHtml(row.display_name || row.account_id)}</strong><small>${escapeHtml(row.account_id)} · ${escapeHtml(row.account_mode)}</small>` },
    { key: "model_id", label: "生产模型", render: (row) => `<strong>${escapeHtml(text(row.model_id, "--"))}</strong><small>${escapeHtml(text(row.model_run_id, ""))}</small>` },
    { key: "confidence_state", label: "置信度", render: (row) => `<span class="badge ${tradingBadgeClass(row.confidence_state)}">${escapeHtml(text(row.confidence_state, row.contract?.startsWith("confidence_cash_") ? "待计算" : "legacy"))}</span><small>${escapeHtml(text(row.confidence_reason, ""))}</small>` },
    { key: "target_exposure", label: "目标/实际仓位", render: (row) => `${pct(row.target_exposure, 1)} / ${pct(row.actual_exposure, 1)}` },
    { key: "target_cash", label: "目标现金", render: (row) => pct(row.target_cash, 1) },
    { key: "selected_count", label: "有效槽位" },
    { key: "trade_date", label: "账本日期" },
    { key: "account_value", label: "账户净值", render: (row) => shortNumber(row.account_value, 2) },
    { key: "total_return", label: "累计收益", render: (row) => pct(row.total_return, 2) },
    { key: "pending_count", label: "Pending" },
    { key: "gap_count", label: "历史缺口" },
    { key: "run_status", label: "状态", render: (row) => `<span class="badge ${tradingBadgeClass(row.run_status)}">${escapeHtml(text(row.run_status))}</span>` },
  ], "尚未创建生产模拟账户。");

  const qualityRows = [];
  const gapRows = accounts.map((account) => {
    const planResult = account.gap_plan || {};
    const planOutputs = serviceOutputs(planResult);
    const plan = planOutputs.plan || planOutputs;
    const quality = plan.score_quality || {};
    for (const row of quality.dates || []) {
      qualityRows.push({ account_id: account.account_id, ...row });
    }
    const blockers = plan.blockers || (planResult.err ? [planResult.err] : []);
    return {
      account_id: account.account_id,
      from_date: plan.from_date,
      to_date: plan.to_date,
      trade_date_count: plan.trade_date_count || 0,
      starting_snapshot_date: plan.starting_snapshot_date,
      replay_basis: plan.replay_basis,
      automatic: plan.automatic,
      score_status: quality.status,
      blocker_count: blockers.length,
      blockers,
    };
  });
  const gapTable = tableFromRows(gapRows, [
    { key: "account_id", label: "账户" },
    { key: "starting_snapshot_date", label: "最后账本" },
    { key: "from_date", label: "补跑起点" },
    { key: "to_date", label: "目标日期" },
    { key: "trade_date_count", label: "待补交易日" },
    { key: "automatic", label: "执行策略", render: (row) => row.trade_date_count === 0 ? "已补齐" : row.automatic ? "1—5日自动" : "需人工确认" },
    { key: "score_status", label: "分数质量", render: (row) => `<span class="badge ${tradingBadgeClass(row.score_status)}">${escapeHtml(text(row.score_status, "未检查"))}</span>` },
    { key: "blocker_count", label: "阻断数" },
    { key: "blockers", label: "阻断摘要", render: (row) => {
      const items = row.blockers || [];
      const summary = items.slice(0, 2).join("；");
      return escapeHtml(summary ? `${summary}${items.length > 2 ? `；另有 ${items.length - 2} 条` : ""}` : "无");
    } },
  ], "尚未创建账户，因此没有历史缺口计划。");
  const qualityTable = tableFromRows(qualityRows, [
    { key: "account_id", label: "账户" },
    { key: "signal_date", label: "信号日" },
    { key: "status", label: "质量", render: (row) => `<span class="badge ${tradingBadgeClass(row.status)}">${escapeHtml(text(row.status))}</span>` },
    { key: "record_count", label: "候选数" },
    { key: "unique_score_count", label: "唯一分数" },
    { key: "boundary_tied", label: "Top20边界", render: (row) => row.boundary_tied ? "并列" : "清晰" },
    { key: "strictly_above_boundary", label: "严格高于" },
    { key: "equal_to_boundary", label: "边界同分" },
    { key: "confidence", label: "目标仓位/现金", render: (row) => `${pct(row.confidence?.target_stock_exposure, 1)} / ${pct(row.confidence?.target_cash_weight, 1)}` },
    { key: "model_confidence", label: "模型置信度", render: (row) => `${escapeHtml(text(row.confidence?.model_confidence?.state, "--"))} · ${pct(row.confidence?.model_confidence?.multiplier, 0)}` },
    { key: "st_filter", label: "ST过滤", render: (row) => text(row.st_filter?.st_filtered_count, "0") },
    { key: "identity_coverage", label: "PIT身份覆盖", render: (row) => pct(row.st_filter?.identity_match_ratio, 2) },
  ], "预测尚未生成，因此没有逐日分数质量结果。");
  replayCenter.innerHTML = `${gapTable}${monitorBlock("逐日分数质量", "Top20 截止位必须由模型分数唯一决定", qualityTable)}`;
}

function legacyPaperTradingView() {
  legacyPaperFleetView();
  const trading = serviceOutputs(state.tradingStatus);
  const { latest: dailyLatest, summary: dailySummary } = latestDailyOpsOutputs();
  const data = serviceOutputs(state.data);
  const prediction = trading.prediction?.outputs || serviceOutputs(state.predictionStatus);
  const result = serviceOutputs(state.latestTradingResult);
  const latestRec = trading.latest_recommendation || {};
  const latestExec = trading.latest_qlib_paper_execution || trading.latest_execution || {};
  const latestExecSummary = trading.latest_execution_summary || {};
  const latestAccount = trading.qlib_paper_account || dailySummary.qlib_paper_account || {};
  const accountHistory = trading.qlib_paper_account_history || dailySummary.qlib_paper_account_history || [];
  const paperAccounts = trading.qlib_paper_accounts || dailySummary.qlib_paper_accounts || [];
  const pending = trading.pending_recommendations || [];
  const dailyWarnings = state.dailyOpsStatus?.warnings || [];
  const warnings = [...(trading.warnings || state.tradingStatus?.warnings || []), ...dailyWarnings];
  const orderActionRank = { buy: 0, hold: 1, sell: 2 };
  const orders = [...(trading.latest_orders || [])].sort((a, b) => {
    const actionDiff = (orderActionRank[text(a.action, "").toLowerCase()] ?? 3)
      - (orderActionRank[text(b.action, "").toLowerCase()] ?? 3);
    if (actionDiff) return actionDiff;
    const weightDiff = Number(b.target_weight || 0) - Number(a.target_weight || 0);
    if (weightDiff) return weightDiff;
    const scoreDiff = Number(b.score || 0) - Number(a.score || 0);
    if (scoreDiff) return scoreDiff;
    return Math.abs(Number(b.delta_shares || 0)) - Math.abs(Number(a.delta_shares || 0));
  });
  const execMetrics = latestExec.metrics || {};
  const recConfidence = latestRec.metrics?.confidence || {};
  const accountValue = Number(execMetrics.ending_account_value ?? execMetrics.account_value ?? latestAccount.account_value);
  const accountCash = Number(execMetrics.cash ?? execMetrics.ending_cash ?? latestAccount.cash);
  const accountStockValue = Number(execMetrics.stock_value ?? latestAccount.stock_value);
  const recQuality = trading.latest_recommendation_quality || dailySummary.latest_recommendation_quality || {};
  const productionValidation = trading.production_validation_summary || dailySummary.production_validation_summary || {};
  const validationBlocks = productionValidation.hard_blocks || [];
  const targetRows = dailySummary.target_rows || [];
  const latestRecId = text(latestRec.recommendation_id, "");
  const latestExecRecId = text(latestExec.recommendation_id, "");
  const hasPendingNewTarget = latestRecId && latestExecRecId && latestRecId !== latestExecRecId && pending.length;
  const dataDates = dailySummary.data_latest_dates || dailyLatest.data_latest_date_after || {
    hdf5: data.snapshot?.latest_hdf5_trade_date,
    qlib: data.snapshot?.latest_qlib_trade_date,
    quantgpt: data.snapshot?.latest_quantgpt_trade_date,
  };
  const quality = dailyLatest.data_quality_summary || {};
  const advice = tradingNextAdvice({ dailyLatest, trading, prediction, warnings, productionValidation });
  const latestOutputFiles = latestExec.output_files || {};
  const latestCodeActivity = quality.latest_code_activity || {};
  const filePaths = {
    recommendation: latestRec.recommendation_file,
    orders_preview: latestRec.order_preview_file,
    portfolio_decision: latestRec.portfolio_decision_file || latestRec.target_file,
    daily_ledger: latestOutputFiles.ledger_file || dailySummary.paper_ledger_path,
    trades: latestOutputFiles.trades_file,
    positions: latestOutputFiles.holdings_file,
    trading_latest_status: trading.paths?.latest_status_file,
    daily_ops_latest_status: serviceOutputs(state.dailyOpsStatus).latest_status_file,
  };

  appendMetricCards(document.getElementById("trading-summary"), [
    { label: "Daily Ops", value: text(dailyLatest.status || serviceOutputs(state.dailyOpsStatus).status, "empty"), note: text(dailyLatest.waiting_reason || dailyLatest.blocked_reason || "完整日切状态") },
    { label: "生产数据", value: text(dataDates.qlib || dataDates.hdf5, "unknown"), note: `HDF5 ${text(dataDates.hdf5)} / QGPT ${text(dataDates.quantgpt)}` },
    { label: "生产模型", value: text(prediction.run_context?.model_id || dailySummary.production_model_id, "暂无"), note: validationBlocks.length ? `blocked: ${validationBlocks.join(", ")}` : text(prediction.run_context?.model_run_id || dailySummary.production_model_run_id || prediction.run_context?.feature_set_id, "") },
    { label: "Paper PnL", value: shortNumber(execMetrics.daily_pnl, 2), note: `account ${shortNumber(accountValue, 2)} / positions ${text(execMetrics.position_count ?? Object.keys(latestAccount.positions || {}).length, "0")}` },
  ]);

  document.getElementById("trading-advice").innerHTML = `
    <div class="trading-advice ${escapeHtml(advice.tone)}">
      <span class="badge ${escapeHtml(tradingBadgeClass(advice.tone))}">${escapeHtml(text(dailyLatest.decision_status || dailyLatest.status || trading.status, "status"))}</span>
      <strong>${escapeHtml(advice.title)}</strong>
      <p>${escapeHtml(advice.body)}</p>
    </div>
  `;

  const flowItems = [
    ["数据", dataDates.qlib || dataDates.hdf5, quality.passed === false ? "danger" : "ok", `HDF5 ${text(dataDates.hdf5)} · Qlib ${text(dataDates.qlib)} · QGPT ${text(dataDates.quantgpt)}`],
    ["模型", prediction.run_context?.model_id || dailySummary.production_model_id, validationBlocks.length ? "danger" : (prediction.run_context?.model_id ? "ok" : "warn"), validationBlocks.length ? `validation: ${validationBlocks.join(", ")}` : (prediction.run_context?.model_run_id || dailySummary.production_model_run_id)],
    ["预测", prediction.status || dailySummary.prediction_status, tradingBadgeClass(prediction.status || dailySummary.prediction_status), `Qlib ${text(trading.qlib_latest || dailyLatest.qlib_latest)}`],
    ["推荐", latestRec.signal_date, tradingBadgeClass(latestRec.status), `${text(latestRec.status)} -> ${text(latestRec.execution_date, "pending")}`],
    ["执行", latestExec.trade_date, tradingBadgeClass(latestExec.status), `${text(latestExec.status, "none")} · trades ${text(execMetrics.trade_count, "0")}`],
  ];
  document.getElementById("trading-flow").innerHTML = flowItems.map(([label, value, tone, note]) => `
    <article class="flow-card ${escapeHtml(tone)}">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(text(value, "--"))}</strong>
      <small>${escapeHtml(text(note, ""))}</small>
    </article>
  `).join("");

  const riskItems = [
    ...warnings.map((item) => ({ tone: item.includes("waiting") || item.includes("pending") ? "warn" : "danger", text: item })),
    ...validationBlocks.map((item) => ({ tone: "danger", text: `production_validation:${item}` })),
    ...(dailyLatest.blockers || []).map((item) => ({ tone: "danger", text: item })),
    ...(dailyLatest.waiting_reason ? [{ tone: "warn", text: dailyLatest.waiting_reason }] : []),
    ...(quality.warnings || []).map((item) => ({ tone: "warn", text: item })),
  ];
  document.getElementById("trading-risk").innerHTML = riskItems.length
    ? riskItems.map((item) => `<span class="risk-pill ${escapeHtml(item.tone)}">${escapeHtml(item.text)}</span>`).join("")
    : `<span class="risk-pill ok">暂无风险告警</span>`;

  document.getElementById("trading-daily-ops").innerHTML = `
    <div class="ops-timeline">
      <div><span>1</span><strong>Data</strong><small>${escapeHtml(text(dailyLatest.data_update_result || "status"))}</small></div>
      <div><span>2</span><strong>Model</strong><small>${escapeHtml(text(prediction.run_context?.model_id || dailySummary.production_model_id, "--"))}</small></div>
      <div><span>3</span><strong>Predict</strong><small>${escapeHtml(text(prediction.status || dailySummary.prediction_status, "--"))}</small></div>
      <div><span>4</span><strong>Recommend</strong><small>${escapeHtml(text(latestRec.status, "--"))}</small></div>
      <div><span>5</span><strong>Execute</strong><small>${escapeHtml(text(latestExec.status || dailyLatest.trade_action, "--"))}</small></div>
    </div>
    <div class="detail-grid terminal-detail-grid">
      <div><span class="detail-label">Last Status</span><strong>${escapeHtml(text(dailyLatest.status || serviceOutputs(state.dailyOpsStatus).status, "empty"))}</strong></div>
      <div><span class="detail-label">Decision</span><strong>${escapeHtml(text(dailyLatest.decision_status, "--"))}</strong></div>
      <div><span class="detail-label">Generated At</span><strong>${escapeHtml(text(dailyLatest.generated_at, "--"))}</strong></div>
      <div><span class="detail-label">Trade Action</span><strong>${escapeHtml(text(dailyLatest.trade_action, "--"))}</strong></div>
      <div><span class="detail-label">Quality</span><strong>${quality.passed === false ? "failed" : "passed"}</strong></div>
      <div><span class="detail-label">QGPT Coverage</span><strong>${pct(quality.quantgpt_coverage_ratio, 2)}</strong></div>
      <div><span class="detail-label">Stale Codes</span><strong>${escapeHtml(text(latestCodeActivity.stale_stock_count ?? quality.quantgpt_stale_stock_count, "0"))}</strong></div>
      <div><span class="detail-label">Score Unique</span><strong>${escapeHtml(text(recQuality.unique_score_count, "--"))}</strong></div>
      <div><span class="detail-label">Score Std</span><strong>${shortNumber(recQuality.score_std, 6)}</strong></div>
      <div><span class="detail-label">Model Validation</span><strong>${escapeHtml(text(productionValidation.status, "--"))}</strong></div>
      <div><span class="detail-label">Hard Blocks</span><strong>${escapeHtml(text(validationBlocks.join(", "), "none"))}</strong></div>
      <div><span class="detail-label">Confidence State</span><strong>${escapeHtml(text(recConfidence.confidence_state, "legacy"))}</strong></div>
      <div><span class="detail-label">Target / Actual Exposure</span><strong>${pct(recConfidence.target_stock_exposure ?? execMetrics.target_stock_exposure, 1)} / ${pct(execMetrics.actual_stock_exposure, 1)}</strong></div>
      <div><span class="detail-label">Target / Actual Cash</span><strong>${pct(recConfidence.target_cash_weight ?? execMetrics.target_cash_weight, 1)} / ${pct(execMetrics.actual_cash_weight, 1)}</strong></div>
      <div><span class="detail-label">Model / Selection Confidence</span><strong>${escapeHtml(`${text(recConfidence.model_confidence?.state, "--")} / ${text(recConfidence.selection_confidence?.state, "--")}`)}</strong></div>
    </div>
    ${validationBlocks.length ? `
      <div class="trading-interpretation warn">
        <strong>模拟交易已被生产模型验证闸门阻断。</strong>
        <span>当前 hard block: ${escapeHtml(validationBlocks.join("；"))}。Artifact: ${escapeHtml(text(productionValidation.artifact_path, "--"))}</span>
      </div>
    ` : ""}
    <div class="paper-engine-card">
      <span class="badge neutral">Qlib Exchange</span>
      <strong>PaperAccount: daily fills + account ledger</strong>
      <small>Qlib 负责成交价、交易单位、费用和涨跌停限制；FXAlpha 冻结分数、订单、成交和账户快照。</small>
    </div>
    <div class="detail-copy terminal-log"><span class="detail-label">Commands Run</span><pre>${escapeHtml((dailyLatest.commands_run || []).join("\n") || "暂无 daily-ops 记录。")}</pre></div>
  `;

  const pendingTable = tableFromRows(pending, [
    { key: "recommendation_id", label: "Recommendation" },
    { key: "signal_date", label: "Signal" },
    { key: "execution_date", label: "Execution" },
    { key: "status", label: "Status", render: (row) => `<span class="badge ${tradingBadgeClass(row.status)}">${escapeHtml(text(row.status))}</span>` },
    { key: "topk", label: "TopK" },
    { key: "warnings", label: "Warnings", render: (row) => escapeHtml((row.warnings || []).join("；")) },
  ], "暂无 pending 推荐。");

  const targetTable = tableFromRows(targetRows.slice(0, 30), [
    { key: "rank", label: "Rank" },
    { key: "instrument", label: "代码 / 名称", render: symbolCell },
    { key: "market_code", label: "Market" },
    { key: "score", label: "Score", render: (row) => shortNumber(row.score, 6) },
    { key: "target_weight", label: "Weight", render: (row) => pct(row.target_weight, 2) },
    { key: "target_value", label: "Target Value", render: (row) => shortNumber(row.target_value, 2) },
  ], "暂无最新目标组合。");

  const ordersTable = tableFromRows(orders.slice(0, 30), [
    { key: "instrument", label: "代码 / 名称", render: symbolCell },
    { key: "action", label: "方向", render: (row) => `<span class="badge subtle action-${escapeHtml(text(row.action))}">${escapeHtml(text(row.action))}</span>` },
    { key: "current_shares", label: "Current" },
    { key: "target_shares", label: "Target" },
    { key: "delta_shares", label: "Delta" },
    { key: "target_weight", label: "Weight", render: (row) => pct(row.target_weight, 2) },
    { key: "estimated_notional", label: "Notional", render: (row) => shortNumber(row.estimated_notional, 2) },
  ], "暂无订单预览。");

  const tradesTable = tableFromRows(latestExecSummary.trade_rows || dailySummary.trade_rows || [], [
    { key: "instrument", label: "代码 / 名称", render: symbolCell },
    { key: "direction", label: "方向", render: (row) => `<span class="badge subtle action-${escapeHtml(text(row.direction || row.action))}">${escapeHtml(text(row.direction || row.action))}</span>` },
    { key: "price", label: "成交价", render: (row) => shortNumber(row.price, 2) },
    { key: "shares", label: "数量", render: (row) => shortNumber(row.shares ?? row.filled_amount, 0) },
    { key: "notional", label: "成交额", render: (row) => shortNumber(row.notional ?? row.trade_value, 2) },
    { key: "trade_date", label: "日期" },
  ], "暂无成交回报。");

  document.getElementById("trading-picks").innerHTML = `
    ${hasPendingNewTarget ? `
      <div class="trading-interpretation warn">
        <strong>当前 GUI 看到的持仓来自上一笔已执行推荐。</strong>
        <span>最新 pending 推荐 ${escapeHtml(latestRecId)} 尚未到执行日；下面“最新目标组合”才是修复后的 T+1 目标，调仓订单会先卖出旧事故仓、再买入新目标。</span>
      </div>
    ` : ""}
    <div class="detail-grid">
      <div><span class="detail-label">Recommendation ID</span><strong>${escapeHtml(text(latestRec.recommendation_id, "暂无"))}</strong></div>
      <div><span class="detail-label">Signal Date</span><strong>${escapeHtml(text(latestRec.signal_date, "--"))}</strong></div>
      <div><span class="detail-label">Execution Date</span><strong>${escapeHtml(text(latestRec.execution_date, "pending"))}</strong></div>
      <div><span class="detail-label">Pending 批次</span><strong>${escapeHtml(text(pending.length, "0"))}</strong></div>
      <div><span class="detail-label">订单预览</span><strong>${escapeHtml(text(latestRec.order_preview_file, "--"))}</strong></div>
      <div><span class="detail-label">推荐文件</span><strong>${escapeHtml(text(latestRec.recommendation_file, "--"))}</strong></div>
      <div><span class="detail-label">策略契约</span><strong>${escapeHtml(text(latestRec.strategy_contract_version, "legacy"))}</strong></div>
      <div><span class="detail-label">置信度策略</span><strong>${escapeHtml(text(recConfidence.confidence_policy_version, "--"))}</strong></div>
      <div><span class="detail-label">有效槽位 / 单槽权重</span><strong>${escapeHtml(text(recConfidence.selected_count, "--"))} / ${pct(recConfidence.slot_weight, 2)}</strong></div>
      <div><span class="detail-label">证据截止</span><strong>${escapeHtml(text(recConfidence.evidence_as_of, "--"))}</strong></div>
    </div>
    ${monitorBlock("活动推荐", "pending 批次等待下一交易日数据后自动执行", pendingTable)}
    ${monitorBlock("最新目标组合 Target Portfolio", "修复后的 pending 推荐目标组合，不等同于当前已执行持仓", targetTable)}
    ${monitorBlock("调仓订单 Rebalance Orders", "按买入目标优先展示；sell 行是从当前账户退出旧仓，不代表新股票池偏小票", ordersTable)}
    ${monitorBlock("成交回报 Trade Monitor", "Qlib Exchange 推进产生的最近成交", tradesTable)}
  `;

  const ledgerRows = accountHistory.length
    ? accountHistory.map((row) => ({
      trade_date: row.trade_date,
      ending_account_value: row.account_value,
      daily_pnl: row.daily_pnl,
      daily_return: row.daily_return,
      cash: row.cash,
      stock_value: row.stock_value,
      target_stock_exposure: row.risk_metrics?.target_stock_exposure,
      actual_stock_exposure: row.risk_metrics?.actual_stock_exposure,
      target_cash_weight: row.risk_metrics?.target_cash_weight,
      actual_cash_weight: row.risk_metrics?.actual_cash_weight,
    }))
    : (latestExecSummary.ledger_rows || dailySummary.ledger_rows || []);
  const ledgerTable = tableFromRows(ledgerRows, [
    { key: "trade_date", label: "Date" },
    { key: "ending_account_value", label: "Account", render: (row) => shortNumber(row.ending_account_value, 2) },
    { key: "daily_pnl", label: "Daily PnL", render: (row) => signedNumberCell(row.daily_pnl, 2) },
    { key: "daily_return", label: "Daily Ret", render: (row) => pct(row.daily_return, 2) },
    { key: "cash", label: "Cash", render: (row) => shortNumber(row.cash, 2) },
    { key: "stock_value", label: "Stock", render: (row) => shortNumber(row.stock_value, 2) },
    { key: "target_stock_exposure", label: "Target Exp", render: (row) => pct(row.target_stock_exposure, 1) },
    { key: "actual_stock_exposure", label: "Actual Exp", render: (row) => pct(row.actual_stock_exposure, 1) },
    { key: "target_cash_weight", label: "Target Cash", render: (row) => pct(row.target_cash_weight, 1) },
  ], "暂无账本预览。");
  const accountCompareTable = tableFromRows(paperAccounts, [
    { key: "account_id", label: "Account / Model" },
    { key: "trade_date", label: "Date" },
    { key: "account_value", label: "Account Value", render: (row) => shortNumber(row.account_value, 2) },
    { key: "cash", label: "Cash", render: (row) => shortNumber(row.cash, 2) },
    { key: "stock_value", label: "Stock", render: (row) => shortNumber(row.stock_value, 2) },
    { key: "positions", label: "Positions", render: (row) => escapeHtml(text(Object.keys(row.positions || {}).length, "0")) },
  ], "暂无多模型账户快照。");
  const positionTable = tableFromRows(latestExecSummary.position_rows || dailySummary.position_rows || [], [
    { key: "instrument", label: "代码 / 名称", render: symbolCell },
    { key: "shares", label: "Shares", render: (row) => shortNumber(row.shares ?? row.amount, 0) },
    { key: "price", label: "Price", render: (row) => shortNumber(row.price, 2) },
    { key: "market_value", label: "Market Value", render: (row) => shortNumber(row.market_value, 2) },
    { key: "weight", label: "Weight", render: (row) => pct(row.weight, 2) },
  ], "暂无持仓预览。");

  document.getElementById("trading-account").innerHTML = `
    ${hasPendingNewTarget ? `
      <div class="trading-interpretation warn">
        <strong>持仓监控显示 latest execution，不是 latest recommendation。</strong>
        <span>已执行推荐 ${escapeHtml(latestExecRecId)} 和最新推荐 ${escapeHtml(latestRecId)} 不一致；旧仓会在下一次 pending 执行时按调仓订单处理。</span>
      </div>
    ` : ""}
    <div class="account-terminal-card">
      <div>
        <span class="badge ${tradingBadgeClass(latestExec.status)}">${escapeHtml(text(latestExec.status, "no execution"))}</span>
        <h4>${escapeHtml(text(latestExec.trade_date, "暂无交易日"))}</h4>
        <p>Adapter: ${escapeHtml(text(latestExec.adapter, "qlib_exchange_paper"))}</p>
      </div>
      <div class="account-value ${numberTone(execMetrics.daily_pnl)}">
        <span>Ending Account Value</span>
        <strong>${shortNumber(accountValue, 2)}</strong>
        <small>Daily ${pct(execMetrics.daily_return, 2)} · Stock ${shortNumber(accountStockValue, 2)}</small>
      </div>
    </div>
    <div class="terminal-stat-grid">
      ${terminalStat("现金", shortNumber(accountCash, 2), "cash", numberTone(accountCash))}
      ${terminalStat("今日收益", signedNumberCell(execMetrics.daily_pnl, 2), "daily pnl", numberTone(execMetrics.daily_pnl))}
      ${terminalStat("成交笔数", escapeHtml(text(execMetrics.trade_count, "0")), "trades")}
      ${terminalStat("持仓数", escapeHtml(text(execMetrics.position_count ?? Object.keys(latestAccount.positions || {}).length, "0")), "positions")}
      ${terminalStat("股票市值", shortNumber(accountStockValue, 2), "stock value")}
      ${terminalStat("成交成本", shortNumber(execMetrics.cost ?? execMetrics.commission, 2), "cost")}
    </div>
    ${monitorBlock("当前已执行持仓 Position Monitor", "来自 latest Qlib paper execution；若存在 pending 新推荐，这里仍会显示上一笔执行后的旧仓", positionTable)}
    ${monitorBlock("资金曲线 Account Monitor", "每日更新的 Qlib paper ledger", renderPaperAccountCurve(ledgerRows) + ledgerTable)}
    ${monitorBlock("账户账本快照", "每个 account_id 独立维护 Qlib paper account；完整模型部署与缺口比较见页面顶部 Fleet", accountCompareTable)}
    <div class="detail-copy">
      <span class="detail-label">最近 GUI 操作</span>
      <pre>${escapeHtml(JSON.stringify(state.latestTradingResult || result || { status: "waiting" }, null, 2))}</pre>
    </div>
  `;

  document.getElementById("trading-files").innerHTML = renderPathList(filePaths);
}

const PAPER_TRADING_TABS = new Set(["console", "overview", "risk", "plan", "trades"]);
const PAPER_CONSOLE_TABS = new Set(["status", "automation", "accounts", "create", "replay", "diagnostics", "settings"]);

function normalizePaperTradingTab(tab) {
  if (tab === "ops" || tab === "replay") return "console";
  return PAPER_TRADING_TABS.has(tab) ? tab : "overview";
}

function normalizePaperConsoleTab(tab) {
  return PAPER_CONSOLE_TABS.has(tab) ? tab : "status";
}

function setPaperConsoleTab(tab) {
  state.paperConsoleTab = normalizePaperConsoleTab(tab);
  try { window.localStorage?.setItem("fxalpha.paperConsoleTabV1", state.paperConsoleTab); } catch (error) { /* ignore */ }
  document.querySelectorAll("[data-paper-console-tab]").forEach((button) => {
    const active = button.dataset.paperConsoleTab === state.paperConsoleTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  document.querySelectorAll("[data-paper-console-pane]").forEach((pane) => {
    const active = pane.dataset.paperConsolePane === state.paperConsoleTab;
    pane.hidden = !active;
    pane.classList.toggle("active", active);
  });
}

function refreshTradingWorkspace(reason) {
  if (state.activePanel !== "trading") return Promise.resolve();
  return refreshState({ reason }).catch((error) => {
    state.latestTradingResult = { ok: false, error: String(error), _paperUiAction: "workspace_refresh" };
    renderTrading();
  });
}

function paperStatusLabel(value) {
  const key = text(value, "").toLowerCase();
  const labels = {
    active: "运行中",
    paused: "已暂停",
    retired: "已退休",
    completed: "已完成",
    ready: "正常",
    ok: "正常",
    weak: "弱信号",
    strong: "强信号",
    no_trade: "不交易",
    blocked: "已阻断",
    pending: "等待执行",
    superseded: "已终止",
    frozen: "已冻结",
    failed: "失败",
    waiting: "等待中",
    legacy: "旧版策略",
    neutral_unavailable: "业绩样本不足",
    not_checked: "无需检查",
    already_current: "已是最新",
    idle: "空闲",
    reset: "历史重置",
  };
  return labels[key] || text(value, "未知");
}

function paperReasonLabel(reason) {
  const raw = text(reason, "");
  const pendingMatch = raw.match(/^(\d+) pending recommendation\(s\) waiting for execution$/i);
  if (pendingMatch) return `${pendingMatch[1]} 条推荐等待下一交易日执行`;
  if (raw.startsWith("prediction score degenerate for latest recommendation")) {
    const unique = raw.match(/unique_score_count=(\d+)/)?.[1] || "--";
    const required = raw.match(/required_unique>=(\d+)/)?.[1] || "--";
    return `最新预测分数区分度偏低：唯一分数 ${unique}（要求至少 ${required}），系统已按弱信号降低仓位`;
  }
  if (raw.startsWith("model_tree_count_weak:")) return `模型树数量偏少（${raw.split(":")[1]}棵），股票仓位按50%上限控制`;
  if (raw.startsWith("score_unique_below_reference:")) return `预测分数区分度偏低（${raw.split(":")[1]}）`;
  if (raw.startsWith("topk_boundary_tied:")) return "Top20边界存在大量同分，只保留严格高于边界的股票";
  if (raw.includes("execution_date_unresolved") || raw.includes("execution_date_not_available")) return "下一交易日数据尚未进入Qlib，推荐将继续等待";
  return raw;
}

function paperSelectedAccount(accounts = []) {
  if (!accounts.length) return null;
  let account = accounts.find((item) => item.account_id === state.selectedPaperAccountId);
  if (!account) account = accounts.find((item) => item.status === "active") || accounts[0];
  if (account && state.selectedPaperAccountId !== account.account_id) {
    state.selectedPaperAccountId = account.account_id;
    try { window.localStorage?.setItem("fxalpha.paperAccountId", account.account_id); } catch (error) { /* ignore */ }
  }
  return account;
}

async function refreshPaperRiskPolicy(accountId = state.selectedPaperAccountId) {
  const suffix = accountId ? `&account_id=${encodeURIComponent(accountId)}` : "";
  const result = await getJsonSafe(`/trade/risk-policy?history_days=160${suffix}`, { timeoutMs: 12000 });
  state.riskPolicyStatus = keepPreviousOnReadFailure(result, state.riskPolicyStatus);
  return state.riskPolicyStatus;
}

function setPaperTradingTab(tab) {
  state.paperTradingTab = normalizePaperTradingTab(tab);
  try { window.localStorage?.setItem("fxalpha.paperTradingTabV2", state.paperTradingTab); } catch (error) { /* ignore */ }
  const tradingPanel = document.getElementById("panel-trading");
  tradingPanel?.classList.toggle("paper-console-active", state.paperTradingTab === "console");
  document.querySelectorAll("[data-paper-trading-tab]").forEach((button) => {
    const active = button.dataset.paperTradingTab === state.paperTradingTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  document.querySelectorAll("[data-paper-trading-pane]").forEach((pane) => {
    const active = pane.dataset.paperTradingPane === state.paperTradingTab;
    pane.hidden = !active;
    pane.classList.toggle("active", active);
  });
  if (state.paperTradingTab === "console") setPaperConsoleTab(state.paperConsoleTab);
}

function paperAccountPositionRows(account, executionSummary = {}) {
  const snapshot = account?.latest_snapshot || {};
  const names = { ...(account?.security_names || {}) };
  const costBasis = paperPositionCostBasis(account?.daily_trades || {});
  (executionSummary.position_rows || []).forEach((row) => {
    if (row.instrument && row.security_name) names[row.instrument] = row.security_name;
  });
  return paperHoldingRows(snapshot, names).map((row) => {
    const inventory = costBasis.get(row.instrument);
    const shares = Number(row.shares || 0);
    const averageCost = inventory && inventory.shares > 0 ? inventory.cost / inventory.shares : null;
    const positionCost = Number.isFinite(averageCost) ? averageCost * shares : null;
    const marketValue = Number(row.market_value);
    const holdingPnl = Number.isFinite(positionCost) && Number.isFinite(marketValue) ? marketValue - positionCost : null;
    const holdingReturn = Number.isFinite(holdingPnl) && positionCost > 0 ? holdingPnl / positionCost : null;
    return { ...row, average_cost: averageCost, position_cost: positionCost, holding_pnl: holdingPnl, holding_return: holdingReturn };
  });
}

function paperDisplayInstrument(value) {
  const raw = text(value, "--").trim();
  const match = raw.match(/^(\d{6})(sh|sz)$/i);
  return match ? `${match[1]}.${match[2].toUpperCase()}` : raw.toUpperCase();
}

function paperConfidenceSummary(confidence = {}) {
  if (!Object.keys(confidence).length) return "该账户使用旧版固定仓位策略。";
  const selected = Number(confidence.selected_count || 0);
  const stock = pct(confidence.target_stock_exposure, 1);
  const cash = pct(confidence.target_cash_weight, 1);
  if (confidence.confidence_state === "weak") return `当日信号偏弱，仅选择${selected}只股票，目标股票仓位${stock}，其余${cash}保留现金。`;
  if (confidence.confidence_state === "no_trade") return "当日信号不满足生产要求，目标为全现金。";
  return `当日选择${selected}只股票，目标股票仓位${stock}，现金仓位${cash}。`;
}

function syncPaperTargetListHeight() {
  const workspace = document.querySelector("#trading-picks .paper-plan-workspace");
  const targetPortfolio = workspace?.querySelector(".paper-target-portfolio");
  const rebalanceOrders = workspace?.querySelector(".paper-rebalance-orders");
  const targetList = targetPortfolio?.querySelector(".paper-target-list");
  if (!targetPortfolio || !rebalanceOrders || !targetList) return;
  if (!window.matchMedia?.("(min-width: 1181px)").matches) {
    targetList.style.removeProperty("height");
    targetList.style.removeProperty("max-height");
    return;
  }
  const headerHeight = targetPortfolio.querySelector(":scope > header")?.getBoundingClientRect().height || 0;
  const filterHeight = targetPortfolio.querySelector(":scope > .paper-target-filter")?.getBoundingClientRect().height || 0;
  const adjacentHeight = rebalanceOrders.getBoundingClientRect().height;
  const availableHeight = Math.max(280, Math.floor(adjacentHeight - headerHeight - filterHeight - 2));
  const nextHeight = `${availableHeight}px`;
  if (targetList.style.height !== nextHeight) targetList.style.height = nextHeight;
  if (targetList.style.maxHeight !== nextHeight) targetList.style.maxHeight = nextHeight;
}

function observePaperPlanLayout() {
  window.requestAnimationFrame(syncPaperTargetListHeight);
  if (typeof ResizeObserver !== "function") return;
  if (!paperPlanResizeObserver) paperPlanResizeObserver = new ResizeObserver(syncPaperTargetListHeight);
  paperPlanResizeObserver.disconnect();
  const workspace = document.querySelector("#trading-picks .paper-plan-workspace");
  const rebalanceOrders = workspace?.querySelector(".paper-rebalance-orders");
  if (workspace) paperPlanResizeObserver.observe(workspace);
  if (rebalanceOrders) paperPlanResizeObserver.observe(rebalanceOrders);
}

function paperConstraintLabel(reason) {
  const labels = {
    hold_thresh: "未满持有期",
    n_drop_ramp_limit: "新增持仓数量受限",
    gross_exposure_budget_exhausted: "仓位预算已用尽",
    gross_exposure_budget_rounding: "剩余预算不足一手",
    insufficient_cash: "可用现金不足",
    not_tradable: "当日不可交易",
    missing_deal_price: "缺少成交价格",
  };
  const key = text(reason, "").trim().toLowerCase();
  return key ? (labels[key] || "其他执行限制") : "可正常调仓";
}

function paperModelTags(account = {}) {
  const binding = account.model_binding || account.metadata?.model_binding || {};
  const tags = Array.isArray(binding.tags) ? binding.tags.filter(Boolean) : [];
  if (tags.length) return tags;
  if (binding.promotion_label) return [binding.promotion_label];
  return [];
}

function paperModelTagBadges(account = {}) {
  return paperModelTags(account).map((label) => {
    const tone = label === "手工晋升" || label === "研究来源" ? "warn" : "subtle";
    return `<span class="badge ${tone}">${escapeHtml(label)}</span>`;
  }).join("");
}

function paperProductionModelCatalog() {
  const registry = serviceOutputs(state.modelRegistry);
  const production = serviceOutputs(state.modelProduction);
  const tradingProduction = serviceOutputs(serviceOutputs(state.tradingStatus).production_model);
  const activeModel = production.production_model || tradingProduction.production_model || {};
  const activeRunId = text(activeModel.model_run_id, "");
  const rows = [
    ...(registry.items || registry.models || []),
    ...(production.items || production.production_models || []),
    activeModel,
  ];
  const byRunId = new Map();
  rows.forEach((item) => {
    const runId = text(item?.model_run_id, "");
    const status = text(item?.status || item?.asset_status, "").toLowerCase();
    if (!runId || (status && status !== "production")) return;
    byRunId.set(runId, { ...(byRunId.get(runId) || {}), ...item, model_run_id: runId, status: "production" });
  });
  return [...byRunId.values()].sort((a, b) => {
    const activeOrder = Number(text(b.model_run_id, "") === activeRunId) - Number(text(a.model_run_id, "") === activeRunId);
    return activeOrder
      || text(b.created_at || b.updated_at, "").localeCompare(text(a.created_at || a.updated_at, ""))
      || text(a.model_id, "").localeCompare(text(b.model_id, ""));
  }).map((item) => ({ ...item, is_active: text(item.model_run_id, "") === activeRunId }));
}

function renderPaperFleet() {
  const fleet = serviceOutputs(state.paperFleetStatus);
  const activeAccounts = fleet.active_accounts || fleet.accounts || [];
  const pausedAccounts = fleet.paused_accounts || [];
  const retiredAccounts = fleet.retired_accounts || [];
  const accounts = [...activeAccounts, ...pausedAccounts, ...retiredAccounts];
  const account = paperSelectedAccount(accounts);
  const switcher = document.getElementById("paper-account-switcher");
  const context = document.getElementById("paper-account-context");
  const overview = document.getElementById("paper-fleet-overview");
  const comparison = document.getElementById("paper-account-comparison");
  const replay = document.getElementById("paper-replay-center");
  const replayScope = document.getElementById("paper-replay-scope-account");
  const accountActionResult = document.getElementById("paper-account-action-result");
  const accountCreateResult = document.getElementById("paper-account-create-result");
  const productionModelSelect = document.getElementById("paper-account-model-run-select");

  if (productionModelSelect) {
    const currentValue = productionModelSelect.value;
    const modelCatalog = paperProductionModelCatalog();
    productionModelSelect.innerHTML = modelCatalog.length
      ? modelCatalog.map((item) => {
        const runId = text(item.model_run_id, "");
        const label = text(item.display_name, text(item.model_id, "生产模型"));
        const activeLabel = item.is_active ? "当前 · " : "";
        const title = [item.model_id, runId, item.feature_set_id].filter(Boolean).join(" · ");
        return `<option value="${escapeHtml(runId)}" title="${escapeHtml(title)}">${escapeHtml(`${activeLabel}${label}`)}</option>`;
      }).join("")
      : `<option value="">模型库中没有已晋升的生产模型</option>`;
    const selectableValues = new Set(modelCatalog.map((item) => text(item.model_run_id, "")));
    productionModelSelect.value = selectableValues.has(currentValue)
      ? currentValue
      : text(modelCatalog.find((item) => item.is_active)?.model_run_id || modelCatalog[0]?.model_run_id, "");
    productionModelSelect.disabled = modelCatalog.length === 0;
  }

  const renderAccountWriteResult = (node, action) => {
    if (!node) return;
    const result = state.latestTradingResult || {};
    if (result._paperUiAction !== action) {
      node.hidden = true;
      node.innerHTML = "";
      return;
    }
    const outputs = serviceOutputs(result);
    const ok = result.ok !== false && !result.error;
    const changedAccount = outputs.account || {};
    const messages = {
      account_status: ok
        ? `账户 ${text(changedAccount.account_id, "")} 已${changedAccount.status === "paused" ? "暂停" : "恢复运行"}。`
        : `账户状态更新失败：${paperReasonLabel(result.error || "unknown_error")}`,
      account_create: ok
        ? `账户 ${text(changedAccount.account_id, "")} 已创建并完成模型绑定。`
        : `新建任务失败：${paperReasonLabel(result.error || "unknown_error")}`,
    };
    node.hidden = false;
    node.className = `paper-console-action-result ${ok ? "is-ok" : "is-danger"}`;
    node.innerHTML = `<span>${ok ? "操作完成" : "操作未完成"}</span><strong>${escapeHtml(messages[action] || "操作结果已更新。")}</strong>`;
  };
  renderAccountWriteResult(accountActionResult, "account_status");
  renderAccountWriteResult(accountCreateResult, "account_create");

  if (switcher) {
    switcher.innerHTML = accounts.length ? `
      <label class="paper-account-select">
        <span>账户切换</span>
        <select id="paper-account-select">
          ${accounts.map((item) => `<option value="${escapeHtml(item.account_id)}" ${item.account_id === account?.account_id ? "selected" : ""}>${escapeHtml(item.model_binding?.display_feature_set || item.display_name || item.account_id)} 模拟账户 · ${escapeHtml(paperStatusLabel(item.status))}</option>`).join("")}
        </select>
      </label>
    ` : `<div class="empty-state compact">尚未创建模拟交易账户。</div>`;
  }

  if (context) {
    const deployment = [...(account?.deployments || [])].sort((a, b) => text(b.effective_from).localeCompare(text(a.effective_from)))[0] || {};
    const binding = account?.model_binding || {};
    context.innerHTML = account ? `
      <div class="paper-account-heading">
        <div>
          <span class="paper-account-kicker">固定模型模拟账户</span>
          <strong>${escapeHtml(binding.display_feature_set || "生产模型")} 账户</strong>
          <small>${escapeHtml(binding.model_display_name || account.display_name || account.account_id)}</small>
        </div>
        <div class="paper-account-tags">${paperModelTagBadges(account) || `<span class="badge subtle">${escapeHtml(binding.promotion_label || "模型来源待核对")}</span>`}</div>
        <span class="badge ${tradingBadgeClass(account.status)}">${escapeHtml(paperStatusLabel(account.status))}</span>
      </div>
      <div class="paper-account-facts">
        <div><span>账户 ID</span><strong title="${escapeHtml(account.account_id)}">${escapeHtml(account.account_id)}</strong></div>
        <div><span>绑定模型</span><strong title="${escapeHtml(text(deployment.model_id, "未绑定"))}">${escapeHtml(text(deployment.model_id, "未绑定"))}</strong></div>
        <div><span>Feature Set</span><strong title="${escapeHtml(text(deployment.feature_set_id || binding.feature_set_id, "--"))}">${escapeHtml(text(deployment.feature_set_id || binding.feature_set_id, "--"))}</strong></div>
        <div><span>账户生效日</span><strong>${escapeHtml(text(deployment.effective_from || account.metadata?.inception_date, "--"))}</strong></div>
      </div>
    ` : "";
  }

  if (replayScope) {
    replayScope.textContent = account
      ? `${account.model_binding?.display_feature_set || account.display_name || account.account_id} · ${account.account_id}`
      : "尚未选择账户";
  }

  if (overview) {
    const latestFleet = fleet.latest_fleet_run || {};
    const latestFleetPreflight = serviceOutputs(latestFleet.preflight || {});
    const data = fleet.data || {};
    const latestProcessedDate = latestFleet.target_date
      || latestFleetPreflight.target_date
      || account?.recent_runs?.[0]?.signal_date
      || account?.latest_snapshot?.trade_date;
    const fleetIsCurrent = text(latestFleet.status, "") === "already_current";
    overview.innerHTML = `
      <div class="paper-console-kpi-grid">
        <div><span>账户组状态</span><strong>${escapeHtml(paperStatusLabel(latestFleet.status || fleet.status))}</strong><small>${fleetIsCurrent ? "全部账户已追平生产数据" : "全部生产模拟账户"}</small></div>
        <div><span>账户状态</span><strong>${escapeHtml(text(fleet.active_account_count, "0"))} 运行 · ${escapeHtml(text(fleet.paused_account_count, "0"))} 暂停</strong><small>${escapeHtml(text(fleet.retired_account_count, "0"))} 个退休账户只读保留</small></div>
        <div><span>生产数据</span><strong>${escapeHtml(text(data.qlib_latest, "--"))}</strong><small>${escapeHtml(paperStatusLabel(data.production_health))}</small></div>
        <div><span>最近处理日</span><strong>${escapeHtml(text(latestProcessedDate, "--"))}</strong><small>${fleetIsCurrent ? "自动检查完成 · 无需重复运行" : `写入锁 ${paperStatusLabel(fleet.operation_lock?.status || "ready")}`}</small></div>
      </div>`;
  }

  if (comparison) {
    const rows = accounts.map((item) => {
      const snapshot = item.latest_snapshot || {};
      const initial = Number(item.initial_capital || 0);
      const value = Number(snapshot.account_value);
      const gap = item.gap_summary || {};
      const gapCount = gap.status === "current" ? 0 : "待检查";
      return { ...item, trade_date: snapshot.trade_date, account_value: value, total_return: initial > 0 && Number.isFinite(value) ? value / initial - 1 : null, pending_count: (item.pending_recommendations || []).length, gap_count: gapCount };
    });
    comparison.innerHTML = rows.length ? `
      <div class="paper-account-group-list">
        ${rows.map((row) => `
          <article class="paper-account-group-row ${row.account_id === account?.account_id ? "active" : ""}">
            <button type="button" class="paper-account-link" data-paper-account-id="${escapeHtml(row.account_id)}">
              <strong>${escapeHtml(row.model_binding?.display_feature_set || row.display_name || row.account_id)}</strong>
              <small>${escapeHtml(row.account_id)}</small>
              <span class="paper-account-tags">${paperModelTagBadges(row) || `<b class="badge subtle">${escapeHtml(row.model_binding?.promotion_label || "来源待核对")}</b>`}</span>
            </button>
            <div class="paper-account-group-facts">
              <span><small>运行状态</small><strong class="badge ${tradingBadgeClass(row.status)}">${escapeHtml(paperStatusLabel(row.status))}</strong></span>
              <span><small>账本日期</small><strong>${escapeHtml(text(row.trade_date, "--"))}</strong></span>
              <span><small>账户净值</small><strong>${shortNumber(row.account_value, 2)}</strong></span>
              <span><small>累计收益</small><strong class="${numberTone(row.total_return)}">${pct(row.total_return, 2)}</strong></span>
              <span><small>${row.status === "paused" ? "冻结计划 / 待补" : "待执行 / 待补"}</small><strong>${escapeHtml(text(row.pending_count, "0"))} / ${escapeHtml(text(row.gap_count, "0"))}</strong></span>
            </div>
            <div class="paper-account-group-actions">
              ${row.status === "retired"
                ? `<span class="badge subtle">已归档</span>`
                : `<button type="button" class="tiny-button ${row.status === "active" ? "danger-soft" : "is-primary"}" data-paper-account-status="${row.status === "active" ? "paused" : "active"}" data-paper-account-status-id="${escapeHtml(row.account_id)}">${row.status === "active" ? "暂停账户" : "恢复账户"}</button>`}
            </div>
          </article>`).join("")}
      </div>` : `<div class="empty-state">尚未创建生产模拟账户。</div>`;
  }

  if (replay) {
    const latestOutputs = serviceOutputs(state.latestTradingResult || {});
    const requestedPlan = latestOutputs.plan || (latestOutputs.account_id ? latestOutputs : {});
    const plan = requestedPlan.account_id === account?.account_id ? requestedPlan : {};
    const gap = account?.gap_summary || {};
    const dates = plan.trade_dates || [];
    const blockers = plan.blockers || [];
    const hasExactPlan = plan.account_id === account?.account_id;
    const isCurrent = hasExactPlan ? dates.length === 0 && blockers.length === 0 : gap.status === "current";
    const accountLabel = account?.model_binding?.display_feature_set || account?.display_name || account?.account_id;
    const replayTone = blockers.length ? "danger" : isCurrent ? "ok" : "warn";
    replay.innerHTML = account ? `
      <div class="paper-replay-summary ${blockers.length ? "danger" : isCurrent ? "ok" : "warn"}">
        <div><span>所选账户</span><strong title="${escapeHtml(account.account_id)}">${escapeHtml(accountLabel)}</strong></div>
        <div><span>最后完成</span><strong>${escapeHtml(text(plan.starting_snapshot_date, gap.latest_completed_date || account.latest_snapshot?.trade_date || "暂无"))}</strong></div>
        <div><span>待补交易日</span><strong>${escapeHtml(hasExactPlan ? text(plan.trade_date_count, "0") : (isCurrent ? "0" : "待检查"))}</strong></div>
        <div><span>处理方式</span><strong>${hasExactPlan ? (dates.length ? (plan.automatic ? "可自动补跑" : "需要人工确认") : "历史已补齐") : (isCurrent ? "无需补跑" : "先生成精确计划")}</strong></div>
      </div>
      <div class="paper-replay-decision ${escapeHtml(replayTone)}">
        <span>${blockers.length ? "存在阻断" : isCurrent ? "账户已是最新" : dates.length ? "补跑计划已就绪" : "等待缺口检查"}</span>
        <strong>${blockers.length ? "当前禁止执行补跑" : isCurrent ? "无需补跑" : dates.length ? `${dates.length} 个交易日待处理` : "先在下方检查精确日期"}</strong>
        <small>${blockers.length ? escapeHtml(blockers.map((item) => paperReasonLabel(item)).join("；")) : isCurrent ? "账户账本已经推进到当前生产数据日期，执行按钮已锁定。" : "补跑只会处理账本缺口，已完成日期不会覆盖。"}</small>
      </div>
      ${dates.length ? `<div class="paper-replay-dates"><span>待补日期</span><p>${dates.map(escapeHtml).join("、")}</p></div>` : ""}
      ${blockers.length ? `<div class="trading-interpretation warn"><strong>补跑被阻断</strong><span>${escapeHtml(blockers.join("；"))}</span></div>` : ""}
    ` : `<div class="empty-state">请先选择账户。</div>`;

    const replayForm = document.getElementById("paper-replay-form");
    const replayAccountInput = replayForm?.querySelector('[name="account_id"]');
    const replayFromInput = replayForm?.querySelector('[name="from_date"]');
    const replayToInput = replayForm?.querySelector('[name="to_date"]');
    const runReplayButton = document.getElementById("run-paper-replay");
    const planReplayButton = document.getElementById("plan-paper-replay");
    const productionDate = text(fleet.data?.qlib_latest, "");
    if (replayAccountInput) replayAccountInput.value = account?.account_id || "";
    [replayFromInput, replayToInput].filter(Boolean).forEach((input) => {
      if (productionDate) input.max = productionDate;
      input.disabled = !account || state.paperReplayBusy;
    });
    const accountIsActive = account?.status === "active";
    if (planReplayButton) {
      planReplayButton.disabled = !accountIsActive || state.paperReplayBusy;
      planReplayButton.title = accountIsActive ? "检查所选账户的账本缺口" : "只有运行中的账户可以生成补跑计划";
    }
    if (runReplayButton) {
      const canExecute = Boolean(accountIsActive && hasExactPlan && dates.length && blockers.length === 0);
      runReplayButton.disabled = !canExecute || state.paperReplayBusy;
      runReplayButton.title = canExecute ? `执行 ${dates.length} 个缺口交易日` : (!accountIsActive ? "请先恢复账户，再执行补跑" : (isCurrent ? "账户已经是最新，无需补跑" : "请先检查并生成无阻断的补跑计划"));
    }
  }
  return { fleet, accounts, account };
}

function renderTrading() {
  const { fleet, account } = renderPaperFleet();
  renderTradingBackgroundWorkflowSummary();
  renderBackgroundWorkflowStatus("trading-background-workflow-status", ["paper_trading", "data_foundation"]);
  renderBackgroundAutomationActionResult();
  setPaperTradingTab(state.paperTradingTab);
  const trading = serviceOutputs(state.tradingStatus);
  const { latest: dailyLatest, summary: dailySummary } = latestDailyOpsOutputs();
  const data = serviceOutputs(state.data);
  const prediction = trading.prediction?.outputs || serviceOutputs(state.predictionStatus);
  const snapshot = account?.latest_snapshot || {};
  const history = account?.account_history || [];
  const recommendation = account?.latest_recommendation || {};
  const pending = account?.pending_recommendations || [];
  const confidence = recommendation.metrics?.confidence || {};
  const risk = snapshot.risk_metrics || {};
  const riskPolicyHistoryStatus = serviceOutputs(state.riskPolicyStatus);
  const riskPolicyStatus = riskPolicyHistoryStatus.account_id === (account?.account_id || "")
    ? riskPolicyHistoryStatus
    : (trading.risk_policy || {});
  const riskPolicyConfig = riskPolicyStatus.config || {};
  const riskDecision = recommendation.metrics?.risk_policy || riskPolicyStatus.latest_decision || risk.policy || {};
  const selectedId = account?.account_id || "";
  const executionSummary = trading.latest_execution_summary?.metrics?.account_id === selectedId ? trading.latest_execution_summary : {};
  const latestExecution = (trading.latest_qlib_paper_execution || trading.latest_execution || {});
  const executionMatches = latestExecution.account_id === selectedId;
  const execution = executionMatches ? latestExecution : {};
  const executionMetrics = executionSummary.metrics || execution.metrics || {};
  const recommendationMatches = recommendation.account_id && recommendation.account_id === trading.latest_recommendation?.account_id;
  const accountHasOrders = Array.isArray(account?.latest_orders);
  const orders = accountHasOrders ? [...account.latest_orders] : recommendationMatches ? [...(trading.latest_orders || [])] : [];
  const planDetailsLoaded = accountHasOrders || recommendationMatches;
  const positionRows = paperAccountPositionRows(account, executionSummary);
  const accountDailyTrades = account?.daily_trades || {};
  const accountTradeRows = Object.entries(accountDailyTrades)
    .sort(([left], [right]) => right.localeCompare(left))
    .flatMap(([tradeDate, rows]) => (rows || []).map((row) => ({ ...row, trade_date: tradeDate })));
  const tradeRows = accountTradeRows.length ? accountTradeRows : (executionSummary.trade_rows || []);
  const hasLatestAccountTrades = Object.prototype.hasOwnProperty.call(accountDailyTrades, snapshot.trade_date);
  const latestAccountTradeRows = hasLatestAccountTrades
    ? (accountDailyTrades[snapshot.trade_date] || [])
    : tradeRows.filter((row) => row.trade_date === snapshot.trade_date);
  const latestTradeCount = hasLatestAccountTrades
    ? latestAccountTradeRows.length
    : Number(executionMetrics.trade_count ?? latestAccountTradeRows.length);
  const accountValue = Number(snapshot.account_value);
  const initialCapital = Number(account?.initial_capital || 0);
  const totalReturn = initialCapital > 0 && Number.isFinite(accountValue) ? accountValue / initialCapital - 1 : null;
  const cumulativeProfit = Number.isFinite(accountValue) && Number.isFinite(initialCapital) ? accountValue - initialCapital : null;
  const latestHistory = history[history.length - 1] || {};
  const dailyPnl = Number(latestHistory.daily_pnl ?? executionMetrics.daily_pnl);
  const dailyReturn = Number(latestHistory.daily_return ?? executionMetrics.daily_return);
  const actualStock = risk.actual_stock_exposure ?? (accountValue > 0 ? Number(snapshot.stock_value || 0) / accountValue : null);
  const actualCash = risk.actual_cash_weight ?? (accountValue > 0 ? Number(snapshot.cash || 0) / accountValue : null);
  const benchmarkRows = serviceOutputs(state.paperBenchmark).rows || [];

  const dashboard = document.getElementById("trading-summary");
  const stockWidth = Math.max(0, Math.min(100, Number(actualStock || 0) * 100));
  const targetStock = riskDecision.final_stock_cap ?? risk.target_stock_exposure ?? confidence.target_stock_exposure;
  if (dashboard) dashboard.innerHTML = account ? `
    <article class="surface paper-summary-card">
      <div class="paper-summary-three-grid">
        <section class="paper-summary-block paper-summary-equity">
          <span>账户净值</span>
          <strong>${shortNumber(accountValue, 2)}</strong>
          <small>初始资金 ${shortNumber(initialCapital, 0)} · ${positionRows.length} 只持仓</small>
        </section>
        <section class="paper-summary-block paper-summary-returns">
          <div><span>今日盈亏</span><strong class="${numberTone(dailyReturn)}">${Number.isFinite(dailyReturn) ? `${dailyReturn >= 0 ? "+" : ""}${pct(dailyReturn, 2)}` : "--"}</strong><small class="paper-summary-return-amount">${Number.isFinite(dailyPnl) ? `${dailyPnl >= 0 ? "+" : ""}${shortNumber(dailyPnl, 2)}` : "--"}</small></div>
          <div><span>累计收益</span><strong class="${numberTone(totalReturn)}">${Number.isFinite(totalReturn) ? `${totalReturn >= 0 ? "+" : ""}${pct(totalReturn, 2)}` : "--"}</strong><small class="paper-summary-return-amount">${Number.isFinite(cumulativeProfit) ? `${cumulativeProfit >= 0 ? "+" : ""}${shortNumber(cumulativeProfit, 2)}` : "--"}</small></div>
        </section>
        <section class="paper-summary-block paper-summary-allocation-block" style="--paper-current-stock:${stockWidth.toFixed(1)}%">
          <header><span>股票 / 现金仓位</span><strong>${pct(actualStock, 1)} <small>/ ${pct(actualCash, 1)}</small></strong></header>
          <div class="paper-summary-allocation-bar" aria-label="股票仓位 ${pct(actualStock, 1)}，现金仓位 ${pct(actualCash, 1)}"><i></i></div>
          <div class="paper-summary-asset-rows">
            <span>股票 ${shortNumber(snapshot.stock_value, 2)}</span><span>现金 ${shortNumber(snapshot.cash, 2)}</span>
          </div>
          <small>目标股票仓位 ${pct(targetStock, 1)}</small>
        </section>
      </div>
    </article>
  ` : `<div class="empty-state">尚未创建模拟账户。</div>`;

  const marketRisk = riskDecision.market || {};
  const accountRisk = riskDecision.account || {};
  const marketConfig = riskPolicyConfig.market || {};
  const accountConfig = riskPolicyConfig.account || {};
  const riskMode = text(riskPolicyStatus.status, riskDecision.enforced ? "enforced" : "shadow");
  const riskModeLabel = riskMode === "enforced" ? "正式约束" : riskMode === "shadow" ? "影子观察" : "已停用";
  const riskBindingLabels = { model: "模型仓位", market: "市场状态", account: "账户制动" };
  const riskHistory = riskPolicyStatus.history || {};
  const marketHistory = riskHistory.market || [];
  const accountRiskHistory = riskHistory.account || [];
  const capHistory = riskHistory.caps || [];
  const thresholds = riskHistory.thresholds || {};
  const breadthChart = renderRiskLineChart(marketHistory, [
    { key: "breadth_short", label: `${marketConfig.short_window || 20}日上涨广度`, color: "#60a5fa" },
    { key: "breadth_long", label: `${marketConfig.long_window || 60}日上涨广度`, color: "#cbd5e1" },
  ], {
    label: "沪深300、中证500、中证1000上涨广度历史",
    minValue: 0,
    maxValue: 1,
    thresholds: [{ value: thresholds.breadth ?? marketConfig.breadth_threshold, label: `压力阈值 ${pct(thresholds.breadth ?? marketConfig.breadth_threshold, 1)}` }],
    showStress: true,
  });
  const volatilityMax = Math.max(0.25, ...(marketHistory || []).flatMap((row) => [Number(row.volatility_short), Number(row.volatility_long)]).filter(Number.isFinite)) * 1.12;
  const volatilityChart = renderRiskLineChart(marketHistory, [
    { key: "volatility_short", label: `${marketConfig.short_window || 20}日年化波动`, color: "#60a5fa" },
    { key: "volatility_long", label: `${marketConfig.long_window || 60}日年化波动`, color: "#fbbf24" },
  ], {
    label: "三指数等权复合收益波动率历史",
    minValue: 0,
    maxValue: volatilityMax,
    thresholds: [{ value: thresholds.volatility ?? marketConfig.volatility_threshold, label: `压力阈值 ${pct(thresholds.volatility ?? marketConfig.volatility_threshold, 1)}` }],
    showStress: true,
  });
  const drawdownMin = Math.min(-0.02, thresholds.drawdown ?? -Number(accountConfig.drawdown_threshold || 0.08), ...(accountRiskHistory || []).map((row) => Number(row.drawdown)).filter(Number.isFinite)) * 1.15;
  const drawdownChart = renderRiskLineChart(accountRiskHistory, [
    { key: "drawdown", label: `${accountConfig.drawdown_window || 60}日滚动回撤`, color: "#60a5fa" },
  ], {
    label: "账户滚动回撤历史",
    minValue: drawdownMin,
    maxValue: 0,
    thresholds: [{ value: thresholds.drawdown ?? -Number(accountConfig.drawdown_threshold || 0.08), label: `制动阈值 ${pct(thresholds.drawdown ?? -Number(accountConfig.drawdown_threshold || 0.08), 1)}` }],
  });
  const modelCapChart = renderRiskLineChart(capHistory, [
    { key: "model_cap", label: "模型仓位上限", color: "#60a5fa" },
  ], {
    label: "模型仓位上限历史",
    minValue: 0,
    maxValue: 1,
  });
  const riskPanel = state.paperTradingTab === "risk" ? document.getElementById("trading-risk-policy") : null;
  if (riskPanel) riskPanel.innerHTML = riskDecision.signal_date ? `
    <section class="paper-plan-summary paper-risk-summary ${marketRisk.market_stress ? "stress" : "normal"}">
      <div class="paper-risk-summary-status"><small>当前决策 · ${escapeHtml(riskDecision.signal_date)}</small><strong>${marketRisk.market_stress ? "市场压力状态已确认" : "市场状态正常"}</strong><span>当前由“${escapeHtml(riskBindingLabels[riskDecision.binding_layer] || riskDecision.binding_layer || "模型仓位")}”主导</span></div>
      <div class="paper-risk-summary-caps">
        <span class="${riskDecision.binding_layer === "model" ? "binding" : ""}"><small>模型仓位上限</small><strong>${pct(riskDecision.model_cap, 1)}</strong></span>
        <span class="${riskDecision.binding_layer === "market" ? "binding" : ""}"><small>市场状态上限</small><strong>${pct(riskDecision.market_cap, 1)}</strong></span>
        <span class="${riskDecision.binding_layer === "account" ? "binding" : ""}"><small>账户制动上限</small><strong>${pct(riskDecision.account_cap, 1)}</strong></span>
      </div>
      <div class="paper-risk-decision"><small>${escapeHtml(riskModeLabel)}</small><strong>${pct(riskDecision.final_stock_cap, 1)}</strong><span>最终股票仓位</span></div>
    </section>
    <section class="paper-risk-history-workspace">
      <header><div><span>Risk Layer History</span><h4>四层风控历史</h4></div><p>浅橙色为已确认市场压力区间；全部曲线均来自同一无前视历史重建。</p></header>
      <div class="paper-risk-history-grid">
        <article><header><div><span>Market Breadth</span><h5>三指数上涨广度</h5></div><small>当前 ${pct(marketRisk.breadth_short, 1)} / ${pct(marketRisk.breadth_long, 1)}</small></header>${breadthChart}</article>
        <article><header><div><span>Market Volatility</span><h5>三指数复合波动率</h5></div><small>当前 ${pct(marketRisk.volatility_short, 1)} / ${pct(marketRisk.volatility_long, 1)}</small></header>${volatilityChart}</article>
        <article><header><div><span>Model Cap History</span><h5>模型仓位上限</h5></div><small>当前 ${pct(riskDecision.model_cap, 1)}</small></header>${modelCapChart}</article>
        <article><header><div><span>Account Brake History</span><h5>账户回撤制动</h5></div><small>当前回撤 ${pct(accountRisk.drawdown, 2)} · 60日高点 ${shortNumber(accountRisk.rolling_high, 2)}</small></header>${drawdownChart}</article>
      </div>
      <div class="paper-risk-history-notes"><span>市场压力：广度同时低于 ${pct(marketConfig.breadth_threshold, 1)} 且波动达到 ${pct(marketConfig.volatility_threshold, 1)}，连续 ${escapeHtml(text(marketConfig.enter_days, "2"))} 日确认。</span><span>账户制动：仅在市场压力确认且 ${accountConfig.drawdown_window || 60} 日回撤达到 -${pct(accountConfig.drawdown_threshold, 1)} 时，将账户上限降至 ${pct(accountConfig.brake_cap, 0)}。</span></div>
    </section>
    <section class="paper-risk-formula"><strong>计算顺序</strong><span>T 日收盘读取沪深300、中证500、中证1000 → 确认市场状态 → 读取最近60个已完成账本日的账户回撤 → 与模型仓位取最小值 → T+1 开盘执行。</span></section>
    <section class="paper-risk-source"><span>后端服务 <strong>${escapeHtml(text(riskHistory.service, "services.trading_service.trading_risk_policy_status"))}</strong></span><span>计算器 <strong>${escapeHtml(text(riskHistory.calculator, riskPolicyConfig.version))}</strong></span><span>序列口径 <strong>${escapeHtml(text(riskHistory.method, "reconstructed_asof_no_lookahead"))}</strong></span><span>截至 <strong>${escapeHtml(text(riskHistory.as_of_date, riskDecision.signal_date))}</strong></span></section>
  ` : `<div class="empty-state">尚无风控决策。新推荐生成后会在这里显示三层仓位上限和触发依据。</div>`;

  const riskConsole = document.getElementById("risk-policy-console-status");
  if (riskConsole) riskConsole.innerHTML = `
    <div class="paper-risk-console-strip"><span><small>当前状态</small><strong>${escapeHtml(riskModeLabel)}</strong></span><span><small>策略版本</small><strong>${escapeHtml(text(riskPolicyConfig.version, "--"))}</strong></span><span><small>配置指纹</small><strong>${escapeHtml(text(riskPolicyStatus.config_hash, "--"))}</strong></span><span><small>最近决策</small><strong>${escapeHtml(text(riskDecision.signal_date, "尚无"))}</strong></span></div>`;
  const riskForm = document.getElementById("risk-policy-form");
  if (riskForm && riskPolicyConfig.version) {
    riskForm.elements.enabled.value = String(Boolean(riskPolicyConfig.enabled));
    riskForm.elements.mode.value = text(riskPolicyConfig.mode, "enforced");
    riskForm.elements.volatility_threshold.value = Number(marketConfig.volatility_threshold || 0) * 100;
    riskForm.elements.stress_cap.value = Number(marketConfig.stress_cap || 0) * 100;
    riskForm.elements.enter_days.value = Number(marketConfig.enter_days || 2);
    riskForm.elements.exit_days.value = Number(marketConfig.exit_days || 3);
    riskForm.elements.drawdown_threshold.value = Number(accountConfig.drawdown_threshold || 0) * 100;
    riskForm.elements.brake_cap.value = Number(accountConfig.brake_cap || 0) * 100;
  }

  const accountPanel = state.paperTradingTab === "overview" ? document.getElementById("trading-account") : null;
  const ledgerRows = history.map((row) => ({
    trade_date: row.trade_date,
    ending_account_value: row.account_value,
    daily_pnl: row.daily_pnl,
    daily_return: row.daily_return,
    cash: row.cash,
    stock_value: row.stock_value,
    position_count: Object.keys(row.positions || {}).length,
    actual_stock_exposure: row.risk_metrics?.actual_stock_exposure,
    actual_cash_weight: row.risk_metrics?.actual_cash_weight,
  }));
  const orderedLedgerRows = [...ledgerRows].sort((left, right) => text(left.trade_date, "").localeCompare(text(right.trade_date, "")));
  const benchmarkByDate = new Map((benchmarkRows || [])
    .map((row) => [text(row.date || row.trade_date, ""), Number(row.close ?? row.adj_close)])
    .filter(([date, value]) => date && Number.isFinite(value)));
  const comparableLedgerRows = orderedLedgerRows.filter((row) => benchmarkByDate.has(row.trade_date));
  const firstBenchmarkValue = comparableLedgerRows.length ? benchmarkByDate.get(comparableLedgerRows[0].trade_date) : null;
  const lastBenchmarkValue = comparableLedgerRows.length ? benchmarkByDate.get(comparableLedgerRows[comparableLedgerRows.length - 1].trade_date) : null;
  const benchmarkReturn = Number(firstBenchmarkValue) > 0 && Number.isFinite(Number(lastBenchmarkValue))
    ? Number(lastBenchmarkValue) / Number(firstBenchmarkValue) - 1
    : null;
  const relativeReturn = Number.isFinite(totalReturn) && Number.isFinite(benchmarkReturn)
    ? (1 + totalReturn) / (1 + benchmarkReturn) - 1
    : null;
  const returnGap = Number.isFinite(totalReturn) && Number.isFinite(benchmarkReturn) ? totalReturn - benchmarkReturn : null;
  const latestTradeValue = latestAccountTradeRows.reduce((sum, row) => sum + Math.abs(Number(row.trade_value || 0)), 0);
  const latestTradeCost = latestAccountTradeRows.reduce((sum, row) => sum + Number(row.cost || 0), 0);
  const cumulativeTradeValue = accountTradeRows.reduce((sum, row) => sum + Math.abs(Number(row.trade_value || 0)), 0);
  const cumulativeTradeCost = accountTradeRows.reduce((sum, row) => sum + Number(row.cost || 0), 0);
  const latestTurnover = accountValue > 0 ? latestTradeValue / accountValue : null;
  const signedAmount = (value, digits = 2) => Number.isFinite(value) ? `${value >= 0 ? "+" : ""}${shortNumber(value, digits)}` : "--";
  const signedPoints = (value, digits = 2) => Number.isFinite(value) ? `${value >= 0 ? "+" : ""}${percentagePoints(value, digits)}` : "--";
  const performanceMetrics = `
    <section class="paper-performance-summary">
      <div class="paper-performance-head"><div><span>Performance Overview</span><h4>账户表现摘要</h4></div><small>账本净值已扣除实际交易成本</small></div>
      <div class="backtest-metric-grid paper-performance-metric-grid">
        <article class="backtest-metric-card tone-return"><span>账户净累计收益</span><strong class="${numberTone(totalReturn)}">${signedPercent(totalReturn, 2)}</strong><small>初始 ${shortNumber(initialCapital, 0)} → 当前 ${shortNumber(accountValue, 2)}</small></article>
        <article class="backtest-metric-card tone-return"><span>累计收益额</span><strong class="${numberTone(cumulativeProfit)}">${signedAmount(cumulativeProfit, 2)}</strong><small>当前净值 − 初始资金</small></article>
        <article class="backtest-metric-card tone-benchmark"><span>沪深 300 同期收益</span><strong class="${numberTone(benchmarkReturn)}">${signedPercent(benchmarkReturn, 2)}</strong><small>与账户曲线相同起止日</small></article>
        <article class="backtest-metric-card tone-excess"><span>累计超额（净值差）</span><strong class="${numberTone(returnGap)}">${signedPoints(returnGap, 2)}</strong><small>相对净值累计 ${signedPercent(relativeReturn, 2)}</small></article>
        <article class="backtest-metric-card tone-return"><span>当日收益</span><strong class="${numberTone(dailyReturn)}">${signedPercent(dailyReturn, 2)}</strong><small>当日盈亏 ${signedAmount(dailyPnl, 2)}</small></article>
        <article class="backtest-metric-card tone-trading"><span>当日成交</span><strong>${latestTradeCount} 笔</strong><small>成交额 ${moneyNumber(latestTradeValue)} · 换手估算 ${pct(latestTurnover, 2)}</small></article>
        <article class="backtest-metric-card tone-cost"><span>当日交易成本</span><strong>${shortNumber(latestTradeCost, 2)}</strong><small>占当前净值 ${pct(accountValue > 0 ? latestTradeCost / accountValue : null, 4)}</small></article>
        <article class="backtest-metric-card tone-cost"><span>累计交易成本</span><strong>${shortNumber(cumulativeTradeCost, 2)}</strong><small>${accountTradeRows.length} 笔 · 累计成交额 ${moneyNumber(cumulativeTradeValue)}</small></article>
      </div>
    </section>`;
  if (accountPanel) accountPanel.innerHTML = account
    ? `${performanceMetrics}<div class="paper-overview-chart">${renderPaperAccountCurve(ledgerRows, benchmarkRows)}</div>`
    : `<div class="empty-state">尚未创建模拟账户。</div>`;

  const constraints = new Map((risk.execution_constraints || []).map((item) => [item.instrument, item.reason]));
  const positionListRows = positionRows.map((row, index) => {
    const constraint = constraints.get(row.instrument);
    const constraintLabel = paperConstraintLabel(constraint);
    const holdingPnl = row.holding_pnl == null ? Number.NaN : Number(row.holding_pnl);
    const holdingReturn = row.holding_return == null ? Number.NaN : Number(row.holding_return);
    const hasHoldingPnl = Number.isFinite(holdingPnl) && Number.isFinite(holdingReturn);
    return `
      <div class="paper-position-list-row">
        <span class="paper-position-rank">${index + 1}</span>
        <span class="paper-position-security"><strong>${escapeHtml(paperDisplayInstrument(row.instrument))}</strong><small>${escapeHtml(row.security_name || "名称待同步")}</small></span>
        <span class="paper-position-pnl ${hasHoldingPnl ? numberTone(holdingPnl) : "flat"}"><strong>${hasHoldingPnl ? `${holdingPnl >= 0 ? "+" : ""}${shortNumber(holdingPnl, 2)}` : "--"}</strong><small>${hasHoldingPnl ? `${holdingReturn >= 0 ? "+" : ""}${pct(holdingReturn, 2)}` : "成本待同步"}</small></span>
        <span class="paper-position-number">${pct(row.weight, 2)}</span>
        <span class="paper-position-number">${shortNumber(row.market_value, 2)}</span>
        <span class="paper-position-number">${shortNumber(row.shares, 0)}</span>
        <span class="paper-position-number">${shortNumber(row.price, 2)}</span>
        <span class="paper-position-number">${shortNumber(row.count_day, 0)} 天</span>
        <span class="paper-position-limit"><b class="badge ${constraint ? "warn" : "subtle"}">${escapeHtml(constraintLabel)}</b></span>
      </div>`;
  }).join("");
  const positionsPanel = state.paperTradingTab === "overview" ? document.getElementById("trading-positions") : null;
  if (positionsPanel) positionsPanel.innerHTML = `
    <div class="paper-position-overview">
      <div><span>持仓股票</span><strong>${positionRows.length} 只</strong></div>
      <div><span>股票总市值</span><strong>${shortNumber(snapshot.stock_value, 2)}</strong></div>
    </div>
    <div class="paper-position-list">
      <div class="paper-position-list-head"><span>排名</span><span>股票</span><span>持仓盈亏</span><span>账户占比</span><span>市值</span><span>持仓数量</span><span>最新价格</span><span>持有天数</span><span>调仓限制</span></div>
      ${positionListRows || `<div class="empty-state">当前账户没有股票持仓。</div>`}
    </div>`;

  const targetRows = orders
    .filter((row) => Number(row.target_weight || 0) > 0)
    .sort((a, b) => Number(b.score || 0) - Number(a.score || 0))
    .map((row, index) => ({ ...row, plan_rank: index + 1 }));
  const actionableOrders = orders.filter((row) => row.action !== "ignore");
  const buyOrders = actionableOrders.filter((row) => row.action === "buy");
  const sellOrders = actionableOrders.filter((row) => row.action === "sell");
  const deferredOrders = orders.filter((row) => row.action === "ignore");
  const buyNotional = buyOrders.reduce((sum, row) => sum + Math.abs(Number(row.estimated_notional || 0)), 0);
  const sellNotional = sellOrders.reduce((sum, row) => sum + Math.abs(Number(row.estimated_notional || 0)), 0);
  const paperPlanActionMeta = (row) => {
    const action = text(row.action, "hold");
    const currentShares = Number(row.current_shares || 0);
    const targetShares = Number(row.target_shares || 0);
    if (action === "buy") return currentShares > 0 ? { label: "加仓", tone: "add" } : { label: "新买", tone: "new" };
    if (action === "sell") return targetShares > 0 ? { label: "减仓", tone: "reduce" } : { label: "卖出", tone: "exit" };
    if (action === "ignore") return { label: "暂不成交", tone: "defer" };
    return { label: "保持不变", tone: "hold" };
  };
  const targetRankByInstrument = new Map(targetRows.map((row) => [row.instrument, row.plan_rank]));
  const targetInstructionRows = orders.map((row) => ({
    ...row,
    plan_rank: targetRankByInstrument.get(row.instrument) || "—",
    action_meta: paperPlanActionMeta(row),
  }));
  const targetFilterDefinitions = [
    { key: "all", label: "全部" },
    { key: "new", label: "新买" },
    { key: "add", label: "加仓" },
    { key: "reduce", label: "减仓" },
    { key: "exit", label: "卖出" },
    { key: "hold", label: "保持不变" },
    { key: "defer", label: "暂不成交" },
  ];
  const validTargetFilters = new Set(targetFilterDefinitions.map((item) => item.key));
  const activeTargetFilter = validTargetFilters.has(state.paperTargetFilter) ? state.paperTargetFilter : "all";
  const targetFilterCounts = targetInstructionRows.reduce((counts, row) => {
    counts[row.action_meta.tone] = (counts[row.action_meta.tone] || 0) + 1;
    return counts;
  }, { all: targetInstructionRows.length });
  const filteredTargetRows = activeTargetFilter === "all"
    ? targetInstructionRows
    : targetInstructionRows.filter((row) => row.action_meta.tone === activeTargetFilter);
  const targetFilterChips = targetFilterDefinitions.map((item) => `
    <button type="button" class="${item.key === activeTargetFilter ? "active" : ""}" data-paper-target-filter="${escapeHtml(item.key)}" aria-pressed="${item.key === activeTargetFilter ? "true" : "false"}">${escapeHtml(item.label)} <b>${escapeHtml(text(targetFilterCounts[item.key], "0"))}</b></button>
  `).join("");
  const targetListRows = filteredTargetRows.map((row) => {
    const actionMeta = row.action_meta;
    return `
      <div class="paper-target-list-row">
        <span class="paper-target-rank">${escapeHtml(row.plan_rank)}</span>
        <span class="paper-target-security"><strong>${escapeHtml(paperDisplayInstrument(row.instrument))}</strong><small>${escapeHtml(row.security_name || "名称待同步")}</small></span>
        <span><b class="badge paper-action-badge action-${escapeHtml(actionMeta.tone)}">${escapeHtml(actionMeta.label)}</b></span>
        <span class="paper-target-number">${pct(row.target_weight, 2)}</span>
        <span class="paper-target-number">${shortNumber(row.target_shares, 0)}</span>
        <span class="paper-target-number">${shortNumber(row.target_value, 0)}</span>
        <span class="paper-target-number">${shortNumber(row.score, 6)}</span>
      </div>`;
  }).join("");
  const renderPaperOrderRows = (rows, side) => rows.map((row) => {
    const actionMeta = paperPlanActionMeta(row);
    return `
      <div class="paper-order-list-row action-${escapeHtml(actionMeta.tone)}">
        <span class="paper-order-security"><strong>${escapeHtml(paperDisplayInstrument(row.instrument))}</strong><small>${escapeHtml(row.security_name || "名称待同步")}</small></span>
        <span class="paper-order-quantity"><small>${shortNumber(row.current_shares, 0)}</small><i>→</i><strong>${shortNumber(row.target_shares, 0)}</strong></span>
        <span class="paper-order-number ${numberTone(row.delta_shares)}">${Number(row.delta_shares) > 0 ? "+" : ""}${shortNumber(row.delta_shares, 0)}</span>
        <span class="paper-order-number">${shortNumber(row.estimated_notional, 2)}</span>
      </div>`;
  }).join("");
  const renderPaperOrderSide = (rows, side, title, englishTitle) => `
    <section class="paper-order-side ${escapeHtml(side)}">
      <header><div><span>${escapeHtml(englishTitle)}</span><h5>${escapeHtml(title)}</h5></div><strong>${rows.length} 只</strong></header>
      <div class="paper-order-list">
        <div class="paper-order-list-head"><span>股票</span><span>当前 → 目标</span><span>变化</span><span>预计金额</span></div>
        ${renderPaperOrderRows(rows, side) || `<div class="empty-state">本次无${escapeHtml(title)}。</div>`}
      </div>
    </section>`;
  const deferredOrderList = deferredOrders.length ? `
    <details class="paper-order-deferred">
      <summary><span>暂不成交 / 保留</span><strong>${deferredOrders.length} 条</strong></summary>
      <div class="paper-order-list">
        <div class="paper-order-list-head"><span>股票</span><span>当前 → 目标</span><span>变化</span><span>预计金额</span></div>
        ${renderPaperOrderRows(deferredOrders, "deferred")}
      </div>
    </details>` : "";
  const ordersList = orders.length ? `
    <div class="paper-order-split">
      ${renderPaperOrderSide(buyOrders, "buy", "买入清单", "Buy Orders")}
      ${renderPaperOrderSide(sellOrders, "sell", "卖出清单", "Sell Orders")}
      ${deferredOrderList}
    </div>` : `<div class="empty-state">${escapeHtml(planDetailsLoaded ? "当前组合无需调仓。" : "正在加载所选账户的调仓明细。")}</div>`;
  const picksPanel = state.paperTradingTab === "plan" ? document.getElementById("trading-picks") : null;
  const reasons = (confidence.reasons || []).map(paperReasonLabel);
  if (picksPanel) picksPanel.innerHTML = recommendation.signal_date ? `
    <div class="paper-plan-summary ${confidence.confidence_state === "weak" ? "weak" : ""}">
      <div class="paper-plan-status">
        <span class="badge ${tradingBadgeClass(recommendation.status || "waiting")}">${escapeHtml(paperStatusLabel(recommendation.status || "waiting"))}</span>
        <div><small>${escapeHtml(recommendation.signal_date)} 信号</small><strong>${recommendation.execution_date ? `${escapeHtml(recommendation.execution_date)} 计划执行` : "等待下一交易日数据"}</strong></div>
      </div>
      <div class="paper-plan-facts">
        <span><small>目标组合</small><strong>${escapeHtml(text(confidence.selected_count, targetRows.length))} 只</strong></span>
        <span><small>预计买入</small><strong>${buyOrders.length} 只</strong></span>
        <span><small>预计卖出</small><strong>${sellOrders.length} 只</strong></span>
        <span><small>暂不成交</small><strong>${deferredOrders.length} 只</strong></span>
      </div>
      <div class="paper-plan-allocation" style="--paper-target-stock:${Math.max(0, Math.min(100, Number(confidence.target_stock_exposure || 0) * 100)).toFixed(1)}%">
        <div><span>股票 ${pct(confidence.target_stock_exposure, 1)}</span><span>现金 ${pct(confidence.target_cash_weight, 1)}</span></div>
        <i><b></b></i>
      </div>
    </div>
    ${reasons.length ? `<details class="paper-plan-rationale"><summary><span><strong>仓位依据</strong><small>${escapeHtml(paperConfidenceSummary(confidence))}</small></span><b>查看 ${reasons.length} 条原因</b></summary><ul>${reasons.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></details>` : ""}
    <div class="paper-plan-workspace">
      <section class="paper-plan-block paper-target-portfolio">
        <header><div><span>Target Portfolio</span><h4>目标组合</h4></div><small>信号日已冻结 · 目标 ${targetRows.length} 只 · 指令 ${orders.length} 条</small></header>
        <div class="paper-target-filter" role="group" aria-label="按调仓操作筛选">${targetFilterChips}</div>
        <div class="paper-target-list">
          <div class="paper-target-list-head"><span>排名</span><span>股票</span><span>操作</span><span>权重</span><span>目标数量</span><span>目标金额</span><span>模型分数</span></div>
          ${targetListRows || `<div class="empty-state">${planDetailsLoaded ? "当前筛选没有对应调仓指令。" : "正在加载目标组合。"}</div>`}
        </div>
      </section>
      <section class="paper-plan-block paper-rebalance-orders">
        <header><div><span>Rebalance Orders</span><h4>调仓清单</h4></div><small>相对于当前已执行持仓</small></header>
        <div class="paper-rebalance-stats">
          <span class="buy"><small>买入估算 · ${buyOrders.length} 只</small><strong>${shortNumber(buyNotional, 0)}</strong></span>
          <span class="sell"><small>卖出估算 · ${sellOrders.length} 只</small><strong>${shortNumber(sellNotional, 0)}</strong></span>
          <span><small>订单合计</small><strong>${orders.length} 条</strong></span>
        </div>
        ${ordersList}
      </section>
    </div>
  ` : `<div class="empty-state">所选账户尚无新推荐。</div>`;
  observePaperPlanLayout();

  const ledgerDates = orderedLedgerRows.map((row) => text(row.trade_date, "")).filter(Boolean);
  const defaultLedgerDate = text(snapshot.trade_date || ledgerDates[ledgerDates.length - 1], "");
  const ledgerQueryDate = text(state.paperLedgerQueryDate || defaultLedgerDate, "");
  const ledgerExportUrl = `/trade/ledger/export?account_id=${encodeURIComponent(account?.account_id || "")}&trade_date=${encodeURIComponent(ledgerQueryDate)}`;
  const selectedLedgerIndex = orderedLedgerRows.findIndex((row) => text(row.trade_date, "") === ledgerQueryDate);
  const selectedLedgerRow = selectedLedgerIndex >= 0 ? orderedLedgerRows[selectedLedgerIndex] : null;
  const selectedHistoryRow = history.find((row) => text(row.trade_date, "") === ledgerQueryDate) || {};
  const previousLedgerRow = selectedLedgerIndex > 0 ? orderedLedgerRows[selectedLedgerIndex - 1] : null;
  const selectedTradeRows = Object.prototype.hasOwnProperty.call(accountDailyTrades, ledgerQueryDate)
    ? (accountDailyTrades[ledgerQueryDate] || [])
    : tradeRows.filter((row) => text(row.trade_date, "") === ledgerQueryDate);
  const selectedEndingValue = Number(selectedLedgerRow?.ending_account_value);
  const selectedDailyPnl = Number(selectedLedgerRow?.daily_pnl);
  const selectedOpeningValue = previousLedgerRow
    ? Number(previousLedgerRow.ending_account_value)
    : selectedEndingValue - selectedDailyPnl;
  const selectedCash = Number(selectedLedgerRow?.cash);
  const selectedStockValue = Number(selectedLedgerRow?.stock_value);
  const selectedDailyReturn = Number(selectedLedgerRow?.daily_return);
  const selectedPositionCount = Number(selectedLedgerRow?.position_count ?? Object.keys(selectedHistoryRow.positions || {}).length);
  const selectedStockExposureRaw = selectedLedgerRow?.actual_stock_exposure;
  const selectedCashExposureRaw = selectedLedgerRow?.actual_cash_weight;
  const selectedStockWeight = selectedStockExposureRaw !== null && selectedStockExposureRaw !== undefined && Number.isFinite(Number(selectedStockExposureRaw))
    ? Number(selectedStockExposureRaw)
    : selectedEndingValue > 0 ? selectedStockValue / selectedEndingValue : null;
  const selectedCashWeight = selectedCashExposureRaw !== null && selectedCashExposureRaw !== undefined && Number.isFinite(Number(selectedCashExposureRaw))
    ? Number(selectedCashExposureRaw)
    : selectedEndingValue > 0 ? selectedCash / selectedEndingValue : null;
  const selectedCumulativePnl = Number.isFinite(selectedEndingValue) ? selectedEndingValue - initialCapital : null;
  const selectedCumulativeReturn = initialCapital > 0 && Number.isFinite(selectedEndingValue) ? selectedEndingValue / initialCapital - 1 : null;
  const selectedBuyRows = selectedTradeRows.filter((row) => text(row.action || row.direction || row.side, "").toLowerCase() === "buy");
  const selectedSellRows = selectedTradeRows.filter((row) => text(row.action || row.direction || row.side, "").toLowerCase() === "sell");
  const selectedBuyNotional = selectedBuyRows.reduce((sum, row) => sum + Math.abs(Number(row.trade_value ?? row.notional ?? 0)), 0);
  const selectedSellNotional = selectedSellRows.reduce((sum, row) => sum + Math.abs(Number(row.trade_value ?? row.notional ?? 0)), 0);
  const selectedTurnover = selectedBuyNotional + selectedSellNotional;
  const selectedTradeCost = selectedTradeRows.reduce((sum, row) => sum + Number(row.cost || 0), 0);
  const selectedIntegrityGap = selectedEndingValue - selectedCash - selectedStockValue;
  const ledgerDateIndex = new Map(orderedLedgerRows.map((row, index) => [text(row.trade_date, ""), index]));

  const selectedTradeList = selectedTradeRows.length ? `
    <div class="paper-record-table paper-day-trade-record-table">
      <div class="paper-record-head paper-day-trade-grid"><span>股票</span><span>方向</span><span>成交价</span><span>数量</span><span>成交额</span><span>费用</span><span>状态</span></div>
      <div class="paper-record-list">
        ${selectedTradeRows.map((row) => {
          const action = text(row.action || row.direction || row.side, "").toLowerCase();
          const actionLabel = action === "buy" ? "买入" : action === "sell" ? "卖出" : text(action, "成交");
          const status = text(row.status, "filled").toLowerCase();
          const statusLabel = status === "filled" ? "已成交" : status === "rejected" ? "已拒绝" : paperStatusLabel(status);
          return `
            <article class="paper-record-row paper-day-trade-grid">
              <span class="paper-record-symbol" data-label="股票"><strong>${escapeHtml(paperDisplayInstrument(row.instrument))}</strong><small>${escapeHtml(row.security_name || "名称待同步")}</small></span>
              <span data-label="方向"><b class="badge paper-trade-action action-${escapeHtml(action)}">${escapeHtml(actionLabel)}</b></span>
              <span class="paper-record-number" data-label="成交价"><strong>${shortNumber(row.price, 2)}</strong></span>
              <span class="paper-record-number" data-label="数量"><strong>${shortNumber(row.filled_amount ?? row.shares, 0)}</strong></span>
              <span class="paper-record-number" data-label="成交额"><strong>${shortNumber(row.trade_value ?? row.notional, 2)}</strong></span>
              <span class="paper-record-number paper-record-fee" data-label="费用"><strong>${shortNumber(row.cost, 2)}</strong></span>
              <span class="paper-trade-status" data-label="状态"><b>${escapeHtml(statusLabel)}</b></span>
            </article>`;
        }).join("")}
      </div>
    </div>` : `<div class="paper-day-empty"><strong>当日无成交</strong><span>该交易日只进行了持仓盯市和账户日结，没有产生买卖成交。</span></div>`;

  const ledgerBrowserList = orderedLedgerRows.length ? `
    <div class="paper-record-table paper-ledger-browser-table">
      <div class="paper-record-head paper-ledger-record-grid"><span>交易日</span><span>期初净值</span><span>期末净值</span><span>现金余额</span><span>股票市值</span><span>当日盈亏</span><span>当日收益</span><span>持仓</span><span>成交</span></div>
      <div class="paper-record-list paper-ledger-browser-list">
        ${[...orderedLedgerRows].reverse().map((row) => {
          const rowDate = text(row.trade_date, "");
          const rowIndex = ledgerDateIndex.get(rowDate) ?? -1;
          const previous = rowIndex > 0 ? orderedLedgerRows[rowIndex - 1] : null;
          const rowEnding = Number(row.ending_account_value);
          const rowPnl = Number(row.daily_pnl);
          const rowOpening = previous ? Number(previous.ending_account_value) : rowEnding - rowPnl;
          const rowTrades = accountDailyTrades[rowDate] || [];
          return `
            <button type="button" class="paper-record-row paper-ledger-record-grid paper-ledger-record-row ${rowDate === ledgerQueryDate ? "active" : ""}" data-paper-ledger-date="${escapeHtml(rowDate)}">
              <span><strong>${escapeHtml(rowDate)}</strong><small>${rowDate === ledgerQueryDate ? "当前查看" : "查看详情"}</small></span>
              <span class="paper-record-number"><strong>${shortNumber(rowOpening, 2)}</strong></span>
              <span class="paper-record-number"><strong>${shortNumber(rowEnding, 2)}</strong></span>
              <span class="paper-record-number"><strong>${shortNumber(row.cash, 2)}</strong></span>
              <span class="paper-record-number"><strong>${shortNumber(row.stock_value, 2)}</strong></span>
              <span class="paper-record-number"><strong class="${numberTone(rowPnl)}">${rowPnl >= 0 ? "+" : ""}${shortNumber(rowPnl, 2)}</strong></span>
              <span class="paper-record-number"><strong class="${numberTone(row.daily_return)}">${pct(row.daily_return, 2)}</strong></span>
              <span class="paper-record-number"><strong>${escapeHtml(text(row.position_count, "0"))} 只</strong></span>
              <span class="paper-record-number"><strong>${rowTrades.length} 笔</strong></span>
            </button>`;
        }).join("")}
      </div>
    </div>` : `<div class="empty-state">暂无账户账本。</div>`;

  const tradesPanel = state.paperTradingTab === "trades" ? document.getElementById("trading-trades") : null;
  if (tradesPanel) tradesPanel.innerHTML = account ? `
    <form class="paper-ledger-query" id="paper-ledger-query-form">
      <div class="paper-ledger-query-title"><span>Account Ledger Query</span><h4>账户流水查询</h4><small>按交易日读取账户日结与逐笔成交</small></div>
      <label><span>查询交易日</span><input class="paper-ledger-date-input" type="date" name="trade_date" value="${escapeHtml(ledgerQueryDate)}" min="${escapeHtml(ledgerDates[0] || "")}" max="${escapeHtml(ledgerDates[ledgerDates.length - 1] || "")}" /></label>
      <button type="submit" class="primary">查询</button>
      <button type="button" class="ghost" data-paper-ledger-latest="${escapeHtml(ledgerDates[ledgerDates.length - 1] || "")}">最近一日</button>
      <a class="button ghost paper-ledger-export" href="${escapeHtml(ledgerExportUrl)}" download>导出 Excel</a>
      <div class="paper-ledger-query-range"><span>可查询 ${ledgerDates.length} 个账本日</span><small>${escapeHtml(ledgerDates[0] || "--")} — ${escapeHtml(ledgerDates[ledgerDates.length - 1] || "--")}</small></div>
    </form>
    ${selectedLedgerRow ? `
      <section class="paper-day-snapshot">
        <header>
          <div><span>Daily Account Snapshot</span><h4>${escapeHtml(ledgerQueryDate)} 日结快照</h4></div>
          <div class="paper-day-snapshot-status"><b><i></i> 日结完成</b><small>${selectedTradeRows.length ? `${selectedTradeRows.length} 笔成交已入账` : "当日无成交，仅盯市结算"}</small></div>
        </header>
        <div class="paper-day-metric-grid">
          <article><span>期初净值</span><strong>${shortNumber(selectedOpeningValue, 2)}</strong><small>上一账本日结转</small></article>
          <article><span>期末净值</span><strong>${shortNumber(selectedEndingValue, 2)}</strong><small>现金与股票资产合计</small></article>
          <article><span>现金余额</span><strong>${shortNumber(selectedCash, 2)}</strong><small>现金仓位 ${pct(selectedCashWeight, 1)}</small></article>
          <article><span>股票市值</span><strong>${shortNumber(selectedStockValue, 2)}</strong><small>${selectedPositionCount} 只 · 股票仓位 ${pct(selectedStockWeight, 1)}</small></article>
          <article class="paper-day-return-metric"><span>当日盈亏</span><strong class="${numberTone(selectedDailyReturn)}">${selectedDailyReturn >= 0 ? "+" : ""}${pct(selectedDailyReturn, 2)}</strong><small class="${numberTone(selectedDailyPnl)}">金额 ${selectedDailyPnl >= 0 ? "+" : ""}${shortNumber(selectedDailyPnl, 2)}</small></article>
          <article class="paper-day-return-metric"><span>累计收益</span><strong class="${numberTone(selectedCumulativeReturn)}">${selectedCumulativeReturn >= 0 ? "+" : ""}${pct(selectedCumulativeReturn, 2)}</strong><small class="${numberTone(selectedCumulativePnl)}">金额 ${selectedCumulativePnl >= 0 ? "+" : ""}${shortNumber(selectedCumulativePnl, 2)}</small></article>
          <article><span>成交概览</span><strong>${selectedTradeRows.length} 笔</strong><small>买入 ${selectedBuyRows.length} · 卖出 ${selectedSellRows.length} · 成交额 ${shortNumber(selectedTurnover, 2)}</small></article>
          <article><span>交易成本</span><strong>${shortNumber(selectedTradeCost, 2)}</strong><small>买入 ${shortNumber(selectedBuyNotional, 2)} · 卖出 ${shortNumber(selectedSellNotional, 2)}</small></article>
        </div>
        <div class="paper-day-asset-check ${Math.abs(selectedIntegrityGap) <= 0.02 ? "ok" : "warn"}">
          <span>资产校验</span><strong>${shortNumber(selectedCash, 2)} 现金 + ${shortNumber(selectedStockValue, 2)} 股票 = ${shortNumber(selectedEndingValue, 2)} 净值</strong><small>${Math.abs(selectedIntegrityGap) <= 0.02 ? "账本平衡" : `存在 ${shortNumber(selectedIntegrityGap, 4)} 差额，请检查账本`}</small>
        </div>
      </section>
      <section class="paper-record-block paper-day-trades-block">
        <header><div><span>Daily Trades</span><h4>当日成交明细</h4></div><small>${escapeHtml(ledgerQueryDate)} · 买入 ${selectedBuyRows.length} 笔 / 卖出 ${selectedSellRows.length} 笔</small></header>
        ${selectedTradeList}
      </section>
    ` : `<div class="paper-ledger-query-empty"><strong>${escapeHtml(ledgerQueryDate || "所选日期")} 没有账户日结</strong><span>请选择下方账本中已有的交易日；周末、节假日或尚未完成日结的日期不会产生记录。</span></div>`}
    <section class="paper-record-block paper-ledger-browser">
      <header><div><span>Account Ledger</span><h4>日结账本</h4></div><small>共 ${orderedLedgerRows.length} 个交易日 · 点击任意一行查看</small></header>
      ${ledgerBrowserList}
    </section>
  ` : `<div class="empty-state">请先选择模拟账户。</div>`;

  const warnings = [...(recommendation.warnings || [])];
  const productionValidation = trading.production_validation_summary || dailySummary.production_validation_summary || {};
  const advice = tradingNextAdvice({ dailyLatest, trading, prediction, warnings, productionValidation, pendingRecommendations: pending, recommendation, snapshot });
  const renderConsoleStatus = state.paperTradingTab === "console" && state.paperConsoleTab === "status";
  const adviceEl = renderConsoleStatus ? document.getElementById("trading-advice") : null;
  if (adviceEl) adviceEl.innerHTML = `
    <div class="trading-advice paper-console-decision ${escapeHtml(advice.tone)}">
      <div class="paper-console-decision-state"><span>当前判断</span><strong class="badge ${tradingBadgeClass(advice.tone)}">${escapeHtml(advice.badge)}</strong></div>
      <div class="paper-console-decision-copy"><strong>${escapeHtml(advice.title)}</strong><p>${escapeHtml(advice.body)}</p></div>
    </div>`;
  const dataDates = fleet.data || dailySummary.data_latest_dates || {};
  const productionDate = dataDates.qlib_latest || dataDates.qlib;
  const flow = renderConsoleStatus ? document.getElementById("trading-flow") : null;
  if (flow) flow.innerHTML = [
    { label: "1 · 生产数据", value: productionDate, note: paperStatusLabel(dataDates.production_health || "ready"), tone: dataDates.production_health === "ready" ? "ok" : "warn" },
    { label: "2 · 模型绑定", value: account?.model_binding?.display_feature_set || recommendation.model_id || prediction.run_context?.model_id, note: recommendation.model_id ? "当前账户已绑定" : "等待模型绑定", tone: recommendation.model_id ? "ok" : "warn" },
    { label: "3 · 调仓计划", value: recommendation.signal_date, note: pending.length ? `${pending.length} 条 · 等待下一交易日行情` : paperStatusLabel(recommendation.status || "waiting"), tone: pending.length ? "waiting" : "ok" },
    { label: "4 · 账户账本", value: snapshot.trade_date, note: snapshot.trade_date && snapshot.trade_date === productionDate ? "已追平生产数据" : (snapshot.trade_date ? "等待数据推进" : "暂无账本"), tone: snapshot.trade_date && snapshot.trade_date === productionDate ? "ok" : "waiting" },
  ].map((item) => `<article class="flow-card ${escapeHtml(item.tone)}"><span>${escapeHtml(item.label)}</span><strong title="${escapeHtml(text(item.value, "--"))}">${escapeHtml(text(item.value, "--"))}</strong><small>${escapeHtml(item.note)}</small></article>`).join("");
  const riskEl = renderConsoleStatus ? document.getElementById("trading-risk") : null;
  const attentionItems = [...new Set(warnings.map((item) => paperReasonLabel(item)).filter(Boolean))];
  if (riskEl) riskEl.innerHTML = attentionItems.length ? `
    <section class="paper-console-attention">
      <header><strong>需要关注</strong><span>以下是所选账户的等待或降级提示，不等于硬性阻断</span></header>
      <ul>${attentionItems.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
    </section>` : `
    <section class="paper-console-attention is-ok"><strong>当前没有硬性阻断</strong><span>系统会按定时器继续推进，无需手工执行。</span></section>`;
  const dailyOps = state.paperTradingTab === "console" && state.paperConsoleTab === "diagnostics" ? document.getElementById("trading-daily-ops") : null;
  const latestFleetRun = fleet.latest_fleet_run || {};
  const latestFleetRunOutputs = serviceOutputs(latestFleetRun.preflight || {});
  const latestFleetRunDate = latestFleetRun.target_date || latestFleetRunOutputs.target_date || snapshot.trade_date;
  if (dailyOps) dailyOps.innerHTML = `
    <div class="detail-grid terminal-detail-grid">
      <div><span class="detail-label">账户组最近检查</span><strong>${escapeHtml(paperStatusLabel(latestFleetRun.status || fleet.status))}</strong></div>
      <div><span class="detail-label">最近处理日</span><strong>${escapeHtml(text(latestFleetRunDate, "--"))}</strong></div>
      <div><span class="detail-label">生产数据日期</span><strong>${escapeHtml(text(dataDates.qlib_latest || dataDates.qlib, "--"))}</strong></div>
      <div><span class="detail-label">模型验证</span><strong>${escapeHtml(paperStatusLabel(productionValidation.status))}</strong></div>
      <div><span class="detail-label">硬性阻断</span><strong>${escapeHtml((productionValidation.hard_blocks || []).join("；") || "无")}</strong></div>
      <div><span class="detail-label">当前账户</span><strong>${escapeHtml(selectedId || "--")}</strong></div>
      <div><span class="detail-label">旧版日切记录</span><strong>${escapeHtml(paperStatusLabel(dailyLatest.status || serviceOutputs(state.dailyOpsStatus).status))}</strong></div>
      <div><span class="detail-label">旧版记录时间</span><strong>${escapeHtml(text(dailyLatest.generated_at, "--"))}</strong></div>
    </div>
    <div class="detail-copy terminal-log"><span class="detail-label">历史运行命令</span><pre>${escapeHtml((dailyLatest.commands_run || []).join("\n") || "当前快照没有可展示的历史命令；运行条件请使用上方只读检查。")}</pre></div>`;
  const filePaths = {
    recommendation: recommendation.recommendation_file,
    orders_preview: recommendation.order_preview_file,
    portfolio_decision: recommendation.decision_file || recommendation.target_file,
    ledger: snapshot.output_files?.ledger_file,
    trades: snapshot.output_files?.trades_file,
    positions: snapshot.output_files?.holdings_file,
    account_state: snapshot.output_files?.account_state_file,
  };
  const files = state.paperTradingTab === "console" && state.paperConsoleTab === "diagnostics" ? document.getElementById("trading-files") : null;
  if (files) files.innerHTML = renderPathList(filePaths);

  const accountInput = document.querySelector('#paper-replay-form [name="account_id"]');
  if (accountInput && !accountInput.value && selectedId) accountInput.value = selectedId;
}

function dataFoundationPanelIsVisible() {
  const visiblePanel = document.querySelector(".panel.active")?.id?.replace(/^panel-/, "");
  return (visiblePanel || state.activePanel) === "data-foundation";
}

function setDataFoundationTab(tab) {
  state.dataFoundationTab = ["status", "live", "query"].includes(tab) ? tab : "status";
  localStorage.setItem("fxalpha-data-foundation-tab", state.dataFoundationTab);
  renderDataFoundation();
  if (state.dataFoundationTab === "live") {
    refreshDataLive().catch((error) => console.error("data live refresh failed", error));
  }
  if (state.dataFoundationTab === "query" && !state.dataQueryFields) {
    refreshDataQueryFields().catch((error) => console.error("data query fields refresh failed", error));
  }
}

function renderDataFoundationNav() {
  document.querySelectorAll("[data-data-foundation-tab]").forEach((button) => {
    const active = button.dataset.dataFoundationTab === state.dataFoundationTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  [
    ["status", "data-foundation-status-panel"],
    ["live", "data-foundation-live-panel"],
    ["query", "data-foundation-query-panel"],
  ].forEach(([tab, id]) => {
    const panel = document.getElementById(id);
    if (!panel) return;
    panel.hidden = tab !== state.dataFoundationTab;
  });
}

function renderDataFoundationSummary(data, quality, schemaSummary, factorAudit, staleSample) {
  const qualityPassed = quality.passed;
  const qualityText = qualityPassed === true ? "通过" : qualityPassed === false ? "异常" : "未知";
  const limitPrice = quality.limit_price_quality || {};
  const qualityNote = quality.latest_trade_date
    ? `质量日期 ${text(quality.latest_trade_date, "--")}`
    : `${text((quality.issues || []).length, "0")} issues · ${text((quality.warnings || []).length, "0")} warnings`;
  appendMetricCards(document.getElementById("data-foundation-summary"), [
    { label: "生产最新日", value: text(data.snapshot?.latest_hdf5_trade_date, "--"), note: "Tushare HDF / Qlib 对齐基准" },
    { label: "覆盖率", value: pct(data.snapshot?.quantgpt_latest_coverage_ratio, 2), note: `${text(data.snapshot?.quantgpt_stock_parquet_count, "0")} 只股票 parquet` },
    { label: "质量门", value: qualityText, note: qualityNote },
    { label: "涨跌停价", value: pct(limitPrice.coverage_ratio, 3), note: `missing ${text(limitPrice.missing_row_count, "0")} · no-limit ${text(limitPrice.structural_no_limit_row_count, "0")}` },
    { label: "Schema", value: text(schemaSummary.schema_version, "legacy"), note: text(schemaSummary.price_mode, "unknown") },
  ]);
}

function dataQualityTone(value, inverse = false) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "warn";
  const metric = inverse ? 1 - number : number;
  if (metric >= 0.995) return "ok";
  if (metric >= 0.94) return "warn";
  return "danger";
}

function dataQualityMeter(label, value, note, options = {}) {
  const number = Number(value);
  const known = Number.isFinite(number);
  const ratio = known ? Math.min(100, Math.max(0, (options.inverse ? 1 - number : number) * 100)) : 0;
  const display = known ? `${ratio.toFixed(options.digits ?? 2)}%` : "--";
  return `
    <article class="data-quality-meter ${dataQualityTone(value, Boolean(options.inverse))}">
      <div>
        <span class="detail-label metric-label">${escapeHtml(label)}</span>
        <strong class="metric-value">${escapeHtml(display)}</strong>
        <small class="metric-note">${escapeHtml(note)}</small>
      </div>
      <div class="data-quality-bar"><i style="width:${ratio.toFixed(1)}%"></i></div>
    </article>
  `;
}

function dataQualityFact(label, value, note, tone = "subtle") {
  return `
    <article class="data-quality-fact ${tone}">
      <span class="detail-label metric-label">${escapeHtml(label)}</span>
      <strong class="metric-value">${escapeHtml(text(value, "--"))}</strong>
      <small class="metric-note">${escapeHtml(text(note, ""))}</small>
    </article>
  `;
}

function renderDataFoundationStatus(data, quality, schemaSummary, factorAudit, staleSample) {
  const target = document.getElementById("data-foundation-detail");
  if (!target) return;
  const latestActivity = quality.latest_code_activity || {};
  const metadataQuality = quality.metadata_quality || {};
  const metadata = metadataQuality.metadata || {};
  const benchmarkQuality = quality.benchmark_index_quality || {};
  const limitPrice = quality.limit_price_quality || {};
  const benchmarkChecks = benchmarkQuality.checks || [];
  const statusFields = metadata.status_fields || {};
  const issues = quality.issues || [];
  const warnings = quality.warnings || [];
  const staleExamples = latestActivity.stale_examples || [];
  const totalRows = Number(quality.n_rows);
  const qualityPassed = quality.passed === true;
  const qualityTone = quality.passed === false ? "danger" : qualityPassed ? "ok" : "warn";
  const latestStockCount = Number(latestActivity.latest_day_stock_count);
  const stockCount = Number(latestActivity.stock_code_count);
  const latestCoverage = stockCount > 0 ? latestStockCount / stockCount : null;
  const adjustedMismatchCount = Object.values(factorAudit.field_mismatches || {}).reduce((sum, item) => sum + Number(item?.mismatch_count || 0), 0)
    + Number(factorAudit.adj_pre_close_mismatch_count || 0);
  const benchmarkOk = benchmarkChecks.length
    ? benchmarkChecks.every((item) => item.present && Object.values(item.core_nulls || {}).every((count) => Number(count || 0) === 0))
    : null;
  const limitPriceOk = limitPrice.passed === true;
  target.innerHTML = `
    <section class="data-status-board">
      <section class="data-quality-hero">
        <div>
          <p class="eyebrow">Production Quality Cockpit</p>
          <h3>${qualityPassed ? "质量检查通过" : quality.passed === false ? "质量检查异常" : "质量检查未知"}</h3>
          <p>${escapeHtml(text(quality.latest_trade_date || data.snapshot?.latest_hdf5_trade_date, "--"))} · ${escapeHtml(totalRows ? totalRows.toLocaleString("en-US") : "--")} rows · ${escapeHtml(text(metadata.package_id || data.current_dataset?.production_package_id, "current production"))}</p>
        </div>
        <span class="status-chip data-quality-status ${qualityTone}"><i></i>${escapeHtml(qualityPassed ? "passed" : quality.passed === false ? "failed" : "unknown")}</span>
      </section>

      <section class="data-quality-fact-grid">
        ${dataQualityFact("生产最新日", data.snapshot?.latest_hdf5_trade_date, `Qlib ${text(data.snapshot?.latest_qlib_trade_date, "--")}`, "primary")}
        ${dataQualityFact("最新日股票覆盖", Number.isFinite(latestCoverage) ? pct(latestCoverage, 2) : "--", `${text(latestActivity.latest_day_stock_count, "0")} / ${text(latestActivity.stock_code_count, "0")} stocks`, dataQualityTone(latestCoverage))}
        ${dataQualityFact("QuantGPT 覆盖", pct(quality.quantgpt_coverage_ratio, 2), `${text(data.snapshot?.quantgpt_stock_parquet_count, "0")} parquet`, dataQualityTone(quality.quantgpt_coverage_ratio))}
        ${dataQualityFact("涨跌停价覆盖", pct(limitPrice.coverage_ratio, 3), `missing ${text(limitPrice.missing_row_count, "0")} · official ${text(limitPrice.official_row_count, "0")}`, limitPrice.passed === false ? "danger" : limitPriceOk ? "ok" : "warn")}
        ${dataQualityFact("滞后股票", text(latestActivity.stale_stock_count ?? quality.quantgpt_stale_stock_count, "0"), `recent ${text(latestActivity.recent_stale_stock_count, "0")} · long ${text(latestActivity.long_stale_stock_count, "0")}`, Number(latestActivity.stale_stock_count || 0) ? "warn" : "ok")}
      </section>

      <section class="data-quality-grid">
        <article class="data-quality-panel data-quality-panel-fields">
          <div class="section-head compact"><div><p class="eyebrow">Field Completeness</p><h3>字段完整性</h3></div></div>
          <div class="data-quality-meter-grid">
            ${dataQualityMeter("行情核心字段", quality.market_core_max_missing_pct, `max missing ${pct(quality.market_core_max_missing_pct, 3)}`, { inverse: true, digits: 2 })}
            ${dataQualityMeter("估值/基本面字段", quality.fundamental_max_missing_pct, `max missing ${pct(quality.fundamental_max_missing_pct, 2)}`, { inverse: true, digits: 2 })}
            ${dataQualityMeter("融资融券字段", quality.margin_max_missing_pct, `max missing ${pct(quality.margin_max_missing_pct, 2)}`, { inverse: true, digits: 2 })}
            ${dataQualityMeter("涨跌停价字段", limitPrice.coverage_ratio, `official ${text(limitPrice.official_row_count, "0")} · no-limit ${text(limitPrice.structural_no_limit_row_count, "0")}`, { digits: 3 })}
            ${dataQualityMeter("零收盘价检查", quality.zero_close_ratio, `zero close ${pct(quality.zero_close_ratio, 4)}`, { inverse: true, digits: 2 })}
          </div>
        </article>

        <article class="data-quality-panel data-quality-panel-schema">
          <div class="section-head compact"><div><p class="eyebrow">Schema & Metadata</p><h3>结构与元数据</h3></div></div>
          <div class="data-quality-schema-list">
            <div><span>Schema</span><strong>${escapeHtml(text(schemaSummary.schema_version, "legacy"))}</strong><small>${escapeHtml(text(schemaSummary.price_mode, "unknown"))}</small></div>
            <div><span>Adjusted Price</span><strong>${escapeHtml(text(schemaSummary.adjusted_price_mode, "unknown"))}</strong><small>${escapeHtml(text(factorAudit.mode, "unknown"))}</small></div>
            <div><span>Limit Source</span><strong>${limitPriceOk ? "official" : limitPrice.passed === false ? "gap" : "unknown"}</strong><small>${escapeHtml(`structural ${text(limitPrice.structural_no_limit_row_count, "0")} · missing ${text(limitPrice.missing_row_count, "0")}`)}</small></div>
            <div><span>Status Fields</span><strong>${escapeHtml(Object.keys(statusFields).length ? "list/st" : "--")}</strong><small>${escapeHtml(`list ${text((statusFields.list_status || []).join("/"), "--")} · st ${text((statusFields.st_status || []).join("/"), "--")}`)}</small></div>
            <div><span>Metadata</span><strong>${metadataQuality.present ? "present" : "missing"}</strong><small>${escapeHtml(text(metadata.package_kind || metadata.source, "--"))}</small></div>
          </div>
        </article>

        <article class="data-quality-panel data-quality-panel-benchmark">
          <div class="section-head compact"><div><p class="eyebrow">Benchmark & Adjustment</p><h3>基准与复权</h3></div></div>
          <div class="data-quality-check-list">
            <div><span>Benchmark</span><strong class="badge ${benchmarkOk === false ? "danger" : benchmarkOk === true ? "ok" : "warn"}">${benchmarkOk === false ? "issue" : benchmarkOk === true ? "clean" : "unknown"}</strong></div>
            ${(benchmarkChecks.length ? benchmarkChecks : [{ code: "--", latest_date: "--", present: false }]).map((item) => `
              <div><span>${escapeHtml(text(item.code, "--"))}</span><strong>${escapeHtml(text(item.latest_date, "--"))}</strong><small>${escapeHtml(item.present ? "present · core nulls 0" : "missing")}</small></div>
            `).join("")}
            <div><span>复权一致性</span><strong class="badge ${adjustedMismatchCount ? "danger" : "ok"}">${adjustedMismatchCount ? `${adjustedMismatchCount} mismatch` : "clean"}</strong><small>${escapeHtml(text(factorAudit.mode, "unknown"))}</small></div>
          </div>
        </article>

        <article class="data-quality-panel data-quality-panel-signals">
          <div class="section-head compact"><div><p class="eyebrow">Signals</p><h3>关注项</h3></div></div>
          <div class="data-quality-alert-list">
            ${[...issues.map((item) => ({ tone: "danger", text: item })), ...warnings.map((item) => ({ tone: "warn", text: item }))].slice(0, 6).map((item) => `<span class="${item.tone}">${escapeHtml(item.text)}</span>`).join("") || `<span class="ok">暂无阻断项</span>`}
          </div>
          <div class="data-stale-sample-list">
            ${staleExamples.slice(0, 8).map((item) => `<span>${escapeHtml(item.code)} <b>${escapeHtml(text(item.last_trade_date, "--"))}</b> <em>${escapeHtml(text(item.calendar_gap_days, "0"))}d</em></span>`).join("") || `<span>暂无滞后样本</span>`}
          </div>
        </article>
      </section>
    </section>
  `;
}

function badgeTone(status) {
  const value = String(status || "").toLowerCase();
  if (/complete|promoted|ok|started|running|dry_run/.test(value)) return "ok";
  if (/fail|block|error/.test(value)) return "danger";
  if (/warn|unknown|not_found|idle/.test(value)) return "warn";
  return "subtle";
}

function dataLiveStageLabel(name) {
  const key = String(name || "").toLowerCase();
  const labels = {
    source_rebuild: "源包构建",
    source_prepare_production: "生产整备",
    merge_production_hdf: "HDF 合并",
    merged_quality_check: "质量门",
    build_compat_outputs: "兼容产物",
    completed: "完成",
    stock_basic: "stock_basic",
    daily: "daily",
    daily_basic: "daily_basic",
    stock_st: "stock_st",
    suspend_d: "suspend_d",
    adj_factor: "adj_factor",
    moneyflow: "moneyflow",
    margin_detail: "margin_detail",
    pro_bar_hfq: "pro_bar_hfq",
    income: "income",
    balancesheet: "balancesheet",
    fina_indicator: "fina_indicator",
    holder_num: "holder_num",
    cyq_perf: "cyq_perf",
    index_daily: "index_daily",
    raw_quality_report: "raw_quality_report",
    assemble_research_daily: "assemble_research_daily",
    quality_report: "quality_report",
  };
  return labels[key] || name;
}

function dataProgressTone(status, ratio = null) {
  const value = String(status || "").toLowerCase();
  if (/complete|promoted|ok/.test(value)) return "ok";
  if (/running|in_progress|started/.test(value)) return "active";
  if (/fail|block|error/.test(value)) return "danger";
  if (/pending|queued|initialized/.test(value)) return "pending";
  if (!Number.isFinite(Number(ratio)) && !value) return "unknown";
  return "unknown";
}

function renderProgressList(stages) {
  const entries = Object.entries(stages || {});
  if (!entries.length) return `<div class="empty-state compact">暂无全量重建阶段进度。</div>`;
  return `<div class="data-progress-grid">${entries.map(([name, stage]) => {
    const cursor = Number(stage?.cursor || 0);
    const total = Number(stage?.total || 0);
    const hasTotal = total > 0;
    const ratio = hasTotal ? Math.min(100, Math.max(0, cursor / total * 100)) : null;
    const tone = dataProgressTone(stage?.status, ratio);
    const progressLabel = hasTotal ? `${cursor}/${total}` : "计数未知";
    const ratioLabel = hasTotal ? `${ratio.toFixed(0)}%` : "仅状态";
    const detailLabel = text(stage?.current_key || stage?.endpoint, hasTotal ? "按 cursor / total 计量" : text(stage?.status, "status only"));
    return `
      <article class="data-progress-card ${hasTotal ? "is-determinate" : "is-status-only"} ${tone}">
        <div class="data-progress-card-head">
          <div class="data-progress-card-title">
            <strong>${escapeHtml(dataLiveStageLabel(name))}</strong>
            <span>${escapeHtml(detailLabel)}</span>
          </div>
          <b class="badge ${badgeTone(stage?.status)}">${escapeHtml(text(stage?.status, "pending"))}</b>
        </div>
        <div class="data-progress-card-meta">
          <span class="data-progress-count">${escapeHtml(progressLabel)}</span>
          <span class="data-progress-ratio">${escapeHtml(ratioLabel)}</span>
        </div>
        ${hasTotal ? `
          <div class="data-progress-meter ${tone}" aria-label="${escapeHtml(name)} ${progressLabel} ${ratio.toFixed(0)}%">
            <i style="width:${ratio.toFixed(1)}%"></i>
          </div>
        ` : `
          <div class="data-progress-meter is-unknown ${tone}" aria-label="${escapeHtml(name)} status only">
            <span>无 total，仅显示状态</span>
          </div>
        `}
      </article>
    `;
  }).join("")}</div>`;
}

function dailyStageProgress(summary) {
  const total = Number(summary?.total_stage_count || 0) || 6;
  const completed = Number(summary?.completed_stage_count || (summary?.completed_stages || []).length || 0);
  return {
    completed,
    total,
    ratio: total > 0 ? Math.min(100, Math.max(0, completed / total * 100)) : 0,
  };
}

function sourceProgressSummary(progress) {
  const stages = Object.values(progress?.stages || {});
  const determinate = stages.filter((stage) => Number(stage?.total || 0) > 0);
  const cursor = determinate.reduce((sum, stage) => sum + Number(stage.cursor || 0), 0);
  const total = determinate.reduce((sum, stage) => sum + Number(stage.total || 0), 0);
  return {
    stageCount: stages.length,
    determinateCount: determinate.length,
    cursor,
    total,
    ratio: total > 0 ? Math.min(100, Math.max(0, cursor / total * 100)) : null,
  };
}

function dataLiveGauge(label, value, note, ratio, tone = "") {
  const determinate = Number.isFinite(Number(ratio));
  const safeRatio = determinate ? Math.min(100, Math.max(0, Number(ratio))) : 0;
  return `
    <article class="data-live-gauge ${tone}">
      <div class="data-live-ring" style="--progress:${safeRatio}">
        <span>${determinate ? `${safeRatio.toFixed(0)}%` : "--"}</span>
      </div>
      <div>
        <span class="detail-label">${escapeHtml(label)}</span>
        <strong>${escapeHtml(value)}</strong>
        <small>${escapeHtml(note)}</small>
      </div>
    </article>
  `;
}

function dataLiveCurrentCard(label, value, packageId, status) {
  return `
    <article class="data-live-current-card">
      <div>
        <span class="detail-label">${escapeHtml(label)}</span>
        <strong>${escapeHtml(text(value, "--"))}</strong>
        <small>${escapeHtml(text(packageId, "current production"))}</small>
      </div>
      <b class="badge ${badgeTone(status)}">${escapeHtml(text(status, "current"))}</b>
    </article>
  `;
}

function renderDataLivePreflight() {
  const target = document.getElementById("data-live-preflight-result");
  if (!target) return;
  const result = state.dataLivePreflightResult;
  if (!result) {
    target.innerHTML = "";
    return;
  }
  const outputs = serviceOutputs(result);
  const resources = outputs.resources || {};
  const network = outputs.network || {};
  const idleState = outputs.idle_state || {};
  const rebuild = outputs.source_rebuild || outputs.tushare_preflight || {};
  const blockers = outputs.blockers || idleState.blockers || [];
  const warnings = [...(outputs.warnings || []), ...(network.warnings || [])];
  const cards = [
    ["网络", network.status || outputs.tushare_preflight?.network?.status, `${text((network.reachable_ips || outputs.tushare_preflight?.network?.reachable_ips || []).join(", "), "direct route")}`],
    ["目标日期", outputs.selected_target_date || outputs.target_date, `replace ${text(outputs.replace_from_date, "--")}`],
    ["交易日/股票", `${text(rebuild.trade_date_count, "0")} / ${text(rebuild.code_count, "0")}`, "trade dates / stock codes"],
    ["资源", resources.disk_ok === false || resources.mem_ok === false ? "warning" : "ok", `disk ${resources.disk_ok === false ? "low" : "ok"} · mem ${resources.mem_ok === false ? "low" : "ok"}`],
    ["生产锁", blockers.length ? `${blockers.length} blocker` : "clear", text((idleState.processes || []).length ? `${idleState.processes.length} processes` : "no running data job")],
  ];
  target.innerHTML = `
    <section class="data-preflight-card ${result.ok && !blockers.length ? "ok" : "warn"}">
      <div class="data-preflight-head">
        <div>
          <p class="eyebrow">Pre Flight</p>
          <h3>${escapeHtml(result.ok && !blockers.length ? "检查通过" : "需要确认")}</h3>
        </div>
        <span class="badge ${result.ok && !blockers.length ? "ok" : "warn"}">${escapeHtml(text(outputs.status, result.ok ? "ok" : "failed"))}</span>
      </div>
      <div class="data-preflight-grid">
        ${cards.map(([label, value, note]) => `
          <article>
            <span>${escapeHtml(text(label))}</span>
            <strong>${escapeHtml(text(value))}</strong>
            <small>${escapeHtml(text(note, ""))}</small>
          </article>
        `).join("")}
      </div>
      ${(blockers.length || warnings.length) ? `
        <div class="data-preflight-notes">
          ${blockers.map((item) => `<span class="danger">${escapeHtml(text(item))}</span>`).join("")}
          ${warnings.slice(0, 6).map((item) => `<span class="warn">${escapeHtml(text(item))}</span>`).join("")}
        </div>
      ` : ""}
    </section>
  `;
}

function liveOperationCard(label, status, value, note) {
  const tone = dataProgressTone(status);
  return `
    <article class="data-live-operation ${tone}">
      <span class="detail-label">${escapeHtml(label)}</span>
      <strong>${escapeHtml(text(value, "--"))}</strong>
      <small>${escapeHtml(text(note, ""))}</small>
      <b class="badge ${badgeTone(status)}">${escapeHtml(text(status, "unknown"))}</b>
    </article>
  `;
}

function renderDailyStageFlow(summary) {
  const stages = ["source_rebuild", "source_prepare_production", "merge_production_hdf", "merged_quality_check", "build_compat_outputs", "completed"];
  const completed = new Set(summary?.completed_stages || []);
  const current = summary?.current_stage || "idle";
  return `
    <div class="data-stage-flow">
      ${stages.map((stage, index) => {
        const status = completed.has(stage) ? "done" : stage === current ? "active" : "pending";
        return `<span class="data-stage-chip ${status}"><b>${index + 1}</b>${escapeHtml(dataLiveStageLabel(stage))}</span>`;
      }).join("")}
    </div>
  `;
}

function renderDataLive() {
  const target = document.getElementById("data-live-detail");
  if (!target) return;
  renderDataLivePreflight();
  const live = serviceOutputs(state.dataLiveStatus);
  const daily = live.daily_update || {};
  const stageSummary = live.daily_stage_summary || daily.stage_summary || {};
  const fullProgress = live.source_progress || {};
  const activeJob = live.active_job || {};
  const latestJob = live.latest_job || {};
  const packageInfo = live.latest_staging_package || {};
  const currentDataset = live.current_production_dataset || {};
  const events = live.events || [];
  const dailyProgress = dailyStageProgress(stageSummary);
  const sourceProgress = sourceProgressSummary(fullProgress);
  const jobStatus = activeJob.status || latestJob.status || live.status;
  target.innerHTML = `
    <section class="data-live-cockpit">
      <article class="data-live-command">
        <div>
          <p class="eyebrow">Update Cockpit</p>
          <h3>${escapeHtml(text(stageSummary.current_stage || daily.current_stage || live.status, "idle"))}</h3>
          <p>${escapeHtml(text(activeJob.job_id || latestJob.job_id || daily.package_id || packageInfo.package_id || "暂无 GUI 异步任务"))}</p>
        </div>
        <span class="status-chip ${badgeTone(jobStatus)}"><i></i>${escapeHtml(text(jobStatus, "idle"))}</span>
      </article>
      <div class="data-live-gauge-grid">
        ${dataLiveGauge("日更阶段", `${dailyProgress.completed}/${dailyProgress.total}`, "按真实 stage_summary 计数", dailyProgress.ratio, "daily")}
        ${dataLiveGauge("源包下载", sourceProgress.ratio === null ? `${sourceProgress.stageCount} 阶段` : `${sourceProgress.cursor}/${sourceProgress.total}`, sourceProgress.ratio === null ? "缺少 total 时不显示假百分比" : "来自 full_rebuild_progress cursor/total", sourceProgress.ratio, "source")}
        ${dataLiveCurrentCard("生产最新日", currentDataset.latest_trade_date || currentDataset.latest_dates?.hdf5 || "--", currentDataset.production_package_id || currentDataset.source || "current production", currentDataset.status || live.status)}
      </div>
    </section>
    <section class="data-live-section data-live-pipeline-card">
      <div class="section-head compact">
        <div><p class="eyebrow">Production Pipeline</p><h3>生产接续流程</h3></div>
        <span class="section-hint">这里展示源包完成后的整备、合并、质检与兼容输出，不会推断不存在的进度。</span>
      </div>
      ${renderDailyStageFlow(stageSummary)}
      <div class="data-live-operation-grid">
        ${liveOperationCard("源包构建", daily.source_rebuild?.status, daily.source_rebuild?.package_id, `${text(daily.source_rebuild?.trade_date_count, "0")} 个交易日 · ${text(daily.source_rebuild?.code_count, "0")} 只股票`)}
        ${liveOperationCard("生产整备", daily.source_prepare_production?.status, `${text(daily.source_prepare_production?.stock_rows, "0")} 股票行`, `${text(daily.source_prepare_production?.index_rows, "0")} 指数行`)}
        ${liveOperationCard("HDF 合并", daily.merge_result?.status || daily.status, `${text(daily.merge_result?.delta_rows, "0")} 行增量`, `保留 ${text(daily.merge_result?.preserved_rows, "0")} · 移除 ${text(daily.merge_result?.removed_rows, "0")}`)}
        ${liveOperationCard("兼容产物", daily.compat_manifest?.status || daily.status, text(daily.snapshot?.latest_quantgpt_trade_date, "--"), `${text(daily.snapshot?.quantgpt_stock_parquet_count, "0")} 个 parquet`)}
      </div>
    </section>
    <section class="data-live-section data-live-columns">
      <article>
        <div class="section-head compact"><div><p class="eyebrow">Tushare Download</p><h3>源包下载进度</h3></div><span class="section-hint">有 total 才显示百分比；没有 total 只显示状态，不拼假进度。</span></div>
        ${renderProgressList(fullProgress.stages || {})}
      </article>
      <article>
        <div class="section-head compact"><div><p class="eyebrow">Events</p><h3>最近事件与原始状态</h3></div></div>
        <div class="data-event-list">
          ${events.length ? events.map((event) => `
            <div><span>${escapeHtml(text(event.stage, "--"))}</span><b class="badge ${badgeTone(event.status)}">${escapeHtml(text(event.status, "--"))}</b></div>
          `).join("") : `<div class="empty-state compact">暂无事件记录。</div>`}
        </div>
        <section class="data-live-raw-block">
          <div class="data-live-raw-head">
            <span class="detail-label">原始状态</span>
            <small>live-status outputs</small>
          </div>
          <pre class="data-live-raw-preview">${escapeHtml(JSON.stringify(live || {}, null, 2))}</pre>
        </section>
      </article>
    </section>
  `;
}

function renderDataQueryFields() {
  const target = document.getElementById("data-query-fields");
  if (!target) return;
  const fields = serviceOutputs(state.dataQueryFields);
  const groups = fields.groups || {};
  const defaults = new Set(fields.default_fields || []);
  if (!Object.keys(groups).length) {
    target.innerHTML = `<div class="empty-state compact">点击“刷新字段”读取生产库字段。</div>`;
    return;
  }
  const groupOrder = ["价格衍生", "成交", "估值", "财务基本面", "资金", "筹码成本", "其他字段"];
  const fieldGroupMap = new Map();
  Object.entries(groups).forEach(([group, items]) => {
    (items || []).forEach((field) => fieldGroupMap.set(field, group));
  });
  const orderedEntries = Object.entries(groups).sort(([left], [right]) => {
    const leftRank = groupOrder.indexOf(left);
    const rightRank = groupOrder.indexOf(right);
    return (leftRank === -1 ? 99 : leftRank) - (rightRank === -1 ? 99 : rightRank);
  });
  const selected = new Set(state.dataQuerySelectedFields || fields.default_fields || []);
  if (!state.dataQueryExpandedGroups) state.dataQueryExpandedGroups = [];
  const expandedGroups = new Set(state.dataQueryExpandedGroups);
  const totalFieldCount = orderedEntries.reduce((sum, [, items]) => sum + (items || []).length, 0);
  const selectedFieldCount = [...selected].length;
  const orderedFields = orderedEntries.flatMap(([group, items]) => (items || []).map((field) => ({ field, group })));
  const renderCandidateGroups = () => {
    const groupHtml = orderedEntries.map(([group, items]) => {
      const availableItems = (items || []).filter((field) => !selected.has(field));
      if (!availableItems.length) return "";
      const isExpanded = expandedGroups.has(group);
      return `
        <section class="data-query-field-group ${isExpanded ? "is-expanded" : ""}">
          <button class="data-query-field-group-toggle" type="button" data-query-group="${escapeHtml(group)}" aria-expanded="${isExpanded ? "true" : "false"}">
            <span class="data-query-group-chevron">${isExpanded ? "−" : "+"}</span>
            <span>${escapeHtml(group)}</span>
            <small>${escapeHtml(String(availableItems.length))} 项</small>
          </button>
          ${isExpanded ? `
            <div class="data-query-field-group-body">
              ${availableItems.map((field) => `
                <label class="data-query-transfer-row is-available">
                  <input type="checkbox" name="data-query-transfer-candidate" value="${escapeHtml(field)}" />
                  <span>${escapeHtml(field)}</span>
                  <small>${escapeHtml(group)}</small>
                </label>
              `).join("")}
            </div>
          ` : ""}
        </section>
      `;
    }).join("");
    return groupHtml || `<div class="empty-state compact">当前所有字段都已加入右侧。</div>`;
  };
  const renderPane = ({ title, note, wantSelected, emptyText, tone }) => {
    const filtered = orderedFields.filter((item) => selected.has(item.field) === wantSelected);
    const bodyHtml = wantSelected
      ? filtered.map(({ field, group }) => `
        <label class="data-query-transfer-row is-selected">
          <input type="checkbox" name="data-query-transfer-selected" value="${escapeHtml(field)}" />
          <span>${escapeHtml(field)}</span>
          <small>${escapeHtml(group || fieldGroupMap.get(field) || "")}</small>
        </label>
      `).join("")
      : renderCandidateGroups();
    return `
      <article class="data-query-select-pane ${escapeHtml(tone)}">
        <div class="data-query-select-pane-head">
          <div>
            <span>${escapeHtml(title)}</span>
            <strong>${wantSelected ? selectedFieldCount : Math.max(0, totalFieldCount - selectedFieldCount)}</strong>
          </div>
          <small>${escapeHtml(note)}</small>
        </div>
        <div class="data-query-select-pane-body">
          ${bodyHtml || `<div class="empty-state compact">${escapeHtml(emptyText)}</div>`}
        </div>
      </article>
    `;
  };
  target.innerHTML = `
    <div class="data-query-selection-board">
      ${renderPane({
        title: "候选字段",
        note: "选择字段后用右箭头加入查询",
        wantSelected: false,
        emptyText: "当前所有字段都已加入右侧。",
        tone: "available",
      })}
      <div class="data-query-transfer-rail" aria-label="字段移动">
        <button class="ghost data-query-transfer-button" type="button" data-transfer-direction="right" aria-label="加入查询字段">›</button>
        <button class="ghost data-query-transfer-button" type="button" data-transfer-direction="left" aria-label="移回候选字段">‹</button>
      </div>
      ${renderPane({
        title: "查询字段",
        note: "右列字段会进入查询与图表",
        wantSelected: true,
        emptyText: "还没有加入任何字段。",
        tone: "selected",
      })}
    </div>
  `;
}

function setupDatePickerButtons(scope = document) {
  scope.querySelectorAll(".date-input-shell").forEach((shell) => {
    const input = shell.querySelector('input[type="date"]');
    const button = shell.querySelector(".date-picker-button");
    if (!input || !button || button.dataset.bound === "true") return;
    button.dataset.bound = "true";
    button.addEventListener("click", () => {
      if (typeof input.showPicker === "function") {
        input.showPicker();
      } else {
        input.focus();
      }
    });
  });
}

function selectedDataQueryFields() {
  const defaults = serviceOutputs(state.dataQueryFields).default_fields || ["volume", "PE", "PB"];
  const selected = state.dataQuerySelectedFields || defaults;
  state.dataQuerySelectedFields = selected;
  return selected;
}

function transferDataQueryFields(direction) {
  const fields = serviceOutputs(state.dataQueryFields);
  const defaults = fields.default_fields || ["volume", "PE", "PB"];
  const selected = new Set(state.dataQuerySelectedFields || defaults);
  const selector = direction === "right"
    ? 'input[name="data-query-transfer-candidate"]:checked'
    : 'input[name="data-query-transfer-selected"]:checked';
  const moving = [...document.querySelectorAll(`#data-query-fields ${selector}`)].map((input) => input.value);
  if (!moving.length) return;
  moving.forEach((field) => {
    if (direction === "right") {
      selected.add(field);
    } else {
      selected.delete(field);
    }
  });
  state.dataQuerySelectedFields = [...selected];
  renderDataQueryFields();
}

const DATA_RIGHT_AXIS_FIELDS = new Set([
  "volume",
  "amount",
  "turnover_rate",
  "sm_net_vol",
  "sm_net_amount",
  "lg_net_vol",
  "lg_net_amount",
  "buy_sm_vol",
  "sell_sm_vol",
  "net_mf_vol",
  "net_mf_amount",
  "margin_buy_amount",
  "fin_balance",
  "margin_balance",
  "short_balance",
]);

const DATA_QUERY_DEFAULT_WINDOW_DAYS = 180;
const DATA_QUERY_MAX_CHART_ROWS = 160;

function dataSeriesAxis(item) {
  const field = String(item?.field || "").toLowerCase();
  if (item?.kind === "benchmark") return "left";
  if (DATA_RIGHT_AXIS_FIELDS.has(field)) return "right";
  if (/(_vol|volume|amount|turnover|balance|margin|net_mf|buy_|sell_)/.test(field)) return "right";
  return "left";
}

function renderDataCompositeChart(rows, series, transform) {
  const sortedRows = [...(rows || [])].sort((left, right) => String(left.date || "").localeCompare(String(right.date || "")));
  const chartRows = sortedRows.slice(-DATA_QUERY_MAX_CHART_ROWS);
  const chartDates = new Set(chartRows.map((row) => row.date).filter(Boolean));
  const candles = chartRows.filter((row) => ["open", "high", "low", "close"].every((field) => Number.isFinite(Number(row[field]))));
  const cleanSeries = (series || []).map((item) => ({
    ...item,
    axis: dataSeriesAxis(item),
    points: (item.points || []).filter((point) => (
      chartDates.has(point.date)
      && point.value !== null
      && point.value !== undefined
      && Number.isFinite(Number(point.value))
    )),
  })).filter((item) => item.points.length >= 2);
  if (candles.length < 2 && !cleanSeries.length) {
    return `<div class="empty-state">暂无可绘制的 K 线或数值字段。</div>`;
  }

  const width = 900;
  const kHeight = 250;
  const lineHeight = 230;
  const pad = { top: 18, right: 26, bottom: 30, left: 58 };
  const innerW = width - pad.left - pad.right;
  const classes = ["accent-a", "accent-b", "accent-c", "benchmark", "model", "excess"];
  const dateUniverse = [...new Set([
    ...candles.map((row) => row.date),
    ...cleanSeries.flatMap((item) => item.points.map((point) => point.date)),
  ])].sort();
  const dateIndex = new Map(dateUniverse.map((date, idx) => [date, idx]));
  const xByDate = (date) => pad.left + ((dateIndex.get(date) || 0) / Math.max(1, dateUniverse.length - 1)) * innerW;

  let klineMarkup = `<div class="empty-state compact">当前窗口缺少 OHLC，无法绘制 K 线。</div>`;
  if (candles.length >= 2) {
    const values = candles.flatMap((row) => [Number(row.high), Number(row.low)]).filter(Number.isFinite);
    const yMin = Math.min(...values);
    const yMax = Math.max(...values);
    const span = yMax - yMin || 1;
    const y = (value) => pad.top + (1 - ((Number(value) - (yMin - span * 0.05)) / (span * 1.1))) * (kHeight - pad.top - pad.bottom);
    const candleW = Math.max(2, Math.min(10, innerW / candles.length * 0.55));
    klineMarkup = `
      <svg class="data-kline-chart" viewBox="0 0 ${width} ${kHeight}" role="img" aria-label="单票 K 线与指标组合图上层 K 线">
        <line class="chart-grid" x1="${pad.left}" y1="${pad.top}" x2="${width - pad.right}" y2="${pad.top}"></line>
        <line class="chart-grid" x1="${pad.left}" y1="${kHeight - pad.bottom}" x2="${width - pad.right}" y2="${kHeight - pad.bottom}"></line>
        ${candles.map((row) => {
          const open = Number(row.open);
          const high = Number(row.high);
          const low = Number(row.low);
          const close = Number(row.close);
          const up = close >= open;
          const x = xByDate(row.date);
          const bodyTop = Math.min(y(open), y(close));
          const bodyHeight = Math.max(1, Math.abs(y(close) - y(open)));
          const tooltip = `${row.date} O:${shortNumber(open, 2)} H:${shortNumber(high, 2)} L:${shortNumber(low, 2)} C:${shortNumber(close, 2)} list:${text(row.list_status, "--")} st:${text(row.st_status, "--")}`;
          return `
            <g class="data-candle ${up ? "up" : "down"}">
              <line x1="${x.toFixed(1)}" y1="${y(high).toFixed(1)}" x2="${x.toFixed(1)}" y2="${y(low).toFixed(1)}"></line>
              <rect class="data-candle-body" x="${(x - candleW / 2).toFixed(1)}" y="${bodyTop.toFixed(1)}" width="${candleW.toFixed(1)}" height="${bodyHeight.toFixed(1)}"></rect>
              <rect class="data-candle-hit" x="${(x - Math.max(candleW, 8) / 2).toFixed(1)}" y="${pad.top}" width="${Math.max(candleW, 8).toFixed(1)}" height="${(kHeight - pad.top - pad.bottom).toFixed(1)}">
                <title>${escapeHtml(tooltip)}</title>
              </rect>
            </g>
          `;
        }).join("")}
        <text class="chart-label y-axis" x="8" y="${y(yMax).toFixed(1)}">${shortNumber(yMax, 2)}</text>
        <text class="chart-label y-axis" x="8" y="${y(yMin).toFixed(1)}">${shortNumber(yMin, 2)}</text>
      </svg>
    `;
  }

  let lineMarkup = `<div class="empty-state compact">暂无可绘制的附加数值字段。</div>`;
  if (cleanSeries.length) {
    const scaleFor = (items) => {
      const values = items.flatMap((item) => item.points.map((point) => Number(point.value))).filter(Number.isFinite);
      if (!values.length) return null;
      const minRaw = Math.min(...values);
      const maxRaw = Math.max(...values);
      const span = maxRaw - minRaw || 1;
      const min = minRaw - span * 0.06;
      const max = maxRaw + span * 0.08;
      const y = (value) => pad.top + (1 - ((Number(value) - min) / (max - min))) * (lineHeight - pad.top - pad.bottom);
      return { minRaw, maxRaw, y };
    };
    const leftSeries = cleanSeries.filter((item) => item.axis !== "right");
    const rightSeries = cleanSeries.filter((item) => item.axis === "right");
    const leftScale = scaleFor(leftSeries);
    const rightScale = scaleFor(rightSeries);
    const fallbackScale = leftScale || rightScale;
    const scaleForItem = (item) => item.axis === "right" ? (rightScale || fallbackScale) : (leftScale || fallbackScale);
    const pathFor = (item) => {
      const scale = scaleForItem(item);
      return item.points.map((point, idx) => `${idx === 0 ? "M" : "L"} ${xByDate(point.date).toFixed(1)} ${scale.y(point.value).toFixed(1)}`).join(" ");
    };
    lineMarkup = `
      <svg class="data-line-chart" viewBox="0 0 ${width} ${lineHeight}" role="img" aria-label="单票 K 线与指标组合图下层趋势">
        <line class="chart-grid" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${lineHeight - pad.bottom}"></line>
        ${rightScale ? `<line class="chart-grid axis-right-grid" x1="${width - pad.right}" y1="${pad.top}" x2="${width - pad.right}" y2="${lineHeight - pad.bottom}"></line>` : ""}
        <line class="chart-grid" x1="${pad.left}" y1="${pad.top}" x2="${width - pad.right}" y2="${pad.top}"></line>
        <line class="chart-grid" x1="${pad.left}" y1="${lineHeight - pad.bottom}" x2="${width - pad.right}" y2="${lineHeight - pad.bottom}"></line>
        ${cleanSeries.map((item, idx) => `<path class="chart-line ${classes[idx % classes.length]} axis-${escapeHtml(item.axis)}" d="${pathFor(item)}"></path>`).join("")}
        ${dateUniverse.map((date) => {
          const tooltip = cleanSeries.map((item) => {
            const point = item.points.find((candidate) => candidate.date === date);
            return point ? `${item.label || item.field}(${item.axis === "right" ? "右轴" : "左轴"}): ${shortNumber(point.value, 3)}` : null;
          }).filter(Boolean).join(" | ");
          return `<rect class="chart-hover-hit" x="${(xByDate(date) - 3).toFixed(1)}" y="${pad.top}" width="6" height="${lineHeight - pad.top - pad.bottom}"><title>${escapeHtml(date)} ${escapeHtml(tooltip)}</title></rect>`;
        }).join("")}
        ${leftScale ? `
          <text class="chart-label axis-title left-axis-title" x="8" y="${pad.top - 6}">左轴</text>
          <text class="chart-label y-axis" x="8" y="${leftScale.y(leftScale.maxRaw).toFixed(1)}">${shortNumber(leftScale.maxRaw, 2)}</text>
          <text class="chart-label y-axis" x="8" y="${leftScale.y(leftScale.minRaw).toFixed(1)}">${shortNumber(leftScale.minRaw, 2)}</text>
        ` : ""}
        ${rightScale ? `
          <text class="chart-label axis-title right-axis" x="${width - 4}" y="${pad.top - 6}">右轴</text>
          <text class="chart-label y-axis right-axis" x="${width - 4}" y="${rightScale.y(rightScale.maxRaw).toFixed(1)}">${shortNumber(rightScale.maxRaw, 2)}</text>
          <text class="chart-label y-axis right-axis" x="${width - 4}" y="${rightScale.y(rightScale.minRaw).toFixed(1)}">${shortNumber(rightScale.minRaw, 2)}</text>
        ` : ""}
      </svg>
    `;
  }

  const clipped = sortedRows.length > chartRows.length;
  const statusNote = clipped
    ? `图表显示最近 ${chartRows.length} / ${sortedRows.length} 行`
    : `图表显示完整窗口 ${chartRows.length} 行`;

  return `
    <div class="data-chart-shell data-composite-chart-shell">
      <div class="data-composite-layout">
        <div class="data-composite-chart-body">
          <div class="data-composite-pane">
            <span class="detail-label">价格轴 · 原始 K 线</span>
            ${klineMarkup}
          </div>
          <div class="data-composite-pane">
            <span class="detail-label">指标轴 · ${escapeHtml(text(transform, "zscore"))}</span>
            ${lineMarkup}
          </div>
        </div>
        <aside class="chart-legend data-side-legend" aria-label="图表图例">
          <span><i class="legend-dot model"></i>K 线原始 OHLC<em>价格轴</em></span>
          ${cleanSeries.map((item, idx) => `<span><i class="legend-dot ${classes[idx % classes.length]}"></i>${escapeHtml(item.label || item.field)}<em>${item.axis === "right" ? "右轴" : "左轴"}</em></span>`).join("")}
          <small>默认 Z-score；成交量与资金字段自动使用右轴。</small>
        </aside>
      </div>
      <div class="data-chart-status-strip">
        ${dateUniverse.length ? `<span>${escapeHtml(dateUniverse[0])}</span><span>${escapeHtml(statusNote)}</span><span>${escapeHtml(dateUniverse[dateUniverse.length - 1])}</span>` : ""}
      </div>
    </div>
  `;
}

function renderDataKlineChart(rows) {
  const candles = (rows || []).filter((row) => ["open", "high", "low", "close"].every((field) => Number.isFinite(Number(row[field]))));
  if (candles.length < 2) return "";
  const width = 860;
  const height = 220;
  const pad = { top: 16, right: 24, bottom: 28, left: 54 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const values = candles.flatMap((row) => [Number(row.high), Number(row.low)]).filter(Number.isFinite);
  const yMin = Math.min(...values);
  const yMax = Math.max(...values);
  const span = yMax - yMin || 1;
  const y = (value) => pad.top + (1 - ((Number(value) - (yMin - span * 0.05)) / (span * 1.1))) * innerH;
  const x = (idx) => pad.left + (idx / Math.max(1, candles.length - 1)) * innerW;
  const candleW = Math.max(2, Math.min(10, innerW / candles.length * 0.55));
  return `
    <div class="data-chart-shell">
      <div class="chart-legend"><span><i class="legend-dot model"></i>K 线原始价格</span></div>
      <svg class="data-kline-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="单票 K 线图">
        <line class="chart-grid" x1="${pad.left}" y1="${pad.top}" x2="${width - pad.right}" y2="${pad.top}"></line>
        <line class="chart-grid" x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}"></line>
        ${candles.map((row, idx) => {
          const open = Number(row.open);
          const high = Number(row.high);
          const low = Number(row.low);
          const close = Number(row.close);
          const up = close >= open;
          const bodyTop = Math.min(y(open), y(close));
          const bodyHeight = Math.max(1, Math.abs(y(close) - y(open)));
          return `
            <g class="data-candle ${up ? "up" : "down"}">
              <line x1="${x(idx).toFixed(1)}" y1="${y(high).toFixed(1)}" x2="${x(idx).toFixed(1)}" y2="${y(low).toFixed(1)}"></line>
              <rect class="data-candle-body" x="${(x(idx) - candleW / 2).toFixed(1)}" y="${bodyTop.toFixed(1)}" width="${candleW.toFixed(1)}" height="${bodyHeight.toFixed(1)}">
                <title>${escapeHtml(row.date)} O:${shortNumber(open, 2)} H:${shortNumber(high, 2)} L:${shortNumber(low, 2)} C:${shortNumber(close, 2)}</title>
              </rect>
            </g>
          `;
        }).join("")}
        <text class="chart-label" x="${pad.left}" y="${height - 8}">${escapeHtml(candles[0].date)}</text>
        <text class="chart-label end" x="${width - pad.right}" y="${height - 8}">${escapeHtml(candles[candles.length - 1].date)}</text>
        <text class="chart-label y-axis" x="8" y="${y(yMax).toFixed(1)}">${shortNumber(yMax, 2)}</text>
        <text class="chart-label y-axis" x="8" y="${y(yMin).toFixed(1)}">${shortNumber(yMin, 2)}</text>
      </svg>
    </div>
  `;
}

function renderDataLineChart(series) {
  const cleanSeries = (series || []).map((item) => ({
    ...item,
    points: (item.points || []).filter((point) => point.value !== null && point.value !== undefined && Number.isFinite(Number(point.value))),
  })).filter((item) => item.points.length >= 2);
  if (!cleanSeries.length) return `<div class="empty-state">暂无可绘制的数值字段。</div>`;
  const width = 860;
  const height = 260;
  const pad = { top: 18, right: 24, bottom: 34, left: 54 };
  const allDates = [...new Set(cleanSeries.flatMap((item) => item.points.map((point) => point.date)))].sort();
  const dateIndex = new Map(allDates.map((date, idx) => [date, idx]));
  const values = cleanSeries.flatMap((item) => item.points.map((point) => Number(point.value))).filter(Number.isFinite);
  const yMinRaw = Math.min(...values);
  const yMaxRaw = Math.max(...values);
  const span = yMaxRaw - yMinRaw || 1;
  const yMin = yMinRaw - span * 0.06;
  const yMax = yMaxRaw + span * 0.08;
  const x = (date) => pad.left + ((dateIndex.get(date) || 0) / Math.max(1, allDates.length - 1)) * (width - pad.left - pad.right);
  const y = (value) => pad.top + (1 - ((Number(value) - yMin) / (yMax - yMin))) * (height - pad.top - pad.bottom);
  const classes = ["model", "benchmark", "excess", "accent-a", "accent-b", "accent-c"];
  const pathFor = (item) => item.points.map((point, idx) => `${idx === 0 ? "M" : "L"} ${x(point.date).toFixed(1)} ${y(point.value).toFixed(1)}`).join(" ");
  return `
    <div class="data-chart-shell">
      <div class="chart-legend">
        ${cleanSeries.map((item, idx) => `<span><i class="legend-dot ${classes[idx % classes.length]}"></i>${escapeHtml(item.label || item.field)}</span>`).join("")}
      </div>
      <svg class="data-line-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="单票字段趋势图">
        <line class="chart-grid" x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}"></line>
        <line class="chart-grid" x1="${pad.left}" y1="${pad.top}" x2="${width - pad.right}" y2="${pad.top}"></line>
        <line class="chart-grid" x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}"></line>
        ${cleanSeries.map((item, idx) => `<path class="chart-line ${classes[idx % classes.length]}" d="${pathFor(item)}"></path>`).join("")}
        ${allDates.map((date) => {
          const tooltip = cleanSeries.map((item) => {
            const point = item.points.find((candidate) => candidate.date === date);
            return point ? `${item.label || item.field}: ${shortNumber(point.value, 3)}` : null;
          }).filter(Boolean).join(" | ");
          return `<rect class="chart-hover-hit" x="${(x(date) - 3).toFixed(1)}" y="${pad.top}" width="6" height="${height - pad.top - pad.bottom}"><title>${escapeHtml(date)} ${escapeHtml(tooltip)}</title></rect>`;
        }).join("")}
        <text class="chart-label" x="${pad.left}" y="${height - 10}">${escapeHtml(allDates[0])}</text>
        <text class="chart-label end" x="${width - pad.right}" y="${height - 10}">${escapeHtml(allDates[allDates.length - 1])}</text>
        <text class="chart-label y-axis" x="8" y="${y(yMaxRaw).toFixed(1)}">${shortNumber(yMaxRaw, 2)}</text>
        <text class="chart-label y-axis" x="8" y="${y(yMinRaw).toFixed(1)}">${shortNumber(yMinRaw, 2)}</text>
      </svg>
    </div>
  `;
}

function renderDataQueryResult() {
  const target = document.getElementById("data-query-result");
  if (!target) return;
  if (state.dataQueryLoading) {
    target.innerHTML = `<div class="empty-state">查询中，正在读取生产 HDF 并生成图表...</div>`;
    queueFloatingXScrollbarRefresh(target);
    return;
  }
  const result = state.dataQueryResult;
  if (!result) {
    target.innerHTML = `<div class="empty-state">输入股票代码后点击查询。</div>`;
    queueFloatingXScrollbarRefresh(target);
    return;
  }
  if (!result.ok) {
    target.innerHTML = `<div class="warning-strip">查询失败：${escapeHtml(result.err || result.error || "unknown")}</div>`;
    queueFloatingXScrollbarRefresh(target);
    return;
  }
  const outputs = serviceOutputs(result);
  const metadata = outputs.metadata || {};
  const rows = outputs.rows || [];
  const missing = outputs.missing_rate || {};
  const tableFields = [...new Set(["date", "code", "list_status", "st_status", ...(outputs.fields || [])])].slice(0, 12);
  const recentRows = rows.slice(-80).reverse();
  target.innerHTML = `
    <section class="data-query-meta data-query-summary-strip">
      <article class="data-query-identity"><span>代码</span><strong>${escapeHtml(text(metadata.code, "--"))}</strong><small>${escapeHtml(text(metadata.security_name, ""))}</small></article>
      <article><span>日期范围</span><strong>${escapeHtml(text(metadata.start, "--"))} → ${escapeHtml(text(metadata.end, "--"))}</strong><small>${escapeHtml(text(metadata.row_count, "0"))} 行</small></article>
      <article class="data-query-status"><span>状态</span><strong>${escapeHtml(text(metadata.latest_list_status, "--"))} / ${escapeHtml(text(metadata.latest_st_status, "--"))}</strong><small>list_status / st_status</small></article>
      <article class="data-query-mode"><span>图表模式</span><strong>${escapeHtml(text(outputs.transform, "zscore"))}</strong><small>数值字段趋势</small></article>
    </section>
    ${renderDataCompositeChart(rows, outputs.chart_series || [], outputs.transform)}
    <div class="data-missing-strip">
      ${Object.entries(missing).map(([field, value]) => `<span>${escapeHtml(field)} 缺失 ${pct(value, 1)}</span>`).join("")}
    </div>
    <div class="table-shell data-query-table">
      <table class="data-table">
        <thead><tr>${tableFields.map((field) => `<th>${escapeHtml(field)}</th>`).join("")}</tr></thead>
        <tbody>
          ${recentRows.map((row) => `
            <tr>${tableFields.map((field) => `<td>${escapeHtml(text(row[field], ""))}</td>`).join("")}</tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
  queueFloatingXScrollbarRefresh(target);
}

function renderDataFoundation() {
  const data = serviceOutputs(state.data);
  const quality = data.data_quality_summary || {};
  const schemaSummary = quality.schema_summary || {};
  const factorAudit = quality.factor_adjusted_quality || {};
  const staleSample = (data.snapshot?.quantgpt_stale_sample || []).slice(0, 5).join(", ");
  renderBackgroundWorkflowStatus("data-background-workflow-status", ["data_foundation"]);
  renderDataFoundationSummary(data, quality, schemaSummary, factorAudit, staleSample);
  renderDataFoundationNav();
  renderDataFoundationStatus(data, quality, schemaSummary, factorAudit, staleSample);
  renderDataLive();
  renderDataQueryFields();
  renderDataQueryResult();
  if (state.dataFoundationTab === "query") {
    ensureDataQueryDateDefaults(document.getElementById("data-query-form"));
  }
  queueFloatingXScrollbarRefresh(document.getElementById("panel-data-foundation") || document);
}

function renderStockResearch() {
  const container = document.getElementById("stock-research-detail");
  if (!container) return;
  container.innerHTML = `
    <div class="module-roadmap">
      <div><strong>输入</strong><span>股票代码、研究日期、关注问题、财报/公告/新闻/行业知识库。</span></div>
      <div><strong>过程</strong><span>未来以 MCP Agent 编排：资料检索 → 关键事实抽取 → 风险/催化剂分析 → 估值与交易假设。</span></div>
      <div><strong>输出</strong><span>个股研究卡片、定性评分、事件跟踪、可回写模型/交易的观点标签。</span></div>
    </div>
  `;
}

function parseMetadata(item) {
  if (!item) return {};
  if (typeof item.metadata === "object" && item.metadata) return item.metadata;
  if (typeof item.metadata === "string") {
    try {
      return JSON.parse(item.metadata);
    } catch {
      return {};
    }
  }
  if (typeof item.metadata_json === "object" && item.metadata_json) return item.metadata_json;
  if (typeof item.metadata_json === "string") {
    try {
      return JSON.parse(item.metadata_json);
    } catch {
      return {};
    }
  }
  return {};
}

function libraryMetric(item, key) {
  const metadata = parseMetadata(item);
  const metrics = metadata.metrics || {};
  const backtest = metadata.backtest_summary || {};
  const aliases = {
    rank_ic_mean: [item?.rank_ic_mean, item?.rank_ic, metrics.rank_ic, backtest.rank_ic_mean, backtest.rank_ic],
    rank_icir: [item?.rank_icir, metrics.rank_icir, backtest.rank_ic_ir, backtest.rank_icir],
    annual_return: [item?.annual_return, metrics.annual_return, backtest.annual_return, backtest.annualized_return],
    quick_score: [item?.quick_score, metrics.quick_score],
    deep_score: [item?.deep_score, metrics.deep_score, metadata.deep_validation?.deep_score],
    max_drawdown: [item?.max_drawdown, metrics.max_drawdown, backtest.max_drawdown],
    turnover: [item?.turnover, metrics.turnover, backtest.turnover],
  };
  const values = aliases[key] || [item?.[key], metrics[key], backtest[key]];
  return values.find((value) => value !== undefined && value !== null && value !== "");
}

function filteredLibraryItems() {
  const factorConsole = serviceOutputs(state.factorConsole);
  const fullLibrary = serviceOutputs(state.factorLibraryRaw);
  const library = (fullLibrary.items || []).length
    ? fullLibrary
    : (factorConsole.factor_library || { items: [] });
  const query = state.libraryFilter.query.trim().toLowerCase();
  const status = state.libraryFilter.status;
  const category = state.libraryFilter.category;
  const holdingPeriod = state.libraryFilter.holdingPeriod;
  return (library.items || []).filter((item) => {
    if (status !== "all" && text(item.status, "").toLowerCase() !== status) return false;
    if (category !== "all" && text(item.category || "其他") !== category) return false;
    if (holdingPeriod !== "all" && String(item.holding_period_days || parseMetadata(item).holding_period_days || "5") !== holdingPeriod) return false;
    if (!query) return true;
    const metadata = parseMetadata(item);
    const hay = [
      item.factor_id,
      item.name,
      item.expression,
      item.category,
      item.status,
      metadata?.wq?.last_status,
    ].map((v) => text(v, "").toLowerCase()).join(" ");
    return hay.includes(query);
  });
}

function renderLibraryCategoryFilter(allItems) {
  const select = document.getElementById("library-category-filter");
  if (!select) return;
  const counts = new Map();
  (allItems || []).forEach((item) => {
    const category = text(item.category || "其他");
    counts.set(category, (counts.get(category) || 0) + 1);
  });
  const categories = [
    ...STANDARD_FACTOR_CATEGORIES.filter((category) => counts.has(category)),
    ...[...counts.keys()].filter((category) => !STANDARD_FACTOR_CATEGORIES.includes(category)).sort(),
  ];
  const current = state.libraryFilter.category;
  select.innerHTML = `
    <option value="all">全部分类</option>
    ${categories.map((category) => `
      <option value="${escapeHtml(category)}">${escapeHtml(category)} (${counts.get(category)})</option>
    `).join("")}
  `;
  select.value = categories.includes(current) ? current : "all";
  state.libraryFilter.category = select.value;
}

function renderLibraryHoldingPeriodFilter(allItems) {
  const select = document.getElementById("library-holding-filter");
  if (!select) return;
  const counts = new Map();
  (allItems || []).forEach((item) => {
    const hp = String(item.holding_period_days || parseMetadata(item).holding_period_days || 5);
    counts.set(hp, (counts.get(hp) || 0) + 1);
  });
  const periods = [...counts.keys()].sort((a, b) => Number(a) - Number(b));
  const current = state.libraryFilter.holdingPeriod;
  select.innerHTML = `
    <option value="all">全部周期</option>
    ${periods.map((period) => `
      <option value="${escapeHtml(period)}">${escapeHtml(period)}D (${counts.get(period)})</option>
    `).join("")}
  `;
  select.value = periods.includes(current) ? current : "all";
  state.libraryFilter.holdingPeriod = select.value;
}

function renderFactorLibrary(consoleOutputs) {
  const fullLibrary = serviceOutputs(state.factorLibraryRaw);
  const library = (fullLibrary.items || []).length
    ? fullLibrary
    : (consoleOutputs.factor_library || { total: 0, items: [] });
  renderLibraryCategoryFilter(library.items || []);
  renderLibraryHoldingPeriodFilter(library.items || []);
  const items = filteredLibraryItems();
  const duplicateOutputs = serviceOutputs(state.duplicateAudit);
  const categoryCount = new Set((library.items || []).map((item) => text(item.category || "Other"))).size;
  const registrySummary = consoleOutputs.registry_summary || library.registry_summary || {};
  const summaryNode = document.getElementById("library-summary");
  if (summaryNode) {
    summaryNode.innerHTML = `
      <div class="library-compact-summary">
        <span><b>活跃因子</b><strong>${escapeHtml(text(registrySummary.active, "0"))}</strong></span>
        <span><b>分类</b><strong>${escapeHtml(text(categoryCount, "0"))}</strong></span>
        <span><b>重复组</b><strong>${escapeHtml(text(duplicateOutputs.duplicate_groups, "0"))}</strong></span>
        <span><b>可退休副本</b><strong>${escapeHtml(text(duplicateOutputs.duplicate_factor_count, "0"))}</strong></span>
      </div>
    `;
  }
  renderFactorLibraryAudit(serviceOutputs(state.factorAudit));
  renderDuplicateAudit(duplicateOutputs);

  const container = document.getElementById("factor-table");
  if (!items.length) {
    container.innerHTML = `<div class="empty-state">当前筛选条件下没有因子。</div>`;
    queueFloatingXScrollbarRefresh(container);
    return;
  }

  if (items.length && (!state.inspector || (state.activePanel === "library" && state.inspector.kind !== "library"))) {
    state.inspector = { kind: "library", payload: items[0] };
  }

  const rows = items.map((item, index) => {
    const metadata = parseMetadata(item);
    const expression = item.expression || metadata.expression || metadata.factor_expression || "";
    const wq = metadata.wq || {};
    const factorName = item.name || metadata.factor_name || metadata.category_info?.suggested_factor_name || item.factor_id;
    const isSelected = state.inspector?.kind === "library"
      && state.inspector?.payload?.factor_id === item.factor_id;
    return `
      <tr class="${isSelected ? "selected-row" : ""}" data-factor-index="${index}">
        <td>
          <strong>${escapeHtml(clip(factorName, 72))}</strong>
          <span class="muted-id">${escapeHtml(text(item.factor_id || ""))}</span>
          <code class="library-row-expression">${escapeHtml(clip(expression, 120))}</code>
        </td>
        <td><span class="badge subtle">${escapeHtml(text(item.status || "unknown"))}</span><small>${escapeHtml(text(item.category || "--"))}</small></td>
        <td>${shortNumber(item.icir, 4)}</td>
        <td>${shortNumber(libraryMetric(item, "rank_icir"), 4)}</td>
        <td>${shortNumber(item.sharpe, 3)}</td>
        <td>${shortNumber(libraryMetric(item, "deep_score"), 1)}<small>${escapeHtml(text(item.holding_period_days, "5"))}D</small></td>
        <td><span class="muted-id">${escapeHtml(text(wq.last_status || "未提交"))}</span></td>
      </tr>
    `;
  }).join("");

  container.innerHTML = `
    <div class="library-table-meta">
      <strong>${items.length} / ${escapeHtml(text(library.total, "0"))}</strong>
      <span>active ${escapeHtml(text(registrySummary.active, "0"))} · retired ${escapeHtml(text(registrySummary.retired, "0"))}</span>
    </div>
    <table class="data-table library-factor-table">
      <thead>
        <tr>
          <th>因子 / 表达式</th>
          <th>状态 / 分类</th>
          <th>ICIR</th>
          <th>Rank ICIR</th>
          <th>Sharpe</th>
          <th>Deep</th>
          <th>WQ 状态</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;

  container.querySelectorAll("tbody tr").forEach((row) => {
    row.addEventListener("click", () => {
      const item = items[Number(row.dataset.factorIndex)];
      setInspector("library", item);
      renderFactorLibrary(consoleOutputs);
      document.getElementById("library-inspector-detail")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
  });
  queueFloatingXScrollbarRefresh(container);
}

function renderDuplicateAudit(duplicateOutputs) {
  const container = document.getElementById("duplicate-audit");
  if (!container) return;
  const groups = duplicateOutputs.groups || [];
  if (!groups.length) {
    container.innerHTML = `
      <div class="detail-grid">
        <div><span class="detail-label">重复表达式组</span><strong>0</strong></div>
        <div><span class="detail-label">可退休副本</span><strong>0</strong></div>
        <div><span class="detail-label">结论</span><strong>active 因子库当前没有完全重复表达式</strong></div>
      </div>
    `;
    return;
  }
  container.innerHTML = `
    <div class="detail-grid">
      <div><span class="detail-label">重复表达式组</span><strong>${text(duplicateOutputs.duplicate_groups, "0")}</strong></div>
      <div><span class="detail-label">可退休副本</span><strong>${text(duplicateOutputs.duplicate_factor_count, "0")}</strong></div>
      <div><span class="detail-label">处理方式</span><strong>保留每组 ICIR/IC/创建时间综合最优的一条</strong></div>
    </div>
    <div class="duplicate-list">
      ${groups.slice(0, 8).map((group) => `
        <article class="duplicate-card">
          <div class="note-head">
            <strong>${escapeHtml(text(group.count, "0"))} 条重复</strong>
            <span>keeper: ${escapeHtml(text(group.keeper?.factor_id))}</span>
          </div>
          <code>${escapeHtml(clip(group.expression, 260))}</code>
          <p>待退休：${escapeHtml((group.duplicates || []).map((item) => item.factor_id).join(", "))}</p>
        </article>
      `).join("")}
    </div>
  `;
}

function renderRelationGraphMarkup(relationGraph, informationClusters, topPairs, options = {}) {
  const nodes = relationGraph?.nodes || [];
  const edges = relationGraph?.edges || [];
  const graphSummary = relationGraph?.summary || {};
  const runOverlayByClusterId = options.runOverlayByClusterId instanceof Map
    ? options.runOverlayByClusterId
    : new Map();
  if (!nodes.length) {
    return `<div class="empty-state">当前报告尚未包含全库关系图。请点击“信息簇核查”生成最新报告。</div>`;
  }

  const width = 1040;
  const clusters = [...new Set(nodes.map((node) => node.cluster_id || "unclustered"))]
    .map((clusterId) => ({
      clusterId,
      nodes: nodes.filter((node) => (node.cluster_id || "unclustered") === clusterId)
        .sort((a, b) => Number(b.is_representative) - Number(a.is_representative)),
    }))
    .sort((a, b) => b.nodes.length - a.nodes.length || a.clusterId.localeCompare(b.clusterId));
  const columns = Math.max(4, Math.min(8, Math.ceil(Math.sqrt(clusters.length * 1.45))));
  const rows = Math.max(1, Math.ceil(clusters.length / columns));
  const height = Math.max(560, rows * 126 + 70);
  const cellWidth = (width - 80) / columns;
  const cellHeight = (height - 60) / rows;
  const positions = new Map();
  const clusterIndex = new Map();
  clusters.forEach((cluster, index) => {
    clusterIndex.set(cluster.clusterId, index);
    const col = index % columns;
    const row = Math.floor(index / columns);
    const centerX = 40 + cellWidth * (col + 0.5);
    const centerY = 30 + cellHeight * (row + 0.5);
    const representative = cluster.nodes.find((node) => node.is_representative) || cluster.nodes[0];
    const members = cluster.nodes.filter((node) => node.factor_id !== representative?.factor_id);
    if (representative) positions.set(representative.factor_id, { x: centerX, y: centerY, representative: true, clusterId: cluster.clusterId });
    const radius = Math.min(38, 19 + members.length * 5);
    members.forEach((node, memberIndex) => {
      const angle = -Math.PI / 2 + (Math.PI * 2 * memberIndex) / Math.max(members.length, 1);
      positions.set(node.factor_id, {
        x: centerX + Math.cos(angle) * radius,
        y: centerY + Math.sin(angle) * radius,
        representative: false,
        clusterId: cluster.clusterId,
      });
    });
  });
  const graphColor = (clusterId, alpha = 1) => {
    const index = clusterIndex.get(clusterId) || 0;
    const hue = (210 + index * 47) % 360;
    return `hsla(${hue}, 78%, 66%, ${alpha})`;
  };
  const nodeById = new Map(nodes.map((node) => [String(node.factor_id), node]));
  const edgeMarkup = edges.map((edge) => {
    const source = positions.get(String(edge.source));
    const target = positions.get(String(edge.target));
    if (!source || !target) return "";
    const strength = Math.max(0, Math.min(1, Number(edge.dependency_score || 0)));
    const representativeLink = edge.relation_type === "representative_link";
    const stroke = representativeLink ? "rgba(248,113,113,0.58)" : graphColor(source.clusterId, 0.42);
    return `
      <line class="global-relation-edge ${representativeLink ? "is-cross-family" : "is-family"}"
        x1="${source.x.toFixed(1)}" y1="${source.y.toFixed(1)}"
        x2="${target.x.toFixed(1)}" y2="${target.y.toFixed(1)}"
        style="stroke:${stroke};stroke-width:${(0.45 + strength * 2.1).toFixed(2)}">
        <title>${escapeHtml(text(edge.name_source || edge.source))} ↔ ${escapeHtml(text(edge.name_target || edge.target))} · 依赖 ${shortNumber(strength, 3)}</title>
      </line>`;
  }).join("");
  const haloMarkup = clusters.map((cluster) => {
    const representative = cluster.nodes.find((node) => node.is_representative) || cluster.nodes[0];
    const pos = representative ? positions.get(representative.factor_id) : null;
    if (!pos) return "";
    return `<circle class="global-cluster-halo" cx="${pos.x}" cy="${pos.y}" r="${Math.min(49, 27 + cluster.nodes.length * 5)}" style="stroke:${graphColor(cluster.clusterId, 0.26)};fill:${graphColor(cluster.clusterId, 0.035)}"><title>${escapeHtml(cluster.clusterId)} · ${cluster.nodes.length} factors</title></circle>`;
  }).join("");
  const nodeMarkup = nodes.map((node) => {
    const pos = positions.get(String(node.factor_id));
    if (!pos) return "";
    const representative = Boolean(node.is_representative);
    const suffix = text(node.cluster_id, "--").replace(/^information_/, "");
    const runOverlay = runOverlayByClusterId.get(text(node.cluster_id));
    const overlayClass = runOverlay?.imported
      ? "is-run-imported"
      : runOverlay?.action
        ? "is-run-action"
        : "is-run-covered";
    const overlayTitle = runOverlay
      ? `本 run 覆盖 ${runOverlay.count} 条轨迹${runOverlay.imported ? ` · 入库 ${runOverlay.imported}` : ""}`
      : "";
    return `
      <g class="global-factor-node ${representative ? "is-representative" : "is-member"}" data-factor-graph-id="${escapeHtml(text(node.factor_id))}" tabindex="0">
        ${representative && runOverlay ? `<circle class="global-factor-overlay ${overlayClass}" cx="${pos.x.toFixed(1)}" cy="${pos.y.toFixed(1)}" r="15.5"><title>${escapeHtml(overlayTitle)}</title></circle>` : ""}
        <circle cx="${pos.x.toFixed(1)}" cy="${pos.y.toFixed(1)}" r="${representative ? 10 : 5.2}" style="fill:${representative ? "rgba(200,29,37,0.82)" : graphColor(node.cluster_id, 0.78)};stroke:${representative ? "rgba(254,202,202,0.92)" : graphColor(node.cluster_id, 1)}"></circle>
        ${representative ? `<text x="${pos.x.toFixed(1)}" y="${(pos.y + 3).toFixed(1)}" text-anchor="middle">${escapeHtml(suffix)}</text>` : ""}
        <title>${escapeHtml(text(node.name || node.factor_id))} · ${escapeHtml(text(node.cluster_id))} · Score ${shortNumber(node.admission_score, 1)}</title>
      </g>`;
  }).join("");
  const pairRows = (topPairs || []).slice(0, 8).map((pair) => `
    <div>
      <strong>${escapeHtml(clip(pair.name_a || pair.factor_id_a || pair.factor_a, 28))}</strong>
      <span>${shortNumber(pair.dependency_score, 3)}</span>
      <strong>${escapeHtml(clip(pair.name_b || pair.factor_id_b || pair.factor_b, 28))}</strong>
    </div>
  `).join("");
  const clusterRows = clusters.map((cluster) => {
    const representative = cluster.nodes.find((node) => node.is_representative) || cluster.nodes[0] || {};
    return `<span><b>${escapeHtml(text(cluster.clusterId).replace(/^information_/, ""))}</b>${escapeHtml(clip(representative.name || representative.factor_id, 34))}<small>${cluster.nodes.length}</small></span>`;
  }).join("");

  return `
    <article class="global-relation-card">
      <div class="global-relation-meta">
        <span><b>${escapeHtml(text(graphSummary.node_count || nodes.length, "0"))}</b><small>全库因子</small></span>
        <span><b>${escapeHtml(text(graphSummary.cluster_count || clusters.length, "0"))}</b><small>信息家族</small></span>
        <span><b>${escapeHtml(text(graphSummary.representative_edge_count, "0"))}</b><small>核心间关系</small></span>
        <span><b>${escapeHtml(text(graphSummary.available_pair_count, "0"))}</b><small>底层相关 pair</small></span>
      </div>
      <div class="global-relation-legend">
        <span><i class="representative"></i>家族代表</span>
        <span><i class="member"></i>家族成员</span>
        <span><i class="cross"></i>代表因子之间的关系</span>
        ${runOverlayByClusterId.size ? `<span><i class="run-covered"></i>本 run 覆盖</span><span><i class="run-imported"></i>本 run 有入库</span><span><i class="run-action"></i>建议换机制</span>` : ""}
        <small>线条越粗，依赖度越高；悬停节点查看完整因子名称。</small>
      </div>
      <div class="global-relation-canvas">
        <svg class="global-relation-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="全因子库信息相关性网络">
          ${haloMarkup}
          ${edgeMarkup}
          ${nodeMarkup}
        </svg>
      </div>
      <details class="global-cluster-index">
        <summary>展开信息家族索引</summary>
        <div>${clusterRows}</div>
      </details>
    </article>
    ${pairRows ? `
      <details class="relation-pair-list">
        <summary>查看最高相关 pair</summary>
        ${pairRows}
      </details>
    ` : ""}
  `;
}

function renderFactorLibraryAudit(auditOutputs) {
  const qualityContainer = document.getElementById("factor-library-audit");
  const featureContainer = document.getElementById("factor-feature-sets");
  const graphContainer = document.getElementById("factor-relation-graph");
  const summary = auditOutputs.summary || {};
  const redundancyClusters = auditOutputs.redundancy_clusters || [];
  const informationClusters = auditOutputs.information_clusters || [];
  const topPairs = auditOutputs.top_correlated_pairs || [];
  const relationGraph = auditOutputs.relation_graph || {};
  const featureSets = auditOutputs.feature_set_recommendations || [];
  const factorChecks = auditOutputs.factor_checks || [];
  const actions = auditOutputs.actions || {};
  const runStatus = serviceOutputs(state.factorAuditRunStatus);
  const auditRunning = ["queued", "running"].includes(runStatus.status);
  setFactorAuditActionState(auditRunning);
  const stale = Boolean(summary.stale);
  const staleText = stale
    ? `需重新信息簇核查${summary.current_active_count ? ` · 当前 active ${summary.current_active_count}` : ""}`
    : "当前";
  const runText = auditRunning
    ? `${text(runStatus.scope, "因子库")}核查${runStatus.status === "queued" ? "排队中" : "运行中"}`
    : runStatus.status === "failed"
      ? `${text(runStatus.scope, "因子库")}核查失败：${text(runStatus.last_error, "unknown")}`
      : "";

  if (summary.status === "missing" || (!summary.status && !auditOutputs.audit_version)) {
    const empty = `<div class="empty-state">暂无因子库审计报告。可直接点击“质量核查”“信息簇核查”或“全部核查”启动后台审计。</div>`;
    if (qualityContainer) qualityContainer.innerHTML = empty;
    if (featureContainer) featureContainer.innerHTML = empty;
    if (graphContainer) graphContainer.innerHTML = empty;
    return;
  }

  const allCount = Number(summary.factor_count || summary.active_count || 0);
  const generatedAt = auditOutputs.generated_at || summary.generated_at || summary.last_audit_at;
  const auditMode = text(summary.scope || summary.audit_type || "cached");
  const auditAge = generatedAt && Number.isFinite(secondsSince(generatedAt))
    ? `${Math.max(0, Math.round(secondsSince(generatedAt) / 60))} 分钟前`
    : "暂无时间";
  const compressionText = (item) => item.compression_ratio === undefined || item.compression_ratio === null ? "--" : pct(item.compression_ratio, 1);
  const setStatus = (item) => {
    if (item.degenerate) return `<span class="badge warn">未降维</span>`;
    if ((item.factor_ids || []).length < allCount) return `<span class="badge ok">已压缩</span>`;
    return `<span class="badge neutral">全量</span>`;
  };
  const issueChecks = factorChecks.filter((item) => item.health !== "ok" || (item.issues || []).length);
  const coverageUniverse = factorChecks
    .filter((item) => item.data_coverage && Number.isFinite(Number(item.data_coverage.coverage_ratio)))
    .map((item) => {
      const total = Number(item.data_coverage.total || 0);
      const nonNull = Number(item.data_coverage.non_null || 0);
      const coverage = Number(item.data_coverage.coverage_ratio || 0);
      return {
        ...item,
        _coverage: coverage,
        _missingRatio: Math.max(0, 1 - coverage),
        _missingRows: Math.max(0, total - nonNull),
      };
    });
  const coverageRatios = coverageUniverse.map((item) => item._coverage);
  const avgCoverage = coverageRatios.length
    ? coverageRatios.reduce((sum, value) => sum + value, 0) / coverageRatios.length
    : null;
  const minCoverage = coverageRatios.length ? Math.min(...coverageRatios) : null;
  const highCoverageCount = coverageUniverse.filter((item) => item._coverage >= 0.99).length;
  const mediumCoverageCount = coverageUniverse.filter((item) => item._coverage >= 0.90 && item._coverage < 0.99).length;
  const lowCoverageCount = coverageUniverse.filter((item) => item._coverage < 0.90).length;
  const worstCoverage = coverageUniverse
    .slice()
    .sort((a, b) => b._missingRatio - a._missingRatio)
    .slice(0, 5);
  const coverageTotal = Math.max(coverageUniverse.length, 1);
  const distributionSegment = (count, className) => `
    <span class="${className}" style="flex:${Math.max(count, 0)}" title="${count} factors"></span>
  `;

  if (featureContainer) {
    const visibleFeatureSets = featureSets.slice(0, 6);
    const selectedFeatureSet = visibleFeatureSets.find((item) => item.name === state.selectedFeatureSetName)
      || visibleFeatureSets.find((item) => !item.degenerate)
      || visibleFeatureSets[0]
      || null;
    state.selectedFeatureSetName = selectedFeatureSet?.name || "";
    const factorById = new Map(factorChecks.map((item) => [String(item.factor_id), item]));
    const selectedFactorIds = selectedFeatureSet?.factor_ids || [];
    featureContainer.innerHTML = `
      <div class="audit-brief-card">
        <div>
          <span class="detail-label">最近审计</span>
          <strong>${escapeHtml(compactDateTime(generatedAt))}</strong>
          <small>${escapeHtml(auditMode)} · ${escapeHtml(auditAge)}</small>
        </div>
        <div>
          <span class="detail-label">建议来源</span>
          <p>${stale ? "当前信息报告已过期，Feature Set 仅作历史参考；请重新运行信息簇核查。" : "Feature Set 来自最近一次信息簇核查，仅作为模型输入组合建议。"}</p>
        </div>
        <span class="badge ${stale ? "warn" : "subtle"}">${escapeHtml(staleText)}</span>
      </div>
      <div class="feature-set-card-grid">
        ${visibleFeatureSets.map((item) => `
          <button class="feature-set-card ${item.degenerate ? "is-degenerate" : ""}${item.name === state.selectedFeatureSetName ? " selected" : ""}" type="button" data-feature-set-name="${escapeHtml(text(item.name))}">
            <div class="note-head">
              <strong>${escapeHtml(text(item.name))}</strong>
              ${setStatus(item)}
            </div>
            <div class="feature-set-scoreline">
              <span><b>${escapeHtml(text(item.count || (item.factor_ids || []).length, "0"))}</b><small>因子</small></span>
              <span><b>${compressionText(item)}</b><small>压缩率</small></span>
              <span><b>${escapeHtml(text(item.family_coverage_count, "0"))}</b><small>家族覆盖</small></span>
            </div>
            <p>${escapeHtml(text(item.use_case || item.rationale || "--"))}</p>
            <code>${escapeHtml((item.factor_ids || []).slice(0, 4).join(", "))}${(item.factor_ids || []).length > 4 ? " ..." : ""}</code>
          </button>
        `).join("") || `<div class="empty-state">暂无 Feature Set 建议。</div>`}
      </div>
      <section class="feature-set-detail-panel">
        <div class="note-head">
          <div>
            <span class="detail-label">当前集合</span>
            <strong>${escapeHtml(text(selectedFeatureSet?.name, "--"))}</strong>
          </div>
          <span class="badge subtle">${escapeHtml(text(selectedFactorIds.length, "0"))} factors</span>
        </div>
        <p>${escapeHtml(text(selectedFeatureSet?.rationale || selectedFeatureSet?.use_case, "暂无说明"))}</p>
        <div class="feature-set-factor-list">
          ${selectedFactorIds.map((factorId) => {
            const factor = factorById.get(String(factorId)) || {};
            return `
              <div>
                <strong>${escapeHtml(text(factor.name || factorId))}</strong>
                <span>${escapeHtml(text(factorId))}</span>
                <small>ICIR ${shortNumber(factor.metrics?.icir, 4)} · Rank ${shortNumber(factor.metrics?.rank_icir, 4)} · ${escapeHtml(text(factor.category, "--"))}</small>
              </div>
            `;
          }).join("") || `<div class="empty-state">当前 Feature Set 没有因子明细。</div>`}
        </div>
      </section>
    `;
    featureContainer.querySelectorAll("[data-feature-set-name]").forEach((button) => {
      button.addEventListener("click", () => {
        state.selectedFeatureSetName = button.dataset.featureSetName || "";
        renderFactorLibraryAudit(auditOutputs);
      });
    });
  }

  if (qualityContainer) {
    qualityContainer.innerHTML = `
      <div class="audit-brief-card">
        <div>
          <span class="detail-label">最近审计</span>
          <strong>${escapeHtml(compactDateTime(generatedAt))}</strong>
          <small>${escapeHtml(auditMode)} · ${escapeHtml(auditAge)}</small>
        </div>
        <div>
          <span class="detail-label">治理边界</span>
          <p>${escapeHtml(runText || (stale ? "报告与当前因子库不一致；退休建议需要 fresh 信息簇核查后才可使用。" : "审计报告只读，不自动修改 registry；执行清理或退休仍需人工确认。"))}</p>
        </div>
        <span class="badge ${auditRunning || stale ? "warn" : "subtle"}">${escapeHtml(auditRunning ? text(runStatus.status) : text(summary.status, "completed"))}</span>
      </div>
      <div class="detail-grid audit-governance-grid">
        <div><span class="detail-label">可用</span><strong>${escapeHtml(text(summary.usable_count, "0"))} / ${escapeHtml(text(summary.factor_count, "0"))}</strong><small>active ${escapeHtml(text(summary.active_count, "0"))}</small></div>
        <div><span class="detail-label">平均覆盖</span><strong>${avgCoverage === null ? "--" : pct(avgCoverage, 1)}</strong><small>${escapeHtml(text(coverageUniverse.length, "0"))} factors</small></div>
        <div><span class="detail-label">最低覆盖</span><strong>${minCoverage === null ? "--" : pct(minCoverage, 1)}</strong><small>缺失 Top5 见下方</small></div>
        <div><span class="detail-label">覆盖异常</span><strong>${escapeHtml(text(summary.data_issue_count, "0"))}</strong><small>watch ${escapeHtml(text(summary.watch_count, "0"))}</small></div>
        <div><span class="detail-label">强相关</span><strong>${escapeHtml(text(summary.redundancy_cluster_count, "0"))}</strong><small>retire 建议</small></div>
        <div><span class="detail-label">待确认</span><strong>${escapeHtml(text((actions.retire_candidates || []).length, "0"))}</strong><small>不自动执行</small></div>
        <div><span class="detail-label">新鲜度</span><strong>${escapeHtml(stale ? "Stale" : "Fresh")}</strong><small>${escapeHtml(text(summary.stale_reason || runStatus.status || "ok"))}</small></div>
      </div>
      <div class="quality-overview-panel">
        <section class="quality-distribution-card">
          <div class="note-head">
            <strong>覆盖分布</strong>
            <span>${highCoverageCount} 高覆盖 · ${mediumCoverageCount} 中等 · ${lowCoverageCount} 低覆盖</span>
          </div>
          <div class="coverage-bar">
            ${distributionSegment(highCoverageCount, "good")}
            ${distributionSegment(mediumCoverageCount, "warn")}
            ${distributionSegment(lowCoverageCount, "bad")}
          </div>
          <div class="coverage-legend">
            <span><i class="good"></i> >=99%</span>
            <span><i class="warn"></i> 90%-99%</span>
            <span><i class="bad"></i> <90%</span>
          </div>
        </section>
        <section class="quality-worst-card">
          <div class="note-head">
            <strong>缺失率最高 Top 5</strong>
            <span>按因子值覆盖率倒序</span>
          </div>
          <div class="quality-ranking-list">
            ${worstCoverage.map((item, idx) => `
              <div>
                <b>${idx + 1}</b>
                <strong>${escapeHtml(clip(item.name || item.factor_id, 34))}</strong>
                <span>缺失 ${pct(item._missingRatio, 1)} · 覆盖 ${pct(item._coverage, 1)} · 缺失行 ${escapeHtml(text(item._missingRows.toLocaleString(), "0"))}</span>
              </div>
            `).join("") || `<div><b>--</b><strong>暂无覆盖数据</strong><span>最近审计没有覆盖率字段。</span></div>`}
          </div>
        </section>
      </div>
      <details class="raw-event compact-raw">
        <summary>展开因子覆盖明细</summary>
        <div class="table-shell mini-table-shell">
          <table class="data-table">
            <thead><tr><th>因子</th><th>健康</th><th>问题</th><th>覆盖</th><th>缺失率</th><th>Unique</th><th>建议</th></tr></thead>
            <tbody>
              ${(issueChecks.length ? issueChecks : coverageUniverse).map((item) => `
                <tr>
                  <td>${escapeHtml(text(item.name || item.factor_id))}</td>
                  <td>${escapeHtml(text(item.health, "--"))}</td>
                  <td>${escapeHtml((item.issues || []).join(", ") || "healthy")}</td>
                  <td>${item.data_coverage?.coverage_ratio !== undefined ? pct(item.data_coverage.coverage_ratio, 1) : "--"}</td>
                  <td>${item._missingRatio !== undefined ? pct(item._missingRatio, 1) : "--"}</td>
                  <td>${escapeHtml(text(item.data_coverage?.nunique, "--"))}</td>
                  <td>${escapeHtml(text(item.recommendation, "--"))}</td>
                </tr>
              `).join("") || `<tr><td colspan="7">暂无因子检查明细。</td></tr>`}
            </tbody>
          </table>
        </div>
      </details>
    `;
  }

  if (graphContainer) {
    graphContainer.innerHTML = `
      <div class="detail-grid audit-governance-grid relation-summary-grid">
        <div><span class="detail-label">信息家族</span><strong>${escapeHtml(text(informationClusters.length, "0"))}</strong><small>cluster</small></div>
        <div><span class="detail-label">相关 pair</span><strong>${escapeHtml(text(relationGraph.summary?.available_pair_count || topPairs.length, "0"))}</strong><small>全量计算</small></div>
        <div><span class="detail-label">强相关簇</span><strong>${escapeHtml(text(redundancyClusters.length, "0"))}</strong><small>watch</small></div>
      </div>
      <div class="relation-inline-head">
        <div>
          <p class="eyebrow">Information Families</p>
          <h4>全库因子关系图</h4>
        </div>
        <span>展示全部 active 因子、各信息家族及代表因子之间的强关系；相关性仍来自同一份信息簇审计。</span>
      </div>
      ${renderRelationGraphMarkup(relationGraph, informationClusters, topPairs)}
      <details class="raw-event compact-raw">
        <summary>展开聚类明细</summary>
        <pre>${escapeHtml(JSON.stringify({ information_clusters: informationClusters, redundancy_clusters: redundancyClusters, top_correlated_pairs: topPairs.slice(0, 20) }, null, 2))}</pre>
      </details>
    `;
  }
}

function renderInspector() {
  const containers = document.querySelectorAll(".inspector-detail");
  if (!containers.length) return;
  const inspector = state.inspector;
  if (!inspector) {
    hideInspector("#inspector-detail");
    hideInspector("#library-inspector-detail");
    return;
  }

  if (inspector.kind === "candidate") {
    const item = inspector.payload || {};
    const summary = candidateMetrics(item);
    const auto = item.persistence_diagnostic || item.autocorrelation || item.metrics?.autocorrelation || {};
    const anti = item.anti_overfit_summary || item.anti_overfit || item.metrics?.anti_overfit || {};
    const adv = item.adversarial_validation || item.deep_validation?.adversarial_validation || {};
    const novelty = item.novelty_metrics || item.novelty_guard || item.deep_validation?.novelty_correlation || item.novelty_correlation || {};
    const matchedFactorId = novelty.matched_existing_factor_id || novelty.matched_existing_factor || item.matched_existing_factor_id || item.matched_existing_factor;
    const matchedFactorName = novelty.matched_existing_factor_name || item.matched_existing_factor_name;
    const matchedExpression = novelty.matched_existing_expression_summary || item.matched_existing_expression_summary;
    const matchedClusterId = novelty.matched_information_cluster_id || item.matched_information_cluster_id;
    const reasons = candidateRejectReasons(item);
    const grade = candidateGrade(item);
    const gradeLabel = item?.grade_provenance === "quick_score" ? `快筛 ${grade}` : grade;
    const stageLabel = candidateStageLabel(item);
    const decision = candidateDecision(item);
    const official = item.deep_validation?.score_parts || {};
    const stageHistory = (item.stage_history || []).slice().reverse().slice(0, 8);
    const taskHistory = (item.task_history || []).slice(0, 10);
    const candidateTitle = candidateDisplayName(item, item.factor_id || "候选因子");
    const present = (value) => value !== undefined && value !== null && value !== "";
    const metricCard = (label, value) => `
      <div><span class="detail-label">${escapeHtml(label)}</span><strong>${escapeHtml(String(value))}</strong></div>
    `;
    const coreMetricCards = [
      ["IC", summary.ic_mean, (value) => shortNumber(value, 4)],
      ["ICIR", summary.ic_ir, (value) => shortNumber(value, 4)],
      ["Rank IC", summary.rank_ic_mean, (value) => shortNumber(value, 4)],
      ["Rank ICIR", summary.rank_ic_ir, (value) => shortNumber(value, 4)],
      ["Sharpe", summary.sharpe, (value) => shortNumber(value, 3)],
      ["年化", summary.annual_return, (value) => pct(value, 2)],
      ["胜率", summary.ic_win_rate, (value) => pct(value, 2)],
      ["换手", summary.turnover, (value) => shortNumber(value, 3)],
    ].filter(([, value]) => present(value)).map(([label, value, format]) => metricCard(label, format(value))).join("");
    const antiScore = anti.score ?? item.anti_overfit_score ?? item.deep_validation?.score_parts?.component_scores?.anti_overfit;
    const adversarialEvidence = present(adv.passed_count)
      ? `${text(adv.passed_count, "0")}/${text(adv.total_count, "4")}`
      : present(adv.score) ? shortNumber(adv.score, 1) : "";
    const noveltyP = novelty.max_existing_pearson ?? novelty.max_pearson;
    const noveltyR = novelty.max_existing_rank_corr ?? novelty.max_rank_corr;
    const p90P = novelty.p90_pearson ?? novelty.p90_existing_pearson;
    const p90R = novelty.p90_rank_corr ?? novelty.p90_existing_rank_corr;
    const evidenceCards = [
      ["抗过拟合", antiScore, (value) => shortNumber(value, 1)],
      ["对抗验证", adversarialEvidence, (value) => value],
      ["Novelty P", noveltyP, (value) => shortNumber(value, 4)],
      ["Novelty R", noveltyR, (value) => shortNumber(value, 4)],
      ["p90 P", p90P, (value) => shortNumber(value, 4)],
      ["p90 R", p90R, (value) => shortNumber(value, 4)],
      ["匹配因子", matchedFactorName || matchedFactorId, (value) => value],
      ["信息簇", matchedClusterId, (value) => value],
      ["Risk", auto.risk_flag, (value) => value],
      ["Lag1", auto.ic_lag1_autocorr, (value) => shortNumber(value, 4)],
    ].filter(([, value]) => present(value)).map(([label, value, format]) => metricCard(label, format(value))).join("");
    const evidencePending = summary.quick_score === undefined
      ? `当前处于${candidateStageLabel(item)}：快筛与回测尚未完成；通过互相关检查后才会计算抗过拟合、Rolling 和对抗验证。`
      : "快筛已返回，等待后续互相关或深度验证证据。";
    hideInspector("#library-inspector-detail");
    paintInspector("#inspector-detail", `
      <div class="candidate-detail-card">
        <div class="candidate-detail-head">
          <div>
            <p class="eyebrow">Candidate Detail</p>
            <h3>${escapeHtml(candidateTitle)}</h3>
          </div>
          <div class="inspector-badges">
            <span class="badge grade-${String(grade || "p").toLowerCase()}" title="${escapeHtml(item?.grade_provenance === "quick_score" ? "Quick Score 映射等级，不代表最终入库结论" : "当前可用等级")}">${escapeHtml(text(gradeLabel, "--"))}</span>
            <span class="badge tone-${candidateStatusTone(stageLabel)}">${escapeHtml(stageLabel)}</span>
            <span class="badge tone-${candidateStatusTone(decision)}">${escapeHtml(candidateDecisionLabel(decision))}</span>
            <span class="badge subtle">Quick ${shortNumber(summary.quick_score, 1)}</span>
            <span class="badge subtle">Deep ${shortNumber(summary.deep_score, 1)}</span>
            <span class="badge subtle">${escapeHtml(text(item.holding_period_days || item.holding_period, 5))}D</span>
          </div>
        </div>
        <div class="detail-expression compact-expression">
          <span class="detail-label">表达式</span>
          <code>${escapeHtml(text(item.expression, "暂无表达式"))}</code>
        </div>
        <div class="candidate-detail-grid">
          ${coreMetricCards ? `<section class="candidate-detail-panel">
            <span class="detail-label">核心指标</span>
            <div class="detail-grid compact-metric-grid">${coreMetricCards}</div>
          </section>` : ""}
          <section class="candidate-detail-panel">
            <span class="detail-label">验证证据</span>
            ${evidenceCards ? `<div class="detail-grid compact-metric-grid">${evidenceCards}</div>` : `<p class="evidence-pending">${escapeHtml(evidencePending)}</p>`}
            ${matchedExpression ? `<div class="detail-expression compact-expression"><span class="detail-label">匹配表达式</span><code>${escapeHtml(matchedExpression)}</code></div>` : ""}
          </section>
          <section class="candidate-detail-panel conclusion-panel">
            <span class="detail-label">结论</span>
            <p>${escapeHtml(text(screeningSummary(item), "暂无质量结论"))}${reasons.length ? ` · ${escapeHtml(reasons.join("；"))}` : ""}</p>
            <div class="inspector-badges compact-badges">
              <span class="badge subtle">Official ${shortNumber(summary.deep_score, 1)}</span>
              <span class="badge subtle">抗过拟合 ${shortNumber(official.anti_overfit_score, 1)}</span>
              <span class="badge subtle">${escapeHtml(text(official.official_grade, "Grade --"))}</span>
            </div>
          </section>
        </div>
        ${stageHistory.length ? `
          <details class="candidate-history">
            <summary>研究轨迹 <span>${stageHistory.length} 个节点</span></summary>
            <ol>
              ${stageHistory.map((step) => `
                <li>
                  <b>${escapeHtml(text(researchStepTitle(step), step.stage || "step"))}</b>
                  <small>${escapeHtml(ageLabel(step.ts))}</small>
                  ${step.summary || step.decision || step.next ? `<p>${escapeHtml(clip(step.summary || step.decision || step.next || "", 220))}</p>` : ""}
                </li>
              `).join("")}
            </ol>
          </details>
        ` : ""}
        ${taskHistory.length ? `
          <details class="candidate-history">
            <summary>工具任务 <span>${taskHistory.length} 项</span></summary>
            <ol>
              ${taskHistory.map((task) => `
                <li>
                  <b>${escapeHtml(text(task.task_type, "task"))} · ${escapeHtml(text(task.status, "--"))}</b>
                  <small>${escapeHtml(ageLabel(task.completed_at || task.created_at))}</small>
                  <p>${escapeHtml(clip(task.error || JSON.stringify(task.result_summary || {}), 220))}</p>
                </li>
              `).join("")}
            </ol>
          </details>
        ` : ""}
        <details class="raw-event compact-raw">
          <summary>展开候选原始 JSON</summary>
          <pre>${escapeHtml(JSON.stringify(item, null, 2))}</pre>
        </details>
      </div>
    `);
    return;
  }

  if (inspector.kind === "library") {
    const item = inspector.payload || {};
    const metadata = parseMetadata(item);
    const wq = metadata.wq || {};
    const anti = item.anti_overfit_summary || metadata.anti_overfit_summary || {};
    const adv = item.adversarial_validation || metadata.adversarial_validation || {};
    const novelty = item.novelty_guard || metadata.novelty_guard || {};
    const persistence = item.persistence_diagnostic || metadata.persistence_diagnostic || {};
    const factorName = item.name || metadata.factor_name || metadata.category_info?.suggested_factor_name || item.factor_id;
    const thesis = item.economic_thesis || metadata.economic_thesis || {};
    const hypothesis = item.hypothesis || metadata.hypothesis || thesis.translation_plan || "";
    hideInspector("#inspector-detail");
    paintInspector("#library-inspector-detail", `
      <div class="library-detail-card">
        <div class="library-detail-head">
          <div>
            <p class="eyebrow">Selected Factor</p>
            <h4>${escapeHtml(text(factorName))}</h4>
            <span>${escapeHtml(text(item.factor_id || item.name))}</span>
          </div>
          <div class="inspector-badges compact-badges">
            <span class="badge subtle">${escapeHtml(text(item.status || "unknown"))}</span>
            <span class="badge subtle">${escapeHtml(text(item.category || "未分类"))}</span>
            <span class="badge subtle">${escapeHtml(text(item.holding_period_days, 5))}D</span>
          </div>
        </div>
      <div class="detail-expression compact-expression">
        <span class="detail-label">表达式</span>
        <code>${escapeHtml(text(item.expression, "暂无表达式"))}</code>
      </div>
      <div class="detail-grid compact-metric-grid library-metric-grid">
        <div><span class="detail-label">IC</span><strong>${shortNumber(item.ic_mean, 4)}</strong></div>
        <div><span class="detail-label">ICIR</span><strong>${shortNumber(item.icir, 4)}</strong></div>
        <div><span class="detail-label">Rank IC</span><strong>${shortNumber(libraryMetric(item, "rank_ic_mean"), 4)}</strong></div>
        <div><span class="detail-label">Rank ICIR</span><strong>${shortNumber(libraryMetric(item, "rank_icir"), 4)}</strong></div>
        <div><span class="detail-label">Sharpe</span><strong>${shortNumber(item.sharpe, 3)}</strong></div>
        <div><span class="detail-label">年化</span><strong>${pct(libraryMetric(item, "annual_return"), 2)}</strong></div>
        <div><span class="detail-label">Deep Score</span><strong>${shortNumber(libraryMetric(item, "deep_score"), 1)}</strong></div>
        <div><span class="detail-label">换手率</span><strong>${shortNumber(libraryMetric(item, "turnover"), 3)}</strong></div>
      </div>
      <details class="library-detail-section">
        <summary>入库与验证信息</summary>
        <div class="detail-grid compact-metric-grid">
        <div><span class="detail-label">WQ 状态</span><strong>${escapeHtml(text(wq.last_status || "未提交"))}</strong></div>
        <div><span class="detail-label">WQ Alpha ID</span><strong>${escapeHtml(text(wq.alpha_id))}</strong></div>
        <div><span class="detail-label">数据列名</span><strong>${escapeHtml(text(metadata.data_column))}</strong></div>
        <div><span class="detail-label">数据文件</span><strong>${escapeHtml(text(metadata.data_path))}</strong></div>
        <div><span class="detail-label">Anti-Overfit</span><strong>${escapeHtml(text(anti.recommendation || anti.score, "--"))}</strong></div>
        <div><span class="detail-label">Adversarial</span><strong>${escapeHtml(adv.passed_count !== undefined ? `${text(adv.passed_count, "0")}/${text(adv.total_count, "4")} · ${text(adv.recommendation, "--")}` : "--")}</strong></div>
        <div><span class="detail-label">Novelty</span><strong>${escapeHtml(text(novelty.allowed, "--"))}</strong></div>
        <div><span class="detail-label">Persistence</span><strong>${escapeHtml(text(persistence.risk_flag, "--"))}</strong></div>
        <div><span class="detail-label">来源</span><strong>${escapeHtml(text(item.source || metadata.source || "quantgpt"))}</strong></div>
        <div><span class="detail-label">创建时间</span><strong>${escapeHtml(text(item.created_at || metadata.created_at))}</strong></div>
      </div>
      </details>
      <details class="library-detail-section">
        <summary>研究假说与原始记录</summary>
        <div class="detail-copy">
        <span class="detail-label">研究假说</span>
        <p>${escapeHtml(text(hypothesis, "暂无研究假说"))}</p>
      </div>
      <div class="detail-copy">
        <span class="detail-label">因子注册原始记录</span>
        <pre>${escapeHtml(JSON.stringify(item, null, 2))}</pre>
      </div>
      </details>
      </div>
    `);
  }
}

function renderResearchPanels() {
  const factorConsole = serviceOutputs(state.factorConsole);
  const activeJob = researchProjection();
  const latestResearch = {};

  if (activeJob?.run_id) {
    state.lastRunId = activeJob.run_id;
  }

  renderCompactSystemStrip(activeJob);
  renderResearchProgressBoard(activeJob);
  renderResearchLiveDesk(activeJob, latestResearch);
  renderCandidateResultTable();
  renderGuidance(activeJob);
  renderRecentNotes(factorConsole.recent_notes || []);
  renderFactorMapWorkspace();
  renderOrchestratorTraceWorkspace();
  renderCommandConsole();
  renderFactorLibrary(factorConsole);
  renderInspector();
}

function renderEvaluationModeBar() {
  const bar = document.getElementById("evaluation-mode-bar");
  const indicator = document.getElementById("evaluation-mode-indicator");
  const status = serviceOutputs(state.evaluationProfile);
  const activeMode = status.active_default_mode || status.active_profile?.evaluation_mode || "";
  const labels = { research: "研究模式", production: "生产模式" };
  if (indicator) {
    indicator.dataset.mode = activeMode;
    const label = indicator.querySelector("strong");
    if (label) label.textContent = labels[activeMode] || "模式未加载";
  }
  if (!bar) return;
  const copy = bar.querySelector(".evaluation-mode-copy");
  if (copy) {
    copy.innerHTML = `
      <p class="eyebrow">Evaluation Profile</p>
      <div>
        <strong>${escapeHtml(labels[activeMode] || "模式未加载")}</strong>
        <span>平台默认 · 仅影响新建任务</span>
      </div>
    `;
  }
  bar.querySelectorAll("[data-evaluation-mode]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.evaluationMode === activeMode);
    button.disabled = state.evaluationModeSwitching;
    button.setAttribute("aria-pressed", button.dataset.evaluationMode === activeMode ? "true" : "false");
  });
}

async function switchEvaluationMode(evaluationMode) {
  const current = serviceOutputs(state.evaluationProfile).active_default_mode;
  if (!evaluationMode || evaluationMode === current || state.evaluationModeSwitching) return;
  state.evaluationModeSwitching = true;
  renderEvaluationModeBar();
  try {
    const result = await postJson("/platform/evaluation-profile", {
      evaluation_mode: evaluationMode,
      changed_by: "web_gui",
    });
    if (!result?.ok) {
      window.alert(`模式切换失败：${text(result?.err || result?.outputs?.detail, "unknown error")}`);
      return;
    }
    state.evaluationProfile = result;
    state.factorStatus = await getJsonSafe("/factor/status");
    applyResearchRuntimeDefaults();
  } finally {
    state.evaluationModeSwitching = false;
    renderAll();
  }
}

function renderAll() {
  renderEvaluationModeBar();
  renderApiChip();
  renderMiniMetrics();
  const activePanel = document.querySelector(".panel.active")?.id?.replace(/^panel-/, "") || state.activePanel || "overview";
  if (activePanel === "overview") renderOverviewCockpit();
  if (activePanel === "research") {
    renderResearchSummary();
    renderResearchPanels();
  }
  if (activePanel === "library") {
    renderFactorLibrary(serviceOutputs(state.factorConsole));
    renderInspector();
  }
  if (activePanel === "model-research") renderModelResearch();
  if (activePanel === "model-library") renderModelLibrary();
  if (activePanel === "trading") renderTrading();
  if (activePanel === "data-foundation") renderDataFoundation();
  if (activePanel === "stock-research") renderStockResearch();
  queueFloatingXScrollbarRefresh();
}

async function refreshState({ reason = "manual" } = {}) {
  if (state.refreshInFlight) {
    state.pendingRefreshReason = reason;
    return;
  }
  state.refreshInFlight = true;
  const visiblePanel = document.querySelector(".panel.active")?.id?.replace(/^panel-/, "");
  const activePanel = visiblePanel || state.activePanel || "overview";
  state.activePanel = activePanel;
  setPanelBusy(activePanel, true);
  const wantsOverview = activePanel === "overview";
  const wantsResearchPanel = activePanel === "research";
  const wantsResearch = wantsResearchPanel;
  const wantsModelResearch = activePanel === "model-research";
  const wantsFactorLibrary = wantsOverview || activePanel === "library" || wantsResearchPanel || wantsModelResearch;
  const wantsFactorStatus = !wantsOverview && wantsFactorLibrary;
  const wantsModelLibrary = activePanel === "model-library" || activePanel === "model-research";
  const wantsModelStatus = wantsModelResearch;
  const wantsPaperModelCatalog = activePanel === "trading"
    && state.paperTradingTab === "console"
    && state.paperConsoleTab === "create";
  const wantsModelRegistry = wantsModelLibrary || wantsPaperModelCatalog;
  const wantsTrading = activePanel === "trading" || wantsOverview;
  const wantsPaperRisk = activePanel === "trading" && (
    state.paperTradingTab === "risk"
    || (state.paperTradingTab === "console" && state.paperConsoleTab === "settings")
  );
  const wantsDailyOps = activePanel === "trading"
    && state.paperTradingTab === "console"
    && ["status", "diagnostics"].includes(state.paperConsoleTab);
  const wantsPaperBenchmark = activePanel === "trading" && state.paperTradingTab === "overview";
  const wantsFullPaperFleet = activePanel === "trading" && state.paperTradingTab !== "console";
  const wantsData = activePanel === "data-foundation" || wantsOverview;
  const wantMaintenance = false;
  const overviewReadOptions = wantsOverview ? { timeoutMs: 6000 } : {};
  const overviewFactorConsoleOptions = wantsOverview ? { timeoutMs: 8000 } : overviewReadOptions;
  const overviewAuxReadOptions = wantsOverview ? { timeoutMs: 1500 } : overviewReadOptions;

  const previous = {
    data: state.data,
    factorStatus: state.factorStatus,
    factorConsole: state.factorConsole,
    factorResearchPreflight: state.factorResearchPreflight,
    factorResearchControl: state.factorResearchControl,
    orchestratorEvents: state.orchestratorEvents,
    orchestratorTraces: state.orchestratorTraces,
    modelOrchestratorEvents: state.modelOrchestratorEvents,
    modelOrchestratorTraces: state.modelOrchestratorTraces,
    factorOverviewSnapshot: state.factorOverviewSnapshot,
    factorLibraryRaw: state.factorLibraryRaw,
    duplicateAudit: state.duplicateAudit,
    factorAudit: state.factorAudit,
    factorAuditRunStatus: state.factorAuditRunStatus,
    modelStatus: state.modelStatus,
    modelPreflight: state.modelPreflight,
    modelFeatureSets: state.modelFeatureSets,
    modelRuns: state.modelRuns,
    modelRegistry: state.modelRegistry,
    modelProduction: state.modelProduction,
    modelBacktest: state.modelBacktest,
    predictionStatus: state.predictionStatus,
    tradingStatus: state.tradingStatus,
    riskPolicyStatus: state.riskPolicyStatus,
    dailyOpsStatus: state.dailyOpsStatus,
    paperFleetStatus: state.paperFleetStatus,
    paperBenchmark: state.paperBenchmark,
    pipelineStatus: state.pipelineStatus,
    platformRuntime: state.platformRuntime,
    automationStatus: state.automationStatus,
    codexUsageSnapshot: state.codexUsageSnapshot,
    deepseekUsageSnapshot: state.deepseekUsageSnapshot,
	    maintenanceStatus: state.maintenanceStatus,
	    modelOrchestratorStatus: state.modelOrchestratorStatus,
	    modelCurrentContext: state.modelCurrentContext,
	    modelResearchCurrent: state.modelResearchCurrent,
	    modelResearchJournal: state.modelResearchJournal,
	    modelResearchOrchTraces: state.modelResearchOrchTraces,
	    modelResearchMcpTraces: state.modelResearchMcpTraces,
	  };

  try {
    const health = await getJsonSafe("/health");
    state.health = health;
    renderApiChip();
    const evaluationProfile = await getJsonSafe("/platform/evaluation-profile", { timeoutMs: 6000 });
    state.evaluationProfile = keepPreviousOnReadFailure(evaluationProfile, state.evaluationProfile);
    const [
      data,
      factorStatus,
      factorConsoleResp,
      factorResearchPreflight,
      factorResearchControl,
      orchestratorEvents,
      orchestratorTraces,
      modelOrchestratorEvents,
      modelOrchestratorTraces,
      factorOverviewSnapshot,
      factorLibraryRaw,
      duplicateAudit,
      factorAudit,
      factorAuditRunStatus,
      modelStatus,
      modelPreflight,
      modelResearchCurrent,
      modelResearchJournal,
      modelResearchOrchTraces,
      modelFeatureSets,
      modelRuns,
      modelRegistry,
      modelProduction,
      modelBacktest,
      predictionStatus,
      tradingStatus,
      riskPolicyStatus,
      dailyOpsStatus,
      paperFleetStatus,
      paperBenchmark,
      pipelineStatus,
      platformRuntime,
      codexUsageSnapshot,
      deepseekUsageSnapshot,
	      maintenanceStatus,
	      modelOrchestratorStatus,
	      modelCurrentContext,
	      modelResearchMcpTraces,
	    ] = await Promise.all([
      wantsData ? getJsonSafe("/data/status", overviewReadOptions) : Promise.resolve(previous.data),
      wantsFactorStatus ? getJsonSafe("/factor/status", overviewReadOptions) : Promise.resolve(previous.factorStatus),
      wantsResearch ? getFactorConsoleSafe(overviewFactorConsoleOptions) : Promise.resolve(previous.factorConsole),
      wantsResearchPanel && state.activeWorkspace === "command" ? getJsonSafe("/factor/research/preflight", overviewReadOptions) : Promise.resolve(previous.factorResearchPreflight),
      (wantsOverview || wantsResearchPanel) ? getJsonSafe(`/factor/research/control${wantsOverview ? "?compact=true" : ""}`, overviewReadOptions) : Promise.resolve(previous.factorResearchControl),
      wantsResearchPanel && ["run", "orch-trace"].includes(state.activeWorkspace) ? getJsonSafe(orchestratorEventsUrl(), overviewAuxReadOptions) : Promise.resolve(previous.orchestratorEvents),
      wantsResearchPanel && state.activeWorkspace === "orch-trace" ? getJsonSafe(orchestratorTracesUrl({ includePayload: false }), overviewAuxReadOptions) : Promise.resolve(previous.orchestratorTraces),
      wantsModelResearch ? getJsonSafe(`${MODEL_API_PREFIX}/orchestrator/events?limit=140&include_payload=false`, overviewAuxReadOptions) : Promise.resolve(previous.modelOrchestratorEvents),
      wantsModelResearch ? getJsonSafe(`${MODEL_API_PREFIX}/orchestrator/traces?limit=100&include_payload=${state.activeModelWorkspace === "orch-trace" ? "true" : "false"}`, overviewAuxReadOptions) : Promise.resolve(previous.modelOrchestratorTraces),
      wantsResearch ? getJsonSafe("/gui/factor_overview_snapshot.json", { timeoutMs: 2500 }) : Promise.resolve(previous.factorOverviewSnapshot),
      wantsFactorLibrary ? getJsonSafe(`/factors?status=active&limit=${wantsOverview ? "1" : "500"}&sort_by=icir&compact=${wantsOverview ? "true" : "false"}`, overviewReadOptions) : Promise.resolve(previous.factorLibraryRaw),
      activePanel === "library" ? getJsonSafe("/factor/registry/duplicates") : Promise.resolve(previous.duplicateAudit),
      wantsFactorLibrary ? getJsonSafe(`/factor/library/audit/status${activePanel === "library" ? "" : "?compact=true"}`, overviewReadOptions) : Promise.resolve(previous.factorAudit),
      (activePanel === "library" || wantsModelResearch) ? getJsonSafe("/factor/library/audit/run-status") : Promise.resolve(previous.factorAuditRunStatus),
      wantsModelStatus ? getJsonSafe(`${MODEL_API_PREFIX}/status`, overviewReadOptions) : Promise.resolve(previous.modelStatus),
      wantsModelResearch ? getJsonSafe(`${MODEL_API_PREFIX}/preflight`, overviewReadOptions) : Promise.resolve(previous.modelPreflight),
      (wantsOverview || wantsModelResearch) ? getJsonSafe(`${MODEL_API_PREFIX}/research/current`, overviewReadOptions) : Promise.resolve(previous.modelResearchCurrent),
      wantsModelResearch ? getJsonSafe(`${MODEL_API_PREFIX}/research/journal?limit=120`, overviewReadOptions) : Promise.resolve(previous.modelResearchJournal),
      Promise.resolve(previous.modelResearchOrchTraces),
      wantsModelResearch ? getJsonSafe(`${MODEL_API_PREFIX}/feature-sets?limit=100&compact=true`, { timeoutMs: 30000 }) : Promise.resolve(previous.modelFeatureSets),
      wantsModelResearch ? getJsonSafe(`${MODEL_API_PREFIX}/runs`, overviewReadOptions) : Promise.resolve(previous.modelRuns),
      wantsModelRegistry ? getJsonSafe(`${MODEL_API_PREFIX}/registry?status=${wantsPaperModelCatalog ? "production" : "library"}&compact=true`, overviewReadOptions) : Promise.resolve(previous.modelRegistry),
      wantsModelRegistry ? getJsonSafe(`${MODEL_API_PREFIX}/production`, overviewReadOptions) : Promise.resolve(previous.modelProduction),
      Promise.resolve(previous.modelBacktest),
      Promise.resolve(previous.predictionStatus),
      wantsTrading ? getJsonSafe("/trade/status?compact=true", overviewReadOptions) : Promise.resolve(previous.tradingStatus),
      wantsPaperRisk ? getJsonSafe(`/trade/risk-policy?history_days=160${state.selectedPaperAccountId ? `&account_id=${encodeURIComponent(state.selectedPaperAccountId)}` : ""}`, { timeoutMs: 12000 }) : Promise.resolve(previous.riskPolicyStatus),
      wantsDailyOps ? getJsonSafe("/daily-ops/status", overviewReadOptions) : Promise.resolve(previous.dailyOpsStatus),
      wantsTrading ? getJsonSafe(`/paper/status${wantsFullPaperFleet ? "" : "?compact=true"}`, overviewReadOptions) : Promise.resolve(previous.paperFleetStatus),
      wantsPaperBenchmark ? getJsonSafe(paperBenchmarkQueryUrl(), overviewReadOptions) : Promise.resolve(previous.paperBenchmark),
      Promise.resolve(previous.pipelineStatus),
      Promise.resolve(previous.platformRuntime),
	      Promise.resolve(previous.codexUsageSnapshot),
	      Promise.resolve(previous.deepseekUsageSnapshot),
	      wantMaintenance ? getJsonSafe("/maintenance/status") : Promise.resolve(previous.maintenanceStatus),
	      wantsModelResearch ? getJsonSafe(`${MODEL_API_PREFIX}/orchestrator/status`, overviewReadOptions) : Promise.resolve(previous.modelOrchestratorStatus),
	      wantsModelResearch ? getJsonSafe(`${MODEL_API_PREFIX}/context/current`, overviewReadOptions) : Promise.resolve(previous.modelCurrentContext),
	      wantsModelResearch ? getJsonSafe(`${MODEL_API_PREFIX}/mcp/traces?limit=100&include_payload=${state.activeModelWorkspace === "orch-trace" ? "true" : "false"}`, overviewAuxReadOptions) : Promise.resolve(previous.modelResearchMcpTraces),
	    ]);
  state.data = keepPreviousOnReadFailure(data, previous.data);
  state.factorStatus = keepPreviousOnReadFailure(factorStatus, previous.factorStatus);
  state.factorResearchPreflight = keepPreviousOnReadFailure(factorResearchPreflight, previous.factorResearchPreflight);
  state.factorResearchControl = keepPreviousOnReadFailure(factorResearchControl, previous.factorResearchControl);
  state.orchestratorEvents = keepPreviousOnReadFailure(orchestratorEvents, previous.orchestratorEvents);
  state.orchestratorTraces = keepPreviousOnReadFailure(orchestratorTraces, previous.orchestratorTraces);
  state.modelOrchestratorEvents = keepPreviousOnReadFailure(modelOrchestratorEvents, previous.modelOrchestratorEvents);
  state.modelOrchestratorTraces = keepPreviousOnReadFailure(modelOrchestratorTraces, previous.modelOrchestratorTraces);
  state.factorOverviewSnapshot = keepPreviousOnReadFailure(factorOverviewSnapshot, previous.factorOverviewSnapshot);
  state.factorLibraryRaw = keepPreviousOnReadFailure(factorLibraryRaw, previous.factorLibraryRaw);
  state.duplicateAudit = keepPreviousOnReadFailure(duplicateAudit, previous.duplicateAudit);
  state.factorAudit = keepPreviousOnReadFailure(factorAudit, previous.factorAudit);
  state.factorAuditRunStatus = keepPreviousOnReadFailure(factorAuditRunStatus, previous.factorAuditRunStatus);
	  if (wantsResearch && factorConsoleResp && !factorConsoleResp._failed && !factorConsoleResp.error) {
	    state.backendMode = "console";
	    state.factorConsole = normalizeFactorConsole(factorConsoleResp);
	  } else if (wantsResearch && factorStatus && !factorStatus._failed && !factorStatus.error) {
	    state.backendMode = "status";
	    state.factorConsole = normalizeFactorConsole(factorStatus);
	  } else if (wantsResearch) {
	    state.backendMode = "offline_snapshot";
	    state.factorConsole = offlineResearchConsoleFromSnapshot(state.factorOverviewSnapshot);
	  } else if (!state.factorConsole) {
    state.factorConsole = normalizeFactorConsole({ ok: true, outputs: {} });
  }
  state.modelStatus = keepPreviousOnReadFailure(modelStatus, previous.modelStatus);
  state.modelPreflight = keepPreviousOnReadFailure(modelPreflight, previous.modelPreflight);
  state.modelResearchCurrent = keepPreviousOnReadFailure(modelResearchCurrent, previous.modelResearchCurrent);
  state.modelResearchJournal = keepPreviousOnReadFailure(modelResearchJournal, previous.modelResearchJournal);
  state.modelResearchOrchTraces = keepPreviousOnReadFailure(modelOrchestratorTraces, keepPreviousOnReadFailure(modelResearchOrchTraces, previous.modelResearchOrchTraces));
  state.modelFeatureSets = keepPreviousOnReadFailure(modelFeatureSets, previous.modelFeatureSets);
  state.modelRuns = keepPreviousOnReadFailure(modelRuns, previous.modelRuns);
  state.modelRegistry = keepPreviousOnReadFailure(modelRegistry, previous.modelRegistry);
  state.modelProduction = keepPreviousOnReadFailure(modelProduction, previous.modelProduction);
  state.modelBacktest = keepPreviousOnReadFailure(modelBacktest, previous.modelBacktest);
  if (modelBacktestWorkspaceIsVisible()) {
    await loadSelectedModelBacktest({ force: reason === "manual", render: false, includeDaily: true });
  }
  state.tradingStatus = keepPreviousOnReadFailure(tradingStatus, previous.tradingStatus);
  state.riskPolicyStatus = keepPreviousOnReadFailure(riskPolicyStatus, previous.riskPolicyStatus);
  const embeddedPredictionStatus = serviceOutputs(state.tradingStatus).prediction;
  state.predictionStatus = embeddedPredictionStatus
    ? keepPreviousOnReadFailure(embeddedPredictionStatus, previous.predictionStatus)
    : keepPreviousOnReadFailure(predictionStatus, previous.predictionStatus);
  state.dailyOpsStatus = keepPreviousOnReadFailure(dailyOpsStatus, previous.dailyOpsStatus);
  state.paperFleetStatus = keepPreviousOnReadFailure(paperFleetStatus, previous.paperFleetStatus);
  state.paperBenchmark = keepPreviousOnReadFailure(paperBenchmark, previous.paperBenchmark);
  state.pipelineStatus = keepPreviousOnReadFailure(pipelineStatus, previous.pipelineStatus);
  state.platformRuntime = keepPreviousOnReadFailure(platformRuntime, previous.platformRuntime);
  state.codexUsageSnapshot = keepPreviousOnReadFailure(codexUsageSnapshot, previous.codexUsageSnapshot);
	  state.deepseekUsageSnapshot = keepPreviousOnReadFailure(deepseekUsageSnapshot, previous.deepseekUsageSnapshot);
	  state.modelOrchestratorStatus = keepPreviousOnReadFailure(modelOrchestratorStatus, previous.modelOrchestratorStatus);
	  state.modelCurrentContext = keepPreviousOnReadFailure(modelCurrentContext, previous.modelCurrentContext);
	  state.modelResearchMcpTraces = keepPreviousOnReadFailure(modelResearchMcpTraces, previous.modelResearchMcpTraces);
  if (wantsOverview && (
    !state.codexUsageSnapshot || state.codexUsageSnapshot._failed
    || !state.deepseekUsageSnapshot || state.deepseekUsageSnapshot._failed
  )) {
    Promise.all([
      getJsonSafe("/gui/codex_usage_snapshot.json", { timeoutMs: 6000 }),
      getJsonSafe("/gui/deepseek_usage_snapshot.json", { timeoutMs: 6000 }),
    ]).then(([codexSnapshot, deepseekSnapshot]) => {
      state.codexUsageSnapshot = keepPreviousOnReadFailure(codexSnapshot, state.codexUsageSnapshot);
      state.deepseekUsageSnapshot = keepPreviousOnReadFailure(deepseekSnapshot, state.deepseekUsageSnapshot);
      renderOverviewCockpit();
    }).catch(() => {});
  }
  if (wantsOverview) {
    getJsonSafe(paperBenchmarkQueryUrl(), { timeoutMs: 6000 }).then((benchmark) => {
      if (benchmark && !benchmark._failed) {
        state.paperBenchmark = benchmark;
        renderOverviewCockpit();
      }
    }).catch(() => {});
  }
  if (wantsOverview || wantsResearchPanel) {
    getJsonSafe(`/platform/runtime-status${wantsOverview ? "?compact=true" : ""}`, { timeoutMs: wantsOverview ? 3000 : 15000 }).then((runtimeStatus) => {
      if (runtimeStatus && !runtimeStatus._failed) {
        state.platformRuntime = runtimeStatus;
        if (wantsOverview) renderOverviewCockpit();
        if (wantsResearchPanel && researchPanelIsVisible()) {
          renderCompactSystemStrip(researchProjection());
        }
      } else if (!state.platformRuntime) {
        state.platformRuntime = runtimeStatus;
        if (researchPanelIsVisible()) renderCompactSystemStrip(researchProjection());
      }
    }).catch(() => {
      if (!state.platformRuntime) {
        state.platformRuntime = { _failed: true, error: "platform_runtime_status_unavailable" };
        if (researchPanelIsVisible()) renderCompactSystemStrip(researchProjection());
      }
    });
  }
  if (activePanel === "data-foundation" || activePanel === "trading") {
    getJsonSafe("/platform/automation-status", { timeoutMs: 5000 }).then((automationStatus) => {
      state.automationStatus = keepPreviousOnReadFailure(automationStatus, state.automationStatus);
      if (activePanel === "data-foundation") renderDataFoundation();
      if (activePanel === "trading") renderTrading();
    }).catch(() => {
      if (!state.automationStatus) {
        state.automationStatus = { _failed: true, error: "platform_automation_status_unavailable" };
      }
      if (activePanel === "data-foundation") renderDataFoundation();
      if (activePanel === "trading") renderTrading();
    });
  }
  state.maintenanceStatus = maintenanceStatus;
  if (activePanel === "data-foundation" && state.dataFoundationTab === "live") {
    state.dataLiveStatus = await getJsonSafe("/data/live-status");
  }
  if (activePanel === "data-foundation" && state.dataFoundationTab === "query" && !state.dataQueryFields) {
    state.dataQueryFields = await getJsonSafe("/data/query/fields");
  }
  applyResearchRuntimeDefaults();
  state.lastRefreshAt = Date.now();
  state.nextAutoRefreshAt = state.lastRefreshAt + AUTO_REFRESH_INTERVAL_MS;
  renderAll();
  if (wantsModelResearch) {
    const selectedFeatureSetId = modelResearchFeatureSetId();
    const selectedPreflightId = text(modelCommandPreflightOutputs().feature_set_id, "");
    if (selectedFeatureSetId && selectedPreflightId !== selectedFeatureSetId) {
      window.setTimeout(() => refreshModelCommandPreflight().catch(() => {}), 0);
    }
  }
  } catch (error) {
    if (activePanel === "overview") renderOverviewFailure(error);
    throw error;
  } finally {
    setPanelBusy(activePanel, false);
    state.refreshInFlight = false;
    const pendingReason = state.pendingRefreshReason;
    state.pendingRefreshReason = null;
    if (reason !== "auto") {
      startPolling();
    }
    startLiveResearchPolling();
    scheduleDataLivePolling();
    if (pendingReason) {
      setTimeout(() => refreshState({ reason: pendingReason }), 0);
    }
  }
}

function researchPanelIsVisible() {
  const visiblePanel = document.querySelector(".panel.active")?.id?.replace(/^panel-/, "");
  return (visiblePanel || state.activePanel) === "research";
}

function modelResearchPanelIsVisible() {
  const visiblePanel = document.querySelector(".panel.active")?.id?.replace(/^panel-/, "");
  return (visiblePanel || state.activePanel) === "model-research";
}

function setRefreshButtonState(button, phase = "idle") {
  if (!button) return;
  if (!button.dataset.defaultLabel) {
    button.dataset.defaultLabel = button.querySelector("span")?.textContent?.trim() || button.textContent?.trim() || "刷新状态";
  }
  if (!button.dataset.defaultHint) {
    button.dataset.defaultHint = button.querySelector("small")?.textContent?.trim() || "立即拉取最新状态";
  }
  const label = button.querySelector("span");
  const hint = button.querySelector("small");
  const hasStructuredContent = Boolean(label || hint);
  if (phase === "refreshing") {
    button.disabled = true;
    button.classList.add("is-refreshing");
    button.classList.remove("is-success");
    button.setAttribute("aria-busy", "true");
    if (label) label.textContent = "刷新中...";
    if (hint) hint.textContent = "正在请求最新数据";
    if (!hasStructuredContent) button.textContent = "刷新中...";
    return;
  }
  if (phase === "success") {
    button.disabled = false;
    button.classList.remove("is-refreshing");
    button.classList.add("is-success");
    button.setAttribute("aria-busy", "false");
    if (label) label.textContent = "已刷新";
    if (hint) hint.textContent = "状态已重新拉取";
    if (!hasStructuredContent) button.textContent = "已刷新";
    window.setTimeout(() => {
      if (!button.isConnected) return;
      button.classList.remove("is-success");
      if (label) label.textContent = button.dataset.defaultLabel || "刷新状态";
      if (hint) hint.textContent = button.dataset.defaultHint || "立即拉取最新状态";
      if (!hasStructuredContent) button.textContent = button.dataset.defaultLabel || "刷新状态";
    }, 1400);
    return;
  }
  button.disabled = false;
  button.classList.remove("is-refreshing", "is-success");
  button.setAttribute("aria-busy", "false");
  if (label) label.textContent = button.dataset.defaultLabel || "刷新状态";
  if (hint) hint.textContent = button.dataset.defaultHint || "立即拉取最新状态";
  if (!hasStructuredContent) button.textContent = button.dataset.defaultLabel || "刷新状态";
}

async function refreshModelResearchResults() {
  if (state.modelResultsRefreshInFlight) return;
  const button = document.getElementById("refresh-model-research");
  state.modelResultsRefreshInFlight = true;
  setRefreshButtonState(button, "refreshing");
  try {
    // Results are the operator's immediate concern.  Do not make them wait for
    // the much larger status, journal, and trace payloads used by the live view.
    const [registry, runs] = await Promise.all([
      getJsonSafe(`${MODEL_API_PREFIX}/registry`, { timeoutMs: 12000 }),
      getJsonSafe(`${MODEL_API_PREFIX}/runs?limit=100`, { timeoutMs: 12000 }),
    ]);
    const registryReady = registry && !registry._failed && !registry.error;
    const runsReady = runs && !runs._failed && !runs.error;
    if (!registryReady && !runsReady) {
      throw new Error("模型结果接口未返回可用数据");
    }
    if (registryReady) state.modelRegistry = registry;
    if (runsReady) state.modelRuns = runs;
    state.lastRefreshAt = Date.now();
    renderAll();
    setRefreshButtonState(button, "success");

    // Refresh supporting live-state data afterwards.  A slow auxiliary response
    // must never keep newly written research results off the result list.
    Promise.all([
      getJsonSafe(`${MODEL_API_PREFIX}/status`, { timeoutMs: 30000 }),
      getJsonSafe(`${MODEL_API_PREFIX}/preflight`, { timeoutMs: 12000 }),
      getJsonSafe(`${MODEL_API_PREFIX}/research/current`, { timeoutMs: 12000 }),
      getJsonSafe(`${MODEL_API_PREFIX}/orchestrator/status`, { timeoutMs: 30000 }),
    ]).then(([status, preflight, researchCurrent, orchStatus]) => {
      state.modelStatus = keepPreviousOnReadFailure(status, state.modelStatus);
      state.modelPreflight = keepPreviousOnReadFailure(preflight, state.modelPreflight);
      state.modelResearchCurrent = keepPreviousOnReadFailure(researchCurrent, state.modelResearchCurrent);
      state.modelOrchestratorStatus = keepPreviousOnReadFailure(orchStatus, state.modelOrchestratorStatus);
      if (modelResearchPanelIsVisible()) renderModelResearch();
    }).catch(() => {});
  } catch (error) {
    console.error("GUI model results refresh failed", error);
    setRefreshButtonState(button, "idle");
  } finally {
    state.modelResultsRefreshInFlight = false;
  }
}

async function waitForRefreshIdle(timeoutMs = 6000) {
  const startedAt = Date.now();
  while (state.refreshInFlight || state.liveRefreshInFlight) {
    if (Date.now() - startedAt >= timeoutMs) {
      break;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 120));
  }
}

function researchJobIsLive() {
  const digest = liveResearchDigest();
  const status = String(digest.status || serviceOutputs(state.factorConsole).status || "");
  return Boolean(digest.run_id) && !/completed|failed|stopped|cancelled|idle/i.test(status);
}

async function refreshResearchLive({ force = false, includeTracePayload = orchestratorTraceWorkspaceVisible() } = {}) {
  if (!researchPanelIsVisible()) return;
  if (state.refreshInFlight || state.liveRefreshInFlight) {
    if (force) startLiveResearchPolling();
    return;
  }
  state.liveRefreshInFlight = true;
  try {
    const needsPreflight = state.activeWorkspace === "command";
    const needsEvents = ["run", "orch-trace"].includes(state.activeWorkspace);
    const needsTraces = state.activeWorkspace === "orch-trace";
    const [factorConsoleResp, factorResearchPreflight, factorResearchControl, orchestratorEvents, orchestratorTraces] = await Promise.all([
      getFactorConsoleSafe(),
      needsPreflight ? getJsonSafe("/factor/research/preflight") : Promise.resolve(state.factorResearchPreflight),
      getJsonSafe("/factor/research/control"),
      needsEvents ? getJsonSafe(orchestratorEventsUrl()) : Promise.resolve(state.orchestratorEvents),
      needsTraces ? getJsonSafe(orchestratorTracesUrl({ includePayload: includeTracePayload })) : Promise.resolve(state.orchestratorTraces),
    ]);
    state.factorResearchPreflight = factorResearchPreflight;
    state.factorResearchControl = factorResearchControl;
    state.orchestratorEvents = orchestratorEvents;
    state.orchestratorTraces = orchestratorTraces;
    if (factorConsoleResp && !factorConsoleResp._failed && !factorConsoleResp.error) {
      state.backendMode = "console";
      state.factorConsole = normalizeFactorConsole(factorConsoleResp);
      const runView = state.factorConsole?.outputs?.run_view;
      state.factorRunView = runView && Object.keys(runView).length ? { ok: true, outputs: runView } : null;
      state.lastRefreshAt = Date.now();
      renderApiChip();
      renderMiniMetrics();
      renderResearchSummary();
      renderResearchPanels();
      if (isCurrentOrchestratorMode()) {
        getJsonSafe("/platform/runtime-status", { timeoutMs: 15000 }).then((runtimeStatus) => {
          if (runtimeStatus && !runtimeStatus._failed) {
            state.platformRuntime = runtimeStatus;
          } else if (!state.platformRuntime) {
            state.platformRuntime = runtimeStatus;
          }
          if (researchPanelIsVisible()) renderCompactSystemStrip(researchProjection());
        }).catch(() => {
          if (!state.platformRuntime) {
            state.platformRuntime = { _failed: true, error: "platform_runtime_status_unavailable" };
          }
          if (researchPanelIsVisible()) renderCompactSystemStrip(researchProjection());
        });
      }
    }
  } finally {
    state.liveRefreshInFlight = false;
    startLiveResearchPolling();
  }
}

document.getElementById("research-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const source = new FormData(event.target);
  const target = document.getElementById("orchestrator-command-form");
  ["direction", "universe", "target_adopted", "start_date", "end_date", "benchmark", "holding_period", "top_frac", "cost_rate", "n_candidates", "n_rounds"].forEach((name) => {
    const field = formField(target, name);
    const value = source.get(name);
    if (field && value !== null) field.value = String(value);
  });
  const targetSubmitWq = formField(target, "submit_wq");
  const targetNeutralizeCap = formField(target, "neutralize_cap");
  if (targetSubmitWq) targetSubmitWq.checked = source.get("submit_wq") === "on";
  if (targetNeutralizeCap) targetNeutralizeCap.checked = source.get("neutralize_cap") === "on";
  setWorkspace("command");
  setCommandMessage("参数已同步到唯一的研究指令台；请核对预检状态后启动。", "ok");
  await refreshCommandPreflight();
});

document.getElementById("orchestrator-command-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  await submitCommandOrchestrator();
});

document.querySelector(".command-llm-model-switch")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-command-llm-model]");
  if (!button) return;
  setCommandLlmModel(button.dataset.commandLlmModel);
});

document.getElementById("evaluation-mode-bar")?.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-evaluation-mode]");
  if (!button) return;
  await switchEvaluationMode(button.dataset.evaluationMode);
});

document.getElementById("command-save-defaults")?.addEventListener("click", async () => {
  await saveCommandDefaults();
});

document.getElementById("command-pause-orchestrator")?.addEventListener("click", async () => {
  await submitCommandControl("pause");
});

document.getElementById("command-resume-orchestrator")?.addEventListener("click", async () => {
  await submitCommandControl("resume");
});

document.getElementById("command-stop-orchestrator")?.addEventListener("click", async () => {
  await submitCommandControl("stop");
});

document.getElementById("command-refresh-preflight")?.addEventListener("click", async () => {
  setCommandMessage("正在刷新预检...", "subtle");
  const preflight = await refreshCommandPreflight();
  if (preflight.can_start) {
    setCommandMessage("预检通过，可以启动 Orchestrator。", "ok");
  } else if (preflight.active_orchestrator_run?.run_id) {
    setCommandMessage(`已有运行中 Orchestrator：${preflight.active_orchestrator_run.run_id}`, "warn");
  } else {
    setCommandMessage(`预检阻断：${(preflight.blocking_errors || []).join(", ") || preflight.doctor_hint || "unknown"}`, "danger");
  }
});

document.getElementById("command-open-run")?.addEventListener("click", () => {
  setWorkspace("run");
});

document.getElementById("command-open-guidance")?.addEventListener("click", () => {
  setWorkspace("command");
  window.requestAnimationFrame(() => {
    document.getElementById("command-guidance")?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
});

document.getElementById("guidance-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("guidance-input");
  const message = input.value.trim();
  if (!message) return;
  const factorConsole = serviceOutputs(state.factorConsole);
  const digest = liveResearchDigest();
  const control = commandControlOutputs();
  const runId = control.run_id || digest.run_id || state.lastRunId || "";
  if (!runId || !(control.allowed_actions || []).includes("guidance")) {
    document.getElementById("guidance-note").textContent = "当前状态不接受研究干预；请先启动或暂停一个 ORCH run。";
    return;
  }
  const submitButton = event.currentTarget.querySelector("button[type='submit']");
  const note = document.getElementById("guidance-note");
  submitButton.disabled = true;
  note.textContent = "正在提交一次性干预…";
  try {
    const result = await postJson("/factor/research/guidance", {
      run_id: runId,
      message,
      author: "web_gui",
    });
    if (!result?.ok) {
      note.textContent = text(result?.err, "干预提交失败，请刷新状态后重试。");
      return;
    }
    input.value = "";
    await refreshState();
  } finally {
    submitButton.disabled = false;
  }
});

document.getElementById("load-auto-template")?.addEventListener("click", () => {
  const form = document.getElementById("research-form");
  const defaults = serviceOutputs(state.factorStatus).runtime_defaults || {};
  form.direction.value = "auto";
  form.target_adopted.value = "20";
  form.n_candidates.value = "10";
  form.n_rounds.value = "4";
  form.seed_count.value = "3";
  form.seed_max_concurrent.value = "3";
  form.top_frac.value = "0.2";
  form.cost_rate.value = "0.003";
  form.rebalance_anchor.value = "";
  form.universe_date.value = "";
  form.max_direction_attempts.value = "4";
  form.max_stagnation_rounds.value = "3";
  form.auto_sessions.value = "1";
  form.seed_batch_rounds.value = "0";
  form.seed_batch_max_candidates.value = "0";
  applyManagedDefault(form.universe, defaults.universe);
  applyManagedDefault(form.start_date, defaults.selection_start_date);
  applyManagedDefault(form.end_date, defaults.selection_end_date);
  applyManagedDefault(form.benchmark, defaults.benchmark);
  applyManagedDefault(form.holding_period, defaults.holding_period);
  if (form.orchestration_mode) form.orchestration_mode.value = "orchestrator";
});

async function handleConsoleRefresh() {
  const button = document.getElementById("refresh-console");
  if (!button || button.disabled) return;
  setRefreshButtonState(button, "refreshing");
  try {
    await waitForRefreshIdle();
    await refreshState({ reason: "manual" });
    setRefreshButtonState(button, "success");
  } catch (error) {
    setRefreshButtonState(button, "idle");
    throw error;
  }
}

document.getElementById("refresh-console")?.addEventListener("click", () => {
  handleConsoleRefresh().catch((error) => {
    console.error("GUI manual refresh failed", error);
  });
});
document.getElementById("refresh-overview").addEventListener("click", refreshState);
document.getElementById("refresh-library").addEventListener("click", refreshState);
document.getElementById("refresh-model-research")?.addEventListener("click", refreshModelResearchResults);
document.getElementById("refresh-model-library")?.addEventListener("click", refreshState);
document.getElementById("refresh-trading")?.addEventListener("click", refreshState);
document.getElementById("refresh-data-foundation")?.addEventListener("click", refreshState);
document.getElementById("audit-duplicates")?.addEventListener("click", refreshState);
document.getElementById("factor-audit-refresh")?.addEventListener("click", refreshState);
function scheduleFactorAuditRunPoll() {
  if (state.factorAuditRunPollTimer) {
    clearTimeout(state.factorAuditRunPollTimer);
  }
  state.factorAuditRunPollTimer = setTimeout(async () => {
    const status = await getJsonSafe("/factor/library/audit/run-status");
    if (status?._failed) {
      scheduleFactorAuditRunPoll();
      return;
    }
    state.factorAuditRunStatus = status;
    const outputs = serviceOutputs(status);
    if (["queued", "running"].includes(outputs.status)) {
      renderFactorLibrary(serviceOutputs(state.factorConsole));
      if (modelResearchPanelIsVisible()) renderModelFeatureSetChooser();
      scheduleFactorAuditRunPoll();
      return;
    }
    await refreshState({ reason: "factor-audit-run-finished" });
  }, 3000);
}

function setFactorAuditActionState(running) {
  ["factor-audit-quality", "factor-audit-information", "factor-audit-all", "model-refresh-factor-audit"].forEach((id) => {
    const button = document.getElementById(id);
    if (button) button.disabled = Boolean(running);
  });
}

async function runFactorAuditFromGui(scope) {
  const current = serviceOutputs(state.factorAuditRunStatus);
  if (["queued", "running"].includes(current.status)) return;
  setFactorAuditActionState(true);
  try {
    const result = await postJson("/factor/library/audit/run", {
      scope,
      save_report: true,
      include_feature_sets: scope !== "quality",
      async: true,
    });
    if (!result?.ok) {
      throw new Error(result?.err || result?.error || "factor_library_audit_start_failed");
    }
    state.factorAuditRunStatus = result;
    renderFactorLibrary(serviceOutputs(state.factorConsole));
    if (modelResearchPanelIsVisible()) renderModelFeatureSetChooser();
    scheduleFactorAuditRunPoll();
  } catch (error) {
    state.factorAuditRunStatus = {
      ok: true,
      outputs: {
        status: "failed",
        scope,
        last_error: String(error),
      },
    };
    setFactorAuditActionState(false);
    renderFactorLibrary(serviceOutputs(state.factorConsole));
    if (modelResearchPanelIsVisible()) renderModelFeatureSetChooser();
  }
}

document.getElementById("factor-audit-quality")?.addEventListener("click", () => runFactorAuditFromGui("quality"));
document.getElementById("factor-audit-information")?.addEventListener("click", () => runFactorAuditFromGui("information"));
document.getElementById("factor-audit-all")?.addEventListener("click", () => runFactorAuditFromGui("all"));
document.getElementById("model-refresh-factor-audit")?.addEventListener("click", async () => {
  setModelActionMessage("已提交因子库信息簇审计；完成后会自动更新 Feature Set 建议。", "subtle");
  await runFactorAuditFromGui("information");
  renderModelFactorAuditBridge();
});
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-maintenance-cleanup]");
  if (!button) return;
  const execute = button.dataset.maintenanceCleanup === "execute";
  if (execute && !window.confirm("执行 safe 清理会删除可再生缓存、旧日志和旧报告；核心数据资产会被保护。确认继续？")) {
    return;
  }
  button.disabled = true;
  try {
    state.latestMaintenanceAction = await postJson("/maintenance/cleanup", {
      profile: "safe",
      execute,
    });
    await refreshState();
  } finally {
    button.disabled = false;
  }
});

window.addEventListener("resize", syncPaperTargetListHeight);
document.getElementById("retire-duplicates-dry-run")?.addEventListener("click", async () => {
  const result = await postJson("/factor/registry/retire-duplicates", {
    dry_run: true,
    reason: "duplicate_active_expression_gui_dry_run",
  });
  state.duplicateAudit = {
    ok: result.ok,
    outputs: {
      duplicate_groups: result.outputs?.duplicate_groups || 0,
      duplicate_factor_count: result.outputs?.retire_count || 0,
      groups: result.outputs?.groups || [],
      dry_run: true,
    },
  };
  renderFactorLibrary(serviceOutputs(state.factorConsole));
});
document.getElementById("retire-duplicates-apply")?.addEventListener("click", async () => {
  const yes = window.confirm("确认退休 active 因子库中的重复表达式副本？该操作只改状态，不删除历史记录。");
  if (!yes) return;
  await postJson("/factor/registry/retire-duplicates", {
    dry_run: false,
    reason: "duplicate_active_expression_gui_apply",
  });
  await refreshState();
});
document.getElementById("model-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const model = serviceOutputs(state.modelStatus);
  const latestSession = model.live_session || {};
  const guidance = String(form.get("human_guidance") || "").trim();
  if (!guidance) {
    window.alert("请先填写人工干预意见。");
    return;
  }
  await postJson(`${MODEL_API_PREFIX}/tools/research-step`, {
    stage: "human_guidance",
    summary: guidance,
    decision: "human_guidance_recorded",
    next: "context_review",
    feature_set_id: latestSession.feature_set_id || null,
    model_run_id: latestSession.model_run_id || "",
    refs: [latestSession.session_id, latestSession.model_run_id].filter(Boolean),
    extra: {
      model_family: "lgbm",
      model_policy: "qlib_lgbm_canonical",
      priority: "normal",
    },
  });
  event.target.reset();
  await refreshState();
});

document.getElementById("start-model-orch")?.addEventListener("click", async () => {
  const formEl = document.getElementById("model-command-form");
  const form = new FormData(formEl);
  const evaluationMode = String(form.get("evaluation_mode") || modelCommandMode());
  const featureSetId = String(form.get("feature_set_id") || "").trim();
  const preflight = modelCommandPreflightOutputs();
  if (evaluationMode === "research" && state.modelFeatureSource === "custom" && !featureSetId) {
    setModelActionMessage("请先把自定义因子组合冻结为不可变 Feature Set，再执行训练预检。", "danger");
    return;
  }
  if (evaluationMode === "research" && (preflight.passed !== true || (featureSetId && preflight.feature_set_id !== featureSetId))) {
    setModelActionMessage(`预检阻断：${text(preflight.blocker?.human_message || preflight.stale_reason || (preflight.errors || []).join(", "), "请先刷新预检")}`, "danger");
    return;
  }
  let payload;
  let confirmationText;
  if (evaluationMode === "production") {
    const sourceRoundGroupId = String(form.get("source_round_group_id") || "").trim();
    if (!sourceRoundGroupId) {
      setModelActionMessage("生产模式必须选择已通过 Seed17/83 研究确认的来源轮次。", "danger");
      return;
    }
    payload = {
      evaluation_mode: "production",
      source_round_group_id: sourceRoundGroupId,
      write_registry: form.get("production_write_registry") === "on",
      run_id: `model_production_gui_${Date.now()}`,
    };
    confirmationText = `确认启动 Production Rolling？\\n来源轮次: ${payload.source_round_group_id}\\n参数策略: 固定继承研究确认轮次\\n验证: 四折 expanding Rolling + Seed42/17/83\\nRegistry: ${payload.write_registry ? "on" : "off"}`;
  } else {
    const validationError = validateModelCommandBaselineParams();
    if (validationError) {
      setModelActionMessage(validationError, "danger");
      return;
    }
    const baselineOverrides = modelCommandBaselineOverrides();
    payload = {
      evaluation_mode: "research",
      feature_set_id: featureSetId || null,
      max_stage: form.get("max_stage") || "round_synthesis",
      n_rounds: Number(form.get("model_orch_rounds") || 0),
      execute_qlib: form.get("execute_qlib") === "on",
      write_registry: form.get("write_registry") === "on",
      baseline_model_params: baselineOverrides,
      run_id: `model_gui_${Date.now()}`,
    };
    const featureSetLine = `Feature set: ${payload.feature_set_id || "当前快照"}`;
    const baselineLine = Object.keys(baselineOverrides).length
      ? `自定义基线: ${Object.entries(baselineOverrides).map(([key, value]) => `${key}=${value}`).join(", ")}`
      : "基线参数: FXAlpha 当前默认";
    confirmationText = `确认启动 Model ORCH？\\n模式: Research\\n${featureSetLine}\\n${baselineLine}\\n调参轮次: ${payload.n_rounds}\\n实际执行: Round 0 基准测试 + Round 1–${payload.n_rounds}（共 ${payload.n_rounds + 1} 轮）\\n运行至: ${payload.max_stage}\\nSeed: 每轮 Seed42 筛选；会话最优参数再做 Seed17/83 稳定性复核\\nQlib: ${payload.execute_qlib ? "on" : "off"}\\nRegistry: ${payload.write_registry ? "on" : "off"}`;
  }
  const confirmed = window.confirm(confirmationText);
  if (!confirmed) return;
  setModelActionMessage("正在启动 Model ORCH...", "subtle");
  const result = await postJson(`${MODEL_API_PREFIX}/orchestrator/start`, payload);
  const outputs = serviceOutputs(result);
  if (result?.ok === false || result?._failed || result?.error) {
    setModelActionMessage(`启动失败：${text(result.err || result.error || "unknown")}`, "danger");
  } else if (outputs.status === "already_running") {
    setModelActionMessage(`已有 ORCH 正在运行：${text(outputs.active_job?.job_id, "unknown")}`, "warn");
  } else {
    setModelActionMessage(`已提交后台训练：${text(outputs.job_id || outputs.job?.job_id, "等待 job_id")}`, "ok");
  }
  await refreshState({ reason: "model_orch_start" });
});

document.getElementById("stop-model-orch")?.addEventListener("click", async () => {
  const model = serviceOutputs(state.modelStatus);
  const activeJob = modelCommandOrchestratorOutputs().active_job || (model.orchestrator || {}).active_job || {};
  const result = await postJson(`${MODEL_API_PREFIX}/jobs/stop`, { job_id: activeJob.job_id || null });
  const outputs = serviceOutputs(result);
  setModelActionMessage(outputs.status === "idle" ? "当前没有训练任务。" : "已请求停止；当前 Seed 或轮次完成后安全退出。", outputs.status === "idle" ? "subtle" : "warn");
  await refreshState({ reason: "model_orch_stop" });
});

document.getElementById("resume-model-orch")?.addEventListener("click", async () => {
  const model = serviceOutputs(state.modelStatus);
  const latestJob = modelCommandOrchestratorOutputs().latest_job || model.latest_job || (model.orchestrator || {}).latest_job || {};
  if (!latestJob.job_id) {
    setModelActionMessage("没有可继续的训练任务。", "warn");
    return;
  }
  const result = await postJson(`${MODEL_API_PREFIX}/jobs/resume`, { job_id: latestJob.job_id });
  const outputs = serviceOutputs(result);
  const failed = result?.ok === false || result?._failed || result?.error;
  setModelActionMessage(failed ? `继续失败：${text(result.err || result.error, "unknown")}` : `已继续后台训练：${text(outputs.job_id, latestJob.job_id)}`, failed ? "danger" : "ok");
  await refreshState({ reason: "model_orch_resume" });
});

document.getElementById("refresh-model-command")?.addEventListener("click", async () => {
  setModelActionMessage(modelCommandMode() === "production" ? "正在刷新生产来源与运行状态..." : "正在刷新模型研究预检...", "subtle");
  const preflight = await refreshModelCommandPreflight();
  if (modelCommandMode() === "production") {
    setModelActionMessage(modelProductionSourceRounds().length ? `已刷新：${modelProductionSourceRounds().length} 个研究确认轮次可用于 Production Rolling。` : "没有找到已通过研究确认的生产来源轮次。", modelProductionSourceRounds().length ? "ok" : "warn");
    return;
  }
  setModelActionMessage(
    preflight.passed === true ? "预检通过，可以启动模型研究。" : `预检阻断：${text(preflight.blocker?.human_message || preflight.stale_reason || (preflight.errors || []).join(", "), "unknown")}`,
    preflight.passed === true ? "ok" : "danger",
  );
});

document.querySelector('#model-command-form [name="feature_set_id"]')?.addEventListener("change", async () => {
  await refreshModelCommandPreflight();
});

document.querySelectorAll("[data-model-feature-source]").forEach((button) => {
  button.addEventListener("click", async () => {
    state.modelFeatureSource = button.dataset.modelFeatureSource === "custom" ? "custom" : "catalog";
    try {
      window.localStorage?.setItem("fxalpha.modelFeatureSource", state.modelFeatureSource);
    } catch (error) {
      // Ignore storage failures in restricted browser contexts.
    }
    state.modelPreflight = null;
    syncModelFeatureSourceUI();
    renderModelCommandConsole();
    if (modelResearchFeatureSetId()) await refreshModelCommandPreflight();
  });
});

document.getElementById("model-feature-set-select")?.addEventListener("change", async (event) => {
  state.modelFeatureSource = "catalog";
  state.modelSelectedFeatureSetId = String(event.target.value || "");
  state.modelPreflight = null;
  renderModelCommandConsole();
  await refreshModelCommandPreflight();
});

document.getElementById("model-feature-set-presets")?.addEventListener("click", async (event) => {
  const button = event.target.closest?.("[data-model-feature-set-id]");
  if (!button) return;
  state.modelFeatureSource = "catalog";
  state.modelSelectedFeatureSetId = String(button.dataset.modelFeatureSetId || "");
  state.modelPreflight = null;
  renderModelCommandConsole();
  await refreshModelCommandPreflight();
});

document.getElementById("model-factor-search")?.addEventListener("input", (event) => {
  state.modelFactorQuery = String(event.target.value || "");
  renderModelFactorPicker();
});

document.getElementById("model-factor-category")?.addEventListener("change", (event) => {
  state.modelFactorCategory = String(event.target.value || "all");
  renderModelFactorPicker();
});

document.getElementById("model-factor-picker")?.addEventListener("change", (event) => {
  const input = event.target.closest?.("[data-model-factor-id]");
  if (!input) return;
  const factorId = String(input.dataset.modelFactorId || "");
  if (input.checked) state.modelSelectedFactorIds.add(factorId);
  else state.modelSelectedFactorIds.delete(factorId);
  state.modelCustomFeatureSetId = "";
  state.modelPreflight = null;
  renderModelFactorPicker();
  syncModelFeatureSourceUI();
  renderModelLaunchReview();
});

document.getElementById("model-factor-recommendations")?.addEventListener("click", (event) => {
  const button = event.target.closest?.("[data-model-audit-recommendation]");
  if (!button) return;
  const recommendation = modelAuditFeatureRecommendations().find((item) => item.name === button.dataset.modelAuditRecommendation);
  if (!recommendation) return;
  const activeIds = new Set(modelFactorLibraryItems().map((item) => String(item.factor_id)));
  state.modelSelectedFactorIds = new Set((recommendation.factor_ids || []).map(String).filter((factorId) => activeIds.has(factorId)));
  state.modelCustomFeatureSetId = "";
  state.modelPreflight = null;
  renderModelFactorPicker();
  syncModelFeatureSourceUI();
  renderModelLaunchReview();
  setModelActionMessage(`已选用审计组合 ${recommendation.name}：${state.modelSelectedFactorIds.size} 个当前 Active 因子；请创建不可变 Feature Set。`, "subtle");
});

document.getElementById("model-factor-select-visible")?.addEventListener("click", () => {
  filteredModelFactorItems().forEach((item) => state.modelSelectedFactorIds.add(String(item.factor_id)));
  state.modelCustomFeatureSetId = "";
  state.modelPreflight = null;
  renderModelFactorPicker();
  syncModelFeatureSourceUI();
  renderModelLaunchReview();
});

document.getElementById("model-factor-clear")?.addEventListener("click", () => {
  state.modelSelectedFactorIds.clear();
  state.modelCustomFeatureSetId = "";
  state.modelPreflight = null;
  renderModelFactorPicker();
  syncModelFeatureSourceUI();
  renderModelLaunchReview();
});

document.getElementById("freeze-model-feature-set")?.addEventListener("click", async (event) => {
  if (!state.modelSelectedFactorIds.size) return;
  const source = modelActiveFeatureSetSource();
  if (!source?.feature_set_id) {
    setModelActionMessage("没有找到覆盖当前 Active 因子的全量源快照，无法安全冻结自定义组合。", "danger");
    return;
  }
  const button = event.currentTarget;
  const now = new Date();
  const stamp = [now.getFullYear(), String(now.getMonth() + 1).padStart(2, "0"), String(now.getDate()).padStart(2, "0")].join("")
    + "_" + [String(now.getHours()).padStart(2, "0"), String(now.getMinutes()).padStart(2, "0"), String(now.getSeconds()).padStart(2, "0")].join("");
  const featureSetId = `fs-model-custom-${stamp}-${state.modelSelectedFactorIds.size}`;
  button.disabled = true;
  setModelActionMessage(`正在从 ${displayModelIdentifier(source.feature_set_id)} 冻结 ${state.modelSelectedFactorIds.size} 个因子...`, "subtle");
  const result = await postJson(`${MODEL_API_PREFIX}/tools/feature-snapshot`, {
    feature_set_id: featureSetId,
    factor_ids: [...state.modelSelectedFactorIds],
    source_feature_set_id: source.feature_set_id,
    source_type: "gui_manual_subset",
    provenance_note: "Model research command console custom factor selection",
    feature_missing_strategy: "qlib_processor_only",
    dry_run: false,
  });
  const outputs = serviceOutputs(result);
  if (result?.ok === false || result?._failed || result?.error || outputs.validation?.passed === false) {
    setModelActionMessage(`Feature Set 创建失败：${text(result.err || result.error || outputs.validation?.errors?.join(", "), "unknown")}`, "danger");
    button.disabled = false;
    return;
  }
  state.modelCustomFeatureSetId = String(outputs.feature_set_id || featureSetId);
  state.modelFeatureSets = await getJsonSafe(`${MODEL_API_PREFIX}/feature-sets?limit=100&compact=true`, { timeoutMs: 30000 });
  state.modelPreflight = null;
  renderModelCommandConsole();
  const preflight = await refreshModelCommandPreflight();
  setModelActionMessage(preflight.passed === true
    ? `已创建 ${displayModelIdentifier(state.modelCustomFeatureSetId)}，${state.modelSelectedFactorIds.size} 个因子，预检通过。`
    : `Feature Set 已创建，但预检存在阻断：${text(preflight.errors?.join(", ") || preflight.blocker?.human_message, "unknown")}`,
  preflight.passed === true ? "ok" : "danger");
});

document.querySelectorAll("[data-model-protocol-preset]").forEach((button) => {
  button.addEventListener("click", () => {
    applyModelProtocolPreset(button.dataset.modelProtocolPreset);
    setModelActionMessage(`已应用${button.querySelector("strong")?.textContent || "研究"}方案；可继续微调轮次和执行阶段。`, "subtle");
  });
});

document.querySelectorAll('#model-command-form [name="model_orch_rounds"], #model-command-form [name="max_stage"], #model-command-form [name="execute_qlib"], #model-command-form [name="write_registry"]').forEach((field) => {
  field.addEventListener("change", () => {
    document.querySelectorAll("[data-model-protocol-preset]").forEach((button) => button.classList.remove("is-active"));
    renderModelLaunchReview();
  });
});

document.querySelectorAll("[data-model-evaluation-mode]").forEach((button) => {
  button.addEventListener("click", () => {
    setModelCommandMode(button.dataset.modelEvaluationMode);
    setModelActionMessage(button.dataset.modelEvaluationMode === "production" ? "已切换到生产模式；请选择研究确认来源轮次。" : "已切换到研究模式；可编辑本次 Round 0 基线参数。", "subtle");
  });
});

document.querySelectorAll("[data-model-param-preset]").forEach((button) => {
  button.addEventListener("click", () => {
    applyModelCommandParamPreset(button.dataset.modelParamPreset);
    const label = button.dataset.modelParamPreset === "qlib" ? "Qlib Alpha158" : "FXAlpha 当前默认";
    setModelActionMessage(`已载入 ${label} 参数；启动前仍会执行有界与参数关系校验。`, "subtle");
  });
});

document.getElementById("model-command-reset-defaults")?.addEventListener("click", () => {
  applyModelCommandParamPreset("fxalpha");
  setModelActionMessage("已恢复 FXAlpha 当前默认基线参数。", "subtle");
});

document.querySelectorAll("#model-command-form [data-model-param]").forEach((field) => {
  field.addEventListener("input", () => {
    document.querySelectorAll("[data-model-param-preset]").forEach((button) => button.classList.remove("is-active"));
    renderModelLaunchReview();
  });
});

document.getElementById("model-production-source-round")?.addEventListener("change", renderModelCommandConsole);
document.getElementById("start-model-production-orch")?.addEventListener("click", () => document.getElementById("start-model-orch")?.click());
document.getElementById("refresh-model-production-command")?.addEventListener("click", () => document.getElementById("refresh-model-command")?.click());

document.getElementById("refresh-feature-set")?.addEventListener("click", async () => {
  const defaults = modelRuntimeDefaults();
  const payload = {
    dry_run: false,
  };
  if (defaults.start_date) payload.start_date = defaults.start_date;
  if (defaults.end_date) payload.end_date = defaults.end_date;
  if (defaults.label_forward_period) payload.label_forward_period = Number(defaults.label_forward_period);
  await postJson(`${MODEL_API_PREFIX}/tools/feature-snapshot`, payload);
  await refreshState();
});

function paperReplayFormPayload() {
  const formEl = document.getElementById("paper-replay-form");
  const form = new FormData(formEl);
  return {
    account_id: form.get("account_id") || state.selectedPaperAccountId || "",
    from_date: form.get("from_date") || null,
    to_date: form.get("to_date") || null,
  };
}

async function runDailyOpsFromGui() {
  state.latestTradingResult = await getJsonSafe("/paper/fleet/preflight");
  await refreshState();
}

document.getElementById("dry-run-daily-ops")?.addEventListener("click", async () => {
  await runDailyOpsFromGui();
});

document.getElementById("copy-daily-ops-command")?.addEventListener("click", async () => {
  const command = "PYTHONPATH=. .venv/bin/python cli.py paper-fleet-run";
  await navigator.clipboard?.writeText(command);
  state.latestTradingResult = { ok: true, outputs: { status: "copied", command } };
  renderTrading();
});

document.getElementById("plan-paper-replay")?.addEventListener("click", async () => {
  const payload = paperReplayFormPayload();
  if (!payload.account_id) {
    window.alert("请先选择需要检查的模拟账户。");
    return;
  }
  const params = new URLSearchParams({ account_id: payload.account_id });
  if (payload.from_date) params.set("from_date", payload.from_date);
  if (payload.to_date) params.set("to_date", payload.to_date);
  state.paperReplayBusy = true;
  renderTrading();
  try {
    state.latestTradingResult = await getJsonSafe(`/paper/replay/plan?${params.toString()}`);
  } finally {
    state.paperReplayBusy = false;
    renderTrading();
  }
});

document.getElementById("run-paper-replay")?.addEventListener("click", async () => {
  const payload = paperReplayFormPayload();
  if (!payload.account_id) {
    window.alert("请先选择需要补跑的模拟账户。");
    return;
  }
  const plan = serviceOutputs(state.latestTradingResult || {}).plan || {};
  const dates = plan.account_id === payload.account_id ? (plan.trade_dates || []) : [];
  const blockers = plan.account_id === payload.account_id ? (plan.blockers || []) : [];
  if (!dates.length || blockers.length) {
    window.alert(blockers.length ? "当前补跑计划存在阻断，不能执行。" : "请先检查缺口并生成精确补跑计划。");
    return;
  }
  const yes = window.confirm(`确认按历史 As-Of 口径补跑账户 ${payload.account_id} 的 ${dates.length} 个交易日？已完成日期不会覆盖。`);
  if (!yes) return;
  state.paperReplayBusy = true;
  renderTrading();
  try {
    state.latestTradingResult = await postJson("/paper/replay", {
      account_id: payload.account_id,
      from_date: payload.from_date,
      to_date: payload.to_date,
      confirm: true,
      confirm_long_replay: true,
    });
    await refreshState();
  } finally {
    state.paperReplayBusy = false;
    renderTrading();
  }
});

document.getElementById("reset-paper-replay-range")?.addEventListener("click", () => {
  const form = document.getElementById("paper-replay-form");
  if (!form) return;
  form.querySelector('[name="from_date"]').value = "";
  form.querySelector('[name="to_date"]').value = "";
});

document.getElementById("paper-account-create-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submitButton = event.target.querySelector('[type="submit"]');
  const form = new FormData(event.target);
  const payload = {
    account_id: form.get("account_id"),
    model_run_id: form.get("model_run_id"),
    effective_from: form.get("effective_from"),
    account_mode: "fixed_model",
    initial_capital: 1000000,
    topk: 20,
    n_drop: 2,
    hold_thresh: 5,
    deal_price: "open",
    strategy_contract_version: form.get("strategy_contract_version") || "confidence_cash_top20_drop2_hold5_open_v2",
    confirm: true,
  };
  if (!payload.model_run_id) {
    state.latestTradingResult = { ok: false, error: "production_model_not_ready", _paperUiAction: "account_create" };
    renderTrading();
    return;
  }
  const yes = window.confirm(`确认创建生产模拟账户 ${payload.account_id} 并永久绑定指定模型？如需运行另一个模型，必须新建账户。`);
  if (!yes) return;
  if (submitButton) submitButton.disabled = true;
  try {
    const result = await postJson("/paper/accounts", payload);
    state.latestTradingResult = { ...result, _paperUiAction: "account_create" };
    await refreshState({ reason: "paper_account_create" });
  } finally {
    if (submitButton) submitButton.disabled = false;
    renderTrading();
  }
});

document.getElementById("risk-policy-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submitButton = event.target.querySelector('[type="submit"]');
  const form = new FormData(event.target);
  const payload = {
    enabled: form.get("enabled") === "true",
    mode: form.get("mode") || "enforced",
    market: {
      volatility_threshold: Number(form.get("volatility_threshold")) / 100,
      stress_cap: Number(form.get("stress_cap")) / 100,
      enter_days: Number(form.get("enter_days")),
      exit_days: Number(form.get("exit_days")),
    },
    account: {
      drawdown_threshold: Number(form.get("drawdown_threshold")) / 100,
      brake_cap: Number(form.get("brake_cap")) / 100,
    },
    confirm: true,
  };
  const yes = window.confirm("确认更新生产模拟交易风控参数？新参数只作用于下一份新生成的推荐，已冻结计划保持不变。");
  if (!yes) return;
  if (submitButton) submitButton.disabled = true;
  try {
    state.latestTradingResult = await postJson("/trade/risk-policy", payload);
    await refreshState({ reason: "risk_policy_update" });
    setPaperTradingTab("risk");
  } finally {
    if (submitButton) submitButton.disabled = false;
    renderTrading();
  }
});

document.addEventListener("click", async (event) => {
  const paperTargetFilter = event.target.closest("[data-paper-target-filter]");
  if (paperTargetFilter) {
    state.paperTargetFilter = paperTargetFilter.dataset.paperTargetFilter || "all";
    try { window.localStorage?.setItem("fxalpha.paperTargetFilter", state.paperTargetFilter); } catch (error) { /* ignore */ }
    renderTrading();
    return;
  }
  const paperConsoleTab = event.target.closest("[data-paper-console-tab]");
  if (paperConsoleTab) {
    setPaperConsoleTab(paperConsoleTab.dataset.paperConsoleTab);
    await refreshTradingWorkspace("paper_console_tab");
    return;
  }
  const paperTab = event.target.closest("[data-paper-trading-tab]");
  if (paperTab) {
    setPaperTradingTab(paperTab.dataset.paperTradingTab);
    if (paperTab.dataset.paperConsoleTarget) setPaperConsoleTab(paperTab.dataset.paperConsoleTarget);
    await refreshTradingWorkspace("paper_trading_tab");
    return;
  }
  const ledgerDateButton = event.target.closest("[data-paper-ledger-date]");
  if (ledgerDateButton) {
    state.paperLedgerQueryDate = ledgerDateButton.dataset.paperLedgerDate || "";
    renderTrading();
    return;
  }
  const latestLedgerButton = event.target.closest("[data-paper-ledger-latest]");
  if (latestLedgerButton) {
    state.paperLedgerQueryDate = latestLedgerButton.dataset.paperLedgerLatest || "";
    renderTrading();
    return;
  }
  const accountStatusButton = event.target.closest("[data-paper-account-status]");
  if (accountStatusButton) {
    const accountId = accountStatusButton.dataset.paperAccountStatusId || "";
    const nextStatus = accountStatusButton.dataset.paperAccountStatus || "";
    const verb = nextStatus === "paused" ? "暂停" : "恢复";
    if (!accountId || !["active", "paused"].includes(nextStatus)) return;
    const detail = nextStatus === "paused"
      ? "暂停后，该账户不会参与后续自动日切；已有持仓、账本和计划不会被删除。"
      : "恢复前会检查账户、模型绑定和账本完整性；检查不通过时不会恢复。";
    if (!window.confirm(`确认${verb}账户 ${accountId}？\n\n${detail}`)) return;
    accountStatusButton.disabled = true;
    try {
      const result = await postJson("/paper/accounts/status", { account_id: accountId, status: nextStatus, confirm: true });
      state.latestTradingResult = { ...result, _paperUiAction: "account_status" };
      await refreshState({ reason: "paper_account_status" });
      setPaperTradingTab("console");
      setPaperConsoleTab("accounts");
    } finally {
      accountStatusButton.disabled = false;
      renderTrading();
    }
    return;
  }
  const accountButton = event.target.closest("[data-paper-account-id]");
  if (accountButton) {
    state.selectedPaperAccountId = accountButton.dataset.paperAccountId || "";
    state.paperLedgerQueryDate = "";
    document.getElementById("paper-replay-form")?.reset();
    try { window.localStorage?.setItem("fxalpha.paperAccountId", state.selectedPaperAccountId); } catch (error) { /* ignore */ }
    renderTrading();
    await refreshPaperRiskPolicy(state.selectedPaperAccountId);
    renderTrading();
    window.scrollTo({ top: document.getElementById("panel-trading")?.offsetTop || 0, behavior: "smooth" });
    return;
  }
  const openOps = event.target.closest("[data-open-paper-ops]");
  if (openOps) {
    setPaperTradingTab("console");
    setPaperConsoleTab(openOps.dataset.openPaperOps || "settings");
    await refreshTradingWorkspace("paper_console_open");
    window.requestAnimationFrame(() => document.querySelector(`[data-paper-console-pane="${state.paperConsoleTab}"]`)?.scrollIntoView({ behavior: "smooth", block: "start" }));
    return;
  }
  const copyButton = event.target.closest("[data-copy-text]");
  if (!copyButton) return;
  await navigator.clipboard?.writeText(copyButton.dataset.copyText || "");
  copyButton.textContent = "已复制";
  setTimeout(() => { copyButton.textContent = "复制"; }, 1200);
});

document.addEventListener("submit", (event) => {
  if (event.target.id !== "paper-ledger-query-form") return;
  event.preventDefault();
  const form = new FormData(event.target);
  state.paperLedgerQueryDate = text(form.get("trade_date"), "");
  renderTrading();
});

document.addEventListener("change", (event) => {
  if (event.target.id !== "paper-account-select") return;
  state.selectedPaperAccountId = event.target.value || "";
  state.paperLedgerQueryDate = "";
  document.getElementById("paper-replay-form")?.reset();
  try { window.localStorage?.setItem("fxalpha.paperAccountId", state.selectedPaperAccountId); } catch (error) { /* ignore */ }
  renderTrading();
  refreshPaperRiskPolicy(state.selectedPaperAccountId).then(renderTrading).catch(() => {});
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-background-workflow-action]");
  if (!button || state.automationControlBusy) return;
  const workflow = button.dataset.backgroundWorkflow || "";
  const action = button.dataset.backgroundWorkflowAction || "";
  const workflowLabel = workflow === "data_foundation" ? "数据底座日更" : "模拟交易日切";
  const actionPrompts = {
    resume: `确认启用“${workflowLabel}”自动调度？定时器会从下一次计划时间开始触发。`,
    pause: `确认暂停“${workflowLabel}”自动调度？这只停止未来触发，不会中止当前正在运行的任务。`,
    run_now: `确认立即启动“${workflowLabel}”？该操作会调用正式生产任务，并遵循任务自身的生产门禁。`,
    update_schedule: `确认修改“${workflowLabel}”的自动调度时间？星期范围仍保持周二至周六。`,
  };
  const scheduleInput = document.querySelector(`[data-background-workflow-time="${workflow}"]`);
  const scheduleTime = action === "update_schedule" ? text(scheduleInput?.value, "") : null;
  if (action === "update_schedule" && !scheduleTime) {
    window.alert("请先选择执行时间。");
    return;
  }
  if (!window.confirm(actionPrompts[action] || "确认执行该后台自动化操作？")) return;

  state.automationControlBusy = true;
  button.disabled = true;
  try {
    state.automationActionResult = await postJson("/platform/automation-control", {
      workflow,
      action,
      schedule_time: scheduleTime,
      confirm: true,
    });
    state.automationStatus = await getJsonSafe("/platform/automation-status", { timeoutMs: 5000 });
  } catch (error) {
    state.automationActionResult = { ok: false, error: String(error), inputs: { workflow, action } };
  } finally {
    state.automationControlBusy = false;
    renderTrading();
  }
});

document.addEventListener("click", async (event) => {
  const tab = event.target.closest("[data-data-foundation-tab]");
  if (!tab) return;
  setDataFoundationTab(tab.dataset.dataFoundationTab);
});

document.querySelectorAll("[data-data-foundation-tab]").forEach((button) => {
  button.addEventListener("click", () => {
    setDataFoundationTab(button.dataset.dataFoundationTab);
  });
});

document.getElementById("refresh-data-live")?.addEventListener("click", async () => {
  const button = document.getElementById("refresh-data-live");
  setRefreshButtonState(button, "refreshing");
  try {
    await refreshDataLive();
    setRefreshButtonState(button, "success");
  } catch (error) {
    setRefreshButtonState(button, "idle");
    throw error;
  }
});

document.getElementById("data-live-preflight")?.addEventListener("click", async () => {
  state.dataLivePreflightResult = await postJson("/data/daily-preflight", {
    target_date: dataLiveTargetDate(),
  });
  state.latestDataAction = state.dataLivePreflightResult;
  state.dataFoundationTab = "live";
  localStorage.setItem("fxalpha-data-foundation-tab", state.dataFoundationTab);
  renderDataFoundation();
});

document.getElementById("start-data-daily-live")?.addEventListener("click", async () => {
  const yes = window.confirm("确认启动日期更新？这会按目标日期构建 staging 包，通过质量门后 promote 到生产数据。");
  if (!yes) return;
  await startDataUpdateFromGui({ mode: "daily", dryRun: false });
});

document.getElementById("start-data-full-rebuild-live")?.addEventListener("click", async () => {
  const yes = window.confirm("确认启动全量重建？这会消耗 Tushare 配额并生成新的全量源包；开始前建议先运行 Pre flight。");
  if (!yes) return;
  await startDataUpdateFromGui({ mode: "full_rebuild", dryRun: false });
});

document.getElementById("data-query-load-fields")?.addEventListener("click", async () => {
  await refreshDataQueryFields();
});

document.getElementById("data-query-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  await runDataQueryFromForm();
});

document.getElementById("data-query-submit")?.addEventListener("click", async () => {
  await runDataQueryFromForm();
});

document.getElementById("data-query-fields")?.addEventListener("click", (event) => {
  const groupButton = event.target.closest("[data-query-group]");
  if (groupButton) {
    const group = groupButton.dataset.queryGroup;
    const expanded = new Set(state.dataQueryExpandedGroups || []);
    if (expanded.has(group)) {
      expanded.delete(group);
    } else {
      expanded.add(group);
    }
    state.dataQueryExpandedGroups = [...expanded];
    renderDataQueryFields();
    return;
  }
  const transferButton = event.target.closest("[data-transfer-direction]");
  if (!transferButton) return;
  transferDataQueryFields(transferButton.dataset.transferDirection);
});

document.getElementById("library-search").addEventListener("input", (event) => {
  state.libraryFilter.query = event.target.value || "";
  renderFactorLibrary(serviceOutputs(state.factorConsole));
});

document.getElementById("library-status-filter").addEventListener("change", (event) => {
  state.libraryFilter.status = event.target.value;
  renderFactorLibrary(serviceOutputs(state.factorConsole));
});

document.getElementById("library-category-filter")?.addEventListener("change", (event) => {
  state.libraryFilter.category = event.target.value;
  renderFactorLibrary(serviceOutputs(state.factorConsole));
});

document.getElementById("library-holding-filter")?.addEventListener("change", (event) => {
  state.libraryFilter.holdingPeriod = event.target.value;
  renderFactorLibrary(serviceOutputs(state.factorConsole));
});

function startPolling() {
  if (state.refreshTimer) window.clearTimeout(state.refreshTimer);
  state.nextAutoRefreshAt = Date.now() + AUTO_REFRESH_INTERVAL_MS;
  state.refreshTimer = window.setTimeout(() => {
    if (document.hidden) {
      startPolling();
      return;
    }
    refreshState({ reason: "auto" }).catch((error) => {
      console.error("GUI auto refresh failed", error);
    }).finally(startPolling);
  }, AUTO_REFRESH_INTERVAL_MS);
}

document.getElementById("theme-toggle")?.addEventListener("click", () => {
  setTheme(currentTheme() === "light" ? "dark" : "light");
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  const staleFor = Date.now() - Number(state.lastRefreshAt || 0);
  if (state.lastRefreshAt && staleFor >= AUTO_REFRESH_INTERVAL_MS) {
    refreshState({ reason: "visibility" }).catch((error) => {
      console.error("GUI visibility refresh failed", error);
    });
  } else {
    startPolling();
    startLiveResearchPolling();
  }
});

function startLiveResearchPolling() {
  if (state.liveRefreshTimer) window.clearTimeout(state.liveRefreshTimer);
  state.liveRefreshTimer = null;
  if (document.hidden || (!researchPanelIsVisible() && !modelResearchPanelIsVisible())) return;
  const delay = researchPanelIsVisible()
    ? LIVE_RESEARCH_REFRESH_INTERVAL_MS
    : MODEL_LIVE_REFRESH_INTERVAL_MS;
  state.liveRefreshTimer = window.setTimeout(() => {
    const refresher = researchPanelIsVisible()
      ? refreshResearchLive({ force: true })
      : refreshState({ reason: "model_live" });
    refresher.catch((error) => {
      console.error("GUI research live refresh failed", error);
      startLiveResearchPolling();
    }).finally(() => {
      if (!researchPanelIsVisible()) startLiveResearchPolling();
    });
  }, delay);
}

restoreInitialNavigation();
syncThemeToggle();
buildGuidancePresets();
setupDatePickerButtons();
refreshState()
  .catch((error) => {
    console.error("GUI init failed", error);
    document.getElementById("api-chip").textContent = `Init failed: ${error}`;
  });
