# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True

from .board cimport CBoard, uint64, int8

cdef uint64[10][10][4][5] ZOBRIST_PIECE
cdef uint64[61] ZOBRIST_TURN

cpdef void init_zobrist()
cdef uint64 c_compute_hash(CBoard board) noexcept
cdef uint64 c_update_hash_move(uint64 old_hash, int old_turn, int new_turn,
                                int start_x, int start_y,
                                int target_x, int target_y,
                                int team, int value,
                                int8 captured_field) noexcept nogil
