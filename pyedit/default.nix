{
  lib,
  pkgs,
  hatchling,
  unidiff,
  pathspec,
  rope,
  pygit2,
  pygls,
  lsprotocol,
  pyright,
  tree-sitter,
  tree-sitter-grammars,
  cacert,
  git,
  pytestCheckHook,
  hypothesis,
  buildPythonApplication,
  buildPythonPackage,
}:
let
  # tree-sitter grammar dylibs carry no install id, so every parser's
  # id is the bare basename "parser"; dyld dedupes by id and the first
  # grammar loaded wins, breaking every other binding on macOS.
  # Rewriting the binding's load command to its own parser's absolute
  # path sidesteps the id collision
  binding = name:
    (tree-sitter-grammars.${name}).overrideAttrs (old: {
      nativeBuildInputs = (old.nativeBuildInputs or [ ])
        ++ lib.optionals pkgs.stdenv.hostPlatform.isDarwin [ pkgs.darwin.sigtool ];
      postFixup =
        (old.postFixup or "")
        + lib.optionalString pkgs.stdenv.hostPlatform.isDarwin ''
          for so in $out/${pkgs.python3Packages.python.sitePackages}/${lib.replaceStrings [ "-" ] [ "_" ] name}/_binding*.so; do
            install_name_tool -change parser "${pkgs.tree-sitter-grammars.${name}}/parser" "$so"
            codesign --force --sign - "$so"
          done
        '';
    });

  attrs = {
    pname = "pyedit";
    # one source: src/pyedit/_version.py, read by hatchling too
    version = builtins.elemAt (builtins.match "(.|\n)*__version__ = \"([^\"]+)\"(.|\n)*" (builtins.readFile ../src/pyedit/_version.py)) 1;
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
      pygls
      lsprotocol
      tree-sitter
    ]
    ++ map (name: binding name) [
      # python bindings for every grammar live in the generated
      # tree-sitter-grammars scope; a grammar missing there is bindable
      # with the same generator: callPackage
      # ../development/python-modules/tree-sitter-grammars { inherit
      # name grammarDrv; } against pkgs.tree-sitter-grammars
      "tree-sitter-python"
      "tree-sitter-javascript"
      "tree-sitter-typescript"
      "tree-sitter-tsx"
      "tree-sitter-go"
      "tree-sitter-rust"
      "tree-sitter-c"
      "tree-sitter-cpp"
      "tree-sitter-bash"
      "tree-sitter-json"
      "tree-sitter-yaml"
      "tree-sitter-toml"
      "tree-sitter-nix"
      "tree-sitter-ruby"
      "tree-sitter-java"
      "tree-sitter-lua"
      "tree-sitter-zig"
    ];

    # pygit2 performs TLS setup at import; without certificates to load
    # it fails - same workaround as nixpkgs uses for pygit2's own tests
    env.SSL_CERT_FILE = "${cacert}/etc/ssl/certs/ca-bundle.crt";

    nativeCheckInputs = [
      pytestCheckHook
      pyright
      hypothesis
      git
    ];

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
