# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True

from .board cimport CBoard

cdef double c_evaluate(CBoard board, int our_team) noexcept

cpdef void set_eval_params(
    double best_swarm,
    double swarm_count,
    double material,
    double isolated,
    double distance
)
cpdef void reset_eval_params()
cpdef tuple get_eval_params()
