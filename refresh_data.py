#!/usr/bin/env python3
"""Refresh the published datasets (Kagi Small Web list, Kagi bangs).

Run weekly by trivet-data.timer. The service also calls ensure() at startup,
but a long-lived process would otherwise never pick up new domains or triggers.
"""
from __future__ import annotations

import bangs
import smallweb

if __name__ == "__main__":
    domains = smallweb.refresh()
    triggers = bangs.refresh()
    print(f"smallweb: {domains or 'refresh failed'} domains")
    print(f"bangs: {triggers or 'refresh failed'} triggers")
    raise SystemExit(0 if (domains and triggers) else 1)
