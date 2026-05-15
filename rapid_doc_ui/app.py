from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from rapid_doc_ui.postprocess import polish_markdown_layout


ROOT_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
OUTPUT_ROOT = ROOT_DIR / "ui_output"
LOG_ROOT = ROOT_DIR / "ui_logs"
DEFAULT_TEST_PDF = ROOT_DIR.parent / "test.pdf"
API_KEY_FILE_CANDIDATES = (ROOT_DIR.parent / "apikey.txt", ROOT_DIR / "apikey.txt")
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
SILICONFLOW_DEFAULT_MODEL = "Qwen/Qwen3-VL-32B-Instruct"


class ConvertResponse(BaseModel):
    job_id: str
    file_name: str
    output_dir: str
    zip_path: str
    markdown_path: str | None = None
    html_path: str | None = None
    middle_json_path: str | None = None
    content_json_path: str | None = None
    docx_path: str | None = None
    log_path: str | None = None
    markdown_preview: str = ""
    html_preview: str = ""
    warnings: list[str] = []
    third_party_api_enabled: bool = False


app = FastAPI(title="RapidDoc OCR UI", version="0.1.0")
EXECUTOR = ThreadPoolExecutor(max_workers=1)
JOBS: dict[str, dict] = {}
JOBS_LOCK = Lock()
DEVICE_LOCK = Lock()
CURRENT_DEVICE_MODE: str | None = None
VALID_DEVICE_MODES = {"cpu", "cuda", "cuda:0", "cuda:1", "npu", "npu:0", "directml"}
DEVICE_MODE_ALIASES = {"dml": "directml", "amd": "directml"}


def _normalize_device_mode(device_mode: str) -> str:
    normalized = (device_mode or "cpu").strip().lower()
    return DEVICE_MODE_ALIASES.get(normalized, normalized)


def _rapid_doc_runtime_device_mode(device_mode: str) -> str:
    # DirectML is applied per ONNXRuntime model; keep RapidDoc's global device on
    # CPU so OCR keeps its stable CPU/OpenVINO path instead of falling through.
    return "cpu" if device_mode == "directml" else device_mode


def _set_job(job_id: str, **updates) -> None:
    updates["updated_at"] = time.time()
    with JOBS_LOCK:
        current = JOBS.setdefault(job_id, {})
        current.update(updates)


def _get_job(job_id: str) -> dict | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job is not None else None


def _apply_device_mode(device_mode: str) -> None:
    global CURRENT_DEVICE_MODE
    normalized = _normalize_device_mode(device_mode)
    if normalized not in VALID_DEVICE_MODES:
        raise ValueError("device_mode must be one of: cpu, cuda, cuda:0, cuda:1, npu, npu:0, directml.")

    runtime_device_mode = _rapid_doc_runtime_device_mode(normalized)
    with DEVICE_LOCK:
        if CURRENT_DEVICE_MODE == normalized and os.environ.get("MINERU_DEVICE_MODE") == runtime_device_mode:
            return

        os.environ["MINERU_DEVICE_MODE"] = runtime_device_mode
        CURRENT_DEVICE_MODE = normalized

        # RapidDoc caches initialized models globally; clear them when device changes.
        try:
            if "rapid_doc.backend.pipeline.pipeline_analyze" in sys.modules:
                from rapid_doc.backend.pipeline.pipeline_analyze import ModelSingleton

                ModelSingleton._models.clear()
            if "rapid_doc.backend.pipeline.model_init" in sys.modules:
                from rapid_doc.backend.pipeline.model_init import AtomModelSingleton

                AtomModelSingleton._models.clear()
            if "rapid_doc.utils.download_file" in sys.modules:
                import rapid_doc.utils.download_file as download_file

                download_file.device_mode = runtime_device_mode
        except Exception:
            pass


def _safe_stem(name: str) -> str:
    keep = []
    for char in Path(name).stem:
        if char.isalnum() or char in ("-", "_"):
            keep.append(char)
        elif char in (" ", ".", "(", ")"):
            keep.append("_")
    stem = "".join(keep).strip("_")
    return stem or "document"


def _parse_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.lower() in {"1", "true", "yes", "on"}


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _clean_text_for_utf8(text: str) -> str:
    return "".join("\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char for char in str(text))


def _clean_data_for_utf8(value):
    if isinstance(value, str):
        return _clean_text_for_utf8(value)
    if isinstance(value, list):
        return [_clean_data_for_utf8(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_clean_data_for_utf8(item) for item in value)
    if isinstance(value, dict):
        return {
            _clean_text_for_utf8(key) if isinstance(key, str) else key: _clean_data_for_utf8(item)
            for key, item in value.items()
        }
    return value


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_clean_text_for_utf8(text), encoding="utf-8")


def _safe_log_params(params: dict) -> dict:
    redacted = dict(params)
    if redacted.get("third_party_api_key"):
        redacted["third_party_api_key"] = "<redacted>"
    if redacted.get("third_party_api_keys"):
        redacted["third_party_api_keys"] = f"<{len(redacted['third_party_api_keys'])} keys>"
    return redacted


def _log_file_path(job_id: str) -> Path:
    return LOG_ROOT / f"{job_id}.log"


def _append_log_line(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(f"[{timestamp}] {message}\n")


class _TeeTextIO:
    def __init__(self, original, log_handle) -> None:
        self.original = original
        self.log_handle = log_handle

    def write(self, text: str) -> int:
        self.original.write(text)
        self.log_handle.write(text)
        return len(text)

    def flush(self) -> None:
        self.original.flush()
        self.log_handle.flush()

    def isatty(self) -> bool:
        return False


class _JobLogCapture:
    def __init__(self, enabled: bool, log_path: Path) -> None:
        self.enabled = enabled
        self.log_path = log_path
        self._handle = None
        self._stdout_cm = None
        self._stderr_cm = None
        self._loguru_sink_id = None
        self._python_handler = None

    def __enter__(self):
        if not self.enabled:
            return self

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.log_path.open("a", encoding="utf-8", errors="replace")
        self._stdout_cm = redirect_stdout(_TeeTextIO(sys.stdout, self._handle))
        self._stderr_cm = redirect_stderr(_TeeTextIO(sys.stderr, self._handle))
        self._stdout_cm.__enter__()
        self._stderr_cm.__enter__()

        try:
            from loguru import logger

            self._loguru_sink_id = logger.add(
                self.log_path,
                encoding="utf-8",
                enqueue=True,
                backtrace=True,
                diagnose=False,
                level="DEBUG",
            )
        except Exception:
            self._loguru_sink_id = None

        self._python_handler = logging.FileHandler(self.log_path, encoding="utf-8")
        self._python_handler.setLevel(logging.DEBUG)
        self._python_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        logging.getLogger().addHandler(self._python_handler)
        logging.getLogger().setLevel(logging.DEBUG)

        _append_log_line(self.log_path, "Debug log capture started")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self.enabled:
            return None

        if exc is not None:
            _append_log_line(self.log_path, f"Exception: {exc!r}")
        _append_log_line(self.log_path, "Debug log capture finished")

        if self._python_handler is not None:
            logging.getLogger().removeHandler(self._python_handler)
            self._python_handler.close()
        if self._loguru_sink_id is not None:
            try:
                from loguru import logger

                logger.remove(self._loguru_sink_id)
            except Exception:
                pass
        if self._stderr_cm is not None:
            self._stderr_cm.__exit__(exc_type, exc, tb)
        if self._stdout_cm is not None:
            self._stdout_cm.__exit__(exc_type, exc, tb)
        if self._handle is not None:
            self._handle.close()
        return None


def _read_api_keys_text(text: str) -> list[str]:
    keys: list[str] = []
    for line in text.splitlines():
        key = line.strip()
        if not key or key.startswith("#"):
            continue
        if key not in keys:
            keys.append(key)
    return keys


def _load_api_keys_from_project() -> tuple[list[str], Path | None]:
    for path in API_KEY_FILE_CANDIDATES:
        if path.exists() and path.is_file():
            return _read_api_keys_text(path.read_text(encoding="utf-8")), path
    return [], None


def _mask_api_key(api_key: str) -> str:
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:4]}...{api_key[-4:]}"


def _copy_images(output_dir: Path, images: dict[str, bytes], image_dir_name: str = "images") -> None:
    for rel_path, data in images.items():
        normalized_path = rel_path.replace("\\", "/")
        _write_bytes(output_dir / normalized_path, data)
        if "/" not in normalized_path:
            _write_bytes(output_dir / image_dir_name / normalized_path, data)


def _zip_directory(source_dir: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in source_dir.rglob("*"):
            if file_path.is_file():
                archive.write(file_path, file_path.relative_to(source_dir))


def _build_docx(markdown: str, output_path: Path, image_base_path: Path) -> None:
    from rapid_doc.utils.markdown_to_word import markdown_to_docx

    markdown_to_docx(
        markdown,
        output_path=str(output_path),
        image_base_path=str(image_base_path),
    )


def _settings_from_form(
    third_party_enabled: bool,
    third_party_base_url: str,
    third_party_api_key: str,
    third_party_api_keys: list[str],
    third_party_model: str,
    third_party_use_ocr: bool,
    third_party_use_formula: bool,
    third_party_use_table: bool,
    progress_callback=None,
    key_event_callback=None,
):
    from rapid_doc_ui.third_party import ThirdPartyAPISettings

    base_url = third_party_base_url.strip()
    model = third_party_model.strip()
    if third_party_enabled and not base_url:
        base_url = SILICONFLOW_BASE_URL
    if third_party_enabled and not model:
        model = SILICONFLOW_DEFAULT_MODEL

    return ThirdPartyAPISettings(
        enabled=third_party_enabled,
        base_url=base_url,
        api_key=third_party_api_key.strip(),
        api_keys=third_party_api_keys,
        model=model,
        use_for_ocr=third_party_use_ocr,
        use_for_formula=third_party_use_formula,
        use_for_table=third_party_use_table,
        temperature=0.1,
        progress_callback=progress_callback,
        key_event_callback=key_event_callback,
    )


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "rapid_doc_root": str(ROOT_DIR),
        "default_test_pdf_exists": DEFAULT_TEST_PDF.exists(),
    }


@app.get("/api/api-keys")
def api_keys() -> dict:
    keys, path = _load_api_keys_from_project()
    return {
        "found": path is not None,
        "path": str(path) if path else None,
        "count": len(keys),
        "keys": [
            {"index": index, "label": f"{index + 1}. {_mask_api_key(key)}", "value": key}
            for index, key in enumerate(keys)
        ],
    }


@app.get("/api/device-diagnostics")
def device_diagnostics() -> dict:
    diagnostics: dict = {
        "mineru_device_mode": os.getenv("MINERU_DEVICE_MODE", "cpu"),
        "onnxruntime": None,
        "torch": None,
        "nvidia_smi": None,
        "warnings": [],
    }

    try:
        import onnxruntime as ort

        providers = ort.get_available_providers()
        diagnostics["onnxruntime"] = {
            "version": ort.__version__,
            "device": ort.get_device(),
            "providers": providers,
            "has_cuda_provider": "CUDAExecutionProvider" in providers,
            "has_directml_provider": "DmlExecutionProvider" in providers,
        }
        if "CUDAExecutionProvider" not in providers:
            diagnostics["warnings"].append("onnxruntime-gpu is not available in this Python environment.")
        if "DmlExecutionProvider" not in providers:
            diagnostics["warnings"].append("onnxruntime-directml is not available in this Python environment.")
    except Exception as exc:
        diagnostics["onnxruntime"] = {"error": str(exc)}
        diagnostics["warnings"].append("onnxruntime could not be imported.")

    try:
        import torch

        cuda_available = torch.cuda.is_available()
        diagnostics["torch"] = {
            "version": torch.__version__,
            "cuda_version": getattr(torch.version, "cuda", None),
            "cuda_available": cuda_available,
            "device_count": torch.cuda.device_count() if cuda_available else 0,
            "device_name": torch.cuda.get_device_name(0) if cuda_available else None,
        }
        if not cuda_available:
            diagnostics["warnings"].append("PyTorch CUDA is not available; OCR/formula GPU engines may fail or fall back.")
    except Exception as exc:
        diagnostics["torch"] = {"error": str(exc)}
        diagnostics["warnings"].append("torch could not be imported; RapidDoc GPU mode needs torch with CUDA.")

    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            output = subprocess.check_output(
                [nvidia_smi, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
                text=True,
                stderr=subprocess.STDOUT,
                timeout=10,
            )
            diagnostics["nvidia_smi"] = {"available": True, "gpus": [line.strip() for line in output.splitlines() if line.strip()]}
        except Exception as exc:
            diagnostics["nvidia_smi"] = {"available": False, "error": str(exc)}
            diagnostics["warnings"].append("nvidia-smi exists but could not be queried.")
    else:
        diagnostics["nvidia_smi"] = {"available": False}
        diagnostics["warnings"].append("nvidia-smi was not found in PATH.")

    return diagnostics


def _run_convert_job(job_id: str, input_name: str, input_bytes: bytes, params: dict) -> None:
    warnings: list[str] = []
    output_dir = OUTPUT_ROOT / job_id
    debug_log_enabled = bool(params.get("debug_log_enabled"))
    log_path = _log_file_path(job_id)

    def progress(percent: int, message: str) -> None:
        _set_job(job_id, status="running", percent=percent, message=message)
        if debug_log_enabled:
            _append_log_line(log_path, f"progress {percent}%: {message}")

    def api_progress(task: str, index: int | None, total: int | None) -> None:
        label_map = {"ocr": "OCR", "formula": "公式", "table": "表格"}
        label = label_map.get(task, task)
        if index and total:
            progress(45, f"第三方 API 正在处理{label}图块 {index}/{total}")
        else:
            progress(45, f"第三方 API 正在处理{label}图块")

    def api_key_event(message: str) -> None:
        progress(45, message)

    try:
        with _JobLogCapture(debug_log_enabled, log_path):
            _run_convert_job_inner(
                job_id,
                input_name,
                input_bytes,
                params,
                warnings,
                output_dir,
                progress,
                api_progress,
                api_key_event,
                log_path if debug_log_enabled else None,
            )
    except Exception as exc:
        if debug_log_enabled:
            _append_log_line(log_path, f"Job failed: {exc!r}")
        _set_job(
            job_id,
            status="failed",
            percent=100,
            message=f"转换失败：{exc}",
            error=str(exc),
            log_path=str(log_path) if debug_log_enabled else None,
        )


def _run_convert_job_inner(
    job_id: str,
    input_name: str,
    input_bytes: bytes,
    params: dict,
    warnings: list[str],
    output_dir: Path,
    progress,
    api_progress,
    api_key_event,
    log_path: Path | None,
) -> None:
        progress(5, "准备输入文件")
        if log_path:
            _append_log_line(log_path, f"job_id={job_id}")
            _append_log_line(log_path, f"input_name={input_name}")
            _append_log_line(log_path, "params=" + json.dumps(_safe_log_params(params), ensure_ascii=False, indent=2))
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_bytes(output_dir / input_name, input_bytes)

        progress(8, f"切换到 {params['device_mode'].upper()} 模式")
        _apply_device_mode(params["device_mode"])

        progress(10, "初始化解析配置")
        from rapid_doc import RapidDoc
        from rapid_doc.model.layout.rapid_layout_self import EngineType as LayoutEngineType
        from rapid_doc.model.formula.rapid_formula_self import EngineType as FormulaEngineType
        from rapid_doc.model.table.rapid_table_self import EngineType as TableEngineType
        from rapid_doc_ui.third_party import build_custom_model_configs

        settings = _settings_from_form(
            third_party_enabled=params["third_party_enabled"],
            third_party_base_url=params["third_party_base_url"],
            third_party_api_key=params["third_party_api_key"],
            third_party_api_keys=params["third_party_api_keys"],
            third_party_model=params["third_party_model"],
            third_party_use_ocr=params["third_party_use_ocr"],
            third_party_use_formula=params["third_party_use_formula"],
            third_party_use_table=params["third_party_use_table"],
            progress_callback=api_progress,
            key_event_callback=api_key_event,
        )
        if settings.enabled and (not settings.base_url or not settings.model):
            raise ValueError("Third-party API requires both base URL and model name.")

        progress(18, "加载 RapidDoc 模型")
        ocr_config, formula_config, table_config = build_custom_model_configs(settings)
        ocr_config["seal_enable"] = params["seal_enable"]
        ocr_config["heterogeneous_parallel"] = params["heterogeneous_parallel"]
        layout_config = {}
        if params["device_mode"] == "directml":
            dml_engine_cfg = {"use_dml": True}
            layout_config.update(
                {
                    "engine_type": LayoutEngineType.ONNXRUNTIME,
                    "engine_cfg": dml_engine_cfg,
                }
            )
            formula_config.setdefault("engine_type", FormulaEngineType.ONNXRUNTIME)
            formula_config["engine_cfg"] = dml_engine_cfg
            table_config.setdefault("engine_type", TableEngineType.ONNXRUNTIME)
            table_config["engine_cfg"] = dml_engine_cfg
            progress(18, "AMD/DirectML 模式：ONNX 模型尝试使用 DirectML，OCR 使用 CPU/OpenVINO")
        if params["layout_cpu_fallback"] and params["device_mode"].startswith("cuda"):
            layout_config["engine_type"] = LayoutEngineType.OPENVINO
            progress(18, "CUDA 兼容模式：版面模型使用 CPU/OpenVINO")
        engine = RapidDoc(
            layout_config=layout_config,
            ocr_config=ocr_config,
            formula_config=formula_config,
            table_config=table_config,
            parse_method=params["parse_method"],
            formula_enable=params["formula_enable"],
            table_enable=params["table_enable"],
            image_output_mode="url",
            image_dir_name="images",
        )

        progress(30, f"{params['device_mode'].upper()} 正在解析 PDF 页面")
        result = engine(
            input_bytes,
            lang=params["lang"],
            start_page_id=max(0, params["start_page"]),
            end_page_id=params["end_page"],
            f_dump_middle_json=True,
            f_dump_content_list=True,
        )
        result.markdown = _clean_text_for_utf8(result.markdown)
        result.middle_json = _clean_data_for_utf8(result.middle_json)
        result.content_list_json = _clean_data_for_utf8(result.content_list_json)

        if params["layout_cleanup"]:
            progress(72, "整理 Markdown 版面")
            result.markdown = _clean_text_for_utf8(polish_markdown_layout(result.markdown))

        progress(78, "写入图片和 Markdown")
        base_name = _safe_stem(input_name)
        _copy_images(output_dir, result.images, image_dir_name="images")

        markdown_path = output_dir / f"{base_name}.md"
        html_path = output_dir / f"{base_name}.html"
        middle_json_path = output_dir / f"{base_name}_middle.json"
        content_json_path = output_dir / f"{base_name}_content.json"
        docx_path = output_dir / f"{base_name}.docx"

        if params["export_markdown"]:
            _write_text(markdown_path, result.markdown)
        else:
            markdown_path = None

        html_preview = ""
        if params["export_html"]:
            progress(84, "生成 HTML")
            try:
                from rapid_doc.utils.markdown_to_html import markdown_to_html

                html_preview = markdown_to_html(
                    result.markdown,
                    output_path=str(html_path),
                    title=base_name,
                    embed_images=True,
                    image_base_path=str(output_dir),
                )
            except Exception as exc:
                warnings.append(f"HTML export failed: {exc}")
                html_path = None
        else:
            html_path = None

        if params["export_middle_json"] and result.middle_json is not None:
            _write_text(middle_json_path, json.dumps(result.middle_json, ensure_ascii=False, indent=2))
        else:
            middle_json_path = None

        if params["export_content_json"] and result.content_list_json is not None:
            _write_text(content_json_path, json.dumps(result.content_list_json, ensure_ascii=False, indent=2))
        else:
            content_json_path = None

        if params["export_docx"]:
            progress(90, "生成 Word 文档")
            try:
                _build_docx(result.markdown, docx_path, output_dir)
            except Exception as exc:
                warnings.append(f"Word export failed: {exc}")
                docx_path = None
        else:
            docx_path = None

        metadata = {
            "job_id": job_id,
            "source": input_name,
            "parse_method": params["parse_method"],
            "lang": params["lang"],
            "device_mode": params["device_mode"],
            "layout_cpu_fallback": params["layout_cpu_fallback"],
            "heterogeneous_parallel": params["heterogeneous_parallel"],
            "debug_log_enabled": params["debug_log_enabled"],
            "log_path": str(log_path) if log_path else None,
            "formula_enable": params["formula_enable"],
            "table_enable": params["table_enable"],
            "seal_enable": params["seal_enable"],
            "layout_cleanup": params["layout_cleanup"],
            "third_party_api": {
                "enabled": settings.enabled,
                "base_url": settings.base_url,
                "model": settings.model,
                "use_for_ocr": settings.use_for_ocr,
                "use_for_formula": settings.use_for_formula,
                "use_for_table": settings.use_for_table,
                "api_key_count": len(settings.api_keys or []),
            },
            "warnings": warnings,
        }
        metadata = _clean_data_for_utf8(metadata)
        _write_text(output_dir / "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))

        progress(96, "打包结果")
        zip_path = OUTPUT_ROOT / f"{job_id}.zip"
        _zip_directory(output_dir, zip_path)

        response = ConvertResponse(
            job_id=job_id,
            file_name=input_name,
            output_dir=str(output_dir),
            zip_path=str(zip_path),
            markdown_path=str(markdown_path) if markdown_path else None,
            html_path=str(html_path) if html_path else None,
            middle_json_path=str(middle_json_path) if middle_json_path else None,
            content_json_path=str(content_json_path) if content_json_path else None,
            docx_path=str(docx_path) if docx_path else None,
            log_path=str(log_path) if log_path else None,
            markdown_preview=_clean_text_for_utf8(result.markdown[:60000]),
            html_preview=_clean_text_for_utf8(html_preview[:250000]),
            warnings=_clean_data_for_utf8(warnings),
            third_party_api_enabled=settings.enabled,
        )
        _set_job(
            job_id,
            status="completed",
            percent=100,
            message="转换完成",
            result=response.model_dump(),
        )


@app.post("/api/convert")
async def convert(
    file: Annotated[UploadFile | None, File()] = None,
    use_sample: Annotated[str, Form()] = "false",
    parse_method: Annotated[str, Form()] = "auto",
    lang: Annotated[str, Form()] = "ch",
    device_mode: Annotated[str, Form()] = "cpu",
    layout_cpu_fallback: Annotated[str, Form()] = "false",
    heterogeneous_parallel: Annotated[str, Form()] = "false",
    debug_log_enabled: Annotated[str, Form()] = "false",
    start_page: Annotated[int, Form()] = 0,
    end_page: Annotated[int | None, Form()] = None,
    formula_enable: Annotated[str, Form()] = "true",
    table_enable: Annotated[str, Form()] = "true",
    seal_enable: Annotated[str, Form()] = "false",
    export_markdown: Annotated[str, Form()] = "true",
    export_middle_json: Annotated[str, Form()] = "true",
    export_content_json: Annotated[str, Form()] = "true",
    export_html: Annotated[str, Form()] = "true",
    export_docx: Annotated[str, Form()] = "false",
    layout_cleanup: Annotated[str, Form()] = "true",
    third_party_enabled: Annotated[str, Form()] = "false",
    third_party_base_url: Annotated[str, Form()] = "",
    third_party_api_key: Annotated[str, Form()] = "",
    third_party_api_key_choice: Annotated[str, Form()] = "",
    third_party_api_keys_text: Annotated[str, Form()] = "",
    third_party_api_key_file: Annotated[UploadFile | None, File()] = None,
    third_party_model: Annotated[str, Form()] = "",
    third_party_use_ocr: Annotated[str, Form()] = "true",
    third_party_use_formula: Annotated[str, Form()] = "false",
    third_party_use_table: Annotated[str, Form()] = "false",
) -> JSONResponse:
    sample_enabled = _parse_bool(use_sample)
    if sample_enabled:
        if not DEFAULT_TEST_PDF.exists():
            raise HTTPException(status_code=404, detail=f"Sample PDF not found: {DEFAULT_TEST_PDF}")
        input_name = DEFAULT_TEST_PDF.name
        input_bytes = DEFAULT_TEST_PDF.read_bytes()
    else:
        if file is None:
            raise HTTPException(status_code=400, detail="Please upload a PDF or enable sample mode.")
        input_name = file.filename or "document.pdf"
        input_bytes = await file.read()

    if parse_method not in {"auto", "ocr", "txt"}:
        raise HTTPException(status_code=400, detail="parse_method must be auto, ocr, or txt.")
    normalized_device_mode = (device_mode or "cpu").strip().lower()
    normalized_device_mode = _normalize_device_mode(normalized_device_mode)
    if normalized_device_mode not in VALID_DEVICE_MODES:
        raise HTTPException(status_code=400, detail="device_mode must be one of: cpu, cuda, cuda:0, cuda:1, npu, npu:0, directml.")

    project_api_keys, _ = _load_api_keys_from_project()
    uploaded_api_keys: list[str] = []
    if third_party_api_key_file is not None:
        uploaded_text = (await third_party_api_key_file.read()).decode("utf-8-sig", errors="ignore")
        uploaded_api_keys = _read_api_keys_text(uploaded_text)

    inline_api_keys = _read_api_keys_text(third_party_api_keys_text)
    available_api_keys: list[str] = []
    for key in [*project_api_keys, *uploaded_api_keys, *inline_api_keys]:
        if key and key not in available_api_keys:
            available_api_keys.append(key)

    selected_api_key = (third_party_api_key_choice or third_party_api_key or "").strip()
    if selected_api_key:
        available_api_keys = [selected_api_key, *[key for key in available_api_keys if key != selected_api_key]]

    job_id = f"{int(time.time())}_{_safe_stem(input_name)}"
    params = {
        "parse_method": parse_method,
        "lang": lang,
        "device_mode": normalized_device_mode,
        "layout_cpu_fallback": _parse_bool(layout_cpu_fallback),
        "heterogeneous_parallel": _parse_bool(heterogeneous_parallel),
        "debug_log_enabled": _parse_bool(debug_log_enabled),
        "start_page": start_page,
        "end_page": end_page,
        "formula_enable": _parse_bool(formula_enable, True),
        "table_enable": _parse_bool(table_enable, True),
        "seal_enable": _parse_bool(seal_enable),
        "export_markdown": _parse_bool(export_markdown, True),
        "export_middle_json": _parse_bool(export_middle_json, True),
        "export_content_json": _parse_bool(export_content_json, True),
        "export_html": _parse_bool(export_html, True),
        "export_docx": _parse_bool(export_docx),
        "layout_cleanup": _parse_bool(layout_cleanup, True),
        "third_party_enabled": _parse_bool(third_party_enabled),
        "third_party_base_url": third_party_base_url,
        "third_party_api_key": selected_api_key,
        "third_party_api_keys": available_api_keys,
        "third_party_model": third_party_model,
        "third_party_use_ocr": _parse_bool(third_party_use_ocr, True),
        "third_party_use_formula": _parse_bool(third_party_use_formula),
        "third_party_use_table": _parse_bool(third_party_use_table),
    }
    _set_job(
        job_id,
        status="queued",
        percent=0,
        message="任务已创建",
        file_name=input_name,
        created_at=time.time(),
    )
    EXECUTOR.submit(_run_convert_job, job_id, input_name, input_bytes, params)
    return JSONResponse({"job_id": job_id, "status": "queued", "percent": 0, "message": "任务已创建"})


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> JSONResponse:
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return JSONResponse(job)


@app.get("/api/download/{job_id}")
def download(job_id: str) -> FileResponse:
    zip_path = OUTPUT_ROOT / f"{job_id}.zip"
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Zip file not found.")
    return FileResponse(zip_path, filename=zip_path.name, media_type="application/zip")


@app.get("/api/log/{job_id}")
def download_log(job_id: str) -> FileResponse:
    log_path = _log_file_path(job_id)
    if not log_path.exists() or not log_path.is_file():
        raise HTTPException(status_code=404, detail="Log file not found.")
    return FileResponse(log_path, filename=log_path.name, media_type="text/plain; charset=utf-8")


@app.get("/api/file/{job_id}/{file_name}")
def download_file(job_id: str, file_name: str) -> FileResponse:
    file_path = OUTPUT_ROOT / job_id / file_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(file_path, filename=file_path.name)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def main() -> None:
    import uvicorn

    os.environ.setdefault("MINERU_DEVICE_MODE", "cpu")
    uvicorn.run("rapid_doc_ui.app:app", host="127.0.0.1", port=7862, reload=False)


if __name__ == "__main__":
    main()
