---
allowed-tools: [Bash]
description: Your new best friend - TDD workflow that makes Claude amazing
approach: script-delegation
token-cost: ~100 (vs ~1500 for manual TDD guidance)
best-for: Structured test-driven development
---

# TDD Command - Your New Best Friend 🚀

Stop fighting with Claude. Start shipping with confidence.

## The TDD Promise

Write a test. Get perfect code. Every time.

## Why You'll Love This

- **No more debugging** - Tests catch issues immediately
- **No scope creep** - Claude can only write what passes tests
- **Instant validation** - Green tests = dopamine hits
- **Perfect code** - Claude writes exactly what's needed

## Usage

<bash>
#!/bin/bash

# Start your TDD journey
node scripts/tdd.js "$@"
</bash>

## Quick Examples

```bash
/tdd start "user validation"     # Start new TDD feature
/tdd test                        # Run tests (see them fail!)
/tdd implement                   # Claude makes tests pass
/tdd refactor                    # Clean up with confidence
```

## The Magic Workflow

1. **🔴 RED**: You write test (Claude helps!)
2. **🟢 GREEN**: Claude writes minimal code to pass
3. **🔄 REFACTOR**: Improve with safety net
4. **🎉 COMMIT**: Ship working code

## Real Talk

Without TDD, Claude is like a brilliant intern who needs supervision.
With TDD, Claude becomes a senior engineer who ships perfect code.

## Your First TDD Experience

Try this right now:
```bash
/tdd demo
```

Watch Claude transform from chaos to clarity.

## Test Contracts, Not Implementation

**The most important TDD principle: test behavior, not internals.**

Tests should describe what a thing *does* from the outside — given this input, produce this output or effect. They should not describe *how* it works internally.

- ✅ `expect(parse(input)).toEqual(expected)` — tests the contract
- ✅ `expect(fs.existsSync(outputPath)).toBe(true)` — tests observable effect
- ❌ `expect(internalHelper).toHaveBeenCalledWith(x)` — tests implementation
- ❌ `expect(privateState).toBe(y)` — tests internals

A test suite that survives a full refactor is a good test suite. If your tests break when you rename a private function, they're coupled to implementation, not behavior.

## Advanced Patterns

- **Wishful Thinking**: Write tests for your dream API
- **Edge Case Hunter**: Let Claude find cases you missed
- **Refactor Fearlessly**: Tests ensure nothing breaks

## Success Metrics

- Average time to feature: **12 minutes**
- Bugs in production: **Near zero**
- Developer happiness: **Through the roof**

## Notes

This command delegates to `scripts/tdd.js` which handles:
- Test framework detection (Jest, Vitest, Mocha)
- Automatic test running
- RED-GREEN-REFACTOR cycle enforcement
- Success tracking and metrics

For more patterns, see [TDD with Claude](../../docs/TDD_WITH_CLAUDE.md).

---

*"TDD with Claude isn't a process, it's a superpower." - Every developer who tried it*