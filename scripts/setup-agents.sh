#!/bin/bash
# Setup rápido de agents para LegalTech
# Uso: bash /opt/legal-tech-microservices/scripts/setup-agents.sh

set -euo pipefail

echo "🚀 Setting up agents for LegalTech..."
echo ""

# 1. Codex CLI
echo "📦 Step 1/5: Installing Codex CLI..."
if ! command -v codex &> /dev/null; then
    npm install -g @openai/codex
    echo "✅ Codex CLI installed"
else
    echo "✅ Codex CLI already installed"
fi
echo ""

# 2. GitHub CLI
echo "📦 Step 2/5: Verifying GitHub CLI..."
if ! command -v gh &> /dev/null; then
    sudo apt install -y gh
    gh auth login
fi
echo "✅ GitHub CLI ready"
echo ""

# 3. Claude Code permissions
echo "🔧 Step 3/5: Configuring Claude Code..."
if [ -f ~/.claude/settings.json ]; then
    echo "✅ Claude Code already configured"
else
    cat > ~/.claude/settings.json << 'EOF'
{
  "permissions": {
    "allow": [
      "Bash(git status:*)",
      "Bash(git diff:*)",
      "Bash(git log:*)",
      "Bash(git branch:*)",
      "Bash(npm run:*)",
      "Bash(npm install:*)",
      "Bash(npm test:*)",
      "Bash(npm run build:*)",
      "Bash(node:*)",
      "Bash(pnpm:*)",
      "Bash(yarn:*)",
      "Bash(docker compose:*)",
      "Bash(docker build:*)",
      "Bash(docker run:*)",
      "Bash(curl:*)",
      "Bash(wget:*)",
      "Bash(cat:*)",
      "Bash(ls:*)",
      "Bash(find:*)",
      "Bash(grep:*)",
      "Bash(head:*)",
      "Bash(tail:*)",
      "Bash(wc:*)",
      "Bash(sort:*)",
      "Bash(uniq:*)",
      "Bash(jq:*)",
      "Bash(rg:*)",
      "Bash(ripgrep:*)",
      "Edit(*:*)",
      "Write(*:*)",
      "Read(*)",
      "Glob(*)",
      "Grep(*)",
      "LS(*)",
      "WebFetch(*)",
      "WebSearch(*)"
    ],
    "ask": [
      "Bash(sudo:*)",
      "Bash(rm -rf:*)",
      "Bash(dd:*)",
      "Bash(mkfs:*)",
      "Bash(chmod:*)",
      "Bash(chown:*)",
      "Bash(systemctl:*)",
      "Bash(service:*)",
      "Bash(useradd:*)",
      "Bash(userdel:*)",
      "Bash(passwd:*)"
    ],
    "deny": [
      "Bash(rm -rf /:*)",
      "Bash(rm -rf /*:*)"
    ]
  }
}
EOF
    echo "✅ Claude Code configured"
fi
echo ""

# 4. Clawdbot config
echo "🔧 Step 4/5: Configuring Clawdbot..."
mkdir -p ~/.clawdbot
if [ -f ~/.clawdbot/clawdbot.json ]; then
    echo "✅ Clawdbot already configured"
else
    cat > ~/.clawdbot/clawdbot.json << 'EOF'
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "8343683301:AAEemeviAxm5VGELPnbekIy2lou_0Trh4Zk",
      "allowFrom": [886820553]
    }
  },
  "hooks": {
    "enabled": true,
    "token": "legaltech-secret-token-2026"
  },
  "gateway": {
    "port": 18789,
    "mode": "local",
    "bind": "loopback"
  }
}
EOF
    echo "✅ Clawdbot configured"
fi
echo ""

# 5. AGENTS.md
echo "📝 Step 5/5: Creating AGENTS.md..."
if [ -f /opt/legal-tech-microservices/AGENTS.md ]; then
    echo "✅ AGENTS.md already exists"
else
    cp /opt/legal-tech-microservices/scripts/AGENTS.md.template /opt/legal-tech-microservices/AGENTS.md 2>/dev/null || echo "⚠️ Create AGENTS.md manually"
fi
echo ""

echo "✅ Setup complete!"
echo ""
echo "═══════════════════════════════════════════"
echo "Next steps:"
echo "1. Edit AGENTS.md with your specific rules"
echo "2. Test Codex: codex --yolo exec 'Hello'"
echo "3. Test Claude: claude 'Hello'"
echo "4. Test GitHub: gh repo view"
echo "═══════════════════════════════════════════"
