# Using the ReceiptLens deployment

How to ship changes, operate and troubleshoot the running system once the first deployment is done. For the one-time setup, see the [README](../README.md#first-deployment).

All commands assume the app region:

```bash
export AWS_REGION=eu-central-1
STACK=receiptlens-prod
out() { aws cloudformation describe-stacks --stack-name $STACK \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
```

`out WebUrl`, `out UserPoolId`, `out ApiUrl` and `out DashboardUrl` then print the stack outputs.

---

## 1. How a change reaches production

```
branch ──► pull request ──► CI + change set comment ──► merge to main ──► change set ──► approve ──► execute ──► publish web
```

1. **Create a branch** and commit:
   ```bash
   git switch -c feat/my-change
   git commit -am "feat: ..."
   git push -u origin feat/my-change
   ```
2. **Open a pull request.** Two workflows run:
   - **CI** runs ruff, pytest, cfn-lint, shellcheck and Checkov. All must pass to merge.
   - **Deploy → Create change set** uses the read-only role. It packages the Lambdas, creates a change set and comments on the PR with the resources it would add, modify or replace. The change set is then deleted; it is only for review.
3. **Review the change set comment.** The **Replacement** column matters most; see [Reading a change set](#reading-a-change-set).
4. **Merge.** On `main`, a new change set is created and shown in the run **Summary**.
5. **Approve.** The **Execute change set (prod)** job waits at *Waiting for review*. Click **Review deployments → prod → Approve and deploy**.
6. **Wait for the deploy to finish.** The job executes exactly that change set, waits for the stack, then uploads the web app with a fresh `config.js`.

| Change | Typical deploy time |
| --- | --- |
| Web only (`web/`) | under 1 minute: no infrastructure changes, only the upload |
| Lambda code (`app/`) | 1–2 minutes |
| Infrastructure (`infra/template.yaml`) | 1–5 minutes; 10–20 minutes if CloudFront changes |

**Running it manually:** **Actions → Deploy → Run workflow** on `main`. This is useful to re-publish the web app or retry after a fix in AWS.

### Reading a change set

| Action / Replacement | Meaning | What to do |
| --- | --- | --- |
| `Add` | New resource | Fine |
| `Modify` / `False` | Updated in place | Fine |
| `Modify` / `Conditional` | May be replaced, depending on the final values | Check which property changed |
| `Modify` / `True` | **Deleted and recreated** | Stop and think. Replacing `ExpensesTable`, `DocumentsBucket` or `UserPool` **loses data or users**. Usually the cause is a changed name or key schema. |
| `Remove` | Deleted | Make sure it is intended |

---

## 2. Everyday tasks

### Change the extraction logic or API
Edit `app/extractor/` or `app/api/`, add or adjust tests in `tests/`, and open a PR. The change set shows `Modify` on `ExtractorFunction` / `ApiFunction` only.

Run the checks locally first:
```bash
ruff check app tests && pytest -q
```

### Change the web app
Edit `web/`. The change set reports *no infrastructure changes*, and the deploy job only uploads the files. CloudFront caching is disabled for this small app, so changes are visible immediately.

### Change infrastructure
Edit `infra/template.yaml`, then lint it locally:
```bash
cfn-lint && checkov -d infra --config-file .checkov.yaml --compact
```
If Checkov flags something you deliberately accept, add it to `.checkov.yaml` **with a reason**.

### Change a setting (model, budget, log retention…)
These are template parameters with defaults at the top of `infra/template.yaml`, such as `BedrockModelId`, `MonthlyBudgetUsd`, `LogRetentionDays` and `ExtractorMaxConcurrency`. Change the `Default:` and open a PR.

`AlertEmail` comes from the `ALERT_EMAIL` repository secret. After changing the secret, run the Deploy workflow and confirm the new SNS subscription email.

### Roll back a bad change
- **The deploy failed:** CloudFormation rolls the stack back to the previous version automatically. Nothing to do except fix and redeploy.
- **The deploy succeeded but the app is broken:** revert the merge and let the pipeline deploy the previous version:
  ```bash
  git revert -m 1 <merge-commit-sha>   # or: git revert <commit-sha>
  git push                             # via a PR if main is protected
  ```

**Don't change resources by hand in the console.** The next deploy may overwrite the change, and the stack drifts from the code. To check for drift:
```bash
DID=$(aws cloudformation detect-stack-drift --stack-name $STACK --query StackDriftDetectionId --output text)
sleep 30
aws cloudformation describe-stack-drift-detection-status --stack-drift-detection-id $DID \
  --query '[DetectionStatus,StackDriftStatus]' --output text
```

---

## 3. Users

Self sign-up is disabled, so users are invited.

```bash
POOL=$(out UserPoolId)

# invite (the temporary password arrives by email)
aws cognito-idp admin-create-user --user-pool-id $POOL --username user@example.com \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true

# list
aws cognito-idp list-users --user-pool-id $POOL --query 'Users[].[Username,UserStatus,Enabled]' --output table

# resend the invite / reset the password
aws cognito-idp admin-create-user --user-pool-id $POOL --username user@example.com --message-action RESEND
aws cognito-idp admin-reset-user-password --user-pool-id $POOL --username user@example.com

# disable or delete
aws cognito-idp admin-disable-user --user-pool-id $POOL --username user@example.com
aws cognito-idp admin-delete-user  --user-pool-id $POOL --username user@example.com
```

Each user is their own tenant: documents and expenses are keyed by the user's Cognito `sub`. There are no roles; every account is a normal user.

### Demo user (no email, permanent password)
Use this to hand an interviewer a working login on the spot:
```bash
DEMO=demo@receiptlens.test
PASS='Demo-ReceiptLens-2026'   # min 12 chars, upper + lower + digit

aws cognito-idp admin-create-user --user-pool-id $POOL \
  --username $DEMO --message-action SUPPRESS \
  --user-attributes Name=email,Value=$DEMO Name=email_verified,Value=true

aws cognito-idp admin-set-user-password --user-pool-id $POOL \
  --username $DEMO --password "$PASS" --permanent
```
- **`--message-action SUPPRESS`** sends no email, so the address doesn't need to exist.
- **`--permanent`** skips the first-sign-in password change.
- **Check the status:** the user should show as `CONFIRMED` in `list-users`. Users invited by email stay `FORCE_CHANGE_PASSWORD` until their first sign-in.
- **Afterwards:** disable the account (`admin-disable-user`, reversible) or delete it (`admin-delete-user`). Deleting an account does not delete its documents. Remove those by `sub` (see [Data](#5-data)).

### From the console
Cognito → User pools → `receiptlens-prod` → Users → **Create user**.
- **Normal user:** choose **Send an email invitation**.
- **Demo user:** choose **Don't send an invitation** and **Set a password**. Console passwords are temporary, so make it permanent with `admin-set-user-password ... --permanent`.

---

## 4. Monitoring

| What | Where |
| --- | --- |
| Dashboard: documents, NeedsReview, Textract fallbacks, Bedrock tokens, queue depth, Lambda errors and p95 | `out DashboardUrl` |
| Alarms: DLQ not empty, extractor errors, API 5xx, Bedrock token spike | CloudWatch → Alarms (`receiptlens-prod-*`); notifications go to `ALERT_EMAIL` |
| Traces | CloudWatch → X-Ray traces (both Lambdas use active tracing) |
| Budget | Billing → Budgets → `receiptlens-prod-monthly` |

**Logs:**
```bash
aws logs tail /aws/lambda/receiptlens-prod-extractor --since 1h --follow
aws logs tail /aws/lambda/receiptlens-prod-api --since 1h
aws logs tail /aws/apigateway/receiptlens-prod --since 1h      # API access log
```

**Documents that keep failing (DLQ):** after 3 failed attempts, a message moves to the DLQ and the alarm fires.
```bash
DLQ_URL=$(aws sqs get-queue-url --queue-name receiptlens-prod-extract-dlq --query QueueUrl --output text)
DLQ_ARN=$(aws sqs get-queue-attributes --queue-url $DLQ_URL --attribute-names QueueArn --query Attributes.QueueArn --output text)

# look at one message without removing it
aws sqs receive-message --queue-url $DLQ_URL --visibility-timeout 0 --max-number-of-messages 1

# after fixing the cause, send everything back to the main queue for another try
aws sqs start-message-move-task --source-arn $DLQ_ARN
```

**Costs:** in Cost Explorer, filter by the tag `Project = receiptlens`. Activate it once under **Billing → Cost allocation tags**.

---

## 5. Data

```bash
TABLE=receiptlens-prod-expenses

# all documents of one user (tenant_id = the user's Cognito sub)
SUB=$(aws cognito-idp admin-get-user --user-pool-id $(out UserPoolId) --username user@example.com \
  --query "UserAttributes[?Name=='sub'].Value" --output text)
aws dynamodb query --table-name $TABLE --key-condition-expression "tenant_id = :t" \
  --expression-attribute-values "{\":t\":{\"S\":\"$SUB\"}}" \
  --query 'Items[].[doc_id.S,status.S,expense.M.supplier_name.S,expense.M.total.N]' --output table

# original files
aws s3 ls s3://$(out DocumentsBucketName)/uploads/$SUB/ --recursive
```

| Data | Protection |
| --- | --- |
| DynamoDB | Point-in-time recovery, 35 days. Restore to a new table: `aws dynamodb restore-table-to-point-in-time --source-table-name $TABLE --target-table-name ${TABLE}-restore --use-latest-restorable-time` |
| S3 documents | Versioned. Moved to Standard-IA after 30 days; old versions expire after 30 days. |

---

## 6. Updating the bootstrap

You only need to update the bootstrap stack to change what GitHub is allowed to do, for example after renaming the repo or changing the deploy permissions. Always deploy it in the app region:

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
aws cloudformation deploy --region eu-central-1 --stack-name receiptlens-bootstrap \
  --template-file bootstrap/bootstrap.yaml --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides GitHubOwner=<owner> GitHubRepo=<repo> \
    ExistingOidcProviderArn=arn:aws:iam::$ACCOUNT:oidc-provider/token.actions.githubusercontent.com \
    GitHubSubjectPrefix='<prefix, if GitHub sends IDs in the subject>'
```

If OIDC login fails afterwards, see the README section [Debugging the OIDC role assumption](../README.md#debugging-the-oidc-role-assumption).

---

## 7. Pausing and tearing down

At demo traffic the stack costs cents per month, so there is usually no need to pause it. To remove everything:

```bash
# empty the buckets (versioned, so delete versions too)
for B in $(out DocumentsBucketName) $(out WebBucketName); do
  aws s3api list-object-versions --bucket $B --output json \
    --query '{Objects: [Versions, DeleteMarkers][][].{Key:Key,VersionId:VersionId}}' > /tmp/v.json
  [ "$(jq '.Objects | length' /tmp/v.json)" -gt 0 ] && aws s3api delete-objects --bucket $B --delete file:///tmp/v.json
done

aws cloudformation delete-stack --stack-name $STACK
aws cloudformation wait stack-delete-complete --stack-name $STACK
```

To bring it back, run **Actions → Deploy → Run workflow**. A new user pool means new users, and the CloudFront URL changes.

Delete `receiptlens-bootstrap` only if you are done with CI/CD for good. Empty its artifacts bucket first. The shared GitHub OIDC provider is not part of the stack and stays.

---

## 8. Demo checklist (interview)

1. **Before the interview,** create a demo user (see [Users](#3-users)) and give its email and password to the interviewer if they want to try the app.
2. **Show the code path:** a small PR, its CI checks and the change set comment.
3. **Merge, approve the deploy,** and point out the read-only plan role vs the execute-only deploy role.
4. **Open the web app** and upload a real receipt. It shows up as `EXTRACTED` within seconds.
5. **Upload a blurry or partial receipt.** It shows up as `NEEDS_REVIEW` with the validation issues listed. Explain the Textract fallback.
6. **Show the dashboard:** tokens per document, the fallback rate, and the queue staying at 0.
7. **Open an X-Ray trace** of one extraction.
8. **Talk through cost:** the budget, the tag filter in Cost Explorer, and the `.checkov.yaml` list of deliberate trade-offs (no NAT, no WAF, no customer-managed KMS keys) with their reasons.
9. **Tell the troubleshooting story:** OIDC subject with immutable IDs, the bootstrap region, and how CloudTrail made each one visible.
