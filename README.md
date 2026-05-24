# mcp-server-polarion

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server for **Polarion ALM**. Lets AI assistants read documents, work items, and traceability links — and create, update, and reorganize work items — directly from your Polarion instance.

[![PyPI](https://img.shields.io/pypi/v/mcp-server-polarion)](https://pypi.org/project/mcp-server-polarion/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## Prerequisites

> **Polarion 2506 or higher** is required. Earlier versions lack REST API endpoints this server depends on.

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
| `POLARION_VERIFY_SSL` | Verify TLS certificates (default `true`). Set `false` for self-signed certs on trusted networks. | `true` |

> MCP client `env` objects must use **string** values, so booleans are quoted (e.g. `"POLARION_VERIFY_SSL": "true"`). The server parses `"true"` / `"false"` into a real `bool`.

<details>
<summary><b>VS Code (GitHub Copilot)</b></summary>

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
        "POLARION_TOKEN": "your-personal-access-token",
        "POLARION_VERIFY_SSL": "true"
      }
    }
  }
}
```

</details>

<details>
<summary><b>Claude Desktop</b></summary>

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mcp-server-polarion": {
      "command": "uvx",
      "args": ["mcp-server-polarion"],
      "env": {
        "POLARION_URL": "https://polarion.example.com",
        "POLARION_TOKEN": "your-personal-access-token",
        "POLARION_VERIFY_SSL": "true"
      }
    }
  }
}
```

</details>

<details>
<summary><b>Cursor</b></summary>

Add to Cursor MCP settings:

```json
{
  "mcpServers": {
    "mcp-server-polarion": {
      "command": "uvx",
      "args": ["mcp-server-polarion"],
      "env": {
        "POLARION_URL": "https://polarion.example.com",
        "POLARION_TOKEN": "your-personal-access-token",
        "POLARION_VERIFY_SSL": "true"
      }
    }
  }
}
```

</details>

<details>
<summary><b>Claude Code</b></summary>

Register via the `claude mcp add` command:

```bash
claude mcp add mcp-server-polarion \
  -e POLARION_URL=https://polarion.example.com \
  -e POLARION_TOKEN=your-personal-access-token \
  -e POLARION_VERIFY_SSL=true \
  -- uvx mcp-server-polarion
```

</details>

## Tools

### Read

| Tool | Description |
|---|---|
| `list_projects` | List accessible projects |
| `list_documents` | List documents in a project |
| `list_work_items` | Search work items with Lucene or SQL queries |
| `get_document` | Get document metadata, optionally with the raw body HTML |
| `read_document` | Render a document end-to-end as Markdown |
| `read_document_parts` | List a document's structural parts with embedded work item metadata |
| `get_work_item` | Get work item details with the body as raw HTML |
| `read_work_item` | Get work item details with the body as Markdown |
| `list_work_item_links` | List a work item's outgoing or incoming links |
| `list_document_comments` | List a document's comments with thread relationships |
| `list_document_enum_options` | Resolve valid enum ids for a document field |
| `list_work_item_enum_options` | Resolve valid enum ids for a work item field |

All list tools support pagination via `page_size` (1–100) and `page_number` parameters.

### Write

| Tool | Description |
|---|---|
| `create_work_item` | Create a new work item |
| `update_work_item` | Update an existing work item |
| `create_document` | Create a new document |
| `update_document` | Update document metadata, body, or workflow status |
| `create_work_item_links` | Create one or more outgoing links from a source work item |
| `update_work_item_links` | Update `suspect` / `revision` on one or more outgoing links |
| `delete_work_item_links` | Delete one or more outgoing links from a source work item |
| `move_work_item_to_document` | Attach a work item to a document at a chosen position |
| `move_work_item_from_document` | Detach a work item from its document |

## Example Prompts

> "List the documents in space 'Specifications' of project MCPT."

> "Read the SRS document of project MCPT and summarize each open requirement."

> "Find every approved requirement in project MCPT whose title starts with 'Auth' and show me their owning document."

> "Show the outgoing and incoming links for MCPT-042 and flag any child task that is still open."

> "Which requirements in the SRS document have no 'verifies' back link from a test case?"

> "List the valid status values for a defect in project MCPT, then move MCPT-077 to 'in_review'."

> "Create a task in project MCPT titled 'Refactor authentication module' and link it to MCPT-042 as 'relates_to'."

> "Add a new requirement under section 3.2 of the SRS document with the body I just drafted."

> "Move MCPT-201 into the SRS document right after MCPT-150."

> "Detach MCPT-077 from its document so I can rework it as a standalone task."

> "Bump MCPT-042's priority to 90, set severity to 'major', and approve the workflow."

## License

[MIT](LICENSE)