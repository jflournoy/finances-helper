# Test-Driven Development (TDD)

**STRONGLY RECOMMENDED: Use Test-Driven Development for all non-trivial changes**

TDD helps Claude produce more focused, correct code by clarifying requirements upfront and reducing wildly wrong approaches. This is the default expectation for any feature, refactor, or significant bug fix. Exceptions (typo fixes, one-liners) are rare and should be explicitly justified.

## Benefits of TDD with Claude

- **Without TDD**: Claude may over-engineer or miss requirements
- **With TDD**: Claude writes targeted code that meets specific criteria

## TDD Workflow

1. 🔴 **RED**: Write a failing test to define requirements
2. 🟢 **GREEN**: Write minimal code to pass the test
3. 🔄 **REFACTOR**: Improve code with test safety net
4. ✓ **COMMIT**: Ship working, tested code

## Test Contracts, Not Implementation

**CRITICAL: Tests must describe behavior from the caller's perspective — not internal mechanics.**

- ✅ Test inputs and outputs (the contract)
- ✅ Test observable side effects (files written, messages sent)
- ❌ Do NOT assert on internal function calls, private state, or call order
- ❌ Do NOT couple tests to specific implementation details that could change during refactoring

A good test survives a complete rewrite of the implementation. If refactoring breaks your tests without changing behavior, the tests are wrong.

## The TDD Command

```bash
/tdd start "your feature"  # Guides through the TDD cycle
```

Consider TDD especially for complex features or when requirements are unclear.

## TDD Examples

- [🔴 test: add failing test for updateCommandCatalog isolation (TDD RED)](../../commit/00e7a22)
- [🔴 test: add failing tests for tdd.js framework detection (TDD RED)](../../commit/2ce43d1)
- [🔴 test: add failing tests for learn.js functions (TDD RED)](../../commit/8b90d58)
- [🔴 test: add failing tests for formatBytes and estimateTokens (TDD RED)](../../commit/1fdac58)
- [🔴 test: add failing tests for findBrokenLinks (TDD RED phase)](../../commit/8ec6319)
