"""Types for parsed JSON: an object is dict[str, Any], an array list[Any]. The guards narrow a value the way
isinstance does, but to those types (isinstance(x, dict) on an Any gives pyright dict[Unknown, Unknown])."""

from __future__ import annotations

from typing import Any, TypeGuard

Obj = dict[str, Any]


def is_obj(v: object) -> TypeGuard[Obj]:
    return isinstance(v, dict)


def is_list(v: object) -> TypeGuard[list[Any]]:
    return isinstance(v, list)


def objs(v: object) -> list[Obj]:
    """The objects in a JSON array (nothing for anything else)."""
    return [x for x in v if is_obj(x)] if is_list(v) else []
