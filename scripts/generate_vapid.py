from __future__ import annotations

import argparse
import base64
from pathlib import Path

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid


def build_env_lines(subject: str) -> list[str]:
    vapid = Vapid()
    vapid.generate_keys()

    public_bytes = vapid.public_key.public_bytes(
        encoding=Encoding.X962,
        format=PublicFormat.UncompressedPoint,
    )
    public_key = base64.urlsafe_b64encode(public_bytes).decode().rstrip("=")
    private_key = vapid.private_pem().decode("utf-8").replace("\n", "\\n")

    return [
        f"VAPID_PUBLIC_KEY={public_key}",
        f"VAPID_PRIVATE_KEY={private_key}",
        f"VAPID_SUBJECT={subject}",
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate VAPID keys as .env-ready lines.")
    parser.add_argument(
        "--subject",
        default="mailto:pocketkid@example.com",
        help="VAPID subject claim (default: mailto:pocketkid@example.com)",
    )
    parser.add_argument(
        "--write-env",
        action="store_true",
        help="Write/replace VAPID values directly into .env",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to env file used with --write-env (default: .env)",
    )
    return parser.parse_args()


def write_env_file(env_path: Path, new_lines: list[str]) -> None:
    keys_to_replace = {"VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_PRIVATE_KEY_B64", "VAPID_SUBJECT"}

    existing_lines: list[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    filtered_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            filtered_lines.append(line)
            continue

        key = stripped.split("=", 1)[0].strip()
        if key in keys_to_replace:
            continue
        filtered_lines.append(line)

    if filtered_lines and filtered_lines[-1].strip():
        filtered_lines.append("")

    output_lines = filtered_lines + new_lines
    env_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    lines = build_env_lines(args.subject)

    if args.write_env:
        env_path = Path(args.env_file)
        write_env_file(env_path, lines)
        print(f"Updated {env_path} with VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY and VAPID_SUBJECT")
        return

    for line in lines:
        print(line)


if __name__ == "__main__":
    main()
