{
  lib,
  hatchling,
  unidiff,
  pathspec,
  rope,
  pytestCheckHook,
  buildPythonApplication,
  buildPythonPackage,
}:
let
  attrs = {
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

    build-system = [ hatchling ];

    dependencies = [
      unidiff
      pathspec
      rope
    ];

    nativeCheckInputs = [ pytestCheckHook ];

    pythonImportsCheck = [ "pyedit" ];

    meta = {
      description = "Scripted multi-file edits with dry-run diffs for AI agents";
      mainProgram = "pyedit";
    };
  };
in
(
  buildPythonApplication attrs
  // {
    passthru = {
      library = buildPythonPackage attrs;
    };
  }
)
