# Papers Agent

## Contents

1. [Publication sources](#publication-sources)
2. [Why this project exists](#why-this-project-exists)
3. [Prerequisites](#prerequisites)
4. [Build and run](#build-and-run)
5. [The arXiv metadata database](#the-arxiv-metadata-database)
6. [Install the MCP server](#install-the-mcp-server)
   - [pi](#pi)
   - [Claude Code](#claude-code)
   - [OpenCode](#opencode)
   - [Codex](#codex)
7. [Install the skills](#install-the-skills)
   - [pi](#pi-1)
   - [Claude Code](#claude-code-1)
   - [OpenCode](#opencode-1)
   - [Codex](#codex-1)
8. [Available tools](#available-tools)
9. [Architecture](#architecture)
10. [More publication sources](#more-publication-sources)

## Publication sources

- [x] arXiv
- [ ] bioRxiv
- [ ] OpenReview
- [ ] DOI-based lookup via Crossref

## Why this project exists

AI coding agents are great at writing code, but when it comes to scholarly papers they hit a wall. Ask an agent to "summarize this paper" and it either lacks the tools to search arXiv, pull metadata, or follow reference chains, or it has to trawl the live arXiv API, which is slow, rate-limited, and awkward to paginate through mid-task. Meanwhile, the paper's PDF sits on disk as a wall of two-column text that is tedious to extract cleanly.

This project grew out of wanting to just say "take these papers and give me structured notes" and have it actually work. It packages scholarly paper operations — search, metadata, references, PDF text and image extraction — as MCP tools that any AI coding agent can call directly. A local arXiv metadata database keeps searches fast and unlimited, so agents can explore the literature the way researchers do: iteratively and without waiting on a remote API.

The project is intentionally small and personal, but it is open for anyone to use, contribute to, or give feedback on — issues and pull requests are welcome. It stands on the work of others, notably [arXiv](https://arxiv.org) for open metadata access and the [Kaggle arXiv dataset](https://www.kaggle.com/datasets/Cornell-University/arxiv) that powers the local database.

## Prerequisites

Install Python 3.14+ and [uv](https://docs.astral.sh/uv/). Pick your operating system:

### Linux / macOS

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Or if you use [mise](https://mise.jdx.dev/) to manage your development environment, just run the following in this directory to install Python 3.14 and uv:

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
uv run python -m papers_agent.sources.arxiv_backup.kaggle_import /path/to/archive.zip
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

Verify with:

```sh
claude mcp list
```

Or add it manually to `~/.claude.json` (global) or `.mcp.json` (project root, shareable via git):

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

OpenCode has no install command, so add the server to `~/.config/opencode/opencode.json` (global) or `opencode.json` (project):

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

Codex has no install command either and configures MCP servers in `~/.codex/config.toml` (TOML, not JSON):

```toml
[mcp_servers.papers]
command = "uv"
args = ["run", "--directory", "/path/to/papers-agent", "papers-agent"]
```

Verify by starting `codex` and running `/mcp` — the `papers` server should be listed as connected.

## Install the skills

The repository ships companion [skills](skills/) for AI coding agents.

Skills are intentionally **not** placed in the auto-discovered project skills directory (`.agents/skills/`): they are meant to be installed into your agent, not tied to this checkout.

All agents below expect the absolute path to this checkout as `$PAPERS_AGENT_DIR` (see [Install the MCP server](#install-the-mcp-server)).

### pi

Point pi at the checkout's `skills/` directory in `~/.pi/agent/settings.json`:

```json
{
  "skills": ["/path/to/papers-agent/skills"]
}
```

### Claude Code

Claude Code loads personal skills from `~/.claude/skills/`. Symlink the skills in (a plain copy would drift out of date):

```sh
mkdir -p ~/.claude/skills
ln -s "$PAPERS_AGENT_DIR/skills/paper-summary" ~/.claude/skills/paper-summary
```

> **Windows note:** creating symlinks requires PowerShell running as Administrator (or Developer Mode). Alternatively, copy: `Copy-Item -Recurse "$env:PAPERS_AGENT_DIR\skills\paper-summary" "$env:USERPROFILE\.claude\skills\paper-summary"`.

### OpenCode

OpenCode loads global skills from `~/.config/opencode/skills/`, or from extra sources via the `skills` array in `opencode.json` (project) or `~/.config/opencode/opencode.json` (global) — the latter means no copying, pointing straight at the checkout:

```json
{
  "skills": ["/path/to/papers-agent/skills"]
}
```

If you already installed the skills for Claude Code, OpenCode picks up `~/.claude/skills` as a compatibility source and you don't need anything else.

### Codex

Codex reads user-level skills from `~/.agents/skills/` and follows symlinked skill directories:

```sh
mkdir -p ~/.agents/skills
ln -s "$PAPERS_AGENT_DIR/skills/paper-summary" ~/.agents/skills/paper-summary
```

## Available tools

Most tools come in `..._by_url` / `..._by_doi` pairs that do the same job and differ only in how the paper is identified. For the `url` variants the source and doi are inferred automatically, so you can pass an arXiv abstract or pdf page link straight from a search result. A paper's *citation key* is the short identifier used to cite it (for example `vaswani2017attention`).

| Tool | Description |
|---|---|
| `get_sources` | List the names of all registered paper sources. |
| `keyword_search` | Keyword search against a paper source (arXiv). |
| `semantic_search` | Semantic search by natural-language description (arXiv via alphaXiv embeddings). |
| `get_papers_in_date_range` | List papers from a source published within a date range. |
| `get_paper_metadata_by_url` | Fetch the metadata of a single paper by url. |
| `get_paper_metadata_by_doi` | Fetch the metadata of a single paper by doi. |
| `get_paper_metadata_references_by_doi` | Fetch the bibliography of a paper as raw reference strings. |
| `get_paper_metadata_references_by_url` | Fetch the bibliography of a paper as raw reference strings. |
| `get_paper_metadata_citation_key_by_url` | Return the citation key of a paper. |
| `get_paper_metadata_citation_key_by_doi` | Return the citation key of a paper. |
| `get_paper_pdf_text_by_doi` | Return the full text extracted from a paper's pdf. |
| `get_paper_pdf_text_by_url` | Return the full text extracted from a paper's pdf. |
| `get_paper_pdf_images_by_doi` | Extract a paper's pdf images into a directory and return their file paths, named `<citation key>_image_<number>.png`. |
| `get_paper_pdf_images_by_url` | Extract a paper's pdf images into a directory and return their file paths, named `<citation key>_image_<number>.png`. |

## Architecture

The project is a small, layered Python package (`src/papers_agent/`, ~2,500 lines). Each layer only knows the one below it, so any layer can be read on its own:

1. **MCP tools** (`server.py`) — one function per tool from the table above, plus `get_sources` and `backup_status`. When a tool receives a url or doi, it picks the right source automatically; the agent never has to.
2. **Caching** (`cache.py`, `cached.py`) — every source is wrapped in a cache that stores lookups in a small sqlite database (`~/.papers-agent-cache/`) for three days. Repeated questions are answered instantly, and stale entries are evicted before every tool call.
3. **Sources** (`sources/arxiv/`, `sources/arxiv_backup/` — one directory per source) — this is where the actual paper knowledge lives, behind one shared abstraction (`PaperSource` in `types.py`). Every source is a composite: it holds a list of sub-sources and its default method implementations query all of them together and merge their answers — sub-sources that error out simply don't contribute to the results — so composition is part of the source architecture, not a source of its own:
   - `ArxivBackupSource` — the local arXiv metadata database from [The arXiv metadata database](#the-arxiv-metadata-database), for fast, unlimited search and lookups.
   - `ArxivSource` — the live arXiv API (and alphaXiv for semantic search), used when the local database is missing or doesn't have the paper.
   - The registered `arxiv` source composes the two, querying the local database and the live API together and merging whatever each answers, so tools work with or without the multi-gigabyte download.
4. **PDF handling** (`pdf.py`) — downloads a paper's pdf to a temporary file and uses PyMuPDF to pull out plain text and images. `citation_key.py` turns a paper into the short identifier used to name those files and cite them.
5. **Plumbing** (`config.py` reads `~/.config/papers-agent/config.toml`; `errors.py` and `types.py` hold the shared error and paper types; `sources/arxiv_backup/index.py` builds the full-text, pub-date and doi indexes described under [Indexing](#indexing); `sources/arxiv_backup/kaggle_import.py` is the importer CLI).

A typical tool call travels the whole stack: the agent asks for a paper by url → `server.py` recognises the arXiv link (`sources/arxiv/paper.py`) → the cached composite source checks the three-day cache, then queries the local database and the live API together and merges their answers → the pdf is downloaded and extracted only when text or images were asked for.

## More publication sources

Everything today works with arXiv, but the project was built for more. None of the tools are arXiv-specific: they talk to sources through the single `PaperSource` abstraction described in [Architecture](#architecture), and the server already routes by doi and url instead of hard-coding arXiv addresses. A new publisher means implementing a `PaperSource` subclass — a leaf with no sub-sources that overrides what it supports (search, metadata, references, and a way to reach the paper's pdf) — and registering it in `server.py`. The tool table stays the same: `get_sources` lists the new source by name, and the url and doi routing picks it up automatically.

Support for additional publication sources is planned for a future release — see [Publication sources](#publication-sources) for what is on the list and what has landed. If there is a venue or publisher you would like to see supported, say so in an issue — it directly shapes the priority order.
