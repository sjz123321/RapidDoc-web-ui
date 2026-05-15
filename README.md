# RapidDoc OCR UI

这是一个基于 RapidDoc 的本地 OCR Web UI，用于将 PDF、图片和常见 Office 文档转换为 Markdown、JSON、HTML、Word 等格式。项目保留 RapidDoc 的本地 CPU/GPU 推理能力，并增加了第三方多模态 API 接入、API Key 自动轮换、任务进度显示和常见 GPU 兼容开关。

本项目适合需要批量处理文档、保留图片资源、导出结构化结果，或在本地 OCR 基础上用第三方 VLM 做局部增强的场景。

## 功能

- PDF / 图片 / Office 文档转换
- Markdown、Middle JSON、Content JSON、HTML、Word、ZIP 输出
- 本地 CPU / OpenVINO 模式
- NVIDIA CUDA GPU 模式
- AMD / DirectML 实验模式
- V100 等老架构显卡可用的 CUDA 兼容模式
- OCR + 公式异构并行加速实验模式
- 可选调试日志输出到 `ui_logs/`
- 第三方 OpenAI-compatible VLM API 接入
- 硅基流动 API 默认配置
- `apikey.txt` 自动导入和 API Key 失败轮换
- 转换进度轮询显示
- 图片尺寸自动补白，规避部分 VLM 的最小尺寸限制
- Markdown 版面后处理
- 印章识别开关，默认关闭以避免不必要的模型兼容问题

## 环境要求

- Python 3.10 到 3.13
- Windows / Linux / macOS
- CPU 模式推荐，兼容性最好
- GPU 模式需要 NVIDIA CUDA 环境

AMD 显卡不能使用 CUDA 模式；如需尝试 AMD GPU，可使用 DirectML 实验模式。

## 安装

克隆项目后进入目录：

```powershell
cd RapidDoc-main
```

推荐 CPU 安装：

```powershell
python -m pip install -e ".[cpu,ui]"
```

如果不使用 editable 安装，也可以安装 PyPI 版 RapidDoc 和 UI 依赖：

```powershell
python -m pip install "rapid-doc[cpu]"
python -m pip install fastapi "uvicorn[standard]" python-multipart markdown-it-py mdit-py-plugins pygments pypandoc-binary
```

NVIDIA GPU 安装：

```powershell
python -m pip install -e ".[gpu,ui]"
```

GPU 依赖必须与本机 CUDA、cuDNN、驱动版本匹配。如果 GPU 模式不可用，请先使用 CPU 模式确认主流程正常。

AMD / DirectML 实验模式建议使用独立环境，避免 `onnxruntime`、`onnxruntime-gpu`、`onnxruntime-directml` 混装：

```powershell
python -m pip uninstall -y onnxruntime onnxruntime-gpu onnxruntime-directml
python -m pip install -e ".[directml,ui]"
python -m pip uninstall -y onnxruntime onnxruntime-gpu
python -m pip install --force-reinstall "onnxruntime-directml>=1.18.0,<1.24.0"
```

DirectML 模式目前是混合推理：OCR 仍使用 CPU/OpenVINO，版面、公式、表格等 ONNXRuntime 模型会尝试使用 `DmlExecutionProvider`。

## 启动

```powershell
python -m uvicorn rapid_doc_ui.app:app --host 127.0.0.1 --port 7862
```

也可以直接运行脚本：

```powershell
.\start_ui.ps1
```

浏览器打开：

```text
http://127.0.0.1:7862
```

## 使用

1. 上传 PDF、图片或 Office 文件。
2. 选择解析模式、语言和设备模式。
3. 选择要导出的格式。
4. 如需使用第三方 API，勾选“第三方 API”并填写 Base URL、模型名和 API Key。
5. 点击“开始转换”。

转换结果会写入：

```text
RapidDoc-main/ui_output/
```

每个任务会生成独立目录和 ZIP 包，通常包含：

- `.md`
- `_middle.json`
- `_content.json`
- `.html`
- `.docx`，如果勾选 Word
- 图片资源
- `metadata.json`

## 设备模式

界面中的“设备模式”可以选择：

- `CPU / OpenVINO`
- `GPU / CUDA`
- `GPU / CUDA:0`
- `GPU / CUDA:1`
- `AMD / DirectML（实验）`

CPU 模式最稳定。GPU 模式使用 RapidDoc 的 `MINERU_DEVICE_MODE=cuda` 路线，主要面向 NVIDIA 显卡。

DirectML 模式主要面向 Windows + AMD 显卡。它不是完整 GPU 化：OCR 保持 CPU/OpenVINO，以避免 RapidOCR 在非 CUDA 设备上进入不稳定路径。

### 异构并行加速

可以勾选“异构并行加速”尝试让 OCR 与公式识别并行运行：

```text
layout 版面分析
-> OCR 和 formula 并行
-> table 表格识别
-> 结果合并
```

这个模式适合 CUDA、CUDA 兼容模式、DirectML 这类 CPU/GPU 混合场景。CPU 单独模式下不一定更快，因为 OCR 和公式会争抢同一组 CPU 资源。

表格识别目前仍在 OCR 和公式完成后执行，因为表格处理可能需要公式区域结果。

### 调试日志

勾选“输出调试日志”后，每个任务会在项目目录生成：

```text
RapidDoc-main/ui_logs/<job_id>.log
```

日志会记录任务参数、进度、RapidDoc / RapidOCR / ONNXRuntime 输出和异常信息。API Key 会在任务参数中脱敏。转换完成后可在“文件”页下载 Log。

### CUDA 兼容模式

部分老架构 NVIDIA 显卡，例如 V100，可能在版面模型的 ONNXRuntime CUDA 推理中报错：

```text
cudaErrorNoKernelImageForDevice:no kernel image is available for execution on the device
```

遇到这种情况可以勾选“CUDA 兼容模式”。该模式会让版面模型使用 CPU/OpenVINO，避开不兼容的 ONNX CUDA kernel，其他模型仍尽量按 CUDA 配置运行。

## 第三方 API

第三方 API 使用 OpenAI-compatible Chat Completions 格式。默认兼容硅基流动：

- Base URL：`https://api.siliconflow.cn/v1`
- 默认模型：`Qwen/Qwen3-VL-32B-Instruct`
- 鉴权：`Authorization: Bearer <API Key>`

第三方 API 可选择用于：

- OCR
- 公式
- 表格

建议先用本地 CPU 模式完成主要识别，再按需开启第三方 API。通用 VLM 更适合局部增强，不适合作为稳定的整页排版引擎。

## API Key 自动导入和轮换

可以在项目根目录放置 `apikey.txt`，每一行填写一个 API Key。程序启动后会自动读取以下位置：

```text
../apikey.txt
RapidDoc-main/apikey.txt
```

界面会显示“已加载 Key”下拉菜单，可手动选择从哪个 key 开始调用。转换过程中如果第三方 API 返回错误码，程序会自动切换到下一个 key，并重试刚才失败的图块。

如果没有找到 `apikey.txt`，可以在界面 API Key 输入框右侧点击钥匙按钮选择一个本地 `.txt` 文件。也可以不使用文件，直接在输入框中填写单个 API Key。

不要把 `apikey.txt` 提交到 GitHub。项目 `.gitignore` 已默认排除该文件。

## 诊断接口

服务启动后可以打开：

```text
http://127.0.0.1:7862/api/device-diagnostics
```

重点检查：

- `onnxruntime.has_cuda_provider`
- `onnxruntime.has_directml_provider`
- `torch.cuda_available`
- `nvidia_smi.available`

这些字段可以帮助判断 GPU 环境是否正确安装。

## 常见问题

### Word 导出失败

请确认安装：

```powershell
python -m pip install pypandoc-binary python-docx
```

### GPU 不工作

请确认：

- 使用 NVIDIA 显卡
- CUDA/cuDNN 与 `onnxruntime-gpu` 匹配
- 已安装 `rapid-doc[gpu]`
- 已安装支持 CUDA 的 `torch` / `torchvision`
- `api/device-diagnostics` 中 CUDA 相关字段正常

### AMD / DirectML 不工作

请确认：

- Windows 系统已正确安装 AMD 显卡驱动
- 当前 Python 环境安装的是 `onnxruntime-directml`
- 如果曾安装 CPU/GPU 版 ONNXRuntime，最后重新执行过 `pip install --force-reinstall onnxruntime-directml`
- `api/device-diagnostics` 中 `onnxruntime.has_directml_provider` 为 `true`

DirectML 是实验模式。如果模型或算子不被 DirectML 支持，ONNXRuntime 可能回退到 CPU，或在部分模型上报错。遇到这种情况请使用 CPU 模式。

### 异构并行加速不明显

请确认文档里确实有公式区域。当前实验模式主要并行 OCR 与公式识别；如果文档几乎没有公式，或者当前模式下公式模型也在 CPU 上运行，加速效果会很有限。

### V100 报 cudaErrorNoKernelImageForDevice

勾选“CUDA 兼容模式”。如果仍失败，请尝试更换 `onnxruntime-gpu` 版本，或直接使用 CPU 模式。

### architecture pp-ocrv4_mobile_seal_det is not in arch_config.yaml

这是印章 OCR 模型在当前 RapidOCR / torch 配置里找不到对应 architecture。UI 已将“印章”识别默认关闭。普通 PDF 转 Markdown/JSON/HTML/Word 不需要启用它。

### 第三方 API 报图片尺寸错误

当前 UI 会自动放大并补白过小的裁剪图块。如果仍报错，请确认使用的是最新代码，并重启服务。

### UTF-8 surrogate 编码错误

当前 UI 会在写入 Markdown、JSON、HTML 预览前清理非法 surrogate 字符，避免因单个坏字符导致转换中断。

## 项目结构

```text
rapid_doc_ui/
  app.py              # FastAPI 后端、任务队列、导出逻辑
  third_party.py      # 第三方 VLM API 适配器和 key 轮换
  postprocess.py      # Markdown 版面后处理
  static/             # Web UI
README.md
README_UI_DEPLOY.md
requirements-ui-cpu.txt
requirements-ui-gpu.txt
start_ui.ps1
start_ui.bat
```

## 说明

本项目基于 RapidDoc / RapidOCR 生态构建。RapidDoc 原始项目请参考上游仓库文档。当前仓库重点维护本地 OCR Web UI、第三方 API 集成和部署体验。
