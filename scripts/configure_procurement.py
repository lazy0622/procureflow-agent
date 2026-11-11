"""Create ignored local deployment settings without printing provider credentials."""

import argparse
import secrets
from pathlib import Path

IMAGE = "public.ecr.aws/skyvern/skyvern@sha256:75be52c5f16e78810c1eee16f8c1a098cc398b1279a6dc857c3e1e4adc0c70aa"
ROOT = Path(__file__).resolve().parents[1]


def read_settings(path: Path) -> dict[str, str]:
    """Read simple KEY=value dotenv entries, without executing shell expressions."""
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def configure(model_config: Path | None) -> None:
    """Preserve existing configuration or import the explicitly selected local source."""
    target = ROOT / ".env"
    if target.exists():
        print("Existing .env preserved; no secrets changed.")
        return
    if model_config is None:
        raise SystemExit("Supply --model-config with a local dotenv file containing the API key")
    source = read_settings(model_config)
    key = source.get("OPENAI_COMPATIBLE_API_KEY") or source.get("OPENAI_API_KEY")
    if not key:
        raise SystemExit("The selected file does not contain a supported API key setting")
    values = {
        "ENV": "local",
        "SECRET_KEY": secrets.token_urlsafe(48),
        "SKYVERN_TELEMETRY": "false",
        "ENABLE_OPENAI_COMPATIBLE": "true",
        "OPENAI_COMPATIBLE_MODEL_NAME": "deepseek-flash",
        "OPENAI_COMPATIBLE_API_KEY": key,
        "OPENAI_COMPATIBLE_API_BASE": source.get("OPENAI_COMPATIBLE_API_BASE")
        or source.get("OPENAI_API_BASE", "https://api.deepseek.com/v1"),
        "OPENAI_COMPATIBLE_SUPPORTS_VISION": "true",
        "OPENAI_COMPATIBLE_MAX_TOKENS": "8192",
        "LLM_KEY": "OPENAI_COMPATIBLE",
        "SECONDARY_LLM_KEY": "OPENAI_COMPATIBLE",
        "ENABLE_URL_VALIDATION": "false",
        "BROWSER_TYPE": "chromium-headless",
        "PROCUREMENT_POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "PROCUREMENT_SKYVERN_IMAGE": IMAGE,
    }
    target.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8"
    )
    print("Created local .env for deepseek-flash; credential values are not displayed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-config", type=Path)
    configure(parser.parse_args().model_config)
