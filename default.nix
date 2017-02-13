{
  pkgsPath ? <nixpkgs>,
  pkgs ? import pkgsPath {},
  pythonPackages ? pkgs.python3Packages,
  passthru ? {},
}:

let
  rtyaml = pythonPackages.buildPythonPackage {
    name = "rtyaml-0.0.3";
    src = pkgs.fetchurl {
      url = "https://pypi.python.org/packages/ba/35/d17851c3a79b52379739b71182da24ac29a4cb3f3c2d02ee975c9625db4b/rtyaml-0.0.3.tar.gz";
      sha256 = "0f7d5n3hs0by9rjl9pzkigdr21ml3q8kpd45c302cjm2i9xy2i45";
    };
    propagatedBuildInputs = [pythonPackages.pyyaml];
  };
  pysodium = pythonPackages.buildPythonPackage rec {
    name = "pysodium-${version}";
    version = "0.6.9.1";
    src = pkgs.fetchurl {
      url = "https://pypi.python.org/packages/1c/7b/140b954748b466564e7e4d6728cf02109d52999a15f7f6cdce4532542440/pysodium-${version}.tar.gz";
      sha256 = "00xswqhacgkrswpp56xsa070r83h7jwz9szapd95mykdqg3zaa86";
    };
    libsodiumPath = "${pkgs.libsodium}/lib/libsodium.so";
    # Patch in the abspath to libsodium because it is looked up dynamically.
    patchPhase = ''
      sed -i "s,ctypes.util.find_library('sodium'),'$libsodiumPath'," pysodium/__init__.py
    '';
    propagatedBuildInputs = [pkgs.libsodium];
  };
  # Use .out so we have the binaries callable
  inherit (builtins) replaceStrings readFile;
  version = replaceStrings ["\n"] [""] (readFile ./version.txt);
in

pythonPackages.buildPythonPackage {
  name = "pynix-${version}";
  buildInputs = [pythonPackages.ipython];
  propagatedBuildInputs = [
    pkgs.coreutils
    pkgs.gzip
    pkgs.xz
    pkgs.nix
    pkgs.pv
    pythonPackages.flask
    pythonPackages.requests2
    pythonPackages.ipdb
    pythonPackages.six
    pythonPackages.datadiff
    rtyaml
    pysodium
  ];
  src = ./.;
  passthru = {inherit pythonPackages;} // passthru;
  # Hard-code a bunch of paths so that they can be called even when
  # the library is imported.
  patchPhase = let
    mkPath = deriv: bin: "${pkgs.lib.makeBinPath [deriv]}/${bin}";
    utils = "src/pynix/utils.py";
    nixBin = pkgs.lib.makeBinPath [pkgs.nix];
    fixpath = deriv: bin: let bpath = mkPath deriv bin; in ''
      if ! [[ -x ${bpath} ]]; then
        echo "Invalid binary path for ${bin}: ${bpath}"
        exit 1
      fi
      sed -i 's,_resolve_bin("${bin}"),"${bpath}",' ${utils}
    '';
  in ''
    sed -i 's,dirname(_resolve_bin("nix-env")),"${nixBin}",' ${utils}
    ${fixpath pkgs.gzip "gzip"}
    ${fixpath pkgs.bzip2 "bzip2"}
    ${fixpath pkgs.xz "xz"}
    ${fixpath pkgs.pv "pv"}
    ${fixpath pkgs.coreutils "du"}
  '';
}
