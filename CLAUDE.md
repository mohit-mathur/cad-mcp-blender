# cad-mcp-blender — Claude Code project context

This file is loaded into Claude Code's context whenever you work in this repo.

## Repo layout

```
cad-mcp-blender/
├── addon/                      # Blender addon (installed inside Blender)
│   ├── __init__.py             # 1700+ lines: socket server, 47 command handlers
│   └── blender_manifest.toml   # Blender 4.2+ extension manifest
├── server/                     # Node.js MCP server (talks to addon over TCP)
│   ├── package.json            # npm-publishable as `cad-mcp-blender`
│   └── src/
│       ├── index.js            # 51 MCP tool definitions, command map
│       └── socket-client.js    # Length-prefixed TCP client, auto-reconnect
├── scripts/
│   └── build_addon.py          # Cross-platform: zips addon/ into a Blender extension
├── .github/workflows/          # CI + release automation
├── README.md
├── LICENSE                     # MIT
└── .gitignore
```

## Two-process architecture

1. **MCP server** (Node, in `server/`): talks to Claude over stdio (JSON-RPC), forwards commands to the addon over a local TCP socket on port 9876.
2. **Blender addon** (Python, in `addon/`): runs *inside* Blender's process, listens on the TCP socket, dispatches commands to Blender's main thread via `bpy.app.timers`.

Commands cross from Node → Blender's Python via the **length-prefixed JSON protocol** (`[4-byte big-endian length][UTF-8 JSON]`). This avoids the fragmentation bugs that plague newline-delimited TCP protocols when payloads contain embedded newlines or large base64 images.

## How a tool call flows

1. User in Claude: *"create a red cube"*
2. Claude reads tool descriptions from `server/src/index.js` `TOOLS[]`, picks `cad_create_object`, emits a `tool_use` block with `{primitive: "cube"}`.
3. `handleToolCall` in [server/src/index.js](server/src/index.js) maps `cad_create_object` → `create_object` via `COMMAND_MAP`, sends to addon.
4. Addon's `handle_command` switch in [addon/__init__.py](addon/__init__.py) matches `"create_object"`, calls `bpy.ops.mesh.primitive_cube_add(...)`. Cube appears in user's viewport.
5. If the command is in `MODIFYING_COMMANDS` (line ~669 of `__init__.py`), the addon also captures a viewport PNG and base64-encodes it.
6. Server unwraps the base64 into an MCP `image` content block. Claude *sees* the result image, not just text.

## Adding a new tool

1. Add command handler to [addon/__init__.py](addon/__init__.py) — new `elif cmd_type == "your_command":` branch in `handle_command`.
2. If it modifies the scene, add the command name to `MODIFYING_COMMANDS` so it triggers auto-screenshot.
3. Add MCP tool definition to `TOOLS[]` in [server/src/index.js](server/src/index.js) — give it a clear `description` since that's literally all Claude has to decide when to invoke it.
4. Add a `cad_your_tool: 'your_command'` entry to `COMMAND_MAP`.
5. Run `npm run check` in `server/` and `python -c "import ast; ast.parse(open('addon/__init__.py', encoding='utf-8').read())"` to syntax-check.

## Versioning

Two files have to stay in sync:
- `addon/blender_manifest.toml` → `version = "X.Y.Z"`
- `addon/__init__.py` → `bl_info["version"] = (X, Y, Z)`

The build script (`scripts/build_addon.py`) refuses to build if they disagree.

## Useful commands

```bash
# Syntax check both pieces in one go
python -c "import ast; ast.parse(open('addon/__init__.py', encoding='utf-8').read())" && \
  cd server && npm run check

# Build the addon zip for release
python scripts/build_addon.py

# Run the MCP server locally (Blender must be running with addon connected)
cd server && npm start
```

## Testing approach

There's no automated end-to-end test — those would need a headless Blender, which loses the live-viewport feedback that makes this addon worth using. Instead:

- CI runs syntax checks (Python `ast.parse` + Node `--check`).
- Manual smoke test: install the addon, click *Connect to Claude*, run a few prompts in Claude Desktop / Code.

## What lives elsewhere

- The unified Node server that drives both Blender *and* FreeCAD is at `c:/Mohit/software/CAD_MCP/files/`. This Blender-only repo was forked from it.
- A separate `cad-mcp-freecad` repo will hold the FreeCAD addon and a similarly stripped server.
