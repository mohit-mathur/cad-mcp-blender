#!/usr/bin/env node

/**
 * CAD-MCP for Blender
 *
 * Model Context Protocol server connecting Claude (and any MCP client) to a
 * live Blender instance via the companion addon. Features:
 *   - Checkpoint/undo system exposed as tools
 *   - Auto viewport screenshots after every modifying operation
 *   - Hierarchical scene queries (no monolithic dumps)
 *   - Selection/context awareness
 *   - Full PBR materials, HDRI environments, Cycles/Eevee rendering
 *   - Mesh editing (extrude, inset, subdivide, etc.)
 *   - Curves, text, particles, physics, animation keyframes
 *   - Poly Haven asset library (CC0 HDRIs / textures / models, no key)
 *   - Reliable length-prefixed protocol with auto-reconnect
 *
 * Env vars:
 *   BLENDER_HOST (default: localhost)
 *   BLENDER_PORT (default: 9876)
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';
import { CADSocketClient } from './socket-client.js';

// ── Single Blender client (lazy-connect) ──

let client = null;

function getClient() {
  if (!client) {
    client = new CADSocketClient(
      process.env.BLENDER_HOST || 'localhost',
      parseInt(process.env.BLENDER_PORT || '9876'),
      'Blender'
    );
  }
  return client;
}

async function send(commandType, params = {}) {
  const c = getClient();
  if (!c.connected) await c.connect();
  const response = await c.sendCommand(commandType, params);
  if (response.status === 'error') {
    throw new Error(response.result?.error || JSON.stringify(response.result));
  }
  return response.result;
}

// ── Tool Definitions ──

const TOOLS = [
  // ━━ Checkpoint & Undo ━━
  {
    name: 'cad_save_checkpoint',
    description: 'Save current state as a named checkpoint. Use before risky operations so you can roll back.',
    inputSchema: {
      type: 'object',
      properties: { name: { type: 'string', description: 'Checkpoint name (auto-generated if omitted)' } },
    },
  },
  {
    name: 'cad_restore_checkpoint',
    description: 'Restore state from a previously saved checkpoint.',
    inputSchema: {
      type: 'object',
      properties: { name: { type: 'string' } },
      required: ['name'],
    },
  },
  {
    name: 'cad_list_checkpoints',
    description: 'List all saved checkpoints.',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'cad_delete_checkpoint',
    description: 'Delete a named checkpoint to free disk space (does not affect current scene).',
    inputSchema: {
      type: 'object',
      properties: { name: { type: 'string' } },
      required: ['name'],
    },
  },
  {
    name: 'cad_undo',
    description: 'Undo the last operation(s). Faster than restoring a checkpoint for small mistakes. Note: Blender\'s undo from outside an operator can occasionally no-op; checkpoints are more reliable.',
    inputSchema: {
      type: 'object',
      properties: { count: { type: 'number', description: 'Steps to undo (default 1)' } },
    },
  },
  {
    name: 'cad_redo',
    description: 'Redo previously undone operation(s).',
    inputSchema: {
      type: 'object',
      properties: { count: { type: 'number' } },
    },
  },

  // ━━ Scene Queries ━━
  {
    name: 'cad_get_scene_summary',
    description: 'Lightweight overview: object count, types, active/selected. Always start here before drilling into details.',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'cad_get_object_details',
    description: 'Full details of one object: geometry, transforms, materials, modifiers, world bounding box.',
    inputSchema: {
      type: 'object',
      properties: { name: { type: 'string' } },
      required: ['name'],
    },
  },
  {
    name: 'cad_get_objects_by_type',
    description: 'List objects filtered by Blender type (MESH, CURVE, LIGHT, CAMERA, EMPTY, FONT, etc.).',
    inputSchema: {
      type: 'object',
      properties: { type: { type: 'string' } },
      required: ['type'],
    },
  },
  {
    name: 'cad_get_selection',
    description: "What the user has selected in Blender, including edit-mode geometry counts. Use to interpret spatial intent like 'fillet this' when the user has an edge selected.",
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'cad_get_object_tree',
    description: 'Parent/child hierarchy of all objects in the scene.',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'cad_scene_search',
    description: 'Search objects by name pattern (supports * and ? wildcards).',
    inputSchema: {
      type: 'object',
      properties: { query: { type: 'string' } },
      required: ['query'],
    },
  },

  // ━━ Object Operations ━━
  {
    name: 'cad_create_object',
    description: 'Create a primitive: cube, sphere, cylinder, cone, torus, plane, circle, icosphere, monkey.',
    inputSchema: {
      type: 'object',
      properties: {
        primitive: { type: 'string' },
        name: { type: 'string' },
        location: { type: 'array', items: { type: 'number' } },
        size: { type: 'number' },
        radius: { type: 'number' },
        depth: { type: 'number' },
        major_radius: { type: 'number' },
        minor_radius: { type: 'number' },
      },
      required: ['primitive'],
    },
  },
  {
    name: 'cad_delete_object',
    description: 'Delete an object by name.',
    inputSchema: {
      type: 'object',
      properties: { name: { type: 'string' } },
      required: ['name'],
    },
  },
  {
    name: 'cad_transform_object',
    description: 'Move, rotate, or scale an object. Rotation in degrees.',
    inputSchema: {
      type: 'object',
      properties: {
        name: { type: 'string' },
        location: { type: 'array', items: { type: 'number' } },
        rotation: { type: 'array', items: { type: 'number' }, description: 'Degrees [rx,ry,rz]' },
        scale: { type: 'array', items: { type: 'number' } },
      },
      required: ['name'],
    },
  },
  {
    name: 'cad_duplicate_object',
    description: 'Duplicate an object, optionally renaming it and placing it at a new location.',
    inputSchema: {
      type: 'object',
      properties: {
        name: { type: 'string' },
        new_name: { type: 'string' },
        location: { type: 'array', items: { type: 'number' } },
      },
      required: ['name'],
    },
  },

  {
    name: 'cad_boolean',
    description: 'Boolean op between two objects: union, difference, intersection.',
    inputSchema: {
      type: 'object',
      properties: {
        target: { type: 'string', description: 'Object to modify' },
        tool: { type: 'string', description: 'Object used as boolean tool' },
        operation: { type: 'string', enum: ['UNION', 'DIFFERENCE', 'INTERSECT'] },
        delete_tool: { type: 'boolean', description: 'Delete the tool object after (default true)' },
      },
      required: ['target', 'tool', 'operation'],
    },
  },

  // ━━ Edge Treatments ━━
  {
    name: 'cad_fillet',
    description: 'Round edges on a mesh (BEVEL modifier with multiple segments). Default applies the modifier permanently.',
    inputSchema: {
      type: 'object',
      properties: {
        object: { type: 'string' },
        width: { type: 'number', description: 'Bevel width' },
        segments: { type: 'number', description: 'Smoothness (default 3)' },
        limit_method: { type: 'string', description: 'NONE, ANGLE, WEIGHT, VGROUP (default ANGLE)' },
        apply: { type: 'boolean', description: 'Bake the modifier permanently (default true)' },
      },
      required: ['object'],
    },
  },
  {
    name: 'cad_chamfer',
    description: 'Angled-edge bevel (BEVEL modifier with 1 segment).',
    inputSchema: {
      type: 'object',
      properties: {
        object: { type: 'string' },
        width: { type: 'number' },
        limit_method: { type: 'string' },
        apply: { type: 'boolean' },
      },
      required: ['object'],
    },
  },

  // ━━ 2D Sketch (mesh wireframe) ━━
  {
    name: 'cad_create_sketch',
    description: 'Build a 2D wireframe sketch (lines/circles/arcs/rectangles) on the XY, XZ, or YZ plane. Note: this is a mesh, not a parametric sketch — for parametric sketching use FreeCAD.',
    inputSchema: {
      type: 'object',
      properties: {
        name: { type: 'string' },
        plane: { type: 'string', enum: ['XY', 'XZ', 'YZ'] },
        entities: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              type: { type: 'string', enum: ['line', 'circle', 'arc', 'rectangle'] },
              startX: { type: 'number' }, startY: { type: 'number' },
              endX: { type: 'number' }, endY: { type: 'number' },
              centerX: { type: 'number' }, centerY: { type: 'number' },
              radius: { type: 'number' },
              startAngle: { type: 'number' }, endAngle: { type: 'number' },
              x: { type: 'number' }, y: { type: 'number' },
              width: { type: 'number' }, height: { type: 'number' },
            },
            required: ['type'],
          },
        },
      },
      required: ['plane', 'entities'],
    },
  },

  // ━━ Materials ━━
  {
    name: 'cad_set_material',
    description: 'Apply a flat-color PBR material (color, metallic, roughness). For image textures use cad_set_textured_material.',
    inputSchema: {
      type: 'object',
      properties: {
        object: { type: 'string' },
        material_name: { type: 'string' },
        color: { type: 'array', items: { type: 'number' }, description: 'RGBA [r,g,b,a] 0-1' },
        metallic: { type: 'number' },
        roughness: { type: 'number' },
      },
      required: ['object'],
    },
  },
  {
    name: 'cad_set_textured_material',
    description: 'Image-based PBR material with color/roughness/metallic/normal/displacement maps. Pair with cad_polyhaven_download to get free CC0 textures.',
    inputSchema: {
      type: 'object',
      properties: {
        object: { type: 'string' },
        material_name: { type: 'string' },
        color_map: { type: 'string' },
        roughness_map: { type: 'string' },
        metallic_map: { type: 'string' },
        normal_map: { type: 'string' },
        displacement_map: { type: 'string' },
      },
      required: ['object'],
    },
  },

  // ━━ Modifiers ━━
  {
    name: 'cad_add_modifier',
    description: 'Add a Blender modifier (BEVEL, SOLIDIFY, MIRROR, ARRAY, SUBSURF, BOOLEAN, etc.). Stays live until you call cad_apply_modifier.',
    inputSchema: {
      type: 'object',
      properties: {
        object: { type: 'string' },
        modifier_type: { type: 'string' },
        modifier_name: { type: 'string' },
        settings: { type: 'object' },
      },
      required: ['object', 'modifier_type'],
    },
  },
  {
    name: 'cad_apply_modifier',
    description: 'Bake a modifier into the mesh permanently.',
    inputSchema: {
      type: 'object',
      properties: {
        object: { type: 'string' },
        modifier_name: { type: 'string' },
      },
      required: ['object', 'modifier_name'],
    },
  },

  // ━━ Mesh Editing ━━
  {
    name: 'cad_mesh_edit',
    description: 'Edit-mode mesh op on the whole mesh (or current selection). Operations: extrude, inset, subdivide, merge, triangulate, recalculate_normals, shade_smooth, shade_flat. The bridge from primitives + boolean to real modeling.',
    inputSchema: {
      type: 'object',
      properties: {
        object: { type: 'string' },
        operation: { type: 'string', enum: ['extrude', 'inset', 'subdivide', 'merge', 'triangulate', 'recalculate_normals', 'shade_smooth', 'shade_flat'] },
        amount: { type: 'number', description: 'Extrude distance along axis' },
        axis: { type: 'string', enum: ['X', 'Y', 'Z'] },
        vector: { type: 'array', items: { type: 'number' }, description: 'Explicit [x,y,z] for extrude (overrides amount/axis)' },
        thickness: { type: 'number', description: 'Inset thickness' },
        depth: { type: 'number' },
        cuts: { type: 'number' },
        merge_type: { type: 'string' },
        select_mode: { type: 'string', enum: ['all', 'none'] },
      },
      required: ['object', 'operation'],
    },
  },

  // ━━ Curves & Text ━━
  {
    name: 'cad_create_curve',
    description: 'Create a Bezier or NURBS curve. Set extrude > 0 for thickness, or bevel_depth > 0 for tube-like geometry.',
    inputSchema: {
      type: 'object',
      properties: {
        curve_type: { type: 'string', enum: ['bezier', 'bezier_circle', 'nurbs', 'nurbs_circle', 'nurbs_path'] },
        name: { type: 'string' },
        location: { type: 'array', items: { type: 'number' } },
        radius: { type: 'number' },
        extrude: { type: 'number' },
        bevel_depth: { type: 'number' },
        bevel_resolution: { type: 'number' },
      },
      required: ['curve_type'],
    },
  },
  {
    name: 'cad_create_text',
    description: 'Create a 3D text object. Set extrude for depth.',
    inputSchema: {
      type: 'object',
      properties: {
        text: { type: 'string' },
        name: { type: 'string' },
        location: { type: 'array', items: { type: 'number' } },
        size: { type: 'number' },
        extrude: { type: 'number' },
        align_x: { type: 'string' },
        align_y: { type: 'string' },
      },
      required: ['text'],
    },
  },

  // ━━ Array / Pattern ━━
  {
    name: 'cad_array_pattern',
    description: 'Linear or circular pattern of an object. linear = relative repetition; linear_constant = absolute offset; circular = rotated copies around a pivot.',
    inputSchema: {
      type: 'object',
      properties: {
        object: { type: 'string' },
        pattern: { type: 'string', enum: ['linear', 'linear_constant', 'circular'] },
        count: { type: 'number' },
        offset: { type: 'array', items: { type: 'number' } },
        axis: { type: 'string', enum: ['X', 'Y', 'Z'] },
        angle: { type: 'number', description: 'Total degrees for circular (default 360)' },
        pivot: { type: 'array', items: { type: 'number' } },
        apply: { type: 'boolean' },
      },
      required: ['object', 'pattern', 'count'],
    },
  },

  // ━━ Lighting ━━
  {
    name: 'cad_add_light',
    description: 'Add a light to the scene.',
    inputSchema: {
      type: 'object',
      properties: {
        type: { type: 'string', enum: ['POINT', 'SUN', 'SPOT', 'AREA'] },
        location: { type: 'array', items: { type: 'number' } },
        energy: { type: 'number' },
        color: { type: 'array', items: { type: 'number' } },
        name: { type: 'string' },
      },
      required: ['type'],
    },
  },
  {
    name: 'cad_set_world',
    description: 'Set the world environment for renders: HDRI image (best for realism) or solid color. The single biggest factor in render quality. Use cad_polyhaven_download to fetch a free HDRI first.',
    inputSchema: {
      type: 'object',
      properties: {
        hdri_path: { type: 'string', description: 'Absolute path to HDR/EXR' },
        color: { type: 'array', items: { type: 'number' }, description: 'RGBA fallback if no HDRI' },
        strength: { type: 'number' },
        rotation: { type: 'number', description: 'HDRI rotation in degrees around Z' },
      },
    },
  },

  // ━━ Camera ━━
  {
    name: 'cad_set_camera',
    description: 'Position the active camera and aim it.',
    inputSchema: {
      type: 'object',
      properties: {
        location: { type: 'array', items: { type: 'number' } },
        look_at: { type: 'array', items: { type: 'number' } },
      },
    },
  },
  {
    name: 'cad_set_camera_settings',
    description: 'Camera lens/FOV, sensor, projection, depth-of-field.',
    inputSchema: {
      type: 'object',
      properties: {
        name: { type: 'string', description: 'Camera object name (default scene camera)' },
        type: { type: 'string', enum: ['PERSP', 'ORTHO', 'PANO'] },
        lens: { type: 'number', description: 'Focal length in mm' },
        fov: { type: 'number', description: 'FOV in degrees (alt to lens)' },
        sensor_width: { type: 'number' },
        ortho_scale: { type: 'number' },
        dof_distance: { type: 'number' },
        fstop: { type: 'number' },
        dof_object: { type: 'string', description: 'Auto-focus on this object' },
      },
    },
  },

  // ━━ Animation ━━
  {
    name: 'cad_set_keyframe',
    description: 'Insert a keyframe on an object property at a given frame. Common props: location, rotation_euler, scale.',
    inputSchema: {
      type: 'object',
      properties: {
        object: { type: 'string' },
        property: { type: 'string' },
        frame: { type: 'number' },
        value: { description: 'Optional: set property to this value before keyframing' },
      },
      required: ['object', 'property'],
    },
  },
  {
    name: 'cad_set_frame',
    description: 'Set current frame, frame range, or framerate.',
    inputSchema: {
      type: 'object',
      properties: {
        frame: { type: 'number' },
        start: { type: 'number' },
        end: { type: 'number' },
        fps: { type: 'number' },
      },
    },
  },

  // ━━ Scene Management ━━
  {
    name: 'cad_set_visibility',
    description: 'Show or hide an object in viewport and renders.',
    inputSchema: {
      type: 'object',
      properties: {
        name: { type: 'string' },
        visible: { type: 'boolean' },
      },
      required: ['name', 'visible'],
    },
  },
  {
    name: 'cad_rename_object',
    description: 'Rename an object. Blender appends .001 if the name collides.',
    inputSchema: {
      type: 'object',
      properties: {
        name: { type: 'string' },
        new_name: { type: 'string' },
      },
      required: ['name', 'new_name'],
    },
  },
  {
    name: 'cad_set_parent',
    description: 'Parent one object to another (or unparent if parent omitted).',
    inputSchema: {
      type: 'object',
      properties: {
        child: { type: 'string' },
        parent: { type: 'string' },
        keep_transform: { type: 'boolean' },
      },
      required: ['child'],
    },
  },
  {
    name: 'cad_create_collection',
    description: 'Create a Blender collection for grouping objects.',
    inputSchema: {
      type: 'object',
      properties: {
        name: { type: 'string' },
        parent: { type: 'string' },
      },
      required: ['name'],
    },
  },
  {
    name: 'cad_move_to_collection',
    description: 'Move an object into a collection.',
    inputSchema: {
      type: 'object',
      properties: {
        object: { type: 'string' },
        collection: { type: 'string' },
      },
      required: ['object', 'collection'],
    },
  },

  // ━━ Particles & Physics ━━
  {
    name: 'cad_add_particle_system',
    description: 'Add a particle system (EMITTER for fire/smoke/dust, HAIR for grass/fur).',
    inputSchema: {
      type: 'object',
      properties: {
        object: { type: 'string' },
        name: { type: 'string' },
        type: { type: 'string', enum: ['EMITTER', 'HAIR'] },
        count: { type: 'number' },
        frame_start: { type: 'number' },
        frame_end: { type: 'number' },
        hair_length: { type: 'number' },
      },
      required: ['object'],
    },
  },
  {
    name: 'cad_add_physics',
    description: 'Add physics: rigid_body, cloth, collision, soft_body, fluid.',
    inputSchema: {
      type: 'object',
      properties: {
        object: { type: 'string' },
        type: { type: 'string', enum: ['rigid_body', 'cloth', 'collision', 'soft_body', 'fluid'] },
        body_type: { type: 'string', enum: ['ACTIVE', 'PASSIVE'] },
        mass: { type: 'number' },
        shape: { type: 'string' },
      },
      required: ['object', 'type'],
    },
  },

  // ━━ Measurement ━━
  {
    name: 'cad_measure',
    description: 'Distance between points/objects, or volume/surface area/bounding box of a mesh.',
    inputSchema: {
      type: 'object',
      properties: {
        type: { type: 'string', enum: ['distance', 'volume', 'surface_area', 'bounding_box'] },
        point1: { type: 'array', items: { type: 'number' } },
        point2: { type: 'array', items: { type: 'number' } },
        object1: { type: 'string' },
        object2: { type: 'string' },
        object: { type: 'string' },
      },
      required: ['type'],
    },
  },

  // ━━ Render ━━
  {
    name: 'cad_render',
    description: 'Full Cycles or Eevee render with materials, HDRIs, ray-tracing. Returns the rendered image. Slower than the viewport screenshot but presentation-quality.',
    inputSchema: {
      type: 'object',
      properties: {
        engine: { type: 'string', description: 'CYCLES, BLENDER_EEVEE_NEXT, BLENDER_EEVEE' },
        width: { type: 'number' },
        height: { type: 'number' },
        samples: { type: 'number' },
        denoise: { type: 'boolean' },
        filepath: { type: 'string' },
      },
    },
  },
  {
    name: 'cad_set_render_settings',
    description: 'Configure render settings without actually rendering.',
    inputSchema: {
      type: 'object',
      properties: {
        engine: { type: 'string' },
        width: { type: 'number' },
        height: { type: 'number' },
        percentage: { type: 'number' },
        samples: { type: 'number' },
        denoise: { type: 'boolean' },
        file_format: { type: 'string' },
      },
    },
  },

  // ━━ Viewport & Export & Import ━━
  {
    name: 'cad_get_viewport_screenshot',
    description: 'Capture the 3D viewport as a PNG (OpenGL render — fast, not ray-traced). Returned as an image content block.',
    inputSchema: {
      type: 'object',
      properties: {
        width: { type: 'number' },
        height: { type: 'number' },
      },
    },
  },
  {
    name: 'cad_set_view',
    description: 'Set the viewport to a preset angle: front, back, top, bottom, left, right, isometric.',
    inputSchema: {
      type: 'object',
      properties: {
        preset: { type: 'string', enum: ['front', 'back', 'top', 'bottom', 'left', 'right', 'isometric'] },
      },
      required: ['preset'],
    },
  },
  {
    name: 'cad_export',
    description: 'Export to a file: stl, obj, fbx, gltf, glb, ply.',
    inputSchema: {
      type: 'object',
      properties: {
        format: { type: 'string' },
        filepath: { type: 'string' },
        objects: { type: 'array', items: { type: 'string' }, description: 'Specific objects to export (all if omitted)' },
      },
      required: ['format'],
    },
  },
  {
    name: 'cad_import_file',
    description: 'Import a 3D file (stl, obj, fbx, gltf, glb, ply, blend). Counterpart to cad_export.',
    inputSchema: {
      type: 'object',
      properties: { filepath: { type: 'string' } },
      required: ['filepath'],
    },
  },

  // ━━ Poly Haven (free CC0 asset library) ━━
  {
    name: 'cad_polyhaven_search',
    description: 'Search Poly Haven for free CC0 HDRIs, textures, or 3D models. Returns asset IDs you can pass to cad_polyhaven_download.',
    inputSchema: {
      type: 'object',
      properties: {
        category: { type: 'string', enum: ['hdris', 'textures', 'models'] },
        query: { type: 'string' },
        limit: { type: 'number' },
      },
      required: ['category'],
    },
  },
  {
    name: 'cad_polyhaven_download',
    description: 'Download a Poly Haven asset to a temp directory. Pair with cad_set_world (HDRIs), cad_set_textured_material (textures), or cad_import_file (models).',
    inputSchema: {
      type: 'object',
      properties: {
        asset_id: { type: 'string' },
        category: { type: 'string', enum: ['hdris', 'textures', 'models'] },
        resolution: { type: 'string', description: '1k, 2k, 4k, 8k (default 2k)' },
        format: { type: 'string' },
        map: { type: 'string', description: 'For textures: Diffuse, Rough, nor_gl, Displacement, etc.' },
      },
      required: ['asset_id', 'category'],
    },
  },

  // ━━ Code Execution ━━
  {
    name: 'cad_execute_code',
    description: 'Execute arbitrary Python in Blender. Auto-saves a checkpoint first. Returns stdout, stderr, and a scene diff. Use for anything not covered by structured tools.',
    inputSchema: {
      type: 'object',
      properties: {
        code: { type: 'string' },
        auto_checkpoint: { type: 'boolean', description: 'Save checkpoint before exec (default true)' },
      },
      required: ['code'],
    },
  },
];

// ── Tool name → addon command mapping ──

const COMMAND_MAP = {
  cad_save_checkpoint: 'save_checkpoint',
  cad_restore_checkpoint: 'restore_checkpoint',
  cad_list_checkpoints: 'list_checkpoints',
  cad_delete_checkpoint: 'delete_checkpoint',
  cad_undo: 'undo',
  cad_redo: 'redo',
  cad_get_scene_summary: 'get_scene_summary',
  cad_get_object_details: 'get_object_details',
  cad_get_objects_by_type: 'get_objects_by_type',
  cad_get_selection: 'get_selection',
  cad_get_object_tree: 'get_object_tree',
  cad_scene_search: 'scene_search',
  cad_create_object: 'create_object',
  cad_delete_object: 'delete_object',
  cad_transform_object: 'transform_object',
  cad_duplicate_object: 'duplicate_object',
  cad_boolean: 'boolean_operation',
  cad_fillet: 'fillet',
  cad_chamfer: 'chamfer',
  cad_create_sketch: 'create_sketch',
  cad_set_material: 'set_material',
  cad_set_textured_material: 'set_textured_material',
  cad_add_modifier: 'add_modifier',
  cad_apply_modifier: 'apply_modifier',
  cad_mesh_edit: 'mesh_edit',
  cad_create_curve: 'create_curve',
  cad_create_text: 'create_text',
  cad_array_pattern: 'array_pattern',
  cad_add_light: 'add_light',
  cad_set_world: 'set_world',
  cad_set_camera: 'set_camera',
  cad_set_camera_settings: 'set_camera_settings',
  cad_set_keyframe: 'set_keyframe',
  cad_set_frame: 'set_frame',
  cad_set_visibility: 'set_visibility',
  cad_rename_object: 'rename_object',
  cad_set_parent: 'set_parent',
  cad_create_collection: 'create_collection',
  cad_move_to_collection: 'move_to_collection',
  cad_add_particle_system: 'add_particle_system',
  cad_add_physics: 'add_physics',
  cad_measure: 'measure',
  cad_render: 'render',
  cad_set_render_settings: 'set_render_settings',
  cad_get_viewport_screenshot: 'get_viewport_screenshot',
  cad_set_view: 'set_view',
  cad_export: 'export',
  cad_import_file: 'import_file',
  cad_polyhaven_search: 'polyhaven_search',
  cad_polyhaven_download: 'polyhaven_download',
  cad_execute_code: 'execute_code',
};

async function handleToolCall(name, args) {
  const commandType = COMMAND_MAP[name];
  if (!commandType) throw new Error(`Unknown tool: ${name}`);

  const result = await send(commandType, args || {});

  const content = [];

  // Surface viewport screenshots / explicit images as MCP image content
  if (result.viewport_screenshot) {
    content.push({ type: 'image', data: result.viewport_screenshot, mimeType: 'image/png' });
    delete result.viewport_screenshot;
  }
  if (result.image_base64) {
    content.push({ type: 'image', data: result.image_base64, mimeType: 'image/png' });
    delete result.image_base64;
  }

  content.push({ type: 'text', text: JSON.stringify(result, null, 2) });
  return content;
}

// ── Server Setup ──

const server = new Server(
  { name: 'cad-mcp-blender', version: '1.0.0' },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  try {
    const content = await handleToolCall(name, args || {});
    return { content };
  } catch (error) {
    return {
      content: [{ type: 'text', text: JSON.stringify({ error: error.message }, null, 2) }],
      isError: true,
    };
  }
});

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error('cad-mcp-blender running on stdio');
}

main().catch((err) => {
  console.error('Fatal:', err);
  process.exit(1);
});
