# safe_convert_encoding.py
import os
import shutil
from pathlib import Path

import chardet


TEXT_EXTENSIONS = {".txt", ".c", ".h"}

# 常见中文编码候选
CHINESE_ENCODINGS = [
    "gb18030",  # 兼容 GB2312 / GBK / GB18030
    "gbk",
    "gb2312",
]

# 优先尝试的通用编码
COMMON_ENCODINGS = [
    "utf-8-sig",
    "utf-8",
    "gb18030",
    "gbk",
    "gb2312",
    "big5",
]


def read_bytes(file_path: Path) -> bytes:
    with open(file_path, "rb") as f:
        return f.read()


def has_utf8_bom(raw: bytes) -> bool:
    return raw.startswith(b"\xef\xbb\xbf")


def detect_by_chardet(raw: bytes):
    """
    使用 chardet 作为辅助判断。
    注意：chardet 的结果不能完全信任，尤其是 GBK / GB2312 / GB18030。
    """
    result = chardet.detect(raw)
    encoding = result.get("encoding")
    confidence = result.get("confidence", 0) or 0
    return encoding, confidence


def normalize_encoding_name(encoding: str | None) -> str | None:
    if not encoding:
        return None

    enc = encoding.lower().replace("_", "-")

    aliases = {
        "gb2312": "gb18030",
        "gbk": "gb18030",
        "gb18030": "gb18030",
        "windows-936": "gb18030",
        "cp936": "gb18030",
        "ansi": "gb18030",
        "utf-8": "utf-8",
        "utf-8-sig": "utf-8-sig",
        "ascii": "utf-8",
        "big5": "big5",
    }

    return aliases.get(enc, enc)


def try_decode(raw: bytes, encoding: str):
    """
    严格解码。
    成功返回文本，失败返回 None。
    """
    try:
        return raw.decode(encoding, errors="strict")
    except UnicodeDecodeError:
        return None
    except LookupError:
        return None


def is_suspicious_single_byte_encoding(encoding: str | None, confidence: float) -> bool:
    """
    判断 chardet 是否给出了可疑的单字节编码。
    GBK/GB2312 文件经常被误判为 cp1250 / windows-125x / ISO-8859-x。
    """
    if not encoding:
        return True

    enc = encoding.lower().replace("_", "-")

    suspicious_prefixes = (
        "cp125",
        "windows-125",
        "iso-8859",
        "latin",
        "mac",
    )

    if enc.startswith(suspicious_prefixes):
        return True

    # 低置信度结果不可信
    if confidence < 0.60:
        return True

    return False

def resolve_encoding(raw: bytes):
    """
    返回: (encoding, text, reason)

    核心策略：
    1. 空文件按 UTF-8 处理
    2. UTF-8 BOM 优先
    3. 严格尝试 UTF-8
    4. 严格尝试 GB18030，覆盖 GB2312 / GBK / GB18030
    5. chardet 只作为辅助，低置信度 cp125x / ISO-8859-x 不采信
    6. 最后再尝试其他编码
    """

    if not raw:
        return "utf-8", "", "empty file"

    # 1. UTF-8 BOM
    if raw.startswith(b"\xef\xbb\xbf"):
        text = try_decode(raw, "utf-8-sig")
        if text is not None:
            return "utf-8-sig", text, "utf-8 bom"

    # 2. 严格 UTF-8
    text = try_decode(raw, "utf-8")
    if text is not None:
        return "utf-8", text, "strict utf-8"

    # 3. 对中文工程优先尝试 GB18030
    # GB18030 向下兼容 GBK / GB2312
    text = try_decode(raw, "gb18030")
    if text is not None:
        return "gb18030", text, "strict gb18030 fallback for Chinese source files"

    # 4. chardet 辅助判断
    detected_encoding, confidence = detect_by_chardet(raw)
    normalized = normalize_encoding_name(detected_encoding)

    if not is_suspicious_single_byte_encoding(detected_encoding, confidence):
        text = try_decode(raw, normalized)
        if text is not None:
            return normalized, text, f"trusted chardet: {detected_encoding}, confidence={confidence:.2f}"

    # 5. 兜底尝试其他常见编码
    # 注意：这里不要再把 cp1250 放前面
    fallback_encodings = [
        "gbk",
        "gb2312",
        "big5",
        "utf-16",
        "utf-16-le",
        "utf-16-be",
    ]

    for encoding in fallback_encodings:
        text = try_decode(raw, encoding)
        if text is not None:
            return encoding, text, f"fallback strict decode, chardet={detected_encoding}, confidence={confidence:.2f}"

    return None, None, f"unable to decode, chardet={detected_encoding}, confidence={confidence:.2f}"


def should_convert_to_utf8(source_encoding: str) -> bool:
    """
    判断是否需要转换成 UTF-8。
    """

    enc = source_encoding.lower().replace("_", "-")

    if enc in {"utf-8", "utf-8-sig", "ascii"}:
        return False

    if enc in {
        "gb18030",
        "gbk",
        "gb2312",
        "windows-936",
        "cp936",
    }:
        return True

    # 其他编码默认不自动转换，避免误伤
    return False


def backup_file(file_path: Path) -> Path:
    backup_path = file_path.with_suffix(file_path.suffix + ".bak")

    index = 1
    while backup_path.exists():
        backup_path = file_path.with_suffix(file_path.suffix + f".bak{index}")
        index += 1

    shutil.copy2(file_path, backup_path)
    return backup_path


def convert_file_to_utf8(file_path: Path, make_backup: bool = True) -> bool:
    raw = read_bytes(file_path)
    encoding, text, reason = resolve_encoding(raw)

    if not encoding:
        print(f"❌ 无法解析编码：{file_path} ({reason})")
        return False

    if not should_convert_to_utf8(encoding):
        print(f"⏩ 跳过：{file_path}，编码={encoding}，原因={reason}")
        return False

    try:
        if make_backup:
            backup_path = backup_file(file_path)
            print(f"📦 已备份：{backup_path}")

        with open(file_path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

        print(f"✅ 转换成功：{file_path} ({encoding} → utf-8)，原因={reason}")
        return True

    except Exception as e:
        print(f"❌ 转换失败：{file_path} ({e})")
        return False


def batch_convert(folder_path: str, make_backup: bool = True):
    folder = Path(folder_path).expanduser().resolve()

    if not folder.exists():
        print(f"❌ 路径不存在：{folder}")
        return

    if not folder.is_dir():
        print(f"❌ 不是文件夹：{folder}")
        return

    total = 0
    converted = 0
    skipped = 0
    failed = 0

    for root, _, files in os.walk(folder):
        for name in files:
            path = Path(root) / name

            if path.suffix.lower() not in TEXT_EXTENSIONS:
                continue

            total += 1
            ok = convert_file_to_utf8(path, make_backup=make_backup)

            if ok:
                converted += 1
            else:
                skipped += 1

    print()
    print("====== 汇总 ======")
    print(f"扫描文件数：{total}")
    print(f"转换成功数：{converted}")
    print(f"跳过/未转换：{skipped}")
    print(f"失败数：{failed}")


if __name__ == "__main__":
    folder = input("请输入文件夹路径：").strip().strip('"')
    batch_convert(folder, make_backup=True)