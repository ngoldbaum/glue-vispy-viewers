# This file implements a MultiVolumeVisual class that can be used to show
# multiple volumes simultaneously. It is derived from the original VolumeVisual
# class in vispy.visuals.volume, which is releaed under a BSD license included
# here:
#
# ===========================================================================
# Vispy is licensed under the terms of the (new) BSD license:
#
# Copyright (c) 2015, authors of Vispy
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# * Redistributions of source code must retain the above copyright
#   notice, this list of conditions and the following disclaimer.
# * Redistributions in binary form must reproduce the above copyright
#   notice, this list of conditions and the following disclaimer in the
#   documentation and/or other materials provided with the distribution.
# * Neither the name of Vispy Development Team nor the names of its
#   contributors may be used to endorse or promote products
#   derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
# IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
# TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
# PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER
# OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
# PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
# ===========================================================================
#
# This modified version is released under the BSD license given in the LICENSE
# file in this repository.

from __future__ import absolute_import, division, print_function

from distutils.version import LooseVersion
from collections import defaultdict

import numpy as np
from glue.external import six
from glue.utils import iterate_chunks

from ..extern.vispy.gloo import Texture3D, TextureEmulated3D, VertexBuffer, IndexBuffer
from ..extern.vispy.visuals import VolumeVisual, Visual
from ..extern.vispy.visuals.shaders import Function
from ..extern.vispy.color import get_colormap, Color
from ..extern.vispy.scene.visuals import create_visual_node

from .shaders import get_frag_shader, VERT_SHADER

NUMPY_LT_1_13 = LooseVersion(np.__version__) < LooseVersion('1.13')


class MultiVolumeVisual(VolumeVisual):
    """
    Displays multiple 3D volumes simultaneously.

    Parameters
    ----------
    volumes : list of tuples
        The volumes to show. Each tuple should contain three elements: the data
        array, the clim values, and the colormap to use. The clim values should
        be either a 2-element tuple, or None.
    relative_step_size : float
        The relative step size to step through the volume. Default 0.8.
        Increase to e.g. 1.5 to increase performance, at the cost of
        quality.
    emulate_texture : bool
        Use 2D textures to emulate a 3D texture. OpenGL ES 2.0 compatible,
        but has lower performance on desktop platforms.
    n_volume_max : int
        Absolute maximum number of volumes that can be shown.
    """

    def __init__(self, n_volume_max=10, relative_step_size=0.8,
                 emulate_texture=False, bgcolor='white', resolution=256):

        # Choose texture class
        tex_cls = TextureEmulated3D if emulate_texture else Texture3D

        self._n_volume_max = n_volume_max
        self._vol_shape = (resolution, resolution, resolution)
        self._need_vertex_update = True
        self._data_slice = None

        self.resolution = resolution

        # We deliberately don't use super here because we don't want to call
        # VolumeVisual.__init__
        Visual.__init__(self, vcode=VERT_SHADER, fcode="")

        self.volumes = defaultdict(dict)

        self._clip_data = False
        self._update_shader()

        # Create gloo objects
        self._vertices = VertexBuffer()
        self._texcoord = VertexBuffer(
            np.array([[0, 0, 0],
                      [1, 0, 0],
                      [0, 1, 0],
                      [1, 1, 0],
                      [0, 0, 1],
                      [1, 0, 1],
                      [0, 1, 1],
                      [1, 1, 1]], dtype=np.float32))

        self.textures = []
        for i in range(n_volume_max):

            # Set up texture object
            self.textures.append(tex_cls(self._vol_shape, interpolation='linear',
                                         wrapping='clamp_to_edge'))

            # Pass texture object and default colormap to shader program
            self.shared_program['u_volumetex_{0}'.format(i)] = self.textures[i]

            # Make sure all textures are disbaled
            self.shared_program['u_enabled_{0}'.format(i)] = 0
            self.shared_program['u_weight_{0}'.format(i)] = 1

        self.shared_program['a_position'] = self._vertices
        self.shared_program['a_texcoord'] = self._texcoord
        self.shared_program['u_shape'] = self._vol_shape[::-1]

        self.shared_program['u_clip_min'] = [0, 0, 0]
        self.shared_program['u_clip_max'] = [1, 1, 1]

        self.shared_program['u_downsample'] = 1.

        self._draw_mode = 'triangle_strip'
        self._index_buffer = IndexBuffer()

        self.shared_program.frag['sampler_type'] = self.textures[0].glsl_sampler_type
        self.shared_program.frag['sample'] = self.textures[0].glsl_sample

        self.set_background(bgcolor)

        # Only show back faces of cuboid. This is required because if we are
        # inside the volume, then the front faces are outside of the clipping
        # box and will not be drawn.
        self.set_gl_state('translucent', cull_face=False)

        self.relative_step_size = relative_step_size
        self.relative_step_size_orig = self.relative_step_size

        self.volumes = defaultdict(dict)

        try:
            self.freeze()
        except AttributeError:  # Older versions of VisPy
            pass

    def _update_shader(self, force=False):
        shader = get_frag_shader(self.volumes, clipped=self._clip_data,
                                 n_volume_max=self._n_volume_max)
        if not force and getattr(self, '_shader_cache', None) == shader:
            return
        self.shared_program.frag = shader
        self._shader_cache = shader
        for label in self.volumes:
            index = self.volumes[label]['index']
            cmap = self.volumes[label]['cmap']
            self.shared_program.frag['cmap{0:d}'.format(index)] = Function(cmap.glsl_map)
        self.shared_program['u_downsample'] = 1.

    def set_clip(self, clip_data, clip_limits):
        self._clip_data = int(clip_data)
        if clip_data:
            self.shared_program['u_clip_min'] = clip_limits[:3]
            self.shared_program['u_clip_max'] = clip_limits[3:]
        self._update_shader()

    def downsample(self):
        if self._vol_shape is None:
            return
        min_dimension = min(self._vol_shape)
        self.shared_program['u_downsample'] = min_dimension / 20

    def upsample(self):
        self.shared_program['u_downsample'] = 1.

    def set_background(self, color):
        self.shared_program['u_bgcolor'] = Color(color).rgba

    def set_resolution(self, resolution):
        self.resolution = resolution
        self._vol_shape = (resolution, resolution, resolution)
        self.shared_program['u_shape'] = self._vol_shape[::-1]

    @property
    def _free_slot_index(self):
        for i in range(self._n_volume_max):
            if self.shared_program['u_enabled_{0}'.format(i)] == 0:
                return i
        raise ValueError("No free slots")

    def allocate(self, label):
        if label in self.volumes:
            raise ValueError("Label {0} already exists".format(label))
        index = self._free_slot_index
        self.volumes[label] = {}
        self.volumes[label]['index'] = index

    def enable(self, label):
        index = self.volumes[label]['index']
        self.volumes[label]['enabled'] = True
        self.shared_program['u_enabled_{0}'.format(index)] = 1
        self._update_shader()

    def disable(self, label):
        index = self.volumes[label]['index']
        self.volumes[label]['enabled'] = False
        self.shared_program['u_enabled_{0}'.format(index)] = 0
        self._update_shader()

    def deallocate(self, label):
        self.disable(label)
        self.volumes.pop(label)

    def set_cmap(self, label, cmap):
        if isinstance(cmap, six.string_types):
            cmap = get_colormap(cmap)
        self.volumes[label]['cmap'] = cmap
        self._update_shader(force=True)

    def set_clim(self, label, clim):
        # Avoid setting the same limits again
        if 'clim' in self.volumes[label] and self.volumes[label]['clim'] == clim:
            return
        self.volumes[label]['clim'] = clim
        if 'data' in self.volumes[label]:
            self._update_scaled_data(label)

    def set_weight(self, label, weight):
        index = self.volumes[label]['index']
        self.shared_program['u_weight_{0:d}'.format(index)] = weight

    def set_multiply(self, label, label_other):
        self.volumes[label]['multiply'] = label_other
        self._update_shader()

    def set_data(self, label, data, layer=None):

        if 'clim' not in self.volumes[label]:
            raise ValueError("set_clim should be called before set_data")

        # Avoid adding the same data again
        if 'data' in self.volumes[label] and self.volumes[label]['data'] is data:
            return

        self.volumes[label]['data'] = data
        self.volumes[label]['layer'] = layer
        self._update_scaled_data(label, initial_shape=True)

    def label_for_layer(self, layer):
        for label in self.volumes:
            if 'layer' in self.volumes[label]:
                if self.volumes[label]['layer'] is layer:
                    return label

    def _update_scaled_data(self, label, initial_shape=False):

        if self._data_slice is None:
            return

        index = self.volumes[label]['index']
        clim = self.volumes[label].get('clim', None)
        data = self.volumes[label]['data']

        # With certain graphics cards, sending the data in one chunk to OpenGL
        # causes artifacts in the rendering - see e.g.
        # https://github.com/vispy/vispy/issues/1412
        # To avoid this, we process the data in chunks. Since we need to do
        # this, we can also do the copy and renormalization on the chunk to
        # avoid excessive memory usage.

        # To start off we need to tell the texture about the new shape
        self.shared_program['u_volumetex_{0:d}'.format(index)].resize(data.shape)

        # Determine the chunk shape - the value of 128 as the minimum value
        # is arbitrary but appears to work nicely. We can reduce that in future
        # if needed.

        sliced_data = data[self._data_slice]

        chunk_shape = [min(x, 128, self.resolution) for x in sliced_data.shape]

        # FIXME: shouldn't be needed!
        zeros = np.zeros(self._vol_shape, dtype=np.float32)
        self.shared_program['u_volumetex_{0:d}'.format(index)].set_data(zeros)

        # Now loop over chunks

        for view in iterate_chunks(sliced_data.shape, chunk_shape=chunk_shape):

            chunk = sliced_data[view]
            chunk = chunk.astype(np.float32)
            if clim is not None:
                chunk -= clim[0]
                chunk *= 1 / (clim[1] - clim[0])

            # PERF: nan_to_num doesn't actually help memory usage as it runs
            # isnan internally, and it's slower, so we just use the following
            # methind. In future we could do this directly with a C extension.
            chunk[np.isnan(chunk)] = 0.

            offset = tuple([s.start for s in view])

            if chunk.size == 0:
                continue

            self.shared_program['u_volumetex_{0:d}'.format(index)].set_data(chunk, offset=offset)

    def _get_step_start(self, vmin, vmax):

        size = vmax - vmin
        if size < self.resolution:
            step = 1
            start = int(vmin)
        else:
            step = int(np.ceil(size / self.resolution))
            start = int(vmin)

        if start < 0:
            start = 0

        return step, start

    def _update_slice_transform(self, x_min, x_max, y_min, y_max, z_min, z_max):

        x_step, x_start = self._get_step_start(x_min, x_max)
        y_step, y_start = self._get_step_start(y_min, y_max)
        z_step, z_start = self._get_step_start(z_min, z_max)

        self._data_slice = [slice(z_start, z_start + self.resolution * z_step, z_step),
                            slice(y_start, y_start + self.resolution * y_step, y_step),
                            slice(x_start, x_start + self.resolution * x_step, x_step)]

        self.transform.inner.scale = [x_step, y_step, z_step]
        self.transform.inner.translate = [x_start, y_start, z_start]

        for label in self.volumes:
            self._update_scaled_data(label)

        self.transform._update_shaders()
        self.transform.update()

    @property
    def enabled(self):
        return [self.shared_program['u_enabled_{0}'.format(i)] == 1
                for i in range(self._n_volume_max)]

    def draw(self):
        if not any(self.enabled):
            return
        else:
            try:
                super(MultiVolumeVisual, self).draw()
            except Exception:
                pass


MultiVolume = create_visual_node(MultiVolumeVisual)
