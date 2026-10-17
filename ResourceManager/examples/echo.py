"""Echo opaque input lines until EOF, without an application-specific protocol."""

import sys

for line in sys.stdin:
    sys.stdout.write(line)
    sys.stdout.flush()
