{
  pkgs ? import <nixpkgs> { },
}:
rec {
  pyedit = pkgs.callPackage ./pyedit { };
  shell = pkgs.mkShell {
    packages = [ (pkgs.python3.withPackages (_: pyedit)) ];
  };
}
