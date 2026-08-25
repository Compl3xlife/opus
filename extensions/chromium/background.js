let showdownPlayLoadError = "";
try {
  importScripts("showdown_play.js");
} catch (err) {
  showdownPlayLoadError = String(err && err.message ? err.message : err);
  console.warn("showdown_play.js failed to load", err);
}

async function ensureOffscreen() {
  try {
    const contexts = await chrome.runtime.getContexts({
      contextTypes: ["OFFSCREEN_DOCUMENT"],
    });
    if (contexts && contexts.length > 0) return;
  } catch (err) {
    // Older Edge may not support getContexts — try create anyway.
  }
  try {
    await chrome.offscreen.createDocument({
      url: "offscreen.html",
      reasons: ["WORKERS"],
      justification: "Keep a persistent WebSocket to the local Opus assistant.",
    });
  } catch (err) {
    // Already exists or unsupported.
    const msg = String(err && err.message ? err.message : err);
    if (!/already exists|Only a single/i.test(msg)) {
      console.warn("offscreen create failed", err);
    }
  }
}

function setBadge(on, label) {
  chrome.action.setBadgeText({ text: on ? "on" : label || "off" });
  chrome.action.setBadgeBackgroundColor({ color: on ? "#d4a574" : "#8b3a3a" });
}

async function activeTab() {
  // Prefer last-focused browser window, but when Discord/another app has OS focus
  // Edge often returns nothing for lastFocusedWindow — fall back across all windows.
  try {
    const [focused] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (focused) return focused;
  } catch (err) {
    // ignore
  }
  try {
    const windows = await chrome.windows.getAll({ populate: true, windowTypes: ["normal"] });
    for (const win of windows) {
      if (!win.tabs) continue;
      const active = win.tabs.find((t) => t.active);
      if (active) return active;
    }
  } catch (err) {
    // ignore
  }
  const [any] = await chrome.tabs.query({ active: true });
  return any || null;
}

/** Remembered game tab so chess keeps working after you switch to Discord/etc. */
let chessTabId = null;

async function loadChessTabPin() {
  try {
    const data = await chrome.storage.session.get(["chessTabId"]);
    if (data && data.chessTabId != null) chessTabId = data.chessTabId;
  } catch (err) {
    // session storage may be unavailable on older Edge
  }
}

async function saveChessTabPin(tabId) {
  chessTabId = tabId;
  try {
    if (tabId == null) await chrome.storage.session.remove(["chessTabId"]);
    else await chrome.storage.session.set({ chessTabId: tabId });
  } catch (err) {
    // ignore
  }
}

function isChessUrl(url) {
  const u = String(url || "").toLowerCase();
  return (
    u.includes("chess.com") ||
    u.includes("lichess.org") ||
    u.includes("lichess.dev") ||
    u.includes("chess24.com")
  );
}

let maniaTabId = null;

async function loadManiaTabPin() {
  try {
    const data = await chrome.storage.session.get(["maniaTabId"]);
    if (data && data.maniaTabId != null) maniaTabId = data.maniaTabId;
  } catch (err) {
    // ignore
  }
}

async function saveManiaTabPin(tabId) {
  maniaTabId = tabId;
  try {
    if (tabId == null) await chrome.storage.session.remove(["maniaTabId"]);
    else await chrome.storage.session.set({ maniaTabId: tabId });
  } catch (err) {
    // ignore
  }
}

function isManiaUrl(url) {
  const u = String(url || "").toLowerCase();
  return u.includes("webosumania.com") || u.includes("web-osu-mania");
}

function isShowdownUrl(url) {
  const u = String(url || "").toLowerCase();
  return u.includes("pokemonshowdown.com") || u.includes("psim.us");
}

let showdownTabId = null;

async function loadShowdownTabPin() {
  try {
    const data = await chrome.storage.session.get(["showdownTabId"]);
    if (data && data.showdownTabId != null) showdownTabId = data.showdownTabId;
  } catch (err) {
    // ignore
  }
}

async function saveShowdownTabPin(tabId) {
  showdownTabId = tabId;
  try {
    if (tabId == null) await chrome.storage.session.remove(["showdownTabId"]);
    else await chrome.storage.session.set({ showdownTabId: tabId });
  } catch (err) {
    // ignore
  }
}

async function findShowdownTabs() {
  try {
    const tabs = await chrome.tabs.query({});
    return tabs.filter((t) => t && t.id != null && isShowdownUrl(t.url));
  } catch (err) {
    return [];
  }
}

async function pinShowdownTab(tab) {
  if (!tab || tab.id == null) return;
  await saveShowdownTabPin(tab.id);
  try {
    await chrome.tabs.update(tab.id, { autoDiscardable: false });
  } catch (err) {
    // ignore
  }
}

async function resolveShowdownTab() {
  await loadShowdownTabPin();
  const pinned = await getTabById(showdownTabId);
  if (pinned && isShowdownUrl(pinned.url)) return pinned;
  if (pinned && showdownTabId != null && !isShowdownUrl(pinned.url)) {
    await saveShowdownTabPin(null);
  }
  const tabs = await findShowdownTabs();
  if (tabs.length === 1) return tabs[0];
  if (tabs.length > 1) {
    const focused = tabs.find((t) => t.active);
    if (focused) return focused;
    tabs.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0));
    return tabs[0];
  }
  return await activeTab();
}

async function waitShowdownTabReady(tabId) {
  for (let i = 0; i < 40; i++) {
    let tab = null;
    try {
      tab = await chrome.tabs.get(tabId);
    } catch (err) {
      return null;
    }
    if (tab && tab.status === "complete" && isShowdownUrl(tab.url)) return tab;
    await sleepMs(250);
  }
  try {
    return await chrome.tabs.get(tabId);
  } catch (err) {
    return null;
  }
}

async function runShowdownPlay(enabled, createIfMissing) {
  await loadShowdownTabPin();
  let tab = await resolveShowdownTab();
  if (!tab || tab.id == null || !isShowdownUrl(tab.url)) {
    const tabs = await findShowdownTabs();
    tab = tabs[0] || null;
  }
  if (!tab || tab.id == null || !isShowdownUrl(tab.url)) {
    if (!createIfMissing) {
      return { ok: false, error: "Open https://play.pokemonshowdown.com/ in Edge, then try again." };
    }
    try {
      tab = await chrome.tabs.create({ url: "https://play.pokemonshowdown.com/", active: true });
    } catch (err) {
      return { ok: false, error: "Open https://play.pokemonshowdown.com/ in Edge, then try again." };
    }
  }
  tab = (await waitShowdownTabReady(tab.id)) || tab;
  try {
    await chrome.tabs.update(tab.id, { active: true, autoDiscardable: false });
  } catch (err) {
    // ignore
  }
  await pinShowdownTab(tab);
  try {
    let result = null;
    const playFn = typeof pageShowdownPlay === "function"
      ? pageShowdownPlay
      : (typeof globalThis.pageShowdownPlay === "function" ? globalThis.pageShowdownPlay : null);
    if (playFn) {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        world: "MAIN",
        func: playFn,
        args: [{ enabled: enabled !== false }],
      });
      result = results && results[0] ? results[0].result : null;
    } else {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        world: "MAIN",
        files: ["showdown_play.js"],
      });
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        world: "MAIN",
        func: (on) => {
          const fn = (typeof pageShowdownPlay === "function" && pageShowdownPlay)
            || (typeof globalThis !== "undefined" && globalThis.pageShowdownPlay)
            || (typeof window !== "undefined" && window.pageShowdownPlay);
          if (typeof fn !== "function") {
            return { ok: false, error: "Showdown bot missing." };
          }
          return fn({ enabled: on });
        },
        args: [enabled !== false],
      });
      result = results && results[0] ? results[0].result : null;
    }
    if (result && result.ok) {
      result.tab_id = tab.id;
      return { ok: true, result };
    }
    const detail = (result && result.error) || showdownPlayLoadError || "Showdown inject failed.";
    return { ok: false, error: detail };
  } catch (err) {
    return { ok: false, error: String(err && err.message ? err.message : err) };
  }
}

async function findManiaTabs() {
  try {
    const tabs = await chrome.tabs.query({});
    return tabs.filter((t) => t && t.id != null && isManiaUrl(t.url));
  } catch (err) {
    return [];
  }
}

async function pinManiaTab(tab) {
  if (!tab || tab.id == null) return;
  await saveManiaTabPin(tab.id);
  try {
    await chrome.tabs.update(tab.id, { autoDiscardable: false });
  } catch (err) {
    // ignore
  }
}

async function resolveManiaTab() {
  await loadManiaTabPin();
  const pinned = await getTabById(maniaTabId);
  if (pinned && isManiaUrl(pinned.url)) return pinned;
  if (pinned && maniaTabId != null && !isManiaUrl(pinned.url)) {
    await saveManiaTabPin(null);
  }
  const maniaTabs = await findManiaTabs();
  if (maniaTabs.length === 1) return maniaTabs[0];
  if (maniaTabs.length > 1) {
    const focused = maniaTabs.find((t) => t.active);
    if (focused) return focused;
    maniaTabs.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0));
    return maniaTabs[0];
  }
  return await activeTab();
}

function sleepMs(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitManiaTabReady(tabId) {
  for (let i = 0; i < 40; i++) {
    let tab = null;
    try {
      tab = await chrome.tabs.get(tabId);
    } catch (err) {
      return null;
    }
    if (tab && tab.status === "complete" && isManiaUrl(tab.url)) return tab;
    await sleepMs(250);
  }
  try {
    return await chrome.tabs.get(tabId);
  } catch (err) {
    return null;
  }
}

async function runManiaPlay(enabled, startMap) {
  await loadManiaTabPin();
  let tab = await resolveManiaTab();
  if (!tab || tab.id == null || !isManiaUrl(tab.url)) {
    const maniaTabs = await findManiaTabs();
    tab = maniaTabs[0] || null;
  }
  if (!tab || tab.id == null || !isManiaUrl(tab.url)) {
    if (!enabled) {
      return { ok: true, result: { playing: false, message: "Mania bot off." } };
    }
    try {
      tab = await chrome.tabs.create({ url: "https://webosumania.com/", active: true });
    } catch (err) {
      return { ok: false, error: "Open https://webosumania.com/ in Edge, then try again." };
    }
  }
  tab = (await waitManiaTabReady(tab.id)) || tab;
  try {
    await chrome.tabs.update(tab.id, { active: true, autoDiscardable: false });
  } catch (err) {
    // ignore
  }
  try {
    if (tab.windowId != null) await chrome.windows.update(tab.windowId, { focused: true });
  } catch (err) {
    // ignore
  }
  await pinManiaTab(tab);
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "MAIN",
      func: pageManiaPlay,
      args: [{ enabled: enabled !== false, startMap: !!startMap }],
    });
    const result = results && results[0] ? results[0].result : null;
    if (result && result.ok) {
      result.tab_id = tab.id;
      return { ok: true, result };
    }
    return { ok: false, error: (result && result.error) || "Mania inject failed." };
  } catch (err) {
    return { ok: false, error: String(err && err.message ? err.message : err) };
  }
}

async function getTabById(tabId) {
  if (tabId == null) return null;
  try {
    return await chrome.tabs.get(tabId);
  } catch (err) {
    return null;
  }
}

async function findChessTabs() {
  try {
    const tabs = await chrome.tabs.query({});
    return tabs.filter((t) => t && t.id != null && isChessUrl(t.url));
  } catch (err) {
    return [];
  }
}

async function pinChessTab(tab) {
  if (!tab || tab.id == null) return;
  await saveChessTabPin(tab.id);
  try {
    await chrome.tabs.update(tab.id, { autoDiscardable: false });
  } catch (err) {
    // ignore
  }
}

async function resolveChessTab() {
  await loadChessTabPin();
  const pinned = await getTabById(chessTabId);
  if (pinned && isChessUrl(pinned.url)) return pinned;
  if (pinned && chessTabId != null && !isChessUrl(pinned.url)) {
    await saveChessTabPin(null);
  }

  const chessTabs = await findChessTabs();
  if (chessTabs.length === 1) return chessTabs[0];
  if (chessTabs.length > 1) {
    const focused = chessTabs.find((t) => t.active);
    if (focused) return focused;
    chessTabs.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0));
    return chessTabs[0];
  }

  return await activeTab();
}

async function runChessRead() {
  await loadChessTabPin();
  const tried = new Set();
  const candidates = [];
  const push = (tab) => {
    if (!tab || tab.id == null || tried.has(tab.id)) return;
    tried.add(tab.id);
    candidates.push(tab);
  };

  push(await getTabById(chessTabId));
  for (const tab of await findChessTabs()) push(tab);
  push(await activeTab());

  let lastError = "No chess board pieces found on this page.";
  for (const tab of candidates) {
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: pageChessRead,
      });
      const result = results && results[0] ? results[0].result : null;
      if (result && result.ok) {
        await pinChessTab(tab);
        result.tab_id = tab.id;
        return { ok: true, result };
      }
      if (result && result.error) lastError = result.error;
    } catch (err) {
      lastError = String(err && err.message ? err.message : err);
    }
  }
  return { ok: false, error: lastError };
}

async function runChessMove(message) {
  const tab = await resolveChessTab();
  if (!tab || tab.id == null) {
    return { ok: false, error: "No chess tab found. Open Chess.com/Lichess and try again." };
  }
  try {
    // Activate the tab inside its window without requiring OS focus (Discord can stay on top).
    try {
      await chrome.tabs.update(tab.id, { active: true, autoDiscardable: false });
    } catch (err) {
      // ignore
    }
    await pinChessTab(tab);
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: pageChessMove,
      args: [
        {
          from: message.from || "",
          to: message.to || "",
          promotion: message.promotion || "",
          orientation: message.orientation || "white",
        },
      ],
    });
    const result = results && results[0] ? results[0].result : null;
    if (result && result.ok) {
      return { ok: true, result: result.message || "Moved." };
    }
    return { ok: false, error: (result && result.error) || "Chess move failed." };
  } catch (err) {
    return { ok: false, error: String(err && err.message ? err.message : err) };
  }
}

async function pushActiveTab() {
  const tab = await activeTab();
  if (!tab) return;
  const payload = await readTab(tab);
  try {
    await chrome.runtime.sendMessage({ type: "bridge-send", payload });
  } catch (err) {
    // Offscreen may be waking up.
  }
}

async function handleAction(message) {
  const action = String(message.action || "");
  try {
    if (action === "chess_reset") {
      await saveChessTabPin(null);
      return { ok: true, result: "Chess tab pin cleared." };
    }
    if (action === "chess_read") {
      return await runChessRead();
    }
    if (action === "chess_move") {
      return await runChessMove(message);
    }
    if (action === "mania_play") {
      return await runManiaPlay(true, message.startMap !== false);
    }
    if (action === "mania_stop") {
      return await runManiaPlay(false, false);
    }
    if (action === "showdown_play") {
      return await runShowdownPlay(true, message.createIfMissing !== false);
    }
    if (action === "showdown_stop") {
      return await runShowdownPlay(false, false);
    }
    if (action === "play_detect") {
      await loadShowdownTabPin();
      await loadManiaTabPin();
      await loadChessTabPin();
      const tab = await activeTab();
      return {
        ok: true,
        result: {
          showdown: (await findShowdownTabs()).length > 0,
          mania: (await findManiaTabs()).length > 0,
          chess: (await findChessTabs()).length > 0,
          url: (tab && tab.url) || "",
        },
      };
    }

    const tab = await activeTab();
    if (!tab || tab.id == null) {
      return { ok: false, error: "No active browser tab." };
    }
    if (action === "navigate") {
      const url = String(message.url || "");
      if (!url) return { ok: false, error: "Missing url." };
      await chrome.tabs.update(tab.id, { url });
      return { ok: true, result: `Navigated to ${url}` };
    }
    if (action === "read") {
      return { ok: true, result: await readTab(tab) };
    }
    if (action === "click") {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: pageClick,
        args: [
          {
            selector: message.selector || "",
            text: message.text || "",
            x: message.x,
            y: message.y,
          },
        ],
      });
      const result = results && results[0] ? results[0].result : null;
      if (result && result.ok) return { ok: true, result: result.message || "Clicked." };
      return { ok: false, error: (result && result.error) || "Click failed." };
    }
    if (action === "type") {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: pageType,
        args: [
          {
            selector: message.selector || "",
            text: message.text || "",
            clear: !!message.clear,
          },
        ],
      });
      const result = results && results[0] ? results[0].result : null;
      if (result && result.ok) return { ok: true, result: result.message || "Typed." };
      return { ok: false, error: (result && result.error) || "Type failed." };
    }
    if (action === "press") {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: pagePress,
        args: [String(message.key || "Enter")],
      });
      const result = results && results[0] ? results[0].result : null;
      if (result && result.ok) return { ok: true, result: result.message || "Pressed." };
      return { ok: false, error: (result && result.error) || "Press failed." };
    }
    return { ok: false, error: `Unknown action: ${action}` };
  } catch (err) {
    return { ok: false, error: String(err && err.message ? err.message : err) };
  }
}

async function readTab(tab) {
  const payload = { url: tab.url || "", title: tab.title || "", selection: "", page_text: "" };
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => ({
        selection: window.getSelection()?.toString() || "",
        page_text: (document.body?.innerText || "").slice(0, 6000),
      }),
    });
    if (results && results[0] && results[0].result) {
      Object.assign(payload, results[0].result);
    }
  } catch (err) {
    // Restricted pages such as chrome:// cannot be scripted.
  }
  return payload;
}

function pageClick(opts) {
  const selector = (opts && opts.selector) || "";
  const text = ((opts && opts.text) || "").trim().toLowerCase();
  let el = null;
  if (selector) {
    try {
      el = document.querySelector(selector);
    } catch (err) {
      return { ok: false, error: "Invalid selector." };
    }
  }
  if (!el && text) {
    const candidates = Array.from(
      document.querySelectorAll("button, a, [role='button'], input, .btn, [onclick]")
    );
    el =
      candidates.find((node) => (node.innerText || node.value || "").trim().toLowerCase().includes(text)) ||
      null;
  }
  if (!el && opts && typeof opts.x === "number" && typeof opts.y === "number") {
    el = document.elementFromPoint(opts.x, opts.y);
  }
  if (!el) return { ok: false, error: "Element not found." };
  el.scrollIntoView({ block: "center", inline: "center" });
  const rect = el.getBoundingClientRect();
  const cx = rect.left + rect.width / 2;
  const cy = rect.top + rect.height / 2;
  for (const type of ["pointerdown", "mousedown", "mouseup", "pointerup", "click"]) {
    el.dispatchEvent(
      new MouseEvent(type, { bubbles: true, cancelable: true, view: window, clientX: cx, clientY: cy })
    );
  }
  if (typeof el.click === "function") el.click();
  return { ok: true, message: `Clicked ${el.tagName.toLowerCase()}` };
}

function pageType(opts) {
  const selector = (opts && opts.selector) || "";
  const text = (opts && opts.text) || "";
  let el = null;
  if (selector) {
    try {
      el = document.querySelector(selector);
    } catch (err) {
      return { ok: false, error: "Invalid selector." };
    }
  }
  if (!el) el = document.activeElement;
  if (!el || (el.tagName !== "INPUT" && el.tagName !== "TEXTAREA" && !el.isContentEditable)) {
    return { ok: false, error: "No editable field focused." };
  }
  el.focus();
  if (opts && opts.clear) {
    if ("value" in el) el.value = "";
    else el.textContent = "";
  }
  if ("value" in el) {
    el.value = `${el.value || ""}${text}`;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  } else {
    el.textContent = `${el.textContent || ""}${text}`;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
  return { ok: true, message: "Typed into field." };
}

async function pageManiaPlay(opts) {
  const enable = !opts || opts.enabled !== false;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function fireClick(el) {
    if (!el) return false;
    try {
      el.scrollIntoView({ block: "center", inline: "center" });
    } catch (err) {
      // ignore
    }
    const rect = el.getBoundingClientRect();
    const cx = rect.left + Math.max(rect.width / 2, 1);
    const cy = rect.top + Math.max(rect.height / 2, 1);
    const base = { bubbles: true, cancelable: true, view: window, clientX: cx, clientY: cy, button: 0, buttons: 1 };
    try {
      el.dispatchEvent(
        new PointerEvent("pointerdown", {
          ...base,
          pointerId: 1,
          pointerType: "mouse",
          isPrimary: true,
        })
      );
    } catch (err) {
      // ignore
    }
    el.dispatchEvent(new MouseEvent("mousedown", base));
    try {
      el.dispatchEvent(
        new PointerEvent("pointerup", {
          ...base,
          buttons: 0,
          pointerId: 1,
          pointerType: "mouse",
          isPrimary: true,
        })
      );
    } catch (err) {
      // ignore
    }
    el.dispatchEvent(new MouseEvent("mouseup", { ...base, buttons: 0 }));
    el.dispatchEvent(new MouseEvent("click", { ...base, buttons: 0 }));
    if (typeof el.click === "function") el.click();
    return true;
  }

  function persistAutoplay(on) {
    try {
      const raw = localStorage.getItem("settings");
      const data = raw ? JSON.parse(raw) : { state: {}, version: 0 };
      if (!data.state || typeof data.state !== "object") data.state = {};
      if (!data.state.mods || typeof data.state.mods !== "object") data.state.mods = {};
      data.state.mods.autoplay = !!on;
      localStorage.setItem("settings", JSON.stringify(data));
      return true;
    } catch (err) {
      return false;
    }
  }

  function clickNamedTab(name) {
    const re = new RegExp("^\\s*" + name + "\\s*$", "i");
    const tabs = Array.from(document.querySelectorAll('[role="tab"], button'));
    const tab = tabs.find((el) => re.test((el.innerText || "").trim()));
    return tab ? fireClick(tab) : false;
  }

  function autoplaySwitchOn() {
    const switches = Array.from(
      document.querySelectorAll('button[role="switch"], [role="switch"], input[type="checkbox"]')
    );
    for (const sw of switches) {
      const row = sw.closest("label") || sw.parentElement || sw;
      const text = ((row && row.innerText) || sw.getAttribute("aria-label") || "").toLowerCase();
      if (!text.includes("autoplay")) continue;
      return (
        sw.getAttribute("aria-checked") === "true" ||
        sw.getAttribute("data-state") === "checked" ||
        !!sw.checked
      );
    }
    return false;
  }

  async function setAutoplaySwitch(on) {
    if (autoplaySwitchOn() === on) return true;
    clickNamedTab("Mods");
    await sleep(250);
    const switches = Array.from(
      document.querySelectorAll('button[role="switch"], [role="switch"], input[type="checkbox"]')
    );
    for (const sw of switches) {
      const row = sw.closest("label") || sw.parentElement || sw;
      const text = ((row && row.innerText) || sw.getAttribute("aria-label") || "").toLowerCase();
      if (!text.includes("autoplay")) continue;
      const checked =
        sw.getAttribute("aria-checked") === "true" ||
        sw.getAttribute("data-state") === "checked" ||
        !!sw.checked;
      if (checked !== on) fireClick(sw);
      await sleep(120);
      clickNamedTab("Filters");
      return true;
    }
    clickNamedTab("Filters");
    return false;
  }

  function isGame(obj) {
    return !!(obj && obj.inputSystem && Array.isArray(obj.columns) && obj.song);
  }

  function gameFromReact(el) {
    if (!el) return null;
    const key = Object.keys(el).find(
      (k) => k.startsWith("__reactFiber") || k.startsWith("__reactInternalInstance")
    );
    if (!key) return null;
    let fiber = el[key];
    for (let i = 0; i < 80 && fiber; i++) {
      let hook = fiber.memoizedState;
      for (let j = 0; j < 80 && hook; j++) {
        const value = hook.memoizedState;
        if (isGame(value)) return value;
        if (value && isGame(value.game)) return value.game;
        hook = hook.next;
      }
      const props = fiber.memoizedProps;
      if (props && isGame(props.game)) return props.game;
      fiber = fiber.return;
    }
    return null;
  }

  function playColumns(game, t) {
    const input = game.inputSystem;
    if (!input) return;
    for (let col = 0; col < game.columns.length; col++) {
      for (let n = 0; n < 128; n++) {
        const obj = game.columns[col][game.currentColumnIndices[col]];
        if (!obj || !obj.data) break;
        const start = obj.data.time;
        const end = obj.data.endTime != null ? obj.data.endTime : start;
        if (!input.pressedColumns[col] && t >= start) {
          input.hit(col, start);
        }
        if (input.pressedColumns[col] && t >= end) {
          input.release(col, end);
          continue;
        }
        break;
      }
    }
  }

  function prepareFrame(game) {
    const bot = window.__opusManiaBot;
    if (!bot || !bot.enabled || !game) return;
    bot.keys = (game.difficulty && game.difficulty.keyCount) || game.columns.length || 0;
    bot.state = game.state || "";
    if (game.state === "WAIT") {
      try {
        if (game.inputSystem && game.inputSystem.tappedColumns) {
          game.inputSystem.tappedColumns[0] = true;
        }
      } catch (err) {
        // ignore
      }
      bot.playing = false;
      return;
    }
    if (game.state !== "PLAY") {
      bot.playing = false;
      return;
    }
    bot.playing = true;
    const t = Math.round(game.song.seek() * 1000);
    game.timeElapsed = t;
    if (game.replayPlayer) return;
    playColumns(game, t);
  }

  function attachReplay(game) {
    if (!game || game.replayPlayer) return;
    const events = [];
    const objects = game.hitObjects || [];
    for (let i = 0; i < objects.length; i++) {
      const obj = objects[i];
      if (!obj || obj.type !== "tap") continue;
      const start = obj.time;
      const end = obj.endTime != null ? obj.endTime : obj.time;
      events.push({ column: obj.column, time: start, type: "down" });
      events.push({ column: obj.column, time: end, type: "up" });
    }
    events.sort((a, b) => a.time - b.time || (a.type === "down" ? -1 : 1));
    let index = 0;
    game.replayPlayer = {
      __opus: true,
      currentEventIndex: 0,
      replayData: { version: 2, inputs: [] },
      update(time) {
        while (index < events.length && events[index].time <= time) {
          const event = events[index++];
          if (event.type === "down") game.inputSystem.hit(event.column, event.time);
          else game.inputSystem.release(event.column, event.time);
        }
        this.currentEventIndex = index;
      },
    };
    try {
      if (game.mods) game.mods.autoplay = true;
    } catch (err) {
      // ignore
    }
  }

  function patchGame(game) {
    if (!game) return game;
    attachReplay(game);
    if (game.__opusPatched) return game;
    game.__opusPatched = true;
    const origUpdate = game.update;
    if (typeof origUpdate === "function") {
      game.update = function (time) {
        prepareFrame(this);
        return origUpdate.call(this, time);
      };
    }
    const origHitObjects = game.updateHitObjects;
    if (typeof origHitObjects === "function") {
      game.updateHitObjects = function () {
        prepareFrame(this);
        return origHitObjects.call(this);
      };
    }
    return game;
  }

  function wrapPixiApp(app) {
    if (!app || !app.ticker || app.ticker.__opusWrapped) return;
    app.ticker.__opusWrapped = true;
    const orig = app.ticker.add.bind(app.ticker);
    app.ticker.add = function (fn, ctx, pri) {
      const wrapped = function (ticker) {
        if (!window.__opusGame) {
          if (ctx && isGame(ctx)) window.__opusGame = ctx;
          else if (this && isGame(this)) window.__opusGame = this;
          else window.__opusGame = findGame();
        }
        const game = window.__opusGame;
        if (game) {
          patchGame(game);
          prepareFrame(game);
        }
        return fn.call(this, ticker);
      };
      return orig(wrapped, ctx, pri);
    };
  }

  function installPixiHook() {
    if (window.__opusPixiHooked) return;
    window.__opusPixiHooked = true;
    try {
      let current = window.__PIXI_APP__;
      Object.defineProperty(window, "__PIXI_APP__", {
        configurable: true,
        enumerable: true,
        get() {
          return current;
        },
        set(app) {
          current = app;
          wrapPixiApp(app);
        },
      });
      if (current) wrapPixiApp(current);
    } catch (err) {
      if (window.__PIXI_APP__) wrapPixiApp(window.__PIXI_APP__);
    }
  }

  function tapToStart() {
    const codes = ["KeyD", "KeyF", "KeyJ"];
    for (const code of codes) {
      const key = code === "Space" ? " " : code.slice(3).toLowerCase();
      const down = new KeyboardEvent("keydown", { key, code, bubbles: true, cancelable: true });
      const up = new KeyboardEvent("keyup", { key, code, bubbles: true, cancelable: true });
      document.dispatchEvent(down);
      window.dispatchEvent(down);
      document.dispatchEvent(up);
      window.dispatchEvent(up);
    }
  }

  function findGame() {
    if (isGame(window.__opusGame)) return window.__opusGame;
    let el = document.querySelector("canvas");
    while (el) {
      const found = gameFromReact(el);
      if (found) {
        window.__opusGame = patchGame(found);
        return window.__opusGame;
      }
      el = el.parentElement;
    }
    const hosts = document.querySelectorAll("canvas, [class*='fixed'], [class*='inset']");
    for (const host of hosts) {
      const found = gameFromReact(host);
      if (found) {
        window.__opusGame = patchGame(found);
        return window.__opusGame;
      }
    }
    if (window.__PIXI_APP__) wrapPixiApp(window.__PIXI_APP__);
    return isGame(window.__opusGame) ? patchGame(window.__opusGame) : null;
  }

  function difficultyButtons() {
    return Array.from(document.querySelectorAll("button, [role='button']")).filter((btn) => {
      const text = btn.innerText || "";
      if (/high score|reset mods|filters/i.test(text)) return false;
      return /★|⭐/.test(text) || /\d+\.\d{2}\s*★/.test(text);
    });
  }

  function beatmapTriggers() {
    const out = [];
    const push = (el) => {
      if (el && out.indexOf(el) < 0) out.push(el);
    };
    const badges = Array.from(document.querySelectorAll("span, div, button")).filter((el) =>
      /^(RANKED|LOVED|QUALIFIED|PENDING|WIP|GRAVEYARD|LOCAL)$/i.test((el.innerText || "").trim())
    );
    for (const badge of badges) push(badge.closest("button") || badge.parentElement);
    const images = Array.from(document.querySelectorAll("img")).filter((img) => {
      const src = String(img.currentSrc || img.src || img.getAttribute("src") || "");
      return /ppy\.sh|covers|beatmap|sayobot|catboy/i.test(src);
    });
    for (const img of images) push(img.closest("button") || img.parentElement || img);
    return out.filter((el) => {
      try {
        const box = el.getBoundingClientRect();
        return box.width > 80 && box.height > 40;
      } catch (err) {
        return true;
      }
    });
  }

  async function startFirstMap() {
    if (document.querySelector("canvas")) return "ingame";
    for (let i = 0; i < 48 && !beatmapTriggers().length && !difficultyButtons().length; i++) {
      await sleep(250);
    }
    let diffs = difficultyButtons();
    if (!diffs.length) {
      const covers = beatmapTriggers();
      if (covers.length) fireClick(covers[0]);
      for (let i = 0; i < 20 && !difficultyButtons().length; i++) {
        await sleep(150);
      }
      diffs = difficultyButtons();
    }
    if (diffs.length) {
      fireClick(diffs[0]);
      return "started";
    }
    return "menu";
  }

  function installBot() {
    if (window.__opusManiaBot && window.__opusManiaBot.stop) {
      try {
        window.__opusManiaBot.stop();
      } catch (err) {
        // ignore
      }
    }
    const bot = {
      enabled: true,
      hits: 0,
      keys: 0,
      playing: false,
      state: "",
    };
    const tick = () => {
      if (!bot.enabled) return;
      const game = findGame();
      if (!game) {
        bot.playing = false;
        bot.state = document.querySelector("canvas") ? "loading" : "menu";
        if (bot.state === "loading") {
          const now = Date.now();
          if (!bot._lastTap || now - bot._lastTap > 500) {
            bot._lastTap = now;
            tapToStart();
          }
        }
        return;
      }
      patchGame(game);
      bot.keys = (game.difficulty && game.difficulty.keyCount) || game.columns.length || 0;
      bot.state = game.state || "";
      bot.playing = game.state === "PLAY";
      if (game.state === "WAIT") {
        try {
          if (game.inputSystem && game.inputSystem.tappedColumns) {
            game.inputSystem.tappedColumns[0] = true;
          }
          if (typeof game.play === "function") game.play();
        } catch (err) {
          // ignore
        }
      }
    };
    const interval = setInterval(tick, 250);
    bot.stop = () => {
      bot.enabled = false;
      clearInterval(interval);
    };
    window.__opusManiaBot = bot;
    return bot;
  }

  persistAutoplay(enable);
  installPixiHook();

  if (!enable) {
    if (window.__opusManiaBot && window.__opusManiaBot.stop) window.__opusManiaBot.stop();
    window.__opusManiaBot = null;
    window.__opusGame = null;
    return { ok: true, message: "Mania autoplay off.", autoplay: false, playing: false, keys: 0, canvas: false };
  }

  const already = window.__opusManiaBot && window.__opusManiaBot.enabled;
  let flipped = false;
  let started = "idle";
  const hasCanvas = () => !!document.querySelector("canvas");
  if (opts && opts.startMap && !hasCanvas()) {
    flipped = await setAutoplaySwitch(true);
    started = await startFirstMap();
    if (started === "started") {
      for (let i = 0; i < 48 && !hasCanvas(); i++) {
        await sleep(250);
      }
    }
  }

  const bot = already ? window.__opusManiaBot : installBot();
  const game = findGame();
  if (game) patchGame(game);
  if (hasCanvas() && (!game || game.state === "WAIT")) tapToStart();
  const keys = (game && game.difficulty && game.difficulty.keyCount) || bot.keys || 0;
  const state = (game && game.state) || bot.state || started || "menu";
  return {
    ok: true,
    message:
      keys > 0 || state === "PLAY" || state === "WAIT"
        ? `Playing Web osu!mania perfectly (${keys || "?"}K).`
        : started === "started"
          ? "Starting a map with autoplay."
          : "Couldn't start a map — open a beatmap on webosu!mania.",
    autoplay: true,
    switched: flipped,
    playing: !!(game && game.state === "PLAY"),
    keys,
    state,
    started,
    canvas: hasCanvas(),
  };
}

function pagePress(key) {
  const target = document.activeElement || document.body;
  const name = key || "Enter";
  target.dispatchEvent(
    new KeyboardEvent("keydown", { key: name, code: name, bubbles: true, cancelable: true })
  );
  target.dispatchEvent(
    new KeyboardEvent("keyup", { key: name, code: name, bubbles: true, cancelable: true })
  );
  return { ok: true, message: `Pressed ${name}` };
}

function pageChessRead() {
  const files = "abcdefgh";
  const empty = Array.from({ length: 8 }, () => Array(8).fill(null));

  function sqToRC(sq) {
    if (!sq || sq.length < 2) return null;
    const f = files.indexOf(sq[0].toLowerCase());
    const r = 8 - parseInt(sq[1], 10);
    if (f < 0 || r < 0 || r > 7) return null;
    return [r, f];
  }

  function put(grid, row, col, piece) {
    if (row == null || col == null) return;
    grid[row][col] = piece;
  }

  function queryPieces(selectors) {
    const out = [];
    const seen = new Set();
    const pushAll = (node, sel) => {
      if (!node || !node.querySelectorAll) return;
      try {
        for (const el of node.querySelectorAll(sel)) {
          if (seen.has(el)) continue;
          seen.add(el);
          out.push(el);
        }
      } catch (err) {
        // ignore
      }
    };
    for (const sel of selectors) {
      pushAll(document, sel);
      // Shallow shadow pierce only on known board hosts (not the whole DOM).
      for (const host of document.querySelectorAll("wc-chess-board, chess-board, cg-board, .cg-board, .board")) {
        if (host.shadowRoot) pushAll(host.shadowRoot, sel);
      }
    }
    return out;
  }

  function pieceFromLichess(el) {
    const cls = el.className || "";
    const color = cls.includes("white") ? "w" : cls.includes("black") ? "b" : "";
    let kind = "";
    if (cls.includes("king")) kind = "k";
    else if (cls.includes("queen")) kind = "q";
    else if (cls.includes("rook")) kind = "r";
    else if (cls.includes("bishop")) kind = "b";
    else if (cls.includes("knight")) kind = "n";
    else if (cls.includes("pawn")) kind = "p";
    if (!color || !kind) return null;
    return color === "w" ? kind.toUpperCase() : kind;
  }

  function pieceFromChessCom(el) {
    const cls = (el.className || "").toLowerCase();
    const m = cls.match(/\b([wb])([prnbqk])\b/);
    if (!m) return null;
    return m[1] === "w" ? m[2].toUpperCase() : m[2];
  }

  function chessComSquare(el) {
    const cls = el.className || "";
    const m = cls.match(/square-(\d)(\d)/);
    if (!m) return null;
    const file = parseInt(m[1], 10) - 1;
    const rank = parseInt(m[2], 10);
    return sqToRC(files[file] + String(rank));
  }

  function isTransientPiece(el) {
    const cls = String(el.className || "");
    return /dragging|animating|moving|being-dragged/i.test(cls);
  }

  const grid = empty.map((row) => row.slice());
  let source = "";
  let orientation = "white";
  let unstable = false;

  // Lichess uses <piece> tags; Chess.com uses <div class="piece wp square-52">.
  const liPieces = queryPieces(["cg-board piece", ".cg-board piece", "piece.white", "piece.black"]);
  if (liPieces.length) {
    source = "lichess";
    const wrap = document.querySelector(".cg-wrap, cg-container, .main-board");
    if (wrap && /orientation-black|black/i.test(wrap.className || "")) orientation = "black";
    if (document.querySelector(".cg-wrap.orientation-black, .orientation-black")) orientation = "black";
    liPieces.forEach((el) => {
      if (isTransientPiece(el)) {
        unstable = true;
        return;
      }
      const piece = pieceFromLichess(el);
      const sq = (el.className.match(/square-([a-h][1-8])/) || [])[1];
      const rc = sqToRC(sq);
      if (piece && rc) {
        if (grid[rc[0]][rc[1]]) unstable = true;
        put(grid, rc[0], rc[1], piece);
      }
    });
  }

  if (!source) {
    const ccPieces = queryPieces([
      "wc-chess-board .piece",
      "chess-board .piece",
      ".board .piece",
      "div.piece[class*='square-']",
      ".piece[class*='square-']",
    ]);
    if (ccPieces.length) {
      source = "chesscom";
      const boardEl =
        document.querySelector("wc-chess-board, chess-board, .board") ||
        ccPieces[0].closest("wc-chess-board, chess-board, .board");
      if (boardEl) {
        const flipped =
          boardEl.getAttribute("flipped") === "true" ||
          /flipped/i.test(boardEl.className || "");
        if (flipped) orientation = "black";
      }
      ccPieces.forEach((el) => {
        if (isTransientPiece(el)) {
          unstable = true;
          return;
        }
        const piece = pieceFromChessCom(el);
        const rc = chessComSquare(el);
        if (piece && rc) {
          if (grid[rc[0]][rc[1]]) unstable = true;
          put(grid, rc[0], rc[1], piece);
        }
      });
    }
  }

  function gridToFen(g) {
    const rows = [];
    for (let r = 0; r < 8; r++) {
      let emptyCount = 0;
      let row = "";
      for (let c = 0; c < 8; c++) {
        const p = g[r][c];
        if (!p) emptyCount += 1;
        else {
          if (emptyCount) row += String(emptyCount);
          emptyCount = 0;
          row += p;
        }
      }
      if (emptyCount) row += String(emptyCount);
      rows.push(row);
    }
    return rows.join("/");
  }

  const pieceCount = grid.flat().filter(Boolean).length;
  if (!source || pieceCount < 2) {
    return { ok: false, error: "No chess board pieces found on this page." };
  }

  let turn = "w";
  const bodyText = ((document.body && document.body.innerText) || "").slice(0, 2500).toLowerCase();
  if (bodyText.includes("black to play") || bodyText.includes("black to move")) turn = "b";
  if (bodyText.includes("white to play") || bodyText.includes("white to move")) turn = "w";

  const bottomClock = document.querySelector(
    ".rclock-bottom, .clock-bottom, .board-layout-bottom .clock, [class*='clock-bottom']"
  );
  const topClock = document.querySelector(
    ".rclock-top, .clock-top, .board-layout-top .clock, [class*='clock-top']"
  );
  const bottomRunning =
    bottomClock &&
    (/running|clock-player-turn|player-clock-active/i.test(bottomClock.className || "") ||
      bottomClock.getAttribute("data-active") === "true");
  const topRunning =
    topClock &&
    (/running|clock-player-turn|player-clock-active/i.test(topClock.className || "") ||
      topClock.getAttribute("data-active") === "true");
  if (bottomRunning) turn = orientation.startsWith("b") ? "b" : "w";
  if (topRunning) turn = orientation.startsWith("b") ? "w" : "b";

  const fenBoard = gridToFen(grid);
  if (!(fenBoard.includes("K") && fenBoard.includes("k"))) unstable = true;
  const fen = `${fenBoard} ${turn} - - 0 1`;
  let rect = null;
  const boardNode =
    document.querySelector("cg-board") ||
    document.querySelector("chess-board") ||
    document.querySelector("wc-chess-board") ||
    document.querySelector(".cg-board") ||
    document.querySelector(".board");
  if (boardNode) {
    const r = boardNode.getBoundingClientRect();
    rect = { x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, left: r.left };
  }

  return {
    ok: true,
    fen,
    source,
    orientation,
    piece_count: pieceCount,
    unstable,
    board_rect: rect,
    url: location.href,
    title: document.title,
  };
}

function pageChessMove(opts) {
  const from = String((opts && opts.from) || "").toLowerCase();
  const to = String((opts && opts.to) || "").toLowerCase();
  if (!/^[a-h][1-8]$/.test(from) || !/^[a-h][1-8]$/.test(to)) {
    return Promise.resolve({ ok: false, error: "Bad squares." });
  }
  const orientation = String((opts && opts.orientation) || "white").toLowerCase();
  const boardNode =
    document.querySelector("cg-board") ||
    document.querySelector("chess-board") ||
    document.querySelector("wc-chess-board") ||
    document.querySelector(".cg-board") ||
    document.querySelector(".board");
  if (!boardNode) return Promise.resolve({ ok: false, error: "Board not found." });
  const r = boardNode.getBoundingClientRect();
  if (!r.width || !r.height) return Promise.resolve({ ok: false, error: "Board has no size." });

  function squareCenter(sq) {
    const file = sq.charCodeAt(0) - "a".charCodeAt(0);
    const rank = parseInt(sq[1], 10) - 1;
    let f = file;
    let rk = rank;
    if (orientation.startsWith("b")) {
      f = 7 - f;
      rk = 7 - rk;
    }
    const x = r.left + ((f + 0.5) * r.width) / 8;
    const y = r.top + (((7 - rk) + 0.5) * r.height) / 8;
    return { x, y };
  }

  function fireClick(x, y) {
    const el = document.elementFromPoint(x, y) || boardNode;
    for (const type of ["pointerdown", "mousedown", "mouseup", "pointerup", "click"]) {
      el.dispatchEvent(
        new MouseEvent(type, {
          bubbles: true,
          cancelable: true,
          view: window,
          clientX: x,
          clientY: y,
        })
      );
    }
    if (typeof el.click === "function") el.click();
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  return (async () => {
    const a = squareCenter(from);
    const b = squareCenter(to);
    fireClick(a.x, a.y);
    await sleep(180 + Math.floor(Math.random() * 320));
    fireClick(b.x, b.y);
    if (opts && opts.promotion) {
      await sleep(160 + Math.floor(Math.random() * 120));
      fireClick(b.x, b.y - Math.min(40, r.height / 10));
    }
    return { ok: true, message: `Moved ${from}${to}` };
  })();
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || !message.type) return false;

  if (message.type === "bridge-badge") {
    setBadge(!!message.on, message.label || "");
    return false;
  }
  if (message.type === "bridge-push-tab") {
    pushActiveTab().then(() => sendResponse({ ok: true })).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message.type === "bridge-action") {
    handleAction(message.message || {})
      .then((reply) => sendResponse(reply))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }
  if (message.type === "reconnect") {
    ensureOffscreen()
      .then(() => chrome.runtime.sendMessage({ type: "bridge-reconnect" }))
      .then((r) => sendResponse(r || { ok: true }))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message.type === "status") {
    chrome.runtime
      .sendMessage({ type: "bridge-status" })
      .then((r) => sendResponse(r || { connected: false }))
      .catch(() => sendResponse({ connected: false }));
    return true;
  }
  return false;
});

chrome.alarms.create("opus-keepalive", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== "opus-keepalive") return;
  await ensureOffscreen();
  try {
    await chrome.runtime.sendMessage({ type: "bridge-reconnect" });
  } catch (err) {
    // ignore
  }
});

chrome.runtime.onStartup.addListener(() => ensureOffscreen());
chrome.runtime.onInstalled.addListener(() => ensureOffscreen());

chrome.tabs.onRemoved.addListener((tabId) => {
  if (chessTabId === tabId) {
    saveChessTabPin(null);
  }
  if (maniaTabId === tabId) {
    saveManiaTabPin(null);
  }
  if (showdownTabId === tabId) {
    saveShowdownTabPin(null);
  }
});

chrome.tabs.onActivated.addListener(() => {
  ensureOffscreen().then(() => pushActiveTab());
});
chrome.tabs.onUpdated.addListener((_id, change, tab) => {
  if (tab.active && (change.status === "complete" || change.title || change.url)) {
    ensureOffscreen().then(() => pushActiveTab());
  }
});

chrome.downloads.onChanged.addListener(async (delta) => {
  if (!delta.state || delta.state.current !== "complete") return;
  try {
    const [item] = await chrome.downloads.search({ id: delta.id });
    if (item && item.filename) {
      await ensureOffscreen();
      await chrome.runtime.sendMessage({
        type: "bridge-send",
        payload: { download_path: item.filename, url: item.url || "", title: item.filename },
      });
    }
  } catch (err) {
    // ignore
  }
});

ensureOffscreen().then(() => {
  setBadge(false, "…");
  return loadChessTabPin();
});
