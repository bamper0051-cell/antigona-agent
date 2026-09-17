#!/usr/bin/env python3
from __future__ import annotations

import sys

from antigona.security import read_private_credential

if __name__=="__main__": read_private_credential(sys.argv[1])