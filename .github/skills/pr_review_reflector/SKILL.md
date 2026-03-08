---
name: pr-review-reflector
description: 'Analyze PR review comments, propose changes, validate via lint/tests, and automate commits/replies in English.'
---

# PR Review Reflector
This skill automates the reflection of GitHub Pull Request reviews by analyzing feedback, applying verified code changes, and maintaining professional English communication.

## Process
1. **Analyze & Propose**: Fetch PR details and review comments.
   - Evaluate each comment to determine if it requires a code change.
2. **Review Guidelines**: Read `${workspaceFolder}/.vscode/git_commit_guide.md` to ensure all automated commits strictly follow the project's formatting rules.
3. **Apply Changes**: Modify the source code based on the approved feedback.
   - If a comment is ambiguous, use the appropriate PR tool to ask the reviewer for clarification in English.
4. **Validation Pipeline**: Execute the following quality checks in order:
   - **Lint & Format**: Run `ruff format` followed by `ruff --check` to ensure style compliance.
   - **Type Integrity**: Run `mypy` to verify static type safety. (--strict is required for all type checks)
   - **Functional Testing**: Run `pytest` to ensure no regressions were introduced.
   - *Error Handling*: If any validation fails, analyze the output, fix the code, and restart the validation pipeline.
5. **Commit & Push**: Once all validations pass, commit and push the changes.
   - The commit message must align with the guidelines retrieved in Step 2.
6. **Finalize in English**: Update the PR status and reply to the review comments.
   - **[Constraint]** Omit polite fillers (e.g., "Thank you," "I appreciate"). Focus on technical facts.
      - **Content Structure**: 
      1. **Action Taken**: Briefly state what was changed (e.g., "Updated `logic.py` to handle null values").
      2. **Validation Result**: Confirm the fix passed `ruff`, `mypy`, and `pytest`.
      3. **Status**: Clearly state "Resolved" or "Fixed."

## Requirements
- **Tool Autonomy**: Select the most appropriate tools for reading, editing, executing terminal commands, or interacting with GitHub (MCP-native) without being restricted to specific tool names.
- **English-First Policy**: Maintain all external communication (comments, commits, logs) in English to support global collaboration.
- **Context Preservation**: Respect the existing code architecture. Avoid unnecessary refactoring unless specifically requested by the reviewer.