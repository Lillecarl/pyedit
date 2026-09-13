{
  pkgs ? import <nixpkgs> { },
}:
rec {
  pyedit = pkgs.python3Packages.callPackage ./pyedit { git = pkgs.git; };
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
