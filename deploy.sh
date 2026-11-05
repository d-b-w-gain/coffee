#!/usr/bin/env bash
# Build the coffee image, import it into k3s, and roll the deployment.
set -euo pipefail
cd "$(dirname "$0")"
TAG=$(cat app.py index.html order.html rate.html Dockerfile | sha256sum | cut -c1-10)
docker build -q -t localhost/coffee:"$TAG" .
docker save localhost/coffee:"$TAG" | sudo k3s ctr images import -
sed "s/IMAGE_TAG/$TAG/" k8s.yaml | kubectl apply -f -
kubectl -n home-automation rollout status deploy/coffee --timeout=90s
