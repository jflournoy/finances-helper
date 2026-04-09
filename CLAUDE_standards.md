# Code & Quality Standards

## Development Workflow

- Always run quality checks before commits
- Use custom commands for common tasks
- Document insights and decisions
- Estimate Claude usage before starting tasks
- Track actual vs estimated Claude interactions

## Quality Standards

- Zero errors policy
- All tests passing before commit
- No warnings in critical paths

## Testing Standards

**CRITICAL: Any error during test execution = test failure**

- **Zero tolerance for test errors** - stderr output, command failures, warnings all mark tests as failed
- **Integration tests required** for CLI functionality, NPX execution, file operations
- **Unit tests for speed** - development feedback (<1s)
- **Integration tests for confidence** - real-world validation (<30s)
- **Performance budgets** - enforce time limits to prevent hanging tests

## Markdown Standards

**All markdown files must pass validation before commit**

- **Syntax validation** - Uses remark-lint to ensure valid markdown syntax
- **Consistent formatting** - Enforces consistent list markers, emphasis, and code blocks
- **Link validation** - Checks that internal links point to existing files
- **Auto-fix available** - Run `npm run markdown:fix` to auto-correct formatting issues

### Markdown Quality Checks

- `npm run markdown:lint` - Validate all markdown files
- `npm run markdown:fix` - Auto-fix formatting issues
- Included in `hygiene:quick` and `commit:check` scripts
- CI validates markdown on every push/PR

### Markdown Style Guidelines

- Use `-` for unordered lists
- Use `*` for emphasis, `**` for strong emphasis
- Use fenced code blocks with language tags
- Use `.` for ordered list markers
- Ensure all internal links are valid

## Architecture Principles

- Keep functions under 15 complexity
- Code files under 400 lines
- Comprehensive error handling
- Prefer functional programming patterns
- Avoid mutation where possible

## Project Standards

- Test coverage: 60% minimum
- Documentation: All features documented
- Error handling: Graceful failures with clear messages
- Performance: Monitor code complexity and file sizes
- ALWAYS use atomic commits
- Use emojis judiciously
- NEVER Update() a file before you Read() the file
