"""Audited runtime loading seam that cannot resolve the frozen legacy driver."""

from __future__ import annotations

import importlib.util
from importlib import import_module as _import_module
import inspect
from collections.abc import Iterator
from functools import cached_property, partial
from pathlib import Path
from types import (
    BuiltinFunctionType,
    FunctionType,
    GetSetDescriptorType,
    MemberDescriptorType,
    MethodType,
    ModuleType,
)

_LEGACY_DRIVER_MODULE = "aletheia.scheduler.driver"
_MAX_ORIGIN_OBJECTS = 4096
_AUDITED_CLASS_DUNDER_STATE = {"__annotations__", "__orig_bases__"}


def _is_legacy_driver_module(module_name: object) -> bool:
    return isinstance(module_name, str) and (
        module_name == _LEGACY_DRIVER_MODULE or module_name.startswith(f"{_LEGACY_DRIVER_MODULE}.")
    )


def _class_state(owner: type) -> Iterator[object]:
    """Yield raw class values while never binding or invoking user descriptors."""

    try:
        namespace = vars(owner)
    except Exception as exc:
        raise ValueError("could not safely inspect a dynamic class state") from exc
    for name, class_value in namespace.items():
        if (
            name.startswith("__")
            and name.endswith("__")
            and name not in _AUDITED_CLASS_DUNDER_STATE
        ):
            continue
        if type(class_value) in {classmethod, staticmethod}:
            yield class_value.__func__
            continue
        if type(class_value) is property:
            yield from (
                accessor
                for accessor in (class_value.fget, class_value.fset, class_value.fdel)
                if accessor is not None
            )
            continue
        if type(class_value) is cached_property:
            yield class_value.func
            continue
        if (
            isinstance(class_value, (GetSetDescriptorType, MemberDescriptorType))
            or inspect.isdatadescriptor(class_value)
            or inspect.ismethoddescriptor(class_value)
        ):
            continue
        yield class_value


def _callable_instance_state(value: object) -> Iterator[object]:
    """Yield conventional Python instance state without invoking user attribute hooks."""

    value_type = type(value)
    for owner in value_type.__mro__:
        try:
            namespace = vars(owner)
        except Exception as exc:
            raise ValueError("could not safely inspect a dynamic callable's class state") from exc

        dictionary_descriptor = namespace.get("__dict__")
        if isinstance(dictionary_descriptor, GetSetDescriptorType):
            try:
                instance_dictionary = dictionary_descriptor.__get__(value, value_type)
            except AttributeError:
                pass
            except Exception as exc:
                raise ValueError(
                    "could not safely inspect a dynamic callable's instance dictionary"
                ) from exc
            else:
                if not isinstance(instance_dictionary, dict):
                    raise ValueError("dynamic callable instance dictionary is not a plain dict")
                try:
                    yield from tuple(instance_dictionary.values())
                except Exception as exc:
                    raise ValueError(
                        "could not safely inspect a dynamic callable's instance dictionary"
                    ) from exc

        for descriptor in namespace.values():
            if not isinstance(descriptor, MemberDescriptorType):
                continue
            try:
                yield descriptor.__get__(value, value_type)
            except AttributeError:
                continue
            except Exception as exc:
                raise ValueError("could not safely inspect a dynamic callable's slots") from exc


def _resolved_object_origins(value: object) -> Iterator[object]:
    """Yield a bounded graph of identities that can carry an implementation origin."""

    pending = [value]
    seen: set[int] = set()
    while pending:
        candidate = pending.pop()
        identity = id(candidate)
        if identity in seen:
            continue
        seen.add(identity)
        if len(seen) > _MAX_ORIGIN_OBJECTS:
            raise ValueError("dynamic object origin graph exceeds the audited inspection bound")
        yield candidate

        if isinstance(candidate, partial):
            pending.extend((candidate.func, *candidate.args))
            if candidate.keywords:
                pending.extend(candidate.keywords.values())
        if isinstance(candidate, MethodType):
            pending.extend((candidate.__func__, candidate.__self__))
        if isinstance(candidate, type):
            pending.extend(candidate.__mro__[1:])
            pending.extend(_class_state(candidate))
        if isinstance(candidate, FunctionType):
            pending.extend(candidate.__defaults__ or ())
            pending.extend((candidate.__kwdefaults__ or {}).values())
            try:
                function_state = candidate.__dict__
                annotations = candidate.__annotations__
                pending.extend(tuple(function_state.values()))
                pending.extend(tuple(annotations.keys()))
                pending.extend(tuple(annotations.values()))
            except Exception as exc:
                raise ValueError("could not safely inspect dynamic function state") from exc
            for cell in candidate.__closure__ or ():
                try:
                    pending.append(cell.cell_contents)
                except ValueError:
                    continue
            try:
                closure_variables = inspect.getclosurevars(candidate)
            except Exception as exc:
                raise ValueError("could not safely inspect dynamic function origins") from exc
            pending.extend(closure_variables.nonlocals.values())
            pending.extend(closure_variables.globals.values())
        if isinstance(candidate, dict):
            pending.extend(candidate.keys())
            pending.extend(candidate.values())
        elif isinstance(candidate, (list, tuple, set, frozenset)):
            pending.extend(candidate)

        try:
            wrapped = inspect.getattr_static(candidate, "__wrapped__")
        except AttributeError:
            wrapped = None
        except Exception as exc:
            raise ValueError("could not safely inspect a dynamic object's wrapped origin") from exc
        if wrapped is not None:
            pending.append(wrapped)

        if not isinstance(
            candidate,
            (
                BuiltinFunctionType,
                FunctionType,
                MethodType,
                ModuleType,
                partial,
                type,
            ),
        ):
            pending.append(type(candidate))
            if callable(candidate):
                pending.extend(_callable_instance_state(candidate))
                try:
                    call_implementation = inspect.getattr_static(type(candidate), "__call__")
                except Exception as exc:
                    raise ValueError(
                        "could not safely inspect a dynamic callable implementation"
                    ) from exc
                pending.append(call_implementation)


def _assert_not_legacy_driver_object(value: object) -> None:
    for candidate in _resolved_object_origins(value):
        if isinstance(candidate, ModuleType):
            module_name: object = candidate.__name__
        elif isinstance(candidate, (BuiltinFunctionType, FunctionType, MethodType, type)):
            module_name = candidate.__module__
        else:
            continue
        if _is_legacy_driver_module(module_name):
            raise ValueError(
                "resolved dynamic object belongs to the raw legacy driver; "
                "use a reviewed compatibility or durable entry point"
            )


def _assert_not_legacy_driver_path(source_path: Path) -> None:
    lowered_parts = tuple(part.casefold() for part in source_path.parts)
    raw_source = len(lowered_parts) >= 3 and lowered_parts[-3:] in {
        ("aletheia", "scheduler", "driver.py"),
        ("aletheia", "scheduler", "driver.pyc"),
    }
    raw_bytecode = (
        len(lowered_parts) >= 4
        and lowered_parts[-4:-1] == ("aletheia", "scheduler", "__pycache__")
        and lowered_parts[-1].startswith("driver.")
        and lowered_parts[-1].endswith(".pyc")
    )
    if raw_source or raw_bytecode:
        raise ValueError(
            "raw legacy driver source paths are forbidden; "
            "use a reviewed compatibility or durable entry point"
        )


def _assert_module_exports_no_legacy_driver(module: ModuleType) -> None:
    _assert_not_legacy_driver_object(module)
    module_path = getattr(module, "__file__", None)
    if isinstance(module_path, str):
        _assert_not_legacy_driver_path(Path(module_path).resolve(strict=False))
    for value in vars(module).values():
        _assert_not_legacy_driver_object(value)


def resolve_guarded_dynamic_attribute(module_name: str, attribute_name: str) -> object:
    """Resolve one explicit attribute while rejecting raw and re-exported legacy-driver objects."""

    if _is_legacy_driver_module(module_name):
        raise ValueError(
            "raw legacy driver handlers are forbidden; register aletheia.scheduler.durable"
        )
    value = getattr(_import_module(module_name), attribute_name)
    _assert_not_legacy_driver_object(value)
    return value


def load_guarded_source_module(module_name: str, source_path: str | Path) -> ModuleType:
    """Execute one source file while rejecting raw paths and exported legacy-driver objects."""

    if _is_legacy_driver_module(module_name):
        raise ValueError("raw legacy driver module identities are forbidden")
    resolved_path = Path(source_path).expanduser().resolve(strict=True)
    _assert_not_legacy_driver_path(resolved_path)
    spec = importlib.util.spec_from_file_location(module_name, resolved_path)
    if spec is None:
        raise RuntimeError(f"could not construct a source loader for {resolved_path}")
    module = importlib.util.module_from_spec(spec)
    try:
        source_bytes = resolved_path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"could not read guarded source: {resolved_path}") from exc
    exec(compile(source_bytes, str(resolved_path), "exec"), vars(module))
    _assert_module_exports_no_legacy_driver(module)
    return module


def load_guarded_source_bytes(
    module_name: str,
    source_path: str | Path,
    source_bytes: bytes,
) -> ModuleType:
    """Execute the caller's exact pinned bytes, then audit every exported origin.

    This variant lets a deployment fresh-read and hash one regular file before execution without a
    second loader read introducing a time-of-check/time-of-use gap.  ``source_path`` remains the
    diagnostic/compiler identity and is still checked against the frozen legacy-driver path.
    """

    if _is_legacy_driver_module(module_name):
        raise ValueError("raw legacy driver module identities are forbidden")
    if not isinstance(source_bytes, bytes) or not source_bytes:
        raise ValueError("guarded source bytes must be nonempty bytes")
    resolved_path = Path(source_path).expanduser().resolve(strict=True)
    _assert_not_legacy_driver_path(resolved_path)
    spec = importlib.util.spec_from_file_location(module_name, resolved_path)
    if spec is None:
        raise RuntimeError(f"could not construct a source loader for {resolved_path}")
    module = importlib.util.module_from_spec(spec)
    exec(compile(source_bytes, str(resolved_path), "exec"), vars(module))
    _assert_module_exports_no_legacy_driver(module)
    return module


__all__ = [
    "load_guarded_source_bytes",
    "load_guarded_source_module",
    "resolve_guarded_dynamic_attribute",
]
