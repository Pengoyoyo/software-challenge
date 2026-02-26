# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True

cimport cython
from libc.stdlib cimport rand, srand

from .board cimport CBoard, get_team, get_value, uint64

cdef uint64[10][10][4][5] ZOBRIST_PIECE  # [x][y][team][value]
cdef uint64[61] ZOBRIST_TURN
cdef bint _initialized = False


cpdef void init_zobrist():
    global _initialized
    if _initialized:
        return

    cdef int x, y, t, v, i
    srand(42)

    for x in range(10):
        for y in range(10):
            for t in range(4):
                for v in range(5):
                    ZOBRIST_PIECE[x][y][t][v] = _rand64()

    for i in range(61):
        ZOBRIST_TURN[i] = _rand64()

    _initialized = True


cdef inline uint64 _rand64() noexcept nogil:
    return (
        (<uint64>rand() << 48) ^
        (<uint64>rand() << 32) ^
        (<uint64>rand() << 16) ^
        <uint64>rand()
    )


cpdef uint64 compute_hash(CBoard board):
    cdef uint64 h = ZOBRIST_TURN[board.turn]
    cdef int x, y, team, value
    cdef int8 field

    for x in range(10):
        for y in range(10):
            field = board.get_field(x, y)
            team = get_team(field)
            if team != 0:  # Nicht leer
                value = get_value(field)
                h ^= ZOBRIST_PIECE[x][y][team][value]

    return h


cpdef uint64 update_hash_move(
    uint64 old_hash,
    int old_turn,
    int new_turn,
    int start_x, int start_y,
    int target_x, int target_y,
    int team, int value
):
    cdef uint64 h = old_hash

    h ^= ZOBRIST_TURN[old_turn]
    h ^= ZOBRIST_TURN[new_turn]

    h ^= ZOBRIST_PIECE[start_x][start_y][team][value]

    h ^= ZOBRIST_PIECE[target_x][target_y][team][value]

    return h


ctypedef signed char int8
