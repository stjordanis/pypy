"""
Implementation of the 'buffer' and 'memoryview' types.
"""
import operator

from rpython.rlib.buffer import Buffer, SubBuffer
from rpython.rlib.objectmodel import compute_hash
from rpython.rlib.rstruct.error import StructError
from pypy.interpreter.baseobjspace import W_Root
from pypy.interpreter.error import OperationError, oefmt
from pypy.interpreter.gateway import interp2app
from pypy.interpreter.typedef import TypeDef, GetSetProperty,  make_weakref_descr
from pypy.module.struct.formatiterator import UnpackFormatIterator, PackFormatIterator
from rpython.rlib.unroll import unrolling_iterable

MEMORYVIEW_MAX_DIM = 64
MEMORYVIEW_SCALAR   = 0x0001
MEMORYVIEW_C        = 0x0002
MEMORYVIEW_FORTRAN  = 0x0004
MEMORYVIEW_SCALAR   = 0x0008
MEMORYVIEW_PIL      = 0x0010


class W_MemoryView(W_Root):
    """Implement the built-in 'memoryview' type as a wrapper around
    an interp-level buffer.
    """

    def __init__(self, buf, format=None, itemsize=1, ndim=-1,
                 shape=None, strides=None, suboffsets=None):
        assert isinstance(buf, Buffer)
        self.buf = buf
        self._hash = -1
        self.format = format
        self.itemsize = itemsize
        self.shape = shape
        self.strides = strides
        self.suboffsets = suboffsets
        self.ndim = ndim
        self.flags = 0
        self._init_flags()

    # several fields are "overwritten" by the memory view (shape, strides, ...)
    # thus use only those getter fields instead of directly accessing the fields
    def getndim(self):
        if self.ndim == -1:
            return self.buf.getndim()
        return self.ndim

    def getshape(self):
        if self.shape is None:
            return self.buf.getshape()
        return self.shape

    def getstrides(self):
        if self.strides is None:
            return self.buf.getstrides()
        return self.strides

    def getitemsize(self):
        return self.itemsize

    # memoryview needs to modify the field 'format', to prevent the modification
    # of the buffer, we save the new format here!
    def getformat(self):
        if self.format is None:
            return self.buf.getformat()
        return self.format

    def setformat(self, value):
        self.format = value

    def buffer_w_ex(self, space, flags):
        self._check_released(space)
        space.check_buf_flags(flags, self.buf.readonly)
        return self.buf, self.getformat(), self.itemsize

    @staticmethod
    def descr_new_memoryview(space, w_subtype, w_object):
        return W_MemoryView(*space.buffer_w_ex(w_object, space.BUF_FULL_RO))

    def _make_descr__cmp(name):
        def descr__cmp(self, space, w_other):
            if self.buf is None:
                return space.wrap(getattr(operator, name)(self, w_other))
            if isinstance(w_other, W_MemoryView):
                # xxx not the most efficient implementation
                str1 = self.as_str()
                str2 = w_other.as_str()
                return space.wrap(getattr(operator, name)(str1, str2))

            try:
                buf = space.buffer_w(w_other, space.BUF_CONTIG_RO)
            except OperationError as e:
                if not e.match(space, space.w_TypeError):
                    raise
                return space.w_NotImplemented
            else:
                str1 = self.as_str()
                str2 = buf.as_str()
                return space.wrap(getattr(operator, name)(str1, str2))
        descr__cmp.func_name = name
        return descr__cmp

    descr_eq = _make_descr__cmp('eq')
    descr_ne = _make_descr__cmp('ne')

    def as_str(self):
        buf = self.buf
        n_bytes = buf.getlength()
        return buf.getslice(0, n_bytes, 1, n_bytes)

    def getlength(self):
        return self.buf.getlength() // self.itemsize

    def descr_tobytes(self, space):
        self._check_released(space)
        return space.newbytes(self.as_str())

    def descr_tolist(self, space):
        self._check_released(space)

        buf = self.buf
        dim = buf.getndim()
        fmt = self.getformat()
        if dim == 0:
            raise NotImplementedError
        elif dim == 1:
            return self._tolist(space, buf, self.getlength(), fmt)
        else:
            return self._tolist_rec(space, buf, fmt)

    def _tolist(self, space, buf, count, fmt):
        # TODO: this probably isn't very fast
        fmtiter = UnpackFormatIterator(space, self.buf)
        fmtiter.interpret(fmt * count)
        return space.newlist(fmtiter.result_w)

    def _tolist_rec(self, space, buf, start, dim, fmt):
        idim = dim-1
        strides = self.getstrides()
        stride = strides[idim]
        itemsize = self.getitemsize()
        if dim >= buf.getndim():
            return self._tolist(space, SubBuffer(buf, start, itemsize), stride // itemsize, fmt)
        shape = self.getshape()
        dimshape = shape[idim]
        items = [None] * dimshape

        for i in range(dimshape):
            item = self._tolist_rec(space, SubBuffer(buf, start, self.itemsize), dim+1, fmt)
            items[i] = item
            start += stride

        return space.newlist(items,len(items))


    def _start_from_tuple(self, space, w_tuple):
        from pypy.objspace.std.tupleobject import W_TupleObject
        start = 0

        view = self.buf
        length = space.len_w(w_tuple)
        dim = view.getndim()
        dim = 0
        assert isinstance(w_tuple, W_TupleObject)
        while dim < length:
            w_obj = w_tuple.getitem(space, dim)
            index = w_obj.int_w(space)
            start = self.lookup_dimension(space, start, dim, index)
            dim += 1
        return start

    def lookup_dimension(self, space, start, dim, index):
        view = self.buf
        shape = view.getshape()
        strides = view.getstrides()
        nitems = shape[dim]
        if index < 0:
            index += nitems
        if index < 0 or index >= nitems:
            raise oefmt(space.w_IndexError,
                "index out of bounds on dimension %d", dim+1)
        start += strides[dim] * index
        # TODO suboffsets?
        return start

    def _getitem_tuple_indexed(self, space, w_index):
        view = self.buf

        fmt = view.getformat() # TODO adjust format?

        length = space.len_w(w_index)
        ndim = view.getndim()
        if length < ndim:
            raise OperationError(space.w_NotImplementedError, \
                    space.wrap("sub-views are not implemented"))

        if length > ndim:
            raise oefmt(space.w_TypeError, \
                    "cannot index %d-dimension view with %d-element tuple",
                    length, ndim)

        start = self._start_from_tuple(space, w_index)

        buf = SubBuffer(self.buf, start, view.getitemsize())
        fmtiter = UnpackFormatIterator(space, buf)
        fmtiter.interpret(fmt)
        return fmtiter.result_w[0]


    def descr_getitem(self, space, w_index):
        self._check_released(space)

        if space.isinstance_w(w_index, space.w_tuple):
            return self._getitem_tuple_indexed(space, w_index)

        start, stop, step, size = space.decode_index4(w_index, self.getlength())
        itemsize = self.itemsize
        if itemsize > 1:
            start *= itemsize
            size *= itemsize
            stop  = start + size
            # XXX why? returns a memory view on int index if step == 0:
            #    step = 1

            # start & stop are now byte offset, thus use self.buf.getlength()
            if stop > self.buf.getlength():
                raise oefmt(space.w_IndexError, 'index out of range')
        if step not in (0, 1):
            raise oefmt(space.w_NotImplementedError, "")
        if step == 0:  # index only
            # TODO: this probably isn't very fast
            buf = SubBuffer(self.buf, start, self.itemsize)
            fmtiter = UnpackFormatIterator(space, buf)
            fmtiter.interpret(self.getformat())
            return fmtiter.result_w[0]
        elif step == 1:
            buf = SubBuffer(self.buf, start, size)
            return W_MemoryView(buf, self.getformat(), self.itemsize)
        else:
            buf = SubBuffer(self.buf, start, size)
            return W_MemoryView(buf)

    def descr_setitem(self, space, w_index, w_obj):
        self._check_released(space)
        if self.buf.readonly:
            raise oefmt(space.w_TypeError, "cannot modify read-only memory")
        if space.isinstance_w(w_index, space.w_tuple):
            raise oefmt(space.w_NotImplementedError, "")
        start, stop, step, size = space.decode_index4(w_index, self.getlength())
        itemsize = self.itemsize
        if itemsize > 1:
            start *= itemsize
            size *= itemsize
            stop  = start + size
            # XXX why? returns a memory view on int index if step == 0:
            #    step = 1

            # start & stop are now byte offset, thus use self.bug.getlength()
            if stop > self.buf.getlength():
                raise oefmt(space.w_IndexError, 'index out of range')
        if step not in (0, 1):
            raise oefmt(space.w_NotImplementedError, "")
        if step == 0:  # index only
            # TODO: this probably isn't very fast
            fmtiter = PackFormatIterator(space, [w_obj], self.itemsize)
            try:
                fmtiter.interpret(self.getformat())
            except StructError as e:
                raise oefmt(space.w_TypeError,
                            "memoryview: invalid type for format '%s'",
                            self.getformat())
            self.buf.setslice(start, fmtiter.result.build())
        elif step == 1:
            value = space.buffer_w(w_obj, space.BUF_CONTIG_RO)
            if value.getlength() != size * self.itemsize:
                raise oefmt(space.w_ValueError,
                            "cannot modify size of memoryview object")
            self.buf.setslice(start, value.as_str())
        else:
            raise oefmt(space.w_NotImplementedError, "")

    def descr_len(self, space):
        self._check_released(space)
        return space.wrap(self.getlength())

    def w_get_format(self, space):
        self._check_released(space)
        return space.wrap(self.getformat())

    def w_get_itemsize(self, space):
        self._check_released(space)
        return space.wrap(self.itemsize)

    def w_get_ndim(self, space):
        self._check_released(space)
        return space.wrap(self.buf.getndim())

    def w_is_readonly(self, space):
        self._check_released(space)
        return space.newbool(bool(self.buf.readonly))

    def w_get_shape(self, space):
        self._check_released(space)
        return space.newtuple([space.wrap(x) for x in self.buf.getshape()])

    def w_get_strides(self, space):
        self._check_released(space)
        return space.newtuple([space.wrap(x) for x in self.buf.getstrides()])

    def w_get_suboffsets(self, space):
        self._check_released(space)
        # I've never seen anyone filling this field
        return space.newtuple([])

    def descr_repr(self, space):
        if self.buf is None:
            return self.getrepr(space, u'released memory')
        else:
            return self.getrepr(space, u'memory')

    def descr_hash(self, space):
        if self._hash == -1:
            self._check_released(space)
            if not self.buf.readonly:
                raise oefmt(space.w_ValueError,
                            "cannot hash writable memoryview object")
            self._hash = compute_hash(self.buf.as_str())
        return space.wrap(self._hash)

    def descr_release(self, space):
        self.buf = None

    def _check_released(self, space):
        if self.buf is None:
            raise oefmt(space.w_ValueError,
                        "operation forbidden on released memoryview object")

    def descr_enter(self, space):
        self._check_released(space)
        return self

    def descr_exit(self, space, __args__):
        self.buf = None
        return space.w_None

    def descr_pypy_raw_address(self, space):
        from rpython.rtyper.lltypesystem import lltype, rffi
        try:
            ptr = self.buf.get_raw_address()
        except ValueError:
            # report the error using the RPython-level internal repr of self.buf
            msg = ("cannot find the underlying address of buffer that "
                   "is internally %r" % (self.buf,))
            raise OperationError(space.w_ValueError, space.wrap(msg))
        return space.wrap(rffi.cast(lltype.Signed, ptr))
    
    def get_native_fmtchar(self, fmt):
        from rpython.rtyper.lltypesystem import rffi
        size = -1
        if fmt[0] == '@':
            f = fmt[1]
        else:
            f = fmt[0]
        if f == 'c' or f == 'b' or f == 'B':
            size = rffi.sizeof(rffi.CHAR)
        elif f == 'h' or f == 'H':
            size = rffi.sizeof(rffi.SHORT)
        elif f == 'i' or f == 'I':
            size = rffi.sizeof(rffi.INT)
        elif f == 'l' or f == 'L':
            size = rffi.sizeof(rffi.LONG)
        elif f == 'q' or f == 'Q':
            size = rffi.sizeof(rffi.LONGLONG)
        elif f == 'n' or f == 'N':
            size = rffi.sizeof(rffi.SIZE_T)
        elif f == 'f':
            size = rffi.sizeof(rffi.FLOAT)
        elif f == 'd':
            size = rffi.sizeof(rffi.DOUBLE)
        elif f == '?':
            size = rffi.sizeof(rffi.CHAR)
        elif f == 'P':
            size = rffi.sizeof(rffi.VOIDP)
        return size

    def _zero_in_shape(self):
        # this method could be moved to the class Buffer
        buf = self.buf
        shape = buf.getshape()
        for i in range(buf.getndim()):
            if shape[i] == 0:
                return True
        return False

    def descr_cast(self, space, w_format, w_shape=None):
        self._check_released(space)

        if not space.isinstance_w(w_format, space.w_unicode):
            raise OperationError(space.w_TypeError, \
                    space.wrap("memoryview: format argument must be a string"))

        fmt = space.str_w(w_format)
        buf = self.buf
        ndim = 1

        if not memory_view_c_contiguous(space, self.flags):
            raise OperationError(space.w_TypeError, \
                    space.wrap("memoryview: casts are restricted" \
                               " to C-contiguous views"))

        if (w_shape or buf.getndim() != 1) and self._zero_in_shape():
            raise OperationError(space.w_TypeError, \
                    space.wrap("memoryview: cannot casts view with" \
                               " zeros in shape or strides"))

        itemsize = self.get_native_fmtchar(fmt)
        if w_shape:
            if not (space.isinstance_w(w_shape, space.w_list) or space.isinstance_w(w_shape, space.w_tuple)):
                raise oefmt(space.w_TypeError, "expected list or tuple got %T", w_shape)
            ndim = space.len_w(w_shape)
            if ndim > MEMORYVIEW_MAX_DIM:
                raise oefmt(space.w_ValueError, \
                        "memoryview: number of dimensions must not exceed %d",
                        ndim)
            # yes access ndim as field
            if self.ndim > 1 and buf.getndim() != 1:
                raise OperationError(space.w_TypeError, \
                    space.wrap("memoryview: cast must be 1D -> ND or ND -> 1D"))

        mv = W_MemoryView(buf, self.format, self.itemsize)
        origfmt = mv.getformat()
        mv._cast_to_1D(space, origfmt, fmt, itemsize)
        if w_shape:
            shape = [space.int_w(w_obj) for w_obj in w_shape.getitems_unroll()]
            mv._cast_to_ND(space, shape, ndim)
        return mv

    def _init_flags(self):
        buf = self.buf
        ndim = buf.getndim()
        flags = 0
        if ndim == 0:
            flags |= MEMORYVIEW_SCALAR | MEMORYVIEW_C | MEMORYVIEW_FORTRAN
        if ndim == 1:
            shape = buf.getshape()
            strides = buf.getstrides()
            if len(shape) > 0 and shape[0] == 1 and \
               len(strides) > 0 and strides[0] == buf.getitemsize():
                flags |= MEMORYVIEW_C | MEMORYVIEW_SCALAR
        # TODO for now?
        flags |= MEMORYVIEW_C
        # TODO if buf.is_contiguous('C'):
        # TODO     flags |= MEMORYVIEW_C
        # TODO elif buf.is_contiguous('F'):
        # TODO     flags |= MEMORYVIEW_FORTRAN

        # XXX missing suboffsets

        self.flags = flags

    def _cast_to_1D(self, space, origfmt, fmt, itemsize):
        buf = self.buf
        if itemsize < 0:
            raise oefmt(space.w_ValueError, "memoryview: destination" \
                    " format must be a native single character format prefixed" \
                    " with an optional '@'")

        if self.get_native_fmtchar(origfmt) < 0 or \
           (not is_byte_format(fmt) and not is_byte_format(origfmt)):
            raise oefmt(space.w_TypeError,
                    "memoryview: cannot cast between" \
                    " two non-byte formats")

        if buf.getlength() % itemsize != 0:
            raise oefmt(space.w_TypeError,
                    "memoryview: length is not a multiple of itemsize")

        newfmt = self.get_native_fmtstr(fmt)
        if not newfmt:
            raise oefmt(space.w_RuntimeError,
                    "memoryview: internal error")
        self.setformat(newfmt)
        self.itemsize = itemsize
        self.ndim = 1
        self.shape = [buf.getlength() // buf.getitemsize()]
        self.strides = [buf.getitemsize()]
        # XX suboffsets

        self._init_flags()

    def get_native_fmtstr(self, fmt):
        lenfmt = len(fmt)
        nat = False
        if lenfmt == 0:
            return None
        elif lenfmt == 1:
            format = fmt[0] # fine!
        elif lenfmt == 2:
            if fmt[0] == '@':
                nat = True
                format = fmt[1]
            else:
                return None
        else:
            return None

        chars = ['c','b','B','h','H','i','I','l','L','q',
                 'Q','n','N','f','d','?','P']
        for c in unrolling_iterable(chars):
            if c == format:
                if nat: return '@'+c
                else: return c

        return None

    def _cast_to_ND(self, space, shape, ndim):
        buf = self.buf

        self.ndim = ndim
        length = self.itemsize
        if ndim == 0:
            self.shape = []
            self.strides = []
        else:
            self.shape = shape
            for i in range(ndim):
                length *= shape[i]
            self._init_strides_from_shape()

        if length != self.buf.getlength():
            raise OperationError(space.w_TypeError,
                    space.wrap("memoryview: product(shape) * itemsize != buffer size"))

        self._init_flags()

    def _init_strides_from_shape(self):
        s = [0] * len(self.shape)
        self.strides = s
        dim = self.getndim()
        s[dim-1] = self.itemsize
        for i in range(0,ndim-2,-1):
            s[i] = s[i+1] * shape[i+1]

    def descr_hex(self, space):
        from pypy.objspace.std.bytearrayobject import _array_to_hexstring
        self._check_released(space)
        return _array_to_hexstring(space, self.buf)

def is_byte_format(char):
    return char == 'b' or char == 'B' or char == 'c'

def memory_view_c_contiguous(space, flags):
    return flags & (space.BUF_CONTIG_RO|MEMORYVIEW_C) != 0

W_MemoryView.typedef = TypeDef(
    "memoryview",
    __doc__ = """\
Create a new memoryview object which references the given object.
""",
    __new__     = interp2app(W_MemoryView.descr_new_memoryview),
    __eq__      = interp2app(W_MemoryView.descr_eq),
    __getitem__ = interp2app(W_MemoryView.descr_getitem),
    __len__     = interp2app(W_MemoryView.descr_len),
    __ne__      = interp2app(W_MemoryView.descr_ne),
    __setitem__ = interp2app(W_MemoryView.descr_setitem),
    __repr__    = interp2app(W_MemoryView.descr_repr),
    __hash__      = interp2app(W_MemoryView.descr_hash),
    __enter__   = interp2app(W_MemoryView.descr_enter),
    __exit__    = interp2app(W_MemoryView.descr_exit),
    __weakref__ = make_weakref_descr(W_MemoryView),
    cast        = interp2app(W_MemoryView.descr_cast),
    hex         = interp2app(W_MemoryView.descr_hex),
    tobytes     = interp2app(W_MemoryView.descr_tobytes),
    tolist      = interp2app(W_MemoryView.descr_tolist),
    release     = interp2app(W_MemoryView.descr_release),
    format      = GetSetProperty(W_MemoryView.w_get_format),
    itemsize    = GetSetProperty(W_MemoryView.w_get_itemsize),
    ndim        = GetSetProperty(W_MemoryView.w_get_ndim),
    readonly    = GetSetProperty(W_MemoryView.w_is_readonly),
    shape       = GetSetProperty(W_MemoryView.w_get_shape),
    strides     = GetSetProperty(W_MemoryView.w_get_strides),
    suboffsets  = GetSetProperty(W_MemoryView.w_get_suboffsets),
    _pypy_raw_address = interp2app(W_MemoryView.descr_pypy_raw_address),
    )
W_MemoryView.typedef.acceptable_as_base_class = False
