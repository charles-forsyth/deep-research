# Security Policy

## Supported versions

Only the latest release on `main` receives fixes. Please upgrade before reporting:

```bash
uv tool install --force git+https://github.com/charles-forsyth/deep-research.git
```

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private reporting:
**Security > Report a vulnerability** on this repository
(<https://github.com/charles-forsyth/deep-research/security/advisories/new>).

Include what you found, how to reproduce it, and the version (`deep-research --version`). You should
get an acknowledgement within a week. Fixes are released as soon as practical and credited in the
changelog unless you prefer otherwise.

## Security model

Things to know before you deploy it:

- **The web dashboard has no authentication.** Anyone who can reach its port can read your research
  history and start runs billed to your API key. It binds `0.0.0.0` by default for use on a trusted
  network or a private overlay such as Tailscale. Use `--host 127.0.0.1` on any untrusted network,
  and never expose it directly to the internet.
- **Your API key** is read from the environment or `~/.config/deepresearch/.env`. Keep that file
  private (`chmod 600`) and restrict the key to the Generative Language API in Google Cloud.
- **Uploaded documents** are sent to a Gemini File Search Store for the run and deleted afterwards;
  `deep-research cleanup` removes any stores left behind by interrupted runs.
- **Research reports are untrusted content.** The dashboard renders Markdown through DOMPurify and
  opens external links with `rel="noopener noreferrer"`.
- State-changing dashboard requests require `Content-Type: application/json`, which blocks simple
  cross-site form posts.
