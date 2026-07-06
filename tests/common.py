"""Load the extensionless executable scripts as importable modules."""

import importlib.machinery
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(modname, filename):
    loader = importlib.machinery.SourceFileLoader(modname, os.path.join(ROOT, filename))
    spec = importlib.util.spec_from_loader(modname, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod
