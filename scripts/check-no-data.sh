#!/bin/sh
# Pre-commit hook: reject commits containing database files, config secrets,
# or suspiciously large files that might contain real financial data.
#
# POSIX sh compatible - no bash required.
#
# Install: cp scripts/check-no-data.sh .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit

set -eu

errors=0

# Check for database files
for pattern in '*.db' '*.db-journal' '*.db-wal'; do
    if git diff --cached --name-only | grep -q "$pattern"; then
        echo "ERROR: Staged file matches '$pattern' - database files must not be committed"
        errors=1
    fi
done

# Check for config secrets (example files are safe to commit)
if git diff --cached --name-only | grep -E '^config/.*\.(json|env)$' | grep -qvE '\.example\.'; then
    echo "ERROR: Staged file in config/ - credentials and tokens must not be committed"
    errors=1
fi

# Check for large files (>100KB) that might be data dumps
_tmpfile=$(mktemp)
trap 'rm -f "$_tmpfile"' EXIT
git diff --cached --name-only --diff-filter=ACM > "$_tmpfile"
while IFS= read -r file; do
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
