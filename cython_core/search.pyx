# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True

cimport cython
from libc.stdlib cimport malloc, free
from libc.time cimport clock, CLOCKS_PER_SEC

from .board cimport CBoard, get_team, get_value, uint64, int8
from .board import from_game_state, apply_move, generate_moves, get_target_position
from .zobrist import init_zobrist, compute_hash, update_hash_move
from .evaluate import evaluate

DEF INF = 1000000.0
DEF WIN_SCORE = 100000.0
DEF TT_SIZE = 1048576
DEF TT_EXACT = 0
DEF TT_LOWER = 1
DEF TT_UPPER = 2
DEF TEAM_ONE = 1
DEF TEAM_TWO = 2


cdef struct TTEntry:
    uint64 hash_key
    double score
    int depth
    int flag
    int move_start_x
    int move_start_y
    int move_direction
    int move_target_x
    int move_target_y


cdef TTEntry* tt = NULL


cpdef void init_search():
    global tt
    init_zobrist()

    if tt == NULL:
        tt = <TTEntry*>malloc(TT_SIZE * sizeof(TTEntry))
        for i in range(TT_SIZE):
            tt[i].hash_key = 0
            tt[i].depth = -1
            tt[i].move_start_x = -1


cpdef void clear_tt():
    global tt
    if tt != NULL:
        for i in range(TT_SIZE):
            tt[i].hash_key = 0
            tt[i].depth = -1
            tt[i].move_start_x = -1


cdef class SearchState:
    cdef public int our_team
    cdef public int opp_team
    cdef public double start_time
    cdef public double time_limit
    cdef public int nodes_searched
    cdef public int tt_hits
    cdef public int max_depth_reached

    cdef int[30][2] killer_moves
    cdef int[800] history

    def __cinit__(self, int our_team, double time_limit):
        self.our_team = our_team
        self.opp_team = TEAM_TWO if our_team == TEAM_ONE else TEAM_ONE
        self.time_limit = time_limit
        self.nodes_searched = 0
        self.tt_hits = 0
        self.max_depth_reached = 0

        cdef int d, s, i
        for d in range(30):
            for s in range(2):
                self.killer_moves[d][s] = -1
        for i in range(800):
            self.history[i] = 0

    cdef inline bint is_timeout(self):
        cdef double elapsed = (<double>clock() / CLOCKS_PER_SEC) - self.start_time
        return elapsed >= self.time_limit

    cdef inline void update_killer(self, int move_idx, int depth):
        if depth < 30 and move_idx >= 0:
            if self.killer_moves[depth][0] != move_idx:
                self.killer_moves[depth][1] = self.killer_moves[depth][0]
                self.killer_moves[depth][0] = move_idx

    cdef inline void update_history(self, int start_x, int start_y, int direction, int depth):
        cdef int key = (start_x * 10 + start_y) * 8 + direction
        if key < 800:
            self.history[key] += depth * depth

    cdef inline int get_history(self, int start_x, int start_y, int direction):
        cdef int key = (start_x * 10 + start_y) * 8 + direction
        if key < 800:
            return self.history[key]
        return 0


cdef list order_moves(SearchState state, list moves, int depth, tuple tt_move):
    cdef list scored = []
    cdef int i, score
    cdef tuple m

    for i, m in enumerate(moves):
        score = 0

        if tt_move is not None and m[0] == tt_move[0] and m[1] == tt_move[1] and m[2] == tt_move[2]:
            score += 100000

        if depth < 30:
            if i == state.killer_moves[depth][0]:
                score += 5000
            elif i == state.killer_moves[depth][1]:
                score += 4000

        score += state.get_history(m[0], m[1], m[2])
        score -= abs(m[3] - 5) * 10 + abs(m[4] - 5) * 10

        scored.append((score, i, m))

    scored.sort(reverse=True)
    return [x[2] for x in scored]


cdef tuple alpha_beta(
    SearchState state,
    CBoard board,
    uint64 state_hash,
    int depth,
    double alpha,
    double beta,
    bint maximizing
):
    if state.is_timeout():
        raise Exception("Timeout")

    state.nodes_searched += 1
    if depth > state.max_depth_reached:
        state.max_depth_reached = depth

    cdef double alpha_orig = alpha
    cdef int tt_idx = <int>(state_hash % TT_SIZE)
    cdef TTEntry* entry = &tt[tt_idx]
    cdef tuple tt_move = None

    if entry.hash_key == state_hash:
        if entry.move_start_x >= 0:
            tt_move = (
                entry.move_start_x, entry.move_start_y,
                entry.move_direction, entry.move_target_x, entry.move_target_y
            )

        if entry.depth >= depth:
            state.tt_hits += 1

            if entry.flag == TT_EXACT:
                return (entry.score, tt_move)
            elif entry.flag == TT_LOWER:
                alpha = max(alpha, entry.score)
            elif entry.flag == TT_UPPER:
                beta = min(beta, entry.score)

            if alpha >= beta:
                return (entry.score, tt_move)

    if depth == 0:
        return (evaluate(board, state.our_team), None)

    cdef int current_team = TEAM_ONE if (board.turn % 2 == 0) else TEAM_TWO
    cdef list moves = generate_moves(board, current_team)

    if not moves:
        return (evaluate(board, state.our_team), None)

    moves = order_moves(state, moves, depth, tt_move)

    cdef double best_score
    cdef tuple best_move = moves[0]
    cdef int i
    cdef tuple m
    cdef CBoard new_board
    cdef uint64 new_hash
    cdef double score
    cdef int target_x, target_y, team, value

    if maximizing:
        best_score = -INF
        for i, m in enumerate(moves):
            if state.is_timeout():
                raise Exception("Timeout")

            new_board = apply_move(board, m[0], m[1], m[2])
            target_x, target_y = m[3], m[4]
            team = get_team(board.get_field(m[0], m[1]))
            value = get_value(board.get_field(m[0], m[1]))
            new_hash = update_hash_move(
                state_hash, board.turn, new_board.turn,
                m[0], m[1], target_x, target_y, team, value
            )

            score, _ = alpha_beta(state, new_board, new_hash, depth - 1, alpha, beta, False)

            if score > best_score:
                best_score = score
                best_move = m

            if score > alpha:
                alpha = score
                state.update_history(m[0], m[1], m[2], depth)

            if beta <= alpha:
                state.update_killer(i, depth)
                break
    else:
        best_score = INF
        for i, m in enumerate(moves):
            if state.is_timeout():
                raise Exception("Timeout")

            new_board = apply_move(board, m[0], m[1], m[2])
            target_x, target_y = m[3], m[4]
            team = get_team(board.get_field(m[0], m[1]))
            value = get_value(board.get_field(m[0], m[1]))
            new_hash = update_hash_move(
                state_hash, board.turn, new_board.turn,
                m[0], m[1], target_x, target_y, team, value
            )

            score, _ = alpha_beta(state, new_board, new_hash, depth - 1, alpha, beta, True)

            if score < best_score:
                best_score = score
                best_move = m

            if score < beta:
                beta = score
                state.update_history(m[0], m[1], m[2], depth)

            if beta <= alpha:
                state.update_killer(i, depth)
                break

    cdef int flag
    if best_score <= alpha_orig:
        flag = TT_UPPER
    elif best_score >= beta:
        flag = TT_LOWER
    else:
        flag = TT_EXACT

    entry.hash_key = state_hash
    entry.score = best_score
    entry.depth = depth
    entry.flag = flag
    entry.move_start_x = best_move[0]
    entry.move_start_y = best_move[1]
    entry.move_direction = best_move[2]
    entry.move_target_x = best_move[3]
    entry.move_target_y = best_move[4]

    return (best_score, best_move)


cpdef object iterative_deepening(object game_state, int our_team, double time_limit):
    from socha import Move, Coordinate, Direction

    init_search()

    cdef CBoard board = from_game_state(game_state)
    cdef SearchState state = SearchState(our_team, time_limit)
    state.start_time = <double>clock() / CLOCKS_PER_SEC

    cdef int current_team = TEAM_ONE if (board.turn % 2 == 0) else TEAM_TWO
    cdef list moves = generate_moves(board, current_team)

    if not moves:
        py_moves = game_state.possible_moves()
        return py_moves[0] if py_moves else None

    if len(moves) == 1:
        m = moves[0]
        directions = Direction.all_directions()
        return Move(Coordinate(m[0], m[1]), directions[m[2]])

    cdef uint64 state_hash = compute_hash(board)
    cdef bint maximizing = (current_team == our_team)

    cdef tuple best_move = moves[0]
    cdef double best_score = -INF

    cdef int depth = 1
    cdef double score
    cdef tuple returned_move

    print(f"Cython Search: {len(moves)} moves, team={our_team}")

    while depth <= 30:
        try:
            score, returned_move = alpha_beta(
                state, board, state_hash, depth,
                -INF, INF, maximizing
            )

            if returned_move is not None:
                best_move = returned_move
                best_score = score

            elapsed = (<double>clock() / CLOCKS_PER_SEC) - state.start_time
            nps = int(state.nodes_searched / elapsed) if elapsed > 0 else 0
            print(f"d{depth}: {score:.0f} | {state.nodes_searched}n {state.tt_hits}h {nps}nps {elapsed:.2f}s")

            if abs(score) >= WIN_SCORE - 100:
                break

            depth += 1

            if elapsed > time_limit * 0.6:
                break

        except:
            break

    directions = Direction.all_directions()
    return Move(
        Coordinate(best_move[0], best_move[1]),
        directions[best_move[2]]
    )
