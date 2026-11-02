"""Share the router's effective first-match decision with authorization."""
from collections.abc import Sequence
from dataclasses import dataclass

from fastapi.routing import iter_route_contexts
from starlette.routing import BaseRoute, Match
from starlette.types import Scope


@dataclass(frozen=True)
class SelectedRoute:
    route: BaseRoute
    path: str
    child: Scope


def selected_route(routes: Sequence[BaseRoute], scope: Scope) -> SelectedRoute | None:
    # Deferred included routers and their prefixes must have the same ordering
    # as dispatch. A later allowed route cannot authorize an earlier handler.
    for context in iter_route_contexts(routes):
        match, child = context.matches(dict(scope))
        if match == Match.FULL:
            return SelectedRoute(context.original_route, str(context.path), child)
    return None


def explicitly_registered(selected: SelectedRoute | None, allowed: Sequence[BaseRoute]) -> bool:
    return selected is not None and any(
        selected.route is route and selected.path == getattr(route, "path", None)
        for route in allowed
    )
