#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
convert2utf8.py - 将中文源码（GBK / GB2312 / GB18030 等）批量转换为 UTF-8。

设计目标
--------
- 无损、可回退：转换前自动备份；写入使用「临时文件 + os.replace」原子替换，
  即使写入失败也不会损坏原文件。
- 高可靠编码识别：优先严格尝试 UTF-8 / UTF-8-BOM，再严格尝试 GB18030
  （向下兼容 GBK / GB2312），最后才参考 chardet / charset-normalizer
  （仅作辅助，不轻信其低置信度或被误判的单字节编码结果）。
- 安全：自动跳过 .git 等版本库目录与疑似二进制文件，避免破坏版本库或二进制资源。
- 幂等：已经是 UTF-8 的文件直接跳过；重复运行不会造成二次改动。

用法
----
    # 转换整个目录（递归，默认行为）
    python convert2utf8.py "D:/project/src"

    # 仅转换单个文件
    python convert2utf8.py "D:/project/src/main.c"

    # 试运行（只报告，不改动任何文件）
    python convert2utf8.py "D:/project/src" --dry-run

    # 去除 UTF-8 BOM（把带 BOM 的 UTF-8 重写为无 BOM 的 UTF-8）
    python convert2utf8.py "D:/project/src" --strip-bom

    # 自定义扩展名 / 不备份 / 输出日志
    python convert2utf8.py "D:/project/src" -e c,h,s,txt,ini --no-backup --log conv.log

依赖
----
- 核心功能零三方依赖（仅用标准库即可工作，对中文工程反而更可靠）。
- 可选：安装 `chardet` 或 `charset-normalizer` 可增强辅助探测能力，缺失时自动降级。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# 可选依赖：编码探测库。缺失时退化为「严格解码优先」策略，对中文工程反而更可靠。
# ---------------------------------------------------------------------------
try:  # pragma: no cover - 环境相关
    import chardet as _chardet_mod  # type: ignore
    _HAS_CHARDET = True
except Exception:  # pragma: no cover
    _chardet_mod = None
    _HAS_CHARDET = False

try:  # pragma: no cover - 环境相关
    import charset_normalizer as _cn_mod  # type: ignore
    _HAS_CN = True
except Exception:  # pragma: no cover
    _cn_mod = None
    _HAS_CN = False


# ---------------------------------------------------------------------------
# 扩展名 / 目录配置
# ---------------------------------------------------------------------------

# 默认当作「文本源码」处理的扩展名（面向嵌入式 C / Keil 工程）。
DEFAULT_TEXT_EXTENSIONS = {
    ".c", ".h", ".s", ".asm", ".inc",
    ".cpp", ".cc", ".cxx", ".hpp", ".hh",
    ".txt", ".md", ".py", ".ini", ".cfg", ".conf",
    ".csv", ".xml", ".json", ".ld", ".sct", ".mak", ".mk",
    ".bat", ".cmd", ".sh", ".template",
}

# 这些目录整棵跳过（版本库 / 依赖 / 缓存），绝不触碰。
SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".idea", ".vscode"}

# 不需要解码探测、直接按二进制跳过的扩展名。
BINARY_EXTENSIONS = {
    ".lib", ".o", ".obj", ".a", ".so", ".dll", ".exe",
    ".bin", ".elf", ".hex", ".png", ".jpg", ".jpeg", ".gif",
    ".zip", ".rar", ".7z", ".pdf", ".doc", ".docx", ".xls", ".xlsx",
}

# 体积超过该值的文件直接跳过，避免误吞大二进制 / 卡顿（默认 50 MB）。
DEFAULT_MAX_SIZE = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# 编码识别核心
# ---------------------------------------------------------------------------

# 常见中文编码候选（用于解析阶段兜底）
CHINESE_ENCODINGS = ["gb18030", "gbk", "gb2312"]

# 优先严格尝试的通用编码链
STRICT_CANDIDATES = ["utf-8-sig", "utf-8", "gb18030"]

# 兜底尝试的其他编码
FALLBACK_CANDIDATES = [
    "gbk", "gb2312", "big5",
    "utf-16", "utf-16-le", "utf-16-be",
    "shift_jis", "euc-jp", "euc-kr",
]


def normalize_encoding_name(encoding: Optional[str]) -> Optional[str]:
    """把探测到的编码名规整为内部统一名称。"""
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
        "us-ascii": "utf-8",
        "big5": "big5",
        "big5-hkscs": "big5",
        "shift_jis": "shift_jis",
        "sjis": "shift_jis",
        "euc-jp": "euc-jp",
        "euc-kr": "euc-kr",
        "iso-2022-jp": "iso-2022-jp",
        "utf-16": "utf-16",
        "utf-16-le": "utf-16-le",
        "utf-16-be": "utf-16-be",
        "utf-32": "utf-32",
    }

    return aliases.get(enc, enc)


def detect_with_library(raw: bytes):
    """用第三方库辅助探测，返回 (encoding, confidence)。优先 chardet，其次 charset_normalizer。"""
    if _HAS_CHARDET and _chardet_mod is not None:
        try:
            result = _chardet_mod.detect(raw)
            enc = result.get("encoding")
            conf = float(result.get("confidence") or 0.0)
            if enc:
                return enc, conf
        except Exception:
            pass

    if _HAS_CN and _cn_mod is not None:
        try:
            matches = _cn_mod.from_bytes(raw)
            best = matches.best()
            if best is not None:
                return str(best.encoding), 1.0
        except Exception:
            pass

    return None, 0.0


def try_decode(raw: bytes, encoding: str) -> Optional[str]:
    """严格解码；成功返回文本，失败返回 None。"""
    try:
        return raw.decode(encoding, errors="strict")
    except (UnicodeDecodeError, LookupError):
        return None


def is_suspicious_single_byte(encoding: Optional[str], confidence: float) -> bool:
    """
    判断第三方库是否给出了可疑的单字节编码。
    GBK / GB2312 文件经常被误判为 cp125x / windows-125x / ISO-8859-x / latin-x。
    """
    if not encoding:
        return True

    enc = encoding.lower().replace("_", "-")
    suspicious_prefixes = ("cp125", "windows-125", "iso-8859", "latin", "mac")

    if enc.startswith(suspicious_prefixes):
        return True

    # 低置信度结果不可信
    if confidence < 0.60:
        return True

    return False


def resolve_encoding(raw: bytes):
    """
    返回 (encoding, text, reason)。

    策略：
    1. 空文件按 UTF-8 处理
    2. UTF-8 BOM 优先（用 utf-8-sig 解码，自动去 BOM）
    3. 严格尝试 UTF-8
    4. 严格尝试 GB18030（覆盖 GB2312 / GBK / GB18030）
    5. 第三方库只作辅助：可疑/低置信度的单字节编码不采信
    6. 最后兜底尝试其它常见编码
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

    # 3. 中文工程优先严格尝试 GB18030
    text = try_decode(raw, "gb18030")
    if text is not None:
        return "gb18030", text, "strict gb18030 (covers gbk/gb2312)"

    # 4. 第三方库辅助（仅在可信时采纳）
    detected_encoding, confidence = detect_with_library(raw)
    normalized = normalize_encoding_name(detected_encoding)

    if normalized and not is_suspicious_single_byte(detected_encoding, confidence):
        text = try_decode(raw, normalized)
        if text is not None:
            return normalized, text, f"trusted detector: {detected_encoding} (conf={confidence:.2f})"

    # 5. 兜底严格解码
    for encoding in FALLBACK_CANDIDATES:
        text = try_decode(raw, encoding)
        if text is not None:
            note = ""
            if detected_encoding:
                note = f" (detector suggested {detected_encoding}, conf={confidence:.2f})"
            return encoding, text, "fallback strict decode" + note

    return None, None, f"unable to decode (detector={detected_encoding}, conf={confidence:.2f})"


def looks_binary(raw: bytes) -> bool:
    """启发式判断是否为二进制文件，避免在文本扩展名误命中时破坏二进制。"""
    if not raw:
        return False

    sample = raw[:8192]
    if b"\x00" in sample:
        return True

    # 统计控制字符（换行/制表/回车之外的不可打印字符）占比
    nontext = 0
    for b in sample:
        if b < 0x09 or (0x0E <= b <= 0x1F) or b == 0x7F:
            nontext += 1
    if sample and nontext / len(sample) > 0.05:
        return True

    return False


# ---------------------------------------------------------------------------
# 转换决策
# ---------------------------------------------------------------------------

# 明确需要转换的编码集合
CONVERTIBLE_ENCODINGS = {
    "gb18030", "gbk", "gb2312", "windows-936", "cp936",
    "big5", "big5-hkscs",
    "shift_jis", "euc-jp", "euc-kr", "iso-2022-jp",
    "utf-16", "utf-16-le", "utf-16-be", "utf-32",
}


def needs_conversion(encoding: Optional[str], strip_bom: bool, convert_all: bool) -> bool:
    """
    判断是否需要（以及允许）转换成目标 UTF-8。

    - utf-8 / ascii：无需转换
    - utf-8-sig：仅当 strip_bom=True 时才重写为无 BOM 的 UTF-8
    - 已知可转换编码：转换
    - 其它：默认不自动转换（避免误伤）；convert_all=True 时强制转换
    """
    enc = (encoding or "").lower().replace("_", "-")

    if enc in {"utf-8", "ascii"}:
        return False

    if enc == "utf-8-sig":
        return strip_bom

    if enc in CONVERTIBLE_ENCODINGS:
        return True

    if convert_all:
        return True

    return False


# ---------------------------------------------------------------------------
# 结果记录
# ---------------------------------------------------------------------------

@dataclass
class FileResult:
    path: Path
    status: str  # converted | skipped_utf8 | skipped_unsupported | skipped_binary | skipped_large | failed | error
    source_encoding: Optional[str] = None
    reason: str = ""
    backup: Optional[Path] = None


STATUS_LABELS = {
    "converted": "已转换",
    "skipped_utf8": "已是 UTF-8（跳过）",
    "skipped_unsupported": "不支持的编码（跳过）",
    "skipped_binary": "疑似二进制（跳过）",
    "skipped_large": "文件过大（跳过）",
    "failed": "转换失败",
    "error": "读取错误",
}

STATUS_ICONS = {
    "converted": "✅",
    "skipped_utf8": "⏩",
    "skipped_unsupported": "⚠️ ",
    "skipped_binary": "⛔",
    "skipped_large": "📦",
    "failed": "❌",
    "error": "❌",
}


# ---------------------------------------------------------------------------
# 单文件转换
# ---------------------------------------------------------------------------

def make_backup_path(path: Path) -> Path:
    """生成不覆盖已有备份的备份路径（.bak / .bak1 / .bak2 ...）。"""
    base = path.with_suffix(path.suffix + ".bak")
    if not base.exists():
        return base
    i = 1
    while True:
        cand = path.with_suffix(path.suffix + f".bak{i}")
        if not cand.exists():
            return cand
        i += 1


def convert_one(
    path: Path,
    *,
    make_backup: bool,
    strip_bom: bool,
    convert_all: bool,
    target_encoding: str,
    dry_run: bool,
    max_size: int,
    log_lines: list,
) -> FileResult:
    # 读取
    try:
        raw = path.read_bytes()
    except Exception as e:  # noqa: BLE001
        res = FileResult(path, "error", reason=f"read error: {e}")
        _record(res, log_lines)
        return res

    # 体积
    if len(raw) > max_size:
        res = FileResult(path, "skipped_large", reason=f"{len(raw)} > {max_size} bytes")
        _record(res, log_lines)
        return res

    # 二进制防护
    if looks_binary(raw):
        res = FileResult(path, "skipped_binary", reason="contains NUL/control bytes")
        _record(res, log_lines)
        return res

    # 识别编码
    encoding, text, reason = resolve_encoding(raw)
    if not encoding:
        res = FileResult(path, "failed", reason=reason)
        _record(res, log_lines)
        return res

    # 是否需要转换
    if not needs_conversion(encoding, strip_bom, convert_all):
        already = "utf-8" in (encoding or "").replace("_", "-") or (encoding or "").lower() == "ascii"
        status = "skipped_utf8" if already else "skipped_unsupported"
        res = FileResult(path, status, source_encoding=encoding, reason=reason)
        _record(res, log_lines)
        return res

    # 试运行：只报告
    if dry_run:
        res = FileResult(path, "converted", source_encoding=encoding,
                         reason=reason + " [dry-run, not written]")
        _record(res, log_lines)
        return res

    # 备份
    backup_path: Optional[Path] = None
    if make_backup:
        try:
            backup_path = make_backup_path(path)
            shutil.copy2(path, backup_path)
        except Exception as e:  # noqa: BLE001
            res = FileResult(path, "failed", source_encoding=encoding,
                             reason=f"backup failed: {e}")
            _record(res, log_lines)
            return res

    # 原子写入：先写临时文件，再 os.replace 替换原文件
    tmp_path: Optional[str] = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding=target_encoding, newline="") as f:
            f.write(text)
        os.replace(tmp_path, path)  # 同文件系统内原子替换
        tmp_path = None
    except Exception as e:  # noqa: BLE001
        if tmp_path and Path(tmp_path).exists():
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        res = FileResult(path, "failed", source_encoding=encoding,
                         reason=f"write failed: {e}")
        _record(res, log_lines)
        return res

    res = FileResult(path, "converted", source_encoding=encoding,
                     backup=backup_path, reason=reason)
    _record(res, log_lines)
    return res


def _record(res: FileResult, log_lines: list) -> None:
    icon = STATUS_ICONS.get(res.status, "·")
    enc = res.source_encoding or "-"
    bak = f" [backup: {res.backup.name}]" if res.backup else ""
    line = f"{icon} {res.path} | {enc} | {res.reason}{bak}"
    log_lines.append(line)
    print(line)


# ---------------------------------------------------------------------------
# 批量遍历
# ---------------------------------------------------------------------------

def iter_target_files(folder: Path, recursive: bool, extensions: set) -> list:
    results: list = []
    if recursive:
        for root, dirs, files in os.walk(folder):
            # 原地剪枝，跳过版本库 / 依赖目录
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                p = Path(root) / name
                if p.suffix.lower() in extensions:
                    results.append(p)
    else:
        for p in sorted(folder.iterdir()):
            if p.is_file() and p.suffix.lower() in extensions:
                results.append(p)
    return results


def batch_convert(
    target: Path,
    *,
    make_backup: bool = True,
    recursive: bool = True,
    extensions: set = None,
    strip_bom: bool = False,
    convert_all: bool = False,
    target_encoding: str = "utf-8",
    dry_run: bool = False,
    max_size: int = DEFAULT_MAX_SIZE,
    log_file: Optional[str] = None,
) -> dict:
    extensions = extensions or DEFAULT_TEXT_EXTENSIONS
    log_lines: list = []

    if target.is_file():
        files = [target]
    elif target.is_dir():
        files = iter_target_files(target, recursive, extensions)
    else:
        print(f"❌ 路径不存在或无法访问：{target}")
        return {}

    counts: dict = {k: 0 for k in STATUS_LABELS}
    converted_encodings: dict = {}

    for path in files:
        if path.suffix.lower() in BINARY_EXTENSIONS:
            res = FileResult(path, "skipped_binary", reason="binary extension")
            _record(res, log_lines)
            counts[res.status] += 1
            continue

        res = convert_one(
            path,
            make_backup=make_backup,
            strip_bom=strip_bom,
            convert_all=convert_all,
            target_encoding=target_encoding,
            dry_run=dry_run,
            max_size=max_size,
            log_lines=log_lines,
        )
        counts[res.status] = counts.get(res.status, 0) + 1
        if res.status == "converted" and res.source_encoding:
            converted_encodings[res.source_encoding] = (
                converted_encodings.get(res.source_encoding, 0) + 1
            )

    # 汇总
    print()
    print("=" * 48)
    print("汇总")
    print("=" * 48)
    print(f"扫描文件数 : {len(files)}")
    for status, label in STATUS_LABELS.items():
        c = counts.get(status, 0)
        if c:
            print(f"  {label:<18}: {c}")
    if converted_encodings:
        print("-" * 48)
        print("转换来源编码分布：")
        for enc, c in sorted(converted_encodings.items(), key=lambda x: -x[1]):
            print(f"  {enc:<14}: {c}")
    print("=" * 48)

    if log_file:
        try:
            Path(log_file).write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            print(f"日志已写入：{log_file}")
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 日志写入失败：{e}")

    return counts


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将中文源码批量转换为 UTF-8（GBK/GB2312/GB18030 -> UTF-8）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python convert2utf8.py D:/project/src\n"
            "  python convert2utf8.py D:/project/src --dry-run\n"
            "  python convert2utf8.py D:/project/src --strip-bom\n"
            "  python convert2utf8.py file.c --no-backup\n"
        ),
    )
    parser.add_argument("path", help="目标文件或目录路径")
    parser.add_argument("-e", "--ext", default=",".join(sorted(DEFAULT_TEXT_EXTENSIONS)),
                        help="逗号分隔的扩展名白名单（含点，如 c,h,s,txt）；默认覆盖常见源码类型")
    parser.add_argument("-r", "--recursive", action="store_true", default=True,
                        help="递归处理子目录（默认开启；对单文件无影响）")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false",
                        help="仅处理目录顶层")
    parser.add_argument("-n", "--no-backup", action="store_true",
                        help="不创建 .bak 备份（默认会备份）")
    parser.add_argument("-d", "--dry-run", action="store_true",
                        help="只报告将要执行的操作，不修改任何文件")
    parser.add_argument("-b", "--strip-bom", action="store_true",
                        help="把带 BOM 的 UTF-8 重写为无 BOM 的 UTF-8")
    parser.add_argument("--convert-all", action="store_true",
                        help="对所有可解码但非 UTF-8 的编码都转换（含第三方库识别的编码）")
    parser.add_argument("-t", "--target", default="utf-8",
                        help="目标编码（默认 utf-8）")
    parser.add_argument("--max-size", type=int, default=DEFAULT_MAX_SIZE,
                        help=f"超过该字节数的文件跳过（默认 {DEFAULT_MAX_SIZE}）")
    parser.add_argument("--log", default=None,
                        help="把逐文件操作日志写入指定文件")
    parser.add_argument("--no-color", action="store_true",
                        help="（预留）禁用彩色输出")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    target = Path(args.path).expanduser().resolve()
    exts = {e if e.startswith(".") else "." + e for e in args.ext.split(",") if e.strip()}

    print(f"目标 : {target}")
    print(f"扩展名: {', '.join(sorted(exts))}")
    print(f"备份 : {'否' if args.no_backup else '是'} | "
          f"递归: {'是' if args.recursive else '否'} | "
          f"去BOM: {'是' if args.strip_bom else '否'} | "
          f"试运行: {'是' if args.dry_run else '否'}")
    print("-" * 48)

    batch_convert(
        target,
        make_backup=not args.no_backup,
        recursive=args.recursive,
        extensions=exts,
        strip_bom=args.strip_bom,
        convert_all=args.convert_all,
        target_encoding=args.target,
        dry_run=args.dry_run,
        max_size=args.max_size,
        log_file=args.log,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
