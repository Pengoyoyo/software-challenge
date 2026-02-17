# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True

cimport cython
from libc.string cimport memcpy

DEF TEAM_NONE = 0
DEF TEAM_ONE = 1
DEF TEAM_TWO = 2
DEF TEAM_SQUID = 3
DEF FIELD_EMPTY = 0
DEF FIELD_TYPE_SQUID = 6

# Richtungsvektoren: Up, UpRight, Right, DownRight, Down, DownLeft, Left, UpLeft
cdef int[8][2] DIRECTION_VECTORS = [
    [0, 1],
    [1, 1],
    [1, 0],
    [1, -1],
    [0, -1],
    [-1, -1],
    [-1, 0],
    [-1, 1],
]


cdef int8 make_field(int team, int value) noexcept nogil:
    return <int8>((team & 0x3) | ((value & 0x7) << 2))


cdef int get_team(int8 field) noexcept nogil:
    return field & 0x3


cdef int get_value(int8 field) noexcept nogil:
    return (field >> 2) & 0x7


cdef class CBoard:
    def __cinit__(self):
        cdef int i
        for i in range(100):
            self.fields[i] = FIELD_EMPTY
        self.turn = 0

    cdef int8 get_field(self, int x, int y) noexcept nogil:
        return self.fields[x * 10 + y]

    cdef void set_field(self, int x, int y, int8 field) noexcept nogil:
        self.fields[x * 10 + y] = field

    cpdef CBoard copy(self):
        cdef CBoard new_board = CBoard.__new__(CBoard)
        memcpy(new_board.fields, self.fields, 100 * sizeof(int8))
        new_board.turn = self.turn
        return new_board

    def __repr__(self):
        lines = []
        for y in range(9, -1, -1):
            row = []
            for x in range(10):
                f = self.fields[x * 10 + y]
                t = get_team(f)
                v = get_value(f)
                if t == TEAM_NONE:
                    row.append(".")
                elif t == TEAM_ONE:
                    row.append(str(v))
                else:
                    row.append(chr(ord('a') + v - 1) if v > 0 else "x")
            lines.append(" ".join(row))
        return "\n".join(lines)


cpdef CBoard from_game_state(object game_state):
    cdef CBoard board = CBoard()
    cdef int x, y, team_int, value, ft_int
    cdef object ft, py_team

    board.turn = game_state.turn

    for y in range(10):
        for x in range(10):
            ft = game_state.board.map[y][x]
            ft_int = int(ft)

            if ft_int == FIELD_TYPE_SQUID:
                board.set_field(x, y, make_field(TEAM_SQUID, 0))
            else:
                py_team = ft.get_team()
                if py_team is None:
                    board.set_field(x, y, FIELD_EMPTY)
                else:
                    team_int = TEAM_ONE if int(py_team) == 0 else TEAM_TWO
                    value = ft.get_value()
                    board.set_field(x, y, make_field(team_int, value))

    return board


cdef int count_fish_on_line(CBoard board, int x, int y, int dx, int dy) noexcept:
    cdef int count = 0
    cdef int nx = x + dx
    cdef int ny = y + dy
    cdef int8 field
    cdef int field_team

    while 0 <= nx < 10 and 0 <= ny < 10:
        field = board.get_field(nx, ny)
        field_team = get_team(field)
        if field_team == TEAM_ONE or field_team == TEAM_TWO:
            count += 1
        nx += dx
        ny += dy

    return count


cdef void c_get_target(CBoard board, int start_x, int start_y, int direction,
                       int* out_x, int* out_y) noexcept:
    cdef int dx = DIRECTION_VECTORS[direction][0]
    cdef int dy = DIRECTION_VECTORS[direction][1]

    cdef int fish_count = 1
    fish_count += count_fish_on_line(board, start_x, start_y, dx, dy)
    fish_count += count_fish_on_line(board, start_x, start_y, -dx, -dy)

    out_x[0] = start_x + (fish_count * dx)
    out_y[0] = start_y + (fish_count * dy)


cpdef tuple get_target_position(CBoard board, int start_x, int start_y, int direction):
    cdef int target_x, target_y
    c_get_target(board, start_x, start_y, direction, &target_x, &target_y)
    return (target_x, target_y)


cdef bint c_is_move_valid(
    CBoard board,
    int start_x,
    int start_y,
    int direction,
    int team,
    int target_x,
    int target_y
) noexcept:
    cdef int dx = DIRECTION_VECTORS[direction][0]
    cdef int dy = DIRECTION_VECTORS[direction][1]
    cdef int opp_team = TEAM_TWO if team == TEAM_ONE else TEAM_ONE
    cdef int nx, ny
    cdef int8 field
    cdef int field_team

    if target_x < 0 or target_x >= 10 or target_y < 0 or target_y >= 10:
        return False

    if target_x == start_x and target_y == start_y:
        return False

    field = board.get_field(target_x, target_y)
    field_team = get_team(field)

    if field_team == team:
        return False
    if field_team == TEAM_SQUID:
        return False

    nx = start_x + dx
    ny = start_y + dy
    while nx != target_x or ny != target_y:
        field = board.get_field(nx, ny)
        field_team = get_team(field)
        if field_team == opp_team:
            return False
        nx += dx
        ny += dy

    return True


cdef void c_generate_moves(CBoard board, int team, CMoveList* out) noexcept:
    cdef int x, y, d
    cdef int8 field
    cdef int target_x, target_y

    out.count = 0

    for x in range(10):
        for y in range(10):
            field = board.get_field(x, y)
            if get_team(field) != team:
                continue

            for d in range(8):
                c_get_target(board, x, y, d, &target_x, &target_y)
                if c_is_move_valid(board, x, y, d, team, target_x, target_y):
                    out.moves[out.count].start_x = x
                    out.moves[out.count].start_y = y
                    out.moves[out.count].direction = d
                    out.moves[out.count].target_x = target_x
                    out.moves[out.count].target_y = target_y
                    out.count += 1


cdef int8 c_apply_move_inplace(CBoard board, CMove* move) noexcept:
    cdef int8 moving_piece = board.get_field(move.start_x, move.start_y)
    cdef int8 captured = board.get_field(move.target_x, move.target_y)

    board.set_field(move.start_x, move.start_y, FIELD_EMPTY)
    board.set_field(move.target_x, move.target_y, moving_piece)
    board.turn += 1

    return captured


cdef void c_undo_move(CBoard board, CMove* move, int8 captured) noexcept:
    cdef int8 moving_piece = board.get_field(move.target_x, move.target_y)

    board.set_field(move.target_x, move.target_y, captured)
    board.set_field(move.start_x, move.start_y, moving_piece)
    board.turn -= 1


cpdef CBoard apply_move(CBoard board, int start_x, int start_y, int direction):
    cdef CBoard new_board = board.copy()
    cdef int target_x, target_y
    cdef int8 moving_piece = board.get_field(start_x, start_y)

    c_get_target(board, start_x, start_y, direction, &target_x, &target_y)

    new_board.set_field(start_x, start_y, FIELD_EMPTY)
    new_board.set_field(target_x, target_y, moving_piece)
    new_board.turn = board.turn + 1

    return new_board


cpdef list generate_moves(CBoard board, int team):
    cdef CMoveList ml
    cdef list moves = []
    cdef int i

    c_generate_moves(board, team, &ml)

    for i in range(ml.count):
        moves.append((ml.moves[i].start_x, ml.moves[i].start_y,
                       ml.moves[i].direction, ml.moves[i].target_x,
                       ml.moves[i].target_y))

    return moves
