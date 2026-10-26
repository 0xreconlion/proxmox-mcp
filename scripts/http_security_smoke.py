"""Exercise private HTTP auth/header/body denials without exposing credentials."""

from __future__ import annotations

import argparse
import ssl
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--ca-file", required=True, type=Path)
    args = parser.parse_args()

    token = args.token_file.read_text(encoding="utf-8").strip()
    context = ssl.create_default_context(cafile=str(args.ca_file))
    base_headers = {"content-type": "application/json", "accept": "application/json"}
    valid_auth = {**base_headers, "authorization": f"Bearer {token}"}
    with httpx.Client(verify=context, timeout=5.0, follow_redirects=False) as client:
        results = {
            "missing_auth": client.post(args.url, headers=base_headers, content=b"{}").status_code,
            "invalid_auth": client.post(
                args.url,
                headers={**base_headers, "authorization": "Bearer deliberately-wrong"},
                content=b"{}",
            ).status_code,
            "invalid_host": client.post(
                args.url,
                headers={**valid_auth, "host": "attacker.invalid"},
                content=b"{}",
            ).status_code,
            "invalid_origin": client.post(
                args.url,
                headers={**valid_auth, "origin": "https://attacker.invalid"},
                content=b"{}",
            ).status_code,
            "oversized_body": client.post(
                args.url, headers=valid_auth, content=b"x" * 65_537
            ).status_code,
        }
    print(results)


if __name__ == "__main__":
    main()
