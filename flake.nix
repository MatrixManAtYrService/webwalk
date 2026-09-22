{
  description = "webwalk - drive a Selenium browser through a web flow: flow-agnostic core (server + stateless CLI + observe/act/verify rule engine) with per-flow CLIs on top";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
      in
      {
        # `nix develop` (or `nix develop --command uv run <...>`) drops you into a
        # shell with uv + Python 3.12. uv owns the venv and dependency resolution;
        # this flake only pins the toolchain.
        devShells.default = pkgs.mkShell {
          packages = [
            pkgs.uv
            pkgs.python312
          ];
          # Let uv use the nixpkgs interpreter rather than downloading its own.
          env.UV_PYTHON = "${pkgs.python312}/bin/python3.12";
        };
      });
}
