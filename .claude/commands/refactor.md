---
allowed-tools: [Read, Grep, Glob, Agent]
description: Deep refactoring analysis — think like a god-tier 1960s programmer
---

# Refactor — Sutherland Mode

Analyze the given file like Ivan Sutherland coding Sketchpad. Every line must justify its existence. Limited resources, unlimited elegance.

## Target

$ARGUMENTS

If no argument given, ask the user which file to analyze.

## Your Mindset

You are programming with 4KB of RAM and a 1MHz processor. You have no room for:
- Redundant variables or repeated computation
- Unnecessary subprocess spawns or I/O
- Defensive code that defends against nothing real
- Abstraction layers that abstract over one thing
- Comments that restate the code

You DO value:
- State machines over nested conditionals
- Data flowing through a pipeline with minimal intermediate allocation
- Code that reads like a specification of the problem
- Single-pass algorithms where possible
- Making the machine's job easy, not the programmer's job easy

## Process

1. **Read the entire file** — understand what it actually does vs. what it thinks it does
2. **Map the data flow** — trace every variable from birth to death. Flag any that exist without purpose.
3. **Count the operations** — how many subprocesses, file reads, loops, conditionals? What's the minimum needed?
4. **Identify structural waste:**
   - Repeated patterns that should be a loop or lookup table
   - Branching that could be a dispatch table
   - Sequential operations that could be parallel (or vice versa)
   - Error handling that handles errors that can't happen
   - Variables that are set and read exactly once with no meaningful name
5. **Propose the refactored design** — not just fixes, but the *shape* of the code as it should be

## Output Format

### Current State
- Lines of code: N
- Subprocess spawns: N
- Conditionals: N
- Core operations (what it actually does in ~3 bullets)

### Waste Map
Table of identified waste: what it is, why it's waste, what replaces it.

### Proposed Design
The refactored structure as pseudocode or a skeleton, with annotations on why each part exists.

### Refactoring Steps
Ordered list of changes, each independently testable. Smallest valuable change first.
