import { convertFileSrc, invoke, listen, stageFile, openSystemPanel } from "./transport.web.js";
import JSZip from "jszip";
import "katex/dist/katex.min.css";
import { renderDirectionalGeneMarkdown } from "./markdown.js";
import "./styles.css";
import { initializeNotifications, syncNotifications } from "./notifications.js";
import { createCohortDownloadClock, elapsedDownloadTime } from "./cohort-download-clock.js";

const app = document.querySelector("#app");
initializeNotifications();
const cohortDownloadClock = createCohortDownloadClock();
const sidebarBrandIconUrl = new URL("./assets/GESKit_icon.svg", import.meta.url).href;
const startupArtIconUrl = new URL("./assets/GESKit_icon_no_bg.png", import.meta.url).href;
const THEME_STORAGE_KEY = "gleskit-theme-v2";
const THEME_LIGHT = "light";
const THEME_DARK = "dark";
const THEME_META_COLORS = {
  light: "#f5f8fc",
  dark: "#0d1117"
};
const DEFAULT_RUNTIME = {
  available: false,
  active_session_id: null,
  capture_enabled: false
};
const STARTUP_LOG_LIMIT = 6;
const STARTUP_STAGE_PROGRESS = {
  shell: 6,
  workspaceBootstrap: 20,
  heartbeat: 30,
  chatBootstrap: 52,
  chatListener: 58,
  workspaceSync: 78,
  cohortWarmup: 92,
  ready: 100
};

function readStoredTheme() {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === THEME_LIGHT || stored === THEME_DARK) {
      return stored;
    }
  } catch {
    // Ignore storage failures and fall back to the explicit light default.
  }
  return THEME_LIGHT;
}

function getMermaidThemeConfig(theme) {
  if (theme === THEME_DARK) {
    return {
      startOnLoad: false,
      securityLevel: "strict",
      theme: "dark"
    };
  }
  return {
    startOnLoad: false,
    securityLevel: "strict",
    theme: "neutral",
    themeVariables: {
      background: "#ffffff",
      primaryColor: "#dff1f8",
      primaryBorderColor: "#7bb9e6",
      primaryTextColor: "#152030",
      secondaryColor: "#eef8f3",
      secondaryBorderColor: "#9ad7be",
      secondaryTextColor: "#152030",
      tertiaryColor: "#f4f8fc",
      tertiaryBorderColor: "#c4d7e8",
      tertiaryTextColor: "#152030",
      lineColor: "#5faaf1",
      textColor: "#152030"
    }
  };
}

function applyTheme(theme) {
  const isDark = theme === THEME_DARK;
  document.body.classList.toggle("theme-dark", isDark);
  document.body.classList.toggle("theme-light", !isDark);
  document.documentElement.dataset.theme = theme;
  document.body.dataset.theme = theme;
  document.documentElement.style.colorScheme = isDark ? "dark" : "light";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) {
    meta.setAttribute("content", THEME_META_COLORS[isDark ? "dark" : "light"]);
  }
}

function setTheme(theme) {
  if (theme !== THEME_LIGHT && theme !== THEME_DARK) {
    return;
  }
  if (state.theme === theme) {
    return;
  }
  state.theme = theme;
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // Ignore storage failures; the live toggle still works for this session.
  }
  render();
}

const state = {
  theme: readStoredTheme(),
  appView: "workspace",
  paths: null,
  workspaceBootstrapState: "loading",
  runtime: { ...DEFAULT_RUNTIME },
  referenceStatus: null,
  referenceBusy: false,
  referenceOperation: "idle",
  startup: {
    visible: false,
    phase: "booting",
    message: "Preparing local workspace",
    progress: STARTUP_STAGE_PROGRESS.shell,
    log: [
      {
        id: "startup-1",
        level: "info",
        message: "Preparing local workspace",
        detail: ""
      }
    ],
    error: ""
  },
  cohortProviders: [],
  cohortProvidersLoaded: false,
  cohortCatalog: [],
  cohortCatalogLoadedProviderId: "",
  cohorts: [],
  cohortsLoaded: false,
  selectedCohortId: null,
  cohortQuery: "",
  cohortBusy: false,
  cohortProviderBusy: false,
  cohortCatalogBusy: false,
  cohortLibraryBusy: false,
  cohortTab: "download",
  cohortDownloadJob: null,
  cohortDownloadPollId: null,
  cohortDownloadLogOpen: false,
  cohortDownloadStarting: false,
  cohortDownloadStartedAt: 0,
  cohortDownloadFinishedAt: 0,
  cohortDownloadCancelling: false,
  cohortDownloadObservationError: "",
  cohortDownloadRefreshing: false,
  cohortDownloadGeneration: 0,
  selectedCohortProviderId: "tcga",
  selectedCohortDatasetId: "",
  selectedCohortTissue: "",
  cohortDownloadName: "",
  cohortDownloadScope: "full",
  cohortSubsetMode: "absolute",
  cohortSubsetValueText: "",
  cohortNotice: null,
  showCreateCohortForm: true,
  sessions: [],
  sessionQuery: "",
  artifactQuery: "",
  uploadQuery: "",
  figureQuery: "",
  noteQuery: "",
  reportQuery: "",
  selectedSessionId: null,
  sessionSummary: null,
  sessionSnapshotLoadedId: null,
  sessionSnapshotStatus: "idle",
  sessionSnapshotError: "",
  sessionLoadGeneration: 0,
  artifacts: [],
  selectedArtifactId: null,
  artifactDetail: null,
  uploads: [],
  selectedUploadId: null,
  uploadDetail: null,
  selectedFigureId: null,
  figureDetail: null,
  reports: [],
  selectedReportId: null,
  reportDetail: null,
  reportPreviewMode: "markdown",
  notes: [],
  notesLoadedSessionId: null,
  selectedNoteId: null,
  noteDetail: null,
  notePreviewMode: "markdown",
  selectedArtifacts: new Set(),
  selectedNotes: new Set(),
  selectedReports: new Set(),
  reportsLoadedSessionId: null,
  message: "",
  chatMessage: "",
  chatSystemMessage: "",
  error: "",
  confirmation: null,
  heartbeatId: null,
  page: "workspace",
  chatPaths: null,
  chatConversations: [],
  chatBootstrapState: "idle",
  chatBootstrapError: "",
  chatConversationLoadGeneration: 0,
  chatConversationLoading: false,
  selectedChatId: null,
  chatMessages: [],
  chatTurnEvents: [],
  chatDraft: "",
  chatBusy: false,
  activeChatTurn: null,
  pendingChatUserMessage: null,
  chatPendingMessageCount: null,
  chatApprovalReplyDrafts: {},
  chatApprovalOtherOpenIds: new Set(),
  chatProviders: [],
  chatModelQuery: "",
  chatModelSelections: {},
  chatProvidersLastLoadedAt: 0,
  chatProvidersRefreshInFlight: false,
  gleskitConfig: {
    system_prompt_text: "",
    agents: [],
    default_agent: "",
    skills: [],
    mcp_enabled: true,
    created_at: "",
    updated_at: "",
    prompt_restore_available: false,
    restore_all_available: false
  },
  selectedProviderId: null,
  selectedModelId: "",
  selectedAgentId: "",
  chatModelPickerOpen: false,
  chatAgentSelections: {},
  mcpStatus: {
    running: false,
    transport: "stdio",
    pid: null,
    error: null
  },
  chatActivitySidebarOpen: false,
  editingChatConversationId: null,
  chatShouldScrollToLatest: false,
  showCreateForm: false,
  showDeriveForm: false,
  showEditSessionForm: false,
  dismissedApprovalIds: new Set(),
  chatErrorExpanded: false,
  chatCancellationInProgress: false,
  chatCancellationConversationId: null
};
applyTheme(state.theme);
let startupLogCounter = 1;
let mermaidModulePromise = null;
let mermaidDiagramCounter = 0;
let mermaidRenderRunId = 0;
let katexModulePromise = null;
let chatTurnEventUnlisten = null;
let chatBootstrapPromise = null;
let cohortLibraryPromise = null;
let chatConversationRefreshTimer = null;
let chatConversationRefreshTarget = null;
let chatConversationRefreshTerminal = false;
let chatConversationRefreshDelay = 750;
let chatConversationRefreshInFlight = false;
let chatSendInFlight = false;
const CHAT_ACTIVITY_TURN_LIMIT = 3;
const CHAT_RENDER_THROTTLE_MS = 80;
const TRACKED_CHAT_EVENT_TYPES = new Set([
  "turn.retrying",
  "turn.progress",
  "turn.started",
  "turn.prompt_compiled",
  "turn.skills_available",
  "turn.provider_request_started",
  "turn.provider_request_finished",
  "turn.completed",
  "turn.failed",
  "turn.cancelled",
  "system.warning",
  "system.error",
  "skill.loaded",
  "skill.file_loaded",
  "tool.approval_requested",
  "tool.approved",
  "tool.rejected",
  "tool.started",
  "tool.finished",
  "tool.failed"
]);
let chatRenderScheduled = false;
let chatRenderLastAt = 0;

async function ensureMermaid() {
  if (!mermaidModulePromise) {
    mermaidModulePromise = import("mermaid").then((module) => module.default);
  }
  const mermaid = await mermaidModulePromise;
  mermaid.initialize(getMermaidThemeConfig(state.theme));
  return mermaid;
}

async function ensureKatex() {
  if (!katexModulePromise) {
    katexModulePromise = Promise.all([
      import("katex"),
      import("katex/contrib/auto-render")
    ]).then(([katexModule, autoRenderModule]) => {
      const katex = katexModule.default || katexModule;
      const renderMathInElement = autoRenderModule.default || autoRenderModule;
      return { katex, renderMathInElement };
    });
  }
  return katexModulePromise;
}

function renderMathInPreviews() {
  const containers = Array.from(document.querySelectorAll(".markdown-preview"));
  if (!containers.length) {
    return;
  }
  void ensureKatex().then(({ renderMathInElement }) => {
    containers.forEach((container) => {
      if (container.dataset.mathRendered === "true") {
        return;
      }
      renderMathInElement(container, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "\\[", right: "\\]", display: true },
          { left: "\\(", right: "\\)", display: false },
          { left: "$", right: "$", display: false }
        ],
        throwOnError: false,
        ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"]
      });
      container.dataset.mathRendered = "true";
    });
  });
}

async function renderMermaidDiagrams() {
  const containers = Array.from(document.querySelectorAll(".markdown-mermaid"));
  if (!containers.length) {
    return;
  }

  const runId = ++mermaidRenderRunId;
  let mermaid;
  try {
    mermaid = await ensureMermaid();
  } catch (error) {
    containers.forEach((container) => {
      const source = container.textContent || "";
      container.classList.add("is-error");
      container.innerHTML = `<pre class="markdown-code"><code>${escapeHtml(source)}</code></pre><div class="mermaid-error">${escapeHtml(formatError(error))}</div>`;
    });
    return;
  }

  for (const container of containers) {
    if (runId !== mermaidRenderRunId) {
      return;
    }
    const source = String(container.textContent || "").trim();
    if (!source) {
      continue;
    }
    const diagramId = `gleskit-mermaid-${mermaidDiagramCounter += 1}`;
    try {
      const { svg, bindFunctions } = await mermaid.render(diagramId, source);
      if (runId !== mermaidRenderRunId) {
        return;
      }
      container.innerHTML = svg;
      container.classList.add("is-rendered");
      if (typeof bindFunctions === "function") {
        bindFunctions(container);
      }
    } catch (error) {
      container.classList.add("is-error");
      container.innerHTML = `<pre class="markdown-code"><code>${escapeHtml(source)}</code></pre><div class="mermaid-error">${escapeHtml(formatError(error))}</div>`;
    }
  }
}

function startupPhaseLabel(phase) {
  const labels = {
    booting: "Booting",
    workspaceBootstrap: "Workspace",
    heartbeat: "Runtime",
    chatBootstrap: "Chat",
    chatListener: "Chat events",
    workspaceSync: "Workspace sync",
    cohortWarmup: "Cohorts",
    ready: "Ready",
    warning: "Warning",
    error: "Error"
  };
  return labels[phase] || "Booting";
}

function pushStartupLog(level, message, detail = "") {
  const text = String(message || "").trim();
  if (!text) {
    return;
  }
  const nextEntry = {
    id: `startup-${startupLogCounter += 1}`,
    level,
    message: text,
    detail: String(detail || "").trim()
  };
  state.startup.log = [...state.startup.log, nextEntry].slice(-STARTUP_LOG_LIMIT);
}

function updateStartupStage(progress, message, { phase = "", level = "info", detail = "" } = {}) {
  const nextProgress = Number(progress);
  if (Number.isFinite(nextProgress)) {
    state.startup.progress = Math.max(state.startup.progress, Math.max(0, Math.min(100, nextProgress)));
  }
  if (phase) {
    state.startup.phase = phase;
  }
  if (message) {
    state.startup.message = message;
  }
  pushStartupLog(level, message, detail);
  render();
}

function logStartupWarning(message, detail = "") {
  pushStartupLog("warning", message, detail);
  render();
}

function finalizeStartup(message = "GLESKit is ready") {
  state.startup.phase = "ready";
  state.startup.progress = STARTUP_STAGE_PROGRESS.ready;
  state.startup.message = message;
  pushStartupLog("success", message);
  state.startup.visible = false;
  render();
}

function renderStartupScreen() {
  const progress = Math.max(0, Math.min(100, Number(state.startup.progress || 0)));
  const phaseLabel = startupPhaseLabel(state.startup.phase);
  const headline = state.startup.phase === "error" ? "Startup paused" : "Preparing your workspace";
  const subhead = state.startup.phase === "error"
    ? "GLESKit hit a blocking error while starting."
    : "Workspace, chat, and cohorts are warming up.";
  const logEntries = Array.isArray(state.startup.log) ? state.startup.log.slice(-STARTUP_LOG_LIMIT) : [];
  return `
    <div class="startup-screen" role="status" aria-live="polite" aria-busy="true">
      <section class="frame startup-card">
        <div class="startup-copy">
          <div class="startup-brand">
            <div class="brand-mark startup-brand-mark" aria-hidden="true">
              <img src="${sidebarBrandIconUrl}" alt="" />
            </div>
            <div>
              <div class="eyebrow">GLESKit Local</div>
              <h1>${escapeHtml(headline)}</h1>
            </div>
          </div>
          <div class="startup-message">${escapeHtml(state.startup.message || subhead)}</div>
          <div class="startup-subhead">${escapeHtml(subhead)}</div>
          <div class="startup-progress-block">
            <div class="startup-progress-meta">
              <span>${escapeHtml(`${Math.round(progress)}%`)}</span>
              <span>${escapeHtml(phaseLabel)}</span>
            </div>
            <div class="progress-track startup-progress-track" aria-hidden="true">
              <div class="progress-fill" style="width: ${progress}%"></div>
            </div>
          </div>
          <div class="startup-log" aria-label="Startup log">
            <div class="startup-log-head">Startup log</div>
            <ul class="startup-log-list">
              ${logEntries.map((entry) => `
                <li class="startup-log-item ${escapeHtml(entry.level || "info")}">
                  <span class="startup-log-dot" aria-hidden="true"></span>
                  <div class="startup-log-copy">
                    <span class="startup-log-message">${escapeHtml(entry.message || "")}</span>
                    ${entry.detail ? `<span class="startup-log-detail">${escapeHtml(entry.detail)}</span>` : ""}
                  </div>
                </li>
              `).join("")}
            </ul>
          </div>
          ${state.startup.error ? `<div class="startup-error">${escapeHtml(state.startup.error)}</div>` : ""}
        </div>
        <div class="startup-art" aria-hidden="true">
          <img class="startup-art-logo" src="${startupArtIconUrl}" alt="" />
        </div>
      </section>
    </div>
  `;
}

async function bootstrap() {
  try {
    // The shell is already painted; backend metadata never gates navigation.
    state.workspaceBootstrapState = "loading";
    render();
    const payload = await invoke("bootstrap_workspace");
    hydrateWorkspaceState(payload, { preferRuntimeSession: true });
    normalizeRuntimeSession();
    state.showCreateForm = state.sessions.length === 0;
    state.workspaceBootstrapState = "ready";
    if (state.paths?.standalone) {
      state.chatProviders = await invoke("list_chat_providers");
    }
    updateStartupStage(STARTUP_STAGE_PROGRESS.workspaceBootstrap, "Loading workspace metadata", {
      phase: "workspaceBootstrap"
    });
    try {
      await writeHeartbeat();
      startHeartbeat();
      updateStartupStage(STARTUP_STAGE_PROGRESS.heartbeat, "Starting runtime heartbeat", {
        phase: "heartbeat"
      });
    } catch (error) {
      logStartupWarning("Runtime heartbeat could not start yet", formatError(error));
      startHeartbeat();
    }
    // Workspace is usable now; everything below is background warm-up and must
    // never block the window or fail startup.
    finalizeStartup("GLESKit is ready");
    void warmBackgroundServices();
  } catch (error) {
    state.workspaceBootstrapState = "failed";
    state.error = formatError(error);
    render();
  }
}

/**
 * Serial background warm-up that runs after the workspace is already visible.
 *
 * Each stage is independently guarded: a failure is logged and skipped rather
 * than escalated, because none of these services are required for the
 * Workspace view to function.
 */
async function warmBackgroundServices() {
  if (state.selectedSessionId) {
    try {
      await ensureSessionSnapshot(state.selectedSessionId);
    } catch (error) {
      logStartupWarning("Selected session snapshot skipped", formatError(error));
    }
  }
  try {
    await refreshReferenceStatus();
  } catch (error) {
    logStartupWarning("Reference status skipped", formatError(error));
  }
  // Standalone first-run setup does not start OpenCode or scientific imports.
  // The user starts Chat explicitly after configuring a provider.
  if (!state.paths?.standalone) {
    try {
      await ensureChatReady();
    } catch (error) {
      logStartupWarning("Chat warmup skipped", formatError(error));
    }
  }
}

function startHeartbeat() {
  stopHeartbeat();
}

function stopHeartbeat() {
  if (state.heartbeatId) {
    window.clearInterval(state.heartbeatId);
    state.heartbeatId = null;
  }
}

async function writeHeartbeat() {
  const payload = await invoke("write_runtime_heartbeat", {
    activeSessionId: state.runtime.active_session_id,
    captureEnabled: Boolean(state.runtime.capture_enabled && state.runtime.active_session_id)
  });
  state.runtime = {
    ...DEFAULT_RUNTIME,
    ...payload
  };
}

async function bootstrapChatState({ onLog, loadSelectedConversation = true } = {}) {
  if (typeof onLog === "function") {
    onLog("info", "Bootstrapping chat cache");
  }
  const payload = await invoke("bootstrap_chat");
  hydrateChatState(payload);
  if (typeof onLog === "function") {
    onLog(
      "success",
      `Chat cache ready (${state.chatConversations.length} conversations, ${state.chatProviders.length} providers)`
    );
  }
  if (!state.selectedChatId && state.chatConversations[0]) {
    state.selectedChatId = state.chatConversations[0].conversation_id;
  }
  syncSelectedProviderAndModel();
  if (loadSelectedConversation && state.selectedChatId) {
    if (typeof onLog === "function") {
      onLog("info", "Loading selected chat conversation", state.selectedChatId);
    }
    await loadChatConversation(state.selectedChatId, { preserveDraft: true });
    if (typeof onLog === "function") {
      onLog("success", "Chat conversation ready", state.selectedChatId);
    }
  } else if (typeof onLog === "function") {
    onLog("info", "Chat summaries loaded; message history deferred");
  }
}

/**
 * Idempotent Chat readiness gate.
 *
 * Returns immediately when Chat is ready, shares the in-flight promise while
 * loading, and never writes into the global `state.error`. Failures are
 * reported through `chatBootstrapState` / `chatBootstrapError` so the Chat view
 * can render a scoped retry affordance.
 */
function ensureChatReady() {
  if (state.paths?.standalone && !state.chatProviders.some((provider) => provider.enabled)) {
    render();
    return Promise.reject(new Error("Configure and activate a provider in Settings before starting Chat."));
  }
  if (state.chatBootstrapState === "ready") {
    return Promise.resolve();
  }
  if (chatBootstrapPromise) {
    return chatBootstrapPromise;
  }
  state.chatBootstrapState = "loading";
  state.chatBootstrapError = "";
  render();
  chatBootstrapPromise = (async () => {
    try {
      await bootstrapChatState({ loadSelectedConversation: false });
      await ensureChatTurnEventListener();
      state.chatBootstrapState = "ready";
      state.chatBootstrapError = "";
    } catch (error) {
      state.chatBootstrapState = "failed";
      state.chatBootstrapError = formatError(error);
      throw error;
    } finally {
      chatBootstrapPromise = null;
      render();
    }
  })();
  return chatBootstrapPromise;
}

function retryChatBootstrap() {
  state.chatBootstrapState = "idle";
  state.chatBootstrapError = "";
  render();
  void (async () => {
    if (state.paths?.standalone) {
      state.chatBootstrapState = "loading";
      render();
      await invoke("reconnect_chat_runtime");
      state.chatBootstrapState = "idle";
    }
    await ensureChatReady();
  })().catch((error) => {
    state.chatBootstrapState = "failed";
    state.chatBootstrapError = formatError(error);
    render();
  });
}

function isChatReady() {
  return state.chatBootstrapState === "ready";
}

async function ensureChatTurnEventListener() {
  if (chatTurnEventUnlisten) {
    return;
  }
  chatTurnEventUnlisten = await listen("gleskit://chat-turn-event", (event) => {
    handleChatTurnEvent(event.payload);
  });
}

function scheduleChatConversationRefresh(conversationId = state.selectedChatId, { terminal = false, delay = 750 } = {}) {
  if (!conversationId) {
    return;
  }
  const sameTarget = chatConversationRefreshTarget === conversationId;
  chatConversationRefreshTarget = conversationId;
  chatConversationRefreshTerminal = (sameTarget && chatConversationRefreshTerminal) || terminal;
  chatConversationRefreshDelay = sameTarget ? Math.min(chatConversationRefreshDelay, delay) : delay;
  // Events arriving during a snapshot fetch queue one follow-up, never another
  // concurrent full-conversation request. Completion keeps its priority.
  if (chatConversationRefreshInFlight) return;
  if (chatConversationRefreshTimer && !terminal) return;
  if (chatConversationRefreshTimer) {
    window.clearTimeout(chatConversationRefreshTimer);
  }
  chatConversationRefreshTimer = window.setTimeout(() => {
    const nextConversationId = chatConversationRefreshTarget;
    const nextTerminal = chatConversationRefreshTerminal;
    chatConversationRefreshTimer = null;
    chatConversationRefreshTarget = null;
    chatConversationRefreshTerminal = false;
    if (!nextConversationId) {
      return;
    }
    void refreshSelectedChatConversation(nextConversationId, { terminal: nextTerminal });
  }, chatConversationRefreshDelay);
}

function clearChatConversationRefreshTimer() {
  if (chatConversationRefreshTimer) {
    window.clearTimeout(chatConversationRefreshTimer);
    chatConversationRefreshTimer = null;
  }
  chatConversationRefreshTarget = null;
  chatConversationRefreshTerminal = false;
  chatConversationRefreshDelay = 750;
}

function startChatTurnRequest() {
  clearChatConversationRefreshTimer();
  state.chatBusy = true;
  state.chatMessage = "";
  state.chatSystemMessage = "";
}

async function refreshSelectedChatConversation(
  conversationId = state.selectedChatId,
  { terminal = false } = {},
) {
  if (!conversationId) {
    return;
  }
  if (chatConversationRefreshInFlight) {
    scheduleChatConversationRefresh(conversationId, { terminal });
    return;
  }
  chatConversationRefreshInFlight = true;
  const requestedTurn = state.activeChatTurn;
  try {
    const payload = await invoke("get_chat_conversation", { conversationId });
    if (state.selectedChatId !== conversationId || state.activeChatTurn !== requestedTurn) {
      return;
    }
    state.chatMessages = payload.messages || [];
    state.chatTurnEvents = Array.isArray(payload.turn_events)
      ? payload.turn_events.map(normalizeChatTurnEvent).filter(Boolean)
      : [];
    state.chatConversations = state.chatConversations.map((item) => (
      item.conversation_id === conversationId ? payload.conversation : item
    ));
    const activeTurnId = state.activeChatTurn?.turn_id || "";
    const completed = state.chatTurnEvents.filter((event) => event.turn_id === activeTurnId && event.event_type === "turn.completed").at(-1);
    const failed = state.chatTurnEvents.find((event) => event.turn_id === activeTurnId && ["turn.failed", "turn.cancelled"].includes(event.event_type));
    const terminalResponseReady = (terminal || completed) && state.chatMessages.some((message) => (
      message.role === "assistant" && message.turn_id === activeTurnId && message.finished &&
      (!completed?.message_id || message.message_id === completed.message_id)
    ));
    // A completion notification is not the final persisted answer. Keep polling
    // and showing dots until the matching finished snapshot is actually visible.
    if (terminalResponseReady || failed || (terminal && state.activeChatTurn?.failed) || (terminal && state.activeChatTurn?.cancelled)) {
      if (failed?.payload?.error) state.error = formatError(failed.payload.error);
      state.activeChatTurn = null;
      state.pendingChatUserMessage = null;
      state.chatBusy = false;
      state.chatMessage = "";
      state.chatPendingMessageCount = null;
    } else if (state.chatBusy) {
      if (state.chatMessages.some((message) => message.role === "user" && message.turn_id === activeTurnId)) state.pendingChatUserMessage = null;
      scheduleChatConversationRefresh(conversationId, { terminal, delay: 1500 });
    }
    syncSelectedProviderAndModel();
    if (!state.chatModelQuery.trim()) {
      state.chatModelQuery = state.selectedModelId || "";
    }
    queueChatScrollToLatest();
    scheduleChatRender();
  } catch (error) {
    if (state.selectedChatId === conversationId && state.activeChatTurn === requestedTurn && state.chatBusy) {
      state.chatMessage = "Reconnecting to the response…";
      scheduleChatConversationRefresh(conversationId, { terminal, delay: 2000 });
    }
  } finally {
    chatConversationRefreshInFlight = false;
    if (chatConversationRefreshTarget === state.selectedChatId && state.chatBusy) {
      scheduleChatConversationRefresh(chatConversationRefreshTarget, {
        terminal: chatConversationRefreshTerminal, delay: chatConversationRefreshDelay
      });
    } else {
      clearChatConversationRefreshTimer();
    }
  }
}

function handleChatTurnEvent(payload) {
  const nextEvent = normalizeChatTurnEvent(payload);
  if (!nextEvent) {
    return;
  }
  const isSystemEvent = nextEvent.event_type.startsWith("system.");
  if (nextEvent.event_type === "system.mcp_status") {
    state.mcpStatus = nextEvent.payload;
    scheduleChatRender();
    return;
  }
  const conversationId = nextEvent.conversation_id || state.selectedChatId || "";
  if (!isSystemEvent && conversationId !== state.selectedChatId) {
    return;
  }
  if (!isSystemEvent && !shouldTrackChatTurnEvent(nextEvent.event_type)) {
    return;
  }
  // Stream notifications can arrive after the final snapshot. Only a user
  // submission may open a turn; late notifications must never reopen it.
  if (!isSystemEvent && (!state.chatBusy || !state.activeChatTurn)) return;
  if (!isSystemEvent && state.activeChatTurn.turn_id && nextEvent.turn_id && state.activeChatTurn.turn_id !== nextEvent.turn_id) return;
  const storedEvent = {
    ...nextEvent,
    conversation_id: conversationId,
    turn_id: nextEvent.turn_id || state.activeChatTurn?.turn_id || conversationId || "system"
  };
  if (storedEvent.event_type === "turn.progress") {
    scheduleChatConversationRefresh(conversationId);
    return;
  }
  if (storedEvent.event_type === "turn.retrying") {
    state.chatMessage = storedEvent.payload.message;
    scheduleChatRender();
    return;
  }
  if (isSystemEvent) {
    const noticeSource = storedEvent.payload?.error ?? storedEvent.payload?.message ?? storedEvent.payload;
    const notice = formatError(noticeSource);
    if (storedEvent.event_type === "system.warning") {
      state.chatSystemMessage = notice;
      state.chatMessage = "";
    } else if (storedEvent.event_type === "system.error") {
      state.error = notice;
      state.chatErrorExpanded = false;
    }
    if (conversationId) {
      state.chatTurnEvents = [
        ...state.chatTurnEvents.filter((event) => event.event_id !== storedEvent.event_id),
        storedEvent
      ];
      if (state.activeChatTurn) {
        state.activeChatTurn.events = [
          ...state.activeChatTurn.events.filter((event) => event.event_id !== storedEvent.event_id),
          storedEvent
        ];
      }
    }
    queueChatScrollToLatest();
    scheduleChatRender();
    return;
  }
  if (!state.activeChatTurn || state.activeChatTurn.turn_id !== storedEvent.turn_id) {
    state.activeChatTurn = {
      turn_id: storedEvent.turn_id,
      events: [],
      phase: "thinking",
      attachedSkills: [],
      pendingApproval: null,
      completed: false,
      failed: false
    };
  }
  state.activeChatTurn.events = [...state.activeChatTurn.events, storedEvent];
  if (storedEvent.event_type === "turn.started") {
    const pipelineMode = Boolean(storedEvent.payload?.pipeline_mode);
    state.activeChatTurn.phase = pipelineMode ? "pipeline mode active" : "thinking";
    if (pipelineMode) {
      state.chatSystemMessage = "Pipeline mode activated.";
    }
    state.chatMessage = "";
    state.chatBusy = true;
  }
  applyActiveTurnEvent(storedEvent);
  if (storedEvent.event_type === "turn.completed" || storedEvent.event_type === "turn.failed" || storedEvent.event_type === "turn.cancelled") {
    state.chatMessage = "";
    if (storedEvent.event_type === "turn.cancelled") {
      state.chatCancellationInProgress = false;
      state.chatCancellationConversationId = null;
      state.chatMessage = "Request cancelled.";
    }
    state.chatBusy = true;
    scheduleChatConversationRefresh(conversationId, {
      terminal: true,
      delay: 250
    });
  }
  queueChatScrollToLatest();
  scheduleChatRender();
}

function normalizeChatTurnEvent(payload) {
  if (!payload || typeof payload !== "object") {
    return null;
  }
  return {
    event_id: String(payload.event_id || ""),
    conversation_id: String(payload.conversation_id || ""),
    turn_id: String(payload.turn_id || ""),
    message_id: payload.message_id || null,
    event_type: String(payload.event_type || ""),
    payload: payload.payload && typeof payload.payload === "object" ? payload.payload : {},
    created_at: String(payload.created_at || new Date().toISOString())
  };
}

function applyActiveTurnEvent(event) {
  if (!state.activeChatTurn) {
    return;
  }
  if (event.event_type.startsWith("system.")) {
    return;
  }
  const payload = event.payload || {};
  if (event.event_type === "turn.skills_available") {
    state.activeChatTurn.attachedSkills = Array.isArray(payload.skills) ? payload.skills : [];
  }
  if (event.event_type === "tool.approval_requested") {
    const approvalId = String(payload.approval_id || "");
    if (approvalId) {
      state.dismissedApprovalIds.delete(approvalId);
    }
    state.activeChatTurn.pendingApproval = payload;
  }
  if (["tool.approved", "tool.rejected", "tool.started", "tool.finished", "tool.failed"].includes(event.event_type)) {
    state.activeChatTurn.pendingApproval = null;
  }
  if (event.event_type === "turn.completed") {
    state.activeChatTurn.completed = true;
  }
  if (event.event_type === "turn.failed") {
    state.activeChatTurn.failed = true;
  }
  if (event.event_type === "turn.cancelled") {
    state.activeChatTurn.cancelled = true;
  }
  if (["turn.started", "turn.provider_request_started", "tool.approval_requested", "tool.started", "tool.finished"].includes(event.event_type)) {
    state.chatBusy = true;
  }
  if (event.event_type !== "turn.started") {
    state.activeChatTurn.phase = phaseForTurnEvent(event.event_type);
  }
}

function phaseForTurnEvent(eventType) {
  if (eventType === "tool.approval_requested") {
    return "waiting for approval";
  }
  if (eventType === "tool.started") {
    return "running tool";
  }
  if (eventType === "turn.completed") {
    return "complete";
  }
  if (eventType === "turn.failed") {
    return "failed";
  }
  if (eventType === "turn.cancelled") {
    return "cancelled";
  }
  if (eventType === "turn.provider_request_finished") {
    return "finalizing";
  }
  return "thinking";
}

function activityTimelineEvents(events) {
  const result = [], terminals = new Map(), seen = new Set();
  for (const event of events) {
    if (event.event_type === "turn.progress") continue;
    if (["turn.completed", "turn.failed", "turn.cancelled"].includes(event.event_type) && event.turn_id) {
      const key = JSON.stringify([event.conversation_id, event.turn_id]);
      if (terminals.has(key)) result[terminals.get(key)] = event;
      else { terminals.set(key, result.length); result.push(event); }
    } else if (!event.event_id || !seen.has(event.event_id)) {
      result.push(event);
    }
    seen.add(event.event_id);
  }
  return result;
}

function countActivityToolCalls(events) {
  const calls = new Set(), legacy = new Map();
  for (const event of events) {
    if (!["tool.started", "tool.finished", "tool.failed"].includes(event.event_type)) continue;
    const payload = event.payload || {};
    if (payload.call_id) {
      calls.add(JSON.stringify([event.conversation_id, event.turn_id, payload.call_id]));
    } else {
      // Older/native hosts lack call IDs. Pair start/end counts per tool and
      // message rather than mislabelling every lifecycle event as a new call.
      const key = JSON.stringify([event.conversation_id, event.turn_id, event.message_id, payload.tool_name]);
      const counts = legacy.get(key) || { started: 0, ended: 0 };
      counts[event.event_type === "tool.started" ? "started" : "ended"] += 1;
      legacy.set(key, counts);
    }
  }
  return calls.size + [...legacy.values()].reduce((total, counts) => total + Math.max(counts.started, counts.ended), 0);
}

function visibleChatTurnEvents() {
  const persisted = Array.isArray(state.chatTurnEvents) ? state.chatTurnEvents : [];
  const live = state.activeChatTurn?.events || [];
  if (!live.length) {
    return activityTimelineEvents(persisted).filter((event) => inlineActivityEventTypes().includes(event.event_type));
  }
  const seen = new Set(persisted.map((event) => event.event_id));
  return activityTimelineEvents(persisted
    .concat(live.filter((event) => !seen.has(event.event_id))))
    .filter((event) => inlineActivityEventTypes().includes(event.event_type));
}

function selectedConversationAllowsAlways() {
  return getSelectedChatConversation()?.tool_approval_mode === "allow_always";
}

async function refreshChatProviders({ force = false } = {}) {
  if (state.chatProvidersRefreshInFlight) {
    return;
  }
  const now = window.performance.now();
  if (!force && now - state.chatProvidersLastLoadedAt < 10000) {
    return;
  }
  state.chatProvidersRefreshInFlight = true;
  try {
    if (force) {
      try {
        await invoke("refresh_chat_model_catalog");
      } catch (error) {
        state.chatSystemMessage = `OpenCode model catalog refresh failed: ${formatError(error)}`;
      }
    }
    const providers = await invoke("list_chat_providers");
    state.chatProviders = Array.isArray(providers) ? providers : [];
    state.chatProvidersLastLoadedAt = window.performance.now();
    syncSelectedProviderAndModel();
    scheduleChatRender();
  } catch (error) {
    state.chatProvidersLastLoadedAt = window.performance.now();
    state.chatSystemMessage = `OpenCode providers could not be refreshed: ${formatError(error)}`;
    scheduleChatRender();
  } finally {
    state.chatProvidersRefreshInFlight = false;
  }
}

function hydrateChatState(payload) {
  state.chatPaths = payload?.paths || state.chatPaths;
  state.chatConversations = payload?.conversations || [];
  state.chatProviders = payload?.providers || [];
  state.chatProvidersLastLoadedAt = window.performance.now();
  state.gleskitConfig = payload?.gleskit_config || state.gleskitConfig;
  state.mcpStatus = payload?.mcp_status || state.mcpStatus;
  if (state.selectedChatId && !state.chatConversations.some((item) => item.conversation_id === state.selectedChatId)) {
    state.selectedChatId = null;
    state.chatMessages = [];
    state.chatTurnEvents = [];
    state.activeChatTurn = null;
    state.pendingChatUserMessage = null;
    state.chatPendingMessageCount = null;
  }
  syncSelectedProviderAndModel();
}

function syncSelectedProviderAndModel() {
  const selectedConversation = getSelectedChatConversation();
  if (selectedConversation?.provider_id && state.chatProviders.some((item) => item.provider_id === selectedConversation.provider_id)) {
    state.selectedProviderId = selectedConversation.provider_id;
  } else if (!state.chatProviders.some((item) => item.provider_id === state.selectedProviderId && item.enabled)) {
    state.selectedProviderId = state.chatProviders.find((item) => item.enabled)?.provider_id || state.chatProviders[0]?.provider_id || null;
  }
  if (state.paths?.standalone) {
    state.selectedProviderId = state.chatProviders.find((provider) => provider.enabled)?.provider_id || null;
  }
  const selectedProvider = getSelectedChatProvider();
  const providerModels = Array.isArray(selectedProvider?.models) ? selectedProvider.models : [];
  const selectedConversationId = selectedConversation?.conversation_id || "";
  let nextModelId = "";
  const selectionMap = state.chatModelSelections || {};
  const hasRememberedModel = selectedConversationId
    ? Object.prototype.hasOwnProperty.call(selectionMap, selectedConversationId)
    : false;
  const rememberedModelId = hasRememberedModel ? String(selectionMap[selectedConversationId] || "") : "";
  if (hasRememberedModel) {
    if (rememberedModelId && providerModels.includes(rememberedModelId)) {
      nextModelId = rememberedModelId;
    }
  } else if (selectedConversation?.model_id && providerModels.includes(selectedConversation.model_id)) {
    nextModelId = selectedConversation.model_id;
  }
  if (!nextModelId && state.paths?.standalone) { nextModelId = selectedProvider?.default_model || ""; }
  state.selectedModelId = nextModelId;
  if (selectedConversationId) {
    state.chatModelSelections = {
      ...state.chatModelSelections,
      [selectedConversationId]: nextModelId,
    };
  }
  const query = String(state.chatModelQuery || "").trim();
  if (!query || query === String(state.selectedModelId || "").trim()) {
    state.chatModelQuery = nextModelId;
  }
  const agentOptions = Array.isArray(state.gleskitConfig.agents) ? state.gleskitConfig.agents : [];
  const selectedAgentIds = new Set(agentOptions.map((item) => item.agent_id));
  const hasSelectedConversation = Boolean(selectedConversationId);
  const rememberedAgentId = hasSelectedConversation && Object.prototype.hasOwnProperty.call(state.chatAgentSelections || {}, selectedConversationId)
    ? String(state.chatAgentSelections[selectedConversationId] || "")
    : "";
  let nextAgentId = "";
  if (rememberedAgentId && selectedAgentIds.has(rememberedAgentId)) {
    nextAgentId = rememberedAgentId;
  } else if (selectedConversation?.agent && selectedAgentIds.has(selectedConversation.agent)) {
    nextAgentId = selectedConversation.agent;
  } else if (state.gleskitConfig.default_agent && selectedAgentIds.has(state.gleskitConfig.default_agent)) {
    nextAgentId = state.gleskitConfig.default_agent;
  } else {
    nextAgentId = agentOptions[0]?.agent_id || "";
  }
  state.selectedAgentId = nextAgentId;
  if (hasSelectedConversation) {
    state.chatAgentSelections = {
      ...state.chatAgentSelections,
      [selectedConversationId]: nextAgentId,
    };
  }
}

function normalizeChatModelToken(value) {
  return String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
}

function scoreChatModelText(value, query) {
  const rawQuery = String(query || "").trim().toLowerCase();
  if (!rawQuery) {
    return 10;
  }
  const normalizedQuery = normalizeChatModelToken(rawQuery);
  const lowerValue = String(value || "").toLowerCase();
  const normalizedValue = normalizeChatModelToken(value);
  if (!normalizedValue) {
    return Number.POSITIVE_INFINITY;
  }
  if (normalizedValue === normalizedQuery || lowerValue === rawQuery) {
    return 0;
  }
  if (normalizedValue.startsWith(normalizedQuery) || lowerValue.startsWith(rawQuery)) {
    return 1;
  }
  if (normalizedValue.includes(normalizedQuery) || lowerValue.includes(rawQuery)) {
    return 2;
  }
  const queryTokens = rawQuery.split(/[^a-z0-9]+/).filter(Boolean);
  if (queryTokens.length && queryTokens.every((token) => lowerValue.includes(token))) {
    return 3;
  }
  return Number.POSITIVE_INFINITY;
}

function scoreChatModelOption(modelId, query, modelLabel = "") {
  const idScore = scoreChatModelText(modelId, query);
  const labelScore = scoreChatModelText(modelLabel, query);
  const normalizedLabelScore = labelScore === Number.POSITIVE_INFINITY ? labelScore : labelScore + 0.25;
  return Math.min(idScore, normalizedLabelScore);
}

function getChatModelOptionEntries(provider) {
  const models = Array.isArray(provider?.models) ? provider.models : [];
  const labels = Array.isArray(provider?.model_labels) ? provider.model_labels : [];
  return models.map((modelId, index) => ({
    modelId,
    modelLabel: labels[index] || modelId,
  }));
}

function getChatModelSuggestions(provider, query, { limit = 8 } = {}) {
  const models = Array.isArray(provider?.models) ? provider.models : [];
  if (!models.length) {
    return [];
  }
  const normalizedQuery = String(query || "").trim();
  const selectedModelId = String(state.selectedModelId || "").trim();
  const entries = getChatModelOptionEntries(provider);
  return entries
    .map(({ modelId, modelLabel }, index) => ({
      modelId,
      modelLabel,
      score: scoreChatModelOption(modelId, normalizedQuery, modelLabel),
      index,
    }))
    .filter((item) => item.score !== Number.POSITIVE_INFINITY)
    .sort((left, right) => {
      if (left.modelId === selectedModelId && right.modelId !== selectedModelId) {
        return -1;
      }
      if (right.modelId === selectedModelId && left.modelId !== selectedModelId) {
        return 1;
      }
      return left.score - right.score || left.modelId.localeCompare(right.modelId) || left.index - right.index;
    })
    .slice(0, limit)
    .map((item) => ({
      modelId: item.modelId,
      modelLabel: item.modelLabel,
    }));
}

function resolveChatModelSelection(provider, rawValue) {
  const value = String(rawValue || "").trim();
  if (!value) {
    return "";
  }
  const models = Array.isArray(provider?.models) ? provider.models : [];
  if (models.includes(value)) {
    return value;
  }
  const suggestion = getChatModelSuggestions(provider, value, { limit: 1 })[0];
  if (suggestion?.modelId) {
    return suggestion.modelId;
  }
  if (value.includes("/")) {
    return value;
  }
  return value;
}

function renderChatModelSuggestionItems(provider, query) {
  const totalModels = Array.isArray(provider?.models) ? provider.models.length : 0;
  if (!totalModels) {
    return "";
  }
  const suggestions = getChatModelSuggestions(provider, query, { limit: totalModels });
  if (!suggestions.length) {
    return "";
  }
  return suggestions.map(({ modelId, modelLabel }) => {
    const isSelected = modelId === state.selectedModelId;
    const displayLabel = modelLabel || modelId;
    const displayId = displayLabel === modelId ? "" : modelId;
    return `
      <button
        type="button"
        class="chat-model-suggestion ${isSelected ? "is-selected" : ""}"
        data-action="select-chat-model"
        data-model-id="${escapeHtml(modelId)}"
        title="${escapeHtml(displayId ? `${displayLabel} · ${displayId}` : displayLabel)}"
      >
        <span class="chat-model-suggestion-main">
          <span class="chat-model-suggestion-title">${escapeHtml(displayLabel)}</span>
          ${displayId ? `<span class="chat-model-suggestion-subtitle">${escapeHtml(displayId)}</span>` : ""}
        </span>
      </button>
    `;
  }).join("");
}

function shouldShowChatModelSuggestions(query, { open = false } = {}) {
  const trimmedQuery = String(query || "").trim();
  const selectedModelId = String(state.selectedModelId || "").trim();
  return Boolean(open || (trimmedQuery && trimmedQuery !== selectedModelId));
}

function syncChatModelSuggestions() {
  const provider = getSelectedChatProvider();
  const container = document.querySelector(".chat-model-suggestions");
  if (!provider || !container) {
    return;
  }
  const query = String(state.chatModelQuery || "").trim();
  const suggestionQuery = query;
  const suggestionMarkup = renderChatModelSuggestionItems(provider, suggestionQuery);
  const shouldShow = shouldShowChatModelSuggestions(suggestionQuery, { open: state.chatModelPickerOpen }) && Boolean(suggestionMarkup);
  container.hidden = !shouldShow;
  container.innerHTML = shouldShow ? suggestionMarkup : "";
}

function renderChatModelPicker(provider) {
  const query = String(state.chatModelQuery || "").trim();
  const inputValue = query;
  const totalModels = Array.isArray(provider?.models) ? provider.models.length : 0;
  const suggestionQuery = query;
  const showSuggestions = shouldShowChatModelSuggestions(suggestionQuery, { open: state.chatModelPickerOpen });
  const suggestionMarkup = totalModels ? renderChatModelSuggestionItems(provider, suggestionQuery) : "";
  return `
    <div class="chat-model-picker">
      <div class="chat-model-picker-row">
        <input
          id="chat-model-input"
          class="context-select chat-model-input"
          type="search"
          aria-label="Select model"
          value="${escapeHtml(inputValue)}"
          placeholder="${escapeHtml(totalModels ? "Select model" : "No models available")}"
          autocomplete="off"
          spellcheck="false"
          ${totalModels && !state.chatBusy && !state.chatCancellationInProgress ? "" : "disabled"}
        />
        ${totalModels ? `
          <button
            type="button"
            class="action-button compact icon-button icon-only chat-model-toggle ${state.chatModelPickerOpen ? "is-active" : ""}"
            data-action="toggle-chat-model-picker"
            aria-label="${state.chatModelPickerOpen ? "Hide model list" : "Show model list"}"
            aria-expanded="${state.chatModelPickerOpen ? "true" : "false"}"
            title="${state.chatModelPickerOpen ? "Hide model list" : "Show model list"}"
            ${state.chatBusy || state.chatCancellationInProgress ? "disabled" : ""}
          >
            ${state.chatModelPickerOpen ? upIcon() : downIcon()}
          </button>
        ` : ""}
      </div>
      <div class="chat-model-suggestions" role="listbox" aria-label="Suggested models" ${showSuggestions && suggestionMarkup ? "" : "hidden"}>
        ${showSuggestions && suggestionMarkup ? suggestionMarkup : ""}
      </div>
    </div>
  `;
}

function commitChatModelQuery() {
  const provider = getSelectedChatProvider();
  if (!provider) {
    return;
  }
  const resolved = resolveChatModelSelection(provider, state.chatModelQuery);
  state.selectedModelId = resolved;
  state.chatModelQuery = resolved;
  if (state.selectedChatId) {
    state.chatModelSelections = {
      ...state.chatModelSelections,
      [state.selectedChatId]: resolved,
    };
  }
}

function getSelectedChatConversation() {
  return state.chatConversations.find((item) => item.conversation_id === state.selectedChatId) || null;
}

function getSelectedChatProvider() {
  return state.chatProviders.find((item) => item.provider_id === state.selectedProviderId) || null;
}

function queueChatScrollToLatest() {
  state.chatShouldScrollToLatest = true;
}

function scheduleChatRender() {
  if (chatRenderScheduled) {
    return;
  }
  chatRenderScheduled = true;
  const elapsed = window.performance.now() - chatRenderLastAt;
  const delay = elapsed >= CHAT_RENDER_THROTTLE_MS ? 0 : CHAT_RENDER_THROTTLE_MS - elapsed;
  const run = () => {
    chatRenderScheduled = false;
    chatRenderLastAt = window.performance.now();
    render();
  };
  if (delay <= 0) {
    window.requestAnimationFrame(run);
    return;
  }
  window.setTimeout(() => {
    window.requestAnimationFrame(run);
  }, delay);
}

function shouldTrackChatTurnEvent(eventType) {
  return TRACKED_CHAT_EVENT_TYPES.has(eventType);
}

async function loadChatConversation(conversationId, { preserveDraft = false } = {}) {
  clearChatConversationRefreshTimer();
  const previousConversationId = state.selectedChatId;
  const generation = state.chatConversationLoadGeneration + 1;
  state.chatConversationLoadGeneration = generation;
  state.chatConversationLoading = false;
  if (String(previousConversationId || "") !== String(conversationId || "")) {
    state.chatApprovalReplyDrafts = {};
    state.chatApprovalOtherOpenIds = new Set();
    state.chatMessages = [];
    state.chatTurnEvents = [];
    state.activeChatTurn = null;
    state.pendingChatUserMessage = null;
    state.chatBusy = false;
    state.chatCancellationInProgress = false;
    state.chatPendingMessageCount = null;
  }
  if (!conversationId) {
    state.selectedChatId = null;
    state.chatMessages = [];
    state.chatTurnEvents = [];
    state.activeChatTurn = null;
    state.pendingChatUserMessage = null;
    state.chatPendingMessageCount = null;
    state.chatSystemMessage = "";
    state.chatBusy = false;
    state.chatCancellationInProgress = false;
    if (!preserveDraft) {
      state.chatDraft = "";
    }
    syncSelectedProviderAndModel();
    render();
    return;
  }
  state.selectedChatId = conversationId;
  state.chatConversationLoading = true;
  render();
  let payload;
  try {
    payload = await invoke("get_chat_conversation", { conversationId });
  } finally {
    if (generation === state.chatConversationLoadGeneration) {
      state.chatConversationLoading = false;
      render();
    }
  }
  // Discard results that arrived after the user moved on.
  if (generation !== state.chatConversationLoadGeneration || state.selectedChatId !== conversationId) {
    return;
  }
  state.chatMessages = payload.messages || [];
  state.chatTurnEvents = Array.isArray(payload.turn_events) ? payload.turn_events.map(normalizeChatTurnEvent).filter(Boolean) : [];
  state.chatSystemMessage = "";
  state.chatConversations = state.chatConversations.map((item) => item.conversation_id === conversationId ? payload.conversation : item);
  if (!state.chatBusy || previousConversationId !== conversationId || payload.turn_state?.status !== "active") {
    state.chatBusy = false;
    state.activeChatTurn = null;
    state.pendingChatUserMessage = null;
    state.chatPendingMessageCount = null;
  }
  if (payload.turn_state?.status === "active") {
    state.chatBusy = true;
    state.activeChatTurn = { turn_id: payload.turn_state.turn_id, events: state.chatTurnEvents, phase: "thinking", pendingApproval: null };
    scheduleChatConversationRefresh(conversationId, { delay: 250 });
  }
  if (payload.history_warning) state.chatSystemMessage = payload.history_warning;
  if (!preserveDraft) {
    state.chatDraft = "";
  }
  state.editingChatConversationId = null;
  queueChatScrollToLatest();
  syncSelectedProviderAndModel();
  render();
}

async function createChatConversation() {
  const created = await invoke("create_chat_conversation", { title: null });
  const payload = await invoke("bootstrap_chat");
  hydrateChatState(payload);
  await loadChatConversation(created.conversation_id);
  state.appView = "chat";
  queueChatScrollToLatest();
  state.chatMessage = "New chat created.";
  state.error = "";
  render();
}

async function renameChatConversation(conversationId, title) {
  const renamed = await invoke("rename_chat_conversation", { conversationId, title });
  state.chatConversations = state.chatConversations.map((item) => item.conversation_id === conversationId ? renamed : item);
  state.editingChatConversationId = null;
  if (state.selectedChatId === conversationId) {
    await loadChatConversation(conversationId, { preserveDraft: true });
  } else {
    render();
  }
  state.chatMessage = `Renamed chat to "${renamed.title}".`;
  state.error = "";
  render();
}

async function deleteChatConversation(conversationId) {
  await invoke("delete_chat_conversation", { conversationId });
  const nextSelected = state.selectedChatId === conversationId
    ? state.chatConversations.find((item) => item.conversation_id !== conversationId)?.conversation_id || null
    : state.selectedChatId;
  const payload = await invoke("bootstrap_chat");
  hydrateChatState(payload);
  state.confirmation = null;
  if (nextSelected && state.chatConversations.some((item) => item.conversation_id === nextSelected)) {
    await loadChatConversation(nextSelected, { preserveDraft: true });
  } else {
    state.selectedChatId = null;
    state.chatMessages = [];
    state.chatTurnEvents = [];
    state.activeChatTurn = null;
    state.pendingChatUserMessage = null;
    state.chatPendingMessageCount = null;
    render();
  }
  state.chatMessage = "Chat deleted.";
  state.error = "";
  render();
}

async function sendChatMessageFromContent(content) {
  if (chatSendInFlight) {
    return;
  }
  if (state.chatBusy || state.chatCancellationInProgress) {
    throw new Error("Wait for the current turn to finish before sending another message.");
  }
  const rawContent = String(content || "").trim();
  const turnDirectives = parseChatTurnDirectives(rawContent);
  const parsedContent = turnDirectives.content;
  if (!state.selectedChatId) {
    throw new Error("Create a chat before sending a message.");
  }
  const provider = getSelectedChatProvider();
  if (!provider) {
    throw new Error("Add or select a provider before sending a message.");
  }
  const resolvedModelId = resolveChatModelSelection(provider, state.chatModelQuery);
  if (!resolvedModelId) {
    throw new Error("Select a model before sending a message.");
  }
  const resolvedAgentId = String(state.selectedAgentId || "").trim();
  if (!resolvedAgentId) {
    throw new Error("Select an agent before sending a message.");
  }
  state.selectedModelId = resolvedModelId;
  state.chatModelQuery = resolvedModelId;
  if (state.selectedChatId && resolvedModelId) {
    state.chatModelSelections = {
      ...state.chatModelSelections,
      [state.selectedChatId]: resolvedModelId,
    };
  }
  state.selectedAgentId = resolvedAgentId;
  if (state.selectedChatId) {
    state.chatAgentSelections = {
      ...state.chatAgentSelections,
      [state.selectedChatId]: resolvedAgentId,
    };
  }
  if (!parsedContent) {
    throw new Error("Add a question after the command directives.");
  }
  chatSendInFlight = true;
  startChatTurnRequest();
  state.activeChatTurn = {
    turn_id: "",
    events: [],
    phase: "thinking",
    attachedSkills: [],
    pendingApproval: null,
    completed: false,
    failed: false,
    cancelled: false
  };
  state.chatSystemMessage = "";
  state.chatMessage = "";
  state.chatDraft = "";
  state.chatPendingMessageCount = state.chatMessages.length;
  const pendingUserMessage = {
    message_id: `pending_${Date.now()}`,
    conversation_id: state.selectedChatId,
    role: "user",
    content_markdown: parsedContent,
    tool_calls_json: null,
    provider_id: state.selectedProviderId,
    model_id: resolvedModelId,
    created_at: new Date().toISOString(),
    position: (state.chatMessages.at(-1)?.position || 0) + 1
  };
  state.pendingChatUserMessage = pendingUserMessage;
  queueChatScrollToLatest();
  render();
  try {
    const payload = await invoke("send_chat_message", {
      conversationId: state.selectedChatId,
      providerId: state.selectedProviderId,
      modelId: resolvedModelId,
      agentId: resolvedAgentId,
      content: parsedContent,
      pipelineMode: turnDirectives.pipelineMode,
      inputDataRefs: turnDirectives.inputDataRefs
    });
    state.chatDraft = "";
    if (payload.accepted) {
      if (state.activeChatTurn) state.activeChatTurn.turn_id = payload.turn_id;
      if (state.pendingChatUserMessage) state.pendingChatUserMessage.turn_id = payload.turn_id;
      scheduleChatConversationRefresh(payload.conversation_id, { delay: 750 });
      state.error = "";
      return;
    }
    state.chatMessages = payload.messages || [];
    state.chatTurnEvents = Array.isArray(payload.turn_events) ? payload.turn_events.map(normalizeChatTurnEvent).filter(Boolean) : [];
    const persistedCount = Array.isArray(state.chatMessages) ? state.chatMessages.length : 0;
    state.pendingChatUserMessage = persistedCount > state.chatPendingMessageCount ? null : pendingUserMessage;
    state.chatConversations = [payload.conversation, ...state.chatConversations.filter((item) => item.conversation_id !== payload.conversation.conversation_id)];
    state.selectedChatId = payload.conversation.conversation_id;
    queueChatScrollToLatest();
    syncSelectedProviderAndModel();
    const currentTurnId = persistedCount > state.chatPendingMessageCount
      ? ([...state.chatMessages].reverse().find((message) => message?.role === "user")?.turn_id || "")
      : "";
    const payloadHasTerminalEvent = Boolean(currentTurnId) && Array.isArray(state.chatTurnEvents) && state.chatTurnEvents.some((event) => (
      event.conversation_id === payload.conversation.conversation_id &&
      event.turn_id === currentTurnId &&
      ["turn.completed", "turn.failed", "turn.cancelled"].includes(event.event_type)
    ));
    if (payloadHasTerminalEvent) {
      state.activeChatTurn = null;
      state.chatBusy = false;
      state.chatMessage = "";
      state.chatPendingMessageCount = null;
    } else {
      state.chatBusy = true;
      scheduleChatConversationRefresh(state.selectedChatId, { delay: 750 });
    }
    state.error = "";
  } catch (error) {
    state.chatDraft = parsedContent;
    state.pendingChatUserMessage = null;
    state.activeChatTurn = null;
    state.chatPendingMessageCount = null;
    state.chatBusy = false;
    const wasCancelled = state.chatCancellationInProgress && state.chatCancellationConversationId === state.selectedChatId;
    if (!wasCancelled && state.selectedChatId) {
      try {
        const payload = await invoke("get_chat_conversation", { conversationId: state.selectedChatId });
        state.chatMessages = payload.messages || [];
        state.chatTurnEvents = Array.isArray(payload.turn_events) ? payload.turn_events.map(normalizeChatTurnEvent).filter(Boolean) : [];
        state.chatConversations = state.chatConversations.map((item) => item.conversation_id === state.selectedChatId ? payload.conversation : item);
      } catch {
        // ignore secondary refresh errors and surface the original failure
      }
    }
    if (wasCancelled) {
      state.chatBusy = false;
      state.chatMessage = "Request cancelled.";
      state.error = "";
      return;
    }
    throw error;
  } finally {
    state.chatCancellationInProgress = false;
    state.chatCancellationConversationId = null;
    chatSendInFlight = false;
    render();
  }
}

async function sendChatMessage(form) {
  const rawContent = String(new FormData(form).get("content") || state.chatDraft || "").trim();
  return sendChatMessageFromContent(rawContent);
}

async function cancelChatRequest() {
  const conversationId = state.selectedChatId;
  if (!conversationId || (!state.chatBusy && !state.chatCancellationInProgress)) {
    return;
  }
  startChatTurnRequest();
  state.chatCancellationInProgress = true;
  state.chatCancellationConversationId = conversationId;
  if (state.activeChatTurn) {
    state.activeChatTurn.phase = "cancelling";
  }
  state.chatMessage = "Stopping OpenCode request...";
  render();
  try {
    const conversation = await invoke("cancel_chat_request", { conversationId });
    if (conversation?.conversation_id && state.selectedChatId === conversation.conversation_id) {
      state.chatConversations = state.chatConversations.map((item) => (
        item.conversation_id === conversation.conversation_id ? conversation : item
      ));
    }
    state.chatBusy = false;
    state.chatPendingMessageCount = null;
    state.chatMessage = "Request cancelled.";
    state.error = "";
    render();
  } catch (error) {
    state.error = formatError(error);
    render();
  } finally {
    state.chatCancellationInProgress = false;
    state.chatCancellationConversationId = null;
  }
}

async function resolveChatToolApproval(approvalId, action) {
  const conversation = await invoke("resolve_chat_tool_approval", {
    approvalId,
    action
  });
  state.chatConversations = state.chatConversations.map((item) => item.conversation_id === conversation.conversation_id ? conversation : item);
  if (state.selectedChatId === conversation.conversation_id && state.activeChatTurn) {
    state.activeChatTurn.pendingApproval = null;
  }
  clearChatApprovalOtherState(String(approvalId || "").trim());
  render();
}

async function resetChatConversationToolApproval(conversationId) {
  const conversation = await invoke("reset_chat_conversation_tool_approval", {
    conversationId
  });
  state.chatConversations = state.chatConversations.map((item) => item.conversation_id === conversation.conversation_id ? conversation : item);
  if (state.selectedChatId === conversation.conversation_id) {
    render();
  }
}

function startRenameChatConversation(conversationId) {
  state.editingChatConversationId = conversationId;
  render();
}

function cancelRenameChatConversation() {
  state.editingChatConversationId = null;
  render();
}

function promptDeleteChatConversation(conversationId) {
  const conversation = state.chatConversations.find((item) => item.conversation_id === conversationId);
  state.confirmation = {
    kind: "delete-chat-conversation",
    conversationId,
    title: conversation?.title || "this chat"
  };
  render();
}

async function refreshSessions(query = state.sessionQuery) {
  state.sessionQuery = query;
  const payload = await invoke("list_sessions", { query });
  state.sessions = payload.sessions || [];
  if (state.selectedSessionId && !state.sessions.some((item) => item.session_id === state.selectedSessionId)) {
    state.selectedSessionId = state.sessions[0]?.session_id || null;
    state.selectedArtifactId = null;
    state.artifactDetail = null;
    state.selectedUploadId = null;
    state.uploadDetail = null;
    state.selectedReportId = null;
    state.reports = [];
    state.reportDetail = null;
    state.sessionSummary = null;
    state.artifacts = [];
    state.uploads = [];
  }
  if (!state.selectedSessionId && state.sessions[0]) {
    state.selectedSessionId = state.sessions[0].session_id;
  }
  if (!state.selectedSessionId) {
    state.showEditSessionForm = false;
  }
  render();
}

async function refreshCohorts(query = state.cohortQuery, { force = false } = {}) {
  state.cohortQuery = query;
  if (state.cohortsLoaded && !force) {
    render();
    return;
  }
  const payload = await invoke("list_cohorts");
  state.cohorts = payload.cohorts || [];
  state.cohortsLoaded = true;
  if (state.selectedCohortId && !state.cohorts.some((item) => item.cohort_id === state.selectedCohortId)) {
    state.selectedCohortId = state.cohorts[0]?.cohort_id || null;
  }
  if (!state.selectedCohortId && state.cohorts[0]) {
    state.selectedCohortId = state.cohorts[0].cohort_id;
  }
  render();
}

async function refreshCohortLibrary({ force = false } = {}) {
  await refreshCohortProviders({ force });
  await refreshCohortCatalog(state.selectedCohortProviderId, { force });
  await refreshCohorts(state.cohortQuery, { force });
}

/**
 * Load the cohort library at most once per session, sharing any in-flight
 * request. Re-entering Cohorts with a populated cache performs no calls.
 */
function ensureCohortLibraryLoaded() {
  if (state.cohortsLoaded && state.cohortProvidersLoaded && state.cohortCatalogLoadedProviderId === state.selectedCohortProviderId) {
    return Promise.resolve();
  }
  if (cohortLibraryPromise) {
    return cohortLibraryPromise;
  }
  state.cohortLibraryBusy = true;
  render();
  cohortLibraryPromise = refreshCohortLibrary()
    .catch((error) => {
      state.error = formatError(error);
    })
    .finally(() => {
      cohortLibraryPromise = null;
      state.cohortLibraryBusy = false;
      render();
    });
  return cohortLibraryPromise;
}

async function refreshCohortProviders({ force = false } = {}) {
  if (state.cohortProvidersLoaded && !force) {
    if (!state.selectedCohortProviderId || !state.cohortProviders.some((item) => item.provider_id === state.selectedCohortProviderId)) {
      state.selectedCohortProviderId = state.cohortProviders[0]?.provider_id || "tcga";
    }
    render();
    return;
  }
  state.cohortProviderBusy = true;
  try {
    const payload = await invoke("list_cohort_providers");
    state.cohortProviders = payload.providers || [];
    state.cohortProvidersLoaded = true;
    if (!state.selectedCohortProviderId || !state.cohortProviders.some((item) => item.provider_id === state.selectedCohortProviderId)) {
      state.selectedCohortProviderId = state.cohortProviders[0]?.provider_id || "tcga";
    }
  } finally {
    state.cohortProviderBusy = false;
  }
  render();
}

async function refreshCohortCatalog(providerId = state.selectedCohortProviderId, { force = false } = {}) {
  if (!providerId) {
    state.cohortCatalogBusy = false;
    state.cohortCatalog = [];
    state.cohortCatalogLoadedProviderId = "";
    render();
    return;
  }
  if (state.cohortCatalogLoadedProviderId === providerId && !force) {
    syncCohortSelectionWithTissue();
    render();
    return;
  }
  state.cohortCatalogBusy = true;
  try {
    const payload = await invoke("cohort_catalog", { providerId });
    state.cohortCatalog = payload.datasets || [];
    state.cohortCatalogLoadedProviderId = providerId;
    if (!state.selectedCohortDatasetId || !state.cohortCatalog.some((item) => item.dataset_id === state.selectedCohortDatasetId)) {
      state.selectedCohortDatasetId = state.cohortCatalog[0]?.dataset_id || "";
    }
  } finally {
    state.cohortCatalogBusy = false;
  }
  syncCohortSelectionWithTissue();
  render();
}

function clearCohortDownloadPoll() {
  if (state.cohortDownloadPollId) {
    window.clearInterval(state.cohortDownloadPollId);
    state.cohortDownloadPollId = null;
  }
}

function isCohortDownloadTerminal(job) {
  const status = String(job?.status || "").toLowerCase();
  return ["completed", "failed", "cancelled"].includes(status);
}

function isCohortDownloadLocked() {
  return state.cohortDownloadStarting || Boolean(state.cohortDownloadJob && !isCohortDownloadTerminal(state.cohortDownloadJob) && !state.cohortDownloadObservationError);
}

function syncCohortDownloadClock() {
  const active = state.cohortDownloadStarting || (state.cohortDownloadJob && !isCohortDownloadTerminal(state.cohortDownloadJob));
  cohortDownloadClock?.update({ job: state.cohortDownloadStarting ? null : state.cohortDownloadJob,
    startedAt: state.cohortDownloadStartedAt, finishedAt: state.cohortDownloadFinishedAt,
    running: Boolean(active && !document.hidden && state.appView === "cohorts" && state.cohortTab === "download") });
}

function cohortDownloadEtaText(job) {
  if (isCohortDownloadTerminal(job)) return "";
  const startedAt = job?.started_at ? new Date(job.started_at) : null;
  const percent = Number(job?.progress_percent || 0);
  if (!startedAt || Number.isNaN(startedAt.getTime()) || percent <= 0 || percent >= 99.5) {
    return "";
  }
  const elapsedSeconds = Math.max(0, (Date.now() - startedAt.getTime()) / 1000);
  const totalSeconds = elapsedSeconds * (100 / percent);
  const remainingSeconds = Math.max(0, Math.round(totalSeconds - elapsedSeconds));
  if (!Number.isFinite(remainingSeconds) || remainingSeconds <= 0) {
    return "";
  }
  const minutes = Math.floor(remainingSeconds / 60);
  const seconds = remainingSeconds % 60;
  return minutes > 0 ? `~${minutes}m ${seconds}s remaining` : `~${seconds}s remaining`;
}

function cohortDownloadRenderKey(job) {
  if (!job) {
    return "";
  }
  return JSON.stringify({
    status: String(job.status || ""),
    stage: String(job.stage || ""),
    event_status: String(job.event_status || ""),
    progress_percent: Number.isFinite(Number(job.progress_percent)) ? Number(job.progress_percent) : null,
    samples_downloaded: job.samples_downloaded ?? null,
    samples_total: job.samples_total ?? null,
    samples_total_all: job.samples_total_all ?? null,
    eta_seconds: job.eta_seconds ?? null,
    updated_at: String(job.updated_at || ""),
    finished_at: String(job.finished_at || ""),
    error: String(job.error || ""),
    log_length: Array.isArray(job.log) ? job.log.length : 0,
    result_cohort_id: job.result?.cohort_id || job.result?.cohort?.cohort_id || null
  });
}

function cohortDownloadStageLabel(job) {
  const stage = String(job?.stage || "");
  const eventStatus = String(job?.event_status || "");
  if (stage) {
    return stage;
  }
  if (eventStatus === "completed") {
    return "Completed";
  }
  if (eventStatus === "cancelled") {
    return "Cancelled";
  }
  return "Working";
}

async function refreshCohortDownloadJob(jobId = state.cohortDownloadJob?.job_id || "") {
  if (!jobId || state.cohortDownloadRefreshing || state.cohortDownloadCancelling || state.cohortDownloadJob?.job_id !== jobId) return;
  state.cohortDownloadRefreshing = true;
  if (state.cohortDownloadObservationError) render();
  const generation = state.cohortDownloadGeneration;
  try {
    const job = await invoke("get_cohort_download_job", { jobId });
    if (generation !== state.cohortDownloadGeneration || state.cohortDownloadJob?.job_id !== jobId) return;
    await applyCohortDownloadSnapshot(job);
  } finally { state.cohortDownloadRefreshing = false; }
}

async function applyCohortDownloadSnapshot(job) {
  const generation = state.cohortDownloadGeneration;
  const previousRenderKey = cohortDownloadRenderKey(state.cohortDownloadJob);
  const alreadyTerminal = state.cohortDownloadJob?.job_id === job.job_id && isCohortDownloadTerminal(state.cohortDownloadJob);
  const recovered = Boolean(state.cohortDownloadObservationError);
  state.cohortDownloadJob = job;
  state.cohortDownloadObservationError = "";
  const nextRenderKey = cohortDownloadRenderKey(job);
  if (isCohortDownloadTerminal(job)) {
    clearCohortDownloadPoll();
    state.cohortBusy = false;
    state.cohortDownloadStarting = false;
    state.cohortDownloadFinishedAt ||= Date.now();
    render(); // Unlock the form and stop the clock before refreshing the library.
    if (alreadyTerminal) return;
    if (job.status === "completed") {
      const resultCohortId = job.result?.cohort_id || job.result?.cohort?.cohort_id || null;
      state.message = `Downloaded cohort "${job.title}".`;
      state.error = "";
      await refreshCohorts(state.cohortQuery, { force: true });
      if (resultCohortId && generation === state.cohortDownloadGeneration && state.cohortDownloadJob?.job_id === job.job_id) {
        await selectCohort(resultCohortId);
      }
    } else if (job.status === "cancelled") {
      state.message = "";
      state.cohortNotice = { kind: "warning", message: "Cohort download cancelled." };
      state.error = "";
    } else if (job.status === "failed") {
      state.error = job.error || "Cohort download failed.";
    }
  }
  if (!recovered && previousRenderKey === nextRenderKey && !isCohortDownloadTerminal(job)) {
    return;
  }
  render();
}

function startCohortDownloadPolling(jobId) {
  clearCohortDownloadPoll();
  if (!jobId) return;
  state.cohortDownloadPollId = window.setInterval(() => {
    const generation = state.cohortDownloadGeneration;
    void refreshCohortDownloadJob(jobId).catch((error) => {
      if (state.cohortDownloadJob?.job_id !== jobId || generation !== state.cohortDownloadGeneration) return;
      clearCohortDownloadPoll();
      state.cohortBusy = false;
      state.cohortDownloadObservationError = isCohortDownloadTerminal(state.cohortDownloadJob) ? "" : "Download status is unavailable. It may still be running. Retry status or Cancel before starting another download; no request was repeated.";
      state.error = formatError(error);
      render();
    });
  }, 1200);
}

async function cancelCohortDownload() {
  const jobId = state.cohortDownloadJob?.job_id;
  if (!jobId || state.cohortDownloadCancelling || isCohortDownloadTerminal(state.cohortDownloadJob)) {
    return;
  }
  state.cohortDownloadCancelling = true;
  state.cohortDownloadGeneration += 1; // Invalidate snapshots issued before Cancel.
  const generation = state.cohortDownloadGeneration;
  render();
  try {
    const payload = await invoke("cancel_cohort_download_job", { jobId });
    if (state.cohortDownloadJob?.job_id !== jobId || generation !== state.cohortDownloadGeneration) return;
    await applyCohortDownloadSnapshot(payload);
    if (generation === state.cohortDownloadGeneration && !isCohortDownloadTerminal(payload)) startCohortDownloadPolling(jobId);
  } catch (error) {
    if (generation === state.cohortDownloadGeneration && !isCohortDownloadTerminal(state.cohortDownloadJob)) {
      clearCohortDownloadPoll();
      state.cohortDownloadObservationError = "Cancellation could not be confirmed. The download may still be running. Retry status or Cancel before starting another download.";
    }
    throw error;
  } finally {
    if (generation === state.cohortDownloadGeneration) { state.cohortDownloadCancelling = false; render(); }
  }
}

async function retryCohortDownloadStatus() {
  state.error = "";
  await refreshCohortDownloadJob();
  if (state.cohortDownloadJob && !isCohortDownloadTerminal(state.cohortDownloadJob)) startCohortDownloadPolling(state.cohortDownloadJob.job_id);
}

async function selectCohort(cohortId) {
  if (!cohortId) {
    state.selectedCohortId = null;
    render();
    return;
  }
  // Selection is render-only: the detail payload is not displayed anywhere, so
  // fetching it would cost a CLI process for nothing.
  state.selectedCohortId = cohortId;
  render();
}

/**
 * Load one session's summary, artifacts, and uploads in a single CLI call.
 *
 * Race-safe: the generation counter is bumped before the request, and results
 * are applied only when both the generation and the selected session still
 * match. Stale responses are discarded silently.
 */
async function ensureSessionSnapshot(sessionId, { force = false } = {}) {
  if (!sessionId) {
    return null;
  }
  if (!force && state.sessionSnapshotLoadedId === sessionId && state.sessionSnapshotStatus === "ready") {
    return null;
  }
  const generation = state.sessionLoadGeneration + 1;
  state.sessionLoadGeneration = generation;
  state.sessionSnapshotStatus = "loading";
  state.sessionSnapshotError = "";
  render();
  try {
    const payload = await invoke("get_session_snapshot", { sessionId });
    if (generation !== state.sessionLoadGeneration || state.selectedSessionId !== sessionId) {
      return null;
    }
    state.sessionSummary = payload?.summary || null;
    state.artifacts = payload?.artifacts?.artifacts || [];
    state.uploads = payload?.uploads?.uploads || payload?.uploads?.artifacts || [];
    state.sessionSnapshotLoadedId = sessionId;
    state.sessionSnapshotStatus = "ready";
    state.sessionSnapshotError = "";
    render();
    return payload;
  } catch (error) {
    if (generation !== state.sessionLoadGeneration || state.selectedSessionId !== sessionId) {
      return null;
    }
    state.sessionSnapshotStatus = "failed";
    state.sessionSnapshotError = formatError(error);
    render();
    throw error;
  }
}

function retrySessionSnapshot() {
  if (!state.selectedSessionId) {
    return;
  }
  void ensureSessionSnapshot(state.selectedSessionId, { force: true }).catch(() => {
    // Failure is already surfaced through sessionSnapshotStatus.
  });
}

async function selectSession(sessionId) {
  if (!sessionId) {
    state.selectedSessionId = null;
    state.selectedArtifactId = null;
    state.artifactDetail = null;
    state.selectedUploadId = null;
    state.uploadDetail = null;
    state.selectedFigureId = null;
    state.figureDetail = null;
    state.selectedReportId = null;
    state.reports = [];
    state.reportDetail = null;
    state.selectedNoteId = null;
    state.noteDetail = null;
    state.notes = [];
    state.sessionSummary = null;
    state.artifacts = [];
    state.uploads = [];
    state.notesLoadedSessionId = null;
    state.reportsLoadedSessionId = null;
    state.sessionSnapshotLoadedId = null;
    state.sessionSnapshotStatus = "idle";
    state.sessionSnapshotError = "";
    state.showEditSessionForm = false;
    render();
    return;
  }
  state.selectedSessionId = sessionId;
  state.showEditSessionForm = false;
  state.selectedArtifactId = null;
  state.artifactDetail = null;
  state.selectedUploadId = null;
  state.uploadDetail = null;
  state.selectedFigureId = null;
  state.figureDetail = null;
  state.selectedReportId = null;
  state.reports = [];
  state.reportDetail = null;
  state.selectedNoteId = null;
  state.noteDetail = null;
  state.notes = [];
  state.notesLoadedSessionId = null;
  state.reportsLoadedSessionId = null;
  // Render the selection and its loading state before awaiting the CLI.
  render();
  await ensureSessionSnapshot(sessionId);
}

async function openArtifact(sessionId, artifactId) {
  if (state.selectedSessionId !== sessionId) {
    await selectSession(sessionId);
  }
  state.page = "artifacts";
  state.selectedArtifactId = artifactId;
  state.artifactDetail = await invoke("get_artifact", { sessionId, artifactId });
  render();
}

async function openUpload(sessionId, uploadId) {
  if (state.selectedSessionId !== sessionId) {
    await selectSession(sessionId);
  }
  state.page = "uploads";
  state.selectedUploadId = uploadId;
  state.uploadDetail = await invoke("get_upload", { sessionId, uploadId });
  render();
}

async function openFigure(sessionId, artifactId) {
  if (state.selectedSessionId !== sessionId) {
    await selectSession(sessionId);
  }
  state.page = "figures";
  state.selectedFigureId = artifactId;
  state.figureDetail = await invoke("get_artifact", { sessionId, artifactId });
  render();
}

function hydrateWorkspaceState(payload, { preferRuntimeSession = false } = {}) {
  state.paths = payload?.paths || state.paths;
  state.referenceStatus = payload?.reference_status || state.referenceStatus;
  state.sessions = payload?.sessions?.sessions || [];
  state.runtime = {
    ...DEFAULT_RUNTIME,
    ...(payload?.runtime || {})
  };
  const runtimeSessionId = state.runtime.active_session_id;
  const runtimeSessionExists = runtimeSessionId
    ? state.sessions.some((item) => item.session_id === runtimeSessionId)
    : false;
  if (preferRuntimeSession) {
    state.selectedSessionId = runtimeSessionExists
      ? runtimeSessionId
      : state.sessions[0]?.session_id || null;
    return;
  }
  if (state.selectedSessionId && state.sessions.some((item) => item.session_id === state.selectedSessionId)) {
    return;
  }
  state.selectedSessionId = runtimeSessionExists
    ? runtimeSessionId
    : state.sessions[0]?.session_id || null;
}

function normalizeRuntimeSession() {
  const runtimeSessionId = state.runtime.active_session_id;
  if (!runtimeSessionId) {
    return;
  }
  const runtimeSessionExists = state.sessions.some((item) => item.session_id === runtimeSessionId);
  if (!runtimeSessionExists && state.selectedSessionId === runtimeSessionId) {
    state.selectedSessionId = state.sessions[0]?.session_id || null;
  }
}

async function createSession(form) {
  const data = new FormData(form);
  const title = String(data.get("title") || "").trim();
  const description = String(data.get("description") || "").trim();
  const species = String(data.get("species") || "human").trim();
  if (!title || !description) {
    state.error = "Title and description are required.";
    render();
    return;
  }
  const created = await invoke("create_session", { title, description, species, tags: [] });
  state.message = `Created session "${title}".`;
  state.error = "";
  state.page = "workspace";
  state.showCreateForm = false;
  state.showEditSessionForm = false;
  form.reset();
  await refreshSessions();
  if (created?.session_id) {
    await selectSession(created.session_id);
  }
}

async function updateSessionMetadata(form) {
  const data = new FormData(form);
  const sessionId = String(data.get("session_id") || "").trim();
  const title = String(data.get("title") || "").trim();
  const description = String(data.get("description") || "").trim();
  if (!sessionId) {
    state.error = "Session not found.";
    render();
    return;
  }
  if (!title || !description) {
    state.error = "Session title and description are required.";
    render();
    return;
  }
  const updated = await invoke("update_session", {
    sessionId,
    title,
    description
  });
  state.message = `Updated session "${updated?.title || title}".`;
  state.error = "";
  state.showEditSessionForm = false;
  await refreshSessions();
  await selectSession(sessionId);
}

async function refreshReferenceStatus() {
  state.referenceOperation = "checking";
  render();
  try {
    state.referenceStatus = await invoke("reference_status");
  } finally {
    state.referenceOperation = "idle";
    render();
  }
}

function promptDeleteSession(sessionId) {
  const session = state.sessions.find((item) => item.session_id === sessionId);
  if (!session) {
    state.error = "Session not found.";
    render();
    return;
  }
  state.confirmation = {
    kind: "delete-session",
    sessionId,
    title: session.title || sessionId
  };
  render();
}

function clearConfirmation() {
  state.confirmation = null;
  render();
}

async function deleteSession(sessionId) {
  const payload = await invoke("delete_sessions", { sessionIds: [sessionId] });
  if (!payload?.deleted_session_ids?.includes(sessionId)) {
    throw new Error(`Session ${sessionId} could not be deleted.`);
  }
  if (state.selectedSessionId === sessionId) {
    state.page = "workspace";
    state.showEditSessionForm = false;
  }
  if (state.runtime.active_session_id === sessionId) {
    state.runtime.active_session_id = null;
    state.runtime.capture_enabled = false;
    await writeHeartbeat();
  }
  state.selectedArtifacts = new Set(
    [...state.selectedArtifacts].filter((token) => !token.startsWith(`${sessionId}:`))
  );
  state.selectedNotes = new Set(
    [...state.selectedNotes].filter((token) => !token.startsWith(`${sessionId}:`))
  );
  state.selectedReports = new Set(
    [...state.selectedReports].filter((token) => !token.startsWith(`${sessionId}:`))
  );
  state.selectedSessionId = state.selectedSessionId === sessionId ? null : state.selectedSessionId;
  state.selectedArtifactId = null;
  state.artifactDetail = null;
  state.selectedUploadId = null;
  state.uploadDetail = null;
  state.uploads = [];
  state.selectedFigureId = null;
  state.figureDetail = null;
  state.selectedReportId = null;
  state.reports = [];
  state.reportDetail = null;
  state.selectedNoteId = null;
  state.noteDetail = null;
  state.confirmation = null;
  state.message = `Deleted ${sessionId}.`;
  state.error = "";
  await refreshSessions();
  if (state.selectedSessionId) {
    await selectSession(state.selectedSessionId);
  } else {
    render();
  }
}

async function toggleActiveSession(sessionId) {
  if (state.runtime.active_session_id === sessionId) {
    state.runtime.active_session_id = null;
    state.runtime.capture_enabled = false;
    state.message = "Chat is back in stateless mode.";
    await writeHeartbeat();
  } else {
    state.runtime.active_session_id = sessionId;
    state.runtime.capture_enabled = true;
    state.message = `Chat is now connected to ${sessionId}.`;
    await writeHeartbeat();
  }
  state.error = "";
  render();
}

function toggleArtifactSelection(sessionId, artifactId) {
  const token = `${sessionId}:${artifactId}`;
  if (state.selectedArtifacts.has(token)) {
    state.selectedArtifacts.delete(token);
  } else {
    state.selectedArtifacts.add(token);
  }
  render();
}

function toggleNoteSelection(sessionId, noteId) {
  const token = `${sessionId}:${noteId}`;
  if (state.selectedNotes.has(token)) {
    state.selectedNotes.delete(token);
  } else {
    state.selectedNotes.add(token);
  }
  render();
}

function toggleReportSelection(sessionId, reportId) {
  const token = `${sessionId}:${reportId}`;
  if (state.selectedReports.has(token)) {
    state.selectedReports.delete(token);
  } else {
    state.selectedReports.add(token);
  }
  render();
}

function selectedArtifactsForSession(sessionId) {
  if (!sessionId) {
    return [];
  }
  return [...state.selectedArtifacts]
    .filter((token) => token.startsWith(`${sessionId}:`))
    .map((token) => token.slice(sessionId.length + 1));
}

function selectedNotesForSession(sessionId) {
  if (!sessionId) {
    return [];
  }
  return [...state.selectedNotes]
    .filter((token) => token.startsWith(`${sessionId}:`))
    .map((token) => token.slice(sessionId.length + 1));
}

function selectedReportsForSession(sessionId) {
  if (!sessionId) {
    return [];
  }
  return [...state.selectedReports]
    .filter((token) => token.startsWith(`${sessionId}:`))
    .map((token) => token.slice(sessionId.length + 1));
}

function allArtifactsSelected(sessionId, artifacts) {
  if (!sessionId || !artifacts.length) {
    return false;
  }
  const selected = new Set(selectedArtifactsForSession(sessionId));
  return artifacts.every((artifact) => selected.has(artifact.artifact_id));
}

function allNotesSelected(sessionId, notes) {
  if (!sessionId || !notes.length) {
    return false;
  }
  const selected = new Set(selectedNotesForSession(sessionId));
  return notes.every((note) => selected.has(note.note_id));
}

function allReportsSelected(sessionId, reports) {
  if (!sessionId || !reports.length) {
    return false;
  }
  const selected = new Set(selectedReportsForSession(sessionId));
  return reports.every((report) => selected.has(report.report_id));
}

function toggleAllArtifacts(sessionId, artifacts) {
  if (!sessionId || !artifacts.length) {
    return;
  }
  const selectAll = !allArtifactsSelected(sessionId, artifacts);
  for (const artifact of artifacts) {
    const token = `${sessionId}:${artifact.artifact_id}`;
    if (selectAll) {
      state.selectedArtifacts.add(token);
    } else {
      state.selectedArtifacts.delete(token);
    }
  }
  render();
}

function toggleAllNotes(sessionId, notes) {
  if (!sessionId || !notes.length) {
    return;
  }
  const selectAll = !allNotesSelected(sessionId, notes);
  for (const note of notes) {
    const token = `${sessionId}:${note.note_id}`;
    if (selectAll) {
      state.selectedNotes.add(token);
    } else {
      state.selectedNotes.delete(token);
    }
  }
  render();
}

function toggleAllReports(sessionId, reports) {
  if (!sessionId || !reports.length) {
    return;
  }
  const selectAll = !allReportsSelected(sessionId, reports);
  for (const report of reports) {
    const token = `${sessionId}:${report.report_id}`;
    if (selectAll) {
      state.selectedReports.add(token);
    } else {
      state.selectedReports.delete(token);
    }
  }
  render();
}

function promptDeleteArtifacts(sessionId, explicitArtifactIds = null, targetPage = "artifacts") {
  const session = state.sessions.find((item) => item.session_id === sessionId);
  const artifactIds = explicitArtifactIds || selectedArtifactsForSession(sessionId);
  if (!session) {
    state.error = "Session not found.";
    render();
    return;
  }
  if (!artifactIds.length) {
    state.error = "Select at least one artifact to delete.";
    render();
    return;
  }
  state.confirmation = {
    kind: "delete-artifacts",
    sessionId,
    title: session.title || sessionId,
    artifactIds,
    targetPage
  };
  render();
}

function promptDeleteNotes(sessionId, explicitNoteIds = null) {
  const session = state.sessions.find((item) => item.session_id === sessionId);
  const noteIds = explicitNoteIds || selectedNotesForSession(sessionId);
  if (!session) {
    state.error = "Session not found.";
    render();
    return;
  }
  if (!noteIds.length) {
    state.error = "Select at least one note to delete.";
    render();
    return;
  }
  state.confirmation = {
    kind: "delete-notes",
    sessionId,
    title: session.title || sessionId,
    noteIds
  };
  render();
}

function promptDeleteReports(sessionId, explicitReportIds = null) {
  const session = state.sessions.find((item) => item.session_id === sessionId);
  const reportIds = explicitReportIds || selectedReportsForSession(sessionId);
  if (!session) {
    state.error = "Session not found.";
    render();
    return;
  }
  if (!reportIds.length) {
    state.error = "Select at least one report to delete.";
    render();
    return;
  }
  state.confirmation = {
    kind: "delete-reports",
    sessionId,
    title: session.title || sessionId,
    reportIds
  };
  render();
}

async function createSessionFromSelectedArtifacts(form) {
  const data = new FormData(form);
  const title = String(data.get("title") || "").trim();
  const description = String(data.get("description") || "").trim();
  const species = String(data.get("species") || "human").trim();
  const sources = [...state.selectedArtifacts].map((token) => {
    const [session_id, artifact_id] = token.split(":");
    return { session_id, artifact_id };
  });
  if (!title || !description) {
    state.error = "A title and description are required for the new session.";
    render();
    return;
  }
  if (!sources.length) {
    state.error = "Select at least one artifact first.";
    render();
    return;
  }
  const created = await invoke("create_session_from_artifacts", {
    title,
    description,
    species,
    sources,
    tags: []
  });
  state.message = `Created "${title}" from ${sources.length} artifact(s).`;
  state.error = "";
  state.page = "workspace";
  state.selectedArtifacts.clear();
  state.showDeriveForm = false;
  form.reset();
  await refreshSessions();
  if (created?.session_id) {
    await selectSession(created.session_id);
  }
}

async function deleteSelectedArtifacts(sessionId, artifactIds, targetPage = "artifacts") {
  const uniqueArtifactIds = [...new Set((artifactIds || []).filter(Boolean))];
  if (!uniqueArtifactIds.length) {
    throw new Error("No artifacts were selected for deletion.");
  }
  const payload = await invoke("delete_session_artifacts", {
    sessionId,
    artifactIds: uniqueArtifactIds
  });
  const deleted = payload?.deleted_artifact_ids || [];
  if (deleted.length !== uniqueArtifactIds.length) {
    throw new Error("Some selected artifacts could not be deleted.");
  }
  state.selectedArtifacts = new Set(
    [...state.selectedArtifacts].filter((token) => {
      const [tokenSessionId, artifactId] = token.split(":");
      return tokenSessionId !== sessionId || !deleted.includes(artifactId);
    })
  );
  if (state.selectedArtifactId && deleted.includes(state.selectedArtifactId)) {
    state.selectedArtifactId = null;
    state.artifactDetail = null;
  }
  if (state.selectedFigureId && deleted.includes(state.selectedFigureId)) {
    state.selectedFigureId = null;
    state.figureDetail = null;
  }
  state.confirmation = null;
  state.message = `Deleted ${deleted.length} artifact${deleted.length === 1 ? "" : "s"}.`;
  state.error = "";
  await selectSession(sessionId);
  state.page = targetPage;
  if (targetPage === "figures") {
    const figures = figureArtifactsForSession(state.artifacts);
    if (state.selectedFigureId && figures.some((item) => item.artifact_id === state.selectedFigureId)) {
      await openFigure(sessionId, state.selectedFigureId);
      return;
    }
    if (figures[0]) {
      await openFigure(sessionId, figures[0].artifact_id);
      return;
    }
    state.selectedFigureId = null;
    state.figureDetail = null;
  }
  render();
}

async function deleteUpload(sessionId, uploadId) {
  const token = String(uploadId || "").trim();
  if (!token) {
    throw new Error("No upload was selected for deletion.");
  }
  const nextUploadId = state.selectedUploadId && state.selectedUploadId !== token ? state.selectedUploadId : null;
  const payload = await invoke("delete_session_uploads", {
    sessionId,
    uploadIds: [token]
  });
  const deleted = payload?.deleted_artifact_ids || [];
  if (!deleted.includes(token)) {
    throw new Error("The selected upload could not be deleted.");
  }
  if (state.selectedUploadId === token) {
    state.selectedUploadId = null;
    state.uploadDetail = null;
  }
  state.confirmation = null;
  state.message = "Deleted 1 upload.";
  state.error = "";
  await selectSession(sessionId);
  state.page = "uploads";
  const uploads = filterUploads(state.uploads, state.uploadQuery);
  if (nextUploadId && uploads.some((item) => getUploadId(item) === nextUploadId)) {
    await openUpload(sessionId, nextUploadId);
    return;
  }
  if (uploads[0]) {
    await openUpload(sessionId, getUploadId(uploads[0]));
    return;
  }
  render();
}

async function uploadSelectedFile(file) {
  if (!state.selectedSessionId) {
    throw new Error("Select a session first.");
  }
  if (!file) {
    return;
  }
  const stagingId = await stageFile(file);
  const payload = await invoke("create_session_upload", {
    sessionId: state.selectedSessionId,
    sourceMode: "copied",
    fileName: file.name || "upload.bin",
    mimeType: file.type || "",
    title: file.name || "",
    stagingId
  });
  state.message = `Uploaded ${file.name || "file"}.`;
  state.error = "";
  state.page = "uploads";
  await selectSession(state.selectedSessionId);
  if (payload?.upload_id) {
    await openUpload(state.selectedSessionId, payload.upload_id);
  }
}

async function linkUploadPath(form) {
  if (!state.selectedSessionId) {
    throw new Error("Select a session first.");
  }
  const data = new FormData(form);
  const rawPath = String(data.get("path") || "").trim();
  if (!rawPath) {
    throw new Error("Provide a file path to link.");
  }
  const fileName = rawPath.split(/[\\/]/).filter(Boolean).pop() || "upload";
  const payload = await invoke("create_session_upload", {
    sessionId: state.selectedSessionId,
    sourceMode: "linked",
    fileName,
    mimeType: "",
    sourcePath: rawPath,
    title: fileName
  });
  state.message = `Linked ${fileName}.`;
  state.error = "";
  state.page = "uploads";
  form.reset();
  await selectSession(state.selectedSessionId);
  if (payload?.upload_id) {
    await openUpload(state.selectedSessionId, payload.upload_id);
  }
}

async function createCohort(form) {
  const data = new FormData(form);
  const countsPath = String(data.get("counts_path") || "").trim();
  const metadataPath = String(data.get("metadata_path") || "").trim();
  const countsFile = data.get("counts_file");
  const metadataFile = data.get("metadata_file");
  const title = inferLocalCohortTitle(countsFile, countsPath, metadataFile, metadataPath);
  let countsFileName = "";
  let countsSourceMode = "";
  let countsSourcePath = "";
  let countsStagingId = null;
  if (countsFile instanceof File && countsFile.size > 0) {
    countsStagingId = await stageFile(countsFile);
    countsFileName = countsFile.name || "counts.tsv";
    countsSourceMode = "copied";
  } else if (countsPath) {
    countsSourcePath = countsPath;
    countsSourceMode = "linked";
  } else {
    throw new Error("Provide counts as a file or local path.");
  }
  let metadataFileName = "";
  let metadataSourceMode = "";
  let metadataSourcePath = "";
  let metadataStagingId = null;
  if (metadataFile instanceof File && metadataFile.size > 0) {
    metadataStagingId = await stageFile(metadataFile);
    metadataFileName = metadataFile.name || "metadata.tsv";
    metadataSourceMode = "copied";
  } else if (metadataPath) {
    metadataSourcePath = metadataPath;
    metadataSourceMode = "linked";
  } else {
    throw new Error("Provide metadata as a file or local path.");
  }
  state.cohortNotice = null;
  const payload = await invoke("create_cohort", {
    title,
    transpose: false,
    sampleColumn: "",
    groupColumn: "",
    minCount: 10,
    countsSourceMode,
    metadataSourceMode,
    countsFileName: countsFileName || null,
    metadataFileName: metadataFileName || null,
    countsSourcePath: countsSourcePath || null,
    metadataSourcePath: metadataSourcePath || null,
    sourceCountsPath: countsSourcePath || null,
    sourceMetadataPath: metadataSourcePath || null,
    tags: [],
    countsStagingId, metadataStagingId
  });
  state.cohortNotice = {
    kind: "success",
    title: "Import complete",
    message: `Created cohort "${title}".`
  };
  state.message = "";
  state.error = "";
  state.appView = "cohorts";
  const payloadWarnings = Array.isArray(payload?.validation?.warnings)
    ? payload.validation.warnings.filter((item) => String(item || "").trim())
    : Array.isArray(payload?.warnings)
      ? payload.warnings.filter((item) => String(item || "").trim())
      : [];
  if (payloadWarnings.length) {
    state.cohortNotice = {
      kind: "warning",
      title: "Validation warning",
      message: payloadWarnings.length === 1
        ? payloadWarnings[0]
        : `${payloadWarnings[0]} (+${payloadWarnings.length - 1} more)`
    };
  }
  form.reset();
  await refreshCohorts();
  if (payload?.cohort_id) {
    await selectCohort(payload.cohort_id);
  }
}

async function downloadCohort(form) {
  if (state.cohortDownloadStarting || (state.cohortDownloadJob && !isCohortDownloadTerminal(state.cohortDownloadJob))) {
    throw new Error("Finish or cancel the current cohort download before starting another.");
  }
  const data = new FormData(form);
  const title = String(data.get("title") || "").trim();
  const providerId = String(data.get("provider_id") || state.selectedCohortProviderId || "").trim();
  const datasetId = String(data.get("dataset_id") || state.selectedCohortDatasetId || "").trim();
  const tissue = String(data.get("tissue") || state.selectedCohortTissue || "").trim();
  const downloadScope = String(data.get("download_scope") || state.cohortDownloadScope || "full").trim();
  const subsetMode = String(data.get("subset_mode") || state.cohortSubsetMode || "absolute").trim();
  const subsetValueText = String(data.get("subset_value") || state.cohortSubsetValueText || "").trim();
  const catalog = getFilteredCohortCatalog(state.cohortCatalog, tissue);
  const selectedCatalogEntry = catalog.find((item) => item.dataset_id === datasetId) || null;
  const sampleCount = Number(selectedCatalogEntry?.sample_count || 0) || 0;
  let sampleLimit = 0;
  if (!title) {
    throw new Error("Provide a cohort title.");
  }
  if (!providerId) {
    throw new Error("Choose a cohort provider.");
  }
  if (!datasetId) {
    throw new Error("Choose a provider dataset.");
  }
  if (state.cohortProviders.length && !state.cohortProviders.some((item) => item.provider_id === providerId)) {
    throw new Error("Unknown cohort provider.");
  }
  if (catalog.length && !catalog.some((item) => item.dataset_id === datasetId)) {
    throw new Error("Unknown cohort dataset.");
  }
  if (downloadScope === "subset") {
    if (!subsetValueText) {
      throw new Error("Choose a subset size.");
    }
    if (!sampleCount) {
      throw new Error("Subset downloads require a dataset with a known sample count.");
    }
    if (subsetMode === "fraction") {
      const percent = Number.parseFloat(subsetValueText);
      if (!Number.isFinite(percent) || percent <= 0 || percent > 100) {
        throw new Error("Enter a valid subset percentage between 0 and 100.");
      }
      sampleLimit = Math.max(1, Math.min(sampleCount, Math.round((sampleCount * percent) / 100)));
    } else {
      const absolute = Number.parseInt(subsetValueText, 10);
      if (!Number.isFinite(absolute) || absolute <= 0) {
        throw new Error("Enter a valid subset sample count.");
      }
      if (absolute > sampleCount) {
        throw new Error(`Subset count cannot exceed the available sample count (${sampleCount}).`);
      }
      sampleLimit = absolute;
    }
  }
  state.cohortBusy = true;
  state.cohortDownloadStarting = true;
  state.cohortDownloadStartedAt = Date.now();
  state.cohortDownloadFinishedAt = 0;
  state.cohortDownloadGeneration += 1;
  const generation = state.cohortDownloadGeneration;
  state.cohortDownloadCancelling = false;
  state.cohortDownloadObservationError = "";
  state.cohortDownloadName = title;
  state.cohortDownloadJob = null;
  state.cohortNotice = null;
  state.message = "";
  state.error = "";
  render();
  document.getElementById("cohort-download-progress-title")?.focus({ preventScroll: true });
  try {
    const payload = await invoke("start_cohort_download_job", {
      title,
      providerId,
      datasetId,
      tissue,
      sampleLimit: Number.isFinite(sampleLimit) && sampleLimit > 0 ? sampleLimit : 0,
      tags: []
    });
    if (generation !== state.cohortDownloadGeneration) return;
    state.cohortDownloadStarting = false;
    await applyCohortDownloadSnapshot(payload?.job || payload);
    if (generation !== state.cohortDownloadGeneration) return;
    state.cohortDownloadLogOpen = false;
    if (!isCohortDownloadTerminal(state.cohortDownloadJob)) startCohortDownloadPolling(state.cohortDownloadJob?.job_id || "");
  } finally {
    if (generation === state.cohortDownloadGeneration) {
      state.cohortBusy = false;
      state.cohortDownloadStarting = false;
      render();
    }
  }
}

async function createSessionFromCohortId(cohortId) {
  const cohort = state.cohorts.find((item) => item.cohort_id === cohortId);
  if (!cohort) {
    throw new Error("Select a cohort first.");
  }
  const payload = await invoke("create_session_from_cohort", {
    cohortId,
    title: `${cohort.title} session`,
    description: `Analysis session linked to cohort ${cohort.cohort_id}.`,
    species: "human",
    tags: []
  });
  state.message = `Created session from cohort ${cohortId}.`;
  state.error = "";
  state.appView = "workspace";
  await refreshSessions();
  if (payload?.session_id) {
    await selectSession(payload.session_id);
    state.page = "uploads";
  }
}

function promptRenameCohort(cohortId) {
  const cohort = state.cohorts.find((item) => item.cohort_id === cohortId);
  if (!cohort) {
    return;
  }
  state.confirmation = {
    kind: "rename-cohort",
    cohortId,
    title: cohort.title || cohort.cohort_id,
    draftTitle: cohort.title || cohort.cohort_id
  };
  render();
}

async function confirmRenameCohort(cohortId, nextTitle) {
  const cleanedTitle = String(nextTitle || "").trim();
  if (!cleanedTitle) {
    throw new Error("Provide a cohort name.");
  }
  await invoke("update_cohort", { cohortId, title: cleanedTitle });
  state.message = `Renamed cohort to "${cleanedTitle}".`;
  state.error = "";
  await refreshCohorts();
  if (state.selectedCohortId === cohortId) {
    await selectCohort(cohortId);
  }
}

function promptDeleteCohort(cohortId) {
  const cohort = state.cohorts.find((item) => item.cohort_id === cohortId);
  const title = cohort?.title || cohortId;
  if (!cohort) {
    return;
  }
  state.confirmation = {
    kind: "delete-cohort",
    cohortId,
    title
  };
  render();
}

async function confirmDeleteCohort(cohortId) {
  const cohort = state.cohorts.find((item) => item.cohort_id === cohortId);
  const title = cohort?.title || cohortId;
  await invoke("delete_cohort", { cohortId });
  state.message = `Deleted cohort "${title}".`;
  state.error = "";
  await refreshCohorts();
  if (state.selectedCohortId === cohortId) {
    state.selectedCohortId = null;
  }
}

async function deleteSelectedNotes(sessionId, noteIds) {
  const uniqueNoteIds = [...new Set((noteIds || []).filter(Boolean))];
  if (!uniqueNoteIds.length) {
    throw new Error("No notes were selected for deletion.");
  }
  const payload = await invoke("delete_session_notes", {
    sessionId,
    noteIds: uniqueNoteIds
  });
  const deleted = payload?.deleted_note_ids || [];
  if (deleted.length !== uniqueNoteIds.length) {
    throw new Error("Some selected notes could not be deleted.");
  }
  state.selectedNotes = new Set(
    [...state.selectedNotes].filter((token) => {
      const [tokenSessionId, noteId] = token.split(":");
      return tokenSessionId !== sessionId || !deleted.includes(noteId);
    })
  );
  if (state.selectedNoteId && deleted.includes(state.selectedNoteId)) {
    state.selectedNoteId = null;
    state.noteDetail = {
      title: "",
      body_markdown: "",
      author: "user",
      linked_artifact_ids: [],
      tags: []
    };
  }
  state.confirmation = null;
  state.message = `Deleted ${deleted.length} note${deleted.length === 1 ? "" : "s"}.`;
  state.error = "";
  await selectSession(sessionId);
  await ensureNotesLoaded(sessionId);
  state.page = "notes";
  render();
}

async function deleteSelectedReports(sessionId, reportIds) {
  const uniqueReportIds = [...new Set((reportIds || []).filter(Boolean))];
  if (!uniqueReportIds.length) {
    throw new Error("No reports were selected for deletion.");
  }
  const payload = await invoke("delete_session_reports", {
    sessionId,
    reportIds: uniqueReportIds
  });
  const deleted = payload?.deleted_report_ids || [];
  if (deleted.length !== uniqueReportIds.length) {
    throw new Error("Some selected reports could not be deleted.");
  }
  state.selectedReports = new Set(
    [...state.selectedReports].filter((token) => {
      const [tokenSessionId, reportId] = token.split(":");
      return tokenSessionId !== sessionId || !deleted.includes(reportId);
    })
  );
  if (state.selectedReportId && deleted.includes(state.selectedReportId)) {
    state.selectedReportId = null;
    state.reportDetail = null;
  }
  state.confirmation = null;
  state.message = `Deleted ${deleted.length} report${deleted.length === 1 ? "" : "s"}.`;
  state.error = "";
  await selectSession(sessionId);
  await ensureReportsLoaded(sessionId);
  state.page = "reports";
  render();
}

async function ensureNotesLoaded(sessionId) {
  if (!sessionId) {
    state.notes = [];
    state.notesLoadedSessionId = null;
    return;
  }
  if (hasLoadedEmptyNotes(sessionId)) {
    state.notes = [];
    state.notesLoadedSessionId = sessionId;
    return;
  }
  if (state.notesLoadedSessionId === sessionId) {
    return;
  }
  const payload = await invoke("list_session_notes", { sessionId });
  state.notes = payload.notes || [];
  state.notesLoadedSessionId = sessionId;
}

async function ensureReportsLoaded(sessionId) {
  if (!sessionId) {
    state.reports = [];
    state.selectedReportId = null;
    state.reportDetail = null;
    state.reportsLoadedSessionId = null;
    return;
  }
  if (hasLoadedEmptyReports(sessionId)) {
    state.reports = [];
    state.selectedReportId = null;
    state.reportDetail = null;
    state.reportsLoadedSessionId = sessionId;
    return;
  }
  const expectedReportCount = Number(state.sessionSummary?.report_count || 0);
  const cacheMatchesSummary = state.reportsLoadedSessionId === sessionId
    && state.reports.length === expectedReportCount;
  if (cacheMatchesSummary) {
    return;
  }
  const payload = await invoke("list_session_reports", { sessionId });
  state.reports = payload.reports || [];
  state.reportsLoadedSessionId = sessionId;
}

function hasLoadedEmptyNotes(sessionId) {
  return state.selectedSessionId === sessionId && Number(state.sessionSummary?.note_count || 0) === 0;
}

function hasLoadedEmptyReports(sessionId) {
  return state.selectedSessionId === sessionId && Number(state.sessionSummary?.report_count || 0) === 0;
}

async function selectReport(sessionId, reportId) {
  if (state.selectedSessionId !== sessionId) {
    await selectSession(sessionId);
  }
  state.page = "reports";
  await ensureReportsLoaded(sessionId);
  state.selectedReportId = reportId;
  state.reportDetail = await invoke("get_session_report", { sessionId, reportId });
  render();
}

async function selectNote(sessionId, noteId) {
  if (state.selectedSessionId !== sessionId) {
    await selectSession(sessionId);
  }
  state.page = "notes";
  await ensureNotesLoaded(sessionId);
  state.selectedNoteId = noteId;
  state.noteDetail = await invoke("get_session_note", { sessionId, noteId });
  render();
}

function composeNewNote() {
  if (!state.selectedSessionId) {
    state.error = "Select a session first.";
    render();
    return;
  }
  state.page = "notes";
  state.selectedNoteId = null;
  state.noteDetail = {
    title: "",
    body_markdown: "",
    author: "user",
    linked_artifact_ids: [],
    tags: []
  };
  render();
}

async function saveNote(form) {
  if (!state.selectedSessionId) {
    state.error = "Select a session first.";
    render();
    return;
  }
  const data = new FormData(form);
  const title = String(data.get("title") || "").trim();
  const body_markdown = String(data.get("body_markdown") || "").trim();
  if (!title || !body_markdown) {
    state.error = "Title and markdown body are required.";
    render();
    return;
  }
  const payload = state.selectedNoteId
    ? await invoke("update_session_note", {
        sessionId: state.selectedSessionId,
        noteId: state.selectedNoteId,
        title,
        bodyMarkdown: body_markdown,
        author: "user",
        linkedArtifactIds: [],
        tags: []
      })
    : await invoke("create_session_note", {
        sessionId: state.selectedSessionId,
        title,
        bodyMarkdown: body_markdown,
        author: "user",
        linkedArtifactIds: [],
        tags: []
      });
  state.message = state.selectedNoteId ? `Updated note "${title}".` : `Saved note "${title}".`;
  state.error = "";
  await selectSession(state.selectedSessionId);
  await ensureNotesLoaded(state.selectedSessionId);
  state.page = "notes";
  state.selectedNoteId = payload.note_id;
  state.noteDetail = payload;
  render();
}

async function deleteNote(sessionId, noteId) {
  promptDeleteNotes(sessionId, [noteId]);
}

async function deleteReport(sessionId, reportId) {
  promptDeleteReports(sessionId, [reportId]);
}

async function deleteFigure(sessionId, artifactId) {
  promptDeleteArtifacts(sessionId, [artifactId], "figures");
}

let filterRenderFrame = null;
let pendingFilterFocus = null;

/**
 * Coalesce filter-driven renders into a single animation frame.
 *
 * Typing in a filter input used to trigger a full re-render per keystroke. The
 * focus snapshot is captured synchronously here, before the frame is scheduled,
 * so the caret position is read from the live input rather than from a node a
 * previous frame may already have replaced.
 */
function scheduleFilterRender() {
  if (filterRenderFrame !== null) {
    return;
  }
  pendingFilterFocus = captureRenderFocus();
  filterRenderFrame = window.requestAnimationFrame(() => {
    filterRenderFrame = null;
    const snapshot = pendingFilterFocus;
    pendingFilterFocus = null;
    render({ focusSnapshot: snapshot });
  });
}

function render({ focusSnapshot: providedFocusSnapshot } = {}) {
  // A synchronous render supersedes any queued filter frame.
  if (filterRenderFrame !== null) {
    window.cancelAnimationFrame(filterRenderFrame);
    filterRenderFrame = null;
    pendingFilterFocus = null;
  }
  applyTheme(state.theme);
  document.body.classList.toggle("chat-view", state.appView === "chat");
  document.body.classList.toggle("workspace-view", state.appView === "workspace");
  document.body.classList.toggle("cohort-view", state.appView === "cohorts");
  document.body.classList.toggle("booting", Boolean(state.startup.visible));
  if (state.startup.visible) {
    app.innerHTML = renderStartupScreen();
    syncWebNotifications();
    return;
  }
  const focusSnapshot = providedFocusSnapshot ?? captureRenderFocus();
  const scrollSnapshot = captureRenderScroll();
  const selectedSession = state.sessions.find((item) => item.session_id === state.selectedSessionId) || null;
  const selectedArtifact = state.artifacts.find((item) => item.artifact_id === state.selectedArtifactId) || null;
  const visibleArtifacts = filterArtifacts(state.artifacts, state.artifactQuery);
  const figureArtifacts = state.artifacts.filter((item) => item.kind === "figure");
  const visibleFigures = filterArtifacts(figureArtifacts, state.figureQuery);
  const visibleNotes = filterNotes(state.notes, state.noteQuery);
  const visibleReports = filterReports(state.reports, state.reportQuery);
  const runtimeMode = state.runtime.active_session_id ? "Connected" : "Stateless";
  const captureMode = state.runtime.active_session_id
    ? state.runtime.capture_enabled
      ? "Armed"
      : "Idle"
    : "Disabled";
  const detailJson = state.artifactDetail ? escapeHtml(JSON.stringify(state.artifactDetail, null, 2)) : "";
  const selectedConversation = getSelectedChatConversation();

  app.innerHTML = `
    <div class="app-shell ${state.appView === "chat" ? "chat-layout" : state.appView === "workspace" ? "workspace-layout" : state.appView === "cohorts" ? "cohort-layout" : ""}">
      ${renderSidebar(selectedSession, selectedConversation)}

      <section class="main-column ${state.appView === "chat" ? "chat-column" : ""}">
        ${state.workspaceBootstrapState !== "ready" ? `<section class="frame reference-copy" role="status">${state.workspaceBootstrapState === "failed"
          ? `<button type="button" data-action="retry-workspace-bootstrap">Retry loading workspace</button>`
          : "Reading local workspace metadata. Navigation is available; workspace actions will be ready when this finishes."}</section>` : ""}
        ${state.appView !== "chat" && state.appView !== "system" ? `
          <header class="topbar frame">
            <div class="topbar-status">
              ${renderTopbarStatus(runtimeMode, captureMode)}
            </div>

            <div class="topbar-actions">
              ${renderTopbarActions()}
            </div>
          </header>
        ` : ""}

        ${state.appView === "workspace" && (state.showCreateForm || state.showDeriveForm) ? `
          <section class="composer-row">
            ${state.showCreateForm ? renderCreateForm() : ""}
            ${state.showDeriveForm ? renderDeriveForm() : ""}
          </section>
        ` : ""}

        ${state.appView === "chat"
          ? renderChatMain(selectedConversation)
          : state.appView === "cohorts"
            ? renderCohortsMain()
          : renderWorkspaceMain(selectedSession, visibleArtifacts, selectedArtifact, visibleFigures, visibleNotes, visibleReports, detailJson)}
      </section>
    </div>
    ${renderConfirmationDialog()}
  `;

  bindEvents();
  restoreRenderFocus(focusSnapshot);
  bindMarkdownImageFallbacks();
  bindMarkdownExternalLinks();
  void renderMermaidDiagrams();
  renderMathInPreviews();
  restoreRenderScroll(scrollSnapshot);
  syncCohortDownloadClock();
  syncWebNotifications();
}

function syncWebNotifications() {
  const chatOpen = state.appView === "chat";
  const configured = state.chatProviders.some((row) => row.enabled && row.connected);
  const unavailable = chatOpen && state.chatBootstrapState === "ready" && !state.mcpStatus.running && state.mcpStatus.connection_state !== "connecting";
  const cohortNotice = state.cohortNotice;
  const notice = (source, severity, field, enabled = true) => {
    const message = enabled ? state[field] : "";
    return { source, severity, message, acknowledge() {
      if (state[field] === message) state[field] = "";
      syncWebNotifications();
    } };
  };
  syncNotifications([
    notice("error", "error", "error"),
    notice("message", "success", "message"),
    notice("chat-warning", "warning", "chatSystemMessage"),
    notice("chat-message", "success", "chatMessage", !state.chatBusy),
    { source: "chat-bootstrap", severity: "error", message: chatOpen && state.chatBootstrapState === "failed" ? state.chatBootstrapError : "" },
    { source: "chat-tools", severity: configured ? "error" : "warning", message: unavailable && !state.error ? (configured ? state.mcpStatus.error || "Local tools are stopped. Use Settings to reconnect Chat." : "Select an existing .env key or add a model connection in Settings, then save & activate it to use Chat.") : "" },
    { source: "session", severity: "error", message: state.sessionSnapshotStatus === "failed" ? state.sessionSnapshotError : "" },
    { source: "cohort", severity: cohortNotice?.kind === "warning" ? "warning" : "success", message: cohortNotice?.message, acknowledge() {
      if (state.cohortNotice === cohortNotice) state.cohortNotice = null;
      syncWebNotifications();
    } },
    { source: "read-only", severity: "warning", message: state.paths?.read_only ? "Browsing your configured workspace read-only. Existing results cannot be changed here. Use Settings to change the workspace view." : "" },
  ]);
}

function captureRenderFocus() {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement) || (!app.contains(active) && !document.body.contains(active))) {
    return null;
  }
  if (!active.id) {
    return null;
  }
  const isTextControl = active instanceof HTMLTextAreaElement
    || (active instanceof HTMLInputElement && !["checkbox", "radio", "file", "button", "submit"].includes(active.type));
  return {
    id: active.id,
    selectionStart: isTextControl ? active.selectionStart : null,
    selectionEnd: isTextControl ? active.selectionEnd : null,
    scrollTop: active instanceof HTMLTextAreaElement ? active.scrollTop : null
  };
}

function restoreRenderFocus(snapshot) {
  if (document.querySelector(".gleskit-notification[open]")) return;
  if (!snapshot?.id) {
    return;
  }
  const next = document.getElementById(snapshot.id);
  if (!(next instanceof HTMLElement) || next.hasAttribute("disabled")) {
    return;
  }
  next.focus({ preventScroll: true });
  if (typeof snapshot.selectionStart === "number"
    && typeof snapshot.selectionEnd === "number"
    && typeof next.setSelectionRange === "function") {
    next.setSelectionRange(snapshot.selectionStart, snapshot.selectionEnd);
  }
  if (next instanceof HTMLTextAreaElement && typeof snapshot.scrollTop === "number") {
    next.scrollTop = snapshot.scrollTop;
  }
}

function captureRenderScroll() {
  const selectors = [
    ".chat-settings-panel",
    ".chat-settings-tabs",
    ".chat-provider-list",
    ".skill-file-list",
    ".cohort-scroll"
  ];
  return selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)).map((element) => {
    if (!(element instanceof HTMLElement)) {
      return null;
    }
    return {
      selector,
      scrollLeft: element.scrollLeft,
      scrollTop: element.scrollTop
    };
  })).filter(Boolean);
}

function restoreRenderScroll(snapshot) {
  if (!Array.isArray(snapshot) || !snapshot.length) {
    return;
  }
  snapshot.forEach((item) => {
    const elements = document.querySelectorAll(item.selector);
    elements.forEach((element) => {
      if (!(element instanceof HTMLElement)) {
        return;
      }
      element.scrollLeft = item.scrollLeft;
      element.scrollTop = item.scrollTop;
    });
  });
}

function renderSidebar(selectedSession, selectedConversation) {
  const isChat = state.appView === "chat";
  return `
    <aside class="sidebar frame web-sidebar">
      <a class="web-brand" href="#" aria-label="GLESKit local workspace"><img src="${sidebarBrandIconUrl}" alt=""/><span>GLESKit<small>Local research workspace</small></span></a>
      ${renderAppNavigation()}
      <div class="web-sidebar-content">${isChat ? renderChatSidebarSummary() : state.appView === "workspace" ? renderWorkspaceSidebarActions() : ""}</div>
      ${renderWebSidebarFooter()}
    </aside>`;
}

function renderWebSidebarFooter() {
  return `<div class="web-sidebar-footer">
    <button id="open-settings-button" type="button" class="action-button compact sidebar-settings-button" data-action="show-app-system" aria-haspopup="dialog" aria-controls="web-settings-dialog">${gearIcon()}<span>Settings</span></button>
    <div class="web-local-notice"><span class="web-local-dot"></span> On your computer <p>Scientific data and results stay in your local workspace.</p></div>
  </div>`;
}

function renderWorkspaceSidebarActions() {
  return `
    <div class="sidebar-actions">
      <button type="button" class="action-button primary compact" id="toggle-create-form">${state.showCreateForm ? "Close" : "New Session"}</button>
      <button type="button" class="action-button compact" id="toggle-derive-form">${state.showDeriveForm ? "Close" : "Merge"}</button>
    </div>
  `;
}

function renderChatSidebarSummary() {
  const chatCount = state.chatConversations.length;
  return `
    <div class="sidebar-actions">
      <button type="button" class="action-button primary compact" id="new-chat-button">New Chat</button>
    </div>
    <section class="frame chat-sidebar-threads">
      <div class="chat-list-head">
        <div>
          <div class="mini-label">Conversations</div>
          <h2>Chat Threads</h2>
        </div>
        <span class="status-chip ${chatCount ? "positive" : ""}">${chatCount}</span>
      </div>
      <div class="chat-list-scroll">
        ${state.chatConversations.length
          ? state.chatConversations.map(renderChatConversationRow).join("")
          : `<div class="empty-state">No chats yet. Start one and choose a provider to begin.</div>`}
      </div>
    </section>
  `;
}

function renderTopbarStatus(runtimeMode, captureMode) {
  if (state.appView === "chat") {
    return `<div class="topbar-spacer"></div>`;
  }
  return `
    <span class="status-chip ${state.runtime.available ? "positive" : ""}">Bridge ${escapeHtml(state.runtime.available ? "Live" : "Offline")}</span>
    <span class="status-chip ${state.runtime.active_session_id ? "positive" : ""}">Route ${escapeHtml(runtimeMode)}</span>
    <span class="status-chip ${state.runtime.capture_enabled ? "positive" : ""}">Capture ${escapeHtml(captureMode)}</span>
  `;
}

function renderAppNavigation() {
  return `
    <div class="app-nav">
      <button type="button" class="action-button compact ${state.appView === "workspace" ? "is-active" : ""}" data-action="show-app-workspace">Workspace</button>
      <button type="button" class="action-button compact ${state.appView === "cohorts" ? "is-active" : ""}" data-action="show-app-cohorts">Cohorts</button>
      <button type="button" class="action-button compact ${state.appView === "chat" ? "is-active" : ""}" data-action="show-app-chat">Chat</button>
    </div>
  `;
}

function renderTopbarActions() {
  return renderThemeToggle();
}

/**
 * Session snapshot status banner.
 *
 * `ensureSessionSnapshot` loads the summary, artifacts, and uploads for the
 * selected session in one CLI call. While that is in flight the detail tabs
 * would otherwise render stale or empty data, so surface the state here.
 */
function renderSessionSnapshotBanner() {
  if (!state.selectedSessionId) {
    return "";
  }
  if (state.sessionSnapshotStatus === "loading") {
    return `
      <div class="signal-banner system session-snapshot-banner" aria-busy="true">
        Loading session data…
      </div>
    `;
  }
  if (state.sessionSnapshotStatus === "failed") {
    return `<button type="button" class="action-button compact" data-action="retry-session-snapshot">Retry loading session</button>`;

  }
  return "";
}

function renderWorkspaceMain(selectedSession, visibleArtifacts, selectedArtifact, visibleFigures, visibleNotes, visibleReports, detailJson) {
  return `
    <main class="console-main">
      ${renderOverviewHeader(selectedSession)}
      <section class="view-shell">
        ${renderContentTabs(Boolean(selectedSession))}
        <div class="view-body">
          ${renderSessionSnapshotBanner()}
          ${state.page === "artifacts"
            ? renderArtifactView(selectedSession, visibleArtifacts, selectedArtifact, detailJson)
            : state.page === "uploads"
              ? renderUploadsView(selectedSession)
            : state.page === "figures"
              ? renderFiguresView(selectedSession, visibleFigures)
            : state.page === "notes"
              ? renderNotesView(selectedSession, visibleNotes)
              : state.page === "reports"
                ? renderReportsView(selectedSession, visibleReports)
              : renderWorkspaceView(selectedSession)}
        </div>
      </section>
    </main>
  `;
}

function renderCohortsMain() {
  const visibleCohorts = filterCohorts(state.cohorts, state.cohortQuery);
  const cohortTab = state.cohortTab || "download";
  return `
    <main class="console-main cohort-main">
      <div class="cohort-scroll">
        <section class="frame focus-strip">
          <div class="focus-header">
            <div class="focus-copy">
              <div class="mini-label">Cohort Library</div>
              <h2>Bulk RNA cohorts</h2>
              <p class="focus-description">
                Import, download, and validate bulk RNA cohorts with deterministic preprocessing before they are used by sessions.
                Cohorts are reusable datasets; session studies stay in Workspace.
              </p>
            </div>
          </div>
        </section>
        <section class="cohort-library-grid">
          <section class="frame cohort-workbench-panel">
            <div class="cohort-workbench-tabs" role="tablist" aria-label="Cohort import and download">
              <button
                type="button"
                class="view-tab ${cohortTab === "download" ? "is-active" : ""}"
                data-action="show-cohort-download"
                role="tab"
                aria-selected="${cohortTab === "download"}"
              >
                Download cohort
              </button>
              <button
                type="button"
                class="view-tab ${cohortTab === "import" ? "is-active" : ""}"
                data-action="show-cohort-import"
                role="tab"
                aria-selected="${cohortTab === "import"}"
              >
                Local import
              </button>
            </div>
            <div class="cohort-workbench-body">
              ${state.cohortLibraryBusy ? `<div class="signal-banner system" aria-busy="true">Loading providers, catalog, and local cohorts…</div>` : ""}
              ${cohortTab === "download" ? renderCohortProviderPanel() : renderCohortImportForm()}
            </div>
          </section>
          <section class="frame cohort-browser-panel">
            <div class="panel-head">
              <div>
                <div class="mini-label">Stored Cohorts</div>
                <h2>Local library</h2>
              </div>
              <span class="status-chip ${visibleCohorts.length ? "positive" : ""}">${visibleCohorts.length} / ${state.cohorts.length}</span>
            </div>
            <div class="panel-subhead">
              <input id="cohort-search" value="${escapeHtml(state.cohortQuery)}" placeholder="Filter cohorts..." />
            </div>
            ${renderCohortTable(visibleCohorts)}
          </section>
        </section>
      </div>
    </main>
  `;
}

function renderCohortDownloadJobPanel() {
  const job = state.cohortDownloadJob;
  if (!job) {
    return "";
  }
  const percent = Number(job.progress_percent || 0);
  const clamped = Math.max(0, Math.min(100, Number.isFinite(percent) ? percent : 0));
  const etaText = cohortDownloadEtaText(job);
  const eventCount = Array.isArray(job.log) ? job.log.length : 0;
  const providerText = [job.provider_id, job.dataset_id, job.tissue].filter(Boolean).join(" · ");
  const statusClass = job.status === "completed" ? "positive" : (job.status === "failed" || job.status === "cancelled") ? "danger" : "";
  const selectedSamples = job.samples_downloaded != null ? Number(job.samples_downloaded) : null;
  const plannedSamples = job.samples_total != null ? Number(job.samples_total) : null;
  const availableSamples = job.samples_total_all != null ? Number(job.samples_total_all) : null;
  const hasSubsetContext = Number.isFinite(selectedSamples) && Number.isFinite(availableSamples) && availableSamples > 0 && selectedSamples < availableSamples;
  const sampleMetric = hasSubsetContext
    ? `${escapeHtml(String(selectedSamples))} selected / ${escapeHtml(String(availableSamples))} available`
    : `${escapeHtml(selectedSamples != null ? String(selectedSamples) : "0")} / ${escapeHtml(plannedSamples != null ? String(plannedSamples) : (availableSamples != null ? String(availableSamples) : "?"))} samples`;
  return `
    <section class="cohort-download-job ${statusClass}">
      <div class="panel-head cohort-download-job-head">
        <div>
          <div class="mini-label">Download Job</div>
          <h3>${escapeHtml(cohortDownloadStageLabel(job))}</h3>
        </div>
        <div class="cohort-download-job-actions">
          <span class="status-chip ${job.status === "completed" ? "positive" : ""}">${escapeHtml(job.status)}</span>
          ${job.can_cancel && (!isCohortDownloadLocked()) ? `<button type="button" class="row-button compact danger" data-action="cancel-cohort-download" ${state.cohortDownloadCancelling ? "disabled" : ""}>${state.cohortDownloadCancelling ? "Cancelling…" : "Cancel"}</button>` : ""}
        </div>
      </div>
      <div class="cohort-download-job-summary">
        <div class="cohort-download-job-copy">
          <strong>${escapeHtml(job.title || "Untitled cohort")}</strong>
          <span>${escapeHtml(providerText)}</span>
        </div>
        <div class="cohort-download-job-metrics">
          <span>Elapsed <span data-cohort-elapsed role="timer" aria-live="off">${elapsedDownloadTime(job, state.cohortDownloadStartedAt, state.cohortDownloadFinishedAt)}</span></span>
          <span>${escapeHtml(clamped.toFixed(1))}%</span>
          <span>${sampleMetric}</span>
          ${etaText ? `<span>${escapeHtml(etaText)}</span>` : ""}
        </div>
      </div>
      ${hasSubsetContext ? `<div class="helper-copy">${escapeHtml(`${selectedSamples} samples selected from ${availableSamples} available.`)}</div>` : ""}
      <div class="progress-track" aria-hidden="true">
        <div class="progress-fill" style="width: ${clamped}%"></div>
      </div>
      <div class="cohort-download-job-footer">
        <span>Started ${escapeHtml(formatTimestamp(job.started_at))}</span>
        <span>Updated ${escapeHtml(formatTimestamp(job.updated_at))}</span>
        <span>${escapeHtml(String(eventCount))} event${eventCount === 1 ? "" : "s"}</span>
      </div>
      ${job.error ? `<div class="helper-copy error-copy">${escapeHtml(job.error)}</div>` : ""}
      ${state.cohortDownloadObservationError ? `<p class="reference-copy">${escapeHtml(state.cohortDownloadObservationError)}</p><button type="button" class="action-button compact" data-action="retry-cohort-download-status" ${state.cohortDownloadRefreshing ? "disabled" : ""}>Retry status</button>` : ""}
      ${renderCohortDownloadJobLog(job)}
    </section>
  `;
}

function renderCohortDownloadJobLog(job) {
  const entries = Array.isArray(job?.log) ? job.log : [];
  if (!entries.length) {
    return "";
  }
  const rows = entries.slice(-20).map((entry) => `
    <li>
      <div class="cohort-download-log-time">${escapeHtml(formatTimestamp(entry.at))}</div>
      <div class="cohort-download-log-body">
        <strong>${escapeHtml(entry.stage || "Event")}</strong>
        <span>${escapeHtml(entry.status || "")}</span>
        ${entry.meta && Object.keys(entry.meta || {}).length ? `<code>${escapeHtml(JSON.stringify(entry.meta))}</code>` : ""}
      </div>
    </li>
  `).join("");
  return `
    <details class="cohort-download-log" ${state.cohortDownloadLogOpen ? "open" : ""}>
      <summary>Event log</summary>
      <ul>${rows}</ul>
    </details>
  `;
}

function renderCohortProviderPanel() {
  const selectedProvider = state.cohortProviders.find((item) => item.provider_id === state.selectedCohortProviderId) || null;
  const allCatalog = state.cohortCatalog || [];
  const catalog = getFilteredCohortCatalog(allCatalog, state.selectedCohortTissue);
  const selectedCatalogEntry = catalog.find((item) => item.dataset_id === state.selectedCohortDatasetId) || null;
  const tissueOptions = getCohortTissueOptions(allCatalog);
  const sampleCount = Number(selectedCatalogEntry?.sample_count || 0) || null;
  const subsetMode = state.cohortSubsetMode || "absolute";
  const subsetInputType = "number";
  const subsetInputMin = "1";
  const subsetInputMax = subsetMode === "fraction" ? "100" : (sampleCount ? String(sampleCount) : "");
  const subsetPlaceholder = subsetMode === "fraction" ? "Percent of samples" : "Sample count";
  const subsetHelp = subsetMode === "fraction"
    ? "Enter a percentage of the selected cohort. GLESKit converts it to a count for download."
    : sampleCount
      ? `Maximum allowed: ${escapeHtml(String(sampleCount))} samples.`
      : "Select a dataset with a known sample count to enable subset download.";
  const locked = isCohortDownloadLocked();
  return `
    <section class="cohort-download-region ${locked ? "is-downloading" : ""}" aria-label="Public cohort sources">
    <div class="cohort-download-controls" ${locked ? "inert" : ""}>
    <form id="cohort-download-form" class="cohort-form" aria-busy="${locked}">
      <div class="panel-head">
        <div>
          <div class="mini-label">Provider Download</div>
          <h2>Public cohort sources</h2>
        </div>
        <span class="status-chip">${state.cohortProviders.length} providers</span>
      </div>
      ${renderCohortDownloadJobPanel()}
      <fieldset class="cohort-download-fields" aria-label="Download configuration" ${locked ? "disabled" : ""}>
      <div class="cohort-form-grid">
        <label>
          <span>Provider</span>
          <select name="provider_id" id="cohort-provider-select">
            ${(state.cohortProviders || []).map((provider) => `
              <option value="${escapeHtml(provider.provider_id)}" ${provider.provider_id === state.selectedCohortProviderId ? "selected" : ""}>${escapeHtml(provider.name || provider.provider_id)}</option>
            `).join("")}
          </select>
        </label>
        <label>
          <span>Dataset</span>
          <select name="dataset_id" id="cohort-dataset-select" ${catalog.length ? "" : "disabled"}>
            ${catalog.length
              ? catalog.map((dataset) => `
                <option value="${escapeHtml(dataset.dataset_id)}" ${dataset.dataset_id === state.selectedCohortDatasetId ? "selected" : ""}>
                  ${escapeHtml(dataset.title || dataset.dataset_id)}
                  ${dataset.sample_count ? ` · ${escapeHtml(String(dataset.sample_count))} samples` : ""}
                </option>
              `).join("")
              : `<option value="">No datasets</option>`}
          </select>
        </label>
        <label>
          <span>Tissue</span>
          <select name="tissue" id="cohort-tissue-select" ${tissueOptions.length ? "" : "disabled"}>
            ${tissueOptions.length
              ? tissueOptions.map((token) => `
                <option value="${escapeHtml(token)}" ${token === state.selectedCohortTissue ? "selected" : ""}>${escapeHtml(token)}</option>
              `).join("")
              : `<option value="">All tissues</option>`}
          </select>
        </label>
        <label>
          <span>Study title</span>
          <input name="title" id="cohort-download-title" value="${escapeHtml(state.cohortDownloadName || selectedCatalogEntry?.title || "")}" placeholder="TCGA BRCA cohort" required />
        </label>
        <label>
          <span>Download scope</span>
          <select name="download_scope" id="cohort-download-scope">
            <option value="full" ${state.cohortDownloadScope === "full" ? "selected" : ""}>Full dataset</option>
            <option value="subset" ${state.cohortDownloadScope === "subset" ? "selected" : ""}>Subset of dataset</option>
          </select>
        </label>
        <label>
          <span>Subset mode</span>
          <select name="subset_mode" id="cohort-subset-mode" ${state.cohortDownloadScope === "subset" ? "" : "disabled"}>
            <option value="absolute" ${subsetMode === "absolute" ? "selected" : ""}>Absolute count</option>
            <option value="fraction" ${subsetMode === "fraction" ? "selected" : ""}>Percentage</option>
          </select>
        </label>
        <label>
          <span>Subset value</span>
          <input
            name="subset_value"
            id="cohort-subset-value"
            type="${subsetInputType}"
            min="${subsetInputMin}"
            ${subsetInputMax ? `max="${escapeHtml(subsetInputMax)}"` : ""}
            placeholder="${subsetPlaceholder}"
            value="${escapeHtml(state.cohortSubsetValueText || "")}"
            ${state.cohortDownloadScope === "subset" ? "" : "disabled"}
          />
        </label>
      </div>
      <div class="cohort-form-actions">
        <button type="submit" class="action-button primary compact" ${state.cohortDownloadStarting || state.cohortProviderBusy || state.cohortCatalogBusy || (state.cohortDownloadJob && !isCohortDownloadTerminal(state.cohortDownloadJob)) ? "disabled" : ""}>Download cohort</button>
      </div>
      </fieldset>
      <div class="helper-copy">
        ${selectedProvider
          ? `Provider: ${escapeHtml(selectedProvider.name || selectedProvider.provider_id)}.`
          : "Choose a provider and dataset to download a public cohort."}
        ${sampleCount ? ` Catalog sample estimate: ${escapeHtml(String(sampleCount))}.` : ""}
        ${state.cohortDownloadScope === "subset" ? ` ${subsetHelp}` : " Full dataset download selected."}
      </div>
    </form>
    </div>
    ${locked ? renderCohortDownloadOverlay() : ""}
    </section>
  `;
}

function renderCohortDownloadOverlay() {
  const job = state.cohortDownloadStarting ? null : state.cohortDownloadJob;
  const known = job?.progress_percent != null && Number.isFinite(Number(job.progress_percent));
  const percent = known ? Math.max(0, Math.min(100, Number(job.progress_percent))) : 0;
  const stage = state.cohortDownloadCancelling ? "Cancelling download…" : job ? cohortDownloadStageLabel(job) : "Starting download…";
  return `<div class="cohort-download-overlay">
    <section class="cohort-download-progress" aria-labelledby="cohort-download-progress-title">
      <span class="cohort-download-spinner" aria-hidden="true"></span>
      <h3 id="cohort-download-progress-title" tabindex="-1">${escapeHtml(stage)}</h3>
      <p>${escapeHtml(job?.title || state.cohortDownloadName || "Cohort download")}</p>
      <div class="cohort-download-elapsed"><span data-cohort-elapsed role="timer" aria-label="Elapsed download time" aria-live="off">${elapsedDownloadTime(job, state.cohortDownloadStartedAt, state.cohortDownloadFinishedAt)}</span><small>elapsed</small></div>
      <div class="progress-track ${known ? "" : "is-indeterminate"}" role="progressbar" aria-label="Cohort download progress" aria-valuemin="0" aria-valuemax="100" ${known ? `aria-valuenow="${percent}"` : 'aria-valuetext="Progress not reported by the provider"'}><div class="progress-fill" style="width: ${known ? percent : 35}%"></div></div>
      <p class="reference-copy" role="status">${known ? `${percent.toFixed(1)}% complete. ` : ""}Download settings are locked until this download finishes. You can use the rest of the app.</p>
      ${job?.can_cancel ? `<button id="cohort-download-cancel" type="button" class="action-button compact danger" data-action="cancel-cohort-download" ${state.cohortDownloadCancelling ? "disabled" : ""}>${state.cohortDownloadCancelling ? "Cancelling…" : "Cancel download"}</button>` : ""}
    </section>
  </div>`;
}

function renderChatMain(selectedConversation) {
  const provider = getSelectedChatProvider();
  const providerOptions = state.chatProviders;
  const draftDirectives = parseChatTurnDirectives(state.chatDraft);
  const chatReady = isChatReady();
  const toolsReady = state.mcpStatus.running;
  const chatComposeDisabled = state.chatBusy || state.chatCancellationInProgress || state.chatConversationLoading || !chatReady;
  const showCancelButton = state.chatBusy || state.chatCancellationInProgress;
  return `
    <main class="console-main chat-main">
      <section class="frame chat-shell ${state.chatActivitySidebarOpen ? "has-activity-sidebar" : ""}">
        <section class="chat-panel">
          <div class="chat-panel-head">
            <div class="chat-context-selectors">
              <select id="chat-provider-select" class="context-select" name="provider_id" aria-label="Select provider" ${(providerOptions.length && !chatComposeDisabled) ? "" : "disabled"} form="chat-compose-form">
                ${providerOptions.length
                  ? providerOptions.map((item) => `<option value="${escapeHtml(item.provider_id)}" ${item.provider_id === state.selectedProviderId ? "selected" : ""} ${item.enabled ? "" : "disabled"}>${escapeHtml(item.label)}</option>`).join("")
                  : `<option value="">No providers</option>`}
              </select>
              ${renderChatModelPicker(provider)}
              <button type="button" class="action-button compact ${state.chatActivitySidebarOpen ? "is-active" : ""}" data-action="toggle-chat-activity-sidebar">Activity</button>
            </div>
            <div class="chat-panel-actions">
              ${renderThemeToggle()}
            </div>
          </div>
          <div class="chat-message-stack">
            <div class="chat-status-slot">
            ${renderChatBootstrapBanner()}
            </div>
            <div class="chat-message-scroll" id="chat-message-scroll">
              <div class="chat-message-inner">
                ${state.chatConversationLoading ? `<p class="reference-copy" role="status">Loading this conversation's saved messages…</p>` : ""}
                ${renderChatMessages()}
              </div>
            </div>
            <button type="button" id="chat-jump-latest" class="action-button compact icon-button icon-only chat-jump-latest" aria-label="Jump to latest message" title="Jump to latest message" hidden>
              ${downIcon()}
            </button>
          </div>
          <div class="chat-compose-wrapper">
            <form id="chat-compose-form" class="chat-compose">
              <div id="chat-compose-pipeline-row" class="chat-compose-directive-row" ${draftDirectives.pipelineMode ? "" : "hidden"}>
                <span id="chat-compose-pipeline-badge" class="status-chip positive chat-compose-pipeline-badge">Pipeline mode</span>
              </div>
              <div id="chat-compose-data-row" class="chat-compose-directive-row" ${draftDirectives.inputDataRefs.length > 0 ? "" : "hidden"}>
                <span id="chat-compose-data-badge" class="status-chip chat-compose-data-badge">${draftDirectives.inputDataRefs.length > 0 ? escapeHtml(draftDirectives.inputDataRefs.length === 1 ? "1 data ref" : `${draftDirectives.inputDataRefs.length} data refs`) : ""}</span>
                <span id="chat-compose-data-summary" class="chat-compose-directive-summary">${draftDirectives.inputDataRefs.length > 0 ? escapeHtml(`${draftDirectives.inputDataRefs.length} data ref${draftDirectives.inputDataRefs.length === 1 ? "" : "s"} attached`) : ""}</span>
              </div>
              <div class="chat-input-shell">
                <textarea name="content" id="chat-draft" class="chat-draft" placeholder="Send a message... Markdown is supported." ${chatComposeDisabled ? "disabled" : ""}>${escapeHtml(state.chatDraft)}</textarea>
                <div class="chat-input-actions">
                  ${showCancelButton ? `
                    <button
                      type="button"
                      class="action-button compact danger icon-button icon-only chat-stop-button"
                      data-action="cancel-chat-request"
                      aria-label="Cancel request"
                      title="Cancel request"
                      ${state.chatCancellationInProgress ? "disabled" : ""}
                    >
                      ${stopIcon()}
                    </button>
                  ` : ""}
                  <button
                    type="submit"
                    class="action-button primary compact icon-button icon-only chat-send-button"
                    aria-label="Send message"
                    title="Send message"
                    ${chatComposeDisabled || !toolsReady || !state.selectedChatId || !state.selectedProviderId || !state.selectedModelId ? "disabled" : ""}
                  >
                    ${upIcon()}
                  </button>
                </div>
              </div>
            </form>
          </div>
        </section>
        ${renderChatActivitySidebar(selectedConversation)}
      </section>
    </main>
  `;
}

/**
 * Chat bootstrap status banner.
 *
 * Chat readiness is tracked separately from `state.error` so a slow or absent
 * OpenCode server never blocks the rest of the app. This banner is the only
 * place that surfaces `chatBootstrapState` to the user.
 */
function renderChatBootstrapBanner() {
  if (state.chatBootstrapState === "loading") {
    return `
      <div class="signal-banner system chat-bootstrap-banner" aria-busy="true">
        Connecting to the chat runtime…
        ${state.paths?.standalone ? "Starting the local engine and connecting its local MCP tools. First use may initialize scientific libraries; other views remain available." : ""}
      </div>
    `;
  }
  if (state.chatBootstrapState === "ready" && !state.mcpStatus.running) {
    const connecting = state.mcpStatus.connection_state === "connecting";
    if (!connecting) return "";
    return `<div class="signal-banner system chat-bootstrap-banner" role="status" aria-busy="true">
      Connecting local GLESKit tools and loading the tool registry. No model request has been sent; other views remain available.
    </div>`;
  }
  return "";
}

function renderChatConversationRow(conversation) {
  if (state.editingChatConversationId === conversation.conversation_id) {
    return `
      <form id="chat-rename-form" class="chat-list-row chat-list-row-editing">
        <input type="hidden" name="conversation_id" value="${escapeHtml(conversation.conversation_id)}" />
        <input name="title" class="chat-list-edit-input" value="${escapeHtml(conversation.title)}" placeholder="Conversation title" />
        <div class="chat-list-tools chat-list-tools-inline">
          <button type="submit" class="action-button compact">Save</button>
          <button type="button" class="action-button compact" data-action="cancel-rename-chat-conversation">Cancel</button>
        </div>
      </form>
    `;
  }
  return `
    <div class="chat-list-row ${conversation.conversation_id === state.selectedChatId ? "is-active" : ""}">
      <button type="button" class="chat-list-button" data-action="open-chat-conversation" data-conversation-id="${escapeHtml(conversation.conversation_id)}">
        <div class="chat-list-title">${escapeHtml(conversation.title)}</div>
        <div class="chat-list-meta">${escapeHtml(formatTimestamp(conversation.created_at))}</div>
      </button>
      <div class="chat-list-tools">
        <button type="button" class="action-button compact icon-button icon-only" data-action="start-rename-chat-conversation" data-conversation-id="${escapeHtml(conversation.conversation_id)}" aria-label="Rename chat" title="Rename chat">${penIcon()}</button>
        <button type="button" class="action-button compact icon-button icon-only danger" data-action="delete-chat-conversation" data-conversation-id="${escapeHtml(conversation.conversation_id)}" aria-label="Delete chat" title="Delete chat">${trashIcon()}</button>
      </div>
    </div>
  `;
}

function renderChatMessages() {
  const visibleMessages = state.pendingChatUserMessage
    ? [...state.chatMessages, state.pendingChatUserMessage]
    : state.chatMessages;
  if (!state.selectedChatId) {
    return `<div class="empty-state">Create a chat to start talking with a provider.</div>`;
  }
  if (!visibleMessages.length && !state.chatBusy) {
    return "";
  }
  const groups = groupChatMessagesByTurn(visibleMessages);
  let html = groups.map((group) => renderChatTurnGroup(group)).join("");
  const showBusy = state.chatBusy;
  if (showBusy) {
    html += renderChatBusyArticle(state.activeChatTurn?.phase || (state.chatCancellationInProgress ? "cancelling" : "thinking"));
  }
  const approvalPayload = getCurrentChatApprovalPayload();
  if (approvalPayload) {
    html += renderCurrentChatApprovalPanel(approvalPayload);
  }
  return html;
}

function getCurrentChatApprovalPayload() {
  if (state.activeChatTurn?.pendingApproval) {
    return state.activeChatTurn.pendingApproval;
  }
  const activeTurnId = String(state.activeChatTurn?.turn_id || "").trim();
  if (!activeTurnId) {
    return null;
  }
  const events = sortActivityEvents([
    ...(Array.isArray(state.chatTurnEvents) ? state.chatTurnEvents : []),
    ...(Array.isArray(state.activeChatTurn?.events) ? state.activeChatTurn.events : [])
  ].filter((event) => String(event?.turn_id || "").trim() === activeTurnId));
  const approvalEvents = events.filter((event) => event.event_type === "tool.approval_requested");
  for (let index = 0; index < approvalEvents.length; index += 1) {
    if (isApprovalEventPending(approvalEvents[index], approvalEvents, index)) {
      return approvalEvents[index].payload || null;
    }
  }
  return null;
}

function clearChatApprovalOtherState(approvalId) {
  if (!approvalId) {
    return;
  }
  const nextDrafts = { ...(state.chatApprovalReplyDrafts || {}) };
  delete nextDrafts[approvalId];
  state.chatApprovalReplyDrafts = nextDrafts;
  const openIds = new Set(state.chatApprovalOtherOpenIds || []);
  openIds.delete(approvalId);
  state.chatApprovalOtherOpenIds = openIds;
}

function setChatApprovalOtherOpen(approvalId, open) {
  if (!approvalId) {
    return;
  }
  const openIds = new Set(state.chatApprovalOtherOpenIds || []);
  if (open) {
    openIds.add(approvalId);
  } else {
    openIds.delete(approvalId);
  }
  state.chatApprovalOtherOpenIds = openIds;
}

function isChatApprovalOtherOpen(approvalId) {
  return Boolean(approvalId && state.chatApprovalOtherOpenIds?.has(approvalId));
}

function renderCurrentChatApprovalPanel(payload) {
  const approvalId = String(payload?.approval_id || "").trim();
  if (!approvalId) {
    return "";
  }
  return `
    <section class="chat-approval-panel">
      <div class="chat-approval-panel-head">
        <div>
          <div class="mini-label">Approval Needed</div>
          <div class="reference-subtle">${escapeHtml(payload.tool_name || "tool request")}</div>
        </div>
        <span class="status-chip approval-chip">Action required</span>
      </div>
      ${payload.args_summary ? `<div class="reference-copy">${escapeHtml(payload.args_summary)}</div>` : ""}
      <div class="chat-approval-panel-body">
        ${renderApprovalControls(payload, true)}
      </div>
    </section>
  `;
}

function lookupProviderLabel(providerId) {
  return state.chatProviders.find((item) => item.provider_id === providerId)?.label || providerId;
}

function parseToolCalls(raw) {
  if (!raw) {
    return [];
  }
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function chatMessageTurnId(message) {
  if (!message) {
    return "";
  }
  if (String(message.turn_id || "").trim()) {
    return String(message.turn_id).trim();
  }
  if (message.role === "user") {
    return String(message.message_id || message.parent_message_id || message.conversation_id || "").trim();
  }
  return String(message.parent_message_id || message.message_id || message.conversation_id || "").trim();
}

function groupChatMessagesByTurn(messages) {
  const groups = [];
  const lookup = new Map();
  for (const message of Array.isArray(messages) ? messages : []) {
    const turnId = chatMessageTurnId(message) || `turn_${groups.length}`;
    let group = lookup.get(turnId);
    if (!group) {
      group = {
        turn_id: turnId,
        messages: [],
        user_messages: [],
        assistant_messages: []
      };
      lookup.set(turnId, group);
      groups.push(group);
    }
    group.messages.push(message);
    if (message.role === "user") {
      group.user_messages.push(message);
    } else {
      group.assistant_messages.push(message);
    }
  }
  return groups;
}

function aggregateToolCallsForMessages(messages) {
  const aggregated = [];
  const seen = new Set();
  for (const message of Array.isArray(messages) ? messages : []) {
    for (const toolCall of parseToolCalls(message.tool_calls_json)) {
      const key = toolCall.call_id || `${toolCall.name || ""}|${toolCall.args_summary || ""}|${toolCall.result_summary || ""}|${toolCall.provenance_summary || ""}`;
      if (seen.has(key)) {
        continue;
      }
      seen.add(key);
      aggregated.push(toolCall);
    }
  }
  return aggregated;
}

function promptEventForTurn(turnId) {
  if (!turnId) {
    return null;
  }
  const events = [
    ...(Array.isArray(state.chatTurnEvents) ? state.chatTurnEvents : []),
    ...(state.activeChatTurn?.events || [])
  ];
  return events.find((event) => event.turn_id === turnId && event.event_type === "turn.prompt_compiled") || null;
}

function renderChatUserArticle(message) {
  return `
    <article class="chat-message is-user">
      <div class="chat-avatar">${humanAvatarIcon()}</div>
      <div class="chat-message-content">
        <div class="chat-message-meta">
          <span class="chat-message-author">You</span>
          <span class="chat-message-time">${escapeHtml(formatTimestamp(message.created_at))}</span>
        </div>
        <div class="markdown-preview chat-message-body">${renderMarkdownPreview(message.content_markdown || "")}</div>
      </div>
    </article>
  `;
}

function renderChatAssistantArticle(messages, turnId) {
  const assistantMessages = Array.isArray(messages) ? messages : [];
  const firstMessage = assistantMessages[0] || null;
  const authorName = firstMessage?.provider_id
    ? `${lookupProviderLabel(firstMessage.provider_id)} \u00b7 ${firstMessage.model_id || ""}`.trim()
    : "Assistant";
  const promptEvent = promptEventForTurn(turnId);
  const toolCalls = aggregateToolCallsForMessages(assistantMessages);
  const fragments = assistantMessages
    .map((message, index) => {
      const body = String(message.content_markdown || "").trim();
      const hasBody = Boolean(body);
      const hasError = Boolean(String(message.error || "").trim());
      if (!hasBody && !hasError) {
        return "";
      }
      return `
        <div class="chat-turn-fragment${index > 0 ? " is-followup" : ""}">
          ${hasBody ? `<div class="markdown-preview chat-message-body">${renderMarkdownPreview(message.content_markdown || "")}</div>` : ""}
          ${hasError ? `<div class="chat-turn-fragment-error">${escapeHtml(message.error || "")}</div>` : ""}
        </div>
      `;
    })
    .filter(Boolean)
    .join("");
  return `
    <article class="chat-message is-assistant chat-turn-assistant">
      <div class="chat-avatar">${botAvatarIcon()}</div>
      <div class="chat-message-content">
        <div class="chat-message-meta">
          <span class="chat-message-author">${escapeHtml(authorName)}</span>
          <span class="chat-message-time">${escapeHtml(formatTimestamp(firstMessage?.created_at || new Date().toISOString()))}</span>
        </div>
        ${promptEvent ? renderPromptTrace(promptEvent) : ""}
        ${toolCalls.length ? renderToolCallTrace(toolCalls) : ""}
        <div class="chat-turn-fragments">
          ${fragments || `<div class="chat-turn-fragment-empty">No markdown content.</div>`}
        </div>
      </div>
    </article>
  `;
}

function renderChatBusyArticle(phase) {
  return `
    <article class="chat-message is-assistant chat-turn-assistant">
      <div class="chat-avatar">${botAvatarIcon()}</div>
      <div class="chat-message-content">
        <div class="chat-message-meta">
          <span class="chat-message-author">${escapeHtml(getSelectedChatProvider()?.label || "Assistant")}</span>
        </div>
        <div class="chat-message-body chat-busy-state">
          <span class="chat-busy-dots" aria-hidden="true">
            <span class="chat-busy-dot"></span>
            <span class="chat-busy-dot"></span>
            <span class="chat-busy-dot"></span>
          </span>
          <span class="chat-busy-text">${escapeHtml(phase || "Working...")}</span>
        </div>
      </div>
    </article>
  `;
}

function renderChatTurnGroup(group) {
  const userMessages = Array.isArray(group?.user_messages) ? group.user_messages : [];
  const assistantMessages = Array.isArray(group?.assistant_messages) ? group.assistant_messages : [];
  return `
    <section class="chat-turn-group">
      ${userMessages.map((message) => renderChatUserArticle(message)).join("")}
      ${assistantMessages.length ? renderChatAssistantArticle(assistantMessages, group.turn_id) : ""}
    </section>
  `;
}

function renderPromptTrace(event) {
  const payload = event?.payload || {};
  const len = Number(payload.system_prompt_len || 0);
  const hash = String(payload.system_prompt_hash || "");
  const suffix = hash ? ` \u00b7 ${hash}` : "";
  return `
    <div class="system-prompt-trace">
      <span>System prompt attached</span>
      <span>${escapeHtml(`${len.toLocaleString()} chars${suffix}`)}</span>
    </div>
  `;
}

function renderToolCallTrace(toolCalls) {
  return `
    <details class="tool-calls-trace">
      <summary>Used ${toolCalls.length} GLESKit tool${toolCalls.length === 1 ? "" : "s"}: ${escapeHtml(toolCalls.map((item) => item.name).join(", "))}</summary>
      <ul class="tool-calls-list">
        ${toolCalls.map((item) => `<li><strong>${escapeHtml(item.name)}</strong> <span>${escapeHtml(item.args_summary || "")}</span> <em>${escapeHtml(item.result_summary || "")}</em>${item.provenance_summary ? `<div class="chat-tool-provenance">${escapeHtml(item.provenance_summary)}</div>` : ""}</li>`).join("")}
      </ul>
    </details>
  `;
}

function renderChatActivitySidebar(selectedConversation) {
  if (!state.chatActivitySidebarOpen) {
    return "";
  }
  const turns = groupChatTurnEvents(sortActivityEvents(visibleChatTurnEvents()).slice(-160)).slice(-CHAT_ACTIVITY_TURN_LIMIT);
  return `
    <aside class="chat-activity-sidebar">
      <div class="chat-activity-sidebar-head">
        <div>
          <div class="mini-label">Activity</div>
          <h3>Run Timeline</h3>
        </div>
        <div class="chat-activity-sidebar-actions">
          ${selectedConversationAllowsAlways() ? `<button type="button" class="action-button compact" data-action="reset-chat-tool-approval">Reset Approvals</button>` : ""}
          <button type="button" class="action-button compact" data-action="toggle-chat-activity-sidebar">Hide</button>
        </div>
      </div>
      <div class="chat-activity-sidebar-meta">
        <span class="status-chip ${selectedConversationAllowsAlways() ? "positive" : ""}">Approvals ${escapeHtml(selectedConversationAllowsAlways() ? "Conversation" : "Ask Every Time")}</span>
        ${state.activeChatTurn ? `<span class="status-chip positive">${escapeHtml(state.activeChatTurn.phase || "thinking")}</span>` : ""}
      </div>
      <div class="chat-activity-sidebar-scroll">
        ${turns.length
          ? turns.map((turn, index) => renderActivityTurnGroup(turn, index, turns.length)).join("")
          : `<div class="empty-state">No activity yet for this chat.</div>`}
      </div>
    </aside>
  `;
}

function groupChatTurnEvents(events) {
  const sortedEvents = sortActivityEvents(events);
  const groups = [];
  const lookup = new Map();
  for (const event of sortedEvents) {
    const turnId = String(event.turn_id || "").trim() || "turn_unknown";
    const existing = lookup.get(turnId);
    if (existing) {
      existing.events.push(event);
    } else {
      const group = { turn_id: turnId, events: [event] };
      lookup.set(turnId, group);
      groups.push(group);
    }
  }
  return groups;
}

function sortActivityEvents(events) {
  return [...(Array.isArray(events) ? events : [])].sort((left, right) => {
    const leftTime = String(left?.created_at || "");
    const rightTime = String(right?.created_at || "");
    if (leftTime !== rightTime) {
      return leftTime.localeCompare(rightTime);
    }
    return String(left?.event_id || "").localeCompare(String(right?.event_id || ""));
  });
}

function renderActivityTurnGroup(turn, index, totalTurns) {
  const events = turn.events || [];
  const firstEvent = events[0] || null;
  const lastEvent = events[events.length - 1] || null;
  const title = `Run ${index + 1}`;
  const meta = firstEvent ? formatTimestamp(firstEvent.created_at) : "";
  const phase = lastEvent ? describeActivityEvent(lastEvent).title : "No activity";
  const isOpen = index === totalTurns - 1;
  const toolEventCount = countActivityToolCalls(events);
  const skillEventCount = events.filter((event) => [
    "turn.skills_available",
    "skill.loaded",
    "skill.file_loaded"
  ].includes(event.event_type)).length;
  const summaryMeta = [
    meta,
    phase,
    `${events.length} event${events.length === 1 ? "" : "s"}`,
    toolEventCount ? `${toolEventCount} tool${toolEventCount === 1 ? "" : "s"}` : "",
    skillEventCount ? `${skillEventCount} skill${skillEventCount === 1 ? "" : "s"}` : ""
  ].filter(Boolean).join(" · ");
  return `
    <details class="activity-turn-group" ${isOpen ? "open" : ""}>
      <summary>
        <span>${escapeHtml(title)}</span>
        <span class="activity-turn-meta">${escapeHtml(summaryMeta)}</span>
      </summary>
      <div class="activity-turn-body">
        ${events.map((event, eventIndex) => renderActivityEventItem(event, { compact: false, events, eventIndex })).join("")}
      </div>
    </details>
  `;
}

function inlineActivityEventTypes() {
  return Array.from(TRACKED_CHAT_EVENT_TYPES);
}

function renderActivityEventItem(event, { compact, events = [], eventIndex = -1 }) {
  const summary = describeActivityEvent(event);
  const payload = event.payload || {};
  const classes = ["activity-event-item", compact ? "is-compact" : "is-detailed"];
  const isPendingApproval = isApprovalEventPending(event, events, eventIndex);
  const orderLabel = eventIndex >= 0 ? `#${String(eventIndex + 1).padStart(2, "0")} · ` : "";
  if (isPendingApproval) {
    classes.push("needs-approval");
  }
  if (compact) {
    return `
      <div class="${classes.join(" ")}">
        <div class="activity-event-head">
          <span>${escapeHtml(`${orderLabel}${summary.title}`)}</span>
          <span class="activity-event-meta">${escapeHtml(summary.meta)}</span>
        </div>
        ${summary.copy ? `<div class="activity-event-copy">${escapeHtml(summary.copy)}</div>` : ""}
      </div>
    `;
  }
  return `
    <details class="${classes.join(" ")}">
      <summary>
        <span>${escapeHtml(`${orderLabel}${summary.title}`)}</span>
        <span class="activity-event-meta">${escapeHtml(summary.meta)}</span>
      </summary>
      <div class="activity-event-body">
        ${summary.copy ? `<div class="activity-event-copy">${escapeHtml(summary.copy)}</div>` : ""}
        ${renderActivityEventDetails(event, compact)}
        ${event.event_type === "tool.approval_requested" && isPendingApproval ? renderApprovalControls(payload, isPendingApproval) : ""}
      </div>
    </details>
  `;
}

function isApprovalEventPending(event, events, eventIndex) {
  if (event?.event_type !== "tool.approval_requested") {
    return false;
  }
  const approvalId = String(event.payload?.approval_id || "").trim();
  if (approvalId && state.dismissedApprovalIds.has(approvalId)) {
    return false;
  }
  const toolName = String(event.payload?.tool_name || "").trim();
  if (!toolName) {
    return true;
  }
  const laterEvents = Array.isArray(events) && eventIndex >= 0 ? events.slice(eventIndex + 1) : [];
  return !laterEvents.some((candidate) => {
    if (!candidate || String(candidate.payload?.tool_name || "").trim() !== toolName) {
      return false;
    }
    return [
      "tool.approved",
      "tool.rejected",
      "tool.started",
      "tool.failed",
      "tool.finished"
    ].includes(candidate.event_type);
  });
}

function renderActivityEventDetails(event, compact) {
  const payload = event.payload || {};
  if (event.event_type === "system.warning" || event.event_type === "system.error") {
    const detailSource = payload.error ?? payload.message ?? payload;
    return `
      <div class="activity-event-copy">${escapeHtml(formatError(detailSource))}</div>
    `;
  }
  if (event.event_type === "turn.skills_available") {
    return renderActivitySkillEntries(Array.isArray(payload.skills) ? payload.skills : [], compact);
  }
  if (event.event_type === "skill.loaded" || event.event_type === "skill.file_loaded") {
    const content = String(payload.content || payload.content_markdown || payload.markdown || "").trim();
    const pieces = [];
    if (payload.skill_name || payload.name) {
      pieces.push(`<div class="activity-event-copy">${escapeHtml(`Skill ${payload.skill_name || payload.name}`)}</div>`);
    }
    if (payload.path) {
      pieces.push(`<div class="activity-event-copy">${escapeHtml(`Path: ${payload.path}`)}</div>`);
    }
    if (payload.result_summary) {
      pieces.push(`<div class="activity-event-copy">${escapeHtml(payload.result_summary)}</div>`);
    }
    if (content) {
      pieces.push(renderActivityCodeBlock("Content", content, compact));
    }
    return pieces.join("");
  }
  if (event.event_type === "tool.approval_requested" || event.event_type === "tool.started") {
    const pieces = [];
    if (payload.tool_name) {
      pieces.push(`<div class="activity-event-copy">${escapeHtml(`Tool: ${payload.tool_name}`)}</div>`);
    }
    if (payload.args_summary) {
      pieces.push(`<div class="activity-event-copy">${escapeHtml(payload.args_summary)}</div>`);
    }
    if (payload.args_json) {
      pieces.push(renderActivityCodeBlock("Arguments", payload.args_json, compact));
    }
    return pieces.join("");
  }
  if (event.event_type === "tool.finished") {
    const pieces = [];
    if (payload.tool_name) {
      pieces.push(`<div class="activity-event-copy">${escapeHtml(`Tool: ${payload.tool_name}`)}</div>`);
    }
    if (payload.result_summary) {
      pieces.push(`<div class="activity-event-copy">${escapeHtml(payload.result_summary)}</div>`);
    }
    if (payload.provenance_summary) {
      pieces.push(`<div class="activity-event-copy">${escapeHtml(`Provenance: ${payload.provenance_summary}`)}</div>`);
    }
    if (payload.result_json) {
      pieces.push(renderActivityCodeBlock("Result", payload.result_json, compact));
    }
    return pieces.join("");
  }
  if (event.event_type === "tool.failed") {
    const pieces = [];
    if (payload.tool_name) {
      pieces.push(`<div class="activity-event-copy">${escapeHtml(`Tool: ${payload.tool_name}`)}</div>`);
    }
    if (payload.error) {
      pieces.push(`<div class="activity-event-copy">${escapeHtml(payload.error)}</div>`);
    }
    if (payload.error_json) {
      pieces.push(renderActivityCodeBlock("Error", payload.error_json, compact));
    }
    return pieces.join("");
  }
  if (event.event_type === "turn.provider_request_started" || event.event_type === "turn.provider_request_finished") {
    const pieces = [];
    if (payload.content_preview) {
      pieces.push(renderActivityCodeBlock("Preview", payload.content_preview, compact));
    }
    if (payload.response_preview && payload.response_preview !== payload.content_preview) {
      pieces.push(renderActivityCodeBlock("Response", payload.response_preview, compact));
    }
    if (payload.request_json) {
      pieces.push(renderActivityCodeBlock("Request", payload.request_json, compact));
    }
    return pieces.join("");
  }
  return "";
}

function renderActivitySkillEntries(skills, compact) {
  if (!Array.isArray(skills) || !skills.length) {
    return "";
  }
  return `
    <div class="activity-event-section">
      <div class="mini-label">Skills</div>
      <div class="activity-skill-list">
        ${skills.map((skill, index) => {
          const label = skill?.label || skill?.skill_name || skill?.name || `Skill ${index + 1}`;
          const description = String(skill?.description || skill?.summary || "").trim();
          const content = String(skill?.content || skill?.content_markdown || skill?.markdown || skill?.text || "").trim();
          const filePaths = Array.isArray(skill?.file_paths) ? skill.file_paths.filter(Boolean) : [];
          return `
            <div class="activity-skill-item">
              <div class="activity-event-head">
                <span>${escapeHtml(label)}</span>
                <span class="activity-event-meta">${escapeHtml(filePaths.length ? filePaths.join(" · ") : "")}</span>
              </div>
              ${description ? `<div class="activity-event-copy">${escapeHtml(description)}</div>` : ""}
              ${content ? renderActivityCodeBlock("Content", content, compact) : ""}
              ${filePaths.length ? `<div class="activity-skill-files">${escapeHtml(filePaths.join(" · "))}</div>` : ""}
            </div>
          `;
        }).join("")}
      </div>
    </div>
  `;
}

function truncateActivityLines(value, maxLines = 200) {
  const text = String(value || "").replace(/\r\n/g, "\n");
  const lines = text.split("\n");
  if (lines.length <= maxLines) {
    return text;
  }
  return `${lines.slice(0, maxLines).join("\n")}\n… truncated after ${maxLines.toLocaleString()} lines`;
}

function renderActivityCodeBlock(label, value, compact) {
  const text = truncateActivityLines(String(value || ""), compact ? 48 : 200);
  return `
    <div class="activity-event-section">
      <div class="mini-label">${escapeHtml(label)}</div>
      <pre class="activity-event-code"><code>${escapeHtml(text)}</code></pre>
    </div>
  `;
}

function renderApprovalControls(payload, isPendingApproval = false) {
  const approvalId = String(payload.approval_id || "");
  if (!approvalId) {
    return "";
  }
  const otherOpen = isChatApprovalOtherOpen(approvalId);
  const draft = String(state.chatApprovalReplyDrafts?.[approvalId] || "");
  return `
    <div class="activity-approval-actions ${isPendingApproval ? "is-pending" : ""}">
      <div class="activity-approval-button-row">
        <button type="button" class="action-button compact primary ${isPendingApproval ? "approval-spotlight" : ""}" data-action="approve-chat-tool-call" data-approval-id="${escapeHtml(approvalId)}" data-approval-mode="allow">Allow</button>
        <button type="button" class="action-button compact ${isPendingApproval ? "approval-spotlight-secondary" : ""}" data-action="approve-chat-tool-call" data-approval-id="${escapeHtml(approvalId)}" data-approval-mode="allow_always">Allow Always</button>
        <button type="button" class="action-button compact danger" data-action="approve-chat-tool-call" data-approval-id="${escapeHtml(approvalId)}" data-approval-mode="deny">Deny</button>
        <button type="button" class="action-button compact" data-action="toggle-chat-tool-approval-other" data-approval-id="${escapeHtml(approvalId)}">${otherOpen ? "Hide Other" : "Other"}</button>
      </div>
      ${otherOpen ? `
        <form class="chat-tool-approval-other-form" data-approval-id="${escapeHtml(approvalId)}">
          <textarea
            name="content"
            class="chat-tool-approval-other-input"
            data-action="chat-tool-approval-other-input"
            data-approval-id="${escapeHtml(approvalId)}"
            rows="3"
            placeholder="Type a custom reply to send to the model..."
          >${escapeHtml(draft)}</textarea>
          <div class="chat-tool-approval-other-actions">
            <button type="submit" class="action-button compact primary">Send Reply</button>
            <button type="button" class="action-button compact" data-action="toggle-chat-tool-approval-other" data-approval-id="${escapeHtml(approvalId)}">Cancel</button>
          </div>
        </form>
      ` : ""}
    </div>
  `;
}

function describeActivityEvent(event) {
  const payload = event.payload || {};
  const createdAt = formatTimestamp(event.created_at);
  if (event.event_type === "turn.prompt_compiled") {
    const len = Number(payload.system_prompt_len || 0);
    const hash = String(payload.system_prompt_hash || "");
    return {
      title: "System prompt attached",
      meta: createdAt,
      copy: `${len.toLocaleString()} chars${hash ? ` · hash ${hash}` : ""}`
    };
  }
  if (event.event_type === "turn.started") {
    const pipelineMode = Boolean(payload.pipeline_mode);
    const inputCount = Array.isArray(payload.input_data_refs) ? payload.input_data_refs.length : 0;
    return {
      title: pipelineMode ? "Pipeline mode activated" : "Turn started",
      meta: createdAt,
      copy: pipelineMode
        ? `The turn will use a single run_pipeline call.${inputCount ? ` ${inputCount} input ref${inputCount === 1 ? "" : "s"} attached.` : ""}`
        : "The turn has started."
    };
  }
  if (event.event_type === "turn.skills_available") {
    const skills = Array.isArray(payload.skills) ? payload.skills : [];
    const fileCount = skills.reduce((count, skill) => count + (skill.file_paths || []).length, 0);
    return {
      title: `Active ${skills.length} skill${skills.length === 1 ? "" : "s"}`,
      meta: createdAt,
      copy: [
        fileCount ? `${fileCount} supporting file${fileCount === 1 ? "" : "s"} available on demand.` : "",
        skills.length ? "Skill details are expanded below in turn order." : "Only SKILL.md entrypoints are active for this turn."
      ].filter(Boolean).join(" ")
    };
  }
  if (event.event_type === "system.warning") {
    return {
      title: "OpenCode warning",
      meta: createdAt,
      copy: formatError(payload.error ?? payload.message ?? payload)
    };
  }
  if (event.event_type === "system.error") {
    return {
      title: "OpenCode error",
      meta: createdAt,
      copy: formatError(payload.error ?? payload.message ?? payload)
    };
  }
  if (event.event_type === "skill.loaded") {
    return {
      title: `Loaded skill · ${payload.skill_name || "skill"}`,
      meta: createdAt,
      copy: payload.result_summary || "Loaded SKILL.md for the active skill."
    };
  }
  if (event.event_type === "skill.file_loaded") {
    return {
      title: `Loaded file · ${payload.path || payload.skill_name || "skill file"}`,
      meta: createdAt,
      copy: payload.result_summary || "Loaded a supporting skill file."
    };
  }
  if (event.event_type === "tool.approval_requested") {
    return {
      title: `Approval needed · ${payload.tool_name || "tool"}`,
      meta: createdAt,
      copy: payload.args_summary || "The assistant requested a tool call."
    };
  }
  if (event.event_type === "tool.approved") {
    return {
      title: `Allowed · ${payload.tool_name || "tool"}`,
      meta: createdAt,
      copy: payload.scope === "conversation" ? "Tool access is now unlocked for this conversation." : "Approved for this call only."
    };
  }
  if (event.event_type === "tool.rejected") {
    return {
      title: `Denied · ${payload.tool_name || "tool"}`,
      meta: createdAt,
      copy: payload.result_summary || "The tool call was denied."
    };
  }
  if (event.event_type === "tool.started") {
    return {
      title: `Running · ${payload.tool_name || "tool"}`,
      meta: createdAt,
      copy: payload.args_summary || "Executing tool call."
    };
  }
  if (event.event_type === "tool.finished") {
    const provenance = String(payload.provenance_summary || "").trim();
    return {
      title: `Completed · ${payload.tool_name || "tool"}`,
      meta: createdAt,
      copy: provenance
        ? `${payload.result_summary || "Tool call completed."} | ${provenance}`
        : (payload.result_summary || "Tool call completed.")
    };
  }
  if (event.event_type === "tool.failed") {
    return {
      title: `Failed · ${payload.tool_name || "tool"}`,
      meta: createdAt,
      copy: payload.error || "Tool call failed."
    };
  }
  if (event.event_type === "turn.provider_request_started") {
    return {
      title: `Provider request ${payload.iteration || 1}`,
      meta: createdAt,
      copy: payload.with_tools ? "Awaiting model response with tool access enabled." : "Awaiting model response."
    };
  }
  if (event.event_type === "turn.provider_request_finished") {
    return {
      title: `Provider response ${payload.iteration || 1}`,
      meta: createdAt,
      copy: payload.content_preview || `Returned ${payload.tool_call_count || 0} tool call(s).`
    };
  }
  if (event.event_type === "turn.completed") {
    return {
      title: "Turn completed",
      meta: createdAt,
      copy: Number.isInteger(payload.tool_count)
        ? `${payload.tool_count} tool call${payload.tool_count === 1 ? "" : "s"} completed.`
        : "The turn completed."
    };
  }
  if (event.event_type === "turn.failed") {
    return {
      title: "Turn failed",
      meta: createdAt,
      copy: payload.error || "The turn ended with an error."
    };
  }
  if (event.event_type === "turn.cancelled") {
    return {
      title: "Turn cancelled",
      meta: createdAt,
      copy: payload.reason || "The turn was cancelled."
    };
  }
  return {
    title: event.event_type || "activity",
    meta: createdAt,
    copy: ""
  };
}

function renderCreateForm() {
  return `
    <form id="create-session-form" class="frame composer-panel">
      <div class="mini-label">Compose</div>
      <input name="title" placeholder="Circadian clock × cancer" />
      <textarea name="description" placeholder="Mechanistic study linking circadian genes, DNA damage, tumor suppression, and oncogenic signaling."></textarea>
      <div class="inline-form compact">
        <input name="species" value="human" />
        <button class="action-button primary compact" type="submit">Create</button>
      </div>
    </form>
  `;
}

function renderDeriveForm() {
  return `
    <form id="derive-session-form" class="frame composer-panel">
      <div class="mini-label">Synthesize</div>
      <input name="title" placeholder="Cross-study synthesis" />
      <textarea name="description" placeholder="Copy the selected artifacts into a new comparison workspace."></textarea>
      <div class="inline-form compact">
        <input name="species" value="human" />
        <button class="action-button primary compact" type="submit">Create</button>
      </div>
      <div class="helper-copy">${state.selectedArtifacts.size} artifact(s) selected</div>
    </form>
  `;
}

function renderOverviewHeader(selectedSession) {
  const sessionTitle = state.sessionSummary?.title || selectedSession?.title || "No Session Selected";
  const description = state.sessionSummary?.description || selectedSession?.description || "No session selected yet.";
  // Counts come from the session snapshot. Until it lands they are unknown, so
  // render an em dash rather than a misleading zero.
  const summaryLoaded = state.sessionSnapshotStatus === "ready";
  const metricValue = (value) => (summaryLoaded && value !== undefined && value !== null ? String(value) : "—");
  const notes = state.sessionSummary?.note_count;
  const reports = state.sessionSummary?.report_count ?? selectedSession?.report_count;
  const artifactCount = state.sessionSummary?.artifact_count ?? selectedSession?.artifact_count;
  const figureCount = state.sessionSummary?.figure_count ?? state.sessionSummary?.artifact_counts?.figure;
  const species = state.sessionSummary?.species || selectedSession?.species || "human";
  const status = selectedSession && state.runtime.active_session_id === selectedSession.session_id ? "active" : "inactive";
  const lastNote = state.sessionSummary?.notes?.[0]?.excerpt || "";
  return `
    <section class="frame focus-strip">
      <div class="focus-header">
        <div class="focus-copy">
          <div class="mini-label">Selected Session</div>
          ${
            selectedSession
              ? `<div class="session-id-row"><span class="session-id-pill">${escapeHtml(selectedSession.session_id)}</span></div>`
              : ""
          }
          ${
            state.showEditSessionForm && selectedSession
              ? renderEditSessionForm(selectedSession, sessionTitle, description)
              : `<h2>${escapeHtml(sessionTitle)}</h2>`
          }
        </div>
        <div class="focus-actions">
          ${renderSessionActionButtons(selectedSession)}
        </div>
      </div>
      ${
        state.showEditSessionForm && selectedSession
          ? ""
          : `<p class="focus-description">${escapeHtml(description)}</p>`
      }
      ${
        !state.showEditSessionForm && lastNote
          ? `<div class="focus-note" title="${escapeHtml(lastNote)}">${escapeHtml(truncate(lastNote, 108))}</div>`
          : ""
      }
      <div class="focus-meta">
        ${renderMetricTile("Species", species, "")}
        ${renderMetricTile("Status", status, status === "active" ? "positive" : "")}
        ${renderMetricTile("Artifacts", metricValue(artifactCount), "")}
        ${renderMetricTile("Figures", metricValue(figureCount), "")}
        ${renderMetricTile("Notes", metricValue(notes), "")}
        ${renderMetricTile("Reports", metricValue(reports), "")}
      </div>
    </section>
  `;
}

function renderEditSessionForm(selectedSession, title, description) {
  return `
    <form id="edit-session-form" class="focus-editor">
      <input type="hidden" name="session_id" value="${escapeHtml(selectedSession.session_id)}" />
      <input
        name="title"
        class="focus-editor-title"
        value="${escapeHtml(title)}"
        placeholder="Session title"
      />
      <textarea
        name="description"
        class="focus-editor-description"
        placeholder="Session description"
      >${escapeHtml(description)}</textarea>
      <div class="focus-editor-actions">
        <button type="button" class="action-button compact" data-action="cancel-edit-session">Cancel</button>
        <button type="submit" class="action-button compact primary">Save</button>
      </div>
    </form>
  `;
}

function renderMetricTile(label, value, tone) {
  return `
    <div class="metric-tile ${tone}">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </div>
  `;
}

function renderContentTabs(hasSelectedSession) {
  return `
    <div class="view-tabs" role="tablist" aria-label="Workspace views">
      <button
        type="button"
        class="view-tab ${state.page === "workspace" ? "is-active" : ""}"
        data-action="show-workspace"
        role="tab"
        aria-selected="${state.page === "workspace"}"
      >
        Sessions
      </button>
      <button
        type="button"
        class="view-tab ${state.page === "artifacts" ? "is-active" : ""}"
        data-action="show-artifacts"
        role="tab"
        aria-selected="${state.page === "artifacts"}"
        ${hasSelectedSession ? "" : "disabled"}
      >
        Artifacts
      </button>
      <button
        type="button"
        class="view-tab ${state.page === "uploads" ? "is-active" : ""}"
        data-action="show-uploads"
        role="tab"
        aria-selected="${state.page === "uploads"}"
        ${hasSelectedSession ? "" : "disabled"}
      >
        Uploads
      </button>
      <button
        type="button"
        class="view-tab ${state.page === "figures" ? "is-active" : ""}"
        data-action="show-figures"
        role="tab"
        aria-selected="${state.page === "figures"}"
        ${hasSelectedSession ? "" : "disabled"}
      >
        Figures
      </button>
      <button
        type="button"
        class="view-tab ${state.page === "notes" ? "is-active" : ""}"
        data-action="show-notes"
        role="tab"
        aria-selected="${state.page === "notes"}"
        ${hasSelectedSession ? "" : "disabled"}
      >
        Notes
      </button>
      <button
        type="button"
        class="view-tab ${state.page === "reports" ? "is-active" : ""}"
        data-action="show-reports"
        role="tab"
        aria-selected="${state.page === "reports"}"
        ${hasSelectedSession ? "" : "disabled"}
      >
        Reports
      </button>
    </div>
  `;
}

function renderConfirmationDialog() {
  if (!state.confirmation) {
    return "";
  }
  if (state.confirmation.kind === "delete-session") {
    return `
      <div class="modal-backdrop">
        <section class="frame modal-card" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
          <div class="mini-label">Warning</div>
          <h2 id="confirm-title">Delete Session?</h2>
          <p class="modal-copy">
            This will permanently erase <strong>${escapeHtml(state.confirmation.title)}</strong>,
            including all saved artifacts, uploads, notes, and generated reports.
          </p>
          <div class="modal-actions">
            <button type="button" class="action-button compact" data-action="cancel-confirmation">Cancel</button>
            <button
              type="button"
              class="action-button compact danger"
              data-action="confirm-delete-session"
              data-session-id="${escapeHtml(state.confirmation.sessionId)}"
            >
              Delete Permanently
            </button>
          </div>
        </section>
      </div>
    `;
  }
  if (state.confirmation.kind === "delete-artifacts") {
    const count = state.confirmation.artifactIds?.length || 0;
    const isFigureDelete = state.confirmation.targetPage === "figures";
    return `
      <div class="modal-backdrop">
        <section class="frame modal-card" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
          <div class="mini-label">Warning</div>
          <h2 id="confirm-title">Delete Selected ${isFigureDelete ? "Figures" : "Artifacts"}?</h2>
          <p class="modal-copy">
            This will permanently erase <strong>${count}</strong> ${isFigureDelete ? `figure${count === 1 ? "" : "s"}` : `artifact${count === 1 ? "" : "s"}`} from
            <strong>${escapeHtml(state.confirmation.title)}</strong>.
          </p>
          <div class="modal-actions">
            <button type="button" class="action-button compact" data-action="cancel-confirmation">Cancel</button>
            <button
              type="button"
              class="action-button compact danger"
              data-action="confirm-delete-artifacts"
              data-session-id="${escapeHtml(state.confirmation.sessionId)}"
            >
              Delete Permanently
            </button>
          </div>
        </section>
      </div>
    `;
  }
  if (state.confirmation.kind === "delete-notes") {
    const count = state.confirmation.noteIds?.length || 0;
    return `
      <div class="modal-backdrop">
        <section class="frame modal-card" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
          <div class="mini-label">Warning</div>
          <h2 id="confirm-title">Delete Selected Notes?</h2>
          <p class="modal-copy">
            This will permanently erase <strong>${count}</strong> note${count === 1 ? "" : "s"} from
            <strong>${escapeHtml(state.confirmation.title)}</strong>.
          </p>
          <div class="modal-actions">
            <button type="button" class="action-button compact" data-action="cancel-confirmation">Cancel</button>
            <button
              type="button"
              class="action-button compact danger"
              data-action="confirm-delete-notes"
              data-session-id="${escapeHtml(state.confirmation.sessionId)}"
            >
              Delete Permanently
            </button>
          </div>
        </section>
      </div>
    `;
  }
  if (state.confirmation.kind === "delete-reports") {
    const count = state.confirmation.reportIds?.length || 0;
    return `
      <div class="modal-backdrop">
        <section class="frame modal-card" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
          <div class="mini-label">Warning</div>
          <h2 id="confirm-title">Delete Selected Reports?</h2>
          <p class="modal-copy">
            This will permanently erase <strong>${count}</strong> report${count === 1 ? "" : "s"} from
            <strong>${escapeHtml(state.confirmation.title)}</strong>.
          </p>
          <div class="modal-actions">
            <button type="button" class="action-button compact" data-action="cancel-confirmation">Cancel</button>
            <button
              type="button"
              class="action-button compact danger"
              data-action="confirm-delete-reports"
              data-session-id="${escapeHtml(state.confirmation.sessionId)}"
            >
              Delete Permanently
            </button>
          </div>
        </section>
      </div>
    `;
  }
  if (state.confirmation.kind === "delete-chat-conversation") {
    return `
      <div class="modal-backdrop">
        <section class="frame modal-card" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
          <div class="mini-label">Warning</div>
          <h2 id="confirm-title">Delete Chat?</h2>
          <p class="modal-copy">
            This will permanently erase <strong>${escapeHtml(state.confirmation.title)}</strong>
            and all messages in this conversation.
          </p>
          <div class="modal-actions">
            <button type="button" class="action-button compact" data-action="cancel-confirmation">Cancel</button>
            <button
              type="button"
              class="action-button compact danger"
              data-action="confirm-delete-chat-conversation"
              data-conversation-id="${escapeHtml(state.confirmation.conversationId)}"
            >
              Delete Permanently
            </button>
          </div>
        </section>
      </div>
    `;
  }
  if (state.confirmation.kind === "rename-cohort") {
    return `
      <div class="modal-backdrop">
        <section class="frame modal-card" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
          <div class="mini-label">Cohort</div>
          <h2 id="confirm-title">Rename Cohort</h2>
          <form id="cohort-rename-form" class="modal-form">
            <input type="hidden" name="cohort_id" value="${escapeHtml(state.confirmation.cohortId)}" />
            <label class="modal-field">
              <span>New title</span>
              <input name="title" value="${escapeHtml(state.confirmation.draftTitle || state.confirmation.title || "")}" />
            </label>
            <div class="modal-actions">
              <button type="button" class="action-button compact" data-action="cancel-confirmation">Cancel</button>
              <button type="submit" class="action-button compact primary">Save</button>
            </div>
          </form>
        </section>
      </div>
    `;
  }
  if (state.confirmation.kind === "delete-cohort") {
    return `
      <div class="modal-backdrop">
        <section class="frame modal-card" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
          <div class="mini-label">Warning</div>
          <h2 id="confirm-title">Delete Cohort?</h2>
          <p class="modal-copy">
            This will permanently erase <strong>${escapeHtml(state.confirmation.title)}</strong>
            from the local cohort library.
          </p>
          <div class="modal-actions">
            <button type="button" class="action-button compact" data-action="cancel-confirmation">Cancel</button>
            <button
              type="button"
              class="action-button compact danger"
              data-action="confirm-delete-cohort"
              data-cohort-id="${escapeHtml(state.confirmation.cohortId)}"
            >
              Delete Permanently
            </button>
          </div>
        </section>
      </div>
    `;
  }
  return "";
}

function renderWorkspaceView(selectedSession) {
  return `
    <section class="frame data-panel">
      <div class="panel-bar">
        <div class="panel-title-stack">
          <div>
            <div class="mini-label">Queue</div>
            <h2>Session Matrix</h2>
          </div>
          <div class="panel-chip">${state.sessions.length} ${state.sessions.length === 1 ? "session" : "sessions"}</div>
        </div>
        <div class="panel-actions">
          <form id="session-search-form" class="search-shell">
            <input id="session-search" value="${escapeHtml(state.sessionQuery)}" placeholder="Search sessions, tags, notes..." />
            <button class="action-button compact" type="submit">Filter</button>
          </form>
        </div>
      </div>
      <div class="table-wrap session-table-wrap">
        ${renderSessionTable(selectedSession)}
      </div>
    </section>
  `;
}

function renderArtifactView(selectedSession, visibleArtifacts, selectedArtifact, detailJson) {
  if (!selectedSession) {
    return `
      <section class="frame data-panel">
        <div class="empty-state">Select a session from the workspace view before opening artifacts.</div>
      </section>
    `;
  }
  const selectedCount = selectedArtifactsForSession(selectedSession.session_id).length;
  const allVisibleSelected = allArtifactsSelected(selectedSession.session_id, visibleArtifacts);
  const selectAllLabel = allVisibleSelected
    ? (visibleArtifacts.length === state.artifacts.length ? "Clear All" : "Clear Shown")
    : (visibleArtifacts.length === state.artifacts.length ? "Select All" : "Select Shown");
  return `
    <section class="artifact-stack">
      <section class="frame data-panel">
        <div class="panel-bar">
          <div class="panel-title-stack">
            <div>
              <div class="mini-label">Artifacts</div>
              <h2>${escapeHtml(selectedSession.title)}</h2>
            </div>
            <div class="panel-chip">${visibleArtifacts.length} shown / ${state.artifacts.length} total</div>
          </div>
          <div class="panel-actions">
            <input id="artifact-search" value="${escapeHtml(state.artifactQuery)}" placeholder="Filter artifacts..." />
            ${
              selectedCount
                ? `
                  <button
                    type="button"
                    class="action-button compact danger icon-button icon-only"
                    data-action="delete-selected-artifacts"
                    data-session-id="${escapeHtml(selectedSession.session_id)}"
                    aria-label="Delete ${selectedCount} selected artifact${selectedCount === 1 ? "" : "s"}"
                    title="Delete ${selectedCount} selected artifact${selectedCount === 1 ? "" : "s"}"
                  >
                    ${trashIcon()}
                  </button>
                `
                : ""
            }
          </div>
        </div>
        ${
          visibleArtifacts.length
            ? `
              <div class="artifact-bulk-bar">
                <div class="artifact-bulk-actions">
                  <button
                    type="button"
                    class="action-button compact"
                    data-action="toggle-select-all-artifacts"
                    data-session-id="${escapeHtml(selectedSession.session_id)}"
                    title="${selectAllLabel}"
                  >
                    ${selectAllLabel}
                  </button>
                  <div class="helper-copy">${selectedCount} artifact${selectedCount === 1 ? "" : "s"} selected</div>
                </div>
              </div>
            `
            : ""
        }
        <div class="table-wrap artifact-table-wrap artifact-page-table">
          ${renderArtifactTable(selectedSession, visibleArtifacts)}
        </div>
      </section>

      <section class="frame payload-panel">
        <div class="panel-bar">
          <div class="panel-title-stack">
            <div>
              <div class="mini-label">Payload</div>
              <h2>${escapeHtml(selectedArtifact?.title || "Artifact Payload")}</h2>
            </div>
            ${
              selectedArtifact
                ? `<div class="panel-chip">${escapeHtml(selectedArtifact.kind)}</div>`
                : ""
            }
          </div>
        </div>
        ${
          detailJson
            ? `
          <div class="payload-summary">${escapeHtml(state.artifactDetail?.summary || selectedArtifact?.summary || "No summary provided.")}</div>
          <div class="payload-meta">
            <span>${escapeHtml(selectedArtifact?.kind || "")}</span>
            <span>${escapeHtml(selectedArtifact?.provider || "local")}</span>
            <span>${escapeHtml(selectedArtifact?.artifact_id || "")}</span>
          </div>
          <pre class="json-viewer">${detailJson}</pre>
        `
            : `<div class="empty-state">Open an artifact row to inspect the full payload.</div>`
        }
      </section>
    </section>
  `;
}

function renderUploadsView(selectedSession) {
  if (!selectedSession) {
    return `
      <section class="frame data-panel">
        <div class="empty-state">Select a session from the workspace view before opening uploads.</div>
      </section>
    `;
  }
  const visibleUploads = filterUploads(state.uploads, state.uploadQuery);
  const selectedUpload = visibleUploads.find((item) => getUploadId(item) === state.selectedUploadId)
    || (state.uploadDetail && getUploadId(state.uploadDetail) === state.selectedUploadId ? state.uploadDetail : null)
    || visibleUploads[0]
    || null;
  const detailJson = selectedUpload ? escapeHtml(JSON.stringify(state.uploadDetail || selectedUpload, null, 2)) : "";
  const selectedUploadSummary = selectedUpload?.one_line_summary
    || selectedUpload?.summary_block?.one_line_summary
    || selectedUpload?.summary
    || selectedUpload?.payload?.preview_excerpt
    || "No summary provided.";
  const selectedUploadValidation = selectedUpload?.validation || selectedUpload?.inspection?.validation || null;
  const selectedUploadHints = selectedUpload?.next_step_hints || selectedUpload?.inspection?.next_step_hints || [];
  const selectedUploadTechnical = selectedUpload?.technical || {};
  return `
    <section class="artifact-stack">
      <section class="frame data-panel">
        <div class="panel-bar">
          <div class="panel-title-stack">
            <div>
              <div class="mini-label">Uploads</div>
              <h2>${escapeHtml(selectedSession.title)}</h2>
            </div>
            <div class="panel-chip">${visibleUploads.length} shown / ${state.uploads.length} total</div>
          </div>
          <div class="panel-actions upload-actions">
            <input id="upload-search" value="${escapeHtml(state.uploadQuery)}" placeholder="Filter uploads..." />
            <button type="button" class="action-button compact" id="upload-file-button">Upload File</button>
            <form id="upload-link-form" class="upload-link-form">
              <input id="upload-link-path" name="path" placeholder="Link existing file path..." />
              <button type="submit" class="action-button compact">Link Path</button>
            </form>
            <input id="upload-file-input" type="file" hidden />
          </div>
        </div>
        <div class="table-wrap artifact-table-wrap artifact-page-table">
          ${renderUploadTable(selectedSession, visibleUploads)}
        </div>
      </section>

      <section class="frame payload-panel">
        <div class="panel-bar">
          <div class="panel-title-stack">
            <div>
              <div class="mini-label">Upload Payload</div>
              <h2>${escapeHtml(selectedUpload?.title || "Upload Payload")}</h2>
            </div>
            ${
              selectedUpload
                ? `<div class="panel-chip">${escapeHtml(selectedUpload.source_mode || selectedUpload.payload?.source_mode || "local")}</div>`
                : ""
            }
          </div>
        </div>
        ${
          selectedUpload
            ? `
              <div class="payload-summary">${escapeHtml(selectedUploadSummary)}</div>
              <div class="payload-meta">
                <span>${escapeHtml(selectedUpload.data_type || selectedUpload.summary_block?.data_type || selectedUpload.source_mode || selectedUpload.payload?.source_mode || "")}</span>
                <span>${escapeHtml(selectedUpload.role || selectedUpload.summary_block?.role || "")}</span>
                <span>${escapeHtml(String(selectedUpload.sample_count ?? selectedUpload.summary_block?.sample_count ?? 0))} samples</span>
                <span>${escapeHtml(String(selectedUpload.gene_count ?? selectedUpload.summary_block?.gene_count ?? 0))} genes</span>
                <span>${escapeHtml(selectedUpload.file_name || selectedUpload.payload?.file_name || "upload")}</span>
                <span>${escapeHtml(selectedUploadTechnical.path || selectedUpload.data_path || selectedUpload.payload?.data_path || "")}</span>
              </div>
              ${
                selectedUploadValidation
                  ? `
                    <div class="cohort-validation">
                      <div class="mini-label">Validation</div>
                      <pre class="markdown-code"><code>${escapeHtml(JSON.stringify(selectedUploadValidation, null, 2))}</code></pre>
                    </div>
                  `
                  : ""
              }
              ${
                selectedUploadHints.length
                  ? `
                    <div class="cohort-validation">
                      <div class="mini-label">Next Steps</div>
                      <ul class="data-hint-list">
                        ${selectedUploadHints.map((hint) => `<li>${escapeHtml(hint)}</li>`).join("")}
                      </ul>
                    </div>
                  `
                  : ""
              }
              <details class="cohort-validation">
                <summary>Technical payload</summary>
                <pre class="json-viewer">${detailJson}</pre>
              </details>
            `
            : `<div class="empty-state">Open an upload row to inspect the path, preview, and metadata.</div>`
        }
      </section>
    </section>
  `;
}

function figureArtifactsForSession(artifacts) {
  return (artifacts || []).filter((item) => item.kind === "figure");
}

function figureImageSrc(detail) {
  const payload = detail?.payload || {};
  const previewDataUrl = String(payload.preview_data_url || "").trim();
  if (previewDataUrl) {
    return previewDataUrl;
  }
  return "";
}

function renderFiguresView(selectedSession, figures) {
  if (!selectedSession) {
    return `
      <section class="frame data-panel">
        <div class="empty-state">Select a session from the workspace view before opening figures.</div>
      </section>
    `;
  }
  const selectedFigure = figures.find((item) => item.artifact_id === state.selectedFigureId)
    || figureArtifactsForSession(state.artifacts).find((item) => item.artifact_id === state.selectedFigureId)
    || null;
  const figureDetail = state.figureDetail;
  const imageSrc = figureImageSrc(figureDetail);
  const selectedCount = selectedArtifactsForSession(selectedSession.session_id)
    .filter((artifactId) => figureArtifactsForSession(state.artifacts).some((item) => item.artifact_id === artifactId))
    .length;
  const allVisibleSelected = allArtifactsSelected(selectedSession.session_id, figures);
  const selectAllLabel = allVisibleSelected
    ? (figures.length === figureArtifactsForSession(state.artifacts).length ? "Clear All" : "Clear Shown")
    : (figures.length === figureArtifactsForSession(state.artifacts).length ? "Select All" : "Select Shown");
  return `
    <section class="artifact-stack">
      <section class="frame data-panel">
        <div class="panel-bar">
          <div class="panel-title-stack">
            <div>
              <div class="mini-label">Figures</div>
              <h2>${escapeHtml(selectedSession.title)}</h2>
            </div>
            <div class="panel-chip">${figures.length} shown / ${figureArtifactsForSession(state.artifacts).length} total</div>
          </div>
          <div class="panel-actions">
            <input id="figure-search" value="${escapeHtml(state.figureQuery)}" placeholder="Filter figures..." />
            ${
              selectedCount
                ? `
                  <button
                    type="button"
                    class="action-button compact danger icon-button icon-only"
                    data-action="delete-selected-figures"
                    data-session-id="${escapeHtml(selectedSession.session_id)}"
                    aria-label="Delete ${selectedCount} selected figure${selectedCount === 1 ? "" : "s"}"
                    title="Delete ${selectedCount} selected figure${selectedCount === 1 ? "" : "s"}"
                  >
                    ${trashIcon()}
                  </button>
                `
                : ""
            }
          </div>
        </div>
        ${
          figures.length
            ? `
              <div class="artifact-bulk-bar">
                <div class="artifact-bulk-actions">
                  <button
                    type="button"
                    class="action-button compact"
                    data-action="toggle-select-all-figures"
                    data-session-id="${escapeHtml(selectedSession.session_id)}"
                    title="${selectAllLabel}"
                  >
                    ${selectAllLabel}
                  </button>
                  <div class="helper-copy">${selectedCount} figure${selectedCount === 1 ? "" : "s"} selected</div>
                </div>
              </div>
            `
            : ""
        }
        <div class="table-wrap artifact-table-wrap artifact-page-table">
          ${renderFiguresTable(selectedSession, figures)}
        </div>
      </section>

      <section class="frame payload-panel figure-preview-panel">
        <div class="panel-bar">
          <div class="panel-title-stack">
            <div>
              <div class="mini-label">Preview</div>
              <h2>${escapeHtml(selectedFigure?.title || figureDetail?.title || "Figure Preview")}</h2>
            </div>
            ${
              selectedFigure
                ? `
                  <div class="panel-chip-row">
                    <div class="panel-chip">${escapeHtml(formatFigureType(selectedFigure.provenance?.figure_type || selectedFigure.payload?.figure_type || figureDetail?.payload?.figure_type || ""))}</div>
                    <div class="panel-chip">${escapeHtml(selectedFigure.provider || "gleskit_figures")}</div>
                  </div>
                `
                : ""
            }
          </div>
          ${
            selectedFigure
              ? `
                <div class="panel-actions">
                  <button
                    type="button"
                    class="action-button compact danger icon-button icon-only"
                    data-action="delete-figure"
                    data-session-id="${escapeHtml(selectedSession.session_id)}"
                    data-artifact-id="${escapeHtml(selectedFigure.artifact_id)}"
                    aria-label="Delete figure"
                    title="Delete figure"
                  >
                    ${trashIcon()}
                  </button>
                </div>
              `
              : ""
          }
        </div>
        ${
          figureDetail
            ? `
              <div class="payload-summary">${escapeHtml(figureDetail.summary || "Saved figure artifact.")}</div>
              <div class="payload-meta">
                <span>${escapeHtml(selectedFigure?.artifact_id || figureDetail.artifact_id || "")}</span>
                <span>${escapeHtml(formatFigureType(figureDetail.payload?.figure_type || ""))}</span>
                <span>${escapeHtml(selectedFigure?.created_at ? formatTimestamp(selectedFigure.created_at) : "")}</span>
              </div>
              ${
                imageSrc
                  ? `<div class="figure-preview-wrap"><img class="figure-preview-image" src="${escapeHtml(imageSrc)}" alt="${escapeHtml(selectedFigure?.title || "GLESKit figure")}" /></div>`
                  : `<div class="empty-state">No local preview image is available for this figure.</div>`
              }
              <pre class="json-viewer">${escapeHtml(JSON.stringify(figureDetail.payload || {}, null, 2))}</pre>
            `
            : `<div class="empty-state">Open a figure row to preview the saved image and metadata.</div>`
        }
      </section>
    </section>
  `;
}

function trashIcon() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M9 3h6l1 2h4v2H4V5h4l1-2Zm-2 6h10l-.7 10.2A2 2 0 0 1 14.3 21H9.7a2 2 0 0 1-1.99-1.8L7 9Zm3 2v7h2v-7h-2Zm4 0v7h2v-7h-2Z" />
    </svg>
  `;
}

function penIcon() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M15.14 3.73a2.5 2.5 0 0 1 3.54 0l1.59 1.59a2.5 2.5 0 0 1 0 3.54l-9.94 9.94-4.76 1.22a.8.8 0 0 1-.97-.97l1.22-4.76 9.32-9.32Zm2.41 1.13a.9.9 0 0 0-1.27 0l-1.13 1.13 2.54 2.54 1.13-1.13a.9.9 0 0 0 0-1.27Zm-3.53 2.26-7.55 7.55-.67 2.59 2.59-.67 7.55-7.55-1.92-1.92Z" />
    </svg>
  `;
}

function plusIcon() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M11 5h2v14h-2zM5 11h14v2H5z" />
    </svg>
  `;
}

function downIcon() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M12 19.5 4.5 12l1.41-1.41L11 15.67V4h2v11.67l5.09-5.08L19.5 12 12 19.5Z" />
    </svg>
  `;
}

function upIcon() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="m12 4.5 7.5 7.5-1.41 1.41L13 8.33V20h-2V8.33l-5.09 5.08L4.5 12 12 4.5Z" />
    </svg>
  `;
}

function stopIcon() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M7 7h10v10H7z" />
    </svg>
  `;
}

function gearIcon() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M19.14 12.94c.04-.31.06-.63.06-.94s-.02-.63-.06-.94l2.03-1.58a.5.5 0 0 0 .12-.64l-1.92-3.32a.5.5 0 0 0-.6-.22l-2.39.96a7.05 7.05 0 0 0-1.63-.94L14.4 2.8a.5.5 0 0 0-.49-.4h-3.82a.5.5 0 0 0-.49.4l-.36 2.52c-.58.23-1.13.54-1.63.94l-2.39-.96a.5.5 0 0 0-.6.22L2.7 8.84a.5.5 0 0 0 .12.64l2.03 1.58c-.04.31-.06.63-.06.94s.02.63.06.94L2.82 14.52a.5.5 0 0 0-.12.64l1.92 3.32c.13.22.39.31.6.22l2.39-.96c.5.4 1.05.71 1.63.94l.36 2.52c.04.24.25.4.49.4h3.82c.24 0 .45-.16.49-.4l.36-2.52c.58-.23 1.13-.54 1.63-.94l2.39.96c.22.09.47 0 .6-.22l1.92-3.32a.5.5 0 0 0-.12-.64l-2.03-1.58ZM12 15.5A3.5 3.5 0 1 1 12 8.5a3.5 3.5 0 0 1 0 7Z" />
    </svg>
  `;
}

function humanAvatarIcon() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="currentColor">
      <path d="M12 12c2.7 0 4.8-2.1 4.8-4.8S14.7 2.4 12 2.4 7.2 4.5 7.2 7.2 9.3 12 12 12Zm0 2.4c-3.2 0-9.6 1.6-9.6 4.8v2.4h19.2v-2.4c0-3.2-6.4-4.8-9.6-4.8Z"/>
    </svg>
  `;
}

function botAvatarIcon() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="currentColor">
      <path d="M12 2a2 2 0 0 1 2 2c0 .74-.4 1.38-1 1.73V7h4a2 2 0 0 1 2 2v1a2 2 0 0 1 1 1.73v2.54A2 2 0 0 1 19 16v1a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-1a2 2 0 0 1-1-1.73v-2.54A2 2 0 0 1 5 10V9a2 2 0 0 1 2-2h4V5.73A2 2 0 0 1 10 4a2 2 0 0 1 2-2Zm-4 9a1 1 0 1 0 0 2 1 1 0 0 0 0-2Zm8 0a1 1 0 1 0 0 2 1 1 0 0 0 0-2Zm-4 4c-1.1 0-2-.4-2.6-1h5.2c-.6.6-1.5 1-2.6 1Z"/>
    </svg>
  `;
}

function sunIcon() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M12 4a1 1 0 0 1 1 1v1.1a1 1 0 1 1-2 0V5a1 1 0 0 1 1-1Zm0 13.9a1 1 0 0 1 1 1V20a1 1 0 1 1-2 0v-1.1a1 1 0 0 1 1-1Zm8-6.9a1 1 0 0 1 1 1 1 1 0 0 1-1 1h-1.1a1 1 0 1 1 0-2H20Zm-13.9 0a1 1 0 0 1 1 1 1 1 0 0 1-1 1H5a1 1 0 1 1 0-2h1.1ZM17.66 6.34a1 1 0 0 1 1.41 0l.78.78a1 1 0 0 1-1.41 1.41l-.78-.78a1 1 0 0 1 0-1.41ZM5.15 18.85a1 1 0 0 1 1.41 0l.78.78a1 1 0 0 1-1.41 1.41l-.78-.78a1 1 0 0 1 0-1.41Zm12.51 0a1 1 0 0 1 0 1.41l-.78.78a1 1 0 0 1-1.41-1.41l.78-.78a1 1 0 0 1 1.41 0ZM5.93 5.93a1 1 0 0 1 0 1.41l-.78.78a1 1 0 1 1-1.41-1.41l.78-.78a1 1 0 0 1 1.41 0ZM12 7.5A4.5 4.5 0 1 1 7.5 12 4.5 4.5 0 0 1 12 7.5Z" />
    </svg>
  `;
}

function moonIcon() {
  return `
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M21 12.79A9 9 0 1 1 11.21 3a7 7 0 0 0 9.79 9.79Z" />
    </svg>
  `;
}

function renderThemeToggle() {
  const isDark = state.theme === THEME_DARK;
  const label = isDark ? "Switch to light mode" : "Switch to dark mode";
  const icon = isDark ? sunIcon() : moonIcon();
  return `
    <button
      type="button"
      class="theme-toggle ${isDark ? "is-dark" : "is-light"} icon-button icon-only"
      data-action="toggle-theme"
      aria-label="${label}"
      aria-pressed="${isDark ? "true" : "false"}"
      title="${label}"
    >
      ${icon}
    </button>
  `;
}

function renderSessionActionButtons(selectedSession) {
  if (!selectedSession) {
    return "";
  }
  const sessionActive = state.runtime.active_session_id === selectedSession.session_id;
  return `
    <button
      type="button"
      class="toggle-button compact ${sessionActive ? "is-on" : ""}"
      data-action="activate-session"
      data-session-id="${escapeHtml(selectedSession.session_id)}"
      aria-label="${sessionActive ? "Deactivate session" : "Activate session"}"
      title="${sessionActive ? "Deactivate session" : "Activate session"}"
    >
      <span class="toggle-dot"></span>
    </button>
    <button
      type="button"
      class="action-button compact icon-button icon-only ${state.showEditSessionForm ? "is-active" : ""}"
      data-action="toggle-edit-session"
      data-session-id="${escapeHtml(selectedSession.session_id)}"
      aria-label="${state.showEditSessionForm ? "Close session editor" : "Edit session"}"
      title="${state.showEditSessionForm ? "Close session editor" : "Edit session"}"
    >
      ${penIcon()}
    </button>
    <button
      type="button"
      class="action-button compact danger icon-button icon-only"
      data-action="delete-session"
      data-session-id="${escapeHtml(selectedSession.session_id)}"
      aria-label="Delete session"
      title="Delete session"
    >
      ${trashIcon()}
    </button>
  `;
}

function renderNotesView(selectedSession, notes) {
  if (!selectedSession) {
    return `
      <section class="frame data-panel">
        <div class="empty-state">Select a session from the workspace view before opening notes.</div>
      </section>
    `;
  }
  const note = state.noteDetail || {
    title: "",
    body_markdown: "",
    author: "user"
  };
  const selectedCount = selectedNotesForSession(selectedSession.session_id).length;
  const allVisibleSelected = allNotesSelected(selectedSession.session_id, notes);
  const selectAllLabel = allVisibleSelected
    ? (notes.length === state.notes.length ? "Clear All" : "Clear Shown")
    : (notes.length === state.notes.length ? "Select All" : "Select Shown");
  return `
    <section class="notes-stack">
      <section class="frame data-panel">
        <div class="panel-bar">
          <div class="panel-title-stack">
            <div>
              <div class="mini-label">Notes</div>
              <h2>${escapeHtml(selectedSession.title)}</h2>
            </div>
            <div class="panel-chip">${notes.length} shown / ${state.notes.length} total</div>
          </div>
          <div class="panel-actions">
            <input id="note-search" value="${escapeHtml(state.noteQuery)}" placeholder="Filter notes..." />
            ${
              selectedCount
                ? `
                  <button
                    type="button"
                    class="action-button compact danger icon-button icon-only"
                    data-action="delete-selected-notes"
                    data-session-id="${escapeHtml(selectedSession.session_id)}"
                    aria-label="Delete ${selectedCount} selected note${selectedCount === 1 ? "" : "s"}"
                    title="Delete ${selectedCount} selected note${selectedCount === 1 ? "" : "s"}"
                  >
                    ${trashIcon()}
                  </button>
                `
                : ""
            }
            <button type="button" class="action-button compact primary" id="new-note-button">New Note</button>
          </div>
        </div>
        ${
          notes.length
            ? `
              <div class="artifact-bulk-bar">
                <div class="artifact-bulk-actions">
                  <button
                    type="button"
                    class="action-button compact"
                    data-action="toggle-select-all-notes"
                    data-session-id="${escapeHtml(selectedSession.session_id)}"
                    title="${selectAllLabel}"
                  >
                    ${selectAllLabel}
                  </button>
                  <div class="helper-copy">${selectedCount} note${selectedCount === 1 ? "" : "s"} selected</div>
                </div>
              </div>
            `
            : ""
        }
        <div class="table-wrap notes-table-wrap">
          ${renderNotesTable(selectedSession, notes)}
        </div>
      </section>

      <section class="frame note-editor-panel">
        <div class="panel-bar">
          <div class="panel-title-stack">
            <div>
              <div class="mini-label">Markdown Note</div>
              <h2>${escapeHtml(state.selectedNoteId ? note.title || "Edit note" : "New note")}</h2>
            </div>
            <div class="panel-chip">${escapeHtml(note.author || "user")}</div>
          </div>
          <div class="panel-actions">
            <button type="button" class="action-button compact" data-action="toggle-note-preview">
              ${state.notePreviewMode === "preview" ? "Markdown" : "Preview"}
            </button>
          </div>
        </div>
        <form id="note-form" class="note-form">
          <input name="title" placeholder="Key hypothesis" value="${escapeHtml(note.title || "")}" />
          ${
            state.notePreviewMode === "preview"
              ? `
                <div class="markdown-preview">${renderMarkdownPreview(note.body_markdown || "")}</div>
                <textarea name="body_markdown" class="note-editor" style="display:none;">${escapeHtml(note.body_markdown || "")}</textarea>
              `
              : `<textarea name="body_markdown" class="note-editor" placeholder="Write markdown for this session note...">${escapeHtml(note.body_markdown || "")}</textarea>`
          }
          <div class="note-actions">
            <button type="submit" class="action-button compact primary">Save Note</button>
            ${
              state.selectedNoteId
                ? `<button type="button" class="action-button compact danger icon-button icon-only" data-action="delete-note" data-session-id="${escapeHtml(selectedSession.session_id)}" data-note-id="${escapeHtml(state.selectedNoteId)}" aria-label="Delete note" title="Delete note">${trashIcon()}</button>`
                : ""
            }
          </div>
        </form>
      </section>
    </section>
  `;
}

function renderReportsView(selectedSession, reports) {
  if (!selectedSession) {
    return `
      <section class="frame data-panel">
        <div class="empty-state">Select a session from the workspace view before opening reports.</div>
      </section>
    `;
  }
  const report = state.reportDetail;
  const selectedCount = selectedReportsForSession(selectedSession.session_id).length;
  const allVisibleSelected = allReportsSelected(selectedSession.session_id, reports);
  const selectAllLabel = allVisibleSelected
    ? (reports.length === state.reports.length ? "Clear All" : "Clear Shown")
    : (reports.length === state.reports.length ? "Select All" : "Select Shown");
  return `
    <section class="report-stack">
      <section class="frame data-panel">
        <div class="panel-bar">
          <div class="panel-title-stack">
            <div>
              <div class="mini-label">Reports</div>
              <h2>${escapeHtml(selectedSession.title)}</h2>
            </div>
            <div class="panel-chip">${reports.length} shown / ${state.reports.length} total</div>
          </div>
          <div class="panel-actions">
            <input id="report-search" value="${escapeHtml(state.reportQuery)}" placeholder="Filter reports..." />
            ${
              selectedCount
                ? `
                  <button
                    type="button"
                    class="action-button compact danger icon-button icon-only"
                    data-action="delete-selected-reports"
                    data-session-id="${escapeHtml(selectedSession.session_id)}"
                    aria-label="Delete ${selectedCount} selected report${selectedCount === 1 ? "" : "s"}"
                    title="Delete ${selectedCount} selected report${selectedCount === 1 ? "" : "s"}"
                  >
                    ${trashIcon()}
                  </button>
                `
                : ""
            }
          </div>
        </div>
        ${
          reports.length
            ? `
              <div class="artifact-bulk-bar">
                <div class="artifact-bulk-actions">
                  <button
                    type="button"
                    class="action-button compact"
                    data-action="toggle-select-all-reports"
                    data-session-id="${escapeHtml(selectedSession.session_id)}"
                    title="${selectAllLabel}"
                  >
                    ${selectAllLabel}
                  </button>
                  <div class="helper-copy">${selectedCount} report${selectedCount === 1 ? "" : "s"} selected</div>
                </div>
              </div>
            `
            : ""
        }
        <div class="table-wrap reports-table-wrap">
          ${renderReportsTable(selectedSession, reports)}
        </div>
      </section>

      <section class="frame payload-panel">
        <div class="panel-bar">
          <div class="panel-title-stack">
            <div>
              <div class="mini-label">Report Viewer</div>
              <h2>${escapeHtml(report?.title || "Report Preview")}</h2>
            </div>
            ${
              report?.exists
                ? `<div class="panel-chip">${escapeHtml(report.author || "assistant")}</div>`
                : ""
            }
          </div>
          ${
            report?.exists
              ? `
                <div class="panel-actions">
                  <button type="button" class="action-button compact" data-action="toggle-report-preview">
                    ${state.reportPreviewMode === "preview" ? "Markdown" : "Preview"}
                  </button>
                  <button type="button" class="action-button compact danger icon-button icon-only" data-action="delete-report" data-session-id="${escapeHtml(selectedSession.session_id)}" data-report-id="${escapeHtml(report.report_id)}" aria-label="Delete report" title="Delete report">${trashIcon()}</button>
                </div>
              `
              : ""
          }
        </div>
        ${
          report?.exists
            ? `
              <div class="payload-summary">
                Saved report for this session. Different report angles or studies can coexist here as separate documents.
              </div>
              <div class="payload-meta">
                <span>${escapeHtml(report.report_id || "")}</span>
                <span>${escapeHtml(report.focus || "general")}</span>
                <span>${escapeHtml(report.updated_at ? formatTimestamp(report.updated_at) : "saved")}</span>
              </div>
              ${
                state.reportPreviewMode === "preview"
                  ? `<div class="markdown-preview">${renderMarkdownPreview(report.body_markdown || "")}</div>`
                  : `<pre class="markdown-viewer">${escapeHtml(report.body_markdown || "")}</pre>`
              }
            `
            : `<div class="empty-state">No saved reports exist for this session yet. Ask chat to create a report, and each one will appear here.</div>`
        }
      </section>
    </section>
  `;
}

function renderReportsTable(selectedSession, reports) {
  if (!reports.length) {
    return `<div class="empty-state">No reports match the current filter for this session.</div>`;
  }
  const rows = reports
    .map((report) => {
      const selected = state.selectedReportId === report.report_id;
      const token = `${selectedSession.session_id}:${report.report_id}`;
      const checked = state.selectedReports.has(token);
      return `
        <tr class="${selected ? "is-selected" : ""}" data-action="open-report" data-session-id="${escapeHtml(selectedSession.session_id)}" data-report-id="${escapeHtml(report.report_id)}">
          <td class="checkbox-cell">
            <input
              type="checkbox"
              data-action="toggle-report"
              data-session-id="${escapeHtml(selectedSession.session_id)}"
              data-report-id="${escapeHtml(report.report_id)}"
              ${checked ? "checked" : ""}
            />
          </td>
          <td>
            <div class="table-main tooltip-anchor" data-tip="${escapeHtml(report.excerpt || "")}">${escapeHtml(truncate(report.title, 42))}</div>
            <div class="table-sub">${escapeHtml(truncate(report.report_id, 18))}</div>
          </td>
          <td>${escapeHtml(report.focus || "general")}</td>
          <td>${escapeHtml(formatReportType(report.template_id))}</td>
          <td>${escapeHtml(report.author || "assistant")}</td>
          <td>${escapeHtml(formatTimestamp(report.updated_at || report.created_at))}</td>
        </tr>
      `;
    })
    .join("");
  return `
    <table class="data-table">
      <thead>
        <tr>
          <th class="checkbox-column">Pick</th>
          <th>Report</th>
          <th>Focus</th>
          <th>Type</th>
          <th>Author</th>
          <th>Updated</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function renderNotesTable(selectedSession, notes) {
  if (!notes.length) {
    return `<div class="empty-state">No notes yet. Save one to keep a markdown summary, hypothesis, or interpretation with this session.</div>`;
  }
  const rows = notes
    .map((note) => {
      const selected = state.selectedNoteId === note.note_id;
      const token = `${selectedSession.session_id}:${note.note_id}`;
      const checked = state.selectedNotes.has(token);
      return `
        <tr class="${selected ? "is-selected" : ""}" data-action="open-note" data-session-id="${escapeHtml(selectedSession.session_id)}" data-note-id="${escapeHtml(note.note_id)}">
          <td class="checkbox-cell">
            <input
              type="checkbox"
              data-action="toggle-note"
              data-session-id="${escapeHtml(selectedSession.session_id)}"
              data-note-id="${escapeHtml(note.note_id)}"
              ${checked ? "checked" : ""}
            />
          </td>
          <td>
            <div class="table-main tooltip-anchor" data-tip="${escapeHtml(note.excerpt || "")}">${escapeHtml(truncate(note.title, 42))}</div>
            <div class="table-sub">${escapeHtml(truncate(note.note_id, 18))}</div>
          </td>
          <td>${escapeHtml(note.author || "user")}</td>
          <td>${escapeHtml(formatTimestamp(note.updated_at || note.created_at))}</td>
        </tr>
      `;
    })
    .join("");
  return `
    <table class="data-table">
      <thead>
        <tr>
          <th class="checkbox-column">Pick</th>
          <th>Note</th>
          <th>Author</th>
          <th>Updated</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function renderFiguresTable(selectedSession, figures) {
  if (!figures.length) {
    return `<div class="empty-state">No figures match the current filter for this session.</div>`;
  }
  const rows = figures
    .map((figure) => {
      const selected = state.selectedFigureId === figure.artifact_id;
      const summary = figure.summary || "No summary provided.";
      const figureType = formatFigureType(figure.provenance?.figure_type || figure.payload?.figure_type || "");
      const token = `${selectedSession.session_id}:${figure.artifact_id}`;
      const checked = state.selectedArtifacts.has(token);
      return `
        <tr class="${selected ? "is-selected" : ""}">
          <td class="checkbox-cell">
            <input
              type="checkbox"
              data-action="toggle-figure"
              data-session-id="${escapeHtml(selectedSession.session_id)}"
              data-artifact-id="${escapeHtml(figure.artifact_id)}"
              ${checked ? "checked" : ""}
            />
          </td>
          <td>
            <div class="table-tags">
              <span class="figure-kind-pill">${escapeHtml(figureType)}</span>
            </div>
            <div class="table-main tooltip-anchor" data-tip="${escapeHtml(summary)}">${escapeHtml(truncate(figure.title, 42))}</div>
            <div class="table-sub">${escapeHtml(truncate(figure.artifact_id, 18))}</div>
          </td>
          <td>${escapeHtml(formatTimestamp(figure.created_at))}</td>
          <td>
            <button
              type="button"
              class="row-button"
              data-action="open-figure"
              data-session-id="${escapeHtml(selectedSession.session_id)}"
              data-artifact-id="${escapeHtml(figure.artifact_id)}"
            >
              Open
            </button>
          </td>
        </tr>
      `;
    })
    .join("");
  return `
    <table class="data-table">
      <thead>
        <tr>
          <th class="checkbox-column">Pick</th>
          <th>Figure</th>
          <th>Created</th>
          <th>View</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function renderSessionTable(selectedSession) {
  if (!state.sessions.length) {
    return `<div class="empty-state">No saved sessions yet. Use <strong>New Session</strong> when you want local persistence.</div>`;
  }
  const rows = state.sessions
    .map((session) => {
      const selected = selectedSession?.session_id === session.session_id;
      const active = state.runtime.active_session_id === session.session_id;
      const description = session.description || "No description provided.";
      return `
        <tr class="${selected ? "is-selected" : ""}" data-action="select-session" data-session-id="${escapeHtml(session.session_id)}">
          <td>
            <button
              type="button"
              class="signal-pill ${active ? "active" : ""}"
              data-action="activate-session"
              data-session-id="${escapeHtml(session.session_id)}"
              title="${active ? "Disconnect this session from chat" : "Connect this session to chat"}"
            >
              ${active ? "ACTIVE" : "INACTIVE"}
            </button>
          </td>
          <td>
            <div class="table-main tooltip-anchor" data-tip="${escapeHtml(description)}">${escapeHtml(truncate(session.title, 36))}</div>
            <div class="table-sub">${escapeHtml(truncate(session.session_id, 18))}</div>
          </td>
          <td>${escapeHtml(session.species)}</td>
          <td>${session.artifact_count}</td>
          <td>${escapeHtml(formatTimestamp(session.updated_at))}</td>
        </tr>
      `;
    })
    .join("");
  return `
    <table class="data-table">
      <thead>
        <tr>
          <th>State</th>
          <th>Study</th>
          <th>Species</th>
          <th>Art</th>
          <th>Updated</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function renderArtifactTable(selectedSession, artifacts) {
  if (!selectedSession) {
    return `<div class="empty-state">Select a session to view its artifact queue.</div>`;
  }
  if (!artifacts.length) {
    return `<div class="empty-state">No artifacts match the current filter for this session.</div>`;
  }
  const rows = artifacts
    .map((artifact) => {
      const token = `${selectedSession.session_id}:${artifact.artifact_id}`;
      const checked = state.selectedArtifacts.has(token);
      const selected = state.selectedArtifactId === artifact.artifact_id;
      const summary = artifact.summary || "No summary provided.";
      return `
        <tr class="${selected ? "is-selected" : ""}">
          <td class="checkbox-cell">
            <input
              type="checkbox"
              data-action="toggle-artifact"
              data-session-id="${escapeHtml(selectedSession.session_id)}"
              data-artifact-id="${escapeHtml(artifact.artifact_id)}"
              ${checked ? "checked" : ""}
            />
          </td>
          <td>${escapeHtml(artifact.kind)}</td>
          <td>
            <div class="table-main tooltip-anchor" data-tip="${escapeHtml(summary)}">${escapeHtml(truncate(artifact.title, 42))}</div>
            <div class="table-sub">${escapeHtml(truncate(artifact.artifact_id, 18))}</div>
          </td>
          <td>${escapeHtml(artifact.provider || "local")}</td>
          <td>${escapeHtml(formatTimestamp(artifact.created_at))}</td>
          <td>
              <button
                type="button"
                class="row-button"
                data-action="open-artifact"
                data-session-id="${escapeHtml(selectedSession.session_id)}"
              data-artifact-id="${escapeHtml(artifact.artifact_id)}"
            >
              Open
            </button>
          </td>
        </tr>
      `;
    })
    .join("");
  return `
    <table class="data-table">
      <thead>
        <tr>
          <th class="checkbox-column">Pick</th>
          <th>Kind</th>
          <th>Artifact</th>
          <th>Source</th>
          <th>Time</th>
          <th>View</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function renderUploadTable(selectedSession, uploads) {
  if (!selectedSession) {
    return `<div class="empty-state">Select a session to view its uploads.</div>`;
  }
  if (!uploads.length) {
    return `<div class="empty-state">No uploads match the current filter for this session.</div>`;
  }
  const rows = uploads
    .map((upload) => {
      const uploadId = getUploadId(upload);
      const selected = state.selectedUploadId === uploadId;
      const sizeText = formatFileSize(upload.file_size || upload.payload?.file_size || 0);
      const mode = upload.source_mode || upload.payload?.source_mode || "local";
      const sourceLabel = upload.file_name || upload.payload?.file_name || upload.title || "upload";
      const origin = upload.data_path || upload.stored_path || upload.source_path || upload.payload?.data_path || "";
      const summaryText = upload.one_line_summary
        || upload.summary_block?.one_line_summary
        || upload.summary
        || upload.payload?.preview_excerpt
        || "";
      return `
        <tr class="${selected ? "is-selected" : ""}" data-action="open-upload" data-session-id="${escapeHtml(selectedSession.session_id)}" data-upload-id="${escapeHtml(uploadId)}">
          <td>${escapeHtml(mode)}</td>
          <td>
            <div class="table-main tooltip-anchor" data-tip="${escapeHtml(summaryText)}">${escapeHtml(truncate(sourceLabel, 42))}</div>
            <div class="table-sub">${escapeHtml(truncate(uploadId, 18))}</div>
          </td>
          <td class="upload-path-cell">
            <div class="upload-path-text tooltip-anchor" data-tip="${escapeHtml(origin)}">${escapeHtml(origin)}</div>
          </td>
          <td>${escapeHtml(sizeText)}</td>
          <td>${escapeHtml(formatTimestamp(upload.created_at))}</td>
          <td class="row-actions">
            <button
              type="button"
              class="row-button compact"
              data-action="open-upload"
              data-session-id="${escapeHtml(selectedSession.session_id)}"
              data-upload-id="${escapeHtml(uploadId)}"
            >
              Open
            </button>
            <button
              type="button"
              class="row-button compact danger"
              data-action="delete-upload"
              data-session-id="${escapeHtml(selectedSession.session_id)}"
              data-upload-id="${escapeHtml(uploadId)}"
            >
              Delete
            </button>
          </td>
        </tr>
      `;
    })
    .join("");
  return `
    <table class="data-table">
      <thead>
        <tr>
          <th>Mode</th>
          <th>Upload</th>
          <th>Path</th>
          <th>Size</th>
          <th>Added</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function renderCohortImportForm() {
  return `
    <form id="cohort-import-form" class="cohort-form">
      <div class="panel-head">
        <div>
          <div class="mini-label">Local Import</div>
          <h2>Counts and metadata</h2>
        </div>
        <span class="status-chip">Counts + metadata required</span>
      </div>
      <div class="cohort-source-block">
        <div class="mini-label">Counts</div>
        <div class="cohort-source-row">
          <input name="counts_file" type="file" accept=".tsv,.csv,.txt" />
          <input name="counts_path" placeholder="Linked local path" />
        </div>
      </div>
      <div class="cohort-source-block">
        <div class="mini-label">Metadata</div>
        <div class="cohort-source-row">
          <input name="metadata_file" type="file" accept=".tsv,.csv,.txt" />
          <input name="metadata_path" placeholder="Linked local path" />
        </div>
      </div>
      <div class="cohort-form-actions">
        <button type="submit" class="action-button primary compact">Import cohort</button>
      </div>
      <div class="helper-copy">Use file upload or a linked local path for each file. GLESKit will validate and infer the rest before storing the cohort.</div>
    </form>
  `;
}

function getCohortTissueOptions(catalog) {
  const out = [];
  const seen = new Set();
  for (const item of catalog || []) {
    for (const raw of item.tissues || []) {
      const token = String(raw || "").trim();
      if (!token) continue;
      const key = token.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(token);
    }
  }
  out.sort((left, right) => left.localeCompare(right));
  return out;
}

function inferLocalCohortTitle(countsFile, countsPath, metadataFile, metadataPath) {
  const sources = [
    countsFile instanceof File ? countsFile.name : "",
    countsPath,
    metadataFile instanceof File ? metadataFile.name : "",
    metadataPath
  ];
  for (const source of sources) {
    const token = String(source || "").trim();
    if (!token) {
      continue;
    }
    const raw = token.split(/[\\/]/).filter(Boolean).pop() || token;
    const stem = raw.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim();
    if (stem) {
      return `${stem} cohort`;
    }
  }
  return "Imported cohort";
}

function parseChatTurnDirectives(rawContent) {
  const original = String(rawContent || "").replace(/^\s+/, "");
  let remainder = original;
  let pipelineMode = false;
  const inputDataRefs = [];
  const directivePattern = /^\/([A-Za-z][A-Za-z0-9_-]*)(?:\s+|$)/;
  const dataRefPattern = /^(artifact_[^\s]+|upload_[^\s]+|cohort_[^\s]+|session_[^\s]+)(?:\s+|$)/;

  while (remainder.startsWith("/")) {
    const commandMatch = remainder.match(directivePattern);
    if (!commandMatch) {
      return { content: original.trim(), pipelineMode: false, inputDataRefs: [] };
    }
    const command = commandMatch[1].toLowerCase();
    remainder = remainder.slice(commandMatch[0].length);
    if (command === "pipeline") {
      pipelineMode = true;
      continue;
    }
    if (command === "data") {
      let consumedAnyRef = false;
      while (true) {
        const refMatch = remainder.match(dataRefPattern);
        if (!refMatch) {
          break;
        }
        consumedAnyRef = true;
        inputDataRefs.push(refMatch[1]);
        remainder = remainder.slice(refMatch[0].length);
      }
      if (!consumedAnyRef) {
        return { content: original.trim(), pipelineMode: false, inputDataRefs: [] };
      }
      continue;
    }
    return { content: original.trim(), pipelineMode: false, inputDataRefs: [] };
  }

  return {
    content: remainder.trim(),
    pipelineMode,
    inputDataRefs
  };
}

function syncChatComposerDirectiveBadge() {
  const composer = document.querySelector("#chat-compose-form");
  if (!composer) {
    return;
  }
  const pipelineRow = composer.querySelector("#chat-compose-pipeline-row");
  const pipelineBadge = composer.querySelector("#chat-compose-pipeline-badge");
  const dataRow = composer.querySelector("#chat-compose-data-row");
  const dataBadge = composer.querySelector("#chat-compose-data-badge");
  const dataSummary = composer.querySelector("#chat-compose-data-summary");
  const directives = parseChatTurnDirectives(state.chatDraft);
  if (pipelineRow) {
    pipelineRow.hidden = !directives.pipelineMode;
  }
  if (pipelineBadge) {
    pipelineBadge.hidden = !directives.pipelineMode;
  }
  if (dataRow) {
    dataRow.hidden = directives.inputDataRefs.length === 0;
  }
  if (dataBadge) {
    dataBadge.hidden = directives.inputDataRefs.length === 0;
    dataBadge.textContent = directives.inputDataRefs.length > 0
      ? (directives.inputDataRefs.length === 1 ? "1 data ref" : `${directives.inputDataRefs.length} data refs`)
      : "";
  }
  if (dataSummary) {
    dataSummary.hidden = directives.inputDataRefs.length === 0;
    dataSummary.textContent = directives.inputDataRefs.length > 0
      ? `${directives.inputDataRefs.length} data ref${directives.inputDataRefs.length === 1 ? "" : "s"} attached`
      : "";
  }
}

function getFilteredCohortCatalog(catalog, tissue) {
  const rows = Array.isArray(catalog) ? catalog : [];
  const selectedTissue = String(tissue || "").trim().toLowerCase();
  if (!selectedTissue) {
    return rows;
  }
  return rows.filter((item) =>
    (Array.isArray(item.tissues) ? item.tissues : []).some((value) => String(value || "").trim().toLowerCase() === selectedTissue),
  );
}

function syncCohortSelectionWithTissue() {
  const catalog = getFilteredCohortCatalog(state.cohortCatalog, state.selectedCohortTissue);
  if (!catalog.length) {
    state.selectedCohortDatasetId = "";
    state.cohortDownloadName = "";
    return;
  }
  if (!state.selectedCohortDatasetId || !catalog.some((item) => item.dataset_id === state.selectedCohortDatasetId)) {
    state.selectedCohortDatasetId = catalog[0]?.dataset_id || "";
  }
  const selected = catalog.find((item) => item.dataset_id === state.selectedCohortDatasetId) || null;
  state.cohortDownloadName = selected?.title || selected?.dataset_id || state.cohortDownloadName || "";
}

function renderCohortTable(cohorts) {
  if (!cohorts.length) {
    return `<div class="empty-state">No cohorts yet. Import or download one to begin.</div>`;
  }
  const rows = cohorts
    .map((cohort) => {
      const selected = state.selectedCohortId === cohort.cohort_id;
      const size = cohort.cohort_size || {};
      const rawGeneCount = cohort.summary_block?.raw_gene_count ?? cohort.profile?.row_count ?? size.genes ?? 0;
      const filteredGeneCount = cohort.summary_block?.filtered_gene_count ?? size.genes ?? 0;
      return `
        <tr class="${selected ? "is-selected" : ""}" data-action="open-cohort" data-cohort-id="${escapeHtml(cohort.cohort_id)}">
          <td>
            <div class="table-main tooltip-anchor" data-tip="${escapeHtml(cohort.summary || "")}">${escapeHtml(truncate(cohort.title, 34))}</div>
            <div class="table-sub">${escapeHtml(truncate(cohort.cohort_id, 18))}</div>
          </td>
          <td>${escapeHtml(cohort.provider_id || "local_import")}</td>
          <td>${escapeHtml(String(size.samples || 0))}</td>
          <td>${escapeHtml(String(rawGeneCount))}</td>
          <td>${escapeHtml(String(filteredGeneCount))}</td>
          <td>${escapeHtml(formatTimestamp(cohort.updated_at || cohort.created_at))}</td>
          <td class="row-actions">
            <button type="button" class="action-button compact icon-button icon-only" data-action="attach-cohort-session" data-cohort-id="${escapeHtml(cohort.cohort_id)}" aria-label="Create session from cohort" title="Create session from cohort">${plusIcon()}</button>
            <button type="button" class="action-button compact icon-button icon-only" data-action="rename-cohort" data-cohort-id="${escapeHtml(cohort.cohort_id)}" aria-label="Rename cohort" title="Rename cohort">${penIcon()}</button>
            <button type="button" class="action-button compact danger icon-button icon-only" data-action="delete-cohort" data-cohort-id="${escapeHtml(cohort.cohort_id)}" aria-label="Delete cohort" title="Delete cohort">${trashIcon()}</button>
          </td>
        </tr>
      `;
    })
    .join("");
  return `
    <table class="data-table">
      <thead>
        <tr>
          <th>Cohort</th>
          <th>Provider</th>
          <th>Samples</th>
          <th>Raw genes</th>
          <th>Filtered genes</th>
          <th>Updated</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function getUploadId(upload) {
  return upload?.upload_id || upload?.artifact_id || "";
}

function bindEvents() {
  document.querySelector("#new-chat-button")?.addEventListener("click", async () => {
    await runAction(createChatConversation);
  });

  document.querySelector("#chat-compose-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await runAction(() => sendChatMessage(event.currentTarget));
  });

  document.querySelector("#chat-rename-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    await runAction(() => renameChatConversation(
      String(data.get("conversation_id") || ""),
      String(data.get("title") || "")
    ));
  });

  document.querySelector("#chat-provider-select")?.addEventListener("change", (event) => {
    state.selectedProviderId = String(event.currentTarget.value || "") || null;
    state.selectedModelId = "";
    state.chatModelQuery = "";
    state.chatModelPickerOpen = false;
    render();
  });

  document.querySelector("#chat-agent-select")?.addEventListener("change", (event) => {
    const agentId = String(event.currentTarget.value || "");
    state.selectedAgentId = agentId;
    if (state.selectedChatId) {
      state.chatAgentSelections = {
        ...state.chatAgentSelections,
        [state.selectedChatId]: agentId,
      };
    }
    render();
  });

  document.querySelector("#chat-model-input")?.addEventListener("focus", () => {
    state.chatModelPickerOpen = true;
    syncChatModelSuggestions();
  });

  document.querySelector("#chat-model-input")?.addEventListener("click", () => {
    state.chatModelPickerOpen = true;
    syncChatModelSuggestions();
  });

  document.querySelector("#chat-model-input")?.addEventListener("input", (event) => {
    state.chatModelQuery = String(event.currentTarget.value || "");
    state.chatModelPickerOpen = true;
    syncChatModelSuggestions();
  });

  document.querySelector("#chat-model-input")?.addEventListener("change", () => {
    commitChatModelQuery();
    state.chatModelPickerOpen = false;
    render();
  });

  document.querySelector("#chat-model-input")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      commitChatModelQuery();
      state.chatModelPickerOpen = false;
      render();
    }
  });

  document.querySelector('[data-action="toggle-chat-model-picker"]')?.addEventListener("click", () => {
    state.chatModelPickerOpen = !state.chatModelPickerOpen;
    render();
  });

  document.querySelector("#chat-draft")?.addEventListener("input", (event) => {
    state.chatDraft = String(event.currentTarget.value || "");
    syncChatComposerDirectiveBadge();
  });

  document.querySelector("#chat-draft")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      const form = document.querySelector("#chat-compose-form");
      if (form) {
        form.requestSubmit();
      }
    }
  });

  document.querySelector("#chat-jump-latest")?.addEventListener("click", () => {
    scrollChatToLatest({ behavior: "smooth" });
  });

  document.querySelectorAll('textarea[data-action="chat-tool-approval-other-input"]').forEach((element) => {
    element.addEventListener("input", (event) => {
      const approvalId = String(event.currentTarget.dataset.approvalId || "").trim();
      if (!approvalId) {
        return;
      }
      state.chatApprovalReplyDrafts = {
        ...(state.chatApprovalReplyDrafts || {}),
        [approvalId]: String(event.currentTarget.value || "")
      };
    });
  });

  document.querySelectorAll(".chat-tool-approval-other-form").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const approvalId = String(event.currentTarget.dataset.approvalId || "").trim();
      if (!approvalId) {
        return;
      }
      const draft = String(new FormData(event.currentTarget).get("content") || "").trim();
      if (!draft) {
        state.error = "Type a custom reply before sending it.";
        render();
        return;
      }
      await runAction(async () => {
        await cancelChatRequest();
        clearChatApprovalOtherState(approvalId);
        await sendChatMessageFromContent(draft);
      });
    });
  });

  document.querySelector(".chat-model-suggestions")?.addEventListener("click", (event) => {
    const button = event.target.closest('[data-action="select-chat-model"]');
    if (!button) {
      return;
    }
    state.chatModelQuery = String(button.getAttribute("data-model-id") || "");
    state.chatModelPickerOpen = false;
    commitChatModelQuery();
    render();
  });

  document.querySelector("#toggle-create-form")?.addEventListener("click", () => {
    state.showCreateForm = !state.showCreateForm;
    if (state.showCreateForm) {
      state.showDeriveForm = false;
      state.showEditSessionForm = false;
    }
    render();
  });

  document.querySelector("#toggle-derive-form")?.addEventListener("click", () => {
    state.showDeriveForm = !state.showDeriveForm;
    if (state.showDeriveForm) {
      state.showCreateForm = false;
      state.showEditSessionForm = false;
    }
    render();
  });

  document.querySelector("#create-session-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await runAction(() => createSession(event.currentTarget));
  });

  document.querySelector("#edit-session-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await runAction(() => updateSessionMetadata(event.currentTarget));
  });

  document.querySelector("#derive-session-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await runAction(() => createSessionFromSelectedArtifacts(event.currentTarget));
  });

  document.querySelector("#session-search-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = String(document.querySelector("#session-search")?.value || "");
    await runAction(() => refreshSessions(value));
  });

  document.querySelector("#artifact-search")?.addEventListener("input", (event) => {
    state.artifactQuery = String(event.currentTarget.value || "");
    scheduleFilterRender();
  });

  document.querySelector("#upload-search")?.addEventListener("input", (event) => {
    state.uploadQuery = String(event.currentTarget.value || "");
    scheduleFilterRender();
  });

  document.querySelector("#upload-file-button")?.addEventListener("click", () => {
    document.querySelector("#upload-file-input")?.click();
  });

  document.querySelector("#upload-file-input")?.addEventListener("change", async (event) => {
    const file = event.currentTarget.files?.[0];
    if (!file) {
      return;
    }
    await runAction(() => uploadSelectedFile(file));
    event.currentTarget.value = "";
  });

  document.querySelector("#upload-link-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await runAction(() => linkUploadPath(event.currentTarget));
  });

  document.querySelector("#figure-search")?.addEventListener("input", (event) => {
    state.figureQuery = String(event.currentTarget.value || "");
    scheduleFilterRender();
  });

  document.querySelector("#note-search")?.addEventListener("input", (event) => {
    state.noteQuery = String(event.currentTarget.value || "");
    scheduleFilterRender();
  });

  document.querySelector("#report-search")?.addEventListener("input", (event) => {
    state.reportQuery = String(event.currentTarget.value || "");
    scheduleFilterRender();
  });

  document.querySelector("#cohort-search")?.addEventListener("input", (event) => {
    state.cohortQuery = String(event.currentTarget.value || "");
    scheduleFilterRender();
  });

  document.querySelector("#cohort-provider-select")?.addEventListener("change", async (event) => {
    const providerId = String(event.currentTarget.value || "").trim();
    if (!providerId) {
      return;
    }
    state.selectedCohortProviderId = providerId;
    state.selectedCohortDatasetId = "";
    state.selectedCohortTissue = "";
    state.cohortDownloadName = "";
    await runAction(() => refreshCohortCatalog(providerId));
  });

  document.querySelector("#cohort-dataset-select")?.addEventListener("change", (event) => {
    const datasetId = String(event.currentTarget.value || "").trim();
    state.selectedCohortDatasetId = datasetId;
    const selected = getFilteredCohortCatalog(state.cohortCatalog, state.selectedCohortTissue).find((item) => item.dataset_id === datasetId);
    state.cohortDownloadName = selected?.title || selected?.dataset_id || "";
    state.selectedCohortTissue = selected?.tissues?.[0] || "";
    state.cohortSubsetValueText = "";
    render();
  });

  document.querySelector("#cohort-tissue-select")?.addEventListener("change", (event) => {
    state.selectedCohortTissue = String(event.currentTarget.value || "").trim();
    state.cohortSubsetValueText = "";
    syncCohortSelectionWithTissue();
    render();
  });

  document.querySelector("#cohort-download-scope")?.addEventListener("change", (event) => {
    state.cohortDownloadScope = String(event.currentTarget.value || "full").trim() || "full";
    if (state.cohortDownloadScope === "full") {
      state.cohortSubsetValueText = "";
    }
    render();
  });

  document.querySelector("#cohort-subset-mode")?.addEventListener("change", (event) => {
    state.cohortSubsetMode = String(event.currentTarget.value || "absolute").trim() || "absolute";
    state.cohortSubsetValueText = "";
    render();
  });

  document.querySelector("#cohort-subset-value")?.addEventListener("input", (event) => {
    state.cohortSubsetValueText = String(event.currentTarget.value || "");
  });

  document.querySelector("#cohort-download-title")?.addEventListener("input", (event) => {
    state.cohortDownloadName = String(event.currentTarget.value || "");
  });

  document.querySelector("#cohort-download-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await runAction(() => downloadCohort(event.currentTarget));
  });

  document.querySelector(".cohort-download-log")?.addEventListener("toggle", (event) => {
    state.cohortDownloadLogOpen = Boolean(event.currentTarget?.open);
  });

  document.querySelector("#cohort-import-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await runAction(() => createCohort(event.currentTarget));
  });

  document.querySelector("#cohort-popup-dismiss")?.addEventListener("click", () => {
    state.error = "";
    state.message = "";
    state.cohortNotice = null;
    render();
  });

  document.querySelector("#cohort-rename-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await runAction(async () => {
      const data = new FormData(event.currentTarget);
      const cohortId = String(data.get("cohort_id") || "").trim();
      const nextTitle = String(data.get("title") || "").trim();
      if (!cohortId) {
        throw new Error("Select a cohort first.");
      }
      await confirmRenameCohort(cohortId, nextTitle);
      clearConfirmation();
    });
  });

  document.querySelector("#new-note-button")?.addEventListener("click", () => {
    composeNewNote();
  });

  document.querySelector("#note-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await runAction(() => saveNote(event.currentTarget));
  });

  document.querySelectorAll("[data-action]").forEach((element) => {
    element.addEventListener("click", async (event) => {
      const action = event.currentTarget.dataset.action;
      const sessionId = event.currentTarget.dataset.sessionId;
      const artifactId = event.currentTarget.dataset.artifactId;
      if (action !== "select-session") {
        event.stopPropagation();
      }
      await runAction(async () => {
        if (action === "retry-workspace-bootstrap") {
          await bootstrap();
        } else if (action === "select-session" && sessionId) {
          await selectSession(sessionId);
        } else if (action === "toggle-theme") {
          setTheme(state.theme === THEME_DARK ? THEME_LIGHT : THEME_DARK);
        } else if (action === "show-app-workspace") {
          state.error = "";
          state.appView = "workspace";
          render();
        } else if (action === "show-app-cohorts") {
          state.error = "";
          state.appView = "cohorts";
          render();
          void ensureCohortLibraryLoaded();
        } else if (action === "show-app-chat") {
          if (state.appView === "system" && !state.chatBusy) state.chatBootstrapState = "idle";
          state.appView = "chat";
          queueChatScrollToLatest();
          render();
          await ensureChatReady();
          if (!state.chatBusy && (state.selectedChatId || state.chatConversations[0])) {
            await loadChatConversation(state.selectedChatId || state.chatConversations[0].conversation_id, { preserveDraft: true });
          }
        } else if (action === "show-app-system") {
          state.error = "";
          openSystemPanel();
        } else if (action === "retry-chat-bootstrap") {
          retryChatBootstrap();
        } else if (action === "retry-session-snapshot") {
          retrySessionSnapshot();
        } else if (action === "toggle-chat-error") {
          state.chatErrorExpanded = !state.chatErrorExpanded;
          render();
        } else if (action === "dismiss-chat-error") {
          state.error = "";
          state.chatErrorExpanded = false;
          render();
        } else if (action === "toggle-chat-activity-sidebar") {
          state.chatActivitySidebarOpen = !state.chatActivitySidebarOpen;
          render();
        } else if (action === "cancel-chat-request") {
          await cancelChatRequest();
        } else if (action === "toggle-chat-tool-approval-other" && event.currentTarget.dataset.approvalId) {
          const approvalId = String(event.currentTarget.dataset.approvalId).trim();
          const open = !isChatApprovalOtherOpen(approvalId);
          setChatApprovalOtherOpen(approvalId, open);
          render();
        } else if (action === "approve-chat-tool-call" && event.currentTarget.dataset.approvalId && event.currentTarget.dataset.approvalMode) {
          state.dismissedApprovalIds.add(String(event.currentTarget.dataset.approvalId));
          render();
          await resolveChatToolApproval(event.currentTarget.dataset.approvalId, event.currentTarget.dataset.approvalMode);
        } else if (action === "reset-chat-tool-approval" && state.selectedChatId) {
          await resetChatConversationToolApproval(state.selectedChatId);
        } else if (action === "open-chat-conversation" && event.currentTarget.dataset.conversationId) {
          // Capture before render/await replaces the clicked DOM element.
          const conversationId = event.currentTarget.dataset.conversationId;
          if (state.appView === "system" && !state.chatBusy) state.chatBootstrapState = "idle";
          state.appView = "chat";
          try { await ensureChatReady(); }
          catch (error) { state.chatSystemMessage = formatError(error); }
          await loadChatConversation(conversationId, { preserveDraft: true });
        } else if (action === "start-rename-chat-conversation" && event.currentTarget.dataset.conversationId) {
          startRenameChatConversation(event.currentTarget.dataset.conversationId);
        } else if (action === "cancel-rename-chat-conversation") {
          cancelRenameChatConversation();
        } else if (action === "delete-chat-conversation" && event.currentTarget.dataset.conversationId) {
          promptDeleteChatConversation(event.currentTarget.dataset.conversationId);
        } else if (action === "show-workspace") {
          state.page = "workspace";
          render();
        } else if (action === "show-cohort-download") {
          state.cohortTab = "download";
          render();
        } else if (action === "show-cohort-import") {
          state.cohortTab = "import";
          render();
        } else if (action === "cancel-cohort-download") {
          await cancelCohortDownload();
        } else if (action === "retry-cohort-download-status") {
          await retryCohortDownloadStatus();
        } else if (action === "select-cohort-provider" && event.currentTarget.dataset.providerId) {
          state.selectedCohortProviderId = event.currentTarget.dataset.providerId;
          state.selectedCohortDatasetId = "";
          state.selectedCohortTissue = "";
          state.cohortDownloadName = "";
          await refreshCohortCatalog(event.currentTarget.dataset.providerId, { force: true });
        } else if (action === "select-cohort-dataset" && event.currentTarget.dataset.datasetId) {
          state.selectedCohortDatasetId = event.currentTarget.dataset.datasetId;
          const selected = getFilteredCohortCatalog(state.cohortCatalog, state.selectedCohortTissue).find((item) => item.dataset_id === state.selectedCohortDatasetId);
          state.cohortDownloadName = selected?.title || selected?.dataset_id || "";
          const tissues = selected?.tissues || [];
          state.selectedCohortTissue = tissues[0] || "";
          syncCohortSelectionWithTissue();
          render();
        } else if (action === "open-cohort" && event.currentTarget.dataset.cohortId) {
          state.appView = "cohorts";
          await selectCohort(event.currentTarget.dataset.cohortId);
        } else if (action === "attach-cohort-session" && event.currentTarget.dataset.cohortId) {
          await createSessionFromCohortId(event.currentTarget.dataset.cohortId);
        } else if (action === "rename-cohort" && event.currentTarget.dataset.cohortId) {
          promptRenameCohort(event.currentTarget.dataset.cohortId);
        } else if (action === "delete-cohort" && event.currentTarget.dataset.cohortId) {
          promptDeleteCohort(event.currentTarget.dataset.cohortId);
        } else if (action === "show-artifacts") {
          state.page = "artifacts";
          render();
          if (state.selectedSessionId) {
            await ensureSessionSnapshot(state.selectedSessionId);
          }
        } else if (action === "show-uploads") {
          state.page = "uploads";
          render();
          if (state.selectedSessionId) {
            await ensureSessionSnapshot(state.selectedSessionId);
          }
        } else if (action === "show-figures") {
          state.page = "figures";
          if (state.selectedSessionId) {
            const figures = figureArtifactsForSession(state.artifacts);
            if (state.selectedFigureId && figures.some((item) => item.artifact_id === state.selectedFigureId)) {
              await openFigure(state.selectedSessionId, state.selectedFigureId);
            } else if (figures[0]) {
              await openFigure(state.selectedSessionId, figures[0].artifact_id);
            } else {
              state.selectedFigureId = null;
              state.figureDetail = null;
              render();
            }
          } else {
            render();
          }
        } else if (action === "show-notes") {
          state.page = "notes";
          render();
          if (state.selectedSessionId) {
            if (hasLoadedEmptyNotes(state.selectedSessionId)) {
              state.notes = [];
              state.notesLoadedSessionId = state.selectedSessionId;
              if (!state.noteDetail) {
                composeNewNote();
              } else {
                render();
              }
              return;
            }
            void ensureNotesLoaded(state.selectedSessionId).then(() => {
              if (state.page === "notes" && state.selectedSessionId) {
                if (!state.noteDetail) {
                  composeNewNote();
                } else {
                  render();
                }
              }
            }).catch((error) => {
              state.error = formatError(error);
              render();
            });
          }
        } else if (action === "show-reports") {
          state.page = "reports";
          render();
          if (state.selectedSessionId) {
            if (hasLoadedEmptyReports(state.selectedSessionId)) {
              state.reports = [];
              state.reportsLoadedSessionId = state.selectedSessionId;
              state.selectedReportId = null;
              state.reportDetail = null;
              render();
              return;
            }
            void ensureReportsLoaded(state.selectedSessionId).then(() => {
              if (state.page === "reports" && state.selectedSessionId) {
                if (state.selectedReportId && state.reports.some((item) => item.report_id === state.selectedReportId)) {
                  void selectReport(state.selectedSessionId, state.selectedReportId);
                } else if (state.reports[0]) {
                  void selectReport(state.selectedSessionId, state.reports[0].report_id);
                } else {
                  state.selectedReportId = null;
                  state.reportDetail = null;
                  render();
                }
              }
            }).catch((error) => {
              state.error = formatError(error);
              render();
            });
          }
        } else if (action === "open-report" && sessionId && event.currentTarget.dataset.reportId) {
          await selectReport(sessionId, event.currentTarget.dataset.reportId);
        } else if (action === "toggle-report-preview") {
          state.reportPreviewMode = state.reportPreviewMode === "preview" ? "markdown" : "preview";
          render();
        } else if (action === "toggle-note-preview") {
          const editor = document.querySelector(".note-editor");
          if (editor) {
            state.noteDetail = state.noteDetail || {
              title: "",
              body_markdown: "",
              author: "user",
              linked_artifact_ids: [],
              tags: []
            };
            state.noteDetail.body_markdown = editor.value;
          }
          state.notePreviewMode = state.notePreviewMode === "preview" ? "markdown" : "preview";
          render();
        } else if (action === "activate-session" && sessionId) {
          await toggleActiveSession(sessionId);
        } else if (action === "toggle-edit-session" && sessionId) {
          if (state.selectedSessionId !== sessionId) {
            await selectSession(sessionId);
            state.showEditSessionForm = true;
            render();
          } else {
            state.showEditSessionForm = !state.showEditSessionForm;
            render();
          }
        } else if (action === "cancel-edit-session") {
          state.showEditSessionForm = false;
          render();
        } else if (action === "delete-session" && sessionId) {
          promptDeleteSession(sessionId);
        } else if (action === "toggle-select-all-artifacts" && sessionId) {
          toggleAllArtifacts(sessionId, filterArtifacts(state.artifacts, state.artifactQuery));
        } else if (action === "toggle-select-all-figures" && sessionId) {
          toggleAllArtifacts(sessionId, filterArtifacts(figureArtifactsForSession(state.artifacts), state.figureQuery));
        } else if (action === "toggle-select-all-notes" && sessionId) {
          toggleAllNotes(sessionId, filterNotes(state.notes, state.noteQuery));
        } else if (action === "toggle-select-all-reports" && sessionId) {
          toggleAllReports(sessionId, filterReports(state.reports, state.reportQuery));
        } else if (action === "delete-selected-artifacts" && sessionId) {
          promptDeleteArtifacts(sessionId);
        } else if (action === "delete-selected-figures" && sessionId) {
          const visibleFigureIds = filterArtifacts(figureArtifactsForSession(state.artifacts), state.figureQuery)
            .map((item) => item.artifact_id);
          const figureArtifactIds = selectedArtifactsForSession(sessionId)
            .filter((artifactId) => visibleFigureIds.includes(artifactId) || figureArtifactsForSession(state.artifacts).some((item) => item.artifact_id === artifactId));
          promptDeleteArtifacts(sessionId, figureArtifactIds, "figures");
        } else if (action === "delete-selected-notes" && sessionId) {
          promptDeleteNotes(sessionId);
        } else if (action === "delete-selected-reports" && sessionId) {
          promptDeleteReports(sessionId);
        } else if (action === "cancel-confirmation") {
          clearConfirmation();
        } else if (action === "confirm-delete-session" && sessionId) {
          await deleteSession(sessionId);
        } else if (action === "confirm-delete-chat-conversation" && event.currentTarget.dataset.conversationId) {
          await deleteChatConversation(event.currentTarget.dataset.conversationId);
        } else if (action === "confirm-delete-cohort" && event.currentTarget.dataset.cohortId) {
          await confirmDeleteCohort(event.currentTarget.dataset.cohortId);
          clearConfirmation();
        } else if (action === "confirm-delete-artifacts" && sessionId) {
          await deleteSelectedArtifacts(sessionId, state.confirmation?.artifactIds || [], state.confirmation?.targetPage || "artifacts");
        } else if (action === "confirm-delete-notes" && sessionId) {
          await deleteSelectedNotes(sessionId, state.confirmation?.noteIds || []);
        } else if (action === "confirm-delete-reports" && sessionId) {
          await deleteSelectedReports(sessionId, state.confirmation?.reportIds || []);
        } else if (action === "open-note" && sessionId && event.currentTarget.dataset.noteId) {
          await selectNote(sessionId, event.currentTarget.dataset.noteId);
        } else if (action === "delete-note" && sessionId && event.currentTarget.dataset.noteId) {
          await deleteNote(sessionId, event.currentTarget.dataset.noteId);
        } else if (action === "delete-report" && sessionId && event.currentTarget.dataset.reportId) {
          await deleteReport(sessionId, event.currentTarget.dataset.reportId);
        } else if (action === "delete-figure" && sessionId && artifactId) {
          await deleteFigure(sessionId, artifactId);
        } else if (action === "open-artifact" && sessionId && artifactId) {
          await openArtifact(sessionId, artifactId);
        } else if (action === "open-upload" && sessionId && event.currentTarget.dataset.uploadId) {
          await openUpload(sessionId, event.currentTarget.dataset.uploadId);
        } else if (action === "delete-upload" && sessionId && event.currentTarget.dataset.uploadId) {
          await deleteUpload(sessionId, event.currentTarget.dataset.uploadId);
        } else if (action === "open-figure" && sessionId && artifactId) {
          await openFigure(sessionId, artifactId);
        }
      });
    });
  });

  document.querySelectorAll('input[data-action="toggle-artifact"]').forEach((element) => {
    element.addEventListener("click", (event) => {
      event.stopPropagation();
    });
    element.addEventListener("change", (event) => {
      const sessionId = event.currentTarget.dataset.sessionId;
      const artifactId = event.currentTarget.dataset.artifactId;
      if (sessionId && artifactId) {
        toggleArtifactSelection(sessionId, artifactId);
      }
    });
  });

  document.querySelectorAll('input[data-action="toggle-figure"]').forEach((element) => {
    element.addEventListener("click", (event) => {
      event.stopPropagation();
    });
    element.addEventListener("change", (event) => {
      const sessionId = event.currentTarget.dataset.sessionId;
      const artifactId = event.currentTarget.dataset.artifactId;
      if (sessionId && artifactId) {
        toggleArtifactSelection(sessionId, artifactId);
      }
    });
  });

  document.querySelectorAll('input[data-action="toggle-note"]').forEach((element) => {
    element.addEventListener("click", (event) => {
      event.stopPropagation();
    });
    element.addEventListener("change", (event) => {
      const sessionId = event.currentTarget.dataset.sessionId;
      const noteId = event.currentTarget.dataset.noteId;
      if (sessionId && noteId) {
        toggleNoteSelection(sessionId, noteId);
      }
    });
  });

  document.querySelectorAll('input[data-action="toggle-report"]').forEach((element) => {
    element.addEventListener("click", (event) => {
      event.stopPropagation();
    });
    element.addEventListener("change", (event) => {
      const sessionId = event.currentTarget.dataset.sessionId;
      const reportId = event.currentTarget.dataset.reportId;
      if (sessionId && reportId) {
        toggleReportSelection(sessionId, reportId);
      }
    });
  });

  bindChatScrollBehavior();
}

function filterArtifacts(artifacts, query) {
  const token = String(query || "").trim().toLowerCase();
  if (!token) {
    return artifacts;
  }
  return artifacts.filter((artifact) => {
    const figureType = artifact.provenance?.figure_type || artifact.payload?.figure_type || "";
    const haystack = `${artifact.kind} ${artifact.title} ${artifact.summary} ${artifact.provider || ""} ${artifact.file_name || ""} ${artifact.data_path || ""} ${artifact.stored_path || ""} ${artifact.source_path || ""} ${figureType} ${formatFigureType(figureType)}`.toLowerCase();
    return haystack.includes(token);
  });
}

function filterUploads(uploads, query) {
  const token = String(query || "").trim().toLowerCase();
  if (!token) {
    return uploads;
  }
  return uploads.filter((upload) => {
    const haystack = `${upload.title} ${upload.summary} ${upload.one_line_summary || ""} ${upload.summary_block?.one_line_summary || ""} ${upload.data_type || ""} ${upload.role || ""} ${upload.file_name || ""} ${upload.data_path || ""} ${upload.stored_path || ""} ${upload.source_path || ""} ${upload.source_mode || ""}`.toLowerCase();
    return haystack.includes(token);
  });
}

function filterCohorts(cohorts, query) {
  const token = String(query || "").trim().toLowerCase();
  if (!token) {
    return cohorts;
  }
  return cohorts.filter((cohort) => {
    const profile = cohort.profile || {};
    const haystack = `${cohort.title} ${cohort.cohort_id} ${cohort.summary} ${cohort.provider_id || ""} ${cohort.source_kind || ""} ${cohort.counts_path || ""} ${cohort.metadata_path || ""} ${profile.recommended_group_column || ""} ${profile.recommended_design_formula || ""}`.toLowerCase();
    return haystack.includes(token);
  });
}

function filterNotes(notes, query) {
  const token = String(query || "").trim().toLowerCase();
  if (!token) {
    return notes;
  }
  return notes.filter((note) => {
    const haystack = `${note.title} ${note.excerpt} ${note.author || ""}`.toLowerCase();
    return haystack.includes(token);
  });
}

function filterReports(reports, query) {
  const token = String(query || "").trim().toLowerCase();
  if (!token) {
    return reports;
  }
  return reports.filter((report) => {
    const haystack = `${report.title} ${report.excerpt} ${report.focus || ""} ${report.author || ""} ${report.template_id || ""} ${formatReportType(report.template_id)}`.toLowerCase();
    return haystack.includes(token);
  });
}

function bindChatScrollBehavior() {
  const scroller = document.querySelector("#chat-message-scroll");
  if (!scroller) {
    return;
  }
  const syncJumpButton = () => {
    const button = document.querySelector("#chat-jump-latest");
    if (!button) {
      return;
    }
    const distanceFromBottom = scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop;
    const shouldShow = distanceFromBottom > 56;
    button.hidden = !shouldShow;
    button.classList.toggle("is-visible", shouldShow);
  };
  scroller.addEventListener("scroll", syncJumpButton, { passive: true });
  if (state.chatShouldScrollToLatest) {
    scrollChatToLatest({ behavior: "auto" });
    state.chatShouldScrollToLatest = false;
  } else {
    syncJumpButton();
  }
}

function scrollChatToLatest({ behavior = "auto" } = {}) {
  const scroller = document.querySelector("#chat-message-scroll");
  if (!scroller) {
    return;
  }
  scroller.scrollTo({ top: scroller.scrollHeight, behavior });
  const button = document.querySelector("#chat-jump-latest");
  if (button) {
    button.hidden = true;
    button.classList.remove("is-visible");
  }
}

/**
 * Markdown render cache.
 *
 * `renderMarkdownPreview` is pure, but it re-parses the full document on every
 * render, and the app re-renders the whole view on any state change. Chat
 * transcripts and note/report bodies are the expensive inputs, so cache the
 * resulting HTML for repeated identical sources.
 *
 * Bounded on both axes: oversized documents are never cached, and the map is
 * capped at `MARKDOWN_CACHE_LIMIT` entries with least-recently-used eviction.
 */
const MARKDOWN_CACHE_LIMIT = 32;
const MARKDOWN_CACHE_MAX_SOURCE_LENGTH = 50000;
const markdownPreviewCache = new Map();

function renderMarkdownPreview(markdown) {
  const source = String(markdown || "").replace(/\r\n?/g, "\n").trim();
  // The empty case is a fixed string and must never occupy a cache slot.
  if (!source) {
    return `<div class="empty-state">No markdown content.</div>`;
  }
  if (source.length > MARKDOWN_CACHE_MAX_SOURCE_LENGTH) {
    return renderMarkdownPreviewUncached(source);
  }
  const cached = markdownPreviewCache.get(source);
  if (cached !== undefined) {
    // Refresh recency so hot entries survive eviction.
    markdownPreviewCache.delete(source);
    markdownPreviewCache.set(source, cached);
    return cached;
  }
  const html = renderMarkdownPreviewUncached(source);
  markdownPreviewCache.set(source, html);
  if (markdownPreviewCache.size > MARKDOWN_CACHE_LIMIT) {
    const oldest = markdownPreviewCache.keys().next().value;
    markdownPreviewCache.delete(oldest);
  }
  return html;
}

function renderMarkdownPreviewUncached(source) {
  const blocks = [];
  const lines = source.split("\n");
  let paragraph = [];
  let listType = null;
  let listItems = [];
  let codeLines = [];
  let codeLanguage = "";
  let inCode = false;
  let inMathBlock = false;
  let mathLines = [];
  const isTableSeparator = (line) =>
    /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/.test(line);
  const splitTableRow = (line) => {
    const trimmed = line.trim();
    const withoutEdges = trimmed.replace(/^\|/, "").replace(/\|$/, "");
    return withoutEdges.split("|").map((cell) => cell.trim());
  };
  const renderTable = (startIndex) => {
    const headerLine = lines[startIndex];
    const separatorLine = lines[startIndex + 1];
    if (!headerLine || !separatorLine || !isTableSeparator(separatorLine)) {
      return null;
    }
    const headerCells = splitTableRow(headerLine);
    const alignHints = splitTableRow(separatorLine).map((cell) => {
      const left = cell.startsWith(":");
      const right = cell.endsWith(":");
      if (left && right) return "center";
      if (right) return "right";
      if (left) return "left";
      return "";
    });
    const bodyRows = [];
    let index = startIndex + 2;
    while (index < lines.length) {
      const rowLine = lines[index];
      if (!rowLine.trim() || !rowLine.includes("|")) {
        break;
      }
      if (isTableSeparator(rowLine)) {
        break;
      }
      bodyRows.push(splitTableRow(rowLine));
      index += 1;
    }
    const headerHtml = headerCells
      .map((cell, idx) => {
        const align = alignHints[idx] ? ` style="text-align:${alignHints[idx]};"` : "";
        return `<th${align}>${renderInlineMarkdown(cell)}</th>`;
      })
      .join("");
    const bodyHtml = bodyRows
      .map((row) => {
        const cells = row
          .map((cell, idx) => {
            const align = alignHints[idx] ? ` style="text-align:${alignHints[idx]};"` : "";
            return `<td${align}>${renderInlineMarkdown(cell)}</td>`;
          })
          .join("");
        return `<tr>${cells}</tr>`;
      })
      .join("");
    return {
      html: `<table><thead><tr>${headerHtml}</tr></thead><tbody>${bodyHtml}</tbody></table>`,
      nextIndex: index - 1
    };
  };

  const flushParagraph = () => {
    if (!paragraph.length) {
      return;
    }
    blocks.push(`<p>${renderInlineMarkdown(paragraph.join(" "))}</p>`);
    paragraph = [];
  };

  const flushList = () => {
    if (!listItems.length || !listType) {
      return;
    }
    const items = listItems.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("");
    blocks.push(`<${listType}>${items}</${listType}>`);
    listItems = [];
    listType = null;
  };

  const flushCode = () => {
    if (!codeLines.length) {
      return;
    }
    const code = codeLines.join("\n");
    if (codeLanguage === "mermaid") {
      blocks.push(`<div class="markdown-mermaid">${escapeHtml(code)}</div>`);
    } else {
      blocks.push(`<pre class="markdown-code"><code>${escapeHtml(code)}</code></pre>`);
    }
    codeLines = [];
    codeLanguage = "";
  };

  const flushMathBlock = () => {
    if (!mathLines.length) {
      return;
    }
    const mathSource = mathLines.join("\n");
    blocks.push(`<div class="markdown-math">$$\n${escapeHtml(mathSource)}\n$$</div>`);
    mathLines = [];
  };

  for (let i = 0; i < lines.length; i += 1) {
    const rawLine = lines[i];
    const line = rawLine.trimEnd();
    if (line.startsWith("```")) {
      flushParagraph();
      flushList();
      flushMathBlock();
      if (inCode) {
        flushCode();
        inCode = false;
      } else {
        inCode = true;
        codeLanguage = line.slice(3).trim().toLowerCase();
      }
      continue;
    }
    if (inCode) {
      codeLines.push(rawLine);
      continue;
    }
    const trimmed = line.trim();
    if (trimmed.startsWith("$$")) {
      flushParagraph();
      flushList();
      flushCode();
      if (inMathBlock) {
        const closingLine = trimmed.slice(0, -2).trim();
        if (closingLine) {
          mathLines.push(closingLine);
        }
        flushMathBlock();
        inMathBlock = false;
      } else {
        const remainder = trimmed.slice(2);
        if (remainder.endsWith("$$")) {
          const inlineMath = remainder.slice(0, -2).trim();
          if (inlineMath) {
            blocks.push(`<div class="markdown-math">$$${escapeHtml(inlineMath)}$$</div>`);
          }
        } else {
          inMathBlock = true;
          if (remainder) {
            mathLines.push(remainder.trim());
          }
        }
      }
      continue;
    }
    if (inMathBlock) {
      if (trimmed.endsWith("$$")) {
        const closingLine = trimmed.slice(0, -2).trim();
        if (closingLine) {
          mathLines.push(closingLine);
        }
        flushMathBlock();
        inMathBlock = false;
      } else {
        mathLines.push(rawLine);
      }
      continue;
    }
    if (!line.trim()) {
      flushParagraph();
      flushList();
      continue;
    }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
      flushParagraph();
      flushList();
      blocks.push("<hr />");
      continue;
    }
    if (line.includes("|") && !inCode) {
      const table = renderTable(i);
      if (table) {
        flushParagraph();
        flushList();
        blocks.push(table.html);
        i = table.nextIndex;
        continue;
      }
    }
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushParagraph();
      flushList();
      const level = heading[1].length;
      blocks.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }
    const unordered = line.match(/^[-*]\s+(.*)$/);
    if (unordered) {
      flushParagraph();
      if (listType && listType !== "ul") {
        flushList();
      }
      listType = "ul";
      listItems.push(unordered[1]);
      continue;
    }
    const ordered = line.match(/^\d+\.\s+(.*)$/);
    if (ordered) {
      flushParagraph();
      if (listType && listType !== "ol") {
        flushList();
      }
      listType = "ol";
      listItems.push(ordered[1]);
      continue;
    }
    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      flushParagraph();
      flushList();
      blocks.push(`<blockquote>${renderInlineMarkdown(quote[1])}</blockquote>`);
      continue;
    }
    paragraph.push(line.trim());
  }

  flushParagraph();
  flushList();
  flushCode();
  flushMathBlock();
  return blocks.join("");
}

function renderInlineMarkdown(value) {
  let text = escapeHtml(value || "");
  text = text.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (match, alt, src) => {
    const normalized = normalizeImageSrc(src);
    if (!normalized.src) {
      return match;
    }
    const fallbackAttr = normalized.fallbackSrc
      ? ` data-fallback-src="${escapeHtml(normalized.fallbackSrc)}"`
      : "";
    return `<img src="${escapeHtml(normalized.src)}" alt="${escapeHtml(alt || "")}"${fallbackAttr} />`;
  });
  text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, label, target) => {
    // Never allow a model-generated javascript:/file: link to execute locally.
    const href = normalizeExternalUrl(target.replace(/&amp;/g, "&"));
    return href ? `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${label}</a>` : label;
  });
  text = text.replace(/`([^`]+)`/g, "<code>$1</code>");
  text = renderDirectionalGeneMarkdown(text);
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  text = text.replace(/(^|[\s(])\*([^*]+)\*(?=[\s).,!?]|$)/g, '$1<em>$2</em>');
  text = text.replace(/(^|[\s(])_([^_]+)_(?=[\s).,!?]|$)/g, '$1<em>$2</em>');
  return text;
}

function normalizeImageSrc(rawSrc) {
  const src = String(rawSrc || "").trim();
  if (!src) {
    return { src: "", fallbackSrc: "" };
  }
  if (/^https?:\/\//i.test(src)) {
    try {
      if (new URL(src).origin !== window.location.origin) return { src: "", fallbackSrc: "" };
    } catch { return { src: "", fallbackSrc: "" }; }
    return { src, fallbackSrc: "" };
  }
  if (/^data:image\//i.test(src)) {
    if (!/^data:image\/(png|jpeg|webp);base64,/i.test(src)) return { src: "", fallbackSrc: "" };
    return { src, fallbackSrc: "" };
  }
  if (/^file:\/\//i.test(src)) {
    const path = decodeURI(src.replace(/^file:\/\//i, ""));
    return path
      ? { src: convertFileSrc(path), fallbackSrc: "" }
      : { src: "", fallbackSrc: "" };
  }
  if (src.startsWith("/") || /^[a-zA-Z]:[\\/]/.test(src)) {
    if (src.startsWith("/api/files/")) return { src, fallbackSrc: "" };
    return { src: convertFileSrc(src), fallbackSrc: "" };
  }
  return { src: "", fallbackSrc: "" };
}

function bindMarkdownImageFallbacks() {
  const images = Array.from(document.querySelectorAll(".markdown-preview img[data-fallback-src]"));
  images.forEach((img) => {
    if (img.dataset.fallbackBound === "true") {
      return;
    }
    img.dataset.fallbackBound = "true";
    img.addEventListener("error", () => {
      const fallback = img.dataset.fallbackSrc || "";
      if (fallback && img.src !== fallback) {
        img.src = fallback;
      }
    });
  });
}

function bindMarkdownExternalLinks() {
  const links = Array.from(document.querySelectorAll(".markdown-preview a[href]"));
  links.forEach((link) => {
    if (link.dataset.externalBound === "true") {
      return;
    }
    link.dataset.externalBound = "true";
    link.addEventListener("click", (event) => {
      const href = normalizeExternalUrl(link.getAttribute("href"));
      if (!href) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      void runAction(() => openExternalUrl(href));
    });
  });
}

function normalizeExternalUrl(raw) {
  const candidate = String(raw || "").trim();
  if (!candidate) {
    return "";
  }
  try {
    const parsed = new URL(candidate);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      return parsed.toString();
    }
  } catch {
    return "";
  }
  return "";
}

async function openExternalUrl(url) {
  await invoke("open_external_url", { url });
}

async function runAction(fn) {
  try {
    await fn();
  } catch (error) {
    state.error = formatError(error);
    state.message = "";
    render();
  }
}

function truncate(value, limit) {
  const text = String(value || "");
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function formatFileSize(size) {
  const value = Number(size || 0);
  if (!Number.isFinite(value) || value < 0) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unitIndex = 0;
  let amount = value;
  while (amount >= 1024 && unitIndex < units.length - 1) {
    amount /= 1024;
    unitIndex += 1;
  }
  return unitIndex === 0 ? `${Math.round(amount)} B` : `${amount.toFixed(1)} ${units[unitIndex]}`;
}

function formatReportType(templateId) {
  const token = String(templateId || "").trim();
  if (!token) {
    return "General";
  }
  return token
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatFigureType(figureType) {
  const token = String(figureType || "").trim();
  if (!token) {
    return "Figure";
  }
  const known = {
    enrichment_barplot: "Enrichment Bar Plot",
    enrichment_emapplot: "Enrichment Map",
    enrichment_cnetplot: "Gene-Concept Network",
    enrichment_ppi_network: "Gene-Gene PPI Network",
    disease_emapplot: "Disease Enrichment Map",
    disease_cnetplot: "Disease Gene-Concept Network",
    disease_ppi_network: "Disease Gene-Gene PPI Network",
    compare_programs_barplot: "Program Comparison Bar Plot",
    compare_programs_emapplot: "Program Comparison Enrichment Map",
    compare_programs_cnetplot: "Program Comparison Gene-Concept Network",
    rnaseq_up_barplot: "RNA-seq Upregulated Bar Plot",
    rnaseq_up_emapplot: "RNA-seq Upregulated Enrichment Map",
    rnaseq_up_cnetplot: "RNA-seq Upregulated Gene-Concept Network",
    rnaseq_up_ppi_network: "RNA-seq Upregulated Gene-Gene PPI Network",
    rnaseq_down_barplot: "RNA-seq Downregulated Bar Plot",
    rnaseq_down_emapplot: "RNA-seq Downregulated Enrichment Map",
    rnaseq_down_cnetplot: "RNA-seq Downregulated Gene-Concept Network",
    rnaseq_down_ppi_network: "RNA-seq Downregulated Gene-Gene PPI Network",
    rnaseq_volcano: "RNA-seq Volcano Plot",
    ranked_gsea_plot: "Ranked GSEA Plot",
    expression_context_barplot: "Expression Context Plot"
  };
  if (known[token]) {
    return known[token];
  }
  return token
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatTimestamp(value) {
  if (!value) {
    return "n/a";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit"
  }).format(date);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatError(error) {
  if (typeof error === "string") {
    return error;
  }
  if (error && typeof error === "object" && "message" in error) {
    const parts = [];
    const message = String(error.message || "").trim();
    if (message) {
      parts.push(message);
    }
    const data = error.data;
    if (data != null) {
      const dataText = typeof data === "string" ? data.trim() : safeStringify(data);
      if (dataText && dataText !== message) {
        parts.push("Details:");
        parts.push(dataText);
      }
    }
    const causeMessage = typeof error.cause === "string"
      ? error.cause.trim()
      : error.cause && typeof error.cause === "object" && "message" in error.cause
        ? String(error.cause.message || "").trim()
        : "";
    if (causeMessage && causeMessage !== message) {
      parts.push("Cause:");
      parts.push(causeMessage);
    }
    const stack = typeof error.stack === "string" ? error.stack.trim() : "";
    if (stack && stack !== message) {
      parts.push("Stack:");
      parts.push(stack);
    }
    return parts.join("\n\n") || "An unexpected error occurred.";
  }
  return "An unexpected error occurred.";
}

function safeStringify(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

document.addEventListener("gleskit:settings-closed", (event) => {
  state.chatProviders = event.detail.providers || state.chatProviders;
  state.mcpStatus = event.detail.mcp || state.mcpStatus;
  syncSelectedProviderAndModel();
  if (event.detail.chatChanged && !state.chatBusy) {
    state.chatBootstrapState = "idle";
    if (!state.chatProviders.some((row) => row.enabled)) state.chatBootstrapState = "ready";
    else if (state.appView === "chat") void ensureChatReady().catch((error) => {
      state.error = formatError(error); render();
    });
  }
  render();
});

document.addEventListener("gleskit:runtime-updated", (event) => {
  state.runtime = { ...DEFAULT_RUNTIME, ...(event.detail?.runtime || {}) };
  if (state.appView === "chat") scheduleChatRender();
  else render();
});

document.addEventListener("gleskit:chat-reconcile", () => {
  if (!state.selectedChatId) return;
  if (state.chatBusy) scheduleChatConversationRefresh(state.selectedChatId, { delay: 0 });
  else void loadChatConversation(state.selectedChatId, { preserveDraft: true }).catch((error) => {
    state.chatSystemMessage = formatError(error); scheduleChatRender();
  });
});

window.addEventListener("focus", () => {
  if (state.appView === "chat") {
    void refreshChatProviders();
  }
});

document.addEventListener("visibilitychange", () => {
  syncCohortDownloadClock();
  if (!document.hidden && (state.appView === "chat")) {
    void refreshChatProviders();
  }
});

window.addEventListener("beforeunload", () => {
  stopHeartbeat();
});

render();
bootstrap();
