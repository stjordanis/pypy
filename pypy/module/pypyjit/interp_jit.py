"""This is not the JIT :-)

This is transformed to become a JIT by code elsewhere: pypy/jit/*
"""

from rpython.tool.pairtype import extendabletype
from rpython.rtyper.annlowlevel import cast_base_ptr_to_instance
from rpython.rlib.rarithmetic import r_uint, intmask
from rpython.rlib.jit import JitDriver, hint, we_are_jitted, dont_look_inside,\
     BaseJitCell
from rpython.rlib import jit, jit_hooks
from rpython.rlib.jit import current_trace_length, unroll_parameters
import pypy.interpreter.pyopcode   # for side-effects
from pypy.interpreter.error import OperationError, operationerrfmt
from pypy.interpreter.pycode import PyCode, CO_GENERATOR
from pypy.interpreter.pyframe import PyFrame
from pypy.interpreter.pyopcode import ExitFrame
from pypy.interpreter.gateway import unwrap_spec
from opcode import opmap

PyFrame._virtualizable2_ = ['last_instr', 'pycode',
                            'valuestackdepth', 'locals_stack_w[*]',
                            'cells[*]',
                            'last_exception',
                            'lastblock',
                            'is_being_profiled',
                            'w_globals',
                            'w_f_trace',
                            ]

JUMP_ABSOLUTE = opmap['JUMP_ABSOLUTE']

def get_printable_location(next_instr, is_being_profiled, bytecode):
    from pypy.tool.stdlib_opcode import opcode_method_names
    name = opcode_method_names[ord(bytecode.co_code[next_instr])]
    return '%s #%d %s' % (bytecode.get_repr(), next_instr, name)

def get_jitcell_at(next_instr, is_being_profiled, bytecode):
    # use only uints as keys in the jit_cells dict, rather than
    # a tuple (next_instr, is_being_profiled)
    key = (next_instr << 1) | r_uint(intmask(is_being_profiled))
    return bytecode.jit_cells.get(key, None)

def set_jitcell_at(newcell, next_instr, is_being_profiled, bytecode):
    key = (next_instr << 1) | r_uint(intmask(is_being_profiled))
    bytecode.jit_cells[key] = newcell


def should_unroll_one_iteration(next_instr, is_being_profiled, bytecode):
    return (bytecode.co_flags & CO_GENERATOR) != 0

class PyPyJitDriver(JitDriver):
    reds = ['frame', 'ec']
    greens = ['next_instr', 'is_being_profiled', 'pycode']
    virtualizables = ['frame']


pypyjitdriver = PyPyJitDriver(get_printable_location = get_printable_location,
                              get_jitcell_at = get_jitcell_at,
                              set_jitcell_at = set_jitcell_at,
                              should_unroll_one_iteration =
                              should_unroll_one_iteration,
                              name='pypyjit')

class __extend__(PyFrame):

    def dispatch(self, pycode, next_instr, ec):
        self = hint(self, access_directly=True)
        next_instr = r_uint(next_instr)
        is_being_profiled = self.is_being_profiled
        try:
            while True:
                pypyjitdriver.jit_merge_point(ec=ec,
                    frame=self, next_instr=next_instr, pycode=pycode,
                    is_being_profiled=is_being_profiled)
                co_code = pycode.co_code
                self.valuestackdepth = hint(self.valuestackdepth, promote=True)
                next_instr = self.handle_bytecode(co_code, next_instr, ec)
                is_being_profiled = self.is_being_profiled
        except ExitFrame:
            return self.popvalue()

    def jump_absolute(self, jumpto, ec):
        if we_are_jitted():
            #
            # assume that only threads are using the bytecode counter
            decr_by = 0
            if self.space.actionflag.has_bytecode_counter:   # constant-folded
                if self.space.threadlocals.gil_ready:   # quasi-immutable field
                    decr_by = _get_adapted_tick_counter()
            #
            self.last_instr = intmask(jumpto)
            ec.bytecode_trace(self, decr_by)
            jumpto = r_uint(self.last_instr)
        #
        pypyjitdriver.can_enter_jit(frame=self, ec=ec, next_instr=jumpto,
                                    pycode=self.getcode(),
                                    is_being_profiled=self.is_being_profiled)
        return jumpto

def _get_adapted_tick_counter():
    # Normally, the tick counter is decremented by 100 for every
    # Python opcode.  Here, to better support JIT compilation of
    # small loops, we decrement it by a possibly smaller constant.
    # We get the maximum 100 when the (unoptimized) trace length
    # is at least 3200 (a bit randomly).
    trace_length = r_uint(current_trace_length())
    decr_by = trace_length // 32
    if decr_by < 1:
        decr_by = 1
    elif decr_by > 100:    # also if current_trace_length() returned -1
        decr_by = 100
    return intmask(decr_by)


PyCode__initialize = PyCode._initialize

class __extend__(PyCode):
    __metaclass__ = extendabletype

    def _initialize(self):
        PyCode__initialize(self)
        self.jit_cells = {}
        self.bridge_init_threshold = 0

    def _cleanup_(self):
        self.jit_cells = {}

# ____________________________________________________________
#
# Public interface

def set_param(space, __args__):
    '''Configure the tunable JIT parameters.
        * set_param(name=value, ...)            # as keyword arguments
        * set_param("name=value,name=value")    # as a user-supplied string
        * set_param("off")                      # disable the jit
        * set_param("default")                  # restore all defaults
    '''
    # XXXXXXXXX
    args_w, kwds_w = __args__.unpack()
    if len(args_w) > 1:
        msg = "set_param() takes at most 1 non-keyword argument, %d given"
        raise operationerrfmt(space.w_TypeError, msg, len(args_w))
    if len(args_w) == 1:
        text = space.str_w(args_w[0])
        try:
            jit.set_user_param(None, text)
        except ValueError:
            raise OperationError(space.w_ValueError,
                                 space.wrap("error in JIT parameters string"))
    for key, w_value in kwds_w.items():
        if key == 'enable_opts':
            jit.set_param(None, 'enable_opts', space.str_w(w_value))
        else:
            intval = space.int_w(w_value)
            for name, _ in unroll_parameters:
                if name == key and name != 'enable_opts':
                    jit.set_param(None, name, intval)
                    break
            else:
                raise operationerrfmt(space.w_TypeError,
                                      "no JIT parameter '%s'", key)

@dont_look_inside
def residual_call(space, w_callable, __args__):
    '''For testing.  Invokes callable(...), but without letting
    the JIT follow the call.'''
    return space.call_args(w_callable, __args__)

def _jitcell_at(w_code, pos):
    try:
        jitcell = w_code.jit_cells[pos << 1]
    except KeyError:
        ref = jit_hooks.new_jitcell()
        jitcell = cast_base_ptr_to_instance(BaseJitCell, ref)
        w_code.jit_cells[pos << 1] = jitcell
    return jitcell

@jit.dont_look_inside
@unwrap_spec(w_code=PyCode, pos=r_uint, value=int)
def set_local_threshold(space, w_code, pos, value):
    """ set_local_threshold(code, pos, value)

    For testing. Set the threshold for this code object at position pos
    at value given.
    """
    jitcell = _jitcell_at(w_code, pos)
    jitcell.counter = value

@jit.dont_look_inside
@unwrap_spec(w_code=PyCode)
def dont_trace_inside(space, w_code):
    """ dont trace inside this function
    """
    from rpython.rlib.nonconst import NonConstant

    flag = True
    if NonConstant(0):
        flag = False # annotation hack to annotate it as real bool
    jitcell = _jitcell_at(w_code, 0)
    jitcell.dont_trace_here = flag

