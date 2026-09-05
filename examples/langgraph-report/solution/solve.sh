#!/bin/bash
# Oracle reference: the report a correct agent run should produce. Lets `-a oracle`
# prove the task and its verifier are well-formed for free, before spending money
# on a real agent.
set -e
mkdir -p /app/reports
cat > /app/reports/noise-pollution.md <<'EOF'
# Noise Pollution: Health Impact and Mitigation

## Summary
Environmental noise is unwanted sound that interferes with daily activity, sleep,
and health, driven in urban areas by road traffic, rail, aircraft, and
construction. Its health burden runs mainly through cardiovascular effects and
sleep disturbance rather than hearing loss. Mitigation is well understood but
varies sharply in effectiveness by source.

## Key findings
- Prolonged exposure above 85 decibels can cause permanent hearing damage, while
  sleep fragmentation begins at night-time averages above 40 decibels.
- The World Health Organization estimates 1.6 million healthy life years are lost
  annually in Western Europe to environmental noise.
- Low-noise road surfaces cut traffic noise by 3 to 5 decibels; barriers help
  ground-level sources but not aircraft, where night flight restrictions dominate.

## Sources
- noise-pollution.md
EOF
