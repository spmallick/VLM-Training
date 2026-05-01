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

function setStatus(message) {
  if (intakeStatus) intakeStatus.textContent = message;
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
  } catch (error) {
    stopCamera();
    showCameraLauncher("Start camera", "Allow camera access or use Upload photo instead.");
    setStatus(`Could not start the camera: ${error.message}`);
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
    beginPolling();

    if (body.extraction?.retake_required) {
      setStatus(body.extraction.retake_reason || "The receipt needs to be captured again.");
      updateRunButton(true);
      return;
    }

    if (canAutoRun(body)) {
      setStatus("Receipt ready. Open the company portal and start the agent when ready.");
      updateRunButton(false);
    } else {
      setStatus("The receipt needs more manual review before the agent can run.");
      updateRunButton(true);
    }
  } catch (error) {
    setStatus(error.message);
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

  if (session.status === "completed") {
    setStatus("The agent finished the reimbursement run.");
  } else if (session.status === "awaiting_confirmation") {
    setStatus("The agent filled the form but paused for human confirmation.");
  } else if (session.status === "needs_review") {
    setStatus("The agent stopped because the receipt needs manual review.");
  } else if (session.status === "agent_running") {
    setStatus(`Agent running: ${session.current_step.replaceAll("_", " ")}.`);
  } else if (session.status === "ready_for_review") {
    setStatus("Receipt analyzed. The simplified portal can only continue if extraction was strong enough.");
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
  await uploadReceipt(file, file.name);
  uploadInput.value = "";
});

companySelect.addEventListener("change", () => {
  state.portalUrl = selectedPortalUrl();
  state.sessionId = null;
  updateRunButton(true);
  setStatus(
    companySelect.value
      ? "Company selected. Capture or upload a receipt photo next."
      : "Choose the company you want reimbursement from."
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
