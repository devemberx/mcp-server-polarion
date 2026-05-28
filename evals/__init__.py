"""Pre-deploy evaluation suite for mcp-server-polarion.

Tier 1 ("forbidden behaviour") gate: drives an LLM agent through the real
in-memory MCP server against a mocked Polarion backend, then asserts the
agent never took a destructive / footgun action. Deterministic — no LLM
judge. See evals/README.md.
"""
