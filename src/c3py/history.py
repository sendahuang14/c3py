import logging
from abc import ABC, abstractmethod
from copy import deepcopy
from types import MappingProxyType
from typing import Any, NamedTuple, Self

import pydot

from c3py.poset import Poset

logger = logging.getLogger(__name__)


class Instruction(NamedTuple):
    method: Any
    arg: Any
    op_id: str | None = None

    def __repr__(self) -> str:
        prefix = f"{self.op_id}:" if self.op_id is not None else ""
        # if arg is a collection, print as a comma-separated list
        if isinstance(self.arg, (list, tuple)):
            return prefix + f"{self.method}({', '.join(map(str, self.arg))})"
        return prefix + f"{self.method}({self.arg})"


class Operation(NamedTuple):
    method: Any
    arg: Any
    ret: Any = None
    op_id: str | None = None

    def to_instruction(self):
        return Instruction(self.method, self.arg, self.op_id)

    def __repr__(self) -> str:
        prefix = f"{self.op_id}:" if self.op_id is not None else ""
        # if arg is a collection, print as a comma-separated list
        if isinstance(self.arg, (list, tuple)):
            return prefix + f"{self.method}({', '.join(map(str, self.arg))})▷{self.ret}"
        return prefix + f"{self.method}({self.arg})▷{self.ret}"


class History:
    def __init__(self, data: dict[str, list[Operation]]):
        # validation
        assert isinstance(data, dict), "data should be a dictionary"
        for _, ops in data.items():
            for op in ops:
                assert isinstance(op, Operation), "invalid operation"

        self.operations = set()
        self.label = dict[str, Operation | Instruction]()
        for process, ops in data.items():
            for i in range(len(ops)):
                op_id = f"{process}.{i + 1}"
                self.operations.add(op_id)
                self.label[op_id] = ops[i]._replace(op_id=op_id)

        self.poset = Poset(self.operations)
        for process, ops in data.items():
            for i in range(len(ops) - 1):
                self.poset.order_try(f"{process}.{i + 1}", f"{process}.{i + 2}")

    def causal_hist(self, op_id: str, ret_set: set[str]) -> Self:
        ch = deepcopy(self)
        p = ch.poset.predecessors(op_id)
        ch.operations = p
        ch.poset = ch.poset.subset(p)
        ch.label = {
            op_id: op if op_id in ret_set else op.to_instruction()
            for op_id, op in ch.label.items()
        }
        return ch

    def causal_arb(self, op_id: str, arb: list[str]) -> list[Instruction | Operation]:
        """compute CausalArb(op_id){op_id} for `arb`

        Because `arb` is a strict total order, this function returns a history as a list of `Instruction`s and `Operation`s.
        """
        p = self.poset.predecessors(op_id)
        # filter out operations that are not in the causal history
        arb = [o for o in arb if o in p]
        idx = arb.index(op_id)
        history: list[Instruction | Operation] = [
            self.label[o].to_instruction() for o in arb[: (idx + 1)]
        ]
        history[idx] = self.label[op_id]
        return history

    def visualize(self, include_label: bool = True) -> pydot.Dot:
        label = {op_id: f'"{str(op)}"' for op_id, op in self.label.items()}
        dot = self.poset.visualize(label if include_label else None)
        return dot


class Specification(ABC):
    @abstractmethod
    def start(self):
        pass

    @abstractmethod
    def step(self, state, instr: Instruction) -> tuple[Any, Operation]:
        pass

    def satisfies(self, log):
        state = self.start()
        for instr in log:
            state, op = self.step(state, instr)
            # if the instruction has a return value, check if it matches the executed operation
            if isinstance(instr, Operation) and (op.ret != instr.ret):
                return False
        return True


class RWMemorySpecification(Specification):
    def start(self):
        return MappingProxyType({})

    def step(
        self, state: MappingProxyType, instr: Instruction
    ) -> tuple[MappingProxyType, Operation]:
        match instr.method:
            case "wr":
                (key, value) = instr.arg
                return (
                    MappingProxyType(state | {key: value}),
                    Operation("wr", instr.arg, None),
                )
            case "rd":
                key = instr.arg
                return (state, Operation("rd", instr.arg, state.get(key)))
            case _:
                assert False, f"Unexpected method {instr.method}"


# NOTE: `check_CC` and `check_CM` share the common basic structure, but not sure if it makes sense to merge the two functions


class CCResult(NamedTuple):
    is_CC: bool
    causal_history: History | None
    serializations: dict[str, list[Operation | Instruction]] | None


def check_CC(h: History, spec: Specification) -> CCResult:
    for i, co in enumerate(h.poset.refinements()):
        logger.debug(f"check co #{i}: {co}")
        all_op_satisfied = True
        serializations: dict[str, list[Operation | Instruction]] | None = dict()
        for op_id in co.elements():
            logger.debug(f"    focus on {op_id}: {h.label[op_id]}")
            exists_valid_topological_sort = False

            # TODO: This deepcopy is probably not necessary (the same for other functions)
            ch = deepcopy(h)
            ch.poset = co
            ch = ch.causal_hist(op_id, {op_id})
            ros = [*ch.poset.all_topological_sorts()]
            logger.debug(f"    {len(ros)} possible topological orderings")

            for ro in ros:
                log = [ch.label[op_id] for op_id in ro]
                logger.debug(f"        checking: {log}")
                if spec.satisfies(log):
                    logger.debug("        satisfied")
                    logger.info(f"        found satisfying serialization: {ro}")
                    exists_valid_topological_sort = True
                    serializations[op_id] = log
                    break
                else:
                    logger.debug("        not satisfied")

            if not exists_valid_topological_sort:
                all_op_satisfied = False
                break
        if all_op_satisfied:
            ch = deepcopy(h)
            ch.poset = co
            return CCResult(True, ch, serializations)
    return CCResult(False, None, None)


class CMResult(NamedTuple):
    is_CM: bool
    causal_history: History | None
    serializations: dict[str, list[Operation | Instruction]] | None | None


def check_CM(h: History, spec: Specification) -> CMResult:
    for i, co in enumerate(h.poset.refinements()):
        logger.debug(f"check co #{i}: {co}")
        all_op_satisfied = True
        serializations: dict[str, list[Operation | Instruction]] | None = dict()
        for op_id in co.elements():
            logger.debug(f"    focus on {op_id}: {h.label[op_id]}")
            exists_valid_topological_sort = False

            po_past = h.poset.predecessors(op_id)
            ch = deepcopy(h)
            ch.poset = co
            ch = ch.causal_hist(op_id, po_past)
            ros = [*ch.poset.all_topological_sorts()]
            logger.debug(f"    {len(ros)} possible topological orderings")

            for ro in ros:
                log = [ch.label[op_id] for op_id in ro]
                logger.debug(f"        checking: {log}")
                if spec.satisfies(log):
                    logger.debug("        satisfied")
                    exists_valid_topological_sort = True
                    serializations[op_id] = log
                    break
                else:
                    logger.debug("        not satisfied")

            if not exists_valid_topological_sort:
                all_op_satisfied = False
                break
        if all_op_satisfied:
            ch = deepcopy(h)
            ch.poset = co
            return CMResult(True, ch, serializations)
    return CMResult(False, None, None)


class CCvResult(NamedTuple):
    is_CCv: bool
    causal_history: History | None
    arbitration: list[Operation] | None
    serializations: dict[str, list[Instruction | Operation]] | None


def check_CCv(h: History, spec: Specification) -> CCvResult:
    for i, co in enumerate(h.poset.refinements()):
        logger.debug(f"check co #{i}: {co}")
        arbs = co.all_topological_sorts()
        serializations = {}
        for j, arb in enumerate(arbs):
            logger.debug(f"    check arb #{j}: {arb}")
            all_op_satisfied = True
            for op_id in co.elements():
                logger.debug(f"        focus on {op_id}")
                ch = deepcopy(h)
                ch.poset = co
                log = ch.causal_arb(op_id, arb)
                logger.debug(f"        log: {log}")
                if not spec.satisfies(log):
                    logger.debug("        not satisfied")
                    all_op_satisfied = False
                    break
                else:
                    logger.debug("        satisfied")
                    serializations[op_id] = log
            if all_op_satisfied:
                ch = deepcopy(h)
                ch.poset = co
                return CCvResult(True, ch, [h.label[s] for s in arb], serializations)
    return CCvResult(False, None, None, None)
