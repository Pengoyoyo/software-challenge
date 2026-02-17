# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True

from .board cimport CBoard, CMoveList

cdef double c_evaluate(CBoard board, int our_team) noexcept
cdef double c_evaluate_with_mobility(CBoard board, int our_team, CMoveList* our_moves, CMoveList* opp_moves) noexcept
