{
  pkgs ? import <nixpkgs> { },
}:
{
  pyedit = pkgs.callPackage ./pyedit { };
}
