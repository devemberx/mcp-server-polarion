# mcp-server-polarion

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server for **Polarion ALM**. Lets AI assistants read documents, work items, and traceability links directly from your Polarion instance.

[![PyPI](https://img.shields.io/pypi/v/mcp-server-polarion)](https://pypi.org/project/mcp-server-polarion/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## Prerequisites

This server is distributed as a Python package and requires [**uv**](https://docs.astral.sh/uv/) to run.

**Install uv** (if not already installed):

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Or via pip:

```bash
pip install uv
```

No other installation is needed — `uvx mcp-server-polarion` downloads and runs the server automatically.

---

## Setup

### Environment Variables

| Variable | Description | Example |
|---|---|---|
| `POLARION_URL` | Base URL of your Polarion instance | `https://polarion.example.com` |
| `POLARION_TOKEN` | Personal Access Token for authentication | `your-personal-access-token` |

### VS Code (GitHub Copilot)

Add to `.vscode/mcp.json`:

```json
{
  "servers": {
    "mcp-server-polarion": {
      "type": "stdio",
      "command": "uvx",
      "args": ["mcp-server-polarion"],
      "env": {
        "POLARION_URL": "https://polarion.example.com",
        "POLARION_TOKEN": "your-personal-access-token"
      }
    }
  }
}
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mcp-server-polarion": {
      "command": "uvx",
      "args": ["mcp-server-polarion"],
      "env": {
        "POLARION_URL": "https://polarion.example.com",
        "POLARION_TOKEN": "your-personal-access-token"
      }
    }
  }
}
```

### Cursor

Add to Cursor MCP settings:

```json
{
  "mcpServers": {
    "mcp-server-polarion": {
      "command": "uvx",
      "args": ["mcp-server-polarion"],
      "env": {
        "POLARION_URL": "https://polarion.example.com",
        "POLARION_TOKEN": "your-personal-access-token"
      }
    }
  }
}
```

## Tools

| Tool | Description |
|---|---|
| `list_projects` | List all accessible Polarion projects (supports Lucene query filtering) |
| `list_documents` | List documents in a project (with optional name/space filtering) |
| `get_document` | Get full document content in Markdown |
| `get_document_parts` | List structural parts (headings, work items) with part IDs |
| `list_work_items` | Search work items with Lucene queries (e.g. `type:requirement`) |
| `get_work_item` | Get full work item details including description in Markdown |
| `get_linked_work_items` | Get all forward and back links for traceability |

All list tools support pagination via `page_size` (1–100) and `page_number` parameters.

## Example Prompts

> "List all projects in Polarion"

> "Show me the documents in project MCPT"

> "Read the Software Requirement Specification document in project MCPT"

> "Find all approved requirements in project MCPT"

> "What work items are linked to MCPT-001?"

## License

[MIT](LICENSE)