# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True

ctypedef signed char int8
ctypedef unsigned long long uint64

cdef int[8][2] DIRECTION_VECTORS

cdef struct CMove:
    int start_x, start_y, direction, target_x, target_y

cdef struct CMoveList:
    CMove moves[200]
    int count

cdef int8 make_field(int team, int value) noexcept nogil
cdef int get_team(int8 field) noexcept nogil
cdef int get_value(int8 field) noexcept nogil

cdef class CBoard:
    cdef int8[100] fields
    cdef public int turn

    cdef int8 get_field(self, int x, int y) noexcept nogil
    cdef void set_field(self, int x, int y, int8 field) noexcept nogil
    cpdef CBoard copy(self)

cdef void c_get_target(CBoard board, int start_x, int start_y, int direction,
                       int* out_x, int* out_y) noexcept
cdef void c_generate_moves(CBoard board, int team, CMoveList* out) noexcept
cdef int8 c_apply_move_inplace(CBoard board, CMove* move) noexcept
cdef void c_undo_move(CBoard board, CMove* move, int8 captured) noexcept

cpdef CBoard from_game_state(object game_state)
cpdef CBoard apply_move(CBoard board, int start_x, int start_y, int direction)
cpdef tuple get_target_position(CBoard board, int start_x, int start_y, int direction)
cpdef list generate_moves(CBoard board, int team)
