async function check() {
  const status = document.getElementById("status");
  const detail = document.getElementById("detail");
  try {
    const response = await fetch("http://127.0.0.1:5840/api/port", { cache: "no-store" });
    if (!response.ok) throw new Error("Opus HTTP " + response.status);
    const data = await response.json();
    status.textContent = "Opus reachable on " + (data.port || 5840);
    detail.textContent = "If the toolbar badge is not “on”, click Reconnect or Reload the extension.";
  } catch (err) {
    status.textContent = "Opus not running";
    detail.textContent =
      "ERR_CONNECTION_REFUSED means nothing is on port 5840. Start Opus from the tray, wait a few seconds, then Reconnect.";
  }
}

document.getElementById("reconnect").addEventListener("click", async () => {
  try {
    await chrome.runtime.sendMessage({ type: "reconnect" });
  } catch (err) {
    // ignore
  }
  await check();
});

check();
