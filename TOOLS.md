# Tools Configuration - LegalTech

## Codex CLI

**Installation:** `npm install -g @openai/codex`  
**Auth:** `codex auth login`  
**Version:** 0.101.0

### Usage:
```bash
# Modo YOLO (sin aprobaciones)
codex --yolo exec "Fix this bug..."

# Con sandbox (más seguro)
codex --full-auto exec "..."

# Interactive mode
codex
```

### Config:
```json
// ~/.codex/config.json
{
  "model": "codex",
  "mode": "yolo",
  "workdir": "/tmp/codex-fix-*"
}
```

---

## Claude Code

**Installation:** Pre-instalado  
**Config:** `~/.claude/settings.json`  
**Permissions:** Allowlist configurada

### Allowed Commands:
- `git status/diff/log/branch/*`
- `npm run/install/test/build/*`
- `node/*`, `pnpm/*`, `yarn/*`
- `docker compose/build/run/*`
- `curl/*`, `wget/*`
- `cat/ls/find/grep/head/tail/wc/sort/uniq/jq/rg/*`
- `Edit/*`, `Write/*`, `Read/*`, `Glob/*`, `Grep/*`, `LS/*`
- `WebFetch/*`, `WebSearch/*`

### Requires Approval:
- `sudo/*`
- `rm -rf/*`
- `dd/*`, `mkfs/*`
- `chmod/*`, `chown/*`
- `systemctl/*`, `service/*`
- `useradd/*`, `userdel/*`, `passwd/*`
- `curl/wget | bash/sh`

### Denied:
- `rm -rf /*`
- `:(){:|:&};:` (fork bomb)

---

## Opencode

**Installation:** Pre-instalado  
**Usage:** `opencode "prompt"`

### Use Cases:
- Testing rápido
- Exploración de código
- Refactors simples

---

## GitHub CLI

**Installation:** `gh` v2.45.0  
**Auth:** OAuth (logged in as rrzu777)  
**Token Scopes:** `gist`, `read:org`, `repo`, `workflow`

### Common Commands:
```bash
# Create PR
gh pr create --title "fix: ..." --body "..." --base staging

# List PRs
gh pr list --head fix/sentry-*

# View PR
gh pr view [number]

# Merge PR
gh pr merge [number] --merge

# Check CI
gh run list --branch [branch]
```

---

## Git Worktrees

### Create:
```bash
git worktree add -b fix/sentry-[id] /tmp/codex-fix-[id] main
```

### List:
```bash
git worktree list
```

### Remove:
```bash
git worktree remove /tmp/codex-fix-[id]
```

### Cleanup all:
```bash
git worktree prune
```

---

## Worktree Locations

| Type | Location |
|------|----------|
| **Codex fixes** | `/tmp/codex-fix-*` |
| **Claude fixes** | `/tmp/claude-fix-*` |
| **Opencode fixes** | `/tmp/opencode-fix-*` |

---

## Wake System

### Notify completion:
```bash
clawdbot gateway wake --text "Done: [summary]" --mode now
```

### Modes:
- `now` → Notificación inmediata
- `next` → En el próximo heartbeat
- `never` → Sin notificación

---

## Testing Commands

### LegalTech Tests:
```bash
# Run all tests
pytest tests/ -v

# Run specific test
pytest tests/test_something.py::test_function -v

# With coverage
pytest tests/ -v --cov=app --cov-report=html
```

### Linting:
```bash
# Pylint
pylint app/

# Black (formatter)
black app/

# Check only
black --check app/
```

---

## Environment Variables

```bash
# Supabase
SUPABASE_URL=https://fyfymrxnagkcbjxwpelq.supabase.co
SUPABASE_SERVICE_KEY=[tu-key]

# API Keys
API_KEY=[tu-api-key]

# Cron
CRON_SECRET=36124920982081505ef7dd1d819760cd9b9ab28a96178a777e3b0f78ac958e1e
```

---

## Quick Reference

| Task | Command |
|------|---------|
| **Fix bug rápido** | `codex --yolo exec "..."` |
| **Feature compleja** | `claude "..."` |
| **Crear PR** | `gh pr create --base staging` |
| **Verificar cambios** | `git log --oneline -3 && git diff --stat` |
| **Notificar done** | `clawdbot gateway wake --text "Done" --mode now` |
