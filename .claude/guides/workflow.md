# Development Workflow & Collaboration

## Claude Usage Guidelines

- Use the `usage-estimator` agent before starting any non-trivial task
- Track actual Claude interactions vs estimates
- Optimize for message efficiency in complex tasks
- Budget Claude usage for different project phases

**Typical Usage Patterns**:
- **Bug Fix**: 10-30 messages
- **Small Feature**: 30-80 messages
- **Major Feature**: 100-300 messages
- **Architecture Change**: 200-500 messages

## Collaboration Guidelines

- Always add Claude as co-author on commits
- Run `/hygiene` before asking for help
- Use the built-in todo tracking for task capture, or `gh issue` for anything that outlives the session
- Record durable facts in the memory system, not in a file this repo maintains
- Use `/next` when deciding what to pick up

