#!/usr/bin/env python
#
# Copyright 2012 Knowledge Economy Developments Ltd
# 
# Henry Gomersall
# heng@kedevelopments.co.uk
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

'''
A set of utility functions for use with the builders.
'''

import pyfftw
import numpy

__all__ = ['_rc_dtype_pairs', '_default_dtype', '_Xfftn', '_FFTWWrapper',
        '_setup_input_slicers', '_compute_array_shapes', '_precook_1d_args',
        '_cook_nd_args']

_valid_efforts = ('FFTW_ESTIMATE', 'FFTW_MEASURE', 
        'FFTW_PATIENT', 'FFTW_EXHAUSTIVE')

# Looking up a dtype in here returns the complex complement of the same
# precision.
_rc_dtype_pairs = {'float32': 'complex64',
        'float64': 'complex128',
        'longdouble': 'clongdouble',
        'complex64': 'float32',
        'complex128': 'float64',
        'clongdouble': 'longdouble'}

_default_dtype = numpy.dtype('float64')

def _Xfftn(a, s, axes, planner_effort, threads, auto_align_input, 
        avoid_copy, inverse, real):
    '''Generic transform interface for all the transforms. No
    defaults exist. The transform must be specified exactly.
    '''
    invreal = inverse and real

    if inverse:
        direction = 'FFTW_BACKWARD'
    else:
        direction = 'FFTW_FORWARD'

    if planner_effort not in _valid_efforts:
        raise ValueError('Invalid planner effort: ', planner_effort)

    s, axes = _cook_nd_args(a, s, axes, invreal)
    
    input_shape, output_shape = _compute_array_shapes(
            a, s, axes, inverse, real)

    a_is_complex = numpy.iscomplexobj(a)

    # Make the input dtype correct
    if str(a.dtype) not in _rc_dtype_pairs:
        # We make it the default dtype
        if not real or inverse:
            # It's going to be complex
            a = numpy.asarray(a, dtype=_rc_dtype_pairs[str(_default_dtype)])
        else:
            a = numpy.asarray(a, dtype=_default_dtype)
    
    elif not (real and not inverse) and not a_is_complex:
        # We need to make it a complex dtype
        a = numpy.asarray(a, dtype=_rc_dtype_pairs[str(a.dtype)])

    elif (real and not inverse) and a_is_complex:
        # It should be real
        a = numpy.asarray(a, dtype=_rc_dtype_pairs[str(a.dtype)])

    # Make the output dtype correct
    if not real:
        output_dtype = a.dtype
    
    else:
        output_dtype = _rc_dtype_pairs[str(a.dtype)]

    if not avoid_copy:
        a_copy = a.copy()

    output_array = pyfftw.n_byte_align_empty(output_shape, 16, output_dtype)

    if not auto_align_input:
        flags = ['FFTW_UNALIGNED', planner_effort]
    else:
        flags = [planner_effort]

    if not a.shape == input_shape:
        # This means we need to use an _FFTWWrapper object
        # and so need to create slicers.
        update_input_array_slicer, FFTW_array_slicer = (
                _setup_input_slicers(a.shape, input_shape))

        # Also, the input array will be a different shape to the shape of 
        # `a`, so we need to create a new array.
        input_array = pyfftw.n_byte_align_empty(input_shape, 16, a.dtype)

        FFTW_object = _FFTWWrapper(input_array, output_array, axes, direction,
                flags, threads, input_array_slicer=update_input_array_slicer,
                FFTW_array_slicer=FFTW_array_slicer)

        if not avoid_copy:
            # We copy the data back into the internal FFTW object array
            internal_array = FFTW_object.get_input_array()
            internal_array[:] = 0
            internal_array[FFTW_array_slicer] = (
                    a_copy[update_input_array_slicer])

    else:
        # Otherwise we can use `a` as-is
        if auto_align_input:
            input_array = pyfftw.n_byte_align(a, 16)
        else:
            input_array = a

        FFTW_object = pyfftw.FFTW(input_array, output_array, axes, direction,
                flags, threads)

        # Copy the data back into the (likely) destroyed array
        FFTW_object.get_input_array()[:] = a_copy

    return FFTW_object


class _FFTWWrapper(pyfftw.FFTW):

    def __init__(self, input_array, output_array, axes=[-1], 
            direction='FFTW_FORWARD', flags=['FFTW_MEASURE'], 
            threads=1, *args, **kwargs):

        self.__input_array_slicer = kwargs.pop('input_array_slicer')
        self.__FFTW_array_slicer = kwargs.pop('FFTW_array_slicer')

        super(_FFTWWrapper, self).__init__(input_array, output_array, 
                axes, direction, flags, threads, *args, **kwargs)

    def __call__(self, input_array=None, output_array=None, 
            normalise_idft=True):

        if input_array is not None:
            # Do the update here (which is a copy, so it's alignment
            # safe etc).

            internal_input_array = self.get_input_array()
            input_array = numpy.asanyarray(input_array)

            sliced_internal = internal_input_array[self.__FFTW_array_slicer]
            sliced_input = input_array[self.__input_array_slicer]

            if sliced_internal.shape != sliced_input.shape:
                raise ValueError('Invalid input shape: '
                        'The new input array should be the same shape '
                        'as the input array used to instantiate the '
                        'object.')

            sliced_internal[:] = sliced_input

        return super(_FFTWWrapper, self).__call__(input_array=None,
                output_array=output_array, normalise_idft=normalise_idft)


def _setup_input_slicers(a_shape, input_shape):
    ''' This function returns two slicers that are to be used to
    copy the data from the input array to the FFTW object internal
    array, which can then be passed to _FFTWWrapper.

    These are:
    update_input_array_slicer
    FFTW_array_slicer

    On calls to _FFTWWrapper objects, the input array is copied in
    as:
    FFTW_array[FFTW_array_slicer] = input_array[update_input_array_slicer]
    '''

    # default the slicers to include everything
    update_input_array_slicer = (
            [slice(None)]*len(a_shape))
    FFTW_array_slicer = [slice(None)]*len(a_shape)

    # iterate over each dimension and modify the slicer and FFTW dimension
    for axis in xrange(len(a_shape)):

        if a_shape[axis] > input_shape[axis]:
            update_input_array_slicer[axis] = (
                    slice(0, input_shape[axis]))

        elif a_shape[axis] < input_shape[axis]:
            FFTW_array_slicer[axis] = (
                    slice(0, a_shape[axis]))
            update_input_array_slicer[axis] = (
                    slice(0, a_shape[axis]))

        else:
            # If neither of these, we use the whole dimension.
            update_input_array_slicer[axis] = (
                    slice(0, a_shape[axis]))

    return update_input_array_slicer, FFTW_array_slicer

def _compute_array_shapes(a, s, axes, inverse, real):
    '''Given a passed in array a, and the rest of the arguments
    (that have been fleshed out with _cook_nd_args), compute
    the shape the input and output arrays needs to be in order 
    to satisfy all the requirements for the transform. The input
    shape *may* be different to the shape of a.

    returns:
    (input_shape, output_shape)
    '''
    # Start with the shape of a
    orig_domain_shape = list(a.shape)
    fft_domain_shape = list(a.shape)
    
    try:
        for n, axis in enumerate(axes):
            orig_domain_shape[axis] = s[n]
            fft_domain_shape[axis] = s[n]

        if real:
            fft_domain_shape[axes[-1]] = s[-1]//2 + 1

    except IndexError:
        raise IndexError('Invalid axes: '
                'At least one of the passed axes is invalid.')

    if inverse:
        input_shape = fft_domain_shape
        output_shape = orig_domain_shape
    else:
        input_shape = orig_domain_shape
        output_shape = fft_domain_shape

    return tuple(input_shape), tuple(output_shape)

def _precook_1d_args(a, n, axis):
    '''Turn *(n, axis) into (s, axes)
    '''
    if n is not None:
        s = [int(n)]
    else:
        s = None

    # Force an error with an invalid axis
    a.shape[axis]

    return s, (axis,)

def _cook_nd_args(a, s=None, axes=None, invreal=False):
    '''Similar to _cook_nd_args in numpy's fftpack
    '''

    if axes is None:
        if s is None:
            len_s = len(a.shape)
        else:
            len_s = len(s)

        axes = range(-len_s, 0)

    if s is None:
        s = list(numpy.take(a.shape, axes))

        if invreal:
            s[-1] = (a.shape[axes[-1]] - 1) * 2


    if len(s) != len(axes):
        raise ValueError('Shape error: '
                'Shape and axes have different lengths.')

    if len(s) > len(a.shape):
        raise ValueError('Shape error: '
                'The length of s or axes cannot exceed the dimensionality '
                'of the input array, a.')

    return tuple(s), tuple(axes)

