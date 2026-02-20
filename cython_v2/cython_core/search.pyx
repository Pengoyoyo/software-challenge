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
from .evaluate cimport c_evaluate, c_is_connected, c_try_terminal_eval

DEF INF = 1000000.0
DEF WIN_SCORE = 100000.0
DEF TT_CLUSTER_SIZE = 4
DEF TT_CLUSTER_COUNT = 262144
DEF TT_CLUSTER_MASK = TT_CLUSTER_COUNT - 1
DEF TT_EXACT = 0
DEF TT_LOWER = 1
DEF TT_UPPER = 2
DEF TEAM_NONE = 0
DEF TEAM_ONE = 1
DEF TEAM_TWO = 2
DEF MAX_DEPTH = 40
DEF HISTORY_CAP = 1000000
DEF NULL_MOVE_R = 2
DEF ENABLE_NULL_MOVE = 1
DEF TIME_USAGE_FRACTION = 0.80
DEF QSEARCH_MAX_DEPTH = 10
DEF COUNTER_TABLE_SIZE = 800

cdef struct TTEntry:
    uint64 hash_key
    double score
    int depth
    int flag
    unsigned char generation
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
cdef unsigned char g_tt_generation = 1

# Killer moves: store actual CMove coordinates, not indices
cdef CMove g_killer_moves[MAX_DEPTH][2]
cdef bint g_killer_valid[MAX_DEPTH][2]

# History heuristic table
cdef int g_history[800]
cdef bint g_history_initialized = False

# Counter-move table (indexed by previous move key)
cdef CMove g_counter_moves[COUNTER_TABLE_SIZE]
cdef bint g_counter_valid[COUNTER_TABLE_SIZE]


cpdef void init_search():
    global tt, g_history_initialized
    cdef int i
    init_zobrist()

    if tt == NULL:
        tt = <TTEntry*>malloc(TT_CLUSTER_COUNT * TT_CLUSTER_SIZE * sizeof(TTEntry))
        memset(tt, 0, TT_CLUSTER_COUNT * TT_CLUSTER_SIZE * sizeof(TTEntry))
        for i in range(TT_CLUSTER_COUNT * TT_CLUSTER_SIZE):
            tt[i].depth = -1
            tt[i].generation = 0
            tt[i].has_move = False

    if not g_history_initialized:
        for i in range(800):
            g_history[i] = 0
        g_history_initialized = True


cpdef void clear_tt():
    global tt
    cdef int i
    if tt != NULL:
        memset(tt, 0, TT_CLUSTER_COUNT * TT_CLUSTER_SIZE * sizeof(TTEntry))
        for i in range(TT_CLUSTER_COUNT * TT_CLUSTER_SIZE):
            tt[i].depth = -1
            tt[i].generation = 0
            tt[i].has_move = False


cdef inline void reset_search_state(int our_team, double time_limit) noexcept nogil:
    global g_our_team, g_opp_team, g_start_time, g_time_limit
    global g_nodes_searched, g_tt_hits, g_max_depth_reached, g_timeout_flag
    global g_tt_generation, g_history_initialized
    cdef int d, s, i

    g_our_team = our_team
    g_opp_team = TEAM_TWO if our_team == TEAM_ONE else TEAM_ONE
    g_start_time = <double>clock() / CLOCKS_PER_SEC
    g_time_limit = time_limit
    g_nodes_searched = 0
    g_tt_hits = 0
    g_max_depth_reached = 0
    g_timeout_flag = False
    g_tt_generation = <unsigned char>(g_tt_generation + 1)
    if g_tt_generation == 0:
        g_tt_generation = 1

    for d in range(MAX_DEPTH):
        for s in range(2):
            g_killer_valid[d][s] = False

    for i in range(COUNTER_TABLE_SIZE):
        g_counter_valid[i] = False

    if not g_history_initialized:
        for i in range(800):
            g_history[i] = 0
        g_history_initialized = True
    else:
        for i in range(800):
            g_history[i] = (g_history[i] * 7) >> 3


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


cdef inline int move_key(CMove* move) noexcept nogil:
    cdef int key = (move.start_x * 10 + move.start_y) * 8 + move.direction
    if key < 0 or key >= COUNTER_TABLE_SIZE:
        return -1
    return key


cdef inline void age_history() noexcept nogil:
    cdef int i
    for i in range(800):
        g_history[i] >>= 1


# Move scoring for ordering
cdef inline int score_move(CMove* move, int depth, CMove* tt_move, bint has_tt,
                            CBoard board, int prev_move_key) noexcept:
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

    # Counter move (response to the previous move at parent ply)
    if 0 <= prev_move_key < COUNTER_TABLE_SIZE and g_counter_valid[prev_move_key]:
        if cmove_equals(move, &g_counter_moves[prev_move_key]):
            score += 45000

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
    if depth < 3 or move_num < 3:
        return 0

    cdef int reduction = 1
    if depth >= 6 and move_num >= 8:
        reduction += 1
    if depth >= 10 and move_num >= 14:
        reduction += 1
    return reduction


cdef inline void count_team_pieces(CBoard board, int* out_one, int* out_two) noexcept:
    cdef int idx
    cdef int team

    out_one[0] = 0
    out_two[0] = 0

    for idx in range(100):
        team = get_team(board.fields[idx])
        if team == TEAM_ONE:
            out_one[0] += 1
        elif team == TEAM_TWO:
            out_two[0] += 1


cdef double quiescence(
    CBoard board,
    double alpha,
    double beta,
    int ply,
    int qdepth
):
    global g_timeout_flag, g_nodes_searched

    cdef int current_team = TEAM_ONE if (board.turn % 2 == 0) else TEAM_TWO
    cdef int opp_team = TEAM_TWO if current_team == TEAM_ONE else TEAM_ONE
    cdef int color = 1 if current_team == g_our_team else -1
    cdef double terminal_score
    cdef int one_count, two_count, own_count, opp_count
    cdef double stand_pat, score
    cdef CMoveList moves
    cdef CMove noisy_moves[200]
    cdef int noisy_scores[200]
    cdef int noisy_count = 0
    cdef int i, j, best_idx, best_score, tmp_score
    cdef CMove tmp_move
    cdef CMove* m
    cdef int8 target_field
    cdef int target_team
    cdef int reduction_score
    cdef int8 captured

    if g_timeout_flag:
        return 0.0
    if (g_nodes_searched & 1023) == 0:
        if check_timeout():
            return 0.0
    g_nodes_searched += 1

    if board.turn >= 60:
        if c_try_terminal_eval(board, g_our_team, &terminal_score):
            return color * terminal_score
        return color * c_evaluate(board, g_our_team)

    count_team_pieces(board, &one_count, &two_count)
    if current_team == TEAM_ONE:
        own_count = one_count
        opp_count = two_count
    else:
        own_count = two_count
        opp_count = one_count

    if own_count == 0 and opp_count > 0:
        return -WIN_SCORE + ply
    if opp_count == 0 and own_count > 0:
        return WIN_SCORE - ply

    if c_is_connected(board, opp_team):
        return -WIN_SCORE + ply

    stand_pat = color * c_evaluate(board, g_our_team)
    if stand_pat >= beta:
        return stand_pat
    if stand_pat > alpha:
        alpha = stand_pat

    if qdepth >= QSEARCH_MAX_DEPTH:
        return stand_pat

    c_generate_moves(board, current_team, &moves)
    for i in range(moves.count):
        target_field = board.get_field(moves.moves[i].target_x, moves.moves[i].target_y)
        target_team = get_team(target_field)
        if target_team == TEAM_NONE:
            continue

        noisy_moves[noisy_count] = moves.moves[i]
        reduction_score = get_value(target_field)
        noisy_scores[noisy_count] = 100000 + reduction_score * 1000
        noisy_count += 1

    if noisy_count == 0:
        return stand_pat

    for i in range(noisy_count):
        if g_timeout_flag:
            return alpha

        best_idx = i
        best_score = noisy_scores[i]
        for j in range(i + 1, noisy_count):
            if noisy_scores[j] > best_score:
                best_score = noisy_scores[j]
                best_idx = j

        if best_idx != i:
            tmp_move = noisy_moves[i]
            noisy_moves[i] = noisy_moves[best_idx]
            noisy_moves[best_idx] = tmp_move
            tmp_score = noisy_scores[i]
            noisy_scores[i] = noisy_scores[best_idx]
            noisy_scores[best_idx] = tmp_score

        m = &noisy_moves[i]
        captured = c_apply_move_inplace(board, m)

        if c_is_connected(board, current_team):
            score = WIN_SCORE - ply
        else:
            score = -quiescence(board, -beta, -alpha, ply + 1, qdepth + 1)

        c_undo_move(board, m, captured)

        if g_timeout_flag:
            return alpha

        if score >= beta:
            return score
        if score > alpha:
            alpha = score

    return alpha


cdef double negamax(
    CBoard board,
    uint64 state_hash,
    int depth,
    double alpha,
    double beta,
    bint is_pv,
    bint allow_null,
    int ply,
    int prev_move_key
):
    global g_timeout_flag, g_nodes_searched, g_tt_hits, g_max_depth_reached

    # All cdef declarations at top of function
    cdef double alpha_orig = alpha
    cdef int tt_base = <int>(state_hash & TT_CLUSTER_MASK) * TT_CLUSTER_SIZE
    cdef int tt_slot
    cdef TTEntry* entry
    cdef CMove tt_move
    cdef bint has_tt_move = False
    cdef int tt_move_depth = -1
    cdef int current_team, opp_team, color
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
    cdef int mkey
    cdef bint quiet
    cdef int8 target_field_before
    cdef int flag
    cdef int replace_idx, replace_quality, age, quality
    cdef double terminal_score
    cdef double static_eval
    cdef int one_count, two_count, own_count, opp_count

    if g_timeout_flag:
        return 0.0
    if (g_nodes_searched & 1023) == 0:
        if check_timeout():
            return 0.0

    g_nodes_searched += 1
    if ply > g_max_depth_reached:
        g_max_depth_reached = ply

    # TT probe (4-way clustered buckets to reduce collision loss)
    for tt_slot in range(TT_CLUSTER_SIZE):
        entry = &tt[tt_base + tt_slot]
        if not entry.has_move or entry.hash_key != state_hash:
            continue

        if entry.depth > tt_move_depth:
            tt_move = entry.best_move
            has_tt_move = True
            tt_move_depth = entry.depth

        if entry.depth < depth:
            continue

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
    opp_team = TEAM_TWO if current_team == TEAM_ONE else TEAM_ONE
    color = 1 if current_team == g_our_team else -1

    if board.turn >= 60:
        if c_try_terminal_eval(board, g_our_team, &terminal_score):
            return color * terminal_score
        return color * c_evaluate(board, g_our_team)

    count_team_pieces(board, &one_count, &two_count)
    if current_team == TEAM_ONE:
        own_count = one_count
        opp_count = two_count
    else:
        own_count = two_count
        opp_count = one_count

    if own_count == 0 and opp_count > 0:
        return -WIN_SCORE + ply
    if opp_count == 0 and own_count > 0:
        return WIN_SCORE - ply

    if c_is_connected(board, opp_team):
        return -WIN_SCORE + ply

    if depth == 0:
        return quiescence(board, alpha, beta, ply, 0)

    static_eval = color * c_evaluate(board, g_our_team)
    if not is_pv and depth <= 3 and static_eval - 120.0 * depth >= beta:
        return static_eval

    c_generate_moves(board, current_team, &moves)

    if moves.count == 0:
        return static_eval

    # Null-move pruning
    if ENABLE_NULL_MOVE and allow_null and not is_pv and depth >= 4 and board.turn < 56 and own_count > 4 and opp_count > 4:
        board.turn += 1
        null_hash = state_hash ^ 0xDEADBEEFCAFEBABEULL
        null_score = -negamax(board, null_hash,
                               depth - 1 - NULL_MOVE_R, -beta, -beta + 1,
                               False, False, ply + 1, -1)
        board.turn -= 1

        if g_timeout_flag:
            return 0.0

        if null_score >= beta:
            return null_score

    # Score moves for ordering
    for i in range(moves.count):
        scores[i] = score_move(&moves.moves[i], depth, &tt_move, has_tt_move, board, prev_move_key)

    best_score = -INF
    best_move = moves.moves[0]

    for i in range(moves.count):
        if g_timeout_flag:
            return 0.0

        # Incremental selection sort - pick best remaining move
        order_moves_selection(&moves, scores, i)

        m = &moves.moves[i]
        mkey = move_key(m)

        # Get piece info before applying move
        team = get_team(board.get_field(m.start_x, m.start_y))
        value = get_value(board.get_field(m.start_x, m.start_y))
        target_field_before = board.get_field(m.target_x, m.target_y)
        quiet = get_team(target_field_before) == TEAM_NONE

        if not is_pv and quiet:
            if depth <= 2 and i >= 3 and static_eval + 130.0 * depth <= alpha:
                continue
            if depth <= 2 and i >= 8 + 4 * depth:
                break

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
                             -alpha - 1, -alpha, False, True, ply + 1, mkey)
            if not g_timeout_flag and score > alpha:
                score = -negamax(board, new_hash, depth - 1,
                                 -beta, -alpha, False, True, ply + 1, mkey)
        elif not is_pv or i == 0:
            score = -negamax(board, new_hash, depth - 1,
                             -beta, -alpha, i == 0 and is_pv, True, ply + 1, mkey)
        else:
            # PVS: zero-window search first
            score = -negamax(board, new_hash, depth - 1,
                             -alpha - 1, -alpha, False, True, ply + 1, mkey)
            if not g_timeout_flag and score > alpha and score < beta:
                score = -negamax(board, new_hash, depth - 1,
                                 -beta, -alpha, True, True, ply + 1, mkey)

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
            if 0 <= prev_move_key < COUNTER_TABLE_SIZE:
                g_counter_moves[prev_move_key] = m[0]
                g_counter_valid[prev_move_key] = True
            update_killer(m, depth)
            break

    # Store in TT
    if best_score <= alpha_orig:
        flag = TT_UPPER
    elif best_score >= beta:
        flag = TT_LOWER
    else:
        flag = TT_EXACT

    # Prefer updating same key if depth improves (or exact score is available).
    for tt_slot in range(TT_CLUSTER_SIZE):
        entry = &tt[tt_base + tt_slot]
        if entry.has_move and entry.hash_key == state_hash:
            if depth >= entry.depth or flag == TT_EXACT:
                entry.hash_key = state_hash
                entry.score = best_score
                entry.depth = depth
                entry.flag = flag
                entry.best_move = best_move
                entry.generation = g_tt_generation
                entry.has_move = True
            return best_score

    # No matching key found: choose empty slot, else weakest depth/age score.
    replace_idx = -1
    replace_quality = 1_000_000
    for tt_slot in range(TT_CLUSTER_SIZE):
        entry = &tt[tt_base + tt_slot]
        if not entry.has_move:
            replace_idx = tt_base + tt_slot
            break

        age = ((<int>g_tt_generation - <int>entry.generation) & 0xFF)
        quality = entry.depth - (age * 2)
        if quality < replace_quality:
            replace_quality = quality
            replace_idx = tt_base + tt_slot

    entry = &tt[replace_idx]
    entry.hash_key = state_hash
    entry.score = best_score
    entry.depth = depth
    entry.flag = flag
    entry.best_move = best_move
    entry.generation = g_tt_generation
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
    cdef int tt_base, tt_slot, best_tt_depth
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
                                asp_alpha, asp_beta, True, False, 0, -1)

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
                            -INF, INF, True, False, 0, -1)

        if g_timeout_flag:
            break

        # Retrieve best move from TT
        tt_base = <int>(state_hash & TT_CLUSTER_MASK) * TT_CLUSTER_SIZE
        best_tt_depth = -1
        for tt_slot in range(TT_CLUSTER_SIZE):
            entry = &tt[tt_base + tt_slot]
            if entry.has_move and entry.hash_key == state_hash:
                if entry.depth > best_tt_depth:
                    best_tt_depth = entry.depth
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

        if elapsed > time_limit * TIME_USAGE_FRACTION:
            break

    directions = Direction.all_directions()
    return Move(
        Coordinate(best_move.start_x, best_move.start_y),
        directions[best_move.direction]
    )
