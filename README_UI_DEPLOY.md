# RapidDoc OCR UI 部署说明

这个目录包含一个基于 RapidDoc 的本地 OCR Web UI，可将 PDF/图片/Office 文件转换为 Markdown、JSON、HTML、Word，并支持可选第三方多模态 API 增强。

## 1. 环境要求

- Python 3.10 到 3.13
- Windows / Linux / macOS 均可运行
- CPU 模式推荐，兼容性最好
- GPU 模式仅建议 NVIDIA CUDA 环境使用
- AMD 显卡可尝试 DirectML 实验模式

## 2. 安装依赖

进入项目目录：

```powershell
cd RapidDoc-main
```

推荐 CPU 安装：

```powershell
python -m pip install -e ".[cpu,ui]"
```

如果只想安装 PyPI 版本而不是 editable，也可以：

```powershell
python -m pip install "rapid-doc[cpu]"
python -m pip install fastapi "uvicorn[standard]" python-multipart markdown-it-py mdit-py-plugins pygments pypandoc-binary
```

GPU 模式需要 NVIDIA CUDA，并且依赖版本要和本机 CUDA/cuDNN 匹配：

```powershell
python -m pip install -e ".[gpu,ui]"
```

如果 GPU 依赖安装后不可用，请先使用 CPU 模式。

AMD / DirectML 实验模式建议使用独立环境，避免 ONNXRuntime 包冲突：

```powershell
python -m pip uninstall -y onnxruntime onnxruntime-gpu onnxruntime-directml
python -m pip install -e ".[directml,ui]"
python -m pip uninstall -y onnxruntime onnxruntime-gpu
python -m pip install --force-reinstall "onnxruntime-directml>=1.18.0,<1.24.0"
```

或安装依赖文件：

```powershell
python -m pip install -r requirements-ui-directml.txt
```

## 3. 启动 UI

```powershell
python -m uvicorn rapid_doc_ui.app:app --host 127.0.0.1 --port 7862
```

浏览器打开：

```text
http://127.0.0.1:7862
```

## 4. 输出文件

转换结果会写入：

```text
RapidDoc-main/ui_output/
```

每个任务会生成一个独立目录和一个 ZIP 包，包含：

- `.md`
- `_middle.json`
- `_content.json`
- `.html`
- `.docx`，如果勾选 Word
- 图片资源
- `metadata.json`

## 5. CPU / GPU 切换

界面中的“设备模式”可以选择：

- `CPU / OpenVINO`
- `GPU / CUDA`
- `GPU / CUDA:0`
- `GPU / CUDA:1`
- `AMD / DirectML（实验）`

CPU 模式最稳定。GPU 模式使用 RapidDoc 的 `MINERU_DEVICE_MODE=cuda` 路线，主要面向 NVIDIA 显卡。

DirectML 模式面向 Windows + AMD 显卡。当前实现为混合推理：OCR 仍使用 CPU/OpenVINO，版面、公式、表格等 ONNXRuntime 模型会尝试使用 `DmlExecutionProvider`。

### 异构并行加速

界面可勾选“异构并行加速”。该实验模式会在版面分析后并行执行：

- CPU/OpenVINO OCR
- CUDA 或 DirectML 公式识别

表格识别仍会在 OCR 和公式完成后执行，因为表格处理可能依赖公式区域信息。

该模式适合 CUDA、CUDA 兼容模式、DirectML 这类 CPU/GPU 混合场景。纯 CPU 模式下不一定更快。

### 调试日志

界面可勾选“输出调试日志”。开启后会在项目目录生成：

```text
RapidDoc-main/ui_logs/<job_id>.log
```

日志会记录任务参数、进度、RapidDoc / RapidOCR / ONNXRuntime 输出和异常信息。API Key 会在任务参数中脱敏。转换完成后可在“文件”页下载 Log。

## 6. 第三方 API

界面中可以勾选“第三方 API”。默认兼容硅基流动：

- Base URL：`https://api.siliconflow.cn/v1`
- 默认模型：`Qwen/Qwen3-VL-32B-Instruct`
- 鉴权：`Authorization: Bearer <API Key>`

第三方 API 可选择用于：

- OCR
- 公式
- 表格

建议先用本地 CPU 模式完成主要识别，再按需开启第三方 API。通用 VLM 更适合局部增强，不适合作为稳定的整页排版引擎。

### API Key 自动加载与轮换

可以在项目根目录放置 `apikey.txt`，每一行填写一个 API Key。程序启动后会自动读取以下位置：

```text
../apikey.txt
RapidDoc-main/apikey.txt
```

界面会显示“已加载 Key”下拉菜单，可手动选择从哪个 key 开始调用。转换过程中如果第三方 API 返回错误码，程序会自动切换到下一个 key，并重试刚才失败的图块。

如果没有找到 `apikey.txt`，可以在界面 API Key 输入框右侧点击钥匙按钮选择一个本地 `.txt` 文件。也可以不使用文件，直接在输入框中填写单个 API Key。

## 7. 首次运行说明

首次转换时 RapidDoc 会自动下载模型，耗时会比后续运行长。模型默认下载到 RapidDoc 或 RapidOCR 的模型目录中。

如果需要指定模型存储目录，可在启动前设置：

```powershell
$env:RAPID_MODELS_DIR="D:\RapidDocModels"
python -m uvicorn rapid_doc_ui.app:app --host 127.0.0.1 --port 7862
```

## 8. 常见问题

### Word 导出失败

请确认安装了：

```powershell
python -m pip install pypandoc-binary python-docx
```

### GPU 不工作

请确认：

- 使用 NVIDIA 显卡
- CUDA/cuDNN 与 `onnxruntime-gpu` 匹配
- 已安装 `rapid-doc[gpu]`
- 已安装支持 CUDA 的 `torch` / `torchvision`
- 在浏览器打开 `http://127.0.0.1:7862/api/device-diagnostics`，确认：
  - `onnxruntime.has_cuda_provider` 为 `true`
  - AMD/DirectML 模式下 `onnxruntime.has_directml_provider` 为 `true`
  - `torch.cuda_available` 为 `true`
  - `nvidia_smi.available` 为 `true`

AMD 显卡不建议走这个 GPU 模式。

### AMD / DirectML 不工作

请确认：

- 使用 Windows 系统
- AMD 显卡驱动正常
- 已安装 `onnxruntime-directml`
- 如果曾安装 CPU/GPU 版 ONNXRuntime，最后重新执行过 `pip install --force-reinstall onnxruntime-directml`
- `http://127.0.0.1:7862/api/device-diagnostics` 中 `onnxruntime.has_directml_provider` 为 `true`

DirectML 是实验模式，部分模型或算子可能无法在 AMD GPU 上执行。如果遇到模型运行错误，请切回 CPU 模式。

### 异构并行加速不明显

请确认文档中有公式区域。当前实验模式主要并行 OCR 与公式识别；如果没有公式，或者公式模型也在 CPU 上运行，加速效果会很有限。

如果日志里只有 `Could not determine GPU memory, using default batch_ratio: 1`，通常只是没有读取到显存大小，RapidDoc 会使用最小 batch 继续运行，不代表 GPU 初始化失败。

如果日志里出现 `ScatterND with reduction=='none'...`，这是 ONNXRuntime CUDA 的算子警告，通常不是致命错误。真正的错误一般会包含 `CUDAExecutionProvider`、`cudnn`、`DLL`、`torch.cuda.is_available()` 或 `out of memory` 等字样。

### V100 报 cudaErrorNoKernelImageForDevice

如果日志包含：

```text
cudaErrorNoKernelImageForDevice:no kernel image is available for execution on the device
```

这通常表示当前 `onnxruntime-gpu` / CUDA 二进制包没有适配这张 NVIDIA GPU 的计算架构。V100 属于 Volta 架构，某些较新的预编译 CUDA 运行时可能会遇到这个问题。双显卡工作站里接入 AMD 显卡通常不是直接原因，因为 AMD 不会作为 CUDA 设备被 ONNXRuntime 选择。

处理方式：

- 在 UI 勾选“CUDA 兼容模式”，让版面模型使用 CPU/OpenVINO，避开 V100 上的 ONNX CUDA kernel。
- 或尝试更换 `onnxruntime-gpu` 版本，使其与 V100、CUDA、cuDNN 匹配。
- 如果仍失败，直接使用 CPU 模式最稳定。

### architecture pp-ocrv4_mobile_seal_det is not in arch_config.yaml

这是印章 OCR 模型在当前 RapidOCR / torch 配置里找不到对应 architecture。UI 已将“印章”识别默认关闭；普通 PDF 转 Markdown/JSON/HTML/Word 不需要启用它。

如果确实需要识别印章文字，可以尝试升级 RapidDoc/RapidOCR，或改用 CPU 模式后再勾选“印章”。如果不需要印章，请保持“印章”开关关闭。

### 第三方 API 报图片尺寸错误

当前 UI 已自动放大并补白过小的裁剪图块。如果仍报错，请确认使用的是最新代码，并重启服务。
