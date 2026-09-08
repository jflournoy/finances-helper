---
allowed-tools: [Bash]
description: Push commits to remote repository
---

# Push Command

Simple, fast push for routine development workflow.

## Your Task

Check whether CI is already failing, then push:

```bash
if command -v gh >/dev/null 2>&1; then
  CI_STATUS=$(gh run list --limit 1 --json conclusion,name 2>/dev/null)
  if echo "$CI_STATUS" | grep -q '"conclusion":"failure"'; then
    echo "CI is currently failing on the most recent run:"
    echo "$CI_STATUS"
    echo "Fix it first, or push anyway if this commit is the fix."
    exit 1
  fi
fi
git push
```

If the check blocks a push that is itself the fix, push directly with `git push`.

## Why the check lives here

The `.husky/` hooks that used to hold this check never ran: husky activates by setting
`core.hooksPath`, which this repo cannot use because vexp owns `.git/hooks` for index
maintenance and does not detect the override. A gate that silently does nothing is worse
than no gate, so the check moved into this command, where it actually executes.

## Advanced Options

For complex git scenarios, run git directly:

- `git push --force-with-lease` — force push that refuses to clobber unseen commits
- `git push -u origin <branch>` — set upstream tracking on first push
- `git pull --rebase` then push — handle a rejected push

## Philosophy

- **Local commands**: handle git operations, and check whether CI is *already* red
- **CI/CD (GitHub Actions)**: run the quality validation itself
- **Clear separation**: don't duplicate CI/CD validation locally

When you say "push", you want to push. Let CI/CD handle the quality checks.
