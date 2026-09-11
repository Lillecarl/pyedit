{
  pkgs ? import <nixpkgs> { },
}:
rec {
  pyedit = pkgs.python3Packages.callPackage ./pyedit { };

  shell = pkgs.mkShell {
    packages = [
      (pkgs.python3.withPackages (
        p: [
          p.pytest
          pyedit.passthru.library
        ]
      ))
    ];
  };

  # overlay pattern: pyedit resolves to the live ./src tree via a
  # PEP-660 editable install; PYEDIT_SRC is pinned by the shellHook
  editable = pkgs.mkShell {
    packages = [
      (pkgs.python3.withPackages (
        p: [
          p.pytest
          (p.mkPythonEditablePackage {
            pname = "pyedit";
            version = "0.1.0";
            root = "$PYEDIT_SRC";
            scripts = {
              pyedit = "pyedit.cli:main";
            };
            dependencies = with pkgs.python3Packages; [ unidiff ];
          })
        ]
      ))
    ];
    shellHook = ''
      export PYEDIT_SRC="''${PYEDIT_SRC:-$PWD/src}"
    '';
  };
}
