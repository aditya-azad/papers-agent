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

No external accounts or API keys are required: the arXiv API and the
alphaXiv embedding API are public, though both are rate-limited.

### Local arXiv metadata database (optional)

The arXiv source can serve metadata from a local arxiv metadata
database instead of calling the arXiv API. The database (default:
`~/.local/share/papers-agent/arxiv/metadata.sqlite`, override via
`PAPERS_AGENT_ARXIV_BACKUP_DB`) is a writable sqlite file holding the
papers of an arXiv metadata dump (e.g. the Kaggle `arxiv` dataset,
located via `PAPERS_AGENT_ARXIV_DUMP_DB`, default:
`~/database/third-party-media/papers/arxiv/metadata.sqlite`) plus
derived indexes: a doi map, first-submission dates and a full-text
search index.

| Operation | Served from | When the live API is used |
|---|---|---|
| paper metadata by url/doi, citation keys, batch hydration | database | paper missing from the database |
| keyword search | database full-text index | fewer than `limit` database hits (merges both) |
| papers in date range | database | range reaches past the database's cutoff |
| references, pdf text, pdf images | live arxiv | always (not in the database) |

The database is kept in sync automatically: on first use the server
compares a fingerprint of the upstream dump against the one recorded in
the database and, on mismatch, spawns a detached sync subprocess. The
sync diffs the dump against the database and patches only changed,
new and vanished rows — including their derived rows and full-text
entries. The first run (or a database without recorded metadata)
performs a one-time full index build. While syncing, queries fall back
to the live API. Concurrent syncs are impossible (flock guard), and
the `backup_status` tool reports the current state (ready, syncing
with progress, stale, missing).

No manual steps are needed: the first sync builds the database from
the dump directly (the dump itself is never modified). The first build
takes a while and several GB of disk; afterwards each sync only
patches the rows the new dump changed.

## Skills

Paper-summarization guidance (a fixed structured note format covering architecture, inputs, outputs, loss functions, training procedure, evaluation, contributions over prior work, limitations, and future work) lives in a companion skill for the [pi](https://github.com/badlogic/pi-mono) agent, shipped in this repository under `skills/`. The skill is intentionally not placed in the auto-discovered project skills directory (`.agents/skills/`): it is meant to be installed into your pi agent, not tied to this checkout.

## Install the skill into your pi agent

Point pi at the checkout's `skills/` directory via your agent settings (`~/.pi/agent/settings.json`):

```json
{
  "skills": ["/path/to/papers-agent/skills"]
}
```

The skill is available as `paper-summary` in new sessions.

## Available tools

| Tool | Description |
|---|---|
| `get_sources` | List the names of all registered paper sources. |
| `keyword_search` | Keyword search against a paper source (arXiv). |
| `semantic_search` | Semantic search by natural-language description (arXiv via alphaXiv embeddings). |
| `get_papers_in_date_range` | List papers from a source published within a date range. |
| `get_paper_metadata_by_url` | Fetch the metadata of a single paper by its url (arXiv abstract or pdf page); the source is inferred from the url. |
| `get_paper_metadata_by_doi` | Fetch the metadata of a single paper by its doi; the source is inferred from the doi. |
| `get_paper_metadata_references_by_doi` | Fetch the bibliography of a paper (by doi) as raw reference strings. |
| `get_paper_metadata_references_by_url` | Fetch the bibliography of a paper at the given url (arXiv abstract or pdf page) as raw reference strings; the doi is inferred from the url. |
| `get_paper_metadata_citation_key_by_url` | Return the citation key of a paper by url. |
| `get_paper_metadata_citation_key_by_doi` | Return the citation key of a paper by doi. |
| `get_paper_pdf_text_by_doi` | Return the full text extracted from the pdf of a paper (by doi). |
| `get_paper_pdf_text_by_url` | Return the full text extracted from the pdf of a paper at the given url (arXiv abstract or pdf page); the doi is inferred from the url. |
| `get_paper_pdf_images_by_doi` | Extract the images of a paper's pdf (by doi) into a directory and return their file paths, named `<citation key>_image_<number>.png`. |
| `get_paper_pdf_images_by_url` | Extract the images of a paper's pdf (by url, arXiv abstract or pdf page) into a directory and return their file paths, named `<citation key>_image_<number>.png`; the doi is inferred from the url. |

