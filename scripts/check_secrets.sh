#!/usr/bin/env bash
# ============================================================
# check_secrets.sh — Pre-commit secret scanner
# ============================================================
# Usage:
#   ./scripts/check_secrets.sh          # scan staged files
#   ./scripts/check_secrets.sh --all    # scan entire repo
#
# Add as a git pre-commit hook:
#   ln -sf ../../scripts/check_secrets.sh .git/hooks/pre-commit
# ============================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

if [[ "${1:-}" == "--all" ]]; then
    FILES=$(git ls-files -- '*.py' '*.sh' '*.yml' '*.yaml' '*.json' '*.toml' '*.cfg' '*.ini' '*.env*' '*.md')
else
    FILES=$(git diff --cached --name-only --diff-filter=ACM -- '*.py' '*.sh' '*.yml' '*.yaml' '*.json' '*.toml' '*.cfg' '*.ini' '*.env*' '*.md')
fi

if [[ -z "$FILES" ]]; then
    echo -e "${GREEN}No staged files to scan.${NC}"
    exit 0
fi

FOUND=0

# Pattern 1: Explicit credential assignments (key = "value" with 10+ chars)
if echo "$FILES" | xargs grep -nEi '(api_key|api_secret|secret_key|password|private_key|token)\s*[=:]\s*["\x27][^\x27"]{10,}' 2>/dev/null | grep -v 'example\|placeholder\|your_\|_here\|TODO\|CHANGEME' ; then
    echo -e "${RED}^^^ Potential hardcoded credentials found!${NC}"
    FOUND=1
fi

# Pattern 2: Long hex strings (48+ chars) not in comments
if echo "$FILES" | xargs grep -nE '[0-9a-fA-F]{48,}' 2>/dev/null | grep -v '#\|\.gitignore\|\.example\|check_secrets' ; then
    echo -e "${RED}^^^ Potential hex-encoded secret found!${NC}"
    FOUND=1
fi

# Pattern 3: Base64 blocks (60+ chars)
if echo "$FILES" | xargs grep -nE '[A-Za-z0-9+/]{60,}={0,2}' 2>/dev/null | grep -v '#\|\.gitignore\|\.example\|check_secrets' ; then
    echo -e "${RED}^^^ Potential base64-encoded secret found!${NC}"
    FOUND=1
fi

if [[ "$FOUND" -eq 1 ]]; then
    echo ""
    echo -e "${RED}ERROR: Potential secrets detected in staged files.${NC}"
    echo "Please remove credentials and use environment variables instead."
    exit 1
else
    echo -e "${GREEN}OK: No secrets detected in staged files.${NC}"
    exit 0
fi
