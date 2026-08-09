#!/bin/sh
# Pre-commit hook: reject commits containing database files, config secrets,
# or suspiciously large files that might contain real financial data.
#
# POSIX sh compatible - no bash required.
#
# Install: ln -sf ../../scripts/check-no-data.sh .git/hooks/pre-commit

set -eu

errors=0

# Check for database files and any backups thereof. Uses ERE so the patterns
# behave the same under GNU grep and ugrep (default 'grep' on some systems).
# Patterns matched against full staged-file paths:
#   *.db, *.db-journal, *.db-wal, *.db-shm
#   *.db.<anything>      (e.g. monzo.db.bak)
#   *.bak, *.backup      (any backup file, regardless of inner extension)
db_regex='\.db$|\.db-journal$|\.db-wal$|\.db-shm$|\.db\.|\.bak$|\.backup$'
matched=$(git diff --cached --name-only | grep -E "$db_regex" || true)
if [ -n "$matched" ]; then
    echo "ERROR: Staged file(s) look like database/backup data - must not be committed:"
    echo "$matched" | sed 's/^/  /'
    errors=1
fi

# Check for config secrets. Everything under config/ is rejected except the
# .gitkeep and example files, rather than listing extensions to reject: the
# credential files across these servers include extensionless dotfiles
# (.env, .token-oauth, .token-v2), which an extension pattern misses entirely.
staged_config=$(git diff --cached --name-only | grep -E '^config/' | grep -vE '(^|/)\.gitkeep$|\.example(\.|$)' || true)
if [ -n "$staged_config" ]; then
    echo "ERROR: Staged file(s) under config/ - credentials and tokens must not be committed:"
    echo "$staged_config" | sed 's/^/  /'
    errors=1
fi

# Check for large files (>100KB) that might be data dumps
_tmpfile=$(mktemp)
trap 'rm -f "$_tmpfile"' EXIT
git diff --cached --name-only --diff-filter=ACM > "$_tmpfile"
while IFS= read -r file; do
    # A dependency lockfile is legitimately large and only grows.
    case "$file" in
        uv.lock) continue ;;
    esac
    size=$(git cat-file -s ":$file" 2>/dev/null || echo 0)
    if [ "$size" -gt 102400 ]; then
        echo "ERROR: Staged file '$file' is $(( size / 1024 ))KB (>100KB) - possible data leak"
        errors=1
    fi
done < "$_tmpfile"

if [ "$errors" -ne 0 ]; then
    echo ""
    echo "Commit rejected. Financial data and credentials must never be committed."
    exit 1
fi
