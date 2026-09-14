FROM python:3.11-slim

WORKDIR /app

# The agent's raw material. Two sources, so picking the right one is part of the
# task rather than a foregone conclusion.
COPY info/ /app/info/

# `reports/` is deliberately absent — creating it is the agent's job, and a task
# that pre-creates the destination cannot distinguish "wrote the report" from
# "the directory happened to exist".
