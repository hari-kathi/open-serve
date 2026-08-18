#!/usr/bin/env bash
# Tear down the open-serve kind e2e example.
set -euo pipefail
kind delete cluster --name open-serve
