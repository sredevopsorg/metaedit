# 0019. Deployment pulls a published image; development builds from source

- **Status:** Accepted
- **Date:** 2026-11-16

## Context

Until now `docker-compose.yml` built the image from the checkout
(`build: {context: ., target: runtime}`). That conflated two unrelated jobs: running
metaedit, and changing it.

For running it, building was a liability. `docker compose up` needed a source tree, a
Node toolchain and the uv resolver, and spent minutes compiling a Python image to do
what a `docker pull` does in seconds. Worse, it meant the thing being run was whatever
was in *that* checkout — which is not necessarily what was released, and cannot be
version-pinned at all.

Meanwhile the multi-arch workflow (`.github/workflows/multi-build.yaml`) now publishes
`ghcr.io/sredevopsorg/metaedit` for `linux/amd64` and `linux/arm64`. An image that is
published but that nothing is configured to run is only half a feature.

For changing it, building is exactly right, and that is the case that must not pull.

## Decision

Two compose files, differing only in where the image comes from:

- **`docker-compose.yml`** is deployment. It has an `image:` and **no `build:` key at
  all**, so `docker compose up` cannot fall back to compiling a local checkout. The
  image is `ghcr.io/sredevopsorg/metaedit:${METAEDIT_IMAGE:-main}`.
- **`docker-compose.dev.yml`** is development. It builds `app` and `migrate` from the
  checkout, with the same services, ports and volumes.

Both keep the project name `metaedit` and share the `pgdata` volume, so starting one
while the other runs is an immediate, obvious collision rather than a silent mix of the
two.

Tag policy on the registry is part of the decision, since deployment now selects
behaviour by tag: `latest` follows release tags only, and `main` is the continuous
edge. A `latest` that tracked merged-but-unreleased commits would mean a pull returns
code no changelog describes.

Related decision inside the same change: the Dockerfile's final stage is a `release`
alias of `runtime`. Docker builds the *last* stage when no `--target` is given and the
publish job gives none, so the last stage must be what should ship. It previously was
`seed`, which made the published image start the e2e seeding script and fail its own
health check. CI now asserts the default target starts the server.

## Consequences

- Running metaedit needs no checkout and no toolchain — the compose file and a `.env`.
- The running image is a named, pinnable artefact. `METAEDIT_IMAGE=…:0.3.0` is a
  reproducible deployment; `build:` could never be.
- Upgrades are `docker compose pull && docker compose up -d`, and the `migrate` service
  gates the app so a failed migration leaves the old container serving.
- Anyone working on the code must use the dev file. That is now a documented rule
  rather than a preference, because the default file will not build their change.
- CI's deployment check runs the **dev** file: verifying the deployment file on a PR
  would test the registry rather than the commit under review.
- `main` moves under an unpinned deployment. Documented prominently rather than
  prevented, because the alternative — never publishing an edge tag — makes testing an
  unreleased fix impossible.
- The image is public, as GHCR defaults for a public repository. Anyone can pull it; it
  contains no secrets, which is already a standing rule (configuration is environment
  only).

## Alternatives rejected

- **One compose file with a build/run flag.** A flag that silently switches where the
  image comes from is the flag someone leaves on the wrong setting; the failure mode is
  "I tested the wrong thing" or "production runs my working tree". Two files make the
  choice explicit at the point of invocation.
- **Keep `build:` alongside `image:` in the deployment file.** Compose prefers `build:`
  when both exist, so a plain `up` would rebuild and run the checkout — the opposite of
  the intent, and a trap that looks correct in the file.
- **`build: {context: https://github.com/...}`** — building from a pinned git ref. It
  still compiles a toolchain-local image and reintroduces the minutes and the
  toolchain; the registry already solves this.
- **Pinning `latest` to `main`.** Convenient for the maintainer, wrong for everyone
  else: it makes `latest` mean "whatever merged recently" rather than "what was
  released".
- **A `:dev` tag carrying local builds.** Two registries' worth of provenance for one
  convenience; the dev compose file covers the same use without publishing anything.
