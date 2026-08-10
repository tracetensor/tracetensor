#!/bin/bash
set -euo pipefail
sed -i 's/return a + b/return a * b/' /app/multiply.py
