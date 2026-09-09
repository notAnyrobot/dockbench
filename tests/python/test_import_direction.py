"""Guard the architectural ownership promised by the refactor."""
import ast
from pathlib import Path


def test_backend_is_independent_and_web_does_not_import_cli():
    root = Path(__file__).resolve().parents[2] / 'src/dockbench'
    for package, forbidden in [('core', ('dockbench.cli', 'dockbench.web')),
                               ('web', ('dockbench.cli',))]:
        for path in (root / package).glob('**/*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                names = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                         [item.name for item in node.names] if isinstance(node, ast.Import) else [])
                assert not any(name == prefix or name.startswith(prefix + '.')
                               for name in names for prefix in forbidden), path
