from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from typing import Callable, Literal
from urllib.parse import urljoin

import cv2
import numpy as np
from PIL import Image
import requests

from rapid_doc.model.custom import CustomBaseModel


TaskName = Literal["ocr", "formula", "table"]
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
SILICONFLOW_DEFAULT_MODEL = "Qwen/Qwen3-VL-32B-Instruct"


@dataclass
class ThirdPartyAPISettings:
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    api_keys: list[str] | None = None
    model: str = ""
    use_for_ocr: bool = True
    use_for_formula: bool = False
    use_for_table: bool = False
    temperature: float = 0.0
    max_tokens: int = 4096
    progress_callback: Callable[[str, int | None, int | None], None] | None = None
    key_event_callback: Callable[[str], None] | None = None


class OpenAICompatibleVLMModel(CustomBaseModel):
    """OpenAI-compatible VLM adapter for RapidDoc custom OCR/formula/table hooks."""

    PROMPTS: dict[TaskName, str] = {
        "ocr": (
            "你是 OCR 引擎。请只输出图片中的实际内容，不要解释、不要总结、不要添加 Markdown 装饰。"
            "保留原有段落顺序和必要换行。数学公式必须使用 LaTeX，并用 $...$ 或 $$...$$ 包裹。"
        ),
        "formula": (
            "你是公式识别引擎。请只输出图片中的数学公式 LaTeX，不要解释、不要 Markdown 代码块。"
            "行内公式用 $...$，独立公式用 $$...$$。"
        ),
        "table": (
            "你是表格识别引擎。请只输出图片中表格对应的干净 HTML table。"
            "不要解释、不要 Markdown 代码块、不要添加表格外的文字。"
        ),
    }

    def __init__(self, settings: ThirdPartyAPISettings, task: TaskName) -> None:
        if not settings.base_url:
            raise ValueError("Third-party API base_url is required.")
        if not settings.model:
            raise ValueError("Third-party API model is required.")

        self.settings = settings
        self.task = task
        self.endpoint = self._build_endpoint(settings.base_url)
        self.api_keys = self._normalized_api_keys(settings.api_key, settings.api_keys or [])
        self.key_index = 0

    def batch_predict(self, image_list: list[np.ndarray], **kwargs) -> list[str]:
        results: list[str] = []
        total = len(image_list)
        for index, image in enumerate(image_list, start=1):
            if self.settings.progress_callback:
                self.settings.progress_callback(self.task, index, total)
            results.append(self._predict_one(image))
        return results

    def _predict_one(self, image: np.ndarray) -> str:
        image_url = self._image_to_data_url(image)
        payload = {
            "model": self.settings.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.PROMPTS[self.task]},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
        }
        data = self._post_with_key_rotation(payload)
        content = data["choices"][0]["message"].get("content") or ""
        return self._clean_model_text(content)

    def _post_with_key_rotation(self, payload: dict) -> dict:
        attempts = max(1, len(self.api_keys))
        errors: list[str] = []

        for attempt in range(attempts):
            api_key = self.api_keys[self.key_index] if self.api_keys else ""
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            try:
                response = requests.post(self.endpoint, json=payload, headers=headers, timeout=600)
            except requests.RequestException as exc:
                errors.append(f"request error with {self._mask_key(api_key)}: {exc}")
                response = None

            if response is not None and response.ok:
                return response.json()

            if response is not None:
                errors.append(
                    f"{response.status_code} with {self._mask_key(api_key)}: {response.text[:1000]}"
                )

            if len(self.api_keys) <= 1 or attempt == attempts - 1:
                break

            self.key_index = (self.key_index + 1) % len(self.api_keys)
            if self.settings.key_event_callback:
                self.settings.key_event_callback(
                    f"第三方 API 返回错误，已切换到 {self._mask_key(self.api_keys[self.key_index])} 后重试"
                )

        raise RuntimeError("Third-party API failed after key rotation: " + " | ".join(errors))

    @staticmethod
    def _build_endpoint(base_url: str) -> str:
        normalized = base_url.strip().rstrip("/")
        if normalized.endswith("/chat/completions"):
            return normalized
        if normalized.endswith("/v1"):
            return f"{normalized}/chat/completions"
        return urljoin(f"{normalized}/", "v1/chat/completions")

    @staticmethod
    def _clean_model_text(text: str) -> str:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        return cleaned

    @staticmethod
    def _normalized_api_keys(primary_key: str, api_keys: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw_key in [primary_key, *api_keys]:
            key = (raw_key or "").strip()
            if key and key not in normalized:
                normalized.append(key)
        return normalized

    @staticmethod
    def _mask_key(api_key: str) -> str:
        if not api_key:
            return "empty key"
        if len(api_key) <= 8:
            return "*" * len(api_key)
        return f"{api_key[:4]}...{api_key[-4:]}"

    @staticmethod
    def _image_to_data_url(image: np.ndarray) -> str:
        if not isinstance(image, np.ndarray):
            raise TypeError(f"Unsupported image input type: {type(image)!r}")

        image = OpenAICompatibleVLMModel._normalize_min_image_size(image)
        rgb_image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        pil_image = Image.fromarray(rgb_image)
        with io.BytesIO() as buffer:
            pil_image.save(buffer, format="JPEG", quality=92)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    @staticmethod
    def _normalize_min_image_size(image: np.ndarray, min_side: int = 64) -> np.ndarray:
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        if image.ndim == 3 and image.shape[2] == 4:
            image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)

        height, width = image.shape[:2]
        if height >= min_side and width >= min_side:
            return image

        scale = max(min_side / max(height, 1), min_side / max(width, 1), 1.0)
        new_width = max(min_side, int(round(width * scale)))
        new_height = max(min_side, int(round(height * scale)))
        resized = cv2.resize(image, (new_width, new_height), interpolation=cv2.INTER_CUBIC)

        canvas_width = max(new_width, min_side)
        canvas_height = max(new_height, min_side)
        canvas = np.full((canvas_height, canvas_width, 3), 255, dtype=np.uint8)
        top = (canvas_height - new_height) // 2
        left = (canvas_width - new_width) // 2
        canvas[top:top + new_height, left:left + new_width] = resized
        return canvas


def build_custom_model_configs(
    settings: ThirdPartyAPISettings,
) -> tuple[dict, dict, dict]:
    ocr_config: dict = {}
    formula_config: dict = {}
    table_config: dict = {}

    if not settings.enabled:
        return ocr_config, formula_config, table_config

    if settings.use_for_ocr:
        ocr_config["custom_model"] = OpenAICompatibleVLMModel(settings, "ocr")
    if settings.use_for_formula:
        formula_config["custom_model"] = OpenAICompatibleVLMModel(settings, "formula")
    if settings.use_for_table:
        table_config["custom_model"] = OpenAICompatibleVLMModel(settings, "table")

    return ocr_config, formula_config, table_config
