"""Photoreal casino dice for marketing images, rendered in Blender (Cycles), so every pip is exactly right.

    blender -b -P dice_scene.py -- OUT.png WIDTH HEIGHT SAMPLES [TOP_A TOP_B]

Each die is a standard Western die: opposite faces sum to 7 and 1-2-3 run counterclockwise around their common corner
(right-handed). Casino style: translucent red, sharp edges with a tiny bevel, pips drilled and filled with white
(visible through the body). Set: green felt with a fabric texture, warm out-of-focus casino lights behind, dice on the
right third of a wide frame so a headline fits on the left.
"""
import math, random, sys
import bpy
from mathutils import Euler, Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
OUT = argv[0] if argv else "/tmp/dice.png"
W, H = int(argv[1]) if len(argv) > 1 else 960, int(argv[2]) if len(argv) > 2 else 400
SAMPLES = int(argv[3]) if len(argv) > 3 else 32
TOPS = (int(argv[4]), int(argv[5])) if len(argv) > 5 else (6, 5)

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
random.seed(7)

# ---------- materials ----------
def mat(name):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    return m, m.node_tree.nodes, m.node_tree.links

def principled(m):
    return m.node_tree.nodes["Principled BSDF"]

die_m, n, l = mat("die")
b = principled(die_m)
b.inputs["Base Color"].default_value = (0.75, 0.015, 0.02, 1)
b.inputs["Transmission Weight"].default_value = 1.0
b.inputs["Roughness"].default_value = 0.04
b.inputs["IOR"].default_value = 1.49                       # cellulose acetate, what casino dice are made of
vol = n.new("ShaderNodeVolumeAbsorption")                   # depth makes the red richer where the die is thicker
vol.inputs["Color"].default_value = (0.9, 0.05, 0.05, 1)
vol.inputs["Density"].default_value = 0.22
l.new(vol.outputs[0], n["Material Output"].inputs["Volume"])

pip_m, n, l = mat("pip")
b = principled(pip_m)
b.inputs["Base Color"].default_value = (0.93, 0.93, 0.9, 1)
b.inputs["Roughness"].default_value = 0.35
b.inputs["Emission Color"].default_value = (1, 0.97, 0.94, 1)   # a faint glow: light scattering inside a translucent die
b.inputs["Emission Strength"].default_value = 0.35

felt_m, n, l = mat("felt")
b = principled(felt_m)
b.inputs["Base Color"].default_value = (0.0, 0.2, 0.055, 1)
b.inputs["Roughness"].default_value = 0.95
b.inputs["Sheen Weight"].default_value = 0.15
noise = n.new("ShaderNodeTexNoise"); noise.inputs["Scale"].default_value = 900; noise.inputs["Detail"].default_value = 8
bump = n.new("ShaderNodeBump"); bump.inputs["Strength"].default_value = 0.25
l.new(noise.outputs["Fac"], bump.inputs["Height"]); l.new(bump.outputs["Normal"], b.inputs["Normal"])

def emit(color, strength):
    m, n, l = mat(f"light{len(bpy.data.materials)}")
    n.remove(n["Principled BSDF"])
    e = n.new("ShaderNodeEmission"); e.inputs["Color"].default_value = (*color, 1); e.inputs["Strength"].default_value = strength
    l.new(e.outputs[0], n["Material Output"].inputs["Surface"])
    return m

# ---------- a die ----------
FACES = {1: Vector((0, 0, 1)), 6: Vector((0, 0, -1)), 2: Vector((0, -1, 0)), 5: Vector((0, 1, 0)), 3: Vector((1, 0, 0)), 4: Vector((-1, 0, 0))}
PIPS = {1: [(0, 0)], 2: [(-1, 1), (1, -1)], 3: [(-1, 1), (0, 0), (1, -1)], 4: [(-1, -1), (-1, 1), (1, -1), (1, 1)],
        5: [(-1, -1), (-1, 1), (0, 0), (1, -1), (1, 1)], 6: [(-1, -1), (-1, 0), (-1, 1), (1, -1), (1, 0), (1, 1)]}
OFF, R, DEPTH = 0.27, 0.085, 0.06

def face_axes(nrm):
    up = Vector((0, 0, 1)) if abs(nrm.z) < 0.9 else Vector((0, 1, 0))
    u = up.cross(nrm).normalized()
    v = nrm.cross(u).normalized()
    return u, v

def make_die(name):
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    cube = bpy.context.object; cube.name = name
    bev = cube.modifiers.new("bevel", "BEVEL"); bev.width = 0.018; bev.segments = 4
    cube.data.materials.append(die_m)
    for value, nrm in FACES.items():
        u, v = face_axes(nrm)
        for (a, c) in PIPS[value]:
            centre = nrm * (0.5 - DEPTH / 2 + 0.0008) + u * (a * OFF) + v * (c * OFF)
            bpy.ops.mesh.primitive_cylinder_add(vertices=40, radius=R, depth=DEPTH, location=centre)
            p = bpy.context.object
            p.rotation_euler = nrm.to_track_quat("Z", "Y").to_euler()
            p.data.materials.append(pip_m)
            p.parent = cube
    return cube

# rotation that puts face `top` up (then a turn about the vertical axis)
TO_TOP = {1: (0, 0, 0), 6: (math.pi, 0, 0), 2: (-math.pi / 2, 0, 0), 5: (math.pi / 2, 0, 0), 3: (0, -math.pi / 2, 0), 4: (0, math.pi / 2, 0)}

def place(die, top, x, y, turn):
    die.rotation_mode = "XYZ"
    e = Euler(TO_TOP[top], "XYZ").to_matrix().to_4x4()
    die.matrix_world = (Euler((0, 0, math.radians(turn)), "XYZ").to_matrix().to_4x4() @ e)
    die.location = (x, y, 0.5)

a, b2 = make_die("dieA"), make_die("dieB")
place(a, TOPS[0], 1.55, 0.2, 31)
place(b2, TOPS[1], 2.95, 1.05, -18)

# ---------- set ----------
bpy.ops.mesh.primitive_plane_add(size=60, location=(0, 10, 0))
bpy.context.object.data.materials.append(felt_m)
for i in range(60):                                   # casino lights far behind: bokeh
    x, y, z = random.uniform(-10, 14), random.uniform(9, 19), random.uniform(1.0, 2.9)
    bpy.ops.mesh.primitive_uv_sphere_add(radius=random.uniform(0.15, 0.35), location=(x, y, z))
    warm = random.choice([(1.0, 0.72, 0.38), (1.0, 0.82, 0.55), (1.0, 0.55, 0.3), (0.95, 0.3, 0.25), (1.0, 0.9, 0.7)])
    bpy.context.object.data.materials.append(emit(warm, random.uniform(12, 40)))
    bpy.context.object.visible_glossy = False         # seen behind the dice, never as glare on their faces

def area(name, loc, target, size, power, color=(1, 1, 1)):
    bpy.ops.object.light_add(type="AREA", location=loc)
    L = bpy.context.object; L.name = name
    L.data.size, L.data.energy, L.data.color = size, power, color
    L.rotation_euler = (Vector(target) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()

area("key", (-3.5, -4.5, 5.5), (2.2, 0.6, 0.5), 5.0, 520, (1.0, 0.95, 0.88))
area("rim", (2.6, 4.2, 0.55), (2.2, 0.6, 0.45), 1.6, 520, (1.0, 0.75, 0.5))
area("fill", (6, -4, 2.5), (2.2, 0.6, 0.5), 3.0, 90, (0.75, 0.85, 1.0))

world = bpy.data.worlds.new("dark"); scene.world = world; world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.004, 0.004, 0.006, 1)

# ---------- camera ----------
bpy.ops.object.camera_add(location=(-0.4, -10.5, 3.3))
cam = bpy.context.object; scene.camera = cam
cam.data.lens = 58
target = Vector((0.65, 0.9, 0.95))
cam.rotation_euler = (target - cam.location).to_track_quat("-Z", "Y").to_euler()
cam.data.dof.use_dof = True
cam.data.dof.focus_distance = (Vector((2.2, 0.6, 0.6)) - cam.location).length
cam.data.dof.aperture_fstop = 1.4

# ---------- render ----------
scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = SAMPLES
scene.cycles.use_denoising = False          # Debian's Blender has no OpenImageDenoise: more samples instead
scene.cycles.max_bounces = 16; scene.cycles.transmission_bounces = 16; scene.cycles.volume_bounces = 4
scene.cycles.caustics_reflective = False; scene.cycles.caustics_refractive = False
scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = W, H, 100
scene.view_settings.view_transform = "AgX"; scene.view_settings.look = "AgX - Punchy"
scene.render.filepath = OUT
scene.render.image_settings.file_format = "PNG"
bpy.ops.render.render(write_still=True)
print("rendered", OUT)
