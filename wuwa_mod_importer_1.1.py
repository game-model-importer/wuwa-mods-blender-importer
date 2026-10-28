####  Set MOD_FOLDER below, then run this script in Blender.
####  Created by jinshi_lover | # Version 1.1
####  PATREON: https://www.patreon.com/jinshi_lover
####  GITHUB: https://github.com/game-model-importer/wuwa-mods-blender-importer
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
import json
import hashlib
import itertools
import logging
from pathlib import Path
import re
import colorsys
import math
import os
import operator
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np


MOD_FOLDER = Path(r"C:\path\to\file")
SCALE = 0.01
TOGGLE_VALUES = {}  # Leave empty to use the mod defaults.
IMPORT_DISABLED_PARTS = True
IMPORT_WEIGHTS = True
IMPORT_SHAPE_KEYS = True
FLIP_WINDING = True
FLIP_UV_V = True
STRICT_TEXTURES = False
DECAL_INK_COLOR = (0.008, 0.008, 0.008, 1.0)  # Optional mark color.


def worker_count():

    available = getattr(os, 'process_cpu_count', os.cpu_count)() or 1
    if hasattr(os, 'sched_getaffinity'):
        available = min(available, len(os.sched_getaffinity(0)))
    elif sys.platform == 'win32':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.GetProcessAffinityMask.argtypes = [ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
        process_mask, system_mask = ctypes.c_size_t(), ctypes.c_size_t()
        if kernel.GetProcessAffinityMask(kernel.GetCurrentProcess(),
                                        ctypes.byref(process_mask), ctypes.byref(system_mask)):
            available = min(available, process_mask.value.bit_count())
    return max(1, available // 2)


def file_sha256(file):


    if sys.platform == 'win32':
        import ctypes
        api = ctypes.WinDLL('bcrypt')
        ptr, ulong, handle = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p
        signatures = {
            'BCryptOpenAlgorithmProvider': [ctypes.POINTER(handle), ctypes.c_wchar_p, ctypes.c_wchar_p, ulong],
            'BCryptCreateHash': [handle, ctypes.POINTER(handle), ptr, ulong, ptr, ulong, ulong],
            'BCryptHashData': [handle, ptr, ulong, ulong],
            'BCryptFinishHash': [handle, ptr, ulong, ulong],
            'BCryptDestroyHash': [handle],
            'BCryptCloseAlgorithmProvider': [handle, ulong],
        }
        for name, args in signatures.items():
            function = getattr(api, name)
            function.argtypes, function.restype = args, ctypes.c_long
        algorithm, digest = handle(), handle()
        start = file.tell()
        try:
            def check(status):
                if status < 0:
                    raise OSError('Windows SHA-256 failed: ' + hex(status & 0xffffffff))
            check(api.BCryptOpenAlgorithmProvider(ctypes.byref(algorithm), 'SHA256', None, 0))
            check(api.BCryptCreateHash(algorithm, ctypes.byref(digest), None, 0, None, 0, 0))
            buffer = ctypes.create_string_buffer(1024 * 1024)
            while count := file.readinto(buffer):
                check(api.BCryptHashData(digest, buffer, count, 0))
            output = ctypes.create_string_buffer(32)
            check(api.BCryptFinishHash(digest, output, 32, 0))
            return output.raw.hex()
        except OSError:
            file.seek(start)
        finally:
            if digest:
                api.BCryptDestroyHash(digest)
            if algorithm:
                api.BCryptCloseAlgorithmProvider(algorithm, 0)
    digest = hashlib.sha256()
    buffer = bytearray(1024 * 1024)
    view = memoryview(buffer)
    while count := file.readinto(buffer):
        digest.update(view[:count])
    return digest.hexdigest()


RABBITFX_NOTE = 'Toggle the RabbitFX Effects collection to enable or disable glow.'

RABBITFX_DEFAULTS = dict(h=0., s=0., v=0., brightness=1., interpolate=1.)


def numeric_value(expression, variables, parameters):

    values = {k.lower(): v for k, v in variables.items()}
    values.update({'\\rabbitfx\\' + k: v for k, v in parameters.items()})
    def replace(match):
        key = match[1].lower()
        if key not in values:
            raise ValueError('Unknown RabbitFX expression variable: $' + key)
        return repr(values[key])
    expression = re.sub(r'\$([\\\w]+)', replace, expression)
    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            return -visit(node.operand) if isinstance(node.op, ast.USub) else visit(node.operand)
        if isinstance(node, ast.BinOp):
            a, b = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add): return a + b
            if isinstance(node.op, ast.Sub): return a - b
            if isinstance(node.op, ast.Mult): return a * b
            if isinstance(node.op, ast.Div): return a / b
        raise ValueError('Unsupported RabbitFX numeric expression: ' + expression)
    value = float(visit(ast.parse(expression, mode='eval').body))
    if not math.isfinite(value):
        raise ValueError('Non-finite RabbitFX setting')
    return value


def capture_rabbitfx(state, command, line):

    if command == 'commandlist\\rabbitfx\\clear':
        state['fx_parameters'] = dict(RABBITFX_DEFAULTS)
        state['rabbitfx'] = {}
        return True
    if command != 'commandlist\\rabbitfx\\run':
        return False
    aliases = state['aliases']
    glow = aliases.get('resource\\rabbitfx\\glowmap')
    mask = aliases.get('resource\\rabbitfx\\fxmap')
    mode = 'v8.3.2 named resources'

    if glow is None and 17 in state['slots']:
        glow = state['slots'][17]
        mode = 'legacy ps-t17 adapted to v8.3.2 glow input'
    glow = None if glow == 'null' else glow
    mask = None if mask == 'null' else mask
    state['rabbitfx'] = dict(state.get('fx_parameters', RABBITFX_DEFAULTS), glow_resource=glow,
                            fx_resource=mask, interface=mode, call_line=line)
    state['fx_parameters'] = dict(state.get('fx_parameters', RABBITFX_DEFAULTS), h=0., s=0., v=0., brightness=0.)
    aliases['resource\\rabbitfx\\glowmap'] = 'null'
    aliases['resource\\rabbitfx\\fxmap'] = 'null'
    return True


def adjusted_color(rgb, settings):

    h, s, v = colorsys.rgb_to_hsv(*rgb)
    i = settings.get('interpolate', 1.)
    h = (h + settings.get('h', 0.) / 360. * i) % 1.
    s = max(0., s + settings.get('s', 0.) / 100. * i)
    v = max(0., v + settings.get('v', 0.) / 100. * i)

    return tuple(v * (1 + s * (min(1., max(0., abs(((h + offset) % 1.) * 6 - 3) - 1)) - 1))
                 for offset in (0., 2/3, 1/3))


RABBITFX_RUNTIME_SOURCE = r'''
import bpy
from bpy.app.handlers import persistent
_rabbitfx_busy = False
_rabbitfx_rendering = False

def _excluded(layer, target, inherited=False):
    excluded = inherited or layer.exclude or (layer.hide_viewport and not _rabbitfx_rendering)
    if layer.collection == target:
        return excluded
    for child in layer.children:
        result = _excluded(child, target, excluded)
        if result is not None:
            return result
    return None

@persistent
def rabbitfx_sync(scene=None, depsgraph=None):
    global _rabbitfx_busy
    if _rabbitfx_busy:
        return
    _rabbitfx_busy = True
    try:
        scene = scene if isinstance(scene, bpy.types.Scene) else bpy.context.scene
        layer = bpy.context.view_layer
        if layer.name not in scene.view_layers:
            layer = scene.view_layers[0]
        for collection in bpy.data.collections:
            if not collection.get('rabbitfx_controller'):
                continue
            excluded = _excluded(layer.layer_collection, collection)
            hidden = collection.hide_render if _rabbitfx_rendering else collection.hide_viewport
            enabled = float(excluded is False and not hidden)
            if collection.get('rabbitfx_enabled') != enabled:
                collection['rabbitfx_enabled'] = enabled
                collection.update_tag()
    finally:
        _rabbitfx_busy = False

@persistent
def rabbitfx_render_pre(scene, *args):
    global _rabbitfx_rendering
    _rabbitfx_rendering = True
    rabbitfx_sync(scene)

@persistent
def rabbitfx_render_end(scene, *args):
    global _rabbitfx_rendering
    _rabbitfx_rendering = False
    rabbitfx_sync(scene)

def register():
    for handlers, callback in (
        (bpy.app.handlers.depsgraph_update_post, rabbitfx_sync),
        (bpy.app.handlers.load_post, rabbitfx_sync),
        (bpy.app.handlers.render_pre, rabbitfx_render_pre),
        (bpy.app.handlers.render_post, rabbitfx_render_end),
        (bpy.app.handlers.render_cancel, rabbitfx_render_end),
    ):
        for old in list(handlers):
            if getattr(old, '_wwmi_rabbitfx', False):
                handlers.remove(old)
        callback._wwmi_rabbitfx = True
        handlers.append(callback)
    rabbitfx_sync()

register()
'''


def _rabbitfx_controller(importer):
    if hasattr(importer, 'rabbitfx_collection'):
        return importer.rabbitfx_collection
    bpy = importer.bpy
    collection = importer.track('collections', bpy.data.collections.new('RabbitFX Effects'))
    getattr(importer, 'import_collection', bpy.context.scene.collection).children.link(collection)
    collection['rabbitfx_controller'] = True
    collection['rabbitfx_enabled'] = 1.
    collection['rabbitfx_note'] = 'Exclude this collection to disable effects; model meshes stay in their own collection.'
    importer.rabbitfx_collection = collection
    runtime = importer.track('texts', bpy.data.texts.new('RabbitFX Effects Controls.py'))
    runtime.write(RABBITFX_RUNTIME_SOURCE)

    runtime.use_module = True
    collection['rabbitfx_controls_script'] = runtime.name
    importer.rabbitfx_runtime = {'__name__': 'wwmi_rabbitfx_controls'}
    exec(compile(RABBITFX_RUNTIME_SOURCE, runtime.name, 'exec'), importer.rabbitfx_runtime)
    return collection


def rabbit_fx_effects(importer, material, draw):

    settings = draw.rabbitfx
    if not settings:
        return None
    if material.get('rabbitfx_applied'):
        return next(n for n in material.node_tree.nodes if n.get('rabbitfx_group'))
    glow, mask = settings.get('glow_resource'), settings.get('fx_resource')
    material['rabbitfx_settings'] = json.dumps(settings, sort_keys=True)
    if not glow and not mask:
        material['rabbitfx_note'] = 'Run detected, but no glow/FX texture bound; base material preserved.'
        return None
    collection = _rabbitfx_controller(importer)
    bpy = importer.bpy
    group = importer.track('node_groups', bpy.data.node_groups.new('RabbitFX · ' + draw.name, 'ShaderNodeTree'))
    def interface(name, socket_type, default=None, output=False):
        if bpy.app.version >= (4, 0, 0):
            sock = group.interface.new_socket(name=name, in_out='OUTPUT' if output else 'INPUT', socket_type=socket_type)
        else:
            sock = (group.outputs if output else group.inputs).new(socket_type, name)
        if default is not None:
            sock.default_value = default
        return sock
    interface('Base Surface', 'NodeSocketShader')
    interface('Glow Color', 'NodeSocketColor', (0., 0., 0., 1.))
    interface('Glow Alpha', 'NodeSocketFloat', 0.)
    interface('FX Alpha', 'NodeSocketFloat', 1.)
    interface('Normal', 'NodeSocketVector', (0., 0., 0.))
    interface('Roughness', 'NodeSocketFloat', .6)
    for name, value in [('Enabled', 1.), ('Hue', settings['h']), ('Saturation', settings['s']),
                        ('Value', settings['v']), ('Brightness', settings['brightness']),
                        ('Interpolate', settings['interpolate'])]:
        interface(name, 'NodeSocketFloat', value)
    interface('Surface', 'NodeSocketShader', output=True)

    interface('Adjusted Color', 'NodeSocketColor', output=True)
    interface('Glow Strength', 'NodeSocketFloat', output=True)
    nodes, links = group.nodes, group.links
    source = nodes.new('NodeGroupInput')
    target = nodes.new('NodeGroupOutput')
    source.location = (-1100, 200)
    target.location = (1000, 200)
    def connect(value, socket):
        if hasattr(value, 'is_output'):
            links.new(value, socket)
        else:
            socket.default_value = value
    def math_node(operation, a, b=None, label=''):
        n = nodes.new('ShaderNodeMath')
        n.operation = operation
        n.label = label
        connect(a, n.inputs[0])
        if b is not None: connect(b, n.inputs[1])
        return n.outputs[0]
    split = nodes.new('ShaderNodeSeparateColor')
    split.mode = 'HSV'
    links.new(source.outputs['Glow Color'], split.inputs['Color'])
    def shift(channel, parameter, divisor):
        delta = math_node('MULTIPLY', math_node('DIVIDE', source.outputs[parameter], divisor), source.outputs['Interpolate'])
        return math_node('ADD', split.outputs[channel], delta)
    h = math_node('FRACT', shift(0, 'Hue', 360.), label='Wrap hue')
    s = math_node('MAXIMUM', shift(1, 'Saturation', 100.), 0., 'Add saturation; no upper clamp')
    v = math_node('MAXIMUM', shift(2, 'Value', 100.), 0., 'Add value; no upper clamp')
    combine = nodes.new('ShaderNodeCombineXYZ')
    for channel, offset in enumerate((0., 2/3, 1/3)):
        wave = math_node('FRACT', math_node('ADD', h, offset))
        wave = math_node('ABSOLUTE', math_node('SUBTRACT', math_node('MULTIPLY', wave, 6.), 3.))
        wave = math_node('MINIMUM', math_node('MAXIMUM', math_node('SUBTRACT', wave, 1.), 0.), 1.)
        result = math_node('MULTIPLY', v, math_node('ADD', 1., math_node('MULTIPLY', s, math_node('SUBTRACT', wave, 1.))))
        links.new(result, combine.inputs[channel])
    strength = math_node('MULTIPLY', source.outputs['Glow Alpha'], source.outputs['Brightness'], 'Alpha × brightness')
    affected = math_node('MULTIPLY', math_node('GREATER_THAN', source.outputs['Glow Alpha'], 0.), source.outputs['Enabled'])
    surface = nodes.new('ShaderNodeBsdfPrincipled')
    surface.location = (350, 200)
    links.new(combine.outputs[0], surface.inputs['Base Color'])
    links.new(combine.outputs[0], surface.inputs.get('Emission Color') or surface.inputs['Emission'])
    links.new(strength, surface.inputs['Emission Strength'])
    links.new(source.outputs['Roughness'], surface.inputs['Roughness'])

    original = next(n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    if original.inputs['Normal'].is_linked:
        links.new(source.outputs['Normal'], surface.inputs['Normal'])
    mix = nodes.new('ShaderNodeMixShader')
    mix.location = (600, 200)
    links.new(affected, mix.inputs[0])
    links.new(source.outputs['Base Surface'], mix.inputs[1])
    links.new(surface.outputs[0], mix.inputs[2])
    final = mix.outputs[0]
    if mask:
        cut = math_node('MULTIPLY', math_node('LESS_THAN', source.outputs['FX Alpha'], 1.), source.outputs['Enabled'])
        transparent = nodes.new('ShaderNodeBsdfTransparent')
        cut_mix = nodes.new('ShaderNodeMixShader')
        links.new(cut, cut_mix.inputs[0])
        links.new(final, cut_mix.inputs[1])
        links.new(transparent.outputs[0], cut_mix.inputs[2])
        final = cut_mix.outputs[0]
        if hasattr(material, 'blend_method'):
            material.blend_method = 'HASHED'
    links.new(final, target.inputs['Surface'])
    links.new(combine.outputs[0], target.inputs['Adjusted Color'])
    links.new(strength, target.inputs['Glow Strength'])
    for index, n in enumerate(n for n in nodes if n.type == 'MATH'):
        n.location = (-850 + (index % 7) * 190, -200 - (index // 7) * 160)
    split.location = (-850, 200)
    combine.location = (100, 200)
    outer, outer_links = material.node_tree.nodes, material.node_tree.links
    active_node = outer.active
    node = outer.new('ShaderNodeGroup')
    node.node_tree = group
    node.name = 'RabbitFX Effects'
    node.label = 'RabbitFX — disable via Effects collection'
    node['rabbitfx_group'] = True
    node.location = (600, 200)
    output = next(n for n in outer if n.type == 'OUTPUT_MATERIAL' and n.is_active_output)
    previous = output.inputs['Surface'].links[0].from_socket
    outer_links.new(previous, node.inputs['Base Surface'])
    outer_links.new(node.outputs['Surface'], output.inputs['Surface'])
    output.location = (1000, 200)
    uv = next(n for n in outer if n.type == 'UVMAP')
    for resource, color_input, alpha_input in ((glow, 'Glow Color', 'Glow Alpha'), (mask, None, 'FX Alpha')):
        if not resource:
            continue
        image = outer.new('ShaderNodeTexImage')
        image.name = 'RabbitFX ' + resource
        image.label = 'RabbitFX Glow' if color_input else 'RabbitFX explicit cutout'
        srgb = importer.model.texture_resolver.info[resource].get('srgb') is True
        image.image = importer.image(resource, 'sRGB' if color_input and srgb else 'Non-Color')
        image.location = (0, -1200 if color_input else -1500)
        outer_links.new(uv.outputs['UV'], image.inputs['Vector'])
        if color_input: outer_links.new(image.outputs['Color'], node.inputs[color_input])
        outer_links.new(image.outputs['Alpha'], node.inputs[alpha_input])
    for socket in ('Normal', 'Roughness'):
        if original.inputs[socket].is_linked:
            outer_links.new(original.inputs[socket].links[0].from_socket, node.inputs[socket])
        else:
            node.inputs[socket].default_value = original.inputs[socket].default_value
    driver = node.inputs['Enabled'].driver_add('default_value').driver
    driver.type = 'AVERAGE'
    variable = driver.variables.new()
    variable.type = 'SINGLE_PROP'
    variable.targets[0].id_type = 'COLLECTION'
    variable.targets[0].id = collection
    variable.targets[0].data_path = '["rabbitfx_enabled"]'
    outer.active = active_node
    material['rabbitfx_applied'] = True
    material['rabbitfx_source'] = 'RabbitFX WW v8.3.2: CommandListRun and glow/HSV/cutout shader replacement'
    material['rabbitfx_note'] = RABBITFX_NOTE
    return node


WUWA_APPEARANCE_NOTE = 'Toggle WuWa Appearance for optional stylized lighting; use Scene Lights/World in Material Preview.'

WUWA_RUNTIME_SOURCE = r'''
import bpy
import math
from mathutils import Vector
from bpy.app.handlers import persistent
_wuwa_busy = False
_wuwa_rendering = False

def _wuwa_hidden(layer, target, inherited=False):
    collection = layer.collection
    hidden = inherited or layer.exclude or collection.hide_viewport
    hidden = hidden or (collection.hide_render if _wuwa_rendering else layer.hide_viewport)
    if collection == target:
        return hidden
    for child in layer.children:
        found = _wuwa_hidden(child, target, hidden)
        if found is not None:
            return found
    return None

def _wuwa_set(collection, key, value):
    old = collection.get(key)
    same = (tuple(old) == tuple(value)) if isinstance(value, (list, tuple)) and old is not None else old == value
    if not same:
        collection[key] = value
        collection.update_tag()

def _wuwa_lighting(scene, layer, collection):
    if not collection.get('follow_scene_lighting', True):
        return
    root = collection.get('wuwa_root')
    center = Vector(collection.get('model_center', (0, 0, 0)))
    if root:
        center = root.matrix_world @ center
    requested = collection.get('key_light_name', '')
    candidates = []
    for obj in scene.objects:
        if obj.type != 'LIGHT' or obj.name not in layer.objects:
            continue
        if obj.hide_viewport or (obj.hide_render if _wuwa_rendering else obj.hide_get(view_layer=layer)):
            continue
        if not any(_wuwa_hidden(layer.layer_collection, c) is False for c in obj.users_collection):
            continue
        light = obj.data
        if light.energy <= 0:
            continue
        color = tuple(light.color)
        if getattr(light, 'use_temperature', False):


            pass
        if light.type == 'SUN':
            direction = obj.matrix_world.to_quaternion() @ Vector((0, 0, 1))
            power = light.energy
        else:
            delta = obj.matrix_world.translation - center
            direction = delta.normalized()
            power = light.energy / (4 * math.pi * max(delta.length_squared, .01))
            forward = obj.matrix_world.to_quaternion() @ Vector((0, 0, -1))
            facing = max(0., forward.dot(-direction))
            if light.type == 'AREA':
                power *= facing
            elif light.type == 'SPOT':
                edge = math.cos(light.spot_size * .5)
                power *= min(1., max(0., (facing-edge) / max(.001, (1-edge)*light.spot_blend)))
        score = power * sum(a*b for a,b in zip(color, (.2126, .7152, .0722)))
        if score > 0:
            candidates.append((obj.name == requested, light.type == 'SUN', score, obj, direction, power, color))
    if candidates:
        _, _, _, obj, direction, power, color = max(candidates, key=lambda item: item[:3])
        _wuwa_set(collection, 'key_direction', list(direction))
        _wuwa_set(collection, 'key_color', list(color))
        _wuwa_set(collection, 'key_strength', float(power / (1 + power)))
        _wuwa_set(collection, 'wuwa_light_source', obj.name)
    else:
        _wuwa_set(collection, 'key_strength', 0.)
        _wuwa_set(collection, 'wuwa_light_source', 'No visible scene light')
    world = scene.world
    ambient = (0., 0., 0.)
    if world:
        ambient = tuple(world.color)
        if world.use_nodes and world.node_tree:
            output = next((n for n in world.node_tree.nodes if n.type == 'OUTPUT_WORLD' and n.is_active_output), None)
            background = output.inputs['Surface'].links[0].from_node if output and output.inputs['Surface'].is_linked else None
            if background and background.type == 'BACKGROUND':
                color = (tuple(collection['ambient_tint']) if background.inputs['Color'].is_linked
                         else tuple(background.inputs['Color'].default_value[:3]))
                strength = (1. if background.inputs['Strength'].is_linked else background.inputs['Strength'].default_value)
                ambient = tuple(max(0., c * strength) for c in color)
            else:
                ambient = tuple(collection['ambient_tint'])
    _wuwa_set(collection, 'ambient_color', list(ambient))

@persistent
def wuwa_sync(scene=None, depsgraph=None):
    global _wuwa_busy
    if _wuwa_busy:
        return
    _wuwa_busy = True
    try:
        scene = scene if isinstance(scene, bpy.types.Scene) else bpy.context.scene
        layer = getattr(depsgraph, 'view_layer', None) or bpy.context.view_layer
        if layer.name not in scene.view_layers or scene.view_layers[layer.name] != layer:
            layer = scene.view_layers[0]
        for collection in bpy.data.collections:
            if not collection.get('wuwa_controller'):
                continue
            hidden = _wuwa_hidden(layer.layer_collection, collection)
            if hidden is None:
                continue
            _wuwa_set(collection, 'wuwa_enabled', float(not hidden))
            _wuwa_lighting(scene, layer, collection)
    finally:
        _wuwa_busy = False

@persistent
def wuwa_render_pre(scene, *args):
    global _wuwa_rendering
    _wuwa_rendering = True
    wuwa_sync(scene)

@persistent
def wuwa_render_end(scene, *args):
    global _wuwa_rendering
    _wuwa_rendering = False
    wuwa_sync(scene)

def register():
    for handlers, callback in (
        (bpy.app.handlers.depsgraph_update_post, wuwa_sync),
        (bpy.app.handlers.frame_change_post, wuwa_sync),
        (bpy.app.handlers.load_post, wuwa_sync),
        (bpy.app.handlers.render_pre, wuwa_render_pre),
        (bpy.app.handlers.render_post, wuwa_render_end),
        (bpy.app.handlers.render_cancel, wuwa_render_end),
    ):
        for old in list(handlers):
            if getattr(old, '_wwmi_wuwa', False):
                handlers.remove(old)
        callback._wwmi_wuwa = True
        handlers.append(callback)
    wuwa_sync()

register()
'''


def _wuwa_controller(importer):
    if hasattr(importer, 'wuwa_collection'):
        return importer.wuwa_collection
    bpy = importer.bpy
    collection = importer.track('collections', bpy.data.collections.new('WuWa Appearance'))
    getattr(importer, 'import_collection', bpy.context.scene.collection).children.link(collection)
    importer.wuwa_collection = collection
    defaults = dict(wuwa_controller=True, wuwa_enabled=1., follow_scene_lighting=True,
                    key_light_name='', key_direction=(.4, .6, .7), key_color=(1., 1., 1.),
                    key_strength=0., ambient_color=(.05, .05, .05), ambient_tint=(.35, .35, .35),
                    strength=1., physical_mix=.35, shadow_level=.52, shadow_width=.4,
                    shadow_threshold=.05, shadow_tint=(.78, .84, 1.), rim_strength=.08,
                    ambient_gain=.65, key_gain=1.25)
    for key, value in defaults.items():
        collection[key] = value
    for key in ('strength', 'physical_mix', 'shadow_level', 'rim_strength'):
        collection.id_properties_ui(key).update(min=0., max=1.)
    collection.id_properties_ui('shadow_width').update(min=.01, max=2.)
    collection.id_properties_ui('shadow_threshold').update(min=-1., max=1.)
    for key in ('ambient_gain', 'key_gain'):
        collection.id_properties_ui(key).update(min=0., max=5.)
    for key in ('key_color', 'ambient_color', 'ambient_tint', 'shadow_tint'):
        collection.id_properties_ui(key).update(subtype='COLOR', min=0., max=1.)
    collection['wuwa_note'] = WUWA_APPEARANCE_NOTE
    runtime = importer.track('texts', bpy.data.texts.new('WuWa Appearance Controls.py'))
    runtime.write(WUWA_RUNTIME_SOURCE)
    runtime.use_module = True
    collection['wuwa_controls_script'] = runtime.name
    importer.wuwa_runtime = {'__name__': 'wwmi_wuwa_controls'}
    exec(compile(WUWA_RUNTIME_SOURCE, runtime.name, 'exec'), importer.wuwa_runtime)
    return collection


def wuwa_appearance(importer, material):

    if material.get('wuwa_applied'):
        return material.node_tree.nodes['WuWa Appearance']
    bpy = importer.bpy
    collection = _wuwa_controller(importer)
    group = importer.track('node_groups', bpy.data.node_groups.new('WuWa Appearance · ' + material.name, 'ShaderNodeTree'))
    controls = {
        'Enabled': 'wuwa_enabled', 'Strength': 'strength', 'Physical Mix': 'physical_mix',
        'Key Direction': 'key_direction', 'Key Color': 'key_color', 'Key Strength': 'key_strength',
        'Ambient Color': 'ambient_color', 'Ambient Gain': 'ambient_gain', 'Key Gain': 'key_gain',
        'Shadow Tint': 'shadow_tint', 'Shadow Level': 'shadow_level',
        'Shadow Width': 'shadow_width', 'Shadow Threshold': 'shadow_threshold', 'Rim Strength': 'rim_strength'}
    def socket(name, kind, output=False):
        if bpy.app.version >= (4, 0, 0):
            return group.interface.new_socket(name=name, in_out='OUTPUT' if output else 'INPUT', socket_type=kind)
        return (group.outputs if output else group.inputs).new(kind, name)
    for name, kind in [('Base Surface', 'NodeSocketShader'), ('Base Color', 'NodeSocketColor'),
                       ('Alpha', 'NodeSocketFloat'), ('Normal', 'NodeSocketVector')]:
        socket(name, kind)
    for name, key in controls.items():
        value = collection[key]
        kind = 'NodeSocketVector' if key == 'key_direction' else 'NodeSocketColor' if 'color' in key or 'tint' in key else 'NodeSocketFloat'
        port = socket(name, kind)
        port.default_value = (*value, 1.) if kind == 'NodeSocketColor' else value
    socket('Surface', 'NodeSocketShader', True)
    nodes, links = group.nodes, group.links
    source, target = nodes.new('NodeGroupInput'), nodes.new('NodeGroupOutput')
    source.location = (-1200, 300)
    target.location = (1100, 300)
    def connect(value, port):
        if hasattr(value, 'is_output'):
            links.new(value, port)
        else:
            port.default_value = value
    def math(operation, a, b=None):
        node = nodes.new('ShaderNodeMath')
        node.operation = operation
        connect(a, node.inputs[0])
        if b is not None: connect(b, node.inputs[1])
        return node.outputs[0]
    def vector(operation, a, b=None, scale=None):
        node = nodes.new('ShaderNodeVectorMath')
        node.operation = operation
        connect(a, node.inputs[0])
        if b is not None: connect(b, node.inputs[1])
        if scale is not None: connect(scale, node.inputs['Scale'])
        return node.outputs['Value' if operation == 'DOT_PRODUCT' else 'Vector']
    normal = vector('NORMALIZE', source.outputs['Normal'])
    light = vector('NORMALIZE', source.outputs['Key Direction'])
    dot = vector('DOT_PRODUCT', normal, light)
    ramp = nodes.new('ShaderNodeMapRange')
    ramp.interpolation_type = 'SMOOTHSTEP'
    ramp.clamp = True
    links.new(dot, ramp.inputs['Value'])
    links.new(source.outputs['Shadow Threshold'], ramp.inputs['From Min'])
    links.new(math('ADD', source.outputs['Shadow Threshold'], math('MAXIMUM', source.outputs['Shadow Width'], .01)), ramp.inputs['From Max'])
    ramp.inputs['To Min'].default_value = 0.
    ramp.inputs['To Max'].default_value = 1.
    shade = nodes.new('ShaderNodeMixRGB')
    links.new(ramp.outputs['Result'], shade.inputs[0])
    links.new(vector('SCALE', source.outputs['Shadow Tint'], scale=source.outputs['Shadow Level']), shade.inputs[1])
    shade.inputs[2].default_value = (1., 1., 1., 1.)
    key = vector('SCALE', source.outputs['Key Color'], scale=math('MULTIPLY', source.outputs['Key Strength'], source.outputs['Key Gain']))
    ambient = vector('SCALE', source.outputs['Ambient Color'], scale=source.outputs['Ambient Gain'])
    illumination = vector('ADD', vector('MULTIPLY', shade.outputs[0], key), ambient)
    color = vector('MULTIPLY', source.outputs['Base Color'], illumination)
    geometry = nodes.new('ShaderNodeNewGeometry')
    facing = math('ABSOLUTE', vector('DOT_PRODUCT', normal, geometry.outputs['Incoming']))
    rim = math('POWER', math('MAXIMUM', math('SUBTRACT', 1., facing), 0.), 4.)
    rim = math('MULTIPLY', rim, math('MAXIMUM', dot, 0.))
    rim = math('MULTIPLY', rim, source.outputs['Rim Strength'])
    color = vector('ADD', color, vector('SCALE', key, scale=rim))
    emission = nodes.new('ShaderNodeEmission')
    links.new(color, emission.inputs['Color'])
    transparent = nodes.new('ShaderNodeBsdfTransparent')
    alpha = nodes.new('ShaderNodeMixShader')
    links.new(source.outputs['Alpha'], alpha.inputs[0])
    links.new(transparent.outputs[0], alpha.inputs[1])
    links.new(emission.outputs[0], alpha.inputs[2])
    physical = nodes.new('ShaderNodeMixShader')
    links.new(source.outputs['Physical Mix'], physical.inputs[0])
    links.new(alpha.outputs[0], physical.inputs[1])
    links.new(source.outputs['Base Surface'], physical.inputs[2])
    final = nodes.new('ShaderNodeMixShader')
    paths = nodes.new('ShaderNodeLightPath')
    visible = math('MAXIMUM', paths.outputs['Is Camera Ray'], paths.outputs['Is Glossy Ray'])
    enabled = math('MULTIPLY', source.outputs['Enabled'], source.outputs['Strength'])
    links.new(math('MULTIPLY', enabled, visible), final.inputs[0])
    links.new(source.outputs['Base Surface'], final.inputs[1])
    links.new(physical.outputs[0], final.inputs[2])
    links.new(final.outputs[0], target.inputs['Surface'])
    for index, node in enumerate(n for n in nodes if n not in (source, target)):
        node.location = (-950 + index % 7 * 270, -index // 7 * 230)
    outer, outer_links = material.node_tree.nodes, material.node_tree.links
    active = outer.active
    original = next(n for n in outer if n.type == 'BSDF_PRINCIPLED')
    output = next(n for n in outer if n.type == 'OUTPUT_MATERIAL' and n.is_active_output)
    node = outer.new('ShaderNodeGroup')
    node.node_tree = group
    node.name = 'WuWa Appearance'
    node.label = 'WuWa-inspired appearance · collection toggle'
    node.location = (600, 500)
    outer_links.new(output.inputs['Surface'].links[0].from_socket, node.inputs['Base Surface'])
    outer_links.new(node.outputs['Surface'], output.inputs['Surface'])
    for name in ('Base Color', 'Alpha', 'Normal'):
        port = original.inputs[name]
        if port.is_linked:
            outer_links.new(port.links[0].from_socket, node.inputs[name])
        elif name == 'Normal':
            geo = outer.new('ShaderNodeNewGeometry')
            geo.name = 'WuWa Surface Normal'
            outer_links.new(geo.outputs['Normal'], node.inputs['Normal'])
        else:
            node.inputs[name].default_value = port.default_value
    for name, key in controls.items():
        port = node.inputs[name]
        dimensions = 3 if port.type in {'RGBA', 'VECTOR'} else 0
        for index in range(dimensions) if dimensions else (None,):
            curve = port.driver_add('default_value', index) if index is not None else port.driver_add('default_value')
            driver = curve.driver
            driver.type = 'AVERAGE'
            variable = driver.variables.new()
            variable.type = 'SINGLE_PROP'
            variable.targets[0].id_type = 'COLLECTION'
            variable.targets[0].id = collection
            variable.targets[0].data_path = '["' + key + '"]' + (f'[{index}]' if index is not None else '')
    outer.active = active
    material['wuwa_applied'] = True
    material['wuwa_note'] = WUWA_APPEARANCE_NOTE
    return node


def rabbitfx_api():

    return {"DEFAULTS": RABBITFX_DEFAULTS, "numeric_value": numeric_value,
            "capture_rabbitfx": capture_rabbitfx, "adjusted_color": adjusted_color,
            "rabbit_fx_effects": rabbit_fx_effects}


LOG = logging.getLogger("wwmi_import_v11")


CONTROLS_SOURCE = r'''
import bpy
import json

def find_root(obj):
    while obj is not None:
        if obj.get("wwmi_import_version") == "1.1":
            return obj
        obj = obj.parent
    return None

def apply_state(root, changes):
    options = json.loads(root["wwmi_toggle_options"])
    settings = json.loads(root["wwmi_toggle_values"])
    for key, value in changes.items():
        if key not in options or value not in options[key]:
            raise ValueError(f"Unsupported toggle: {key}={value}")
        settings[key] = value
    partial = root.get("wwmi_factorized_states", False)
    def matches(states):
        return any(all(settings.get(k) == v for k, v in state.items()) for state in states)
    if partial and matches(json.loads(root.get("wwmi_invalid_toggle_rules", "[]"))):
        raise ValueError("This configuration references missing source resources. See the texture report.")
    if not partial and settings not in json.loads(root["wwmi_valid_toggle_states"]):
        raise ValueError("This configuration was not imported or references missing source resources. See the texture report.")

    updates = []
    for obj in root.children:
        if "wwmi_material_variants" not in obj:
            continue
        slots = {entry["slot"] for entry in json.loads(obj["wwmi_material_variants"])
                 if (matches(entry["states"]) if partial else settings in entry["states"])}
        if len(slots) > 1:
            raise ValueError(f"{obj.name}: simultaneous materials on the same geometry are unsupported")
        slot = next(iter(slots), None)
        if slot is not None and (slot >= len(obj.data.materials) or obj.data.materials[slot] is None):
            raise ValueError(f"{obj.name}: a required material slot was removed")
        updates.append((obj, slot))
    for obj, slot in updates:
        visible = slot is not None
        if obj.hide_render != (not visible):
            obj.hide_render = not visible
        if obj.hide_get() != (not visible):
            obj.hide_set(not visible)
        if visible:
            if obj.data.polygons and obj.data.polygons[0].material_index != slot:
                obj.data.polygons.foreach_set("material_index", [slot] * len(obj.data.polygons))
            obj.active_material_index = slot
    root["wwmi_toggle_values"] = json.dumps(settings)
    return settings

class WWMI_OT_set_toggle_v11(bpy.types.Operator):
    bl_idname = "wwmi.set_toggle_v11"
    bl_label = "Set model toggle"
    bl_options = {'REGISTER', 'UNDO'}
    root_name: bpy.props.StringProperty()
    toggle: bpy.props.StringProperty()
    value_json: bpy.props.StringProperty()

    def execute(self, context):
        root = bpy.data.objects.get(self.root_name)
        if root is None:
            self.report({'ERROR'}, "Imported model no longer exists")
            return {'CANCELLED'}
        try:
            apply_state(root, {self.toggle: json.loads(self.value_json)})
        except (ValueError, KeyError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}

class WWMI_PT_toggles_v11(bpy.types.Panel):
    bl_label = "Model Toggles"
    bl_idname = "WWMI_PT_toggles_v11"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "WWMI"

    def draw(self, context):
        root = find_root(context.object)
        if root is None:
            self.layout.label(text="Select an imported model or one of its parts")
            return
        self.layout.label(text=root.name)
        settings = json.loads(root["wwmi_toggle_values"])
        for key, values in json.loads(root["wwmi_toggle_options"]).items():
            row = self.layout.row(align=True)
            row.label(text=key.replace('_', ' ').title())
            for value in values:
                op = row.operator("wwmi.set_toggle_v11", text=str(value), depress=settings[key] == value)
                op.root_name = root.name
                op.toggle = key
                op.value_json = json.dumps(value)
        appearance = bpy.data.collections.get(root.get('wwmi_wuwa_collection', ''))
        if appearance:
            box = self.layout.box()
            box.label(text='WuWa Appearance · toggle collection in Outliner')
            for key, label in (('strength', 'Strength'), ('physical_mix', 'Physical shading'),
                               ('shadow_level', 'Shadow brightness'), ('shadow_width', 'Shadow softness'),
                               ('shadow_tint', 'Shadow tint'), ('rim_strength', 'Rim light'),
                               ('follow_scene_lighting', 'Follow scene lighting')):
                box.prop(appearance, '["' + key + '"]', text=label)
            if appearance.get('follow_scene_lighting'):
                box.prop(appearance, '["key_light_name"]', text='Key light (blank = auto)')
            else:
                for key, label in (('key_direction', 'Light direction'), ('key_color', 'Light color'),
                                   ('key_strength', 'Light intensity'), ('ambient_color', 'Ambient color')):
                    box.prop(appearance, '["' + key + '"]', text=label)

for cls in (WWMI_OT_set_toggle_v11, WWMI_PT_toggles_v11):
    previous = getattr(bpy.types, cls.__name__, None)
    if previous is not None:
        bpy.utils.unregister_class(previous)
    bpy.utils.register_class(cls)
'''


@dataclass(frozen=True)
class Line:
    number: int
    text: str
    key: str = field(init=False, repr=False, compare=False)
    value: str = field(init=False, repr=False, compare=False)
    value_lower: str = field(init=False, repr=False, compare=False)
    operands: tuple = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        key, separator, value = self.text.partition('=')
        object.__setattr__(self, 'key', key.strip().lower() if separator else '')
        object.__setattr__(self, 'value', value.strip())
        object.__setattr__(self, 'value_lower', value.strip().lower())
        key, value = self.key, self.value_lower
        target = re.sub(r'^(?:ref|copy)\s+', '', value)
        slot = re.fullmatch(r'ps-t(\d+)', key)
        source_slot = re.fullmatch(r'ps-t(\d+)', target)
        buffer = bool(re.fullmatch(r'ib|vb\d+', key))
        resource = key.startswith('resource') and bool(re.fullmatch(r'resource[^ ]+|null', target))
        checked = re.fullmatch(r'ps-t(\d+)', value) if key == 'checktextureoverride' else None
        numbers = re.fullmatch(r'(\d+)\s*,\s*(\d+)\s*,\s*(-?\d+)', self.value) if key == 'drawindexed' else None
        label = re.match(r';\s*Draw\s+(.+)', self.text, re.I) if self.text.startswith(';') else None
        object.__setattr__(self, 'operands', (
            int(slot[1]) if slot else None, target,
            int(source_slot[1]) if source_slot else None, buffer, resource,
            int(checked[1]) if checked else None,
            tuple(map(int, numbers.groups())) if numbers else None,
            label[1].strip() if label else None))


@dataclass
class Section:
    name: str
    lines: list[Line] = field(default_factory=list)
    source: Path | None = None

    def values(self):
        return {k.strip().lower(): v.strip() for line in self.lines
                if not line.text.startswith(";") and "=" in line.text
                for k, v in [line.text.split("=", 1)]}


@dataclass
class Branch:

    arms: list[tuple[str | None, list]]


@dataclass
class Draw:
    component: int
    name: str
    count: int
    start: int
    base_vertex: int
    line: int
    active: bool
    conditions: tuple[str, ...]
    slots: dict[int, str]
    buffers: dict[str, str]
    candidates: tuple[str, ...]
    checked_slots: tuple[int, ...] = ()
    named_bindings: dict[str, dict] = field(default_factory=dict)
    toggle_states: list[dict] = field(default_factory=list)
    variant_label: str = ""
    rabbitfx: dict = field(default_factory=dict)


def discover_ini(path):

    path = Path(path).expanduser().resolve()
    if path.is_file():
        if path.suffix.lower() != ".ini":
            raise ValueError(f"Expected an INI file: {path}")
        return path
    if not path.is_dir():
        raise FileNotFoundError(path)
    candidates = {}
    for file in sorted(path.rglob("*.ini")):
        sections = read_ini(file)
        if (any(re.fullmatch(r"textureoverridecomponent\d+", name) for name in sections)
                and any(re.match(r"drawindexed\s*=", line.text, re.I)
                        for section in sections.values() for line in section.lines)):
            candidates[file] = {section.source for section in sections.values()} - {file}
    included = set().union(*candidates.values()) if candidates else set()
    roots = [file for file in candidates if file not in included]
    if len(roots) == 1:
        return roots[0]
    if not roots:
        raise ValueError(f"No WWMI mesh INI found in {path}")
    raise ValueError("Multiple mesh INIs found; pass the intended INI file as --mod-folder:\n"
                     + "\n".join(map(str, roots)))


def named_texture_role(binding):

    leaf = binding.lower().rsplit("\\", 1)[-1].removeprefix("resource")
    return {"diffuse": "base_color", "albedo": "base_color", "basecolor": "base_color",
            "base_color": "base_color", "normalmap": "normal", "normal": "normal",
            "lightmap": "mask", "mask": "mask", "emission": "effect",
            "emissive": "effect", "opacity": "opacity"}.get(leaf)


def read_ini(path, _seen=None):

    path = Path(path).resolve()
    seen = set() if _seen is None else _seen
    if path in seen:
        raise ValueError(f"Recursive or duplicate INI include: {path}")
    seen.add(path)
    sections = {}
    current = None
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        text = raw.strip()
        if not text:
            continue
        if not text.startswith(";"):
            text = text.split(";", 1)[0].strip()
        if text.startswith("[") and text.endswith("]"):
            name = text[1:-1].strip()
            if name.lower() in sections:
                raise ValueError(f"Line {number}: duplicate section [{name}]")
            current = Section(name, source=path)
            sections[name.lower()] = current
        elif current is not None:
            current.lines.append(Line(number, text))
    includes = sections.pop("include", None)
    if includes:
        for line in includes.lines:
            if line.text.startswith(";") or "=" not in line.text:
                continue
            key, value = (s.strip() for s in line.text.split("=", 1))
            target = path.parent / value.strip('"').replace("\\", "/")
            if key.lower() == "include":
                files = [target]
            elif key.lower() == "include_recursive":
                files = sorted(target.rglob("*.ini"))
            else:
                continue
            for file in files:
                for name, section in read_ini(file, seen).items():
                    if name in sections:
                        raise ValueError(f"Duplicate included section [{section.name}]; namespace support required")
                    sections[name] = section
    return sections


class Condition(str):


    def __new__(cls, expression):
        result = super().__new__(cls, expression)
        low = expression.strip().lower()
        result.simple = None
        match = re.fullmatch(r'\$([a-z_]\w*)\s*(===|!==|==|!=|<=|>=|<|>)\s*(-?\d+(?:\.\d+)?)', low)
        if match:
            key, operation, literal = match.groups()
            value = float(literal) if '.' in literal else int(literal)
            function = {'==': operator.eq, '===': operator.eq, '!=': operator.ne,
                        '!==': operator.ne, '<': operator.lt, '<=': operator.le,
                        '>': operator.gt, '>=': operator.ge}[operation]
            result.simple = key, function, value
        result.variables = tuple(re.findall(r'\$([a-z_]\w*)', low))
        return result


def control_tree(lines):

    root, block, stack = [], None, []
    block = root
    for line in lines:
        low = line.text.lower()
        if low.startswith("if "):
            arm = []
            branch = Branch([(Condition(line.text[3:].strip()), arm)])
            block.append(branch)
            stack.append((block, branch))
            block = arm
        elif low.startswith("else if ") or low.startswith("elif ") or low == "else":
            if not stack or stack[-1][1].arms[-1][0] is None:
                raise ValueError(f"Line {line.number}: misplaced {line.text}")
            expression = None if low == "else" else Condition(line.text[8 if low.startswith('else if ') else 5:].strip())
            block = []
            stack[-1][1].arms.append((expression, block))
        elif low == "endif":
            if not stack:
                raise ValueError(f"Line {line.number}: unmatched endif")
            block, _ = stack.pop()
        else:
            block.append(line)
    if stack:
        raise ValueError("Unterminated INI if block")
    return root


def condition_value(expression, variables, resources=None):

    if isinstance(expression, Condition) and expression.simple is not None:
        key, compare, value = expression.simple
        if key not in variables:
            raise ValueError(f"Unknown condition variable ${key}: {expression.strip().lower()}")
        return compare(variables[key], value)
    expression = expression.strip().lower()


    if expression in ("$mod_enabled", "resourcemergedskeleton !== null",
                      "resourcemergedskeleton != null"):
        return True
    if re.fullmatch(r"\$state_id_\d+\s*!=\s*\$state_id", expression):
        return False
    if re.fullmatch(r"vs-cb[34]\s*==\s*3381\.7777", expression):
        return False
    resource_test = re.fullmatch(r"(resource[^ ]+)\s*(!==|===|!=|==)\s*null", expression)
    if resource_test and resources is not None:
        present = resources(resource_test[1])
        return present if resource_test[2].startswith("!") else not present

    def variable(match):
        key = match.group(1).lower()
        if key not in variables:
            raise ValueError(f"Unknown condition variable ${key}: {expression}")
        return repr(variables[key])

    converted = re.sub(r"\$([a-z_]\w*)", variable, expression)
    converted = converted.replace("!==", "!=").replace("===", "==")
    converted = converted.replace("&&", " and ").replace("||", " or ")
    converted = re.sub(r"!(?!=)", " not ", converted).strip()

    def visit(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, bool)):
            return node.value
        if isinstance(node, ast.UnaryOp):
            value = visit(node.operand)
            if isinstance(node.op, ast.Not):
                return not value
            if isinstance(node.op, ast.USub):
                return -value
        if isinstance(node, ast.BoolOp):
            values = [bool(visit(v)) for v in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare):
            left = visit(node.left)
            for op, right_node in zip(node.ops, node.comparators):
                right = visit(right_node)
                operations = {ast.Eq: left == right, ast.NotEq: left != right,
                              ast.Lt: left < right, ast.LtE: left <= right,
                              ast.Gt: left > right, ast.GtE: left >= right}
                if type(op) not in operations:
                    break
                if not operations[type(op)]:
                    return False
                left = right
            else:
                return True
        raise ValueError(f"Unsupported INI condition: {expression}")

    return bool(visit(ast.parse(converted, mode="eval").body))


def dds_info(path):

    with Path(path).open("rb") as file:
        header = file.read(148)
        file.seek(0)
        digest = file_sha256(file)
    info = {"digest": digest, "width": 0, "height": 0, "srgb": None}
    if header[:4] != b"DDS " or len(header) < 128:
        return info
    info["height"], info["width"] = struct.unpack_from("<II", header, 12)
    if header[84:88] == b"DX10" and len(header) >= 148:
        dxgi = struct.unpack_from("<I", header, 128)[0]
        info["dxgi"] = dxgi

        info["srgb"] = dxgi in {29, 72, 75, 78, 91, 93, 99}
    return info


class TextureResolver:


    SLOT_ROLES = {0: "normal", 1: "mask", 2: "base_color", 3: "secondary_color", 17: "effect"}

    def __init__(self, model):
        self.model = model
        self.info, self.roles, self.evidence = {}, {}, {}
        paths = {}
        self._memberships = {}
        self._profile_indices = {id(draw): index for index, draw in enumerate(model.draws)}
        for resource, data in model.resources.items():
            if "filename" not in data:
                continue
            path = model.resource_path(resource)
            if path.suffix.lower() not in {".dds", ".png", ".tga", ".jpg", ".jpeg", ".tif", ".exr"}:
                continue
            if not path.is_file():
                raise FileNotFoundError(path)
            paths[resource] = path


        unique_paths = list(dict.fromkeys(paths.values()))
        workers = min(worker_count(), len(unique_paths))
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='WWMI') as pool:
                metadata = dict(zip(unique_paths, pool.map(dds_info, unique_paths)))
        else:
            metadata = {path: dds_info(path) for path in unique_paths}
        for resource, path in paths.items():

            self.info[resource] = dict(metadata[path])
            self.roles[resource] = set()
            self.evidence[resource] = []
        for draw in model.draws:
            for key, role in (("glow_resource", "effect"), ("fx_resource", "mask")):
                resource = draw.rabbitfx.get(key)
                if resource:
                    if resource not in self.info:
                        raise ValueError(f"RabbitFX refers to unavailable texture: {resource}")
                    self.roles[resource].add(role)
                    self.evidence[resource].append(f"RabbitFX Run line {draw.rabbitfx['call_line']}: {key}")
            for binding, entry in draw.named_bindings.items():
                resource = entry["resource"]
                if resource in self.info:
                    self.roles[resource].add(entry["role"])
                    self.evidence[resource].append(f"INI line {entry['line']}: {binding}")
            for slot, resource in draw.slots.items():
                if resource in self.info:
                    role = self.SLOT_ROLES.get(slot, f"shader_slot_{slot}")
                    self.roles[resource].add(role)
                    self.evidence[resource].append(f"INI draw line {draw.line}: ps-t{slot}")
        for resource in self.info:
            stem = model.resource_path(resource).stem
            role = None

            for pattern, candidate in (
                (r"(?:NM|[_. -](?:normal|normalmap|nrm))$", "normal"),
                (r"(?:LM|[_. -](?:lightmap|mask|idmap))$", "mask"),
                (r"(?:FX|[_. -](?:emission|emissive|glow))$", "effect"),
                (r"[_. -](?:diffuse|albedo|basecolor|base_color|color)$", "base_color"),
                (r"[_. -](?:opacity|alpha|cutout)$", "opacity"),
            ):
                if re.search(pattern, stem, re.I):
                    role = candidate
                    break
            if role:
                self.roles[resource].add(role)
                self.evidence[resource].append(f"Semantic filename suffix: {stem}")

        groups = {}
        for resource, info in self.info.items():
            groups.setdefault(info["digest"], []).append(resource)
        for resources in groups.values():
            roles = set().union(*(self.roles[r] for r in resources))
            for resource in resources:
                for other in resources:
                    if other != resource and self.roles[other]:
                        self.evidence[resource].append(f"Identical file bytes: {other}")
                self.roles[resource].update(roles)
        self.profiles = [self.resolve(draw) for draw in model.draws]

    def membership(self, resource):
        if resource not in self._memberships:
            name = self.model.resource_path(resource).name
            match = re.match(r"components?-([0-9]+(?:-[0-9]+)*)(?=\s|\.|$)", name, re.I)
            self._memberships[resource] = tuple(map(int, match[1].split("-"))) if match else ()
        return self._memberships[resource]

    def resolve(self, draw):
        resources = list(dict.fromkeys([*draw.slots.values(),
                                       *(e["resource"] for e in draw.named_bindings.values()),
                                       *draw.candidates,
                                       *(draw.rabbitfx[k] for k in ("glow_resource", "fx_resource")
                                         if draw.rabbitfx.get(k))]))
        resources = [r for r in resources if r in self.info]
        assignments = {}
        notes = []
        color_candidates = []

        for slot, resource in draw.slots.items():
            role = self.SLOT_ROLES.get(slot, f"shader_slot_{slot}")
            assignments[role] = {"resource": resource, "method": "explicit_binding",
                                 "evidence": [f"INI line {draw.line}: ps-t{slot}"], "slot": slot}
        for binding, entry in draw.named_bindings.items():
            role, resource = entry["role"], entry["resource"]
            if role in assignments:
                if assignments[role]["resource"] != resource:
                    notes.append(f"Conflicting named {role} binding {binding}: {resource}; retained pixel-shader binding")
                continue
            assignments[role] = {"resource": resource, "method": "named_resource_binding",
                                 "evidence": [f"INI line {entry['line']}: {binding} = {resource}"]}

        def choose(role):
            if role in assignments:
                return
            eligible = [r for r in resources if role in self.roles[r]
                        and not (self.roles[r] - {role, "secondary_color"})]
            local = [r for r in eligible if self.membership(r) == (draw.component,)]
            eligible = local or eligible

            if len(eligible) == 1:
                resource = eligible[0]
                assignments[role] = {"resource": resource, "method": "resource_cross_reference",
                                     "evidence": list(dict.fromkeys(self.evidence[resource]))}
                if self.info[resource].get("content_inferred_role") == role:
                    assignments[role]["method"] = "image_content_inference"
            elif len(eligible) > 1:
                notes.append(f"Multiple {role} resources: {', '.join(eligible)}")

        for role in ("normal", "mask", "opacity"):
            choose(role)
        if "base_color" not in assignments:


            possible = [r for r in resources if (
                self.roles[r] <= {"base_color", "secondary_color"}
                and ("base_color" in self.roles[r] or self.info[r]["srgb"] is True))]
            local = [r for r in possible if self.membership(r) == (draw.component,)]
            pool = local or [r for r in possible if "base_color" in self.roles[r]] or possible
            color_candidates = pool if len(pool) > 1 else []
            if pool:

                scores = {r: self.info[r]["width"] * self.info[r]["height"] for r in pool}
                best_score = max(scores.values())
                best = [r for r in pool if scores[r] == best_score]
                if len(best) > 1:


                    specificity = {r: len(self.membership(r)) or float("inf") for r in best}
                    best = [r for r in best if specificity[r] == min(specificity.values())]
                alternatives = []
                if len(best) > 1:


                    alternatives = best[:-1]
                    best = best[-1:]
                    color_candidates = []
                if len(best) == 1:
                    resource = best[0]
                    method = "resource_cross_reference" if "base_color" in self.roles[resource] else "dds_color_inference"
                    evidence = list(dict.fromkeys(self.evidence[resource]))
                    if local:
                        evidence.append("Referenced texture names this component only")
                    if self.info[resource]["srgb"] is True:
                        evidence.append("DDS header declares an sRGB color format")
                    if len(pool) > 1:
                        method = "dds_color_inference"
                        evidence.append("Largest remaining color candidate, then narrowest component membership (heuristic)")
                    if any(self.info[r].get("content_inferred_role") for r in resources):
                        evidence.append("Packed data candidates excluded using image-content inference")
                    assignments["base_color"] = {"resource": resource, "method": method, "evidence": evidence}
                    if alternatives:
                        assignments["base_color"]["method"] = "ini_order_fallback"
                        assignments["base_color"]["alternative_resources"] = alternatives
                        evidence.append("Importer fallback policy: later INI reference wins unresolved color ties; "
                                        "file equality is not required. Candidate order: "
                                        + ", ".join([*alternatives, resource]))
                else:
                    notes.append("Ambiguous color candidates: " + ", ".join(best))
                    color_candidates = best
            else:
                notes.append("No color resource could be resolved from supplied references")
        return {"component": draw.component, "draw": draw.name, "line": draw.line,
                "active": draw.active, "assignments": assignments, "resources": resources,
                "variant": draw.variant_label, "toggle_states": draw.toggle_states,
                "checked_slots": list(draw.checked_slots), "color_candidates": color_candidates, "notes": notes,
                "rabbitfx": draw.rabbitfx}

    def profile(self, draw):
        return self.profiles[self._profile_indices[id(draw)]]

    def report(self):
        referenced = {r for p in self.profiles for r in p["resources"]}
        resource_paths = {self.model.resource_path(r).resolve() for r in self.info}
        return {"ini": str(self.model.ini_path), "toggles": self.model.variables,
                "toggle_options": self.model.toggle_options,
                "invalid_alternate_states": self.model.variant_errors,
                "draws": self.profiles,
                "resources": {r: {"path": str(self.model.resource_path(r)), "roles": sorted(self.roles[r]),
                                  "evidence": list(dict.fromkeys(self.evidence[r])), **info}
                              for r, info in self.info.items()},
                "resources_without_draws": sorted(set(self.info) - referenced),
                "unreferenced_texture_files": sorted(str(p) for p in self.model.folder.rglob("*.dds")
                    if p.resolve() not in resource_paths),
                "limitation": "Assignments marked inference are not verified original shader bindings. Packed masks/effects retain source data; their game shading is not reconstructed."}


class Model:
    def __init__(self, folder, toggles=None, include_disabled=True):
        self.ini_path = discover_ini(folder)
        self.folder = self.ini_path.parent
        self.sections = read_ini(self.ini_path)
        self.resources = {name: section.values() for name, section in self.sections.items()
                          if name.startswith("resource")}
        self.variables = {}
        for line in self.sections.get("constants", Section("Constants")).lines:
            match = re.fullmatch(r"global\s+(?:persist\s+)?\$(\w+)(?:\s*=\s*(-?\d+(?:\.\d+)?))?", line.text, re.I)
            if match:
                value = match[2] or "0"
                self.variables[match[1].lower()] = float(value) if "." in value else int(value)
        for key, value in (toggles or {}).items():
            if key.lower().lstrip("$") not in self.variables:
                raise ValueError(f"Unknown toggle: {key}")
            self.variables[key.lower().lstrip("$")] = value
        self.include_disabled = include_disabled
        self.draws = []
        self.warnings = set()
        self.arrays = {}
        self.shape_keys = {}
        self._trees = {}
        self._resource_paths = {}
        self._texture_sections = [section for section in self.sections.values()
                                  if section.name.lower().startswith("textureoverride") and any(
                                      re.match(r"this\s*=", line.text, re.I) for line in section.lines)]
        self._texture_variables = tuple(sorted({key.lower() for section in self._texture_sections
                                                for line in section.lines
                                                for key in re.findall(r"\$([a-z_]\w*)", line.text, re.I)}))
        self._texture_candidates_cache = {}
        self._component_sections = [(int(match[1]), section) for section in self.sections.values()
                                    if (match := re.fullmatch(r"textureoverridecomponent(\d+)", section.name, re.I))]
        self.parse_variants()
        if not self.draws:
            raise ValueError("No mesh draws found")
        self.load_buffers()
        self.validate_draws()
        self.texture_resolver = TextureResolver(self)

    def toggle_domains(self):

        domains = {}
        number = r"-?\d+(?:\.\d+)?"
        for section in self.sections.values():
            if not section.name.lower().startswith("key"):
                continue
            for line in section.lines:
                match = re.fullmatch(r"\$(\w+)\s*=\s*(" + number + r"(?:\s*,\s*" + number + r")*)", line.text)
                if not match or match[1].lower() not in self.variables:
                    continue
                key = match[1].lower()
                values = [float(v) if "." in v else int(v) for v in match[2].split(",")]
                domains.setdefault(key, [self.variables[key]])
                domains[key] = list(dict.fromkeys(domains[key] + values))
        return domains

    @staticmethod
    def geometry_signature(draw):
        return (draw.component, draw.start, draw.count, draw.base_vertex,
                tuple(sorted(draw.buffers.items())))

    @staticmethod
    def draw_signature(draw):


        return (draw.component, draw.start, draw.count, draw.base_vertex,
                tuple(sorted(draw.buffers.items())), tuple(sorted(draw.slots.items())),
                tuple(sorted((k, v["role"], v["resource"]) for k, v in draw.named_bindings.items())),
                draw.candidates, tuple(sorted(draw.rabbitfx.items())))

    def parse_variants(self):


        defaults = dict(self.variables)
        include_disabled = self.include_disabled
        domains = self.toggle_domains()
        self.variant_errors = {}
        self.factorized_states = False
        self.invalid_toggle_rules = []
        self.toggle_options = domains
        if not include_disabled or not domains:
            self.parse_draws()
            for draw in self.draws:
                draw.toggle_states = [{key: defaults[key] for key in domains}]
            return
        count = 1
        for values in domains.values():
            count *= len(values)
        if count > 4096:
            self.parse_factorized_variants(defaults, domains)
            return
        variants = {}
        try:
            self.include_disabled = False
            for index, values in enumerate(itertools.product(*domains.values())):
                settings = dict(zip(domains, values))
                self.variables = dict(defaults, **settings)
                self.draws = []
                try:
                    self.parse_draws()
                except ValueError as exc:
                    if index == 0:
                        raise
                    message = str(exc)
                    self.variant_errors.setdefault(message, {"count": 0, "example": settings})["count"] += 1
                    continue
                state_signatures = set()
                for draw in self.draws:
                    signature = self.draw_signature(draw)
                    if signature not in variants:
                        draw.active = index == 0
                        draw.toggle_states = []
                        changes = {k: v for k, v in settings.items() if v != defaults[k]}
                        draw.variant_label = ", ".join(f"{k}={v}" for k, v in changes.items())
                        variants[signature] = draw
                    if signature not in state_signatures:
                        variants[signature].toggle_states.append(settings)
                        state_signatures.add(signature)
        finally:
            self.variables = defaults
            self.include_disabled = include_disabled
        self.draws = list(variants.values())
        for message, detail in self.variant_errors.items():
            self.warnings.add(f"Skipped {detail['count']} invalid alternate toggle states: {message}; example {detail['example']}")

    def tree(self, section):
        if not hasattr(self, "_trees"):
            self._trees = {}
        key = section.name.lower()
        if key not in self._trees:
            self._trees[key] = control_tree(section.lines)
        return self._trees[key]

    def draw_dependencies(self, domains):


        result = {}
        def variables(text):
            return set(re.findall(r"\$([a-z_]\w*)", text.lower())) & domains.keys()
        texture_deps = set()
        for section in self.sections.values():
            if section.name.lower().startswith("textureoverride") and any(
                    re.match(r"this\s*=", line.text, re.I) for line in section.lines):
                texture_deps.update(variables(" ".join(line.text for line in section.lines)))

        def walk(nodes, state, guards, component, chain):
            for node in nodes:
                if isinstance(node, Branch):
                    branch_deps = set().union(*(variables(expr or "") for expr, _ in node.arms))
                    outcomes = [dict(state)]
                    for _, body in node.arms:
                        child = dict(state)
                        walk(body, child, guards | branch_deps, component, chain)
                        outcomes.append(child)
                    for key in set().union(*(s.keys() for s in outcomes)):
                        state[key] = set().union(*(s.get(key, set()) for s in outcomes))
                    continue
                if node.text.startswith(";") or "=" not in node.text:
                    continue
                key, value = (s.strip().lower() for s in node.text.split("=", 1))
                if key == "run":
                    if value in {"commandlistmergeskeleton", "commandlistcleanupsharedresources"}:
                        continue
                    if value in self.sections:
                        if value in chain or len(chain) >= 16:
                            raise ValueError(f"Recursive command list: {value}")
                        walk(self.tree(self.sections[value]), state, guards, component, chain + (value,))
                elif key == "drawindexed":
                    deps = guards | texture_deps | set().union(*state.values())
                    result.setdefault((component, node.number), set()).update(deps)
                elif re.fullmatch(r"ib|vb\d+|ps-t\d+", key) or key.startswith("resource") or key.startswith("$\\rabbitfx\\"):
                    source = re.sub(r"^(?:ref|copy)\s+", "", value)
                    state[key] = guards | variables(value) | state.get(source, set())
        for section in self.sections.values():
            match = re.fullmatch(r"textureoverridecomponent(\d+)", section.name, re.I)
            if match:
                walk(self.tree(section), {}, set(), int(match[1]), ())
        return result

    def parse_factorized_variants(self, defaults, domains):
        self.factorized_states = True
        groups = {}
        for (component, line), dependencies in self.draw_dependencies(domains).items():
            keys = tuple(key for key in domains if key in dependencies)
            groups.setdefault((component, keys), set()).add(line)
        variants = {}
        evaluations = 0
        try:
            self.include_disabled = False

            self.draws = []
            self.parse_draws()
            for (component, keys), lines in groups.items():
                count = 1
                for key in keys:
                    count *= len(domains[key])
                if count > 65536:
                    raise ValueError(f"Component {component} has {count} interacting draw states; "
                                     "use --active-only to import the selected configuration")
                for values in itertools.product(*(domains[key] for key in keys)):
                    settings = dict(zip(keys, values))
                    self.variables = dict(defaults, **settings)
                    self.draws = []
                    evaluations += 1
                    try:
                        self.parse_draws(only_component=component, only_lines=lines)
                    except ValueError as exc:
                        message = str(exc)
                        self.variant_errors.setdefault(message, {"count": 0, "example": settings})["count"] += 1
                        if settings not in self.invalid_toggle_rules:
                            self.invalid_toggle_rules.append(settings)
                        continue
                    for draw in self.draws:
                        signature = self.draw_signature(draw)
                        if signature not in variants:
                            draw.active = False
                            draw.toggle_states = []
                            draw.variant_label = ", ".join(f"{k}={v}" for k, v in settings.items()
                                                           if v != defaults[k])
                            variants[signature] = draw
                        variant = variants[signature]
                        variant.active |= all(defaults[k] == v for k, v in settings.items())
                        if settings not in variant.toggle_states:
                            variant.toggle_states.append(settings)
        finally:
            self.variables = defaults
            self.include_disabled = True
        self.draws = list(variants.values())
        self.variant_evaluations = evaluations
        for message, detail in self.variant_errors.items():
            self.warnings.add(f"Skipped {detail['count']} invalid local toggle states: {message}")

    def source_buffer(self, resource, slot):


        expected = {"ib": "R32_UINT", "vb0": "R32G32B32_FLOAT", "vb1": "R8G8B8A8_SNORM",
                    "vb2": "R16G16_FLOAT", "vb3": "R8G8B8A8_UNORM", "vb4": "R8_UINT"}
        if slot not in expected:
            return resource
        if not hasattr(self, "_buffer_sources"):
            self._buffer_sources = {}
        if (resource, slot) in self._buffer_sources:
            return self._buffer_sources[resource, slot]
        def sources(name, seen):
            if name in seen:
                return set()
            info = self.resources.get(name, {})
            if "filename" in info:
                return {name}
            found = set()
            for section in self.sections.values():
                for line in section.lines:
                    match = re.fullmatch(r"([^ =]+)\s*=\s*(?:ref|copy)\s+(resource[^ ]+)", line.text, re.I)
                    if match and match[1].lower() == name:
                        found.update(sources(match[2].lower(), seen | {name}))
            return found
        info = self.resources.get(resource, {})
        if "filename" in info and "stride" in info:
            return resource
        candidates = sources(resource, set())
        paths = {self.resource_path(name).resolve() for name in candidates}
        typed = [name for name, data in self.resources.items() if "filename" in data and "stride" in data
                 and data.get("format", "").upper().removeprefix("DXGI_FORMAT_") == expected[slot]
                 and self.resource_path(name).resolve() in paths]
        if len(typed) != 1:
            raise ValueError(f"Cannot resolve exported {slot} source for {resource}: {typed}")
        self.warnings.add(f"{resource}: importing source buffer {typed[0]} before runtime GPU processing")
        self._buffer_sources[resource, slot] = typed[0]
        return typed[0]

    def resource_path(self, resource):
        resource = resource.lower()
        if resource in self._resource_paths:
            return self._resource_paths[resource]
        info = self.resources.get(resource)
        if info is None or "filename" not in info:
            raise ValueError(f"Missing file resource: {resource}")
        section = self.sections.get(resource)
        directory = section.source.parent if section and section.source else self.folder
        path = directory / info["filename"].strip('"').replace("\\", "/")
        self._resource_paths[resource] = path
        return path

    def texture_candidates(self, component):


        variables = dict(self.variables, object_detected=1, mod_enabled=1)
        cache_key = tuple((key, variables.get(key)) for key in self._texture_variables)
        if cache_key in self._texture_candidates_cache:
            return self._texture_candidates_cache[cache_key].get(component, ())
        result = {}

        def selected_lines(nodes):
            for node in nodes:
                if isinstance(node, Branch):
                    for expression, body in node.arms:
                        if expression is None or condition_value(expression, variables):
                            yield from selected_lines(body)
                            break
                else:
                    yield node

        for section in self._texture_sections:


            resource = ""
            for line in selected_lines(self.tree(section)):
                if line.key == 'this' and line.value:
                    resource = line.value_lower
            resource = re.sub(r"^(?:ref|copy)\s+", "", resource)
            if resource and resource != "null" and resource not in self.resources:
                raise ValueError(f"[{section.name}]: selected texture resource is not defined: {resource}")
            info = self.resources.get(resource, {})
            name = Path(info.get("filename", "").replace("\\", "/")).name
            match = re.match(r"components?-([0-9]+(?:-[0-9]+)*)(?=\s|\.|$)", name, re.I)
            if match:
                for member in set(map(int, match[1].split("-"))):
                    result.setdefault(member, []).append(resource)


        candidates = {member: tuple(reversed(dict.fromkeys(reversed(resources))))
                      for member, resources in result.items()}
        if len(self._texture_candidates_cache) >= 256:
            self._texture_candidates_cache.clear()
        self._texture_candidates_cache[cache_key] = candidates
        return candidates.get(component, ())

    def parse_draws(self, only_component=None, only_lines=None):
        runtime_lists = {"commandlistmergeskeleton",
                         "commandlistcleanupsharedresources"}
        user_variables = self.variables.keys() - {
            "mod_enabled", "state_id", "mod_id", "object_detected", "object_guid",
            "mesh_vertex_count", "shapekey_vertex_count", "required_wwmi_version"}

        for component, section in self._component_sections:
            if only_component is not None and component != only_component:
                continue
            candidates = self.texture_candidates(component)

            def walk(nodes, state, active=True, conditions=(), chain=()):
                for node in nodes:
                    if isinstance(node, Branch):
                        chosen = False
                        selected_state = None
                        previous = []
                        is_toggle_branch = self.include_disabled and any(
                            key in user_variables
                            for expression, _ in node.arms if expression is not None
                            for key in expression.variables)
                        for expression, body in node.arms:
                            def resource_present(name):
                                target = state["aliases"].get(name, name)
                                return target != "null" and (target != name or
                                       bool(self.resources.get(target, {}).get("filename")))
                            takes_arm = not chosen and (expression is None or condition_value(
                                expression, self.variables, resource_present))
                            chosen |= takes_arm
                            arm_condition = expression or "else: " + ", ".join(previous)


                            if takes_arm or (self.include_disabled and is_toggle_branch):
                                branch_state = {k: (v.copy() if isinstance(v, (dict, set)) else v) for k, v in state.items()}
                                walk(body, branch_state, active and takes_arm, conditions + (arm_condition,), chain)
                            if takes_arm:
                                selected_state = branch_state
                            if expression:
                                previous.append(expression)
                        if selected_state is not None:
                            state.update(selected_state)
                        continue
                    text = node.text
                    slot, target, source_slot, buffer_slot, resource_binding, checked, numbers, label = node.operands
                    if text.startswith(";"):
                        if label is not None:
                            state["label"] = label
                        continue
                    if "=" not in text:
                        continue
                    key, value, value_lower = node.key, node.value, node.value_lower
                    if key.startswith("$\\rabbitfx\\"):
                        api = rabbitfx_api()
                        parameter = key.rsplit("\\", 1)[-1]
                        if parameter in api["DEFAULTS"]:
                            parameters = state.setdefault("fx_parameters", dict(api["DEFAULTS"]))
                            parameters[parameter] = api["numeric_value"](value, self.variables, parameters)
                        else:
                            self.warnings.add(f"Unsupported RabbitFX setting at line {node.number}: {key}")
                    elif slot is not None:
                        value_lower = target
                        if source_slot is not None:
                            if source_slot not in state["slots"]:
                                raise ValueError(f"Line {node.number}: copy from unresolved slot {value_lower}")
                            value_lower = state["slots"][source_slot]
                        value_lower = state["aliases"].get(value_lower, value_lower)
                        if value_lower == "null":
                            state["slots"].pop(slot, None)
                        else:
                            state["slots"][slot] = value_lower
                    elif buffer_slot:
                        target = state["aliases"].get(target, target)
                        state["buffers"][key] = self.source_buffer(target, key)
                    elif resource_binding:
                        state["aliases"][key] = state["aliases"].get(target, target)
                        role = named_texture_role(key)
                        if role:
                            if target == "null":
                                state["named"].pop(key, None)
                            else:
                                state["named"][key] = {"role": role, "resource": state["aliases"][key],
                                                       "line": node.number}
                    elif key == "checktextureoverride":
                        if checked is not None:
                            state["checked"].add(checked)
                    elif key == "run":
                        if value_lower in {"commandlist\\rabbitfx\\run", "commandlist\\rabbitfx\\clear"}:
                            if "fx_parameters" not in state:
                                state["fx_parameters"] = dict(rabbitfx_api()["DEFAULTS"])
                            rabbitfx_api()["capture_rabbitfx"](state, value_lower, node.number)
                            continue
                        if value_lower in runtime_lists:
                            continue
                        if "\\" in value_lower and value_lower not in self.sections:
                            self.warnings.add(f"External game command not reproduced: {value}")
                            continue
                        if value_lower not in self.sections:
                            raise ValueError(f"Line {node.number}: unknown command list {value}")
                        if value_lower in chain or len(chain) >= 16:
                            raise ValueError(f"Recursive command list: {value}")
                        walk(self.tree(self.sections[value_lower]), state, active, conditions, chain + (value_lower,))
                    elif key == "drawindexed":
                        if numbers is None:
                            raise ValueError(f"Line {node.number}: unsupported drawindexed {value}")
                        count, start, base = numbers
                        if count and (active or self.include_disabled) and (only_lines is None or node.number in only_lines):
                            self.draws.append(Draw(component, state.get("label") or f"Component {component} Draw {len(self.draws)+1}",
                                                   count, start, base, node.number, active, conditions,
                                                   dict(state["slots"]), dict(state["buffers"]), candidates,
                                                   tuple(sorted(state["checked"])), dict(state["named"]),
                                                   rabbitfx=dict(state.get("rabbitfx", {}))))
                        state["label"] = None

            walk(self.tree(section), {"slots": {}, "buffers": {}, "label": None,
                                               "checked": set(), "aliases": {}, "named": {}})

    def read_buffer(self, resource, dtype, width, stride, format_name):
        info = self.resources.get(resource, {})
        if int(info.get("stride", -1)) != stride or info.get("format", "").upper().removeprefix("DXGI_FORMAT_") != format_name:
            raise ValueError(f"Unsupported {resource} layout: {info}")
        path = self.resource_path(resource)
        record_bytes = np.dtype(dtype).itemsize * width
        if path.stat().st_size % record_bytes:
            raise ValueError(f"Truncated {path.name}: size must be a multiple of {record_bytes}")
        data = np.fromfile(path, dtype=dtype).reshape(-1, width)
        if not len(data):
            raise ValueError(f"Empty buffer: {path}")
        return data

    def load_buffers(self):
        layouts = {
            "ib": ("<u4", 3, 12, "R32_UINT"),
            "vb0": ("<f4", 3, 12, "R32G32B32_FLOAT"),
            "vb1": ("i1", 8, 8, "R8G8B8A8_SNORM"),
            "vb2": ("u1", 16, 16, "R16G16_FLOAT"),
            "vb3": ("u1", 4, 4, "R8G8B8A8_UNORM"),
            "vb4": ("u1", 8, 8, "R8_UINT"),
        }
        binding = self.draws[0].buffers
        if set(layouts) - set(binding):
            raise ValueError(f"Missing mesh buffer bindings: {set(layouts) - set(binding)}")
        if any(any(draw.buffers.get(k) != binding[k] for k in layouts) for draw in self.draws):
            raise ValueError("This importer supports one shared WWMI buffer set per model")
        blend_stride = int(self.resources[binding["vb4"]].get("stride", -1))
        if blend_stride not in (8, 16):
            raise ValueError(f"Unsupported skin-weight stride: {blend_stride}; expected 8 or 16")


        self.influences_per_vertex = blend_stride // 2
        layouts["vb4"] = ("u1", blend_stride, blend_stride, "R8_UINT")
        for slot, layout in layouts.items():
            self.arrays[slot] = self.read_buffer(binding[slot], *layout)
        self.positions = self.arrays["vb0"]
        self.indices = self.arrays["ib"].ravel()
        count = len(self.positions)
        expected = self.variables.get("mesh_vertex_count")
        if expected is not None and count != expected:
            self.warnings.add(f"Stale INI vertex count {expected}; using {count} records from the validated buffers")
        for name, array in self.arrays.items():
            if name != "ib" and len(array) != count:
                raise ValueError(f"{name}: {len(array)} records, expected {count}")
        if not np.isfinite(self.positions).all():
            raise ValueError("Position buffer contains NaN or infinite values")

        raw = self.arrays["vb2"]
        halves = raw.view("<f2").reshape(-1, 8)
        self.uvs = [halves[:, offset:offset+2].astype(np.float32) for offset in (0, 4, 6)]
        for uv in self.uvs:
            if not np.isfinite(uv).all():
                raise ValueError("TexCoord buffer contains invalid UV values")
            if FLIP_UV_V:
                uv[:, 1] = 1 - uv[:, 1]
        self.color1 = np.ones((count, 4), dtype=np.float32)
        self.color1[:, :2] = raw.view("<u2").reshape(-1, 8)[:, 2:4] / 65535.0
        self.color1[:, 2] = 0
        self.normals = np.maximum(self.arrays["vb1"][:, 4:7].astype(np.float32) / 127, -1)
        self.bone_ids = self.arrays["vb4"][:, :self.influences_per_vertex].astype(np.int32)
        vg_resource = "resourceblendremapvertexvgbuffer"
        if vg_resource in self.resources:
            if self.resources[vg_resource].get("format", "").upper().removeprefix("DXGI_FORMAT_") != "R16_UINT":
                raise ValueError("Unsupported blend-remap vertex group layout")
            path = self.resource_path(vg_resource)
            if path.stat().st_size != count * self.influences_per_vertex * 2:
                raise ValueError("Blend-remap vertex groups do not match the skin-weight records")
            self.bone_ids = np.fromfile(path, "<u2").reshape(count, self.influences_per_vertex).astype(np.int32)
            self.warnings.add("Numeric vertex groups restored from the exported blend-remap vertex-VG buffer")
        self.load_shape_keys()
        checked_resources = set()
        for draw in self.draws:
            for resource in (set(draw.slots.values()) | set(draw.candidates)
                             | {e["resource"] for e in draw.named_bindings.values()}) - checked_resources:
                if not self.resource_path(resource).is_file():
                    raise FileNotFoundError(self.resource_path(resource))
                checked_resources.add(resource)
        if any(not d.slots and d.candidates for d in self.draws):
            self.warnings.add("Hash replacements are resolved through resource evidence and DDS metadata; inferred assignments are recorded in the texture report.")
        self.warnings.add("Skin weights use numeric merged bone IDs; the source contains no skeleton rest pose.")
        self.warnings.add("RabbitFX Run effects use the separate optional material adapter; other packed game masks remain labelled source data.")

    def load_shape_keys(self):
        names = ("resourceshapekeyoffsetbuffer", "resourceshapekeyvertexidbuffer", "resourceshapekeyvertexoffsetbuffer")
        present = [name in self.resources for name in names]
        if not any(present):
            return
        if not all(present):
            raise ValueError("Incomplete shape-key resource set")
        offsets = self.read_buffer(names[0], "<u4", 128, 16, "R32G32B32A32_UINT").ravel().astype(np.int64)
        ids = self.read_buffer(names[1], "<u4", 1, 4, "R32_UINT").ravel()
        deltas = self.read_buffer(names[2], "<f2", 6, 2, "R16_FLOAT")[:, :3].astype(np.float32)
        if len(offsets) % 128:
            raise ValueError("Expected shape-key tables containing 128 offsets per batch")
        if len(ids) != len(deltas):
            raise ValueError("Shape-key offset/vertex buffers disagree")
        if self.variables.get("shapekey_vertex_count", len(ids)) != len(ids):
            self.warnings.add(f"Stale INI shape-key vertex count; using {len(ids)} validated delta records")
        if np.any(ids >= len(self.positions)) or not np.isfinite(deltas).all():
            raise ValueError("Invalid shape-key vertex IDs or deltas")


        batch_count = len(offsets) // 128
        bases = [0] if batch_count == 1 else [int(self.variables[f"shapekey_vertex_offset_batch{i}"])
                                             for i in range(batch_count)]
        if bases[0] != 0 or any(a >= b for a, b in zip(bases, bases[1:])):
            raise ValueError("Invalid shape-key batch bases")
        ends = offsets.copy()
        for batch, base in enumerate(bases):
            start = batch * 128
            table = offsets[start:start+128].copy()
            limit = bases[batch+1] if batch+1 < batch_count else len(ids)
            if table[0] != 0 or np.any(np.diff(table) < 0) or base + table[-1] > limit:
                raise ValueError(f"Invalid shape-key offset table in batch {batch}")
            if batch_count > 1 and base + table[-1] != limit:
                raise ValueError(f"Shape-key batch {batch} end boundary does not match its vertex records")
            offsets[start:start+128] = table + base
            ends[start:start+128] = np.append(table[1:] + base, limit)
        for key, (start, end) in enumerate(zip(offsets, ends)):
            if batch_count > 1:
                if key % 128 == 127:
                    continue
                key = (key // 128) * 127 + key % 128
            if end > start:
                key_ids = ids[start:end]
                if len(np.unique(key_ids)) != len(key_ids):
                    raise ValueError(f"Shape key {key} has duplicate vertex IDs")
                self.shape_keys[key] = (key_ids, deltas[start:end])

    def validate_draws(self):
        checked = set()
        for draw in self.draws:
            signature = self.geometry_signature(draw)
            if signature in checked:
                continue
            if draw.start < 0 or draw.count % 3 or draw.start + draw.count > len(self.indices):
                raise ValueError(f"Line {draw.line} ({draw.name}): invalid triangle draw range")
            indices = self.indices[draw.start:draw.start+draw.count].astype(np.int64) + draw.base_vertex
            if np.any(indices < 0) or np.any(indices >= len(self.positions)):
                raise ValueError(f"Line {draw.line} ({draw.name}): vertex index outside Position buffer")
            checked.add(signature)

    def geometry(self, draw):
        indices = self.indices[draw.start:draw.start+draw.count].astype(np.int64) + draw.base_vertex


        if len(indices) and (span := int(indices.max() - indices.min()) + 1) <= max(4096, len(indices) * 4):
            first = int(indices.min())
            offsets = indices - first
            lookup = np.zeros(span, dtype=np.int32)
            lookup[offsets] = 1
            used = np.flatnonzero(lookup)
            lookup[used] = np.arange(len(used), dtype=np.int32)
            vertex_ids, remapped = used + first, lookup[offsets]
        else:
            vertex_ids, remapped = np.unique(indices, return_inverse=True)
        faces = remapped.reshape(-1, 3)
        if FLIP_WINDING:
            faces = faces[:, [0, 2, 1]]
        return vertex_ids, np.ascontiguousarray(faces, dtype=np.int32)

    def summary(self):
        geometries = {self.geometry_signature(d): d for d in self.draws}
        return {"folder": str(self.folder), "ini": str(self.ini_path), "vertices_in_buffers": len(self.positions),
                "unique_meshes": len(geometries),
                "unique_mesh_triangles": sum(d.count // 3 for d in geometries.values()),
                "indices_in_buffer": len(self.indices), "draws": len(self.draws),
                "active_draws": sum(d.active for d in self.draws),
                "triangles": sum(d.count // 3 for d in self.draws),
                "active_triangles": sum(d.count // 3 for d in self.draws if d.active),
                "shape_keys": len(self.shape_keys), "phair": self.variables.get("phair"),
                "influences_per_vertex": self.influences_per_vertex,
                "warnings": sorted(self.warnings)}


class BlenderImporter:
    def __init__(self, model):
        import bpy
        self.bpy = bpy
        self.model = model
        self.created = {key: [] for key in ("objects", "meshes", "materials", "node_groups", "images", "collections", "texts")}
        self.materials = {}
        self.images = {}
        self.samples = {}
        self.layout_features = {}

    def track(self, kind, data):
        self.created[kind].append(data)
        return data

    def image(self, resource, colorspace):
        path = str(self.model.resource_path(resource).resolve())
        key = (path, colorspace)
        if key not in self.images:

            image = self.track("images", self.bpy.data.images.load(path, check_existing=False))
            if Path(path).suffix.lower() == ".dds":


                image.alpha_mode = "CHANNEL_PACKED"
            image.colorspace_settings.name = colorspace
            self.images[key] = image
        return self.images[key]

    def sample_rgba(self, resource, size=48):

        digest = self.model.texture_resolver.info[resource]["digest"]
        key = (digest, size)
        if key not in self.samples:
            image = self.bpy.data.images.load(str(self.model.resource_path(resource)), check_existing=False)
            try:
                if self.model.resource_path(resource).suffix.lower() == ".dds":
                    image.alpha_mode = "CHANNEL_PACKED"
                image.colorspace_settings.name = "Non-Color"
                image.scale(size, size)
                data = np.empty(size * size * 4, dtype=np.float32)
                image.pixels.foreach_get(data)
                self.samples[key] = data.reshape(-1, 4).copy()
            finally:
                self.bpy.data.images.remove(image)
        return self.samples[key]

    def sample_rgb(self, resource, size=48):
        return self.sample_rgba(resource, size)[:, :3]

    def prepare_texture_profiles(self):


        resolver = self.model.texture_resolver
        candidates = {r for p in resolver.profiles for r in p["resources"]
                      if p["assignments"].get("base_color", {}).get("method")
                      not in {"explicit_binding", "named_resource_binding"}}
        for resource in sorted(candidates):
            info = resolver.info[resource]
            if resolver.roles[resource] or min(info["width"], info["height"]) < 64:
                continue
            rgb = self.sample_rgb(resource)
            rg = rgb[:, :2]
            role, evidence = None, None
            if (info["srgb"] is False and np.max(np.abs(rg.mean(axis=0) - 0.5)) < 0.04
                    and np.mean(np.max(np.abs(rg - 0.5), axis=1) < 0.14) > 0.70):
                role = "normal"
                evidence = "Image-content inference: linear DDS with centered tangent-normal RG channels"
            elif (np.mean(rgb[:, 2] < 0.02) > 0.98
                    and np.mean((rgb[:, 0] < 0.05) | (rgb[:, 0] > 0.95)) > 0.70
                    and rgb[:, 0].std() > 0.15
                    and np.mean(np.ptp(rgb, axis=1) > 0.2) > 0.80):
                role = "mask"
                evidence = "Image-content inference: packed red regions with an empty blue channel"
            if role:
                resolver.roles[resource].add(role)
                resolver.evidence[resource].append(evidence)
                info["content_inferred_role"] = role
        resolver.profiles = [resolver.resolve(draw) for draw in self.model.draws]
        for profile in resolver.profiles:
            self.refine_shared_normal_layout(profile)

    def layout_feature(self, resource, size, channels):

        digest = self.model.texture_resolver.info[resource]["digest"]
        key = (digest, size, channels)
        if key not in self.layout_features:
            rgb = self.sample_rgb(resource, size).reshape(size, size, 3)[:, :, :channels]
            dx = np.diff(rgb, axis=1)[:-1]
            dy = np.diff(rgb, axis=0)[:, :-1]
            edge = np.sqrt(np.sum(dx * dx + dy * dy, axis=-1)).ravel()
            edge -= edge.mean()
            length = np.linalg.norm(edge)
            self.layout_features[key] = edge / length if length > 1e-8 else np.zeros_like(edge)
        return self.layout_features[key]

    def layout_scores(self, normal, color):
        return [float(np.dot(self.layout_feature(normal, size, 2), self.layout_feature(color, size, 3)))
                for size in (64, 128, 256)]

    def refine_shared_normal_layout(self, profile):


        resolver = self.model.texture_resolver
        assignments = profile["assignments"]
        normal = assignments.get("normal")
        base = assignments.get("base_color")
        if (not normal or normal["method"] not in {"image_content_inference", "resource_cross_reference"}
                or len(resolver.membership(normal["resource"])) <= 1):
            return


        if normal["method"] == "resource_cross_reference" and (not base or base["method"] not in {
                "explicit_binding", "named_resource_binding"}):
            return
        resource = normal["resource"]
        dimensions = resolver.info[resource]
        candidates = []
        for candidate in profile["resources"]:
            info = resolver.info[candidate]
            if (resolver.roles[candidate] <= {"base_color", "secondary_color"}
                    and (info["srgb"] is True or "base_color" in resolver.roles[candidate])
                    and info["width"] * dimensions["height"] == info["height"] * dimensions["width"]):
                candidates.append(candidate)
        scores = {candidate: self.layout_scores(resource, candidate) for candidate in candidates}


        protected = base and (base["method"] in {"explicit_binding", "named_resource_binding"}
                              or resolver.membership(base["resource"]) == (profile["component"],))
        if scores and not protected:
            ranked = sorted(scores, key=lambda r: min(scores[r]), reverse=True)
            best = ranked[0]
            margins = [scores[best][i] - max((scores[r][i] for r in ranked[1:]), default=0)
                       for i in range(3)]
            if min(scores[best]) >= 0.15 and min(margins) >= 0.14:
                assignments["base_color"] = {
                    "resource": best, "method": "image_layout_inference",
                    "evidence": [f"Referenced color atlas matches shared normal {resource} at 64, 128 and 256 pixels",
                                 f"Spatial edge correlations: {scores[best]}; candidate margins: {margins}"]}
                base = assignments["base_color"]
        if base:
            selected_scores = scores.get(base["resource"])
            if selected_scores is None:
                selected_scores = self.layout_scores(resource, base["resource"])
            profile["shared_normal_layout"] = {"normal": resource, "base_color": base["resource"],
                                               "scores": selected_scores}
            if max(selected_scores) < 0.14:
                del assignments["normal"]
                profile["notes"].append(
                    f"Unlinked inferred shared normal {resource}: its atlas layout does not match the selected color texture. "
                    "The source image is retained as auxiliary data.")

    def refine_profile(self, profile):
        existing = profile["assignments"].get("base_color")
        if (existing and existing["method"] in {"explicit_binding", "named_resource_binding"}) or not profile["color_candidates"]:
            return
        normal = profile["assignments"].get("normal")
        if not normal:
            return
        resolver = self.model.texture_resolver
        digest = resolver.info[normal["resource"]]["digest"]
        anchors = {p["assignments"]["base_color"]["resource"] for p in resolver.profiles
                   if "normal" in p["assignments"] and "base_color" in p["assignments"]
                   and p["assignments"]["base_color"]["method"] == "explicit_binding"
                   and resolver.info[p["assignments"]["normal"]["resource"]]["digest"] == digest}
        if not anchors:
            return
        errors = {}
        for resource in profile["color_candidates"]:
            errors[resource] = min(float(np.mean(np.abs(self.sample_rgb(resource) - self.sample_rgb(anchor))))
                                   for anchor in anchors)
        ranked = sorted(errors, key=errors.get)

        if errors[ranked[0]] < 0.04 and (len(ranked) == 1 or errors[ranked[1]] > errors[ranked[0]] * 3 + 0.01):
            profile["assignments"]["base_color"] = {
                "resource": ranked[0], "method": "texture_family_inference",
                "evidence": ["Shares byte-identical normal map with an explicitly bound material",
                             f"RGB thumbnail error {errors[ranked[0]]:.6f} against {sorted(anchors)}"]}
            profile["notes"].append("Color candidate resolved by matching texture-family image content (inference)")

    def refine_packed_decal(self, profile):


        base = profile["assignments"].get("base_color")
        if not base or base["method"] in {"explicit_binding", "named_resource_binding"}:
            return
        if "opacity" in profile["assignments"] or profile.get("decal"):
            return
        resource = base["resource"]
        rgba = self.sample_rgba(resource, 128)
        rgb, alpha = rgba[:, :3], rgba[:, 3]


        intensity = rgb[:, 0]
        border = intensity.reshape(128, 128)
        border = np.concatenate((border[0], border[-1], border[:, 0], border[:, -1]))
        monochrome = np.max(np.ptp(rgb, axis=1)) < 0.025
        coverage_rgb = float(np.mean(intensity > 0.5))
        nonblack = intensity > 0.02
        if (monochrome and np.min(alpha) > 0.98
                and not any(role in profile['assignments'] for role in ('normal', 'mask', 'effect'))
                and 0.85 < np.mean(~nonblack) < 0.99
                and 0.01 < coverage_rgb < 0.15
                and np.mean(intensity[nonblack] > 0.9) > 0.25
                and np.max(border) < 0.02):
            profile['decal'] = {
                'resource': resource, 'method': 'grayscale_decal_inference',
                'coverage_channel': 'Red', 'ink_color': list(DECAL_INK_COLOR),
                'coverage_fraction': coverage_rgb,
                'evidence': [
                    'Sparse high-contrast grayscale silhouette, black border, opaque alpha',
                    'Equivalent RGB channels used as coverage; source alpha contains no silhouette',
                    'No shader tint supplied: editable neutral ink is an importer fallback']}
            profile['notes'].append('Grayscale decal mask uses RGB coverage and editable neutral ink (inference)')
            return
        outside, inside = alpha < 0.02, alpha > 0.9
        coverage = float(np.mean(alpha > 0.5))
        if not (0.65 < outside.mean() < 0.99 and 0.01 < coverage < 0.30 and inside.mean() > 0.01):
            return


        background = rgb[alpha < 0.000001]
        if len(background) < len(alpha) * 0.65:
            return
        red_marker = (background[:, 0] > 0.98) & (np.max(background[:, 1:], axis=1) < 0.02)
        if red_marker.mean() < 0.99:
            return


        if min(np.ptp(rgb[inside, 1:], axis=0)) < 0.20:
            return
        profile["decal"] = {"resource": resource, "method": "packed_decal_inference",
                            "coverage_channel": "Alpha", "ink_color": list(DECAL_INK_COLOR),
                            "coverage_fraction": coverage,
                            "evidence": ["Near-uniform red RGB in zero-alpha background; sparse opaque multichannel silhouette",
                                         "Use alpha as decal coverage instead of displaying packed RGB channels",
                                         "No shader tint supplied: editable neutral ink is an importer fallback"]}
        profile["notes"].append("Packed decal data uses alpha coverage and editable neutral ink (inference)")

    def material(self, draw):
        profile = self.model.texture_resolver.profile(draw)
        self.refine_profile(profile)
        self.refine_packed_decal(profile)
        assignments = profile["assignments"]
        base_choice = assignments.get("base_color")
        if STRICT_TEXTURES and (not base_choice or base_choice["method"] not in {"explicit_binding", "named_resource_binding"}):
            raise ValueError(f"{draw.name}: no explicit base-color binding; strict texture import stopped")
        key = (tuple(sorted((role, entry["resource"]) for role, entry in assignments.items())), tuple(profile["resources"]), json.dumps(draw.rabbitfx, sort_keys=True),
               json.dumps(profile.get("decal"), sort_keys=True))
        if key in self.materials:
            return self.materials[key]
        base = base_choice["resource"] if base_choice else None
        name = self.model.resource_path(base).stem if base else f"Component {draw.component} unresolved"
        mat = self.track("materials", self.bpy.data.materials.new(f"{name} [v1.1]"))
        mat.use_nodes = True
        nodes, links = mat.node_tree.nodes, mat.node_tree.links
        nodes.clear()
        output = nodes.new("ShaderNodeOutputMaterial")
        output.location = (600, 200)
        shader = nodes.new("ShaderNodeBsdfPrincipled")
        shader.location = (250, 200)

        shader.inputs["Base Color"].default_value = (1, 0, 1, 1)
        shader.inputs["Roughness"].default_value = 0.6
        links.new(shader.outputs["BSDF"], output.inputs["Surface"])
        uv = nodes.new("ShaderNodeUVMap")
        uv.uv_map = "TEXCOORD.xy"
        uv.location = (-650, 200)
        roles_by_resource = {}
        for role, entry in assignments.items():
            roles_by_resource.setdefault(entry["resource"], []).append(role)
        alternatives = set(base_choice.get("alternative_resources", ())) if base_choice else set()
        texture_nodes = {}
        for index, resource in enumerate(profile["resources"]):
            roles = (["decal coverage / packed data"] if profile.get("decal", {}).get("resource") == resource else
                     ["base_color alternative"] if resource in alternatives else
                     roles_by_resource.get(resource) or sorted(self.model.texture_resolver.roles[resource])
                     or ["auxiliary / role unresolved"])
            node = nodes.new("ShaderNodeTexImage")
            node.location = (-350, 200 - index * 300)
            slots = [f"ps-t{s}" for s, r in draw.slots.items() if r == resource]
            node.label = " / ".join(roles) + (" [" + ", ".join(slots) + "]" if slots else "")
            node.label += " | " + self.model.resource_path(resource).name
            node.name = resource
            color = resource in alternatives or any(r in {"base_color", "secondary_color", "effect"} for r in roles)
            node.image = self.image(resource, "sRGB" if color else "Non-Color")
            links.new(uv.outputs["UV"], node.inputs["Vector"])
            texture_nodes[resource] = node
            if resource == base:
                links.new(node.outputs["Color"], shader.inputs["Base Color"])
        if profile.get("decal"):
            decal = profile["decal"]
            image_node = texture_nodes[decal["resource"]]
            image_node.extension = "CLIP"
            for link in list(shader.inputs["Base Color"].links):
                links.remove(link)
            ink = nodes.new("ShaderNodeRGB")
            ink.name = "Decal Ink"
            ink.label = "Decal ink — editable color"
            ink.outputs[0].default_value = decal["ink_color"]
            ink.location = (-100, 450)
            links.new(ink.outputs[0], shader.inputs["Base Color"])
            if decal['coverage_channel'] == 'Red':
                coverage = nodes.new('ShaderNodeSeparateColor')
                coverage.name = 'Decal Coverage'
                coverage.label = 'Grayscale mask coverage (linear data)'
                coverage.location = (-100, 50)
                links.new(image_node.outputs['Color'], coverage.inputs['Color'])
                links.new(coverage.outputs['Red'], shader.inputs['Alpha'])
            else:
                links.new(image_node.outputs["Alpha"], shader.inputs["Alpha"])
            shader.inputs["Roughness"].default_value = 0.8
            if hasattr(mat, "use_transparent_shadow"):
                mat.use_transparent_shadow = True
            mat["wwmi_decal"] = json.dumps(decal)
        if base:
            for node in nodes:
                node.select = False
            texture_nodes[base].select = True
            nodes.active = texture_nodes[base]
        normal_choice = assignments.get("normal")
        if normal_choice:
            texture = texture_nodes[normal_choice["resource"]]
            split = nodes.new("ShaderNodeSeparateXYZ")
            split.label = "Packed normal RG (B/A are game material data)"
            split.location = (0, -250)
            links.new(texture.outputs["Color"], split.inputs["Vector"])
            combine = nodes.new("ShaderNodeCombineXYZ")
            combine.location = (200, -250)


            combine.inputs["Z"].default_value = 1.0
            links.new(split.outputs["X"], combine.inputs["X"])
            flip = nodes.new("ShaderNodeMath")
            flip.operation = "SUBTRACT"
            flip.inputs[0].default_value = 1
            flip.label = "DirectX tangent Y to Blender"
            flip.location = (0, -450)
            links.new(split.outputs["Y"], flip.inputs[1])
            links.new(flip.outputs[0], combine.inputs["Y"])
            normal = nodes.new("ShaderNodeNormalMap")
            normal.uv_map = "TEXCOORD.xy"
            normal.location = (400, -250)
            links.new(combine.outputs["Vector"], normal.inputs["Color"])
            links.new(normal.outputs["Normal"], shader.inputs["Normal"])
        if "opacity" in assignments:
            opacity = texture_nodes[assignments["opacity"]["resource"]]
            links.new(opacity.outputs["Color"], shader.inputs["Alpha"])


        if shader.inputs["Alpha"].is_linked and hasattr(mat, "blend_method"):
            mat.blend_method = "HASHED"
        for role in ("mask", "effect"):
            if role not in assignments:
                continue
            texture = texture_nodes[assignments[role]["resource"]]
            split = nodes.new("ShaderNodeSeparateColor")
            split.mode = "RGB"
            split.label = f"{role.upper()} channels - game shader data, not PBR substitutes"
            split.location = (-50, -800 if role == "mask" else -1050)
            links.new(texture.outputs["Color"], split.inputs["Color"])
        mat["wwmi_ps_slots"] = json.dumps(draw.slots, sort_keys=True)
        mat["wwmi_texture_resolution"] = json.dumps(profile, sort_keys=True)
        mat["wwmi_shader_note"] = "Color/normal reconstruction. Packed masks and game effects preserve their source channels; exact game shading is not reproduced."
        wuwa_appearance(self, mat)
        if draw.rabbitfx:
            rabbit_fx_effects(self, mat, draw)
        self.materials[key] = mat
        return mat

    def weights(self, obj, vertex_ids):
        blend = self.model.arrays["vb4"][vertex_ids]


        influences = self.model.influences_per_vertex
        bone_ids = self.model.bone_ids[vertex_ids]
        weights = blend[:, influences:]
        local, columns = np.nonzero(weights)
        if not len(local):
            return
        radix = int(bone_ids.max()) + 1
        encoded = local.astype(np.int64) * radix + bone_ids[local, columns]
        order = np.argsort(encoded, kind="stable")
        encoded = encoded[order]
        starts = np.r_[0, np.flatnonzero(np.diff(encoded)) + 1]
        summed = np.add.reduceat(weights[local, columns][order].astype(np.int64), starts)
        pairs = encoded[starts]

        weight_radix = influences * 255 + 1
        buckets = (pairs % radix) * weight_radix + summed
        order = np.argsort(buckets, kind="stable")
        buckets, vertices = buckets[order], (pairs // radix)[order]
        starts = np.r_[0, np.flatnonzero(np.diff(buckets)) + 1]
        ends = np.r_[starts[1:], len(buckets)]
        groups = {int(bone): obj.vertex_groups.new(name=str(bone))
                  for bone in np.unique(pairs % radix)}
        for start, end in zip(starts, ends):
            bone, weight = divmod(int(buckets[start]), weight_radix)
            groups[bone].add(vertices[start:end].tolist(), weight / 255.0, "REPLACE")

    def shape_keys(self, obj, vertex_ids):
        basis = self.model.positions[vertex_ids]
        lookup = None
        if len(vertex_ids) and (span := int(vertex_ids[-1] - vertex_ids[0]) + 1) <= max(4096, len(vertex_ids) * 8):
            first = int(vertex_ids[0])
            lookup = np.full(span, -1, dtype=np.int32)
            lookup[vertex_ids - first] = np.arange(len(vertex_ids), dtype=np.int32)
        for key_id, (source_ids, deltas) in self.model.shape_keys.items():
            if lookup is None:
                local = np.searchsorted(vertex_ids, source_ids)
                good = local < len(vertex_ids)
                good[good] &= vertex_ids[local[good]] == source_ids[good]
            else:
                offsets = source_ids.astype(np.int64) - first
                good = (offsets >= 0) & (offsets < len(lookup))
                local = np.full(len(source_ids), -1, dtype=np.int32)
                local[good] = lookup[offsets[good]]
                good &= local >= 0
            if not good.any():
                continue
            if obj.data.shape_keys is None:
                obj.shape_key_add(name="Basis")
                obj.data.shape_keys.use_relative = True
            key = obj.shape_key_add(name=f"Deform {key_id}", from_mix=False)
            coordinates = basis.copy()
            coordinates[local[good]] += deltas[good]
            key.data.foreach_set("co", coordinates.ravel())
            key.value = 0.0

    def mesh_object(self, draw, collection, parent):
        bpy = self.bpy
        vertex_ids, faces = self.model.geometry(draw)
        name = draw.name
        mesh = self.track("meshes", bpy.data.meshes.new(name))
        obj = self.track("objects", bpy.data.objects.new(name, mesh))
        mesh.vertices.add(len(vertex_ids))
        mesh.vertices.foreach_set("co", self.model.positions[vertex_ids].ravel())
        mesh.loops.add(faces.size)
        mesh.loops.foreach_set("vertex_index", faces.ravel())
        mesh.polygons.add(len(faces))
        mesh.polygons.foreach_set("loop_start", np.arange(0, faces.size, 3, dtype=np.int32))
        mesh.polygons.foreach_set("loop_total", np.full(len(faces), 3, dtype=np.int32))
        mesh.polygons.foreach_set("use_smooth", np.ones(len(faces), dtype=bool))
        mesh.update(calc_edges=True)
        loop_ids = vertex_ids[faces.ravel()]
        for index, uv_data in enumerate(self.model.uvs):
            name = f"TEXCOORD{index or ''}.xy"
            layer = mesh.uv_layers.new(name=name, do_init=False)
            if hasattr(layer, 'uv'):
                layer.uv.foreach_set("vector", uv_data[loop_ids].ravel())
            else:
                layer.data.foreach_set("uv", uv_data[loop_ids].ravel())
        mesh.uv_layers.active_index = 0
        mesh.uv_layers[0].active_render = True
        for name, data in (("COLOR", self.model.arrays["vb3"][loop_ids].astype(np.float32) / 255),
                           ("COLOR1", self.model.color1[loop_ids])):
            attr = mesh.color_attributes.new(name=name, type="FLOAT_COLOR", domain="CORNER")
            attr.data.foreach_set("color", data.ravel())
        original_ids = mesh.attributes.new(name="wwmi_vertex_id", type="INT", domain="POINT")
        original_ids.data.foreach_set("value", vertex_ids.astype(np.int32))

        vectors = self.model.arrays["vb1"][vertex_ids].astype(np.float32) / 127
        tangent = mesh.attributes.new(name="wwmi_tangent", type="FLOAT_VECTOR", domain="POINT")
        tangent.data.foreach_set("vector", np.maximum(vectors[:, :3], -1).ravel())
        sign = mesh.attributes.new(name="wwmi_normal_w", type="FLOAT", domain="POINT")
        sign.data.foreach_set("value", np.maximum(vectors[:, 7], -1))
        if hasattr(mesh, "use_auto_smooth"):
            mesh.use_auto_smooth = True
        mesh.normals_split_custom_set_from_vertices(self.model.normals[vertex_ids].tolist())
        if IMPORT_WEIGHTS:
            self.weights(obj, vertex_ids)
        if IMPORT_SHAPE_KEYS:
            self.shape_keys(obj, vertex_ids)
        mesh.materials.append(self.material(draw))
        obj["wwmi_component"] = draw.component
        obj["wwmi_draw_start"] = draw.start
        obj["wwmi_draw_count"] = draw.count
        obj["wwmi_base_vertex"] = draw.base_vertex
        obj["wwmi_ini_line"] = draw.line
        obj["wwmi_conditions"] = json.dumps(draw.conditions)
        obj["wwmi_toggle_states"] = json.dumps(draw.toggle_states)
        obj["wwmi_default_visible"] = draw.active


        collection.objects.link(obj)
        obj.parent = parent
        obj.hide_render = not draw.active
        obj.hide_set(not draw.active)
        return obj

    def rollback(self):

        for kind in ("objects", "meshes", "materials", "node_groups", "images", "collections", "texts"):
            library = getattr(self.bpy.data, kind)
            for data in reversed(self.created[kind]):
                library.remove(data, do_unlink=True)

    def run(self):
        bpy = self.bpy
        if bpy.app.version < (3, 6, 0):
            raise RuntimeError("Blender 3.6 or newer is required")
        collection = self.track("collections", bpy.data.collections.new(f"{self.model.folder.name} [v1.1]"))
        bpy.context.scene.collection.children.link(collection)
        self.import_collection = collection
        try:
            self.prepare_texture_profiles()
            root = self.track("objects", bpy.data.objects.new(self.model.folder.name, None))
            collection.objects.link(root)
            root.scale = (SCALE,) * 3
            root["wwmi_import_version"] = "1.1"
            root["wwmi_source"] = str(self.model.folder)
            root["wwmi_import_summary"] = json.dumps(self.model.summary())
            root["wwmi_toggle_options"] = json.dumps(self.model.toggle_options)
            root["wwmi_factorized_states"] = self.model.factorized_states
            root["wwmi_invalid_toggle_rules"] = json.dumps(self.model.invalid_toggle_rules)
            settings = {key: self.model.variables[key] for key in self.model.toggle_options}
            root["wwmi_toggle_values"] = json.dumps(settings)
            states = {}
            groups = {}
            for draw in self.model.draws:
                geometry = self.model.geometry_signature(draw)
                groups.setdefault(geometry, []).append(draw)
                for state in draw.toggle_states:
                    states[tuple(sorted(state.items()))] = state
            root["wwmi_valid_toggle_states"] = json.dumps(list(states.values()))
            for i, variants in enumerate(groups.values(), 1):
                draw = next((d for d in variants if d.active), variants[0])
                LOG.info("[%d/%d] %s (%s material states)", i, len(groups), draw.name, len(variants))

                obj = self.mesh_object(draw, collection, root)
                entries = []
                object_states = {}
                for variant in variants:
                    mat = self.material(variant)
                    slot = next((j for j, existing in enumerate(obj.data.materials) if existing == mat), None)
                    if slot is None:
                        slot = len(obj.data.materials)
                        obj.data.materials.append(mat)
                    entries.append({"slot": slot, "states": variant.toggle_states,
                                    "ini_line": variant.line, "variant": variant.variant_label})
                    for state in variant.toggle_states:
                        object_states[tuple(sorted(state.items()))] = state
                obj["wwmi_material_variants"] = json.dumps(entries)
                obj["wwmi_toggle_states"] = json.dumps(list(object_states.values()))
            root["wwmi_mesh_count"] = len(groups)
            controls = self.track("texts", bpy.data.texts.new("WWMI v1.1 Controls.py"))
            controls.write(CONTROLS_SOURCE)
            root["wwmi_controls_script"] = controls.name
            self.controls = {"__name__": "wwmi_controls_v11"}
            exec(compile(CONTROLS_SOURCE, controls.name, "exec", dont_inherit=True), self.controls)
            self.controls["apply_state"](root, {})
            if hasattr(self, "wuwa_collection"):
                self.wuwa_collection['wuwa_root'] = root
                center = (self.model.positions.min(axis=0) + self.model.positions.max(axis=0)) * .5
                self.wuwa_collection['model_center'] = center.tolist()
                root['wwmi_wuwa_collection'] = self.wuwa_collection.name
                root['wwmi_wuwa_controls'] = self.wuwa_collection['wuwa_controls_script']
                self.wuwa_runtime['wuwa_sync']()
            if hasattr(self, "rabbitfx_collection"):
                root["wwmi_rabbitfx_collection"] = self.rabbitfx_collection.name
                root["wwmi_rabbitfx_controls"] = self.rabbitfx_collection["rabbitfx_controls_script"]
                self.rabbitfx_runtime["rabbitfx_sync"]()

            report = self.track("texts", bpy.data.texts.new("WWMI v1.1 Texture Resolution.json"))
            report.write(json.dumps(self.model.texture_resolver.report(), indent=2))
            root["wwmi_texture_report"] = report.name
            bpy.context.view_layer.update()
        except Exception:
            self.rollback()
            raise
        return root


def main(argv=None):
    global STRICT_TEXTURES
    parser = argparse.ArgumentParser(description="WuWa Mod Importer 1.1")
    parser.add_argument("--mod-folder", type=Path, default=MOD_FOLDER,
                        help="Mod directory (searched recursively) or an explicit INI file")
    parser.add_argument("--validate", action="store_true", help="Check all source data without creating Blender objects")
    parser.add_argument("--phair", type=int, choices=(0, 1), default=None)
    parser.add_argument("--active-only", action="store_true", help="Omit disabled toggle parts instead of importing them hidden")
    parser.add_argument("--strict-textures", action="store_true", default=STRICT_TEXTURES)
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="Set any numeric INI toggle")
    parser.add_argument("--texture-report", type=Path, help="Write texture references and evidence as JSON")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    started = time.perf_counter()
    toggles = dict(TOGGLE_VALUES)
    STRICT_TEXTURES = args.strict_textures
    for setting in args.set:
        key, value = setting.split("=", 1)
        toggles[key] = float(value) if "." in value else int(value)
    if args.phair is not None:
        toggles["phair"] = args.phair
    model = Model(args.mod_folder, toggles, IMPORT_DISABLED_PARTS and not args.active_only)
    LOG.info("Source validation passed:\n%s", json.dumps(model.summary(), indent=2))
    if not args.validate:
        root = BlenderImporter(model).run()
        LOG.info("Import complete: %s (%.1fs)", root.name, time.perf_counter() - started)
    if args.texture_report:
        args.texture_report.write_text(json.dumps(model.texture_resolver.report(), indent=2), encoding="utf-8")
    return model


if __name__ == "__main__":
    if "bpy" in sys.modules:

        arguments = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    else:
        arguments = sys.argv[1:]
    main(arguments)
