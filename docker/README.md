# Docker deployment

The dashboard runs entirely as a read-only observer of your local VS Code data.
Inside the container we:

* listen on `0.0.0.0:8770`
* mount your VS Code user directory **read-only** at `/data/Code`
* set `CD_VSCODE_USER_DIR=/data/Code/User`

Nothing is written back to the host.

## Quickstart (compose)

```bash
docker compose up -d --build
# → http://127.0.0.1:8770
```

To stop:

```bash
docker compose down
```

## Quickstart (plain docker)

```bash
docker build -t copilot-dashboard .

# Linux
docker run -d --name copilot-dashboard \
  -p 127.0.0.1:8770:8770 \
  -v "$HOME/.config/Code:/data/Code:ro" \
  copilot-dashboard

# macOS
docker run -d --name copilot-dashboard \
  -p 127.0.0.1:8770:8770 \
  -v "$HOME/Library/Application Support/Code:/data/Code:ro" \
  copilot-dashboard

# Windows (PowerShell)
docker run -d --name copilot-dashboard `
  -p 127.0.0.1:8770:8770 `
  -v "$env:APPDATA\Code:/data/Code:ro" `
  copilot-dashboard
```

## Watching multiple installs (Code + Code-Insiders + VSCodium)

`CD_VSCODE_USER_DIR` accepts a `:`-separated list (`;` on Windows):

```yaml
environment:
  CD_VSCODE_USER_DIR: /data/Code/User:/data/CodeInsiders/User
volumes:
  - ${HOME}/.config/Code:/data/Code:ro
  - ${HOME}/.config/Code - Insiders:/data/CodeInsiders:ro
```

## Permissions

The image runs as UID 1000. On Linux this matches the typical desktop user, so
the read-only bind mount works out of the box. If your UID differs, either:

* rebuild with `--build-arg ...` (TODO: parameterise) — or
* add `user: "${UID}:${GID}"` in `docker-compose.yml`.

## Watching live updates

`watchdog` uses inotify on Linux. Bind mounts forward inotify events from host
to container, so live tick updates Just Work. On Docker Desktop (macOS /
Windows) inotify over the virtualised FS is best-effort — fall back to the
2-second activity poller, which still updates the UI promptly.
