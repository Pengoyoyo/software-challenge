# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True

from .board cimport CBoard, CMove, uint64

cpdef void init_search()
cpdef void clear_tt()
cpdef object iterative_deepening(object game_state, int our_team, double time_limit)
