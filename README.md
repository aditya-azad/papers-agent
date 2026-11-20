# Papers Agent

An MCP server that exposes scholarly paper source operations (search, metadata, references, PDF text and image extraction) as tools for AI coding agents.

- Python ≥ 3.14
- [uv](https://docs.astral.sh/uv/)

## Contents

1. [Prerequisites](#prerequisites)
2. [Build and run](#build-and-run)
3. [The arXiv metadata database](#the-arxiv-metadata-database)
4. [Install the MCP server](#install-the-mcp-server)
   - [pi](#pi)
   - [Claude Code](#claude-code)
   - [OpenCode](#opencode)
   - [Codex](#codex)
5. [Install the paper-summary skill](#install-the-paper-summary-skill)
6. [Available tools](#available-tools)

## Prerequisites

Install Python 3.14+ and UV. Pick your operating system:

### Linux / macOS

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Or if you use [mise](https://mise.jdx.dev/) to manage your development environment, just run the follwing in this directory to install Python 3.14 and UV:

```sh
mise install
```

### Windows (PowerShell)

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## Build and run

Clone the repository and install dependencies:

```sh
git clone <repo-url>
cd papers-agent
uv sync
```

Smoke-test the server by running it directly (it should start and wait on stdio; `Ctrl+C` to exit):

```sh
uv run papers-agent
```

## The arXiv metadata database

The server prefers a local arXiv metadata database (with full-text search and date-range queries) over the live arXiv API when one is available. It ships with an importer for the [Kaggle arXiv dataset](https://www.kaggle.com/datasets/Cornell-University/arxiv?resource=download), a JSON-lines snapshot of the [arXiv raw metadata](https://info.arxiv.org/help/bulk_data.html).

### Download

Download the dataset from the Kaggle page above (requires a free Kaggle account). The zip contains a single ~5.7 GB file, `arxiv-metadata-oai-snapshot.json`, with one JSON record per paper (~2.5M papers).

### Import

The importer reads the JSON (directly from the zip, no need to extract it) and writes the search database this repository expects. It takes the input file (the Kaggle `archive.zip` or the extracted `arxiv-metadata-oai-snapshot.json`) and writes the papers into the `backup_db` path from [Configuration](#configuration):

```sh
uv run python -m papers_agent.sources.kaggle_import /path/to/archive.zip
```

Importing all ~2.5M records takes a few minutes and produces a multi-gigabyte database.

### Indexing

No separate build step is needed. On first use the server checks whether the papers in the search database are already indexed (full-text search, pub-date and DOI indexes) and builds the indexes automatically in the background if they are not. It re-indexes automatically whenever the database contents change; the `backup_status` MCP tool reports indexing progress.

### Configuration

The search database is configured once in `~/.config/papers-agent/config.toml` and never set again:

```toml
[arxiv]
backup_db = "/path/to/backup.sqlite"
```

The config file is read directly by the server, so no environment variables are needed in your MCP server configuration.

## Install the MCP server

All agents below expect the absolute path to this checkout. Set it once so you can paste the commands as-is:

- Linux / macOS: `export PAPERS_AGENT_DIR="$(pwd)"`
- Windows PowerShell: `$PAPERS_AGENT_DIR = (Get-Location).Path`

### pi

Install globally into your pi agent:

```sh
pi mcp add papers -- uv run --directory "$PAPERS_AGENT_DIR" papers-agent
```

Verify with:

```sh
pi mcp list
```

Or add it manually to `~/.pi/agent/mcp.json` (global) or `.pi/mcp.json` (project-only):

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

### Claude Code

```sh
claude mcp add papers -- uv run --directory "$PAPERS_AGENT_DIR" papers-agent
```

Or add it to `~/.claude.json` (global) or `.mcp.json` (project root, shareable via git):

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

Verify with:

```sh
claude mcp list
```

> **Windows note:** native Windows builds of Claude Code launch MCP servers through `cmd`, so wrap the command instead:
>
> ```json
> {
>   "mcpServers": {
>     "papers": {
>       "command": "cmd",
>       "args": ["/c", "uv", "run", "--directory", "C:\\path\\to\\papers-agent", "papers-agent"]
>     }
>   }
> }
> ```

### OpenCode

Add the server to `~/.config/opencode/opencode.json` (global) or `opencode.json` (project):

```json
{
  "mcp": {
    "papers": {
      "type": "local",
      "command": ["uv", "run", "--directory", "/path/to/papers-agent", "papers-agent"],
      "enabled": true
    }
  }
}
```

> **Windows note:** the command array can't resolve `uv` directly on some setups; use `cmd` as the entrypoint:
>
> ```json
> {
>   "mcp": {
>     "papers": {
>       "type": "local",
>       "command": ["cmd", "/c", "uv", "run", "--directory", "C:\\path\\to\\papers-agent", "papers-agent"],
>       "enabled": true
>     }
>   }
> }
> ```

Verify by running `opencode` and checking that the `papers` server appears in `/mcp` (or the MCP section of `/status`).

### Codex

Codex configures MCP servers in `~/.codex/config.toml` (TOML, not JSON):

```toml
[mcp_servers.papers]
command = "uv"
args = ["run", "--directory", "/path/to/papers-agent", "papers-agent"]
```

Verify by starting `codex` and running `/mcp` — the `papers` server should be listed as connected.

## Install the paper-summary skill

The repository ships a companion skill for the [pi](https://github.com/badlogic/pi-mono) agent: `paper-summary`, a fixed structured note format covering architecture, inputs, outputs, loss functions, training procedure, evaluation, contributions over prior work, limitations, and future work.

It is intentionally **not** placed in the auto-discovered project skills directory (`.agents/skills/`): it is meant to be installed into your agent, not tied to this checkout.

Point pi at the checkout's `skills/` directory in `~/.pi/agent/settings.json`:

```json
{
  "skills": ["/path/to/papers-agent/skills"]
}
```

The skill is available as `paper-summary` in new sessions.

For other agents, copy or symlink the skill into their skills directory, e.g. `~/.claude/skills/paper-summary` (Claude Code) or `~/.config/opencode/skills/paper-summary` (OpenCode).

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
