# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True

from .board cimport CBoard

cdef double c_evaluate(CBoard board, int our_team) noexcept
cdef bint c_is_connected(CBoard board, int team) noexcept
cdef bint c_try_terminal_eval(CBoard board, int our_team, double* out_score) noexcept
