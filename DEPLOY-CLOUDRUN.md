# POCT Hub MVP: deploy to Google Cloud Run (free tier)

> **Research and decision-support only.** Not a medical device. Synthetic data only. Never load real patient data (PHI) into this demo.

**What gets deployed**
- One container: FastAPI + SQLite. The database file is `/tmp/poct_hub.db`. It is created and seeded with synthetic data on every start.
- Cloud Run provides HTTPS.
- Basic auth (`DASHBOARD_USER` / `DASHBOARD_PASSWORD`) protects every page except `/health` (alias `/healthz`) and `/webhook/*`.
  - Use `/health` on `*.run.app`: Google's front end reserves `/healthz` there, so it never reaches the app.
  - The webhook checks an HMAC signature, and it returns 503 unless `WHATSAPP_APP_SECRET` is set.
  - If no password is configured, the app fails closed: every protected page returns 401.
- Meta Model API: optional. Without `META_API_KEY` the app runs in deterministic **stub** mode. Settings verified against https://dev.meta.ai/docs on 4 Nov 2026: base `https://api.meta.ai/v1`, `Authorization: Bearer <key>`, `POST /chat/completions`, model `muse-spark-1.3` (text and image input). See section 4b.

All commands below are for **Windows Command Prompt (cmd.exe)**, typed interactively. `^` continues a line.

## 0. Prerequisites
- Install the Google Cloud CLI, then run `gcloud init` or `gcloud auth login`.
- The project needs a **billing account** linked, even to use the free tier. Set a budget alert at **Billing → Budgets & alerts**, for example $1.
- `tar` and `curl` are built into Windows 10 and 11.

## 1. Unpack and set variables
```bat
tar -xzf poct-hub-cloudrun.tar.gz
cd poct-hub-cloudrun
set PROJECT_ID=your-project-id
set REGION=europe-west1
gcloud config set project %PROJECT_ID%
gcloud config set run/region %REGION%
gcloud projects describe %PROJECT_ID% --format="value(projectNumber)"
set PROJECT_NUMBER=PASTE_THE_NUMBER_PRINTED_ABOVE
```

## 2. Enable APIs and grant the build role
```bat
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com

gcloud projects add-iam-policy-binding %PROJECT_ID% ^
  --member=serviceAccount:%PROJECT_NUMBER%-compute@developer.gserviceaccount.com ^
  --role=roles/run.builder
```
Source deploys build with the Compute Engine default service account, which needs `roles/run.builder`. The grant can take a few minutes to propagate.

## 3. Dashboard password in Secret Manager
This step keeps the password out of the command line, shell history and screen.

In Notepad, type a long password **without pressing Enter**, save, and close. Then run:
```bat
notepad %TEMP%\poct-pw.txt
gcloud secrets create poct-dashboard-password --replication-policy=automatic --data-file=%TEMP%\poct-pw.txt
del %TEMP%\poct-pw.txt

gcloud secrets add-iam-policy-binding poct-dashboard-password ^
  --member=serviceAccount:%PROJECT_NUMBER%-compute@developer.gserviceaccount.com ^
  --role=roles/secretmanager.secretAccessor
```
The app strips any trailing newline or spaces from the password. Store the password in your password manager.

## 4. Deploy
```bat
gcloud run deploy poct-hub --source . --region %REGION% ^
  --allow-unauthenticated --min-instances 0 --max-instances 1 --memory 512Mi --cpu 1 ^
  --set-env-vars DASHBOARD_USER=demo,SEED_ON_START=true ^
  --set-secrets DASHBOARD_PASSWORD=poct-dashboard-password:latest
```
- On the first run, gcloud asks to create the Artifact Registry repository `cloud-run-source-deploy`. Answer **Y**.
- `--allow-unauthenticated` makes the service publicly reachable. The app's own basic auth still protects the pages.
- If the deploy fails with an org-policy error ("domain restricted sharing"), the project belongs to an organisation that blocks `allUsers`. Use a personal project instead.

**Why `--max-instances 1`:** SQLite is a single file inside one container. One instance means every request sees the same data.

**Data resets:** the data goes back to the synthetic seed on every **cold start**. Cold starts happen when the service scales to zero after it has been idle, and on every new revision. That is fine for a demo. `--min-instances 1` would keep the data longer, but it costs money because it is outside the free tier.

## 4b. Add the Meta key (real AI reads instead of the stub)
**Get a key:** sign in at https://dev.meta.ai, open the Model API dashboard, then **API keys → Create API key**. The key starts with `LLM_` and is shown **only once**. Source: https://dev.meta.ai/docs/authentication.

**Cost:** `muse-spark-1.3` costs $1.25 per 1M input tokens and $4.25 per 1M output tokens (https://dev.meta.ai/docs/pricing-rate-limits). A demo uses cents. Do **not** use `muse-spark-1.3-contributor`: Meta may train on content sent to that model.

**Data:** photos and message context go to Meta's API. Send only the synthetic demo meter, never a real patient photo.

In Notepad, paste the key **without pressing Enter**, save, and close. Then run:
```bat
notepad %TEMP%\meta-key.txt
gcloud secrets create meta-api-key --replication-policy=automatic --data-file=%TEMP%\meta-key.txt
del %TEMP%\meta-key.txt

gcloud secrets add-iam-policy-binding meta-api-key ^
  --member=serviceAccount:%PROJECT_NUMBER%-compute@developer.gserviceaccount.com ^
  --role=roles/secretmanager.secretAccessor
```
To replace the key later, run `gcloud secrets versions add meta-api-key --data-file=%TEMP%\meta-key.txt`, then redeploy.

Redeploy with the Meta settings. `--set-env-vars` **replaces all** environment variables, so the dashboard values are repeated here:
```bat
gcloud run deploy poct-hub --source . --region %REGION% ^
  --allow-unauthenticated --min-instances 0 --max-instances 1 --memory 512Mi --cpu 1 ^
  --set-env-vars DASHBOARD_USER=demo,SEED_ON_START=true,META_API_BASE=https://api.meta.ai/v1,META_VISION_MODEL=muse-spark-1.3,META_TEXT_MODEL=muse-spark-1.3 ^
  --set-secrets DASHBOARD_PASSWORD=poct-dashboard-password:latest,META_API_KEY=meta-api-key:latest
```
Check the result:
```bat
curl -s https://poct-hub-250296213348.europe-west1.run.app/health
curl -s -u demo https://poct-hub-250296213348.europe-west1.run.app/api/ai/ping
```
1. `/health` should show `"ai_mode":"meta"` and `"key_configured":true`. This only proves the key is **present**.
2. `/api/ai/ping` makes one tiny live call to check the key **works**. curl prompts for the dashboard password.
   - `{"ok":true,"reply_chars":2,"finish_reason":"stop",...}` means real reads are on. The ping never returns the reply text.
   - `"ok":false` with `"reply_chars":0` and `"finish_reason":"length"` means the model used the whole token budget on reasoning (Muse Spark always reasons, and reasoning tokens count against `max_completion_tokens`). The kit uses 2048 tokens with `minimal` effort for the ping, 4096 with `low` effort for photo reads, and 2048 for messages. `reasoning_tokens` shows how much was used.
   - `"error":"http_401"` means the key is wrong. Add a new secret version and redeploy.
   - `http_403` or `http_429` points to access or rate limits on the Meta account.
3. Meta's public status endpoint (no key needed): `curl -s https://api.meta.ai/v1/status`.

If the API fails, the app fails closed: the photo is treated as unreadable and the submission falls back to the typed value. Messages fall back to fixed templates. Critical results never go to the model.

**Back to stub mode:** `gcloud run services update poct-hub --region %REGION% --remove-secrets META_API_KEY`.

## 5. Verify
```bat
gcloud run services describe poct-hub --region %REGION% --format="value(status.url)"
set URL=PASTE_THE_URL_PRINTED_ABOVE
curl -s %URL%/health
curl -s -o NUL -w "%{http_code}\n" %URL%/
curl -s -u demo %URL%/api/lots
```
1. `/health` should return `{"status":"ok","db":"ok","ai_mode":"stub",...}`, or `"meta"` after section 4b.
2. `/` without credentials should return **401**.
3. In the third command, curl prompts for the password without echoing it. The output should show `LOT-G-2611B` as `"hold"`.

In a browser, open `%URL%/` (dashboard) and `%URL%/simulator`, log in as `demo`, and check:

| Simulator input | Expected result |
|---|---|
| C-001 + LOT-G-2610A + synthetic photo | **verified** |
| C-002 + LOT-G-2611B | **blocked** (lot on hold) |
| Typed value 2.1 | **critical** |

If anything fails, read the logs: `gcloud run services logs read poct-hub --region %REGION% --limit 50`.

## 6. Custom domain poct.wacp.dev (optional; Preview feature)
Cloud Run domain mapping is a **Preview** feature. Google says it is "not recommended for production" because of latency. It is available only in some regions; `europe-west1` is one of them.

```bat
gcloud components install beta
gcloud domains verify wacp.dev
gcloud domains list-user-verified
gcloud beta run domain-mappings create --service poct-hub --domain poct.wacp.dev --region %REGION%
gcloud beta run domain-mappings describe --domain poct.wacp.dev --region %REGION%
```
- `gcloud domains verify` opens Google Search Console. Add the TXT record it gives you in **Cloudflare → DNS**, then click Verify. You must be a verified owner with the same Google account that gcloud uses.
- Then add this record in **Cloudflare → DNS → Records**:

| Type | Name | Target | Proxy status | TTL |
|---|---|---|---|---|
| CNAME | `poct` | `ghs.googlehosted.com` | **DNS only (grey cloud)** | Auto |

- Keep it grey. The orange cloud (proxied) blocks Google's certificate validation.
- The certificate usually arrives in about 15 minutes but can take up to 24 hours. Check with:
```bat
nslookup poct.wacp.dev 1.1.1.1
gcloud beta run domain-mappings describe --domain poct.wacp.dev --region %REGION% --format="value(status.conditions)"
curl -s https://poct.wacp.dev/health
```
**Fallback:** if verification or the certificate stalls, give the judges the `https://poct-hub-....run.app` URL. It has HTTPS from the start and needs no DNS.

## 7. Update, rollback, take offline
```bat
:: redeploy after code changes (same command as step 4)
gcloud run revisions list --service poct-hub --region %REGION%
gcloud run services update-traffic poct-hub --region %REGION% --to-revisions REVISION_NAME=100

:: rotate the dashboard password (Notepad again, no Enter), then roll a new revision so it is picked up
notepad %TEMP%\poct-pw.txt
gcloud secrets versions add poct-dashboard-password --data-file=%TEMP%\poct-pw.txt
del %TEMP%\poct-pw.txt
gcloud run services update poct-hub --region %REGION% --update-secrets DASHBOARD_PASSWORD=poct-dashboard-password:latest

:: take it off the public internet without deleting it
gcloud run services remove-iam-policy-binding poct-hub --region %REGION% --member=allUsers --role=roles/run.invoker
```
- To delete the service completely: `gcloud run services delete poct-hub --region %REGION%`. This is irreversible, and the demo data is synthetic and ephemeral anyway.
- Old images build up in Artifact Registry under `cloud-run-source-deploy`. Delete old images there to stay inside the 0.5 GB free storage.

## Free-tier notes (check current pricing; figures are as last published)
- **Cloud Run** (request-based billing): 2 million requests, 180,000 vCPU-seconds and 360,000 GiB-seconds per month free. With `--min-instances 0`, an idle service costs nothing.
- **Cloud Build:** free build minutes each month. Each deploy uses about 1–2 minutes.
- **Artifact Registry:** 0.5 GB of storage free.
- **Secret Manager:** a small number of active secret versions and access operations are free. One or two secrets fit easily.
