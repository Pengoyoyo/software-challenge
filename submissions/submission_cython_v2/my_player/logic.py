import faulthandler
faulthandler.enable()
import os

from socha import GameState, Move, RulesEngine, TeamEnum
from socha.api.networking.game_client import IClientHandler
from socha.starter import Starter

from cython_core.search import iterative_deepening, init_search
from cython_core.evaluate import set_eval_params, get_eval_params

TIME_LIMIT = 1.8


def apply_env_eval_params() -> None:
    raw = os.environ.get("CYTHON_V2_EVAL_PARAMS", "").strip()
    if not raw:
        return

    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 5:
        print(
            f"Warnung: Ungültiges CYTHON_V2_EVAL_PARAMS Format: '{raw}'",
            flush=True,
        )
        return

    try:
        vals = tuple(float(p) for p in parts)
        set_eval_params(*vals)
        print(f"Eval-Parameter gesetzt: {get_eval_params()}", flush=True)
    except Exception as exc:
        print(f"Warnung: Konnte Eval-Parameter nicht setzen: {exc}", flush=True)


class CythonLogic(IClientHandler):
    def __init__(self):
        self.game_state: GameState | None = None
        self.our_team: TeamEnum | None = None
        self.our_team_int: int = 0

        init_search()
        print("Cython-Module geladen", flush=True)

    def on_update(self, game_state: GameState) -> None:
        self.game_state = game_state
        if self.our_team is None:
            self.our_team = RulesEngine.get_team_on_turn(game_state.turn)
            self.our_team_int = 1 if int(self.our_team) == 0 else 2
            print(f"Team: {self.our_team} (int: {self.our_team_int})", flush=True)

    def calculate_move(self) -> Move:
        assert self.game_state is not None

        print(f"\n=== Zug {self.game_state.turn + 1} ===", flush=True)

        best_move = iterative_deepening(self.game_state, self.our_team_int, TIME_LIMIT)

        print(f"-> {best_move.start} {best_move.direction}", flush=True)
        return best_move

    def on_game_over(self, result) -> None:
        print(f"\n=== Ende ===\n{result}")


if __name__ == "__main__":
    print("=" * 50)
    print("Cython-optimierter Bot gestartet")
    print("=" * 50)
    apply_env_eval_params()
    Starter(CythonLogic())
