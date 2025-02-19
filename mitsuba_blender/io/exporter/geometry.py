from src.utils.shell import call_command
from .materials import export_material
from .export_context import Files
from mathutils import Matrix
import os
import bpy
import numpy as np

def convert_mesh(export_ctx, b_mesh, matrix_world, name, mat_nr):
    '''
    This method creates a mitsuba mesh from a blender mesh and returns it.
    It constructs a dictionary containing the necessary info such as
    pointers to blender's data strucures and then loads the BlenderMesh
    plugin via load_dict.

    Params
    ------
    export_ctx:   The export context.
    b_mesh:       The blender mesh to export.
    matrix_world: The mesh's transform matrix.
    name:         The name to give to the mesh. It will not be saved, so this is mostly
                  for logging/debug purposes.
    mat_nr:       The material ID to export.
    '''
    from mitsuba import load_dict
    props = {'type': 'blender'}
    b_mesh.calc_normals()
    # Compute the triangle tesselation
    b_mesh.calc_loop_triangles()

    props['name'] = name
    loop_tri_count = len(b_mesh.loop_triangles)
    if loop_tri_count == 0:
        export_ctx.log(f"Mesh: {name} has no faces. Skipping.", 'WARN')
        return
    props['loop_tri_count'] = loop_tri_count

    if len(b_mesh.uv_layers) > 1:
        export_ctx.log(f"Mesh: '{name}' has multiple UV layers. Mitsuba only supports one. Exporting the one set active for render.", 'WARN')
    for uv_layer in b_mesh.uv_layers:
        if uv_layer.active_render: # If there is only 1 UV layer, it is always active
            props['uvs'] = uv_layer.data[0].as_pointer()
            break

    for color_layer in b_mesh.vertex_colors:
        props['vertex_%s' % color_layer.name] = color_layer.data[0].as_pointer()

    props['loop_tris'] = b_mesh.loop_triangles[0].as_pointer()
    props['loops'] = b_mesh.loops[0].as_pointer()
    props['polys'] = b_mesh.polygons[0].as_pointer()

    # # Convention difference between Mitsuba and Blender
    # init_rot = Matrix.Rotation(np.pi / 2, 3, 'X')
    # for bmesh_vertex in b_mesh.vertices:
    #     bmesh_vertex.co = bmesh_vertex.co @ init_rot
    props['verts'] = b_mesh.vertices[0].as_pointer()

    if bpy.app.version > (3, 0, 0):
        props['normals'] = b_mesh.vertex_normals[0].as_pointer()
    props['vert_count'] = len(b_mesh.vertices)
    # Apply coordinate change
    init_rot = Matrix.Rotation(np.pi / 2, 4, 'X')
    if matrix_world:
        props['to_world'] = export_ctx.transform_matrix(init_rot @ matrix_world)
    else:
        props['to_world'] = export_ctx.transform_matrix(init_rot)
    props['mat_nr'] = mat_nr
    # Return the mitsuba mesh
    return load_dict(props)


def export_object_ply(deg_instance, export_ctx, is_particle):
    """
    Convert a blender object to mitsuba and save it as Binary PLY
    """

    b_object = deg_instance.object
    # Remove spurious characters such as slashes
    name_clean = bpy.path.clean_name(b_object.name_full)
    object_id = f"mesh-{name_clean}"

    is_instance_emitter = b_object.parent is not None and b_object.parent.is_instancer
    is_instance = deg_instance.is_instance

    # Only write to file objects that have never been exported before
    if export_ctx.data_get(object_id) is None:
        if b_object.type == 'MESH':
            b_mesh = b_object.data
        else: # Metaballs, text, surfaces
            b_mesh = b_object.to_mesh()

        # Convert the mesh into one mitsuba mesh per different material
        mat_count = len(b_mesh.materials)
        converted_parts = []
        if is_instance or is_instance_emitter:
            transform = None
        else:
            transform = b_object.matrix_world

        if mat_count == 0: # No assigned material
            converted_parts.append((-1, convert_mesh(export_ctx,
                                                    b_mesh,
                                                    transform,
                                                    name_clean,
                                                    0)))
        for mat_nr in range(mat_count):
            if b_mesh.materials[mat_nr]:
                mts_mesh = convert_mesh(export_ctx,
                                        b_mesh,
                                        transform,
                                        f"{name_clean}-{b_mesh.materials[mat_nr].name}",
                                        mat_nr)
                if mts_mesh is not None and mts_mesh.face_count() > 0:
                    converted_parts.append((mat_nr, mts_mesh))
                    export_material(export_ctx, b_mesh.materials[mat_nr])

        if b_object.type != 'MESH':
            b_object.to_mesh_clear()

        part_count = len(converted_parts)
        # Use a ShapeGroup for instances and split meshes
        use_shapegroup = is_instance or is_instance_emitter or is_particle
        # TODO: Check if shapegroups for split meshes is worth it
        if use_shapegroup:
            group = {
                'type': 'shapegroup'
            }

        for (mat_nr, mts_mesh) in converted_parts:
            # Determine the file name
            if part_count == 1:
                name = f"{name_clean}"
            else:
                name = f"{name_clean}-{b_mesh.materials[mat_nr].name}"
            mesh_id = f"mesh-{name}"

            # Save as binary ply
            mesh_folder = os.path.join(export_ctx.directory, export_ctx.subfolders['shape'])
            if not os.path.isdir(mesh_folder):
                os.makedirs(mesh_folder)
            filepath = os.path.join(mesh_folder,  f"{name}.ply")
            mts_mesh.write_ply(filepath)

            # Build dictionary entry
            params = {
                'type': 'ply',
                'filename': f"{export_ctx.subfolders['shape']}/{name}.ply"
            }

            # Add flat shading flag if needed
            # if not mts_mesh.has_vertex_normals():
            params["face_normals"] = True

            # Add material info
            if mat_nr == -1:
                if not export_ctx.data_get('default-bsdf'): # We only need to add it once
                    default_bsdf = {
                        'type': 'twosided',
                        'id': 'default-bsdf',
                        'bsdf': {'type':'diffuse'}
                    }
                    export_ctx.data_add(default_bsdf)
                params['bsdf'] = {'type':'ref', 'id':'default-bsdf'}
            else:
                mat_id = f"mat-{b_object.data.materials[mat_nr].name}"
                if export_ctx.exported_mats.has_mat(mat_id): # Add one emitter *and* one bsdf
                    mixed_mat = export_ctx.exported_mats.mats[mat_id]
                    params['bsdf'] = {'type':'ref', 'id':mixed_mat['bsdf']}
                    params['emitter'] = mixed_mat['emitter']
                else:
                    params['bsdf'] = {'type':'ref', 'id':mat_id}

            # Add dict to the scene dict
            if use_shapegroup:
                group[name] = params
            else:
                if export_ctx.export_ids:
                    export_ctx.data_add(params, name=mesh_id)
                else:
                    export_ctx.data_add(params)

        if use_shapegroup:
            export_ctx.data_add(group, name=object_id)

    if is_instance or is_particle:
        params = {
            'type': 'instance',
            'shape': {
                'type': 'ref',
                'id': object_id
            },
            'to_world': export_ctx.transform_matrix(deg_instance.matrix_world)
        }
        export_ctx.data_add(params)


def export_object_obj(deg_instance, export_ctx, is_particle):
    b_object = deg_instance.object
    # Remove spurious characters such as slashes
    name_clean = bpy.path.clean_name(b_object.name_full)
    object_id = f"mesh-{name_clean}"

    is_instance_emitter = b_object.parent is not None and b_object.parent.is_instancer
    is_instance = deg_instance.is_instance

    # Only write to file objects that have never been exported before
    if export_ctx.data_get(object_id) is None:
        if b_object.type == 'MESH':
            b_mesh = b_object.data
        else: # Metaballs, text, surfaces
            b_mesh = b_object.to_mesh()

        # Convert the mesh into one mitsuba mesh per different material
        mat_count = len(b_mesh.materials)
        converted_parts = []
        if is_instance or is_instance_emitter:
            transform = None
        else:
            transform = b_object.matrix_world

        if mat_count == 0: # No assigned material
            converted_parts.append((-1, (b_object, b_mesh)))
        for mat_nr in range(mat_count):
            if b_mesh.materials[mat_nr]:
                converted_parts.append((mat_nr, (b_object, b_mesh)))
                export_material(export_ctx, b_mesh.materials[mat_nr])

        if b_object.type != 'MESH':
            b_object.to_mesh_clear()

        part_count = len(converted_parts)
        # Use a ShapeGroup for instances and split meshes
        use_shapegroup = is_instance or is_instance_emitter or is_particle
        # TODO: Check if shapegroups for split meshes is worth it
        if use_shapegroup:
            group = {
                'type': 'shapegroup'
            }

        # view_layer = bpy.context.view_layer
        # desired_view_layer = None
        # for scene in bpy.data.scenes:
        #     print('scene : %s' % scene.name)
        #     for scene_view_layer in scene.view_layers:
        #         print('___ %s' % scene_view_layer.name)
        #         desired_view_layer = scene_view_layer
        # print(f'Active: {view_layer.name}')
        # print(f'Desired: {desired_view_layer.name}')
        # bpy.context.window.view_layer = desired_view_layer

        scene = bpy.context.scene
        view_layer = bpy.context.view_layer
        print(f'Active: {view_layer.name}')
        my_obj = None
        for obj in scene.objects:
            if obj.name == b_object.name:
                my_obj = obj
                break


        bpy.ops.object.select_all(action='DESELECT')
        for (mat_nr, (b_object, b_mesh)) in converted_parts:
            # Determine the file name
            if part_count == 1:
                name = f"{name_clean}"
            else:
                name = f"{name_clean}-{b_mesh.materials[mat_nr].name}"
            mesh_id = f"mesh-{name}"

            # Save as obj
            mesh_folder = os.path.join(export_ctx.directory, export_ctx.subfolders['shape'])
            print(f"Saving mesh {name} to {mesh_folder}")
            os.makedirs(mesh_folder, exist_ok=True)
            filepath = os.path.join(mesh_folder,  f"{name}.obj")
            view_layer.objects.active = my_obj
            my_obj.select_set(True)
            bpy.ops.export_scene.obj(
                filepath=filepath,
                use_selection=True,
                path_mode="ABSOLUTE",
                axis_forward="-Z",
                axis_up="Y",
                use_materials=False
            )
            my_obj.select_set(False)

            # Build dictionary entry
            params = {
                'type': 'obj',
                'filename': f"{export_ctx.subfolders['shape']}/{name}.obj"
            }

            # Add flat shading flag if needed
            # if not mts_mesh.has_vertex_normals():
            params["face_normals"] = True

            # Add material info
            if mat_nr == -1:
                if not export_ctx.data_get('default-bsdf'): # We only need to add it once
                    default_bsdf = {
                        'type': 'twosided',
                        'id': 'default-bsdf',
                        'bsdf': {'type':'diffuse'}
                    }
                    export_ctx.data_add(default_bsdf)
                params['bsdf'] = {'type':'ref', 'id':'default-bsdf'}
            else:
                mat_id = f"mat-{b_object.data.materials[mat_nr].name}"
                if export_ctx.exported_mats.has_mat(mat_id): # Add one emitter *and* one bsdf
                    mixed_mat = export_ctx.exported_mats.mats[mat_id]
                    params['bsdf'] = {'type':'ref', 'id':mixed_mat['bsdf']}
                    params['emitter'] = mixed_mat['emitter']
                else:
                    params['bsdf'] = {'type':'ref', 'id':mat_id}

            # Add dict to the scene dict
            if use_shapegroup:
                group[name] = params
            else:
                if export_ctx.export_ids:
                    export_ctx.data_add(params, name=mesh_id)
                else:
                    export_ctx.data_add(params)

        if use_shapegroup:
            export_ctx.data_add(group, name=object_id)

    if is_instance or is_particle:
        params = {
            'type': 'instance',
            'shape': {
                'type': 'ref',
                'id': object_id
            },
            'to_world': export_ctx.transform_matrix(deg_instance.matrix_world)
        }
        export_ctx.data_add(params)


def export_object_serialized(deg_instance, export_ctx, is_particle):
    b_object = deg_instance.object
    # Remove spurious characters such as slashes
    name_clean = bpy.path.clean_name(b_object.name_full)
    object_id = f"mesh-{name_clean}"

    is_instance_emitter = b_object.parent is not None and b_object.parent.is_instancer
    is_instance = deg_instance.is_instance

    # Only write to file objects that have never been exported before
    if export_ctx.data_get(object_id) is None:
        if b_object.type == 'MESH':
            b_mesh = b_object.data
        else: # Metaballs, text, surfaces
            b_mesh = b_object.to_mesh()

        # Convert the mesh into one mitsuba mesh per different material
        mat_count = len(b_mesh.materials)
        converted_parts = []
        if is_instance or is_instance_emitter:
            transform = None
        else:
            transform = b_object.matrix_world

        if mat_count == 0: # No assigned material
            converted_parts.append((-1, (b_object, b_mesh)))
        for mat_nr in range(mat_count):
            if b_mesh.materials[mat_nr]:
                converted_parts.append((mat_nr, (b_object, b_mesh)))
                export_material(export_ctx, b_mesh.materials[mat_nr])

        if b_object.type != 'MESH':
            b_object.to_mesh_clear()

        part_count = len(converted_parts)
        # Use a ShapeGroup for instances and split meshes
        use_shapegroup = is_instance or is_instance_emitter or is_particle
        # TODO: Check if shapegroups for split meshes is worth it
        if use_shapegroup:
            group = {
                'type': 'shapegroup'
            }

        # view_layer = bpy.context.view_layer
        # desired_view_layer = None
        # for scene in bpy.data.scenes:
        #     print('scene : %s' % scene.name)
        #     for scene_view_layer in scene.view_layers:
        #         print('___ %s' % scene_view_layer.name)
        #         desired_view_layer = scene_view_layer
        # print(f'Active: {view_layer.name}')
        # print(f'Desired: {desired_view_layer.name}')
        # bpy.context.window.view_layer = desired_view_layer

        scene = bpy.context.scene
        view_layer = bpy.context.view_layer
        print(f'Active: {view_layer.name}')
        my_obj = None
        for obj in scene.objects:
            if obj.name == b_object.name:
                my_obj = obj
                break

        bpy.ops.object.select_all(action='DESELECT')
        for (mat_nr, (b_object, b_mesh)) in converted_parts:
            # Determine the file name
            if part_count == 1:
                name = f"{name_clean}"
            else:
                name = f"{name_clean}-{b_mesh.materials[mat_nr].name}"
            mesh_id = f"mesh-{name}"

            # Save as obj
            mesh_folder = os.path.join(export_ctx.directory, export_ctx.subfolders['shape'])
            print(f"Saving mesh {name} to {mesh_folder}")
            os.makedirs(mesh_folder, exist_ok=True)
            obj_path = os.path.join(mesh_folder,  f"{name}.obj")
            view_layer.objects.active = my_obj
            my_obj.select_set(True)
            bpy.ops.export_scene.obj(
                filepath=obj_path,
                use_selection=True,
                path_mode="ABSOLUTE",
                axis_forward="-Z",
                axis_up="Y",
                use_materials=False
            )
            my_obj.select_set(False)

            # Convert to sserialized

            xml_path = os.path.join(mesh_folder,  f"{name}.xml")
            convert_command = f'source external/mitsuba/setpath.sh && ' \
                              f'LD_LIBRARY_PATH="{os.path.expanduser("~")}/usr/lib:$LD_LIBRARY_PATH" ' \
                              f'mtsimport {obj_path} {xml_path}'
            call_command(convert_command, export_ctx.logger, executable="/bin/bash")

            # Clean up
            os.remove(obj_path)
            os.remove(xml_path)

            # Build dictionary entry
            params = {
                'type': 'serialized',
                'filename': f"{export_ctx.subfolders['shape']}/{name}.serialized"
            }

            # Add flat shading flag if needed
            # if not mts_mesh.has_vertex_normals():
            params["face_normals"] = True

            # Add material info
            if mat_nr == -1:
                if not export_ctx.data_get('default-bsdf'): # We only need to add it once
                    default_bsdf = {
                        'type': 'twosided',
                        'id': 'default-bsdf',
                        'bsdf': {'type':'diffuse'}
                    }
                    export_ctx.data_add(default_bsdf)
                params['bsdf'] = {'type':'ref', 'id':'default-bsdf'}
            else:
                mat_id = f"mat-{b_object.data.materials[mat_nr].name}"
                if export_ctx.exported_mats.has_mat(mat_id): # Add one emitter *and* one bsdf
                    mixed_mat = export_ctx.exported_mats.mats[mat_id]
                    params['bsdf'] = {'type':'ref', 'id':mixed_mat['bsdf']}
                    params['emitter'] = mixed_mat['emitter']
                else:
                    params['bsdf'] = {'type':'ref', 'id':mat_id}

            # Add dict to the scene dict
            if use_shapegroup:
                group[name] = params
            else:
                if export_ctx.export_ids:
                    export_ctx.data_add(params, name=mesh_id)
                else:
                    export_ctx.data_add(params)

        if use_shapegroup:
            export_ctx.data_add(group, name=object_id)

    if is_instance or is_particle:
        params = {
            'type': 'instance',
            'shape': {
                'type': 'ref',
                'id': object_id
            },
            'to_world': export_ctx.transform_matrix(deg_instance.matrix_world)
        }
        export_ctx.data_add(params)


def export_object(deg_instance, export_ctx, is_particle, use_ply=True):
    if use_ply:
        export_object_ply(deg_instance, export_ctx, is_particle)
    else:
        export_object_obj(deg_instance, export_ctx, is_particle)
        # export_object_serialized(deg_instance, export_ctx, is_particle)