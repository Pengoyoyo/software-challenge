# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True

cimport cython

from .board cimport CBoard, get_team, get_value, uint64, int8

cdef uint64[10][10][4][5] ZOBRIST_PIECE  # [x][y][team][value]
cdef uint64[61] ZOBRIST_TURN
cdef bint _initialized = False

# xorshift64 state
cdef uint64 _xorshift_state = 0x123456789ABCDEF0ULL


cdef inline uint64 _xorshift64() noexcept nogil:
    global _xorshift_state
    cdef uint64 x = _xorshift_state
    x ^= x << 13
    x ^= x >> 7
    x ^= x << 17
    _xorshift_state = x
    return x


cpdef void init_zobrist():
    global _initialized, _xorshift_state
    if _initialized:
        return

    cdef int x, y, t, v, i
    _xorshift_state = 0x123456789ABCDEF0ULL

    for x in range(10):
        for y in range(10):
            for t in range(4):
                for v in range(5):
                    ZOBRIST_PIECE[x][y][t][v] = _xorshift64()

    for i in range(61):
        ZOBRIST_TURN[i] = _xorshift64()

    _initialized = True


cdef uint64 c_compute_hash(CBoard board) noexcept:
    cdef uint64 h = ZOBRIST_TURN[board.turn]
    cdef int x, y, team, value
    cdef int8 field

    for x in range(10):
        for y in range(10):
            field = board.get_field(x, y)
            team = get_team(field)
            if team != 0:
                value = get_value(field)
                h ^= ZOBRIST_PIECE[x][y][team][value]

    return h


cdef uint64 c_update_hash_move(
    uint64 old_hash,
    int old_turn,
    int new_turn,
    int start_x, int start_y,
    int target_x, int target_y,
    int team, int value,
    int8 captured_field
) noexcept nogil:
    cdef uint64 h = old_hash
    cdef int cap_team, cap_value

    # Update turn
    h ^= ZOBRIST_TURN[old_turn]
    h ^= ZOBRIST_TURN[new_turn]

    # Remove piece from start
    h ^= ZOBRIST_PIECE[start_x][start_y][team][value]

    # Remove captured piece from target (BUG FIX: was missing before)
    cap_team = get_team(captured_field)
    if cap_team != 0:
        cap_value = get_value(captured_field)
        h ^= ZOBRIST_PIECE[target_x][target_y][cap_team][cap_value]

    # Place piece at target
    h ^= ZOBRIST_PIECE[target_x][target_y][team][value]

    return h


# Keep Python-accessible wrappers for backward compatibility
cpdef uint64 compute_hash(CBoard board):
    return c_compute_hash(board)


cpdef uint64 update_hash_move(
    uint64 old_hash,
    int old_turn,
    int new_turn,
    int start_x, int start_y,
    int target_x, int target_y,
    int team, int value
):
    # Legacy wrapper without capture - pass FIELD_EMPTY
    return c_update_hash_move(old_hash, old_turn, new_turn,
                               start_x, start_y, target_x, target_y,
                               team, value, 0)
