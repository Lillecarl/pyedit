{
  lib,
  python3,
}:
let
  py = python3.pkgs;
in
py.buildPythonApplication {
  pname = "pyedit";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSourceWith {
    src = lib.cleanSource ../.;
    filter =
      path: _type:
      let
        base = baseNameOf path;
      in
      !lib.elem base [
        ".git"
        ".jj"
        ".github"
        ".pytest_cache"
        ".venv"
        "__pycache__"
        "dist"
        "result"
        "result-"
      ];
  };

  build-system = [ py.hatchling ];

  nativeCheckInputs = [ py.pytestCheckHook ];

  pythonImportsCheck = [ "pyedit" ];

  meta = {
    description = "Scripted multi-file edits with dry-run diffs for AI agents";
    mainProgram = "pyedit";
  };
}
