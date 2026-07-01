// HTTP + SSE client for the Lumen backend.

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function sendJSON(url, method, body) {
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

export const api = {
  health: () => getJSON("/api/health"),
  getTools: () => getJSON("/api/tools"),
  getSettings: () => getJSON("/api/settings"),
  saveSettings: (values) => sendJSON("/api/settings", "POST", values),
  listSessions: () => getJSON("/api/sessions"),
  createSession: () => sendJSON("/api/sessions", "POST"),
  deleteSession: (id) => sendJSON(`/api/sessions/${id}`, "DELETE"),
  renameSession: (id, title) => sendJSON(`/api/sessions/${id}/rename`, "POST", { title }),
  getMessages: (id) => getJSON(`/api/sessions/${id}/messages`),
  openFile: (path) => sendJSON("/api/open", "POST", { path }),
};

// Streams chat events as an async iterator of parsed event objects.
export async function* streamChat({ message, sessionId, signal }) {
  const res = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, session_id: sessionId }),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`Chat stream failed: ${res.status}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sep;
    while ((sep = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      for (const line of frame.split("\n")) {
        if (line.startsWith("data:")) {
          const payload = line.slice(5).trim();
          if (payload) {
            try {
              yield JSON.parse(payload);
            } catch {
              /* ignore malformed frame */
            }
          }
        }
      }
    }
  }
}
