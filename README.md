# ReceiptLens

Turn a photo or PDF of a receipt or invoice into validated expense data, on a serverless, event-driven AWS pipeline that costs a few cents a month at demo volume.

**Problem.** Small businesses, freelancers and accountants still retype receipts by hand. Off-the-shelf extraction (Textract AnalyzeExpense) costs $10 per 1,000 pages, and Romanian is not among Textract's announced OCR languages.

**Solution.** A Bedrock vision model reads the document and returns strict JSON. Business rules check the totals, VAT and dates. Only documents that fail validation fall back to Textract.

## Architecture

```
Browser ──► CloudFront + S3 (web app)
   │
   ├─► Cognito (hosted UI, PKCE) ──► access token
   │
   ├─► API Gateway HTTP API (JWT authorizer) ──► Lambda api
   │        POST /uploads → presigned S3 POST (type + size limited)
   │        GET  /expenses, /expenses/{id}, /export (CSV)
   │
   └─► S3 documents bucket (direct upload, uploads/<tenant>/<doc>/<file>)
            │ Object Created
            ▼
        EventBridge rule ──► SQS queue (+ DLQ after 3 failures)
                                 │ max concurrency 2
                                 ▼
                          Lambda extractor
                            1. Bedrock Converse (image/PDF → forced tool call → JSON)
                            2. validate totals, VAT (RO rates), dates
                            3. Textract AnalyzeExpense fallback if invalid
                            4. DynamoDB: EXTRACTED or NEEDS_REVIEW
                            5. EMF metrics: documents, tokens, fallbacks

CloudWatch: alarms (DLQ, errors, 5xx, token spike) → SNS email · dashboard · X-Ray
AWS Budgets: monthly budget with 80% actual / 100% forecast alerts
```

| Service | Why it's here |
| --- | --- |
| Amazon Cognito (Lite) | Sign-in; the user's `sub` is the tenant ID, so isolation comes from the verified token |
| CloudFront + S3 | Static web app; bucket is private and reachable only through Origin Access Control |
| API Gateway HTTP API | JWT authorizer, throttling, access logs; $1 per million requests vs $3.50 for REST |
| S3 | Browser uploads directly via presigned POST, so files never pass through Lambda |
| EventBridge | Filters `Object Created` events to the `uploads/` prefix |
| SQS + DLQ | Absorbs bursts; caps concurrency so Bedrock quotas are never hit; poison files land in the DLQ |
| Lambda (Python 3.13, arm64) | Extraction, validation, API |
| Amazon Bedrock | Multimodal model reads the image; tool use forces schema-shaped JSON |
| Amazon Textract | Fallback only, when validation fails |
| DynamoDB | `tenant_id` + `doc_id`, on-demand, point-in-time recovery |
| CloudWatch, X-Ray, SNS | Metrics, alarms, dashboard, tracing, email alerts |
| AWS Budgets | Cost guardrail |

## Repository layout

```
bootstrap/bootstrap.yaml  One-time stack: GitHub OIDC provider, plan + deploy roles,
                          CloudFormation execution role, artifacts bucket
infra/template.yaml       All application infrastructure (CloudFormation)
app/extractor/            SQS-triggered extraction Lambda
app/api/                  HTTP API Lambda
web/                      Static web client (no build step)
scripts/changeset.sh      Package code, create / execute / delete change sets
scripts/publish-web.sh    Upload the web app with config.js built from stack outputs
tests/                    pytest unit tests
.github/workflows/        CI (lint, tests, cfn-lint, Checkov) and Deploy (change set on PR, execute on main)
.checkov.yaml             Accepted security findings, each with its reason
```

## CI/CD and security model

- **No AWS keys in GitHub.** Actions authenticates with OIDC and assumes a short-lived IAM role.
- **Three roles, least privilege.**
  - `receiptlens-github-plan` is read-only and can also *create* change sets. Pull requests and `main` can assume it.
  - `receiptlens-github-deploy` can only *execute* change sets and publish the web bucket. Only jobs in the `prod` GitHub environment can assume it, and that environment requires a manual approval.
  - `receiptlens-cfn-execution` is the role CloudFormation itself uses to create resources. The GitHub roles can only pass it to CloudFormation and never hold these broad permissions themselves.
- **Review, then execute exactly that.**
  - Every pull request gets a comment listing the resources the change set would add, modify or replace. The change set is deleted afterwards.
  - On `main`, the same kind of change set is created and shown in the job summary. After approval, the deploy job executes exactly that change set.
- **No state bucket.** CloudFormation keeps the state of the stack itself, so there is no state file to store, lock or lose.
- **Checks on every change.** cfn-lint and shellcheck lint the templates and scripts. Checkov fails the build on any finding not listed in `.checkov.yaml`, and every listed skip states its reason.

## First deployment

### Why a bootstrap step?
GitHub can only log in to AWS through OIDC once the OIDC provider and the IAM roles exist. Creating them requires AWS access, which is exactly what they provide. So a human with admin rights deploys `bootstrap/bootstrap.yaml` **once**, and every deploy after that goes through OIDC with no long-lived keys.

### 0. Prerequisites
- An AWS account on the **Paid plan**, so it doesn't close after 6 months, with MFA on root and an admin user (IAM Identity Center).
- In the Bedrock console for your region, open the model catalog and confirm **Amazon Nova Lite** is available. Using a different model? See *Changing the model* below.

### 1. Bootstrap, once, from AWS CloudShell
Open CloudShell in the console. It is already signed in as you, so you need no keys on your laptop. Upload `bootstrap/bootstrap.yaml` or clone the repo, then run:
```bash
aws cloudformation deploy \
  --stack-name receiptlens-bootstrap \
  --template-file bootstrap/bootstrap.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides GitHubOwner=<your-github-user> GitHubRepo=<repo-name>

aws cloudformation describe-stacks --stack-name receiptlens-bootstrap \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table
```
`GitHubOwner` and `GitHubRepo` must match GitHub exactly, including capitalisation: the role trust check is case-sensitive.

You can also deploy the same file from the CloudFormation console with **Create stack → Upload a template file**.

#### If the account already has a GitHub OIDC provider
An AWS account can hold only one OIDC provider per URL. If an earlier project already created one for GitHub, the deploy fails with:

```
Failed to create the changeset ... [AWS::EarlyValidation::ResourceExistenceCheck]
```

Nothing has been created at that point. Reuse the existing provider instead:

```bash
# 1. Confirm it exists and copy its ARN
aws iam list-open-id-connect-providers
#    → arn:aws:iam::<account-id>:oidc-provider/token.actions.githubusercontent.com

# 2. Deploy again, passing that ARN
aws cloudformation deploy \
  --stack-name receiptlens-bootstrap \
  --template-file bootstrap/bootstrap.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides GitHubOwner=<your-github-user> GitHubRepo=<repo-name> \
    ExistingOidcProviderArn=arn:aws:iam::<account-id>:oidc-provider/token.actions.githubusercontent.com
```

The failed attempt leaves the stack in `REVIEW_IN_PROGRESS`. That is harmless, and the second `deploy` reuses it. The shared provider stays outside this stack, so deleting `receiptlens-bootstrap` later won't break your other projects.

Before reusing the provider, check that its audience includes `sts.amazonaws.com`:

```bash
aws iam get-open-id-connect-provider \
  --open-id-connect-provider-arn arn:aws:iam::<account-id>:oidc-provider/token.actions.githubusercontent.com
```

Look for `sts.amazonaws.com` under `ClientIDList`. If it's missing, add it:

```bash
aws iam add-client-id-to-open-id-connect-provider \
  --open-id-connect-provider-arn <arn> --client-id sts.amazonaws.com
```

### 2. Configure the GitHub repository
Under **Settings → Secrets and variables → Actions**:

| Type | Name | Value |
| --- | --- | --- |
| Variable | `AWS_REGION` | `eu-central-1` |
| Variable | `AWS_PLAN_ROLE_ARN` | `PlanRoleArn` output |
| Variable | `AWS_DEPLOY_ROLE_ARN` | `DeployRoleArn` output |
| Variable | `CFN_EXECUTION_ROLE_ARN` | `CloudFormationExecutionRoleArn` output |
| Variable | `ARTIFACTS_BUCKET` | `ArtifactsBucket` output |
| Secret | `ALERT_EMAIL` | where alarms and budget alerts go |

Under **Settings → Environments**, create `prod` and add yourself as a required reviewer.

Under **Settings → Branches**, protect `main`: require a PR and require the CI checks to pass.

### 3. Deploy
Push to `main` (or open a PR first to see the change set), then approve the `prod` deployment in the Actions tab. The first deploy takes about 5–10 minutes, most of it creating the CloudFront distribution. When it finishes:

1. Confirm the SNS subscription email.
2. Create your user:
   ```bash
   aws cloudformation describe-stacks --stack-name receiptlens-prod \
     --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text
   aws cognito-idp admin-create-user \
     --user-pool-id <UserPoolId> \
     --username you@example.com \
     --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true
   ```
3. Open `WebUrl` from the job output, sign in with the temporary password from the email, and upload a receipt.

If the very first deploy fails, the stack ends in `ROLLBACK_COMPLETE` and cannot be updated. Delete it once (`aws cloudformation delete-stack --stack-name receiptlens-prod`), fix the cause shown in the stack events, and push again.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install pytest ruff boto3 cfn-lint checkov
ruff check app tests
pytest -q
cfn-lint
checkov -d infra -d bootstrap --config-file .checkov.yaml --compact
```

## Changing the model

The `BedrockModelId` and `BedrockFoundationModel` parameters in `infra/template.yaml` select the model. The default, `eu.amazon.nova-lite-v1:0`, is an EU cross-region inference profile, so requests stay in EU regions.

Any Bedrock model that supports image input and tool use works. For a Claude model, set both parameters to its EU inference profile and foundation model IDs. The first time, you may need to submit the Anthropic use-case form in the Bedrock console.

## Cost (estimate, low traffic)

| Item | Monthly |
| --- | --- |
| Lambda, SQS, EventBridge, API Gateway, DynamoDB, S3 | ~$0 – 0.50 |
| Bedrock, Nova Lite, a few hundred pages | ~$0.10 – 0.30 |
| Textract fallback, a minority of pages at $0.01 each | cents |
| CloudFront, CloudWatch (14-day retention), X-Ray | ~$0 (free tiers) |
| **Total** | **well under $5**; the budget alerts at $4 actual / $5 forecast |

Things that would cost real money are deliberately absent: NAT gateway, ALB, customer-managed KMS keys and WAF. `.checkov.yaml` explains each one.

## Tear down

```bash
# Buckets must be empty before CloudFormation can delete them
aws s3 rm s3://receiptlens-prod-documents-<account-id> --recursive   # also delete old versions in the console
aws s3 rm s3://receiptlens-prod-web-<account-id> --recursive
aws cloudformation delete-stack --stack-name receiptlens-prod
# last, and only if you are done with CI/CD:
aws cloudformation delete-stack --stack-name receiptlens-bootstrap
```

## Roadmap

- Human review screen for `NEEDS_REVIEW` documents (edit fields, approve)
- Custom domain with ACM certificate and TLS 1.2+ policy
- Step Functions for multi-page PDFs split per page
- e-Factura (ANAF SPV) XML import, so B2B invoices skip OCR entirely
