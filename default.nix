{
  pkgs ? import <nixpkgs> { },
  # A pyjj build means `pyedit -r` works; null (the standalone
  # default) leaves it out and the flag refuses loudly. There is no
  # runtime switch by design, and no nixpkgs pyjj exists, so a null
  # here is honest: distributors pass their own build, or null to
  # exclude it deliberately.
  pyjj ? null,
}:
let
  # pygls from the lillecarl fork until upstream takes the union-hook
  # fix (Lillecarl/pygls#1): stock 2.1.1 cannot structure a server's
  # initialize result when it advertises notebook sync, so `ruff
  # server` never connects. Drop this override when the fix lands.
  pyglsFork = pkgs.python3Packages.pygls.overrideAttrs {
    # Consumed as a library only: the fork's own suite (e2e servers
    # included) runs in its upstream CI, while pyedit's suite verifies
    # the integration end to end through the real `ruff server`.
    doCheck = false;
    doInstallCheck = false;
    src = pkgs.fetchFromGitHub {
      owner = "lillecarl";
      repo = "pygls";
      rev = "b34a5059ad2e2aa5c131d1f38dec8f77dbc31088";
      hash = "sha256-znZ5MViyqium9UK8wyNjVKYV98WIHbJ55B3uWfafWNU=";
    };
  };
  # a real venv instead of nixpkgs' wrapper: sys.executable sees
  # pyedit's packages, which `pyedit.lsp(..., python_path=sys.executable)`
  # relies on, and nothing exports PYTHONPATH to children
  nixvenvSrc = pkgs.fetchFromGitHub {
    owner = "nixidae";
    repo = "nixvenv";
    rev = "edacb28d8100a9d291732329af7295aab78a0387";
    hash = "sha256-VnW8LUMXCy3W+w8or/8VX5IyTnBxSJHx1VgVsSkQ2Yo=";
  };
  inherit ((import nixvenvSrc { inherit pkgs; }).lib) nixvenv;
  build =
    doCheck:
    pkgs.python3Packages.callPackage ./pyedit {
      git = pkgs.git;
      pygls = pyglsFork;
      inherit pyjj nixvenv doCheck;
    };
in
rec {
  pyedit = build true;
  pyedit-nocheck = build false;

  shell = pkgs.mkShell {
    packages = [
      pkgs.ruff
      pkgs.pyright
      pkgs.pyrefly
      (pkgs.python3.withPackages (p: [
        p.pytest
        p.hypothesis
        pyedit-nocheck.passthru.library
      ]))
    ];
  };

  # overlay pattern: pyedit resolves to the live ./src tree via a
  # PEP-660 editable install pointing at the repo's src directory
  editable = pkgs.mkShell {
    packages = [
      (pkgs.python3.withPackages (p: [
        p.pytest
        (p.mkPythonEditablePackage {
          pname = "pyedit";
          version = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;
          root = toString ./src;
          scripts = {
            pyedit = "pyedit.cli:main";
          };
          dependencies = [ ];
        })
      ]))
    ];
  };
}
