#!/bin/bash
# Oracle reference fix: make add() return the sum.
sed -i 's/return a - b/return a + b/' /app/add.py
