"""
twikit 兼容性补丁。

在 AstrBot 的 Python 环境下运行此脚本以修补 twikit 中已知的 KeyError bug。
用法: python patch_twikit.py
"""
import re
import sys
import os


def patch_file(filepath: str, replacements: list):
    if not os.path.exists(filepath):
        print(f"  [SKIP] {filepath} not found")
        return False
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    for old, new in replacements:
        if old in content:
            content = content.replace(old, new)
            print(f"  [PATCH] Applied: {old[:60]}...")
        else:
            print(f"  [INFO] Already patched or not found: {old[:60]}...")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return True


def main():
    site_packages = None
    for p in sys.path:
        if p.endswith("site-packages"):
            site_packages = p
            break
    if not site_packages:
        print("Cannot find site-packages directory")
        return

    twikit_dir = os.path.join(site_packages, "twikit")
    if not os.path.exists(twikit_dir):
        print(f"twikit not found at {twikit_dir}")
        print("Please install twikit first: pip install twikit>=2.1.3")
        return

    print(f"Patching twikit at: {twikit_dir}")

    user_py = os.path.join(twikit_dir, "user.py")
    tweet_py = os.path.join(twikit_dir, "tweet.py")
    client_py = os.path.join(twikit_dir, "client", "client.py")

    patch_file(user_py, [
        ("['urls']", ".get('urls', [])"),
        ("['withheld_in_countries']", ".get('withheld_in_countries', [])"),
        ("['pinned_tweet_ids_str']", ".get('pinned_tweet_ids_str', [])"),
    ])

    patch_file(tweet_py, [
        ("['withheld_in_countries']", ".get('withheld_in_countries', [])"),
    ])

    patch_file(client_py, [
        (
            "reply_next_cursor = entries[-1]['content']['itemContent']['value']",
            "reply_next_cursor = entries[-1]['content'].get('itemContent', {}).get('value', '') if isinstance(entries[-1]['content'].get('itemContent'), dict) else ''",
        ),
    ])

    print("\nPatch complete. Try running: python -c \"from twikit import Client; print('twikit OK')\"")


if __name__ == "__main__":
    main()
