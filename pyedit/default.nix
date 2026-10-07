{
  lib,
  pkgs,
  cffi,
  hatchling,
  platformdirs,
  pathspec,
  rope,
  pygit2,
  unidiff,
  mcp,
  pygls,
  pyjj ? null,
  lsprotocol,
  pyright,
  ruff,
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
  fixup = grammar: binding: dir:
    binding.overrideAttrs (old: {
      nativeBuildInputs = (old.nativeBuildInputs or [ ])
        ++ lib.optionals pkgs.stdenv.hostPlatform.isDarwin [ pkgs.darwin.sigtool ];
      postFixup =
        (old.postFixup or "")
        + lib.optionalString pkgs.stdenv.hostPlatform.isDarwin ''
          for so in $out/${pkgs.python3Packages.python.sitePackages}/${dir}/_binding*.so; do
            install_name_tool -change parser "${grammar}/parser" "$so"
            codesign --force --sign - "$so"
          done
        '';
    });

  binding = name:
    fixup pkgs.tree-sitter-grammars.${name} tree-sitter-grammars.${name} (
      lib.replaceStrings [ "-" ] [ "_" ] name
    );

  # nixpkgs pins svelte to the superseded Himujjal grammar, so the
  # maintained tree-sitter-grammars fork is bound with nixpkgs' own
  # generator instead. The repo ships a pre-generated parser.c, so no
  # generate step (and no tree-sitter-html at build time) is needed.
  svelteGrammar = pkgs.tree-sitter.buildGrammar {
    language = "svelte";
    version = "1.0.2";
    src = pkgs.fetchFromGitHub {
      owner = "tree-sitter-grammars";
      repo = "tree-sitter-svelte";
      rev = "v1.0.2";
      hash = "sha256-mkw3s0pZQ6ry+fiTk2fJeKVA7Nqyv2Z2R1AFZknzpFM=";
    };
  };
  svelteBinding = pkgs.python3Packages.callPackage "${pkgs.path}/pkgs/development/python-modules/tree-sitter-grammars" {
    name = "tree-sitter-svelte";
    grammarDrv = svelteGrammar;
  };

  attrs = {
    pname = "pyedit";
    version = (builtins.fromTOML (builtins.readFile ../pyproject.toml)).project.version;
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
      cffi
      pathspec
      platformdirs
      rope
      pygit2
      unidiff
      mcp
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
      #
      # pyjj rides below, not here: no nixpkgs pyjj exists, so the
      # distributor passes its own build through the `pyjj` argument
      # (null excludes it and -r refuses loudly instead)
      "tree-sitter-python"
      "tree-sitter-javascript"
      "tree-sitter-typescript"
      "tree-sitter-tsx"
      "tree-sitter-go"
      "tree-sitter-rust"
      "tree-sitter-c"
      "tree-sitter-cpp"
      "tree-sitter-css"
      "tree-sitter-bash"
      "tree-sitter-fish"
      "tree-sitter-markdown"
      "tree-sitter-json"
      "tree-sitter-yaml"
      "tree-sitter-toml"
      "tree-sitter-nix"
      "tree-sitter-ruby"
      "tree-sitter-java"
      "tree-sitter-lua"
      "tree-sitter-zig"
    ]
    ++ [ (fixup svelteGrammar svelteBinding "tree_sitter_svelte") ]
    ++ lib.optionals (pyjj != null) [ pyjj ];

    # pygit2 performs TLS setup at import; without certificates to load
    # it fails - same workaround as nixpkgs uses for pygit2's own tests
    env.SSL_CERT_FILE = "${cacert}/etc/ssl/certs/ca-bundle.crt";

    nativeCheckInputs = [
      pytestCheckHook
      pyright
      ruff
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
  buildPythonApplication (
    attrs
    // {
      # the wrapped CLI renders its own skill, so SOURCE_SECTION names
      # this store path. postInstall is too early: mk-python-derivation
      # puts wrapPythonPrograms in postFixup, ahead of this string
      postFixup = ''
        $out/bin/pyedit skill $out/share/skills/pyedit/pyedit/SKILL.md
      '';
    }
  )
  // {
    passthru = {
      library = buildPythonPackage attrs;
    };
  }
)
