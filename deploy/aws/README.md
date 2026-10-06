# Deploying Consilium-Health to AWS ECS Fargate

> **Not medical advice.** This is an educational software project. It does not diagnose, treat, or
> provide clinical guidance, and it must not be used for real medical decisions. No patient data of
> any kind may be used with it.

The service runs as one Fargate task behind a public IP: the FastAPI app from `consilium/api/`,
the self-hosted BM25 + bge-small + RRF retriever, and a ChromaDB index on an EFS volume. GitHub
Actions builds the image on every push to `main`, smoke-tests it, pushes it to ECR and rolls the
service — assuming an IAM role through GitHub's OIDC provider, so no AWS access key is stored
anywhere.

```
GitHub Actions ──OIDC──▶ IAM DeployRole ──▶ ECR (image) ──▶ ECS Fargate service (1 task, 0.5 vCPU / 2 GiB)
                                                                 ├── /data on EFS (chroma index, episodic.db, traces)
                                                                 ├── ANTHROPIC_API_KEY from Secrets Manager
                                                                 └── stdout → CloudWatch Logs /ecs/consilium-health
```

Everything AWS-side is one CloudFormation template, `deploy/aws/stack.yml`. Nothing here needs
the AWS CLI; the console is enough.

## What it costs

| item | price (us-east-1, list) | note |
|---|---|---|
| Fargate task, 0.5 vCPU / 2 GiB | about $0.029 per hour, about $0.70 per day | the only meaningful line; set `DesiredCount` to 0 between demos and it stops |
| EFS | $0.30 per GB-month | the index and traces are a few MB |
| ECR | $0.10 per GB-month | the image is about 1.2 GB; the lifecycle policy keeps 5 |
| Secrets Manager | $0.40 per secret-month | one secret |
| CloudWatch Logs | first 5 GB free | retention is 14 days |
| Anthropic | per answer | a demo turn with the default model is on the order of a cent |

There is no load balancer on purpose: an ALB is about $16 per month, more than everything else
combined, and a demo does not need a stable hostname. The cost is that the public IP changes when
the task is replaced; the deploy job prints the current one in its summary.

## One-time setup

### 1. AWS account and region

Create an account at <https://aws.amazon.com/free> (a card is required; nothing below is charged
until a task actually runs). Pick a region and keep it for everything: **us-east-1** is the
default the workflow assumes; set the repository variable `AWS_REGION` for any other. A free-plan
account may be limited to one region (this project's was `us-east-2`) and its service control
policy forbids creating an IAM OIDC provider, which is what the `access-key` auth mode below is for.

### 2. Create the stack

1. Open the CloudFormation console in that region → **Create stack** → *With new resources* →
   **Upload a template file** → choose `deploy/aws/stack.yml` from your clone.
2. Stack name: `consilium-health`.
3. Parameters: pick the **default VPC** for `VpcId` and two of its subnets (different availability
   zones) for `SubnetA` and `SubnetB`; leave the rest at their defaults (`Provider` = anthropic,
   `DesiredCount` = 1).
4. On the last page tick the acknowledgement that the stack creates IAM resources with custom
   names, then **Submit**. It takes three to five minutes.
5. Open the stack's **Outputs** tab: copy `DeployRoleArn` and note `ProviderKeySecretArn`.

The service exists at this point but has no image to run yet; its tasks will fail to start until
the first deploy in step 4. That is expected.

If the stack fails with *"Provider with url https://token.actions.githubusercontent.com already
exists"*, the account already has the GitHub OIDC provider: delete the failed stack and create it
again with `CreateGitHubOidcProvider` = `false`.

### 3. Put the provider key in Secrets Manager

Secrets Manager console → secret `consilium/provider-api-key` → **Retrieve secret value** →
**Edit** → replace `REPLACE_ME_IN_THE_CONSOLE` with the Anthropic API key, as a plain string
(not JSON) → **Save**. The task reads it as `ANTHROPIC_API_KEY` at start; the key never appears
in the repository, the template, or the GitHub Actions logs.

### 4. Let GitHub Actions deploy

GitHub → the repository → **Settings → Secrets and variables → Actions**.

With `GitHubAuthMode` = `oidc` (the default), add a **variable**:

| name | value |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | the `DeployRoleArn` output from step 2 |
| `AWS_REGION` | the region, if not `us-east-1` |

With `GitHubAuthMode` = `access-key` (needed on the AWS free plan, whose service control policy
forbids creating an OIDC provider): IAM console → Users → `consilium-health-github-deploy` →
**Security credentials → Create access key** → *Application running outside AWS* → copy both
values once, then add them as repository **secrets** `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY`, plus the `AWS_REGION` variable. The user holds only the deploy policy
(one ECR repository, one ECS service), so a leaked key could redeploy the demo and nothing else;
rotate it from the same console page.

Then **Actions → deploy → Run workflow** (or push to `main`). The job builds the image, runs it
with the mock provider until `/healthz` answers, pushes it to ECR, registers a new task
definition revision and waits for the service to stabilise — about ten minutes the first time,
mostly the image build. The job summary ends with the demo page URL, `http://<ip>:8000/`.

With OIDC, the deploy role's trust policy only accepts tokens from this repository's `main`
branch (`repo:Lexieli666/consilium-health:ref:refs/heads/main`); either way the permissions are
limited to this one ECR repository and ECS service.

## Day to day

- **Deploy a change**: push to `main`. The workflow is the deployment.
- **Stop paying for compute**: CloudFormation → the stack → **Update** → *Use existing template*
  → `DesiredCount` = 0. Set it back to 1 to start again; the EFS volume keeps the index, memory
  and traces across stop/start.
- **Logs**: CloudWatch → Log groups → `/ecs/consilium-health`. The app logs JSON lines.
- **Rebuild the index**: delete `/data/chroma` on the volume, or simply change nothing — the app
  re-ingests `data/corpus/` on start when the store's chunk count does not match the corpus.
- **Tear everything down**: ECR console → `consilium-health` → delete all images first (a
  non-empty repository blocks stack deletion), then CloudFormation → delete the stack. Secrets
  Manager schedules the secret for deletion after a recovery window; that is normal.

## Running the image locally

```bash
docker compose up --build          # http://127.0.0.1:8000/ with the mock provider
# or, with a real provider: copy .env.example to .env, set CONSILIUM_PROVIDER and the key, then the same command
docker build -t consilium-health:local .
docker run --rm -p 8000:8000 -e CONSILIUM_PROVIDER=mock consilium-health:local
```

On an Apple Silicon Mac `docker build` produces an arm64 image, which is fine for local use;
`docker build --platform linux/amd64 .` reproduces what the workflow ships to Fargate.

## Design notes

- The image installs torch from PyTorch's CPU wheel index instead of from `uv.lock`, which on
  Linux resolves the CUDA build and several GB of libraries the service never uses. The rest of
  the environment is installed from the lock with `uv sync --frozen`.
- `BAAI/bge-small-en-v1.5` is downloaded at build time into the image and the container runs with
  `HF_HUB_OFFLINE=1`, so a task never contacts Hugging Face and starts in well under a minute.
- The container runs as uid 1000 and the EFS access point enforces the same uid, so a task can
  only write under `/consilium` on the file system, mounted at `/data`.
- Fargate task size is a parameter. 0.5 vCPU / 2 GiB is comfortable for bge-small on CPU; 1 GiB
  works but leaves little headroom while the index is being built on first start.
