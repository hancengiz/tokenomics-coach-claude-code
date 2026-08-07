#!/bin/bash

# Tokenomics Coach — installer
# Copies skills/tokenomics-coach-claude-code/ into ~/.claude/skills/

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SKILL_NAME="tokenomics-coach-claude-code"
SOURCE_DIR="$SCRIPT_DIR/skills/$SKILL_NAME"
INSTALL_DIR="$HOME/.claude/skills/$SKILL_NAME"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  💸 Tokenomics Coach Installer"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

if [ ! -f "$SOURCE_DIR/SKILL.md" ]; then
    echo -e "${RED}✗${NC} Cannot find $SOURCE_DIR/SKILL.md — run this from a repo checkout."
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo -e "${RED}✗${NC} python3 not found. This skill requires Python 3.9+."
    exit 1
fi

if command -v skills-ref >/dev/null 2>&1; then
    echo -e "${GREEN}➜${NC} Validating against the Agent Skills spec..."
    skills-ref validate "$SOURCE_DIR" || { echo -e "${RED}✗${NC} Validation failed"; exit 1; }
    echo ""
fi

mkdir -p "$HOME/.claude/skills"

if [ -d "$INSTALL_DIR" ] || [ -L "$INSTALL_DIR" ]; then
    echo -e "${YELLOW}⚠️  Already installed at $INSTALL_DIR${NC}"
    read -p "   Overwrite? (y/n) " -n 1 -r; echo ""
    [[ $REPLY =~ ^[Yy]$ ]] || { echo -e "${RED}✗${NC} Cancelled"; exit 1; }
    rm -rf "$INSTALL_DIR"
fi

echo -e "${GREEN}➜${NC} Installing..."
cp -R "$SOURCE_DIR" "$INSTALL_DIR"
chmod +x "$INSTALL_DIR/scripts/analyze.py" 2>/dev/null || true

if python3 -m py_compile "$INSTALL_DIR/scripts/analyze.py" 2>/dev/null; then
    echo -e "${GREEN}✓${NC} analyze.py compiles"
else
    echo -e "${YELLOW}⚠️${NC}  analyze.py failed to compile — check your Python 3 install"
fi

echo -e "${GREEN}✓${NC} Installed to $INSTALL_DIR"
echo ""
find "$INSTALL_DIR" -type f | sed "s|$INSTALL_DIR/||" | sort | sed 's/^/     - /'
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🎯 Restart Claude Code, then try:"
echo ""
echo "     \"Where am I wasting tokens?\""
echo "     \"Audit my cache efficiency for the last 30 days\""
echo "     \"Am I paying Opus rates to write files?\""
echo "     \"Why was yesterday's session so expensive?\""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo -e "${GREEN}✨ Done.${NC}"
