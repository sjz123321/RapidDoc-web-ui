const form = document.querySelector("#convert-form");
const fileInput = document.querySelector("#file-input");
const fileName = document.querySelector("#file-name");
const apiToggle = document.querySelector("#third-party-enabled");
const apiFields = document.querySelector("#api-fields");
const apiKeyFileInput = document.querySelector("#third-party-api-key-file");
const apiKeyChoice = document.querySelector("#third-party-api-key-choice");
const apiKeySource = document.querySelector("#api-key-source");
const convertButton = document.querySelector("#convert-button");
const statusLine = document.querySelector("#status-line");
const resultTitle = document.querySelector("#result-title");
const downloadLink = document.querySelector("#download-link");
const markdownPreview = document.querySelector("#markdown-preview");
const htmlPreview = document.querySelector("#html-preview");
const filesPreview = document.querySelector("#files-preview");
const clearButton = document.querySelector("#clear-button");
const progressMessage = document.querySelector("#progress-message");
const progressPercent = document.querySelector("#progress-percent");
const progressBar = document.querySelector("#progress-bar");
let activePoll = null;
let apiKeysText = "";

function boolValue(id) {
  return document.querySelector(id).checked ? "true" : "false";
}

function inputValue(id) {
  return document.querySelector(id).value.trim();
}

function setBusy(isBusy) {
  convertButton.disabled = isBusy;
  convertButton.querySelector("span").textContent = isBusy ? "转换中" : "开始转换";
  statusLine.textContent = isBusy ? "RapidDoc 正在解析" : "本地 CPU 模式";
}

function setProgress(percent, message) {
  const normalized = Math.max(0, Math.min(100, Number(percent) || 0));
  progressBar.style.width = `${normalized}%`;
  progressPercent.textContent = `${Math.round(normalized)}%`;
  progressMessage.textContent = message || "处理中";
}

function maskApiKey(value) {
  const key = String(value || "").trim();
  if (!key) {
    return "";
  }
  if (key.length <= 8) {
    return "*".repeat(key.length);
  }
  return `${key.slice(0, 4)}...${key.slice(-4)}`;
}

function parseApiKeysText(text) {
  const keys = [];
  String(text || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"))
    .forEach((key) => {
      if (!keys.includes(key)) {
        keys.push(key);
      }
    });
  return keys;
}

function renderApiKeyChoices(keys, sourceText) {
  apiKeyChoice.innerHTML = '<option value="">手动输入 / 不使用文件 Key</option>';
  keys.forEach((key, index) => {
    const option = document.createElement("option");
    option.value = key;
    option.textContent = `${index + 1}. ${maskApiKey(key)}`;
    apiKeyChoice.appendChild(option);
  });
  apiKeySource.textContent = sourceText;
}

async function loadProjectApiKeys() {
  try {
    const response = await fetch("/api/api-keys");
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "读取 apikey.txt 失败");
    }
    const keys = payload.keys || [];
    apiKeysText = keys.map((item) => item.value).join("\n");
    renderApiKeyChoices(
      keys.map((item) => item.value),
      payload.found ? `已从 ${payload.path} 加载 ${payload.count} 个 key` : "未找到 apikey.txt，可点击右侧按钮选择文件"
    );
  } catch (error) {
    apiKeysText = "";
    renderApiKeyChoices([], `读取 apikey.txt 失败：${error.message}`);
  }
}

function setActiveTab(name) {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.tab === name);
  });
  document.querySelectorAll(".preview").forEach((preview) => {
    preview.classList.remove("active");
  });
  document.querySelector(`#${name}-preview`).classList.add("active");
}

function renderFiles(data) {
  const files = [
    ["Markdown", data.markdown_path],
    ["HTML", data.html_path],
    ["Middle JSON", data.middle_json_path],
    ["Content JSON", data.content_json_path],
    ["Word", data.docx_path],
    ["Log", data.log_path],
    ["ZIP", data.zip_path],
  ].filter(([, path]) => path);

  const warnings = (data.warnings || [])
    .map((warning) => `<div class="warning">${escapeHtml(warning)}</div>`)
    .join("");

  const rows = files
    .map(([label, path]) => {
      const name = path.split(/[\\/]/).pop();
      const href = label === "ZIP"
        ? `/api/download/${data.job_id}`
        : label === "Log"
          ? `/api/log/${data.job_id}`
          : `/api/file/${data.job_id}/${encodeURIComponent(name)}`;
      return `
        <div class="file-row">
          <div>
            <strong>${escapeHtml(label)}</strong>
            <div>${escapeHtml(name)}</div>
          </div>
          <a href="${href}" download>下载</a>
        </div>`;
    })
    .join("");

  filesPreview.innerHTML = `${warnings}<div class="file-list">${rows}</div>`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

fileInput.addEventListener("change", () => {
  fileName.textContent = fileInput.files[0]?.name || "选择文件";
});

apiKeyFileInput.addEventListener("change", async () => {
  const file = apiKeyFileInput.files[0];
  if (!file) {
    return;
  }
  apiKeysText = await file.text();
  const keys = parseApiKeysText(apiKeysText);
  renderApiKeyChoices(keys, `已从 ${file.name} 加载 ${keys.length} 个 key`);
});

apiToggle.addEventListener("change", () => {
  apiFields.classList.toggle("enabled", apiToggle.checked);
  apiFields.setAttribute("aria-disabled", apiToggle.checked ? "false" : "true");
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => setActiveTab(tab.dataset.tab));
});

clearButton.addEventListener("click", () => {
  resultTitle.textContent = "等待转换";
  statusLine.textContent = "本地 CPU 模式";
  markdownPreview.textContent = "";
  htmlPreview.removeAttribute("srcdoc");
  filesPreview.innerHTML = "";
  downloadLink.href = "#";
  downloadLink.classList.add("disabled");
  setProgress(0, "尚未开始");
  if (activePoll) {
    clearInterval(activePoll);
    activePoll = null;
  }
  setActiveTab("markdown");
});

function renderResult(payload) {
  resultTitle.textContent = payload.file_name;
  statusLine.textContent = payload.third_party_api_enabled ? "已启用第三方 API" : "本地 CPU 模式";
  markdownPreview.textContent = payload.markdown_preview || "";
  htmlPreview.srcdoc = payload.html_preview || "<body></body>";
  renderFiles(payload);
  downloadLink.href = `/api/download/${payload.job_id}`;
  downloadLink.classList.remove("disabled");
  setActiveTab(payload.html_preview ? "html" : "markdown");
}

async function pollJob(jobId) {
  const response = await fetch(`/api/jobs/${jobId}`);
  const job = await response.json();
  if (!response.ok) {
    throw new Error(job.detail || "无法读取任务进度");
  }

  setProgress(job.percent, job.message);
  statusLine.textContent = job.message || "处理中";

  if (job.status === "completed") {
    clearInterval(activePoll);
    activePoll = null;
    setBusy(false);
    setProgress(100, "转换完成");
    renderResult(job.result);
  } else if (job.status === "failed") {
    clearInterval(activePoll);
    activePoll = null;
    setBusy(false);
    resultTitle.textContent = "转换失败";
    statusLine.textContent = job.error || job.message || "转换失败";
    filesPreview.innerHTML = `<div class="warning">${escapeHtml(job.error || job.message || "转换失败")}</div>`;
    setActiveTab("files");
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  const formData = new FormData();
  const selectedFile = fileInput.files[0];
  const useSample = document.querySelector("#use-sample").checked;

  if (!useSample && !selectedFile) {
    statusLine.textContent = "请选择文件";
    return;
  }

  if (selectedFile && !useSample) {
    formData.append("file", selectedFile);
  }

  formData.append("use_sample", boolValue("#use-sample"));
  formData.append("parse_method", inputValue("#parse-method"));
  formData.append("lang", inputValue("#lang"));
  formData.append("device_mode", inputValue("#device-mode"));
  formData.append("layout_cpu_fallback", boolValue("#layout-cpu-fallback"));
  formData.append("heterogeneous_parallel", boolValue("#heterogeneous-parallel"));
  formData.append("debug_log_enabled", boolValue("#debug-log-enabled"));
  formData.append("start_page", inputValue("#start-page") || "0");
  if (inputValue("#end-page")) {
    formData.append("end_page", inputValue("#end-page"));
  }
  formData.append("formula_enable", boolValue("#formula-enable"));
  formData.append("table_enable", boolValue("#table-enable"));
  formData.append("seal_enable", boolValue("#seal-enable"));
  formData.append("export_markdown", boolValue("#export-md"));
  formData.append("export_middle_json", boolValue("#export-middle"));
  formData.append("export_content_json", boolValue("#export-content"));
  formData.append("export_html", boolValue("#export-html"));
  formData.append("export_docx", boolValue("#export-docx"));
  formData.append("layout_cleanup", boolValue("#layout-cleanup"));
  formData.append("third_party_enabled", boolValue("#third-party-enabled"));
  formData.append("third_party_base_url", inputValue("#third-party-base-url"));
  formData.append("third_party_api_key", inputValue("#third-party-api-key"));
  formData.append("third_party_api_key_choice", inputValue("#third-party-api-key-choice"));
  formData.append("third_party_api_keys_text", apiKeysText);
  if (apiKeyFileInput.files[0]) {
    formData.append("third_party_api_key_file", apiKeyFileInput.files[0]);
  }
  formData.append("third_party_model", inputValue("#third-party-model"));
  formData.append("third_party_use_ocr", boolValue("#third-party-use-ocr"));
  formData.append("third_party_use_formula", boolValue("#third-party-use-formula"));
  formData.append("third_party_use_table", boolValue("#third-party-use-table"));

  setBusy(true);
  resultTitle.textContent = "转换中";
  setProgress(0, "正在提交任务");
  markdownPreview.textContent = "";
  htmlPreview.removeAttribute("srcdoc");
  filesPreview.innerHTML = "";
  downloadLink.classList.add("disabled");

  try {
    const response = await fetch("/api/convert", {
      method: "POST",
      body: formData,
    });

    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || "转换失败");
    }

    resultTitle.textContent = "转换中";
    setProgress(payload.percent || 0, payload.message || "任务已创建");
    if (activePoll) {
      clearInterval(activePoll);
    }
    activePoll = setInterval(() => {
      pollJob(payload.job_id).catch((error) => {
        clearInterval(activePoll);
        activePoll = null;
        setBusy(false);
        resultTitle.textContent = "转换失败";
        statusLine.textContent = error.message;
        filesPreview.innerHTML = `<div class="warning">${escapeHtml(error.message)}</div>`;
        setActiveTab("files");
      });
    }, 1000);
    await pollJob(payload.job_id);
  } catch (error) {
    resultTitle.textContent = "转换失败";
    statusLine.textContent = error.message;
    filesPreview.innerHTML = `<div class="warning">${escapeHtml(error.message)}</div>`;
    setActiveTab("files");
    setBusy(false);
  } finally {
    if (!activePoll) {
      setBusy(false);
    }
  }
});

if (window.lucide) {
  window.lucide.createIcons();
}

loadProjectApiKeys();
