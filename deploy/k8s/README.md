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

You need one secret: a **web UI token**. Anyone who reaches the Service and
holds it controls the iCloud account behind the session, so treat it as a
password.

The iCloud cookie is not part of the manifests. It lives on the volume and is
pasted through the UI, which validates it against iCloud before saving.

## Install

```bash
kubectl create namespace hidemyemail

kubectl -n hidemyemail create secret generic hidemyemail \
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

## The iCloud session

On first start there is no session, so the Overview panel shows **no session**.
Capture a cookie the same way the CLI wants it (see
[Cookie Management](../../README.md#cookie-management)), then use **Update
cookie** on that panel and paste it. It is checked against iCloud before it is
saved, so a bad paste changes nothing.

Rotating an expired session is the same action. It applies to the next request
without a restart, because the server reads the cookie per request.

The session is stored on the volume at `/data/cookies.txt` — set by
`HIDEMYEMAIL_COOKIE_FILE` in `deployment.yaml` — precisely so the UI can
replace it. A Secret mount cannot serve that purpose: it is read-only, and the
kubelet swaps it atomically, so anything written there would be reverted within
a minute.

If you would rather keep the session declarative and out of the UI's reach,
mount it from a Secret at `/etc/hidemyemail` instead and drop that environment
variable. The button then disables itself with a note, and rotation is a
`kubectl` apply that takes effect within about a minute.

To check the session from the shell:

```bash
kubectl -n hidemyemail exec deploy/hidemyemail -- hidemyemail whoami
```

## What the manifests assume

| Choice | Why |
| --- | --- |
| `replicas: 1`, `strategy: Recreate` | SQLite on a ReadWriteOnce volume tolerates one writer. Apple's rate limit is per account, so a second replica buys nothing. |
| PVC at `/data` | Holds `hidemyemail.db`, `emails.txt`, and CSV exports. Everything else is disposable. |
| Session on the volume, token in the environment | The UI can replace the session; the token never touches a filesystem and never appears in the pod log when it is set explicitly. |
| `readOnlyRootFilesystem`, non-root uid 1000, all capabilities dropped | Verified against the published image; `/tmp` is a small in-memory `emptyDir`. |
| `/healthz` for both probes | The one path that skips the token check. It reports that the process is serving and nothing about the account. |

## Running commands against the same data

The image's entrypoint is the CLI and its working directory is `/data`, which
holds both the database and the session, so the CLI defaults resolve without
any arguments:

```bash
kubectl -n hidemyemail exec deploy/hidemyemail -- hidemyemail whoami
kubectl -n hidemyemail exec deploy/hidemyemail -- hidemyemail quota
kubectl -n hidemyemail exec deploy/hidemyemail -- hidemyemail list --active
kubectl -n hidemyemail exec deploy/hidemyemail -- \
  hidemyemail generate --label k8s --count 1
```

Automatic cookie capture is the exception: it drives a real browser and is not
installed in the image. Capture the cookie on a workstation and paste it into
the UI.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Pod stuck in `CreateContainerConfigError` | The `hidemyemail` Secret is missing, or has no `webui-token` key. |
| Every request answers `401` | Wrong or missing token. Read it back from the Secret. |
| Overview shows `no session` | Expected before the first paste. Use **Update cookie** on that panel. |
| iCloud calls fail with `Missing X-APPLE-WEBAUTH-USER cookie` | The saved session was captured from the wrong request. Paste one taken from a `maildomainws` or `hme` call. |
| **Update cookie** is disabled | `HIDEMYEMAIL_COOKIE_FILE` points somewhere read-only. With these manifests it should be `/data/cookies.txt`. |
| Writes fail with `readonly database` | The PVC did not bind, so `/data` is not the volume. Check `kubectl -n hidemyemail get pvc`. |
| Pod restarts on every deploy of a new image | Expected: `Recreate` stops the old pod before the new one starts, because both want the same volume. |
