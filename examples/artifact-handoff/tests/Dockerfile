FROM python:3.11-slim

# The grading image bakes its own tests. In separate mode nothing is copied in at
# run time, which is the point: the agent never had a chance to edit this file.
COPY test.sh /tests/test.sh
