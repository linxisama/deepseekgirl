"""独立图片 OCR 工作进程，避免 WinRT OCR 卡住机器人主事件循环。"""

import asyncio
import json
import sys
import uuid
from pathlib import Path


async def extract_text(image_path: str) -> str:
    from PIL import Image, ImageEnhance, ImageOps
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage import FileAccessMode, StorageFile

    image = Image.open(image_path)
    if getattr(image, "n_frames", 1) > 1:
        image.seek(0)
    image = ImageOps.exif_transpose(image)
    longest = max(image.size)
    if longest < 1600:
        scale = min(2.5, 1600 / max(1, longest))
        image = image.resize(
            (
                max(1, int(image.width * scale)),
                max(1, int(image.height * scale)),
            )
        )
    image = ImageEnhance.Contrast(image.convert("L")).enhance(1.6)

    temporary_dir = Path.cwd() / "data" / "ocr_temp"
    temporary_dir.mkdir(parents=True, exist_ok=True)
    temporary = temporary_dir / f"{uuid.uuid4().hex}.png"
    try:
        image.save(temporary, format="PNG")
        storage_file = await StorageFile.get_file_from_path_async(str(temporary))
        stream = await storage_file.open_async(FileAccessMode.READ)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        engine = OcrEngine.try_create_from_user_profile_languages()
        if not engine:
            return ""
        result = await engine.recognize_async(bitmap)
        return (result.text or "").strip()
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> None:
    text = ""
    if len(sys.argv) > 1:
        try:
            text = asyncio.run(extract_text(sys.argv[1]))
        except Exception:
            text = ""
    print(json.dumps({"text": text}, ensure_ascii=False))


if __name__ == "__main__":
    main()
