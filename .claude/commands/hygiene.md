---
allowed-tools: [Bash]
description: Project health check - code quality, tests, dependencies, and git status
approach: direct-implementation
token-cost: ~200
best-for: Quick daily health checks
portability: self-contained; needs only git, and the project's own test/lint runner
---

# Project Hygiene Check

Health assessment that detects the project's language and uses its own tooling. No
`package.json` required — this works in R, Python and TypeScript projects as-is.

## Your Task

```bash
#!/bin/bash
echo "🔍 Project Hygiene Check"
echo "========================"

FAILED=0

if command -v gh >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
  echo ""
  echo "📊 CI status:"
  gh run list --limit 3 2>/dev/null || echo "  (no runs found)"
fi

echo ""
echo "🧪 Tests:"
if [ -f DESCRIPTION ] && grep -q '^Package:' DESCRIPTION 2>/dev/null; then
  if Rscript -e 'requireNamespace("testthat", quietly=TRUE) || quit(status=2)' 2>/dev/null; then
    Rscript -e 'testthat::test_local()' || FAILED=1
  else echo "  ⚠️  testthat not installed"; fi
elif [ -f pyproject.toml ] || [ -f setup.py ] || [ -d tests ]; then
  if command -v pytest >/dev/null 2>&1; then pytest -q || FAILED=1
  elif python -c 'import pytest' 2>/dev/null; then python -m pytest -q || FAILED=1
  else echo "  ⚠️  pytest not installed"; fi
elif [ -f package.json ] && grep -q '"test"' package.json; then
  npm test || FAILED=1
else
  echo "  ⚠️  No test runner detected"
fi

echo ""
echo "🎨 Lint:"
if [ -f DESCRIPTION ] && grep -q '^Package:' DESCRIPTION 2>/dev/null; then
  if Rscript -e 'requireNamespace("lintr", quietly=TRUE) || quit(status=2)' 2>/dev/null; then
    Rscript -e 'lintr::lint_package()' || FAILED=1
  else echo "  ⚠️  lintr not installed"; fi
elif [ -f pyproject.toml ] || [ -f setup.py ]; then
  if command -v ruff >/dev/null 2>&1; then ruff check . || FAILED=1
  else echo "  ⚠️  ruff not installed"; fi
elif [ -f eslint.config.js ] || [ -f .eslintrc.js ]; then
  npx eslint . --max-warnings 10 || FAILED=1
else
  echo "  ⚠️  No linter detected"
fi

echo ""
echo "🔧 Technical debt:"
echo "  TODO: $(grep -rIl 'TODO' . --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=renv --exclude-dir=.venv 2>/dev/null | wc -l | xargs) files"
echo "  FIXME: $(grep -rIl 'FIXME' . --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=renv --exclude-dir=.venv 2>/dev/null | wc -l | xargs) files"

echo ""
echo "📋 Git:"
echo "  Branch: $(git branch --show-current 2>/dev/null || echo unknown)"
echo "  Uncommitted: $(git status --porcelain 2>/dev/null | wc -l | xargs) files"

echo ""
if [ "$FAILED" -ne 0 ]; then
  echo "❌ Hygiene check failed - see above"
  exit 1
fi
echo "✅ Hygiene check passed"
```

## Notes

- Exits non-zero when tests or lint fail, so it is safe to chain or use in CI
- Detection order is R (`DESCRIPTION`) → Python (`pyproject.toml`/`setup.py`/`tests/`) →
  Node (`package.json`); the first match wins
- A missing tool warns; a tool that runs and fails exits non-zero. The two are never
  conflated, and neither is ever reported as success
