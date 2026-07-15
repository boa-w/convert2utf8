# convert2utf8 — 中文源码批量转 UTF-8 工具

把 GBK / GB2312 / GB18030 等中文编码的源码（`.c` `.h` `.s` `.txt` `.ini` `.py` 等）
安全、无损地转换为 UTF-8。面向嵌入式 C / Keil 工程做了针对性优化。

## 为什么需要它

Keil 等老工具链默认以 GBK/GB2312 保存含中文注释的源码。在 Git、VS Code、
跨平台协作时经常乱码。本工具批量把源码统一成 UTF-8，且**不破坏原文件**。

## 核心特性

- **无损、可回退**：转换前自动生成 `<文件>.bak` 备份；写入采用「临时文件 + `os.replace`
  原子替换」，即使写入中途失败也不会损坏原文件。
- **高可靠识别**：优先严格尝试 `UTF-8` → `GB18030`（向下兼容 GBK/GB2312）；
  第三方库（chardet / charset-normalizer）仅作辅助，不轻信其低置信度或被误判的
  单字节编码结果。对中文工程比纯 chardet 更稳。
- **安全护栏**：自动跳过 `.git` 等版本库目录与疑似二进制文件（扩展名 + NUL/控制字符启发式），
  避免破坏版本库或二进制资源。
- **幂等**：已经是 UTF-8 的文件直接跳过，可反复运行。
- **零依赖**：仅用标准库即可运行。

## 用法

```bash
# 转换整个目录（递归，默认行为）
python convert2utf8.py "D:/project/src"

# 仅转换单个文件
python convert2utf8.py "D:/project/src/main.c"

# 试运行：只报告将要做什么，不改动任何文件
python convert2utf8.py "D:/project/src" --dry-run

# 去除 UTF-8 BOM（把带 BOM 的 UTF-8 重写为无 BOM 的 UTF-8）
python convert2utf8.py "D:/project/src" --strip-bom

# 自定义扩展名 / 不备份 / 输出日志
python convert2utf8.py "D:/project/src" -e c,h,s,txt,ini --no-backup --log conv.log
```

## 命令行参数

| 参数 | 说明 |
| --- | --- |
| `path` | 目标文件或目录路径（必填） |
| `-e, --ext` | 逗号分隔的扩展名白名单（含点），默认覆盖常见源码类型 |
| `-r/--recursive` | 递归处理子目录（默认开启） |
| `--no-recursive` | 仅处理目录顶层 |
| `-n, --no-backup` | 不创建 `.bak` 备份（默认会备份） |
| `-d, --dry-run` | 只报告，不修改任何文件 |
| `-b, --strip-bom` | 把带 BOM 的 UTF-8 重写为无 BOM 的 UTF-8 |
| `--convert-all` | 对所有可解码但非 UTF-8 的编码都转换（含第三方库识别的编码） |
| `-t, --target` | 目标编码（默认 `utf-8`） |
| `--max-size` | 超过该字节数的文件跳过（默认 50 MB） |
| `--log` | 把逐文件操作日志写入指定文件 |

## 默认处理的扩展名

`.c .h .s .asm .inc .cpp .cc .cxx .hpp .hh .txt .md .py .ini .cfg .conf
.csv .xml .json .ld .sct .mak .mk .bat .cmd .sh .template`

可直接用 `-e` 覆盖。二进制扩展名（`.lib .o .bin .elf .png ...`）会被自动跳过。

## 备份与回退

- 每次转换会生成 `<原名>.bak`；若已存在则顺延为 `.bak1` `.bak2`。
- 回退某文件：`copy <文件>.bak <文件>` 即可恢复原编码内容。

## 依赖

仅标准库即可运行。可选安装 `chardet` 或 `charset-normalizer` 增强辅助探测：

```bash
pip install chardet
```
