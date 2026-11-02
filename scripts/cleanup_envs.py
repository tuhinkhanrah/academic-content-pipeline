#!/usr/bin/env python3
import os
import sys
from google import genai
from google.genai import types

def main():
    # 1. Initialize GenAI client (picks up GEMINI_API_KEY from env or argument)
    api_key = sys.argv[1] if len(sys.argv) > 1 else os.getenv("GEMINI_API_KEY")
    if not api_key:
        api_key = input("Enter your Google AI Studio API Key: ").strip()
    
    if not api_key:
        print("Error: API key is required to proceed.", file=sys.stderr)
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    print("Fetching environments...")
    try:
        # Note: Using v1alpha route for beta/experimental AI Studio environments
        response = client.environments.list(api_version="v1alpha")
    except Exception as e:
        print(f"Error fetching environments: {e}", file=sys.stderr)
        sys.exit(1)

    environments = getattr(response, "environments", [])
    if not environments:
        print("No environments found.")
        return

    # 2. Loop through and delete each environment
    for env in environments:
        env_id = getattr(env, "id", None) or (env.name.split("/")[-1] if hasattr(env, "name") else None)
        if not env_id:
            continue

        print(f"Deleting environment: {env_id}...")
        try:
            client.environments.delete(id=env_id, api_version="v1alpha")
            print(f"Successfully deleted {env_id}")
        except Exception as e:
            print(f"Failed to delete {env_id}: {e}", file=sys.stderr)

    print("Done!")

if __name__ == "__main__":
    main()
