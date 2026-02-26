import os

from socha.starter import Starter

from client_cython import CythonLogic


if __name__ == "__main__":
    # Ensure baseline client always uses built-in default eval weights.
    os.environ.pop("CYTHON_V2_EVAL_PARAMS", None)
    print("=" * 50)
    print("Cython-v2 Baseline Bot gestartet")
    print("=" * 50)
    Starter(CythonLogic())
