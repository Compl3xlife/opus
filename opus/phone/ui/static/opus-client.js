/* Standalone phone Opus — runs in the browser, no PC required. */
(() => {
  const STORE_KEY = "opus-phone-settings";
  const HISTORY_KEY = "opus-phone-history";
  const NO_CONNECTION = "Sorry I cannot find a good connection for that at the moment.";
  const SYSTEM = `You are Opus on a phone. Talk like a person, not a computer.
Keep spoken answers to one short sentence.
You control apps through their APIs, the way Siri does — never by tapping the screen for the user.
If they ask about weather or temperature, call get_weather. If they ask where they are, call get_location.
If they ask about news, current events, or anything time-sensitive, call search_web before answering.
If they ask to play something on Spotify, or a song/artist/playlist, call play_spotify with only the name. Empty query resumes.
If they ask to pause, skip, go back, or what's playing, call control_spotify.
You cannot play chess, Pokémon Showdown, osu, or any autoplay games on this phone version — those stay on the PC app.
You cannot type into other apps, take screenshots, clip, record the desktop, or run Windows programs.
You were created by Involutional.
When a tool succeeds, STOP. Say Done.`;

  const TOOLS = [
    {
      type: "function",
      function: {
        name: "play_spotify",
        description: "Play or resume music through the connected Spotify account API.",
        parameters: {
          type: "object",
          properties: {
            query: { type: "string", description: "Song, artist, playlist, or album. Empty string resumes." },
          },
          required: ["query"],
          additionalProperties: false,
        },
      },
    },
    {
      type: "function",
      function: {
        name: "control_spotify",
        description: "Pause, skip, go back, or say what's playing on Spotify.",
        parameters: {
          type: "object",
          properties: {
            action: { type: "string", enum: ["pause", "skip", "previous", "now"] },
          },
          required: ["action"],
          additionalProperties: false,
        },
      },
    },
    {
      type: "function",
      function: {
        name: "search_web",
        description: "Search the web for current facts before answering factual questions.",
        parameters: {
          type: "object",
          properties: {
            query: { type: "string" },
            limit: { type: "integer" },
          },
          required: ["query"],
          additionalProperties: false,
        },
      },
    },
    {
      type: "function",
      function: {
        name: "get_weather",
        description: "Get the current weather for the user's location or an optional city.",
        parameters: {
          type: "object",
          properties: { city: { type: "string", description: "Optional city name" } },
          additionalProperties: false,
        },
      },
    },
    {
      type: "function",
      function: {
        name: "get_location",
        description: "Tell the user where they are.",
        parameters: {
          type: "object",
          properties: { reason: { type: "string" } },
          additionalProperties: false,
        },
      },
    },
  ];

  const WEATHER_CODES = {
    0: "clear skies",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "foggy",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    80: "rain showers",
    81: "rain showers",
    82: "heavy rain showers",
    95: "thunderstorms",
    96: "thunderstorms with hail",
    99: "severe thunderstorms",
  };

  const SPOTIFY_SCOPES = [
    "user-modify-playback-state",
    "user-read-playback-state",
    "user-read-currently-playing",
    "user-library-read",
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-read-email",
  ].join(" ");

  const defaults = () => ({
    voice_responses: true,
    volume: 80,
    openai_api_key: "",
    openai_base_url: "",
    openai_model: "",
    spotify_client_id: "",
    location_city: "",
    location_lat: null,
    location_lon: null,
    location_label: "",
    spotify_access_token: "",
    spotify_refresh_token: "",
    spotify_token_expires_at: 0,
    spotify_user_name: "",
  });

  function loadRaw() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      return raw ? { ...defaults(), ...JSON.parse(raw) } : defaults();
    } catch {
      return defaults();
    }
  }

  function saveRaw(data) {
    const next = { ...defaults(), ...data };
    localStorage.setItem(STORE_KEY, JSON.stringify(next));
    return next;
  }

  function snapshot(data) {
    const settings = data || loadRaw();
    return {
      ...settings,
      openai_api_key: "",
      openai_api_key_set: Boolean(settings.openai_api_key),
      spotify_connected: Boolean(settings.spotify_refresh_token),
      spotify_account: settings.spotify_user_name || "",
      spotify_redirect_uri: redirectUri(),
    };
  }

  function online() {
    return navigator.onLine !== false;
  }

  function isNetworkError(err) {
    if (!online()) return true;
    const text = String((err && err.message) || err || "").toLowerCase();
    return (
      (err && err.name === "TypeError") ||
      /failed to fetch|networkerror|load failed|offline|err_internet|timed out|timeout/.test(text)
    );
  }

  async function fetchJson(url, options = {}, timeoutMs = 14000) {
    if (!online()) throw Object.assign(new Error("offline"), { network: true });
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const res = await fetch(url, { ...options, signal: ctrl.signal });
      const data = await res.json().catch(() => ({}));
      return { res, data };
    } catch (err) {
      if (err.name === "AbortError" || isNetworkError(err)) {
        throw Object.assign(new Error("offline"), { network: true });
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  function stripWake(text) {
    return String(text || "")
      .replace(/^\s*(hey|ok|okay|hi)?\s*opus\s*[,.!?:]*/i, "")
      .trim();
  }

  function rollDice(text) {
    let sides = 6;
    const match = /\bd(\d{1,3})\b/i.exec(text || "");
    if (match) sides = Math.max(2, Math.min(1000, Number(match[1]) || 6));
    const value = 1 + Math.floor(Math.random() * sides);
    return `Rolled ${value} on a d${sides}.`;
  }

  function pcOnly(text) {
    if (/\b(play|start)\s+(osu!?|mania|webosu|chess|pokemon|pokémon|showdown)/i.test(text)) {
      return "Game autoplay is only on the PC version of Opus.";
    }
    if (/\b(scan|cyber\s*box)\b/i.test(text)) return "Scanning is only on the PC app.";
    if (/\b(screenshot|clip that|start recording)\b/i.test(text)) return "Clips and screenshots are only on the PC app.";
    return "";
  }

  const COINFLIP = /\b(flip(?:\s+a|\s+the)?\s+coins?|coin\s*flips?|heads\s+or\s+tails)\b/i;
  const DICE = /\b(roll(?:\s+a|\s+the)?\s+dice|roll(?:\s+a)?\s+die|\bd(?:20|12|10|8|6|4)\b)\b/i;
  const MUTE = /\b(mute|disable)\b.*\b(voice|speech|talking)\b/i;
  const UNMUTE = /\b(unmute|enable)\b.*\b(voice|speech|talking)\b/i;
  const VOLUME = /\bvolume\b(?:\s+(up|down|to)\s*(\d{1,3})?)?/i;
  const CREATOR = /\b(who (created|made|built|coded)|who'?s your (creator|maker)|who owns you)\b/i;
  const DISMISS = /\b(thank you|thanks|thank ya|appreciate it)\b/i;
  const SLEEP = /\b(go to sleep|good ?night|that'?s all|never ?mind|stop listening)\b/i;
  const SHUSH = /\b(shut up|stop talking|be quiet|cancel that|forget (that|it))\b/i;
  const WEATHER = /\b(weather|temperature|forecast|how hot|how cold|will it rain|rain today)\b/i;
  const LOCATION = /\b(where am i|my location|what city am i in)\b/i;
  const SPOTIFY_PAUSE = /\b(pause|stop)\s+(the\s+)?(music|song|track|spotify)\b|\bpause\s+spotify\b|\bspotify\s+pause\b/i;
  const SPOTIFY_SKIP = /\b(skip|next)\s+(this\s+|the\s+)?(song|track|tune)\b|\bnext\s+song\b|\bspotify\s+(skip|next)\b/i;
  const SPOTIFY_PREV = /\b(previous|last)\s+(song|track)\b|\bgo\s+back\s+(a\s+)?(song|track)\b|\bspotify\s+(previous|back)\b/i;
  const SPOTIFY_NOW = /\bwhat('?s| is) (playing|this song|the song)\b|\bwhat song is (this|playing)\b|\bnow playing\b/i;
  const SPOTIFY_RESUME = /\b(?:play|resume|start|unpause)\s+(?:on\s+)?spotify\b|\bspotify\s+(?:play|resume|start)\b/i;
  const SPOTIFY_PLAY = /\b(?:play|start)\s+(.+?)(?:\s+(?:on|in|from)\s+spotify)?(?:\s+(?:please|now|for me))?[.!?,]*\s*$/i;

  function matchLocal(text) {
    const command = stripWake(text);
    if (!command) return { kind: "local", reply: "" };
    if (SHUSH.test(command)) return { kind: "local", reply: "" };
    if (DISMISS.test(command)) return { kind: "local", reply: "You're welcome." };
    if (SLEEP.test(command)) return { kind: "local", reply: "I'm still here whenever you need me." };
    if (COINFLIP.test(command)) {
      return { kind: "local", reply: `It's ${Math.random() < 0.5 ? "heads" : "tails"}.` };
    }
    if (DICE.test(command)) return { kind: "local", reply: rollDice(command) };
    if (MUTE.test(command)) {
      saveRaw({ ...loadRaw(), voice_responses: false });
      return { kind: "local", reply: "Voice responses off" };
    }
    if (UNMUTE.test(command)) {
      saveRaw({ ...loadRaw(), voice_responses: true });
      return { kind: "local", reply: "Voice responses on" };
    }
    const vol = VOLUME.exec(command);
    if (vol && /\bvolume\s+(up|down|to)\b|\bset\s+volume\b/i.test(command)) {
      const settings = loadRaw();
      let value = Number(settings.volume || 80);
      const dir = (vol[1] || "").toLowerCase();
      if (dir === "up") value = Math.min(100, value + 10);
      else if (dir === "down") value = Math.max(0, value - 10);
      else if (vol[2]) value = Math.max(0, Math.min(100, Number(vol[2])));
      saveRaw({ ...settings, volume: value });
      return { kind: "local", reply: `Volume ${value}` };
    }
    if (CREATOR.test(command)) return { kind: "local", reply: "Involutional" };
    const blocked = pcOnly(command);
    if (blocked) return { kind: "local", reply: blocked };
    if (WEATHER.test(command)) return { kind: "net", name: "weather", rest: command };
    if (LOCATION.test(command)) return { kind: "net", name: "location" };
    if (SPOTIFY_PAUSE.test(command)) return { kind: "net", name: "spotify_control", action: "pause" };
    if (SPOTIFY_SKIP.test(command)) return { kind: "net", name: "spotify_control", action: "skip" };
    if (SPOTIFY_PREV.test(command)) return { kind: "net", name: "spotify_control", action: "previous" };
    if (SPOTIFY_NOW.test(command)) return { kind: "net", name: "spotify_control", action: "now" };
    if (SPOTIFY_RESUME.test(command)) return { kind: "net", name: "spotify_play", query: "" };
    const play = SPOTIFY_PLAY.exec(command);
    if (play) {
      const query = (play[1] || "").replace(/\bon spotify\b/gi, "").trim();
      if (/\b(osu|chess|pokemon|pokémon|showdown|mania)\b/i.test(query)) {
        return { kind: "local", reply: "Game autoplay is only on the PC version of Opus." };
      }
      if (query) return { kind: "net", name: "spotify_play", query };
    }
    return { kind: "ask", text: command };
  }

  function apiBase(settings) {
    const configured = (settings.openai_base_url || "").replace(/\/+$/, "");
    if (configured) return configured;
    const key = settings.openai_api_key || "";
    if (key.startsWith("gsk_")) return "https://api.groq.com/openai/v1";
    return "https://api.openai.com/v1";
  }

  function chatModel(settings) {
    if (settings.openai_model) return settings.openai_model;
    return apiBase(settings).includes("groq.com") ? "openai/gpt-oss-20b" : "gpt-4o-mini";
  }

  function whisperModel(settings) {
    return apiBase(settings).includes("groq.com") ? "whisper-large-v3" : "whisper-1";
  }

  function todayLine() {
    return `Today is ${new Date().toLocaleDateString("en-US", { weekday: "long", year: "numeric", month: "long", day: "numeric" })}.`;
  }

  function history() {
    try {
      const raw = localStorage.getItem(HISTORY_KEY);
      const items = raw ? JSON.parse(raw) : [];
      return Array.isArray(items) ? items.slice(-10) : [];
    } catch {
      return [];
    }
  }

  function pushHistory(role, content) {
    if (!content) return;
    const items = history();
    items.push({ role, content: String(content).slice(0, 2000) });
    localStorage.setItem(HISTORY_KEY, JSON.stringify(items.slice(-10)));
  }

  async function llmChat(messages, useTools) {
    const settings = loadRaw();
    const { res, data } = await fetchJson(`${apiBase(settings)}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${settings.openai_api_key}`,
      },
      body: JSON.stringify({
        model: chatModel(settings),
        temperature: 0.4,
        messages,
        ...(useTools ? { tools: TOOLS } : {}),
      }),
    }, 45000);
    if (!res.ok) {
      const msg = (data.error && data.error.message) || `Chat failed (${res.status})`;
      if (res.status >= 500 || res.status === 429) {
        throw Object.assign(new Error(msg), { network: res.status >= 500 });
      }
      throw new Error(msg);
    }
    return data;
  }

  async function geocodeCity(city) {
    const query = encodeURIComponent((city || "").trim());
    if (!query) return null;
    const { res, data } = await fetchJson(
      `https://geocoding-api.open-meteo.com/v1/search?name=${query}&count=1&language=en&format=json`
    );
    if (!res.ok) throw Object.assign(new Error("geocode"), { network: true });
    const hit = (data.results || [])[0];
    if (!hit) return null;
    const label = [hit.name, hit.admin1, hit.country].filter(Boolean).join(", ");
    return { lat: hit.latitude, lon: hit.longitude, label: label || city };
  }

  function browserCoords() {
    return new Promise((resolve, reject) => {
      if (!navigator.geolocation) {
        reject(new Error("no geo"));
        return;
      }
      navigator.geolocation.getCurrentPosition(
        (pos) => resolve({ lat: pos.coords.latitude, lon: pos.coords.longitude, label: "your area" }),
        () => reject(new Error("no geo")),
        { timeout: 8000, maximumAge: 600000 }
      );
    });
  }

  async function resolveLocation(city) {
    const settings = loadRaw();
    const override = (city || settings.location_city || "").trim();
    if (override) {
      const geocoded = await geocodeCity(override);
      if (geocoded) {
        saveRaw({ ...settings, location_lat: geocoded.lat, location_lon: geocoded.lon, location_label: geocoded.label });
        return geocoded;
      }
    }
    if (settings.location_lat != null && settings.location_lon != null) {
      return {
        lat: Number(settings.location_lat),
        lon: Number(settings.location_lon),
        label: settings.location_label || "your area",
      };
    }
    try {
      const found = await browserCoords();
      saveRaw({ ...loadRaw(), location_lat: found.lat, location_lon: found.lon, location_label: found.label });
      return found;
    } catch {
      throw new Error("I couldn't figure out where you are. Set your city in settings.");
    }
  }

  async function getWeather(city) {
    const loc = await resolveLocation(city);
    const params = new URLSearchParams({
      latitude: String(loc.lat),
      longitude: String(loc.lon),
      current: "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
      daily: "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
      temperature_unit: "fahrenheit",
      wind_speed_unit: "mph",
      timezone: "auto",
      forecast_days: "1",
    });
    const { res, data } = await fetchJson(`https://api.open-meteo.com/v1/forecast?${params}`);
    if (!res.ok) throw Object.assign(new Error("weather"), { network: true });
    const current = data.current || {};
    const daily = data.daily || {};
    const code = Number(current.weather_code || (daily.weather_code || [0])[0] || 0);
    const summary = WEATHER_CODES[code] || "mixed conditions";
    const parts = [`In ${loc.label}, it's ${summary}`];
    if (current.temperature_2m != null) parts.push(`about ${Math.round(current.temperature_2m)} degrees`);
    if (
      current.apparent_temperature != null &&
      current.temperature_2m != null &&
      Math.abs(current.apparent_temperature - current.temperature_2m) >= 3
    ) {
      parts.push(`feels like ${Math.round(current.apparent_temperature)}`);
    }
    const hi = (daily.temperature_2m_max || [])[0];
    const lo = (daily.temperature_2m_min || [])[0];
    if (hi != null && lo != null) parts.push(`high ${Math.round(hi)}, low ${Math.round(lo)}`);
    const rain = (daily.precipitation_probability_max || [])[0];
    if (rain != null && rain >= 35) parts.push(`${Math.round(rain)} percent chance of rain`);
    if (current.relative_humidity_2m != null) parts.push(`humidity ${Math.round(current.relative_humidity_2m)} percent`);
    if (current.wind_speed_10m != null) parts.push(`wind ${Math.round(current.wind_speed_10m)} miles an hour`);
    return `${parts.join(", ")}.`;
  }

  async function getLocation() {
    const loc = await resolveLocation("");
    return `You are near ${loc.label}.`;
  }

  async function searchWeb(query, limit) {
    const q = encodeURIComponent(query || "");
    const { res, data } = await fetchJson(
      `https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=${q}&srlimit=${Math.min(8, limit || 5)}&format=json&origin=*`
    );
    if (!res.ok) throw Object.assign(new Error("search"), { network: true });
    const hits = (data.query && data.query.search) || [];
    if (!hits.length) return "No search results.";
    return hits
      .map((item) => `${item.title}: ${String(item.snippet || "").replace(/<[^>]+>/g, "")}`)
      .join("\n");
  }

  function redirectUri() {
    return new URL("callback.html", window.location.href).href.split("#")[0].split("?")[0];
  }

  function b64url(bytes) {
    let bin = "";
    bytes.forEach((b) => {
      bin += String.fromCharCode(b);
    });
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  }

  function randomVerifier() {
    const bytes = new Uint8Array(32);
    crypto.getRandomValues(bytes);
    return b64url(bytes);
  }

  async function challengeFrom(verifier) {
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
    return b64url(new Uint8Array(digest));
  }

  async function startSpotifyLogin() {
    const settings = loadRaw();
    const clientId = (settings.spotify_client_id || "").trim();
    if (!clientId) return { ok: false, error: "Add a Spotify client ID in settings first." };
    if (!online()) return { ok: false, error: NO_CONNECTION };
    const verifier = randomVerifier();
    const state = randomVerifier();
    const challenge = await challengeFrom(verifier);
    sessionStorage.setItem("opus-spotify-verifier", verifier);
    sessionStorage.setItem("opus-spotify-state", state);
    const params = new URLSearchParams({
      client_id: clientId,
      response_type: "code",
      redirect_uri: redirectUri(),
      code_challenge_method: "S256",
      code_challenge: challenge,
      state,
      scope: SPOTIFY_SCOPES,
    });
    return { ok: true, url: `https://accounts.spotify.com/authorize?${params}` };
  }

  async function finishSpotifyLogin() {
    const params = new URLSearchParams(window.location.search);
    const error = params.get("error") || "";
    if (error) return { ok: false, error: "Spotify login was cancelled." };
    const code = params.get("code") || "";
    const state = params.get("state") || "";
    const expected = sessionStorage.getItem("opus-spotify-state") || "";
    const verifier = sessionStorage.getItem("opus-spotify-verifier") || "";
    if (!code || !verifier || state !== expected) return { ok: false, error: "Spotify login failed." };
    const settings = loadRaw();
    try {
      const body = new URLSearchParams({
        client_id: settings.spotify_client_id,
        grant_type: "authorization_code",
        code,
        redirect_uri: redirectUri(),
        code_verifier: verifier,
      });
      const { res, data } = await fetchJson("https://accounts.spotify.com/api/token", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body,
      });
      if (!res.ok || !data.access_token) {
        return { ok: false, error: (data.error_description || data.error || "Spotify login failed.") };
      }
      await storeSpotifyTokens(data);
      sessionStorage.removeItem("opus-spotify-verifier");
      sessionStorage.removeItem("opus-spotify-state");
      return { ok: true };
    } catch (err) {
      return { ok: false, error: isNetworkError(err) || err.network ? NO_CONNECTION : "Spotify login failed." };
    }
  }

  async function storeSpotifyTokens(payload) {
    const settings = loadRaw();
    const expires = Date.now() + Math.max(30, Number(payload.expires_in || 3600) - 30) * 1000;
    const next = {
      ...settings,
      spotify_access_token: payload.access_token,
      spotify_refresh_token: payload.refresh_token || settings.spotify_refresh_token,
      spotify_token_expires_at: expires,
    };
    saveRaw(next);
    try {
      const me = await spotifyApi("GET", "/me");
      if (me && me.display_name) saveRaw({ ...loadRaw(), spotify_user_name: me.display_name });
    } catch {
      /* account name is optional */
    }
  }

  async function refreshSpotify() {
    const settings = loadRaw();
    if (!settings.spotify_refresh_token) return false;
    const body = new URLSearchParams({
      client_id: settings.spotify_client_id,
      grant_type: "refresh_token",
      refresh_token: settings.spotify_refresh_token,
    });
    const { res, data } = await fetchJson("https://accounts.spotify.com/api/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
    if (!res.ok || !data.access_token) return false;
    await storeSpotifyTokens(data);
    return true;
  }

  async function spotifyToken() {
    const settings = loadRaw();
    if (settings.spotify_access_token && Date.now() < Number(settings.spotify_token_expires_at || 0)) {
      return settings.spotify_access_token;
    }
    if (await refreshSpotify()) return loadRaw().spotify_access_token;
    return settings.spotify_access_token || "";
  }

  async function spotifyApi(method, path, { json, params } = {}) {
    const token = await spotifyToken();
    if (!token) throw new Error("Connect Spotify in settings first.");
    let url = path.startsWith("http") ? path : `https://api.spotify.com/v1${path}`;
    if (params) url += `?${new URLSearchParams(params)}`;
    let { res, data } = await fetchJson(url, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(json ? { "Content-Type": "application/json" } : {}),
      },
      body: json ? JSON.stringify(json) : undefined,
    });
    if (res.status === 401 && (await refreshSpotify())) {
      const retry = await fetchJson(url, {
        method,
        headers: {
          Authorization: `Bearer ${loadRaw().spotify_access_token}`,
          ...(json ? { "Content-Type": "application/json" } : {}),
        },
        body: json ? JSON.stringify(json) : undefined,
      });
      res = retry.res;
      data = retry.data;
    }
    if (res.status === 204) return {};
    if (res.status === 403) {
      const reason = String((data.error && data.error.reason) || data.error || "");
      if (/premium/i.test(reason)) throw new Error("Spotify Premium is required for playback control.");
      throw new Error("Spotify blocked that. Open Spotify on your phone, then try again.");
    }
    if (res.status === 404 && /PUT|POST/i.test(method)) {
      throw new Error("Open Spotify on this phone first so it can be a playback device.");
    }
    if (!res.ok) throw new Error("Spotify API request failed.");
    return data;
  }

  async function activeDeviceId() {
    const playback = await spotifyApi("GET", "/me/player").catch(() => ({}));
    if (playback && playback.device && playback.device.id) return playback.device.id;
    const payload = await spotifyApi("GET", "/me/player/devices");
    const devices = payload.devices || [];
    if (!devices.length) return "";
    const active = devices.find((item) => item.is_active) || devices[0];
    if (active.id && !active.is_active) {
      await spotifyApi("PUT", "/me/player", { json: { device_ids: [active.id], play: false } }).catch(() => {});
    }
    return active.id || "";
  }

  async function playSpotify(query) {
    const settings = loadRaw();
    if (!settings.spotify_refresh_token) return "Connect Spotify in settings first — I'll control it through Spotify's API.";
    const q = (query || "").trim();
    if (!q) return resumeSpotify();
    if (["liked songs", "liked", "likes", "my liked songs"].includes(q.toLowerCase())) {
      const saved = await spotifyApi("GET", "/me/tracks", { params: { limit: "50" } });
      const uris = (saved.items || []).map((item) => item.track && item.track.uri).filter(Boolean);
      if (!uris.length) return "I couldn't find liked songs on this Spotify account.";
      const deviceId = await activeDeviceId();
      if (!deviceId) return "Open Spotify on this phone first so it can be a playback device.";
      await spotifyApi("PUT", `/me/player/play?device_id=${encodeURIComponent(deviceId)}`, { json: { uris } });
      return "Playing your liked songs.";
    }
    const results = await spotifyApi("GET", "/search", {
      params: { q, type: "track,artist,album,playlist", limit: "8" },
    });
    const tracks = (results.tracks && results.tracks.items) || [];
    const artists = (results.artists && results.artists.items) || [];
    const albums = (results.albums && results.albums.items) || [];
    const playlists = (results.playlists && results.playlists.items) || [];
    const eq = (item) => (item.name || "").toLowerCase() === q.toLowerCase();
    let kind = "track";
    let item = tracks[0];
    if (artists.find(eq)) {
      kind = "artist";
      item = artists.find(eq);
    } else if (playlists.find(eq)) {
      kind = "playlist";
      item = playlists.find(eq);
    } else if (albums.find(eq)) {
      kind = "album";
      item = albums.find(eq);
    } else if (!item && artists[0]) {
      kind = "artist";
      item = artists[0];
    } else if (!item && albums[0]) {
      kind = "album";
      item = albums[0];
    } else if (!item && playlists[0]) {
      kind = "playlist";
      item = playlists[0];
    }
    if (!item) return `I couldn't find ${q} on Spotify.`;
    const deviceId = await activeDeviceId();
    if (!deviceId) return "Open Spotify on this phone first so it can be a playback device.";
    const body = kind === "track" ? { uris: [item.uri] } : { context_uri: item.uri };
    await spotifyApi("PUT", `/me/player/play?device_id=${encodeURIComponent(deviceId)}`, { json: body });
    if (kind === "artist") return `Playing ${item.name}.`;
    if (kind === "playlist") return `Playing the playlist ${item.name}.`;
    if (kind === "album") return `Playing the album ${item.name}.`;
    const artist = ((item.artists || [])[0] || {}).name || "";
    return artist ? `Playing ${item.name} by ${artist}.` : `Playing ${item.name}.`;
  }

  async function resumeSpotify() {
    const deviceId = await activeDeviceId();
    if (!deviceId) return "Open Spotify on this phone first so it can be a playback device.";
    await spotifyApi("PUT", `/me/player/play?device_id=${encodeURIComponent(deviceId)}`, { json: {} });
    return "Playing Spotify.";
  }

  async function controlSpotify(action) {
    const settings = loadRaw();
    if (!settings.spotify_refresh_token) return "Connect Spotify in settings first.";
    if (action === "pause") {
      await spotifyApi("PUT", "/me/player/pause");
      return "Paused Spotify.";
    }
    if (action === "skip") {
      await spotifyApi("POST", "/me/player/next");
      return "Skipped.";
    }
    if (action === "previous") {
      await spotifyApi("POST", "/me/player/previous");
      return "Previous track.";
    }
    const playback = await spotifyApi("GET", "/me/player/currently-playing");
    if (!playback || !playback.item) return "Nothing is playing on Spotify.";
    const name = playback.item.name || "a track";
    const artists = (playback.item.artists || []).map((a) => a.name).filter(Boolean).join(", ");
    return artists ? `${name} by ${artists}.` : name;
  }

  async function runTool(name, args) {
    if (name === "play_spotify") return playSpotify(args.query || "");
    if (name === "control_spotify") return controlSpotify(args.action || "pause");
    if (name === "search_web") return searchWeb(args.query || "", args.limit || 5);
    if (name === "get_weather") return getWeather(args.city || "");
    if (name === "get_location") return getLocation();
    return "That action is only on the PC version of Opus.";
  }

  async function askModel(userText) {
    const settings = loadRaw();
    if (!settings.openai_api_key) return "Add an API key in settings so I can answer.";
    const messages = [{ role: "system", content: `${SYSTEM}\n${todayLine()}` }];
    for (const item of history()) {
      messages.push({
        role: item.role === "opus" ? "assistant" : "user",
        content: item.content,
      });
    }
    messages.push({ role: "user", content: `User said: ${userText}` });
    let useTools = true;
    for (let round = 0; round < 4; round += 1) {
      const data = await llmChat(messages, useTools);
      const choice = (((data.choices || [])[0] || {}).message) || {};
      if (useTools && choice.tool_calls && choice.tool_calls.length) {
        messages.push({
          role: "assistant",
          content: choice.content || "",
          tool_calls: choice.tool_calls,
        });
        for (const call of choice.tool_calls) {
          let args = {};
          try {
            args = JSON.parse(call.function.arguments || "{}");
          } catch {
            args = {};
          }
          let result;
          try {
            result = await runTool(call.function.name, args);
          } catch (err) {
            if (err.network || isNetworkError(err)) result = NO_CONNECTION;
            else result = err.message || NO_CONNECTION;
          }
          messages.push({
            role: "tool",
            tool_call_id: call.id,
            content: String(result || ""),
          });
        }
        continue;
      }
      const text = (choice.content || "").trim();
      if (text) return text;
      if (useTools) {
        useTools = false;
        continue;
      }
      break;
    }
    return "Done.";
  }

  async function handleText(text) {
    const trimmed = String(text || "").trim();
    if (!trimmed) return "";
    const matched = matchLocal(trimmed);
    try {
      if (matched.kind === "local") {
        if (matched.reply) pushHistory("you", trimmed);
        if (matched.reply) pushHistory("opus", matched.reply);
        return matched.reply;
      }
      if (matched.kind === "net") {
        pushHistory("you", trimmed);
        let reply = "";
        if (matched.name === "weather") reply = await getWeather("");
        else if (matched.name === "location") reply = await getLocation();
        else if (matched.name === "spotify_play") reply = await playSpotify(matched.query || "");
        else if (matched.name === "spotify_control") reply = await controlSpotify(matched.action);
        pushHistory("opus", reply);
        return reply;
      }
      pushHistory("you", trimmed);
      const reply = await askModel(matched.text || trimmed);
      pushHistory("opus", reply);
      return reply;
    } catch (err) {
      const reply = err.network || isNetworkError(err) ? NO_CONNECTION : err.message || NO_CONNECTION;
      pushHistory("opus", reply);
      return reply;
    }
  }

  async function transcribe(blob, filename) {
    const settings = loadRaw();
    if (!settings.openai_api_key) return { text: "", error: "Add an API key in settings so I can hear you." };
    if (!online()) return { text: "", error: NO_CONNECTION };
    const body = new FormData();
    body.append("file", blob, filename || "utterance.webm");
    body.append("model", whisperModel(settings));
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 45000);
      const res = await fetch(`${apiBase(settings)}/audio/transcriptions`, {
        method: "POST",
        headers: { Authorization: `Bearer ${settings.openai_api_key}` },
        body,
        signal: ctrl.signal,
      });
      clearTimeout(timer);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        if (res.status >= 500 || !online()) return { text: "", error: NO_CONNECTION };
        return { text: "", error: (data.error && data.error.message) || "Couldn't hear that." };
      }
      return { text: (data.text || "").trim() };
    } catch (err) {
      return { text: "", error: isNetworkError(err) || err.name === "AbortError" ? NO_CONNECTION : "Couldn't hear that." };
    }
  }

  function disconnectSpotify() {
    const settings = loadRaw();
    saveRaw({
      ...settings,
      spotify_access_token: "",
      spotify_refresh_token: "",
      spotify_token_expires_at: 0,
      spotify_user_name: "",
    });
    return snapshot();
  }

  function homeUrl() {
    return new URL("./", window.location.href).href;
  }

  window.OpusPhone = {
    NO_CONNECTION,
    loadSettings: () => snapshot(),
    saveSettings(patch) {
      const current = loadRaw();
      const next = { ...current, ...patch };
      if (!patch.openai_api_key) next.openai_api_key = current.openai_api_key;
      saveRaw(next);
      return snapshot();
    },
    handleText,
    transcribe,
    startSpotifyLogin,
    finishSpotifyLogin,
    disconnectSpotify,
    redirectUri,
    homeUrl,
  };
})();
