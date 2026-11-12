#!/usr/bin/env bash
# Upload the static web app and a config.js generated from the stack outputs,
# so the frontend never hardcodes environment-specific IDs.
set -euo pipefail

output() {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

WEB_URL="$(output WebUrl)"
BUCKET="$(output WebBucketName)"

cat > web/config.js <<JS
window.RECEIPTLENS_CONFIG = {
  apiUrl: "$(output ApiUrl)",
  region: "${AWS_REGION}",
  clientId: "$(output UserPoolClientId)",
  authDomain: "$(output AuthDomain)",
  redirectUri: "${WEB_URL}/"
};
JS

aws s3 sync web/ "s3://${BUCKET}/" --delete
echo "Web app published: ${WEB_URL}"
