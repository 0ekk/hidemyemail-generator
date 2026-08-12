# Kubernetes deployment

Runs the [web UI](../../README.md#web-ui) in a cluster. The Service listens on
port 80 and the server serves the page at `/`, so the Service address by itself
is the entry point — no path or extra routing needed.

The image is `ghcr.io/0ekk/hidemyemail-generator`, built by
[`.github/workflows/docker-image.yml`](../../.github/workflows/docker-image.yml).
Its entrypoint is the CLI, so the same image also runs one-off commands.

## Image tags

| Trigger | Tags |
| --- | --- |
| Push to `main` | `main`, `sha-<short commit>`, `latest` |
| Push tag `v2.2.0` | `2.2.0`, `2.2`, `sha-<short commit>` |
| Pull request | `sha-<short commit>`, built and smoke-tested but not pushed |

`sha-<short commit>` is the only immutable tag — `main` and `latest` move with
every merge, and `2.2` moves with every patch release. Note that `latest`
follows the newest commit on `main`, not the newest release: the workflow
enables it on the default branch, which a tag push is not.

That is why `kustomization.yaml` pins `sha-…`. Deploying a new build is one
edit, and the rollout history then says exactly which commit is running:

```bash
kustomize edit set image ghcr.io/0ekk/hidemyemail-generator=*:sha-e4f5a6b
kubectl apply -k deploy/k8s
kubectl -n hidemyemail rollout status deploy/hidemyemail
```

Rolling back is `kubectl -n hidemyemail rollout undo deploy/hidemyemail`, or the
same edit with the previous tag.

If you would rather track `main` automatically, set the image tag to `latest`
**and** `imagePullPolicy: Always`; with `IfNotPresent` the node keeps serving
the first `latest` it cached.

## Before you start

You need the two secrets this deployment reads:

- an **iCloud cookie**, captured exactly as for the CLI (see
  [Cookie Management](../../README.md#cookie-management));
- a **web UI token**. Anyone who reaches the Service and holds this token
  controls the iCloud account behind the cookie. Treat it as a password.

## Install

```bash
kubectl create namespace hidemyemail

kubectl -n hidemyemail create secret generic hidemyemail \
  --from-file=cookies.txt=./cookies.txt \
  --from-literal=webui-token="$(openssl rand -hex 24)"

kubectl apply -k deploy/k8s
kubectl -n hidemyemail rollout status deploy/hidemyemail
```

Read the token back and open the UI through a port-forward:

```bash
TOKEN="$(kubectl -n hidemyemail get secret hidemyemail \
  -o jsonpath='{.data.webui-token}' | base64 -d)"

kubectl -n hidemyemail port-forward svc/hidemyemail 8765:80
# then open http://127.0.0.1:8765/?token=$TOKEN
```

The token only has to appear in the URL once — the page moves it into
`sessionStorage` and sends it as a header afterwards.

To publish it on a hostname instead, set the host in `ingress.yaml` and
uncomment it in `kustomization.yaml`.

## Rotating the cookie

iCloud sessions expire. With these manifests the cookie is a Secret mounted
read-only, so the **Update cookie** button in the UI is disabled and reports
that — the session is replaced through the cluster instead.

Replacing the Secret is enough; the file updates in place and the server reads
it per request, so no restart is needed:

```bash
kubectl -n hidemyemail create secret generic hidemyemail \
  --from-file=cookies.txt=./cookies.txt \
  --from-literal=webui-token="$TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -
```

The kubelet refreshes mounted Secrets within about a minute. Check the result on
the Overview panel, or with `kubectl -n hidemyemail exec deploy/hidemyemail --
hidemyemail whoami --cookie-file /etc/hidemyemail/cookies.txt`.

### Letting the UI manage the session instead

To paste new sessions straight into the browser, move the cookie onto the
volume, where the server can write it:

```yaml
# deployment.yaml, in the container's env
- name: HIDEMYEMAIL_COOKIE_FILE
  value: /data/cookies.txt
```

The Secret then only carries the token, and the first session is pasted through
the UI rather than created with `kubectl`. That trades declarative, auditable
credentials for a session that whoever holds the token can replace — pick the
one that matches how you run the cluster.

## What the manifests assume

| Choice | Why |
| --- | --- |
| `replicas: 1`, `strategy: Recreate` | SQLite on a ReadWriteOnce volume tolerates one writer. Apple's rate limit is per account, so a second replica buys nothing. |
| PVC at `/data` | Holds `hidemyemail.db`, `emails.txt`, and CSV exports. Everything else is disposable. |
| Cookie as a file, token as an environment variable | The token never touches a filesystem, and never appears in the pod log when it is set explicitly. |
| `readOnlyRootFilesystem`, non-root uid 1000, all capabilities dropped | Verified against the published image; `/tmp` is a small in-memory `emptyDir`. |
| `/healthz` for both probes | The one path that skips the token check. It reports that the process is serving and nothing about the account. |

## Running commands against the same data

The image's entrypoint is the CLI, and the working directory is `/data`, so
commands that only touch the database find it with no arguments:

```bash
kubectl -n hidemyemail exec deploy/hidemyemail -- hidemyemail quota
kubectl -n hidemyemail exec deploy/hidemyemail -- hidemyemail inbox status
```

Commands that talk to iCloud need the cookie path, because the Secret is mounted
outside `/data` so that rotating it does not disturb the volume:

```bash
kubectl -n hidemyemail exec deploy/hidemyemail -- \
  hidemyemail whoami --cookie-file /etc/hidemyemail/cookies.txt

kubectl -n hidemyemail exec deploy/hidemyemail -- \
  hidemyemail list --active --cookie-file /etc/hidemyemail/cookies.txt
```

Automatic cookie capture is not available in the pod: it drives a real browser
and is not installed in the image. Capture the cookie on a workstation and
update the Secret.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Pod stuck in `CreateContainerConfigError` | The `hidemyemail` Secret is missing, or has no `webui-token` key. |
| Every request answers `401` | Wrong or missing token. Read it back from the Secret. |
| UI loads, iCloud calls fail with `Missing X-APPLE-WEBAUTH-USER cookie` | The cookie file is empty or was captured from the wrong request. |
| Writes fail with `readonly database` | The PVC did not bind, so `/data` is not the volume. Check `kubectl -n hidemyemail get pvc`. |
| Pod restarts on every deploy of a new image | Expected: `Recreate` stops the old pod before the new one starts, because both want the same volume. |
