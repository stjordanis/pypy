from pypy.rlib.objectmodel import specialize, we_are_translated
from pypy.rpython.lltypesystem import lltype, llmemory
from pypy.jit.codegen.model import AbstractRGenOp, GenLabel, GenBuilder
from pypy.jit.codegen.model import GenVar, GenConst, CodeGenSwitch
from pypy.objspace.std.multimethod import FailedToImplement
from pypy.jit.codegen.i386.ri386 import *
from pypy.jit.codegen.i386.ri386setup import Conditions
from pypy.jit.codegen.i386 import conftest


WORD = 4    # bytes

RK_NO_RESULT = 0
RK_WORD      = 1
RK_CC        = 2

DEBUG_TRAP = conftest.option.trap


class Operation(GenVar):
    clobbers_cc = True
    result_kind = RK_WORD
    cc_result   = -1

    def allocate(self, allocator):
        pass
    def generate(self, allocator):
        raise NotImplementedError

class Op1(Operation):
    def __init__(self, x):
        self.x = x
    def allocate(self, allocator):
        allocator.using(self.x)

class UnaryOp(Op1):
    def generate(self, allocator):
        try:
            loc = allocator.var2loc[self]
        except KeyError:
            return    # simple operation whose result is not used anyway
        op = allocator.load_location_with(loc, self.x)
        self.emit(allocator.mc, op)

class OpIntNeg(UnaryOp):
    opname = 'int_neg'
    emit = staticmethod(I386CodeBuilder.NEG)

class OpSameAs(Op1):
    clobbers_cc = False
    def generate(self, allocator):
        try:
            dstop = allocator.get_operand(self)
        except KeyError:
            return    # result not used
        srcop = allocator.get_operand(self.x)
        if srcop != dstop:
            mc = allocator.mc
            try:
                mc.MOV(dstop, srcop)
            except FailedToImplement:
                mc.MOV(ecx, srcop)
                mc.MOV(dstop, ecx)

class OpCompare1(Op1):
    result_kind = RK_CC
    def generate(self, allocator):
        srcop = allocator.get_operand(self.x)
        mc = allocator.mc
        self.emit(mc, srcop)

class OpIntIsTrue(OpCompare1):
    opname = 'int_is_true'
    cc_result = Conditions['NE']
    @staticmethod
    def emit(mc, x):
        mc.CMP(x, imm8(0))

class Op2(Operation):
    def __init__(self, x, y):
        self.x = x
        self.y = y
    def allocate(self, allocator):
        allocator.using(self.x)
        allocator.using(self.y)

class BinaryOp(Op2):
    commutative = False
    def generate(self, allocator):
        try:
            dstop = allocator.get_operand(self)
        except KeyError:
            return    # simple operation whose result is not used anyway
        op1 = allocator.get_operand(self.x)
        op2 = allocator.get_operand(self.y)
        # now all of dstop, op1 and op2 may alias each other and be in
        # a register, in the stack or an immediate... finding a correct
        # and encodable combination of instructions is loads of fun
        mc = allocator.mc
        if dstop == op1:
            case = 1       # optimize for this common case
        elif self.commutative and dstop == op2:
            op1, op2 = op2, op1
            case = 1
        elif isinstance(dstop, REG):
            if dstop != op2:
                # REG = OPERATION(op1, op2)   with op2 != REG
                case = 2
            else:
                # REG = OPERATION(op1, REG)
                case = 3
        elif isinstance(op1, REG) and isinstance(op2, REG):
            # STACK = OPERATION(REG, REG)
            case = 2
        else:
            case = 3
        # generate instructions according to the 'case' determined above
        if case == 1:
            # dstop == op1
            try:
                self.emit(mc, op1, op2)
            except FailedToImplement:    # emit(STACK, STACK) combination
                mc.MOV(ecx, op2)
                self.emit(mc, op1, ecx)
        elif case == 2:
            # this case works for:
            #   * REG = OPERATION(op1, op2)   with op2 != REG
            #   * STACK = OPERATION(REG, REG)
            mc.MOV(dstop, op1)
            self.emit(mc, dstop, op2)
        else:
            # most general case
            mc.MOV(ecx, op1)
            self.emit(mc, ecx, op2)
            mc.MOV(dstop, ecx)

class OpIntAdd(BinaryOp):
    opname = 'int_add'
    emit = staticmethod(I386CodeBuilder.ADD)
    commutative = True

class OpIntSub(BinaryOp):
    opname = 'int_sub'
    emit = staticmethod(I386CodeBuilder.SUB)

class OpIntMul(Op2):
    opname = 'int_mul'
    def generate(self, allocator):
        try:
            dstop = allocator.get_operand(self)
        except KeyError:
            return    # simple operation whose result is not used anyway
        op1 = allocator.get_operand(self.x)
        op2 = allocator.get_operand(self.y)
        mc = allocator.mc
        if isinstance(dstop, REG):
            tmpop = dstop
        else:
            tmpop = ecx
        if tmpop == op1:
            mc.IMUL(tmpop, op2)
        elif isinstance(op2, IMM32):
            mc.IMUL(tmpop, op1, op2)
        elif isinstance(op1, IMM32):
            mc.IMUL(tmpop, op2, op1)
        else:
            if tmpop != op2:
                mc.MOV(tmpop, op2)
            mc.IMUL(tmpop, op1)
        if dstop != tmpop:
            mc.MOV(dstop, tmpop)

class OpIntFloorDiv(Op2):
    opname = 'int_floordiv'
    divmod_result_register = eax
    def generate(self, allocator):
        try:
            dstop = allocator.get_operand(self)
        except KeyError:
            return    # simple operation whose result is not used anyway
        op1 = allocator.get_operand(self.x)
        op2 = allocator.get_operand(self.y)
        # not very efficient but not a very common operation either
        mc = allocator.mc
        if dstop != eax:
            mc.PUSH(eax)
        if dstop != edx:
            mc.PUSH(edx)
        if op1 != eax:
            mc.MOV(eax, op1)
        mc.CDQ()
        try:
            mc.IDIV(op2)
        except FailedToImplement:
            mc.MOV(ecx, op2)
            mc.IDIV(ecx)
        mc.MOV(dstop, self.divmod_result_register)
        if dstop != edx:
            mc.POP(edx)
        if dstop != eax:
            mc.POP(eax)

class OpIntMod(OpIntFloorDiv):
    opname = 'int_mod'
    divmod_result_register = edx

class OpCompare2(Op2):
    result_kind = RK_CC
    def generate(self, allocator):
        srcop = allocator.get_operand(self.x)
        dstop = allocator.get_operand(self.y)
        mc = allocator.mc
        # XXX optimize the case CMP(immed, reg-or-modrm)
        try:
            mc.CMP(srcop, dstop)
        except FailedToImplement:
            mc.MOV(ecx, srcop)
            mc.CMP(ecx, dstop)

class OpIntLt(OpCompare2):
    opname = 'int_lt'
    cc_result = Conditions['L']

class OpIntLe(OpCompare2):
    opname = 'int_le'
    cc_result = Conditions['LE']

class OpIntEq(OpCompare2):
    opname = 'int_eq'
    cc_result = Conditions['E']

class OpIntNe(OpCompare2):
    opname = 'int_ne'
    cc_result = Conditions['NE']

class OpIntGt(OpCompare2):
    opname = 'int_gt'
    cc_result = Conditions['G']

class OpIntGe(OpCompare2):
    opname = 'int_ge'
    cc_result = Conditions['GE']

class JumpIf(Operation):
    clobbers_cc = False
    result_kind = RK_NO_RESULT
    def __init__(self, gv_condition, targetbuilder, negate):
        self.gv_condition = gv_condition
        self.targetbuilder = targetbuilder
        self.negate = negate
    def allocate(self, allocator):
        allocator.using_cc(self.gv_condition)
        for gv in self.targetbuilder.inputargs_gv:
            allocator.using(gv)
    def generate(self, allocator):
        cc = self.gv_condition.cc_result
        if self.negate:
            cc = cond_negate(cc)
        mc = allocator.mc
        targetbuilder = self.targetbuilder
        targetbuilder.set_coming_from(mc, insncond=cc)
        targetbuilder.inputoperands = [allocator.get_operand(gv)
                                       for gv in targetbuilder.inputargs_gv]

class OpLabel(Operation):
    clobbers_cc = False
    result_kind = RK_NO_RESULT
    def __init__(self, lbl, args_gv):
        self.lbl = lbl
        self.args_gv = args_gv
    def allocate(self, allocator):
        for v in self.args_gv:
            allocator.using(v)
    def generate(self, allocator):
        lbl = self.lbl
        lbl.targetaddr = allocator.mc.tell()
        lbl.inputoperands = [allocator.get_operand(v) for v in self.args_gv]

class Label(GenLabel):
    targetaddr = 0
    inputoperands = None

# ____________________________________________________________

class IntConst(GenConst):

    def __init__(self, value):
        self.value = value

    def operand(self, builder):
        return imm(self.value)

    def nonimmoperand(self, builder, tmpregister):
        builder.mc.MOV(tmpregister, self.operand(builder))
        return tmpregister

    @specialize.arg(1)
    def revealconst(self, T):
        if isinstance(T, lltype.Ptr):
            return lltype.cast_int_to_ptr(T, self.value)
        elif T is llmemory.Address:
            return llmemory.cast_int_to_adr(self.value)
        else:
            return lltype.cast_primitive(T, self.value)

    def __repr__(self):
        "NOT_RPYTHON"
        try:
            return "const=%s" % (imm(self.value).assembler(),)
        except TypeError:   # from Symbolics
            return "const=%r" % (self.value,)

    def repr(self):
        return "const=$%s" % (self.value,)

class AddrConst(GenConst):

    def __init__(self, addr):
        self.addr = addr

    def operand(self, builder):
        return imm(llmemory.cast_adr_to_int(self.addr))

    def nonimmoperand(self, builder, tmpregister):
        builder.mc.MOV(tmpregister, self.operand(builder))
        return tmpregister

    @specialize.arg(1)
    def revealconst(self, T):
        if T is llmemory.Address:
            return self.addr
        elif isinstance(T, lltype.Ptr):
            return llmemory.cast_adr_to_ptr(self.addr, T)
        elif T is lltype.Signed:
            return llmemory.cast_adr_to_int(self.addr)
        else:
            assert 0, "XXX not implemented"

    def __repr__(self):
        "NOT_RPYTHON"
        return "const=%r" % (self.addr,)

    def repr(self):
        return "const=<0x%x>" % (llmemory.cast_adr_to_int(self.addr),)

# ____________________________________________________________

def setup_opclasses(base):
    d = {}
    for name, value in globals().items():
        if type(value) is type(base) and issubclass(value, base):
            if hasattr(value, 'opname'):
                assert value.opname not in d
                d[value.opname] = value
    return d
OPCLASSES1 = setup_opclasses(Op1)
OPCLASSES2 = setup_opclasses(Op2)
del setup_opclasses

def setup_conditions():
    result1 = [None] * 16
    result2 = [None] * 16
    for key, value in Conditions.items():
        result1[value] = getattr(I386CodeBuilder, 'J'+key)
        result2[value] = getattr(I386CodeBuilder, 'SET'+key)
    return result1, result2
EMIT_JCOND, EMIT_SETCOND = setup_conditions()
INSN_JMP = len(EMIT_JCOND)
EMIT_JCOND.append(I386CodeBuilder.JMP)    # not really a conditional jump
del setup_conditions

def cond_negate(cond):
    assert 0 <= cond < INSN_JMP
    return cond ^ 1


class StackOpCache:
    INITIAL_STACK_EBP_OFS = -4
stack_op_cache = StackOpCache()
stack_op_cache.lst = []

def stack_op(n):
    "Return the mem operand that designates the nth stack-spilled location"
    assert n >= 0
    lst = stack_op_cache.lst
    while len(lst) <= n:
        ofs = WORD * (StackOpCache.INITIAL_STACK_EBP_OFS - len(lst))
        lst.append(mem(ebp, ofs))
    return lst[n]

def stack_n_from_op(op):
    ofs = op.ofs_relative_to_ebp()
    return StackOpCache.INITIAL_STACK_EBP_OFS - ofs / WORD


class RegAllocator(object):
    AVAILABLE_REGS = [eax, edx, ebx, esi, edi]   # XXX ecx reserved for stuff

    # 'gv' -- GenVars, used as arguments and results of operations
    #
    # 'loc' -- location, a small integer that represents an abstract
    #          register number
    #
    # 'operand' -- a concrete machine code operand, which can be a
    #              register (ri386.eax, etc.) or a stack memory operand

    def __init__(self):
        self.nextloc = 0
        self.var2loc = {}
        self.available_locs = []
        self.force_loc2operand = {}
        self.force_operand2loc = {}
        self.initial_moves = []

    def set_final(self, final_vars_gv):
        for v in final_vars_gv:
            if not v.is_const and v not in self.var2loc:
                self.var2loc[v] = self.nextloc
                self.nextloc += 1

    def creating(self, v):
        try:
            loc = self.var2loc[v]
        except KeyError:
            pass
        else:
            self.available_locs.append(loc)   # now available again for reuse

    def using(self, v):
        if not v.is_const and v not in self.var2loc:
            try:
                loc = self.available_locs.pop()
            except IndexError:
                loc = self.nextloc
                self.nextloc += 1
            self.var2loc[v] = loc

    def creating_cc(self, v):
        if self.need_var_in_cc is v:
            # common case: v is a compare operation whose result is precisely
            # what we need to be in the CC
            self.need_var_in_cc = None
        self.creating(v)

    def save_cc(self):
        # we need a value to be in the CC, but we see a clobbering
        # operation, so we copy the original CC-creating operation down
        # past the clobbering operation
        v = self.need_var_in_cc
        if not we_are_translated():
            assert v in self.operations[:self.operationindex]
        self.operations.insert(self.operationindex, v)
        self.need_var_in_cc = None

    def using_cc(self, v):
        assert isinstance(v, Operation)
        assert 0 <= v.cc_result < INSN_JMP
        if self.need_var_in_cc is not None:
            self.save_cc()
        self.need_var_in_cc = v

    def allocate_locations(self, operations):
        # assign locations to gvars
        self.operations = operations
        self.need_var_in_cc = None
        self.operationindex = len(operations)
        for i in range(len(operations)-1, -1, -1):
            v = operations[i]
            kind = v.result_kind
            if kind == RK_WORD:
                self.creating(v)
            elif kind == RK_CC:
                self.creating_cc(v)
            if self.need_var_in_cc is not None and v.clobbers_cc:
                self.save_cc()
            v.allocate(self)
            self.operationindex = i
        if self.need_var_in_cc is not None:
            self.save_cc()

    def force_var_operands(self, force_vars, force_operands, at_start):
        force_loc2operand = self.force_loc2operand
        force_operand2loc = self.force_operand2loc
        for i in range(len(force_vars)):
            v = force_vars[i]
            operand = force_operands[i]
            try:
                loc = self.var2loc[v]
            except KeyError:
                if at_start:
                    pass    # input variable not used anyway
                else:
                    self.add_final_move(v, operand)
            else:
                if loc in force_loc2operand or operand in force_operand2loc:
                    if at_start:
                        self.initial_moves.append((loc, operand))
                    else:
                        self.add_final_move(v, operand)
                else:
                    force_loc2operand[loc] = operand
                    force_operand2loc[operand] = loc

    def add_final_move(self, v, targetoperand):
        v2 = OpSameAs(v)
        self.operations.append(v2)
        loc = self.nextloc
        self.nextloc += 1
        self.var2loc[v2] = loc
        self.force_loc2operand[loc] = targetoperand

    def allocate_registers(self):
        # assign registers to locations that don't have one already
        force_loc2operand = self.force_loc2operand
        operands = []
        seen_regs = 0
        seen_stackn = {}
        for op in force_loc2operand.values():
            if isinstance(op, REG):
                seen_regs |= 1 << op.op
            elif isinstance(op, MODRM):
                seen_stackn[stack_n_from_op(op)] = None
        i = 0
        stackn = 0
        for loc in range(self.nextloc):
            try:
                operand = force_loc2operand[loc]
            except KeyError:
                # grab the next free register
                try:
                    while True:
                        operand = RegAllocator.AVAILABLE_REGS[i]
                        i += 1
                        if not (seen_regs & (1 << operand.op)):
                            break
                except IndexError:
                    while stackn in seen_stackn:
                        stackn += 1
                    operand = stack_op(stackn)
                    stackn += 1
            operands.append(operand)
        self.operands = operands
        self.required_frame_depth = stackn

    def get_operand(self, gv_source):
        if isinstance(gv_source, IntConst):
            return imm(gv_source.value)
        else:
            loc = self.var2loc[gv_source]
            return self.operands[loc]

    def load_location_with(self, loc, gv_source):
        dstop = self.operands[loc]
        srcop = self.get_operand(gv_source)
        if srcop != dstop:
            self.mc.MOV(dstop, srcop)
        return dstop

    def generate_initial_moves(self):
        initial_moves = self.initial_moves
        # first make sure that the reserved stack frame is big enough
        last_n = self.required_frame_depth - 1
        for loc, srcoperand in initial_moves:
            if isinstance(srcoperand, MODRM):
                n = stack_n_from_op(srcoperand)
                if last_n < n:
                    last_n = n
        if last_n >= 0:
            self.mc.LEA(esp, stack_op(last_n))
        # XXX naive algo for now
        for loc, srcoperand in initial_moves:
            if self.operands[loc] != srcoperand:
                self.mc.PUSH(srcoperand)
        initial_moves.reverse()
        for loc, srcoperand in initial_moves:
            if self.operands[loc] != srcoperand:
                self.mc.POP(self.operands[loc])

    def generate_operations(self):
        for v in self.operations:
            v.generate(self)
            cc = v.cc_result
            if cc >= 0 and v in self.var2loc:
                # force a comparison instruction's result into a
                # regular location
                dstop = self.get_operand(v)
                mc = self.mc
                insn = EMIT_SETCOND[cc]
                insn(mc, cl)
                try:
                    mc.MOVZX(dstop, cl)
                except FailedToImplement:
                    mc.MOVZX(ecx, cl)
                    mc.MOV(dstop, ecx)


class Builder(GenBuilder):
    coming_from = 0
    operations = None

    def __init__(self, rgenop, inputargs_gv, inputoperands):
        self.rgenop = rgenop
        self.inputargs_gv = inputargs_gv
        self.inputoperands = inputoperands

    def start_writing(self):
        assert self.operations is None
        self.operations = []

    def generate_block_code(self, final_vars_gv, force_vars=[],
                                                 force_operands=[],
                                                 renaming=True):
        allocator = RegAllocator()
        allocator.set_final(final_vars_gv)
        if not renaming:
            final_vars_gv = allocator.var2loc.keys()  # unique final vars
        allocator.allocate_locations(self.operations)
        allocator.force_var_operands(force_vars, force_operands,
                                     at_start=False)
        allocator.force_var_operands(self.inputargs_gv, self.inputoperands,
                                     at_start=True)
        allocator.allocate_registers()
        mc = self.start_mc()
        allocator.mc = mc
        allocator.generate_initial_moves()
        allocator.generate_operations()
        self.operations = None
        if renaming:
            self.inputargs_gv = [GenVar() for v in final_vars_gv]
        else:
            # just keep one copy of each Variable that is alive
            self.inputargs_gv = final_vars_gv
        self.inputoperands = [allocator.get_operand(v) for v in final_vars_gv]
        return mc

    def enter_next_block(self, kinds, args_gv):
##        mc = self.generate_block_code(args_gv)
##        assert len(self.inputargs_gv) == len(args_gv)
##        args_gv[:len(args_gv)] = self.inputargs_gv
##        self.set_coming_from(mc)
##        self.rgenop.close_mc(mc)
##        self.start_writing()
        for i in range(len(args_gv)):
            op = OpSameAs(args_gv[i])
            args_gv[i] = op
            self.operations.append(op)
        lbl = Label()
        lblop = OpLabel(lbl, args_gv)
        self.operations.append(lblop)
        return lbl

    def set_coming_from(self, mc, insncond=INSN_JMP):
        self.coming_from_cond = insncond
        self.coming_from = mc.tell()
        insnemit = EMIT_JCOND[insncond]
        insnemit(mc, rel32(0))

    def start_mc(self):
        mc = self.rgenop.open_mc()
        # update the coming_from instruction
        start = self.coming_from
        if start:
            targetaddr = mc.tell()
            end = start + 6    # XXX hard-coded, enough for JMP and Jcond
            oldmc = self.rgenop.InMemoryCodeBuilder(start, end)
            insn = EMIT_JCOND[self.coming_from_cond]
            insn(oldmc, rel32(targetaddr))
            oldmc.done()
            self.coming_from = 0
        return mc

    def _jump_if(self, gv_condition, args_for_jump_gv, negate):
        newbuilder = Builder(self.rgenop, list(args_for_jump_gv), None)
        # if the condition does not come from an obvious comparison operation,
        # e.g. a getfield of a Bool or an input argument to the current block,
        # then insert an OpIntIsTrue
        if gv_condition.cc_result < 0 or gv_condition not in self.operations:
            gv_condition = OpIntIsTrue(gv_condition)
            self.operations.append(gv_condition)
        self.operations.append(JumpIf(gv_condition, newbuilder, negate=negate))
        return newbuilder

    def jump_if_false(self, gv_condition, args_for_jump_gv):
        return self._jump_if(gv_condition, args_for_jump_gv, True)

    def jump_if_true(self, gv_condition, args_for_jump_gv):
        return self._jump_if(gv_condition, args_for_jump_gv, False)

    def finish_and_goto(self, outputargs_gv, targetlbl):
        operands = targetlbl.inputoperands
        if operands is None:
            raise NotImplementedError
        mc = self.generate_block_code(outputargs_gv, outputargs_gv, operands)
        mc.JMP(rel32(targetlbl.targetaddr))
        self.rgenop.close_mc(mc)

    def finish_and_return(self, sigtoken, gv_returnvar):
        mc = self.generate_block_code([gv_returnvar], [gv_returnvar], [eax])
        # --- epilogue ---
        mc.LEA(esp, mem(ebp, -12))
        mc.POP(edi)
        mc.POP(esi)
        mc.POP(ebx)
        mc.POP(ebp)
        mc.RET()
        # ----------------
        self.rgenop.close_mc(mc)

    def pause_writing(self, alive_gv):
        mc = self.generate_block_code(alive_gv, renaming=False)
        self.set_coming_from(mc)
        self.rgenop.close_mc(mc)
        return self

    def end(self):
        pass

    @specialize.arg(1)
    def genop1(self, opname, gv_arg):
        cls = OPCLASSES1[opname]
        if cls is None:     # identity
            return gv_arg
        op = cls(gv_arg)
        self.operations.append(op)
        return op

    @specialize.arg(1)
    def genop2(self, opname, gv_arg1, gv_arg2):
        cls = OPCLASSES2[opname]
        op = cls(gv_arg1, gv_arg2)
        self.operations.append(op)
        return op


class RI386GenOp(AbstractRGenOp):
    from pypy.jit.codegen.i386.codebuf import MachineCodeBlock
    from pypy.jit.codegen.i386.codebuf import InMemoryCodeBuilder

    MC_SIZE = 65536
    
    def __init__(self):
        self.mcs = []   # machine code blocks where no-one is currently writing
        self.keepalive_gc_refs = [] 
        self.total_code_blocks = 0

    def open_mc(self):
        if self.mcs:
            # XXX think about inserting NOPS for alignment
            return self.mcs.pop()
        else:
            # XXX supposed infinite for now
            self.total_code_blocks += 1
            return self.MachineCodeBlock(self.MC_SIZE)

    def close_mc(self, mc):
        # an open 'mc' is ready for receiving code... but it's also ready
        # for being garbage collected, so be sure to close it if you
        # want the generated code to stay around :-)
        self.mcs.append(mc)

    def check_no_open_mc(self):
        assert len(self.mcs) == self.total_code_blocks

    def newgraph(self, sigtoken, name):
        # --- prologue ---
        mc = self.open_mc()
        entrypoint = mc.tell()
        if DEBUG_TRAP:
            mc.BREAKPOINT()
        mc.PUSH(ebp)
        mc.MOV(ebp, esp)
        mc.PUSH(ebx)
        mc.PUSH(esi)
        mc.PUSH(edi)
        self.close_mc(mc)
        # NB. a bit of a hack: the first generated block of the function
        # will immediately follow, by construction
        # ----------------
        numargs = sigtoken     # for now
        inputargs_gv = []
        inputoperands = []
        for i in range(numargs):
            inputargs_gv.append(GenVar())
            inputoperands.append(mem(ebp, WORD * (2+i)))
        builder = Builder(self, inputargs_gv, inputoperands)
        builder.start_writing()
        #ops = [OpSameAs(v) for v in inputargs_gv]
        #builder.operations.extend(ops)
        #inputargs_gv = ops
        return builder, IntConst(entrypoint), inputargs_gv[:]

##    def replay(self, label, kinds):
##        return ReplayBuilder(self), [dummy_var] * len(kinds)

    @specialize.genconst(1)
    def genconst(self, llvalue):
        T = lltype.typeOf(llvalue)
        if T is llmemory.Address:
            return AddrConst(llvalue)
        elif isinstance(T, lltype.Primitive):
            return IntConst(lltype.cast_primitive(lltype.Signed, llvalue))
        elif isinstance(T, lltype.Ptr):
            lladdr = llmemory.cast_ptr_to_adr(llvalue)
            if T.TO._gckind == 'gc':
                self.keepalive_gc_refs.append(lltype.cast_opaque_ptr(llmemory.GCREF, llvalue))
            return AddrConst(lladdr)
        else:
            assert 0, "XXX not implemented"
    
    # attached later constPrebuiltGlobal = global_rgenop.genconst

    @staticmethod
    @specialize.memo()
    def fieldToken(T, name):
        FIELD = getattr(T, name)
        if isinstance(FIELD, lltype.ContainerType):
            fieldsize = 0      # not useful for getsubstruct
        else:
            fieldsize = llmemory.sizeof(FIELD)
        return (llmemory.offsetof(T, name), fieldsize)

    @staticmethod
    @specialize.memo()
    def allocToken(T):
        return llmemory.sizeof(T)

    @staticmethod
    @specialize.memo()
    def varsizeAllocToken(T):
        if isinstance(T, lltype.Array):
            return RI386GenOp.arrayToken(T)
        else:
            # var-sized structs
            arrayfield = T._arrayfld
            ARRAYFIELD = getattr(T, arrayfield)
            arraytoken = RI386GenOp.arrayToken(ARRAYFIELD)
            length_offset, items_offset, item_size = arraytoken
            arrayfield_offset = llmemory.offsetof(T, arrayfield)
            return (arrayfield_offset+length_offset,
                    arrayfield_offset+items_offset,
                    item_size)

    @staticmethod
    @specialize.memo()    
    def arrayToken(A):
        return (llmemory.ArrayLengthOffset(A),
                llmemory.ArrayItemsOffset(A),
                llmemory.ItemOffset(A.OF))

    @staticmethod
    @specialize.memo()
    def kindToken(T):
        if T is lltype.Float:
            py.test.skip("not implemented: floats in the i386 back-end")
        return None     # for now

    @staticmethod
    @specialize.memo()
    def sigToken(FUNCTYPE):
        numargs = 0
        for ARG in FUNCTYPE.ARGS:
            if ARG is not lltype.Void:
                numargs += 1
        return numargs     # for now

    @staticmethod
    def erasedType(T):
        if T is llmemory.Address:
            return llmemory.Address
        if isinstance(T, lltype.Primitive):
            return lltype.Signed
        elif isinstance(T, lltype.Ptr):
            return llmemory.GCREF
        else:
            assert 0, "XXX not implemented"

global_rgenop = RI386GenOp()
RI386GenOp.constPrebuiltGlobal = global_rgenop.genconst
