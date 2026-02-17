# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True

cimport cython
from libc.stdlib cimport malloc, free
from libc.string cimport memcpy, memset
from libc.time cimport clock, CLOCKS_PER_SEC
from libc.math cimport log2

from .board cimport CBoard, CMove, CMoveList, get_team, get_value, uint64, int8
from .board cimport c_generate_moves, c_apply_move_inplace, c_undo_move, c_get_target
from .board import from_game_state
from .zobrist cimport c_compute_hash, c_update_hash_move, init_zobrist
from .evaluate cimport c_evaluate, c_evaluate_with_mobility

DEF INF = 1000000.0
DEF WIN_SCORE = 100000.0
DEF TT_SIZE = 1048576
DEF TT_EXACT = 0
DEF TT_LOWER = 1
DEF TT_UPPER = 2
DEF TEAM_NONE = 0
DEF TEAM_ONE = 1
DEF TEAM_TWO = 2
DEF MAX_DEPTH = 40
DEF HISTORY_CAP = 1000000
DEF NULL_MOVE_R = 2

cdef struct TTEntry:
    uint64 hash_key
    double score
    int depth
    int flag
    CMove best_move
    bint has_move

cdef TTEntry* tt = NULL

# Search state as module-level C variables for zero-overhead access
cdef int g_our_team
cdef int g_opp_team
cdef double g_start_time
cdef double g_time_limit
cdef int g_nodes_searched
cdef int g_tt_hits
cdef int g_max_depth_reached
cdef bint g_timeout_flag

# Killer moves: store actual CMove coordinates, not indices
cdef CMove g_killer_moves[MAX_DEPTH][2]
cdef bint g_killer_valid[MAX_DEPTH][2]

# History heuristic table
cdef int g_history[800]


cpdef void init_search():
    global tt
    cdef int i
    init_zobrist()

    if tt == NULL:
        tt = <TTEntry*>malloc(TT_SIZE * sizeof(TTEntry))
        memset(tt, 0, TT_SIZE * sizeof(TTEntry))
        for i in range(TT_SIZE):
            tt[i].depth = -1
            tt[i].has_move = False


cpdef void clear_tt():
    global tt
    cdef int i
    if tt != NULL:
        memset(tt, 0, TT_SIZE * sizeof(TTEntry))
        for i in range(TT_SIZE):
            tt[i].depth = -1
            tt[i].has_move = False


cdef inline void reset_search_state(int our_team, double time_limit) noexcept nogil:
    global g_our_team, g_opp_team, g_start_time, g_time_limit
    global g_nodes_searched, g_tt_hits, g_max_depth_reached, g_timeout_flag
    cdef int d, s, i

    g_our_team = our_team
    g_opp_team = TEAM_TWO if our_team == TEAM_ONE else TEAM_ONE
    g_start_time = <double>clock() / CLOCKS_PER_SEC
    g_time_limit = time_limit
    g_nodes_searched = 0
    g_tt_hits = 0
    g_max_depth_reached = 0
    g_timeout_flag = False

    for d in range(MAX_DEPTH):
        for s in range(2):
            g_killer_valid[d][s] = False

    for i in range(800):
        g_history[i] = 0


cdef inline bint check_timeout() noexcept nogil:
    global g_timeout_flag
    if g_timeout_flag:
        return True
    cdef double elapsed = (<double>clock() / CLOCKS_PER_SEC) - g_start_time
    if elapsed >= g_time_limit:
        g_timeout_flag = True
        return True
    return False


cdef inline bint cmove_equals(CMove* a, CMove* b) noexcept nogil:
    return (a.start_x == b.start_x and a.start_y == b.start_y and
            a.direction == b.direction)


cdef inline void update_killer(CMove* move, int depth) noexcept nogil:
    if depth < 0 or depth >= MAX_DEPTH:
        return
    if not g_killer_valid[depth][0] or not cmove_equals(&g_killer_moves[depth][0], move):
        g_killer_moves[depth][1] = g_killer_moves[depth][0]
        g_killer_valid[depth][1] = g_killer_valid[depth][0]
        g_killer_moves[depth][0] = move[0]
        g_killer_valid[depth][0] = True


cdef inline void update_history(CMove* move, int depth) noexcept nogil:
    cdef int key = (move.start_x * 10 + move.start_y) * 8 + move.direction
    if key < 800:
        g_history[key] += depth * depth
        if g_history[key] > HISTORY_CAP:
            g_history[key] = HISTORY_CAP


cdef inline int get_history(CMove* move) noexcept nogil:
    cdef int key = (move.start_x * 10 + move.start_y) * 8 + move.direction
    if key < 800:
        return g_history[key]
    return 0


cdef inline void age_history() noexcept nogil:
    cdef int i
    for i in range(800):
        g_history[i] >>= 1


# Move scoring for ordering
cdef inline int score_move(CMove* move, int depth, CMove* tt_move, bint has_tt,
                            CBoard board) noexcept:
    cdef int score = 0

    # TT move gets highest priority
    if has_tt and cmove_equals(move, tt_move):
        return 1000000

    # Killer moves
    if 0 <= depth < MAX_DEPTH:
        if g_killer_valid[depth][0] and cmove_equals(move, &g_killer_moves[depth][0]):
            score += 50000
        elif g_killer_valid[depth][1] and cmove_equals(move, &g_killer_moves[depth][1]):
            score += 40000

    # MVV capture bonus: prioritize capturing high-value pieces
    cdef int8 target_field = board.get_field(move.target_x, move.target_y)
    cdef int target_team = get_team(target_field)
    if target_team != TEAM_NONE:
        score += 100000 + get_value(target_field) * 1000

    # History heuristic
    score += get_history(move)

    # Center proximity
    cdef int cx = move.target_x - 5
    cdef int cy = move.target_y - 5
    if cx < 0:
        cx = -cx
    if cy < 0:
        cy = -cy
    score -= (cx + cy) * 5

    return score


# Selection sort: pick best move and swap to position i
cdef inline void order_moves_selection(CMoveList* moves, int* scores, int from_idx) noexcept nogil:
    cdef int best_idx = from_idx
    cdef int best_score = scores[from_idx]
    cdef int j
    cdef CMove tmp
    cdef int tmp_score

    for j in range(from_idx + 1, moves.count):
        if scores[j] > best_score:
            best_score = scores[j]
            best_idx = j

    if best_idx != from_idx:
        tmp = moves.moves[from_idx]
        moves.moves[from_idx] = moves.moves[best_idx]
        moves.moves[best_idx] = tmp
        tmp_score = scores[from_idx]
        scores[from_idx] = scores[best_idx]
        scores[best_idx] = tmp_score


cdef inline int compute_lmr_reduction(int depth, int move_num) noexcept nogil:
    # Logarithmic LMR: more aggressive reductions for later moves
    # Cap reduction so depth - 1 - reduction >= 0
    cdef int r
    if depth < 3 or move_num < 4:
        return 0
    if move_num < 8:
        r = 1
    elif move_num < 16:
        r = 2
    else:
        r = 3
    if r >= depth:
        r = depth - 1
    if r < 0:
        r = 0
    return r


cdef double negamax(
    CBoard board,
    uint64 state_hash,
    int depth,
    double alpha,
    double beta,
    bint is_pv,
    bint allow_null,
    int ply
):
    global g_timeout_flag, g_nodes_searched, g_tt_hits, g_max_depth_reached

    # All cdef declarations at top of function
    cdef double alpha_orig = alpha
    cdef int tt_idx = <int>(state_hash % TT_SIZE)
    cdef TTEntry* entry = &tt[tt_idx]
    cdef CMove tt_move
    cdef bint has_tt_move = False
    cdef int current_team, color
    cdef CMoveList moves
    cdef uint64 null_hash
    cdef double null_score
    cdef int[200] scores
    cdef int i
    cdef double best_score
    cdef CMove best_move
    cdef double score
    cdef int8 captured
    cdef int team, value
    cdef uint64 new_hash
    cdef int reduction
    cdef CMove* m
    cdef int8 target_field_before
    cdef int flag

    if g_timeout_flag:
        return 0.0
    if (g_nodes_searched & 1023) == 0:
        if check_timeout():
            return 0.0

    g_nodes_searched += 1
    if ply > g_max_depth_reached:
        g_max_depth_reached = ply

    # TT probe
    if entry.hash_key == state_hash:
        if entry.has_move:
            tt_move = entry.best_move
            has_tt_move = True

        if entry.depth >= depth:
            g_tt_hits += 1

            if entry.flag == TT_EXACT:
                return entry.score
            elif entry.flag == TT_LOWER:
                if entry.score > alpha:
                    alpha = entry.score
            elif entry.flag == TT_UPPER:
                if entry.score < beta:
                    beta = entry.score

            if alpha >= beta:
                return entry.score

    # Determine current team and perspective
    current_team = TEAM_ONE if (board.turn % 2 == 0) else TEAM_TWO
    color = 1 if current_team == g_our_team else -1

    if depth == 0:
        return color * c_evaluate(board, g_our_team)

    c_generate_moves(board, current_team, &moves)

    if moves.count == 0:
        return color * c_evaluate(board, g_our_team)

    # Null-move pruning
    if allow_null and not is_pv and depth >= 3 and board.turn < 50:
        board.turn += 1
        null_hash = state_hash ^ 0xDEADBEEFCAFEBABEULL
        null_score = -negamax(board, null_hash,
                               depth - 1 - NULL_MOVE_R, -beta, -beta + 1,
                               False, False, ply + 1)
        board.turn -= 1

        if g_timeout_flag:
            return 0.0

        if null_score >= beta:
            return null_score

    # Score moves for ordering
    for i in range(moves.count):
        scores[i] = score_move(&moves.moves[i], depth, &tt_move, has_tt_move, board)

    best_score = -INF
    best_move = moves.moves[0]

    for i in range(moves.count):
        if g_timeout_flag:
            return 0.0

        # Incremental selection sort - pick best remaining move
        order_moves_selection(&moves, scores, i)

        m = &moves.moves[i]

        # Get piece info before applying move
        team = get_team(board.get_field(m.start_x, m.start_y))
        value = get_value(board.get_field(m.start_x, m.start_y))
        target_field_before = board.get_field(m.target_x, m.target_y)

        # Apply move in-place
        captured = c_apply_move_inplace(board, m)

        # Incremental hash update with capture fix
        new_hash = c_update_hash_move(
            state_hash, board.turn - 1, board.turn,
            m.start_x, m.start_y, m.target_x, m.target_y,
            team, value, target_field_before
        )

        # LMR
        reduction = compute_lmr_reduction(depth, i)

        if reduction > 0 and not is_pv:
            score = -negamax(board, new_hash, depth - 1 - reduction,
                             -alpha - 1, -alpha, False, True, ply + 1)
            if not g_timeout_flag and score > alpha:
                score = -negamax(board, new_hash, depth - 1,
                                 -beta, -alpha, False, True, ply + 1)
        elif not is_pv or i == 0:
            score = -negamax(board, new_hash, depth - 1,
                             -beta, -alpha, i == 0 and is_pv, True, ply + 1)
        else:
            # PVS: zero-window search first
            score = -negamax(board, new_hash, depth - 1,
                             -alpha - 1, -alpha, False, True, ply + 1)
            if not g_timeout_flag and score > alpha and score < beta:
                score = -negamax(board, new_hash, depth - 1,
                                 -beta, -alpha, True, True, ply + 1)

        # Undo move
        c_undo_move(board, m, captured)

        if g_timeout_flag:
            return 0.0

        if score > best_score:
            best_score = score
            best_move = m[0]

        if score > alpha:
            alpha = score
            update_history(m, depth)

        if alpha >= beta:
            update_killer(m, depth)
            break

    # Store in TT
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
    entry.best_move = best_move
    entry.has_move = True

    return best_score


cpdef object iterative_deepening(object game_state, int our_team, double time_limit):
    from socha import Move, Coordinate, Direction
    import sys

    init_search()

    cdef CBoard board = from_game_state(game_state)
    cdef int current_team = TEAM_ONE if (board.turn % 2 == 0) else TEAM_TWO
    cdef CMoveList moves
    cdef uint64 state_hash
    cdef int color
    cdef CMove best_move
    cdef double best_score
    cdef int depth
    cdef double score
    cdef double asp_alpha, asp_beta, asp_delta
    cdef int tt_idx
    cdef TTEntry* entry

    c_generate_moves(board, current_team, &moves)

    if moves.count == 0:
        py_moves = game_state.possible_moves()
        return py_moves[0] if py_moves else None

    if moves.count == 1:
        directions = Direction.all_directions()
        return Move(Coordinate(moves.moves[0].start_x, moves.moves[0].start_y),
                    directions[moves.moves[0].direction])

    reset_search_state(our_team, time_limit)

    state_hash = c_compute_hash(board)
    color = 1 if current_team == our_team else -1

    best_move = moves.moves[0]
    best_score = -INF
    depth = 1

    print(f"Cython Search: {moves.count} moves, team={our_team}")
    sys.stdout.flush()

    while depth <= 30:
        g_timeout_flag = False

        # Aspiration windows
        if depth >= 4 and best_score > -WIN_SCORE + 100 and best_score < WIN_SCORE - 100:
            asp_delta = 25.0
            asp_alpha = best_score - asp_delta
            asp_beta = best_score + asp_delta

            while True:
                score = negamax(board, state_hash, depth,
                                asp_alpha, asp_beta, True, False, 0)

                if g_timeout_flag:
                    break

                if score <= asp_alpha:
                    asp_delta *= 4.0
                    asp_alpha = best_score - asp_delta
                    if asp_alpha < -INF + 1:
                        asp_alpha = -INF
                elif score >= asp_beta:
                    asp_delta *= 4.0
                    asp_beta = best_score + asp_delta
                    if asp_beta > INF - 1:
                        asp_beta = INF
                else:
                    break
        else:
            score = negamax(board, state_hash, depth,
                            -INF, INF, True, False, 0)

        if g_timeout_flag:
            break

        # Retrieve best move from TT
        tt_idx = <int>(state_hash % TT_SIZE)
        entry = &tt[tt_idx]
        if entry.hash_key == state_hash and entry.has_move:
            best_move = entry.best_move
            best_score = score

        elapsed = (<double>clock() / CLOCKS_PER_SEC) - g_start_time
        nps = int(g_nodes_searched / elapsed) if elapsed > 0 else 0
        print(f"d{depth}: {score:.0f} | {g_nodes_searched}n {g_tt_hits}h {nps}nps {elapsed:.2f}s")
        sys.stdout.flush()

        if score > WIN_SCORE - 100 or score < -WIN_SCORE + 100:
            break

        # Age history between depths
        age_history()

        depth += 1

        if elapsed > time_limit * 0.5:
            break

    directions = Direction.all_directions()
    return Move(
        Coordinate(best_move.start_x, best_move.start_y),
        directions[best_move.direction]
    )
