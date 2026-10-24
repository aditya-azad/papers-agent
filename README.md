# Papers Agent

An MCP server that exposes scholarly paper source operations as tools.

## Setup

1. Clone this repository and run

```sh
mise install
uv sync
```

2. **Install the MCP server** into your pi agent globally:

```sh
pi mcp add papers -- uv run --directory "$(pwd)" papers-agent
```

Verify the connection with:

```sh
pi mcp list
```

Alternatively, add it manually to `~/.pi/agent/mcp.json` (global) or `.pi/mcp.json` (project-only):

```json
{
  "mcpServers": {
    "papers": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/papers-agent", "papers-agent"]
    }
  }
}
```

You can also run the server directly from this repository to test it:

```sh
uv run papers
```

No external accounts or API keys are required: the arXiv API and the
alphaXiv embedding API are public, though both are rate-limited.

## Available tools

| Tool | Description |
|---|---|
| `get_sources` | List the names of all registered paper sources. |
| `keyword_search` | Keyword search against a paper source (arXiv). |
| `semantic_search` | Semantic search by natural-language description (arXiv via alphaXiv embeddings). |
| `get_papers_in_date_range` | List papers from a source published within a date range. |
| `get_paper_by_url` | Fetch a single paper by its url; the source is inferred from the url. |
| `get_paper_by_doi` | Fetch a single paper by its doi; the source is inferred from the doi. |
| `get_paper_authors` | Fetch the authors of a paper by doi. |
| `get_paper_abstract` | Fetch the abstract of a paper by doi. |
| `get_paper_references` | Fetch the bibliography of a paper (by doi) as raw reference strings. |
| `download_paper_pdf` | Download the pdf of a paper (by doi) to a given file path. |
| `get_paper_pdf_text` | Return the full text extracted from the pdf of a paper (by doi). |
| `get_paper_pdf_images` | Extract the images of a paper's pdf (by doi) into a directory and return their file paths. |

Anticipated failures (unknown source, unresolvable identifier, failed
upstream request, missing paper, invalid date range) are surfaced as tool
errors with a readable message so agents can recover.
