const DEFAULT_PORT = 5840;
let port = DEFAULT_PORT;
let socket = null;
let connected = false;
let connectGeneration = 0;
let reconnectTimer = null;
let connecting = false;

function guessBrowser() {
  const ua = navigator.userAgent;
  if (ua.includes("Edg/")) return "edge";
  if (ua.includes("OPR/") || ua.includes("Opera")) return "opera";
  if (ua.includes("Brave") || navigator.brave) return "brave";
  return "chrome";
}

async function resolvePort() {
  for (const host of ["127.0.0.1", "localhost"]) {
    try {
      const response = await fetch(`http://${host}:${DEFAULT_PORT}/api/port`, { cache: "no-store" });
      if (!response.ok) continue;
      const data = await response.json();
      if (data && data.port) {
        port = Number(data.port) || DEFAULT_PORT;
        return port;
      }
    } catch (err) {
      // Opus not up
    }
  }
  return null;
}

function scheduleReconnect(delayMs) {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, Math.max(800, delayMs || 2000));
}

function setBadge(on, label) {
  chrome.runtime.sendMessage({ type: "bridge-badge", on: !!on, label: label || "" }).catch(() => {});
}

function send(payload) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ browser: guessBrowser(), ...payload }));
  }
}

async function connect() {
  if (connecting) return;
  connecting = true;
  const gen = ++connectGeneration;
  try {
    const resolved = await resolvePort();
    if (!resolved) {
      connected = false;
      setBadge(false, "…");
      scheduleReconnect(2500);
      return;
    }
    port = resolved;

    const old = socket;
    socket = null;
    connected = false;
    if (old) {
      try {
        old.__opusDead = true;
        if (old.readyState === WebSocket.CONNECTING || old.readyState === WebSocket.OPEN) {
          old.close();
        }
      } catch (err) {
        // ignore
      }
    }

    const ws = new WebSocket(`ws://127.0.0.1:${port}/ws/browser`);
    ws.__opusDead = false;
    socket = ws;

    ws.addEventListener("open", () => {
      if (ws.__opusDead || gen !== connectGeneration || socket !== ws) return;
      connected = true;
      setBadge(true);
      send({ hello: true, browser: guessBrowser() });
      chrome.runtime.sendMessage({ type: "bridge-push-tab" }).catch(() => {});
    });

    ws.addEventListener("close", (event) => {
      if (ws.__opusDead) return;
      if (gen !== connectGeneration || socket !== ws) return;
      connected = false;
      socket = null;
      setBadge(false);
      scheduleReconnect(event.code === 1008 ? 4000 : 2000);
    });

    ws.addEventListener("error", () => {
      if (ws.__opusDead || gen !== connectGeneration || socket !== ws) return;
      setBadge(false);
    });

    ws.addEventListener("message", async (event) => {
      if (ws.__opusDead || gen !== connectGeneration || socket !== ws) return;
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      if (!message || !message.action || !message.request_id) return;
      let reply;
      try {
        reply = await chrome.runtime.sendMessage({ type: "bridge-action", message });
      } catch (err) {
        reply = { ok: false, error: String(err && err.message ? err.message : err) };
      }
      send({ request_id: message.request_id, ...(reply || { ok: false, error: "No reply from bridge worker." }) });
    });
  } catch (err) {
    setBadge(false);
    scheduleReconnect(2500);
  } finally {
    connecting = false;
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || !message.type) return false;
  if (message.type === "bridge-reconnect") {
    connectGeneration += 1;
    connecting = false;
    connect().then(() => sendResponse({ ok: true, port, connected })).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message.type === "bridge-status") {
    sendResponse({ connected, port });
    return false;
  }
  if (message.type === "bridge-send") {
    send(message.payload || {});
    sendResponse({ ok: true });
    return false;
  }
  return false;
});

connect();
setInterval(() => {
  if (connected) chrome.runtime.sendMessage({ type: "bridge-push-tab" }).catch(() => {});
  else connect();
}, 15000);
