# mcprobe

`mcprobe` probes an MCP (Model Context Protocol) server at a given URL: it tries
to connect using all supported transports, and on success lists everything the
server exposes — tools, prompts, resources and resource templates — with their
descriptions and full parameter schemas.

## Requirements

- Python 3.10+
- Libraries listed in `mcprobe.requirements.txt`:

```
pip install -r mcprobe.requirements.txt
```

## Usage

```
python mcprobe.py <URL> [options]
```

Options:

| Option | Description |
|---|---|
| `--header "Name: value"` | Extra HTTP header, repeatable. Use for auth, e.g. `--header "Authorization: Bearer TOKEN"`. |
| `--timeout N` | Per-request timeout in seconds (default: 10). |
| `--transport auto\|raw\|http\|sse` | Force a transport or probe all (default: `auto`). |
| `--json` | Output raw JSON instead of a human-readable report. |
| `-v`, `--verbose` | Show per-transport probe progress on stderr. |

## Transports

In `auto` mode the following transports are tried in order; the first
successful one wins:

1. **raw** — bare JSON-RPC `initialize` POST (no SSE framing). A cheap
   single-request probe; also produces clear errors for wrong URLs and
   missing authentication.
2. **streamable-http** (`http`) — the current MCP HTTP transport
   (JSON-RPC POST with optional SSE streaming responses).
3. **sse** — the legacy HTTP + Server-Sent Events transport: GET the URL
   with `Accept: text/event-stream`, receive the `endpoint` event, then
   POST JSON-RPC messages to it.

If none succeed, all per-transport failure reasons are printed and the
exit code is 1.

Once connected, `mcprobe` reports:

- server name, version and negotiated protocol version
- server capabilities and instructions (if any)
- ping round-trip time
- **Tools** — name, description, and every input parameter (type,
  required/optional, default, description, nested object/array schemas)
- **Prompts** — name, description, and arguments with descriptions
- **Resources** and **resource templates** — URI, name, description,
  MIME type

## Examples

Probe a public server:

```
python mcprobe.py https://mcp.deepwiki.com/mcp
```

Probe with verbose transport fallback:

```
python mcprobe.py https://gitmcp.io/docs -v
```

Probe an authenticated server:

```
python mcprobe.py https://mcp.example.com/mcp --header "Authorization: Bearer sk-..."
```

Machine-readable output:

```
python mcprobe.py https://mcp.example.com/mcp --json
```

Force the legacy SSE transport only:

```
python mcprobe.py http://127.0.0.1:8765/sse --transport sse
```

Sample output (excerpt):

```
======================================================================
Server : GitMCP v1.1.0
URL    : https://gitmcp.io/docs
Transport: streamable-http   (handshake 1.391s)
Protocol version: 2025-03-26
Ping   : 2.203s
Capabilities: tools

TOOLS (5)
----------------------------------------------------------------------
* search_generic_code
    Search for code in any GitHub repository by providing owner, project
    name, and search query. Returns matching files.
    owner: string
        The GitHub repository owner (username or organization)
    repo: string
        The GitHub repository name
    query: string
        The search query to find relevant code files
    page: number (optional)
        Page number to retrieve (starting from 1). Each page contains 30 results.
======================================================================
```

## Exit codes

- `0` — connected and reported successfully
- `1` — could not connect with any transport (reasons printed)
- `2` — missing required libraries

## Notes

- Only `http://` and `https://` URLs are supported (WebSocket transport
  is deprecated in the MCP spec and not probed).
- The tool output is meant both for humans and for feeding to an LLM —
  use `--json` for the latter.
