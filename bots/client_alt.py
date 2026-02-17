from socha import GameState, RulesEngine, TeamEnum, Coordinate
from starter import start


def groesster_schwarm(game_state: GameState, team: TeamEnum) -> tuple[int, list[Coordinate]]:
    max_value = 0
    groesster_schwarm = []

    for schwarm in RulesEngine.swarms_of_team(game_state.board, team):
        value = 0
        for pos in schwarm:
            value += game_state.board.get_field(pos).get_value()

        if value > max_value:
            max_value = value
            groesster_schwarm = schwarm

    return max_value, groesster_schwarm


def material(game_state: GameState, team: TeamEnum) -> int:
    score = 0
    for row in game_state.board.map:
        for ft in row:
            t = ft.get_team()
            if t is None:
                continue
            v = ft.get_value()
            if t == team:
                score += v
    return score


def einzelfische(game_state: GameState, team: TeamEnum) -> int:
    value = 0
    for schwarm in RulesEngine.swarms_of_team(game_state.board, team):
        if len(schwarm) == 1:
            value += game_state.board.get_field(schwarm[0]).get_value()
    return value


def mean(l: list[int]) -> int:
    return round(sum(l) / len(l))


def distanz_zum_schwarm(game_state: GameState, team: TeamEnum) -> float:
    value, schwarm = groesster_schwarm(game_state, team)
    ziel = Coordinate(mean([pos.x for pos in schwarm]), mean([pos.y for pos in schwarm]))
    score = 0.0
    for x, row in enumerate(game_state.board.map):
        for y, ft in enumerate(row):
            t = ft.get_team()
            if t is None:
                continue
            pos = Coordinate(x, y)
            if t == team and pos not in schwarm:
                v = pos.get_difference(ziel)
                score += v.get_length()
    return score


def evaluate(game_state: GameState, our_team: TeamEnum, opp_team: TeamEnum) -> float:
    value = 0
    value += groesster_schwarm(game_state, our_team)[0] * 10
    value -= groesster_schwarm(game_state, opp_team)[0] * 10
    value += material(game_state, our_team)
    value -= material(game_state, opp_team)
    value -= einzelfische(game_state, our_team) * 2
    value += einzelfische(game_state, opp_team) * 2
    value -= distanz_zum_schwarm(game_state, our_team)
    value += distanz_zum_schwarm(game_state, opp_team)
    return value


start(evaluate)