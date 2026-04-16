"""
EPOCH-VISION-GUARD
Hybrid Text + Image Safety Guard

## Legal Disclosure
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
This is an independent open-source project.  
**No affiliation or compensation exists** with xAI, Anthropic, Google, OpenAI or any AI laboratory.  
The author owns the evaluated profile and repositories.  
All analysis and code are based solely on publicly available tools and APIs.  
This tool is released under the **MIT License** for defensive and research purposes only.  
It is **not** intended to assist in creating attacks or bypassing safety systems.

# WARNING: "This version is explicitly NOT intended for use in the European Union or EEA. It is not designed to meet EU AI Act or GDPR requirements. Any use in the EU/EEA is entirely at the user's own risk and responsibility."

Legal & Compliance 
Users are solely responsible for compliance with all applicable U.S. federal, state, and local laws. The author disclaims all liability. 
European Union / EEA
This software is explicitly not intended for placement on the EU market or use within the European Union or EEA. If used in the EU/EEA, the user must conduct their own full legal assessment and accept all liability. The tool is provided without any warranty of conformity with the EU AI Act or GDPR.
Rest of the World
Users bear full responsibility for compliance with all local laws and regulations.
Static Release Policy
This is a final, frozen version (April 2026). No maintenance, updates, or security patches will be provided.
Intended Use
Defensive safety research, artistic, technical, educational, and personal use only.

**Code Name:** EPOCH-VISION-G
**Version:** 1.0 (Static Release – April 2026)  
**Author:** ZZZ_EPOCHE  
**Date of Creation:** 2026-04-15

"""

import asyncio
import base64
import os
import sys
import time
import re
import uuid
import argparse
import json
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any

from openai import AsyncOpenAI
from PIL import Image

# ========================= CONFIG =========================
APP_NAME = "EPOCH-VISION-GUARD"

SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.heic', '.heif', '.tiff', '.tif', '.avif'}

# Vision-first models (most likely to succeed)
VISION_MODELS = [
    "grok-2-vision-latest",
    "grok-2-vision-1212",
    "grok-2-vision",
    "grok-vision-beta",
    "grok-4-1-fast-reasoning",
    "grok-4-1-fast-non-reasoning",
    "grok-4.20-reasoning",
    "grok-4.20-non-reasoning",
    "grok-beta"
]

# Text-only fallback models
TEXT_MODELS = [
    "grok-4.20-reasoning",
    "grok-4.20-non-reasoning",
    "grok-4-1-fast-reasoning",
    "grok-4-1-fast-non-reasoning",
    "grok-4.20",
    "grok-beta"
]

DEFAULT_PROMPT = (
    "Analyze this image in rich, natural detail. "
    "Be insightful, accurate, and well-structured. "
    "Adapt the response to suit the content of the image."
)

PII_PATTERNS = [
    re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'),
    re.compile(r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b')
]

# Retry configuration
MAX_RETRIES = 3
BASE_BACKOFF = 1.5  # seconds

# Heartbeat interval (5.55 minutes)
HEARTBEAT_INTERVAL = 5.55 * 60  # 333 seconds


class EpochVisionGuard:
    """Main class managing all state and operations. No globals."""

    def __init__(self):
        self.session_id = f"evg_{uuid.uuid4().hex[:10]}"
        self.total_images = 0
        self.successful_analyses = 0
        self.failed_analyses = 0
        self.latencies: List[float] = []
        self.start_time = time.time()
        self.client: Optional[AsyncOpenAI] = None

    def redact_pii(self, text: str) -> Tuple[str, int]:
        count = 0
        for pattern in PII_PATTERNS:
            text, n = pattern.subn("[REDACTED]", text)
            count += n
        return text, count

    def get_image_description(self, image_path: Path) -> str:
        """Enhanced PIL-based description (zero extra deps)."""
        try:
            with Image.open(image_path) as img:
                w, h = img.size
                aspect = w / h if h != 0 else 1.0
                aspect_str = f"{aspect:.2f}:1"
                return (
                    f"Image technical details:\n"
                    f"- Resolution: {w} × {h} pixels\n"
                    f"- Aspect ratio: {aspect_str}\n"
                    f"- Format: {img.format or 'Unknown'}\n"
                    f"- Color mode: {img.mode}\n"
                    f"- File size: {image_path.stat().st_size / 1024:.1f} KB\n"
                    f"- Approximate DPI: {img.info.get('dpi', 'Unknown')}\n\n"
                )
        except Exception as e:
            return f"Could not extract image details: {e}\n"

    async def _call_api_with_retry(self, model: str, messages: Any, max_tokens: int = 1600) -> Any:
        """Retry + exponential backoff for rate limits / transient errors."""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=0.6
                )
                return response
            except Exception as e:
                error_str = str(e).lower()
                if attempt == MAX_RETRIES or ("rate limit" not in error_str and "429" not in error_str):
                    raise
                backoff = BASE_BACKOFF * (2 ** (attempt - 1))
                print(f"   ⚠️  Transient error (attempt {attempt}/{MAX_RETRIES}). Retrying in {backoff:.1f}s...")
                await asyncio.sleep(backoff)
        raise RuntimeError("Max retries exceeded")

    async def analyze_image(self, image_path: Path, custom_prompt: Optional[str] = None, silent: bool = False) -> Dict[str, Any]:
        """Analyze one image with vision-first → text fallback."""
        start = time.time()
        self.total_images += 1

        if not silent:
            print(f"\n🔍 [{self.total_images}] Analyzing: {image_path.name}")

        try:
            with Image.open(image_path) as img:
                if not silent:
                    print(f"   Resolution: {img.size[0]}×{img.size[1]} • Format: {img.format}")

            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")

            prompt = custom_prompt or DEFAULT_PROMPT

            # === VISION MODE FIRST ===
            success = False
            result_text = None
            used_model = None

            for model in VISION_MODELS:
                if not silent:
                    print(f"   Trying vision model: {model}...")
                try:
                    response = await self._call_api_with_retry(
                        model=model,
                        messages=[{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                            ]
                        }]
                    )
                    result_text = response.choices[0].message.content or "No response received."
                    used_model = model
                    success = True
                    break
                except Exception as e:
                    if not silent:
                        print(f"   → Failed: {str(e)[:100]}...")
                    if "model not found" in str(e).lower() or "invalid argument" in str(e).lower():
                        continue
                    else:
                        break

            # === TEXT FALLBACK ===
            if not success:
                if not silent:
                    print("   No vision model succeeded → switching to text fallback...")
                img_desc = self.get_image_description(image_path)
                full_prompt = f"{img_desc}\n{prompt}"

                for model in TEXT_MODELS:
                    if not silent:
                        print(f"   Trying text model: {model}...")
                    try:
                        response = await self._call_api_with_retry(
                            model=model,
                            messages=[{"role": "user", "content": full_prompt}]
                        )
                        result_text = response.choices[0].message.content or "No response received."
                        used_model = f"{model} (text-fallback)"
                        success = True
                        break
                    except Exception as e:
                        if not silent:
                            print(f"   → Failed: {str(e)[:100]}...")
                        continue

            if not success:
                self.failed_analyses += 1
                if not silent:
                    print("   ❌ All models failed for this image.")
                return {"status": "failed", "path": str(image_path)}

            # Success path
            redacted_text, redacted_count = self.redact_pii(result_text)
            if redacted_count > 0 and not silent:
                print(f"   → Redacted {redacted_count} PII items")

            latency = (time.time() - start) * 1000
            self.latencies.append(latency)
            self.successful_analyses += 1

            if not silent:
                print("\n" + "═" * 90)
                print(f"📄 Analysis — {image_path.name} (using {used_model})")
                print("═" * 90)
                print(redacted_text.strip())
                print("═" * 90)
                print(f"✅ Completed in {int(latency)}ms\n")

            return {
                "status": "success",
                "path": str(image_path),
                "model": used_model,
                "analysis": redacted_text.strip(),
                "latency_ms": latency,
                "pii_redacted": redacted_count
            }

        except Exception as e:
            self.failed_analyses += 1
            if not silent:
                print(f"   ❌ Unexpected error: {str(e)[:150]}")
            return {"status": "failed", "path": str(image_path), "error": str(e)}


async def heartbeat_task(guard: EpochVisionGuard):
    """Heartbeat every 5.55 minutes (less spammy)."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        runtime = (time.time() - guard.start_time) / 60
        avg = sum(guard.latencies) / len(guard.latencies) if guard.latencies else 0
        print(f"❤️  HEARTBEAT | {APP_NAME} | Uptime: {runtime:.1f}min | "
              f"Images: {guard.total_images} | Success: {guard.successful_analyses} | Avg: {avg:.0f}ms")


def print_welcome(guard: EpochVisionGuard):
    print(f"\n🖼️  {APP_NAME} — Grok Image Analyzer")
    print(f"   Session ID: {guard.session_id}\n")


def print_final_report(guard: EpochVisionGuard):
    runtime = (time.time() - guard.start_time) / 60
    avg = sum(guard.latencies) / len(guard.latencies) if guard.latencies else 0

    print("\n" + "═" * 85)
    print(f"📊 {APP_NAME} Session Complete | ID: {guard.session_id}")
    print("═" * 85)
    print(f"   Images Processed : {guard.total_images}")
    print(f"   Successful       : {guard.successful_analyses}")
    print(f"   Failed           : {guard.failed_analyses}")
    print(f"   Average Latency  : {avg:.1f} ms")
    print(f"   Total Runtime    : {runtime:.2f} minutes")
    print("═" * 85)
    print("✅ Thank you for using EPOCH-VISION-GUARD\n")


async def main():
    parser = argparse.ArgumentParser(description="EPOCH-VISION-GUARD — Grok Vision Analyzer")
    parser.add_argument("paths", nargs="*", help="Image files or folders to analyze")
    parser.add_argument("--prompt", "-p", type=str, help="Custom analysis prompt")
    parser.add_argument("--interactive", "-i", action="store_true", help="Force interactive mode")
    parser.add_argument("--silent", "-s", action="store_true", help="Silent mode (no console output except final report)")
    parser.add_argument("--json", "-j", action="store_true", help="Output results as JSON")
    parser.add_argument("--folder", action="store_true", help="Recursively scan folders (if paths are given)")
    args = parser.parse_args()

    guard = EpochVisionGuard()

    if not os.getenv("XAI_API_KEY"):
        print("❌ XAI_API_KEY environment variable is not set.")
        print("   Export it first: export XAI_API_KEY=sk-...")
        sys.exit(1)

    guard.client = AsyncOpenAI(
        api_key=os.getenv("XAI_API_KEY"),
        base_url="https://api.x.ai/v1"
    )

    if not args.silent:
        print_welcome(guard)
        asyncio.create_task(heartbeat_task(guard))

    # Collect all image paths
    image_paths: List[Path] = []
    for p_str in args.paths:
        p = Path(p_str).expanduser().resolve()
        if p.is_dir() and args.folder:
            image_paths.extend([f for f in p.rglob("*") if f.suffix.lower() in SUPPORTED_EXTENSIONS])
        elif p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
            image_paths.append(p)

    # Interactive mode fallback
    if not image_paths or args.interactive:
        if not args.silent:
            print("\n📋 Drag & drop images or paste full paths (empty line = done).")
        while len(image_paths) < 999:  # effectively unlimited
            line = input("→ ").strip() if not args.silent else ""
            if not line:
                break
            p = Path(line).expanduser().resolve()
            if p.exists() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
                image_paths.append(p)
                if not args.silent:
                    print(f"   ✅ Added: {p.name}")

    if not image_paths:
        if not args.silent:
            print("No images provided.")
        print_final_report(guard)
        return

    custom_prompt = args.prompt

    results = []
    for path in image_paths:
        result = await guard.analyze_image(path, custom_prompt, silent=args.silent)
        if args.json:
            results.append(result)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_final_report(guard)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n🛑 Stopped by user.")
    except Exception as e:
        print(f"\nUnexpected error: {e}")
