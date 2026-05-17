#!/bin/sh
set -e
export XDG_CACHE_HOME=./my_player/.pip_cache
export PYTHONPATH=./my_player/packages:./my_player:$PYTHONPATH

pip install --no-index --find-links=./my_player/dependencies/ \
    socha xsdata setuptools \
    --target=./my_player/packages/ \
    --cache-dir=./my_player/.pip_cache

python3 ./my_player/logic.py "$@"
