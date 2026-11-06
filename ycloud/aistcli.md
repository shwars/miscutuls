# aistcli

Command line utility for [Yandex AI Studio](https://aistudio.yandex.ru/), built on top of the
[OpenAI SDK](https://pypi.org/project/openai/) against the AI Studio OpenAI-compatible API.

Currently it manages **files** and **file search indices** (vector stores): listing and
deletion, and lists available models.

## Installation

Requires Python 3.10+ and:

```
pip install openai python-dotenv
```

## Configuration

The utility reads two lowercase keys — `folder_id` and `api_key` — from:

1. Environment variables (highest priority), or
2. `.env` file in the current working directory, or
3. `.env` file in the directory where `aistcli.py` itself is located.

Example `.env`:

```
folder_id=b1gxxxxxxxxxxxxxxxxx
api_key=AQVNxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

On startup the utility prints the `folder_id` it uses and where the configuration came
from (to stderr, so `--json` output stays clean):

```
Using folder_id: b1g... (config source: D:\path\to\.env)
```

## Usage

```
python aistcli.py <group> <resource> [options]
```

### list models

Show models available in the folder. Always queries the live API; reports an error and
exits non-zero if the API is unavailable.

```
python aistcli.py list models
python aistcli.py list models --json
```

### list files

List uploaded files, top 10 by default.

```
python aistcli.py list files                # top 10
python aistcli.py list files --limit 50     # top 50
python aistcli.py list files --json
```

Columns: ID, filename, size (bytes), purpose, creation time (UTC).

### list indices

List all file search indices (vector stores) with their status and file counts.

```
python aistcli.py list indices
python aistcli.py list indices --json
```

### rm files

Delete **all** files from AI Studio.

- Without flags, asks for confirmation per file: `Delete <id> (<filename>)? [y/N/a]`
  - `y` / `yes` — delete this file
  - `n` / `no` / Enter — skip this file
  - `a` — delete this and all remaining files without asking
- `-y` / `--yes` — assume yes, delete everything without asking.

```
python aistcli.py rm files          # interactive
python aistcli.py rm files -y       # delete all without asking
```

### rm indices

Delete **all** file search indices. Same confirmation behavior as `rm files`.

```
python aistcli.py rm indices
python aistcli.py rm indices -y
```

Both `rm` commands print a summary at the end: `Done: N deleted, M skipped, K failed.`
Individual deletion failures do not stop the batch.

## Exit codes

- `0` — success
- `1` — configuration error, API error, or nothing was returned by the API
