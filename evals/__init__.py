"""Pre-deploy eval gate: drives an LLM agent through the in-memory MCP server
against mocked Polarion, asserting no destructive / footgun action.
Deterministic — no LLM judge. See evals/README.md."""
