# Security Model

This document describes the trust boundaries and known limits of `cad-mcp-blender`. Read this before deploying it on a multi-user system, in a container, or anywhere untrusted local processes might exist.

## Architecture summary

```
Claude Desktop / Code  ──stdio (JSON-RPC)──▶  cad-mcp-blender (Node)
                                                    │
                                                    │ TCP 127.0.0.1:9876
                                                    │ length-prefixed JSON
                                                    ▼
                                          Blender + addon (Python)
```

Both the Node server and the Blender addon run as **the same OS user** with the same privileges as the human running Blender. Communication between them is plain JSON, length-prefixed, no encryption — it never leaves the local machine.

## What's protected

| Threat | Mitigation |
|---|---|
| Network attackers reaching the addon | Socket binds to `localhost` only — not reachable off-host |
| Memory DoS via huge payloads | 50 MB per-message cap (addon), 50 MB per-message + 64 MB buffer cap (server) |
| Path traversal in checkpoint names | Strict regex `^[A-Za-z0-9_.-]{1,64}$`; checkpoint dir is per-session via `tempfile.mkdtemp()` |
| Path traversal in Poly Haven downloads | Strict regex on `asset_id`, `resolution`, `format`, `map`; final realpath check ensures result stays inside the temp dir |
| SSRF via Poly Haven downloads | API URL host is hardcoded; downloaded URLs are checked to live on `polyhaven.com` |
| Other local users reading checkpoints/screenshots (Unix) | Temp dirs created with mode `0700` (best-effort; silently no-ops on Windows) |
| Auto-script execution from imported `.blend` files | Blender's `Auto Run Python Scripts` preference is **OFF** by default — keep it that way |

## What is NOT protected — the trust boundary

These are intentional design choices, not bugs. Be aware of them.

### 1. No authentication on the addon socket

Any local process that can reach `127.0.0.1:9876` can send commands to the addon — including `cad_execute_code`, which runs arbitrary Python in the Blender process.

This is a deliberate tradeoff. Adding shared-secret auth would mean coordinating a token between the Node server and the Blender addon (e.g., file in `~/.config`), which complicates setup. For the typical use case — single user on a personal workstation — the marginal protection isn't worth the friction.

**Implication:** Treat the Blender addon as you would treat your shell. Don't run it on a shared system without thinking. Don't run it inside a container that hosts untrusted code.

### 2. `cad_execute_code` is full local code execution

The escape-hatch tool runs whatever Python it's given inside Blender. The auto-checkpoint, scene diff, and stdout capture are *correctness* features (so you can see what your script did, and undo it if it's wrong), **not** security boundaries. There is no sandboxing.

A caller can pass `auto_checkpoint=False` and `code="import shutil; shutil.rmtree(os.path.expanduser('~'))"` and the addon will execute it.

**Implication:** Whoever you give addon access to — that's who you'd give a Python REPL to.

### 3. Tool parameters that take filesystem paths are not jailed

`cad_export`, `cad_import_file`, `cad_render`, `cad_set_world`, `cad_set_textured_material`, etc. accept absolute file paths. The MCP server passes them through to Blender, which reads or writes wherever the user has access.

If your MCP client (Claude or another) is told to read `~/.ssh/id_rsa` as an HDRI, Blender will try. (It will fail to parse it as an image — but the file is touched.) Under more creative prompt injection, file contents might leak via export-then-base64 chains.

**Implication:** Trust whoever is driving the MCP client to choose reasonable paths. The tool descriptions guide Claude toward sensible defaults but are not policy enforcement.

### 4. Imported `.blend` files can contain hostile data

`cad_import_file` for `.blend` uses `bpy.data.libraries.load(filepath, link=False)` which appends objects from another blend file. This is generally safe, but:
- If you've enabled Blender's *"Auto Run Python Scripts"* preference, malicious blend files can execute drivers and frame-change handlers.
- Blender has had historical CVEs in its file parsers. Only import blend files from sources you trust.

### 5. No transport encryption

JSON travels in the clear over the loopback interface. On a single-user machine this is irrelevant (other users would need root to packet-capture loopback). In a multi-tenant container or on a shared dev VM, anyone with elevated privileges can observe the traffic — including any stdout, scene contents, or checkpoint paths exchanged.

## Hardening for shared / multi-user environments

If you must run this somewhere with untrusted local processes:

1. **Bind to a Unix socket** instead of TCP, with `0600` permissions. Requires editing `addon/__init__.py`'s `CADMCPServer` to use `socket.AF_UNIX`. Not currently supported out of the box.
2. **Add a shared-secret token** required as the first field of every command. Generate it on the addon side, write to a `0600` file, configure the Node server to read it from env. Not currently supported out of the box.
3. **Run Blender inside a container** with a private network namespace, mount only the directories you want exposed.

## Reporting vulnerabilities

Open a GitHub Security Advisory (Settings → Security → Report a vulnerability) for anything sensitive. For non-sensitive issues, a regular GitHub issue is fine.
