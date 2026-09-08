import ast
import pathlib
import sys

src = pathlib.Path(__file__).resolve().parents[1] / "app" / "service.py"
tree = ast.parse(src.read_text())
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "greet":
        if ast.get_docstring(node):
            print("CHECK OK: greet has a docstring")
            sys.exit(0)
        print("CHECK FAIL: greet has no docstring")
        sys.exit(1)
print("CHECK FAIL: greet not found")
sys.exit(1)
