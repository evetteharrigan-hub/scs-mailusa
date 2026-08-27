#!/bin/bash
cd "$(dirname "$0")"
echo "Starting ASYCUDA XML Generator..."
echo "Server will be available at http://localhost:8000"
uvicorn main:app --host 0.0.0.0 --port 8000
