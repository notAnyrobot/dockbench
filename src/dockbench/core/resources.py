"""Repository-owned resources for checkout installations.

Only this module knows the source layout. Explicit roots support deployment and
embedded applications; the default is anchored to this installed source file,
never the invoking shell's working directory. No Docker or filesystem probing
is needed to resolve locations.
"""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CheckoutResources:
    repository_root: Path

    @classmethod
    def discover(cls, repository_root: Path | None = None) -> "CheckoutResources":
        root = repository_root if repository_root is not None else Path(__file__).resolve().parents[3]
        return cls(Path(root).expanduser().resolve())

    @property
    def assets(self) -> Path:
        return self.repository_root / "assets"

    @property
    def images(self) -> Path:
        return self.assets / "images"

    @property
    def frontend_source(self) -> Path:
        return self.repository_root / "src" / "dockbench" / "web" / "frontend"

    @property
    def frontend_dist(self) -> Path:
        return self.frontend_source / "dist"
