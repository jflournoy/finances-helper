# Claude Commands Structure

## Organization

Commands are organized by frequency of use and purpose:

### All Commands

**YNAB / Amazon enrich pipeline:**
- `enrich-run` - Run `enrich.py` over a window; produce a changeset (read-only against YNAB)
- `confirm-review` - Dry-run the latest changeset to validate before writing
- `confirm-apply` - PATCH the latest changeset to YNAB (writes real money; prompts before applying)

**Development workflow:**
- `commit` - Atomic commits with quality checks (1-3 files)
- `docs` - Documentation maintenance
- `docs-explain` - Educational documentation walkthrough
- `hygiene` - Code quality checks
- `learn` - Capture insights and learnings
- `maintainability` - Code quality analysis
- `monitor` - GitHub repository monitoring
- `next` - Get workflow guidance
- `plan-execute` - Multi-model plan-and-execute workflow
- `push` - Git push with validations
- `reflect` - Periodic reflection
- `retrospective` - Session analysis
- `session-history` - Session preservation
- `tdd` - Test-driven development
- `todo` - Task management

## Usage

All commands work the same way:
```bash
/commit
/maintainability
/session-history
# etc.
```

## Finding Commands

```bash
# List all available commands
ls -1 .claude/commands/*.md | sed 's|.*/||' | sed 's|\.md||' | grep -v README
```

## Command Philosophy

- **Simple flat structure**: All commands in one directory for easy discovery
- **Minimal by default**: Token-efficient implementations
- **Focused purpose**: Each command does one thing well