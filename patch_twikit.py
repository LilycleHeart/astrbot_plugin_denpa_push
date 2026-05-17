"""
twikit 兼容性补丁。

在 AstrBot 的 Python 环境下运行此脚本以修补 twikit 中已知的 KeyError bug。
用法: python patch_twikit.py
"""
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


def _find_twikit_dir():
    # Method 1: find twikit via import
    try:
        import twikit
        d = os.path.dirname(twikit.__file__)
        if os.path.isdir(d):
            return d
    except Exception:
        pass
    # Method 2: search sys.path for site-packages
    for p in sys.path:
        if p.endswith("site-packages"):
            d = os.path.join(p, "twikit")
            if os.path.isdir(d):
                return d
    # Method 3: site.getsitepackages
    try:
        import site
        for sp in site.getsitepackages():
            d = os.path.join(sp, "twikit")
            if os.path.isdir(d):
                return d
    except Exception:
        pass
    return None


def main():
    twikit_dir = _find_twikit_dir()
    if not twikit_dir:
        print("Cannot find twikit directory")
        return

    print(f"Patching twikit at: {twikit_dir}")

    user_py = os.path.join(twikit_dir, "user.py")
    tweet_py = os.path.join(twikit_dir, "tweet.py")
    client_py = os.path.join(twikit_dir, "client", "client.py")

    patch_file(user_py, [
        ("['urls']", ".get('urls', [])"),
        ("['withheld_in_countries']", ".get('withheld_in_countries', [])"),
        ("['pinned_tweet_ids_str']", ".get('pinned_tweet_ids_str', [])"),
        # .get('urls') without default → returns None if key missing
        (".get('url', {}).get('urls')", ".get('url', {}).get('urls', [])"),
    ])

    patch_file(tweet_py, [
        ("['withheld_in_countries']", ".get('withheld_in_countries', [])"),
    ])
    # 下方 tweet.py 修改项需手动编辑：
    #   entity_set.get('urls') → entity_set.get('urls', [])
    #   legacy['entities'].get('urls') → legacy['entities'].get('urls', [])
    #   legacy['entities'].get('media') → legacy['entities'].get('media', [])

    patch_file(client_py, [
        (
            "reply_next_cursor = entries[-1]['content']['itemContent']['value']",
            "reply_next_cursor = entries[-1]['content'].get('itemContent', {}).get('value', '') if isinstance(entries[-1]['content'].get('itemContent'), dict) else ''",
        ),
    ])

    print("\nPatch complete.")


if __name__ == "__main__":
    main()
