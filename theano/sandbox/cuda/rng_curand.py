"""
Define CURAND_RandomStreams - backed by CURAND
"""

__authors__ = "James Bergstra"
__copyright__ = "(c) 2011, University of Montreal"
__license__ = "3-clause BSD License"
__contact__ = "theano-dev@googlegroups.com"

import numpy
import theano.gof
from theano.compat import PY3
from theano.gof.python25 import all
from theano.sandbox.cuda import CudaNdarrayType, GpuOp
from theano.tensor import (get_vector_length, cast, opt)
from theano.compile import optdb
from theano.gof import local_optimizer, Variable


config = theano.config


class CURAND_Base(GpuOp):
    """ Base class for a random number generator implemented in CURAND.

    The random number generator itself is an opaque reference managed by
    CURAND.  This Op uses a generic-typed shared variable to point to a CObject
    that encapsulates this opaque reference.

    Each random variable is created with a generator of False.
    The actual random number generator is allocated from the seed, on the first
    call to allocate random numbers (see c_code).

    :note:
        One caveat is that the random number state is simply not serializable.
        Consequently, attempts to serialize functions compiled with these
        random numbers will fail.

    """
    def __init__(self, output_type, seed, destructive):
        """
        output_type: a theano type (e.g. tensor.fvector)
        seed: integer
        destructive: True or False (on the generator)
        """
        theano.gof.Op.__init__(self)
        self.destructive = destructive
        self.seed = seed
        if self.destructive:
            self.destroy_map = {0: [0]}
        self.output_type = output_type
        assert output_type.dtype == "float32"

    def as_destructive(self):
        """Return an destructive version of self"""
        return self.__class__(self.output_type, self.seed, destructive=True)

    def _config(self):
        """Return a tuple of attributes that define the Op"""
        return (
                self.destructive,
                self.output_type,
                self.seed,
                )

    def __eq__(self, other):
        return type(self) == type(other) and self._config() == other._config()

    def __hash__(self):
        return hash((type(self), self._config()))

    def __str__(self):
        return (self.__class__.__name__ + "{inplace=%s, out_dtype=%s}" %
                (self.destructive, self.output_type))

    def make_node(self, generator, size):
        return theano.gof.Apply(self, [generator, size],
                [generator.type(), self.output_type()])

    @classmethod
    def new_auto_update(cls, generator, ndim, dtype, size, seed):
        """
        Return a symbolic sample from generator.

        cls dictates the random variable (e.g. uniform, normal)

        """
        v_size = theano.tensor.as_tensor_variable(size)
        if ndim is None:
            ndim = get_vector_length(v_size)
        self = cls(
                output_type=CudaNdarrayType((False,) * ndim),
                seed=seed,
                destructive=False)

        o_gen, sample = self(generator, cast(v_size, 'int32'))

        sample.generator = generator        # for user
        sample.update = (generator, o_gen)  # for CURAND_RandomStreams
        generator.default_update = o_gen    # for pfunc uses this attribute
        return sample

    def c_headers(self):
        return ["curand.h"]

    def c_libraries(self):
        return ['curand']

    def c_support_code(self):
        return """
        #if PY_MAJOR_VERSION >= 3
        void free_generator(PyObject *_gen)
        {
            curandGenerator_t * gen = (curandGenerator_t*)NpyCapsule_AsVoidPtr(_gen);
        #else
        void free_generator(void *_gen)
        {
            curandGenerator_t * gen = (curandGenerator_t*)_gen;
        #endif

            curandStatus_t err = curandDestroyGenerator(*gen);
            if (err != CURAND_STATUS_SUCCESS)
            {
                fprintf(stderr, "Failure (%i) in destroying CURAND generator.\\n",
                    (int)err);
            }
            free(gen);
        }
        """

    def c_code(self, node, nodename, inp, out, sub):
        i_generator, size = inp
        o_generator, o_sample = out
        destructive = int(self.destructive)
        ndim = self.output_type.ndim
        o_type_num = numpy.asarray(0, dtype=self.output_type.dtype).dtype.num
        fail = sub['fail']
        seed = self.seed
        call_string = self._curand_call_str(o_sample=o_sample)
        if self.output_type.dtype == 'float32':
            otype = 'float'
        else:
            otype = 'double'

        code = """
        //////// <code generated by CURAND_Base>
        int odims[%(ndim)s];
        int n_elements = 1;
        int must_alloc_sample = ((NULL == %(o_sample)s)
                || !CudaNdarray_Check(py_%(o_sample)s)
                || (%(o_sample)s->nd != %(ndim)s));

        if (%(size)s->nd != 1)
        {
            PyErr_SetString(PyExc_ValueError, "size must be vector");
            %(fail)s
        }
        if (%(size)s->dimensions[0] != %(ndim)s)
        {
            PyErr_Format(PyExc_ValueError, "size must have length %%i (not %%i)",
                %(ndim)s, %(size)s->dimensions[0]);
            %(fail)s
        }
        if (PyArray_DESCR(%(size)s)->type_num != NPY_INT32)
        {
            PyErr_SetString(PyExc_ValueError, "size must be int32");
            %(fail)s
        }
        for (int i = 0; i < %(ndim)s; ++i)
        {
            odims[i] = ((npy_int32*)(%(size)s->data + %(size)s->strides[0] * i))[0];
            n_elements *= odims[i];
            must_alloc_sample = (must_alloc_sample
                    || CudaNdarray_HOST_DIMS(%(o_sample)s)[i] != odims[i]);
        }
        if (must_alloc_sample)
        {
            Py_XDECREF(%(o_sample)s);
            %(o_sample)s = (CudaNdarray*)CudaNdarray_NewDims(%(ndim)s, odims);
            if(!%(o_sample)s)
            {
                %(fail)s;
            }
        }
        if (!PyCObject_Check(%(i_generator)s))
        {
            // allocate a new generator for o_generator
            Py_XDECREF(%(o_generator)s);
            curandGenerator_t * gen = (curandGenerator_t*)malloc(sizeof(curandGenerator_t));
            assert(gen);
            if (CURAND_STATUS_SUCCESS !=
                    curandCreateGenerator(gen, CURAND_RNG_PSEUDO_DEFAULT)) {
                PyErr_Format(PyExc_RuntimeError, "Failed to initialize curand generator");
                %(fail)s;
            }
            if (CURAND_STATUS_SUCCESS !=
                    curandSetPseudoRandomGeneratorSeed(*gen,%(seed)s))
            {
                PyErr_Format(PyExc_RuntimeError, "Failed to set curand generator seed");
                %(fail)s;
            }
            %(o_generator)s = PyCObject_FromVoidPtr(gen, &free_generator);
            assert (%(i_generator)s == Py_False);
        }
        else if (%(destructive)s)
        {
            // use i_generator for o_generator
            Py_XDECREF(%(o_generator)s);
            Py_INCREF(%(i_generator)s);
            %(o_generator)s = %(i_generator)s;
        }
        else
        {
            // copy i_generator for o_generator
            PyErr_Format(PyExc_NotImplementedError, "non-destructive CURAND generation");
            %(fail)s;
        }
        {
            curandGenerator_t * gen = (curandGenerator_t*)PyCObject_AsVoidPtr(%(o_generator)s);
            curandStatus_t err = %(call_string)s

            if (err != CURAND_STATUS_SUCCESS)
            {
                PyErr_Format(PyExc_RuntimeError, "curand error generating random normals %%i", (int)err);
                %(fail)s;
            }
            cudaThreadSynchronize();
        }
        //////// </ code generated by CURAND_Base>
        """ % locals()

        if PY3:
            code = code.replace("PyCObject", "NpyCapsule")
        return code

    def c_code_cache_version(self):
        return (3,)


class CURAND_Normal(CURAND_Base):
    """Op to draw normal numbers using CURAND
    """
    def _curand_call_str(self, **kwargs):
        return """curandGenerateNormal(*gen,
                CudaNdarray_DEV_DATA(%(o_sample)s),
                n_elements,
                0.0, 1.0);
        """ % kwargs


class CURAND_Uniform(CURAND_Base):
    """Op to draw uniform numbers using CURAND
    """
    def _curand_call_str(self, **kwargs):
        return """ curandGenerateUniform(*gen,
                CudaNdarray_DEV_DATA(%(o_sample)s),
                n_elements);
               """ % kwargs


class CURAND_RandomStreams(object):
    """
    RandomStreams instance that creates CURAND-based random variables.

    One caveat is that generators are not serializable.
    """

    def __init__(self, seed):
        """ seed: int
        """
        self._start_seed = seed
        self._cur_seed = seed
        self._has_lost_states = False  # True if self.state_updates incomplete
        self.state_updates = []

    def updates(self):
        """List of all (old, new) generator update pairs created by this
        instance.
        """
        return list(self.state_updates)

    def next_seed(self):
        """Return a unique seed for initializing a random variable.
        """
        self._cur_seed += 1
        return self._cur_seed - 1

    def __getstate__(self):
        rval = dict(self.__dict__)
        # the CObject used to store updates cannot be serialized
        rval['state_updates'] = []
        rval['_has_lost_states'] = True
        return rval

    def uniform(self, size, low=0.0, high=1.0, ndim=None,
            dtype=config.floatX):
        """
        Return symbolic tensor of uniform numbers.
        """
        if isinstance(size, tuple):
            msg = "size must be a tuple of int or a Theano variable"
            assert all([isinstance(i, int) or isinstance(i, Variable)
                for i in size]), msg
        else:
            msg = "size must be a tuple of int or a Theano variable"
            assert isinstance(size, Variable) and size.ndim == 1, msg
        generator = theano.shared(False)  # makes a generic
        s_size = theano.tensor.as_tensor_variable(size)
        u = CURAND_Uniform.new_auto_update(generator, ndim, dtype, s_size,
                self.next_seed())
        self.state_updates.append(u.update)
        rval = u * (high - low) + low
        if u.type.broadcastable != rval.type.broadcastable:
            raise NotImplementedError(
                'Increase the size to match the broadcasting pattern of '
                'low and `high` arguments'
            )
        return  rval

    def normal(self, size=None, avg=0.0, std=1.0, ndim=None,
            dtype=config.floatX):
        """
        Return symbolic tensor of normally-distributed numbers.

        :param: size: Can be a list of integer or Theano variable(ex: the shape
            of other Theano Variable)
        """
        if isinstance(size, tuple):
            msg = "size must be a tuple of int or a Theano variable"
            assert all([isinstance(i, int) or isinstance(i, Variable)
                for i in size]), msg
        else:
            msg = "size must be a tuple of int or a Theano variable"
            assert isinstance(size, Variable) and size.ndim == 1, msg
        generator = theano.shared(False)  # makes a generic
        s_size = theano.tensor.as_tensor_variable(size)
        u = CURAND_Normal.new_auto_update(generator, ndim, dtype, s_size,
                self.next_seed())
        self.state_updates.append(u.update)
        rval = u * std + avg
        if u.type.broadcastable != rval.type.broadcastable:
            raise NotImplementedError(
                'Increase the size to match the broadcasting pattern of `low`'
                'and `high` arguments'
            )
        return  rval


@local_optimizer([None])
def local_destructive(node):
    op = node.op
    if isinstance(op, CURAND_Base) and not op.destructive:
        # op might be gpu version
        new_op = op.as_destructive()
        return new_op.make_node(*node.inputs).outputs
    return False
optdb.register('CURAND_destructive',
        opt.in2out(local_destructive, ignore_newtrees=True), 99, 'fast_run',
                   'inplace')
