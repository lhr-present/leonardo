#!/usr/bin/env python3
"""
sync_predictions.py — Merge prediction_tracker + leonardo prediction stores
=============================================================================
Merges ~/prediction_tracker/predictions.json and ~/leonardo/predictions.json
into a single master file at ~/leonardo/predictions.json, then creates a
symlink at ~/prediction_tracker/predictions.json pointing to the master.

Deduplication: picks with the same (match, market, selection, timestamp[:10])
are treated as the same pick — the more-settled version wins.

Run once:  python3 sync_predictions.py
Run again: idempotent — safe to re-run at any time.
"""

import json
import os
import sys

TRACKER_FILE  = os.path.expanduser("~/prediction_tracker/predictions.json")
LEONARDO_FILE = os.path.expanduser("~/leonardo/predictions.json")
BACKUP_SUFFIX = ".bak"


def _load(path: str) -> list:
    if not os.path.exists(path) or os.path.islink(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as exc:
        print(f"[sync] Warning: could not load {path}: {exc}")
        return []


def _dedup_key(p: dict) -> str:
    """Stable dedup key: match + market + selection + date."""
    return "|".join([
        str(p.get("match", "")).lower().strip(),
        str(p.get("market", "")).lower().strip(),
        str(p.get("selection", "")).lower().strip(),
        str(p.get("timestamp", p.get("kickoff_utc", "")))[:10],
    ])


def merge() -> list:
    """
    Merge picks from both files. Re-number IDs sequentially.
    For duplicates, keep the record that has settled=True (or whichever is newer).
    """
    tracker_picks  = _load(TRACKER_FILE)
    leonardo_picks = _load(LEONARDO_FILE)

    seen: dict[str, dict] = {}

    def _add(p: dict) -> None:
        key = _dedup_key(p)
        if key not in seen:
            seen[key] = p
        else:
            existing = seen[key]
            # Prefer settled record; if both settled, prefer more recent
            if p.get("settled") and not existing.get("settled"):
                seen[key] = p
            elif p.get("settled") == existing.get("settled"):
                # keep the one with a moltbook_post_id if present
                if p.get("moltbook_post_id") and not existing.get("moltbook_post_id"):
                    seen[key] = p

    # tracker_picks have priority (they're settled)
    for p in tracker_picks:
        _add(p)
    for p in leonardo_picks:
        _add(p)

    # Sort by timestamp, then re-number IDs from 1
    merged = sorted(seen.values(), key=lambda p: p.get("timestamp", ""))
    for i, p in enumerate(merged, start=1):
        p["id"] = i

    return merged


def main() -> None:
    print("[sync] Loading predictions from both files…")
    merged = merge()
    print(f"[sync] Merged: {len(merged)} unique pick(s)")

    # Backup existing leonardo/predictions.json if it has content
    if os.path.exists(LEONARDO_FILE) and not os.path.islink(LEONARDO_FILE):
        try:
            existing = json.load(open(LEONARDO_FILE))
            if existing:
                bak = LEONARDO_FILE + BACKUP_SUFFIX
                os.replace(LEONARDO_FILE, bak)
                print(f"[sync] Backed up leonardo/predictions.json → {bak}")
        except Exception:
            pass

    # Write master to ~/leonardo/predictions.json
    with open(LEONARDO_FILE, "w") as f:
        json.dump(merged, f, indent=2)
    print(f"[sync] Wrote master: {LEONARDO_FILE}")

    # Create symlink: prediction_tracker/predictions.json → master
    if os.path.islink(TRACKER_FILE):
        # Already a symlink — check it points to the right place
        target = os.readlink(TRACKER_FILE)
        if target == LEONARDO_FILE or os.path.abspath(
            os.path.join(os.path.dirname(TRACKER_FILE), target)
        ) == os.path.abspath(LEONARDO_FILE):
            print(f"[sync] Symlink already correct: {TRACKER_FILE} → {LEONARDO_FILE}")
            return
        else:
            os.remove(TRACKER_FILE)

    elif os.path.exists(TRACKER_FILE):
        # Real file — back it up
        bak = TRACKER_FILE + BACKUP_SUFFIX
        os.replace(TRACKER_FILE, bak)
        print(f"[sync] Backed up tracker/predictions.json → {bak}")

    os.symlink(LEONARDO_FILE, TRACKER_FILE)
    print(f"[sync] Symlink created: {TRACKER_FILE} → {LEONARDO_FILE}")

    # Verify
    test_load = json.load(open(TRACKER_FILE))
    print(f"[sync] Verified: reading from symlink returns {len(test_load)} picks ✓")

    # Print summary
    settled = [p for p in merged if p.get("settled")]
    wins    = [p for p in settled if p.get("result") == "WIN"]
    print(f"\n  Total picks   : {len(merged)}")
    print(f"  Settled       : {len(settled)}")
    print(f"  Wins          : {len(wins)}")
    print(f"  Win rate      : {len(wins)/len(settled)*100:.1f}%" if settled else "  Win rate      : N/A")


if __name__ == "__main__":
    main()
