#!/usr/bin/env python3
import argparse
import getpass
import os
import sys

from google import genai


def cleanup_batches(client):
    print("Fetching batch jobs...")
    try:
        batches = getattr(client.batches.list(), "batches", None) or []
    except Exception as error:
        print(f"Error fetching batch jobs: {error}", file=sys.stderr)
        return False

    if not batches:
        print("No batch jobs found.")
        return True

    successful = True
    for batch in batches:
        batch_name = getattr(batch, "name", None)
        if not batch_name:
            continue

        print(f"Deleting batch job: {batch_name}...")
        try:
            client.batches.delete(name=batch_name)
            print(f"Successfully deleted {batch_name}")
        except Exception as error:
            print(f"Failed to delete {batch_name}: {error}", file=sys.stderr)
            successful = False

    return successful


def cleanup_environments(client):
    print("Fetching environments...")
    try:
        environments = getattr(
            client.environments.list(api_version="v1alpha"), "environments", None
        ) or []
    except Exception as error:
        print(f"Error fetching environments: {error}", file=sys.stderr)
        return False

    if not environments:
        print("No environments found.")
        return True

    successful = True
    for environment in environments:
        name = getattr(environment, "name", None)
        environment_id = getattr(environment, "id", None) or (
            name.rsplit("/", 1)[-1] if name else None
        )
        if not environment_id:
            continue

        print(f"Deleting environment: {environment_id}...")
        try:
            client.environments.delete(id=environment_id, api_version="v1alpha")
            print(f"Successfully deleted {environment_id}")
        except Exception as error:
            print(f"Failed to delete {environment_id}: {error}", file=sys.stderr)
            successful = False

    return successful


def main():
    parser = argparse.ArgumentParser(
        description="Delete Google AI Studio batch jobs, environments, or both."
    )
    parser.add_argument(
        "resource",
        choices=("batches", "environments", "all"),
        help="which resources to delete",
    )
    parser.add_argument(
        "--api-key",
        help="Google AI Studio API key (defaults to GEMINI_API_KEY or a secure prompt)",
    )
    args = parser.parse_args()

    api_key = args.api_key or os.getenv("GEMINI_API_KEY")
    if not api_key:
        api_key = getpass.getpass("Enter your Google AI Studio API Key: ").strip()
    if not api_key:
        print("Error: API key is required to proceed.", file=sys.stderr)
        return 1

    try:
        client = genai.Client(api_key=api_key)
    except Exception as error:
        print(f"Error initializing Google AI client: {error}", file=sys.stderr)
        return 1

    cleanup_functions = {
        "batches": (cleanup_batches,),
        "environments": (cleanup_environments,),
        "all": (cleanup_batches, cleanup_environments),
    }
    successful = True
    for cleanup in cleanup_functions[args.resource]:
        if not cleanup(client):
            successful = False

    print("Done!")
    return 0 if successful else 1


if __name__ == "__main__":
    sys.exit(main())
