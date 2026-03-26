# CI/CD Workflows

## Overview

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `ci-build-push.yml` | Push to `development`/`main`, PRs | Run tests, build Docker images, push to DEV ECR |
| `DEV-CD-deploy.yml` | Manual (workflow_dispatch) | Deploy specific image tags to DEV Lambda functions |
| `PROD-CD-lambda-deploy.yml` | Manual (workflow_dispatch) | Promote DEV images to PROD ECR & deploy to PROD Lambdas |

## Pipeline Flow

```
PR / Push
    |
    v
[CI] ci-build-push.yml
    1. Run pytest (353 tests)
    2. Build 6 Docker images (parallel matrix)
    3. Push to DEV ECR with git SHA tag + latest
    |
    v (manual trigger)
[DEV CD] DEV-CD-deploy.yml
    - Select which Lambdas to deploy
    - Provide image tag (git SHA from CI)
    - Updates DEV Lambda function code
    |
    v (manual trigger, after QA)
[PROD CD] PROD-CD-lambda-deploy.yml
    - Pull image from DEV ECR
    - Re-tag and push to PROD ECR
    - Update PROD Lambda function code
```

## Lambda → ECR Mapping

| Lambda Function | DEV ECR Repo | PROD ECR Repo |
|-----------------|-------------|---------------|
| `ieee-cc-pdf-extractor` | `ieee-cc-pdf-extractor` | `prod-ieee-cc-pdf-extractor` |
| `ieee-cc-video-transcriber` | `ieee-cc-video-transcriber` | `prod-ieee-cc-video-transcriber` |
| `ieee-cc-bedrock-inference` | `ieee-cc-bedrock-inference` | `prod-ieee-cc-bedrock-inference` |
| `ieee-rc-ai-orchestrator` | `ieee-rc-ai-orchestrator` | `prod-ieee-rc-ai-orchestrator` |
| `ieee-rc-image-generator` | `ieee-rc-image-generator` | `prod-ieee-rc-image-generator` |
| `ieee-rc-dlq-processor` | `ieee-rc-dlq-processor` | `prod-ieee-rc-dlq-processor` |

## Image Tagging Strategy

- **CI builds** tag with short git SHA (e.g. `ee96758`) + `latest`
- **DEV CD** deploys a specific tag to DEV Lambdas
- **PROD CD** promotes a DEV tag to PROD ECR (same tag + `latest`) and deploys

## Requirements

- **Self-hosted runner** with label `lambda-builder` (for Docker builds)
- **AWS credentials** configured on the runner (IAM role or access keys)
- Runner needs: `docker`, `aws` CLI, `python3`

## PROD Setup (TODO)

Before using PROD CD, the DevOps engineer needs to:

1. Create PROD ECR repos (prefixed with `prod-`)
2. Create PROD Lambda functions with PROD-specific env vars
3. Create PROD IAM roles with appropriate permissions
4. Update PROD Lambda names and ECR repos in `PROD-CD-lambda-deploy.yml` env vars
5. Create PROD SQS queue, SNS topics if needed
