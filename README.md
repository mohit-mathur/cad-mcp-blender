# cad-mcp-blender

**Live Blender control for Claude AI via the Model Context Protocol.**

51 MCP tools that let Claude (or any MCP client) drive a running Blender session — modeling, materials, HDRI lighting, ray-traced rendering, animation, particles, physics, and the Poly Haven CC0 asset library — with visual feedback after every change.

---

## Why another Blender MCP?

| Feature | Other Blender MCPs | cad-mcp-blender |
|---|---|---|
| Checkpoint / undo as tools | ✗ | ✓ Named snapshots, undo/redo, auto-checkpoint before code exec |
| Visual feedback | Manual screenshot tool | ✓ **Auto viewport capture after every modifying op** |
| Code execution safety | Raw `exec`, no guardrails | ✓ Auto-checkpoint + stdout capture + scene diff |
| Scene queries | One monolithic dump | ✓ Hierarchical: summary → details → search |
| User intent capture | ✗ | ✓ Selection awareness (which face/edge/vertex was clicked) |
| Connection reliability | Newline-delimited, fragments on big payloads | ✓ Length-prefixed binary framing, heartbeat, auto-reconnect |
| HDRI environments | ✗ | ✓ `cad_set_world` + Poly Haven downloads |
| Image-based PBR | ✗ | ✓ Color/roughness/metallic/normal/displacement maps |
| Real Cycles/Eevee render | Viewport only | ✓ Full ray-traced render returned as image |
| Mesh editing | Primitives + boolean only | ✓ Extrude, inset, subdivide, shade-smooth, more |
| Asset library | ✗ | ✓ Poly Haven (free CC0 HDRIs, textures, models) |

---

## Architecture

```
┌─────────────────────────────────────┐
│         Claude / MCP Client         │
└──────────────┬──────────────────────┘
               │ stdio (JSON-RPC, MCP)
┌──────────────▼──────────────────────┐
│   cad-mcp-blender server (Node)     │
│   51 tools                          │
└──────────────┬──────────────────────┘
               │ TCP :9876 (length-prefixed JSON)
┌──────────────▼──────────────────────┐
│   Blender Addon (cad_mcp_blender)   │
│   Socket server inside live Blender │
│   Commands run on the main thread,  │
│   user sees changes immediately     │
└─────────────────────────────────────┘
```

The addon runs *inside* Blender's process, so Claude operates on the user's live session — not a headless subprocess. Every modifying tool returns a viewport screenshot in the response, so Claude actually *sees* what it built.

---

## Installation

### 1. Install the Blender addon

Download `cad-mcp-blender-1.0.0.zip` from the [Releases](https://github.com/mohit-mathur/cad-mcp-blender/releases) page.

**Blender 4.2+ (extension):**
Edit → Preferences → Get Extensions → ⌄ menu → *Install from Disk* → select the zip.

**Blender 3.2 – 4.1 (legacy add-on):**
Edit → Preferences → Add-ons → *Install* → select the zip → enable **Interface: CAD-MCP**.

After install, open the 3D Viewport, press `N` to open the sidebar, find the **CAD-MCP** tab, and click **Connect to Claude**. The default port is 9876.

### 2. Install the MCP server

You'll need [Node.js 18+](https://nodejs.org) and Git.

```bash
git clone https://github.com/mohit-mathur/cad-mcp-blender.git
cd cad-mcp-blender/server
npm install
```

Note the absolute path to `server/src/index.js` — you'll need it for the next step.

### 3. Wire it into Claude

**Claude Desktop** — add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "blender": {
      "command": "node",
      "args": ["/absolute/path/to/cad-mcp-blender/server/src/index.js"]
    }
  }
}
```

> On Windows, use forward slashes or escaped backslashes:
> `"C:/Users/you/code/cad-mcp-blender/server/src/index.js"`

**Claude Code:**

```bash
claude mcp add blender node /absolute/path/to/cad-mcp-blender/server/src/index.js
```

Restart your Claude client. To update later: `git pull && cd server && npm install` from the cloned dir.

### 4. Try it

In Blender, click *Connect to Claude*. Then in Claude:

> *"Create a red metallic sphere at the origin, add a sun light above it,
> position the camera at (5, -5, 3) looking at the sphere, then render
> with Cycles at 1080p with 64 samples and show me the result."*

---

## Tools (51 total)

| Group | Tools |
|---|---|
| **Checkpoints / Undo** | `cad_save_checkpoint`, `cad_restore_checkpoint`, `cad_list_checkpoints`, `cad_delete_checkpoint`, `cad_undo`, `cad_redo` |
| **Scene queries** | `cad_get_scene_summary`, `cad_get_object_details`, `cad_get_objects_by_type`, `cad_get_selection`, `cad_get_object_tree`, `cad_scene_search` |
| **Object ops** | `cad_create_object`, `cad_delete_object`, `cad_transform_object`, `cad_duplicate_object`, `cad_boolean` |
| **Modeling** | `cad_fillet`, `cad_chamfer`, `cad_create_sketch`, `cad_mesh_edit`, `cad_create_curve`, `cad_create_text`, `cad_array_pattern` |
| **Modifiers** | `cad_add_modifier`, `cad_apply_modifier` |
| **Materials** | `cad_set_material`, `cad_set_textured_material` |
| **Lighting / world** | `cad_add_light`, `cad_set_world` |
| **Camera** | `cad_set_camera`, `cad_set_camera_settings` |
| **Animation** | `cad_set_keyframe`, `cad_set_frame` |
| **Scene mgmt** | `cad_set_visibility`, `cad_rename_object`, `cad_set_parent`, `cad_create_collection`, `cad_move_to_collection` |
| **Particles / physics** | `cad_add_particle_system`, `cad_add_physics` |
| **Render** | `cad_render`, `cad_set_render_settings` |
| **Viewport / IO** | `cad_get_viewport_screenshot`, `cad_set_view`, `cad_export`, `cad_import_file`, `cad_measure` |
| **Asset library** | `cad_polyhaven_search`, `cad_polyhaven_download` |
| **Escape hatch** | `cad_execute_code` (auto-checkpoint, scene diff, stdout capture) |

---

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `BLENDER_HOST` | `localhost` | Blender addon host |
| `BLENDER_PORT` | `9876` | Blender addon port |

The auto-screenshot resolution and auto-screenshot toggle live in the addon's sidebar UI.

---

## Building the addon zip locally

```bash
python scripts/build_addon.py
# → dist/cad-mcp-blender-1.0.0.zip
```

The script verifies that `bl_info["version"]` and `blender_manifest.toml` agree before building.

---

## Development

```bash
# Server
cd server
npm install
npm run check    # syntax check
npm run dev      # watch mode

# Addon (no build needed — just install the source dir as Blender addon during dev,
# or symlink addon/ into Blender's addons folder)
```

---

## Key design decisions

- **Length-prefixed protocol.** Every TCP message is `[4-byte big-endian length][JSON]`. Eliminates fragmentation bugs that plague newline-delimited protocols when JSON payloads contain embedded newlines or large image data.
- **Auto-checkpoint before code execution.** `cad_execute_code` saves a `.blend` snapshot first, so Claude can roll back if its Python crashes or produces garbage.
- **Scene diff.** After arbitrary code, the response includes added/removed/modified objects so Claude can verify what its code actually did.
- **Selection awareness.** `cad_get_selection` reports the active object, all selected objects, and edit-mode geometry counts (verts/edges/faces) — letting Claude respond to *"fillet this edge"* when the user has an edge selected.
- **Hierarchical scene queries.** A scene of 1000 objects shouldn't dump as one giant JSON. `cad_get_scene_summary` is cheap, then drill in with `cad_get_object_details` or `cad_scene_search`.

---

## Security

The Blender addon listens on `127.0.0.1:9876` with **no authentication** — by design — and `cad_execute_code` runs arbitrary Python. Treat the addon as you would a local Python REPL. See [SECURITY.md](SECURITY.md) for the full threat model and hardening guidance for multi-user systems.

## License

MIT
