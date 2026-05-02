# Contributing to NomadPortal

Thanks for your interest. This is a small project — keep contributions focused, and please open an issue first for anything beyond a small fix.

## Reporting bugs and requesting features

- **Bugs:** open a [bug report](https://github.com/JamesM92/NomadPortal/issues/new?template=bug_report.yml).
- **Features:** open a [feature request](https://github.com/JamesM92/NomadPortal/issues/new?template=feature_request.yml).
- **Security issues:** do **not** open a public issue. See [SECURITY.md](SECURITY.md).

## Local development

The fastest path is the Docker workflow used in production:

```bash
git clone https://github.com/JamesM92/NomadPortal.git
cd NomadPortal
./start.sh --build --fg
```

This builds the image, runs it in the foreground, and streams logs. Open `https://localhost:8443` and accept the self-signed certificate.

To run the Flask app directly without Docker (requires Reticulum and the rest of the stack on the host):

```bash
pip install -r requirements.txt
python app.py
```

## Pull requests

1. Fork the repo and create a branch off `main`.
2. Keep changes small and focused — one concern per PR.
3. Update the `[Unreleased]` section of [CHANGELOG.md](CHANGELOG.md).
4. Make sure the Docker image still builds (CI will check, but please verify locally for non-trivial changes).
5. Open the PR using the template; describe the motivation and test steps.

## Code style

- Python: follow the existing style of the surrounding code (PEP 8, 4-space indent).
- Templates / static assets: match the conventions already in [templates/](templates/) and [static/](static/).
- Avoid introducing new top-level dependencies without discussion — every dependency adds attack surface for a security-sensitive project.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE) that covers the rest of the project.
