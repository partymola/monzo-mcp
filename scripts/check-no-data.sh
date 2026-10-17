#!/bin/sh
# Pre-commit hook: reject commits containing database files, config secrets,
# or suspiciously large files that might contain real financial data.
#
# POSIX sh compatible - no bash required.
#
# Install: ln -sf ../../scripts/check-no-data.sh .git/hooks/pre-commit

set -eu

# Byte-wise matching: under a UTF-8 locale, grep treats a name that is not
# valid UTF-8 as binary and reports no match.
LC_ALL=C
export LC_ALL

errors=0

# Paths staged as added, copied, modified, renamed or retyped. A deletion
# removes data, so it is not checked. Read with -z because git otherwise
# quotes an unusual name, and a quoted name matches none of the patterns
# below. Each step is a command of its own so that set -e stops the hook if
# git fails, rather than letting it check an empty list.
_raw=$(mktemp)
_paths=$(mktemp)
trap 'rm -f "$_raw" "$_paths"' EXIT
git diff --cached --name-only -z --diff-filter=ACMRT > "$_raw"
tr '\0' '\n' < "$_raw" > "$_paths"

# A newline inside a name would split it across two lines of the list.
nul_count=$(tr -cd '\0' < "$_raw" | wc -c)
line_count=$(wc -l < "$_paths")
if [ $(($nul_count)) -ne $(($line_count)) ]; then
    echo "ERROR: A staged path contains a newline - rename it before committing."
    exit 1
fi

# Check for database files and any backups thereof. Uses ERE so the patterns
# behave the same under GNU grep and ugrep (default 'grep' on some systems).
# Patterns matched against full staged-file paths:
#   *.db, *.db-journal, *.db-wal, *.db-shm
#   *.db.<anything>      (e.g. monzo.db.bak)
#   *.bak, *.backup      (any backup file, regardless of inner extension)
db_regex='\.db$|\.db-journal$|\.db-wal$|\.db-shm$|\.db\.|\.bak$|\.backup$'
matched=$(grep -E "$db_regex" "$_paths" || true)
if [ -n "$matched" ]; then
    echo "ERROR: Staged file(s) look like database/backup data - must not be committed:"
    echo "$matched" | sed 's/^/  /'
    errors=1
fi

# Check for config secrets. Everything under config/ is rejected except the
# .gitkeep and example files, rather than listing extensions to reject: the
# credential files across these servers include extensionless dotfiles
# (.env, .token-oauth, .token-v2), which an extension pattern misses entirely.
staged_config=$(grep -E '^config/' "$_paths" | grep -vE '(^|/)\.gitkeep$|\.example(\.|$)' || true)
if [ -n "$staged_config" ]; then
    echo "ERROR: Staged file(s) under config/ - credentials and tokens must not be committed:"
    echo "$staged_config" | sed 's/^/  /'
    errors=1
fi

# Check for large files (>100KB) that might be data dumps. ":0:" names the
# index entry explicitly, so a path such as "1:x" is not read as a stage
# number. A size that cannot be read is refused rather than taken as zero.
while IFS= read -r file; do
    # A dependency lockfile is legitimately large and only grows.
    case "$file" in
        uv.lock) continue ;;
    esac
    # A submodule entry points at a commit in another repository, so it has
    # no size to read here.
    if [ "$(git ls-files -s -- ":(literal)$file" | cut -c1-6)" = 160000 ]; then
        continue
    fi
    if ! size=$(git cat-file -s ":0:$file" 2>/dev/null); then
        echo "ERROR: Could not read the staged size of '$file'"
        errors=1
        continue
    fi
    if [ "$size" -gt 102400 ]; then
        echo "ERROR: Staged file '$file' is $(( size / 1024 ))KB (>100KB) - possible data leak"
        errors=1
    fi
done < "$_paths"

if [ "$errors" -ne 0 ]; then
    echo ""
    echo "Commit rejected. Financial data and credentials must never be committed."
    exit 1
fi
