// Lumen controller: wires the UI to the backend and drives the streaming loop.
import { api, streamChat } from "./api.js";
import {
  AssistantTurn,
  createErrorBlock,
  createUserMessage,
  el,
  renderSessions,
  renderTools,
  setToolMeta,
  toast,
} from "./ui.js";

const $ = (id) => document.getElementById(id);
const state = { sessionId: null, streaming: false, abort: null, settings: {}, tools: [] };

const messagesEl = $("messages");
const transcriptEl = $("transcript");
const emptyEl = $("empty-state");
const inputEl = $("input");

// ----------------------------------------------------------------- bootstrap
async function init() {
  applyTheme(localStorage.getItem("lumen-theme") || "light");
  wireEvents();
  try {
    const [health, tools, settings, sessions] = await Promise.all([
      api.health(),
      api.getTools(),
      api.getSettings(),
      api.listSessions(),
    ]);
    state.tools = tools;
    state.settings = settings;
    setToolMeta(tools);
    renderTools($("tools-panel"), tools);
    renderSessionList(sessions);
    $("model-chip").textContent = health.model;
    populateModelSelector(settings.providers, settings.active_provider_id, health.model);
    if (!localStorage.getItem("lumen-theme") && settings.theme) applyTheme(settings.theme);
    if (!health.has_api_key) {
      toast("Add at least one LLM provider in Settings to begin.");
      openSettings();
    }
  } catch (err) {
    toast("Could not reach the Lumen server.");
    console.error(err);
  }
}

// ----------------------------------------------------------------- chat loop
async function runTurn(message) {
  if (state.streaming || !message.trim()) return;
  hideEmpty();
  state.streaming = true;
  updateComposer();

  messagesEl.append(createUserMessage(message));
  const turn = new AssistantTurn(openArtifact);
  messagesEl.append(turn.element);
  scrollDown();

  state.abort = new AbortController();
  let failed = false;
  try {
    for await (const ev of streamChat({ message, sessionId: state.sessionId, signal: state.abort.signal })) {
      handleEvent(turn, ev);
      scrollDownSoon();
    }
  } catch (err) {
    if (err.name !== "AbortError") {
      failed = true;
      turn.element.after(createErrorBlock(`Connection error: ${err.message}`));
    }
  } finally {
    turn.finish();
    state.streaming = false;
    state.abort = null;
    updateComposer();
    if (!failed) refreshSessions();
    scrollDown();
  }
}

function handleEvent(turn, ev) {
  switch (ev.type) {
    case "session":
      state.sessionId = ev.id;
      $("session-title").textContent = ev.title;
      break;
    case "token":
      turn.addToken(ev.delta);
      break;
    case "reasoning":
      turn.addReasoning(ev.delta);
      break;
    case "tool_call":
      turn.addToolCall(ev.name, ev.arguments);
      break;
    case "tool_output":
      turn.completeToolCall(ev.name, ev.output);
      break;
    case "error":
      turn.element.after(createErrorBlock(ev.message));
      break;
    case "done":
    default:
      break;
  }
}

function openArtifact(path) {
  api.openFile(path).then(() => toast(`Opening ${path.split("/").pop()}…`)).catch(() => toast("Couldn't open that file."));
}

// ----------------------------------------------------------------- sessions
function renderSessionList(sessions) {
  renderSessions($("sessions"), sessions, state.sessionId, {
    open: openSession,
    remove: removeSession,
  });
}

async function refreshSessions() {
  try {
    renderSessionList(await api.listSessions());
  } catch (err) {
    console.error(err);
  }
}

async function openSession(id) {
  if (state.streaming) return;
  state.sessionId = id;
  hideEmpty();
  messagesEl.replaceChildren();
  try {
    const history = await api.getMessages(id);
    for (const item of history) renderHistoryItem(item);
    const sessions = await api.listSessions();
    renderSessionList(sessions);
    const cur = sessions.find((s) => s.id === id);
    $("session-title").textContent = cur ? cur.title : "Conversation";
  } catch (err) {
    toast("Could not load that conversation.");
  }
  scrollDown();
}

function renderHistoryItem(item) {
  if (item.role === "user") {
    messagesEl.append(createUserMessage(item.text));
  } else if (item.role === "assistant" && item.text) {
    const turn = new AssistantTurn(openArtifact);
    turn.addToken(item.text);
    turn.finish();
    messagesEl.append(turn.element);
  } else if (item.role === "tool_call") {
    messagesEl.append(
      el("div", { class: "msg assistant" }, el("div", { class: "msg-avatar" }, "✦"),
        el("div", { class: "msg-body" }, el("div", { class: "tool-row" }, `Used ${item.name}`)))
    );
  }
}

async function removeSession(id) {
  if (!confirm("Delete this conversation?")) return;
  await api.deleteSession(id);
  if (state.sessionId === id) newChat();
  refreshSessions();
}

function newChat() {
  state.sessionId = null;
  messagesEl.replaceChildren();
  $("session-title").textContent = "New conversation";
  showEmpty();
  refreshSessions();
  inputEl.focus();
}

// ----------------------------------------------------------------- settings
function openSettings() {
  const s = state.settings;
  $("set-workspace").value = s.workspace || "";
  $("set-max-turns").value = s.max_turns || 24;
  $("max-turns-val").textContent = s.max_turns || 24;
  $("set-tracing").checked = !!s.enable_tracing;
  renderProviderCards(s.providers || [], s.active_provider_id || "");
  $("settings-modal").hidden = false;
}

function closeSettings() {
  $("settings-modal").hidden = true;
}

async function saveSettings() {
  const payload = {
    workspace: $("set-workspace").value.trim() || undefined,
    max_turns: Number($("set-max-turns").value),
    enable_tracing: $("set-tracing").checked,
    providers: collectProviders(),
    active_provider_id: document.querySelector('input[name="active-provider"]:checked')?.value || "",
  };

  try {
    state.settings = await api.saveSettings(payload);
    const health = await api.health();
    $("model-chip").textContent = health.model;
    populateModelSelector(state.settings.providers, state.settings.active_provider_id, health.model);
    toast("Settings saved.");
    closeSettings();
  } catch (err) {
    toast("Could not save settings.");
  }
}

// ----------------------------------------------------------------- provider cards (settings modal)

function renderProviderCards(providers, activeId) {
  const list = $("providers-list");
  list.replaceChildren();
  if (!providers.length) {
    list.append(el("div", { class: "providers-empty" }, "No providers configured yet. Add one to start chatting."));
    return;
  }
  for (const p of providers) {
    list.append(buildProviderCard(p, activeId));
  }
}

function buildProviderCard(prov, activeId) {
  const isActive = prov.id === activeId;
  const card = el("div", {
    class: "provider-card" + (isActive ? " active" : ""),
    "data-provider-id": prov.id,
  });

  // Header
  const head = el("div", { class: "provider-card-head" });
  head.append(
    el("label", { class: "provider-radio" },
      el("input", {
        type: "radio",
        name: "active-provider",
        value: prov.id,
        checked: isActive ? "checked" : undefined,
      }),
      el("span", { class: "provider-name" }, prov.name),
      isActive ? el("span", { class: "provider-badge" }, "active") : null,
    ),
    el("button", { class: "icon-btn provider-delete", title: "Remove provider", "aria-label": "Remove " + prov.name },
      el("svg", { viewBox: "0 0 24 24", class: "icon" },
        el("path", { d: "M18 6 6 18M6 6l12 12" })
      )
    )
  );

  // Body (collapsible)
  const body = el("div", { class: "provider-card-body" + (isActive ? "" : " collapsed") });
  // Display name
  body.append(
    el("label", { class: "field" },
      el("span", { class: "field-label" }, "Display name"),
      el("input", { class: "prov-name", type: "text", value: prov.name, placeholder: "e.g. DeepSeek" }),
    )
  );
  // API key
  body.append(
    el("label", { class: "field" },
      el("span", { class: "field-label" }, "API key"),
      el("input", { class: "prov-key", type: "password", value: "", placeholder: prov.api_key_set ? "(key is saved)" : "sk-…", autocomplete: "off" }),
      prov.api_key_set ? el("span", { class: "field-note" }, "Leave blank to keep the saved key.") : null,
    )
  );
  // Base URL
  body.append(
    el("label", { class: "field" },
      el("span", { class: "field-label" }, el("em", {}, "Optional"), " API base URL"),
      el("input", { class: "prov-url", type: "text", value: prov.base_url || "", placeholder: "e.g. https://api.deepseek.com" }),
    )
  );
  // Models
  body.append(
    el("label", { class: "field" },
      el("span", { class: "field-label" }, "Models"),
    )
  );
  const modelsCtn = el("div", { class: "model-tags" });
  for (const m of prov.models || []) {
    modelsCtn.append(modelTag(m, prov.id, m === prov.default_model));
  }
  // Add model input
  const addRow = el("div", { class: "model-tag-add-row" });
  const addInput = el("input", {
    class: "model-add-input",
    type: "text",
    placeholder: "Add model name…",
    autocomplete: "off",
  });
  const addBtn = el("button", { class: "btn-sm model-add-btn" }, "+");
  addBtn.addEventListener("click", () => {
    const val = addInput.value.trim();
    if (val) {
      addInput.value = "";
      const dup = modelsCtn.querySelector(`[data-model="${CSS.escape(val)}"]`);
      if (!dup) {
        // If this is the first model, mark it as default
        const hasModels = modelsCtn.querySelectorAll(".model-tag").length > 0;
        modelsCtn.append(modelTag(val, prov.id, !hasModels));
      }
    }
  });
  addInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); addBtn.click(); }
  });
  addRow.append(addInput, addBtn);
  modelsCtn.append(addRow);
  body.append(modelsCtn);

  // Toggle body on header click
  head.addEventListener("click", (e) => {
    if (e.target.closest(".provider-delete") || e.target.closest(".icon-btn")) return;
    body.classList.toggle("collapsed");
  });

  // Delete button
  head.querySelector(".provider-delete").addEventListener("click", () => {
    if (confirm(`Remove provider "${prov.name}"?`)) {
      card.remove();
      // If this was the active one, check the first remaining
      const remaining = $("providers-list").querySelectorAll(".provider-card");
      if (remaining.length && isActive) {
        const firstRadio = remaining[0].querySelector('input[name="active-provider"]');
        if (firstRadio) firstRadio.checked = true;
      }
    }
  });

  // Active radio change
  head.querySelector('input[name="active-provider"]').addEventListener("change", () => {
    // Remove active class from all cards
    $("providers-list").querySelectorAll(".provider-card").forEach((c) => c.classList.remove("active"));
    card.classList.add("active");
    // Remove all badges
    $("providers-list").querySelectorAll(".provider-badge").forEach((b) => b.remove());
    head.querySelector(".provider-name").after(el("span", { class: "provider-badge" }, "active"));
  });

  card.append(head, body);
  return card;
}

function modelTag(name, provId, isDefault) {
  const tag = el("span", { class: "model-tag", "data-model": name, "data-provider-id": provId });
  const label = el("span", { class: "model-tag-label" }, name);
  if (isDefault) label.classList.add("is-default");
  const rm = el("button", { class: "model-tag-rm", title: "Remove model", "aria-label": "Remove " + name },
    el("svg", { viewBox: "0 0 24 24", class: "icon-xs" }, el("path", { d: "M18 6 6 18M6 6l12 12" }))
  );
  rm.addEventListener("click", () => {
    const card = tag.closest(".provider-card");
    tag.remove();
    // If default was removed, make the first remaining model default
    if (isDefault && card) {
      const first = card.querySelector(".model-tag");
      if (first) first.querySelector(".model-tag-label").classList.add("is-default");
    }
  });
  tag.append(label, rm);
  return tag;
}

function collectProviders() {
  const cards = $("providers-list").querySelectorAll(".provider-card");
  const providers = [];
  for (const card of cards) {
    const id = card.dataset.providerId || "";
    const name = card.querySelector(".prov-name")?.value.trim() || "";
    const apiKey = card.querySelector(".prov-key")?.value.trim() || "";
    const baseUrl = card.querySelector(".prov-url")?.value.trim() || "";
    const tags = card.querySelectorAll(".model-tag-label");
    const models = Array.from(tags).map((t) => t.textContent.trim()).filter(Boolean);
    const defaultModel = card.querySelector(".model-tag-label.is-default")?.textContent.trim() || models[0] || "";
    if (name) {
      providers.push({
        id: id || name.toLowerCase().replace(/[^a-z0-9]+/g, "-"),
        name,
        api_key: apiKey,
        base_url: baseUrl,
        models,
        default_model: defaultModel,
      });
    }
  }
  return providers;
}

// ----------------------------------------------------------------- model selector (composer)

function populateModelSelector(providers, activeId, selectedModel) {
  const sel = $("model-select");
  sel.replaceChildren();
  if (!providers || !providers.length) {
    sel.hidden = true;
    return;
  }
  // Find active provider
  const active = providers.find((p) => p.id === activeId) || providers[0];
  if (!active.models || !active.models.length) {
    sel.hidden = true;
    return;
  }
  for (const m of active.models) {
    const opt = el("option", { value: m }, m);
    if (m === selectedModel) opt.selected = true;
    sel.append(opt);
  }
  sel.hidden = active.models.length <= 1;
}

async function onModelChange() {
  const model = $("model-select").value;
  if (!model) return;
  try {
    state.settings = await api.saveSettings({ selected_model: model });
    $("model-chip").textContent = model;
    toast(`Model switched to ${model}.`);
  } catch (err) {
    toast("Could not switch model.");
  }
}

// ----------------------------------------------------------------- theme
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("lumen-theme", theme);
}
function toggleTheme() {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  applyTheme(next);
}

// ----------------------------------------------------------------- helpers
function hideEmpty() { emptyEl.classList.add("hidden"); }
function showEmpty() { emptyEl.classList.remove("hidden"); }
function updateComposer() {
  $("send").disabled = state.streaming;
  $("stop").hidden = !state.streaming;
  $("status-hint").textContent = state.streaming ? "Lumen is working…" : "Enter to send · Shift+Enter for a new line";
}
let _scrollQueued = false;
function scrollDown() { transcriptEl.scrollTop = transcriptEl.scrollHeight; }
function scrollDownSoon() {
  if (_scrollQueued) return;
  _scrollQueued = true;
  requestAnimationFrame(() => { scrollDown(); _scrollQueued = false; });
}

function submit() {
  const message = inputEl.value.trim();
  if (!message) return;
  inputEl.value = "";
  autoGrow();
  runTurn(message);
}
function autoGrow() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + "px";
}

// ----------------------------------------------------------------- events
function wireEvents() {
  $("send").addEventListener("click", submit);
  $("stop").addEventListener("click", () => state.abort && state.abort.abort());
  $("new-chat").addEventListener("click", newChat);
  $("theme-toggle").addEventListener("click", toggleTheme);
  $("open-settings").addEventListener("click", openSettings);
  $("save-settings").addEventListener("click", saveSettings);
  $("add-provider").addEventListener("click", () => {
    const list = $("providers-list");
    const existing = list.querySelectorAll(".provider-card").length;
    const newId = "provider-" + (existing + 1);
    const newProv = {
      id: newId,
      name: "",
      api_key: "",
      base_url: "",
      models: [],
      default_model: "",
      api_key_set: false,
    };
    list.append(buildProviderCard(newProv, ""));
    // Auto-expand the new card and focus name input
    const card = list.lastElementChild;
    card.querySelector(".provider-card-body").classList.remove("collapsed");
    card.querySelector(".prov-name").focus();
    // If first provider, check its radio
    if (existing === 0) {
      const radio = card.querySelector('input[name="active-provider"]');
      if (radio) radio.checked = true;
      card.classList.add("active");
    }
  });
  $("set-max-turns").addEventListener("input", (e) => ($("max-turns-val").textContent = e.target.value));
  $("model-select").addEventListener("change", onModelChange);
  $("model-chip").addEventListener("click", () => $("model-select").focus());

  inputEl.addEventListener("input", autoGrow);
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });

  $("example-chips").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (chip) { inputEl.value = chip.dataset.prompt; autoGrow(); submit(); }
  });

  document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", closeSettings));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSettings(); });
}

init();
