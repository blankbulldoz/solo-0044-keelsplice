"""Serve the small, explicit set of packaged workbench stylesheets."""

from .errors import invalid_route
from .render import DIRECTORY

STYLES = {'tokens.css', 'workspace.css', 'controls.css'}


def serve(name):
    if name not in STYLES:
        raise invalid_route()
    return (DIRECTORY / 'static' / name).read_bytes()
