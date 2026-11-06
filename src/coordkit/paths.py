"""Import paths baked into hooks and jobs, without pinning a release symlink target."""
import os
from pathlib import Path


def install_root():
    """Return a PYTHONPATH root, or None for a normal wheel/venv install.

    COORDKIT_HOME points at an import root (or a checkout containing src/).
    A managed release may live at current/src/coordkit or current/coordkit.
    Keep that lexical `current` path so switching releases updates existing jobs.
    """
    override = os.environ.get('COORDKIT_HOME')
    if override:
        root = Path(override).expanduser().absolute()
        return root / 'src' if (root / 'src' / 'coordkit').is_dir() else root
    package = Path(__file__).absolute().parent
    if any(p.name in ('site-packages', 'dist-packages') for p in package.parents):
        return None
    if any(p.name == 'current' for p in package.parents):
        return package.parent
    # Some importers supply the physical release path. Prefer its stable sibling
    # only when it names this exact release; never redirect an unrelated checkout.
    for parent in package.parents:
        if parent.parent.name == 'releases':
            current = parent.parent.parent / 'current'
            if current.is_dir() and current.resolve() == parent.resolve():
                return current / package.parent.relative_to(parent)
    return package.resolve().parent


def python_env():
    root = install_root()
    return {'PYTHONPATH': str(root)} if root is not None else {}


def env_args():
    return ['env', *[f'{key}={value}' for key, value in python_env().items()]]
