#!/usr/bin/env node

/**
 * Plan & Execute Multi-Model Workflow
 *
 * Orchestrates a multi-stage planning and execution workflow
 * that leverages different Claude models for different stages:
 *
 *   1. DRAFT (haiku)   - Fast, cheap initial plan
 *   2. REFINE (opus)   - Critical review and extension
 *   3. ISSUES (sonnet)  - Break plan into GH issues
 *   4. EXECUTE (haiku)  - Implement each issue
 *   5. REVIEW (opus)    - Evaluate implementation
 *
 * State is tracked in .plan-execute/ directory.
 */

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const command = process.argv[2] || 'help';
const args = process.argv.slice(3).join(' ');

const STATE_DIR = '.plan-execute';
const CURRENT_FILE = path.join(STATE_DIR, 'current.json');

const STAGES = {
  draft: {
    model: 'haiku',
    label: 'DRAFT',
    color: '\x1b[33m', // yellow
    emoji: '📝',
    next: 'refine'
  },
  refine: {
    model: 'opus',
    label: 'REFINE',
    color: '\x1b[35m', // magenta
    emoji: '🔍',
    next: 'issues'
  },
  issues: {
    model: 'sonnet',
    label: 'ISSUES',
    color: '\x1b[36m', // cyan
    emoji: '📋',
    next: 'execute'
  },
  execute: {
    model: 'haiku',
    label: 'EXECUTE',
    color: '\x1b[32m', // green
    emoji: '🔨',
    next: 'review'
  },
  review: {
    model: 'opus',
    label: 'REVIEW',
    color: '\x1b[34m', // blue
    emoji: '✅',
    next: null
  }
};

const RESET = '\x1b[0m';
const BOLD = '\x1b[1m';
const DIM = '\x1b[2m';

// --- State management ---

function ensureStateDir() {
  if (!fs.existsSync(STATE_DIR)) {
    fs.mkdirSync(STATE_DIR, { recursive: true });
  }
}

function loadState() {
  if (!fs.existsSync(CURRENT_FILE)) return null;
  return JSON.parse(fs.readFileSync(CURRENT_FILE, 'utf8'));
}

function saveState(state) {
  ensureStateDir();
  fs.writeFileSync(CURRENT_FILE, JSON.stringify(state, null, 2));
}

function getPlanPath(state) {
  return path.join(STATE_DIR, `plan-${state.id}.md`);
}

function getRefinedPlanPath(state) {
  return path.join(STATE_DIR, `plan-${state.id}-refined.md`);
}

// --- Validation ---

function validateStageCompletion(stage, state) {
  switch (stage) {
    case 'draft': {
      const planPath = getPlanPath(state);
      if (!fs.existsSync(planPath)) return false;
      // Check it's not still the placeholder
      const content = fs.readFileSync(planPath, 'utf8');
      return !content.includes('_Draft pending');
    }

    case 'refine': {
      const refinedPath = getRefinedPlanPath(state);
      if (!fs.existsSync(refinedPath)) return false;
      const content = fs.readFileSync(refinedPath, 'utf8');
      // Should have multiple sections, not placeholder
      return content.length > 500 && !content.includes('_Refine pending');
    }

    case 'issues': {
      // Fallback: check state.issues array if gh is not available
      if (Array.isArray(state.issues) && state.issues.length > 0) {
        return true;
      }
      // Check that at least 1-2 issues were created with the plan label
      try {
        const label = state.label || `plan-${state.id}`;
        const result = execSync(
          `gh issue list --label "${label}" --state all --json number 2>/dev/null`,
          { encoding: 'utf8' }
        );
        const issues = JSON.parse(result);
        return issues.length > 0;
      } catch {
        return false;
      }
    }

    case 'execute': {
      // Fallback: check state.issues array for closed items if gh is not available
      if (Array.isArray(state.issues) && state.issues.some(i => i.status === 'closed')) {
        return true;
      }
      // Check that issues are closed or work is committed
      try {
        const label = state.label || `plan-${state.id}`;
        const result = execSync(
          `gh issue list --label "${label}" --state closed --json number 2>/dev/null`,
          { encoding: 'utf8' }
        );
        const issues = JSON.parse(result);
        return issues.length > 0;
      } catch {
        return false;
      }
    }

    case 'review':
      // Review is terminal, always OK to mark done
      return true;

    default:
      return true;
  }
}

// --- Commands ---

function start(description) {
  if (!description) {
    console.log('Usage: /plan-execute start "description of the feature or task"');
    process.exit(1);
  }

  const id = Date.now().toString(36);
  const state = {
    id,
    description,
    stage: 'draft',
    created: new Date().toISOString(),
    label: null,
    issues: []
  };

  saveState(state);

  const planPath = getPlanPath(state);
  fs.writeFileSync(planPath, `# Plan: ${description}\n\n_Draft pending..._\n`);

  const stage = STAGES.draft;
  console.log(`\n${stage.emoji} ${BOLD}Plan & Execute: Starting New Workflow${RESET}`);
  console.log(`${'═'.repeat(50)}`);
  console.log(`\n  Description: ${description}`);
  console.log(`  Plan ID:     ${id}`);
  console.log(`  Plan file:   ${planPath}`);
  console.log(`  Stage:       ${stage.color}${stage.label}${RESET}`);
  console.log('');

  printStageInstructions('draft', state);
}

function advance(stageName) {
  const state = loadState();
  if (!state) {
    console.log('No active plan. Start one with: /plan-execute start "description"');
    process.exit(1);
  }

  const currentStage = state.stage;

  // VALIDATION: Check if current stage work is done
  if (!validateStageCompletion(currentStage, state)) {
    console.log(`\n❌ ${BOLD}Stage Not Complete${RESET}`);
    console.log(`\nYou're still in the ${STAGES[currentStage].label} stage.`);
    console.log(`Complete the work and commit before advancing.\n`);
    process.exit(1);
  }

  // VALIDATION: Detect attempt to skip stages
  if (stageName && stageName !== currentStage) {
    const currentIdx = Object.keys(STAGES).indexOf(currentStage);
    const targetIdx = Object.keys(STAGES).indexOf(stageName);

    // Only allow moving backward (for /plan-execute stage <name>) or forward by 1
    if (targetIdx > currentIdx + 1) {
      console.log(`\n⚠️  ${BOLD}Cannot Skip Stages${RESET}`);
      console.log(`\nTrying to jump from ${STAGES[currentStage].label} to ${STAGES[stageName].label}`);
      console.log(`Complete stages in order: ${Object.values(STAGES).map(s => s.label).join(' → ')}\n`);
      process.exit(1);
    }
  }

  if (stageName && STAGES[stageName]) {
    state.stage = stageName;
  } else {
    const nextStage = STAGES[state.stage]?.next;
    if (!nextStage) {
      console.log('Workflow complete! No next stage.');
      process.exit(0);
    }
    state.stage = nextStage;
  }

  saveState(state);

  const stage = STAGES[state.stage];
  console.log(`\n${stage.emoji} ${BOLD}Advancing to: ${stage.color}${stage.label}${RESET}`);
  console.log(`${'═'.repeat(50)}`);
  console.log('');

  printStageInstructions(state.stage, state);

  console.log(`\n${'─'.repeat(60)}`);
  console.log(`\nWhen you've finished the ${stage.label} stage, run: ${BOLD}/plan-execute next${RESET}\n`);
}

function printStageInstructions(stageName, state) {
  const stage = STAGES[stageName];

  console.log(`\n${'█'.repeat(60)}`);
  console.log(`${BOLD}  STOP — DO NOT PROCEED WITH THIS STAGE${RESET}`);
  console.log(`${'█'.repeat(60)}\n`);
  console.log(`  Stage: ${stage.color}${stage.label}${RESET}`);
  console.log(`  Required model: ${stage.color}${stage.model}${RESET}`);
  console.log(`  Switch with: /model ${stage.model}\n`);
  console.log(`  AI: Show this output to the user and STOP.`);
  console.log(`  Do NOT do the stage work. The user will switch models.\n`);
  console.log(`${'─'.repeat(60)}\n`);
  console.log(`${BOLD}Instructions (for the ${stage.model} model after switching):${RESET}\n`);

  switch (stageName) {
  case 'draft': {
    console.log(`${BOLD}Instructions for Claude (${stage.model}):${RESET}`);
    console.log('');
    console.log('  You are drafting an implementation plan. Read the codebase as needed');
    console.log('  to understand the current architecture, then produce a plan.');
    console.log('');
    console.log('  Your plan should include:');
    console.log('    1. Goal statement (what and why)');
    console.log('    2. Current state (relevant files, patterns, constraints)');
    console.log('    3. Proposed changes (ordered list of modifications)');
    console.log('    4. Testing strategy');
    console.log('    5. Risks and open questions');
    console.log('');
    console.log(`  Write the plan to: ${BOLD}${getPlanPath(state)}${RESET}`);
    console.log('');
    console.log(`  When done, run: ${BOLD}/plan-execute next${RESET}`);
    break;
  }

  case 'refine': {
    const planPath = getPlanPath(state);
    const refinedPath = getRefinedPlanPath(state);
    console.log(`${BOLD}Instructions for Claude (${stage.model}):${RESET}`);
    console.log('');
    console.log(`  Read the draft plan at: ${BOLD}${planPath}${RESET}`);
    console.log('');
    console.log('  Critically evaluate and improve the plan:');
    console.log('    - Challenge assumptions and flag risks');
    console.log('    - Identify missing steps or edge cases');
    console.log('    - Suggest better approaches where applicable');
    console.log('    - Add concrete implementation details');
    console.log('    - Estimate complexity per step');
    console.log('    - Ensure testability of each change');
    console.log('');
    console.log(`  Write the refined plan to: ${BOLD}${refinedPath}${RESET}`);
    console.log('');
    console.log(`  When done, run: ${BOLD}/plan-execute next${RESET}`);
    break;
  }

  case 'issues': {
    const refinedPath = getRefinedPlanPath(state);
    const planPath = getPlanPath(state);
    const bestPlan = fs.existsSync(refinedPath) ? refinedPath : planPath;
    console.log(`${BOLD}Instructions for Claude (${stage.model}):${RESET}`);
    console.log('');
    console.log(`  Read the plan at: ${BOLD}${bestPlan}${RESET}`);
    console.log('');
    console.log('  Break the plan into GitHub issues that are easy for a fast model');
    console.log('  (haiku) to follow and execute independently.');
    console.log('');
    console.log('  Each issue should:');
    console.log('    - Have a clear, specific title');
    console.log('    - List exact files to modify');
    console.log('    - Include acceptance criteria (testable)');
    console.log('    - Include a "Tests to write" section with specific test cases');
    console.log('      that the executor must write BEFORE implementing the change');
    console.log('    - Tests MUST include integration tests that exercise the actual');
    console.log('      call chain (real data loading, real function calls with the');
    console.log('      exact arguments the caller will use). Do NOT rely solely on');
    console.log('      unit tests with mock data — these miss wiring bugs.');
    console.log('    - Reference the plan for context');
    console.log('    - Be completable in a single focused session');
    console.log('    - Be ordered by dependency (earlier issues first)');
    console.log('');
    if (state.label) {
      console.log(`  Label issues with: ${BOLD}${state.label}${RESET}`);
    } else {
      console.log(`  Label issues with: ${BOLD}plan-${state.id}${RESET}`);
    }
    console.log('');
    console.log('  Create issues using:');
    console.log(`    ${DIM}gh issue create --title "..." --body "..." --label "plan-${state.id}"${RESET}`);
    console.log('');
    console.log('  Note: You may need to create the label first:');
    console.log(`    ${DIM}gh label create "plan-${state.id}" --description "Auto-plan: ${state.description}"${RESET}`);
    console.log('');
    console.log(`  When done, run: ${BOLD}/plan-execute next${RESET}`);
    break;
  }

  case 'execute': {
    const label = state.label || `plan-${state.id}`;
    console.log(`${BOLD}Instructions for Claude (${stage.model}):${RESET}`);
    console.log('');
    console.log('  Work through the GitHub issues created in the previous stage.');
    console.log('');

    // Try to list open issues
    try {
      const issuesJson = execSync(
        `gh issue list --label "${label}" --state open --json number,title --limit 20 2>/dev/null`,
        { encoding: 'utf8' }
      );
      const issues = JSON.parse(issuesJson);
      if (issues.length > 0) {
        console.log('  Open issues:');
        issues.forEach(issue => {
          console.log(`    #${issue.number}: ${issue.title}`);
        });
        console.log('');
      }
    } catch {
      console.log(`  List issues with: ${DIM}gh issue list --label "${label}" --state open${RESET}`);
      console.log('');
    }

    console.log('  For each issue:');
    console.log('    1. Read the issue body for requirements');
    console.log('    2. Write failing tests FIRST (TDD red phase)');
    console.log('       - Use the "Tests to write" section from the issue');
    console.log('       - Run tests to confirm they fail for the right reason');
    console.log('    3. Implement the minimal code to pass the tests (TDD green phase)');
    console.log('    4. Run ALL tests (not just new ones) to check for regressions');
    console.log('    5. Commit with reference to issue number');
    console.log('    6. Close the issue if all acceptance criteria are met');
    console.log('');
    console.log(`  ${BOLD}TDD is mandatory.${RESET} Do not implement code without a failing test first.`);
    console.log('  If the issue lacks test specifications, write tests based on the');
    console.log('  acceptance criteria before implementing.');
    console.log('');
    console.log(`  ${BOLD}CRITICAL: Test the wiring, not just the functions.${RESET}`);
    console.log('  Unit tests with mocks are necessary but NOT sufficient.');
    console.log('  You MUST also write at least one integration test that exercises');
    console.log('  the actual data flow end-to-end (loading real data, calling');
    console.log('  functions with real arguments, verifying outputs). If code');
    console.log('  generates a report/document, test the exact calls the report');
    console.log('  will make — not just the functions in isolation.');
    console.log('');
    console.log(`  When all issues are done, run: ${BOLD}/plan-execute next${RESET}`);
    break;
  }

  case 'review': {
    const label = state.label || `plan-${state.id}`;
    console.log(`${BOLD}Instructions for Claude (${stage.model}):${RESET}`);
    console.log('');
    console.log('  Review the implementation from the execute stage.');
    console.log('');

    // Show closed issues for context
    try {
      const issuesJson = execSync(
        `gh issue list --label "${label}" --state closed --json number,title --limit 20 2>/dev/null`,
        { encoding: 'utf8' }
      );
      const issues = JSON.parse(issuesJson);
      if (issues.length > 0) {
        console.log('  Completed issues:');
        issues.forEach(issue => {
          console.log(`    #${issue.number}: ${issue.title}`);
        });
        console.log('');
      }
    } catch {
      // gh not available, skip
    }

    console.log('  Review checklist:');
    console.log('    - Does the implementation match the refined plan?');
    console.log('    - Are all acceptance criteria met?');
    console.log('    - Are there any bugs, edge cases, or regressions?');
    console.log('    - Is the code clean and well-tested?');
    console.log('    - Are there follow-up tasks needed?');
    console.log('');
    console.log('  ${BOLD}Inner Loop: Fix bugs found during review${RESET}');
    console.log('  If issues are found:');
    console.log('    1. Create new GH issues for the bugs (label them with the plan label)');
    console.log('    2. Run: ${BOLD}/plan-execute stage execute${RESET} (go back to implement fixes)');
    console.log('    3. After fixes are done, run: ${BOLD}/plan-execute stage review${RESET} (loop back here)');
    console.log('');
    console.log('  When review is complete with no remaining issues:');
    console.log(`    Run: ${BOLD}/plan-execute done${RESET}`);
    break;
  }
  }
}

function status() {
  const state = loadState();
  if (!state) {
    console.log('No active plan. Start one with: /plan-execute start "description"');
    return;
  }

  const stage = STAGES[state.stage];
  const label = state.label || `plan-${state.id}`;

  console.log(`\n${BOLD}Plan & Execute Status${RESET}`);
  console.log(`${'═'.repeat(50)}`);
  console.log(`  ID:          ${state.id}`);
  console.log(`  Description: ${state.description}`);
  console.log(`  Stage:       ${stage.color}${stage.emoji} ${stage.label}${RESET} (model: ${stage.model})`);
  console.log(`  Label:       ${label}`);
  console.log(`  Created:     ${state.created}`);
  console.log('');

  // Show stage progress
  console.log('  Stages:');
  const stageOrder = ['draft', 'refine', 'issues', 'execute', 'review'];
  const currentIdx = stageOrder.indexOf(state.stage);
  stageOrder.forEach((s, i) => {
    const st = STAGES[s];
    const marker = i < currentIdx ? '✅' : (i === currentIdx ? '👉' : '  ');
    console.log(`    ${marker} ${st.color}${st.label}${RESET} (${st.model})`);
  });
  console.log('');

  // Show current task status
  console.log(`${BOLD}Current Task:${RESET}`);
  const isDone = validateStageCompletion(state.stage, state);
  if (!isDone) {
    console.log(`  ⏳ Still working on ${stage.label} stage`);
    console.log(`  ⏸️  When done, run: /plan-execute next`);
  } else {
    console.log(`  ✅ ${stage.label} stage is complete`);
    console.log(`  ▶️  Next: /plan-execute next`);
  }
  console.log('');

  // Show file status
  console.log(`${BOLD}Files:${RESET}`);
  const planPath = getPlanPath(state);
  const refinedPath = getRefinedPlanPath(state);
  if (fs.existsSync(planPath)) {
    const isDraftDone = !fs.readFileSync(planPath, 'utf8').includes('_Draft pending');
    console.log(`  ${isDraftDone ? '✅' : '⏳'} Draft plan: ${planPath}`);
  }
  if (fs.existsSync(refinedPath)) {
    console.log(`  ✅ Refined plan: ${refinedPath}`);
  }
  console.log('');
}

function done() {
  const state = loadState();
  if (!state) {
    console.log('No active plan.');
    return;
  }

  state.stage = 'done';
  state.completed = new Date().toISOString();
  saveState(state);

  // Archive
  const archivePath = path.join(STATE_DIR, `completed-${state.id}.json`);
  fs.copyFileSync(CURRENT_FILE, archivePath);
  fs.unlinkSync(CURRENT_FILE);

  console.log(`\n${BOLD}✅ Plan & Execute Complete!${RESET}`);
  console.log(`${'═'.repeat(50)}`);
  console.log(`  Description: ${state.description}`);
  console.log(`  Archived:    ${archivePath}`);
  console.log('');
}

function listPlans() {
  ensureStateDir();

  const current = loadState();
  const archives = fs.readdirSync(STATE_DIR)
    .filter(f => f.startsWith('completed-') && f.endsWith('.json'));

  console.log(`\n${BOLD}Plan & Execute History${RESET}`);
  console.log(`${'═'.repeat(50)}`);

  if (current) {
    const stage = STAGES[current.stage];
    console.log(`\n  ${BOLD}Active:${RESET} ${current.description}`);
    console.log(`    Stage: ${stage?.color || ''}${stage?.label || current.stage}${RESET}`);
    console.log(`    ID: ${current.id}`);
  } else {
    console.log('\n  No active plan.');
  }

  if (archives.length > 0) {
    console.log(`\n  ${BOLD}Completed:${RESET}`);
    archives.forEach(f => {
      const data = JSON.parse(fs.readFileSync(path.join(STATE_DIR, f), 'utf8'));
      console.log(`    ${data.id}: ${data.description} (${data.completed || 'unknown'})`);
    });
  }
  console.log('');
}

function showHelp() {
  console.log(`
${BOLD}📋 Plan & Execute - Multi-Model Workflow${RESET}
${'═'.repeat(50)}

Orchestrates planning and implementation across Claude models:

  ${STAGES.draft.color}${STAGES.draft.emoji} DRAFT${RESET}   (haiku)  - Fast initial plan
  ${STAGES.refine.color}${STAGES.refine.emoji} REFINE${RESET}  (opus)   - Critical review & extension
  ${STAGES.issues.color}${STAGES.issues.emoji} ISSUES${RESET}  (sonnet) - Break into GH issues
  ${STAGES.execute.color}${STAGES.execute.emoji} EXECUTE${RESET} (haiku)  - Implement each issue
  ${STAGES.review.color}${STAGES.review.emoji} REVIEW${RESET}  (opus)   - Evaluate implementation

${BOLD}Commands:${RESET}

  /plan-execute start "description"  Start a new plan
  /plan-execute next                 Advance to next stage
  /plan-execute status               Show current state
  /plan-execute stage <name>         Jump to a specific stage
  /plan-execute list                 Show plan history
  /plan-execute done                 Mark workflow complete
  /plan-execute help                 Show this help

${BOLD}Workflow:${RESET}

  1. /plan-execute start "add caching layer"
  2. Switch to haiku (/model haiku), draft the plan
  3. /plan-execute next
  4. Switch to opus (/model opus), refine the plan
  5. /plan-execute next
  6. Switch to sonnet (/model sonnet), create GH issues
  7. /plan-execute next
  8. Switch to haiku (/model haiku), implement issues
  9. /plan-execute next
  10. Switch to opus (/model opus), review implementation
  11. /plan-execute done
`);
}

// --- Main ---

if (require.main === module) {
  switch (command) {
  case 'start':
  case 'begin':
  case 'new':
    start(args);
    break;
  case 'next':
  case 'advance':
    advance();
    break;
  case 'stage':
  case 'goto':
    advance(args.trim());
    break;
  case 'status':
  case 'show':
    status();
    break;
  case 'done':
  case 'complete':
  case 'finish':
    done();
    break;
  case 'list':
  case 'history':
    listPlans();
    break;
  case 'help':
  default:
    showHelp();
  }
} else {
  module.exports = {
    loadState,
    saveState,
    STAGES,
    start,
    advance,
    status,
    done
  };
}
