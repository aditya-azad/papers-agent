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
uv run papers-agent
```

### HTTP transport

The same server can be served over Streamable HTTP instead of stdio:

```sh
uv run papers-agent --http                     # binds 127.0.0.1:8000
uv run papers-agent --http --host 0.0.0.0 --port 8080
```

The MCP endpoint is then `http://<host>:<port>/mcp`, which can be added to
agents over HTTP:

```sh
pi mcp add papers --url http://localhost:8000/mcp
```

The bind host/port can also be set via `PAPERS_AGENT_HOST` and
`PAPERS_AGENT_PORT`.

### Docker

Build and run the server in Docker (serves over HTTP on port 8000):

```sh
docker compose up -d --build
```

This exposes `http://localhost:8000/mcp` on localhost only. `docker run`
works too:

```sh
docker build -t papers-agent .
docker run -p 127.0.0.1:8000:8000 papers-agent
```

Tools that take a `path` argument (pdf download / image extraction) should
write under `/data`, which is a volume in the compose setup.

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
