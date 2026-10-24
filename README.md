# Papers Agent

An MCP server that exposes [describe the domain] operations as tools, so
agents can [describe capabilities]. All operations are restricted to
[describe any policy/scope].

Tool-use guidance (how to use the tools well, conventions, and which tools
to call when) lives in companion skills for the
[pi](https://github.com/badlogic/pi-mono) agent, shipped in this repository
under `skills/`. The skills are intentionally not placed in the
auto-discovered project skills directory (`.agents/skills/`): they are meant
to be installed into your pi agent, not tied to this checkout.

## Setup

1. Clone this repository and run

```sh
mise install
uv sync
```

2. [Describe the external dependency, e.g. a local app/API, with config
   env vars.]

3. **Install the MCP server** into your pi agent:

```sh
pi mcp add -l reference -- uv run --directory "$(pwd)" reference-agent
```

This adds a project-level (`-l`) stdio server entry that runs the server
via uv, so no separate manual launch is needed. Verify the connection with:

```sh
pi mcp list
```

Alternatively, add it manually to `.pi/mcp.json` (project) or
`~/.pi/agent/mcp.json` (user):

```json
{
  "mcpServers": {
    "reference": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/reference-agent", "reference-agent"]
    }
  }
}
```

You can also run the server directly from this repository to test it:

```sh
uv run reference-agent
```

## Available tools

| Tool | Description |
|---|---|
| `keyword_search` | Keyword search against a paper source (arXiv). |
| `get_papers_in_date_range` | List papers from a source published within a date range. |
| `get_paper_by_url` | Fetch a single paper by its url. |
| `get_paper_by_doi` | Fetch a single paper by its doi. |
| `get_paper_authors` | Fetch the authors of a paper by doi. |
| `get_paper_abstract` | Fetch the abstract of a paper by doi. |

## Install the skills into your pi agent

Point pi at the checkout's `skills/` directory via your agent settings
(`~/.pi/agent/settings.json`):

```json
{
  "skills": ["/path/to/reference-agent/skills"]
}
```
