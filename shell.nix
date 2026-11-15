# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later

{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = [
    pkgs.python312
    pkgs.uv
    pkgs.ruff
    pkgs."check-jsonschema"
    pkgs.flutter
    pkgs.chromium
  ];

  CHROME_EXECUTABLE = "${pkgs.chromium}/bin/chromium";

  shellHook = ''
    unset PYTHONPATH
    export CHROME_EXECUTABLE="${pkgs.chromium}/bin/chromium"
  '';
}
