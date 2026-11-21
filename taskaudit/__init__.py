"""Execution-backed label audit for SWE-bench-format task collections.

The package name is used as the prefix of container names, image tags, labels and environment variables; it is read
from ``NAME`` below, so renaming the package means renaming this directory and its entries in pyproject.toml.
"""
NAME = __name__.split(".")[0]
ENV_PREFIX = NAME.upper()
