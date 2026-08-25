const PORT = 5840;
let socket;
let connected = false;

function connect() {
  try {
    socket = new WebSocket(`ws://127.0.0.1:${PORT}/ws/browser`);
  } catch (err) {
    setTimeout(connect, 2000);
    return;
  }
  socket.addEventListener("open", () => {
    connected = true;
    chrome.action.setBadgeText({ text: "on" });
    chrome.action.setBadgeBackgroundColor({ color: "#d4a574" });
    pushActiveTab();
  });
  socket.addEventListener("close", () => {
    connected = false;
    chrome.action.setBadgeText({ text: "" });
    setTimeout(connect, 2000);
  });
  socket.addEventListener("error", () => {
    try {
      socket.close();
    } catch (e) {
      /* ignore */
    }
  });
  socket.addEventListener("message", async (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    if (!message || !message.action || !message.request_id) return;
    const reply = await handleAction(message);
    send({ request_id: message.request_id, ...reply });
  });
}

function send(payload) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ browser: "firefox", ...payload }));
  }
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab || null;
}

async function handleAction(message) {
  const action = String(message.action || "");
  try {
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
    if (action === "chess_read") {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: pageChessRead,
      });
      const result = results && results[0] ? results[0].result : null;
      if (result && result.ok) return { ok: true, result };
      return { ok: false, error: (result && result.error) || "Chess board not found." };
    }
    if (action === "chess_move") {
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
      if (result && result.ok) return { ok: true, result: result.message || "Moved." };
      return { ok: false, error: (result && result.error) || "Chess move failed." };
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
    // Restricted pages cannot be scripted.
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

  const grid = empty.map((row) => row.slice());
  let source = "";
  let orientation = "white";

  const liPieces = document.querySelectorAll("cg-board piece, .cg-board piece");
  if (liPieces.length) {
    source = "lichess";
    if (document.querySelector(".cg-wrap.orientation-black, .orientation-black")) orientation = "black";
    liPieces.forEach((el) => {
      const piece = pieceFromLichess(el);
      const sq = (el.className.match(/square-([a-h][1-8])/) || [])[1];
      const rc = sqToRC(sq);
      if (piece && rc) put(grid, rc[0], rc[1], piece);
    });
  }

  if (!source) {
    const ccPieces = document.querySelectorAll(
      "chess-board piece, wc-chess-board piece, .board piece, piece[class*='square-']"
    );
    if (ccPieces.length) {
      source = "chesscom";
      const boardEl = document.querySelector("chess-board, wc-chess-board, .board");
      if (boardEl) {
        const flipped =
          boardEl.getAttribute("flipped") === "true" ||
          /flipped/i.test(boardEl.className || "");
        if (flipped) orientation = "black";
      }
      ccPieces.forEach((el) => {
        const piece = pieceFromChessCom(el);
        const rc = chessComSquare(el);
        if (piece && rc) put(grid, rc[0], rc[1], piece);
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
    board_rect: rect,
    url: location.href,
    title: document.title,
  };
}

function pageChessMove(opts) {
  const from = String((opts && opts.from) || "").toLowerCase();
  const to = String((opts && opts.to) || "").toLowerCase();
  if (!/^[a-h][1-8]$/.test(from) || !/^[a-h][1-8]$/.test(to)) {
    return { ok: false, error: "Bad squares." };
  }
  const orientation = String((opts && opts.orientation) || "white").toLowerCase();
  const boardNode =
    document.querySelector("cg-board") ||
    document.querySelector("chess-board") ||
    document.querySelector("wc-chess-board") ||
    document.querySelector(".cg-board") ||
    document.querySelector(".board");
  if (!boardNode) return { ok: false, error: "Board not found." };
  const r = boardNode.getBoundingClientRect();
  if (!r.width || !r.height) return { ok: false, error: "Board has no size." };

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

  const a = squareCenter(from);
  const b = squareCenter(to);
  fireClick(a.x, a.y);
  fireClick(b.x, b.y);
  if (opts && opts.promotion) {
    fireClick(b.x, b.y - Math.min(40, r.height / 10));
  }
  return { ok: true, message: `Moved ${from}${to}` };
}

async function pushActiveTab() {
  const tab = await activeTab();
  if (!tab) return;
  send(await readTab(tab));
}

chrome.tabs.onActivated.addListener(() => pushActiveTab());
chrome.tabs.onUpdated.addListener((_id, change, tab) => {
  if (tab.active && (change.status === "complete" || change.title || change.url)) {
    pushActiveTab();
  }
});

chrome.downloads.onChanged.addListener(async (delta) => {
  if (!delta.state || delta.state.current !== "complete") return;
  try {
    const [item] = await chrome.downloads.search({ id: delta.id });
    if (item && item.filename) {
      send({ download_path: item.filename, url: item.url || "", title: item.filename });
    }
  } catch (err) {
    // ignore
  }
});

connect();
setInterval(() => {
  if (connected) pushActiveTab();
}, 8000);
