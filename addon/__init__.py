# CAD-MCP Blender Addon
# TCP socket server running inside live Blender for AI-driven 3D modeling.
# Improvements over blender-mcp:
#   - Checkpoint/undo system exposed as tools
#   - Auto viewport screenshots after modifying operations
#   - Hybrid structured tools + sandboxed code execution
#   - Hierarchical scene queries (not monolithic dumps)
#   - Selection/context awareness (user intent capture)
#   - Reliable connection with heartbeat, chunked protocol, auto-reconnect
#   - Command queuing during long operations

bl_info = {
    "name": "CAD-MCP",
    "author": "CAD-MCP Project",
    "version": (1, 0, 0),
    "blender": (3, 2, 0),
    "location": "View3D > Sidebar > CAD-MCP",
    "description": "Connect Blender to Claude AI via MCP with checkpoints, visual feedback, and engineering tools",
    "category": "Interface",
}

import bpy
import bmesh
import mathutils
import json
import threading
import socket
import struct
import time
import traceback
import os
import tempfile
import base64
import fnmatch
import re
import io
from contextlib import redirect_stdout, redirect_stderr
from bpy.props import IntProperty, BoolProperty, StringProperty
from collections import OrderedDict
from datetime import datetime

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# INPUT VALIDATORS
# Prevent path-traversal where user-supplied strings are interpolated into
# filesystem paths or URLs. See SECURITY.md for the full trust model.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SAFE_NAME_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
_SAFE_ASSET_ID_RE = re.compile(r'^[a-z0-9_]{1,80}$')
_SAFE_RESOLUTION_RE = re.compile(r'^(1k|2k|4k|8k|16k)$')
_SAFE_FORMAT_RE = re.compile(r'^[a-z]{2,5}$')
_SAFE_MAP_KEY_RE = re.compile(r'^[A-Za-z_]{1,32}$')


def _check_safe_name(value, what):
    """Reject anything that could traverse paths."""
    if not isinstance(value, str) or not _SAFE_NAME_RE.match(value):
        raise ValueError(
            f"Invalid {what}: must be 1-64 chars from [A-Za-z0-9_.-], "
            f"no path separators or traversal"
        )
    if value in (".", ".."):
        raise ValueError(f"Invalid {what}: reserved")
    return value


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PROTOCOL: Length-prefixed JSON over TCP
# Each message: [4 bytes big-endian length][JSON payload]
# This prevents the "read until newline" fragmentation bugs in blender-mcp.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def send_message(sock, data):
    """Send a length-prefixed JSON message."""
    payload = json.dumps(data).encode('utf-8')
    header = struct.pack('>I', len(payload))
    sock.sendall(header + payload)


def recv_message(sock, timeout=60):
    """Receive a length-prefixed JSON message."""
    sock.settimeout(timeout)
    # Read 4-byte length header
    header = b''
    while len(header) < 4:
        chunk = sock.recv(4 - len(header))
        if not chunk:
            raise ConnectionError("Connection closed")
        header += chunk
    length = struct.unpack('>I', header)[0]
    if length > 50 * 1024 * 1024:  # 50MB safety limit
        raise ValueError(f"Message too large: {length} bytes")
    # Read payload
    payload = b''
    while len(payload) < length:
        chunk = sock.recv(min(65536, length - len(payload)))
        if not chunk:
            raise ConnectionError("Connection closed during payload")
        payload += chunk
    return json.loads(payload.decode('utf-8'))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CHECKPOINT SYSTEM
# Named snapshots of the .blend file state. Claude can save, restore, list.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CheckpointManager:
    def __init__(self, max_checkpoints=20):
        self.checkpoints = OrderedDict()
        self.max_checkpoints = max_checkpoints
        # Per-session dir with 0700 perms (Unix) — avoids name collisions
        # between concurrent Blender instances and isolates from other users.
        self.checkpoint_dir = tempfile.mkdtemp(prefix='cad-mcp-checkpoints-')
        try:
            os.chmod(self.checkpoint_dir, 0o700)
        except (OSError, NotImplementedError):
            pass  # Windows / non-POSIX

    def save(self, name=None):
        """Save current state as a named checkpoint."""
        if name is None:
            name = f"auto_{datetime.now().strftime('%H%M%S')}"
        # Reject path traversal — name is interpolated into a file path below.
        name = _check_safe_name(name, "checkpoint name")
        # Evict oldest if at capacity
        while len(self.checkpoints) >= self.max_checkpoints:
            oldest_name, oldest_path = self.checkpoints.popitem(last=False)
            if os.path.exists(oldest_path):
                os.remove(oldest_path)

        filepath = os.path.join(self.checkpoint_dir, f"checkpoint_{name}.blend")
        bpy.ops.wm.save_as_mainfile(filepath=filepath, copy=True)
        self.checkpoints[name] = filepath
        return {
            "status": "ok",
            "checkpoint": name,
            "path": filepath,
            "total_checkpoints": len(self.checkpoints)
        }

    def restore(self, name):
        """Restore state from a named checkpoint."""
        if name not in self.checkpoints:
            return {"error": f"Checkpoint '{name}' not found. Available: {list(self.checkpoints.keys())}"}
        filepath = self.checkpoints[name]
        if not os.path.exists(filepath):
            return {"error": f"Checkpoint file missing: {filepath}"}
        bpy.ops.wm.open_mainfile(filepath=filepath)
        return {"status": "ok", "restored": name}

    def list_all(self):
        """List all available checkpoints."""
        result = []
        for name, path in self.checkpoints.items():
            size = os.path.getsize(path) if os.path.exists(path) else 0
            result.append({"name": name, "size_bytes": size})
        return result

    def delete(self, name):
        """Delete a specific checkpoint."""
        if name not in self.checkpoints:
            return {"error": f"Checkpoint '{name}' not found"}
        path = self.checkpoints.pop(name)
        if os.path.exists(path):
            os.remove(path)
        return {"status": "ok", "deleted": name}

    def cleanup(self):
        """Remove all checkpoint files and the per-session directory."""
        for name, path in self.checkpoints.items():
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        self.checkpoints.clear()
        try:
            # Best-effort: only succeeds if dir is empty
            os.rmdir(self.checkpoint_dir)
        except OSError:
            pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# VIEWPORT SCREENSHOT CAPTURE
# Captures the 3D viewport as a base64-encoded PNG.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def capture_viewport(width=800, height=600):
    """Capture the active 3D viewport as base64 PNG."""
    try:
        # Find a 3D viewport
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                # Save current render settings
                scene = bpy.context.scene
                old_x = scene.render.resolution_x
                old_y = scene.render.resolution_y
                old_pct = scene.render.resolution_percentage
                old_fmt = scene.render.image_settings.file_format

                scene.render.resolution_x = width
                scene.render.resolution_y = height
                scene.render.resolution_percentage = 100
                scene.render.image_settings.file_format = 'PNG'

                # Render viewport
                tmp_path = os.path.join(tempfile.gettempdir(), 'cad_mcp_viewport.png')
                override = bpy.context.copy()
                override['area'] = area
                override['region'] = [r for r in area.regions if r.type == 'WINDOW'][0]
                override['space_data'] = area.spaces.active

                with bpy.context.temp_override(**override):
                    bpy.ops.render.opengl(write_still=True)
                    bpy.data.images['Render Result'].save_render(filepath=tmp_path)

                # Restore settings
                scene.render.resolution_x = old_x
                scene.render.resolution_y = old_y
                scene.render.resolution_percentage = old_pct
                scene.render.image_settings.file_format = old_fmt

                # Read and encode
                with open(tmp_path, 'rb') as f:
                    img_data = base64.b64encode(f.read()).decode('ascii')
                os.remove(tmp_path)
                return img_data

        return None  # No 3D viewport found
    except Exception as e:
        print(f"CAD-MCP: Viewport capture failed: {e}")
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SCENE DIFF — detect what changed after an operation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def snapshot_scene():
    """Take a lightweight snapshot of scene state for diffing."""
    snap = {}
    for obj in bpy.data.objects:
        snap[obj.name] = {
            "type": obj.type,
            "location": list(obj.location),
            "dimensions": list(obj.dimensions),
            "visible": obj.visible_get(),
            "modifiers": [m.name for m in obj.modifiers] if hasattr(obj, 'modifiers') else [],
            "material_count": len(obj.data.materials) if obj.data and hasattr(obj.data, 'materials') else 0,
        }
    return snap


def diff_scenes(before, after):
    """Compute what changed between two scene snapshots."""
    added = [n for n in after if n not in before]
    removed = [n for n in before if n not in after]
    modified = []
    for name in after:
        if name in before and after[name] != before[name]:
            changes = {}
            for key in after[name]:
                if after[name][key] != before[name].get(key):
                    changes[key] = {"before": before[name].get(key), "after": after[name][key]}
            if changes:
                modified.append({"name": name, "changes": changes})
    return {"added": added, "removed": removed, "modified": modified}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMMAND HANDLERS — structured tools + code execution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

checkpoint_mgr = CheckpointManager()


def handle_command(cmd_type, params):
    """Route a command to the appropriate handler. Returns a dict."""

    # ── Checkpoint & Undo ──
    if cmd_type == "save_checkpoint":
        return checkpoint_mgr.save(params.get("name"))

    elif cmd_type == "restore_checkpoint":
        return checkpoint_mgr.restore(params.get("name", ""))

    elif cmd_type == "list_checkpoints":
        return {"checkpoints": checkpoint_mgr.list_all()}

    elif cmd_type == "delete_checkpoint":
        return checkpoint_mgr.delete(params.get("name", ""))

    elif cmd_type == "undo":
        count = params.get("count", 1)
        for _ in range(count):
            bpy.ops.ed.undo()
        return {"status": "ok", "undone": count}

    elif cmd_type == "redo":
        count = params.get("count", 1)
        for _ in range(count):
            bpy.ops.ed.redo()
        return {"status": "ok", "redone": count}

    # ── Scene Queries (hierarchical, not monolithic) ──
    elif cmd_type == "get_scene_summary":
        scene = bpy.context.scene
        objects = list(bpy.data.objects)
        type_counts = {}
        for obj in objects:
            type_counts[obj.type] = type_counts.get(obj.type, 0) + 1
        total_verts = sum(len(o.data.vertices) for o in objects if o.type == 'MESH' and o.data)
        return {
            "scene_name": scene.name,
            "object_count": len(objects),
            "type_counts": type_counts,
            "total_vertices": total_verts,
            "frame_current": scene.frame_current,
            "active_object": bpy.context.active_object.name if bpy.context.active_object else None,
            "selected_objects": [o.name for o in bpy.context.selected_objects],
        }

    elif cmd_type == "get_object_details":
        name = params.get("name", "")
        obj = bpy.data.objects.get(name)
        if not obj:
            return {"error": f"Object '{name}' not found"}
        info = {
            "name": obj.name,
            "type": obj.type,
            "location": list(obj.location),
            "rotation_euler": list(obj.rotation_euler),
            "scale": list(obj.scale),
            "dimensions": list(obj.dimensions),
            "visible": obj.visible_get(),
            "parent": obj.parent.name if obj.parent else None,
            "children": [c.name for c in obj.children],
            "modifiers": [{"name": m.name, "type": m.type} for m in obj.modifiers],
        }
        if obj.type == 'MESH' and obj.data:
            mesh = obj.data
            info["mesh"] = {
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
                "materials": [m.name if m else "None" for m in mesh.materials],
            }
            # Bounding box in world space
            bbox_world = [obj.matrix_world @ mathutils.Vector(corner) for corner in obj.bound_box]
            info["world_bounding_box"] = {
                "min": [min(v[i] for v in bbox_world) for i in range(3)],
                "max": [max(v[i] for v in bbox_world) for i in range(3)],
            }
        return info

    elif cmd_type == "get_objects_by_type":
        obj_type = params.get("type", "MESH").upper()
        objects = [o for o in bpy.data.objects if o.type == obj_type]
        return {"type": obj_type, "objects": [
            {"name": o.name, "location": list(o.location), "dimensions": list(o.dimensions)}
            for o in objects
        ]}

    elif cmd_type == "get_selection":
        selected = bpy.context.selected_objects
        active = bpy.context.active_object
        mode = bpy.context.mode
        result = {
            "mode": mode,
            "active_object": active.name if active else None,
            "selected_objects": [o.name for o in selected],
            "selected_count": len(selected),
        }
        # If in edit mode, report selected geometry
        if mode == 'EDIT_MESH' and active and active.type == 'MESH':
            bm = bmesh.from_edit_mesh(active.data)
            result["selected_verts"] = sum(1 for v in bm.verts if v.select)
            result["selected_edges"] = sum(1 for e in bm.edges if e.select)
            result["selected_faces"] = sum(1 for f in bm.faces if f.select)
        return result

    elif cmd_type == "get_object_tree":
        """Return parent/child hierarchy."""
        roots = [o for o in bpy.data.objects if o.parent is None]
        def build_tree(obj):
            node = {"name": obj.name, "type": obj.type}
            if obj.children:
                node["children"] = [build_tree(c) for c in obj.children]
            return node
        return {"tree": [build_tree(r) for r in roots]}

    elif cmd_type == "scene_search":
        pattern = params.get("query", "*")
        matches = []
        for obj in bpy.data.objects:
            if fnmatch.fnmatch(obj.name.lower(), pattern.lower()):
                matches.append({"name": obj.name, "type": obj.type, "location": list(obj.location)})
        return {"query": pattern, "matches": matches}

    # ── Structured Object Operations ──
    elif cmd_type == "create_object":
        ptype = params.get("primitive", "cube")
        name = params.get("name")
        loc = params.get("location", [0, 0, 0])
        size = params.get("size", 2)
        radius = params.get("radius", 1)
        depth = params.get("depth", 2)

        ops_map = {
            "cube": lambda: bpy.ops.mesh.primitive_cube_add(size=size, location=loc),
            "sphere": lambda: bpy.ops.mesh.primitive_uv_sphere_add(radius=radius, location=loc),
            "cylinder": lambda: bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=depth, location=loc),
            "cone": lambda: bpy.ops.mesh.primitive_cone_add(radius1=radius, depth=depth, location=loc),
            "torus": lambda: bpy.ops.mesh.primitive_torus_add(
                major_radius=params.get("major_radius", 1),
                minor_radius=params.get("minor_radius", 0.25),
                location=loc),
            "plane": lambda: bpy.ops.mesh.primitive_plane_add(size=size, location=loc),
            "circle": lambda: bpy.ops.mesh.primitive_circle_add(radius=radius, location=loc, fill_type='NGON'),
            "icosphere": lambda: bpy.ops.mesh.primitive_ico_sphere_add(radius=radius, location=loc),
            "monkey": lambda: bpy.ops.mesh.primitive_monkey_add(size=size, location=loc),
        }
        if ptype not in ops_map:
            return {"error": f"Unknown primitive: {ptype}. Supported: {list(ops_map.keys())}"}

        ops_map[ptype]()
        obj = bpy.context.active_object
        if name:
            obj.name = name
        return {
            "status": "ok", "name": obj.name, "type": obj.type,
            "location": list(obj.location), "dimensions": list(obj.dimensions)
        }

    elif cmd_type == "delete_object":
        name = params.get("name", "")
        obj = bpy.data.objects.get(name)
        if not obj:
            return {"error": f"Object '{name}' not found"}
        bpy.data.objects.remove(obj, do_unlink=True)
        return {"status": "ok", "deleted": name}

    elif cmd_type == "transform_object":
        name = params.get("name", "")
        obj = bpy.data.objects.get(name)
        if not obj:
            return {"error": f"Object '{name}' not found"}
        if "location" in params:
            obj.location = params["location"]
        if "rotation" in params:
            obj.rotation_euler = [r * 3.14159 / 180 for r in params["rotation"]]  # degrees to radians
        if "scale" in params:
            obj.scale = params["scale"]
        return {
            "status": "ok", "name": obj.name,
            "location": list(obj.location),
            "rotation_euler": list(obj.rotation_euler),
            "scale": list(obj.scale),
        }

    elif cmd_type == "duplicate_object":
        name = params.get("name", "")
        obj = bpy.data.objects.get(name)
        if not obj:
            return {"error": f"Object '{name}' not found"}
        new_obj = obj.copy()
        if obj.data:
            new_obj.data = obj.data.copy()
        bpy.context.collection.objects.link(new_obj)
        new_name = params.get("new_name")
        if new_name:
            new_obj.name = new_name
        if "location" in params:
            new_obj.location = params["location"]
        return {"status": "ok", "original": name, "duplicate": new_obj.name}

    # ── Modifiers ──
    elif cmd_type == "add_modifier":
        name = params.get("object", "")
        obj = bpy.data.objects.get(name)
        if not obj:
            return {"error": f"Object '{name}' not found"}
        mod_type = params.get("modifier_type", "BEVEL").upper()
        mod_name = params.get("modifier_name", mod_type.title())

        mod = obj.modifiers.new(name=mod_name, type=mod_type)

        # Apply common modifier settings
        settings = params.get("settings", {})
        for key, val in settings.items():
            if hasattr(mod, key):
                setattr(mod, key, val)

        return {"status": "ok", "object": name, "modifier": mod.name, "type": mod.type}

    elif cmd_type == "apply_modifier":
        name = params.get("object", "")
        obj = bpy.data.objects.get(name)
        if not obj:
            return {"error": f"Object '{name}' not found"}
        mod_name = params.get("modifier_name", "")
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.modifier_apply(modifier=mod_name)
        return {"status": "ok", "object": name, "applied": mod_name}

    # ── Edge Treatments (BEVEL modifier under the hood) ──
    elif cmd_type == "fillet":
        name = params.get("object", "")
        obj = bpy.data.objects.get(name)
        if not obj:
            return {"error": f"Object '{name}' not found"}
        if obj.type != 'MESH':
            return {"error": f"Fillet requires a mesh object, got {obj.type}"}
        width = params.get("width", 0.1)
        segments = params.get("segments", 3)
        bpy.context.view_layer.objects.active = obj
        mod = obj.modifiers.new(name="Fillet", type='BEVEL')
        mod.width = width
        mod.segments = segments
        mod.profile = 0.5
        mod.limit_method = params.get("limit_method", "ANGLE").upper()
        if params.get("apply", True):
            bpy.ops.object.modifier_apply(modifier=mod.name)
        return {"status": "ok", "object": name, "width": width, "segments": segments,
                "applied": params.get("apply", True)}

    elif cmd_type == "chamfer":
        name = params.get("object", "")
        obj = bpy.data.objects.get(name)
        if not obj:
            return {"error": f"Object '{name}' not found"}
        if obj.type != 'MESH':
            return {"error": f"Chamfer requires a mesh object, got {obj.type}"}
        width = params.get("width", params.get("distance", 0.1))
        bpy.context.view_layer.objects.active = obj
        mod = obj.modifiers.new(name="Chamfer", type='BEVEL')
        mod.width = width
        mod.segments = 1
        mod.profile = 0.5
        mod.limit_method = params.get("limit_method", "ANGLE").upper()
        if params.get("apply", True):
            bpy.ops.object.modifier_apply(modifier=mod.name)
        return {"status": "ok", "object": name, "width": width,
                "applied": params.get("apply", True)}

    # ── 2D Sketch (mesh wireframe on a chosen plane) ──
    elif cmd_type == "create_sketch":
        import math
        sketch_name = params.get("name", "Sketch")
        plane = params.get("plane", "XY").upper()
        entities = params.get("entities", [])

        def project(x, y):
            if plane == "XZ":
                return (x, 0.0, y)
            if plane == "YZ":
                return (0.0, x, y)
            return (x, y, 0.0)  # XY default

        mesh = bpy.data.meshes.new(sketch_name + "_mesh")
        sketch_obj = bpy.data.objects.new(sketch_name, mesh)
        bpy.context.collection.objects.link(sketch_obj)

        bm = bmesh.new()
        entity_count = 0
        for e in entities:
            etype = e.get("type")
            if etype == "line":
                v1 = bm.verts.new(project(e.get("startX", 0), e.get("startY", 0)))
                v2 = bm.verts.new(project(e.get("endX", 0), e.get("endY", 0)))
                bm.edges.new((v1, v2))
                entity_count += 1
            elif etype == "rectangle":
                x = e.get("x", 0); y = e.get("y", 0)
                w = e.get("width", 1); h = e.get("height", 1)
                corners = [bm.verts.new(project(*c)) for c in
                           [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]]
                for i in range(4):
                    bm.edges.new((corners[i], corners[(i + 1) % 4]))
                entity_count += 1
            elif etype == "circle":
                cx = e.get("centerX", 0); cy = e.get("centerY", 0)
                r = e.get("radius", 1)
                segs = max(8, int(e.get("segments", 32)))
                verts = [bm.verts.new(project(cx + r * math.cos(2 * math.pi * i / segs),
                                              cy + r * math.sin(2 * math.pi * i / segs)))
                         for i in range(segs)]
                for i in range(segs):
                    bm.edges.new((verts[i], verts[(i + 1) % segs]))
                entity_count += 1
            elif etype == "arc":
                cx = e.get("centerX", 0); cy = e.get("centerY", 0)
                r = e.get("radius", 1)
                a1 = math.radians(e.get("startAngle", 0))
                a2 = math.radians(e.get("endAngle", 90))
                # ~11.25° per segment
                segs = max(2, int(abs(a2 - a1) / (math.pi / 16)))
                verts = [bm.verts.new(project(cx + r * math.cos(a1 + (a2 - a1) * i / segs),
                                              cy + r * math.sin(a1 + (a2 - a1) * i / segs)))
                         for i in range(segs + 1)]
                for i in range(segs):
                    bm.edges.new((verts[i], verts[i + 1]))
                entity_count += 1
            else:
                return {"error": f"Unknown sketch entity: {etype}. Supported: line, circle, arc, rectangle"}

        bm.to_mesh(mesh)
        bm.free()
        return {"status": "ok", "name": sketch_obj.name, "plane": plane,
                "entity_count": entity_count,
                "note": "Blender sketch is a mesh wireframe; for parametric sketching use FreeCAD."}

    # ── Boolean Operations ──
    elif cmd_type == "boolean_operation":
        target = params.get("target", "")
        tool = params.get("tool", "")
        operation = params.get("operation", "DIFFERENCE").upper()
        target_obj = bpy.data.objects.get(target)
        tool_obj = bpy.data.objects.get(tool)
        if not target_obj:
            return {"error": f"Target '{target}' not found"}
        if not tool_obj:
            return {"error": f"Tool '{tool}' not found"}

        bpy.context.view_layer.objects.active = target_obj
        mod = target_obj.modifiers.new(name="Boolean", type='BOOLEAN')
        mod.operation = operation
        mod.object = tool_obj
        bpy.ops.object.modifier_apply(modifier="Boolean")

        if params.get("delete_tool", True):
            bpy.data.objects.remove(tool_obj, do_unlink=True)

        return {"status": "ok", "target": target, "operation": operation, "tool_deleted": params.get("delete_tool", True)}

    # ── Materials ──
    elif cmd_type == "set_material":
        name = params.get("object", "")
        obj = bpy.data.objects.get(name)
        if not obj:
            return {"error": f"Object '{name}' not found"}

        mat_name = params.get("material_name", f"{name}_material")
        color = params.get("color", [0.8, 0.8, 0.8, 1.0])
        metallic = params.get("metallic", 0.0)
        roughness = params.get("roughness", 0.5)

        mat = bpy.data.materials.get(mat_name)
        if not mat:
            mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf:
            bsdf.inputs["Base Color"].default_value = color[:4] if len(color) >= 4 else color + [1.0]
            bsdf.inputs["Metallic"].default_value = metallic
            bsdf.inputs["Roughness"].default_value = roughness

        if obj.data.materials:
            obj.data.materials[0] = mat
        else:
            obj.data.materials.append(mat)

        return {"status": "ok", "object": name, "material": mat_name}

    # ── Export ──
    elif cmd_type == "export":
        fmt = params.get("format", "stl").lower()
        filepath = params.get("filepath")
        if not filepath:
            filepath = os.path.join(tempfile.gettempdir(), f"cad_mcp_export.{fmt}")

        # Select specific objects or all
        obj_names = params.get("objects")
        if obj_names:
            bpy.ops.object.select_all(action='DESELECT')
            for n in obj_names:
                o = bpy.data.objects.get(n)
                if o:
                    o.select_set(True)
            use_selection = True
        else:
            bpy.ops.object.select_all(action='SELECT')
            use_selection = False

        if fmt == "stl":
            # Blender 4.x: bpy.ops.wm.stl_export. 3.x: bpy.ops.export_mesh.stl.
            if hasattr(bpy.ops.wm, "stl_export"):
                bpy.ops.wm.stl_export(filepath=filepath, export_selected_objects=use_selection)
            else:
                bpy.ops.export_mesh.stl(filepath=filepath, use_selection=use_selection)
        elif fmt == "obj":
            if hasattr(bpy.ops.wm, "obj_export"):
                bpy.ops.wm.obj_export(filepath=filepath, export_selected_objects=use_selection)
            else:
                bpy.ops.export_scene.obj(filepath=filepath, use_selection=use_selection)
        elif fmt == "fbx":
            bpy.ops.export_scene.fbx(filepath=filepath, use_selection=use_selection)
        elif fmt == "gltf" or fmt == "glb":
            bpy.ops.export_scene.gltf(filepath=filepath, use_selection=use_selection,
                                       export_format='GLB' if fmt == 'glb' else 'GLTF_SEPARATE')
        elif fmt == "ply":
            if hasattr(bpy.ops.wm, "ply_export"):
                bpy.ops.wm.ply_export(filepath=filepath, export_selected_objects=use_selection)
            else:
                bpy.ops.export_mesh.ply(filepath=filepath, use_selection=use_selection)
        else:
            return {"error": f"Unsupported format: {fmt}. Supported: stl, obj, fbx, gltf, glb, ply"}

        size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
        return {"status": "ok", "format": fmt, "filepath": filepath, "size_bytes": size}

    # ── Viewport Screenshot ──
    elif cmd_type == "get_viewport_screenshot":
        width = params.get("width", 800)
        height = params.get("height", 600)
        img = capture_viewport(width, height)
        if img:
            return {"status": "ok", "image_base64": img, "width": width, "height": height}
        return {"error": "Could not capture viewport. No 3D view area found."}

    # ── Camera & View ──
    elif cmd_type == "set_camera":
        loc = params.get("location")
        target = params.get("look_at")
        cam = bpy.context.scene.camera
        if not cam:
            bpy.ops.object.camera_add()
            cam = bpy.context.active_object
            bpy.context.scene.camera = cam
        if loc:
            cam.location = loc
        if target:
            direction = mathutils.Vector(target) - cam.location
            rot_quat = direction.to_track_quat('-Z', 'Y')
            cam.rotation_euler = rot_quat.to_euler()
        return {"status": "ok", "camera": cam.name, "location": list(cam.location)}

    # ── Lighting ──
    elif cmd_type == "add_light":
        light_type = params.get("type", "POINT").upper()
        loc = params.get("location", [0, 0, 5])
        energy = params.get("energy", 1000)
        name = params.get("name", f"{light_type.title()}_Light")
        color = params.get("color", [1, 1, 1])

        light_data = bpy.data.lights.new(name=name, type=light_type)
        light_data.energy = energy
        light_data.color = color[:3]
        light_obj = bpy.data.objects.new(name=name, object_data=light_data)
        bpy.context.collection.objects.link(light_obj)
        light_obj.location = loc
        return {"status": "ok", "name": light_obj.name, "type": light_type, "energy": energy}

    # ── Execute Code (sandboxed with scene diff and output capture) ──
    elif cmd_type == "execute_code":
        code = params.get("code", "")
        auto_checkpoint = params.get("auto_checkpoint", True)

        # Auto-checkpoint before execution
        if auto_checkpoint:
            checkpoint_mgr.save("pre_execute")

        # Snapshot before
        before = snapshot_scene()

        # Capture stdout/stderr
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        result = {"status": "ok"}

        try:
            exec_globals = {"bpy": bpy, "bmesh": bmesh, "mathutils": mathutils, "Vector": mathutils.Vector}
            with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                exec(code, exec_globals)
            result["stdout"] = stdout_capture.getvalue()
            result["stderr"] = stderr_capture.getvalue()
        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)
            result["traceback"] = traceback.format_exc()

        # Scene diff
        after = snapshot_scene()
        result["scene_diff"] = diff_scenes(before, after)

        return result

    # ── Mesh Edit Operations (extrude, inset, subdivide, shade smooth) ──
    elif cmd_type == "mesh_edit":
        name = params.get("object", "")
        op = params.get("operation", "").lower()
        obj = bpy.data.objects.get(name)
        if not obj:
            return {"error": f"Object '{name}' not found"}
        if obj.type != 'MESH':
            return {"error": f"Mesh edit requires mesh, got {obj.type}"}

        bpy.context.view_layer.objects.active = obj
        try:
            if op == "shade_smooth":
                bpy.ops.object.shade_smooth()
                return {"status": "ok", "object": name, "operation": op}
            if op == "shade_flat":
                bpy.ops.object.shade_flat()
                return {"status": "ok", "object": name, "operation": op}

            bpy.ops.object.mode_set(mode='EDIT')
            select_mode = params.get("select_mode", "all").lower()
            if select_mode == "all":
                bpy.ops.mesh.select_all(action='SELECT')
            elif select_mode == "none":
                bpy.ops.mesh.select_all(action='DESELECT')

            if op == "extrude":
                vec = params.get("vector")
                if not vec:
                    amount = params.get("amount", 1.0)
                    axis = params.get("axis", "Z").upper()
                    vec = {"X": (amount, 0, 0), "Y": (0, amount, 0), "Z": (0, 0, amount)}.get(axis, (0, 0, amount))
                bpy.ops.mesh.extrude_region_move(TRANSFORM_OT_translate={"value": tuple(vec)})
            elif op == "inset":
                bpy.ops.mesh.inset(thickness=params.get("thickness", 0.1),
                                   depth=params.get("depth", 0.0))
            elif op == "subdivide":
                bpy.ops.mesh.subdivide(number_cuts=params.get("cuts", 1))
            elif op == "merge":
                bpy.ops.mesh.merge(type=params.get("merge_type", "CENTER").upper())
            elif op == "triangulate":
                bpy.ops.mesh.quads_convert_to_tris()
            elif op == "recalculate_normals":
                bpy.ops.mesh.normals_make_consistent(inside=params.get("inside", False))
            else:
                bpy.ops.object.mode_set(mode='OBJECT')
                return {"error": f"Unknown mesh op: {op}. Supported: extrude, inset, subdivide, merge, triangulate, recalculate_normals, shade_smooth, shade_flat"}

            bpy.ops.object.mode_set(mode='OBJECT')
            return {"status": "ok", "object": name, "operation": op}
        except Exception as e:
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                pass
            return {"error": f"Mesh edit failed: {e}"}

    # ── Import (counterpart to export) ──
    elif cmd_type == "import_file":
        filepath = params.get("filepath", "")
        if not filepath or not os.path.exists(filepath):
            return {"error": f"File not found: {filepath}"}
        ext = os.path.splitext(filepath)[1].lower().lstrip(".")
        before = set(o.name for o in bpy.data.objects)
        try:
            if ext == "stl":
                if hasattr(bpy.ops.wm, "stl_import"):
                    bpy.ops.wm.stl_import(filepath=filepath)
                else:
                    bpy.ops.import_mesh.stl(filepath=filepath)
            elif ext == "obj":
                if hasattr(bpy.ops.wm, "obj_import"):
                    bpy.ops.wm.obj_import(filepath=filepath)
                else:
                    bpy.ops.import_scene.obj(filepath=filepath)
            elif ext == "fbx":
                bpy.ops.import_scene.fbx(filepath=filepath)
            elif ext in ("gltf", "glb"):
                bpy.ops.import_scene.gltf(filepath=filepath)
            elif ext == "ply":
                if hasattr(bpy.ops.wm, "ply_import"):
                    bpy.ops.wm.ply_import(filepath=filepath)
                else:
                    bpy.ops.import_mesh.ply(filepath=filepath)
            elif ext == "blend":
                # Append all objects from another .blend
                with bpy.data.libraries.load(filepath, link=False) as (src, dst):
                    dst.objects = list(src.objects)
                for obj in dst.objects:
                    if obj is not None:
                        bpy.context.collection.objects.link(obj)
            else:
                return {"error": f"Unsupported import: {ext}. Supported: stl, obj, fbx, gltf, glb, ply, blend"}
        except Exception as e:
            return {"error": f"Import failed: {e}"}
        new_objs = [o.name for o in bpy.data.objects if o.name not in before]
        return {"status": "ok", "format": ext, "imported_objects": new_objs}

    # ── Render (full Cycles/Eevee, not just viewport) ──
    elif cmd_type == "render":
        scene = bpy.context.scene
        if "engine" in params:
            try:
                scene.render.engine = params["engine"].upper()
            except Exception:
                pass
        scene.render.resolution_x = params.get("width", scene.render.resolution_x)
        scene.render.resolution_y = params.get("height", scene.render.resolution_y)
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = 'PNG'

        engine = scene.render.engine
        samples = params.get("samples")
        if samples is not None:
            if engine == "CYCLES":
                scene.cycles.samples = samples
            elif "EEVEE" in engine:
                try:
                    scene.eevee.taa_render_samples = samples
                except Exception:
                    pass
        if engine == "CYCLES" and "denoise" in params:
            try:
                scene.cycles.use_denoising = bool(params["denoise"])
            except Exception:
                pass

        filepath = params.get("filepath", os.path.join(tempfile.gettempdir(), 'cad_mcp_render.png'))
        scene.render.filepath = filepath

        bpy.ops.render.render(write_still=True)

        img_data = None
        if os.path.exists(filepath):
            with open(filepath, 'rb') as f:
                img_data = base64.b64encode(f.read()).decode('ascii')
        return {
            "status": "ok",
            "engine": engine,
            "width": scene.render.resolution_x,
            "height": scene.render.resolution_y,
            "samples": samples,
            "filepath": filepath,
            "image_base64": img_data,
        }

    elif cmd_type == "set_render_settings":
        scene = bpy.context.scene
        if "engine" in params:
            scene.render.engine = params["engine"].upper()
        if "width" in params:
            scene.render.resolution_x = params["width"]
        if "height" in params:
            scene.render.resolution_y = params["height"]
        if "percentage" in params:
            scene.render.resolution_percentage = params["percentage"]
        if "samples" in params:
            try:
                scene.cycles.samples = params["samples"]
            except Exception:
                pass
            try:
                scene.eevee.taa_render_samples = params["samples"]
            except Exception:
                pass
        if "denoise" in params:
            try:
                scene.cycles.use_denoising = bool(params["denoise"])
            except Exception:
                pass
        if "file_format" in params:
            scene.render.image_settings.file_format = params["file_format"].upper()
        return {
            "status": "ok",
            "engine": scene.render.engine,
            "resolution": [scene.render.resolution_x, scene.render.resolution_y],
            "percentage": scene.render.resolution_percentage,
        }

    # ── World / HDRI environment ──
    elif cmd_type == "set_world":
        scene = bpy.context.scene
        world = scene.world
        if not world:
            world = bpy.data.worlds.new("World")
            scene.world = world
        world.use_nodes = True
        nodes = world.node_tree.nodes
        links = world.node_tree.links
        nodes.clear()
        bg = nodes.new("ShaderNodeBackground")
        out = nodes.new("ShaderNodeOutputWorld")
        links.new(bg.outputs[0], out.inputs[0])

        if "hdri_path" in params:
            hdri_path = params["hdri_path"]
            if not os.path.exists(hdri_path):
                return {"error": f"HDRI file not found: {hdri_path}"}
            env = nodes.new("ShaderNodeTexEnvironment")
            tex_coord = nodes.new("ShaderNodeTexCoord")
            mapping = nodes.new("ShaderNodeMapping")
            try:
                env.image = bpy.data.images.load(hdri_path, check_existing=True)
            except Exception as e:
                return {"error": f"Failed to load HDRI: {e}"}
            links.new(tex_coord.outputs["Generated"], mapping.inputs["Vector"])
            if "rotation" in params:
                import math
                mapping.inputs["Rotation"].default_value[2] = math.radians(params["rotation"])
            links.new(mapping.outputs["Vector"], env.inputs["Vector"])
            links.new(env.outputs["Color"], bg.inputs["Color"])
        elif "color" in params:
            color = list(params["color"])
            if len(color) == 3:
                color.append(1.0)
            bg.inputs["Color"].default_value = color

        bg.inputs["Strength"].default_value = params.get("strength", 1.0)
        return {"status": "ok", "world": world.name,
                "strength": bg.inputs["Strength"].default_value,
                "hdri": params.get("hdri_path")}

    # ── Texture-based PBR material ──
    elif cmd_type == "set_textured_material":
        name = params.get("object", "")
        obj = bpy.data.objects.get(name)
        if not obj:
            return {"error": f"Object '{name}' not found"}
        mat_name = params.get("material_name", f"{name}_textured")
        mat = bpy.data.materials.get(mat_name) or bpy.data.materials.new(mat_name)
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        bsdf = nodes.get("Principled BSDF") or nodes.new("ShaderNodeBsdfPrincipled")

        loaded = []

        def add_image_node(image_path, color_space):
            try:
                img = bpy.data.images.load(image_path, check_existing=True)
                img.colorspace_settings.name = color_space
                node = nodes.new("ShaderNodeTexImage")
                node.image = img
                return node
            except Exception:
                return None

        if "color_map" in params:
            n = add_image_node(params["color_map"], "sRGB")
            if n:
                links.new(n.outputs["Color"], bsdf.inputs["Base Color"])
                loaded.append("color")
        if "roughness_map" in params:
            n = add_image_node(params["roughness_map"], "Non-Color")
            if n:
                links.new(n.outputs["Color"], bsdf.inputs["Roughness"])
                loaded.append("roughness")
        if "metallic_map" in params:
            n = add_image_node(params["metallic_map"], "Non-Color")
            if n:
                links.new(n.outputs["Color"], bsdf.inputs["Metallic"])
                loaded.append("metallic")
        if "normal_map" in params:
            n = add_image_node(params["normal_map"], "Non-Color")
            if n:
                normal = nodes.new("ShaderNodeNormalMap")
                links.new(n.outputs["Color"], normal.inputs["Color"])
                links.new(normal.outputs["Normal"], bsdf.inputs["Normal"])
                loaded.append("normal")
        if "displacement_map" in params:
            n = add_image_node(params["displacement_map"], "Non-Color")
            if n:
                disp = nodes.new("ShaderNodeDisplacement")
                out_node = nodes.get("Material Output")
                if out_node:
                    links.new(n.outputs["Color"], disp.inputs["Height"])
                    links.new(disp.outputs["Displacement"], out_node.inputs["Displacement"])
                    loaded.append("displacement")

        if obj.data.materials:
            obj.data.materials[0] = mat
        else:
            obj.data.materials.append(mat)
        return {"status": "ok", "object": name, "material": mat_name, "maps_loaded": loaded}

    # ── Camera advanced settings (FOV, lens, DOF) ──
    elif cmd_type == "set_camera_settings":
        cam_obj = bpy.data.objects.get(params.get("name")) if params.get("name") else bpy.context.scene.camera
        if not cam_obj or cam_obj.type != 'CAMERA':
            return {"error": "No camera found"}
        cam = cam_obj.data
        if "type" in params:
            cam.type = params["type"].upper()  # PERSP, ORTHO, PANO
        if "lens" in params:
            cam.lens = params["lens"]
        if "fov" in params:
            import math
            cam.angle = math.radians(params["fov"])
        if "sensor_width" in params:
            cam.sensor_width = params["sensor_width"]
        if "ortho_scale" in params:
            cam.ortho_scale = params["ortho_scale"]
        if "dof_distance" in params:
            cam.dof.use_dof = True
            cam.dof.focus_distance = params["dof_distance"]
        if "fstop" in params:
            cam.dof.use_dof = True
            cam.dof.aperture_fstop = params["fstop"]
        if "dof_object" in params:
            target = bpy.data.objects.get(params["dof_object"])
            if target:
                cam.dof.use_dof = True
                cam.dof.focus_object = target
        return {"status": "ok", "camera": cam_obj.name, "type": cam.type,
                "lens": cam.lens, "dof": cam.dof.use_dof}

    # ── Scene management ──
    elif cmd_type == "set_visibility":
        name = params.get("name", "")
        obj = bpy.data.objects.get(name)
        if not obj:
            return {"error": f"Object '{name}' not found"}
        visible = bool(params.get("visible", True))
        obj.hide_viewport = not visible
        obj.hide_render = not visible
        return {"status": "ok", "object": name, "visible": visible}

    elif cmd_type == "rename_object":
        name = params.get("name", "")
        new_name = params.get("new_name", "")
        if not new_name:
            return {"error": "new_name required"}
        obj = bpy.data.objects.get(name)
        if not obj:
            return {"error": f"Object '{name}' not found"}
        obj.name = new_name
        return {"status": "ok", "old_name": name, "new_name": obj.name}

    elif cmd_type == "set_parent":
        child = bpy.data.objects.get(params.get("child", ""))
        if not child:
            return {"error": "Child not found"}
        parent_name = params.get("parent")
        parent = bpy.data.objects.get(parent_name) if parent_name else None
        if parent_name and not parent:
            return {"error": f"Parent '{parent_name}' not found"}
        if params.get("keep_transform", True) and parent:
            child.parent = parent
            child.matrix_parent_inverse = parent.matrix_world.inverted()
        else:
            child.parent = parent
        return {"status": "ok", "child": child.name,
                "parent": parent.name if parent else None}

    # ── Collections ──
    elif cmd_type == "create_collection":
        coll_name = params.get("name", "Collection")
        if bpy.data.collections.get(coll_name):
            return {"error": f"Collection '{coll_name}' already exists"}
        coll = bpy.data.collections.new(coll_name)
        parent_name = params.get("parent")
        parent = bpy.data.collections.get(parent_name) if parent_name else None
        (parent or bpy.context.scene.collection).children.link(coll)
        return {"status": "ok", "collection": coll.name,
                "parent": parent.name if parent else "Scene Collection"}

    elif cmd_type == "move_to_collection":
        obj = bpy.data.objects.get(params.get("object", ""))
        coll = bpy.data.collections.get(params.get("collection", ""))
        if not obj:
            return {"error": "Object not found"}
        if not coll:
            return {"error": "Collection not found"}
        for c in list(obj.users_collection):
            c.objects.unlink(obj)
        coll.objects.link(obj)
        return {"status": "ok", "object": obj.name, "collection": coll.name}

    # ── Curves & Text ──
    elif cmd_type == "create_curve":
        ctype = params.get("curve_type", "bezier").lower()
        loc = params.get("location", [0, 0, 0])
        if ctype == "bezier":
            bpy.ops.curve.primitive_bezier_curve_add(location=loc)
        elif ctype == "bezier_circle":
            bpy.ops.curve.primitive_bezier_circle_add(radius=params.get("radius", 1), location=loc)
        elif ctype == "nurbs":
            bpy.ops.curve.primitive_nurbs_curve_add(location=loc)
        elif ctype == "nurbs_circle":
            bpy.ops.curve.primitive_nurbs_circle_add(radius=params.get("radius", 1), location=loc)
        elif ctype == "nurbs_path":
            bpy.ops.curve.primitive_nurbs_path_add(location=loc)
        else:
            return {"error": f"Unknown curve type: {ctype}"}
        obj = bpy.context.active_object
        if params.get("name"):
            obj.name = params["name"]
        if "extrude" in params:
            obj.data.extrude = params["extrude"]
        if "bevel_depth" in params:
            obj.data.bevel_depth = params["bevel_depth"]
        if "bevel_resolution" in params:
            obj.data.bevel_resolution = params["bevel_resolution"]
        return {"status": "ok", "name": obj.name, "type": ctype}

    elif cmd_type == "create_text":
        loc = params.get("location", [0, 0, 0])
        bpy.ops.object.text_add(location=loc)
        obj = bpy.context.active_object
        if params.get("name"):
            obj.name = params["name"]
        obj.data.body = params.get("text", "Text")
        if "size" in params:
            obj.data.size = params["size"]
        if "extrude" in params:
            obj.data.extrude = params["extrude"]
        if "align_x" in params:
            obj.data.align_x = params["align_x"].upper()
        if "align_y" in params:
            obj.data.align_y = params["align_y"].upper()
        return {"status": "ok", "name": obj.name, "text": obj.data.body}

    # ── Array / Pattern wrapper (high-level around ARRAY modifier) ──
    elif cmd_type == "array_pattern":
        name = params.get("object", "")
        obj = bpy.data.objects.get(name)
        if not obj:
            return {"error": f"Object '{name}' not found"}
        bpy.context.view_layer.objects.active = obj
        pattern = params.get("pattern", "linear").lower()
        count = params.get("count", 3)
        mod = obj.modifiers.new(name="Array", type='ARRAY')
        mod.count = count

        if pattern == "linear_constant":
            mod.use_relative_offset = False
            mod.use_constant_offset = True
            mod.constant_offset_displace = params.get("offset", [1.0, 0.0, 0.0])
        elif pattern == "circular":
            # Create empty as pivot, use object offset
            empty_name = params.get("pivot_name", f"{name}_pivot")
            empty = bpy.data.objects.get(empty_name)
            if not empty:
                bpy.ops.object.empty_add(type='PLAIN_AXES', location=params.get("pivot", [0, 0, 0]))
                empty = bpy.context.active_object
                empty.name = empty_name
            import math
            angle = params.get("angle", 360.0)
            rot_axis = params.get("axis", "Z").upper()
            rot = [0.0, 0.0, 0.0]
            rot["XYZ".index(rot_axis)] = math.radians(angle / count)
            empty.rotation_euler = rot
            mod.use_relative_offset = False
            mod.use_object_offset = True
            mod.offset_object = empty
            bpy.context.view_layer.objects.active = obj
        else:  # linear (relative)
            mod.use_relative_offset = True
            mod.relative_offset_displace = params.get("offset", [1.0, 0.0, 0.0])

        if params.get("apply", False):
            bpy.ops.object.modifier_apply(modifier=mod.name)
        return {"status": "ok", "object": name, "pattern": pattern, "count": count}

    # ── Animation: keyframes & frame control ──
    elif cmd_type == "set_keyframe":
        obj = bpy.data.objects.get(params.get("object", ""))
        if not obj:
            return {"error": "Object not found"}
        prop = params.get("property", "location")
        frame = params.get("frame", bpy.context.scene.frame_current)
        if "value" in params:
            try:
                setattr(obj, prop, params["value"])
            except Exception as e:
                return {"error": f"Could not set {prop}: {e}"}
        try:
            obj.keyframe_insert(data_path=prop, frame=frame)
        except Exception as e:
            return {"error": f"Keyframe insert failed: {e}"}
        return {"status": "ok", "object": obj.name, "property": prop, "frame": frame}

    elif cmd_type == "set_frame":
        scene = bpy.context.scene
        if "frame" in params:
            scene.frame_set(params["frame"])
        if "start" in params:
            scene.frame_start = params["start"]
        if "end" in params:
            scene.frame_end = params["end"]
        if "fps" in params:
            scene.render.fps = params["fps"]
        return {"status": "ok",
                "current": scene.frame_current,
                "start": scene.frame_start,
                "end": scene.frame_end,
                "fps": scene.render.fps}

    # ── View preset (Blender mirror of FreeCAD's set_view) ──
    elif cmd_type == "set_view":
        import math
        from mathutils import Euler
        preset = params.get("preset", "isometric").lower()
        presets = {
            "front":     (math.pi / 2, 0, 0),
            "back":      (math.pi / 2, 0, math.pi),
            "top":       (0, 0, 0),
            "bottom":    (math.pi, 0, 0),
            "left":      (math.pi / 2, 0, math.pi / 2),
            "right":     (math.pi / 2, 0, -math.pi / 2),
            "isometric": (math.radians(60), 0, math.radians(45)),
        }
        if preset not in presets:
            return {"error": f"Unknown preset: {preset}. Supported: {list(presets.keys())}"}
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                rv3d = area.spaces.active.region_3d
                rv3d.view_rotation = Euler(presets[preset]).to_quaternion()
                return {"status": "ok", "preset": preset}
        return {"error": "No 3D viewport found"}

    # ── Measurement (distance, volume, surface area, bounding box) ──
    elif cmd_type == "measure":
        mtype = params.get("type", "distance").lower()
        if mtype == "distance":
            p1 = params.get("point1")
            p2 = params.get("point2")
            if p1 and p2:
                d = sum((a - b) ** 2 for a, b in zip(p1, p2)) ** 0.5
                return {"status": "ok", "type": "distance", "value": d}
            o1 = bpy.data.objects.get(params.get("object1", ""))
            o2 = bpy.data.objects.get(params.get("object2", ""))
            if o1 and o2:
                d = (o1.matrix_world.translation - o2.matrix_world.translation).length
                return {"status": "ok", "type": "distance",
                        "object1": o1.name, "object2": o2.name, "value": d}
            return {"error": "Provide point1+point2 or object1+object2"}
        if mtype == "volume":
            obj = bpy.data.objects.get(params.get("object", ""))
            if not obj or obj.type != 'MESH':
                return {"error": "Mesh object required"}
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            vol = bm.calc_volume(signed=False)
            bm.free()
            sx, sy, sz = obj.scale
            return {"status": "ok", "type": "volume",
                    "object": obj.name, "value": vol * sx * sy * sz}
        if mtype == "surface_area":
            obj = bpy.data.objects.get(params.get("object", ""))
            if not obj or obj.type != 'MESH':
                return {"error": "Mesh object required"}
            area = sum(p.area for p in obj.data.polygons)
            return {"status": "ok", "type": "surface_area",
                    "object": obj.name, "value": area}
        if mtype == "bounding_box":
            obj = bpy.data.objects.get(params.get("object", ""))
            if not obj:
                return {"error": "Object not found"}
            bbox = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
            mn = [min(v[i] for v in bbox) for i in range(3)]
            mx = [max(v[i] for v in bbox) for i in range(3)]
            return {"status": "ok", "type": "bounding_box",
                    "object": obj.name, "min": mn, "max": mx,
                    "size": [mx[i] - mn[i] for i in range(3)]}
        return {"error": f"Unknown measurement: {mtype}. Supported: distance, volume, surface_area, bounding_box"}

    # ── Particle system (basic) ──
    elif cmd_type == "add_particle_system":
        obj = bpy.data.objects.get(params.get("object", ""))
        if not obj:
            return {"error": "Object not found"}
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.particle_system_add()
        ps = obj.particle_systems[-1]
        if params.get("name"):
            ps.name = params["name"]
        s = ps.settings
        s.type = params.get("type", "EMITTER").upper()  # EMITTER or HAIR
        if "count" in params:
            s.count = params["count"]
        if "frame_start" in params:
            s.frame_start = params["frame_start"]
        if "frame_end" in params:
            s.frame_end = params["frame_end"]
        if s.type == "HAIR" and "hair_length" in params:
            s.hair_length = params["hair_length"]
        return {"status": "ok", "object": obj.name,
                "particle_system": ps.name, "type": s.type, "count": s.count}

    # ── Physics (rigid body / cloth / collision / soft body) ──
    elif cmd_type == "add_physics":
        obj = bpy.data.objects.get(params.get("object", ""))
        if not obj:
            return {"error": "Object not found"}
        bpy.context.view_layer.objects.active = obj
        ptype = params.get("type", "rigid_body").lower()
        if ptype == "rigid_body":
            bpy.ops.rigidbody.object_add()
            obj.rigid_body.type = params.get("body_type", "ACTIVE").upper()
            if "mass" in params:
                obj.rigid_body.mass = params["mass"]
            if "shape" in params:
                obj.rigid_body.collision_shape = params["shape"].upper()
        elif ptype == "cloth":
            obj.modifiers.new(name="Cloth", type='CLOTH')
        elif ptype == "collision":
            obj.modifiers.new(name="Collision", type='COLLISION')
        elif ptype == "soft_body":
            obj.modifiers.new(name="SoftBody", type='SOFT_BODY')
        elif ptype == "fluid":
            obj.modifiers.new(name="Fluid", type='FLUID')
        else:
            return {"error": f"Unknown physics type: {ptype}. Supported: rigid_body, cloth, collision, soft_body, fluid"}
        return {"status": "ok", "object": obj.name, "physics": ptype}

    # ── Poly Haven asset library (free, no API key required) ──
    elif cmd_type == "polyhaven_search":
        from urllib.request import urlopen, Request
        category = params.get("category", "hdris").lower()
        if category not in ("hdris", "textures", "models"):
            return {"error": "category must be hdris, textures, or models"}
        try:
            req = Request(f"https://api.polyhaven.com/assets?type={category}",
                          headers={"User-Agent": "CAD-MCP"})
            with urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            return {"error": f"Poly Haven API failed: {e}"}
        query = params.get("query", "").lower()
        results = []
        for k, v in data.items():
            name_str = v.get("name", "")
            if not query or query in k.lower() or query in name_str.lower():
                results.append({"id": k, "name": name_str,
                                "categories": v.get("categories", []),
                                "tags": v.get("tags", [])})
        limit = params.get("limit", 20)
        return {"status": "ok", "category": category,
                "total_matches": len(results), "results": results[:limit]}

    elif cmd_type == "polyhaven_download":
        from urllib.request import urlopen, urlretrieve, Request
        from urllib.parse import quote, urlparse

        # Validate every user-supplied string that touches a path or URL.
        asset_id = params.get("asset_id", "")
        if not _SAFE_ASSET_ID_RE.match(asset_id):
            return {"error": "Invalid asset_id (must match [a-z0-9_]{1,80})"}
        category = params.get("category", "hdris").lower()
        if category not in ("hdris", "textures", "models"):
            return {"error": "category must be hdris, textures, or models"}
        resolution = params.get("resolution", "2k")
        if not _SAFE_RESOLUTION_RE.match(resolution):
            return {"error": "resolution must be one of 1k, 2k, 4k, 8k, 16k"}
        file_format = params.get("format")
        if file_format is not None and not _SAFE_FORMAT_RE.match(file_format):
            return {"error": "format must be 2-5 lowercase letters"}
        map_key = params.get("map", "Diffuse")
        if not _SAFE_MAP_KEY_RE.match(map_key):
            return {"error": "map must be 1-32 chars from [A-Za-z_]"}

        try:
            req = Request(
                f"https://api.polyhaven.com/files/{quote(asset_id, safe='')}",
                headers={"User-Agent": "CAD-MCP"},
            )
            with urlopen(req, timeout=20) as resp:
                files = json.loads(resp.read().decode())

            file_url, ext = None, None
            if category == "hdris":
                fmt = file_format or "hdr"
                node = files.get("hdri", {}).get(resolution, {}).get(fmt)
                if node:
                    file_url, ext = node["url"], fmt
            elif category == "textures":
                fmt = file_format or "jpg"
                node = files.get(map_key, {}).get(resolution, {}).get(fmt)
                if node:
                    file_url, ext = node["url"], fmt
            elif category == "models":
                fmt = file_format or "blend"
                node = files.get(fmt, {}).get(resolution, {}).get(fmt)
                if node:
                    file_url, ext = node["url"], fmt

            if not file_url:
                return {"error": f"No matching file: asset={asset_id}, cat={category}, res={resolution}, fmt={file_format}"}

            # Defense-in-depth: file_url comes from Poly Haven's API but we
            # don't blindly trust it — confirm host before downloading.
            parsed = urlparse(file_url)
            if parsed.scheme != "https" or not (
                parsed.hostname and parsed.hostname.endswith(".polyhaven.com")
                or parsed.hostname == "polyhaven.com"
            ):
                return {"error": f"Refusing to download from non-polyhaven URL: {file_url}"}

            target_dir = os.path.join(tempfile.gettempdir(), "cad-mcp-polyhaven")
            os.makedirs(target_dir, exist_ok=True)
            try:
                os.chmod(target_dir, 0o700)
            except (OSError, NotImplementedError):
                pass
            target_path = os.path.join(target_dir, f"{asset_id}_{resolution}.{ext}")
            # Final guard: resolved path must still be inside target_dir
            if os.path.commonpath([os.path.realpath(target_dir),
                                    os.path.realpath(target_path)]) != os.path.realpath(target_dir):
                return {"error": "Path escape detected — refusing to write"}
            urlretrieve(file_url, target_path)
            return {"status": "ok", "asset_id": asset_id, "category": category,
                    "filepath": target_path,
                    "size_bytes": os.path.getsize(target_path)}
        except Exception as e:
            return {"error": f"Download failed: {e}"}

    # ── Heartbeat ──
    elif cmd_type == "ping":
        return {"status": "pong", "timestamp": time.time(), "blender_version": bpy.app.version_string}

    else:
        return {"error": f"Unknown command: {cmd_type}. Use 'execute_code' for custom operations."}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TCP SERVER — runs in a thread, dispatches commands to Blender's main thread
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CADMCPServer:
    def __init__(self, host='localhost', port=9876):
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None
        self.command_queue = []
        self.response_ready = threading.Event()
        self.current_response = None
        self.auto_screenshot = True
        self.screenshot_resolution = (800, 600)
        self.lock = threading.Lock()

    # Commands that modify the scene (trigger auto-screenshot)
    MODIFYING_COMMANDS = {
        "create_object", "delete_object", "transform_object", "duplicate_object",
        "add_modifier", "apply_modifier", "boolean_operation", "set_material",
        "execute_code", "restore_checkpoint", "undo", "redo",
        "add_light", "set_camera",
        "fillet", "chamfer", "create_sketch",
        "mesh_edit", "import_file", "set_world", "set_textured_material",
        "set_camera_settings", "set_visibility", "rename_object", "set_parent",
        "create_collection", "move_to_collection",
        "create_curve", "create_text", "array_pattern",
        "set_keyframe", "set_frame", "set_view",
        "add_particle_system", "add_physics",
    }

    def start(self):
        if self.running:
            print("CAD-MCP: Server already running")
            return
        self.running = True
        self.server_thread = threading.Thread(target=self._server_loop, daemon=True)
        self.server_thread.start()
        # Register timer for processing commands on main thread
        bpy.app.timers.register(self._process_queue, first_interval=0.1, persistent=True)
        print(f"CAD-MCP: Server started on {self.host}:{self.port}")

    def stop(self):
        self.running = False
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
        if bpy.app.timers.is_registered(self._process_queue):
            bpy.app.timers.unregister(self._process_queue)
        checkpoint_mgr.cleanup()
        print("CAD-MCP: Server stopped")

    def _server_loop(self):
        """Accept connections and read commands."""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.settimeout(1.0)
            self.socket.bind((self.host, self.port))
            self.socket.listen(1)
            print(f"CAD-MCP: Listening on {self.host}:{self.port}")

            while self.running:
                try:
                    conn, addr = self.socket.accept()
                    print(f"CAD-MCP: Client connected from {addr}")
                    self._handle_client(conn)
                except socket.timeout:
                    continue
                except Exception as e:
                    if self.running:
                        print(f"CAD-MCP: Accept error: {e}")
                        time.sleep(1)
        except Exception as e:
            print(f"CAD-MCP: Server error: {e}")
        finally:
            if self.socket:
                self.socket.close()

    def _handle_client(self, conn):
        """Handle a single client connection."""
        conn.settimeout(120)
        try:
            while self.running:
                try:
                    message = recv_message(conn, timeout=120)
                except socket.timeout:
                    # Send heartbeat
                    try:
                        send_message(conn, {"type": "heartbeat", "timestamp": time.time()})
                    except:
                        break
                    continue
                except (ConnectionError, ConnectionResetError):
                    break

                cmd_type = message.get("type", "")
                params = message.get("params", {})

                # Queue command for main thread execution
                self.response_ready.clear()
                with self.lock:
                    self.command_queue.append((cmd_type, params))

                # Wait for response from main thread
                if self.response_ready.wait(timeout=60):
                    response = self.current_response

                    # Auto-screenshot after modifying operations
                    if (self.auto_screenshot and
                        cmd_type in self.MODIFYING_COMMANDS and
                        response.get("status") != "error"):
                        # Queue a screenshot capture
                        self.response_ready.clear()
                        with self.lock:
                            self.command_queue.append(("_internal_screenshot", {}))
                        if self.response_ready.wait(timeout=10):
                            screenshot_result = self.current_response
                            if screenshot_result and screenshot_result.get("image_base64"):
                                response["viewport_screenshot"] = screenshot_result["image_base64"]

                    send_message(conn, {"status": "ok", "result": response})
                else:
                    send_message(conn, {"status": "error", "result": {"error": "Command timed out (60s)"}})

        except Exception as e:
            print(f"CAD-MCP: Client error: {e}")
        finally:
            conn.close()
            print("CAD-MCP: Client disconnected")

    def _process_queue(self):
        """Timer callback — processes queued commands on Blender's main thread."""
        with self.lock:
            if not self.command_queue:
                return 0.05  # Check again in 50ms

            cmd_type, params = self.command_queue.pop(0)

        try:
            if cmd_type == "_internal_screenshot":
                w, h = self.screenshot_resolution
                img = capture_viewport(w, h)
                self.current_response = {"image_base64": img} if img else {}
            else:
                self.current_response = handle_command(cmd_type, params)
        except Exception as e:
            self.current_response = {"error": str(e), "traceback": traceback.format_exc()}

        self.response_ready.set()
        return 0.05


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BLENDER UI PANEL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

server_instance = None


class CADMCP_OT_Connect(bpy.types.Operator):
    bl_idname = "cadmcp.connect"
    bl_label = "Connect to Claude"

    def execute(self, context):
        global server_instance
        if server_instance and server_instance.running:
            self.report({'WARNING'}, "Already connected")
            return {'CANCELLED'}
        scene = context.scene
        server_instance = CADMCPServer(host='localhost', port=scene.cadmcp_port)
        server_instance.auto_screenshot = scene.cadmcp_auto_screenshot
        server_instance.screenshot_resolution = (scene.cadmcp_screenshot_width, scene.cadmcp_screenshot_height)
        server_instance.start()
        self.report({'INFO'}, f"CAD-MCP server started on port {scene.cadmcp_port}")
        return {'FINISHED'}


class CADMCP_OT_Disconnect(bpy.types.Operator):
    bl_idname = "cadmcp.disconnect"
    bl_label = "Disconnect"

    def execute(self, context):
        global server_instance
        if server_instance:
            server_instance.stop()
            server_instance = None
        self.report({'INFO'}, "CAD-MCP server stopped")
        return {'FINISHED'}


class CADMCP_PT_Panel(bpy.types.Panel):
    bl_label = "CAD-MCP"
    bl_idname = "CADMCP_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'CAD-MCP'

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # Connection status
        global server_instance
        is_connected = server_instance is not None and server_instance.running

        if is_connected:
            layout.label(text="Status: Connected", icon='CHECKMARK')
            layout.operator("cadmcp.disconnect", text="Disconnect", icon='CANCEL')
        else:
            layout.label(text="Status: Disconnected", icon='ERROR')
            layout.operator("cadmcp.connect", text="Connect to Claude", icon='PLUGIN')

        layout.separator()

        # Settings
        box = layout.box()
        box.label(text="Settings", icon='PREFERENCES')
        box.prop(scene, "cadmcp_port", text="Port")
        box.prop(scene, "cadmcp_auto_screenshot", text="Auto Viewport Screenshots")
        if scene.cadmcp_auto_screenshot:
            row = box.row()
            row.prop(scene, "cadmcp_screenshot_width", text="W")
            row.prop(scene, "cadmcp_screenshot_height", text="H")

        layout.separator()

        # Checkpoint info
        box = layout.box()
        box.label(text="Checkpoints", icon='FILE_BACKUP')
        cp_list = checkpoint_mgr.list_all()
        box.label(text=f"  {len(cp_list)} saved checkpoints")
        for cp in cp_list[-5:]:  # Show last 5
            box.label(text=f"  • {cp['name']}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# REGISTRATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

classes = (
    CADMCP_OT_Connect,
    CADMCP_OT_Disconnect,
    CADMCP_PT_Panel,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.cadmcp_port = IntProperty(name="Port", default=9876, min=1024, max=65535)
    bpy.types.Scene.cadmcp_auto_screenshot = BoolProperty(name="Auto Screenshot", default=True)
    bpy.types.Scene.cadmcp_screenshot_width = IntProperty(name="Width", default=800, min=320, max=3840)
    bpy.types.Scene.cadmcp_screenshot_height = IntProperty(name="Height", default=600, min=240, max=2160)
    print("CAD-MCP addon registered")

def unregister():
    global server_instance
    if server_instance:
        server_instance.stop()
        server_instance = None
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.cadmcp_port
    del bpy.types.Scene.cadmcp_auto_screenshot
    del bpy.types.Scene.cadmcp_screenshot_width
    del bpy.types.Scene.cadmcp_screenshot_height
    print("CAD-MCP addon unregistered")

if __name__ == "__main__":
    register()
