"use strict";

const $ = (id) => document.getElementById(id);
const image = ".jpeg,.jpg,.png,.webp";
const visual = image + ",.gif,.mp4,.mov,.webm,.mkv";
const audio = ".mp3,.wav,.m4a,.aac,.ogg,.flac";
const active = new Set(["queued", "running"]);
const defaultLimits = { max_files: 16, per_file_bytes: 80 * 2 ** 20, total_bytes: 90 * 2 ** 20 };

let connection;
let connecting = false;
let submitting = false;
let poll;
let selectedJob;
let selection = 0;
let previewUrl;
const recent = new Map();

function note(id, message, error = false) {
  $(id).textContent = message;
  $(id).classList.toggle("error", error);
}

function address(value) {
  const url = new URL(value.trim());
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if ((url.protocol !== "https:" && !(loopback && url.protocol === "http:")) ||
      url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("Enter the HTTPS Cloudflare base URL, without a path or credentials.");
  }
  return url.origin;
}

async function request(target, path, options = {}) {
  const response = await fetch(target.base + path, {
    ...options,
    headers: { Authorization: "Bearer " + target.token },
    credentials: "omit",
    cache: "no-store",
    redirect: "error",
    signal: AbortSignal.timeout(options.method === "POST" || path.endsWith("/video") ? 600000 : 20000),
  }).catch(() => {
    throw new Error("Could not reach the API. Check the URL and confirm Cells 5 and 6 are running with the updated source ZIP.");
  });
  if (response.ok) return response;
  const body = await response.json().catch(() => ({}));
  const detail = Array.isArray(body.detail)
    ? body.detail.map((item) => (item.loc || []).join(".") + " — " + item.msg).join(" · ")
    : body.detail;
  if (response.status === 401) throw new Error("Token rejected. Copy api_token from Cell 6.");
  throw new Error(typeof detail === "string" ? detail : "API request failed (" + response.status + ").");
}

function updateControls() {
  $("submit").disabled = !connection || submitting || connecting;
  $("connect").disabled = connecting || submitting;
  $("refresh").disabled = !connection;
  $("badge").textContent = connection ? "Connected" : "Disconnected";
  $("badge").classList.toggle("connected", Boolean(connection));
}

function clearPreview() {
  const video = $("preview");
  video.hidden = true;
  if (previewUrl) {
    video.removeAttribute("src");
    video.load();
    URL.revokeObjectURL(previewUrl);
    previewUrl = undefined;
  }
}

function clearConnection() {
  connection = undefined;
  clearTimeout(poll);
  selectedJob = undefined;
  selection++;
  recent.clear();
  clearPreview();
  $("job").hidden = true;
  $("job-picker").hidden = true;
  note("jobs-note", "Connect to view jobs from this runtime.");
  note("submit-note", "Connect to Colab to submit a video.");
  updateControls();
}

$("connection").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (connecting || submitting) return;
  clearConnection();
  connecting = true;
  updateControls();
  $("base").disabled = $("token").disabled = true;
  note("connection-note", "Checking the connection…");
  try {
    const target = { base: address($("base").value), token: $("token").value.trim() };
    if (!target.token) throw new Error("Enter the Colab API token.");
    const options = await (await request(target, "/options")).json();
    if (!options.upload_limits) throw new Error("This URL did not return Video API options.");
    target.limits = options.upload_limits;
    connection = target;
    $("connect").textContent = "Reconnect";
    note("connection-note", "Connected to Colab.");
    note("submit-note", "");
    await refreshJobs(target);
  } catch (error) {
    note("connection-note", error.message, true);
  } finally {
    connecting = false;
    $("base").disabled = $("token").disabled = false;
    updateControls();
  }
});

for (const id of ["base", "token"]) {
  $(id).addEventListener("input", () => {
    clearConnection();
    $("connect").textContent = "Connect";
    note("connection-note", "");
  });
}
if (location.protocol === "https:" || location.pathname.startsWith("/studio/")) {
  $("base").value = location.origin;
}
$("show-token").addEventListener("click", () => {
  const visible = $("token").type === "password";
  $("token").type = visible ? "text" : "password";
  $("show-token").textContent = visible ? "Hide" : "Show";
  $("show-token").setAttribute("aria-pressed", visible);
});

function fileTile(label, accept, field) {
  const tile = document.createElement("label");
  tile.className = "media-tile";
  tile.textContent = label;
  const input = document.createElement("input");
  input.type = "file";
  input.accept = accept;
  input.dataset.field = field;
  tile.append(input);
  return tile;
}

const closingTile = fileTile("Closing artwork", image, "closing_slide");
$("media").append(
  fileTile("Shared background", visual, "background"),
  fileTile("Company logo", image, "logo"),
  closingTile,
  fileTile("Background music", audio, "music"),
);
$("closing").addEventListener("change", () => {
  closingTile.querySelector("input").disabled = !$("closing").checked;
  closingTile.hidden = !$("closing").checked;
});

$("add-scene").addEventListener("click", () => {
  const scene = document.createElement("div");
  scene.className = "scene";
  const label = document.createElement("label");
  label.textContent = "Scene";
  const number = document.createElement("input");
  number.type = "number";
  number.min = "1";
  number.max = "40";
  number.required = true;
  const used = new Set([...$("scenes").querySelectorAll("input[type=number]")].map((input) => Number(input.value)));
  number.value = Array.from({ length: 40 }, (_, index) => index + 1).find((index) => !used.has(index)) || 1;
  label.append(number);
  const tile = fileTile("Scene image or video", visual, "");
  tile.querySelector("input").required = true;
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "secondary";
  remove.textContent = "Remove";
  remove.addEventListener("click", () => {
    scene.remove();
    $("add-scene").focus();
  });
  scene.append(label, tile, remove);
  $("scenes").append(scene);
  number.focus();
});

function configFromBrief() {
  const prompt = $("prompt").value.trim();
  if ($("mode").value === "brief" && !prompt) throw new Error("Enter a video brief.");
  const [width, height] = $("resolution").value.split("×").map(Number);
  const config = {
    prompt,
    duration_seconds: Number($("duration").value),
    output: { width, height, fps: Number($("fps").value) },
    voice: { enabled: $("narration").checked, tone: $("tone").value },
    brand: { company_name: $("company").value.trim() },
    closing_slide: { enabled: $("closing").checked },
  };
  const files = new Map();
  for (const input of $("media").querySelectorAll("input")) {
    if (input.files[0] && !input.disabled) files.set(input.dataset.field, input.files[0]);
  }
  if (files.has("background")) config.background = { file_field: "background" };
  if (files.has("logo")) config.brand.logo_file_field = "logo";
  if (files.has("music")) config.music = { enabled: true, file_field: "music" };
  if (files.has("closing_slide")) config.closing_slide.file_field = "closing_slide";
  config.scene_media = [...$("scenes").children].map((row) => {
    const scene = Number(row.querySelector("input[type=number]").value);
    const field = "scene_" + scene;
    if (files.has(field)) throw new Error("Attach only one file per scene.");
    files.set(field, row.querySelector("input[type=file]").files[0]);
    return { scene, file_field: field };
  });
  if ($("voice").value.trim()) config.voice.name = $("voice").value.trim();
  if ($("mode").value === "storyboard") {
    config.storyboard = readStoryboard();
    delete config.prompt;
    delete config.duration_seconds;
    delete config.closing_slide.enabled;
    config.voice.tone = "professional";
    config.voice.enabled = true;
  }
  return { config, files };
}

function readStoryboard() {
  let story;
  try {
    story = JSON.parse($("storyboard").value.trim().replace(/^```(?:json)?\s*|\s*```$/gi, ""));
  } catch {
    throw new Error("Paste the JSON storyboard returned by your writing assistant.");
  }
  story = story?.storyboard ?? story;
  if (!Array.isArray(story?.scenes) || !story.scenes.length) throw new Error("The storyboard needs a scenes array.");
  return story;
}

$("storyboard").addEventListener("input", () => {
  try {
    const story = readStoryboard();
    const long = story.scenes.filter(scene =>
      String(scene.narration || "").trim().split(/\s+/).length > 2 * scene.target_seconds);
    note("timing-note", long.length ? "Review narration length in scenes " + long.map(s => s.scene).join(", ") +
      ". If needed, ask your storyboard writer to shorten these scenes while preserving the main points." :
      "Colab measures the voiceover. The complete video can finish within three seconds of the requested duration.");
  } catch (error) {
    note("timing-note", error.message, true);
  }
});

$("mode").addEventListener("change", () => {
  const storyboard = $("mode").value === "storyboard";
  $("storyboard-panel").hidden = !storyboard;
  $("storyboard").disabled = !storyboard;
  $("prompt-label").hidden = $("prompt").hidden = $("prompt").disabled = storyboard;
  for (const id of ["duration", "tone", "narration", "closing"]) {
    $(id).closest("label").hidden = storyboard;
    $(id).disabled = storyboard;
  }
  closingTile.hidden = !storyboard && !$("closing").checked;
  closingTile.querySelector("input").disabled = closingTile.hidden;
});
$("mode").dispatchEvent(new Event("change"));
function formData(limits = defaultLimits) {
  const { config, files } = configFromBrief();
  const json = JSON.stringify(config);
  const size = new Blob([json]).size;
  if (size > 256 * 1024) throw new Error("The API accepts configuration JSON up to 256 KiB.");
  if (files.size > limits.max_files) throw new Error("Select at most " + limits.max_files + " files.");
  const data = new FormData();
  data.append("config", json);
  let total = size;
  for (const [field, file] of files) {
    if (!file) throw new Error("Choose a file for " + field + ".");
    if (file.size > limits.per_file_bytes) throw new Error(file.name + " exceeds the per-file upload limit.");
    total += file.size;
    data.append(field, file);
  }
  if (total >= limits.total_bytes) throw new Error("The combined uploads exceed the request size limit.");
  return data;
}

$("video-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!connection || submitting || connecting) return;
  const target = connection;
  submitting = true;
  $("base").disabled = $("token").disabled = true;
  updateControls();
  try {
    const body = formData(target.limits);
    note("submit-note", "Uploading to Colab…");
    const job = await (await request(target, "/jobs", { method: "POST", body })).json();
    note("submit-note", "Video queued.");
    recent.set(job.job_id, job);
    showJobs();
    selectJob(job.job_id);
  } catch (error) {
    note("submit-note", error.message, true);
  } finally {
    submitting = false;
    $("base").disabled = $("token").disabled = false;
    updateControls();
  }
});

function renderJob(job) {
  $("job").hidden = false;
  $("job-state").textContent = job.status[0].toUpperCase() + job.status.slice(1);
  $("job-stage").textContent = (job.stage || job.status).replaceAll("_", " ");
  $("progress").value = job.completed_stages || 0;
  $("stages").textContent = (job.completed_stages || 0) + " of 13 stages complete";
  note("job-error", job.error || "");
  for (const button of $("job-actions").querySelectorAll("button")) {
    if (["video", "preview"].includes(button.dataset.output)) button.hidden = job.status !== "completed";
  }
}

function showJobs() {
  $("job-picker").hidden = !recent.size;
  const options = [...recent.values()].sort((a, b) => (b.created || 0) - (a.created || 0)).map((job) => {
    const date = job.created ? new Date(job.created * 1000).toLocaleString() + " · " : "";
    return new Option(date + job.job_id.slice(0, 8) + " · " + job.status, job.job_id);
  });
  $("job-list").replaceChildren(...options);
  if (selectedJob) $("job-list").value = selectedJob;
}

async function refreshJobs(target) {
  try {
    const jobs = await (await request(target, "/jobs")).json();
    if (connection !== target) return;
    for (const job of jobs) recent.set(job.job_id, job);
    showJobs();
    note("jobs-note", jobs.length ? "" : "No video jobs yet.");
    if (!selectedJob && jobs.length) selectJob(jobs[0].job_id);
    else if (selectedJob) checkJob(target, selectedJob, ++selection);
  } catch (error) {
    if (connection === target) note("jobs-note", error.message, true);
  }
}

async function checkJob(target, id, version) {
  clearTimeout(poll);
  try {
    const job = await (await request(target, "/jobs/" + encodeURIComponent(id))).json();
    if (version !== selection) return;
    recent.set(id, job);
    showJobs();
    renderJob(job);
    note("jobs-note", "");
    if (active.has(job.status)) poll = setTimeout(() => checkJob(target, id, version), 3500);
  } catch (error) {
    if (version !== selection) return;
    note("jobs-note", error.message + " Use Refresh to resume tracking.", true);
  }
}

function selectJob(id) {
  selectedJob = id;
  $("job-list").value = id;
  clearPreview();
  $("output").hidden = true;
  note("output-note", "");
  renderJob(recent.get(id));
  checkJob(connection, id, ++selection);
}

$("refresh").addEventListener("click", () => refreshJobs(connection));
$("job-list").addEventListener("change", () => selectJob($("job-list").value));

$("job-actions").addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button || !connection || !selectedJob) return;
  const target = connection;
  const id = selectedJob;
  const output = button.dataset.output;
  const path = output === "preview" ? "video" : output;
  button.disabled = true;
  note("output-note", "Loading " + output + "…");
  try {
    const response = await request(target, "/jobs/" + encodeURIComponent(id) + "/" + path);
    const result = path === "video" ? await response.blob()
      : path === "report" ? JSON.stringify(await response.json(), null, 2) : await response.text();
    if (connection !== target || selectedJob !== id) return;
    if (output === "preview") {
      clearPreview();
      previewUrl = URL.createObjectURL(result);
      $("preview").src = previewUrl;
      $("preview").hidden = false;
    } else if (output === "video") {
      const url = URL.createObjectURL(result);
      const link = document.createElement("a");
      link.href = url;
      link.download = "video_" + id + ".mp4";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    } else {
      $("output").textContent = result;
      $("output").hidden = false;
    }
    note("output-note", "");
  } catch (error) {
    if (connection === target && selectedJob === id) note("output-note", error.message, true);
  } finally {
    button.disabled = false;
  }
});
