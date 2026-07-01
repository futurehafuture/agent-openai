// DOM rendering helpers: messages, the streaming assistant turn, tool cards,
// the sessions sidebar, and the tools palette.
import { renderMarkdown } from "./markdown.js";

const CAT_GLYPH = {
  data: "M4 19V5M4 19h16M8 17v-6M12 17V8M16 17v-9",
  pptx: "M4 4h16v12H4zM9 20h6M12 16v4",
  files: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  docs: "M7 3h7l5 5v13H7zM14 3v5h5",
};
const CHECK = "M5 13l4 4L19 7";
const ARTIFACT_RE = /~\/[^\s*`)]+\.(?:pptx|png|csv|xlsx|docx|txt|json|parquet|pdf)/i;

let TOOL_CAT = {};
export function setToolMeta(tools) {
  TOOL_CAT = Object.fromEntries(tools.map((t) => [t.name, t.category]));
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2).toLowerCase(), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

function svg(path, cls = "icon") {
  return el("span", { class: "ico", html: `<svg viewBox="0 0 24 24" class="${cls}"><path d="${path}"/></svg>` });
}

export function toast(message) {
  const t = document.getElementById("toast");
  t.textContent = message;
  t.hidden = false;
  requestAnimationFrame(() => t.classList.add("show"));
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    t.classList.remove("show");
    setTimeout(() => (t.hidden = true), 280);
  }, 2400);
}

export function createUserMessage(text) {
  return el(
    "div",
    { class: "msg user" },
    el("div", { class: "msg-avatar" }, "you".slice(0, 1).toUpperCase()),
    el("div", { class: "msg-body" }, el("div", { class: "prose", html: renderMarkdown(text) }))
  );
}

export function createErrorBlock(message) {
  return el(
    "div",
    { class: "msg assistant" },
    el("div", { class: "msg-avatar" }, "✦"),
    el(
      "div",
      { class: "msg-body" },
      el(
        "div",
        { class: "msg-error" },
        svg("M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"),
        el("div", {}, message)
      )
    )
  );
}

// A single assistant turn: interleaves streamed text blocks and tool cards.
export class AssistantTurn {
  constructor(onOpenArtifact) {
    this.onOpenArtifact = onOpenArtifact;
    this.body = el("div", { class: "msg-body" });
    this.element = el("div", { class: "msg assistant" }, el("div", { class: "msg-avatar" }, "✦"), this.body);
    this.caret = el("span", { class: "caret" });
    this.body.append(this.caret);
    this._text = null;
    this._buffer = "";
    this._pending = {};
  }

  _moveCaretToEnd() {
    this.body.append(this.caret);
  }

  addToken(delta) {
    if (!this._text) {
      this._text = el("div", { class: "prose" });
      this._buffer = "";
      this.body.insertBefore(this._text, this.caret);
    }
    this._buffer += delta;
    this._text.innerHTML = renderMarkdown(this._buffer);
    this._moveCaretToEnd();
  }

  addReasoning(delta) {
    if (!this._reason) {
      this._reason = el("div", { class: "prose", style: "color:var(--muted);font-size:13.5px" });
      this.body.insertBefore(this._reason, this.caret);
      this._reasonBuf = "";
    }
    this._reasonBuf += delta;
    this._reason.textContent = this._reasonBuf;
    this._moveCaretToEnd();
  }

  addToolCall(name, args) {
    this._text = null; // following text starts a fresh block after the card
    const cat = TOOL_CAT[name] || "data";
    const argText = typeof args === "object" ? JSON.stringify(args, null, 2) : String(args ?? "");
    const body = el(
      "div",
      { class: "tool-card-body" },
      el("div", { class: "tool-arg-label" }, "Arguments"),
      el("div", { class: "tool-args" }, argText || "—")
    );
    const state = el("div", { class: "tool-state" }, el("span", { class: "spinner" }), el("span", {}, "running"));
    const head = el(
      "div",
      { class: "tool-card-head", onclick: () => card.classList.toggle("open") },
      el("span", { class: "tool-glyph", html: `<svg viewBox="0 0 24 24" class="icon"><path d="${CAT_GLYPH[cat]}"/></svg>` }),
      el("span", { class: "tool-name" }, name),
      state
    );
    const card = el("div", { class: "tool-card" }, head, body);
    this.body.insertBefore(card, this.caret);
    this._moveCaretToEnd();
    (this._pending[name] = this._pending[name] || []).push({ card, body, state });
    return card;
  }

  completeToolCall(name, output) {
    const queue = this._pending[name];
    const ref = queue && queue.shift();
    if (!ref) return;
    ref.state.replaceChildren(svg(CHECK, "icon"), el("span", {}, "done"));
    ref.state.firstChild.className = "tool-check";
    ref.body.append(el("div", { class: "tool-out-label" }, "Result"));
    ref.body.append(el("div", { class: "tool-output", html: renderMarkdown(output || "") }));

    const match = (output || "").match(ARTIFACT_RE);
    if (match && this.onOpenArtifact) {
      const open = el(
        "button",
        { class: "chip", style: "margin-top:10px", onclick: () => this.onOpenArtifact(match[0]) },
        svg("M14 3h7v7M21 3l-9 9M10 5H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5"),
        "Open file"
      );
      ref.body.append(open);
    }
  }

  finish() {
    this.caret.remove();
  }
}

export function renderSessions(container, sessions, activeId, handlers) {
  container.replaceChildren();
  if (!sessions.length) {
    container.append(el("div", { class: "tool-row", style: "padding:6px 10px" }, "No conversations yet."));
    return;
  }
  for (const s of sessions) {
    const item = el(
      "div",
      { class: `session-item ${s.id === activeId ? "active" : ""}`, onclick: () => handlers.open(s.id) },
      el("span", { class: "s-title" }, s.title || "New conversation"),
      el("button", { class: "s-del", title: "Delete", onclick: (e) => { e.stopPropagation(); handlers.remove(s.id); } },
        svg("M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"))
    );
    container.append(item);
  }
}

export function renderTools(panel, tools) {
  panel.replaceChildren();
  const groups = {};
  for (const t of tools) (groups[t.category] = groups[t.category] || { label: t.label, items: [] }).items.push(t);

  for (const [cat, group] of Object.entries(groups)) {
    const list = el(
      "div",
      { class: "tool-list" },
      ...group.items.map((t) => el("div", { class: "tool-row", title: t.summary }, el("code", {}, t.name)))
    );
    const head = el(
      "div",
      { class: "tool-group-head" },
      el("span", { class: "tool-dot" }),
      el("span", {}, group.label),
      el("span", { class: "count" }, String(group.items.length)),
      el("span", { class: "ico", html: `<svg viewBox="0 0 24 24" class="caret icon"><path d="M9 6l6 6-6 6"/></svg>` })
    );
    const groupEl = el("div", { class: "tool-group" }, head, list);
    head.addEventListener("click", () => groupEl.classList.toggle("open"));
    panel.append(groupEl);
  }
}
