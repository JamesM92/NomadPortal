# Third-Party Notices

NomadPortal is licensed under the MIT License (see `LICENSE`). It bundles
or depends on the following third-party software, each retaining its own
license terms.

## Bundled assets

- **Roboto Mono Nerd Font** (`static/fonts/RobotoMonoNerdFont/`) —
  Roboto Mono is © Google Inc. under the Apache License 2.0; the Nerd
  Fonts glyph patches are © Ryan L McIntyre under the MIT license. See
  `static/fonts/RobotoMonoNerdFont/LICENSE.md` for full text.

## Python dependencies

NomadPortal pulls these Python packages (each under its own license; see
each project's repository):

- **Reticulum (`rns`)** — MIT
- **LXMF** — MIT
- **NomadNet** — MIT
- **Flask** — BSD-3-Clause
- **Flask-Login** — MIT
- **Authlib** — BSD-3-Clause
- **Gunicorn** — MIT
- **Micron2HTML** — MIT (separate repository: <https://github.com/JamesM92/Micron2HTML>)
- **PyYAML** — MIT
- **Werkzeug** — BSD-3-Clause
- **requests** — Apache-2.0

The pinned versions are listed in `requirements.txt`. Run
`pip-licenses --from=mixed` after a build for an exact list.

## NomadNet protocol

NomadPortal speaks the NomadNet, LXMF, and Reticulum protocols defined by
Mark Qvist (<https://github.com/markqvist>). It is an independent client
implementation and is neither endorsed by nor affiliated with the
upstream projects.
