{
  pkgs ? import <nixpkgs> { },
}:
let
  # pygls from the lillecarl fork until upstream takes the union-hook
  # fix (Lillecarl/pygls#1): stock 2.1.1 cannot structure a server's
  # initialize result when it advertises notebook sync, so `ruff
  # server` never connects. Drop this override when the fix lands.
  pyglsFork = pkgs.python3Packages.pygls.overrideAttrs {
    version = "2.1.1+lillecarl.1";
    src = pkgs.fetchFromGitHub {
      owner = "lillecarl";
      repo = "pygls";
      rev = "b34a5059ad2e2aa5c131d1f38dec8f77dbc31088";
      hash = "sha256-znZ5MViyqium9UK8wyNjVKYV98WIHbJ55B3uWfafWNU=";
    };
  };
in
rec {
  pyedit = pkgs.python3Packages.callPackage ./pyedit {
    git = pkgs.git;
    pygls = pyglsFork;
  };
  pyedit-nocheck = pyedit.overrideAttrs { doCheck = false; doInstallCheck = false; };

  shell = pkgs.mkShell {
    packages = [
      pkgs.pyright
      (pkgs.python3.withPackages (p: [
        p.pytest
        p.hypothesis
        (pyedit.passthru.library.overrideAttrs {
          doCheck = false;
          doInstallCheck = false;
        })
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
