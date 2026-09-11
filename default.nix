{
  pkgs ? import <nixpkgs> { },
}:
rec {
  pyedit = pkgs.python3Packages.callPackage ./pyedit { };
  shell = pkgs.mkShell {
    packages = [ (pkgs.python3.withPackages (_: [ pyedit.passthru.library ])) ];
  };
}
