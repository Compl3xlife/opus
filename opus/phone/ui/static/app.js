const $ = (id) => document.getElementById(id);
const chat = $("chat");
const statusEl = $("status");
const modeEl = $("mode");
const talk = $("talk");
const textInput = $("text");
const sheet = $("sheet");

const SILENCE_MS = 600;
const MAX_LISTEN_MS = 10000;
const TAP_MS = 280;
const SPEECH_RMS = 0.045;

let settings = {};
let recorder = null;
let chunks = [];
let holding = false;
let speaking = false;
let autoListen = false;
let holdStarted = 0;
let listenWatch = null;
let listenStream = null;

function speak(text) {
  if (!settings.voice_responses || !text || !window.speechSynthesis) return;
  window.speechSynthesis.cancel();
  const utter = new SpeechSynthesisUtterance(text);
  utter.rate = 0.92;
  utter.pitch = 0.85;
  utter.volume = Math.max(0.1, Math.min(1, Number(settings.volume || 80) / 100));
  speaking = true;
  utter.onend = () => { speaking = false; };
  window.speechSynthesis.speak(utter);
}

function addBubble(role, text) {
  if (!text) return;
  const el = document.createElement("div");
  el.className = `bubble ${role}`;
  el.textContent = text;
  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
}

function listenUrl() {
  return new URL("?listen=1", window.location.href).href.split("#")[0];
}

function applySettings(data) {
  settings = data || {};
  $("voice_responses").checked = Boolean(settings.voice_responses);
  $("volume").value = String(settings.volume ?? 80);
  $("volumeLabel").textContent = String(settings.volume ?? 80);
  $("openai_api_key").placeholder = settings.openai_api_key_set ? "Key saved — paste to replace" : "Paste a key";
  $("spotify_client_id").value = settings.spotify_client_id || "";
  $("location_city").value = settings.location_city || "";
  $("redirectHint").textContent = `Add this redirect URI in the Spotify app: ${settings.spotify_redirect_uri || ""}`;
  $("spotifyAccount").textContent = settings.spotify_connected
    ? `Connected as ${settings.spotify_account || "Spotify"}`
    : "Not connected.";
  $("keyHint").textContent = settings.openai_api_key_set
    ? "API key is saved on this phone."
    : "Needed for talk and answers. Uses this phone's internet, not your PC.";
  if ($("siriUrl")) $("siriUrl").textContent = listenUrl();
}

function loadSettings() {
  applySettings(window.OpusPhone.loadSettings());
}

function saveSettings() {
  const patch = {
    voice_responses: $("voice_responses").checked,
    volume: Number($("volume").value),
    spotify_client_id: $("spotify_client_id").value.trim(),
    location_city: $("location_city").value.trim(),
  };
  const key = $("openai_api_key").value.trim();
  if (key) patch.openai_api_key = key;
  applySettings(window.OpusPhone.saveSettings(patch));
  $("openai_api_key").value = "";
  statusEl.textContent = "Saved on this phone.";
}

async function ask(text) {
  const trimmed = (text || "").trim();
  if (!trimmed) return;
  addBubble("you", trimmed);
  textInput.value = "";
  statusEl.textContent = "Thinking…";
  modeEl.textContent = "Thinking";
  try {
    const reply = await window.OpusPhone.handleText(trimmed);
    applySettings(window.OpusPhone.loadSettings());
    if (reply) {
      addBubble("opus", reply);
      speak(reply);
    }
    statusEl.textContent = "Tap to talk, or say Hey Siri, Opus.";
    modeEl.textContent = "Phone";
  } catch (err) {
    const reply = window.OpusPhone.NO_CONNECTION;
    addBubble("opus", reply);
    speak(reply);
    statusEl.textContent = reply;
    modeEl.textContent = "Phone";
  }
}

async function transcribe(blob, ext = "webm") {
  statusEl.textContent = "Hearing you…";
  const data = await window.OpusPhone.transcribe(blob, `utterance.${ext}`);
  if (data.error && !data.text) throw new Error(data.error);
  return (data.text || "").trim();
}

function rmsLevel(bytes) {
  let sum = 0;
  for (let i = 0; i < bytes.length; i += 1) {
    const v = (bytes[i] - 128) / 128;
    sum += v * v;
  }
  return Math.sqrt(sum / Math.max(1, bytes.length));
}

function stopSilenceWatch() {
  if (listenWatch) {
    clearInterval(listenWatch);
    listenWatch = null;
  }
}

function watchSilence(stream) {
  stopSilenceWatch();
  if (!stream) {
    listenWatch = setTimeout(() => endHold({ preventDefault() {} }, true), MAX_LISTEN_MS);
    return;
  }
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) {
    listenWatch = setTimeout(() => endHold({ preventDefault() {} }, true), MAX_LISTEN_MS);
    return;
  }
  const ctx = new AudioCtx();
  const source = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  source.connect(analyser);
  const data = new Uint8Array(analyser.fftSize);
  const started = Date.now();
  let heard = false;
  let silentFor = 0;
  listenWatch = setInterval(() => {
    if (!holding) {
      stopSilenceWatch();
      ctx.close().catch(() => {});
      return;
    }
    analyser.getByteTimeDomainData(data);
    const rms = rmsLevel(data);
    if (rms >= SPEECH_RMS) {
      heard = true;
      silentFor = 0;
    } else if (heard) {
      silentFor += 80;
    }
    if ((heard && silentFor >= SILENCE_MS) || Date.now() - started >= MAX_LISTEN_MS) {
      ctx.close().catch(() => {});
      endHold({ preventDefault() {} }, true);
    }
  }, 80);
}

async function startHold(event) {
  if (event) event.preventDefault();
  if (holding) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    statusEl.textContent = "This browser can't record audio.";
    return;
  }
  holding = true;
  autoListen = false;
  holdStarted = Date.now();
  talk.classList.add("hot");
  talk.textContent = "Listening…";
  chunks = [];
  try {
    listenStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const types = ["audio/mp4", "audio/webm;codecs=opus", "audio/webm"];
    const mime = types.find((type) => MediaRecorder.isTypeSupported(type)) || "";
    recorder = mime ? new MediaRecorder(listenStream, { mimeType: mime }) : new MediaRecorder(listenStream);
    recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    recorder.start(200);
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    statusEl.textContent = "Listening…";
    modeEl.textContent = "Listening";
  } catch (err) {
    holding = false;
    talk.classList.remove("hot");
    talk.textContent = "Tap to talk";
    statusEl.textContent = "Microphone permission is needed. Open Opus from the home screen, then tap the button.";
  }
}

async function endHold(event, force = false) {
  if (event) event.preventDefault();
  if (!holding) return;
  const heldFor = Date.now() - holdStarted;
  if (!force && heldFor < TAP_MS) {
    autoListen = true;
    watchSilence(listenStream);
    statusEl.textContent = "Listening until you finish talking.";
    return;
  }
  if (!force && autoListen) return;
  holding = false;
  autoListen = false;
  stopSilenceWatch();
  talk.classList.remove("hot");
  talk.textContent = "Tap to talk";
  const rec = recorder;
  recorder = null;
  const stream = listenStream;
  listenStream = null;
  if (!rec) return;
  const ext = (rec.mimeType || "").includes("mp4") ? "m4a" : "webm";
  const blob = await new Promise((resolve) => {
    rec.onstop = () => {
      if (stream) stream.getTracks().forEach((track) => track.stop());
      resolve(new Blob(chunks, { type: rec.mimeType || "audio/webm" }));
    };
    rec.stop();
  });
  if (!blob.size) {
    statusEl.textContent = "I didn't catch that.";
    return;
  }
  try {
    const heard = await transcribe(blob, ext);
    if (!heard) {
      statusEl.textContent = "I didn't catch that.";
      return;
    }
    await ask(heard);
  } catch (err) {
    const reply = err.message || window.OpusPhone.NO_CONNECTION;
    statusEl.textContent = reply;
    if (reply === window.OpusPhone.NO_CONNECTION) {
      addBubble("opus", reply);
      speak(reply);
    }
  }
}

function wantAutoListen() {
  const params = new URLSearchParams(window.location.search);
  return params.get("listen") === "1" || params.get("siri") === "1";
}

async function startFromSiri() {
  statusEl.textContent = "Siri opened Opus. Starting the mic…";
  await startHold(null);
  if (!holding) return;
  autoListen = true;
  watchSilence(listenStream);
}

$("composer").addEventListener("submit", (event) => {
  event.preventDefault();
  ask(textInput.value);
});

$("settingsBtn").addEventListener("click", () => sheet.classList.remove("hidden"));
$("closeSheet").addEventListener("click", () => sheet.classList.add("hidden"));
sheet.addEventListener("click", (event) => {
  if (event.target === sheet) sheet.classList.add("hidden");
});
$("saveSettings").addEventListener("click", () => {
  try {
    saveSettings();
  } catch (err) {
    statusEl.textContent = err.message || "Save failed.";
  }
});
$("volume").addEventListener("input", () => {
  $("volumeLabel").textContent = $("volume").value;
});

$("spotifyConnect").addEventListener("click", async () => {
  try {
    saveSettings();
    const data = await window.OpusPhone.startSpotifyLogin();
    if (!data.ok) {
      statusEl.textContent = data.error || "Couldn't start Spotify login.";
      return;
    }
    window.location.href = data.url;
  } catch (err) {
    statusEl.textContent = err.message || window.OpusPhone.NO_CONNECTION;
  }
});

$("spotifyDisconnect").addEventListener("click", () => {
  applySettings(window.OpusPhone.disconnectSpotify());
  statusEl.textContent = "Spotify disconnected.";
});

if ($("copySiriUrl")) {
  $("copySiriUrl").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(listenUrl());
      statusEl.textContent = "Siri URL copied. Paste it into a Shortcut, then Add to Siri.";
    } catch {
      statusEl.textContent = "Copy the URL shown in settings into a Shortcut.";
    }
  });
}

talk.addEventListener("pointerdown", startHold);
window.addEventListener("pointerup", (event) => endHold(event, false));
window.addEventListener("pointercancel", (event) => endHold(event, true));

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").catch(() => {});
}

loadSettings();
statusEl.textContent = "Tap to talk, or set up Hey Siri, Opus in settings.";
if (wantAutoListen()) {
  window.addEventListener("load", () => {
    setTimeout(() => startFromSiri(), 250);
  });
}
