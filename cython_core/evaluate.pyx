# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True

cimport cython
from libc.math cimport sqrt

from .board cimport CBoard, get_team, get_value, int8, uint64

DEF WIN_SCORE = 100000.0
DEF TEAM_NONE = 0
DEF TEAM_ONE = 1
DEF TEAM_TWO = 2

# Nachbar-Offsets für Schwarm-BFS (8 Richtungen)
cdef int[8][2] NEIGHBOR_OFFSETS = [
    [-1, -1], [-1, 0], [-1, 1],
    [0, -1],           [0, 1],
    [1, -1],  [1, 0],  [1, 1]
]


cdef struct SwarmData:
    int best_value
    int num_swarms
    int best_swarm_size
    double center_x
    double center_y
    uint64 best_swarm_mask_lo
    uint64 best_swarm_mask_hi


cdef SwarmData compute_swarm_data(CBoard board, int team) noexcept:
    cdef SwarmData data
    data.best_value = 0
    data.num_swarms = 0
    data.best_swarm_size = 0
    data.center_x = 4.5
    data.center_y = 4.5
    data.best_swarm_mask_lo = 0
    data.best_swarm_mask_hi = 0

    cdef bint[100] visited
    cdef int[100] queue_x
    cdef int[100] queue_y
    cdef int[100] current_swarm_indices
    cdef int x, y, i, qfront, qback, nx, ny, idx, cx, cy, cidx, nidx, bidx
    cdef int swarm_value, swarm_size
    cdef int8 field
    cdef double sx, sy

    for i in range(100):
        visited[i] = False

    for x in range(10):
        for y in range(10):
            idx = x * 10 + y
            if visited[idx]:
                continue

            field = board.fields[idx]
            if get_team(field) != team:
                continue

            data.num_swarms += 1
            swarm_value = 0
            swarm_size = 0
            sx = 0.0
            sy = 0.0

            qfront = 0
            qback = 0
            queue_x[qback] = x
            queue_y[qback] = y
            qback += 1
            visited[idx] = True

            while qfront < qback:
                cx = queue_x[qfront]
                cy = queue_y[qfront]
                qfront += 1

                cidx = cx * 10 + cy
                field = board.fields[cidx]
                swarm_value += get_value(field)
                current_swarm_indices[swarm_size] = cidx
                swarm_size += 1
                sx += cx
                sy += cy

                for i in range(8):
                    nx = cx + NEIGHBOR_OFFSETS[i][0]
                    ny = cy + NEIGHBOR_OFFSETS[i][1]

                    if nx < 0 or nx >= 10 or ny < 0 or ny >= 10:
                        continue

                    nidx = nx * 10 + ny
                    if visited[nidx]:
                        continue

                    if get_team(board.fields[nidx]) == team:
                        visited[nidx] = True
                        queue_x[qback] = nx
                        queue_y[qback] = ny
                        qback += 1

            if swarm_value > data.best_value:
                data.best_value = swarm_value
                data.best_swarm_size = swarm_size
                if swarm_size > 0:
                    data.center_x = sx / swarm_size
                    data.center_y = sy / swarm_size

                data.best_swarm_mask_lo = 0
                data.best_swarm_mask_hi = 0
                for i in range(swarm_size):
                    bidx = current_swarm_indices[i]
                    if bidx < 64:
                        data.best_swarm_mask_lo |= (1ULL << bidx)
                    else:
                        data.best_swarm_mask_hi |= (1ULL << (bidx - 64))

    return data


cdef bint is_in_best_swarm(SwarmData* data, int idx) noexcept:
    if idx < 64:
        return (data.best_swarm_mask_lo & (1ULL << idx)) != 0
    else:
        return (data.best_swarm_mask_hi & (1ULL << (idx - 64))) != 0


cpdef double evaluate(CBoard board, int our_team):
    cdef int opp_team = TEAM_TWO if our_team == TEAM_ONE else TEAM_ONE

    cdef SwarmData our_data = compute_swarm_data(board, our_team)
    cdef SwarmData opp_data = compute_swarm_data(board, opp_team)

    if our_data.num_swarms == 0:
        return -WIN_SCORE
    if opp_data.num_swarms == 0:
        return WIN_SCORE

    if board.turn >= 60:
        if our_data.best_value > opp_data.best_value:
            return WIN_SCORE
        elif opp_data.best_value > our_data.best_value:
            return -WIN_SCORE

    cdef double value = 0.0

    value += (our_data.best_value - opp_data.best_value) * 18.0
    value -= (our_data.num_swarms - 1) * 4.0
    value += (opp_data.num_swarms - 1) * 4.0

    cdef int our_material = 0
    cdef int opp_material = 0
    cdef int our_isolated = 0
    cdef int opp_isolated = 0
    cdef double our_dist = 0.0
    cdef double opp_dist = 0.0
    cdef int x, y, idx, t, val
    cdef double dx, dy
    cdef int8 field

    for x in range(10):
        for y in range(10):
            idx = x * 10 + y
            field = board.fields[idx]
            t = get_team(field)

            if t == TEAM_NONE:
                continue

            val = get_value(field)

            if t == our_team:
                our_material += val
                if not is_in_best_swarm(&our_data, idx):
                    dx = x - our_data.center_x
                    dy = y - our_data.center_y
                    our_dist += sqrt(dx * dx + dy * dy)
                    our_isolated += val
            elif t == opp_team:
                opp_material += val
                if not is_in_best_swarm(&opp_data, idx):
                    dx = x - opp_data.center_x
                    dy = y - opp_data.center_y
                    opp_dist += sqrt(dx * dx + dy * dy)
                    opp_isolated += val

    value += (our_material - opp_material) * 2.0
    value -= our_isolated * 3.0
    value += opp_isolated * 3.0
    value -= our_dist * 0.7
    value += opp_dist * 0.7

    return value


cpdef tuple get_swarm_info(CBoard board, int team):
    cdef SwarmData data = compute_swarm_data(board, team)
    return (data.best_value, data.num_swarms, data.best_swarm_size,
            data.center_x, data.center_y)


cpdef bint is_terminal(CBoard board, int our_team):
    cdef int opp_team = TEAM_TWO if our_team == TEAM_ONE else TEAM_ONE
    cdef SwarmData our_data = compute_swarm_data(board, our_team)
    cdef SwarmData opp_data = compute_swarm_data(board, opp_team)

    if our_data.num_swarms == 0 or opp_data.num_swarms == 0:
        return True
    if board.turn >= 60:
        return True
    return False
