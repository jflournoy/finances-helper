#!/usr/bin/env node
/**
 * PostToolUse hook: route an edited file to the reviewer agent that fits it.
 *
 * Dispatch lives here, as a tested lookup table, rather than as inline bash in
 * settings.json or as prose an agent re-interprets each run. Adding a file type
 * is a one-line change with a test beside it.
 *
 * Contract: reads the hook payload as JSON on stdin, prints a hookSpecificOutput
 * object naming the agent to run, and exits 0. Every failure path exits 0 with no
 * output, so a broken hook degrades to vanilla behavior and never blocks an edit.
 *
 * Disable for a session with CLAUDE_REVIEWER_DISPATCH=0.
 */

'use strict';

// Ordered: first match wins. Keep this list short — every entry costs context and
// latency on each edit, and a reviewer nobody reads is worse than no reviewer.
const RULES = [
  { test: /\.stan$/i, agent: 'stan-reviewer',
    label: 'STAN MODEL MODIFIED' },
  { test: /\.(R|r|Rmd|rmd|qmd)$/, agent: 'r-analysis-reviewer',
    label: 'R / ANALYSIS FILE MODIFIED' }
];

/**
 * Pure dispatch. Returns {agent, label} or null when no rule matches.
 * Exported for tests; keep it free of I/O.
 */
function agentFor(filePath) {
  if (typeof filePath !== 'string' || filePath.length === 0) return null;
  const base = filePath.split('/').pop();
  if (!base || base.startsWith('.')) return null;
  for (const rule of RULES) {
    if (rule.test.test(base)) return { agent: rule.agent, label: rule.label };
  }
  return null;
}

/** Extract the edited path from a PostToolUse payload, or null. */
function filePathFrom(payload) {
  if (!payload || typeof payload !== 'object') return null;
  const fromInput = payload.tool_input && payload.tool_input.file_path;
  const fromResponse = payload.tool_response && payload.tool_response.filePath;
  return fromInput || fromResponse || null;
}

/** Build the hook response Claude Code consumes, or null when nothing to say. */
function buildOutput(payload) {
  const hit = agentFor(filePathFrom(payload));
  if (!hit) return null;
  const file = filePathFrom(payload);
  return {
    hookSpecificOutput: {
      hookEventName: 'PostToolUse',
      additionalContext:
        `${hit.label}: ${file} — run the ${hit.agent} agent on this change before ` +
        'moving on. It reports findings; it does not edit. If the change is trivial ' +
        '(a comment, a rename), say so in one line and skip it.'
    }
  };
}

function main() {
  if (process.env.CLAUDE_REVIEWER_DISPATCH === '0') return;
  let raw = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (c) => { raw += c; });
  process.stdin.on('end', () => {
    try {
      const out = buildOutput(JSON.parse(raw));
      if (out) process.stdout.write(JSON.stringify(out));
    } catch {
      // Fail open: a malformed payload must never break the edit.
    }
  });
}

if (require.main === module) main();

module.exports = { agentFor, filePathFrom, buildOutput, RULES };
