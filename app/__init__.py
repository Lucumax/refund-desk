"""Refund-first approval agent for a PayPal merchant. Python 3.11, standard library only."""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENDOR = os.path.join(REPO_ROOT, "vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)  # makes `import approval_receipt` resolve to the vendored core
