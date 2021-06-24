#+
# Importer for reading MagicaVoxel files into Blender.
# File format description is taken from
# <https://github.com/ephtracy/voxel-model/blob/master/MagicaVoxel-file-format-vox.txt>
#
# Copyright 2021 by Lawrence D'Oliveiro <ldo@geek-central.gen.nz>.
# Licensed under CC-BY-SA <http://creativecommons.org/licenses/by-sa/4.0/>.
#-

import sys
import os
import enum
from collections import \
    namedtuple
import struct
import bpy
from bpy.props import \
    CollectionProperty, \
    StringProperty
import bpy_extras

bl_info = \
    {
        "name" : "Magivox Import",
        "author" : "Lawrence D'Oliveiro <ldo@geek-central.gen.nz>",
        "version" : (0, 5, 5),
        "blender" : (2, 93, 0),
        "location" : "File > Import > MagicaVoxel",
        "description" :
            "imports files in MagicaVoxel format",
        "warning" : "",
        "wiki_url" : "",
        "tracker_url" : "",
        "category" : "Import-Export",
    }

#+
# Useful stuff
#-

def structread(fromfile, decode_struct) :
    "reads sufficient bytes from fromfile to be unpacked according to" \
    " decode_struct, and returns the unpacked results."
    return struct.unpack(decode_struct, fromfile.read(struct.calcsize(decode_struct)))
#end structread

class Failure(Exception) :

    def __init__(self, msg) :
        self.msg = msg
    #end __init__

#end Failure

class Equivalence :
    "for defining equivalence classes among a collection of objects."

    __slots__ = ("entries", "nr_distinct")

    def __init__(self) :
        self.entries = {}
        self.nr_distinct = 0
    #end __init__

    def add_equiv(self, a, b) :
        "adds an equivalence between a and b. This can cause merging" \
        " of sets, if there are already existing equivalences defined" \
        " for a or b."
        existing_a = self.entries.get(a)
        existing_b = self.entries.get(b)
        if existing_a != None :
            if existing_b != None :
                if existing_a == existing_b :
                    pass # already considered equivalent
                else :
                    # merge the two sets
                    for c in existing_b :
                        existing_a.add(c)
                        self.entries[c] = existing_a
                    #end for
                    self.nr_distinct -= 1
                #end if
            else :
                existing_a.add(b)
                self.entries[b] = existing_a
            #end if
        else :
            if existing_b != None :
                existing_b.add(a)
                self.entries[a] = existing_b
            else :
                # both are new
                self.entries[a] = self.entries[b] = {a, b}
                self.nr_distinct += 1
            #end if
        #end if
    #end add_equiv

    def get_equiv(self, a) :
        return self.existing[a]
    #end get_equiv

    def all_equivs(self) :
        "yields all the equivalence classes that have been defined."
        done = set()
        for entry in self.entries.values() :
            key = frozenset(entry)
            if key not in done :
                done.add(key)
                yield entry
            #end if
        #end for
    #end all_equivs

    def __len__(self) :
        return self.nr_distinct
    #end __len__

#end Equivalence

#+
# Format decoding
#-

class Chunk :

    __slots__ = ("id", "content", "children")

    def __init__(self, id, content, children) :
        self.id = id
        self.content = content
        self.children = children
    #end __init__

    @classmethod
    def decode_children(celf, block) :
        children = []
        while True :
            if len(block) == 0 :
                break
            if len(block) < 12 :
                raise Failure("child chunk header too short")
            #end if
            id, content_len, grandchildren_len = struct.unpack("<4sII", block[:12])
            block = block[12:]
            if content_len + grandchildren_len > len(block) :
                raise Failure("child content too short")
            #end if
            content = block[:content_len]
            block = block[content_len:]
            grandchildren_block = block[:grandchildren_len]
            block = block[grandchildren_len:]
            grandchildren = celf.decode_children(grandchildren_block)
            children.append(celf(id, content, grandchildren))
        #end while
        return \
            children
    #end decode_children

    @classmethod
    def load(celf, fromfile) :
        id, content_len, children_len = structread(fromfile, "<4sII")
        content = fromfile.read(content_len)
        children_block = fromfile.read(children_len)
        if len(content) < content_len or len(children_block) < children_len :
            raise Failure("input file too short")
        #end if
        return \
            celf(id, content, celf.decode_children(children_block))
    #end load

    def assert_no_content(self) :
        if len(self.content) != 0 :
            raise Failure("%s chunk has unexpected immediate content" % self.id.decode())
        #end if
    #end assert_no_content

    def assert_no_children(self) :
        if len(self.children) != 0 :
            raise Failure("%s chunk has unexpected children" % self.id.decode())
        #end if
    #end assert_no_children

#end Chunk

class VoxModel :

    __slots__ = ("models", "palette", "materials")

    Voxel = namedtuple("Voxel", ("x", "y", "z", "c"))
    Colour = namedtuple("Colour", ("r", "g", "b", "a"))

    class MATT_TYPE(enum.IntEnum) :
        DIFFUSE = 0
        METAL = 1
        GLASS = 2
        EMISSIVE = 3
    #end MATT_TYPE

    class MATT_PROPS(enum.IntEnum) :
        PLASTIC = 0
        ROUGHNESS = 1
        SPECULAR = 2
        IOR = 3
        ATTEN = 4
        POWER = 5
        GLOW = 6
        ISTOTALPOWER = 7

        @property
        def has_value(self) :
            return \
                self != type(self).ISTOTALPOWER
        #end has_value

    #end MATT_PROPS

    class Material :

        __slots__ = ("id", "type", "weight", "props")

        def __init__(self, id, type, weight, props) :
            self.id = id
            self.type = type
            self.weight = weight
            self.props = props
        #end __init__

        def __repr__(self) :
            return \
                (
                    "%s(%d, %d, %d, %s)"
                %
                    (type(self).__name__, self.id, self.type, self.weight, repr(self.props))
                )
        #end __repr__

    #end Material

    # default_palette set up below

    def __init__(self, main) :
        celf = type(self)
        if main.id != b"MAIN" :
            raise Failure("top-level chunk should have id MAIN")
        #end if
        main.assert_no_content()
        nr_models = None
        self.models = []
        self.palette = None
        self.materials = []
        last_size = None
        for chunk in main.children :
            if chunk.id == b"PACK" :
                if nr_models != None :
                    raise Failure("only allowed one PACK chunk")
                #end if
                chunk.assert_no_children()
                nr_models, = struct.unpack("<I", chunk.content)
            elif chunk.id == b"SIZE" :
                if last_size != None :
                    raise Failure("expecting XYZI following SIZE")
                #end if
                chunk.assert_no_children()
                if len(chunk.content) < 12 :
                    raise Failure("SIZE chunk too short")
                #end if
                last_size = struct.unpack("<III", chunk.content)
            elif chunk.id == b"XYZI" :
                if last_size == None :
                    raise Failure("missing SIZE preceding XYZI")
                #end if
                chunk.assert_no_children()
                if len(chunk.content) < 4 :
                    raise Failure("XYZI chunk initial too short")
                #end if
                nr_voxels, = struct.unpack("<I", chunk.content[:4])
                if len(chunk.content) < 4 + 4 * nr_voxels :
                    raise Failure("XYZI chunk rest too short")
                #end if
                voxels = struct.unpack("<" + "B" * nr_voxels * 4, chunk.content[4:])
                model = []
                for i in range(nr_voxels) :
                    voxel = self.Voxel \
                      (
                        x = voxels[i * 4],
                        y = voxels[i * 4 + 1],
                        z = voxels[i * 4 + 2],
                        c = voxels[i * 4 + 3]
                      )
                    if (
                            voxel.x >= last_size[0]
                        or
                            voxel.y >= last_size[1]
                        or
                            voxel.z >= last_size[2]
                        or
                            voxel.c == 0
                    ) :
                        raise Failure \
                          (
                                "voxel[%d] = (%d, %d, %d, %d) out of range of dimensions (%d, %d, %d, 1 .. 255)"
                            %
                                (
                                    i,
                                    voxel.x, voxel.y, voxel.z, voxel.c,
                                    last_size[0], last_size[1], last_size[2],
                                )
                          )
                    #end if
                    model.append(voxel)
                #end for
                self.models.append((last_size, model))
                last_size = None
            elif chunk.id == b"RGBA" :
                if self.palette != None :
                    raise Failure("only allowed one RGBA chunk")
                #end if
                chunk.assert_no_children()
                if len(chunk.content) < 1024 :
                    raise Failure("RGBA chunk too short")
                #end if
                rgba = struct.unpack("<1024B", chunk.content)
                self.palette = tuple \
                  (
                    self.Colour(r = rgba[i * 4], g = rgba[i * 4 + 1], b = rgba[i * 4 + 2], a = rgba[i * 4 + 3])
                    for i in range(256)
                  )
                # is entry 0 ignored?
            elif chunk.id == b"MATT" :
                chunk.assert_no_children()
                if len(chunk.content) < 16 :
                    raise Failure("MATT chunk initial too short")
                #end if
                matt_id, matt_type, matt_weight, prop_mask = struct.unpack("<IIfI", chunk.content[:16])
                props_present = set(celf.MATT_PROPS(i) for i in range(32) if 1 << i & prop_mask != 0)
                value_props = list(i for i in sorted(props_present) if i.has_value)
                if len(chunk.content) < 16 + len(value_props) * 4 :
                    raise Failure("MATT chunk rest too short")
                #end if
                propvalues = struct.unpack("<" + "f" * len(value_props), chunk.content[16:])
                props = dict(zip(value_props, propvalues))
                for i in props_present :
                    if not i.has_value :
                        props[i] = None
                    #end if
                #end for
                self.materials.append \
                  (
                    celf.Material
                      (
                        id = matt_id,
                        type = celf.MATT_TYPE(matt_type),
                        weight = matt_weight,
                        props = props
                      )
                  )
            #end if
        #end for
        if last_size != None :
            raise Failure("expecting XYZI following last SIZE")
        #end if
        if nr_models == None :
            nr_models = 1
        #end if
        if len(self.models) != nr_models :
            raise Failure("expected %d models in file, got %d" % (nr_models, len(self.models)))
        #end if
        if self.palette == None :
            self.palette = type(self).default_palette
        #end if
    #end __init__

    def __repr__(self) :
        return \
            (
                    "%s(models[%d] = %s, palette = %s, materials[%d] = %s)"
                %
                    (
                        type(self).__name__,
                        len(self.models), self.models,
                        self.palette,
                        len(self.materials), self.materials,
                    )
            )
    #end __repr__

VoxModel.default_palette = tuple \
  (
    VoxModel.Colour(r = x & 0xff, g = x >> 8 & 0xff, b = x >> 16 & 0xff, a = x >> 24 & 0xff)
    for x in
      (
        0x00000000, 0xffffffff, 0xffccffff, 0xff99ffff, 0xff66ffff, 0xff33ffff, 0xff00ffff, 0xffffccff,
        0xffccccff, 0xff99ccff, 0xff66ccff, 0xff33ccff, 0xff00ccff, 0xffff99ff, 0xffcc99ff, 0xff9999ff,
        0xff6699ff, 0xff3399ff, 0xff0099ff, 0xffff66ff, 0xffcc66ff, 0xff9966ff, 0xff6666ff, 0xff3366ff,
        0xff0066ff, 0xffff33ff, 0xffcc33ff, 0xff9933ff, 0xff6633ff, 0xff3333ff, 0xff0033ff, 0xffff00ff,
        0xffcc00ff, 0xff9900ff, 0xff6600ff, 0xff3300ff, 0xff0000ff, 0xffffffcc, 0xffccffcc, 0xff99ffcc,
        0xff66ffcc, 0xff33ffcc, 0xff00ffcc, 0xffffcccc, 0xffcccccc, 0xff99cccc, 0xff66cccc, 0xff33cccc,
        0xff00cccc, 0xffff99cc, 0xffcc99cc, 0xff9999cc, 0xff6699cc, 0xff3399cc, 0xff0099cc, 0xffff66cc,
        0xffcc66cc, 0xff9966cc, 0xff6666cc, 0xff3366cc, 0xff0066cc, 0xffff33cc, 0xffcc33cc, 0xff9933cc,
        0xff6633cc, 0xff3333cc, 0xff0033cc, 0xffff00cc, 0xffcc00cc, 0xff9900cc, 0xff6600cc, 0xff3300cc,
        0xff0000cc, 0xffffff99, 0xffccff99, 0xff99ff99, 0xff66ff99, 0xff33ff99, 0xff00ff99, 0xffffcc99,
        0xffcccc99, 0xff99cc99, 0xff66cc99, 0xff33cc99, 0xff00cc99, 0xffff9999, 0xffcc9999, 0xff999999,
        0xff669999, 0xff339999, 0xff009999, 0xffff6699, 0xffcc6699, 0xff996699, 0xff666699, 0xff336699,
        0xff006699, 0xffff3399, 0xffcc3399, 0xff993399, 0xff663399, 0xff333399, 0xff003399, 0xffff0099,
        0xffcc0099, 0xff990099, 0xff660099, 0xff330099, 0xff000099, 0xffffff66, 0xffccff66, 0xff99ff66,
        0xff66ff66, 0xff33ff66, 0xff00ff66, 0xffffcc66, 0xffcccc66, 0xff99cc66, 0xff66cc66, 0xff33cc66,
        0xff00cc66, 0xffff9966, 0xffcc9966, 0xff999966, 0xff669966, 0xff339966, 0xff009966, 0xffff6666,
        0xffcc6666, 0xff996666, 0xff666666, 0xff336666, 0xff006666, 0xffff3366, 0xffcc3366, 0xff993366,
        0xff663366, 0xff333366, 0xff003366, 0xffff0066, 0xffcc0066, 0xff990066, 0xff660066, 0xff330066,
        0xff000066, 0xffffff33, 0xffccff33, 0xff99ff33, 0xff66ff33, 0xff33ff33, 0xff00ff33, 0xffffcc33,
        0xffcccc33, 0xff99cc33, 0xff66cc33, 0xff33cc33, 0xff00cc33, 0xffff9933, 0xffcc9933, 0xff999933,
        0xff669933, 0xff339933, 0xff009933, 0xffff6633, 0xffcc6633, 0xff996633, 0xff666633, 0xff336633,
        0xff006633, 0xffff3333, 0xffcc3333, 0xff993333, 0xff663333, 0xff333333, 0xff003333, 0xffff0033,
        0xffcc0033, 0xff990033, 0xff660033, 0xff330033, 0xff000033, 0xffffff00, 0xffccff00, 0xff99ff00,
        0xff66ff00, 0xff33ff00, 0xff00ff00, 0xffffcc00, 0xffcccc00, 0xff99cc00, 0xff66cc00, 0xff33cc00,
        0xff00cc00, 0xffff9900, 0xffcc9900, 0xff999900, 0xff669900, 0xff339900, 0xff009900, 0xffff6600,
        0xffcc6600, 0xff996600, 0xff666600, 0xff336600, 0xff006600, 0xffff3300, 0xffcc3300, 0xff993300,
        0xff663300, 0xff333300, 0xff003300, 0xffff0000, 0xffcc0000, 0xff990000, 0xff660000, 0xff330000,
        0xff0000ee, 0xff0000dd, 0xff0000bb, 0xff0000aa, 0xff000088, 0xff000077, 0xff000055, 0xff000044,
        0xff000022, 0xff000011, 0xff00ee00, 0xff00dd00, 0xff00bb00, 0xff00aa00, 0xff008800, 0xff007700,
        0xff005500, 0xff004400, 0xff002200, 0xff001100, 0xffee0000, 0xffdd0000, 0xffbb0000, 0xffaa0000,
        0xff880000, 0xff770000, 0xff550000, 0xff440000, 0xff220000, 0xff110000, 0xffeeeeee, 0xffdddddd,
        0xffbbbbbb, 0xffaaaaaa, 0xff888888, 0xff777777, 0xff555555, 0xff444444, 0xff222222, 0xff111111,
      )
  )

#end VoxModel

#+
# Mainline
#-

class MagivoxImport(bpy.types.Operator, bpy_extras.io_utils.ImportHelper) :
    bl_idname = "import_mesh.magivox"
    bl_label = "Magivox Import"

    files : CollectionProperty \
      (
        name = "File Path",
        description = "Vox file to load",
        type = bpy.types.OperatorFileListElement
      )
    filename_ext = ".vox"
    filter_glob : StringProperty(default = "*.vox", options = {"HIDDEN"})

    def execute(self, context) :
        try :
            f = open(self.filepath, "rb")
            sig, ver = structread(f, "<4sI")
            if sig != b"VOX " or ver != 150 :
                raise Failure("incorrect file header")
            #end if
            main = Chunk.load(f)
            f.close()
            model = VoxModel(main)
            sys.stderr.write("got model %s\n" % repr(model)) # debug
            common_group_name = "vox material common"
            if common_group_name in bpy.data.node_groups :
                common_group = bpy.data.node_groups[common_group_name]
            else :
                common_group = bpy.data.node_groups.new(common_group_name, "ShaderNodeTree")
                group_inputs = common_group.nodes.new("NodeGroupInput")
                group_inputs.location = (-400, 0)
                common_group.inputs.new("NodeSocketColor", "Colour")
                common_group.inputs.new("NodeSocketFloatFactor", "Alpha")
                reroute = common_group.nodes.new("NodeReroute")
                reroute.location = (-200, -150)
                common_group.links.new(group_inputs.outputs[0], reroute.inputs[0])
                transparent_shader = common_group.nodes.new("ShaderNodeBsdfTransparent")
                transparent_shader.location = (-100, -0)
                common_group.links.new(reroute.outputs[0], transparent_shader.inputs[0])
                diffuse_shader = common_group.nodes.new("ShaderNodeBsdfDiffuse")
                diffuse_shader.location = (-100, -150)
                common_group.links.new(reroute.outputs[0], diffuse_shader.inputs[0])
                mix_shader = common_group.nodes.new("ShaderNodeMixShader")
                mix_shader.location = (150, -75)
                common_group.links.new(group_inputs.outputs[1], mix_shader.inputs[0])
                common_group.links.new(transparent_shader.outputs[0], mix_shader.inputs[1])
                common_group.links.new(diffuse_shader.outputs[0], mix_shader.inputs[2])
                # group will contain material output directly
                material_output = common_group.nodes.new("ShaderNodeOutputMaterial")
                material_output.location = (400, 0)
                common_group.links.new(mix_shader.outputs[0], material_output.inputs[0])
                for node in common_group.nodes :
                    node.select = False
                #end for
            #end if
            materials = {}
            for objindex, (obj_dims, obj) in enumerate(model.models) :
                obj_materials = []
                verts = []
                voxel_colours = {(v.x, v.y, v.z) : v.c for v in obj}
                voxel_shells = {} # key is (x, y, z), value is shell index
                vertindexes = {} # key is (x, y, z, shell), value is index in verts
                nr_shells = 0
                faces = []
                corner_lo = \
                    (
                        min(v[0] for v in voxel_colours),
                        min(v[1] for v in voxel_colours),
                        min(v[2] for v in voxel_colours),
                    )
                corner_hi = \
                    (
                        max(v[0] for v in voxel_colours) + 1,
                        max(v[1] for v in voxel_colours) + 1,
                        max(v[2] for v in voxel_colours) + 1,
                    )
                def all_vox_coords() :
                    # iterate over bounding box covering all voxel coordinates.
                    for x in range(corner_lo[0], corner_hi[0]) :
                        for y in range(corner_lo[1], corner_hi[1]) :
                            for z in range(corner_lo[2], corner_hi[2]) :
                                yield (x, y, z)
                            #end for
                        #end for
                    #end for
                #end all_vox_coords
                def all_neighbours(x, y, z) :
                    # iterates over the coordinates of all neighbouring voxels,
                    # also returning the axis and direction to that neighbour.
                    for axis in range(3) : # x, y, z
                        for positive in (False, True) : # direction along axis
                            neighbour_step = (-1, +1)[positive]
                            yield \
                                (
                                    (
                                        x + neighbour_step * int(axis == 0),
                                        y + neighbour_step * int(axis == 1),
                                        z + neighbour_step * int(axis == 2)
                                    ),
                                    # neighbouring voxel in specified direction
                                    # along specified axis
                                    axis,
                                    positive,
                                )
                        #end for
                    #end for
                #end all_neighbours
                # assign shells to contiguous regions with same material
                merge_shells = Equivalence()
                for x, y, z in all_vox_coords() :
                    my_colour = voxel_colours.get((x, y, z))
                    if my_colour != None :
                        my_shell = None
                        fix_neighbours = set()
                        for neighbour, axis, positive in all_neighbours(x, y, z) :
                            if voxel_colours.get(neighbour) == my_colour :
                                if neighbour in voxel_shells :
                                    neighbour_shell = voxel_shells[neighbour]
                                    if my_shell == None :
                                        my_shell = neighbour_shell
                                    elif my_shell != neighbour_shell :
                                        merge_shells.add_equiv(my_shell, neighbour_shell)
                                    #end if
                                else :
                                    fix_neighbours.add(neighbour)
                                #end if
                            #end if
                        #end for
                        if my_shell == None :
                            # new shell just for me (to start with)
                            my_shell = nr_shells
                            nr_shells += 1
                        #end if
                        voxel_shells[x, y, z] = my_shell
                        for neighbour in fix_neighbours :
                            voxel_shells[neighbour] = my_shell
                        #end for
                    #end if voxel exists
                #end for x, y, z
                sys.stderr.write \
                  (
                        "total shells = %d, merge[%d] = [%s]\n"
                    %
                        (
                            nr_shells,
                            len(merge_shells),
                            ", ".join("[%d]%s" % (len(e), repr(e)) for e in merge_shells.all_equivs())
                        )
                  ) # debug
                for merging in merge_shells.all_equivs() :
                    merge_into = merging.pop()
                    for vox in all_vox_coords() :
                        if voxel_shells.get(vox) in merging :
                            voxel_shells[vox] = merge_into
                        #end if
                    #end for
                #end for
                # output faces in some predictable order
                for x, y, z in all_vox_coords() :
                    my_shell = voxel_shells.get((x, y, z))
                    if my_shell != None : # voxel exists
                        vox_faces = []
                        # add faces only on outer sides
                        for neighbour, axis, positive in all_neighbours(x, y, z) :
                            if voxel_shells.get(neighbour) != my_shell :
                                # compute verts of voxel face in that
                                # direction along that axis
                                coords = [[x, y, z] for i in range(4)]
                                other_axes = \
                                    ( # axes in plane of face, ordered in specified direction
                                        (axis + 2 - int(positive)) % 3,
                                        (axis + 1 + int(positive)) % 3
                                    )
                                for i, step in enumerate((0, 1, 3, 2)) :
                                    # generate vertex points in correct order
                                    # to orient normal
                                    if step & 1 != 0 :
                                        coords[i][other_axes[0]] += 1
                                    #end if
                                    if step & 2 != 0 :
                                        coords[i][other_axes[1]] += 1
                                    #end if
                                #end for
                                if positive :
                                    # voxel coord is used for face
                                    # in negative direction along that
                                    # axis, add 1 for face in positive
                                    # direction
                                    for i in range(4) :
                                        coords[i][axis] += 1
                                    #end for
                                #end if
                                vox_faces.append(tuple(tuple(c) for c in coords))
                            #end if
                        #end for neighbour
                        if len(vox_faces) != 0 :
                            matindex = voxel_colours[x, y, z] - 1
                            # only define materials for referenced colours
                            # TODO: model.materials
                            if matindex not in materials :
                                mat_colour = model.palette[matindex + 1]
                                mat_name = "vox_%d_%02x%02x%02x%02x" % (matindex, mat_colour.r, mat_colour.g, mat_colour.b, mat_colour.a)
                                material_colour = (mat_colour.r / 255, mat_colour.g / 255, mat_colour.b / 255)
                                material_alpha = mat_colour.a / 255
                                material = bpy.data.materials.new(mat_name)
                                material.diffuse_color = material_colour + (material_alpha,)
                                material.shadow_method = "HASHED"
                                  # needed if any nonopaque materials present
                                if material_alpha < 1.0 :
                                    material.blend_method = "BLEND"
                                #end if
                                material.use_nodes = True
                                material_tree = material.node_tree
                                for node in list(material_tree.nodes) :
                                  # clear out default nodes
                                    material_tree.nodes.remove(node)
                                #end for
                                group_node = material_tree.nodes.new("ShaderNodeGroup")
                                group_node.node_tree = common_group
                                group_node.location = (0, 0)
                                in_colour = material_tree.nodes.new("ShaderNodeRGB")
                                in_colour.location = (-300, 100)
                                in_colour.outputs[0].default_value = material_colour + (1,)
                                in_alpha = material_tree.nodes.new("ShaderNodeValue")
                                in_alpha.location = (-300, -150)
                                in_alpha.outputs[0].default_value = material_alpha
                                material_tree.links.new \
                                  (
                                    in_colour.outputs[0],
                                    group_node.inputs[0]
                                  )
                                material_tree.links.new \
                                  (
                                    in_alpha.outputs[0],
                                    group_node.inputs[1]
                                  )
                                for node in material_tree.nodes :
                                    node.select = False
                                #end for
                                materials[matindex] = mat_name
                            #end if
                            if matindex not in obj_materials :
                                obj_materials.append(matindex)
                            #end if
                        #end if
                        for vox_face in vox_faces :
                            face = []
                            for vox_vert in vox_face :
                                vox_key = vox_vert + (my_shell,)
                                if vox_key not in vertindexes :
                                    vertindexes[vox_key] = len(verts)
                                    verts.append(vox_vert)
                                #end if
                                face.append(vertindexes[vox_key])
                            #end for
                            faces.append((face, matindex))
                        #end for
                    #end if voxel exists
                #end for x, y, z
                vox_name = "vox_%03d" % (objindex + 1)
                vox_mesh = bpy.data.meshes.new(vox_name)
                vox_mesh.from_pydata(verts, [], [f[0] for f in faces])
                vox_mesh.update()
                vox_obj = bpy.data.objects.new(vox_name, vox_mesh)
                context.scene.collection.objects.link(vox_obj)
                bpy.data.objects[vox_obj.name].select_set(True)
                vox_materials = []
                for matindex in obj_materials :
                    vox_materials.append(matindex)
                    vox_mesh.materials.append(bpy.data.materials[materials[matindex]])
                #end for
                for faceindex, (_, matindex) in enumerate(faces) :
                    vox_mesh.polygons[faceindex].material_index = vox_materials.index(matindex)
                #end for
                vox_mesh.update()
            #end for obj
            # all done
            status = {"FINISHED"}
        except Failure as why :
            sys.stderr.write("Failure: %s\n" % why.msg) # debug
            self.report({"ERROR"}, why.msg)
            status = {"CANCELLED"}
        #end try
        return \
            status
    #end execute

#end MagivoxImport

def add_invoke_item(self, context) :
    self.layout.operator(MagivoxImport.bl_idname, text = "MagicaVoxel")
#end add_invoke_item

_classes_ = \
    (
        MagivoxImport,
    )

def register() :
    for ċlass in _classes_ :
        bpy.utils.register_class(ċlass)
    #end for
    bpy.types.TOPBAR_MT_file_import.append(add_invoke_item)
#end register

def unregister() :
    bpy.types.TOPBAR_MT_file_import.remove(add_invoke_item)
    for ċlass in _classes_ :
        bpy.utils.unregister_class(ċlass)
    #end for
#end unregister

if __name__ == "__main__" :
    register()
#end if
