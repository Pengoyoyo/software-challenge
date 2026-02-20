from setuptools import Extension, setup
from Cython.Build import cythonize

extensions = [
    Extension(
        "cython_core.bridge_cy",
        ["cython_core/bridge_cy.pyx"],
    )
]

setup(
    name="cython_v3",
    ext_modules=cythonize(
        extensions,
        compiler_directives={"language_level": "3"},
    ),
)
