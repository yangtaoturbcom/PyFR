# -*- coding: utf-8 -*-

import pycuda.driver as cuda
import numpy as np

import pyfr.backends.base as base

from pyfr.backends.cuda.util import memcpy2d_htod, memcpy2d_dtoh

class _CudaBase2D(object):
    def __init__(self, backend, nrow, ncol, initval, tags):
        self._nrow = nrow
        self._ncol = ncol
        self.tags = tags

        # Compute the size, in bytes, of the minor dimension
        mindimsz = self.mindim*self.itemsize

        if 'nopad' not in tags:
            # Allocate a 2D array aligned to the major dimension
            self.data, self.pitch = cuda.mem_alloc_pitch(mindimsz, self.majdim,
                                                         self.itemsize)
            self._nbytes = self.majdim*self.pitch

            # Ensure that the pitch is a multiple of itemsize
            assert (self.pitch % self.itemsize) == 0
        else:
            # Allocate a standard, tighly packed, array
            self._nbytes = mindimsz*self.majdim
            self.data = cuda.mem_alloc(self._nbytes)
            self.pitch = mindimsz

        # Process any initial values
        if initval is not None:
            self._set(initval)

    def _get(self):
        # Allocate an empty buffer
        buf = np.empty((self.nrow, self.ncol), dtype=self.dtype,
                       order=self.order)

        # Copy
        memcpy2d_dtoh(buf, self.data, self.pitch, self.mindim*self.itemsize,
                      self.mindim*self.itemsize, self.majdim)

        return buf

    def _set(self, ary):
        if ary.shape != (self.nrow, self.ncol):
            raise ValueError('Matrix has invalid dimensions')

        nary = np.asanyarray(ary, dtype=self.dtype, order=self.order)

        # Copy
        memcpy2d_htod(self.data, nary, self.mindim*self.itemsize,
                      self.pitch, self.mindim*self.itemsize, self.majdim)

    def offsetof(self, i, j):
        if i >= self._nrow or j >= self._ncol:
            raise ValueError('Index ({},{}) out of bounds ({},{}))'.\
                             format(i, j, self._nrow, self._ncol))

        return self.pitch*i + j*self.itemsize if self.order == 'C' else\
               self.pitch*j + i*self.itemsize

    def addrof(self, i, j):
        return np.intp(int(self.data) + self.offsetof(i, j))

    @property
    def nrow(self):
        return self._nrow

    @property
    def ncol(self):
        return self._ncol

    @property
    def nbytes(self):
        return self._nbytes

    @property
    def itemsize(self):
        return np.dtype(self.dtype).itemsize

    @property
    def majdim(self):
        return self._nrow if self.order == 'C' else self._ncol

    @property
    def mindim(self):
        return self._ncol if self.order == 'C' else self._nrow

    @property
    def leaddim(self):
        return self.pitch / self.itemsize

    @property
    def traits(self):
        return self.leaddim, self.mindim, self.order, self.dtype


class CudaMatrix(_CudaBase2D, base.Matrix):
    order = 'F'
    dtype = np.float64

    def get(self):
        return self._get()

    def set(self, ary):
        self._set(ary)


class CudaMatrixBank(base.MatrixBank):
    def __init__(self, backend, mats, tags):
        for m in mats[1:]:
            if m.traits != mats[0].traits:
                raise ValueError('Matrices in a bank must be homogeneous')

        super(CudaMatrixBank, self).__init__(mats)


class CudaConstMatrix(CudaMatrix, base.ConstMatrix):
    def __init__(self, backend, initval, tags):
        nrow, ncol = initval.shape
        return super(CudaConstMatrix, self).__init__(backend, nrow, ncol,
                                                     initval, tags)


class CudaSparseMatrix(object):
    def __init__(self, backend, initval, tags):
        raise NotImplementedError('SparseMatrix todo!')


class CudaView(_CudaBase2D, base.View):
    order = 'F'
    dtype = np.intp

    def __init__(self, backend, matmap, rcmap, tags):
        nrow, ncol = matmap.shape

        # Get the different matrices which we map onto
        self._mats = list(np.unique(matmap))

        # Extract the data type and item size from the first matrix
        self.refdtype = self._mats[0].dtype
        self.refitemsize = self._mats[0].itemsize

        # Validate the matrices
        for m in self._mats:
            if not isinstance(m, CudaMatrix):
                raise TypeError('Incompatible matrix type for view')

            if m.dtype != self.refdtype:
                raise TypeError('Mixed view matrix types are not supported')

        # Fold rcmap from an N*M*2 array to a matrix of N*M 2-tuples
        rcmap = rcmap.view(','.join((rcmap.dtype.char,)*2)).squeeze()

        # Go from matrices and row/column indices to addresses
        mapping = np.vectorize(lambda m, rc: m.addrof(*rc))(matmap, rcmap)

        # Create a matrix on the GPU of these pointers
        super(CudaView, self).__init__(backend, nrow, ncol, mapping, tags)


class CudaMPIMatrix(CudaMatrix, base.MPIMatrix):
    def __init__(self, backend, nrow, ncol, initval, tags):
        # Ensure that our CUDA buffer will not be padded
        ntags = tags | {'nopad'}

        # Call the standard matrix constructor
        super(CudaMPIMatrix, self).__init__(backend, nrow, ncol, initval,
                                            ntags)

        # Allocate a page-locked buffer on the host for MPI to send/recv from
        self.hdata = cuda.pagelocked_empty((self.nrow, self.ncol),
                                           self.dtype, self.order)


class CudaMPIView(base.MPIView):
    def __init__(self, backend, matmap, rcmap, tags):
        nrow, ncol = matmap.shape

        # Create a normal CUDA view
        self.view = backend.view(matmap, rcmap, tags)

        # Now create an MPI matrix so that the view contents may be packed
        self.mpimat = backend.mpi_matrix(nrow, ncol, None, tags)

    @property
    def nbytes(self):
        return self.view.nbytes + self.mpimat.nbytes