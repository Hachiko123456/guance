"""Fixed-tolerance scalar parity checks for the Phase 5 gate."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence

from .lifecycle_contracts import fail

LOGIT_RTOL = 1e-5
LOGIT_ATOL = 1e-6
PROBABILITY_RTOL = 1e-5
PROBABILITY_ATOL = 1e-7
PROBABILITY_SUM_ATOL = 1e-6
CACHE_RTOL = 1e-5
CACHE_ATOL = 1e-7
CACHE_MAX_ABS = 1e-6

@dataclass(frozen=True, slots=True)
class NumericParity:
    count: int
    max_abs: float
    max_rel: float
    argmax_equal: bool
    within_tolerance: bool

    def validate(self) -> None:
        if type(self.count) is not int or self.count < 1:
            fail("parity_count_invalid")
        if not all(math.isfinite(x) and x >= 0 for x in (self.max_abs, self.max_rel)):
            fail("parity_metric_invalid")
        if type(self.argmax_equal) is not bool or type(self.within_tolerance) is not bool:
            fail("parity_flag_invalid")


def compare_numeric(expected: Iterable[float], actual: Iterable[float], *,
                   rtol: float, atol: float) -> NumericParity:
    left=tuple(float(x) for x in expected); right=tuple(float(x) for x in actual)
    if not left or len(left)!=len(right): fail("parity_shape_mismatch")
    diffs=[]; rels=[]
    for a,b in zip(left,right):
        if not math.isfinite(a) or not math.isfinite(b): fail("parity_nonfinite")
        diff=abs(a-b); diffs.append(diff); rels.append(diff/max(abs(a),abs(b),1e-12))
    result=NumericParity(len(left),max(diffs),max(rels),left.index(max(left))==right.index(max(right)),
                         all(d <= atol + rtol*abs(a) for a,d in zip(left,diffs)))
    result.validate(); return result


def compare_probabilities(expected: Sequence[float], actual: Sequence[float], *,
                          illegal_indices: Iterable[int] = ()) -> NumericParity:
    result=compare_numeric(expected,actual,rtol=PROBABILITY_RTOL,atol=PROBABILITY_ATOL)
    for index in illegal_indices:
        if expected[index] != 0.0 or actual[index] != 0.0: fail("illegal_probability_nonzero")
    if abs(sum(expected)-1.0)>PROBABILITY_SUM_ATOL or abs(sum(actual)-1.0)>PROBABILITY_SUM_ATOL:
        fail("probability_sum_invalid")
    return result

__all__=["CACHE_ATOL","CACHE_MAX_ABS","CACHE_RTOL","LOGIT_ATOL","LOGIT_RTOL","NumericParity","PROBABILITY_ATOL","PROBABILITY_RTOL","PROBABILITY_SUM_ATOL","compare_numeric","compare_probabilities"]
