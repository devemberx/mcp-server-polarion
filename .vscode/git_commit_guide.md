## Task: Generate a strictly formatted Git commit message based on provided code changes.

# 1. Type Selection (Industry Standard):
- feat: A new feature (e.g., a new MCP tool, resource, or prompt).
- fix: A bug fix (logic errors, protocol non-compliance).
- docs: Documentation only changes (README, docstrings, internal guides).
- refactor: Code change that neither fixes a bug nor adds a feature.
- perf: A code change that improves performance.
- test: Adding missing tests or correcting existing tests.
- ci: Changes to CI configuration files and scripts (e.g., GitHub Actions, workflows).
- chore: Changes to the build process or auxiliary tools and libraries (e.g., dependency updates in pyproject.toml or requirements.txt).

# 2. Scope Selection (NEVER OMIT):
- tool: Executable functions/logic provided to the AI (e.g., search_files, run_query).
- resource: Data or files exposed for the AI to read (e.g., logs, db_schema).
- prompt: Templates, system instructions, or context windows for the model.
- server: Core MCP server lifecycle, initialization, or internal state logic.
- transport: Communication layers between client and server (e.g., stdio, sse, http).
- config: Environment variables, .env files, or static application settings.
- deps: Python package management (e.g., pyproject.toml, requirements.txt, pip).
- project: Large-scale changes affecting multiple scopes simultaneously.
- meta: Repository maintenance (e.g., .github workflows, licenses, CI/CD).
- git: Git-specific configuration (e.g., .gitignore, pre-commit hooks).

# 3. Mandatory Format:
<type>(<scope>): <subject>

[BLANK LINE]
- <Why: High-level purpose>
- <What: Specific technical changes>

# 4. Strict Constraints:
- Subject:
  - Use the imperative, present tense ("add" not "added", "fix" not "fixes").
  - Start with lowercase.
  - No period (.) at the end.
  - Maximum 50 characters.
- Scope:
  - Use lowercase nouns (e.g., tool, server, git).
- Body:
  - Must follow a blank line after the subject.
  - Must contain exactly 2 bullet points.
  - Focus on Why and How.

# 4. Correct/Incorrect Examples (DO NOT DEVIATE):

## Case 1: Documentation
- INCORRECT: feat(docs): add guide (X) -> Never mix feat and docs.
- CORRECT: docs(prompt): add mcp tool development instructions (O)

## Case 2: Tense & Punctuation
- INCORRECT: fix(tool): Fixed the prompt injection bug. (X) -> Wrong tense, has period.
- CORRECT: fix(tool): prevent prompt injection in search tool (O)

## Case 3: Scope & Case
- INCORRECT: feat: add new resource (X) -> Missing scope.
- INCORRECT: feat(RESOURCE): Add new resource (X) -> Use lowercase for scope and subject.
- CORRECT: feat(resource): add user profile data provider (O)

## Case 4: Mandatory Body
- INCORRECT: docs(git): update readme (X) -> Missing mandatory blank line and 2 bullets.
- CORRECT: 
docs(git): update readme with setup instructions

- Provide step-by-step guide for local MCP server installation.
- Add environment variable requirements for API authentication. (O)
