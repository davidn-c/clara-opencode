#!/bin/bash

cd /home/dave/Clara_OpenCode || exit 1

echo "Starting Clara..."

source clara_venv/bin/activate

python main.py

echo "Clara exited with code $?"
