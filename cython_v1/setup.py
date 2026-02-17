from setuptools import setup, Extension
from Cython.Build import cythonize

extensions = [
    Extension(
        "cython_core.board",
        ["cython_core/board.pyx"],
        extra_compile_args=["-O3", "-ffast-math"],
    ),
    Extension(
        "cython_core.zobrist",
        ["cython_core/zobrist.pyx"],
        extra_compile_args=["-O3", "-ffast-math"],
    ),
    Extension(
        "cython_core.evaluate",
        ["cython_core/evaluate.pyx"],
        extra_compile_args=["-O3", "-ffast-math"],
    ),
    Extension(
        "cython_core.search",
        ["cython_core/search.pyx"],
        extra_compile_args=["-O3", "-ffast-math"],
    ),
]

setup(
    name="piranhas-cython",
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            "language_level": 3,
            "boundscheck": False,
            "wraparound": False,
            "cdivision": True,
        },
    ),
)
