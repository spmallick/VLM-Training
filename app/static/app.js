const state = {
  sessionId: null,
  portalUrl: null,
  mediaStream: null,
  pollingHandle: null,
};

const cameraShell = document.querySelector(".camera-shell");
const cameraEl = document.getElementById("camera");
const canvasEl = document.getElementById("snapshot");
const previewEl = document.getElementById("preview");
const cameraLauncher = document.getElementById("camera-launcher");
const cameraLauncherTitle = document.getElementById("camera-launcher-title");
const cameraLauncherDetail = document.getElementById("camera-launcher-detail");
const cameraControls = document.getElementById("camera-controls");
const previewControls = document.getElementById("preview-controls");
const captureButton = document.getElementById("capture-button");
const stopCameraButton = document.getElementById("stop-camera");
const restartCameraButton = document.getElementById("restart-camera");
const uploadInput = document.getElementById("upload-input");
const companySelect = document.getElementById("company-select");
const runAgentButton = document.getElementById("run-agent-button");
const intakeStatus = document.getElementById("intake-status");
const commentaryStatus = document.getElementById("intake-commentary-status");
const commentaryLog = document.getElementById("intake-commentary-log");
const extractionJsonWrap = document.getElementById("extraction-json-wrap");
const extractionJsonPanel = document.getElementById("extraction-json");

function formatClock(value = new Date()) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function setStatus(message) {
  if (intakeStatus) intakeStatus.textContent = message;
}

function setCommentaryStatus(message) {
  if (commentaryStatus && message) commentaryStatus.textContent = message;
}

function eventKind(kind) {
  return ["info", "success", "warning", "error", "action"].includes(kind) ? kind : "info";
}

function appendCommentary(message, kind = "info", createdAt = new Date()) {
  if (!commentaryLog || !message) return;
  const item = document.createElement("li");
  item.className = "event-item intake-event-item";
  const badge = document.createElement("span");
  const normalizedKind = eventKind(kind);
  badge.className = `event-kind ${normalizedKind}`;
  badge.textContent = normalizedKind;
  const body = document.createElement("div");
  const text = document.createElement("p");
  text.textContent = message;
  const time = document.createElement("time");
  time.textContent = formatClock(createdAt);
  body.append(text, time);
  item.append(badge, body);
  commentaryLog.append(item);
  commentaryLog.scrollTop = commentaryLog.scrollHeight;
}

function renderSessionEvents(session) {
  if (!commentaryLog || !session?.events) return;
  commentaryLog.replaceChildren();
  session.events.forEach((event) => {
    appendCommentary(event.message, event.kind, event.created_at);
  });
}

function renderExtractionJson(session) {
  if (!extractionJsonPanel || !extractionJsonWrap) return;
  if (!session?.extraction) {
    extractionJsonWrap.hidden = true;
    extractionJsonPanel.textContent = "";
    return;
  }
  extractionJsonWrap.hidden = false;
  extractionJsonPanel.textContent = JSON.stringify(session.extraction, null, 2);
}

function renderSessionTrace(session) {
  renderSessionEvents(session);
  renderExtractionJson(session);
}

function selectedPortalUrl() {
  const option = companySelect?.selectedOptions?.[0];
  return option?.dataset.portalPath || "";
}

function ensureCompanySelected() {
  if (companySelect?.value) return true;
  setStatus("Choose the company first.");
  companySelect?.focus();
  return false;
}

function setCameraMode(mode) {
  if (cameraShell) cameraShell.dataset.cameraMode = mode;
}

function stopCamera() {
  if (state.mediaStream) {
    state.mediaStream.getTracks().forEach((track) => track.stop());
    state.mediaStream = null;
  }
  cameraEl.pause();
  cameraEl.srcObject = null;
  cameraEl.hidden = true;
  cameraControls.hidden = true;
  previewControls.hidden = true;
}

function setCameraControlState(active) {
  captureButton.disabled = !active;
  stopCameraButton.disabled = !active;
  cameraControls.hidden = !active;
}

function showCameraLauncher(title, detail) {
  stopCamera();
  cameraLauncherTitle.textContent = title;
  cameraLauncherDetail.textContent = detail;
  cameraLauncher.hidden = false;
  previewEl.hidden = true;
  previewEl.removeAttribute("src");
  setCameraControlState(false);
  previewControls.hidden = true;
  setCameraMode("idle");
}

function showCameraFeed() {
  cameraLauncher.hidden = true;
  previewEl.hidden = true;
  previewEl.removeAttribute("src");
  cameraEl.hidden = false;
  previewControls.hidden = true;
  setCameraControlState(true);
  setCameraMode("live");
}

function showPreview(url) {
  stopCamera();
  previewEl.src = url;
  previewEl.hidden = false;
  cameraLauncher.hidden = true;
  setCameraControlState(false);
  previewControls.hidden = false;
  setCameraMode("preview");
}

async function startCamera() {
  if (!ensureCompanySelected()) return;
  setStatus("Requesting camera access.");
  setCommentaryStatus("Camera");
  appendCommentary("Requesting camera access.", "action");
  stopCamera();
  try {
    state.mediaStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "environment" },
      audio: false,
    });
    cameraEl.srcObject = state.mediaStream;
    await cameraEl.play();
    showCameraFeed();
    setStatus("Camera is live. Capture the receipt photo when ready.");
    appendCommentary("Camera is live. Capture the receipt photo when ready.", "success");
  } catch (error) {
    stopCamera();
    showCameraLauncher("Start camera", "Allow camera access or use Upload photo instead.");
    setStatus(`Could not start the camera: ${error.message}`);
    appendCommentary(`Could not start the camera: ${error.message}`, "error");
  }
}

function updateRunButton(disabled) {
  runAgentButton.disabled = disabled;
}

function setBusy(button, busy, text) {
  if (!button) return;
  if (text) {
    button.dataset.originalText ||= button.textContent;
    button.textContent = busy ? text : button.dataset.originalText;
  }
  button.disabled = busy;
}

function canAutoRun(session) {
  const fields = session?.reviewed_fields || {};
  return Boolean(fields.vendor || fields.total) && !session?.extraction?.retake_required;
}

async function uploadReceipt(blob, filename = "receipt.jpg") {
  if (!ensureCompanySelected()) return;

  const formData = new FormData();
  formData.append("image", blob, filename);
  formData.append("company_slug", companySelect.value);

  setBusy(captureButton, true, "Analyzing...");
  updateRunButton(true);
  setStatus("Uploading the receipt and starting intake checks.");
  setCommentaryStatus("Extracting");
  appendCommentary("Uploading the receipt image.", "action");
  appendCommentary("Waiting for Qwen to extract the receipt JSON.", "info");
  try {
    const response = await fetch("/api/sessions/capture", {
      method: "POST",
      body: formData,
    });
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body.detail || "Upload failed.");
    }

    state.sessionId = body.session_id;
    state.portalUrl = selectedPortalUrl();
    renderSessionTrace(body);
    beginPolling();

    if (body.extraction?.retake_required) {
      setStatus(body.extraction.retake_reason || "The receipt needs to be captured again.");
      setCommentaryStatus("Retake needed");
      updateRunButton(true);
      return;
    }

    if (canAutoRun(body)) {
      setStatus("Receipt ready. Open the company portal and start the agent when ready.");
      setCommentaryStatus("Ready");
      appendCommentary("Receipt JSON is ready for review.", "success");
      updateRunButton(false);
    } else {
      setStatus("The receipt needs more manual review before the agent can run.");
      setCommentaryStatus("Review needed");
      appendCommentary("Receipt intake completed, but the extracted fields need review.", "warning");
      updateRunButton(true);
    }
  } catch (error) {
    setStatus(error.message);
    setCommentaryStatus("Error");
    appendCommentary(error.message, "error");
  } finally {
    setBusy(captureButton, false);
  }
}

async function captureFromCamera() {
  if (!cameraEl.videoWidth) {
    setStatus("The camera is not ready yet.");
    return;
  }
  setStatus("Capturing a photo from the live camera feed.");
  canvasEl.width = cameraEl.videoWidth;
  canvasEl.height = cameraEl.videoHeight;
  const context = canvasEl.getContext("2d");
  context.drawImage(cameraEl, 0, 0, canvasEl.width, canvasEl.height);

  canvasEl.toBlob(async (blob) => {
    if (!blob) return;
    showPreview(URL.createObjectURL(blob));
    await uploadReceipt(blob, "camera-capture.jpg");
  }, "image/jpeg", 0.92);
}

async function runAgent() {
  if (!state.sessionId) {
    setStatus("Capture or upload a receipt first.");
    return;
  }
  if (!ensureCompanySelected()) return;

  setBusy(runAgentButton, true, "Running...");
  setStatus("Starting the agent. A controlled browser window will open for the company portal.");
  setCommentaryStatus("Agent running");
  appendCommentary("Starting the browser agent.", "action");
  try {
    const response = await fetch(`/api/sessions/${state.sessionId}/run`, { method: "POST" });
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body.detail || "Could not start the agent.");
    }
    beginPolling();
  } catch (error) {
    setStatus(error.message);
  } finally {
    setBusy(runAgentButton, false);
  }
}

async function pollSession() {
  if (!state.sessionId) return;
  const response = await fetch(`/api/sessions/${state.sessionId}`);
  if (!response.ok) return;
  const session = await response.json();
  state.portalUrl = session.portal_state?.open_portal_url || state.portalUrl;
  renderSessionTrace(session);

  if (session.status === "completed") {
    setStatus("The agent finished the reimbursement run.");
    setCommentaryStatus("Completed");
  } else if (session.status === "awaiting_confirmation") {
    setStatus("The agent filled the form but paused for human confirmation.");
    setCommentaryStatus("Paused");
  } else if (session.status === "needs_review") {
    setStatus("The agent stopped because the receipt needs manual review.");
    setCommentaryStatus("Review needed");
  } else if (session.status === "agent_running") {
    setStatus(`Agent running: ${session.current_step.replaceAll("_", " ")}.`);
    setCommentaryStatus("Agent running");
  } else if (session.status === "ready_for_review") {
    setStatus("Receipt analyzed. The simplified portal can only continue if extraction was strong enough.");
    setCommentaryStatus("Ready");
  }

  if (
    ["completed", "awaiting_confirmation", "needs_review", "needs_recapture", "error"].includes(
      session.status
    )
  ) {
    clearInterval(state.pollingHandle);
    state.pollingHandle = null;
  }
}

function beginPolling() {
  if (state.pollingHandle) {
    clearInterval(state.pollingHandle);
  }
  state.pollingHandle = setInterval(pollSession, 1000);
}

captureButton.addEventListener("click", captureFromCamera);
stopCameraButton.addEventListener("click", () => {
  showCameraLauncher("Start camera", "Use the laptop camera to capture a receipt photo.");
  setStatus("Camera stopped.");
});
restartCameraButton.addEventListener("click", startCamera);
runAgentButton.addEventListener("click", (event) => {
  event.preventDefault();
  runAgent();
});

uploadInput.addEventListener("change", async (event) => {
  const [file] = event.target.files;
  if (!file) return;
  if (!ensureCompanySelected()) {
    uploadInput.value = "";
    return;
  }
  showPreview(URL.createObjectURL(file));
  setStatus(`Uploading ${file.name}.`);
  appendCommentary(`Selected ${file.name} for upload.`, "action");
  await uploadReceipt(file, file.name);
  uploadInput.value = "";
});

companySelect.addEventListener("change", () => {
  state.portalUrl = selectedPortalUrl();
  state.sessionId = null;
  if (state.pollingHandle) {
    clearInterval(state.pollingHandle);
    state.pollingHandle = null;
  }
  if (commentaryLog) commentaryLog.replaceChildren();
  renderExtractionJson(null);
  updateRunButton(true);
  setStatus(
    companySelect.value
      ? "Company selected. Capture or upload a receipt photo next."
      : "Choose the company you want reimbursement from."
  );
  setCommentaryStatus(companySelect.value ? "Ready for receipt" : "Waiting");
  appendCommentary(
    companySelect.value
      ? "Company selected. Waiting for a receipt image."
      : "Company selection cleared.",
    companySelect.value ? "action" : "info"
  );
});

cameraLauncher.addEventListener("click", startCamera);
window.addEventListener("beforeunload", stopCamera);
window.addEventListener("pagehide", stopCamera);
window.addEventListener("pageshow", () => {
  showCameraLauncher("Start camera", "Use the laptop camera to capture a receipt photo.");
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopCamera();
});

showCameraLauncher("Start camera", "Use the laptop camera to capture a receipt photo.");
updateRunButton(true);
setStatus("Waiting for you to choose a company and provide a receipt image.");
setCommentaryStatus("Waiting");
appendCommentary("Waiting for you to choose a company and provide a receipt image.");
