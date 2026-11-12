#!/usr/bin/env bash
# Create, describe, execute or delete a CloudFormation change set for the app stack.
#
#   scripts/changeset.sh create   -> package code, create change set, write changes.md
#   scripts/changeset.sh execute  -> execute a reviewed change set and wait
#   scripts/changeset.sh delete   -> remove a change set (pull request cleanup)
#
# Required env: STACK_NAME, CHANGE_SET_NAME
# create also needs: ARTIFACTS_BUCKET, CFN_EXECUTION_ROLE_ARN, ALERT_EMAIL, REPOSITORY
set -euo pipefail

TEMPLATE="infra/template.yaml"
PACKAGED="packaged.yaml"
out() { [[ -n "${GITHUB_OUTPUT:-}" ]] && echo "$1=$2" >> "$GITHUB_OUTPUT"; echo "$1=$2"; }

stack_status() {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "NONE"
}

check_region() {
  # The artifacts bucket and the roles' permissions are tied to the region the
  # bootstrap was deployed in; Lambda can only load code from a same-region bucket.
  local bucket_region
  bucket_region="$(aws s3api get-bucket-location --bucket "$ARTIFACTS_BUCKET" \
    --query LocationConstraint --output text)"
  [[ "$bucket_region" == "None" || "$bucket_region" == "null" ]] && bucket_region="us-east-1"
  if [[ "$bucket_region" != "$AWS_REGION" ]]; then
    echo "::error::Bootstrap is in $bucket_region but the pipeline deploys to $AWS_REGION."
    echo "Redeploy the bootstrap with --region $AWS_REGION (see README, Troubleshooting)."
    exit 1
  fi
}

create() {
  local status type reason
  check_region
  aws cloudformation package \
    --template-file "$TEMPLATE" \
    --s3-bucket "$ARTIFACTS_BUCKET" \
    --s3-prefix "packages/${STACK_NAME}" \
    --output-template-file "$PACKAGED"

  status="$(stack_status)"
  case "$status" in
    NONE|REVIEW_IN_PROGRESS) type="CREATE" ;;
    ROLLBACK_COMPLETE)
      echo "::error::Stack $STACK_NAME is in ROLLBACK_COMPLETE (its first create failed)."
      echo "Delete it once from the console or CloudShell, fix the cause, then re-run."
      exit 1 ;;
    *_IN_PROGRESS)
      echo "::error::Stack $STACK_NAME is busy ($status). Re-run when it settles."
      exit 1 ;;
    *) type="UPDATE" ;;
  esac
  out change_set_type "$type"

  aws cloudformation create-change-set \
    --stack-name "$STACK_NAME" \
    --change-set-name "$CHANGE_SET_NAME" \
    --change-set-type "$type" \
    --template-body "file://$PACKAGED" \
    --role-arn "$CFN_EXECUTION_ROLE_ARN" \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameters "ParameterKey=AlertEmail,ParameterValue=${ALERT_EMAIL}" \
    --tags "Key=Project,Value=receiptlens" "Key=Environment,Value=prod" \
           "Key=ManagedBy,Value=cloudformation" "Key=Repository,Value=${REPOSITORY}" \
    > /dev/null

  if ! aws cloudformation wait change-set-create-complete \
        --stack-name "$STACK_NAME" --change-set-name "$CHANGE_SET_NAME" 2>/dev/null; then
    reason="$(aws cloudformation describe-change-set --stack-name "$STACK_NAME" \
      --change-set-name "$CHANGE_SET_NAME" --query StatusReason --output text)"
    if [[ "$reason" == *"didn't contain changes"* || "$reason" == *"No updates are to be performed"* ]]; then
      echo "No infrastructure changes."
      aws cloudformation delete-change-set --stack-name "$STACK_NAME" --change-set-name "$CHANGE_SET_NAME"
      out has_changes false
      printf '### CloudFormation: no infrastructure changes\n' > changes.md
      return 0
    fi
    echo "::error::Change set failed: $reason"
    exit 1
  fi

  out has_changes true
  {
    # shellcheck disable=SC2016 # backticks are Markdown, not command substitution
    printf '### CloudFormation change set `%s` (%s)\n\n' "$CHANGE_SET_NAME" "$type"
    printf '| Action | Logical ID | Type | Replacement |\n| --- | --- | --- | --- |\n'
    aws cloudformation describe-change-set \
      --stack-name "$STACK_NAME" --change-set-name "$CHANGE_SET_NAME" --output json \
      | jq -r '.Changes[].ResourceChange | "| \(.Action) | \(.LogicalResourceId) | \(.ResourceType) | \(.Replacement // "-") |"'
  } > changes.md
  cat changes.md
}

execute() {
  # DescribeChangeSet does not return the change set type, so decide from the
  # stack status: a stack that was never created sits in REVIEW_IN_PROGRESS.
  local before
  before="$(stack_status)"
  aws cloudformation execute-change-set --stack-name "$STACK_NAME" --change-set-name "$CHANGE_SET_NAME"
  if [[ "$before" == "REVIEW_IN_PROGRESS" ]]; then
    aws cloudformation wait stack-create-complete --stack-name "$STACK_NAME"
  else
    aws cloudformation wait stack-update-complete --stack-name "$STACK_NAME"
  fi
  echo "Stack $STACK_NAME is now $(stack_status)."
}

delete() {
  aws cloudformation delete-change-set --stack-name "$STACK_NAME" \
    --change-set-name "$CHANGE_SET_NAME" 2>/dev/null || true
}

"${1:?usage: changeset.sh create|execute|delete}"
