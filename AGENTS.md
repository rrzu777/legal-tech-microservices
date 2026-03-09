# Agent Rules - LegalTech

## Bug Fix Rules

### Auto-Fix (sin preguntar):
- ✅ Null reference errors
- ✅ Type mismatches
- ✅ Missing imports
- ✅ Obvious logic bugs
- ✅ Unhandled edge cases
- ✅ Formatting issues
- ✅ Simple refactors
- ✅ Add logging/debugging

### Escalar (preguntar):
- ⚠️ Architecture issues
- ⚠️ Database migrations
- ⚠️ Security-sensitive code
- ⚠️ API contract changes
- ⚠️ Breaking changes
- ⚠️ New dependencies

## Fix Process

### 1. Create isolated worktree:
```bash
cd /opt/legal-tech-microservices
git worktree add -b fix/sentry-[id] /tmp/codex-fix-[id] main
```

### 2. Spawn Codex (o Claude Code):

**Opción A - Codex CLI (recomendado para bugs rápidos):**
```bash
cd /tmp/codex-fix-[id]
codex --yolo exec "Fix: [error details]
  After changes:
  1. Run tests: pytest tests/ -v
  2. Run linter: pylint app/
  3. Fix failures
  4. Commit with descriptive message
  5. Push and open PR targeting staging"
```

**Opción B - Claude Code (para features complejas):**
```bash
cd /tmp/codex-fix-[id]
claude "Fix: [error details]
  After changes:
  1. Run tests
  2. Run linter
  3. Fix failures
  4. Commit and push
  5. Open PR"
```

### 3. Verify before notifying:
```bash
# En el worktree
git log --oneline -3
git diff --stat
gh pr list --head fix/sentry-[id]
```

### 4. Notify via wake:
```bash
clawdbot gateway wake --text "Done: Fixed [description]. PR #[id] ready" --mode now
```

## Tools Available

| Tool | Command | Use Case |
|------|---------|----------|
| **Codex CLI** | `codex --yolo exec "..."` | Bug fixes rápidos |
| **Claude Code** | `claude "..."` | Features complejas |
| **Opencode** | `opencode "..."` | Testing/exploración |
| **GitHub CLI** | `gh pr create ...` | Crear PRs |
| **Git worktrees** | `git worktree add ...` | Aislamiento |

## Standard Prompts

### Bug Fix Template:
```
Fix the following error:

Error: [pegar error de Sentry/log]
File: [archivo:línea]
Stack trace: [pegar stack trace]

The issue is [breve descripción].

After making changes:
1. Run tests: pytest tests/ -v
2. Run linter: pylint app/
3. Fix any failures
4. Only open PR if everything passes
5. Create branch: fix/sentry-[short-description]
6. Push and open PR targeting staging

When completely finished, run:
clawdbot gateway wake --text "Done: Fixed [summary]" --mode now
```

### Feature Template:
```
Implement [feature name] with the following requirements:

Requirements:
- [req 1]
- [req 2]
- [req 3]

After implementation:
1. Write tests for new functionality
2. Run full test suite
3. Run linter
4. Update documentation if needed
5. Create branch: feature/[name]
6. Push and open PR targeting staging

When finished:
clawdbot gateway wake --text "Done: Implemented [feature]" --mode now
```

## Verification Checklist

Before notifying completion, always verify:

- [ ] Tests pass (`pytest tests/ -v`)
- [ ] Linter clean (`pylint app/`)
- [ ] Git commit created (`git log --oneline -3`)
- [ ] No uncommitted changes (`git diff --stat`)
- [ ] PR opened (`gh pr list --head [branch]`)
- [ ] PR targeting correct branch (staging, NOT main)

## Escalation

If you encounter any of these, STOP and ask:

1. Database schema changes
2. API breaking changes
3. Security-sensitive code (auth, payments, PII)
4. Architecture decisions
5. New external dependencies
6. Unclear business logic

Message format for escalation:
```
⚠️ Need clarification on: [issue]

Options:
A) [option A]
B) [option B]

Recommendation: [tu recomendación]

Waiting for confirmation before proceeding.
```
