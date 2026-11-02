#!/usr/bin/env python3
import os
import sys
from google import genai

def main():
    # 1. Initialize GenAI client
    api_key = sys.argv[1] if len(sys.argv) > 1 else os.getenv("GEMINI_API_KEY")
    if not api_key:
        api_key = input("Enter your Google AI Studio API Key: ").strip()
    
    if not api_key:
        print("Error: API key is required to proceed.", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    print("Fetching batch jobs...")
    try:
        response = client.batches.list()
    except Exception as e:
        print(f"Error fetching batch jobs: {e}", file=sys.stderr)
        sys.exit(1)

    batches = getattr(response, "batches", [])
    if not batches:
        print("No batch jobs found.")
        return

    # 2. Loop through and delete each batch job
    for batch in batches:
        batch_name = getattr(batch, "name", None) # Format typically batches/{batchId}
        if not batch_name:
            continue

        print(f"Deleting batch job: {batch_name}...")
        try:
            client.batches.delete(name=batch_name)
            print(f"Successfully deleted {batch_name}")
        except Exception as e:
            print(f"Failed to delete {batch_name}: {e}", file=sys.stderr)

    print("Done!")

if __name__ == "__main__":
    main()
