---
allowed-tools: [Bash, Read, Glob, Grep, Write, Edit, Agent]
description: Multi-model plan-and-execute workflow using GH issues
approach: script-delegation
token-cost: ~200
best-for: Complex features needing structured planning across model tiers
---

# Plan & Execute Multi-Model Workflow

Run the plan-execute orchestrator to manage a multi-stage workflow.

```bash
node scripts/plan-execute.js $ARGUMENTS
```

## How It Works

This command orchestrates a planning and execution workflow across different Claude models. Each stage has a recommended model for cost/capability balance:

1. **DRAFT** (haiku) - Fast, cheap initial plan generation
2. **REFINE** (opus) - Critical review, risk assessment, plan improvement
3. **ISSUES** (sonnet) - Break refined plan into self-contained GH issues
4. **EXECUTE** (haiku) - Implement each issue (cheapest model for mechanical work)
5. **REVIEW** (opus) - Evaluate implementation quality

## Your Role

**CRITICAL: After running the script, you MUST stop and wait for the user.**

1. Run the script command above
2. Show the user the script output (stage, model recommendation, instructions)
3. **STOP. Do NOT proceed with the stage work yourself.**
4. Tell the user: "Switch to the recommended model with `/model <name>`, then follow the printed instructions."

You are an orchestrator only — you run the script and relay its output. You do NOT draft plans, refine plans, create issues, or do any stage work. The user will switch models and a different Claude instance will do that work.

**Why:** Each stage is designed for a specific model's cost/capability profile. If you proceed as the current model, you defeat the purpose of the multi-model workflow.

## Key Files

- `.plan-execute/current.json` - Current workflow state
- `.plan-execute/plan-*.md` - Draft and refined plans
- GH issues labeled `plan-<id>` - Implementation tasks

## Important Notes

- During DRAFT and REFINE stages, focus on reading code and planning. Do not modify files.
- During ISSUES stage, create focused issues with clear acceptance criteria and a "Tests to write" section specifying test cases. Tests MUST include integration tests that exercise the actual call chain with real data — not just unit tests with mocks.
- During EXECUTE stage, use TDD: write failing tests first, then implement. Work through issues one at a time, committing after each. Tests must cover the wiring/glue code (e.g., how a report calls functions), not just the functions in isolation.
- During REVIEW stage, be critical. Create new issues for bugs or improvements found.
- **Inner loop**: If REVIEW finds issues, create new GH issues, then `/plan-execute stage execute` to fix them. When done, `/plan-execute stage review` to loop back and verify fixes. Repeat until clean, then `/plan-execute done` to finish.
