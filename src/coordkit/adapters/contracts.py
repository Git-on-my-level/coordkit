"""Runtime-checkable optional adapter contracts."""
from typing import Protocol, runtime_checkable


@runtime_checkable
class HarnessAdapter(Protocol):
    name: str
    def listening(self, manifest, name, rows): ...
    def wake(self, manifest, name, rows, key): ...


@runtime_checkable
class LauncherAdapter(Protocol):
    name: str
    def recent(self, manifest): ...
    def launch(self, manifest, *args, **kwargs): ...
    def cancel(self, manifest, execution): ...
    def await_result(self, manifest, execution): ...
    def owns(self, manifest, execution): ...


@runtime_checkable
class MirrorAdapter(Protocol):
    name: str
    def project(self, manifest, row, all_rows): ...


