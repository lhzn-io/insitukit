# AGENTS.md

Context for AI developer agents working in `lhzn-io/insitukit`. Human entry point: `README.md`.

## Architecture

- `src/insitukit/`: `coops.py` (NOAA CO-OPS; an HTTP 200 with an error body or "No data was found" is an empty
  result), `erddap.py` (ERDDAP tabledap with a per-instance budget, thread-safe pacing and an expiring failure
  cache), `stats.py`, `series.py` (the JSON series form), `store.py` (directory, Workers KV or GCS behind a name and
  size guard), and `identify` in `__init__.py`.
- `tests/`: offline unit tests; HTTP calls are replaced.
- Dependencies stay small: numpy, pandas, requests. Anything heavier is an optional extra (`gcs`).

## Rules

- **Public repository and package.** Name only public data sources. No site-, partner- or project-specific code,
  defaults or examples beyond public stations; those belong in the projects that use the library.
- **Polite clients.** Every request is paced and identified; never remove the pacing, budget or retry limits.
- **Stable formats.** `series` output is read by web pages; change it only in a way old readers still handle, and
  note it in the release.
- Style: no emojis, no em dashes, plain technical language. Organisation name: Long Horizon Observatory.
- **Releases.** Bump `version` in `pyproject.toml` and `__version__` together; a `v<version>` tag publishes to PyPI
  through `.github/workflows/publish.yml` (trusted publishing, no token).
- **Commits.** Author and commit as the human contributor (`git config` user). Record the agent harness and model as
  a plain-text line at the end of the body, for example `Collaborated with Claude Code (Opus 5.5).` Never a
  `Co-Authored-By:` trailer.

## Commands

```bash
uv sync --dev
uv run pytest
uv run pre-commit run --all-files
```
