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

# 1. Fetch the list of environments and extract their IDs
echo "Fetching environments..."
RESPONSE=$(curl -s "https://generativelanguage.googleapis.com/v1beta/environments?key=$API_KEY")

# Extract IDs (assumes standard JSON output containing "id": "...")
# If you have 'jq', replace this parsing logic with: echo "$RESPONSE" | jq -r '.environments[].id'
ENV_IDS=$(echo "$RESPONSE" | grep -o '"id": *"[^"]*"' | sed 's/"id": *//g' | tr -d '"')

if [ -z "$ENV_IDS" ]; then
  echo "No environments found."
  exit 0
fi

# 2. Loop through and delete each environment
for ENV_ID in $ENV_IDS; do
  echo "Deleting environment: $ENV_ID..."
  curl -X DELETE "https://generativelanguage.googleapis.com/v1beta/environments/$ENV_ID?key=$API_KEY"
  echo -e "\nDeleted $ENV_ID"
done

echo "Done!"
