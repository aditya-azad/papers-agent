# Papers Agent

An MCP server that exposes scholarly paper source operations as tools.

## Setup

1. Clone this repository and run

```sh
mise install
uv sync
```

2. **Install the MCP server** into your pi agent:

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
      "args": ["run", "--directory", "/path/to/papers-agent", "reference-agent"]
    }
  }
}
```

You can also run the server directly from this repository to test it:

```sh
uv run reference-agent
```

No external accounts or API keys are required: the arXiv API and the
alphaXiv embedding API are public, though both are rate-limited.

## Available tools

| Tool | Description |
|---|---|
| `keyword_search` | Keyword search against a paper source (arXiv). |
| `semantic_search` | Semantic search by natural-language description (arXiv via alphaXiv embeddings). |
| `get_papers_in_date_range` | List papers from a source published within a date range. |
| `get_paper_by_url` | Fetch a single paper by its url; the source is inferred from the url. |
| `get_paper_by_doi` | Fetch a single paper by its doi; the source is inferred from the doi. |
| `get_paper_authors` | Fetch the authors of a paper by doi. |
| `get_paper_abstract` | Fetch the abstract of a paper by doi. |
| `download_paper_pdf` | Download the pdf of a paper (by doi) to a given file path. |
| `get_paper_pdf_text` | Return the full text extracted from the pdf of a paper (by doi). |
| `get_paper_pdf_images` | Extract the images of a paper's pdf (by doi) into a directory and return their file paths. |

Anticipated failures (unknown source, unresolvable identifier, failed
upstream request, missing paper, invalid date range) are surfaced as tool
errors with a readable message so agents can recover.
