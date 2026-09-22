const $ = (id) => document.getElementById(id);

const fields = [
  "voice_responses",
  "tts_voice",
  "volume",
  "input_device",
  "output_device",
  "default_browser",
  "desktop_audio_device",
  "game_audio_device",
  "call_audio_device",
  "use_cloned_voice",
  "screen_awareness",
  "file_access",
  "background_protection",
  "location_city",
  "openai_api_key",
  "openai_base_url",
  "model",
  "discord_enabled",
  "discord_bot_token",
  "discord_prefix",
  "discord_allowed_guilds",
  "discord_allowed_channels",
  "spotify_client_id",
];

function valueOf(el) {
  if (el.type === "checkbox") return el.checked;
  if (el.type === "range") return Number(el.value);
  return el.value;
}

async function loadSettings() {
  const data = await (await fetch("/api/settings")).json();
  $("voice_responses").checked = !!data.voice_responses;
  $("volume").value = data.volume ?? 80;
  $("volumeLabel").textContent = $("volume").value;
  $("screen_awareness").checked = !!data.screen_awareness;
  $("file_access").checked = !!data.file_access;
  $("background_protection").checked = data.background_protection ?? !!data.download_scanning;
  $("location_city").value = data.location_city || "";
  $("openai_base_url").value = data.openai_base_url || "";
  $("model").value = data.model || "openai/gpt-oss-20b";
  $("discord_enabled").checked = !!data.discord_enabled;
  $("discord_prefix").value = data.discord_prefix || "opus";
  $("discord_allowed_guilds").value = data.discord_allowed_guilds || "";
  $("discord_allowed_channels").value = data.discord_allowed_channels || "";
  if ($("spotify_client_id")) $("spotify_client_id").value = data.spotify_client_id || "";
  applySpotifyStatus(data);
  const locationHint = $("locationHint");
  if (locationHint) {
    locationHint.textContent = data.location_label
      ? `Detected near ${data.location_label}. Set a city to override.`
      : "Opus uses Windows location or your network to guess weather near you.";
  }
  $("keyHint").textContent = data.openai_api_key_set
    ? "An API key is already saved on this PC."
    : "Needed for wake-word transcription and answers.";
  const discordHint = $("discordHint");
  if (discordHint) {
    discordHint.textContent = data.discord_bot_token_set
      ? "Discord token is saved. Use 'opus <question>' or mention the bot. Say 'opus invite' for a server invite link."
      : "Paste your Discord bot token, then turn on Enable Discord replies.";
  }
  const invite = data.discord_invite_url || "";
  const inviteWrap = $("discordInviteWrap");
  const inviteHint = $("discordInviteHint");
  const inviteBtn = $("btnDiscordInvite");
  if (inviteWrap) inviteWrap.hidden = !invite;
  if (inviteHint) inviteHint.hidden = !invite;
  if (inviteBtn) {
    inviteBtn.onclick = () => {
      if (invite) window.open(invite, "_blank", "noopener");
    };
  }
  loadBrowserOptions(data.default_browser);
  await loadDevices(
    data.input_device,
    data.output_device,
    data.desktop_audio_device,
    data.game_audio_device,
    data.call_audio_device
  );
  $("use_cloned_voice").checked = !!data.use_cloned_voice;
  await loadVoices(data.tts_voice);
  await loadCloneStatus();
}

function applySpotifyStatus(data) {
  const hint = $("spotifyHint");
  const redirect = $("spotifyRedirect");
  if (redirect) {
    redirect.textContent = data.spotify_redirect_uri
      ? `Redirect URI: ${data.spotify_redirect_uri}`
      : "";
  }
  if (!hint) return;
  if (data.spotify_connected) {
    if (data.spotify_needs_reconnect) {
      hint.textContent = data.spotify_account
        ? `Connected as ${data.spotify_account}, but playlist access needs a reconnect. Disconnect, then Connect Spotify again.`
        : "Spotify is connected, but playlist access needs a reconnect. Disconnect, then Connect Spotify again.";
    } else {
      hint.textContent = data.spotify_account
        ? `Connected as ${data.spotify_account}. Premium is required for playback.`
        : "Spotify is connected. Premium is required for playback.";
    }
  } else {
    hint.textContent = "Create a Spotify app at developer.spotify.com, add the redirect URI, paste the Client ID, then connect.";
  }
}

function loadBrowserOptions(current) {
  const browsers = [
    { id: "opera-gx", name: "Opera GX" },
    { id: "opera", name: "Opera" },
    { id: "chrome", name: "Google Chrome" },
    { id: "firefox", name: "Firefox" },
    { id: "edge", name: "Microsoft Edge" },
    { id: "brave", name: "Brave" },
    { id: "vivaldi", name: "Vivaldi" },
  ];
  const select = $("default_browser");
  select.innerHTML = "";
  for (const b of browsers) {
    const option = document.createElement("option");
    option.value = b.id;
    option.textContent = b.name;
    select.appendChild(option);
  }
  if (current) select.value = current;
}

async function loadCloneStatus() {
  const status = await (await fetch("/api/voice/status")).json();
  applyCloneStatus(status);
}

function applyCloneStatus(status) {
  const el = $("cloneStatus");
  if (!el) return;
  if (status.ready) {
    el.textContent = status.sample_name
      ? `Baked from ${status.sample_name}. Fast Ultron voice is on — no warmup.`
      : "A voice sample is loaded.";
    if (status.enabled) $("use_cloned_voice").checked = true;
  } else {
    el.textContent = "Send a 5–15 second clip of clean speech to clone a voice.";
  }
}

async function loadVoices(current) {
  const voices = await (await fetch("/api/voices")).json();
  const select = $("tts_voice");
  select.innerHTML = "";
  for (const item of voices) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.name;
    select.appendChild(option);
  }
  if (current) select.value = current;
}

async function loadDevices(input, output, desktopTrack, gameTrack, callTrack) {
  const devices = await (await fetch("/api/devices")).json();
  fillSelect($("input_device"), devices.inputs, input, "Default microphone");
  fillSelect($("output_device"), devices.outputs, output, "Default speakers");
  fillSelect($("desktop_audio_device"), devices.inputs, desktopTrack, "Auto desktop source");
  fillSelect($("game_audio_device"), devices.inputs, gameTrack, "Off");
  fillSelect($("call_audio_device"), devices.inputs, callTrack, "Off");
}

function fillSelect(select, items, current, blankLabel) {
  select.innerHTML = "";
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = blankLabel;
  select.appendChild(blank);
  for (const item of items) {
    const option = document.createElement("option");
    option.value = String(item.index);
    option.textContent = item.name;
    select.appendChild(option);
  }
  if (current !== null && current !== undefined && current !== "") {
    select.value = String(current);
  }
}

async function save(patch) {
  await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

function bindSaves() {
  for (const id of fields) {
    const el = $(id);
    const eventName = el.tagName === "SELECT" || el.type === "checkbox" || el.type === "range" ? "change" : "blur";
    el.addEventListener(eventName, () => {
      const patch = { [id]: valueOf(el) };
      if (id === "openai_api_key" && !el.value) return;
      if (id === "discord_bot_token" && !el.value) return;
      if (id === "volume") $("volumeLabel").textContent = el.value;
      save(patch);
    });
  }
}

function addBubble(role, content) {
  const node = document.createElement("div");
  node.className = `bubble ${role === "you" ? "you" : "opus"}`;
  node.textContent = content;
  $("log").appendChild(node);
  $("log").scrollTop = $("log").scrollHeight;
}

function applyStatus(status) {
  $("mode").textContent = status.mode || "idle";
  $("status").textContent = status.message || "";
  const awake = status.listening && status.mode !== "paused";
  $("orb").classList.toggle("live", awake);
  $("orb").classList.toggle("asleep", !status.listening);
  $("orb").src = status.listening ? "/static/icon.png" : "/static/icon_sleep.png";
  document.body.classList.toggle("asleep", !status.listening);
  const wakeBtn = $("wakeBtn");
  if (wakeBtn) wakeBtn.style.display = status.listening ? "none" : "block";
  const capture = $("captureStatus");
  if (capture) {
    const tracks = Array.isArray(status.audio_tracks) && status.audio_tracks.length
      ? ` Tracks: ${status.audio_tracks.join(", ")}.`
      : "";
    if (status.recording) capture.textContent = `Recording the desktop…${tracks}`;
    else if (status.buffering) capture.textContent = `Clip buffer on — say “clip that” for the last 30 seconds.${tracks}`;
    else capture.textContent = "Clips → D:\\Clips\\Opus\\Clips · Recordings → D:\\Clips\\Opus\\Recordings";
  }
}

function connect() {
  const socket = new WebSocket(`ws://${location.host}/ws/ui`);
  socket.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === "status") applyStatus(payload.data);
    if (payload.type === "message") addBubble(payload.data.role, payload.data.content);
    if (payload.type === "download_status") applyDownloadStatus(payload.data);
  });
  socket.addEventListener("close", () => setTimeout(connect, 1200));
}

$("hide").addEventListener("click", () => {
  fetch("/api/panel/hide", { method: "POST" });
});

$("orb").addEventListener("click", () => {
  fetch("/api/listen", { method: "POST" });
});

$("wakeBtn").addEventListener("click", () => {
  fetch("/api/wake", { method: "POST" });
});

function capture(path) {
  fetch(path, { method: "POST" })
    .then((r) => r.json())
    .then((data) => {
      if (data.result) addBubble("opus", data.result);
    });
}

$("btnRecord").addEventListener("click", () => capture("/api/record/start"));
$("btnStop").addEventListener("click", () => capture("/api/record/stop"));
$("btnClip").addEventListener("click", () => capture("/api/clip"));
$("btnShot").addEventListener("click", () => capture("/api/screenshot"));
$("btnFolder").addEventListener("click", () => fetch("/api/clips/open", { method: "POST" }));

$("btnCloneBrowse").addEventListener("click", () => {
  fetch("/api/voice/browse", { method: "POST" })
    .then((r) => r.json())
    .then((data) => {
      if (data.result) addBubble("opus", data.result);
      if (data.status) applyCloneStatus(data.status);
    });
});

$("clone_file").addEventListener("change", () => {
  const file = $("clone_file").files[0];
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  fetch("/api/voice/upload", { method: "POST", body })
    .then((r) => r.json())
    .then((data) => {
      if (data.result) addBubble("opus", data.result);
      if (data.status) applyCloneStatus(data.status);
      $("clone_file").value = "";
    });
});

$("btnClonePreview").addEventListener("click", () => capture("/api/voice/preview"));

$("askForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = $("ask").value.trim();
  if (!text) return;
  $("ask").value = "";
  fetch("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
});

$("volume").addEventListener("input", () => {
  $("volumeLabel").textContent = $("volume").value;
});

// --- Download ---
$("btnDownload").addEventListener("click", () => {
  const url = $("dl_url").value.trim();
  if (!url) { $("dlStatus").textContent = "Paste a URL first."; return; }
  const format = $("dl_format").value;
  $("dlStatus").textContent = "Starting...";
  $("btnDownload").disabled = true;
  fetch("/api/download", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, format }),
  }).then(r => r.json()).then(data => {
    $("dlStatus").textContent = data.result || data.error || "";
    if (data.error) $("btnDownload").disabled = false;
  });
});

$("btnDlFolder").addEventListener("click", () => fetch("/api/download/open", { method: "POST" }));

function applyDownloadStatus(data) {
  if (!data) return;
  $("dlStatus").textContent = data.progress || "";
  if (!data.running) {
    $("btnDownload").disabled = false;
    if (data.done) { $("dlStatus").textContent = "Done!"; $("dl_url").value = ""; }
  }
}
// --- End Download ---

const btnSpotifyConnect = $("btnSpotifyConnect");
if (btnSpotifyConnect) {
  btnSpotifyConnect.addEventListener("click", async () => {
    const clientId = ($("spotify_client_id") && $("spotify_client_id").value.trim()) || "";
    if (clientId) await save({ spotify_client_id: clientId });
    const result = await (await fetch("/api/apps/spotify/connect", { method: "POST" })).json();
    const hint = $("spotifyHint");
    if (!result.ok && hint) hint.textContent = result.error || "Couldn't start Spotify login.";
    if (result.ok) {
      const poll = setInterval(async () => {
        const data = await (await fetch("/api/settings")).json();
        applySpotifyStatus(data);
        if (data.spotify_connected) clearInterval(poll);
      }, 1500);
      setTimeout(() => clearInterval(poll), 120000);
    }
  });
}
const btnSpotifyDisconnect = $("btnSpotifyDisconnect");
if (btnSpotifyDisconnect) {
  btnSpotifyDisconnect.addEventListener("click", async () => {
    const data = await (await fetch("/api/apps/spotify/disconnect", { method: "POST" })).json();
    applySpotifyStatus({
      spotify_connected: !!data.connected,
      spotify_account: data.account || "",
      spotify_redirect_uri: data.redirect_uri || ($("spotifyRedirect") && $("spotifyRedirect").textContent.replace("Redirect URI: ", "")),
    });
  });
}

loadSettings();
bindSaves();
connect();
fetch("/api/status")
  .then((r) => r.json())
  .then((data) => {
    applyStatus(data.status || {});
    for (const item of data.conversation || []) addBubble(item.role, item.content);
  });
