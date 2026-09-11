{
  lib,
  hatchling,
  unidiff,
  pathspec,
  rope,
  pygit2,
  cacert,
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
      pygit2
    ];

    # pygit2 performs TLS setup at import; without certificates to load
    # it fails - same workaround as nixpkgs uses for pygit2's own tests
    env.SSL_CERT_FILE = "${cacert}/etc/ssl/certs/ca-bundle.crt";

    nativeCheckInputs = [ pytestCheckHook ];

    pythonImportsCheck = [ "pyedit" ];

  meta = {
    description = "Scripted multi-file edits with dry-run diffs for AI agents";
    license = lib.licenses.asl20;
    maintainers = [ lib.maintainers.lillecarl ];
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
