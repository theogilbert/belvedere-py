import importlib
from dataclasses import dataclass

from .base import BaseDriver
from ..protocol import Driver


@dataclass(frozen=True)
class RegisteredDriver:
    module: str
    class_name: str


_REGISTRY: list[RegisteredDriver] = [
    RegisteredDriver(module="sqlite", class_name="SQLiteDriver"),
    RegisteredDriver(module="sqlserver", class_name="SQLServerDriver"),
    RegisteredDriver(module="neo4j", class_name="Neo4jDriver"),
    RegisteredDriver(module="oracle", class_name="OracleDriver"),
    RegisteredDriver(module="mongodb", class_name="MongoDriver"),
    RegisteredDriver(module="elasticsearch", class_name="ElasticsearchDriver"),
]


def get_driver_help(name: str) -> str:
    """Return the HELP string for a named driver without importing its optional package."""
    return _load_class(_find(name)).HELP


def get_driver(name: str) -> type[BaseDriver]:
    cls = _load_class(_find(name))
    if cls.PACKAGE:
        try:
            importlib.import_module(cls.PACKAGE)
        except ImportError:
            raise ValueError(f"{cls.PACKAGE} not installed")
    return cls


def list_drivers() -> list[Driver]:
    """Return capabilities for every driver available in the current environment."""
    result = []
    for entry in _REGISTRY:
        cls = _load_class(entry)
        if cls.PACKAGE:
            try:
                importlib.import_module(cls.PACKAGE)
            except ImportError:
                continue
        result.append(Driver(driver=entry.module, label=cls.LABEL, params=cls.PARAMS))
    return result


def _find(name: str) -> RegisteredDriver:
    for entry in _REGISTRY:
        if entry.module == name.lower():
            return entry
    raise ValueError(f"Unknown driver: {name!r}")


def _load_class(entry: RegisteredDriver) -> type[BaseDriver]:
    mod = importlib.import_module(f".{entry.module}", package=__package__)
    return getattr(mod, entry.class_name)  # type: ignore[return-value]


SENSITIVE_PARAM_KEYS: frozenset[str] = frozenset(
    p.key for d in list_drivers() for p in d.params if p.secret
)
