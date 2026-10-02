#!/usr/bin/env python3
"""smell_scan.py — 语言无关的代码坏味道启发式扫描器

用途：在做代码审查/重构前，快速拿到"机械性线索"（长函数、深嵌套、长参数表、
重复代码块、注释掉的代码、魔法数字、空 catch 等），让审查有起点、不漏类别。

重要：本脚本只给线索，不做判断。它输出的每一项都必须由人/模型结合上下文确认
是否真的有害——启发式扫描必然有误报（例如数据文件里的长函数、测试 fixture 里
的魔法数字、为性能故意写的深嵌套）。

用法：
    python3 smell_scan.py <路径> [选项]
    python3 smell_scan.py src/ --json
    python3 smell_scan.py . --ext .py,.ts --max-func-lines 30 --top 40

仅依赖 Python 标准库，Python 3.8+。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict

# ---------------------------------------------------------------- 语言配置

BRACE_EXTS = {
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".kts",
    ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".php",
    ".swift", ".scala", ".dart", ".m", ".mm", ".groovy",
}
INDENT_EXTS = {".py", ".pyi", ".rb", ".coffee", ".jl", ".nim", ".ex", ".exs"}
OTHER_EXTS = {".sh", ".bash", ".zsh", ".sql", ".lua", ".pl", ".r", ".vue", ".svelte"}
DEFAULT_EXTS = BRACE_EXTS | INDENT_EXTS | OTHER_EXTS

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "env", "__pycache__",
    "dist", "build", "target", "out", "bin", "obj", "vendor", "third_party",
    ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".ruff_cache", "coverage",
    "site-packages", ".next", ".nuxt", ".gradle", ".terraform", "Pods",
    "migrations", "generated", "gen",
}

CONTROL_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "else", "do", "try", "return",
    "case", "default", "with", "match", "when", "foreach", "using", "lock",
    "synchronized", "finally", "new", "delete", "sizeof", "assert", "throw",
}

SEVERITY_ORDER = {"Blocker": 0, "Major": 1, "Minor": 2}


@dataclass
class Finding:
    category: str
    severity: str
    file: str
    line: int
    message: str
    detail: str = ""


# ---------------------------------------------------------------- 工具函数

def detect_exts(root: str, explicit: str | None) -> set[str]:
    if explicit:
        exts = set()
        for part in explicit.split(","):
            part = part.strip().lower()
            if not part:
                continue
            exts.add(part if part.startswith(".") else "." + part)
        return exts
    return set(DEFAULT_EXTS)


def iter_source_files(root: str, exts: set[str], max_files: int):
    if os.path.isfile(root):
        yield root
        return
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() not in exts:
                continue
            path = os.path.join(dirpath, name)
            try:
                if os.path.getsize(path) > 2_000_000:
                    continue
            except OSError:
                continue
            yield path
            count += 1
            if count >= max_files:
                return


def read_lines(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except OSError:
        return []


def strip_strings_and_comment(line: str, prefix: str) -> str:
    """粗略去掉字符串与行尾注释，用于括号计数和代码判定（启发式，不追求精确）。"""
    out = []
    i, n = 0, len(line)
    quote = None
    while i < n:
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            if line.startswith("//", i) or line.startswith("#", i):
                break
            quote = ch
            i += 1
            continue
        if prefix == "//" and line.startswith("//", i):
            break
        if prefix == "#" and ch == "#" and not line.startswith("#!", i):
            # 粗暴处理：Python 的 # 只在字符串外才可能是注释
            break
        if prefix == "--" and line.startswith("--", i):
            break
        out.append(ch)
        i += 1
    return "".join(out)


def comment_prefix_for(path: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    if ext in INDENT_EXTS or ext in {".sh", ".bash", ".zsh", ".pl", ".r", ".nim", ".jl", ".yaml", ".yml", ".toml"}:
        return "#" if ext != ".sql" else "--"
    if ext == ".sql" or ext == ".lua":
        return "--"
    if ext in BRACE_EXTS:
        return "//"
    return None


def is_comment_line(line: str, prefix: str | None) -> bool:
    s = line.strip()
    if not s:
        return False
    if prefix == "#":
        return s.startswith("#")
    if prefix == "//":
        return s.startswith("//") or s.startswith("/*") or s.startswith("*") or s.startswith("*/")
    if prefix == "--":
        return s.startswith("--")
    return False


def continuation_flags(lines: list[str], prefix: str | None) -> list[bool]:
    """标记处于未闭合括号内的"续行"。

    跨行调用/字面量的缩进只是排版，不代表逻辑嵌套层级；不排除它们会把
    `foo(\\n    bar,\\n    baz)` 误报成深层嵌套。
    """
    depth, flags = 0, []
    for line in lines:
        flags.append(depth > 0)
        clean = strip_strings_and_comment(line, prefix or "//")
        depth += clean.count("(") + clean.count("[") + clean.count("{")
        depth -= clean.count(")") + clean.count("]") + clean.count("}")
        if depth < 0:
            depth = 0
    return flags


def detect_indent_unit(lines: list[str]) -> int:
    """推断缩进宽度：选最能整除多数缩进量的候选值（2/4/8），避免个别异常缩进把整份文件算深。"""
    indents = []
    for line in lines:
        if not line.strip() or line.lstrip().startswith(("#", "//", "*")):
            continue
        lead = len(line) - len(line.lstrip(" "))
        if 0 < lead <= 24:
            indents.append(lead)
    if not indents:
        return 4
    scores = {}
    for candidate in (2, 3, 4, 8):
        agree = sum(1 for lead in indents if lead % candidate == 0)
        scores[candidate] = agree / len(indents)
    best_score = max(scores.values())
    # 多个候选打平时取较大值（4 空格缩进的缩进量同时能被 2 整除，但 2 会高估嵌套深度）
    fitting = [c for c, sc in scores.items() if sc >= best_score - 0.02]
    return max(fitting)


# ---------------------------------------------------------------- 单项检查

BRACE_FUNC_PATTERNS = [
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*\("),
    re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)\s*\("),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:function\s*\*?\s*)?\(?[^=()]*\)?\s*=>\s*\{"),
    re.compile(r"^\s*(?:public|private|protected|internal|static|final|virtual|override|async|sealed|abstract|open|inline|suspend|synchronized|partial|extern|\s)*[\w<>\[\],\.\?:\*&@\s]*?(\w+)\s*\([^;{}]*\)\s*(?:const|noexcept|override|final|throws\s+[\w\s,\.]+|->\s*[\w<>\[\],\.\?:\*&\s]+)?\s*\{\s*$"),
    re.compile(r"^\s*(?:public|private|protected|internal|static|final|virtual|override|async|sealed|abstract|open|inline)\s+[\w<>\[\],\.\?:\*&@\s]*?(\w+)\s*\([^;{}]*\)\s*$"),
]

INDENT_FUNC_RE = re.compile(r"^(\s*)(?:async\s+)?(?:def|class|module|function)\s+([\w\.]+)")
EMPTY_BRACE_CATCH_RE = re.compile(r"catch\s*(?:\([^)]*\))?\s*\{\s*\}\s*;?\s*$")
TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
MAGIC_RE = re.compile(r"(?<![\w.$])(\d{2,}(?:\.\d+)?)(?![\w.])")
CONST_DEF_RE = re.compile(r"^\s*(?:export\s+)?(?:const|final|static|val|readonly|#define|let)?\s*[A-Z][A-Z0-9_]{2,}\s*[:=]")
CODEISH_RE = re.compile(r"[;{}]\s*$|^\s*(?:if|for|while|return|def|function|import|from|class|const|let|var|public|private|#include|else|elsif|elif)\b|[=()]\s*\S")


def match_brace_function(line: str) -> str | None:
    stripped = strip_strings_and_comment(line, "//")
    for pattern in BRACE_FUNC_PATTERNS:
        m = pattern.match(stripped)
        if m:
            name = m.group(1)
            if name in CONTROL_KEYWORDS:
                return None
            return name
    return None


def count_params(line: str) -> int:
    start = line.find("(")
    if start < 0:
        return 0
    depth, buf = 0, []
    for ch in line[start:]:
        if ch == "(":
            depth += 1
            if depth == 1:
                continue
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        if depth >= 1:
            buf.append(ch)
    inner = "".join(buf).strip()
    if not inner or inner in {"void", ")"}:
        return 0
    depth = 0
    commas = 0
    for ch in inner:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        elif ch == "," and depth == 0:
            commas += 1
    return commas + 1


def scan_file(path: str, rel: str, opts) -> list[Finding]:
    findings: list[Finding] = []
    lines = read_lines(path)
    if not lines:
        return findings
    ext = os.path.splitext(path)[1].lower()
    prefix = comment_prefix_for(path)
    style = "indent" if ext in INDENT_EXTS else "brace"
    continuation = continuation_flags(lines, prefix)

    code_lines = [ln for ln in lines if ln.strip() and not is_comment_line(ln, prefix)]
    loc = len(code_lines)

    # 1) 长文件
    if loc > opts.max_file_lines:
        findings.append(Finding(
            "长文件", "Minor", rel, 1,
            f"有效代码 {loc} 行（阈值 {opts.max_file_lines}）",
            "文件过长通常意味着承担了多个职责，或缺少模块划分",
        ))

    # 2) 长行
    long_lines = [i + 1 for i, ln in enumerate(lines) if len(ln) > opts.max_line_len]
    if long_lines:
        shown = ", ".join(str(x) for x in long_lines[:5])
        findings.append(Finding(
            "过长代码行", "Minor", rel, long_lines[0],
            f"{len(long_lines)} 行超过 {opts.max_line_len} 字符（如 {shown}）",
            "长行通常需要横向滚动，也常是深层嵌套或长表达式的结果",
        ))

    # 3) 注释掉的代码
    run: list[tuple[int, str]] = []
    for i, ln in enumerate(lines, start=1):
        if is_comment_line(ln, prefix):
            run.append((i, ln))
            continue
        if len(run) >= 3:
            body = [t for _, t in run]
            codeish = sum(1 for t in body if CODEISH_RE.search(t))
            if codeish >= max(2, int(len(body) * 0.6)):
                findings.append(Finding(
                    "注释掉的代码", "Major", rel, run[0][0],
                    f"第 {run[0][0]}-{run[-1][0]} 行有 {len(run)} 行疑似被注释的代码",
                    "版本控制已保留历史；注释掉的代码会随代码漂移，是纯负债",
                ))
        run = []
    if len(run) >= 3:
        body = [t for _, t in run]
        if sum(1 for t in body if CODEISH_RE.search(t)) >= max(2, int(len(body) * 0.6)):
            findings.append(Finding(
                "注释掉的代码", "Major", rel, run[0][0],
                f"第 {run[0][0]}-{run[-1][0]} 行有 {len(run)} 行疑似被注释的代码",
                "版本控制已保留历史；注释掉的代码会随代码漂移，是纯负债",
            ))

    # 4) TODO/FIXME
    todos = [(i, ln.strip()) for i, ln in enumerate(lines, start=1) if TODO_RE.search(ln)]
    if todos:
        findings.append(Finding(
            "遗留标记", "Minor", rel, todos[0][0],
            f"{len(todos)} 处 TODO/FIXME/HACK",
            "确认是否有对应跟踪项，避免长期堆积成为「已知但无人处理」的债务",
        ))

    # 5) 空 catch / 裸 except
    for i, ln in enumerate(lines, start=1):
        if EMPTY_BRACE_CATCH_RE.search(strip_strings_and_comment(ln, "//")):
            findings.append(Finding(
                "吞掉异常", "Major", rel, i,
                "空的 catch 块",
                "异常被静默丢弃，故障会以更隐蔽的形式出现在远处；确为有意忽略时补注释说明，否则记录或重新抛出",
            ))
        stripped = ln.strip()
        if style == "indent" and re.match(r"^except\b.*:\s*$", stripped):
            if re.match(r"^except\s*:\s*$", stripped):
                findings.append(Finding(
                    "吞掉异常", "Major", rel, i,
                    "裸 except（捕获所有异常，含 KeyboardInterrupt/SystemExit）",
                    "掩盖真实错误类型，无法区分预期失败与程序缺陷；捕获具体异常类型",
                ))
            for nxt in lines[i:i + 3]:
                if nxt.strip() == "pass":
                    findings.append(Finding(
                        "吞掉异常", "Major", rel, i,
                        "except 分支只写了 pass",
                        "异常被静默吞掉，故障会以更隐蔽的形式出现在远处；确为有意的 best-effort 清理时补注释说明，否则记录或重新抛出",
                    ))
                    break
                if nxt.strip() and not nxt.strip().startswith("#"):
                    break

    # 6) 魔法数字
    magic = []
    for i, ln in enumerate(lines, start=1):
        if is_comment_line(ln, prefix) or CONST_DEF_RE.search(ln):
            continue
        body = strip_strings_and_comment(ln, prefix)
        for m in MAGIC_RE.finditer(body):
            value = m.group(1)
            if value in {"10", "100", "1000", "1024", "0", "1", "2"}:
                continue
            magic.append((i, value))
            break
    if len(magic) >= 3:
        shown = ", ".join(f"{ln}:{v}" for ln, v in magic[:5])
        findings.append(Finding(
            "魔法数字", "Minor", rel, magic[0][0],
            f"{len(magic)} 行含疑似魔法数字（如 {shown}）",
            "含义不明、多处重复、修改易漏；确属常量则用命名常量替代",
        ))

    # 7) 长函数 / 长参数表 / 深嵌套
    if style == "indent":
        indent_unit = detect_indent_unit(lines)
        stack: list[list] = []  # [def 缩进, 名字, 起始行, 函数内最大嵌套深度]
        for i, ln in enumerate(lines, start=1):
            if not ln.strip() or is_comment_line(ln, prefix) or continuation[i - 1]:
                continue
            lead = len(ln) - len(ln.lstrip(" "))
            m = INDENT_FUNC_RE.match(ln)
            while stack and lead <= stack[-1][0]:
                _, name, start, maxd = stack.pop()
                findings.extend(_function_findings(rel, name, start, i - start, opts, maxd))
            if stack:
                rel_depth = (lead - stack[-1][0]) // indent_unit
                if rel_depth > stack[-1][3]:
                    stack[-1][3] = rel_depth
            if m:
                stack.append([lead, m.group(2), i, 0])
                params = count_params(ln)
                if params > opts.max_params:
                    findings.append(Finding(
                        "参数过多", "Major", rel, i,
                        f"函数 {m.group(2)} 有 {params} 个参数（阈值 {opts.max_params}）",
                        "参数顺序易传错、职责过多；考虑参数对象或拆分函数",
                    ))
        end_line = len(lines) + 1
        while stack:
            _, name, start, maxd = stack.pop()
            findings.extend(_function_findings(rel, name, start, end_line - start, opts, maxd))

        # 计算嵌套深度（按缩进层级，相对文件顶层），阈值放宽 3 层以覆盖"文件→类→函数"这类必要包裹
        nest_threshold = opts.max_depth + 3
        worst_depth, worst_line = 0, 0
        for i, ln in enumerate(lines, start=1):
            if not ln.strip() or is_comment_line(ln, prefix) or continuation[i - 1]:
                continue
            lead = len(ln) - len(ln.lstrip(" "))
            depth = (lead // indent_unit) + 1
            if depth > worst_depth:
                worst_depth, worst_line = depth, i
        if worst_depth > nest_threshold:
            findings.append(Finding(
                "深层嵌套", "Major", rel, worst_line,
                f"最大缩进层级约 {worst_depth}（阈值约 {nest_threshold}）",
                "深缩进常来自多层条件与循环，认知负担高；用卫语句、抽函数或提前 continue 化解",
            ))
    else:
        depth = 0
        func_name, func_start, func_depth = None, 0, 0
        max_depth_in_func = 0
        for i, ln in enumerate(lines, start=1):
            clean = strip_strings_and_comment(ln, prefix or "//")
            if func_name is None:
                name = match_brace_function(ln)
                if name:
                    params = count_params(clean)
                    if params > opts.max_params:
                        findings.append(Finding(
                            "参数过多", "Major", rel, i,
                            f"函数 {name} 有 {params} 个参数（阈值 {opts.max_params}）",
                            "参数顺序易传错、职责过多；考虑参数对象或拆分函数",
                        ))
                    if clean.count("{") > 0:
                        func_name, func_start, func_depth = name, i, depth
                        max_depth_in_func = depth + clean.count("{")
            else:
                max_depth_in_func = max(max_depth_in_func, depth + clean.count("{"))
            depth += clean.count("{") - clean.count("}")
            if depth < 0:
                depth = 0
            if func_name is not None and depth <= func_depth:
                length = i - func_start
                findings.extend(_function_findings(rel, func_name, func_start, length, opts, max_depth_in_func - func_depth))
                func_name = None

    return findings


def _function_findings(rel, name, start, length, opts, nest_depth) -> list[Finding]:
    out = []
    if length > opts.max_func_lines:
        out.append(Finding(
            "长函数", "Major", rel, start,
            f"函数 {name} 约 {length} 行（阈值 {opts.max_func_lines}）",
            "通常说明它做了不止一件事；按「能否再抽出一个有意义函数」判断并拆分",
        ))
    if nest_depth is not None and nest_depth > opts.max_depth:
        out.append(Finding(
            "深层嵌套", "Major", rel, start,
            f"函数 {name} 内嵌套深度约 {nest_depth}（阈值 {opts.max_depth}）",
            "深嵌套易漏边界、难读；用卫语句提前返回或抽函数",
        ))
    return out


# ---------------------------------------------------------------- 重复块

def normalize_line(line: str, prefix: str | None) -> str:
    clean = strip_strings_and_comment(line, prefix or "//").strip()
    clean = re.sub(r"\s+", " ", clean)
    return clean


def find_duplicate_blocks(files: list[tuple[str, list[str]]], window: int, max_groups: int) -> list[Finding]:
    buckets: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for rel, lines in files:
        prefix = comment_prefix_for(rel)
        norm = [normalize_line(ln, prefix) for ln in lines]
        for i in range(len(norm) - window + 1):
            chunk = norm[i:i + window]
            if sum(1 for c in chunk if len(c) > 3 and c not in {"{", "}", "};", ")", "});"}) < window:
                continue
            joined = "\n".join(chunk)
            digest = hashlib.md5(joined.encode("utf-8", "replace")).hexdigest()
            buckets[digest].append((rel, i + 1))

    groups = []
    for digest, locations in buckets.items():
        uniq = sorted(set(locations))
        if len(uniq) < 2:
            continue
        spans_files = len({loc[0] for loc in uniq}) > 1
        groups.append((len(uniq), digest, uniq, spans_files))
    groups.sort(key=lambda g: (-g[0], g[2][0][0]))

    out = []
    for count, digest, locations, spans_files in groups[:max_groups]:
        first_file, first_line = locations[0]
        shown = ", ".join(f"{f}:{l}" for f, l in locations[:3])
        out.append(Finding(
            "重复代码块", "Major" if spans_files else "Minor", first_file, first_line,
            f"约 {window} 行的代码块出现 {count} 次（{shown}{' …' if count > 3 else ''}）",
            "重复是「改一处漏一处」的根源；确认是同一概念后抽成共享实现（Rule of Three）",
        ))
    return out


# ---------------------------------------------------------------- 输出

def render_text(findings: list[Finding], scanned_files: int, total_loc: int, top: int | None) -> str:
    lines = []
    lines.append(f"扫描文件 {scanned_files} 个，有效代码约 {total_loc} 行。")
    lines.append("注意：以下是启发式线索，不是结论——每一项都要结合上下文判断是否真的有害。")
    lines.append("")
    if not findings:
        lines.append("未发现机械性坏味道线索。这不代表代码就是整洁的：命名、职责、抽象层级需要人来判断。")
        return "\n".join(lines)

    counts = defaultdict(int)
    for f in findings:
        counts[f.category] += 1
    by_sev = defaultdict(int)
    for f in findings:
        by_sev[f.severity] += 1

    findings = sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.file, f.line))
    if top:
        findings = findings[:top]

    lines.append("类别统计：" + "，".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
    lines.append("严重度统计：" + "，".join(f"{k} {by_sev[k]}" for k in ("Blocker", "Major", "Minor") if by_sev.get(k)))
    lines.append("")
    current_file = None
    for f in findings:
        if f.file != current_file:
            current_file = f.file
            lines.append(f"── {f.file}")
        loc = f":{f.line}" if f.line else ""
        lines.append(f"  [{f.severity}] {f.category}{loc} — {f.message}")
        if f.detail:
            lines.append(f"      {f.detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语言无关的代码坏味道启发式扫描器（只给线索，不做判断）")
    parser.add_argument("path", help="要扫描的文件或目录")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出，便于程序处理")
    parser.add_argument("--ext", default=None, help="限定扩展名，逗号分隔，如 .py,.ts")
    parser.add_argument("--max-file-lines", type=int, default=500)
    parser.add_argument("--max-func-lines", type=int, default=40)
    parser.add_argument("--max-params", type=int, default=3)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--max-line-len", type=int, default=120)
    parser.add_argument("--dup-window", type=int, default=6, help="重复块判定的行窗口")
    parser.add_argument("--top", type=int, default=None, help="最多输出多少条线索")
    parser.add_argument("--max-files", type=int, default=2000)
    opts = parser.parse_args(argv)

    if not os.path.exists(opts.path):
        print(f"路径不存在：{opts.path}", file=sys.stderr)
        return 2

    exts = detect_exts(opts.path, opts.ext)
    root = os.path.abspath(opts.path)

    findings: list[Finding] = []
    cached: list[tuple[str, list[str]]] = []
    total_loc = 0
    scanned = 0
    for path in iter_source_files(opts.path, exts, opts.max_files):
        rel = os.path.relpath(os.path.abspath(path), os.path.dirname(root) if os.path.isfile(root) else root)
        lines = read_lines(path)
        if not lines:
            continue
        scanned += 1
        prefix = comment_prefix_for(path)
        total_loc += sum(1 for ln in lines if ln.strip() and not is_comment_line(ln, prefix))
        cached.append((rel, lines))
        findings.extend(scan_file(path, rel, opts))

    findings.extend(find_duplicate_blocks(cached, opts.dup_window, max_groups=15))

    if opts.json:
        payload = {
            "scanned_files": scanned,
            "total_loc": total_loc,
            "disclaimer": "启发式线索，需人工/模型结合上下文判断，存在误报。",
            "findings": [asdict(f) for f in sorted(
                findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.file, f.line))],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(findings, scanned, total_loc, opts.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
