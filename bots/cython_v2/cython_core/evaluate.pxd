# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True

from .board cimport CBoard

cdef double c_evaluate(CBoard board, int our_team) noexcept
cdef bint c_is_connected(CBoard board, int team) noexcept
cdef bint c_try_terminal_eval(CBoard board, int our_team, double* out_score) noexcept

cpdef void set_eval_params(
    double best_swarm,
    double swarm_count,
    double material,
    double isolated,
    double distance
)
cpdef void reset_eval_params()
cpdef tuple get_eval_params()
