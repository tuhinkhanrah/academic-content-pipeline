#!/bin/bash

# Accept API key from the first command-line argument ($1)
API_KEY="$1"

# If no argument was provided, prompt the user securely
if [ -z "$API_KEY" ]; then
  read -s -p "Enter your Google AI Studio API Key: " API_KEY
  echo "" # New line after silent input
fi

# Verify that an API key was provided
if [ -z "$API_KEY" ]; then
  echo "Error: API key is required to proceed."
  exit 1
fi

# 1. Fetch the list of batch jobs
echo "Fetching batch jobs..."
RESPONSE=$(curl -s "https://generativelanguage.googleapis.com/v1beta/batches?key=$API_KEY")

# 2. Extract batch resource names/IDs using grep/sed 
# (Batch resource items look like "name": "batches/abc123xyz")
BATCH_NAMES=$(echo "$RESPONSE" | grep -o '"name": *"[^"]*"' | sed 's/"name": *//g' | tr -d '"')

if [ -z "$BATCH_NAMES" ]; then
  echo "No batch jobs found."
  exit 0
fi

# 3. Loop through and delete each batch job resource
for BATCH_NAME in $BATCH_NAMES; do
  echo "Deleting batch job: $BATCH_NAME..."
  curl -X DELETE "https://generativelanguage.googleapis.com/v1beta/$BATCH_NAME?key=$API_KEY"
  echo -e "\nDeleted $BATCH_NAME"
done

echo "Done!"
