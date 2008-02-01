from pypy.rlib.rarithmetic import intmask
from pypy.objspace.flow import model as flowmodel
from pypy.rpython.lltypesystem import lltype
from pypy.jit.hintannotator.model import originalconcretetype
from pypy.jit.timeshifter import rtimeshift, rvalue
from pypy.rlib.unroll import unrolling_iterable

class JitCode(object):
    """
    normal operations have the following format:
    2 byte - operation
    n * 2 byte - arguments
    
    for nonvoid results the result is appended to the varlist

    red vars are just indexes
    green vars are positive indexes
    green consts are negative indexes
    """

    def __init__(self, code, constants):
        self.code = code
        self.constants = constants

    def _freeze_(self):
        return True

SIGN_EXTEND2 = 1 << 15

STOP = object()

class JitInterpreter(object):
    def __init__(self):
        self.opcode_implementations = []
        self.opcode_descs = []
        self.opname_to_index = {}
        self.jitstate = None
        self.queue = None
        self.bytecode = None
        self.pc = -1
        self._add_implemented_opcodes()

    def run(self, jitstate, bytecode, greenargs, redargs):
        self.jitstate = jitstate
        self.queue = rtimeshift.ensure_queue(jitstate,
                                             rtimeshift.BaseDispatchQueue)
        rtimeshift.enter_frame(self.jitstate, self.queue)
        self.frame = self.jitstate.frame
        self.frame.pc = 0
        self.frame.bytecode = bytecode
        self.frame.local_boxes = redargs
        self.frame.local_green = greenargs
        self.bytecode_loop()

    def bytecode_loop(self):
        while 1:
            bytecode = self.load_2byte()
            result = self.opcode_implementations[bytecode](self)
            if result is STOP:
                return

    # construction-time interface

    def _add_implemented_opcodes(self):
        for name in dir(self):
            if not name.startswith("opimpl_"):
                continue
            opname = name[len("opimpl_"):]
            self.opname_to_index[opname] = len(self.opcode_implementations)
            self.opcode_implementations.append(getattr(self, name).im_func)
            self.opcode_descs.append(None)

    def find_opcode(self, name):
        return self.opname_to_index.get(name, -1)

    def make_opcode_implementation(self, color, opdesc):
        numargs = unrolling_iterable(range(opdesc.nb_args))
        if color == "green":
            def implementation(self):
                args = ()
                for i in numargs:
                    genconst = self.get_greenarg()
                    arg = self.jitstate.curbuilder.revealconst(opdesc.ARGS[i])
                    args += (arg, )
                result = opdesc.llop(*args)
                self.green_result(result)
        elif color == "red":
            if opdesc.nb_args == 1:
                impl = rtimeshift.ll_gen1
            elif opdesc.nb_args == 2:
                impl = rtimeshift.ll_gen2
            else:
                XXX
            def implementation(self):
                args = (opdesc, self.jitstate, )
                for i in numargs:
                    args += (self.get_redarg(), )
                result = impl(*args)
                self.red_result(result)
        else:
            assert 0, "unknown color"
        implementation.func_name = "opimpl_%s_%s" % (color, opdesc.opname)
        opname = "%s_%s" % (color, opdesc.opname)
        index = self.opname_to_index[opname] = len(self.opcode_implementations)
        self.opcode_implementations.append(implementation)
        self.opcode_descs.append(opdesc)
        return index
            

    # operation helper functions

    def load_2byte(self):
        pc = self.frame.pc
        result = ((ord(self.frame.bytecode.code[pc]) << 8) |
                   ord(self.frame.bytecode.code[pc + 1]))
        self.frame.pc = pc + 2
        return intmask((result ^ SIGN_EXTEND2) - SIGN_EXTEND2)

    def load_4byte(self):
        pc = self.frame.pc
        result = ((ord(self.frame.bytecode.code[pc + 0]) << 24) |
                  (ord(self.frame.bytecode.code[pc + 1]) << 16) |
                  (ord(self.frame.bytecode.code[pc + 2]) <<  8) |
                  (ord(self.frame.bytecode.code[pc + 3]) <<  0))
        self.frame.pc = pc + 4
        return intmask(result)

    def get_greenarg(self):
        i = self.load_2byte()
        if i < 0:
            return self.frame.bytecode.constants[~i]
        return self.frame.local_green[i]

    def get_redarg(self):
        return self.frame.local_boxes[self.load_2byte()]

    def red_result(self, box):
        self.frame.local_boxes.append(box)

    def green_result(self, gv):
        self.frame.local_green.append(gv)

    # operation implementations
    def opimpl_make_redbox(self):
        XXX

    def opimpl_goto(self):
        target = self.load_4byte()
        self.frame.pc = target

    def opimpl_green_goto_iftrue(self):
        XXX

    def opimpl_red_goto_iftrue(self):
        XXX

    def opimpl_red_return(self):
        rtimeshift.save_return(self.jitstate)
        # XXX for now
        newstate = rtimeshift.leave_graph_red(self.queue, is_portal=True)
        self.jitstate = newstate
        return STOP

    def opimpl_green_return(self):
        XXX
        return STOP # XXX wrong, of course

    def opimpl_make_new_redvars(self):
        # an opcode with a variable number of args
        # num_args arg_old_1 arg_new_1 ...
        num = self.load_2byte()
        newlocalboxes = []
        for i in range(num):
            newlocalboxes.append(self.get_redarg())
        self.frame.local_boxes = newlocalboxes

    def opimpl_make_new_greenvars(self):
        # an opcode with a variable number of args
        # num_args arg_old_1 arg_new_1 ...
        num = self.load_2byte()
        newgreens = []
        for i in range(num):
            newgreens.append(self.get_greenarg())
        self.frame.local_green = newgreens




class BytecodeWriter(object):
    def __init__(self, t, hintannotator, RGenOp):
        self.translator = t
        self.annotator = t.annotator
        self.hannotator = hintannotator
        self.interpreter = JitInterpreter()
        self.RGenOp = RGenOp
        self.current_block = None

    def make_bytecode(self, graph):
        self.seen_blocks = {}
        self.assembler = []
        self.constants = []
        self.const_positions = {}
        self.seen_blocks = {}
        self.additional_positions = {}
        self.redvar_positions = {}
        self.free_red = {}
        self.greenvar_positions = {}
        self.free_green = {}
        self.graph = graph
        self.entrymap = flowmodel.mkentrymap(graph)
        self.make_bytecode_block(graph.startblock)
        assert self.current_block is None
        return JitCode(assemble(self.interpreter, *self.assembler), self.constants)

    def make_bytecode_block(self, block, insert_goto=False):
        if block in self.seen_blocks:
            if insert_goto:
                self.emit("goto")
                self.emit(tlabel(block))
            return
        # inserting a goto not necessary, falling through
        self.seen_blocks[block] = True
        oldblock = self.current_block
        self.free_green[block] = 0
        self.free_red[block] = 0
        self.current_block = block
        self.emit(label(block))
        reds, greens = self.sort_by_color(block.inputargs)
        for arg in reds:
            self.register_redvar(arg)
        for arg in greens:
            self.register_greenvar(arg)
        #self.insert_merges(block)
        for op in block.operations:
            self.serialize_op(op)
        #self.insert_splits(block)
        self.insert_exits(block)
        self.current_block = oldblock

    def insert_exits(self, block):
        if block.exits == ():
            returnvar, = block.inputargs
            color = self.varcolor(returnvar)
            index = self.serialize_oparg(color, returnvar)
            self.emit("%s_return" % color)
            self.emit(index)
        elif len(block.exits) == 1:
            link, = block.exits
            self.insert_renaming(link.args)
            self.make_bytecode_block(link.target, insert_goto=True)
        elif len(block.exits) == 2:
            linkfalse, linktrue = block.exits
            if linkfalse.llexitcase == True:
                linkfalse, linktrue = linktrue, linkfalse
            color = self.varcolor(block.exitswitch)
            index = self.serialize_oparg(color, block.exitswitch)
            self.emit("%s_goto_iftrue" % color)
            self.emit(index)
            self.emit(tlabel(linktrue))
            self.insert_renaming(linkfalse.args)
            self.make_bytecode_block(linkfalse.target, insert_goto=True)
            self.emit(label(linktrue))
            self.insert_renaming(linktrue.args)
            self.make_bytecode_block(linktrue.target, insert_goto=True)
        else:
            XXX

    def insert_renaming(self, args):
        reds, greens = self.sort_by_color(args)
        for color, args in [("red", reds), ("green", greens)]:
            result = []
            for v in args:
                result.append(self.serialize_oparg(color, v))
            self.emit("make_new_%svars" % (color, ))
            self.emit(len(args))
            for index in result:
                self.emit(index)

    def serialize_op(self, op):
        specialcase = getattr(self, "serialize_op_%s" % (op.opname, ), None)
        if specialcase is not None:
            return specialcase(op)
        color = self.opcolor(op)
        args = []
        for arg in op.args:
            args.append(self.serialize_oparg(color, arg))
        self.serialize_opcode(color, op)
        for index in args:
            self.emit(index)
        if self.hannotator.binding(op.result).is_green():
            self.register_greenvar(op.result)
        else:
            self.register_redvar(op.result)
        

    def serialize_opcode(self, color, op):
        opname = op.opname
        name = "%s_%s" % (color, opname)
        index = self.interpreter.find_opcode(name)
        if index == -1:
            hop = PseudoHOP(
                op, [self.hannotator.binding(arg) for arg in op.args],
                self.hannotator.binding(op.result), self.RGenOp)
            opdesc = rtimeshift.make_opdesc(hop)
            index = self.interpreter.make_opcode_implementation(color, opdesc)
        self.emit(name)

    def serialize_oparg(self, color, arg):
        if color == "red":
            if self.varcolor(arg) == "green":
                return self.convert_to_red(arg)
            return self.redvar_position(arg)
        elif color == "green":
            return self.green_position(arg)
        assert 0, "unknown color"

    def convert_to_red(self, arg):
        block = self.current_block
        if (arg, block) in self.redvar_positions:
            # already converted
            return self.redvar_positions[arg]
        self.emit("make_redbox")
        resultindex = self.register_redvar((arg, block))
        argindex = self.green_position(arg)
        self.emit(argindex)
        return resultindex

    def opcolor(self, op):
        for v in op.args:
            if not self.hannotator.binding(v).is_green():
                return "red"
        if not self.hannotator.binding(op.result).is_green():
            return "red"
        return "green"

    def varcolor(self, var):
        if self.hannotator.binding(var).is_green():
            color = "green"
        else:
            color = "red"
        return color
        
    def register_redvar(self, arg):
        assert arg not in self.redvar_positions
        self.redvar_positions[arg] = result = self.free_red[self.current_block]
        self.free_red[self.current_block] += 1
        return result

    def redvar_position(self, arg):
        return self.redvar_positions[arg]

    def register_greenvar(self, arg, where=-1):
        assert isinstance(arg, flowmodel.Variable)
        if where == -1:
            where = self.free_green[self.current_block]
            self.free_green[self.current_block] += 1
        self.greenvar_positions[arg] = where
        return where

    def green_position(self, arg):
        if isinstance(arg, flowmodel.Variable):
            return self.greenvar_positions[arg]
        return ~self.const_position(arg)

    def const_position(self, const):
        if const in self.const_positions:
            return self.const_positions[const]
        const = self.RGenOp.constPrebuiltGlobal(const.value)
        result = len(self.constants)
        self.constants.append(const)
        self.const_positions[const] = result
        return result
        
    def emit(self, stuff):
        assert stuff is not None
        self.assembler.append(stuff)

    def sort_by_color(self, vars, by_color_of_vars=None):
        reds = []
        greens = []
        if by_color_of_vars is None:
            by_color_of_vars = vars
        for v, bcv in zip(vars, by_color_of_vars):
            if v.concretetype is lltype.Void:
                continue
            if self.hannotator.binding(bcv).is_green():
                greens.append(v)
            else:
                reds.append(v)
        return reds, greens

    # operation special cases

    def serialize_op_hint(self, op):
        hints = op.args[1].value
        arg = op.args[0]
        result = op.result
        if "concrete" in hints:
            assert self.hannotator.binding(arg).is_green()
            assert self.hannotator.binding(result).is_green()
            self.register_greenvar(result, self.green_position(arg))
            return
        XXX


class label(object):
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "label(%r)" % (self.name, )

class tlabel(object):
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "tlabel(%r)" % (self.name, )

def assemble(interpreter, *args):
    result = []
    labelpos = {}
    def emit_2byte(index):
        result.append(chr((index >> 8) & 0xff))
        result.append(chr(index & 0xff))
    for arg in args:
        if isinstance(arg, str):
            emit_2byte(interpreter.find_opcode(arg))
        elif isinstance(arg, int):
            emit_2byte(arg)
        elif isinstance(arg, label):
            labelpos[arg.name] = len(result)
        elif isinstance(arg, tlabel):
            result.extend((arg, None, None, None))
        else:
            XXX
    for i in range(len(result)):
        b = result[i]
        if isinstance(b, tlabel):
            index = labelpos[b.name]
            result[i + 0] = chr((index >> 24) & 0xff)
            result[i + 1] = chr((index >> 16) & 0xff)
            result[i + 2] = chr((index >>  8) & 0xff)
            result[i + 3] = chr(index & 0xff)
    return "".join(result)



# XXX too lazy to fix the interface of make_opdesc, ExceptionDesc
class PseudoHOP(object):
    def __init__(self, op, args_s, s_result, RGenOp):
        self.spaceop = op
        self.args_s = args_s
        self.s_result = s_result
        self.rtyper = PseudoHRTyper(RGenOp=RGenOp)

class PseudoHRTyper(object):
    def __init__(self, **args):
        self.__dict__.update(**args)

