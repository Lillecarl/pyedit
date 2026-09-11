{
  pkgs ? import <nixpkgs> { },
}:
rec {
  pyedit = pkgs.python3Packages.callPackage ./pyedit { };

  shell = pkgs.mkShell {
    packages = [
      (pkgs.python3.withPackages (p: [
        p.pytest
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
          version = "0.1.0";
          root = toString ./src;
          scripts = {
            pyedit = "pyedit.cli:main";
          };
          dependencies = with pkgs.python3Packages; [ unidiff ];
        })
      ]))
    ];
  };
}
