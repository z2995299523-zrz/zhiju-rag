"""Offline image text recognition, loaded only by an opted-in parser worker."""

import io
import math
import os
from functools import lru_cache
from importlib.util import find_spec
from pathlib import Path


IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff"})
MAX_OCR_PAGES = 5
MAX_FRAME_PIXELS = 12_000_000
MAX_RASTER_SIDE = 2000
_DISABLED = "扫描件或图片需要 OCR（识别图中文字）。请启用 RAG_OCR_ENABLED=1 并安装离线识别组件，或上传含文本层的 PDF。"
_MISSING = "离线 OCR 组件或中文模型缺失，请安装 requirements-ocr.txt 中的依赖并核对 RapidOCR 模型文件。"
_MODELS = {
    "Det.model_path": "ch_PP-OCRv4_det_infer.onnx",
    "Cls.model_path": "ch_ppocr_mobile_v2.0_cls_infer.onnx",
    "Rec.model_path": "ch_PP-OCRv4_rec_infer.onnx",
    "Rec.rec_keys_path": "ppocr_keys_v1.txt",
}


def enabled():
    return os.getenv("RAG_OCR_ENABLED") == "1"


def require_enabled():
    if not enabled():
        raise ValueError(_DISABLED)


def _model_paths():
    spec = find_spec("rapidocr")
    if spec is None or not spec.origin:
        return {}
    directory = Path(spec.origin).parent / "models"
    return {key: directory / filename for key, filename in _MODELS.items()}


def available():
    if not enabled():
        return False
    paths = _model_paths()
    return bool(paths and all(p.is_file() for p in paths.values())
                and all(find_spec(name) is not None for name in ("onnxruntime", "PIL", "cv2", "pypdfium2")))


def _require_assets():
    require_enabled()
    if not available():
        raise ValueError(_MISSING)


@lru_cache(maxsize=1)
def _recognizer():
    paths = _model_paths()
    if not paths or not all(path.is_file() for path in paths.values()):
        raise ValueError(_MISSING)
    try:
        import cv2
        import onnxruntime
        from rapidocr import RapidOCR
    except (ImportError, OSError) as exc:
        raise ValueError(_MISSING) from exc
    cv2.setNumThreads(1)
    onnxruntime.disable_telemetry_events()
    # Explicit paths prevent RapidOCR's model and dictionary download fallback.
    return RapidOCR(params={
        **{key: str(path) for key, path in paths.items()},
        "EngineConfig.onnxruntime.intra_op_num_threads": 1,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "Det.limit_type": "max", "Det.limit_side_len": 1280,
        "Global.log_level": "error", "Global.text_score": 0.55,
    })


def _recognize(image):
    from PIL import Image

    width, height = image.size
    if min(width, height) <= 0 or width * height > MAX_FRAME_PIXELS or max(width, height) > 10_000:
        raise ValueError("OCR 单页图片限 1200 万像素、最长边 10000 像素，请缩小原图")
    rgb = image.convert("RGB")
    try:
        rgb.thumbnail((MAX_RASTER_SIDE, MAX_RASTER_SIDE), Image.Resampling.LANCZOS)
        result = _recognizer()(rgb)
        lines = [line.strip() for line in (result.txts or ()) if line.strip()]
        return "\n".join(lines)
    finally:
        rgb.close()


def image_pages(data, suffix):
    _require_assets()
    from PIL import Image, ImageOps

    formats = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".tif": "TIFF", ".tiff": "TIFF"}
    with Image.open(io.BytesIO(data), formats=[formats[suffix]]) as image:
        # Seeking one frame at a time also bounds multipage TIFF directory traversal.
        for index in range(MAX_OCR_PAGES + 1):
            try:
                image.seek(index)
            except EOFError:
                break
            if index == MAX_OCR_PAGES:
                raise ValueError(f"单文件最多可 OCR {MAX_OCR_PAGES} 页，请拆分文件")
            if image.width * image.height > MAX_FRAME_PIXELS or max(image.size) > 10_000:
                raise ValueError("OCR 单页图片限 1200 万像素、最长边 10000 像素，请缩小原图")
            with ImageOps.exif_transpose(image) as upright:
                yield index + 1, _recognize(upright)


def pdf_pages(data, page_numbers):
    """Render only textless pages. Page numbers are one-based source PDF indices."""
    require_enabled()
    if len(page_numbers) > MAX_OCR_PAGES:
        raise ValueError(f"单文件最多可 OCR {MAX_OCR_PAGES} 页，请拆分扫描件")
    _require_assets()
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(data) as document:
        for number in page_numbers:
            page = document[number - 1]
            try:
                width, height = page.get_size()
                if not all(math.isfinite(x) and 0 < x <= 20_000 for x in (width, height)):
                    raise ValueError("PDF 页面尺寸无效或过大")
                scale = min(2.0, MAX_RASTER_SIDE / max(width, height))
                bitmap = page.render(scale=scale)
                try:
                    with bitmap.to_pil() as image:
                        yield number, _recognize(image)
                finally:
                    bitmap.close()
            finally:
                page.close()
