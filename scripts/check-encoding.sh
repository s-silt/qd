#!/usr/bin/env bash
# check-encoding.sh — 检测源码中的编码异常字符
# 用法:
#   作为 pre-commit hook（检查暂存文件）:  ./scripts/check-encoding.sh --staged
#   检查整个仓库:                          ./scripts/check-encoding.sh
#   检查指定文件/目录:                     ./scripts/check-encoding.sh path/to/file.py
#
# 检测字符:
#   U+FFFD  — UTF-8 替换字符（损坏的字节序列）
#   U+200B  — 零宽空格 (ZWSP)
#   U+200C  — 零宽不连字 (ZWNJ)
#   U+200D  — 零宽连字 (ZWJ)
#   U+FEFF  — BOM（出现在非文件首字节位置时）
#   U+00AD  — 软连字符 (SHY)
#   U+2028  — 行分隔符 (LS)
#   U+2029  — 段落分隔符 (PS)
#
# 退出码:
#   0 — 未发现异常
#   1 — 发现异常字符（已输出 file:line: issue 格式）

set -euo pipefail

# ---- 颜色输出 ----
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# ---- 文件扩展名白名单 ----
EXTENSIONS='py|md|html|go|css|js|yml|yaml|json|txt|sh|toml|cfg|ini|rst'

# ---- 排除路径（glob 前缀） ----
EXCLUDE_PATTERNS=(
    'web/static/components/'
    '.git/'
    'node_modules/'
    '__pycache__/'
    '.mypy_cache/'
    '.pytest_cache/'
)

# ---- 排除文件名后缀 ----
EXCLUDE_SUFFIXES=(
    '.min.js'
    '.min.css'
)

found_issues=0

should_skip() {
    local file="$1"
    # 检查排除路径
    for pat in "${EXCLUDE_PATTERNS[@]}"; do
        case "$file" in
            *"$pat"*) return 0 ;;
        esac
    done
    # 检查排除后缀
    for suf in "${EXCLUDE_SUFFIXES[@]}"; do
        case "$file" in
            *"$suf") return 0 ;;
        esac
    done
    return 1
}

check_file() {
    local file="$1"

    # 跳过不存在或不可读文件（可能已从暂存区删除）
    [[ -f "$file" && -r "$file" ]] || return 0

    # 检查扩展名
    local ext="${file##*.}"
    if ! echo "$ext" | grep -qE "^($EXTENSIONS)$"; then
        return 0
    fi

    if should_skip "$file"; then
        return 0
    fi

    # 用 Python 检测（避免 grep Unicode 行为差异）
    python3 - "$file" << 'PYEOF'
import sys, re

CHECKS = [
    ('�', 'U+FFFD (UTF-8 replacement character — corrupted bytes)'),
    ('​', 'U+200B (zero-width space)'),
    ('‌', 'U+200C (zero-width non-joiner)'),
    ('‍', 'U+200D (zero-width joiner)'),
    ('­', 'U+00AD (soft hyphen)'),
    (' ', 'U+2028 (line separator)'),
    (' ', 'U+2029 (paragraph separator)'),
]

filepath = sys.argv[1]
try:
    with open(filepath, encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
except Exception as e:
    print(f"WARNING: cannot read {filepath}: {e}", file=sys.stderr)
    sys.exit(0)

issues = 0
for lineno, line in enumerate(lines, start=1):
    for char, desc in CHECKS:
        if char in line:
            print(f"{filepath}:{lineno}: {desc}")
            issues += 1

# Special: U+FEFF (BOM) only problematic when not at byte 0
try:
    with open(filepath, 'rb') as f:
        raw = f.read()
    bom = b'\xef\xbb\xbf'
    pos = 0
    while True:
        idx = raw.find(bom, pos)
        if idx < 0:
            break
        if idx != 0:
            # Find approximate line number
            lineno = raw[:idx].count(b'\n') + 1
            print(f"{filepath}:{lineno}: U+FEFF (BOM mid-file — unexpected byte order mark)")
            issues += 1
        pos = idx + 1
except Exception:
    pass

sys.exit(1 if issues else 0)
PYEOF
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        found_issues=$((found_issues + 1))
    fi
}

# ---- 主逻辑 ----

MODE="${1:-}"

if [[ "$MODE" == "--staged" ]]; then
    # 只检查 Git 暂存区中的文件
    mapfile -t staged_files < <(git diff --cached --name-only --diff-filter=ACM 2>/dev/null || true)
    if [[ ${#staged_files[@]} -eq 0 ]]; then
        exit 0
    fi
    for f in "${staged_files[@]}"; do
        check_file "$f"
    done
elif [[ -n "$MODE" && "$MODE" != "--staged" ]]; then
    # 检查指定路径
    if [[ -d "$MODE" ]]; then
        while IFS= read -r -d '' f; do
            check_file "$f"
        done < <(find "$MODE" -type f -print0)
    else
        check_file "$MODE"
    fi
else
    # 检查整个仓库（跟踪文件 + 未跟踪但未忽略文件）
    while IFS= read -r -d '' f; do
        check_file "$f"
    done < <(git ls-files -z 2>/dev/null || find . -type f -print0)
fi

if [[ $found_issues -gt 0 ]]; then
    echo -e "${RED}[check-encoding] 发现 ${found_issues} 个文件含编码异常字符，请修复后再提交。${NC}" >&2
    echo -e "${YELLOW}提示: 用 'python3 -c \"open(f).read()\"' 或编辑器搜索 U+FFFD 定位问题位置。${NC}" >&2
    exit 1
fi

exit 0
